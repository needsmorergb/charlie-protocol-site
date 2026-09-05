"""The production keeper: `buyback.run_keeper` with everything an operator
needs around it to leave it running unattended.

`buyback` builds and sends one crank correctly. Running cranks for weeks
from a machine nobody is watching needs more than correctness:

* **A config file, not flags.** One `keeper.json` names the coin, the
  wallet, the lot, the cadence and the budget. It is read on every start,
  so what the keeper is doing is always what the file says.
* **`armed: false` by default.** Nothing signs until the operator flips it.
  Every other command works unarmed -- preflight, status, a dry run -- so
  the whole setup can be rehearsed with a key that never signs.
* **The wallet is named in the config and the keypair must match it.** A
  wrong key file is refused before the chain is read, not discovered from
  a transaction sent by the wrong wallet.
* **The budget survives restarts.** Spend is accumulated in a state file;
  a keeper that crashes and is restarted by the scheduler resumes against
  the budget it already used, rather than starting a fresh one.
* **A kill switch that needs no shell.** Creating the stop file (default
  `keeper.stop` next to the config) stops the loop before its next crank.
* **A preflight that says go or no-go in a table**, one row per thing that
  could be wrong, each with the check that decided it -- the same shape as
  the coin's page.
* **One JSON line per crank** appended to a log, and an optional webhook
  told about every landed crank and every stop.

Standard library only, like everything under `indexer/`.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

from . import buyback
from .base58 import decode as b58decode
from .ed25519 import Keypair
from .pump import DecodeError
from .rpc import DEFAULT_ENDPOINTS, RpcClient, RpcUnavailable

CHARLIE_MINT = "8FhAXv2tfXUpyMbJsHDHX9zfiEb9PERzFWSY9sgLpump"
DEFAULT_CONFIG = "keeper.json"
# A lot above this is refused unless the config says, in so many words,
# that it is intended. ARCHITECTURE.md sec.2's figure is 0.05; a stray zero
# should not be able to turn it into 5.
LARGE_LOT_SOL = 1.0
MIN_INTERVAL_SECONDS = 60

EXAMPLE_CONFIG = {
    "mint": CHARLIE_MINT,
    "wallet": "PASTE-THE-KEEPER-WALLET-ADDRESS-HERE",
    "keypair": "keeper-keypair.json",
    "rpc": list(DEFAULT_ENDPOINTS),
    "lot_sol": 0.05,
    "every_seconds": 3600,
    "max_total_sol": 1.0,
    "also_burn_tokens": 0,
    "slippage_bps": 100,
    "priority_fee_micro_lamports": 0,
    "armed": False,
    "log_file": "keeper.log.jsonl",
    "state_file": "keeper-state.json",
    "stop_file": "keeper.stop",
    "notify_url": None,
}


class ConfigError(ValueError):
    """The config as written must not run. Addressed to the operator."""


def _address(value) -> str:
    if not isinstance(value, str):
        raise ConfigError("an address must be a string")
    try:
        raw = b58decode(value.strip())
    except Exception:
        raise ConfigError(f"{value!r} is not a valid Solana address") from None
    if len(raw) != 32:
        raise ConfigError(f"{value!r} is not a valid Solana address")
    return value.strip()


@dataclass(frozen=True)
class Config:
    path: Path
    mint: str
    wallet: str
    keypair: Path
    rpc: tuple[str, ...]
    lot_lamports: int
    every_seconds: float
    max_total_lamports: int
    also_burn_tokens: float
    slippage_bps: int
    priority_fee_micro_lamports: int
    armed: bool
    log_file: Path
    state_file: Path
    stop_file: Path
    notify_url: str | None
    allow_large_lot: bool = False

    @property
    def lot_sol(self) -> float:
        return self.lot_lamports / buyback.LAMPORTS_PER_SOL


def load_config(path) -> Config:
    path = Path(path)
    if not path.exists():
        raise ConfigError(f"{path} does not exist. `keeper init` writes an example to fill in.")
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ConfigError(f"{path} is not valid JSON: {exc}") from None
    if not isinstance(raw, dict):
        raise ConfigError(f"{path} must hold one JSON object")
    unknown = set(raw) - set(EXAMPLE_CONFIG) - {"allow_large_lot"}
    if unknown:
        raise ConfigError(f"{path}: unknown keys {sorted(unknown)} -- a typo here would silently change nothing")
    missing = [k for k in ("mint", "wallet", "keypair", "lot_sol", "every_seconds", "max_total_sol") if k not in raw]
    if missing:
        raise ConfigError(f"{path}: missing {missing}")

    base = path.parent

    def rel(value) -> Path:
        p = Path(str(value)).expanduser()
        return p if p.is_absolute() else base / p

    mint = _address(raw["mint"])
    wallet = _address(raw["wallet"])
    try:
        lot_sol = float(raw["lot_sol"])
        every = float(raw["every_seconds"])
        max_total = float(raw["max_total_sol"])
        also_burn = float(raw.get("also_burn_tokens", 0) or 0)
        slippage = int(raw.get("slippage_bps", 100))
        priority = int(raw.get("priority_fee_micro_lamports", 0) or 0)
    except (TypeError, ValueError) as exc:
        raise ConfigError(f"{path}: a numeric field is not a number ({exc})") from None
    allow_large = bool(raw.get("allow_large_lot", False))
    if lot_sol * buyback.LAMPORTS_PER_SOL < buyback.MIN_LOT_LAMPORTS:
        raise ConfigError(f"lot_sol {lot_sol} is below the {buyback.MIN_LOT_LAMPORTS / buyback.LAMPORTS_PER_SOL} SOL minimum")
    if lot_sol > LARGE_LOT_SOL and not allow_large:
        raise ConfigError(
            f"lot_sol {lot_sol} is above {LARGE_LOT_SOL} SOL. If that is intended, add \"allow_large_lot\": true; "
            "a stray zero must not be able to do this on its own"
        )
    if every < MIN_INTERVAL_SECONDS:
        raise ConfigError(f"every_seconds {every} is below the {MIN_INTERVAL_SECONDS}s minimum interval")
    if max_total <= 0:
        raise ConfigError("max_total_sol must be a positive budget; the keeper does not run uncapped")
    if max_total < lot_sol:
        raise ConfigError(f"max_total_sol {max_total} is smaller than one lot ({lot_sol}); nothing could ever run")
    if not 0 <= slippage < 5000:
        raise ConfigError("slippage_bps must be between 0 and 4999")
    if also_burn < 0 or priority < 0:
        raise ConfigError("also_burn_tokens and priority_fee_micro_lamports cannot be negative")
    rpc = raw.get("rpc") or list(DEFAULT_ENDPOINTS)
    if isinstance(rpc, str):
        rpc = [u.strip() for u in rpc.split(",") if u.strip()]
    if not isinstance(rpc, list) or not all(isinstance(u, str) and u.startswith("http") for u in rpc):
        raise ConfigError("rpc must be a list of http(s) URLs")
    notify = raw.get("notify_url") or None
    if notify is not None and not (isinstance(notify, str) and notify.startswith("https://")):
        raise ConfigError("notify_url must be an https:// URL or null")

    return Config(
        path=path,
        mint=mint,
        wallet=wallet,
        keypair=rel(raw["keypair"]),
        rpc=tuple(rpc),
        lot_lamports=int(round(lot_sol * buyback.LAMPORTS_PER_SOL)),
        every_seconds=every,
        max_total_lamports=int(round(max_total * buyback.LAMPORTS_PER_SOL)),
        also_burn_tokens=also_burn,
        slippage_bps=slippage,
        priority_fee_micro_lamports=priority,
        armed=bool(raw.get("armed", False)),
        log_file=rel(raw.get("log_file") or EXAMPLE_CONFIG["log_file"]),
        state_file=rel(raw.get("state_file") or EXAMPLE_CONFIG["state_file"]),
        stop_file=rel(raw.get("stop_file") or EXAMPLE_CONFIG["stop_file"]),
        notify_url=notify,
        allow_large_lot=allow_large,
    )


# -- persistent state ----------------------------------------------------------
@dataclass
class State:
    spent_lamports: int = 0
    cranks: int = 0
    failures: int = 0
    tokens_burned: int = 0
    last_signature: str | None = None
    last_at: int | None = None
    last_error: str | None = None
    started_at: int | None = None
    signatures: list = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "spent_lamports": self.spent_lamports, "cranks": self.cranks, "failures": self.failures,
            "tokens_burned": self.tokens_burned, "last_signature": self.last_signature, "last_at": self.last_at,
            "last_error": self.last_error, "started_at": self.started_at, "signatures": self.signatures[-200:],
        }


def load_state(path: Path) -> State:
    if not Path(path).exists():
        return State()
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    state = State()
    for key, value in raw.items():
        if hasattr(state, key):
            setattr(state, key, value)
    return state


def save_state(path: Path, state: State) -> None:
    """Written to a sibling and renamed, so a crash mid-write leaves the
    previous state rather than half a file."""
    path = Path(path)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(state.as_dict(), indent=2, sort_keys=True), encoding="utf-8")
    os.replace(tmp, path)


def apply_log_line(state: State, line: str, now: int) -> None:
    """Fold one of `run_keeper`'s JSON lines into the state."""
    try:
        entry = json.loads(line)
    except json.JSONDecodeError:
        return
    if entry.get("ok"):
        state.cranks += 1
        state.spent_lamports += int(entry.get("spent_lamports_max") or 0)
        state.tokens_burned += int(entry.get("tokens_burned") or 0)
        state.last_signature = entry.get("signature")
        state.last_at = now
        state.last_error = None
        state.signatures.append(entry.get("signature"))
    else:
        state.failures += 1
        state.last_error = str(entry.get("error"))
        state.last_at = now


# -- notifications ----------------------------------------------------------------
def notify(url: str | None, text: str, *, opener=urllib.request.urlopen) -> bool:
    """Best effort. A webhook that is down must never stop a crank or be
    retried in a loop; the log line is the record, this is a courtesy."""
    if not url:
        return False
    body = json.dumps({"text": text, "content": text}).encode()
    request = urllib.request.Request(url, data=body, headers={"content-type": "application/json", "user-agent": "charlie-keeper/1"})
    try:
        with opener(request, timeout=10) as response:
            return 200 <= getattr(response, "status", 200) < 300
    except Exception:  # noqa: BLE001
        return False


# -- preflight --------------------------------------------------------------------
@dataclass(frozen=True)
class Row:
    name: str
    status: str    # PASS | FAIL | WARN
    detail: str


def preflight(cfg: Config, *, rpc=None, keypair_loader=Keypair.from_file, python_version=sys.version_info) -> list[Row]:
    """Every reason this keeper should not be started, checked in order of
    how cheap it is to check. Reads the chain; signs nothing."""
    rows: list[Row] = []

    def add(name, ok, detail, warn=False):
        rows.append(Row(name, "PASS" if ok else ("WARN" if warn else "FAIL"), detail))
        return ok

    add("python", python_version >= (3, 11), f"{python_version[0]}.{python_version[1]} (3.11+ required)")
    add("config", True, f"{cfg.path}: lot {cfg.lot_sol} SOL every {cfg.every_seconds:.0f}s, budget {cfg.max_total_lamports / 1e9} SOL")
    add("armed", cfg.armed, "armed: true -- cranks WILL be sent" if cfg.armed else "armed: false -- nothing will be signed until you set it", warn=not cfg.armed)
    add("stop file", not cfg.stop_file.exists(), f"{cfg.stop_file} {'EXISTS -- the keeper would stop at once' if cfg.stop_file.exists() else 'absent'}")

    keypair = None
    try:
        keypair = keypair_loader(cfg.keypair)
        add("keypair", keypair.address == cfg.wallet,
            f"{cfg.keypair} is {keypair.address}" + ("" if keypair.address == cfg.wallet else f" -- config names {cfg.wallet}. REFUSING to use a key that is not the named wallet"))
    except Exception as exc:  # noqa: BLE001
        add("keypair", False, f"{cfg.keypair}: {exc}")

    state = load_state(cfg.state_file)
    remaining = cfg.max_total_lamports - state.spent_lamports
    add("budget", remaining >= cfg.lot_lamports,
        f"{state.spent_lamports / 1e9:.4f} of {cfg.max_total_lamports / 1e9} SOL used over {state.cranks} crank(s); {max(remaining, 0) / 1e9:.4f} SOL remains")

    rpc = rpc or RpcClient(cfg.rpc)
    try:
        blockhash = buyback.latest_blockhash(rpc)
        add("rpc", bool(blockhash), f"{len(cfg.rpc)} endpoint(s); latest blockhash {blockhash[:8]}...")
    except Exception as exc:  # noqa: BLE001
        add("rpc", False, f"no endpoint answered: {exc}")
        return rows

    try:
        state_now = buyback.observe(rpc, cfg.mint, cfg.wallet)
    except (buyback.BuybackError, DecodeError, RpcUnavailable) as exc:
        add("coin", False, str(exc))
        return rows
    add("coin", True, f"{cfg.mint}: {state_now.decimals} decimals, supply {state_now.supply / 10 ** state_now.decimals:,.0f}, {'Token-2022' if state_now.token_program == buyback.TOKEN_2022_PROGRAM else 'Token'}")
    add("mint authority", state_now.mint_authority is None,
        "revoked -- burns are permanent" if state_now.mint_authority is None else f"LIVE ({state_now.mint_authority}) -- burned supply can be reissued", warn=state_now.mint_authority is not None)
    add("pool", state_now.pool.canonical,
        f"{state_now.pool.address} canonical={state_now.pool.canonical}, {state_now.quote_reserve / 1e9:,.2f} SOL / {state_now.base_reserve / 10 ** state_now.decimals:,.0f} tokens, fee tier {state_now.fees.total_bps} bps")
    need = cfg.lot_lamports + buyback.RESERVE_LAMPORTS
    add("wallet SOL", state_now.user_lamports >= need,
        f"{state_now.user_lamports / 1e9:.4f} SOL (one crank needs {need / 1e9:.4f}; the whole budget {(min(remaining, cfg.max_total_lamports) + buyback.RESERVE_LAMPORTS) / 1e9:.4f})")
    if state_now.user_lamports < min(remaining, cfg.max_total_lamports) + buyback.RESERVE_LAMPORTS:
        add("wallet SOL vs budget", False, "the wallet holds less than the remaining budget; the keeper will stop early when it runs dry", warn=True)
    if cfg.also_burn_tokens:
        held = (state_now.user_base_balance or 0) / 10 ** state_now.decimals
        add("held tokens", held >= cfg.also_burn_tokens,
            f"{held:,.0f} held; {cfg.also_burn_tokens:,.0f} burned per crank -> enough for {int(held // cfg.also_burn_tokens) if cfg.also_burn_tokens else 0} crank(s)")

    try:
        plan = buyback.plan_buy_and_burn(
            state_now, lot_lamports=cfg.lot_lamports, slippage_bps=cfg.slippage_bps,
            also_burn=int(round(cfg.also_burn_tokens * 10 ** state_now.decimals)),
            priority_micro_lamports=cfg.priority_fee_micro_lamports,
        )
        sim = buyback.simulate(rpc, buyback.build(plan, blockhash))
        ok = sim.get("err") is None
        add("simulation", ok,
            f"one crank: {plan.base_out / 10 ** plan.decimals:,.2f} tokens for <= {cfg.lot_sol} SOL, price +{plan.impact_bps / 100:.2f}%, {sim.get('unitsConsumed')} CU"
            if ok else buyback.explain(sim) + " | " + " / ".join((sim.get("logs") or [])[-3:]))
    except (buyback.BuybackError, RpcUnavailable) as exc:
        add("simulation", False, str(exc))
    return rows


def render_rows(rows: list[Row]) -> str:
    width = max(len(r.name) for r in rows)
    out = [f"  {r.name:<{width}}  {r.status:<4}  {r.detail}" for r in rows]
    verdict = "GO" if all(r.status != "FAIL" for r in rows) else "NO-GO"
    if verdict == "GO" and any(r.status == "WARN" for r in rows):
        verdict = "GO (with warnings)"
    return "\n".join(out + ["", f"  verdict: {verdict}"])


# -- commands ---------------------------------------------------------------------
def _armed_or_explain(cfg: Config) -> bool:
    if cfg.armed:
        return True
    print(
        f"not armed. {cfg.path} has \"armed\": false, so nothing is signed.\n"
        "Run `keeper preflight`, read every row, then set \"armed\": true when you mean it.",
        file=sys.stderr,
    )
    return False


def _keypair_for(cfg: Config) -> Keypair:
    keypair = Keypair.from_file(cfg.keypair)
    if keypair.address != cfg.wallet:
        raise ConfigError(f"{cfg.keypair} is {keypair.address}; the config names {cfg.wallet}. Refusing.")
    return keypair


def cmd_init(args) -> int:
    path = Path(args.config)
    if path.exists() and not args.force:
        print(f"{path} already exists; pass --force to overwrite", file=sys.stderr)
        return 2
    path.write_text(json.dumps(EXAMPLE_CONFIG, indent=2), encoding="utf-8")
    print(f"wrote {path}. Fill in wallet and keypair, leave armed false, then run: keeper preflight")
    return 0


def cmd_preflight(args, *, rpc=None) -> int:
    cfg = load_config(args.config)
    rows = preflight(cfg, rpc=rpc)
    print(render_rows(rows))
    return 0 if all(r.status != "FAIL" for r in rows) else 1


def cmd_once(args, *, rpc=None, sleep=time.sleep) -> int:
    cfg = load_config(args.config)
    if not _armed_or_explain(cfg):
        return 3
    keypair = _keypair_for(cfg)
    rpc = rpc or RpcClient(cfg.rpc)
    state = load_state(cfg.state_file)
    if cfg.max_total_lamports - state.spent_lamports < cfg.lot_lamports:
        print(f"budget exhausted: {state.spent_lamports / 1e9:.4f} of {cfg.max_total_lamports / 1e9} SOL used", file=sys.stderr)
        return 4
    result = buyback.crank_once(
        rpc, cfg.mint, cfg.wallet, keypair, lot_lamports=cfg.lot_lamports, slippage_bps=cfg.slippage_bps,
        also_burn_ui=cfg.also_burn_tokens, priority_micro_lamports=cfg.priority_fee_micro_lamports, send=True, sleep=sleep,
    )
    line = _line_for(result)
    _record(cfg, state, line)
    print(line)
    return 0 if result["sent"] else 1


def _line_for(result: dict) -> str:
    if result["sent"]:
        return json.dumps({
            "at": int(time.time()), "ok": True, "signature": result["signature"],
            "spent_lamports_max": result["plan"]["expected_cost"]["total"],
            "tokens_burned": result["recorded"]["tokens_burned"], "atomic": result["recorded"]["atomic"],
            "impact_bps": result["plan"]["impact_bps"],
        }, sort_keys=True)
    return json.dumps({"at": int(time.time()), "ok": False, "error": result.get("error"), "simulation": result["simulation"]}, sort_keys=True)


def _record(cfg: Config, state: State, line: str) -> None:
    with open(cfg.log_file, "a", encoding="utf-8") as handle:
        handle.write(line + "\n")
    apply_log_line(state, line, int(time.time()))
    save_state(cfg.state_file, state)
    entry = json.loads(line)
    if entry.get("ok"):
        notify(cfg.notify_url, f"charlie keeper: crank landed {entry['signature']} -- burned {entry['tokens_burned']} raw units, "
                               f"spent <= {entry['spent_lamports_max'] / 1e9:.4f} SOL, total {state.spent_lamports / 1e9:.4f} of {cfg.max_total_lamports / 1e9} SOL")
    else:
        notify(cfg.notify_url, f"charlie keeper: crank FAILED -- {entry.get('error')}")


def cmd_run(args, *, rpc=None, sleep=time.sleep) -> int:
    cfg = load_config(args.config)
    if not _armed_or_explain(cfg):
        return 3
    keypair = _keypair_for(cfg)
    rpc = rpc or RpcClient(cfg.rpc)
    state = load_state(cfg.state_file)
    if state.started_at is None:
        state.started_at = int(time.time())
        save_state(cfg.state_file, state)
    remaining = cfg.max_total_lamports - state.spent_lamports
    if remaining < cfg.lot_lamports:
        print(f"budget exhausted: {state.spent_lamports / 1e9:.4f} of {cfg.max_total_lamports / 1e9} SOL used. Raise max_total_sol or reset the state file to run again.")
        return 4
    print(f"keeper running: {cfg.lot_sol} SOL every {cfg.every_seconds:.0f}s, {remaining / 1e9:.4f} SOL of budget left, wallet {cfg.wallet}. "
          f"Create {cfg.stop_file} to stop.", flush=True)

    def log(line: str) -> None:
        _record(cfg, state, line)
        print(line, flush=True)

    summary = buyback.run_keeper(
        rpc, cfg.mint, keypair, lot_lamports=cfg.lot_lamports, slippage_bps=cfg.slippage_bps,
        every_seconds=cfg.every_seconds, max_total_lamports=remaining, log=log, sleep=sleep,
        also_burn_ui=cfg.also_burn_tokens, priority_micro_lamports=cfg.priority_fee_micro_lamports,
        should_stop=lambda: cfg.stop_file.exists(),
    )
    summary["spent_lamports_total"] = state.spent_lamports
    summary["cranks_total"] = state.cranks
    print(json.dumps(summary, sort_keys=True), flush=True)
    notify(cfg.notify_url, f"charlie keeper stopped: {summary['stopped_because']} after {summary['cranks']} crank(s) this run, "
                           f"{state.spent_lamports / 1e9:.4f} SOL used in total")
    return 0 if summary["stopped_because"] in ("budget reached", "crank count reached", "stop requested") else 1


def cmd_status(args, *, rpc=None) -> int:
    cfg = load_config(args.config)
    state = load_state(cfg.state_file)
    print(f"config   {cfg.path}  armed={cfg.armed}  lot {cfg.lot_sol} SOL / {cfg.every_seconds:.0f}s  budget {cfg.max_total_lamports / 1e9} SOL")
    print(f"state    {state.cranks} crank(s), {state.spent_lamports / 1e9:.4f} SOL used, {state.tokens_burned} raw units burned, "
          f"{state.failures} failure(s), last {state.last_signature or '-'}")
    if state.last_error:
        print(f"         last error: {state.last_error}")
    print(f"stop     {'REQUESTED (' + str(cfg.stop_file) + ' exists)' if cfg.stop_file.exists() else 'not requested'}")
    if cfg.log_file.exists():
        tail = cfg.log_file.read_text(encoding="utf-8").splitlines()[-5:]
        print("log      last lines:")
        for line in tail:
            print(f"         {line}")
    if args.live:
        try:
            live = buyback.observe(rpc or RpcClient(cfg.rpc), cfg.mint, cfg.wallet)
            price = live.quote_reserve / live.base_reserve * 10 ** live.decimals / 1e9
            print(f"live     wallet {live.user_lamports / 1e9:.4f} SOL, {(live.user_base_balance or 0) / 10 ** live.decimals:,.0f} tokens; "
                  f"supply {live.supply / 10 ** live.decimals:,.0f}; pool {live.quote_reserve / 1e9:,.2f} SOL; price {price:.12f} SOL")
        except Exception as exc:  # noqa: BLE001
            print(f"live     could not read the chain: {exc}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="keeper", description="Charlie Protocol BURN-leg keeper: buy and burn from your own wallet, on a schedule.")
    parser.add_argument("--config", default=DEFAULT_CONFIG, help=f"path to the config file (default {DEFAULT_CONFIG})")
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("init", help="write an example config to fill in")
    p.add_argument("--force", action="store_true")
    p.set_defaults(handler=cmd_init)
    sub.add_parser("preflight", help="every go/no-go check, in a table; reads the chain, signs nothing").set_defaults(handler=cmd_preflight)
    sub.add_parser("once", help="one crank now (needs armed: true)").set_defaults(handler=cmd_once)
    sub.add_parser("run", help="the loop, until the budget is spent or the stop file appears (needs armed: true)").set_defaults(handler=cmd_run)
    p = sub.add_parser("status", help="what the keeper has done so far")
    p.add_argument("--live", action="store_true", help="also read the wallet, the pool and the supply")
    p.set_defaults(handler=cmd_status)
    return parser


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.handler(args)
    except ConfigError as exc:
        print(f"config: {exc}", file=sys.stderr)
        return 2
    except buyback.BuybackError as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("interrupted", file=sys.stderr)
        return 130
