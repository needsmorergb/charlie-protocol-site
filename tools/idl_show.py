"""Print named instructions and types from a program's on-chain Anchor IDL.

`idl_dump.py` reports the fee-share instructions and every error; this
prints whatever is asked for, verbatim from the chain -- accounts in order
with their flags, fixed addresses and PDA seed specs, args with types, and
account/type layouts -- so an instruction builder can be written from the
deployed program's own declaration rather than from a write-up.

    python tools/idl_show.py <program id> --instruction buy --type Global

Reads only.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from indexer.rpc import RpcClient                           # noqa: E402
from tools.idl_dump import read_idl                         # noqa: E402
from tools.sample_new_coins import endpoints_from           # noqa: E402


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("program")
    parser.add_argument("--instruction", nargs="*", default=[])
    parser.add_argument("--type", nargs="*", default=[])
    parser.add_argument("--rpc")
    args = parser.parse_args(argv)
    idl = read_idl(RpcClient(endpoints_from(args.rpc)), args.program)
    meta = idl.get("metadata") or {}
    print(f"{args.program}  {meta.get('name')} {meta.get('version')} spec {meta.get('spec')}")
    print(f"  instructions: {' '.join(e.get('name', '') for e in idl.get('instructions') or [])}")
    print(f"  types: {' '.join(e.get('name', '') for e in idl.get('types') or [])}")
    for name in args.instruction:
        entry = next((e for e in idl.get("instructions") or [] if e.get("name") == name), None)
        print(f"\n## instruction {name}")
        if entry is None:
            print("  (not in the IDL)")
            continue
        print(f"  disc {bytes(entry.get('discriminator') or []).hex()}")
        for index, account in enumerate(entry.get("accounts") or []):
            flags = ("s" if account.get("signer") else "-") + ("w" if account.get("writable") else "-")
            extra = ""
            if account.get("address"):
                extra += f"  = {account['address']}"
            if account.get("pda"):
                extra += f"  pda {json.dumps(account['pda'], separators=(',', ':'))}"
            if account.get("optional"):
                extra += "  optional"
            print(f"  {index:>2} {flags} {account.get('name')}{extra}")
        print(f"  args {json.dumps(entry.get('args') or [], separators=(',', ':'))}")
    for name in args.type:
        entry = next((e for e in idl.get("types") or [] if e.get("name") == name), None)
        print(f"\n## type {name}")
        print("  (not in the IDL)" if entry is None else json.dumps(entry.get("type"), separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
