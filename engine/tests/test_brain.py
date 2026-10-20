"""Brain smoke tests on synthetic data (no network)."""
import unittest

import numpy as np

from flygate.config import BrainConfig
from flygate.brain.flybrain import (FlyBrain, render_chart, chart_tilt,
                                    BUY, SELL, HOLD)


def synth(n=200, drift=0.0, seed=1, start=100.0, vol=0.15):
    rng = np.random.default_rng(seed)
    px = start
    out = []
    for i in range(n):
        o = px
        px = max(px * (1 + drift + rng.normal(0, vol / 100)), 1e-6)
        c = px
        hi = max(o, c) * (1 + abs(rng.normal(0, vol / 200)))
        lo = min(o, c) * (1 - abs(rng.normal(0, vol / 200)))
        out.append([1600000000 + i * 60, o, hi, lo, c, 10.0])
    return out


class TestChart(unittest.TestCase):
    def test_render_shapes_and_range(self):
        cds = synth(80)
        lum, col = render_chart(cds, 48, 27, 48)
        self.assertEqual(lum.shape, (27, 48))
        self.assertGreater(lum.sum(), 50)          # something is drawn
        self.assertTrue(((col >= -1) & (col <= 1)).all())

    def test_tilt_flat_is_zero(self):
        cds = [[t, 100, 100.1, 99.9, 100, 1] for t in range(80)]
        lum, _ = render_chart(cds, 48, 27, 48)
        slope, r2, _ = chart_tilt(lum)
        self.assertLess(abs(slope), 0.05)

    def test_tilt_detects_direction(self):
        up = synth(80, drift=0.004, vol=0.03)
        dn = synth(80, drift=-0.004, vol=0.03)
        s_up, r2u, _ = chart_tilt(render_chart(up, 48, 27, 48)[0])
        s_dn, r2d, _ = chart_tilt(render_chart(dn, 48, 27, 48)[0])
        self.assertLess(s_up, -0.3, f"rising chart should tilt negative: {s_up}")
        self.assertGreater(s_dn, 0.3, f"falling chart should tilt positive: {s_dn}")
        self.assertGreater(r2u, 0.7)
        self.assertGreater(r2d, 0.7)


class TestBrain(unittest.TestCase):
    def test_uptrend_buys(self):
        brain = FlyBrain(BrainConfig(), tag="t")
        cds = synth(200, drift=0.004, vol=0.05)     # steady uptrend
        decisions = []
        for i in range(70, 200):
            tel = brain.observe(cds[:i], equity_delta=0.0)
            decisions.append(tel["decision"])
        self.assertIn(BUY, decisions, msg=f"no BUY on uptrend: {set(decisions)}")

    def test_downtrend_sells(self):
        brain = FlyBrain(BrainConfig(), tag="t")
        cds = synth(200, drift=-0.004, vol=0.05)
        decisions = [brain.observe(cds[:i], 0.0)["decision"] for i in range(70, 200)]
        self.assertIn(SELL, decisions, msg=f"no SELL on downtrend: {set(decisions)}")

    def test_flat_holds(self):
        # mean-reverting (OU) sideways market: no coherent trend to follow
        rng = np.random.default_rng(3)
        px, cds = 100.0, []
        for i in range(220):
            o = px
            px = 100.0 + 0.96 * (px - 100.0) + rng.normal(0, 0.08)
            c = px
            hi = max(o, c) + abs(rng.normal(0, 0.03))
            lo = min(o, c) - abs(rng.normal(0, 0.03))
            cds.append([1600000000 + i * 60, o, hi, lo, c, 10.0])
        brain = FlyBrain(BrainConfig(), tag="t")
        decisions = [brain.observe(cds[:i], 0.0)["decision"] for i in range(70, 220)]
        acts = decisions.count(BUY) + decisions.count(SELL)
        self.assertLess(acts, 25, msg=f"too twitchy on sideways data: {acts} trades")

    def test_dopamine_shifts_gate(self):
        # repeated reward after the same pattern should raise the approach gate
        brain = FlyBrain(BrainConfig(), tag="t")
        cds = synth(200, drift=0.001, vol=0.05)
        for i in range(70, 150):
            brain.observe(cds[:i], equity_delta=0.5)   # constant reward
        g_after_reward = brain.last_g
        brain2 = FlyBrain(BrainConfig(), tag="t2")
        for i in range(70, 150):
            brain2.observe(cds[:i], equity_delta=-0.5)  # constant punishment
        self.assertGreater(g_after_reward, brain2.last_g)

    def test_state_roundtrip(self):
        import tempfile, os
        brain = FlyBrain(BrainConfig(), tag="t")
        cds = synth(100, drift=0.002)
        for i in range(70, 90):
            brain.observe(cds[:i], 0.1)
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "b.npz")
            brain.save(p)
            b2 = FlyBrain(BrainConfig(), tag="t")
            self.assertTrue(b2.load(p))
            self.assertEqual(b2.obs_count, brain.obs_count)
            np.testing.assert_allclose(b2.w_a, brain.w_a)


if __name__ == "__main__":
    unittest.main()
