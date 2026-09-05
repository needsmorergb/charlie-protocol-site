"""What an address actually is on chain: owner, lamports, executable, kind.

pump's own IDL names two ways a fee distribution fails on the RECIPIENT
rather than on the payer:

    6052  UnableToDistributeCreatorFeesToExecutableRecipient
    6070  UnableToDistributeCreatorFeesToUninitializedAccount

Only ONE of them is real for our purposes, and this file used to claim the
wrong one. Simulated on mainnet 2026-09-03 against four recipient shapes,
each verified by the lamports that moved rather than by the absence of an
error: an ordinary wallet, a non-executable PDA owned by the fee-share
program, a non-executable PDA owned by pump, and a PDA that DOES NOT EXIST
were all paid 49,189,376 lamports. Only the pump program itself, an
executable account, was refused:

    Unable to distribute to 6EF8rrec... Shareholder is a program account
    Error Code: UnableToDistributeCreatorFeesToExecutableRecipient. 6052.

So `6070` did not fire on anything, including an account with no existence
at all, and the guard keys on `executable` rather than on ownership. A
keyless destination does not need to exist before a coin routes to it, which
is what makes the incinerator payable and a collector PDA payable too.

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
            "  state          does not exist, so it has never held a lamport\n"
            "  distribution   eligible: a non-existent recipient is PAID, measured"
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
        lines.append("  distribution   would FAIL with 6052, the one refusal there is")
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
