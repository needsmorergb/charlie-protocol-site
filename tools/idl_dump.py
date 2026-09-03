"""Read a program's Anchor IDL from the chain and print what it actually says.

Written to settle a claim this project makes on a public page before a dev
walks through a one-way door: `/enroll` and `api/enroll.py` both state that
pump allows a coin's fee split to be changed exactly once, on the strength of
a `FeeSharesAlreadyUpdated` seen in a mainnet simulation. pump's PUBLISHED
IDL contains no such error and names the instruction `update_fee_shares`, not
`update_fee_shares_v2`.

One of those is out of date. The on-chain IDL is the tiebreaker, and it is
the same account `enroll.py` derived its account list from, so this reads it
directly rather than trusting either document.

Anchor stores the IDL at `createWithSeed(base, "anchor:idl", program)`, where
base is the program's own signer PDA. The account is 8 bytes of
discriminator, 32 bytes of authority, a u32 length, then zlib-compressed
JSON.

    python tools/idl_dump.py --rpc <url> <program id> [<program id> ...]

Reads only.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import zlib
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from indexer.base58 import decode, encode, pubkey_bytes   # noqa: E402
from indexer.curve import find_program_address            # noqa: E402
from indexer.rpc import DEFAULT_ENDPOINTS, RpcClient      # noqa: E402

PUMP_FEE_SHARE_PROGRAM = "pfeeUxB6jkeY1Hxd7CsFCAjcbHA9rWtchMGdZ6VojVZ"
PUMP_PROGRAM = "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P"
IDL_SEED = "anchor:idl"


def idl_address(program_id: str) -> str:
    """`createWithSeed(base, "anchor:idl", program)`, where base is the
    program's signer PDA -- the empty-seed program address.

    createWithSeed is a plain sha256 over base, seed and owner. It is not a
    PDA derivation and has no bump.
    """
    base, _bump = find_program_address([], program_id)
    digest = hashlib.sha256(
        pubkey_bytes(base) + IDL_SEED.encode() + pubkey_bytes(program_id)
    ).digest()
    return encode(digest)


def read_idl(rpc, program_id: str) -> dict:
    address = idl_address(program_id)
    account = rpc.accounts([address])[0]
    if not account:
        raise LookupError(f"{program_id}: no IDL account at {address}")
    raw = account.get("data")
    blob = raw[0] if isinstance(raw, list) else raw
    import base64

    data = base64.b64decode(blob)
    # 8 discriminator | 32 authority | 4 length | zlib(JSON)
    length = int.from_bytes(data[40:44], "little")
    payload = data[44 : 44 + length]
    return json.loads(zlib.decompress(payload).decode("utf-8"))


def report(idl: dict, program_id: str) -> str:
    lines = [f"{program_id}", f"  idl account   {idl_address(program_id)}"]
    meta = idl.get("metadata") or {}
    lines.append(f"  metadata      {meta.get('name')} {meta.get('version')} spec {meta.get('spec')}")

    instructions = idl.get("instructions") or []
    lines.append(f"  instructions  {len(instructions)}")
    for entry in instructions:
        name = entry.get("name", "")
        if "fee_share" in name or "distribute" in name or "creator" in name:
            disc = bytes(entry.get("discriminator") or []).hex()
            signers = [a["name"] for a in entry.get("accounts", []) if a.get("signer")]
            lines.append(f"    {name}  disc={disc}  signers={signers or 'NONE'}")

    errors = idl.get("errors") or []
    lines.append(f"  errors        {len(errors)}")
    for entry in errors:
        lines.append(f"    {entry.get('code')} {entry.get('name')} - {entry.get('msg')}")

    # The question this tool exists for, answered in one line.
    names = " ".join(e.get("name", "") for e in errors)
    verdict = "PRESENT" if "AlreadyUpdated" in names else "ABSENT"
    lines.append("")
    lines.append(f"  one-shot update error (…AlreadyUpdated): {verdict}")
    return "\n".join(lines)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("programs", nargs="*", default=None)
    parser.add_argument("--rpc", help="comma-separated RPC endpoints (env CHARLIE_RPC_URLS)")
    parser.add_argument("--json", action="store_true", help="print the whole IDL")
    args = parser.parse_args(argv)

    programs = args.programs or [PUMP_FEE_SHARE_PROGRAM, PUMP_PROGRAM]
    raw = args.rpc or os.environ.get("CHARLIE_RPC_URLS") or ""
    endpoints = tuple(url.strip() for url in raw.split(",") if url.strip()) or DEFAULT_ENDPOINTS
    rpc = RpcClient(endpoints)

    failures = 0
    for program_id in programs:
        try:
            idl = read_idl(rpc, program_id)
        except Exception as exc:                      # noqa: BLE001 - reported, not raised
            print(f"{program_id}\n  unreadable    {exc}")
            failures += 1
            continue
        print(report(idl, program_id))
        if args.json:
            print(json.dumps(idl, indent=1))
        print()
    return 1 if failures == len(programs) else 0


if __name__ == "__main__":
    raise SystemExit(main())
