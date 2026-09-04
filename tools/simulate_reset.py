"""Can a coin's OWN admin escape a fee split after committing to it?

FEE-ROUTING relies on `admin_revoked = true` being a one-way door: once a
config's split is spent (the one-shot `update_fee_shares_v2`, error 6024
"Reward split can only be updated once"), the routing is meant to be
permanent. But the fee-share program also carries:

    reset_fee_sharing_config        (0a02b65f107f81ba)
    reset_fee_sharing_config_v2     (a9f511d15e5bf880)
    transfer_fee_sharing_authority  (ca0a4bc8a422d260)
    revoke_fee_sharing_authority    (12e99e27b9cf3a68)

whose accounts include a `new_admin` and a signer `authority`. In
`update_fee_shares_v2` that same `authority` field IS the config admin; a
stranger gets 6016 NotAuthorized. If the admin can `reset` a config whose
admin_revoked is true, the one-shot is RE-ARMED and the toll is escapable.

This settles it by simulation, never by assertion. For a coin whose config
already has admin_revoked true (every freshly launched fee-shared coin does),
each instruction is run as three callers:

    (admin)    the config's own admin
    (stranger) a random unrelated address
    (global-*) the pump Global authorities, read out of the on-chain Global
               account (authority, set_creator_authority,
               admin_set_creator_authority)

reset_fee_sharing_config and _v2 take their FULL account lists from the
DEPLOYED on-chain IDL. transfer_/revoke_fee_sharing_authority have EMPTY
account lists in the deployed IDL but populated ones in pump's older
published IDL -- so those two are built from the published account list
(same discriminator) and may now answer 6023 DeprecatedInstruction. Not
assumed; simulated.

A successful reset is documented to "distribute pending fees first" and
touches pump_creator_vault. So a successful caller is re-run with the vault
funded above pump's floor and the outgoing shareholders as remaining
accounts, and every relevant lamport balance is measured before against
after -- if a dev can reset, does the dev also walk with what accrued?

Nothing signs and nothing is sent. `sigVerify: false` runs the real programs
against real mainnet state and hands back the runtime's own answer.

    python tools/simulate_reset.py --auto
    python tools/simulate_reset.py <mint> [<mint> ...]

Every account, discriminator, argument and event layout comes from the two
programs' own Anchor IDLs -- the DEPLOYED ones for reset, and pump's older
published pump_fees.json (idl/pump_fees.json) for the two that the deployed
IDL emptied.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from indexer import pump                                    # noqa: E402
from indexer.base58 import encode, pubkey_bytes             # noqa: E402
from indexer.curve import find_program_address              # noqa: E402
from indexer.rpc import RpcClient                           # noqa: E402
from tools.idl_dump import read_idl                         # noqa: E402
from tools.sample_new_coins import endpoints_from, sample   # noqa: E402
from tools.simulate_create_config import (                  # noqa: E402
    _read,
    build_message,
    decode_event,
    encode_args,
    error_name,
    find_instruction,
    resolve_accounts,
    type_def,
)
from tools.simulate_collector_payout import (               # noqa: E402
    creator_vault,
    inspect,
    read_accounts,
    transfer_instruction,
)

SYSTEM_PROGRAM = "11111111111111111111111111111111"
WSOL_MINT = "So11111111111111111111111111111111111111112"
TOKEN_PROGRAM = "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"
ASSOCIATED_TOKEN_PROGRAM = "ATokenGPvbdGVxr1b2hvZbsiqW5xWH25efTNsLJA8knL"

# A funded burner used only as fee payer and top-up source. sigVerify is off,
# so the runtime never checks a signature -- only the message's signer FLAG,
# which the system program reads. ~179 SOL, belonging to nobody.
DEFAULT_PAYER = "burn111111111111111111111111111111111111111"

# A random unrelated address for the stranger caller. Off curve, holds
# nothing, related to none of these coins. Never signs.
STRANGER = "1nc1nerator11111111111111111111111111111111"

# Twice the rent-exempt floor for an empty account: leaves the vault rent
# exempt AND the payout large enough not to strand a fresh recipient. The
# collector tool measured pump's own floor at exactly this value.
DEFAULT_TOP_UP = 50_000_000

PUBLISHED_IDL = (
    Path(__file__).resolve().parents[1].parent
    / "pump-fun-sdk/pump-fun-repos/pump-public-docs/idl/pump_fees.json"
)

# pump's OLDER published account lists for the two instructions the deployed
# IDL emptied, vendored verbatim from that pump_fees.json so the CI checkout
# (which does not contain the pump-fun-sdk tree) can still build them. The
# file above is preferred when present; this is the fallback. `global` is
# PDA(["global"]) under the pump program (the const program bytes below), and
# `program`/`event_authority` resolve under the fee-share program.
PUBLISHED_ACCOUNTS = json.loads(
    '{"revoke_fee_sharing_authority":{"accounts":[{"name":"authority","signer":true},'
    '{"name":"global","pda":{"seeds":[{"kind":"const","value":[103,108,111,98,97,108]}],'
    '"program":{"kind":"const","value":[1,86,224,246,147,102,90,207,68,219,21,104,191,23,'
    '91,170,81,137,203,151,245,210,255,59,101,93,43,182,253,109,24,176]}}},'
    '{"name":"mint","relations":["sharing_config"]},'
    '{"name":"sharing_config","writable":true,"pda":{"seeds":[{"kind":"const","value":'
    '[115,104,97,114,105,110,103,45,99,111,110,102,105,103]},{"kind":"account","path":"mint"}]}},'
    '{"name":"event_authority","pda":{"seeds":[{"kind":"const","value":'
    '[95,95,101,118,101,110,116,95,97,117,116,104,111,114,105,116,121]}]}},{"name":"program"}]},'
    '"transfer_fee_sharing_authority":{"accounts":[{"name":"authority","signer":true},'
    '{"name":"global","pda":{"seeds":[{"kind":"const","value":[103,108,111,98,97,108]}],'
    '"program":{"kind":"const","value":[1,86,224,246,147,102,90,207,68,219,21,104,191,23,'
    '91,170,81,137,203,151,245,210,255,59,101,93,43,182,253,109,24,176]}}},'
    '{"name":"mint","relations":["sharing_config"]},'
    '{"name":"sharing_config","writable":true,"pda":{"seeds":[{"kind":"const","value":'
    '[115,104,97,114,105,110,103,45,99,111,110,102,105,103]},{"kind":"account","path":"mint"}]}},'
    '{"name":"new_admin"},'
    '{"name":"event_authority","pda":{"seeds":[{"kind":"const","value":'
    '[95,95,101,118,101,110,116,95,97,117,116,104,111,114,105,116,121]}]}},{"name":"program"}]}}'
)

# The errors this question turns on, so the verdict line is legible without
# cross-referencing the IDL dump.
MEANING = {
    6009: "SharingConfigAdminRevoked -- can only be updated once",
    6016: "NotAuthorized",
    6023: "DeprecatedInstruction",
    6024: "FeeSharesAlreadyUpdated -- reward split can only be updated once",
    6018: "SharingConfigNotActive",
    6007: "InvalidSharingConfig",
}

RESET_INSTRUCTIONS = ("reset_fee_sharing_config", "reset_fee_sharing_config_v2")
DEPRECATED_CANDIDATES = ("transfer_fee_sharing_authority", "revoke_fee_sharing_authority")


# -- Global, read from the chain ------------------------------------------
def global_pda() -> str:
    return find_program_address([b"global"], pump.PUMP_PROGRAM)[0]


def read_global_authorities(rpc, idls) -> dict:
    """`authority`, `set_creator_authority`, `admin_set_creator_authority`
    decoded from pump's on-chain Global account, via the pump IDL's own
    `Global` type. Question 3 names exactly these."""
    address = global_pda()
    account = read_accounts(rpc, [address])[0]
    out = {"_address": address}
    if not account:
        out["_error"] = "Global account not found"
        return out
    pump_idl = idls.get(pump.PUMP_PROGRAM) or {}
    definition = type_def(pump_idl, "Global")
    if definition is None:
        out["_error"] = "pump IDL has no Global type"
        return out
    blob = base64.b64decode(account["data"][0])
    off = 8  # account discriminator
    try:
        for field in definition.get("fields", []):
            value, off = _read(blob, off, field["type"], pump_idl)
            if field["name"] in ("authority", "set_creator_authority",
                                 "admin_set_creator_authority", "withdraw_authority"):
                out[field["name"]] = value
    except Exception as exc:                                # noqa: BLE001 - reported
        out["_error"] = f"Global decode failed: {exc}"
    return out


# -- instruction assembly -------------------------------------------------
def load_published() -> dict:
    if PUBLISHED_IDL.exists():
        try:
            return json.loads(PUBLISHED_IDL.read_text())
        except Exception as exc:                            # noqa: BLE001 - reported
            print(f"  published IDL unreadable at {PUBLISHED_IDL}: {exc}")
    print("  published IDL file not present; using vendored account lists for "
          "transfer_/revoke_fee_sharing_authority")
    return {"instructions": [
        {"name": name, **body} for name, body in PUBLISHED_ACCOUNTS.items()
    ]}


def published_instruction(published: dict, name: str):
    for entry in published.get("instructions", []) or []:
        if entry.get("name") == name:
            return entry
    return None


def reset_context(mint: str, config: str, identity: str, new_admin: str) -> dict:
    """Everything the reset / authority instructions name that is neither a
    PDA nor a fixed address: the acting `authority`, the `mint`, the
    `new_admin`, and the WSOL quote mint the ATA seeds need."""
    return {
        "mint": mint,
        "authority": identity,
        "signer": identity,
        "payer": identity,
        "admin": identity,
        "new_admin": new_admin,
        "sharing_config": config,
        "config": config,
        "quote_mint": WSOL_MINT,
        "wsol_mint": WSOL_MINT,
        "token_program": TOKEN_PROGRAM,
        "associated_token_program": ASSOCIATED_TOKEN_PROGRAM,
    }


def build_reset(idls, published, name, mint, config, identity, new_admin,
                shareholders):
    """`(program_id, metas, data)` for one reset/authority call, plus a note
    on where its account list came from. Deployed IDL first; if the deployed
    entry has an empty account list (as transfer_/revoke_ do), fall back to
    pump's older published account list, keeping the deployed discriminator.
    """
    program_id, idl, entry = find_instruction(idls, name)
    if entry is None:
        raise LookupError(f"{name} is in neither on-chain IDL")
    source = "deployed IDL"
    accounts = entry.get("accounts") or []
    if not accounts:
        pub = published_instruction(published, name)
        if pub is None or not (pub.get("accounts") or []):
            raise LookupError(
                f"{name} has no account list in the deployed IDL and none in "
                f"the published IDL either"
            )
        # Keep the DEPLOYED discriminator and args; borrow only the account
        # shape the deployed IDL emptied.
        entry = dict(entry)
        entry["accounts"] = pub["accounts"]
        source = "published IDL account list (deployed IDL emptied it)"
    context = reset_context(mint, config, identity, new_admin)
    metas = resolve_accounts(entry, program_id, context)
    # Mirror update_fee_shares_v2: the CPI that distributes pending fees walks
    # the outgoing shareholders as writable remaining accounts, in order.
    metas = metas + [
        (f"outgoing[{i}]", who, False, True) for i, who in enumerate(shareholders)
    ]
    data_args, _notes = encode_args(entry, idl, {})
    data = bytes(entry["discriminator"]) + data_args
    return program_id, metas, data, source


# -- one simulated call ---------------------------------------------------
def simulate_call(rpc, idls, program_ixs, *, payer, watch, verbose):
    blockhash = rpc.call("getLatestBlockhash",
                         [{"commitment": "finalized"}])["value"]["blockhash"]
    message, signature_count, ordered = build_message(program_ixs, payer, blockhash)
    unsigned = bytes([signature_count]) + b"\x00" * (64 * signature_count) + message

    seen = []
    for address in watch:
        if address not in seen and address in ordered:
            seen.append(address)
    before = read_accounts(rpc, seen)

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
                "accounts": {"encoding": "base64", "addresses": seen},
            },
        ],
    )
    value = (result or {}).get("value") or {}
    after = list(value.get("accounts") or [])
    after.extend([None] * (len(seen) - len(after)))

    err = value.get("err")
    code = None
    failing_index = None
    failing_program = None
    if isinstance(err, dict):
        entry = err.get("InstructionError") or [None, None]
        failing_index = entry[0]
        custom = entry[1]
        if isinstance(custom, dict) and "Custom" in custom:
            code = custom["Custom"]
        if isinstance(failing_index, int) and failing_index < len(program_ixs):
            failing_program = program_ixs[failing_index][0]

    return {
        "err": err,
        "code": code,
        "failing_index": failing_index,
        "failing_program": failing_program,
        "logs": value.get("logs") or [],
        "value": value,
        "watched": seen,
        "before": before,
        "after": after,
    }


def decode_events(value, idls):
    from indexer.base58 import decode as b58decode
    from tools.simulate_create_config import ANCHOR_CPI_EVENT
    payloads = []
    for line in value.get("logs") or []:
        marker = "Program data: "
        if marker in line:
            try:
                payloads.append(("log", base64.b64decode(line.split(marker, 1)[1].strip())))
            except Exception:                               # noqa: BLE001
                pass
    for group in value.get("innerInstructions") or []:
        for entry in group.get("instructions") or []:
            raw = entry.get("data")
            if not raw:
                continue
            try:
                blob = b58decode(raw)
            except ValueError:
                continue
            if blob[:8] == ANCHOR_CPI_EVENT:
                payloads.append(("cpi", blob[8:]))
    out = []
    for source, payload in payloads:
        decoded = decode_event(idls, payload)
        if decoded is None:
            out.append((source, f"unknown:{payload[:8].hex()}", {}))
        else:
            out.append((source, decoded[0], decoded[1]))
    return out


def verdict_of(outcome) -> str:
    if outcome["err"] is None:
        return "ESCAPABLE (simulation OK -- the program accepted this)"
    code = outcome["code"]
    if code is not None:
        meaning = MEANING.get(code, "")
        return f"REFUSED  custom error {code}" + (f"  [{meaning}]" if meaning else "")
    return f"REFUSED  err={outcome['err']}"


def print_outcome(idls, outcome, *, watch_labels, verbose):
    print(f"  VERDICT        {verdict_of(outcome)}")
    if outcome["code"] is not None:
        print(f"  error          {error_name(idls, outcome['code'], outcome['failing_program'])}")
        print(f"  failing ix     #{outcome['failing_index']} in {outcome['failing_program']}")
    # Balance deltas -- only meaningful on success; a failed sim returns no
    # post-sim accounts, so its "after" is not a balance.
    if outcome["err"] is None:
        for address in outcome["watched"]:
            i = outcome["watched"].index(address)
            b = int((outcome["before"][i] or {}).get("lamports") or 0)
            a = int((outcome["after"][i] or {}).get("lamports") or 0)
            label = watch_labels.get(address, "")
            print(f"  lamports       {address} {label}\n"
                  f"                 before {b}  after {a}  delta {a - b:+d}")
        # Post-sim config bytes: did admin_revoked flip / version move?
        for address in outcome["watched"]:
            i = outcome["watched"].index(address)
            acct = outcome["after"][i]
            if acct and acct.get("owner") == pump.PUMP_FEE_SHARE_PROGRAM:
                try:
                    cfg = pump.decode_sharing_config(
                        address, {"owner": acct["owner"], "data": acct["data"],
                                  "space": len(base64.b64decode(acct["data"][0]))})
                    print(f"  config AFTER   admin={cfg.admin} admin_revoked={cfg.admin_revoked} "
                          f"version={cfg.version} status={cfg.status} "
                          f"shareholders={len(cfg.shareholders)}")
                except Exception as exc:                    # noqa: BLE001
                    print(f"  config AFTER   undecodable: {exc}")
        for source, name, fields in decode_events(outcome["value"], idls):
            print(f"  event          [{source}] {name}")
            if verbose:
                for k, v in fields.items():
                    print(f"                   {k:<20} {v}")
    print("  --- logs, verbatim ---")
    for line in outcome["logs"]:
        print(f"    | {line}")


# -- the run --------------------------------------------------------------
def run_one_coin(rpc, idls, published, row, *, callers, top_up, verbose):
    mint = row["mint"]
    config = row["config"].address
    shareholders = [who for who, _bps in row["shareholders"]]
    vault = row["vault"]
    print("=" * 78)
    print(f"coin           {mint}")
    print(f"  config       {config}")
    print(f"  admin        {row['admin']}")
    print(f"  admin_revoked {row['admin_revoked']}   status={row['status']}  "
          f"graduated={row['graduated']}")
    print(f"  shareholders {row['shareholders']}")
    print(f"  creator vault {vault}  lamports={row['vault_lamports']}")
    if not row["admin_revoked"]:
        print("  NOTE: this coin's admin_revoked is FALSE -- it has not committed "
              "the one-shot yet, so it is not a test of escaping AFTER commitment.")
    print()

    for caller_label, identity in callers:
        for name in RESET_INSTRUCTIONS + DEPRECATED_CANDIDATES:
            print("-" * 74)
            print(f"instruction    {name}")
            print(f"caller         ({caller_label}) {identity}")
            # new_admin = the caller itself: a successful reset hands control
            # to whoever called it, which is the escape.
            try:
                program_id, metas, data, source = build_reset(
                    idls, published, name, mint, config, identity, identity, shareholders)
            except LookupError as exc:
                print(f"  UNRESOLVED     {exc}")
                continue
            print(f"  account list   {source}")
            watch = [config, vault, identity, row["admin"]] + shareholders
            watch_labels = {config: "<- CONFIG", vault: "<- CREATOR VAULT",
                            identity: "<- CALLER", row["admin"]: "<- OLD ADMIN"}
            outcome = simulate_call(
                rpc, idls, [(program_id, metas, data)],
                payer=DEFAULT_PAYER, watch=watch, verbose=verbose)
            print_outcome(idls, outcome, watch_labels=watch_labels, verbose=verbose)

            # Question 5: if it succeeded, re-run with the vault funded above
            # pump's floor and measure whether pending fees actually move.
            if outcome["err"] is None and top_up:
                print(f"  ~~~ re-run with vault top-up {top_up} lamports (does it MOVE fees?)")
                ixs = [
                    transfer_instruction(DEFAULT_PAYER, vault, top_up),
                    (program_id, metas, data),
                ]
                paid = simulate_call(
                    rpc, idls, ixs, payer=DEFAULT_PAYER, watch=watch, verbose=verbose)
                print_outcome(idls, paid, watch_labels=watch_labels, verbose=verbose)
        print()


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("mints", nargs="*")
    parser.add_argument("--rpc")
    parser.add_argument("--auto", action="store_true",
                        help="sample the launch stream for fee-shared coins")
    parser.add_argument("--auto-limit", type=int, default=30)
    parser.add_argument("--auto-scan", type=int, default=120)
    parser.add_argument("--auto-delay", type=float, default=0.6)
    parser.add_argument("--auto-want", type=int, default=2,
                        help="how many admin_revoked coins to test from the sample")
    parser.add_argument("--top-up", type=int, default=DEFAULT_TOP_UP,
                        help="vault top-up used to test whether a reset moves fees")
    parser.add_argument("--verbose", action="store_true", help="print event fields")
    args = parser.parse_args(argv)

    rpc = RpcClient(endpoints_from(args.rpc))
    idls = {}
    for program_id in (pump.PUMP_PROGRAM, pump.PUMP_FEE_SHARE_PROGRAM):
        try:
            idls[program_id] = read_idl(rpc, program_id)
        except Exception as exc:                            # noqa: BLE001 - reported
            print(f"{program_id}: IDL unreadable {exc}")
    published = load_published()

    authorities = read_global_authorities(rpc, idls)
    print("pump Global authorities (question 3)")
    print(f"  global account  {authorities.get('_address')}")
    for key in ("authority", "set_creator_authority",
                "admin_set_creator_authority", "withdraw_authority"):
        if key in authorities:
            print(f"  {key:<28} {authorities[key]}")
    if authorities.get("_error"):
        print(f"  {authorities['_error']}")
    print()

    rows = []
    explicit = list(args.mints)
    if args.auto:
        print("sampling pump's launch stream for fee-shared coins")
        sampled = sample(rpc, want=args.auto_limit, scan=args.auto_scan, delay=args.auto_delay)
        shared = [r for r in sampled if r.get("route") == "fee_share"]
        print(f"  {len(shared)} of {len(sampled)} sampled coins carry a sharing config")
        explicit.extend(r["mint"] for r in shared)
        print()

    revoked = []
    others = []
    for mint in explicit:
        try:
            row = inspect(rpc, mint)
        except Exception as exc:                            # noqa: BLE001 - reported
            print(f"{mint}: inspect failed {exc}")
            continue
        if "config" not in row:
            print(f"{mint}: {row.get('skip')}")
            continue
        (revoked if row["admin_revoked"] else others).append(row)

    # Prefer admin_revoked coins -- those are the real test of escaping after
    # commitment. Fall back to any config if none are revoked in the sample.
    chosen = revoked[: args.auto_want] if args.auto else revoked + others
    if not chosen and others:
        print("no admin_revoked coin found; testing a not-yet-revoked config so the "
              "caller-authorization answer is at least established")
        chosen = others[: args.auto_want] if args.auto else others
    if not chosen:
        print("no testable coin with a sharing config found")
        return 1

    # Build the caller list once, from the Global authorities plus the admin
    # and a stranger. The admin differs per coin, so it is added inside.
    global_callers = []
    for key in ("authority", "set_creator_authority", "admin_set_creator_authority"):
        addr = authorities.get(key)
        if addr and addr != SYSTEM_PROGRAM:
            global_callers.append((f"global.{key}", addr))

    for row in chosen:
        callers = [("admin", row["admin"]), ("stranger", STRANGER)] + global_callers
        # De-duplicate by address, keeping the first (most meaningful) label.
        seen = set()
        deduped = []
        for label, addr in callers:
            if addr in seen:
                continue
            seen.add(addr)
            deduped.append((label, addr))
        run_one_coin(rpc, idls, published, row, callers=deduped,
                     top_up=args.top_up, verbose=args.verbose)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
