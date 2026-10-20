"""P2 self-play wiring: trip teaching inside the live/paper engine, plus the
frozen shadow twin.

The backtest taught the brain at trip close (P0/P1); the engine never did —
a paper/live fly could play for a week and learn nothing. These tests pin:

  1. a BUY fill from flat opens a trip with basis = cash BEFORE the entry
     (backtest parity); an ADD mid-trip must not reset basis or entry time
  2. any full close (brain sell, reflex, halt flatten) teaches the brain from
     the trip's realized P&L and clears the persisted basis
  3. the trip basis survives a restart (pos.json round trip)
  4. the shadow twin trades its own paper account on the same candles, and
     its weights NEVER move (frozen guarantee) while the learning fly's do
  5. the shadow twin is refused wherever real keys are in play
"""
import os
import tempfile
import unittest
from unittest import mock

import numpy as np

from flygate.config import BrainConfig, Config
from flygate.data import history as history_mod
from flygate.trader.engine import Engine
from flygate.brain.flybrain import FlyBrain

PAIR = "BTC_USDT"


class FakeSpotClient:
    """Offline spot 'exchange': canned klines, no private endpoints."""
    key = ""
    secret = ""

    def __init__(self, rows):
        self.rows = rows

    def klines(self, pair, interval, _from=0, _to=0):
        return self.rows

    def ticker(self, pair):
        return {"last": str(self.rows[-1][4])}


def mk_rows(px=100.0, n=60, ts=1_700_000_000):
    return [[ts + i * 60, px, px, px, px, 1.0] for i in range(n)]


def mk_cfg(brain=None):
    cfg = Config()
    cfg.pairs = [PAIR]
    cfg.futures = {}
    if brain is not None:
        cfg.brain = brain
    return cfg


def pin_main(eng, decision):
    """Pin the learning fly's decisions (engine-wiring tests isolate the
    trip/teach plumbing from the brain's own readout)."""
    for b in eng.brains.values():
        b.observe = lambda rows, delta, **kw: {"decision": decision,
                                               "r2": 0.9, "diff_hz": 3.0,
                                               "gate": 0.9}


def observe(eng, rows):
    with mock.patch.object(history_mod, "append_cache"):
        eng._observe(PAIR)
    return eng


class TestTripTeaching(unittest.TestCase):
    def _engine(self, brain=None, shadow=False):
        self.d = tempfile.mkdtemp()
        eng = Engine(mk_cfg(brain), live=False, client=FakeSpotClient(mk_rows()),
                     runs_dir=self.d, shadow=shadow)
        eng.last_candle_ts[PAIR] = 0
        return eng

    def test_buy_from_flat_opens_trip_with_pre_entry_basis(self):
        eng = self._engine()
        rows = mk_rows()
        pin_main(eng, "BUY")
        observe(eng, rows)
        b = eng.brokers[PAIR]
        self.assertGreater(b.pos.base, 0.0)
        trip = eng._trips[PAIR]
        self.assertAlmostEqual(trip["cash0"], b.cash + b.pos.cost, places=6)
        self.assertEqual(trip["entry_ts"], rows[-1][0])

    def test_add_does_not_reset_basis(self):
        eng = self._engine()
        pin_main(eng, "BUY")
        observe(eng, mk_rows())
        trip0 = dict(eng._trips[PAIR])
        observe(eng, mk_rows(ts=1_700_000_000 + 3600))   # second BUY = ADD
        self.assertEqual(eng._trips[PAIR]["cash0"], trip0["cash0"])
        self.assertEqual(eng._trips[PAIR]["entry_ts"], trip0["entry_ts"])

    def test_sell_closes_trip_and_teaches_realized(self):
        eng = self._engine(BrainConfig(trip_teaching=True))
        pin_main(eng, "BUY")
        observe(eng, mk_rows())
        calls = []
        eng.brains[PAIR].teach_trip = (
            lambda t0, t1, realized, fee_roundtrip=0.0:
            calls.append((t0, t1, realized, fee_roundtrip)) or "punish")
        cash0 = eng._trips[PAIR]["cash0"]
        pin_main(eng, "SELL")
        observe(eng, mk_rows(ts=1_700_000_000 + 600))
        self.assertEqual(len(calls), 1)
        t0, t1, realized, fee_rt = calls[0]
        self.assertEqual(t0, mk_rows()[-1][0])            # entry candle ts
        self.assertAlmostEqual(realized, eng.brokers[PAIR].cash - cash0, places=6)
        self.assertLess(realized, 0.0)                    # fees make it a loss
        self.assertAlmostEqual(fee_rt, eng._fee_roundtrip, places=9)
        self.assertNotIn(PAIR, eng._trips)                # basis cleared
        # the dopamine event reaches the dashboard feed
        self.assertTrue(any(e.get("dopamine") == "punish" for e in
                            eng.state.events))

    def test_real_brain_learns_from_a_completed_loss(self):
        eng = self._engine(BrainConfig(trip_teaching=True))
        brain = eng.brains[PAIR]
        # a pattern "on screen" during the holding period
        pat = np.zeros(brain.cfg.n_kc, dtype=np.float32)
        pat[:10] = 1.0
        brain.memory.append((int(mk_rows()[-1][0]), pat))
        w_a0, w_v0 = brain.w_a.copy(), brain.w_v.copy()
        pin_main(eng, "BUY")
        observe(eng, mk_rows())
        pin_main(eng, "SELL")
        observe(eng, mk_rows(ts=1_700_000_000 + 600))
        changed = (not np.array_equal(brain.w_a, w_a0)
                   or not np.array_equal(brain.w_v, w_v0))
        self.assertTrue(changed, "a punished trip must move the synapses")
        self.assertGreaterEqual(float(brain.w_a.min()), brain.cfg.w_min - 1e-9)
        self.assertLessEqual(float(brain.w_a.max()), brain.cfg.w_max + 1e-9)

    def test_trip_basis_survives_restart(self):
        eng = self._engine()
        pin_main(eng, "BUY")
        observe(eng, mk_rows())
        trip0 = dict(eng._trips[PAIR])
        eng.stop()                       # release the single-instance lock
        eng2 = Engine(mk_cfg(), live=False, client=FakeSpotClient(mk_rows()),
                      runs_dir=self.d)
        self.assertIn(PAIR, eng2._trips)
        self.assertEqual(eng2._trips[PAIR]["cash0"], trip0["cash0"])
        self.assertEqual(eng2._trips[PAIR]["entry_ts"], trip0["entry_ts"])
        eng2.stop()

    def test_trip_teaching_off_leaves_weights_untouched(self):
        eng = self._engine(BrainConfig(trip_teaching=False))
        brain = eng.brains[PAIR]
        w_a0 = brain.w_a.copy()
        pin_main(eng, "BUY")
        observe(eng, mk_rows())
        pin_main(eng, "SELL")
        observe(eng, mk_rows(ts=1_700_000_000 + 600))
        self.assertTrue(np.array_equal(brain.w_a, w_a0))

    def test_learning_panel_telemetry(self):
        eng = self._engine(BrainConfig(trip_teaching=True), shadow=True)
        brain = eng.brains[PAIR]
        # a pattern "on screen" during the holding period (observe is stubbed,
        # so seed the memory the way a real observation would)
        pat = np.zeros(brain.cfg.n_kc, dtype=np.float32)
        pat[:10] = 1.0
        brain.memory.append((int(mk_rows()[-1][0]), pat))
        pin_main(eng, "BUY")
        observe(eng, mk_rows())
        eng.candles[PAIR] = mk_rows()
        with mock.patch.object(history_mod, "append_cache"):
            eng._shadow_observe(PAIR)          # pushes one gap point
        pin_main(eng, "SELL")
        observe(eng, mk_rows(ts=1_700_000_000 + 600))
        ln = eng.state.learning
        self.assertEqual(ln["da_total"]["punish"], 1)
        self.assertEqual(ln["da_today"]["punish"], 1)
        self.assertEqual(ln["trips_today"], 1)
        self.assertGreaterEqual(len(ln["gap_curve"]), 1)
        self.assertGreaterEqual(len(ln["w_curve"]), 1)   # drift points recorded
        snap = eng.state.snapshot()["learning"]
        self.assertEqual(snap["da_total"]["punish"], 1)
        self.assertIn("gap_curve", snap)


class TestShadowTwin(unittest.TestCase):
    def _engine(self, brain=None):
        self.d = tempfile.mkdtemp()
        eng = Engine(mk_cfg(brain), live=False, client=FakeSpotClient(mk_rows()),
                     runs_dir=self.d, shadow=True)
        eng.last_candle_ts[PAIR] = 0
        return eng

    def test_refused_with_live_keys(self):
        c = FakeSpotClient(mk_rows())
        c.key, c.secret = "k", "s"
        with self.assertRaises(ValueError):
            Engine(mk_cfg(), live=True, client=c,
                   runs_dir=tempfile.mkdtemp(), shadow=True)

    def test_shadow_trades_own_account_and_reports_equity(self):
        eng = self._engine()
        # the twin buys on the same signal the main fly buys on
        for b in eng.sh_brains.values():
            b.observe = lambda rows, delta, **kw: {"decision": "BUY",
                                                   "r2": 0.9, "diff_hz": 3.0,
                                                   "gate": 0.9}
        eng.candles[PAIR] = mk_rows()
        eng.state.pairs[PAIR]["price"] = mk_rows()[-1][4]   # card price loaded
        with mock.patch.object(history_mod, "append_cache"):
            eng._shadow_observe(PAIR)
        sh = eng.sh_brokers[PAIR]
        self.assertGreater(sh.pos.base, 0.0)
        self.assertLess(sh.cash, eng.cfg.quote_budget)   # its OWN account
        st = eng.state.snapshot()
        self.assertGreater(st["shadow"]["total_equity"], 0.0)
        self.assertEqual(st["shadow"]["pairs"][PAIR]["decision"], "BUY")
        self.assertTrue(st["shadow"]["equity_curve"])

    def test_frozen_weights_never_move(self):
        eng = self._engine(BrainConfig(trip_teaching=True, eta=4.0))
        w_a0 = eng.sh_brains[PAIR].w_a.copy()
        w_v0 = eng.sh_brains[PAIR].w_v.copy()
        # a whole trip on rising then crashing prices: the twin observes
        # everything the learner does, frozen
        for i, px in enumerate([100.0] * 5 + [101.0, 102.0, 103.0, 101.0, 99.0]):
            eng.candles[PAIR] = mk_rows(px=px, ts=1_700_000_000 + i * 60)
            with mock.patch.object(history_mod, "append_cache"):
                eng._shadow_observe(PAIR)
        self.assertTrue(np.array_equal(eng.sh_brains[PAIR].w_a, w_a0))
        self.assertTrue(np.array_equal(eng.sh_brains[PAIR].w_v, w_v0))

    def test_twin_wiring_matches_the_learner(self):
        eng = self._engine()
        ref = FlyBrain(eng.sh_brains[PAIR].cfg, tag="ref")
        self.assertTrue(np.array_equal(eng.sh_brains[PAIR].kc_mask, ref.kc_mask))
        self.assertTrue(np.array_equal(eng.brains[PAIR].kc_mask,
                                       eng.sh_brains[PAIR].kc_mask))


if __name__ == "__main__":
    unittest.main()
