"""Does the toll survive GRADUATION? Measured on coins that already have.

FEE-ROUTING points a coin's whole pump creator fee at a PDA and splits it.
Before graduation the fee is lamports in pump's `creator-vault` PDA and
`distribute_creator_fees` pays the shareholders out of it. After graduation
the same fee is collected by the AMM instead, as wSOL sitting in a token
account nothing in the existing crank ever touches. If the crank keeps
calling only `distribute_creator_fees`, the coins with the most volume are
exactly the ones it silently stops paying.

Five things measured here, none inferred from documents:

  1. For a coin that HAS graduated and HAS a sharing config, does
     `distribute_creator_fees` still pay? Lamport deltas, not `err is None`.
  2. Does the AMM `Pool.coin_creator` equal that coin's SHARING CONFIG PDA?
     This is the crux. If graduation carried `bonding_curve.creator` (which
     for a fee-shared coin IS the config) into `Pool.coin_creator`, the fee
     still lands under the config and only the LOCATION changed. If not, the
     toll is gone. Real pools are read and the three addresses compared.
  3. The whole crank for a graduated coin in ONE simulated transaction:
     `pump_amm::transfer_creator_fees_to_pump`, then
     `pump::distribute_creator_fees`, with deltas at each step.
  4. Is the AMM-side transfer permissionless, and does it have a floor?
  5. Of coins carrying a sharing config, what fraction have graduated?

Every account, discriminator and layout comes from the three programs'
ON-CHAIN Anchor IDLs, and every instruction is built by the same IDL-driven
resolver `simulate_create_config` already uses. Nothing signs, nothing is
sent: `sigVerify: false` runs the real programs against real mainnet state.

    python tools/graduated_fee_routing.py
    python tools/graduated_fee_routing.py --mints <graduated mint> ...
"""

from __future__ import annotations

import argparse
import base64
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from indexer import pump                                    # noqa: E402
from indexer.base58 import encode, pubkey_bytes             # noqa: E402
from indexer.curve import find_program_address              # noqa: E402
from indexer.rpc import RpcClient                           # noqa: E402
from tools.idl_dump import read_idl                         # noqa: E402
from tools.sample_new_coins import endpoints_from           # noqa: E402
from tools.simulate_collector_payout import (               # noqa: E402
    DEFAULT_PAYER,
    creator_vault,
    lamports_of,
    read_accounts,
    run,
    shape_of,
    transfer_instruction,
)
from tools.simulate_create_config import (                  # noqa: E402
    ASSOCIATED_TOKEN_PROGRAM,
    SYSTEM_PROGRAM,
    TOKEN_PROGRAM,
    WSOL_MINT,
    _read,
    error_name,
    find_instruction,
    resolve_accounts,
    type_def,
)

# The fee-share program's own refusal the brief names. It is NOT pump's 6019
# (`InvalidCreator`) -- the same number means different things in the two
# programs, so which program failed is always printed next to the code.
FEE_SHARE_6019 = "AmmAccountsRequiredForGraduatedCoin"

# pump's bonding curve, byte for byte, as `indexer.pump` already reads it.
CURVE_COMPLETE_OFFSET = 48
CURVE_CREATOR_OFFSET = 49
# 8 disc | 5x u64 | complete 1 | creator 32 | is_mayhem 1 | is_cashback 1
CURVE_QUOTE_MINT_OFFSET = 83

# SPL token account: mint 32 | owner 32 | amount u64.
TOKEN_AMOUNT_OFFSET = 64

CONFIG_MINT_OFFSET = 11
DISC_SHARING_CONFIG = pump.DISC_SHARING_CONFIG


# -- derivations ----------------------------------------------------------
def sharing_config_pda(mint: str) -> str:
    return find_program_address(
        [b"sharing-config", pubkey_bytes(mint)], pump.PUMP_FEE_SHARE_PROGRAM
    )[0]


def pool_authority(mint: str) -> str:
    """pump's migration signer. It is the AMM pool's `creator` -- which is
    NOT the same field as `coin_creator`, and confusing the two is how a
    reader talks themselves into believing the toll survived."""
    return find_program_address([b"pool-authority", pubkey_bytes(mint)], pump.PUMP_PROGRAM)[0]


def canonical_pool(mint: str, quote_mint: str) -> str:
    """The pool `pump::migrate` creates: index 0, creator = pool_authority."""
    return find_program_address(
        [
            b"pool",
            (0).to_bytes(2, "little"),
            pubkey_bytes(pool_authority(mint)),
            pubkey_bytes(mint),
            pubkey_bytes(quote_mint),
        ],
        pump.PUMP_AMM_PROGRAM,
    )[0]


def amm_vault_authority(coin_creator: str) -> str:
    """UNDERSCORE. The AMM seeds `creator_vault`; pump seeds `creator-vault`.
    Two different accounts under two different programs, one character
    apart, and the whole question is whether money moves between them."""
    return find_program_address(
        [b"creator_vault", pubkey_bytes(coin_creator)], pump.PUMP_AMM_PROGRAM
    )[0]


def ata(owner: str, mint: str, token_program: str = TOKEN_PROGRAM) -> str:
    return find_program_address(
        [pubkey_bytes(owner), pubkey_bytes(token_program), pubkey_bytes(mint)],
        ASSOCIATED_TOKEN_PROGRAM,
    )[0]


# -- account reads --------------------------------------------------------
def account_data(account) -> bytes:
    raw = (account or {}).get("data")
    if not raw:
        return b""
    blob = raw[0] if isinstance(raw, list) else raw
    try:
        return base64.b64decode(blob)
    except Exception:                                       # noqa: BLE001
        return b""


def token_amount(account) -> int | None:
    """The wSOL balance of a token account, or None if it is not one."""
    data = account_data(account)
    if len(data) < TOKEN_AMOUNT_OFFSET + 8:
        return None
    return int.from_bytes(data[TOKEN_AMOUNT_OFFSET : TOKEN_AMOUNT_OFFSET + 8], "little")


def decode_pool(idls, account) -> dict | None:
    """The AMM `Pool`, decoded through the AMM's own IDL type -- so a layout
    change fails a decode instead of printing a plausible wrong pubkey."""
    idl = idls.get(pump.PUMP_AMM_PROGRAM)
    if idl is None:
        return None
    definition = type_def(idl, "Pool")
    if definition is None:
        return None
    data = account_data(account)
    if len(data) < 16:
        return None
    out = {}
    off = 8
    for field in definition.get("fields", []):
        try:
            out[field["name"]], off = _read(data, off, field["type"], idl)
        except Exception as exc:                            # noqa: BLE001 - reported
            out[field["name"]] = f"<undecodable: {exc}>"
            break
    return out


# -- Q5: the population ---------------------------------------------------
def sample_population(rpc, *, buckets: tuple[int, ...]) -> dict:
    """An unbiased slice of every sharing config on chain.

    A full sweep of the fee-sharing program is a ~965 MB response at today's
    scale and no gateway serves it. So this filters on ONE BYTE of the mint
    the config records (offset 11), which is uniformly distributed because a
    pump mint's vanity is its SUFFIX, not its head. Each bucket is therefore
    about 1/256 of the population drawn at random, and asking for several
    buckets both grows the sample and shows whether the buckets agree.
    """
    rows: list[dict] = []
    per_bucket = []
    for byte in buckets:
        filters = [
            {"memcmp": {"offset": 0, "bytes": encode(DISC_SHARING_CONFIG)}},
            {"memcmp": {"offset": CONFIG_MINT_OFFSET, "bytes": encode(bytes([byte]))}},
        ]
        try:
            entries = rpc.program_accounts(
                pump.PUMP_FEE_SHARE_PROGRAM,
                filters=filters,
                data_slice={"offset": 0, "length": pump.SINGLE_SHAREHOLDER_SLICE},
            )
        except Exception as exc:                            # noqa: BLE001 - reported
            print(f"  bucket 0x{byte:02x}: getProgramAccounts refused: "
                  f"{type(exc).__name__}: {exc}")
            continue
        print(f"  bucket 0x{byte:02x}: {len(entries)} sharing config(s)")
        per_bucket.append((byte, len(entries)))
        for entry in entries:
            address = entry.get("pubkey")
            account = entry.get("account")
            try:
                config = pump.decode_sharing_config(address, account)
            except pump.TruncatedConfig as exc:
                # More than one shareholder: the slice cut the vec short. The
                # mint and the header are still exact, and the mint is all
                # this census needs.
                rows.append({"config": address, "mint": exc.mint, "truncated": True,
                             "bucket": byte})
                continue
            except pump.DecodeError:
                continue
            rows.append({
                "config": address, "mint": config.mint, "truncated": False,
                "bucket": byte,
                "status": config.status, "admin": config.admin,
                "admin_revoked": config.admin_revoked,
                "shareholders": config.shareholders,
            })
    return {"rows": rows, "per_bucket": per_bucket}


def census(rpc, sampled: dict) -> dict:
    """`complete` read off every sampled coin's bonding curve, in batches.

    The flag is the same byte `indexer/pump.py` reads, requested as a
    33-byte slice (complete + creator) so a 2000-coin census is a handful of
    small responses rather than a megabyte of curve reserves nobody uses.
    """
    rows = sampled["rows"]
    curves = [pump.bonding_curve(r["mint"]) for r in rows]
    graduated = 0
    live = 0
    missing = 0
    creator_is_config = 0
    graduated_rows: list[dict] = []
    for start in range(0, len(curves), 100):
        chunk = curves[start : start + 100]
        result = rpc.call(
            "getMultipleAccounts",
            [chunk, {"encoding": "base64", "commitment": "processed",
                     "dataSlice": {"offset": CURVE_COMPLETE_OFFSET, "length": 33}}],
        )
        values = list((result or {}).get("value") or [])
        values.extend([None] * (len(chunk) - len(values)))
        for row, account in zip(rows[start : start + 100], values):
            data = account_data(account)
            if len(data) < 33:
                missing += 1
                row["graduated"] = None
                continue
            row["graduated"] = bool(data[0])
            row["curve_creator"] = encode(data[1:33])
            if row["curve_creator"] == row["config"]:
                creator_is_config += 1
            if row["graduated"]:
                graduated += 1
                graduated_rows.append(row)
            else:
                live += 1
    # Per bucket, because the buckets disagreed on SIZE the first time this
    # ran (1955 against 2942), which means the mint's first byte is not the
    # uniform coin flip the sampler assumed. Sizes differing does not bias
    # the graduated FRACTION unless graduation correlates with that byte --
    # and the only way to see whether it does is to print the fraction per
    # bucket and let the reader compare them.
    by_bucket: dict[int, list[int]] = {}
    for row in rows:
        if row.get("graduated") is None:
            continue
        entry = by_bucket.setdefault(row.get("bucket"), [0, 0])
        entry[0] += 1
        entry[1] += 1 if row["graduated"] else 0
    return {
        "counted": graduated + live, "graduated": graduated, "live": live,
        "missing": missing, "creator_is_config": creator_is_config,
        "graduated_rows": graduated_rows, "by_bucket": by_bucket,
    }


# -- Q2: where the AMM sends a graduated coin's fee -----------------------
def batch_read(rpc, addresses: list[str], *, data_slice: dict | None = None) -> list:
    """`getMultipleAccounts` in chunks of 100. One round trip per hundred
    accounts instead of one per account -- the difference between a run that
    finishes and a run that times out against a rate-limited gateway."""
    out: list = []
    for start in range(0, len(addresses), 100):
        chunk = addresses[start : start + 100]
        options = {"encoding": "base64", "commitment": "processed"}
        if data_slice is not None:
            options["dataSlice"] = data_slice
        result = rpc.call("getMultipleAccounts", [chunk, options])
        values = list((result or {}).get("value") or [])
        values.extend([None] * (len(chunk) - len(values)))
        out.extend(values[: len(chunk)])
    return out


def pool_report(rpc, idls, rows: list[dict], *, limit: int) -> list[dict]:
    """For each graduated coin: the pool, its `coin_creator`, and whether
    that address is the coin's sharing config. Plus the AMM-side vault the
    fee actually sits in and how much is in it right now.

    Four batched round trips for the whole set, not four per coin.
    """
    rows = rows[:limit]
    if not rows:
        return []

    # 1. bonding curves, sliced from `complete` through `quote_mint`.
    curves = batch_read(
        rpc, [pump.bonding_curve(r["mint"]) for r in rows],
        data_slice={"offset": CURVE_COMPLETE_OFFSET,
                    "length": CURVE_QUOTE_MINT_OFFSET - CURVE_COMPLETE_OFFSET + 32},
    )
    for row, account in zip(rows, curves):
        data = account_data(account)
        if len(data) < 33:
            row["pool_note"] = "bonding curve unreadable"
            continue
        row["graduated"] = bool(data[0])
        row["curve_creator"] = encode(data[1:33])
        quote = WSOL_MINT
        if len(data) >= 67:
            candidate = encode(data[35:67])
            if candidate != SYSTEM_PROGRAM:
                quote = candidate
        row["quote_mint"] = quote
        row["pool"] = canonical_pool(row["mint"], quote)

    # 2. the pools themselves.
    have_pool = [r for r in rows if r.get("pool")]
    pools = batch_read(rpc, [r["pool"] for r in have_pool])
    for row, account in zip(have_pool, pools):
        if not account or account.get("owner") != pump.PUMP_AMM_PROGRAM:
            row["pool_note"] = f"no canonical pool at {row['pool']}"
            continue
        decoded = decode_pool(idls, account) or {}
        row["pool_creator"] = decoded.get("creator")
        row["coin_creator"] = decoded.get("coin_creator")
        row["pool_base_mint"] = decoded.get("base_mint")
        row["pool_quote_mint"] = decoded.get("quote_mint")
        row["coin_creator_is_config"] = row.get("coin_creator") == row["config"]
        row["coin_creator_is_curve_creator"] = (
            row.get("coin_creator") == row.get("curve_creator")
        )

    # 3. the two vaults the fee can be sitting in, for whichever address the
    #    pool actually names -- NOT for the config we hoped it would name.
    live = [r for r in rows if r.get("coin_creator")]
    for row in live:
        row["amm_vault_authority"] = amm_vault_authority(row["coin_creator"])
        row["amm_vault_ata"] = ata(row["amm_vault_authority"], row["quote_mint"])
        row["pump_creator_vault"] = creator_vault(row["coin_creator"])
    atas = batch_read(rpc, [r["amm_vault_ata"] for r in live])
    vaults = batch_read(rpc, [r["pump_creator_vault"] for r in live])
    for row, ata_account, vault_account in zip(live, atas, vaults):
        row["amm_vault_wsol"] = token_amount(ata_account)
        row["amm_vault_ata_exists"] = ata_account is not None
        row["pump_vault_lamports"] = lamports_of(vault_account)
    return rows


def print_pool_row(row: dict) -> None:
    print(f"  {row['mint']}   graduated={row.get('graduated')}")
    print(f"    sharing config     {row['config']}")
    print(f"    bonding_curve.creator {row.get('curve_creator')}"
          f"   == config: {row.get('curve_creator') == row['config']}")
    if row.get("pool_note"):
        print(f"    pool               {row.get('pool')}  [{row['pool_note']}]")
        return
    print(f"    pool               {row['pool']}  (quote {row.get('quote_mint')})")
    print(f"    Pool.creator       {row.get('pool_creator')}   "
          f"(= pool-authority PDA, the migration signer, NOT the fee target)")
    print(f"    Pool.coin_creator  {row.get('coin_creator')}")
    print(f"      == sharing config PDA        {row.get('coin_creator_is_config')}")
    print(f"      == bonding_curve.creator     {row.get('coin_creator_is_curve_creator')}")
    print(f"    AMM vault authority {row.get('amm_vault_authority')}")
    print(f"    AMM vault wSOL ATA  {row.get('amm_vault_ata')}  "
          f"exists={row.get('amm_vault_ata_exists')}  wSOL={row.get('amm_vault_wsol')}")
    print(f"    pump creator-vault  {row.get('pump_creator_vault')}  "
          f"lamports={row.get('pump_vault_lamports')}")


# -- instructions ---------------------------------------------------------
def context_for(row: dict, identity: str) -> dict:
    return {
        "mint": row["mint"],
        "base_mint": row["mint"],
        "bonding_curve.creator": row.get("curve_creator") or row["config"],
        "coin_creator": row.get("coin_creator") or row["config"],
        "creator": identity,
        "payer": identity,
        "user": identity,
        "admin": identity,
        "authority": identity,
        "signer": identity,
        "fee_payer": identity,
        "sharing_config": row["config"],
        "config": row["config"],
        "quote_mint": row.get("quote_mint") or WSOL_MINT,
        "wsol_mint": row.get("quote_mint") or WSOL_MINT,
        "token_program": TOKEN_PROGRAM,
        "quote_token_program": TOKEN_PROGRAM,
    }


def amm_transfer_instruction(idls, row: dict, identity: str):
    program_id, _idl, entry = find_instruction(idls, "transfer_creator_fees_to_pump")
    if entry is None:
        raise LookupError("transfer_creator_fees_to_pump is in no IDL read here")
    metas = resolve_accounts(entry, program_id, context_for(row, identity))
    return (program_id, metas, bytes(entry["discriminator"])), entry


def distribute_instruction(idls, row: dict, identity: str, shareholders):
    program_id, _idl, entry = find_instruction(idls, "distribute_creator_fees")
    if entry is None:
        raise LookupError("distribute_creator_fees is in no IDL read here")
    metas = resolve_accounts(entry, program_id, context_for(row, identity))
    metas += [(f"shareholder[{i}]", a, False, True) for i, a in enumerate(shareholders)]
    return (program_id, metas, bytes(entry["discriminator"]))


def minimum_instruction(idls, row: dict, identity: str, shareholders):
    program_id, _idl, entry = find_instruction(idls, "get_minimum_distributable_fee")
    metas = resolve_accounts(entry, program_id, context_for(row, identity))
    metas += [(f"shareholder[{i}]", a, False, False) for i, a in enumerate(shareholders)]
    return (program_id, metas, bytes(entry["discriminator"]))


# -- reporting ------------------------------------------------------------
def show(idls, outcome: dict, *, tokens: tuple[str, ...] = ()) -> None:
    err = outcome["err"]
    if err is None:
        print("    err          None")
    else:
        print(f"    err          {err}")
        if outcome["code"] is not None:
            print(f"    failing ix   #{outcome['failing_index']} in "
                  f"{outcome['failing_program']}")
            print(f"    error        {outcome['code']}  "
                  f"{error_name(idls, outcome['code'], outcome['failing_program'])}")
    for address in outcome["watched"]:
        before = outcome["before"][address]
        after = outcome["after"][address]
        if err is not None:
            print(f"    lamports     {address}\n"
                  f"                 before {before}  after NOT MEASURED (tx failed)")
            continue
        line = (f"    lamports     {address}\n"
                f"                 before {before}  after {after}  delta {after - before:+d}")
        if address in tokens:
            was = token_amount(outcome["before_accounts"].get(address))
            now = token_amount(outcome["after_accounts"].get(address))
            if was is None and now is None:
                line += "\n                 wSOL   before (no token account) after (none)"
            else:
                delta = (now or 0) - (was or 0)
                line += (f"\n                 wSOL   before {was} after {now} "
                         f"delta {delta:+d}")
        print(line)
    for source, name, fields in outcome["events"]:
        print(f"    event        [{source}] {name}")
        for key, item in fields.items():
            print(f"                   {key:<22} {item}")
    for line in outcome["logs"]:
        if any(k in line for k in ("Error", "error", "failed", "Minimum", "distribut",
                                   "Transfer", "insufficient", "Insufficient")):
            print(f"      | {line}")


def simulate(rpc, idls, ixs, *, payer, watch, label, tokens=()):
    print(f"\n--- {label}")
    outcome = run(rpc, idls, ixs, payer=payer, watch=watch, label=label)
    if outcome["missing"]:
        print(f"    (not in tx, unmeasurable: {outcome['missing']})")
    show(idls, outcome, tokens=tokens)
    return outcome


# -- the run --------------------------------------------------------------
def measure_coin(rpc, idls, row: dict, *, payer: str, top_up: int) -> None:
    mint = row["mint"]
    holders = [a for a, _bps in (row.get("shareholders") or ())]
    print(f"\n{'=' * 72}\n### {mint}   graduated coin with a sharing config")
    print_pool_row(row)
    print(f"    shareholders       {row.get('shareholders')}")
    if row.get("truncated"):
        print("    NOTE: the sampled slice held only one shareholder record; the "
              "config may name more, and distribute_creator_fees requires ALL of "
              "them as remaining accounts. Re-read in full below.")
    if not holders:
        print("    no shareholders readable -- cannot build the remaining accounts")
        return
    if not row.get("coin_creator"):
        print("    no pool -- nothing to measure on the AMM side")
        return

    vault = row["pump_creator_vault"]
    vault_ata = row["amm_vault_ata"]
    authority = row["amm_vault_authority"]
    watch = [vault, vault_ata, authority] + holders

    # pump's own floor for THIS coin, in pump's words.
    simulate(rpc, idls, [minimum_instruction(idls, row, payer, holders)],
             payer=payer, watch=[vault], label="get_minimum_distributable_fee")

    # (1) The crank as it exists today: distribute only. If graduation broke
    # `distribute_creator_fees` itself this fails; if it merely moved the
    # money, this succeeds and pays whatever is still in the pump vault.
    simulate(rpc, idls, [distribute_instruction(idls, row, payer, holders)],
             payer=payer, watch=watch, tokens=(vault_ata,),
             label="distribute_creator_fees ALONE (today's crank, graduated coin)")

    # (2) Same, with the pump vault funded past the floor in the same
    # transaction, so a zero cannot be blamed on the floor. This isolates
    # "does the instruction still pay a graduated coin's shareholders".
    simulate(rpc, idls,
             [transfer_instruction(payer, vault, top_up),
              distribute_instruction(idls, row, payer, holders)],
             payer=payer, watch=watch, tokens=(vault_ata,),
             label=f"distribute_creator_fees with {top_up} lamports put in the pump vault")

    # (3) The AMM side alone. No signer of its own -- see the account dump.
    transfer_ix, entry = amm_transfer_instruction(idls, row, payer)
    signers = [a["name"] for a in entry["accounts"] if a.get("signer")]
    print(f"\n    transfer_creator_fees_to_pump declares signers: {signers or 'NONE'}")
    for name, address, signer, writable in transfer_ix[1]:
        print(f"      {name:<28} {address}  "
              f"{'signer ' if signer else ''}{'writable' if writable else ''}")
    simulate(rpc, idls, [transfer_ix], payer=payer, watch=watch, tokens=(vault_ata,),
             label="pump_amm::transfer_creator_fees_to_pump ALONE")

    # (4) The whole crank, end to end, one transaction.
    simulate(rpc, idls,
             [transfer_ix, distribute_instruction(idls, row, payer, holders)],
             payer=payer, watch=watch, tokens=(vault_ata,),
             label="THE CRANK: transfer_creator_fees_to_pump THEN distribute_creator_fees")


def starvation_check(rpc, idls, rows: list[dict], *, payer: str, limit: int) -> None:
    """The failure mode, on coins where it is currently live.

    A graduated coin's fee accrues in the AMM's wSOL vault, not in pump's
    `creator-vault`. So the crank as designed -- `distribute_creator_fees`
    alone -- runs against an EMPTY pump vault: it returns success, emits its
    event, and pays the shareholders nothing. Nothing errors. Nothing is
    logged as wrong. The money is simply somewhere else.

    Two simulations per coin, on the same accounts in the same minute:
    distribute alone, then transfer-and-distribute. The pair is the whole
    argument -- the second number is what the first one was supposed to be.
    """
    print("\n### Q1 -- distribute alone against transfer-then-distribute, "
          "on coins with money waiting in the AMM")
    candidates = [
        r for r in rows
        if r.get("coin_creator") and (r.get("amm_vault_wsol") or 0) > 0
    ]
    candidates.sort(key=lambda r: (r.get("pump_vault_lamports") or 0))
    configs = batch_read(rpc, [r["config"] for r in candidates[: limit * 3]])
    done = 0
    for row, account in zip(candidates, configs):
        try:
            config = pump.decode_sharing_config(row["config"], account)
        except Exception:                                   # noqa: BLE001
            continue
        holders = [a for a, _bps in config.shareholders]
        if not holders:
            continue
        vault = row["pump_creator_vault"]
        ata_address = row["amm_vault_ata"]
        distribute = distribute_instruction(idls, row, payer, holders)
        transfer_ix, _entry = amm_transfer_instruction(idls, row, payer)
        watch = [vault, ata_address] + holders
        alone = run(rpc, idls, [distribute], payer=payer, watch=watch, label="alone")
        both = run(rpc, idls, [transfer_ix, distribute], payer=payer, watch=watch,
                   label="both")

        def paid(outcome):
            if outcome["err"] is not None:
                return None
            return sum(outcome["after"].get(h, 0) - outcome["before"].get(h, 0)
                       for h in holders)

        print(f"  {row['mint']}")
        print(f"    pump creator-vault {row.get('pump_vault_lamports')} lamports   "
              f"AMM vault {row.get('amm_vault_wsol')} wSOL   "
              f"{len(holders)} shareholder(s)")
        print(f"    distribute alone              err={alone['err']}   "
              f"shareholders {paid(alone)}")
        print(f"    transfer THEN distribute      err={both['err']}   "
              f"shareholders {paid(both)}")
        done += 1
        if done >= limit:
            break
    if not done:
        print("  no graduated coin with a positive AMM vault was reachable")


def floor_sweep(rpc, idls, rows: list[dict], *, payer: str, limit: int) -> None:
    """Q4's second half: does the AMM-side transfer have a floor of its own?

    One simulation per coin, the transfer ALONE, across the whole spread of
    AMM vault balances the sample happens to contain -- smallest first, so
    the smallest balance that still moves is visible rather than assumed.
    An instruction with a floor returns success and moves nothing below it,
    which is exactly what pump's own distribution does, so `err is None` is
    not the measurement. The wSOL delta is.
    """
    print("\n### Q4b -- the AMM transfer against a spread of vault balances")
    print("  wSOL in    -> lamports into pump's creator-vault, transfer alone")
    candidates = [r for r in rows if r.get("coin_creator") and r.get("amm_vault_ata_exists")]
    candidates.sort(key=lambda r: (r.get("amm_vault_wsol") or 0))
    for row in candidates[:limit]:
        vault = row["pump_creator_vault"]
        ata_address = row["amm_vault_ata"]
        try:
            transfer_ix, _entry = amm_transfer_instruction(idls, row, payer)
        except Exception as exc:                            # noqa: BLE001 - reported
            print(f"  {row['mint']}  UNBUILDABLE {exc}")
            continue
        outcome = run(rpc, idls, [transfer_ix], payer=payer,
                      watch=[vault, ata_address, row["amm_vault_authority"]],
                      label="floor")
        was = token_amount(outcome["before_accounts"].get(ata_address))
        now = token_amount(outcome["after_accounts"].get(ata_address))
        moved = (outcome["after"].get(vault, 0) - outcome["before"].get(vault, 0)
                 if outcome["err"] is None else None)
        print(f"  {row['mint']}  wSOL {was} -> {now}   pump vault delta "
              f"{'NOT MEASURED (tx failed)' if moved is None else f'{moved:+d}'}"
              f"   err={outcome['err']}"
              + (f" [{error_name(idls, outcome['code'], outcome['failing_program'])}]"
                 if outcome["code"] is not None else ""))


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--rpc")
    parser.add_argument("--payer", default=DEFAULT_PAYER)
    parser.add_argument("--mints", nargs="*", default=[],
                        help="graduated mints to measure before the sampled ones")
    parser.add_argument("--buckets", type=int, default=1,
                        help="how many 1/256 slices of the config population to draw")
    parser.add_argument("--pools", type=int, default=60,
                        help="graduated coins whose Pool.coin_creator to read")
    parser.add_argument("--measure", type=int, default=2,
                        help="graduated coins to run the full simulation on")
    parser.add_argument("--top-up", type=int, default=50_000_000)
    parser.add_argument("--starvation", type=int, default=5,
                        help="graduated coins to run distribute-alone against "
                             "transfer-then-distribute on")
    parser.add_argument("--floor-sweep", type=int, default=8,
                        help="graduated coins to run the AMM transfer alone against, "
                             "smallest vault first, to find its floor")
    args = parser.parse_args(argv)

    rpc = RpcClient(endpoints_from(args.rpc), max_retries=10)

    idls = {}
    for program_id in (pump.PUMP_PROGRAM, pump.PUMP_FEE_SHARE_PROGRAM, pump.PUMP_AMM_PROGRAM):
        try:
            idls[program_id] = read_idl(rpc, program_id)
            names = [i["name"] for i in idls[program_id]["instructions"]]
            print(f"IDL {program_id}  {len(names)} instructions")
        except Exception as exc:                            # noqa: BLE001 - reported
            print(f"IDL {program_id}: UNREADABLE {exc}")
            return 1

    amm = idls[pump.PUMP_AMM_PROGRAM]
    have = [i["name"] for i in amm["instructions"]]
    print("\n### the AMM instruction the design needs, as the deployed program declares it")
    for name in ("transfer_creator_fees_to_pump", "collect_coin_creator_fee",
                 "migrate_pool_coin_creator", "set_coin_creator", "admin_set_coin_creator"):
        print(f"  {name:<32} {'PRESENT' if name in have else 'ABSENT'}")
    for code in (6019,):
        print(f"  fee-share {code} = "
              f"{error_name(idls, code, pump.PUMP_FEE_SHARE_PROGRAM)}"
              f"   |  pump {code} = {error_name(idls, code, pump.PUMP_PROGRAM)}")

    payer_account = read_accounts(rpc, [args.payer])[0]
    print(f"\nfee payer  {args.payer}  {shape_of(args.payer, payer_account)} "
          f"lamports={lamports_of(payer_account)}")

    # -- Q5 -------------------------------------------------------------
    print("\n### Q5 -- of coins with a sharing config, how many have graduated")
    buckets = tuple(range(args.buckets))
    sampled = sample_population(rpc, buckets=buckets)
    print(f"  sampled {len(sampled['rows'])} config(s) across "
          f"{len(sampled['per_bucket'])} bucket(s) of ~1/256 each")
    if sampled["per_bucket"]:
        drawn = sum(n for _b, n in sampled["per_bucket"])
        print(f"  implied population       ~{drawn * 256 // len(sampled['per_bucket']):,} "
              f"sharing configs on chain (bucket counts "
              f"{[n for _b, n in sampled['per_bucket']]})")
    else:
        print("  NO bucket was served. Q5 is UNRESOLVED unless mints were named.")
    counted = census(rpc, sampled) if sampled["rows"] else {
        "counted": 0, "graduated": 0, "live": 0, "missing": 0,
        "creator_is_config": 0, "graduated_rows": [],
    }
    total = counted["counted"] or 1
    print(f"  bonding curves read      {counted['counted']} "
          f"({counted['missing']} unreadable)")
    print(f"  GRADUATED                {counted['graduated']}  "
          f"({100.0 * counted['graduated'] / total:.1f}%)")
    print(f"  still on the curve       {counted['live']}  "
          f"({100.0 * counted['live'] / total:.1f}%)")
    print(f"  bonding_curve.creator == its own sharing config: "
          f"{counted['creator_is_config']} of {counted['counted']}")
    for byte, (n, grad) in sorted((counted.get("by_bucket") or {}).items()):
        print(f"    bucket 0x{byte:02x}: {grad}/{n} graduated "
              f"({100.0 * grad / max(n, 1):.1f}%)")

    # -- Q2 -------------------------------------------------------------
    print("\n### Q2 -- does Pool.coin_creator equal the sharing config PDA")
    named = []
    for mint in args.mints:
        named.append({"mint": mint, "config": sharing_config_pda(mint), "truncated": True})
    rows = named + counted["graduated_rows"]
    reported = pool_report(rpc, idls, rows, limit=args.pools + len(named))
    agree = 0
    disagree = 0
    for row in reported:
        print_pool_row(row)
        if row.get("coin_creator_is_config") is True:
            agree += 1
        elif row.get("coin_creator") is not None:
            disagree += 1
    print(f"\n  Pool.coin_creator == sharing config PDA: {agree} yes, {disagree} no, "
          f"{len(reported) - agree - disagree} unresolved")

    # -- Q1, Q3, Q4 ------------------------------------------------------
    print("\n### Q1/Q3/Q4 -- the crank, measured")
    # A config read in full: the sampled slice truncates a multi-shareholder
    # vec, and distribute_creator_fees rejects a short remaining-account list
    # with 6054 rather than paying anything.
    candidates = [r for r in reported if r.get("coin_creator")]
    candidates.sort(key=lambda r: (r.get("amm_vault_wsol") or 0), reverse=True)
    measured = 0
    for row in candidates:
        account = read_accounts(rpc, [row["config"]])[0]
        try:
            config = pump.decode_sharing_config(row["config"], account)
        except Exception as exc:                            # noqa: BLE001 - reported
            print(f"  {row['mint']}: config unreadable in full: {exc}")
            continue
        row["shareholders"] = config.shareholders
        row["status"] = config.status
        row["admin_revoked"] = config.admin_revoked
        row["truncated"] = False
        try:
            measure_coin(rpc, idls, row, payer=args.payer, top_up=args.top_up)
        except Exception as exc:                            # noqa: BLE001 - reported
            print(f"  {row['mint']}: UNMEASURABLE {type(exc).__name__}: {exc}")
            continue
        measured += 1
        if measured >= args.measure:
            break
    if not measured:
        print("  NO graduated coin with a readable pool and config was reachable. "
              "The crank question is UNRESOLVED, not answered.")

    if args.starvation:
        try:
            starvation_check(rpc, idls, reported, payer=args.payer, limit=args.starvation)
        except Exception as exc:                            # noqa: BLE001 - reported
            print(f"  starvation check unreadable: {type(exc).__name__}: {exc}")

    if args.floor_sweep:
        try:
            floor_sweep(rpc, idls, reported, payer=args.payer, limit=args.floor_sweep)
        except Exception as exc:                            # noqa: BLE001 - reported
            print(f"  floor sweep unreadable: {type(exc).__name__}: {exc}")

    # -- the 6019 probe --------------------------------------------------
    print("\n### where AmmAccountsRequiredForGraduatedCoin actually comes from")
    print("  distribute_creator_fees declares no AMM account at all -- printed here")
    _pid, _idl, entry = find_instruction(idls, "distribute_creator_fees")
    for a in entry["accounts"]:
        print(f"    {a['name']:<20} {'optional' if a.get('optional') else ''}")
    print("  create_fee_sharing_config's OPTIONAL accounts (the AMM ones):")
    _pid, _idl, entry = find_instruction(idls, "create_fee_sharing_config")
    for a in entry["accounts"]:
        if a.get("optional"):
            print(f"    {a['name']:<28} optional")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
