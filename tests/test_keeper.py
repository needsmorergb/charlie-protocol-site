"""The production keeper: config that refuses what it should, state that
survives restarts, a preflight that says no-go for the right reasons, and a
built copy that cannot drift from its source."""

from __future__ import annotations

import io
import json
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import test_buyback as tb  # noqa: E402
from indexer import buyback, keeper  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]


class Sandbox:
    """A temp folder holding a config, a keypair file and (optionally) state."""

    def __init__(self, **overrides):
        self.dir = Path(tempfile.mkdtemp())
        self.keypair = self.dir / "keeper-keypair.json"
        self.keypair.write_text(json.dumps(list(tb.SEED + tb.KEYPAIR.public)))
        raw = dict(keeper.EXAMPLE_CONFIG)
        raw.update({"wallet": tb.USER, "keypair": "keeper-keypair.json", "max_total_sol": 0.12, "armed": True})
        raw.update(overrides)
        self.config = self.dir / "keeper.json"
        self.config.write_text(json.dumps(raw))

    def load(self):
        return keeper.load_config(self.config)

    def args(self, **kw):
        ns = type("Args", (), {})()
        ns.config = str(self.config)
        ns.live = False
        ns.force = False
        for k, v in kw.items():
            setattr(ns, k, v)
        return ns


def quiet(fn, *args, **kwargs):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = fn(*args, **kwargs)
    return code, out.getvalue(), err.getvalue()


class TestConfig(unittest.TestCase):
    def test_example_loads_once_filled_in(self):
        cfg = Sandbox().load()
        self.assertEqual(cfg.mint, keeper.CHARLIE_MINT)
        self.assertEqual(cfg.lot_lamports, 50_000_000)
        self.assertEqual(cfg.max_total_lamports, 120_000_000)
        self.assertTrue(cfg.keypair.is_absolute())
        self.assertEqual(cfg.stop_file.parent, cfg.path.parent)

    def test_refusals(self):
        cases = [
            ({"lot_sol": 5}, "allow_large_lot"),
            ({"lot_sol": 0.0001}, "minimum"),
            ({"every_seconds": 5}, "minimum interval"),
            ({"max_total_sol": 0}, "positive budget"),
            ({"max_total_sol": 0.01}, "smaller than one lot"),
            ({"wallet": "not-an-address"}, "not a valid Solana address"),
            ({"typo_key": 1}, "unknown keys"),
            ({"notify_url": "http://insecure"}, "https"),
            ({"slippage_bps": 9000}, "slippage_bps"),
        ]
        for overrides, needle in cases:
            with self.subTest(overrides=overrides):
                with self.assertRaises(keeper.ConfigError) as ctx:
                    Sandbox(**overrides).load()
                self.assertIn(needle, str(ctx.exception))

    def test_large_lot_needs_the_explicit_flag(self):
        cfg = Sandbox(lot_sol=2, max_total_sol=4, allow_large_lot=True).load()
        self.assertEqual(cfg.lot_lamports, 2_000_000_000)

    def test_missing_file_points_at_init(self):
        with self.assertRaises(keeper.ConfigError) as ctx:
            keeper.load_config(Path(tempfile.mkdtemp()) / "nope.json")
        self.assertIn("keeper init", str(ctx.exception))

    def test_init_writes_an_unarmed_example(self):
        box = Sandbox()
        target = box.dir / "fresh.json"
        code, out, _ = quiet(keeper.cmd_init, box.args(config=str(target)))
        self.assertEqual(code, 0)
        self.assertFalse(json.loads(target.read_text())["armed"])
        code, _, err = quiet(keeper.cmd_init, box.args(config=str(target)))
        self.assertEqual(code, 2)
        self.assertIn("--force", err)


class TestState(unittest.TestCase):
    def test_log_lines_fold_into_state_and_round_trip(self):
        box = Sandbox()
        state = keeper.State()
        keeper.apply_log_line(state, json.dumps({"ok": True, "signature": "s1", "spent_lamports_max": 49_000_000, "tokens_burned": 7}), 100)
        keeper.apply_log_line(state, json.dumps({"ok": False, "error": "boom"}), 101)
        keeper.apply_log_line(state, "not json", 102)
        keeper.save_state(box.dir / "state.json", state)
        loaded = keeper.load_state(box.dir / "state.json")
        self.assertEqual((loaded.cranks, loaded.spent_lamports, loaded.tokens_burned, loaded.failures), (1, 49_000_000, 7, 1))
        self.assertEqual(loaded.last_signature, "s1")
        self.assertEqual(loaded.last_error, "boom")
        self.assertFalse((box.dir / "state.json.tmp").exists())

    def test_absent_state_is_empty(self):
        self.assertEqual(keeper.load_state(Path(tempfile.mkdtemp()) / "x.json").spent_lamports, 0)


class TestPreflight(unittest.TestCase):
    def rows(self, box, rpc=None, **kw):
        return {r.name: r for r in keeper.preflight(box.load(), rpc=rpc or tb.FakeRpc(tb.chain(), tx=tb.landed_tx(1)), **kw)}

    def test_go_when_everything_is_right(self):
        rows = self.rows(Sandbox())
        self.assertTrue(all(r.status == "PASS" for r in rows.values()), {k: (v.status, v.detail) for k, v in rows.items()})
        self.assertIn("GO", keeper.render_rows(list(rows.values())))
        self.assertIn("tokens for <= 0.05 SOL", rows["simulation"].detail)

    def test_unarmed_is_a_warning_not_a_failure(self):
        rows = self.rows(Sandbox(armed=False))
        self.assertEqual(rows["armed"].status, "WARN")
        self.assertIn("GO (with warnings)", keeper.render_rows(list(rows.values())))

    def test_wrong_keypair_fails(self):
        box = Sandbox()
        box.keypair.write_text(json.dumps(list(bytes(range(32)) + tb.ed25519.public_key(bytes(range(32))))))
        rows = self.rows(box)
        self.assertEqual(rows["keypair"].status, "FAIL")
        self.assertIn("REFUSING", rows["keypair"].detail)

    def test_stop_file_and_spent_budget_fail(self):
        box = Sandbox()
        (box.dir / "keeper.stop").touch()
        keeper.save_state(box.dir / "keeper-state.json", keeper.State(spent_lamports=120_000_000, cranks=3))
        rows = self.rows(box)
        self.assertEqual(rows["stop file"].status, "FAIL")
        self.assertEqual(rows["budget"].status, "FAIL")

    def test_failed_simulation_is_a_no_go(self):
        rpc = tb.FakeRpc(tb.chain(), sim={"err": {"x": 1}, "logs": ["Program log: ExceededSlippage"]})
        rows = self.rows(Sandbox(), rpc=rpc)
        self.assertEqual(rows["simulation"].status, "FAIL")
        self.assertIn("NO-GO", keeper.render_rows(list(rows.values())))

    def test_no_pool_is_reported_not_raised(self):
        accounts = tb.chain()
        del accounts[buyback.canonical_pool(tb.MINT)]
        rows = self.rows(Sandbox(), rpc=tb.FakeRpc(accounts))
        self.assertEqual(rows["coin"].status, "FAIL")
        self.assertIn("graduated", rows["coin"].detail)

    def test_old_python_fails(self):
        rows = self.rows(Sandbox(), python_version=(3, 9, 0))
        self.assertEqual(rows["python"].status, "FAIL")


class TestCommands(unittest.TestCase):
    def test_once_refuses_unarmed(self):
        code, _, err = quiet(keeper.cmd_once, Sandbox(armed=False).args(), rpc=tb.FakeRpc(tb.chain()))
        self.assertEqual(code, 3)
        self.assertIn("not armed", err)

    def test_once_sends_and_persists(self):
        box = Sandbox()
        rpc = tb.FakeRpc(tb.chain(), tx=tb.landed_tx(1))
        code, out, _ = quiet(keeper.cmd_once, box.args(), rpc=rpc, sleep=lambda s: None)
        self.assertEqual(code, 0)
        self.assertEqual(len(rpc.sent), 1)
        state = keeper.load_state(box.dir / "keeper-state.json")
        self.assertEqual(state.cranks, 1)
        self.assertEqual(state.last_signature, "5ignature")
        self.assertEqual(len((box.dir / "keeper.log.jsonl").read_text().splitlines()), 1)
        self.assertIn('"ok": true', out)

    def test_run_honours_budget_across_restarts_and_the_stop_file(self):
        box = Sandbox()
        rpc = tb.FakeRpc(tb.chain(), tx=tb.landed_tx(1))
        rpc.statuses = [{"confirmationStatus": "confirmed", "err": None}] * 10
        # a previous run already used one lot's worth of the 0.12 SOL budget
        keeper.save_state(box.dir / "keeper-state.json", keeper.State(spent_lamports=49_000_000, cranks=1))
        code, out, _ = quiet(keeper.cmd_run, box.args(), rpc=rpc, sleep=lambda s: None)
        self.assertEqual(code, 0)
        self.assertEqual(len(rpc.sent), 1)  # one more lot fits; a third would overshoot
        summary = json.loads(out.strip().splitlines()[-1])
        self.assertEqual(summary["stopped_because"], "budget reached")
        self.assertEqual(summary["cranks_total"], 2)
        self.assertLessEqual(keeper.load_state(box.dir / "keeper-state.json").spent_lamports, 120_000_000)
        # and a further run refuses outright
        code, out, _ = quiet(keeper.cmd_run, box.args(), rpc=rpc, sleep=lambda s: None)
        self.assertEqual(code, 4)

        box2 = Sandbox(max_total_sol=5)
        rpc2 = tb.FakeRpc(tb.chain(), tx=tb.landed_tx(1))
        rpc2.statuses = [{"confirmationStatus": "confirmed", "err": None}] * 10

        def sleep_then_stop(seconds):
            (box2.dir / "keeper.stop").touch()

        code, out, _ = quiet(keeper.cmd_run, box2.args(), rpc=rpc2, sleep=sleep_then_stop)
        self.assertEqual(code, 0)
        self.assertEqual(len(rpc2.sent), 1)
        self.assertEqual(json.loads(out.strip().splitlines()[-1])["stopped_because"], "stop requested")

    def test_status_reads_state_and_log(self):
        box = Sandbox()
        rpc = tb.FakeRpc(tb.chain(), tx=tb.landed_tx(1))
        quiet(keeper.cmd_once, box.args(), rpc=rpc, sleep=lambda s: None)
        code, out, _ = quiet(keeper.cmd_status, box.args(live=True), rpc=tb.FakeRpc(tb.chain()))
        self.assertEqual(code, 0)
        self.assertIn("1 crank(s)", out)
        self.assertIn("5ignature", out)
        self.assertIn("live", out)

    def test_main_reports_config_errors_as_exit_2(self):
        box = Sandbox(lot_sol=7)
        code, _, err = quiet(keeper.main, ["--config", str(box.config), "status"])
        self.assertEqual(code, 2)
        self.assertIn("allow_large_lot", err)


class TestNotify(unittest.TestCase):
    def test_posts_json_and_never_raises(self):
        seen = {}

        class Response:
            status = 204

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

        def opener(request, timeout):
            seen["url"] = request.full_url
            seen["body"] = json.loads(request.data)
            return Response()

        self.assertTrue(keeper.notify("https://hooks.example/x", "hello", opener=opener))
        self.assertEqual(seen["body"]["text"], "hello")
        self.assertFalse(keeper.notify(None, "hello"))

        def broken(request, timeout):
            raise OSError("down")

        self.assertFalse(keeper.notify("https://hooks.example/x", "hello", opener=broken))


class TestBuiltCopy(unittest.TestCase):
    """The committed `charlie-keeper/` must be exactly what the build
    produces from the current source. A stale copy is the drift this
    project exists to make visible, so the suite fails on it."""

    def test_committed_copy_is_current(self):
        sys.path.insert(0, str(ROOT / "scripts"))
        import build_keeper
        problems = build_keeper.check(ROOT / "charlie-keeper")
        self.assertEqual(problems, [])

    def test_the_shipped_entry_point_runs_the_shipped_indexer(self):
        import subprocess
        out = subprocess.run(
            [sys.executable, str(ROOT / "charlie-keeper" / "keeper.py"), "--help"],
            capture_output=True, text=True, cwd=tempfile.mkdtemp(),
        )
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertIn("preflight", out.stdout)


if __name__ == "__main__":
    unittest.main()
