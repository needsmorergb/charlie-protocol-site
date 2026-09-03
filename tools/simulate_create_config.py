"""Simulate `create_fee_sharing_config`, and read what the chain says happens.

Four questions this repository has so far answered from documents:

  1. does a coin arrive with a fee-sharing config, or must one be created?
     (`tools/sample_new_coins.py` samples the launch stream for that one;
     this tool takes a coin that HAS no config and creates one, which is the
     other half of the same answer.)
  2. who becomes the config's `admin`?
  3. what are its `initial_shareholders`, and is the one-shot
     `update_fee_shares_v2` still unspent immediately after creation?
  4. does creating a config move `bonding_curve.creator` to the config PDA?
     pump's error 6049 says "creator has been migrated to sharing config",
     which implies it, but implication is not measurement.

Nothing signs and nothing is sent. `simulateTransaction` with
`sigVerify: false` runs the real program against real mainnet state and hands
back the logs, the emitted events, AND -- this is what settles 3 and 4 --
the POST-SIMULATION BYTES of any account we name. The bonding curve's
`creator` field after the instruction is a measurement, not an inference.

Every account, every discriminator and every event layout is taken from the
programs' own on-chain Anchor IDLs, the same way `tools/simulate_distribute.py`
does it.

    python tools/simulate_create_config.py --auto            # pick a fresh coin
    python tools/simulate_create_config.py <mint>
    python tools/simulate_create_config.py <mint> --then-update

`--then-update` appends `update_fee_shares_v2` to the SAME transaction, after
the creation. If it succeeds, the one-shot was still unspent at that instant;
if it fails with the already-updated error, creation spent it.
"""

from __future__ import annotations

import argparse
import base64
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from indexer import enroll, pump                            # noqa: E402
from indexer.base58 import decode, encode, pubkey_bytes     # noqa: E402
from indexer.curve import find_program_address              # noqa: E402
from indexer.enroll import _compact_u16                     # noqa: E402
from indexer.rpc import DEFAULT_ENDPOINTS, RpcClient        # noqa: E402
from tools.idl_dump import read_idl                         # noqa: E402
from tools.sample_new_coins import decode_curve, endpoints_from, sample  # noqa: E402

SYSTEM_PROGRAM = "11111111111111111111111111111111"
RENT_SYSVAR = "SysvarRent111111111111111111111111111111111"
TOKEN_PROGRAM = "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"
ASSOCIATED_TOKEN_PROGRAM = "ATokenGPvbdGVxr1b2hvZbsiqW5xWH25efTNsLJA8knL"
WSOL_MINT = "So11111111111111111111111111111111111111112"
INCINERATOR = "1nc1nerator11111111111111111111111111111111"

# sha256("anchor:event")[:8] -- the discriminator of the self-CPI Anchor uses
# for `emit_cpi!`. Those events never reach the logs as `Program data:`; they
# arrive as an inner instruction whose data is this prefix then the event.
ANCHOR_CPI_EVENT = bytes.fromhex("e445a52e51cb9a1d")

# Names an IDL account list uses for things that are neither PDAs nor carry a
# fixed address in the IDL. Only consulted after `address`, the caller's
# context and the IDL's own PDA seeds have all failed.
WELL_KNOWN = {
    "system_program": SYSTEM_PROGRAM,
    "rent": RENT_SYSVAR,
    "token_program": TOKEN_PROGRAM,
    "associated_token_program": ASSOCIATED_TOKEN_PROGRAM,
    "pump_program": pump.PUMP_PROGRAM,
    "pump_amm_program": pump.PUMP_AMM_PROGRAM,
    "fee_share_program": pump.PUMP_FEE_SHARE_PROGRAM,
    "quote_mint": WSOL_MINT,
}


# -- IDL helpers ----------------------------------------------------------
def find_instruction(idls: dict[str, dict], name: str):
    for program_id, idl in idls.items():
        for entry in idl.get("instructions", []):
            if entry.get("name") == name:
                return program_id, idl, entry
    return None, None, None


def type_def(idl: dict, name: str) -> dict | None:
    for entry in idl.get("types", []):
        if entry.get("name") == name:
            return entry.get("type")
    return None


def error_name(idls: dict[str, dict], code: int, program_id: str | None = None) -> str:
    """The error, looked up in the IDL of the program that actually failed.

    Both programs number their errors from 6000, so a search that takes the
    first match reads pump's error list for a failure inside the fee-share
    program -- which is how a run reported "InvalidCreator" for an error the
    logs plainly called AmmAccountsRequiredForGraduatedCoin.
    """
    ordered = list(idls.items())
    if program_id:
        ordered.sort(key=lambda item: item[0] != program_id)
    for owner, idl in ordered:
        for entry in idl.get("errors", []):
            if entry.get("code") == code:
                return f"{entry.get('name')} - {entry.get('msg')}  [{owner[:8]}...]"
    return "not in either IDL's error list"


# -- borsh, driven by the IDL ---------------------------------------------
def _read(buf: bytes, off: int, ty, idl: dict):
    if isinstance(ty, str):
        if ty == "pubkey":
            return encode(buf[off : off + 32]), off + 32
        if ty == "bool":
            return bool(buf[off]), off + 1
        if ty == "string":
            length = int.from_bytes(buf[off : off + 4], "little")
            off += 4
            return buf[off : off + length].decode("utf-8", "replace"), off + length
        if ty in ("u8", "i8"):
            return buf[off], off + 1
        widths = {"u16": 2, "i16": 2, "u32": 4, "i32": 4, "u64": 8, "i64": 8, "u128": 16, "i128": 16}
        if ty in widths:
            width = widths[ty]
            signed = ty.startswith("i")
            return int.from_bytes(buf[off : off + width], "little", signed=signed), off + width
        raise ValueError(f"unsupported scalar {ty!r}")
    if isinstance(ty, dict):
        if "option" in ty:
            flag = buf[off]
            off += 1
            if not flag:
                return None, off
            return _read(buf, off, ty["option"], idl)
        if "vec" in ty:
            count = int.from_bytes(buf[off : off + 4], "little")
            off += 4
            items = []
            for _ in range(count):
                value, off = _read(buf, off, ty["vec"], idl)
                items.append(value)
            return items, off
        if "array" in ty:
            inner, count = ty["array"]
            items = []
            for _ in range(count):
                value, off = _read(buf, off, inner, idl)
                items.append(value)
            return items, off
        if "defined" in ty:
            spec = ty["defined"]
            name = spec if isinstance(spec, str) else spec.get("name")
            definition = type_def(idl, name)
            if definition is None:
                raise ValueError(f"no type definition for {name!r}")
            if definition.get("kind") == "struct":
                out = {}
                for field in definition.get("fields", []):
                    out[field["name"]], off = _read(buf, off, field["type"], idl)
                return out, off
            if definition.get("kind") == "enum":
                index = buf[off]
                variants = definition.get("variants", [])
                label = variants[index]["name"] if index < len(variants) else f"#{index}"
                return f"{label}({index})", off + 1
            raise ValueError(f"unsupported type kind for {name!r}")
    raise ValueError(f"unsupported type {ty!r}")


def decode_event(idls: dict[str, dict], payload: bytes):
    """`(name, fields)` for any event either IDL declares, or None."""
    for _program_id, idl in idls.items():
        for entry in idl.get("events", []):
            disc = bytes(entry.get("discriminator") or [])
            if not disc or payload[:8] != disc:
                continue
            name = entry.get("name")
            definition = type_def(idl, name)
            if definition is None:
                return name, {"_": "event has no type definition in the IDL"}
            off = 8
            fields = {}
            for field in definition.get("fields", []):
                try:
                    fields[field["name"]], off = _read(payload, off, field["type"], idl)
                except Exception as exc:                    # noqa: BLE001 - reported
                    fields[field["name"]] = f"<undecodable: {exc}>"
                    break
            return name, fields
    return None


# -- account resolution ---------------------------------------------------
def _seed_bytes(seed: dict, resolved: dict) -> bytes:
    kind = seed.get("kind")
    if kind == "const":
        return bytes(seed.get("value") or [])
    if kind in ("account", "arg"):
        path = seed.get("path")
        value = resolved.get(path)
        if value is None:
            raise KeyError(f"seed refers to unresolved {kind} {path!r}")
        if isinstance(value, (bytes, bytearray)):
            return bytes(value)
        return pubkey_bytes(value)
    raise KeyError(f"unsupported seed kind {kind!r}")


def resolve_accounts(instruction: dict, program_id: str, context: dict):
    """`[(name, address, signer, writable)]` in the IDL's own order.

    Seeded before the walk, not during it: an ATA's seeds name
    `token_program` and `quote_mint`, which appear LATER in the account list
    than the ATA itself, so a strictly positional resolver fails on an
    instruction whose account order is perfectly legal.
    """
    resolved = {}
    for account in instruction.get("accounts", []):
        name = account["name"]
        if account.get("address"):
            resolved[name] = account["address"]
        elif name in WELL_KNOWN:
            resolved[name] = WELL_KNOWN[name]
    resolved.update(context)
    metas = []
    for account in instruction.get("accounts", []):
        name = account["name"]
        fixed = account.get("address")
        if name == "program":
            address = program_id
        elif fixed:
            address = fixed
        elif account.get("pda"):
            pda = account["pda"]
            owner = program_id
            if pda.get("program"):
                spec = pda["program"]
                owner = encode(bytes(spec["value"])) if spec.get("value") else resolved[spec["path"]]
            seeds = [_seed_bytes(seed, resolved) for seed in pda.get("seeds", [])]
            address, _bump = find_program_address(seeds, owner)
        elif name in context:
            address = context[name]
        elif name in WELL_KNOWN:
            address = WELL_KNOWN[name]
        else:
            raise KeyError(f"cannot resolve account {name!r}")
        resolved[name] = address
        metas.append((name, address, bool(account.get("signer")), bool(account.get("writable"))))
    return metas


def encode_args(instruction: dict, idl: dict, overrides: dict) -> tuple[bytes, list[str]]:
    """Anchor's borsh encoding for the instruction's args, with empty/zero
    defaults for anything the caller did not supply. Every value used is
    reported, so a default is never mistaken for something the chain chose.
    """
    out = bytearray()
    notes = []
    for arg in instruction.get("args", []) or []:
        name = arg["name"]
        ty = arg["type"]
        value = overrides.get(name, "__default__")
        raw, note = _write(ty, value, idl)
        out += raw
        notes.append(f"{name}: {ty} = {note}")
    return bytes(out), notes


def _write(ty, value, idl: dict) -> tuple[bytes, str]:
    default = value == "__default__"
    if isinstance(ty, str):
        if ty == "pubkey":
            raw = b"\x00" * 32 if default else pubkey_bytes(value)
            return raw, "default all-zero pubkey" if default else str(value)
        if ty == "bool":
            flag = False if default else bool(value)
            return bytes([1 if flag else 0]), str(flag) + (" (default)" if default else "")
        if ty == "string":
            text = "" if default else str(value)
            body = text.encode()
            return len(body).to_bytes(4, "little") + body, repr(text)
        widths = {"u8": 1, "i8": 1, "u16": 2, "i16": 2, "u32": 4, "i32": 4,
                  "u64": 8, "i64": 8, "u128": 16, "i128": 16}
        if ty in widths:
            number = 0 if default else int(value)
            return number.to_bytes(widths[ty], "little", signed=ty.startswith("i")), str(number)
    if isinstance(ty, dict):
        if "option" in ty:
            if default or value is None:
                return b"\x00", "None (default)"
            body, note = _write(ty["option"], value, idl)
            return b"\x01" + body, f"Some({note})"
        if "vec" in ty:
            items = [] if default else list(value)
            out = bytearray(len(items).to_bytes(4, "little"))
            notes = []
            for item in items:
                body, note = _write(ty["vec"], item, idl)
                out += body
                notes.append(note)
            return bytes(out), f"[{', '.join(notes)}]" + (" (default empty)" if default else "")
        if "defined" in ty:
            spec = ty["defined"]
            name = spec if isinstance(spec, str) else spec.get("name")
            definition = type_def(idl, name) or {}
            if definition.get("kind") == "struct":
                out = bytearray()
                notes = []
                source = {} if default else dict(value)
                for field in definition.get("fields", []):
                    body, note = _write(
                        field["type"], source.get(field["name"], "__default__"), idl
                    )
                    out += body
                    notes.append(f"{field['name']}={note}")
                return bytes(out), f"{name}({', '.join(notes)})"
    raise ValueError(f"cannot encode argument of type {ty!r}")


# -- message --------------------------------------------------------------
def build_message(instructions, payer: str, blockhash: str) -> bytes:
    """A legacy message carrying one or more instructions.

    `instructions` is `[(program_id, metas, data)]` where metas is
    `[(name, address, signer, writable)]`.
    """
    merged: dict[str, list[bool]] = {payer: [True, True]}
    for program_id, metas, _data in instructions:
        for _name, address, signer, writable in metas:
            row = merged.setdefault(address, [False, False])
            row[0] = row[0] or signer
            row[1] = row[1] or writable
        merged.setdefault(program_id, [False, False])

    def rank(item):
        address, (signer, writable) = item
        if address == payer:
            return (-1, "")
        return (0 if signer and writable else 1 if signer else 2 if writable else 3, address)

    ordered = [address for address, _flags in sorted(merged.items(), key=rank)]
    index = {address: i for i, address in enumerate(ordered)}
    signers = [a for a in ordered if merged[a][0]]
    readonly_signed = sum(1 for a in signers if not merged[a][1])
    readonly_unsigned = sum(1 for a in ordered if not merged[a][0] and not merged[a][1])

    out = bytearray()
    out.append(len(signers))
    out.append(readonly_signed)
    out.append(readonly_unsigned)
    out += _compact_u16(len(ordered))
    for address in ordered:
        out += pubkey_bytes(address)
    out += pubkey_bytes(blockhash)
    out += _compact_u16(len(instructions))
    for program_id, metas, data in instructions:
        out.append(index[program_id])
        out += _compact_u16(len(metas))
        for _name, address, _signer, _writable in metas:
            out.append(index[address])
        out += _compact_u16(len(data))
        out += data
    return bytes(out), len(signers), ordered


def describe(program_id: str, idl: dict, instruction: dict) -> str:
    lines = [
        f"instruction    {instruction['name']}",
        f"  program      {program_id}",
        f"  disc         {bytes(instruction['discriminator']).hex()}",
        f"  args         {[(a['name'], a['type']) for a in instruction.get('args', [])] or 'NONE'}",
        "  accounts",
    ]
    for position, account in enumerate(instruction.get("accounts", [])):
        flags = ("s" if account.get("signer") else "-") + ("w" if account.get("writable") else "-")
        detail = "  OPTIONAL" if account.get("optional") else ""
        if account.get("address"):
            detail = f"  = {account['address']}"
        elif account.get("pda"):
            seeds = []
            for seed in account["pda"].get("seeds", []):
                if seed.get("kind") == "const":
                    value = bytes(seed.get("value") or [])
                    try:
                        seeds.append(repr(value.decode()))
                    except UnicodeDecodeError:
                        seeds.append(value.hex())
                else:
                    seeds.append(f"{seed.get('kind')}:{seed.get('path')}")
            under = account["pda"].get("program")
            owner = ""
            if under and under.get("value"):
                owner = f" under {encode(bytes(under['value']))[:8]}..."
            detail = f"  [pda seeds {seeds}{owner}]"
        lines.append(f"    {position:>2} {flags} {account['name']}{detail}")
    return "\n".join(lines)


def describe_event(idls: dict[str, dict], name: str) -> str:
    for _program_id, idl in idls.items():
        for entry in idl.get("events", []):
            if entry.get("name") != name:
                continue
            definition = type_def(idl, name) or {}
            fields = [(f["name"], f["type"]) for f in definition.get("fields", [])]
            return (
                f"event          {name}\n"
                f"  disc         {bytes(entry.get('discriminator') or []).hex()}\n"
                f"  fields       {fields}"
            )
    return f"event          {name}: NOT DECLARED in either IDL"


# -- the run --------------------------------------------------------------
def simulate(rpc, idls, mint: str, *, payer: str | None, then_update: bool,
             update_target: str | None, as_account: str | None = None,
             pool: str | None = None) -> int:
    curve_account = rpc.accounts([pump.bonding_curve(mint)])[0]
    curve = decode_curve(curve_account)
    creator = curve["creator"]
    creator_account = rpc.accounts([creator])[0]
    creator_owner = (creator_account or {}).get("owner")
    config_pda = enroll.sharing_config_address(mint)

    print(f"coin           {mint}")
    print(f"  bonding curve  {pump.bonding_curve(mint)}")
    print(f"  creator        {creator}")
    print(f"  creator owner  {creator_owner}"
          f"   lamports={(creator_account or {}).get('lamports')}")
    print(f"  has config     {creator_owner == pump.PUMP_FEE_SHARE_PROGRAM}")
    print(f"  graduated      {curve['complete']}"
          "   (a graduated coin needs its AMM pool passed -- fee-share error 6019)")
    print(f"  config pda     {config_pda}  exists={rpc.accounts([config_pda])[0] is not None}")

    program_id, idl, instruction = find_instruction(idls, "create_fee_sharing_config")
    if instruction is None:
        print("  create_fee_sharing_config is in NEITHER on-chain IDL")
        return 1
    program_id_for_pool = program_id

    # `pool` carries neither a fixed address nor PDA seeds in the IDL, and the
    # fee-share program's own error 6019 says AMM accounts are required for
    # GRADUATED coins -- so for a coin still on its bonding curve there is no
    # pool, and Anchor's convention for an absent optional account is the
    # program's own id. Overridable, and reported either way.
    identity = as_account or creator
    context = {
        "mint": mint,
        "bonding_curve.creator": creator,
        "creator": identity,
        "payer": identity,
        "user": identity,
        "admin": identity,
        "authority": identity,
        "signer": identity,
        "fee_payer": identity,
        "pool": pool or program_id_for_pool,
    }
    try:
        metas = resolve_accounts(instruction, program_id, context)
    except KeyError as exc:
        print(f"  UNRESOLVED     {exc}")
        return 1
    print(f"  acting as      {identity}"
          + ("  (the coin's creator)" if identity == creator else "  (NOT the creator)"))
    print(f"  pool account   {context['pool']}"
          + ("  = the program id, Anchor's absent-optional-account convention"
             if context["pool"] == program_id_for_pool else ""))
    data_args, notes = encode_args(instruction, idl, {})
    data = bytes(instruction["discriminator"]) + data_args

    print("  resolved accounts")
    for position, (name, address, signer, writable) in enumerate(metas):
        flags = ("s" if signer else "-") + ("w" if writable else "-")
        print(f"    {position:>2} {flags} {name:<28} {address}")
    print(f"  args encoded   {notes or 'no args'}")

    program_ixs = [(program_id, metas, data)]

    if then_update:
        update_program, update_idl, update_ix = find_instruction(idls, "update_fee_shares_v2")
        if update_ix is None:
            print("  update_fee_shares_v2 is in NEITHER IDL -- cannot chain it")
        else:
            target = update_target or creator
            shares = [{"address": target, "bps": 10_000}]
            arg_name = (update_ix.get("args") or [{}])[0].get("name")
            overrides = {arg_name: shares} if arg_name else {}
            # The update's own account list, resolved the same IDL-driven way.
            update_context = dict(context)
            update_context["sharing_config"] = config_pda
            update_context["config"] = config_pda
            try:
                update_metas = resolve_accounts(update_ix, update_program, update_context)
                update_data_args, update_notes = encode_args(update_ix, update_idl, overrides)
            except (KeyError, ValueError) as exc:
                print(f"  chained update UNRESOLVED {exc}")
            else:
                update_data = bytes(update_ix["discriminator"]) + update_data_args
                # 6013 NotEnoughRemainingAccounts: the program checks the new
                # shareholders' accounts as Anchor remaining accounts, in the
                # order of the vec, exactly as distribute_creator_fees does.
                update_metas = update_metas + [
                    (f"shareholder[{position}]", share["address"], False, True)
                    for position, share in enumerate(shares)
                ]
                program_ixs.append((update_program, update_metas, update_data))
                print(f"  chained        update_fee_shares_v2 -> {target} at 100%")
                print(f"  update args    {update_notes}")

    fee_payer = payer or identity
    blockhash = rpc.call("getLatestBlockhash", [{"commitment": "finalized"}])["value"]["blockhash"]
    message, signature_count, ordered = build_message(program_ixs, fee_payer, blockhash)
    unsigned = _compact_u16(signature_count) + b"\x00" * (64 * signature_count) + message

    watch = [address for address in (config_pda, pump.bonding_curve(mint), creator)
             if address in ordered]
    print(f"  fee payer      {fee_payer}  ({signature_count} blank signatures)")

    result = rpc.call(
        "simulateTransaction",
        [
            base64.b64encode(unsigned).decode(),
            {
                "encoding": "base64",
                "sigVerify": False,
                "replaceRecentBlockhash": True,
                "commitment": "processed",
                "innerInstructions": True,
                "accounts": {"encoding": "base64", "addresses": watch},
            },
        ],
    )
    value = (result or {}).get("value") or {}
    err = value.get("err")
    logs = value.get("logs") or []

    print()
    if err is None:
        print("  SIMULATION     OK -- the program accepted this")
    else:
        print(f"  SIMULATION     FAILED err={err}")
        if isinstance(err, dict):
            entry = err.get("InstructionError") or [None, None]
            custom = entry[1]
            if isinstance(custom, dict) and "Custom" in custom:
                code = custom["Custom"]
                index = entry[0]
                failing = (
                    program_ixs[index][0]
                    if isinstance(index, int) and index < len(program_ixs)
                    else None
                )
                print(f"  instruction    #{index}   in {failing}   custom error {code}")
                print(f"  error          {error_name(idls, code, failing)}")

    print("  --- logs, verbatim ---")
    for line in logs:
        print(f"    | {line}")

    print("  --- events ---")
    events = []
    for line in logs:
        marker = "Program data: "
        if marker in line:
            payload = line.split(marker, 1)[1].strip()
            try:
                events.append(("log", base64.b64decode(payload)))
            except Exception:                               # noqa: BLE001
                pass
    for group in value.get("innerInstructions") or []:
        for entry in group.get("instructions") or []:
            raw = entry.get("data")
            if not raw:
                continue
            try:
                blob = decode(raw)
            except ValueError:
                continue
            if blob[:8] == ANCHOR_CPI_EVENT:
                events.append(("cpi", blob[8:]))
    if not events:
        print("    none emitted")
    for source, payload in events:
        decoded = decode_event(idls, payload)
        if decoded is None:
            print(f"    [{source}] unknown event, disc={payload[:8].hex()} "
                  f"len={len(payload)} raw={base64.b64encode(payload).decode()[:120]}")
            continue
        name, fields = decoded
        print(f"    [{source}] {name}")
        for key, item in fields.items():
            print(f"        {key:<22} {item}")

    print("  --- account state AFTER the simulated instruction ---")
    for address, account in zip(watch, value.get("accounts") or []):
        if account is None:
            print(f"    {address}  does not exist after simulation")
            continue
        owner = account.get("owner")
        blob = base64.b64decode(account["data"][0]) if account.get("data") else b""
        print(f"    {address}\n      owner  {owner}  bytes {len(blob)} "
              f"lamports {account.get('lamports')}")
        if address == pump.bonding_curve(mint) and len(blob) >= 81:
            after = encode(blob[49:81])
            print(f"      bonding_curve.creator BEFORE  {creator}")
            print(f"      bonding_curve.creator AFTER   {after}")
            print(f"      migrated to the config pda    {after == config_pda}")
        if owner == pump.PUMP_FEE_SHARE_PROGRAM:
            try:
                config = pump.decode_sharing_config(
                    address,
                    {"owner": owner, "data": account["data"], "space": len(blob)},
                )
            except pump.DecodeError as exc:
                print(f"      config undecodable: {exc}")
            else:
                print(f"      version {config.version}  status {config.status}")
                print(f"      admin   {config.admin}")
                print(f"      admin_revoked {config.admin_revoked}")
                print(f"      shareholders {len(config.shareholders)}")
                for holder, bps in config.shareholders:
                    print(f"        {bps / 100:>6.2f}%  {holder}")
                print(f"      raw header hex {blob[:80].hex()}")
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("mints", nargs="*")
    parser.add_argument("--rpc")
    parser.add_argument("--auto", action="store_true",
                        help="pick a freshly launched coin that has no config")
    parser.add_argument("--auto-limit", type=int, default=8)
    parser.add_argument("--auto-scan", type=int, default=30)
    parser.add_argument("--auto-delay", type=float, default=1.0)
    parser.add_argument("--payer", help="fee payer; defaults to the acting identity")
    parser.add_argument("--as", dest="as_account",
                        help="act as this address instead of the coin's creator")
    parser.add_argument("--pool", help="the AMM pool account, for a graduated coin")
    parser.add_argument("--raw-idl", action="store_true",
                        help="print the raw IDL JSON for the instruction")
    parser.add_argument("--then-update", action="store_true",
                        help="append update_fee_shares_v2 to the same transaction")
    parser.add_argument("--update-target", help="the 100%% share destination for the update")
    args = parser.parse_args(argv)

    rpc = RpcClient(endpoints_from(args.rpc))
    idls = {}
    for program_id in (pump.PUMP_PROGRAM, pump.PUMP_FEE_SHARE_PROGRAM):
        try:
            idls[program_id] = read_idl(rpc, program_id)
        except Exception as exc:                            # noqa: BLE001 - reported
            print(f"{program_id}: IDL unreadable {exc}")

    program_id, idl, instruction = find_instruction(idls, "create_fee_sharing_config")
    if instruction is not None:
        print(describe(program_id, idl, instruction))
        print(describe_event(idls, "CreateFeeSharingConfigEvent"))
        if args.raw_idl:
            import json
            print(json.dumps(instruction, indent=1))
            for name in ("Shareholder", "ConfigStatus", "SharingConfig"):
                print(f"type {name}: {json.dumps(type_def(idl, name))}")
    update_program, update_idl, update_ix = find_instruction(idls, "update_fee_shares_v2")
    if update_ix is not None:
        print(describe(update_program, update_idl, update_ix))
    print()

    mints = list(args.mints)
    if args.auto:
        print("picking a freshly launched coin with no sharing config")
        rows = sample(rpc, want=args.auto_limit, scan=args.auto_scan, delay=args.auto_delay)
        candidates = [
            row for row in rows
            if row.get("route") == "plain_creator"
            and row.get("creator_lamports", 0) > 0
            and not row["curve"]["complete"]
        ]
        print(f"  config-less, un-graduated, funded creator: {len(candidates)} of {len(rows)}")
        candidates.sort(key=lambda row: row.get("creator_lamports", 0), reverse=True)
        if not candidates:
            print("  no config-less launch found in this window")
        else:
            chosen = candidates[0]
            print(f"  chose {chosen['mint']} (creator holds "
                  f"{chosen['creator_lamports'] / 1e9:.4f} SOL)")
            mints.append(chosen["mint"])
        print()

    status = 0
    for mint in mints:
        try:
            status |= simulate(
                rpc, idls, mint,
                payer=args.payer,
                then_update=args.then_update,
                update_target=args.update_target,
                as_account=args.as_account,
                pool=args.pool,
            )
        except Exception as exc:                            # noqa: BLE001 - reported
            print(f"{mint}\n  unreadable     {type(exc).__name__}: {exc}")
            status = 1
        print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
