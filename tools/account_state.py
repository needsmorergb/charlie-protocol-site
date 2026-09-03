"""What an address actually is on chain: owner, lamports, executable, kind.

pump's own IDL names two ways a fee distribution fails on the RECIPIENT
rather than on the payer:

    6052  UnableToDistributeCreatorFeesToExecutableRecipient
    6070  UnableToDistributeCreatorFeesToUninitializedAccount

Both matter to a protocol that routes fees to keyless addresses. A shareholder
that does not exist yet, or that is a program rather than a program-owned
account, makes `distribute_creator_fees` fail -- and it pays every shareholder
in one instruction, so one bad recipient blocks the whole coin's distribution,
the dev's own share included.

This prints the state that decides it.

    python tools/account_state.py --rpc <url> <address> [<address> ...]

Reads only.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from indexer.curve import is_on_curve                     # noqa: E402
from indexer.legs import recipient_kind                   # noqa: E402
from indexer.rpc import DEFAULT_ENDPOINTS, RpcClient      # noqa: E402

INCINERATOR = "1nc1nerator11111111111111111111111111111111"
LEGACY_VANITY = "burn111111111111111111111111111111111111111"


def describe(address: str, account: dict | None) -> str:
    if account is None:
        return (
            f"{address}\n"
            "  state          UNINITIALIZED -- does not exist\n"
            "  distribution   would FAIL with 6070 if named as a shareholder"
        )
    executable = bool(account.get("executable"))
    lamports = account.get("lamports")
    lines = [
        address,
        f"  owner          {account.get('owner')}",
        f"  lamports       {lamports}",
        f"  executable     {executable}",
        f"  kind           {recipient_kind(account)}",
        f"  keyless        {not is_on_curve(address)}",
    ]
    if executable:
        lines.append("  distribution   would FAIL with 6052 (executable recipient)")
    else:
        lines.append("  distribution   recipient is eligible")
    return "\n".join(lines)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("addresses", nargs="*")
    parser.add_argument("--rpc", help="comma-separated RPC endpoints (env CHARLIE_RPC_URLS)")
    args = parser.parse_args(argv)

    addresses = args.addresses or [INCINERATOR, LEGACY_VANITY]
    raw = args.rpc or os.environ.get("CHARLIE_RPC_URLS") or ""
    endpoints = tuple(url.strip() for url in raw.split(",") if url.strip()) or DEFAULT_ENDPOINTS
    rpc = RpcClient(endpoints)

    accounts = rpc.accounts(addresses)
    for address, account in zip(addresses, accounts):
        print(describe(address, account))
        print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
