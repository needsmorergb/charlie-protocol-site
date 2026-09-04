"""Will pump pay a PROGRAM-OWNED PDA? The one question the collector rests on.

The planned design routes 100% of a coin's pump creator fee to
`collector(mint)`, a PDA owned by a program of ours, which then splits it.
That works only if `distribute_creator_fees` will actually pay such an
account. pump's own IDL carries two refusals aimed at the recipient:

    6052 UnableToDistributeCreatorFeesToExecutableRecipient
    6070 UnableToDistributeCreatorFeesToUninitializedAccount

The incinerator -- system owned, non-existent -- was already simulated and
did not trip either. A program-owned PDA is a different SHAPE and has never
been tested. Neither has a PDA that does not exist yet, which is what a
collector is before the program is deployed.

WHAT THIS RUNS, per recipient shape, in ONE simulated transaction:

    0. system transfer, funding the coin's creator vault above pump's floor
    1. update_fee_shares_v2, setting the shareholders to ONE recipient, 10000 bps
    2. distribute_creator_fees for the same mint, that recipient remaining

Step 0 exists because `err is None` is not payment. pump returns SUCCESS and
distributes NOTHING when the vault is under `get_minimum_distributable_fee`
(observed: "Minimum vault balance needed: 1781760 lamports"). Funding the
vault inside the same transaction removes the floor as an explanation for a
zero, so a zero can only mean pump declined to pay. The floor is also read
per coin from pump itself, via `get_minimum_distributable_fee`, whose
`MinimumDistributableFeeEvent` states `minimum_required`,
`distributable_fees` and `can_distribute`.

PAYMENT IS PROVED THREE WAYS, not one:

  * the recipient's lamports BEFORE (`getMultipleAccounts`) against AFTER
    (`simulateTransaction`'s `accounts` field, which returns post-simulation
    account state);
  * the creator vault's own delta, which must be the mirror image;
  * pump's `DistributeCreatorFeesEvent.distributed`, decoded from the
    self-CPI event out of the inner instructions.

Nothing signs and nothing is sent. `sigVerify: false` runs the real programs
against real mainnet state and hands back the runtime's own answer.

    python tools/simulate_collector_payout.py --auto
    python tools/simulate_collector_payout.py <mint>
    python tools/simulate_collector_payout.py --incinerator <mint>

Every account, discriminator, argument layout and event layout comes from the
two programs' ON-CHAIN Anchor IDLs, as in the other tools here.
"""

from __future__ import annotations

import argparse
import base64
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from indexer import pump                                    # noqa: E402
from indexer.base58 import decode, encode, pubkey_bytes     # noqa: E402
from indexer.curve import find_program_address, is_on_curve  # noqa: E402
from indexer.rpc import RpcClient                           # noqa: E402
from tools.idl_dump import read_idl                         # noqa: E402
from tools.sample_new_coins import endpoints_from, sample    # noqa: E402
from tools.simulate_create_config import (                  # noqa: E402
    ANCHOR_CPI_EVENT,
    SYSTEM_PROGRAM,
    build_message,
    decode_event,
    encode_args,
    error_name,
    find_instruction,
    resolve_accounts,
)

INCINERATOR = "1nc1nerator11111111111111111111111111111111"

# Any funded account works as a simulated fee payer and as the source of the
# vault top-up: with sigVerify off the runtime never checks a signature, it
# checks the message's signer FLAG, which is what the system program reads.
# This is the legacy vanity address, ~179 SOL, belonging to nobody. No key is
# used and nothing is sent.
DEFAULT_PAYER = "burn111111111111111111111111111111111111111"

# Rent exemption for a 0-byte account. The floor pump enforces (1781760) is
# exactly twice this, which is not a coincidence: it leaves the vault rent
# exempt AND makes the payout itself large enough to leave a fresh recipient
# rent exempt. A transfer that leaves either below this fails the whole
# transaction with a rent error rather than a pump error, so the size of the
# top-up is load bearing and is stated rather than assumed.
RENT_EXEMPT_EMPTY = 890_880
DEFAULT_TOP_UP = 50_000_000

# Coins known to carry a live sharing config, used only as a source of an
# EXISTING program-owned account to name as a recipient. Any of their sharing
# configs is a data account owned by the fee-share program and not
# executable -- the same shape `collector(mint)` will have.
KNOWN_CONFIGURED = (
    "9vAV2s4ujZ16yU6V334gQqpgo5a7swYRiUdiwRYvpump",
    "5vxYBj3qbAFCSQr2487AZEzNtmpdPRLZBKHpKuvfpump",
    "9MTfWK8chKHVJq1qnDvRZ2udovpzbP2N4tm2pFEipump",
    "Gbu7JAKhTVtGyRryg8cYPiKNhonXpUqbrZuCDjfUpump",
)

MEANING = {
    6052: "EXECUTABLE RECIPIENT",
    6070: "UNINITIALIZED RECIPIENT",
    6051: "sharing config not active",
    6053: "bonding curve creator != sharing config",
    6054: "remaining accounts do not match shareholders",
}


# -- derivations ----------------------------------------------------------
def creator_vault(config: str) -> str:
    """pump's per-creator fee vault. For a fee-shared coin the creator IS the
    sharing config, so the vault is keyed by the config address."""
    return find_program_address([b"creator-vault", pubkey_bytes(config)], pump.PUMP_PROGRAM)[0]


def collector_pda(mint: str, program_id: str) -> str:
    """`collector(mint)` exactly as the design spells it: PDA(["collect", mint]).

    The program id it is derived under is irrelevant to the question -- what
    is under test is the SHAPE of the account (off curve, never funded), not
    which program would own it once created.
    """
    return find_program_address([b"collect", pubkey_bytes(mint)], program_id)[0]


# -- chain reads ----------------------------------------------------------
def read_accounts(rpc, addresses, commitment: str = "processed"):
    """`getMultipleAccounts` at the SAME commitment the simulation runs at.

    A pre-read taken at a different commitment than the bank the simulation
    executes against would make every delta suspect by construction.
    """
    addresses = list(addresses)
    if not addresses:
        return []
    result = rpc.call(
        "getMultipleAccounts",
        [addresses, {"encoding": "base64", "commitment": commitment}],
    )
    values = list((result or {}).get("value") or [])
    values.extend([None] * (len(addresses) - len(values)))
    return values[: len(addresses)]


def lamports_of(account) -> int:
    return int((account or {}).get("lamports") or 0)


def shape_of(address: str, account) -> str:
    if account is None:
        return "DOES NOT EXIST"
    return (
        f"owner={account.get('owner')} executable={bool(account.get('executable'))} "
        f"bytes={account.get('space')} keyless={not is_on_curve(address)}"
    )


# -- instructions ---------------------------------------------------------
def transfer_instruction(source: str, destination: str, lamports: int):
    """The system program's `transfer`, index 2, u64 lamports."""
    data = (2).to_bytes(4, "little") + int(lamports).to_bytes(8, "little")
    metas = [("from", source, True, True), ("to", destination, False, True)]
    return (SYSTEM_PROGRAM, metas, data)


def context_for(mint: str, creator: str, identity: str, config: str) -> dict:
    return {
        "mint": mint,
        "bonding_curve.creator": creator,
        "creator": identity,
        "payer": identity,
        "user": identity,
        "admin": identity,
        "authority": identity,
        "signer": identity,
        "fee_payer": identity,
        "sharing_config": config,
        "config": config,
    }


def update_instruction(idls, mint, creator, config, admin, recipients):
    """`update_fee_shares_v2` naming exactly `recipients`, bps summing to 10000."""
    program_id, idl, entry = find_instruction(idls, "update_fee_shares_v2")
    if entry is None:
        raise LookupError("update_fee_shares_v2 is in neither on-chain IDL")
    share = 10_000 // len(recipients)
    shares = [{"address": address, "share_bps": share} for address in recipients]
    shares[0]["share_bps"] += 10_000 - share * len(recipients)
    arg_name = (entry.get("args") or [{}])[0].get("name")
    data_args, notes = encode_args(entry, idl, {arg_name: shares} if arg_name else {})
    metas = resolve_accounts(entry, program_id, context_for(mint, creator, admin, config))
    # 6013 NotEnoughRemainingAccounts / 6020 ShareholderAccountMismatch: the
    # new shareholders are passed as Anchor remaining accounts, in the vec's
    # own order.
    metas = metas + [
        (f"shareholder[{i}]", address, False, True) for i, address in enumerate(recipients)
    ]
    return (program_id, metas, bytes(entry["discriminator"]) + data_args), notes, shares


def distribute_instruction(idls, mint, creator, config, identity, recipients):
    program_id, idl, entry = find_instruction(idls, "distribute_creator_fees")
    if entry is None:
        raise LookupError("distribute_creator_fees is in neither on-chain IDL")
    metas = resolve_accounts(entry, program_id, context_for(mint, creator, identity, config))
    metas = metas + [
        (f"shareholder[{i}]", address, False, True) for i, address in enumerate(recipients)
    ]
    return (program_id, metas, bytes(entry["discriminator"]))


def minimum_instruction(idls, mint, creator, config, identity):
    program_id, idl, entry = find_instruction(idls, "get_minimum_distributable_fee")
    if entry is None:
        raise LookupError("get_minimum_distributable_fee is in neither on-chain IDL")
    metas = resolve_accounts(entry, program_id, context_for(mint, creator, identity, config))
    return (program_id, metas, bytes(entry["discriminator"]))


# -- the simulation -------------------------------------------------------
def events_in(value: dict, idls) -> list[tuple[str, str, dict]]:
    """Every Anchor event the simulation emitted, from BOTH places they hide:
    `Program data:` log lines, and the self-CPI inner instruction that
    `emit_cpi!` uses, which never reaches the logs at all."""
    payloads = []
    for line in value.get("logs") or []:
        marker = "Program data: "
        if marker in line:
            try:
                payloads.append(("log", base64.b64decode(line.split(marker, 1)[1].strip())))
            except Exception:                               # noqa: BLE001
                pass
    for group in value.get("innerInstructions") or []:
        for entry in group.get("instructions") or []:
            raw = entry.get("data")
            if not raw:
                continue
            try:
                blob = decode(raw)
            except ValueError:
                continue
            if blob[:8] == ANCHOR_CPI_EVENT:
                payloads.append(("cpi", blob[8:]))
    out = []
    for source, payload in payloads:
        decoded = decode_event(idls, payload)
        if decoded is None:
            out.append((source, f"unknown:{payload[:8].hex()}", {}))
            continue
        name, fields = decoded
        out.append((source, name, fields))
    return out


def run(rpc, idls, program_ixs, *, payer: str, watch: list[str], label: str) -> dict:
    """One simulated transaction, with a pre-read of `watch` at the same
    commitment so every reported delta is a subtraction of two measurements
    rather than an assertion."""
    blockhash = rpc.call("getLatestBlockhash", [{"commitment": "finalized"}])["value"]["blockhash"]
    message, signature_count, ordered = build_message(program_ixs, payer, blockhash)
    unsigned = bytes([signature_count]) + b"\x00" * (64 * signature_count) + message

    # De-duplicated, and only accounts the transaction actually carries:
    # `simulateTransaction` refuses a longer address list than the message has
    # accounts, and returns nothing for an address the message never names.
    seen: list[str] = []
    for address in watch:
        if address not in seen:
            seen.append(address)
    watched = [address for address in seen if address in ordered]
    missing = [address for address in seen if address not in ordered]
    before = read_accounts(rpc, watched)

    result = rpc.call(
        "simulateTransaction",
        [
            base64.b64encode(unsigned).decode(),
            {
                "encoding": "base64",
                "sigVerify": False,
                "replaceRecentBlockhash": True,
                "commitment": "processed",
                "innerInstructions": True,
                "accounts": {"encoding": "base64", "addresses": watched},
            },
        ],
    )
    value = (result or {}).get("value") or {}
    after = list(value.get("accounts") or [])
    after.extend([None] * (len(watched) - len(after)))

    err = value.get("err")
    code = None
    failing_index = None
    failing_program = None
    if isinstance(err, dict):
        entry = err.get("InstructionError") or [None, None]
        failing_index = entry[0]
        custom = entry[1]
        if isinstance(custom, dict) and "Custom" in custom:
            code = custom["Custom"]
        if isinstance(failing_index, int) and failing_index < len(program_ixs):
            failing_program = program_ixs[failing_index][0]

    return {
        "label": label,
        "err": err,
        "code": code,
        "failing_index": failing_index,
        "failing_program": failing_program,
        "logs": value.get("logs") or [],
        "events": events_in(value, idls),
        "watched": watched,
        "missing": missing,
        "before": {a: lamports_of(x) for a, x in zip(watched, before)},
        "after": {a: lamports_of(x) for a, x in zip(watched, after)},
        "before_accounts": dict(zip(watched, before)),
        "after_accounts": dict(zip(watched, after)),
    }


def print_result(idls, outcome: dict, *, recipient: str | None, vault: str, verbose_logs: bool):
    err = outcome["err"]
    if err is None:
        print("  err            None")
    else:
        print(f"  err            {err}")
        if outcome["code"] is not None:
            print(
                f"  failing ix     #{outcome['failing_index']} in {outcome['failing_program']}"
            )
            print(
                f"  pump/fee error {outcome['code']}  "
                f"{error_name(idls, outcome['code'], outcome['failing_program'])}"
                + (f"   [{MEANING[outcome['code']]}]" if outcome["code"] in MEANING else "")
            )
    for address in outcome["watched"]:
        before = outcome["before"][address]
        after = outcome["after"][address]
        tag = ""
        if recipient and address == recipient:
            tag = "  <- RECIPIENT"
        elif address == vault:
            tag = "  <- CREATOR VAULT"
        print(
            f"  lamports       {address}{tag}\n"
            f"                 before {before}  after {after}  delta {after - before:+d}"
        )
    for source, name, fields in outcome["events"]:
        if name in ("DistributeCreatorFeesEvent", "MinimumDistributableFeeEvent",
                    "UpdateFeeSharesEvent", "CollectCreatorFeeEvent"):
            print(f"  event          [{source}] {name}")
            for key, item in fields.items():
                print(f"                   {key:<20} {item}")
        else:
            print(f"  event          [{source}] {name}")
    interesting = [
        line for line in outcome["logs"]
        if "Insufficient" in line or "Error" in line or "failed" in line
        or "Minimum" in line or "distribut" in line.lower()
    ]
    for line in (outcome["logs"] if verbose_logs else interesting):
        print(f"    | {line}")


def verdict(outcome: dict, recipient: str, distributed: int | None) -> str:
    delta = outcome["after"].get(recipient, 0) - outcome["before"].get(recipient, 0)
    if outcome["err"] is not None:
        if outcome["code"] in (6052, 6070):
            return f"REFUSED  {outcome['code']}"
        return f"FAILED   err={outcome['err']} code={outcome['code']}"
    if delta > 0 and (distributed is None or distributed > 0):
        return f"CONFIRMED PAYABLE  +{delta} lamports"
    return f"NO PAYMENT (success, delta {delta:+d})"


def distributed_from(outcome: dict) -> int | None:
    for _source, name, fields in outcome["events"]:
        if name == "DistributeCreatorFeesEvent":
            value = fields.get("distributed")
            return value if isinstance(value, int) else None
    return None


# -- candidates -----------------------------------------------------------
def inspect(rpc, mint: str) -> dict:
    """Everything the funnel needs about one coin, in one place."""
    row = {"mint": mint}
    curve_address = pump.bonding_curve(mint)
    curve_account = read_accounts(rpc, [curve_address])[0]
    if not curve_account or curve_account.get("owner") != pump.PUMP_PROGRAM:
        row["skip"] = "no bonding curve"
        return row
    data = base64.b64decode(curve_account["data"][0])
    row["bonding_curve"] = curve_address
    row["graduated"] = bool(data[48])
    row["creator"] = encode(data[49:81])
    creator_account = read_accounts(rpc, [row["creator"]])[0]
    row["creator_owner"] = (creator_account or {}).get("owner")
    if row["creator_owner"] != pump.PUMP_FEE_SHARE_PROGRAM:
        row["skip"] = f"creator is not a sharing config (owner {row['creator_owner']})"
        return row
    try:
        config = pump.decode_sharing_config(row["creator"], creator_account)
    except pump.DecodeError as exc:
        row["skip"] = f"config undecodable: {exc}"
        return row
    row["config"] = config
    row["admin"] = config.admin
    row["admin_revoked"] = config.admin_revoked
    row["status"] = config.status
    row["shareholders"] = config.shareholders
    row["vault"] = creator_vault(row["creator"])
    row["vault_lamports"] = lamports_of(read_accounts(rpc, [row["vault"]])[0])
    row["usable"] = (
        not row["graduated"] and not config.admin_revoked and config.status == 1
    )
    if not row["usable"]:
        row["skip"] = (
            f"graduated={row['graduated']} admin_revoked={config.admin_revoked} "
            f"status={config.status}"
        )
    return row


def describe_row(row: dict) -> str:
    if "config" not in row:
        return f"  {row['mint']}  SKIP  {row.get('skip')}"
    return (
        f"  {row['mint']}\n"
        f"    config       {row['config'].address}  admin={row['admin']}\n"
        f"    admin_revoked {row['admin_revoked']}  status={row['status']}  "
        f"graduated={row['graduated']}  shareholders={len(row['shareholders'])}\n"
        f"    creator vault {row['vault']}  lamports={row['vault_lamports']}\n"
        f"    usable        {row['usable']}" + ("" if row["usable"] else f"  ({row.get('skip')})")
    )


def discover(rpc, *, want: int, scan: int, delay: float) -> list[str]:
    """Coins sampled from pump's own launch stream that already carry a
    sharing config. Not from the fee-share program's accounts: enumerating
    603k configs to find one is a response no gateway will serve."""
    rows = sample(rpc, want=want, scan=scan, delay=delay)
    shared = [r for r in rows if r.get("route") == "fee_share" and not r["curve"]["complete"]]
    print(f"  launch sample: {len(shared)} of {len(rows)} carry a sharing config")
    return [r["mint"] for r in shared]


# -- the run --------------------------------------------------------------
def recipient_shapes(rpc, row: dict, *, wallet: str | None, program_owned: str | None,
                     derive_under: str) -> list[tuple[str, str, str]]:
    """`(key, label, address)` for each shape under test.

    (a) an ordinary existing system-owned wallet   -- the control
    (b) an EXISTING program-owned, non-executable account
    (c) a PDA that has never been funded           -- `collector(mint)` today
    """
    mint = row["mint"]
    shapes: list[tuple[str, str, str]] = []

    candidate_wallets = [wallet] if wallet else []
    candidate_wallets += [row["admin"]]
    chosen_wallet = None
    for address in [a for a in candidate_wallets if a]:
        account = read_accounts(rpc, [address])[0]
        if account and account.get("owner") == SYSTEM_PROGRAM and not account.get("executable"):
            chosen_wallet = address
            break
    if chosen_wallet:
        shapes.append(("a", "ordinary existing system-owned wallet", chosen_wallet))
    else:
        print("  (a) no system-owned wallet available -- the control is not scored")

    if program_owned:
        shapes.append(("b", "existing program-owned account (given)", program_owned))
    else:
        for other in KNOWN_CONFIGURED:
            if other == mint:
                continue
            address = find_program_address(
                [b"sharing-config", pubkey_bytes(other)], pump.PUMP_FEE_SHARE_PROGRAM
            )[0]
            account = read_accounts(rpc, [address])[0]
            if account and not account.get("executable") and account.get("owner") not in (
                SYSTEM_PROGRAM, None
            ):
                shapes.append(
                    ("b", f"existing PDA owned by {account['owner'][:8]}..., not executable",
                     address)
                )
                break
        else:
            print("  (b) no existing program-owned account found -- not scored")

    # (b2) a second program owner, so the answer is not a fact about one
    # program. A bonding curve is owned by pump itself and is not executable.
    for other in KNOWN_CONFIGURED:
        if other == mint:
            continue
        address = pump.bonding_curve(other)
        account = read_accounts(rpc, [address])[0]
        if account and not account.get("executable"):
            shapes.append(("b2", f"existing PDA owned by pump ({address[:8]}...)", address))
            break

    shapes.append(
        ("c", "PDA that has never been funded -- collector(mint) before deployment",
         collector_pda(mint, derive_under))
    )
    shapes.append(("x", "the pump program itself -- the executable control", pump.PUMP_PROGRAM))
    return shapes


def probe_minimum(rpc, idls, row, *, payer: str, top_up: int) -> None:
    """pump's own floor for THIS coin, in pump's own words."""
    mint, creator, config = row["mint"], row["creator"], row["config"].address
    vault = row["vault"]
    for amount in (0, top_up):
        ixs = []
        if amount:
            ixs.append(transfer_instruction(payer, vault, amount))
        ixs.append(minimum_instruction(idls, mint, creator, config, payer))
        outcome = run(rpc, idls, ixs, payer=payer, watch=[vault],
                      label=f"minimum top_up={amount}")
        print(f"\n  get_minimum_distributable_fee   vault top-up {amount} lamports")
        print_result(idls, outcome, recipient=None, vault=vault, verbose_logs=False)


def case(rpc, idls, row, *, key: str, label: str, recipient: str, payer: str, top_up: int,
         with_update: bool, verbose_logs: bool) -> dict:
    mint, creator = row["mint"], row["creator"]
    config = row["config"].address
    vault = row["vault"]

    account = read_accounts(rpc, [recipient])[0]
    print(f"\n=== ({key}) {label}")
    print(f"  recipient      {recipient}")
    print(f"  shape          {shape_of(recipient, account)}")

    ixs = []
    if top_up:
        ixs.append(transfer_instruction(payer, vault, top_up))
    shares_note = "config's existing shareholders (no update)"
    recipients = [recipient]
    if with_update:
        try:
            update_ix, notes, shares = update_instruction(
                idls, mint, creator, config, row["admin"], recipients
            )
        except (KeyError, ValueError, LookupError) as exc:
            print(f"  UNRESOLVED     update_fee_shares_v2: {exc}")
            return {"key": key, "label": label, "recipient": recipient,
                    "verdict": f"UNRESOLVED {exc}"}
        ixs.append(update_ix)
        shares_note = f"update_fee_shares_v2 -> {[(s['address'], s['share_bps']) for s in shares]}"
    else:
        recipients = [address for address, _bps in row["shareholders"]]
    try:
        ixs.append(distribute_instruction(idls, mint, creator, config, payer, recipients))
    except (KeyError, ValueError, LookupError) as exc:
        print(f"  UNRESOLVED     distribute_creator_fees: {exc}")
        return {"key": key, "label": label, "recipient": recipient,
                "verdict": f"UNRESOLVED {exc}"}

    print(f"  shares         {shares_note}")
    print(f"  instructions   {len(ixs)}"
          + ("  [transfer, update, distribute]" if top_up and with_update
             else "  [transfer, distribute]" if top_up
             else "  [update, distribute]" if with_update else "  [distribute]"))
    outcome = run(rpc, idls, ixs, payer=payer, watch=[recipient, vault] + recipients,
                  label=label)
    print_result(idls, outcome, recipient=recipient, vault=vault, verbose_logs=verbose_logs)
    distributed = distributed_from(outcome)
    answer = verdict(outcome, recipient, distributed)
    print(f"  VERDICT        {answer}"
          + (f"   (event distributed={distributed})" if distributed is not None else ""))
    return {"key": key, "label": label, "recipient": recipient, "verdict": answer,
            "distributed": distributed}


def incinerator_case(rpc, idls, mint: str, *, payer: str, top_up: int, verbose_logs: bool):
    """The correction BUILD.md needs: does the incinerator RECEIVE lamports,
    or did the earlier run only observe that nothing errored?"""
    row = inspect(rpc, mint)
    print(f"\n### incinerator payment test -- {mint}")
    print(describe_row(row))
    if "config" not in row:
        print("  cannot test: no readable sharing config")
        return []
    holders = [address for address, _bps in row["shareholders"]]
    if INCINERATOR not in holders:
        print(f"  cannot test: this config does not name the incinerator ({holders})")
        return []
    results = []
    for amount in (0, top_up):
        print(f"\n=== incinerator, vault top-up {amount} lamports "
              f"(natural balance {row['vault_lamports']})")
        ixs = []
        if amount:
            ixs.append(transfer_instruction(payer, row["vault"], amount))
        ixs.append(
            distribute_instruction(
                idls, mint, row["creator"], row["config"].address, payer, holders
            )
        )
        outcome = run(rpc, idls, ixs, payer=payer,
                      watch=[INCINERATOR, row["vault"]] + holders, label="incinerator")
        print_result(idls, outcome, recipient=INCINERATOR, vault=row["vault"],
                     verbose_logs=verbose_logs)
        distributed = distributed_from(outcome)
        answer = verdict(outcome, INCINERATOR, distributed)
        print(f"  VERDICT        {answer}"
              + (f"   (event distributed={distributed})" if distributed is not None else ""))
        results.append({"key": f"incinerator top_up={amount}", "label": mint,
                        "recipient": INCINERATOR, "verdict": answer,
                        "distributed": distributed})
    return results


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("mints", nargs="*", help="coins to test; the first usable one is used")
    parser.add_argument("--rpc")
    parser.add_argument("--payer", default=DEFAULT_PAYER)
    parser.add_argument("--auto", action="store_true",
                        help="also sample pump's launch stream for a usable coin")
    parser.add_argument("--auto-limit", type=int, default=25)
    parser.add_argument("--auto-scan", type=int, default=60)
    parser.add_argument("--auto-delay", type=float, default=0.6)
    parser.add_argument("--top-up", type=int, default=DEFAULT_TOP_UP,
                        help="lamports transferred into the creator vault in the same "
                             "transaction, so pump's floor cannot explain a zero")
    parser.add_argument("--coins", type=int, default=1, help="usable coins to test")
    parser.add_argument("--wallet", help="the (a) control recipient")
    parser.add_argument("--program-owned", help="the (b) recipient")
    parser.add_argument("--derive-under", default=pump.PUMP_FEE_SHARE_PROGRAM,
                        help="program id the never-funded collector PDA is derived under")
    parser.add_argument("--incinerator", nargs="*", default=None,
                        help="mints whose incinerator payment to measure")
    parser.add_argument("--logs", action="store_true", help="print every log line")
    args = parser.parse_args(argv)

    rpc = RpcClient(endpoints_from(args.rpc))
    idls = {}
    for program_id in (pump.PUMP_PROGRAM, pump.PUMP_FEE_SHARE_PROGRAM):
        try:
            idls[program_id] = read_idl(rpc, program_id)
        except Exception as exc:                            # noqa: BLE001 - reported
            print(f"{program_id}: IDL unreadable {exc}")
            return 1

    payer_account = read_accounts(rpc, [args.payer])[0]
    print(f"fee payer        {args.payer}")
    print(f"  shape          {shape_of(args.payer, payer_account)}")
    print(f"  lamports       {lamports_of(payer_account)}")
    if lamports_of(payer_account) < args.top_up + 10_000_000:
        print("  WARNING: payer may not cover the top-up")
    print(f"  top-up         {args.top_up} lamports into the creator vault "
          f"(rent-exempt minimum for an empty account is {RENT_EXEMPT_EMPTY})")

    summary = []

    if args.incinerator is not None:
        targets = args.incinerator or ["5vxYBj3qbAFCSQr2487AZEzNtmpdPRLZBKHpKuvfpump"]
        for mint in targets:
            try:
                summary.extend(
                    incinerator_case(rpc, idls, mint, payer=args.payer,
                                     top_up=args.top_up, verbose_logs=args.logs)
                )
            except Exception as exc:                        # noqa: BLE001 - reported
                print(f"  incinerator test unreadable: {type(exc).__name__}: {exc}")

    candidates = list(args.mints)
    if args.auto:
        candidates += discover(rpc, want=args.auto_limit, scan=args.auto_scan,
                               delay=args.auto_delay)

    print(f"\n### candidate funnel -- {len(candidates)} coin(s)")
    usable = []
    for mint in candidates:
        try:
            row = inspect(rpc, mint)
        except Exception as exc:                            # noqa: BLE001 - reported
            print(f"  {mint}  UNREADABLE {type(exc).__name__}: {exc}")
            continue
        print(describe_row(row))
        if row.get("usable"):
            usable.append(row)
        if len(usable) >= args.coins:
            break

    if not usable:
        print("\nNO USABLE COIN: need a config with admin_revoked false, status Active, "
              "and an un-graduated curve. The recipient question is UNRESOLVED, not answered.")
        return 1

    for row in usable:
        print(f"\n### {row['mint']}   config {row['config'].address}   "
              f"admin {row['admin']}   one-shot unspent")
        probe_minimum(rpc, idls, row, payer=args.payer, top_up=args.top_up)

        # The update alone, so a vault emptied by the update itself cannot be
        # mistaken for a distribution that paid nothing.
        print("\n=== baseline: update_fee_shares_v2 alone, no distribution")
        control = row["admin"]
        try:
            update_ix, _notes, _shares = update_instruction(
                idls, row["mint"], row["creator"], row["config"].address, row["admin"], [control]
            )
            ixs = [transfer_instruction(args.payer, row["vault"], args.top_up), update_ix]
            outcome = run(rpc, idls, ixs, payer=args.payer,
                          watch=[row["vault"], control], label="update only")
            print_result(idls, outcome, recipient=None, vault=row["vault"],
                         verbose_logs=args.logs)
        except Exception as exc:                            # noqa: BLE001 - reported
            print(f"  baseline unreadable: {type(exc).__name__}: {exc}")

        for key, label, recipient in recipient_shapes(
            rpc, row, wallet=args.wallet, program_owned=args.program_owned,
            derive_under=args.derive_under
        ):
            try:
                summary.append(
                    case(rpc, idls, row, key=key, label=label, recipient=recipient,
                         payer=args.payer, top_up=args.top_up, with_update=True,
                         verbose_logs=args.logs)
                )
            except Exception as exc:                        # noqa: BLE001 - reported
                print(f"  ({key}) unreadable: {type(exc).__name__}: {exc}")
                summary.append({"key": key, "label": label, "recipient": recipient,
                                "verdict": f"UNREADABLE {type(exc).__name__}: {exc}"})

    print("\n### SUMMARY")
    for entry in summary:
        print(f"  ({entry['key']}) {entry['label']}\n"
              f"      {entry['recipient']}\n"
              f"      {entry['verdict']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
