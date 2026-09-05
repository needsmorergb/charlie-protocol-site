"""Does the smoke test's enrollment fixture have pump's Trader Cashback on?

`scripts/smoke.py` builds a real enroll transaction against mainnet on every
deploy, using one fixture coin. The enroll preflight now REFUSES any coin whose
bonding curve reports cashback true, because such a coin routes its whole
creator fee to traders and every share of any split would be zero. If the
fixture is a cashback coin, the deploy smoke check starts failing.

That is a question about mainnet, so it is asked of mainnet. Reads only.
Nothing signs, nothing is sent.

    python tools/smoke_fixture_cashback.py
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from indexer import pump                                   # noqa: E402
from indexer.rpc import DEFAULT_ENDPOINTS, RpcClient       # noqa: E402

# scripts/smoke.py ENROLL_MINT / ENROLL_ADMIN, copied here rather than
# imported: that file lives in the other repository.
SMOKE_ENROLL_MINT = "JAMXU2JLraZ3RUhbgc3ttYPc18Kx4ojCnC56XR2zpump"


def main(argv=None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--rpc", help="comma-separated RPC endpoints (env CHARLIE_RPC_URLS)")
    parser.add_argument("--mint", default=SMOKE_ENROLL_MINT)
    args = parser.parse_args(argv)

    raw = args.rpc or os.environ.get("CHARLIE_RPC_URLS") or ""
    endpoints = tuple(url.strip() for url in raw.split(",") if url.strip()) or DEFAULT_ENDPOINTS
    rpc = RpcClient(endpoints)

    curve = pump.read_bonding_curve(rpc, args.mint)
    account = rpc.accounts([pump.bonding_curve(args.mint)])[0]
    import base64
    data = base64.b64decode(account["data"][0])

    print(f"mint            {curve.mint}")
    print(f"bonding curve   {pump.bonding_curve(args.mint)}")
    print(f"account length  {len(data)} bytes")
    print(f"flag offset     {pump.CASHBACK_FLAG_OFFSET}")
    if len(data) > pump.CASHBACK_FLAG_OFFSET:
        print(f"byte 81         {data[81]}")
        print(f"byte 82         {data[pump.CASHBACK_FLAG_OFFSET]}")
    print(f"graduated       {curve.graduated}")
    print(f"creator         {curve.creator}")
    print(f"CASHBACK        {curve.cashback!r}")
    if curve.cashback is True:
        print("VERDICT         smoke check WILL fail: preflight refuses this coin")
    elif curve.cashback is False:
        print("VERDICT         smoke check is unaffected: preflight lets this coin through")
    else:
        print("VERDICT         flag unreadable; preflight only refuses on True, so smoke passes")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
