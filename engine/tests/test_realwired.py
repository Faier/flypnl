"""real_wired 臂（MaleCNS 真实接线，C 阶段冻结突触）的结构性测试。

没有连接组数据时整组跳过（CI / 新机器友好）：
  data/connectome/{body-annotations,connectome-weights}-*.feather
"""
from __future__ import annotations

import glob
import os
import unittest

import numpy as np

from flygate.config import BrainConfig
from flygate.brain.flybrain import FlyBrain

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_RAW = (glob.glob(os.path.join(_REPO, "data/connectome/body-annotations-*.feather"))
        and glob.glob(os.path.join(_REPO, "data/connectome/connectome-weights-*.feather")))


def _candles(n: int, seed: int = 0) -> list:
    rng = np.random.default_rng(seed)
    out, px = [], 100.0
    for i in range(n):
        o = px
        px *= float(np.exp(rng.normal(0, 0.002)))
        out.append([1700000000 + 60 * i, o, max(o, px) * 1.0005,
                    min(o, px) * 0.9995, px, 1.0])
    return out


@unittest.skipUnless(_RAW, "MaleCNS connectome data not downloaded")
class TestRealWired(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        from flygate.brain.realwired import load_real_mb
        cls.sub = load_real_mb(38)          # builds+ caches on first call
        cls.brain = FlyBrain(BrainConfig(real_wired=True), tag="t")

    def test_wiring_is_real(self):
        sub = self.sub
        # substrate: every KC has real extrinsic input and real MBON edges exist
        self.assertGreater(int((sub["w07"] > 0).sum()), 1000)
        self.assertGreater(int((sub["w11"] > 0).sum()), 1000)
        M = sub["M"]
        self.assertGreater(float(M.sum(axis=1).min()), 0.0)
        # zero weight exactly where no real edge exists
        self.assertTrue(np.array_equal(self.brain.w_a == 0, sub["w07"] == 0))
        self.assertTrue(np.array_equal(self.brain.w_v == 0, sub["w11"] == 0))

    def test_wiring_has_no_rng(self):
        a = FlyBrain(BrainConfig(real_wired=True, seed=1), tag="t1")
        b = FlyBrain(BrainConfig(real_wired=True, seed=2), tag="t2")
        np.testing.assert_array_equal(a.kc_mask, b.kc_mask)
        np.testing.assert_array_equal(a.w_a, b.w_a)

    def test_real_wired_is_frozen(self):
        br = FlyBrain(BrainConfig(real_wired=True), tag="t")
        wa, wv = br.w_a.copy(), br.w_v.copy()
        candles = _candles(70)
        for i in range(60, 70):
            br.observe(candles[:i], equity_delta=1e6, frozen=True)   # huge "reward"
            br.observe(candles[:i], equity_delta=-1e6, frozen=True)  # huge "punish"
        np.testing.assert_array_equal(wa, br.w_a)
        np.testing.assert_array_equal(wv, br.w_v)

    def test_gate_scale_survives_real_weights(self):
        br = FlyBrain(BrainConfig(real_wired=True), tag="t")
        candles = _candles(80, seed=3)
        for i in range(60, 80):
            tel = br.observe(candles[:i], 0.0, frozen=True)
            self.assertGreaterEqual(tel["gate"], 0.0)
            self.assertLessEqual(tel["gate"], 1.0)
            self.assertLessEqual(abs(tel["marg"]), 1.0 + 1e-9)

    def test_backtest_arm_runs(self):
        from flygate.config import Config
        from flygate.backtest import run_backtest, MODES
        self.assertIn("real_wired", MODES)
        cfg = Config.load(None)
        rows = _candles(150, seed=5)
        res = run_backtest(cfg, "BTC_USDT", rows, mode="real_wired",
                           verbose=False, seed=1)
        self.assertIn("return_pct", res)
        # seed must not matter: the wiring is the connectome, not a draw
        res2 = run_backtest(cfg, "BTC_USDT", rows, mode="real_wired",
                            verbose=False, seed=2)
        self.assertEqual(res["return_pct"], res2["return_pct"])

    def test_learning_respects_real_edges(self):
        """Phase D: dopamine moves weights only on real edges, within
        0.1x-2x of each edge's own baseline."""
        br = FlyBrain(BrainConfig(real_wired=True, mbon_to_dn=True,
                                  action_gated_elig=True,
                                  elig_normalize=True), tag="t")
        elig = np.zeros(br.cfg.n_kc)
        elig[br._edge_a] = 1.0
        for delta in (1e6, -1e6, 1e6, -1e6):     # alternating reward/punish
            br._dopamine_update(elig, delta, dt_s=60.0)
        self.assertTrue(np.array_equal(br.w_a == 0, ~br._edge_a))
        self.assertTrue(np.array_equal(br.w_v == 0, ~br._edge_v))
        self.assertTrue(np.all(br.w_a[br._edge_a] <= 2.0 * br._base_a[br._edge_a] + 1e-12))
        self.assertTrue(np.all(br.w_a[br._edge_a] >= 0.1 * br._base_a[br._edge_a] - 1e-12))
        self.assertTrue(np.all(br.w_v[br._edge_v] <= 2.0 * br._base_v[br._edge_v] + 1e-12))

    def test_backtest_arms_registered(self):
        from flygate.backtest import MODES
        for arm in ("real_wired", "frozen_dn", "real_wired_dn",
                    "real_wired_learn", "learn_trip", "real_wired_learn_trip",
                    "real_wired_rule_trip"):
            self.assertIn(arm, MODES)

    def test_luohuang_rule_mechanics(self):
        """P1: the baseline-centered rule (a) cancels exactly under constant
        rates, (b) DEPRESSES edges whose KC was active just before the pulse
        (anti-Hebbian ordering), (c) never touches silent edges, (d) stays
        bounded."""
        from flygate.brain.luohuang import EdgeRule
        r = EdgeRule(200, 2)
        gain = np.vstack([np.full(200, 0.7), np.full(200, 0.3)])
        kc = np.zeros(200); kc[:50] = 100.0
        dan_on = np.array([80.0, 0.0])
        dan_off = np.array([0.0, 0.0])
        # (a) constant rates cancel: drive == 0 in steady state
        for _ in range(60):
            r.advance(kc, dan_on, gain, 0.01, eta=1.0)
        self.assertTrue(np.allclose(r.u, 0.0, atol=1e-6))
        # (b) KC active first, THEN the pulse with KC silent -> depression
        r2 = EdgeRule(200, 2)
        for _ in range(30):
            r2.advance(kc, dan_off, gain, 0.01, eta=1.0)   # build the trace
        for _ in range(20):
            r2.advance(np.zeros(200), dan_on, gain, 0.01, eta=1.0)
        self.assertTrue(np.all(r2.u[:50] < 0.0))            # LTD on the trace
        self.assertTrue(np.allclose(r2.u[50:], 0.0))        # silent: untouched
        # (c) bounds hold even under saturated input
        for _ in range(400):
            r2.advance(kc, dan_on, gain, 0.01, eta=10.0)
        self.assertLessEqual((1 + r2.w).max(), 2.0 + 1e-12)
        self.assertGreaterEqual((1 + r2.w).min(), 0.1 - 1e-12)
        # (d) bins > 10 ms rejected
        with self.assertRaises(ValueError):
            r.advance(kc, dan_on, gain, 0.02, eta=1.0)

    def test_rule_arm_runs_and_is_deterministic(self):
        from flygate.config import Config
        from flygate.backtest import run_backtest
        cfg = Config.load(None)
        rows = _candles(130, seed=9)
        res = run_backtest(cfg, "BTC_USDT", rows, mode="real_wired_rule_trip",
                           verbose=False, seed=1)
        res2 = run_backtest(cfg, "BTC_USDT", rows, mode="real_wired_rule_trip",
                            verbose=False, seed=2)
        self.assertIn("return_pct", res)
        self.assertEqual(res["return_pct"], res2["return_pct"])

    def test_trip_teaching_pairs_entry_patterns(self):
        """P0: dopamine at trip close, driven by the trade's REALIZED P&L."""
        br = FlyBrain(BrainConfig(real_wired=True, trip_teaching=True),
                      tag="t")
        # no pattern in memory during the trip -> nothing to teach
        self.assertIsNone(br.teach_trip(0.0, 100.0, 1e6))
        # a stored pattern + realized profit far above the fee -> reward
        pat = np.zeros(br.cfg.n_kc)
        pat[br._edge_a] = 1.0
        br.memory.append((50.0, pat.astype(np.float32)))
        wa, wv = br.w_a.copy(), br.w_v.copy()
        self.assertEqual(br.teach_trip(0.0, 100.0, 1e6), "reward")
        self.assertFalse(np.array_equal(wa, br.w_a))   # approach edges moved
        self.assertFalse(np.array_equal(wv, br.w_v))
        # a loss that does not even cover its own fee teaches nothing
        br.memory.append((150.0, pat.astype(np.float32)))
        self.assertIsNone(br.teach_trip(100.0, 200.0, 1e-9))
        # patterns outside the trip window are not paired
        br.memory = [(9999.0, pat.astype(np.float32))]
        self.assertIsNone(br.teach_trip(0.0, 100.0, 1e6))
        # and the wiring stays real after learning: non-edges stay 0
        self.assertTrue(np.array_equal(br.w_a == 0, ~br._edge_a))
        self.assertTrue(np.array_equal(br.w_v == 0, ~br._edge_v))


if __name__ == "__main__":
    unittest.main()
