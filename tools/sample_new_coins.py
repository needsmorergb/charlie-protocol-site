"""Coins caught at the moment they were launched, and what their creator is.

The question, from the fee-routing work: does a pump coin arrive with a
fee-sharing SharingConfig already attached, or does somebody have to call
`create_fee_sharing_config` for one to exist at all?

Answering that from the fee-share program's own accounts is circular -- every
account it holds IS a config, so the population it can enumerate is 100%
configured by construction. The sample has to come from somewhere that does
not select on the answer.

pump's `create` instruction touches a PDA no other instruction does: the
mint authority, `["mint-authority"]` under pump. Its signature history is
therefore a stream of coin LAUNCHES and nothing else, so a walk of it is an
unbiased sample of freshly launched coins -- the exact population the
question is about. The address is not pasted here; it is derived from the
seeds pump's own on-chain IDL records for that account.

For each launch this reads the bonding curve and reports the byte that
decides the question: whether `bonding_curve.creator` names an account owned
by the fee-share program (a SharingConfig) or an ordinary wallet.

    python tools/sample_new_coins.py --rpc <url> --limit 30

Reads only.
"""

from __future__ import annotations

import argparse
import base64
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from indexer import pump                                   # noqa: E402
from indexer.base58 import decode, encode                  # noqa: E402
from indexer.curve import find_program_address             # noqa: E402
from indexer.enroll import sharing_config_address          # noqa: E402
from indexer.rpc import DEFAULT_ENDPOINTS, RpcClient, RpcUnavailable   # noqa: E402
from tools.idl_dump import read_idl                        # noqa: E402

SYSTEM_PROGRAM = "11111111111111111111111111111111"


def create_instruction(idl: dict) -> dict:
    entry = next((i for i in idl.get("instructions", []) if i["name"] == "create"), None)
    if entry is None:
        raise LookupError("pump's IDL has no `create` instruction")
    return entry


def const_pda(instruction: dict, account_name: str, program_id: str) -> str:
    """A PDA whose seeds are all constants -- the mint authority is one."""
    account = next((a for a in instruction["accounts"] if a["name"] == account_name), None)
    if account is None or not account.get("pda"):
        raise LookupError(f"{account_name} is not a PDA in this instruction")
    seeds = []
    for seed in account["pda"].get("seeds", []):
        if seed.get("kind") != "const":
            raise LookupError(f"{account_name} has a non-constant seed")
        seeds.append(bytes(seed.get("value") or []))
    return find_program_address(seeds, program_id)[0]


def _instructions(tx: dict):
    message = ((tx or {}).get("transaction") or {}).get("message") or {}
    for entry in message.get("instructions") or []:
        yield entry
    for group in ((tx or {}).get("meta") or {}).get("innerInstructions") or []:
        for entry in group.get("instructions") or []:
            yield entry


def launches(rpc, idl: dict, *, want: int, scan: int, delay: float) -> list[dict]:
    """`{mint, signature, block_time}` for the most recent launches.

    `delay` paces the per-signature `getTransaction`. The project gateway
    answers a burst of them with upstream HTTP 429, and a 429 is not an
    answer about the chain -- a run that stopped there would report nothing
    at all. So the walk is paced, a signature that still fails is SKIPPED and
    counted, and the sample stops once `want` launches are in hand.
    """
    instruction = create_instruction(idl)
    authority = const_pda(instruction, "mint_authority", pump.PUMP_PROGRAM)
    discriminator = bytes(instruction["discriminator"])
    names = [a["name"] for a in instruction["accounts"]]
    mint_index = names.index("mint")

    print(f"  mint authority {authority}   (seeds from the IDL, not pasted)")
    print(f"  create disc    {discriminator.hex()}   mint is account #{mint_index}")

    found: list[dict] = []
    seen: set[str] = set()
    skipped = 0
    for position, entry in enumerate(rpc.signatures_for_address(authority, limit=scan)):
        if len(found) >= want:
            break
        if entry.get("err"):
            continue
        if position:
            time.sleep(delay)
        try:
            tx = rpc.transaction(entry["signature"])
        except RpcUnavailable as exc:
            skipped += 1
            print(f"  skipped      {entry['signature'][:16]}... {exc}")
            time.sleep(delay * 2)
            continue
        for candidate in _instructions(tx):
            if candidate.get("programId") != pump.PUMP_PROGRAM:
                continue
            raw = candidate.get("data")
            if not raw:
                continue
            try:
                data = decode(raw)
            except ValueError:
                continue
            if data[:8] != discriminator:
                continue
            accounts = candidate.get("accounts") or []
            if len(accounts) <= mint_index:
                continue
            mint = accounts[mint_index]
            if mint in seen:
                continue
            seen.add(mint)
            found.append(
                {
                    "mint": mint,
                    "signature": entry["signature"],
                    "block_time": entry.get("blockTime"),
                }
            )
    print(f"  launches     {len(found)} read, {skipped} signatures skipped on RPC failure")
    return found


def decode_curve(account: dict | None) -> dict:
    if not account:
        raise pump.DecodeError("no bonding curve account")
    if account.get("owner") != pump.PUMP_PROGRAM:
        raise pump.DecodeError(f"owned by {account.get('owner')}, not pump")
    data = base64.b64decode(account["data"][0])
    if data[:8] != pump.DISC_BONDING_CURVE:
        raise pump.DecodeError("not a bonding curve account")
    return {
        "virtual_token_reserves": int.from_bytes(data[8:16], "little"),
        "virtual_sol_reserves": int.from_bytes(data[16:24], "little"),
        "real_token_reserves": int.from_bytes(data[24:32], "little"),
        "real_sol_reserves": int.from_bytes(data[32:40], "little"),
        "token_total_supply": int.from_bytes(data[40:48], "little"),
        "complete": bool(data[48]),
        "creator": encode(data[49:81]),
        "bytes": len(data),
    }


def _chunked(values, size=100):
    for start in range(0, len(values), size):
        yield values[start : start + size]


def classify(rpc, rows: list[dict]) -> list[dict]:
    """Add the curve, the creator's owner program, and the verdict."""
    mints = [row["mint"] for row in rows]
    curve_addresses = [pump.bonding_curve(mint) for mint in mints]
    curve_accounts: list[dict | None] = []
    for chunk in _chunked(curve_addresses):
        curve_accounts.extend(rpc.accounts(chunk))

    for row, address, account in zip(rows, curve_addresses, curve_accounts):
        row["bonding_curve"] = address
        try:
            row["curve"] = decode_curve(account)
        except pump.DecodeError as exc:
            row["curve"] = None
            row["error"] = str(exc)

    creators = [row["curve"]["creator"] for row in rows if row.get("curve")]
    configs = [sharing_config_address(row["mint"]) for row in rows if row.get("curve")]
    creator_accounts: list[dict | None] = []
    for chunk in _chunked(creators):
        creator_accounts.extend(rpc.accounts(chunk))
    config_accounts: list[dict | None] = []
    for chunk in _chunked(configs):
        config_accounts.extend(rpc.accounts(chunk))

    live = [row for row in rows if row.get("curve")]
    for row, creator_account, config_address, config_account in zip(
        live, creator_accounts, configs, config_accounts
    ):
        owner = (creator_account or {}).get("owner")
        row["creator_owner"] = owner
        row["creator_lamports"] = (creator_account or {}).get("lamports", 0)
        row["route"] = (
            "fee_share" if owner == pump.PUMP_FEE_SHARE_PROGRAM
            else "plain_creator" if owner in (SYSTEM_PROGRAM, None)
            else f"other({owner})"
        )
        row["config_pda"] = config_address
        row["config_pda_exists"] = config_account is not None
        row["config_pda_owner"] = (config_account or {}).get("owner")
    return rows


def render(rows: list[dict]) -> str:
    lines = []
    for row in rows:
        curve = row.get("curve")
        lines.append(row["mint"])
        lines.append(f"    launched     {row.get('block_time')}  sig {row['signature'][:24]}...")
        if not curve:
            lines.append(f"    curve        UNREADABLE {row.get('error')}")
            continue
        lines.append(
            f"    creator      {curve['creator']}  owner={row.get('creator_owner')}"
        )
        lines.append(
            f"    route        {row.get('route')}"
            f"   real_sol_reserves={curve['real_sol_reserves']}"
            f"  complete={curve['complete']}  curve_bytes={curve['bytes']}"
        )
        lines.append(
            f"    config pda   {row.get('config_pda')}  "
            f"exists={row.get('config_pda_exists')}"
        )
    return "\n".join(lines)


def summarise(rows: list[dict]) -> str:
    live = [r for r in rows if r.get("curve")]
    shared = [r for r in live if r.get("route") == "fee_share"]
    plain = [r for r in live if r.get("route") == "plain_creator"]
    other = [r for r in live if r.get("route", "").startswith("other")]
    untouched = [r for r in live if r["curve"]["real_sol_reserves"] == 0]
    pda_present = [r for r in live if r.get("config_pda_exists")]
    return "\n".join(
        [
            "SUMMARY -- freshly launched coins, sampled from pump's own create stream",
            f"  launches read            {len(rows)}",
            f"  bonding curves decoded   {len(live)}",
            f"  creator IS a SharingConfig (owner=pfee...)   {len(shared)}",
            f"  creator is an ordinary wallet (owner=system) {len(plain)}",
            f"  creator owned by something else              {len(other)}",
            f"  sharing-config PDA exists for the mint       {len(pda_present)}",
            f"  curves with zero real SOL reserves (no buys) {len(untouched)}",
        ]
    )


def endpoints_from(argument: str | None) -> tuple[str, ...]:
    raw = argument or os.environ.get("CHARLIE_RPC_URLS") or ""
    return tuple(u.strip() for u in raw.split(",") if u.strip()) or DEFAULT_ENDPOINTS


def sample(rpc, *, want: int = 12, scan: int = 40, delay: float = 1.0) -> list[dict]:
    idl = read_idl(rpc, pump.PUMP_PROGRAM)
    rows = launches(rpc, idl, want=want, scan=scan, delay=delay)
    return classify(rpc, rows)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--rpc", help="comma-separated RPC endpoints (env CHARLIE_RPC_URLS)")
    parser.add_argument("--limit", type=int, default=12, help="launches to read")
    parser.add_argument("--scan", type=int, default=40, help="launch signatures to walk")
    parser.add_argument("--delay", type=float, default=1.0, help="seconds between getTransaction")
    args = parser.parse_args(argv)

    rpc = RpcClient(endpoints_from(args.rpc))
    print("sampling pump launches")
    rows = sample(rpc, want=args.limit, scan=args.scan, delay=args.delay)
    print()
    print(render(rows))
    print()
    print(summarise(rows))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
