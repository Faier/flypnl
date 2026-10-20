"""Polymarket 腿离线测试：edge 计算、Brier 手算对照、校准分箱。"""
import unittest

from flygate.markets.polymarket import (brier, calibration_report,
                                        price_event, reliability_bins)


class FakeJev:
    """固定概率的假客户端；记录最近一次 state/questions。"""

    def __init__(self, p=0.75):
        self.p = p
        self.state = None
        self.questions = None
        self.calls = 0

    def ask(self, state, questions, use_cache=True):
        self.calls += 1
        self.state, self.questions = state, questions
        return {"p_yes": {"noul": self.p}}


class TestPriceEvent(unittest.TestCase):
    def test_edge_and_signals(self):
        c = FakeJev(p=0.75)
        r = price_event(c, "Fed cuts in December?", "FOMC decision due.",
                        days_to_resolve=45, market_yes_price=0.60)
        self.assertEqual(r["p_model"], 0.75)
        self.assertEqual(r["edge"], 0.15)
        self.assertEqual(r["signal"], "yes")
        self.assertEqual(c.state["days_to_resolve"], 45)  # 相对天数，无绝对日期

    def test_pass_band(self):
        c = FakeJev(p=0.75)
        r = price_event(c, "t", "d", 30, market_yes_price=0.70)
        self.assertEqual(r["signal"], "pass")

    def test_short_signal(self):
        c = FakeJev(p=0.30)
        r = price_event(c, "t", "d", 10, market_yes_price=0.60)
        self.assertEqual(r["signal"], "no")

    def test_probability_range_enforced(self):
        class Bad:
            def ask(self, state, questions, use_cache=True):
                return {"p_yes": {"noul": 1.5}}
        with self.assertRaises(ValueError):
            price_event(Bad(), "t", "d", 5, market_yes_price=0.5)


class TestBrier(unittest.TestCase):
    def test_hand_computed(self):
        # (0.7-1)^2=0.09, (0.7-0)^2=0.49 → 0.58/2 = 0.29
        self.assertAlmostEqual(brier([(0.7, 1), (0.7, 0)]), 0.29, places=9)

    def test_perfect_and_worst(self):
        self.assertAlmostEqual(brier([(1.0, 1), (0.0, 0)]), 0.0, places=9)
        self.assertAlmostEqual(brier([(1.0, 0), (0.0, 1)]), 1.0, places=9)

    def test_empty_raises(self):
        with self.assertRaises(ValueError):
            brier([])


class TestCalibration(unittest.TestCase):
    def test_bins_shapes(self):
        rows = [(0.9, 1), (0.9, 0), (0.1, 0)]
        rep = calibration_report(rows, n=5)
        self.assertEqual(rep["n"], 3)
        self.assertAlmostEqual(rep["brier"], (0.01 + 0.81 + 0.01) / 3, places=9)
        by_bin = {b["bin"]: b for b in rep["bins"]}
        self.assertEqual(by_bin[4]["n"], 2)
        self.assertAlmostEqual(by_bin[4]["outcome_rate"], 0.5, places=9)
        self.assertEqual(by_bin[0]["n"], 1)
        self.assertAlmostEqual(by_bin[0]["outcome_rate"], 0.0, places=9)

    def test_empty_bins_reported_with_zero_n(self):
        rep = calibration_report([(0.5, 1)], n=5)
        by_bin = {b["bin"]: b for b in rep["bins"]}
        self.assertEqual(by_bin[2]["n"], 1)
        self.assertEqual(by_bin[0]["n"], 0)


if __name__ == "__main__":
    unittest.main()
