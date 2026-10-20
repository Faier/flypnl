"""Volatility-scaled entry sizing (risk.entry_notional, survey #2.4).

The flat 10U into PONS (annual vol >400%) and BTC alike is a sizing bug:
bet less when the coin is wild, more when it is calm, clamped both ways,
never below the exchange minimum, and EXACTLY the legacy flat notional when
the switch is off (the running live fly must not change behavior silently).
"""
import math
import tempfile
import unittest
from unittest import mock

import numpy as np

from flygate.config import Config, RiskConfig
from flygate.data import history as history_mod
from flygate.trader.engine import Engine
from flygate.trader.risk import entry_notional
from test_selfplay import FakeSpotClient, mk_rows, PAIR


def candles_with_vol(px=100.0, daily_vol=0.02, n=120, ts=1_700_000_000):
    """Synthetic 1m closes carrying ~daily_vol: bar vol = daily/sqrt(1440)."""
    rng = np.random.default_rng(int(daily_vol * 1e6))
    bar_vol = daily_vol / math.sqrt(1440.0)
    out, p = [], px
    for i in range(n):
        p *= math.exp(rng.normal(0, bar_vol))
        out.append([ts + i * 60, p, p * 1.0001, p * 0.9999, p, 1.0])
    return out


class TestEntryNotional(unittest.TestCase):
    def cfg(self, **kw):
        rc = RiskConfig(order_notional=10.0, min_notional=5.0)
        for k, v in kw.items():
            setattr(rc, k, v)
        return rc

    def test_off_is_exactly_legacy_flat(self):
        wild = candles_with_vol(daily_vol=0.15)
        calm = candles_with_vol(daily_vol=0.001)
        for rows in (wild, calm, None, []):
            self.assertEqual(
                entry_notional(rows, self.cfg(vol_target_sizing=False), 10.0, 60.0),
                10.0)

    def test_wild_coin_bets_less(self):
        wild = candles_with_vol(daily_vol=0.10)
        v = entry_notional(wild, self.cfg(vol_target_sizing=True), 10.0, 60.0)
        self.assertLess(v, 10.0)
        self.assertGreaterEqual(v, 10.0 * 0.25)      # clamp floor

    def test_calm_coin_bets_more_and_clamps(self):
        calm = candles_with_vol(daily_vol=0.001)
        v = entry_notional(calm, self.cfg(vol_target_sizing=True), 10.0, 60.0)
        self.assertGreater(v, 10.0)
        self.assertLessEqual(v, 10.0 * 2.0)          # clamp ceiling

    def test_never_below_exchange_minimum(self):
        wild = candles_with_vol(daily_vol=0.50)
        v = entry_notional(wild, self.cfg(vol_target_sizing=True), 10.0, 60.0)
        self.assertGreaterEqual(v, 5.0)

    def test_insufficient_data_falls_back_to_base(self):
        self.assertEqual(
            entry_notional(mk_rows(n=30), self.cfg(vol_target_sizing=True),
                           10.0, 60.0), 10.0)

    def test_engine_uses_scaled_notional(self):
        cfg = Config()
        cfg.pairs = [PAIR]
        cfg.futures = {}
        cfg.risk = self.cfg(vol_target_sizing=True)
        wild = candles_with_vol(daily_vol=0.10)
        client = FakeSpotClient(wild)
        eng = Engine(cfg, live=False, client=client,
                     runs_dir=tempfile.mkdtemp())
        for b in eng.brains.values():
            b.observe = lambda rows, delta, **kw: {"decision": "BUY",
                                                   "r2": 0.9, "diff_hz": 3.0,
                                                   "gate": 0.9}
        eng.candles[PAIR] = wild
        with mock.patch.object(history_mod, "append_cache"):
            eng._observe(PAIR)
        b = eng.brokers[PAIR]
        self.assertGreater(b.pos.base, 0.0)
        spent = cfg.quote_budget - b.cash
        self.assertLess(spent, cfg.risk.order_notional)   # scaled DOWN
        self.assertGreaterEqual(spent, cfg.risk.min_notional)


if __name__ == "__main__":
    unittest.main()
