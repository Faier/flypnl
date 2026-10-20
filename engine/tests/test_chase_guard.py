"""Chase-guard regressions (2026-09-18 leg-top trap fix).

The live runner must veto a FRESH long entry when close > chase_pct above
the lowest low of the last chase_n candles, allow it on a normal staircase,
and never veto adds — mirroring the backtest hook that won its 12.6mo A/B
(+188.1/dd17.4 vs +158.5/dd19.0, adds-veto arm lost 35pp).
"""
import os
import tempfile
import time
import unittest

from flygate.logs import Log
from flygate.trader.engine import AppState
from flygate.trader.futures_runner import FuturesRunner

from test_audit_fixes import FakeFutClient, flat_rows, mk_cfg, step_with


def mk_cfg_chase(pct=None, n=20):
    cfg = mk_cfg()
    if pct is not None:
        cfg.futures["chase_pct"] = pct
        cfg.futures["chase_n"] = n
    return cfg


class TestChaseGuard(unittest.TestCase):
    def _runner(self, cfg):
        d = tempfile.mkdtemp()
        r = FuturesRunner(AppState([]), FakeFutClient(), cfg, d, live=False)
        r.log = Log(d)                # _step logs orders; run() normally sets it
        return r

    def test_vetoes_entry_in_vertical_extension(self):
        r = self._runner(mk_cfg_chase(0.06))
        # 20 candles of flat 1000, then the signal candle closes 1080 (+8%
        # over the window low) — a fresh entry must be vetoed
        rows = flat_rows(1000.0, int(time.time()), n=20)
        rows.append([rows[-1][0] + 300, 1070, 1085, 1065, 1080, 1.0])
        r.candles = rows
        step_with(r, "BUY", 1080.0, rows[-1][0])
        self.assertEqual(r.broker.size, 0)

    def test_allows_entry_on_normal_staircase(self):
        r = self._runner(mk_cfg_chase(0.06))
        rows = flat_rows(1000.0, int(time.time()), n=20)
        rows.append([rows[-1][0] + 300, 1002, 1006, 1000, 1004, 1.0])  # +0.4%
        r.candles = rows
        step_with(r, "BUY", 1004.0, rows[-1][0])
        self.assertEqual(r.broker.size, 2)   # entry_step=2

    def test_never_vetoes_adds(self):
        r = self._runner(mk_cfg_chase(0.06))
        rows = flat_rows(1000.0, int(time.time()), n=20)
        rows.append([rows[-1][0] + 300, 1070, 1085, 1065, 1080, 1.0])
        r.candles = rows
        r.broker.size, r.broker.entry = 2, 1080.0   # positioned AT this level
        r.extreme = 1080.0                          # (pnl 0 — no scale-out latch
        step_with(r, "BUY", 1080.0, rows[-1][0])     # eating the cooldown)
        self.assertEqual(r.broker.size, 4)           # +2 add goes through

    def test_off_by_default(self):
        r = self._runner(mk_cfg())                   # no chase_pct in config
        self.assertIsNone(r.chase_pct)
        rows = flat_rows(1000.0, int(time.time()), n=20)
        rows.append([rows[-1][0] + 300, 1070, 1085, 1065, 1080, 1.0])
        r.candles = rows
        step_with(r, "BUY", 1080.0, rows[-1][0])
        self.assertEqual(r.broker.size, 2)           # entry_step=2


if __name__ == "__main__":
    unittest.main()
