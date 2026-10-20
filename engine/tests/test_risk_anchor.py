"""Regression: the day anchor must not lock onto a PARTIAL equity total
(2026-09-14 live bug — the futures card lagged startup, the anchor locked
~30U low, so the 8U daily-loss halt would have needed a -38U loss to fire)."""
import os
import tempfile
import unittest

from flygate.config import RiskConfig
from flygate.trader.risk import RiskManager


def mk_risk() -> RiskManager:
    cfg = RiskConfig(daily_loss_halt=8.0, peak_drawdown_halt_pct=20.0)
    return RiskManager(cfg, kill_file=os.path.join(tempfile.mkdtemp(), "KILL"),
                       budget_per_pair=40.0, backtest=False)


class TestDayAnchor(unittest.TestCase):
    def test_no_anchor_while_not_ready(self):
        r = mk_risk()
        r.anchor_ready = False
        self.assertEqual(r.update(40.0, now=1789350000), "")
        self.assertIsNone(r.day_start_equity)      # un-anchored...
        self.assertEqual(r.peak_equity, 40.0)      # ...but peak still tracked
        # a crash while un-ready must neither crash nor mis-halt:
        self.assertEqual(r.update(32.0, now=1789350060), "")

    def test_anchor_locks_on_full_total_only(self):
        r = mk_risk()
        r.anchor_ready = False
        r.update(40.0, now=1789350000)             # spot-only, futures lagging
        r.anchor_ready = True
        r.update(70.4, now=1789350060)             # futures card arrived
        self.assertEqual(r.day_start_equity, 70.4)
        # -8U from the TRUE anchor halts (the old wrong 40U anchor wouldn't)
        self.assertIn("daily loss", r.update(62.0, now=1789350120))

    def test_allow_path_inherits_readiness(self):
        r = mk_risk()
        r.anchor_ready = False
        ok, _ = r.allow("PONS_USDT", "SELL", 10.0, 0.5, 40.0, now=1789350000)
        self.assertTrue(ok)
        self.assertIsNone(r.day_start_equity)      # allow()'s update() didn't anchor


if __name__ == "__main__":
    unittest.main()
