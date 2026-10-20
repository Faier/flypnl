"""Negative fixture for lookahead bias (the freqtrade lookahead-analysis idea,
adapted to flygate's own pipeline).

The invariant: the fly's trajectory over a prefix of candles must not depend
on candles it has not seen yet. A renderer or metric that accidentally touches
the future (e.g. normalizing over the whole dataset) produces verdicts that
NEVER change when the future changes — the same bug class as "every seed gives
bit-identical results". Here the future is sabotaged (a 50% crash appended
after the comparison point) and the equity curve up to that point must be
bit-identical, while a forced real position makes the post-crash curve differ,
so the test cannot pass vacuously.
"""
import unittest
from unittest import mock

import numpy as np

from flygate.backtest import run_backtest
from flygate.brain.flybrain import BUY, FlyBrain
from flygate.config import Config

PAIR = "BTC_USDT"


def mk_rows(n=240, ts=1_700_000_000):
    """A mild staircase (entries plausible) — 1m candles."""
    rows, px = [], 100.0
    for i in range(n):
        o = px
        px = round(px * (1 + (0.0006 if i < 120 else 0.0)), 6)
        c = px
        hi, lo = max(o, c) * 1.0005, min(o, c) * 0.9995
        rows.append([ts + i * 60, o, hi, lo, c, 10.0])
    return rows


class TestNoLookahead(unittest.TestCase):
    def test_prefix_invariance_under_future_sabotage(self):
        cfg = Config()
        cfg.pairs = [PAIR]
        cfg.futures = {}
        rows = mk_rows()
        crash_ts = rows[180][0]                    # the future starts here
        rows_alt = [list(r) for r in rows]
        for r in rows_alt[180:]:                   # 50% crash in the future
            r[1] *= 0.5; r[2] *= 0.5; r[3] *= 0.5; r[4] *= 0.5

        buy_ts = rows[100][0]
        orig = FlyBrain.observe

        def forced(self, rws, delta, **kw):
            tel = orig(self, rws, delta, **kw)
            if rws[-1][0] == buy_ts:          # force exactly one real entry
                tel = dict(tel)
                tel["decision"] = BUY
            return tel

        with mock.patch.object(FlyBrain, "observe", forced):
            res1 = run_backtest(cfg, PAIR, rows, mode="normal", verbose=False)
            res2 = run_backtest(cfg, PAIR, rows_alt, mode="normal", verbose=False)

        # teeth: a position was actually opened, and the crash changed the
        # outcome afterwards — the comparison below is not trivially equal
        self.assertGreaterEqual(res1["trades"], 1)
        eq1 = res1["equity_curve"]
        eq2 = res2["equity_curve"]
        self.assertNotEqual([e[1] for e in eq1], [e[1] for e in eq2])

        # the invariant: identical trajectory strictly BEFORE the sabotage
        # point. Candle 180 itself is tampered data, so the observation made
        # from it is legitimately different — only the untouched past must
        # reproduce bit-for-bit.
        pre1 = [e for e in eq1 if e[0] < crash_ts]
        pre2 = [e for e in eq2 if e[0] < crash_ts]
        self.assertEqual(pre1, pre2)


if __name__ == "__main__":
    unittest.main()
