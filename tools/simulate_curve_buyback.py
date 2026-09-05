"""Simulate the BURN leg on a coin still on its bonding curve, against mainnet.

`indexer.curvebuy` builds pump's own `buy` (exact tokens out, at most the lot
in SOL) and an SPL burn of what it bought, in one transaction, from the
deployed program's IDL. This asks mainnet whether it accepts those bytes on
a real, live curve: picks a freshly launched coin that has not graduated,
plans the lot as a funded stand-in wallet with signature checks off, and
reports pump's answer, the compute used and the tokens the burn would
destroy. `--wallet` names a different stand-in; a mint argument names the
coin instead of sampling one.

    python tools/simulate_curve_buyback.py --auto
    python tools/simulate_curve_buyback.py <mint>

Reads only. Nothing signs, nothing is sent.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from indexer import buyback                                 # noqa: E402
from indexer.rpc import RpcClient                           # noqa: E402
from tools.sample_new_coins import endpoints_from, sample   # noqa: E402

# A system-owned wallet holding SOL that nobody's key is needed for under
# sigVerify false: the same stand-in the graduated probe pays with.
STAND_IN = "burn111111111111111111111111111111111111111"


def simulate(rpc, mint: str, wallet: str, lot: int) -> int:
    print(f"coin           {mint}")
    try:
        plan = buyback.plan_for(rpc, mint, wallet, lot_lamports=lot)
    except buyback.BuybackError as exc:
        print(f"  SKIPPED        {exc}")
        return 2
    print("  " + buyback.render(plan).replace("\n", "\n  "))
    result = buyback._execute(rpc, plan, None, send=False)
    sim = result["simulation"]
    if result.get("error"):
        print(f"\n  SIMULATION     FAILED  {result['error']}")
        for line in sim["logs_tail"]:
            print(f"    | {line}")
        return 1
    print(f"\n  SIMULATION     OK -- pump accepted the buy and the burn, {sim['units_consumed']} compute units")
    for line in sim["logs_tail"]:
        if "Instruction:" in line:
            print(f"    | {line}")
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("mints", nargs="*")
    parser.add_argument("--rpc")
    parser.add_argument("--wallet", default=STAND_IN)
    parser.add_argument("--lot", type=float, default=0.05)
    parser.add_argument("--auto", action="store_true")
    parser.add_argument("--auto-limit", type=int, default=6)
    parser.add_argument("--auto-scan", type=int, default=60)
    parser.add_argument("--auto-delay", type=float, default=0.6)
    args = parser.parse_args(argv)
    rpc = RpcClient(endpoints_from(args.rpc))
    lot = int(round(args.lot * buyback.LAMPORTS_PER_SOL))

    mints = list(args.mints)
    if args.auto:
        rows = sample(rpc, want=args.auto_limit, scan=args.auto_scan, delay=args.auto_delay)
        live = [r for r in rows if r.get("curve") and not r["curve"].get("complete")]
        print(f"on the curve: {len(live)} of {len(rows)} sampled")
        mints += [r["mint"] for r in live]
    for mint in mints:
        status = simulate(rpc, mint, args.wallet, lot)
        print()
        if status != 2:
            return status
    print("no coin on its curve to simulate against")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
