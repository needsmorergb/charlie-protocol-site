"""pump's `buy_v2` on a live bonding curve, built from the on-chain IDL.

The legacy `buy` answered 6062 BuybackFeeRecipientMissing on mainnet
(2026-09-05): pump now charges a buyback fee, and only `buy_v2` names its
recipient. This resolves `buy_v2`'s accounts from the deployed IDL's own
seeds with the probes' resolver, wraps the lot into wSOL the way the AMM
path does, buys exactly the tokens the curve's arithmetic quotes for the
lot, burns them, unwraps the rest, and asks mainnet. It prints the
resolved account list so the keeper's builder can be pinned to it.

    python tools/probe_curve_buy.py --auto
    python tools/probe_curve_buy.py <mint>

Reads only. Nothing signs, nothing is sent.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from indexer import buyback, curvebuy, pump                 # noqa: E402
from indexer.base58 import encode                           # noqa: E402
from indexer.enroll import associated_token_address         # noqa: E402
from indexer.message import compile_legacy                  # noqa: E402
from indexer.pump import TOKEN_PROGRAM, _raw                # noqa: E402
from indexer.rpc import RpcClient                           # noqa: E402
from tools.idl_dump import read_idl                         # noqa: E402
from tools.sample_new_coins import endpoints_from, sample   # noqa: E402
from tools.simulate_create_config import (                  # noqa: E402
    encode_args, error_name, find_instruction, resolve_accounts,
)

STAND_IN = "burn111111111111111111111111111111111111111"

# Global, past the fields curvebuy reads: fee_recipients [7] end at 386,
# then set_creator_authority, admin_set_creator_authority, create_v2_enabled
# u8, whitelist_pda, reserved_fee_recipient, mayhem_mode_enabled u8,
# reserved_fee_recipients [7], is_cashback_enabled u8,
# buyback_fee_recipients [8], buyback_basis_points u64.
_BUYBACK_RECIPIENTS = 741
_BUYBACK_BPS = 997


def buyback_recipients(account) -> tuple[list[str], int]:
    data = _raw(account, b"", "pump global", (pump.PUMP_PROGRAM,))
    if len(data) < _BUYBACK_BPS + 8:
        raise pump.DecodeError(f"pump global is {len(data)} bytes; no buyback fields")
    recipients = [encode(data[_BUYBACK_RECIPIENTS + 32 * i:_BUYBACK_RECIPIENTS + 32 * (i + 1)]) for i in range(8)]
    return recipients, int.from_bytes(data[_BUYBACK_BPS:_BUYBACK_BPS + 8], "little")


def probe(rpc, idls, mint: str, wallet: str, lot: int) -> int:
    print(f"coin           {mint}")
    try:
        state = curvebuy.observe(rpc, mint, wallet)
    except buyback.BuybackError as exc:
        print(f"  SKIPPED        {exc}")
        return 2
    recipients, buyback_bps = buyback_recipients(rpc.accounts([curvebuy.GLOBAL])[0])
    live = [r for r in recipients if r != buyback.DEFAULT_PUBKEY]
    print(f"  buyback        {buyback_bps} bps to one of {len(live)} recipients; first {live[0] if live else None}")
    amount = curvebuy.amount_for_lot(lot, state.curve, state.fees, state.charge_creator, 100)
    cost = curvebuy.cost_of(amount, state.curve, state.fees, state.charge_creator)
    print(f"  amount         {amount}  expected cost {cost['total']} lamports of {lot}")

    program_id, idl, entry = find_instruction(idls, "buy_v2")
    base_tp = state.token_program
    user_base = associated_token_address(wallet, mint, base_tp)
    context = {
        "base_mint": mint, "quote_mint": buyback.WSOL_MINT,
        "base_token_program": base_tp, "quote_token_program": TOKEN_PROGRAM,
        "fee_recipient": state.global_.fee_recipient,
        "buyback_fee_recipient": live[0] if live else buyback.DEFAULT_PUBKEY,
        "user": wallet, "associated_base_user": user_base,
        "fee_program": buyback.FEE_PROGRAM,
    }
    metas = resolve_accounts(entry, program_id, context)
    print("  buy_v2 accounts, resolved from the IDL:")
    for index, (name, address, signer, writable) in enumerate(metas):
        print(f"    {index:>2} {'s' if signer else '-'}{'w' if writable else '-'} {name:<40} {address}")
    data, notes = encode_args(entry, idl, {"amount": amount, "max_sol_cost": lot})
    print(f"  args           {notes}")
    buy = (program_id, [(a, s, w) for _n, a, s, w in metas], bytes(entry["discriminator"]) + data)

    user_quote = associated_token_address(wallet, buyback.WSOL_MINT, TOKEN_PROGRAM)
    instructions = [
        buyback.ix_compute_unit_limit(buyback.DEFAULT_COMPUTE_UNITS),
        buyback.ix_create_ata_idempotent(wallet, user_base, wallet, mint, base_tp),
        buyback.ix_create_ata_idempotent(wallet, user_quote, wallet, buyback.WSOL_MINT, TOKEN_PROGRAM),
        buyback.ix_system_transfer(wallet, user_quote, lot),
        buyback.ix_sync_native(user_quote),
        buy,
        buyback.ix_burn(base_tp, user_base, mint, wallet, amount),
        buyback.ix_close_account(user_quote, wallet, wallet),
    ]
    if not state.user_volume_exists:
        instructions.insert(1, curvebuy.ix_init_user_volume_accumulator(wallet))
    blockhash = rpc.call("getLatestBlockhash", [{"commitment": "finalized"}])["value"]["blockhash"]
    message = compile_legacy(wallet, instructions, blockhash)
    value = buyback.simulate(rpc, message)
    err = value.get("err")
    if err is None:
        print(f"\n  SIMULATION     OK -- {value.get('unitsConsumed')} compute units, {len(instructions)} instructions")
        for line in value.get("logs") or []:
            if "Instruction:" in line:
                print(f"    | {line}")
        return 0
    print(f"\n  SIMULATION     FAILED  err={err}")
    if isinstance(err, dict):
        custom = (err.get("InstructionError") or [None, {}])[1]
        if isinstance(custom, dict) and "Custom" in custom:
            print(f"  error          {custom['Custom']}  {error_name(idls, custom['Custom'], program_id)}")
    for line in (value.get("logs") or [])[-14:]:
        print(f"    | {line}")
    return 1


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
    idls = {pump.PUMP_PROGRAM: read_idl(rpc, pump.PUMP_PROGRAM)}
    lot = int(round(args.lot * buyback.LAMPORTS_PER_SOL))
    mints = list(args.mints)
    if args.auto:
        rows = sample(rpc, want=args.auto_limit, scan=args.auto_scan, delay=args.auto_delay)
        mints += [r["mint"] for r in rows if r.get("curve") and not r["curve"].get("complete")]
    for mint in mints:
        status = probe(rpc, idls, mint, args.wallet, lot)
        print()
        if status != 2:
            return status
    print("no coin on its curve to probe")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
