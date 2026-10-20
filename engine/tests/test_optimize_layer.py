"""2026-09-25 optimization-layer regressions.

Covers the four A/B-gated levers plus the honest-cost model, mirroring the
test_entry_guards.py convention (pure math + runner wiring + backtest):

  PersistRatchet (A2)  — confirmation-gated profit floor; math, latch/reset,
                         state round-trip, runner exit path, backtest path
  add decay (B2)       — rung taper per campaign entry, runner + backtest
  sizing_lev (B1)      — equity-linked cap/rung, clamp, fuse independence
  exit_slip / funding (C1/C2) — honest-cost deltas move cash the right way

Every lever defaults OFF; the legacy suite (57 tests) is the parity proof.
"""
import tempfile
import time
import unittest

from flygate.config import Config, RiskConfig
from flygate.logs import Log
from flygate.trader.engine import AppState
from flygate.trader.exits import PersistRatchet
from flygate.trader.futures_runner import FuturesRunner

from test_audit_fixes import FakeFutClient, flat_rows, mk_cfg, step_with

TS0 = 1_750_000_000


def mk_cfg_o(risk=None, **fut):
    cfg = mk_cfg(risk)
    cfg.futures.update(fut)
    return cfg


def mk_runner(cfg, d=None):
    d = d or tempfile.mkdtemp()
    r = FuturesRunner(AppState([]), FakeFutClient(), cfg, d, live=False)
    r.log = Log(d)
    return r, d


# ------------------------------------------------------------ pure math
class TestPersistRatchetMath(unittest.TestCase):
    def test_latch_needs_t_consecutive_candles(self):
        r = PersistRatchet(arm=0.015, t=3, lock=0.002)
        r.step(0.02)
        r.step(0.02)
        self.assertFalse(r.latched)          # 2/3 — not yet
        self.assertIsNone(r.floor(100.0))
        r.step(0.02)
        self.assertTrue(r.latched)           # 3rd consecutive -> armed
        self.assertAlmostEqual(r.floor(100.0, True), 100.2)
        self.assertAlmostEqual(r.floor(100.0, False), 99.8)

    def test_wick_does_not_latch_counter_resets(self):
        r = PersistRatchet(arm=0.015, t=3, lock=0.002)
        r.step(0.02)
        r.step(0.02)
        r.step(0.004)                        # one candle below arm
        self.assertEqual(r.count, 0)
        r.step(0.02)
        r.step(0.02)
        self.assertFalse(r.latched)          # 2/3 again — the wick cost it

    def test_latch_is_sticky_and_resets(self):
        r = PersistRatchet(arm=0.015, t=1, lock=0.002)
        r.step(0.02)
        r.step(-0.05)                        # latched survives the drop
        self.assertTrue(r.latched)
        r.reset()
        self.assertFalse(r.latched)
        self.assertEqual(r.count, 0)

    def test_state_roundtrip(self):
        r = PersistRatchet(arm=0.015, t=3, lock=0.002)
        r.step(0.02)
        r.step(0.02)
        r2 = PersistRatchet(arm=0.015, t=3, lock=0.002)
        r2.load_dict(r.as_dict())
        self.assertEqual(r2.count, 2)
        r2.step(0.02)
        self.assertTrue(r2.latched)          # restart keeps the progress


# ------------------------------------------------------- runner wiring
class TestRunnerRatchet(unittest.TestCase):
    # risk isolates the ratchet: breakeven_trigger 99 disables the instant
    # breakeven floor (the ratchet is its persistence-gated sibling)
    RISK = dict(cooldown_s=0, max_orders_per_day=0, breakeven_trigger=99.0)

    def _armed_runner(self, persist=True, d=None):
        fut = dict(persist_t=3, persist_arm=0.015, persist_lock=0.002) \
            if persist else {}
        cfg = mk_cfg_o(RiskConfig(**self.RISK), **fut)
        r, d = mk_runner(cfg, d)
        ts = TS0
        step_with(r, "BUY", 100.0, ts)       # entry_step=2 @ ~100.05 (slip)
        self.assertEqual(r.broker.size, 2)
        for k in range(3):                   # 3 candles @102: pnl ~1.95%
            ts += 60
            step_with(r, "HOLD", 102.0, ts)
        return r, d, ts

    def test_latches_after_t_candles_and_exits_on_giveback(self):
        r, _, ts = self._armed_runner()
        self.assertTrue(r.ratchet.latched)
        step_with(r, "HOLD", 100.1, ts + 60)   # <= entry*1.002 ~ 100.25
        self.assertEqual(r.broker.size, 0)     # closed by the ratchet floor

    def test_control_without_ratchet_holds_through(self):
        r, _, ts = self._armed_runner(persist=False)
        self.assertIsNone(r.ratchet)
        step_with(r, "HOLD", 100.1, ts + 60)   # trail sits at 102*0.97=98.94
        self.assertEqual(r.broker.size, 2)     # the giveback gap, unrepaired

    def test_counter_resets_on_dip_below_arm(self):
        cfg = mk_cfg_o(RiskConfig(**self.RISK),
                       persist_t=3, persist_arm=0.015, persist_lock=0.002)
        r, _ = mk_runner(cfg)
        ts = TS0
        step_with(r, "BUY", 100.0, ts)
        step_with(r, "HOLD", 102.0, ts + 60)
        step_with(r, "HOLD", 102.0, ts + 120)
        self.assertEqual(r.ratchet.count, 2)
        step_with(r, "HOLD", 100.4, ts + 180)  # pnl 0.35% < arm
        self.assertEqual(r.ratchet.count, 0)
        self.assertFalse(r.ratchet.latched)
        self.assertEqual(r.broker.size, 2)     # nothing exited

    def test_latch_survives_restart(self):
        r, d, ts = self._armed_runner()
        r._save()
        r2, _ = mk_runner(mk_cfg_o(RiskConfig(**self.RISK),
                                   persist_t=3, persist_arm=0.015,
                                   persist_lock=0.002), d)
        self.assertTrue(r2.ratchet.latched)
        step_with(r2, "HOLD", 100.1, ts + 60)
        self.assertEqual(r2.broker.size, 0)    # protection restored, not lost


class TestRunnerAddDecay(unittest.TestCase):
    def test_rungs_taper_within_a_campaign(self):
        cfg = mk_cfg_o(RiskConfig(cooldown_s=0, max_orders_per_day=0),
                       entry_step=2, add_decay=0.5, max_contracts=10)
        r, _ = mk_runner(cfg)
        ts = TS0
        step_with(r, "BUY", 100.0, ts)         # rung 2*0.5^0 = 2
        self.assertEqual(r.broker.size, 2)
        self.assertEqual(r.campaign_entries, 1)
        step_with(r, "BUY", 100.0, ts + 60)    # rung 2*0.5^1 = 1
        self.assertEqual(r.broker.size, 3)
        step_with(r, "BUY", 100.0, ts + 120)   # rung max(1, 2*0.5^2) = 1
        self.assertEqual(r.broker.size, 4)
        self.assertEqual(r.campaign_entries, 3)

    def test_flat_resets_the_decay_index(self):
        cfg = mk_cfg_o(RiskConfig(cooldown_s=0, max_orders_per_day=0),
                       entry_step=2, add_decay=0.5, max_contracts=10,
                       long_only=True)
        r, _ = mk_runner(cfg)
        ts = TS0
        step_with(r, "BUY", 100.0, ts)
        step_with(r, "BUY", 100.0, ts + 60)    # size 3, entries 2
        step_with(r, "SELL", 100.0, ts + 120)  # long_only: counter close
        self.assertEqual(r.broker.size, 0)
        self.assertEqual(r.campaign_entries, 0)
        step_with(r, "BUY", 100.0, ts + 180)   # fresh campaign: full rung
        self.assertEqual(r.broker.size, 2)


class TestRunnerSizing(unittest.TestCase):
    def test_equity_linked_rung_replaces_fixed_step(self):
        cfg = mk_cfg_o(RiskConfig(cooldown_s=0, max_orders_per_day=0),
                       entry_step=3, sizing_lev=1.0, max_contracts=100,
                       budget=60)
        r, _ = mk_runner(cfg)
        # equity 60, px 100, quanto 0.01 -> cap = 60/(0.01*100) = 60,
        # rung = 60//8 = 7 (not the fixed entry_step 3)
        self.assertEqual(r._cap_now(60.0, 100.0), 60)
        step_with(r, "BUY", 100.0, TS0)
        self.assertEqual(r.broker.size, 7)

    def test_config_cap_clamps_and_fuse_still_guards_it(self):
        cfg = mk_cfg_o(RiskConfig(cooldown_s=0, max_orders_per_day=0),
                       entry_step=3, sizing_lev=1.9, max_contracts=5,
                       budget=60)
        r, _ = mk_runner(cfg)
        self.assertEqual(r._cap_now(60.0, 100.0), 5)   # min(114, cap) = 5
        r.broker.size = 8                              # wrong-size-read bug
        r._fuse_overcap(100.0, TS0)                    # fuse uses config cap
        self.assertEqual(r.broker.size, 5)

    def test_legacy_fixed_step_when_sizing_off(self):
        cfg = mk_cfg_o(RiskConfig(cooldown_s=0, max_orders_per_day=0),
                       entry_step=3, max_contracts=100, budget=60)
        r, _ = mk_runner(cfg)
        self.assertEqual(r._cap_now(60.0, 100.0), 100)
        step_with(r, "BUY", 100.0, TS0)
        self.assertEqual(r.broker.size, 3)


# ------------------------------------------------------- backtest paths
class ScriptedBrain:
    """Stand-in for FlyBrain: replays a decision script, r2 pinned high."""
    script = []

    def __init__(self, brain_cfg, **kw):
        self.i = 0

    def observe(self, rows, delta):
        d = (ScriptedBrain.script[self.i]
             if self.i < len(ScriptedBrain.script) else "HOLD")
        self.i += 1
        return {"decision": d, "r2": 0.9}


def _tape(prices, ts0=TS0, step=300):
    return [[ts0 + i * step, p, p, p, p, 1.0] for i, p in enumerate(prices)]


class TestRunDualLevers(unittest.TestCase):
    """run_dual with a scripted brain: exact fills, exact expectations."""

    def setUp(self):
        import flygate.backtest_futures as bf
        self.bf = bf
        self._real_brain = bf.FlyBrain
        bf.FlyBrain = ScriptedBrain

    def tearDown(self):
        self.bf.FlyBrain = self._real_brain

    def _cfg(self):
        cfg = Config()
        cfg.risk = RiskConfig(cooldown_s=0, max_orders_per_day=0,
                              breakeven_trigger=99.0, daily_loss_halt=0)
        return cfg

    def _run(self, prices, script, **kw):
        ScriptedBrain.script = script
        kw.setdefault("mode", "long_only")
        kw.setdefault("budget", 100.0)
        kw.setdefault("order_n", 10.0)
        kw.setdefault("cap", 30.0)
        kw.setdefault("chase_pct", None)
        return self.bf.run_dual(self._cfg(), _tape(prices), verbose=False, **kw)

    def test_ratchet_closes_the_giveback_gap(self):
        prices = [100.0] * 50 + [102.0] * 3 + [100.1] * 3
        script = ["BUY"] + ["HOLD"] * 10
        res = self._run(prices, script, persist=(0.015, 3, 0.002))
        self.assertIn("ratchet", res["close_reasons"])

    def test_control_holds_the_same_tape_to_the_end(self):
        prices = [100.0] * 50 + [102.0] * 3 + [100.1] * 3
        script = ["BUY"] + ["HOLD"] * 10
        res = self._run(prices, script)
        self.assertNotIn("ratchet", res["close_reasons"])
        self.assertEqual(res["close_reasons"].get("end"), 1)

    def test_exit_slip_costs_cash(self):
        prices = [100.0] * 50 + [101.0] * 6
        script = ["BUY"] + ["HOLD"] * 10
        plain = self._run(prices, script)["final"]
        slipped = self._run(prices, script, exit_slip=True)["final"]
        self.assertLess(slipped, plain)       # long exit fills below close

    def test_funding_charges_the_position(self):
        prices = [100.0] * 50 + [100.0] * 6
        script = ["BUY"] + ["HOLD"] * 10
        fts = TS0 + 52 * 300
        res = self._run(prices, script, funding=[[fts, 0.01]])
        # qty ~ 10/100.03; 1% of notional at px 100 ~ 0.1
        self.assertAlmostEqual(res["funding_total"], 0.1, delta=0.01)
        plain = self._run(prices, script)["final"]
        self.assertAlmostEqual(plain - res["final"],
                               res["funding_total"], delta=1e-6)

    def test_add_decay_tapers_entry_notionals(self):
        prices = [100.0] * 56
        script = ["BUY", "BUY", "BUY"] + ["HOLD"] * 10
        res = self._run(prices, script, add_decay=0.5)
        notionals = [e[1] for e in res["entry_log"][:3]]
        self.assertEqual(notionals, [10.0, 5.0, 2.5])
        self.assertEqual([e[2] for e in res["entry_log"][:3]], [0, 1, 2])

    def test_legacy_entries_are_untapered(self):
        prices = [100.0] * 56
        script = ["BUY", "BUY", "BUY"] + ["HOLD"] * 10
        res = self._run(prices, script)
        self.assertEqual([e[1] for e in res["entry_log"][:3]],
                         [10.0, 10.0, 10.0])

    def test_sizing_lev_links_rung_to_equity(self):
        prices = [100.0] * 56
        script = ["BUY"] + ["HOLD"] * 10
        res = self._run(prices, script, sizing_lev=1.0)
        # cap = 1.0 * equity(100) -> rung = 100/8 = 12.5
        self.assertEqual(res["entry_log"][0][1], 12.5)


if __name__ == "__main__":
    unittest.main()
