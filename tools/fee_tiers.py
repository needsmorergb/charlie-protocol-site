"""pump's creator fee, read from the chain instead of from a blog post.

Every dollar figure in `BUILD.md` section 3 rests on one number: what share of
a trade the creator fee actually is. The published range, 0.05% to 0.95%, came
from documentation, and the tier boundaries that decide which end a real coin
sits at were never read.

They live in `FeeConfig`, a PDA of the fee-share program keyed by the program
whose trades it prices:

    FeeConfig(pump) = PDA(["fee_config", 6EF8rrec...], pfeeUxB6...)   bonding curve
    FeeConfig(amm)  = PDA(["fee_config", pAMMBay6...], pfeeUxB6...)   after graduation

`FeeTier` is a market-cap threshold in lamports and a `Fees { lp, protocol,
creator }` triple in basis points, so this prints the whole schedule and the
toll each tier implies at TOLL_BPS.

    python tools/fee_tiers.py --rpc <url>

Reads only. Nothing signs, nothing is sent.
"""

from __future__ import annotations

import argparse
import base64
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from indexer.base58 import pubkey_bytes                    # noqa: E402
from indexer.curve import find_program_address             # noqa: E402
from indexer.rpc import DEFAULT_ENDPOINTS, RpcClient       # noqa: E402
from tools.idl_dump import read_idl                        # noqa: E402
from tools.simulate_create_config import _read             # noqa: E402

PUMP_PROGRAM = "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P"
PUMP_AMM_PROGRAM = "pAMMBay6oceH9fJKBRHGP5D4bD4sWpmSwMn52FMfXEA"
PUMP_FEE_SHARE_PROGRAM = "pfeeUxB6jkeY1Hxd7CsFCAjcbHA9rWtchMGdZ6VojVZ"

FEE_CONFIG_SEED = b"fee_config"
LAMPORTS = 1_000_000_000

# The toll this spec settled on, so the table can state what each tier means
# for the burn rather than leaving the reader to multiply.
TOLL_BPS = 1000


def fee_config_address(config_program_id: str) -> str:
    address, _bump = find_program_address(
        [FEE_CONFIG_SEED, pubkey_bytes(config_program_id)], PUMP_FEE_SHARE_PROGRAM
    )
    return address


def read_fee_config(rpc, idl: dict, config_program_id: str) -> tuple[str, dict]:
    address = fee_config_address(config_program_id)
    account = rpc.accounts([address])[0]
    if not account:
        raise LookupError(f"no FeeConfig at {address} for {config_program_id}")
    raw = account.get("data")
    data = base64.b64decode(raw[0] if isinstance(raw, list) else raw)
    value, _off = _read(data, 8, {"defined": {"name": "FeeConfig"}}, idl)
    return address, value


def _fees_row(label: str, fees: dict) -> str:
    creator = fees["creator_fee_bps"]
    # A basis point is 1/100 of a percent, so bps/100 is the percent of volume.
    of_volume = creator / 100
    toll_of_volume = of_volume * TOLL_BPS / 10_000
    return (
        f"    {label:<22} lp {fees['lp_fee_bps']:>4}  protocol {fees['protocol_fee_bps']:>4}  "
        f"creator {creator:>4} bps = {of_volume:.4f}% of volume"
        f"   toll {toll_of_volume:.5f}%"
    )


def render(label: str, address: str, config: dict) -> str:
    lines = [
        f"{label}",
        f"  account        {address}",
        f"  admin          {config['admin']}",
        "  flat fees (non-pump pools)",
        _fees_row("flat", config["flat_fees"]),
    ]
    for name in ("fee_tiers", "stable_fee_tiers"):
        tiers = config.get(name) or []
        lines.append(f"  {name}  ({len(tiers)})")
        for tier in tiers:
            threshold = tier["market_cap_lamports_threshold"]
            sol = threshold / LAMPORTS
            lines.append(_fees_row(f"mcap >= {sol:,.0f} SOL", tier["fees"]))
    return "\n".join(lines)


def summarise(config: dict) -> str:
    """The two numbers BUILD.md quotes: the cheapest and dearest creator fee a
    coin can be charged, which bound every dollar figure in the spec."""
    creators = [t["fees"]["creator_fee_bps"] for t in (config.get("fee_tiers") or [])]
    if not creators:
        return "  no tiers to summarise"
    low, high = min(creators), max(creators)
    return (
        f"  creator fee spans {low} to {high} bps "
        f"({low / 100:.4f}% to {high / 100:.4f}% of volume)\n"
        f"  at TOLL_BPS {TOLL_BPS}, the toll spans "
        f"{low / 100 * TOLL_BPS / 10_000:.5f}% to {high / 100 * TOLL_BPS / 10_000:.5f}% of volume"
    )


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--rpc", help="comma-separated RPC endpoints (env CHARLIE_RPC_URLS)")
    parser.add_argument("--volume", type=float, default=1_000_000.0,
                        help="daily trade volume in dollars, for the worked example")
    parser.add_argument("--sol", type=float, default=200.0, help="SOL price in dollars")
    args = parser.parse_args(argv)

    raw = args.rpc or os.environ.get("CHARLIE_RPC_URLS") or ""
    endpoints = tuple(url.strip() for url in raw.split(",") if url.strip()) or DEFAULT_ENDPOINTS
    rpc = RpcClient(endpoints)
    idl = read_idl(rpc, PUMP_FEE_SHARE_PROGRAM)

    configs = {}
    for label, program_id in (
        ("bonding curve (pump)", PUMP_PROGRAM),
        ("after graduation (pump AMM)", PUMP_AMM_PROGRAM),
    ):
        try:
            address, config = read_fee_config(rpc, idl, program_id)
        except Exception as exc:                    # noqa: BLE001 - reported, not raised
            print(f"{label}\n  unreadable     {type(exc).__name__}: {exc}\n")
            continue
        configs[label] = config
        print(render(label, address, config))
        print(summarise(config))
        print()

    # What the spec actually claims, restated against the numbers just read.
    for label, config in configs.items():
        creators = [t["fees"]["creator_fee_bps"] for t in (config.get("fee_tiers") or [])]
        if not creators:
            continue
        for creator_bps in (min(creators), max(creators)):
            fee = args.volume * creator_bps / 10_000
            toll = fee * TOLL_BPS / 10_000
            print(
                f"{label}: ${args.volume:,.0f}/day at {creator_bps} bps creator fee"
                f" -> ${fee:,.2f}/day of creator fee, ${toll:,.2f}/day of toll"
                f" ({toll / args.sol:.4f} SOL)"
            )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
