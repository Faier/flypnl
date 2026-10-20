"""极限研究工具的测试：block 归因、多巴胺幅度编码、非对称惩罚死区、
边界饱和度、反事实对账（scripts/cf_report.py 的联接逻辑）。

需要连接组数据的部分沿用 test_realwired 的跳过约定。
"""
from __future__ import annotations

import glob
import importlib.util
import json
import os
import tempfile
import unittest

import numpy as np

from flygate.config import BrainConfig
from flygate.brain.flybrain import FlyBrain

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_RAW = (glob.glob(os.path.join(_REPO, "data/connectome/body-annotations-*.feather"))
        and glob.glob(os.path.join(_REPO, "data/connectome/connectome-weights-*.feather")))


def _ramp(n: int = 60, up: bool = True, drift: float = 0.004) -> list:
    """Deterministic monotonic chart: a perfect ramp pins tilt/r2 at 1."""
    out, px = [], 100.0
    for i in range(n):
        o = px
        px *= float(np.exp(drift if up else -drift))
        out.append([1700000000 + 60 * i, o, max(o, px) * 1.0005,
                    min(o, px) * 0.9995, px, 1.0])
    return out


class TestBlockAttribution(unittest.TestCase):
    """The DN fired but a gate said no — observe() must name WHICH gate."""

    def _brain(self, **kw) -> FlyBrain:
        cfg = BrainConfig(dn_diff_hz=-1e9, r2_gate=kw.pop("r2_gate", 0.0), **kw)
        return FlyBrain(cfg, tag="blk")

    def test_clean_fire_has_no_block(self):
        br = self._brain()
        tel = br.observe(_ramp(), 0.0)
        self.assertEqual(tel["decision"], "BUY")
        self.assertIsNone(tel["block"])

    def test_r2_gate_veto_is_named(self):
        # r2_gate = 1.1 is unreachable -> the coherence gate must be the block
        br = self._brain(r2_gate=1.1)
        tel = br.observe(_ramp(), 0.0)
        self.assertEqual(tel["decision"], "HOLD")
        self.assertEqual(tel["block"], "r2")

    def test_gate_veto_is_named(self):
        # synthetic denominators are the same constant (kc_active), so scaling
        # w_a down while leaving w_v alone makes a_ev << v_ev -> g collapses
        br = self._brain()
        br.w_a[:] = 0.001
        tel = br.observe(_ramp(), 0.0)
        self.assertEqual(tel["decision"], "HOLD")
        self.assertEqual(tel["block"], "gate")

    def test_frozen_legacy_arm_never_blocks(self):
        # the LEGACY frozen arm (frozen_keeps_gate=False) also bypassed the
        # gates — that arm must never report a block
        br = self._brain(r2_gate=1.1, frozen_keeps_gate=False)
        tel = br.observe(_ramp(), 0.0, frozen=True)
        self.assertEqual(tel["decision"], "BUY")
        self.assertIsNone(tel["block"])


@unittest.skipUnless(globals()["_RAW"], "MaleCNS connectome data not downloaded")
class TestDopamineScaling(unittest.TestCase):
    """Magnitude coding + asymmetric punish deadband on the real substrate."""

    def _brain(self, **kw) -> FlyBrain:
        base = dict(real_wired=True, rule="huang2024", eta_rule=0.1)
        base.update(kw)
        return FlyBrain(BrainConfig(**base), tag="da")

    # teach_trip defaults (equity=0, fee_roundtrip=0) leave the bare deadband
    THR = BrainConfig().dopamine_threshold()

    def test_punish_deadband_asymmetric(self):
        br = self._brain(punish_deadband_mult=2.0)
        self.assertEqual(br._dopamine_label(-1.5 * self.THR), None)
        self.assertEqual(br._dopamine_label(-2.5 * self.THR), "punish")
        # reward side keeps the single threshold
        self.assertEqual(br._dopamine_label(+1.5 * self.THR), "reward")

    def test_punish_deadband_symmetric_default(self):
        br = self._brain()
        self.assertEqual(br._dopamine_label(-1.5 * self.THR), "punish")

    def test_pulse_carries_magnitude(self):
        br = self._brain(dan_magnitude_coding=True)
        br.teach_trip(0, 1, realized=3 * self.THR)
        self.assertEqual(br._pending_pulse, ("reward", 3.0))
        br2 = self._brain(dan_magnitude_coding=True)
        br2.teach_trip(0, 1, realized=50 * self.THR)
        self.assertEqual(br2._pending_pulse[1], 4.0)      # dan_mag_cap
        br3 = self._brain()                               # flag off -> binary
        br3.teach_trip(0, 1, realized=50 * self.THR)
        self.assertEqual(br3._pending_pulse, ("reward", 1.0))

    def test_small_outcome_stays_silent(self):
        br = self._brain(dan_magnitude_coding=True)
        label = br.teach_trip(0, 1, realized=0.5 * self.THR)
        self.assertIsNone(label)
        self.assertIsNone(br._pending_pulse)

    def test_delivered_rate_scales(self):
        """A 4x pulse must land ~4x the DAN rate on the substrate."""
        peaks = []
        for scale in (1.0, 4.0):
            br = self._brain()
            br._pending_pulse = ("reward", scale)
            br.observe(_ramp(), 0.0)          # consumes the pulse
            peaks.append(float(br.rule.y_dan.max()))
        self.assertGreater(peaks[0], 0.0)     # the pulse really landed
        self.assertAlmostEqual(peaks[1] / peaks[0], 4.0, delta=0.5)

    def test_edge_saturation_gauge(self):
        br = self._brain()
        self.assertEqual(br.edge_saturation(), 0.0)       # fresh: nothing moved
        br.rule.u[:100] = 0.9                             # pinned high
        br.rule.u[100:200] = -0.85                        # pinned low
        br.rule.u[200:300] = 0.3                          # active, not pinned
        self.assertAlmostEqual(br.edge_saturation(), 200 / 300, places=6)
        br2 = FlyBrain(BrainConfig(), tag="syn")          # synthetic: no rule
        self.assertEqual(br2.edge_saturation(), 0.0)


class TestCfReport(unittest.TestCase):
    """The veto-pricing join (scripts/cf_report.py)."""

    @classmethod
    def setUpClass(cls):
        spec = importlib.util.spec_from_file_location(
            "cf_report", os.path.join(_REPO, "scripts", "cf_report.py"))
        cls.cf = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.cf)

    def test_roundtrip_reconstruction(self):
        with tempfile.TemporaryDirectory() as td:
            p = os.path.join(td, "b.json")
            with open(p, "w") as f:
                json.dump({"cash": 0, "trades": [
                    {"ts": 100, "side": "BUY", "price": 100.0, "base": 1.0,
                     "quote": 100.0, "fee": 0.2},
                    {"ts": 200, "side": "SELL", "price": 101.0, "base": 1.0,
                     "quote": 101.0, "fee": 0.2}]}, f)
            rt = self.cf.roundtrips(p)
            self.assertEqual(len(rt), 1)
            self.assertAlmostEqual(rt[0][3], 101.0 - 0.2 - 100.0 - 0.2, places=9)

    def test_match_and_pricing(self):
        events = [
            {"ts": 1000, "pair": "SOL_USDT", "block": "gate", "side": "BUY"},
            {"ts": 2000, "pair": "BTC_USDT", "block": "r2", "side": "BUY"},
        ]
        rts = {"SOL_USDT": [(1065, 1300, "BUY", +1.0)],     # inside the window
               "BTC_USDT": [(5000, 5300, "BUY", -2.0)]}     # outside -> no match
        text = self.cf.report(events, rts, interval_s=60)
        self.assertIn("gate", text)
        self.assertIn("净亏钱", text)      # veto suppressed a WINNER (pnl +1)
        self.assertIn("events: 2", text)
        self.assertIn("no twin trade matched: 1", text)   # the BTC event
        self.assertIn("+1.0000", text)

    def test_torn_line_ignored(self):
        with tempfile.TemporaryDirectory() as td:
            p = os.path.join(td, "cf.jsonl")
            with open(p, "w") as f:
                f.write(json.dumps({"ts": 1, "pair": "X_USDT", "block": "gate",
                                    "side": "BUY"}) + "\n")
                f.write('{"ts": 2, "pair": "X_USDT", "blo')   # torn write
            ev = self.cf.load_events(p)
            self.assertEqual(len(ev), 1)


if __name__ == "__main__":
    unittest.main()
