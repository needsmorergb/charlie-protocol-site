"""Simulate `distribute_creator_fees` for a coin, and print what pump says.

The question this exists to answer, from `FEE-ROUTING.md` section 6:

    The incinerator has no account on chain. It cannot have one, because the
    runtime deletes the lamports credited to it at the end of every block.
    pump's IDL carries `6070 UnableToDistributeCreatorFeesToUninitializedAccount`
    and `distribute_creator_fees` pays every shareholder in ONE instruction.

If 6070 fires on the incinerator, then a config naming it cannot be
distributed at all, and the dev's own share is stuck with the burn share.
419 sharing configs on mainnet name the incinerator first, so this is not a
hypothetical for them either.

Nothing here signs and nothing is sent. `simulateTransaction` with
`sigVerify: false` is the whole point: the fee payer is any funded address,
and the runtime answers with the program's own error.

    python tools/simulate_distribute.py --rpc <url> <mint> [<mint> ...]

Every account is resolved from the program's ON-CHAIN IDL rather than from a
hand-written list: the IDL records each account's PDA seeds, so the builder
derives them instead of trusting prose that could be stale.
"""

from __future__ import annotations

import argparse
import base64
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from indexer import pump                                   # noqa: E402
from indexer.base58 import encode, pubkey_bytes            # noqa: E402
from indexer.curve import find_program_address             # noqa: E402
from indexer.enroll import _compact_u16                    # noqa: E402
from indexer.rpc import DEFAULT_ENDPOINTS, RpcClient       # noqa: E402
from tools.idl_dump import read_idl                        # noqa: E402

SYSTEM_PROGRAM = "11111111111111111111111111111111"
INCINERATOR = "1nc1nerator11111111111111111111111111111111"

# Any account with lamports works as a simulated fee payer: with sigVerify
# off the runtime never checks a signature, it only checks the payer can
# cover the fee. This one is the legacy vanity address, which holds ~179 SOL
# and belongs to nobody in particular. No key is used and nothing is sent.
DEFAULT_PAYER = "burn111111111111111111111111111111111111111"

# The errors this simulation exists to tell apart, by the code pump reports.
MEANING = {
    6070: "UNINITIALIZED RECIPIENT -- a shareholder account does not exist",
    6052: "EXECUTABLE RECIPIENT -- a shareholder is a program",
    6051: "sharing config is not active",
    6053: "bonding curve creator does not match the sharing config",
    6054: "remaining accounts do not match the shareholders",
    6027: "not enough remaining accounts",
}


def _seed_bytes(seed: dict, resolved: dict) -> bytes:
    """One seed, resolved.

    A seed path is either an account this instruction lists ("mint"), or a
    FIELD of one ("bonding_curve.creator", which is how the creator vault is
    derived). Both arrive as `kind: account`, so both are looked up in the
    same map and the caller seeds it with the field values it has read.
    """
    kind = seed.get("kind")
    if kind == "const":
        return bytes(seed.get("value") or [])
    if kind == "account":
        path = seed.get("path")
        value = resolved.get(path)
        if value is None:
            raise KeyError(f"seed refers to unresolved account {path!r}")
        return pubkey_bytes(value)
    raise KeyError(f"unsupported seed kind {kind!r}")


def resolve_accounts(
    instruction: dict, program_id: str, mint: str, context: dict | None = None
) -> list[tuple[str, bool, bool]]:
    """`(address, is_signer, is_writable)` for each account the IDL lists, in
    the IDL's order, derived from the IDL's own seeds.

    `context` carries values a seed can reference that are not accounts of
    this instruction, such as `bonding_curve.creator`.
    """
    resolved: dict[str, str] = {"mint": mint, **(context or {})}
    metas: list[tuple[str, bool, bool]] = []
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
                owner = encode(bytes(pda["program"]["value"]))
            seeds = [_seed_bytes(seed, resolved) for seed in pda.get("seeds", [])]
            address, _bump = find_program_address(seeds, owner)
        elif name == "system_program":
            address = SYSTEM_PROGRAM
        elif name == "mint":
            address = mint
        else:
            raise KeyError(f"cannot resolve account {name!r}")
        resolved[name] = address
        metas.append((address, bool(account.get("signer")), bool(account.get("writable"))))
    return metas


def build_message(metas, data: bytes, program_id: str, payer: str, blockhash: str) -> bytes:
    """A legacy message, unsigned, ordered the way the runtime requires.

    Same rules `enroll.message` documents: writable signers, readonly
    signers, writable non-signers, readonly non-signers, and the fee payer
    first among them.
    """
    merged: dict[str, list[bool]] = {payer: [True, True]}
    for address, signer, writable in metas + [(program_id, False, False)]:
        row = merged.setdefault(address, [False, False])
        row[0] = row[0] or signer
        row[1] = row[1] or writable

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
    out += _compact_u16(1)
    out.append(index[program_id])
    out += _compact_u16(len(metas))
    for address, _signer, _writable in metas:
        out.append(index[address])
    out += _compact_u16(len(data))
    out += data
    return bytes(out)


def simulate(rpc, idl: dict, mint: str, *, payer: str, name: str = "distribute_creator_fees") -> str:
    instruction = next((i for i in idl["instructions"] if i["name"] == name), None)
    if instruction is None:
        return f"{mint}\n  {name} is not in the IDL"

    curve = pump.read_bonding_curve(rpc, mint)
    config = pump.read_sharing_config(rpc, curve)
    shareholders = [address for address, _bps in config.shareholders]

    metas = resolve_accounts(
        instruction,
        pump.PUMP_PROGRAM,
        mint,
        context={"bonding_curve.creator": curve.creator},
    )
    # Anchor remaining accounts. 6054 says they must be exactly the
    # shareholders, in the config's own order.
    metas = metas + [(address, False, True) for address in shareholders]
    data = bytes(instruction["discriminator"])

    blockhash = rpc.call("getLatestBlockhash", [{"commitment": "finalized"}])["value"]["blockhash"]
    message = build_message(metas, data, pump.PUMP_PROGRAM, payer, blockhash)
    unsigned = bytes([1]) + b"\x00" * 64 + message
    result = rpc.call(
        "simulateTransaction",
        [
            base64.b64encode(unsigned).decode(),
            {
                "encoding": "base64",
                "sigVerify": False,
                "replaceRecentBlockhash": True,
                "commitment": "processed",
            },
        ],
    )
    value = (result or {}).get("value") or {}
    err = value.get("err")
    logs = value.get("logs") or []

    lines = [mint, f"  shareholders   {len(shareholders)}"]
    for address, bps in config.shareholders:
        mark = "  <- INCINERATOR" if address == INCINERATOR else ""
        lines.append(f"    {bps / 100:>6.2f}%  {address}{mark}")

    if err is None:
        lines.append("  simulation     OK -- pump would distribute this")
    else:
        code = None
        if isinstance(err, dict):
            custom = (err.get("InstructionError") or [None, None])[1]
            if isinstance(custom, dict):
                code = custom.get("Custom")
        lines.append(f"  simulation     FAILED err={err}")
        if code is not None:
            lines.append(f"  code           {code}  {MEANING.get(code, 'see the IDL error list')}")
    for line in logs[-8:]:
        lines.append(f"    | {line}")
    return "\n".join(lines)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("mints", nargs="+")
    parser.add_argument("--rpc", help="comma-separated RPC endpoints (env CHARLIE_RPC_URLS)")
    parser.add_argument("--payer", default=DEFAULT_PAYER)
    parser.add_argument("--instruction", default="distribute_creator_fees")
    args = parser.parse_args(argv)

    raw = args.rpc or os.environ.get("CHARLIE_RPC_URLS") or ""
    endpoints = tuple(url.strip() for url in raw.split(",") if url.strip()) or DEFAULT_ENDPOINTS
    rpc = RpcClient(endpoints)
    idl = read_idl(rpc, pump.PUMP_PROGRAM)

    for mint in args.mints:
        try:
            print(simulate(rpc, idl, mint, payer=args.payer, name=args.instruction))
        except Exception as exc:                    # noqa: BLE001 - reported, not raised
            print(f"{mint}\n  unreadable     {type(exc).__name__}: {exc}")
        print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
