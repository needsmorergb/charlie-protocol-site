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


def update_instruction(idls, mint, creator, config, admin, recipients, current):
    """`update_fee_shares_v2` naming exactly `recipients`, bps summing to 10000.

    `current` is the config's OUTGOING shareholders, and they -- not the
    incoming ones -- are what the remaining accounts must be. Measured, not
    guessed: passing the new recipient produced

        AnchorError ... update_fee_shares.rs:182 ShareholderAccountMismatch
        Left: <the new recipient>   Right: <the current shareholder>

    for all five recipients, with the same Right every time. It follows from
    what the instruction does: it CPIs pump's DistributeCreatorFeesV2 to pay
    the outgoing split off before replacing it, and those are the accounts it
    needs in hand to pay. An earlier tool appeared to pass the new
    shareholders successfully only because the coin's creator was both.
    """
    program_id, idl, entry = find_instruction(idls, "update_fee_shares_v2")
    if entry is None:
        raise LookupError("update_fee_shares_v2 is in neither on-chain IDL")
    share = 10_000 // len(recipients)
    shares = [{"address": address, "share_bps": share} for address in recipients]
    shares[0]["share_bps"] += 10_000 - share * len(recipients)
    arg_name = (entry.get("args") or [{}])[0].get("name")
    data_args, notes = encode_args(entry, idl, {arg_name: shares} if arg_name else {})
    metas = resolve_accounts(entry, program_id, context_for(mint, creator, admin, config))
    metas = metas + [
        (f"outgoing[{i}]", address, False, True) for i, address in enumerate(current)
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
        if err is not None:
            # A failed simulation returns no post-simulation accounts, and the
            # zeros it fills in are not balances. Subtracting them would print
            # a spectacular negative delta for a transaction that moved
            # nothing at all.
            print(f"  lamports       {address}{tag}\n"
                  f"                 before {before}  after NOT MEASURED "
                  f"(the transaction failed)")
            continue
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
    if outcome["err"] is not None:
        if outcome["code"] in (6052, 6070):
            return f"REFUSED  {outcome['code']}"
        return f"FAILED   err={outcome['err']} code={outcome['code']}"
    delta = outcome["after"].get(recipient, 0) - outcome["before"].get(recipient, 0)
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
    row["creator_lamports"] = lamports_of(creator_account)
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


def discover(rpc, *, want: int, scan: int, delay: float) -> tuple[list[str], list[dict]]:
    """`(coins that already carry a config, coins that carry none)` from
    pump's own launch stream.

    Not from the fee-share program's accounts: enumerating 603k configs to
    find one is a response no gateway will serve, and the launch stream is
    the population the other tools here already sample.
    """
    rows = sample(rpc, want=want, scan=scan, delay=delay)
    live = [r for r in rows if not r["curve"]["complete"]]
    shared = [r for r in live if r.get("route") == "fee_share"]
    plain = [r for r in live if r.get("route") == "plain_creator"]
    plain.sort(key=lambda r: r.get("creator_lamports", 0), reverse=True)
    print(f"  launch sample: {len(shared)} of {len(rows)} carry a sharing config, "
          f"{len(plain)} un-graduated coins carry none")
    return [r["mint"] for r in shared], plain


# -- the run --------------------------------------------------------------
# Wallets for the (a) control. The control must be neither the fee payer nor
# the acting identity, or its balance moves for two reasons at once and the
# delta stops being evidence -- and the acting identity is whichever wallet
# the sampled coin happens to belong to, so one candidate is not enough.
# All four were read off chain by the other tools here: the first is the 100%
# shareholder of two traced coins, the rest are sharing-config admins.
CONTROL_WALLETS = (
    "GYrPAaSNLuyrtAkwsC5pvBXekS15PCrXJcR1UAPkWxs6",
    "AxqXBbPab4iRUMpUiCxKmkawUk77TR5mRVsTFDiUTsh",
    "2xpKBkzBoAretBBsJYmhzouZR1WZuZFuT6jwoRSFz8Ed",
    "7ZV54HcwtzRhZSEPskT8ox5hn9yNocK9xpe4BQXoziaP",
)


def recipient_shapes(rpc, plan: dict, *, wallet: str | None, program_owned: str | None,
                     derive_under: str) -> list[tuple[str, str, str]]:
    """`(key, label, address)` for each shape under test.

    (a)  an ordinary existing system-owned wallet -- the control
    (b)  an EXISTING program-owned account that is not executable
    (b2) the same, owned by a second program, so the answer is not a fact
         about one program
    (c)  a PDA that has never been funded -- `collector(mint)` today
    (x)  the pump program itself -- the executable control, which must fail
    """
    mint = plan["mint"]
    excluded = {plan["identity"], plan["payer"], plan["vault"], plan["config"]}
    shapes: list[tuple[str, str, str]] = []

    for address in [a for a in ((wallet,) + CONTROL_WALLETS + (plan["identity"],)) if a]:
        if address in excluded:
            continue
        account = read_accounts(rpc, [address])[0]
        if account and account.get("owner") == SYSTEM_PROGRAM and not account.get("executable"):
            shapes.append(("a", "ordinary existing system-owned wallet", address))
            break
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
            if (account and not account.get("executable")
                    and account.get("owner") == pump.PUMP_FEE_SHARE_PROGRAM
                    and address not in excluded):
                shapes.append(
                    ("b", "existing PDA owned by the fee-share program, not executable",
                     address)
                )
                break
        else:
            print("  (b) no existing program-owned account found -- not scored")

    for other in KNOWN_CONFIGURED:
        if other == mint:
            continue
        address = pump.bonding_curve(other)
        account = read_accounts(rpc, [address])[0]
        if account and not account.get("executable") and address not in excluded:
            shapes.append(("b2", "existing PDA owned by pump itself, not executable", address))
            break

    shapes.append(
        ("c", "PDA that has never been funded -- collector(mint) before deployment",
         collector_pda(mint, derive_under))
    )
    shapes.append(("x", "the pump program itself -- the executable control", pump.PUMP_PROGRAM))
    return shapes


# -- plans ----------------------------------------------------------------
def existing_plan(row: dict, payer: str) -> dict:
    """A coin whose config exists and whose one-shot is still unspent."""
    return {
        "mode": "existing",
        "mint": row["mint"],
        "identity": row["admin"],        # update_fee_shares_v2's `authority`
        "config": row["config"].address,
        "curve_creator": row["creator"],  # what bonding_curve.creator says NOW
        "vault": row["vault"],
        "payer": payer,
        "vault_lamports": row["vault_lamports"],
        "current": [address for address, _bps in row["shareholders"]],
    }


def create_plan(row: dict, payer: str) -> dict:
    """A coin with NO config: the config is created in the same transaction.

    Every freshly launched coin that arrives WITH a config arrives with its
    one-shot already spent -- ten of ten in the sample -- so the only way to
    hold an unspent one-shot is to create the config here. After
    `create_fee_sharing_config` the bonding curve's creator IS the config
    PDA, which is what the creator vault is keyed by, so the vault under test
    is the config's, not the wallet's.
    """
    mint = row["mint"]
    config = find_program_address(
        [b"sharing-config", pubkey_bytes(mint)], pump.PUMP_FEE_SHARE_PROGRAM
    )[0]
    return {
        "mode": "create",
        "mint": mint,
        "identity": row["curve"]["creator"],   # the wallet; becomes payer and admin
        "config": config,
        "curve_creator": config,               # AFTER the create instruction
        "vault": creator_vault(config),
        "payer": payer,
        "vault_lamports": 0,
        "creator_lamports": row.get("creator_lamports", 0),
        # CreateFeeSharingConfigEvent.initial_shareholders, measured on
        # 2026-09-03: the creating wallet at 10000 bps, and nothing else.
        "current": [row["curve"]["creator"]],
    }


def create_config_instruction(idls, plan: dict):
    program_id, idl, entry = find_instruction(idls, "create_fee_sharing_config")
    if entry is None:
        raise LookupError("create_fee_sharing_config is in neither on-chain IDL")
    context = context_for(plan["mint"], plan["identity"], plan["identity"], plan["config"])
    # The fee-share program's own 6019 says AMM accounts are required for
    # GRADUATED coins, so a coin still on its bonding curve has no pool, and
    # Anchor's convention for an absent optional account is the program id.
    context["pool"] = program_id
    metas = resolve_accounts(entry, program_id, context)
    return (program_id, metas, bytes(entry["discriminator"]))


def prelude(idls, plan: dict) -> list:
    """The instructions that must run before anything can be distributed."""
    return [create_config_instruction(idls, plan)] if plan["mode"] == "create" else []


def probe_minimum(rpc, idls, plan: dict, *, top_up: int, verbose_logs: bool) -> None:
    """pump's own floor for THIS coin, in pump's own words, with and without
    the top-up -- so the floor is never an unexamined explanation."""
    for amount in (0, top_up):
        ixs = prelude(idls, plan)
        if amount:
            ixs.append(transfer_instruction(plan["payer"], plan["vault"], amount))
        ixs.append(
            minimum_instruction(idls, plan["mint"], plan["curve_creator"], plan["config"],
                                plan["identity"])
        )
        outcome = run(rpc, idls, ixs, payer=plan["payer"], watch=[plan["vault"]],
                      label=f"minimum top_up={amount}")
        print(f"\n  get_minimum_distributable_fee   vault top-up {amount} lamports")
        print_result(idls, outcome, recipient=None, vault=plan["vault"],
                     verbose_logs=verbose_logs)


def case(rpc, idls, plan: dict, *, key: str, label: str, recipient: str, top_up: int,
         verbose_logs: bool) -> dict:
    vault = plan["vault"]
    account = read_accounts(rpc, [recipient])[0]
    print(f"\n=== ({key}) {label}")
    print(f"  recipient      {recipient}")
    print(f"  shape          {shape_of(recipient, account)}")

    try:
        ixs = prelude(idls, plan)
        update_ix, notes, shares = update_instruction(
            idls, plan["mint"], plan["curve_creator"], plan["config"], plan["identity"],
            [recipient], plan["current"],
        )
        ixs.append(update_ix)
        # AFTER the update, never before. `update_fee_shares_v2` CPIs into
        # pump's DistributeCreatorFeesV2 to flush the vault to the OUTGOING
        # shareholders first -- measured, in the logs of the run that put the
        # transfer first -- so a vault funded ahead of it is paid to the old
        # split and the instruction under test finds an empty vault.
        if top_up:
            ixs.append(transfer_instruction(plan["payer"], vault, top_up))
        ixs.append(
            distribute_instruction(idls, plan["mint"], plan["curve_creator"], plan["config"],
                                   plan["identity"], [recipient])
        )
    except (KeyError, ValueError, LookupError) as exc:
        print(f"  UNRESOLVED     {type(exc).__name__}: {exc}")
        return {"key": key, "label": label, "recipient": recipient,
                "verdict": f"UNRESOLVED {exc}"}

    order = (["create_fee_sharing_config"] if plan["mode"] == "create" else []) \
        + ["update_fee_shares_v2"] + (["transfer"] if top_up else []) \
        + ["distribute_creator_fees"]
    print(f"  instructions   {order}")
    print(f"  shares         {[(s['address'], s['share_bps']) for s in shares]}")
    print(f"  outgoing       {plan['current']}  (the update's remaining accounts)")
    outcome = run(rpc, idls, ixs, payer=plan["payer"],
                  watch=[recipient, vault, plan["identity"]], label=label)
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
    parser.add_argument("mints", nargs="*", help="coins to try before the launch stream")
    parser.add_argument("--rpc")
    parser.add_argument("--payer", default=DEFAULT_PAYER)
    parser.add_argument("--auto", action="store_true",
                        help="sample pump's launch stream for a usable coin")
    parser.add_argument("--auto-limit", type=int, default=25)
    parser.add_argument("--auto-scan", type=int, default=60)
    parser.add_argument("--auto-delay", type=float, default=0.6)
    parser.add_argument("--top-up", type=int, default=DEFAULT_TOP_UP,
                        help="lamports transferred into the creator vault in the same "
                             "transaction, so pump's floor cannot explain a zero")
    parser.add_argument("--wallet", help="the (a) control recipient")
    parser.add_argument("--program-owned", help="the (b) recipient")
    parser.add_argument("--derive-under", default=pump.PUMP_FEE_SHARE_PROGRAM,
                        help="program id the never-funded collector PDA is derived under")
    parser.add_argument("--incinerator", nargs="*", default=None,
                        help="mints whose incinerator payment to measure")
    parser.add_argument("--logs", action="store_true", help="print every log line")
    args = parser.parse_args(argv)

    # The gateway answers a burst with an upstream 403 that the next attempt
    # serves correctly, and RpcClient does not penalise the gateway for its
    # upstream's refusal -- it just retries. Three attempts was not enough to
    # get through one, and losing the whole run to it costs more than waiting.
    rpc = RpcClient(endpoints_from(args.rpc), max_retries=10)
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

    configured = list(args.mints)
    unconfigured: list[dict] = []
    if args.auto:
        try:
            found, unconfigured = discover(rpc, want=args.auto_limit, scan=args.auto_scan,
                                           delay=args.auto_delay)
            configured += found
        except Exception as exc:                            # noqa: BLE001 - reported
            # The gateway answers a burst of getTransaction with an upstream
            # 403 often enough that a run has already died here. A sample is
            # a convenience; the coins named on the command line are not.
            print(f"  launch sample failed: {type(exc).__name__}: {exc}")

    print(f"\n### funnel A -- {len(configured)} coin(s) that already have a config")
    print("  a usable one has admin_revoked false: the one-shot update is still unspent")
    plan = None
    named_without_config = []
    for mint in configured:
        try:
            row = inspect(rpc, mint)
        except Exception as exc:                            # noqa: BLE001 - reported
            print(f"  {mint}  UNREADABLE {type(exc).__name__}: {exc}")
            continue
        print(describe_row(row))
        if row.get("usable"):
            plan = existing_plan(row, args.payer)
            break
        if (row.get("creator_owner") == SYSTEM_PROGRAM and not row.get("graduated")
                and "creator" in row):
            named_without_config.append(
                {"mint": mint, "curve": {"creator": row["creator"]},
                 "creator_lamports": row.get("creator_lamports", 0)}
            )
    unconfigured = named_without_config + unconfigured

    if plan is None:
        print(f"\n### funnel B -- {len(unconfigured)} coin(s) with NO config")
        print("  none of the configured coins had an unspent one-shot, so the config is")
        print("  created in the same transaction, which is where an unspent one-shot")
        print("  demonstrably exists (`--then-update` measured that on 2026-09-03).")
        # The config account costs 8017920 lamports of rent and the creating
        # wallet pays it, so a creator that cannot afford its own config is
        # not a candidate.
        for row in unconfigured:
            funds = row.get("creator_lamports", 0)
            print(f"  {row['mint']}  creator={row['curve']['creator']} lamports={funds}")
            if funds >= 15_000_000:
                plan = create_plan(row, args.payer)
                break

    if plan is None:
        print("\nNO USABLE COIN. The recipient question is UNRESOLVED, not answered.")
        return 1

    print(f"\n### {plan['mint']}   mode={plan['mode']}")
    print(f"  config         {plan['config']}")
    print(f"  admin/identity {plan['identity']}")
    print(f"  creator vault  {plan['vault']}  natural lamports {plan['vault_lamports']}")
    probe_minimum(rpc, idls, plan, top_up=args.top_up, verbose_logs=args.logs)

    for key, label, recipient in recipient_shapes(
        rpc, plan, wallet=args.wallet, program_owned=args.program_owned,
        derive_under=args.derive_under
    ):
        try:
            summary.append(
                case(rpc, idls, plan, key=key, label=label, recipient=recipient,
                     top_up=args.top_up, verbose_logs=args.logs)
            )
        except Exception as exc:                            # noqa: BLE001 - reported
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
