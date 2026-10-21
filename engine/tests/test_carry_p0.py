"""carry_paper P0 优化（实盘前三项）的回归测试。

纯函数全部离线可测；网络路径（probe/venue_marks）不在单测范围。
  1) 净盈利门：round_trip_cost / break_even_days —— 15% APR + Gate 全成本
     ≈ 11.6 天 > 7 天门 → 必须拒绝；56% APR ≈ 3.1 天 → 过门
  2) maker 两段式：maker_limits 挂价在价差内侧 30%；maker_crossed 要求
     两腿 mid 同时穿越（单腿不算）
  3) 结算网格对齐：next_settle_ts 锚 UTC 网格；open_position 的
     last_settle 必须是网格点（修复旧 bug：进场后第一次真实结算被漏记）
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))), "strategies"))

from carry_paper import (BE_MAX_DAYS, LEG_USD, VENUES, break_even_days,
                         maker_crossed, maker_limits, next_settle_ts,
                         open_position, round_trip_cost)


class TestNetProfitGate(unittest.TestCase):
    def test_gate_round_trip_cost_is_fees_plus_bridge(self):
        # 开+平 = (0.2% spot + 0.05% fut) × 2 + 过桥费
        self.assertAlmostEqual(round_trip_cost("gate", 0.003), 0.008)
        self.assertAlmostEqual(round_trip_cost("gate", 0.0), 0.005)

    def test_maker_mode_charges_bridge_zero_not_negative_fees(self):
        # 保守模型：maker 只省过桥费，手续费照收（不虚标期货 maker 折扣）
        self.assertAlmostEqual(round_trip_cost("gate", 0.0, maker=True), 0.005)

    def test_15pct_apr_fails_the_7day_gate_on_gate(self):
        be = break_even_days(0.15, round_trip_cost("gate", 0.003))
        self.assertGreater(be, BE_MAX_DAYS)

    def test_56pct_apr_passes(self):
        be = break_even_days(0.56, round_trip_cost("gate", 0.003))
        self.assertLess(be, BE_MAX_DAYS)
        self.assertAlmostEqual(be, 0.008 / (0.56 / 365), places=6)

    def test_zero_apr_never_breaks_even(self):
        self.assertEqual(break_even_days(0.0, 0.001), float("inf"))


class TestMakerTwoPhase(unittest.TestCase):
    def setUp(self):
        self.s_book = {"asks": [["101", "10"]], "bids": [["99", "10"]]}
        self.f_book = {"asks": [["101", "10"]], "bids": [["99", "10"]]}

    def test_limits_sit_inside_the_spread(self):
        s_lim, f_lim = maker_limits(self.s_book, self.f_book)
        self.assertAlmostEqual(s_lim, 99 + 2 * 0.3)     # bid + 30% of spread
        self.assertAlmostEqual(f_lim, 101 - 2 * 0.3)    # ask - 30% of spread

    def test_cross_requires_both_legs(self):
        s_lim, f_lim = maker_limits(self.s_book, self.f_book)
        ok = maker_crossed(99.5, 100.5, s_lim, f_lim)   # both through
        half = maker_crossed(99.5, 99.0, s_lim, f_lim)  # spot only, fut below
        self.assertTrue(ok)
        self.assertFalse(half)

    def test_missing_marks_never_fill(self):
        s_lim, f_lim = maker_limits(self.s_book, self.f_book)
        self.assertFalse(maker_crossed(0.0, 100.5, s_lim, f_lim))


class TestSettlementGrid(unittest.TestCase):
    def test_next_settle_aligns_to_utc_grid(self):
        interval = 4 * 3600
        self.assertEqual(next_settle_ts(0, interval), interval)
        self.assertEqual(next_settle_ts(interval - 1, interval), interval)
        self.assertEqual(next_settle_ts(interval, interval), 2 * interval)

    def test_open_position_anchors_to_grid_not_open_time(self):
        interval_h = 4.0
        interval_s = int(interval_h * 3600)
        now = 1000 * interval_s + 300        # 结算后 5 分钟进场
        r = {"base": "PONS", "contract": "PONS_USDT", "interval_h": interval_h,
             "funding_ann": 0.56}
        probe = {"spot_px": 1.0, "fut_px": 1.0, "spot_mid": 1.0, "fut_mid": 1.0,
                 "entry_cost": 0.003, "spot_limit": 0.99, "fut_limit": 1.01}
        pos = open_position("gate", r, {}, now, probe, mode="taker")
        # 旧 bug: last_settle = now + interval → 进场后第一次真实结算被跳过
        self.assertEqual(pos["last_settle"], 1001 * interval_s)
        self.assertEqual(pos["entry_mode"], "taker")
        self.assertAlmostEqual(pos["notional"], LEG_USD)

    def test_open_position_maker_mode_fills_at_limit_no_bridge(self):
        r = {"base": "X", "contract": "X_USDT", "interval_h": 8.0,
             "funding_ann": 0.4}
        probe = {"spot_limit": 9.9, "fut_limit": 10.1}
        pos = open_position("gate", r, {}, 1000, probe, mode="maker")
        self.assertEqual(pos["spot_entry"], 9.9)
        self.assertEqual(pos["fut_entry"], 10.1)
        self.assertEqual(pos["entry_cost"], 0.0)
        self.assertEqual(pos["entry_mode"], "maker")


if __name__ == "__main__":
    unittest.main()
