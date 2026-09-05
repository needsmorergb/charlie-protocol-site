"""Simulate the EXACT transaction /enroll hands a wallet, against mainnet.

`simulate_create_config.py` proved that pump accepts create_fee_sharing_config
followed by update_fee_shares_v2 in one transaction -- built by that tool,
from the IDL, account by account. What it did not prove is that OUR encoder
produces the same bytes: `indexer.enroll.enrolment_message`, the function the
API calls, with the toll row, the incinerator and the dev's wallet as the
split. That is what a dev signs, so that is what this simulates.

Picks a freshly launched coin with no sharing config (the same sampler the
create tool uses), builds the two-instruction message as the creator, and
asks mainnet to simulate it with signature verification off. Reports the
program's answer, the post-simulation config (admin, admin_revoked, the
shareholders) and where the bonding curve's creator moved to.

The toll row is the protocol's real collection wallet (`legs.TOLL_DESTINATION`),
so a passing run proves that address is one pump will pay. Only if it were
ever unset again does the incinerator stand in, because a None refuses to
build; what is being tested then is the encoding, not the destination.

    python tools/simulate_enrolment.py --auto
    python tools/simulate_enrolment.py <mint>            # must be config-less

Reads only. Nothing signs, nothing is sent.
"""

from __future__ import annotations

import argparse
import base64
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from indexer import enroll, pump                             # noqa: E402
from indexer.base58 import encode                            # noqa: E402
from tools.sample_new_coins import sample                    # noqa: E402
from tools.sample_new_coins import endpoints_from            # noqa: E402
from indexer.rpc import RpcClient                            # noqa: E402

INCINERATOR = "1nc1nerator11111111111111111111111111111111"


def simulate(rpc, mint: str) -> int:
    curve = pump.read_bonding_curve(rpc, mint)
    creator = curve.creator
    config_pda = enroll.sharing_config_address(mint)
    print(f"coin           {mint}")
    print(f"  creator        {creator}")
    print(f"  graduated      {curve.graduated}")
    print(f"  cashback       {curve.cashback}")
    print(f"  config pda     {config_pda}  exists={rpc.accounts([config_pda])[0] is not None}")

    try:
        pump.read_sharing_config(rpc, curve)
    except pump.DecodeError as exc:
        if pump.NO_FEE_SPLIT_MARKER not in str(exc):
            raise
    else:
        print("  this coin already has a config; the create path does not apply")
        return 1

    # Exactly what the page builds by default: the toll, the incinerator, the
    # rest to the creator. The real toll destination once it is set, so the
    # push that sets it proves that address is one pump will pay; the
    # incinerator stands in only while it is None.
    if enroll.legs.TOLL_DESTINATION is None:
        print("  toll           unset -- simulating with the incinerator standing in")
        enroll.legs.TOLL_DESTINATION = INCINERATOR
    else:
        print(f"  toll           {enroll.legs.TOLL_DESTINATION}  (the real destination)")
    shares = [
        enroll.Share(enroll.legs.TOLL_DESTINATION, enroll.TOLL_BPS),
        enroll.Share(INCINERATOR, 2000),
        enroll.Share(creator, 10_000 - enroll.TOLL_BPS - 2000),
    ]
    try:
        enroll.preflight(None, creator, shares, curve=curve)
    except enroll.EnrollError as exc:
        # A coin the page would refuse -- Trader Cashback, mostly. Not a
        # failure of the encoder; just not a coin to test it on.
        print(f"  SKIPPED        preflight refused it: {str(exc)[:90]}")
        return 2
    blockhash = rpc.call("getLatestBlockhash", [{"commitment": "finalized"}])["value"]["blockhash"]
    message = enroll.enrolment_message(mint, creator, shares, blockhash, create=True)
    unsigned = bytes([1]) + b"\x00" * 64 + message
    print(f"  message bytes  {len(message)}   instructions {message[4 + 32 * message[3] + 32]}")
    print(f"  split          {[(s.address, s.bps) for s in shares]}")

    result = rpc.call("simulateTransaction", [
        base64.b64encode(unsigned).decode(),
        {"encoding": "base64", "sigVerify": False, "replaceRecentBlockhash": True,
         "commitment": "processed",
         "accounts": {"encoding": "base64",
                      "addresses": [config_pda, enroll.bonding_curve_address(mint)]}},
    ])
    value = (result or {}).get("value") or {}
    err = value.get("err")
    print()
    if err is None:
        print("  SIMULATION     OK -- pump accepted the transaction /enroll builds")
    else:
        print(f"  SIMULATION     FAILED err={err}")
    for line in value.get("logs") or []:
        if "Instruction:" in line or "Error" in line or "failed" in line:
            print(f"    | {line}")

    accounts = value.get("accounts") or []
    if accounts and accounts[0]:
        blob = base64.b64decode(accounts[0]["data"][0])
        config = pump.decode_sharing_config(
            config_pda, {"owner": accounts[0]["owner"], "data": accounts[0]["data"], "space": len(blob)})
        print(f"  config after   admin={config.admin} admin_revoked={config.admin_revoked}")
        for holder, bps in config.shareholders:
            print(f"    {bps / 100:>6.2f}%  {holder}")
        ok = (config.admin == creator and config.admin_revoked
              and tuple(config.shareholders) == tuple((s.address, s.bps) for s in shares))
        print(f"  split is exactly what was asked for: {ok}")
    if len(accounts) > 1 and accounts[1]:
        blob = base64.b64decode(accounts[1]["data"][0])
        moved = encode(blob[49:81])
        print(f"  bonding curve creator after   {moved}   (config pda: {moved == config_pda})")
    return 0 if err is None else 1


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("mints", nargs="*")
    parser.add_argument("--rpc")
    parser.add_argument("--auto", action="store_true")
    parser.add_argument("--auto-limit", type=int, default=6)
    parser.add_argument("--auto-scan", type=int, default=60)
    parser.add_argument("--auto-delay", type=float, default=0.6)
    args = parser.parse_args(argv)
    rpc = RpcClient(endpoints_from(args.rpc))

    mints = list(args.mints)
    if args.auto:
        rows = sample(rpc, want=args.auto_limit, scan=args.auto_scan, delay=args.auto_delay)
        candidates = [r for r in rows if r.get("route") == "plain_creator"
                      and r.get("creator_lamports", 0) > 0 and not r["curve"]["complete"]]
        candidates.sort(key=lambda r: r.get("creator_lamports", 0), reverse=True)
        print(f"config-less, un-graduated, funded creator: {len(candidates)} of {len(rows)}")
        mints += [r["mint"] for r in candidates]
    # The first coin that is actually enrollable decides the answer; the ones
    # the page itself would refuse are skipped, not counted.
    for mint in mints:
        status = simulate(rpc, mint)
        print()
        if status != 2:
            return status
    print("no enrollable coin in this sample; nothing was simulated")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
