"""carry_paper P1 优化（实盘第一个月）的回归测试。

  4) 跨所比价：pick_best_venue 同币多所只留持续性均值最高者（瞬时尖峰所
     过得了瞬时门但均值低 → 淘汰并留档）
  5) 基差/保证金：basis_pct 符号与量级；fut_short_loss_frac 只对上涨侧
     报警；settle_and_mark 首次触及 +25% 置位 margin_alerted（一次即够）
  6) 候选分散：base_in_use 跨所排重；LEG_USD 对齐 40U pilot、
     MAX_POSITIONS/MAX_BASES = 2
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))), "strategies"))

from carry_paper import (LEG_USD, MARGIN_WARN, MAX_BASES, MAX_POSITIONS,
                         base_in_use, basis_pct, fut_short_loss_frac,
                         pick_best_venue, settle_and_mark)


def _row(base, venue, apr):
    return {"base": base, "contract": f"{base}_USDT", "exchange": venue,
            "interval_h": 4.0, "funding_ann": apr}


class TestPickBestVenue(unittest.TestCase):
    def test_same_base_keeps_highest_persistent_mean(self):
        cands = {
            "gate:PONS_USDT": ("gate", _row("PONS", "gate", 0.60), {"mean": 0.50}),
            "binance:PONSUSDT": ("binance", _row("PONS", "binance", 0.90), {"mean": 0.30}),
        }
        kept, drops = pick_best_venue(cands)
        self.assertEqual(list(kept), ["gate:PONS_USDT"])   # 均值赢,不是瞬时值
        self.assertEqual(len(drops), 1)
        self.assertEqual(drops[0]["dropped"], "binance:PONSUSDT")
        self.assertGreater(drops[0]["mean_kept"], drops[0]["mean_dropped"])

    def test_different_bases_all_kept(self):
        cands = {
            "gate:PONS_USDT": ("gate", _row("PONS", "gate", 0.6), {"mean": 0.5}),
            "gate:DOGE_USDT": ("gate", _row("DOGE", "gate", 0.4), {"mean": 0.4}),
        }
        kept, drops = pick_best_venue(cands)
        self.assertEqual(len(kept), 2)
        self.assertEqual(drops, [])


class TestBasisAndMargin(unittest.TestCase):
    def test_basis_sign_and_scale(self):
        self.assertAlmostEqual(basis_pct(100.0, 101.0), 0.01)
        self.assertAlmostEqual(basis_pct(100.0, 99.0), -0.01)
        self.assertEqual(basis_pct(0.0, 101.0), 0.0)

    def test_short_loss_only_on_adverse_side(self):
        self.assertAlmostEqual(fut_short_loss_frac(100.0, 125.0), 0.25)
        self.assertEqual(fut_short_loss_frac(100.0, 80.0), 0.0)   # 浮盈不报警
        self.assertEqual(fut_short_loss_frac(0.0, 100.0), 0.0)

    def test_settle_marks_alert_once_at_25pct(self):
        pos = {"interval_h": 4.0, "last_settle": 0, "funding_received": 0.0,
               "settlements": 0, "neg_flips": 0, "trailing": [0.5],
               "fut_entry": 100.0, "spot_entry": 100.0, "notional": 40.0,
               "max_px_rise": 1.0, "opened": 0}
        # 价格 +26% → 触发报警标志
        pos = settle_and_mark(pos, {"funding_rate": 0.001, "mark_price": 126.0},
                              {"last": 126.0}, now=60)
        self.assertTrue(pos["margin_alerted"])
        self.assertIn("_new_margin_alert", pos)
        pos.pop("_new_margin_alert")
        # 下一轮仍在 +26% → 不再重复报警
        pos = settle_and_mark(pos, {"funding_rate": 0.001, "mark_price": 126.0},
                              {"last": 126.0}, now=120)
        self.assertNotIn("_new_margin_alert", pos)
        # 基差被逐轮记录
        self.assertAlmostEqual(pos["basis_now"], 0.0, places=5)


class TestDiversification(unittest.TestCase):
    def test_base_in_use_spans_venues_and_pending(self):
        held = {"gate:PONS_USDT": {"base": "PONS"}}
        pending = {"binance:ZECUSDT": {"base": "ZEC"}}
        self.assertTrue(base_in_use(held, pending, "PONS"))
        self.assertTrue(base_in_use(held, pending, "ZEC"))
        self.assertFalse(base_in_use(held, pending, "DOGE"))
        self.assertTrue(base_in_use({}, {"gate:X_USDT": {"base": "X"}}, "X"))

    def test_pilot_sizing_aligned(self):
        self.assertEqual(LEG_USD, 40.0)     # 40U 现货腿,对齐实盘 pilot
        self.assertEqual(MAX_POSITIONS, 2)
        self.assertEqual(MAX_BASES, 2)


if __name__ == "__main__":
    unittest.main()
