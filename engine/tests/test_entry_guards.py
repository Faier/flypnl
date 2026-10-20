"""Entry-guard regressions (2026-09-24 anti-chase layer).

Covers the shared EntryGuards math and its wiring into the live runner:
A extension veto, B add anchor, C leg age, D soft-pullback retest, and the
MR (mean-reversion) entry mode — mirroring the backtest arms that must win
the 12mo A/B before any of this reaches config.json.
"""
import os
import tempfile
import time
import unittest

from flygate.config import RiskConfig
from flygate.logs import Log
from flygate.trader.engine import AppState
from flygate.trader.entry_guards import EntryGuards
from flygate.trader.futures_runner import FuturesRunner

from test_audit_fixes import FakeFutClient, flat_rows, mk_cfg, step_with


def mk_cfg_g(guards=None, **fut):
    cfg = mk_cfg()
    cfg.futures.update(fut)
    if guards:
        cfg.futures.update(guards)
    return cfg


def mk_runner(cfg):
    d = tempfile.mkdtemp()
    r = FuturesRunner(AppState([]), FakeFutClient(), cfg, d, live=False)
    r.log = Log(d)
    return r


def noisy_rows(px, ts, n=80, step=60, spread=1.0):
    """Flat tape with a constant 2*spread candle range so ATR converges."""
    return [[ts - step * (n - 1 - i), px, px + spread, px - spread, px, 1.0]
            for i in range(n)]


def step_n(runner, decision, px, ts):
    """step_with, but on range-y candles (flat candles give ATR=0 and every
    ATR-normalized guard goes inert)."""
    runner.brain.observe = lambda rows, delta: {"decision": decision,
                                                "r2": 0.9}
    runner._step(noisy_rows(px, ts, n=3))


# ------------------------------------------------------------ pure math
class TestEntryGuardsMath(unittest.TestCase):
    def _warm(self, g, px=1000.0, n=300):
        ts = int(time.time())
        for c in noisy_rows(px, ts, n=n):
            g.step(c)

    def test_z_and_extended(self):
        g = EntryGuards(ext_atr=2.0)
        self._warm(g)                       # ATR -> 2.0, ema -> 1000
        self.assertFalse(g.extended(1002))  # z ~ +1
        self.assertTrue(g.extended(1030))   # z far above 2 ATR
        self.assertLess(g.z(990), 0)

    def test_add_anchor(self):
        g = EntryGuards(add_max_atr=1.5)
        self._warm(g)
        self.assertTrue(g.add_ok(1002, 1000.0))
        self.assertFalse(g.add_ok(1030, 1000.0))
        self.assertTrue(g.add_ok(990, 1000.0))   # below avg is always fine

    def test_leg_age(self):
        g = EntryGuards(leg_max_age=5)
        ts = int(time.time())
        for i, px in enumerate([1000] * 80):
            g.step([ts + 60 * i, px, px, px, px, 1.0])
        # a rising tape: every close sits above the lagging ema20
        for i in range(10):
            g.step([ts + 6000 + 60 * i, 1005 + i, 1006 + i, 1004 + i,
                    1005 + i, 1.0])
            g.leg_age += 0                  # (readability)
        self.assertGreater(g.leg_age, 5)
        self.assertTrue(g.leg_too_old())
        g2 = EntryGuards(leg_max_age=5)     # disabled -> never vetoes
        self._warm(g2)
        self.assertFalse(g2.leg_too_old())

    def test_mr_predicates(self):
        g = EntryGuards(mr_buy_atr=3.0, mr_sell_atr=0.5, mr_max_r2=0.6)
        self._warm(g)
        dip = [0, 960.0, 966.0, 955.0, 965.0, 1.0]
        self.assertTrue(g.mr_buy(965.0, dip, {"hs_dn": 0.1, "r2": 0.3}))
        self.assertFalse(g.mr_buy(965.0, [0, 965.0, 966.0, 955.0, 960.0, 1.0],
                                   {"hs_dn": 0.1, "r2": 0.3}))   # red candle
        self.assertFalse(g.mr_buy(965.0, dip, {"hs_dn": 0.9, "r2": 0.3}))  # knife
        self.assertFalse(g.mr_buy(965.0, dip, {"hs_dn": 0.1, "r2": 0.95}))  # trending
        self.assertFalse(g.mr_target(1000.5))     # z ~ +0.25 ATR — hold
        self.assertTrue(g.mr_target(1002.0))      # z ~ +1 ATR — sell strength

    def test_from_config_zero_means_off(self):
        g = EntryGuards.from_config({"ext_atr": 0, "add_max_atr": None,
                                     "leg_max_age": 0, "pullback_atr": "",
                                     "mr_buy_atr": 0})
        self.assertIsNone(g.ext_atr)
        self.assertIsNone(g.add_max_atr)
        self.assertIsNone(g.leg_max_age)
        self.assertIsNone(g.pullback_atr)
        self.assertIsNone(g.mr_buy_atr)
        self.assertFalse(g.mr_on)


# ------------------------------------------------------- runner wiring
class TestRunnerGuards(unittest.TestCase):
    def test_extension_vetoes_fresh_entry(self):
        r = mk_runner(mk_cfg_g(dict(ext_atr=2.0)))
        ts = int(time.time())
        step_n(r, "HOLD", 1000.0, ts)             # warm the baselines (ATR 2)
        step_n(r, "BUY", 1120.0, ts + 60)         # ~10 ATR above the mean
        self.assertEqual(r.broker.size, 0)
        self.assertIn("ext-guard", r.state.pairs[r.key]["telemetry"].get("note", ""))

    def test_add_anchor_blocks_pyramid_top(self):
        r = mk_runner(mk_cfg_g(dict(add_max_atr=1.5)))
        ts = int(time.time())
        step_n(r, "HOLD", 1000.0, ts)
        r.broker.size, r.broker.entry = 2, 1000.0
        r.extreme = 1000.0
        step_n(r, "BUY", 1004.0, ts + 60)         # 2 ATR from avg entry (>1.5)
        self.assertEqual(r.broker.size, 2)        # no add
        step_n(r, "BUY", 1002.0, ts + 600)        # 1 ATR — near avg, add goes
        self.assertEqual(r.broker.size, 4)

    def test_leg_age_vetoes_old_staircase(self):
        r = mk_runner(mk_cfg_g(dict(leg_max_age=5)))
        ts = int(time.time())
        r._step(noisy_rows(1000.0, ts))           # warm
        for i in range(6):                        # 6 slow up-candles
            px = 1001.0 + i
            r._step([[ts + 60 * (i + 1), px - 1, px + 1, px - 2, px, 1.0]])
        step_with(r, "BUY", 1007.0, ts + 600)
        self.assertEqual(r.broker.size, 0)
        self.assertIn("leg-age", r.state.pairs[r.key]["telemetry"].get("note", ""))
        r._step([[ts + 660, 990.0, 991.0, 989.0, 990.0, 1.0]])  # break below ema
        step_with(r, "BUY", 991.0, ts + 720)      # young leg again
        self.assertEqual(r.broker.size, 2)

    def test_pullback_retest_fills_and_expires(self):
        r = mk_runner(mk_cfg_g(dict(pullback_atr=0.5, pullback_ttl=3)))
        ts = int(time.time())
        step_n(r, "HOLD", 1000.0, ts)             # ATR -> 2.0, limit = px-1
        step_n(r, "BUY", 1000.0, ts + 60)         # arms the retest, no market buy
        self.assertEqual(r.broker.size, 0)
        self.assertGreater(r.pb_limit, 0)
        step_n(r, "HOLD", 998.5, ts + 120)        # close <= limit -> fill
        self.assertEqual(r.broker.size, 2)
        self.assertEqual(r.pb_limit, 0.0)
        # expiry arm: a signal that never retests must not linger armed
        r2 = mk_runner(mk_cfg_g(dict(pullback_atr=0.5, pullback_ttl=3)))
        step_n(r2, "HOLD", 1000.0, ts)
        step_n(r2, "BUY", 1000.0, ts + 60)
        for i in range(4):                        # ttl 3 -> disarmed
            step_n(r2, "HOLD", 1000.5, ts + 120 + 60 * i)
        self.assertEqual(r2.pb_limit, 0.0)
        step_n(r2, "HOLD", 998.0, ts + 400)       # dip now opens nothing
        self.assertEqual(r2.broker.size, 0)

    def test_mr_mode_buys_dip_sells_strength(self):
        risk = RiskConfig(cooldown_s=0, max_orders_per_day=0)
        cfg = mk_cfg(risk=risk)
        cfg.futures.update({"entry_mode": "mr", "mr_buy_atr": 3.0,
                            "mr_sell_atr": 0.5})
        r = mk_runner(cfg)
        ts = int(time.time())
        step_n(r, "HOLD", 1000.0, ts)             # warm: ATR 2, ema 1000
        # deep dip closing green ~7 ATR below the mean
        r._step([[ts + 60, 960.0, 966.0, 955.0, 965.0, 1.0]])
        self.assertGreater(r.broker.size, 0)      # mr-dip bought
        # recover well above the mean -> mr-target close
        step_n(r, "HOLD", 1015.0, ts + 600)
        self.assertEqual(r.broker.size, 0)
        self.assertTrue(any(t.reason == "mr-target" for t in r.broker.trades))

    def test_mr_mode_ignores_brain_signals(self):
        r = mk_runner(mk_cfg_g(dict(entry_mode="mr")))
        ts = int(time.time())
        step_n(r, "HOLD", 1000.0, ts)
        step_n(r, "BUY", 1004.0, ts + 60)         # staircase BUY: ignored
        self.assertEqual(r.broker.size, 0)
        r.broker.size, r.broker.entry = 2, 1000.0  # positioned (say, an mr-dip)
        r.extreme = 1000.0
        step_n(r, "SELL", 1001.0, ts + 600)       # counter-signal: suppressed
        self.assertEqual(r.broker.size, 2)

    def test_regime_mode_switches_stair_and_mr(self):
        risk = RiskConfig(cooldown_s=0, max_orders_per_day=0)
        cfg = mk_cfg(risk=risk)
        cfg.futures.update({"entry_mode": "regime", "mr_buy_atr": 3.0,
                            "mr_sell_atr": 0.5, "regime_r2": 0.6})
        r = mk_runner(cfg)
        ts = int(time.time())
        # pin the brain's coherence to force each regime
        pin = lambda rows, delta, r2: {"decision": "HOLD", "r2": r2}
        r.brain.observe = lambda rows, delta: pin(rows, delta, 0.9)   # trending
        r._step(noisy_rows(1000.0, ts, n=90))      # warm + trend regime
        self.assertFalse(r.guards.in_range())
        r.brain.observe = lambda rows, delta: pin(rows, delta, 0.1)   # ranging
        for i in range(120):                        # 120 candles decay the EMA
            r._step(noisy_rows(1000.0, ts + 60 * i, n=2))
        self.assertTrue(r.guards.in_range())
        # ranging: a deep green dip fires mr-dip; a SELL staircase must NOT
        # close it (MR owns exits) — and recovery closes at mr-target
        r._step([[ts + 60 * 200, 960.0, 966.0, 955.0, 965.0, 1.0]])
        self.assertGreater(r.broker.size, 0)
        r.brain.observe = lambda rows, delta: {**pin(rows, delta, 0.1),
                                               "decision": "SELL"}
        r._step(noisy_rows(966.0, ts + 60 * 201, n=2))
        self.assertGreater(r.broker.size, 0)       # counter-signal suppressed
        r.brain.observe = lambda rows, delta: pin(rows, delta, 0.1)
        r._step(noisy_rows(1015.0, ts + 60 * 202, n=2))
        self.assertEqual(r.broker.size, 0)
        self.assertTrue(any(t.reason == "mr-target" for t in r.broker.trades))
        # trending again: stair BUY opens through the classic path
        r.brain.observe = lambda rows, delta: {**pin(rows, delta, 0.9),
                                               "decision": "BUY"}
        for i in range(140):                        # regime EMA decays back up
            r._step(noisy_rows(1000.0, ts + 60 * 300 + 60 * i, n=2))
        self.assertFalse(r.guards.in_range())
        self.assertGreaterEqual(r.broker.size, 2)   # staircase-up opened

    def test_pullback_state_survives_restart(self):
        d = tempfile.mkdtemp()
        cfg = mk_cfg_g(dict(pullback_atr=0.5, pullback_ttl=9))
        r = FuturesRunner(AppState([]), FakeFutClient(), cfg, d, live=False)
        ts = int(time.time())
        step_n(r, "HOLD", 1000.0, ts)
        step_n(r, "BUY", 1000.0, ts + 60)
        r._save()
        r2 = FuturesRunner(AppState([]), FakeFutClient(), cfg, d, live=False)
        self.assertGreater(r2.pb_limit, 0.0)
        self.assertAlmostEqual(r2.pb_limit, r.pb_limit)
        self.assertEqual(r2.pb_ttl, r.pb_ttl)

    def test_hard_stop_flats_bleeding_position(self):
        # stop at −1% with a gentle −1.2% step: below the hard stop, well
        # inside the single-candle flash threshold so only the stop fires
        r = mk_runner(mk_cfg_g(dict(hard_stop_pct=0.01)))
        ts = int(time.time())
        step_n(r, "HOLD", 1000.0, ts)
        r.broker.size, r.broker.entry = 2, 1000.0
        r.extreme = 1000.0
        step_n(r, "HOLD", 988.0, ts + 60)      # −1.2% <= −1% hard stop
        self.assertEqual(r.broker.size, 0)
        self.assertTrue(any(t.reason.startswith("reflex:hard-stop")
                            for t in r.broker.trades))
        # and it does NOT fire inside the band
        r2 = mk_runner(mk_cfg_g(dict(hard_stop_pct=0.01)))
        step_n(r2, "HOLD", 1000.0, ts)
        r2.broker.size, r2.broker.entry = 2, 1000.0
        r2.extreme = 1000.0
        step_n(r2, "HOLD", 995.0, ts + 60)     # −0.5% — holds
        self.assertEqual(r2.broker.size, 2)


if __name__ == "__main__":
    unittest.main()
