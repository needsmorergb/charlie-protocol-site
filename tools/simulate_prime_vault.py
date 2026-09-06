"""Can a coin's creator vault be topped up so pump will distribute it?

pump refuses `distribute_creator_fees` below a minimum and says so in a log
while returning SUCCESS:

    Insufficient fees for distribution. Minimum vault balance needed: N lamports

It calls that a VAULT BALANCE. If it means the account's lamports, then a
coin short of the minimum does not have to trade its way there -- anyone can
send the difference to the vault, because a system transfer needs no
permission from the recipient. That turns "wait for ~4.8 SOL of volume" into
"send 0.015 SOL", which is the difference between testing the payout today
and testing it next month.

If instead pump tracks fees in its own accounting, a topped-up vault will
still be refused, and the log will say so. Either answer is worth having and
only mainnet can give it.

    python tools/simulate_prime_vault.py <mint> [--lamports N] [--rpc URL]

Builds [system transfer -> vault, distribute_creator_fees] as ONE
transaction and simulates it. Nothing signs, nothing is sent: the funder is
pump's own fee wallet, which holds SOL, and `sigVerify: false` lets the
runtime answer as if it had signed. Without --lamports the top-up is read
from pump's own refusal, so the number is never guessed.
"""

from __future__ import annotations

import argparse
import base64
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from indexer import distribute                                  # noqa: E402
from indexer.buyback import ix_system_transfer                  # noqa: E402
from indexer.distribute import STAND_IN_PAYER as FUNDER         # noqa: E402
from indexer.message import compile_legacy, unsigned_transaction  # noqa: E402
from indexer.rpc import DEFAULT_ENDPOINTS, RpcClient            # noqa: E402

LAMPORTS_PER_SOL = 1_000_000_000


def _simulate(rpc, message: bytes) -> dict:
    result = rpc.call("simulateTransaction", [
        base64.b64encode(unsigned_transaction(message)).decode(),
        {"encoding": "base64", "sigVerify": False, "replaceRecentBlockhash": True,
         "commitment": "processed"},
    ])
    return (result or {}).get("value") or {}


def run(rpc, mint: str, lamports: int | None) -> str:
    blockhash = rpc.call("getLatestBlockhash", [{"commitment": "finalized"}])["value"]["blockhash"]
    built = distribute.plan(rpc, mint, FUNDER, blockhash=blockhash)
    lines = [
        mint,
        f"  vault          {built.vault}",
        f"  holds          {built.vault_lamports} lamports",
        f"  shareholders   {len(built.shareholders)}",
    ]

    # What pump says about the vault as it stands, so the top-up is its
    # number rather than ours.
    before = _simulate(rpc, built.message)
    minimum = distribute.declined(before)
    if minimum is None:
        lines.append("  pump           would distribute already; no top-up needed")
        return "\n".join(lines)
    lines.append(f"  pump wants     {minimum} lamports")

    top_up = lamports if lamports is not None else max(0, minimum - built.vault_lamports)
    lines.append(f"  top-up         {top_up} lamports ({top_up / LAMPORTS_PER_SOL:.9f} SOL)")

    primed = compile_legacy(
        FUNDER,
        [ix_system_transfer(FUNDER, built.vault, top_up)] + list(built.instructions),
        blockhash,
    )
    after = _simulate(rpc, primed)
    still = distribute.declined(after)
    err = after.get("err")

    if err is not None:
        lines.append(f"  VERDICT        the primed transaction FAILED: {err}")
    elif still is not None:
        lines.append(f"  VERDICT        pump STILL declined, wanting {still} -- the minimum is not "
                     "just the account's lamports, so a coin cannot be primed and must trade")
    else:
        lines.append("  VERDICT        pump ACCEPTED it -- topping the vault up is enough, and a "
                     "coin short of the minimum can be primed instead of waiting for volume")
    for line in (after.get("logs") or [])[-8:]:
        lines.append(f"    | {line}")
    return "\n".join(lines)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("mints", nargs="+")
    parser.add_argument("--lamports", type=int, default=None,
                        help="top-up to try; default is exactly what pump asked for")
    parser.add_argument("--rpc", help="comma-separated RPC endpoints (env CHARLIE_RPC_URLS)")
    args = parser.parse_args(argv)

    raw = args.rpc or os.environ.get("CHARLIE_RPC_URLS") or ""
    endpoints = tuple(url.strip() for url in raw.split(",") if url.strip()) or DEFAULT_ENDPOINTS
    rpc = RpcClient(endpoints)

    for mint in args.mints:
        try:
            print(run(rpc, mint, args.lamports))
        except Exception as exc:                    # noqa: BLE001 - reported, not raised
            print(f"{mint}\n  unreadable     {type(exc).__name__}: {exc}")
        print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
