"""Fault-injection regressions for the 2026-09-15 audit fixes.

Every test runs fully offline against fake exchange responses: zero fills,
partial fills, foreign-contract positions, expiring stops, day rolls, cap
overshoots and restarts. No network, no real orders, no live state.

Covers:
  P0  paper CLI must force the futures leg to paper (isolated state files)
  P0  read-only client refuses private writes in paper mode
  P0  close_all is reduce_only — a flatten against an already-gone position
      can never open a REVERSE position
  P0  _my_position matches strictly by contract (no rows[0] fallback)
  P0  partial fills book pnl/fee/Trade by the position DELTA, not the request
  P0  same-direction adds keep the watermark / scale latch / profit lock;
      flips reset them; extreme + order counters survive a restart
  P0  StopGuard re-places an aged trigger, cancels only its own orders,
      keeps the id when a cancel fails
  P1  the futures daily order counter rolls over at midnight
  P1  adds are clamped to the contract cap (no overshoot-then-trim)
  P1  an engine halt keeps flattening instead of leaving positions open
  P1  brain save/load carries the EMA trend states (seamless restart)
  P1  backtest win-rate counts ADDS into the trip; a halted run settles at
      the halt candle's price, not the dataset's last candle
"""
import os
import tempfile
import time
import unittest

from flygate.config import Config, RiskConfig, BrainConfig
from flygate.data.gate_client import GateClient, GateError
from flygate.logs import Log
from flygate.trader.engine import AppState, Engine
from flygate.trader.futures_broker import FuturesBroker
from flygate.trader.futures_runner import FuturesRunner
from flygate.trader.stopguard import StopGuard
from flygate.brain.flybrain import FlyBrain, BUY, SELL


# ----------------------------------------------------------------- fakes
class FakeFutClient:
    """Offline futures 'exchange': records writes, returns canned state."""
    key = "k"
    secret = "s"

    def __init__(self, position=None, fill_price=0.0):
        self.position = position          # record the positions endpoint shows
        self.fill_price = fill_price
        self.posts = []                   # (path, body) captured
        self.deletes = []                 # paths captured
        self.price_orders = []
        self._nid = 100

    def _request(self, method, path, query=None, body=None, auth=False,
                 timeout=10.0):
        if method == "POST":
            self.posts.append((path, body))
            self._nid += 1
            return {"id": str(self._nid)}
        if method == "DELETE":
            self.deletes.append(path)
            return {}
        if method == "GET" and path == "/futures/usdt/positions":
            return [self.position] if self.position else []
        if method == "GET" and path.startswith("/futures/usdt/orders/"):
            return {"fill_price": str(self.fill_price), "left": "0"}
        if method == "GET" and path == "/futures/usdt/contracts/ZEC_USDT":
            return {"order_price_round": 0.1}
        if method == "GET" and path == "/futures/usdt/price_orders":
            return self.price_orders
        if method == "GET" and path == "/futures/usdt/accounts":
            return {"available": 55.0}
        raise AssertionError(f"unexpected request {method} {path}")


def mk_broker(client, paper=False, cash=60.0):
    return FuturesBroker("ZEC_USDT", cash, RiskConfig(), client,
                         quanto=0.01, paper=paper)


def mk_cfg(risk=None):
    cfg = Config()
    cfg.pairs = []
    cfg.futures = {"enable": True, "contract": "ZEC_USDT", "budget": 60,
                   "paper": False, "max_contracts": 10, "entry_step": 2,
                   "quanto": 0.01, "fee": 0.0005}
    if risk is not None:
        cfg.risk = risk
    return cfg


def flat_rows(px, ts, n=80, step=60):
    return [[ts - step * (n - 1 - i), px, px, px, px, 1.0] for i in range(n)]


def step_with(runner, decision, px, ts):
    """Drive one _step with the brain pinned to a fixed decision."""
    runner.brain.observe = lambda rows, delta: {"decision": decision,
                                                "r2": 0.9}
    runner._step(flat_rows(px, ts))


# ----------------------------------------------------------------- broker
class TestBrokerFaults(unittest.TestCase):
    def test_close_all_is_reduce_only_and_never_opens_reverse(self):
        # the exchange is ALREADY flat (manual close on the app) while the
        # local model still believes it is long 2
        c = FakeFutClient(position=None, fill_price=110.0)
        b = mk_broker(c)
        b.size, b.entry = 2, 100.0
        t = b.close_all(110.0, "test")
        body = next(body for p, body in c.posts if p == "/futures/usdt/orders")
        self.assertTrue(body["reduce_only"])
        self.assertEqual(b.size, 0)              # flattened, NOT short 2
        self.assertAlmostEqual(t.base, 0.02)     # booked the 2 closed lots
        self.assertAlmostEqual(b.cash, 60 + 0.2 - 0.0011, places=4)

    def test_my_position_no_foreign_fallback(self):
        # asking for ZEC, the endpoint answers with BTC's 500 lots
        c = FakeFutClient(position={"contract": "BTC_USDT", "size": 500,
                                    "entry_price": 60000})
        b = mk_broker(c)
        self.assertIsNone(b._my_position())
        b.size = 2
        b.sync_position()
        self.assertEqual((b.size, b.entry), (0, 0.0))   # ZEC absent = flat

    def test_partial_fill_books_actual_filled_size(self):
        # asked to close 10 at 110; only 4 actually filled
        c = FakeFutClient(position={"contract": "ZEC_USDT", "size": 6,
                                    "entry_price": 100.0}, fill_price=110.0)
        b = mk_broker(c)
        b.size, b.entry = 10, 100.0
        t = b.close_all(110.0, "test")
        self.assertEqual(b.size, 6)
        self.assertAlmostEqual(t.base, 0.04)                    # not 0.10
        # pnl 4*0.01*(110-100) - fee 4*0.01*110*0.0005 = 0.4 - 0.0022
        # (exactly the audit's expected 60.3978, previously booked 60.9945)
        self.assertAlmostEqual(b.cash, 60.3978, places=4)
        self.assertAlmostEqual(t.fee, 0.0022, places=6)

    def test_rejected_order_books_nothing(self):
        # the exchange position is UNCHANGED (order rejected): delta = 0
        c = FakeFutClient(position={"contract": "ZEC_USDT", "size": 2,
                                    "entry_price": 100.0}, fill_price=100.0)
        b = mk_broker(c)
        b.size, b.entry = 2, 100.0
        t = b.close_all(100.0, "test")
        self.assertIsNone(t)
        self.assertEqual(b.cash, 60.0)
        self.assertEqual(b.trades, [])

    def test_post_order_sync_failure_flags_dirty(self):
        class Boom(FakeFutClient):
            def _request(self, method, path, **kw):
                if path == "/futures/usdt/positions":
                    raise RuntimeError("net down")
                return super()._request(method, path, **kw)

        c = Boom(fill_price=100.0)
        b = mk_broker(c)
        b.size, b.entry = 2, 100.0
        self.assertIsNone(b.close_all(100.0, "test"))
        self.assertTrue(b.sync_dirty)          # runner will re-sync next candle


# ----------------------------------------------------------------- runner
class TestRunnerFixes(unittest.TestCase):
    def _runner(self, risk=None, live=False):
        d = tempfile.mkdtemp()
        r = FuturesRunner(AppState([]), FakeFutClient(), mk_cfg(risk), d,
                          live=live)
        r.log = Log(d)
        return r, d

    def test_cli_paper_forces_futures_paper_and_isolates_state(self):
        r, d = self._runner(live=False)
        self.assertTrue(r.paper)               # CLI beats futures.paper=false
        self.assertTrue(r.broker.paper)
        r._save()
        self.assertTrue(os.path.exists(os.path.join(d, "fut.state.paper.json")))
        self.assertFalse(os.path.exists(os.path.join(d, "fut.state.json")))
        r2 = FuturesRunner(AppState([]), FakeFutClient(), mk_cfg(),
                           tempfile.mkdtemp(), live=True)
        self.assertFalse(r2.paper)             # live CLI keeps live futures

    def test_extreme_and_counters_survive_restart(self):
        r, d = self._runner()
        r.broker.size, r.broker.entry = 2, 100.0
        r.extreme, r.profit_lock, r.scaled_out = 120.0, 103.0, True
        r.n_today, r.last_order_ts = 3, 123.0
        r._save()
        r2 = FuturesRunner(AppState([]), FakeFutClient(), mk_cfg(), d,
                           live=False)
        self.assertEqual(r2.extreme, 120.0)    # was re-anchored to entry before
        self.assertEqual(r2.profit_lock, 103.0)
        self.assertTrue(r2.scaled_out)
        self.assertEqual(r2.n_today, 3)

    def test_add_keeps_protection_flip_resets_it(self):
        risk = RiskConfig(cooldown_s=0, max_orders_per_day=0)
        r, _ = self._runner(risk=risk)
        ts = int(time.time() // 60) * 60 - 1
        step_with(r, BUY, 100.0, ts)                   # fresh entry
        self.assertEqual(r.broker.size, 2)
        self.assertEqual(r.extreme, 100.0)
        r.extreme, r.profit_lock, r.scaled_out = 107.0, 103.0, True
        step_with(r, BUY, 104.0, ts + 60)              # same-direction add
        self.assertEqual(r.broker.size, 4)
        self.assertEqual(r.extreme, 107.0)             # watermark kept
        self.assertEqual(r.profit_lock, 103.0)         # lock kept
        self.assertTrue(r.scaled_out)                  # latch kept
        step_with(r, SELL, 105.0, ts + 120)            # flip to short
        self.assertEqual(r.broker.size, -2)            # covered 4, short 2
        self.assertEqual(r.extreme, 105.0)             # fresh anchor on flip
        self.assertEqual(r.profit_lock, 0.0)

    def test_add_clamped_to_cap(self):
        risk = RiskConfig(cooldown_s=0, max_orders_per_day=0)
        r, _ = self._runner(risk=risk)
        ts = int(time.time() // 60) * 60 - 1
        r.broker.size, r.broker.entry = 9, 100.0
        r.extreme = 100.0
        step_with(r, BUY, 100.0, ts)                   # 9 + min(2, 10-9) = 10
        self.assertEqual(r.broker.size, 10)

    def test_daily_counter_resets_across_days(self):
        risk = RiskConfig(cooldown_s=0, max_orders_per_day=6,
                          daily_loss_halt=0)
        r, _ = self._runner(risk=risk)
        ts = int(time.time())
        r.day = time.strftime("%Y-%m-%d", time.localtime(ts - 86400))
        r.n_today = 6                                   # yesterday's quota
        step_with(r, BUY, 100.0, ts)                    # today's candle
        self.assertEqual(r.broker.size, 2)              # rolled -> allowed
        self.assertEqual(r.n_today, 1)

    def test_push_updates_global_totals_when_spot_silent(self):
        r, _ = self._runner()
        r.broker.cash = 61.5
        r._push(100.0, {"decision": "HOLD"})
        self.assertEqual(r.state.total_equity, 61.5)    # was stuck at 0
        self.assertTrue(r.state.equity_curve)


# ----------------------------------------------------------------- guard
class TestStopGuard(unittest.TestCase):
    def test_renews_before_24h_expiry(self):
        c = FakeFutClient()
        g = StopGuard(c, dist=0.10)
        t0 = 1_000_000.0
        g._now = lambda: t0
        creates = lambda: [b for p, b in c.posts
                           if p == "/futures/usdt/price_orders"]
        g.refresh("ZEC_USDT", 1, 100.0)
        self.assertEqual(len(creates()), 1)
        g.refresh("ZEC_USDT", 1, 100.0)                 # young + unchanged
        self.assertEqual(len(creates()), 1)             # no churn
        g._now = lambda: t0 + 21 * 3600                 # aged past renewal
        g.refresh("ZEC_USDT", 1, 100.0)
        self.assertEqual(len(creates()), 2)             # re-placed in time
        self.assertEqual(len(c.deletes), 1)

    def test_reconcile_deletes_only_our_triggers(self):
        c = FakeFutClient()
        c.price_orders = [
            {"id": 1, "initial": {"contract": "ZEC_USDT",
                                  "text": "t-flygate-guard"}},
            {"id": 2, "initial": {"contract": "ZEC_USDT", "text": "api"}},
            {"id": 3, "initial": {"contract": "ZEC_USDT"}},
        ]
        StopGuard(c).reconcile("ZEC_USDT")
        self.assertEqual(c.deletes, ["/futures/usdt/price_orders/1"])

    def test_cancel_failure_keeps_id_for_retry(self):
        class Flaky(FakeFutClient):
            fail_delete = True

            def _request(self, method, path, **kw):
                if method == "DELETE" and self.fail_delete:
                    raise RuntimeError("boom")
                return super()._request(method, path, **kw)

        c = Flaky()
        g = StopGuard(c)
        g._id = 7
        g.cancel()
        self.assertEqual(g._id, 7)          # possibly still live: don't forget
        c.fail_delete = False
        g.cancel()
        self.assertIsNone(g._id)


# ----------------------------------------------------------------- engine
class TestReadOnlyGuard(unittest.TestCase):
    def test_readonly_client_blocks_private_writes(self):
        c = GateClient(key="k", secret="s")
        c.read_only = True
        with self.assertRaises(GateError):
            c._request("POST", "/futures/usdt/orders", body={}, auth=True)
        with self.assertRaises(GateError):
            c._request("DELETE", "/futures/usdt/price_orders/1", auth=True)


class TestEngineHaltFlatten(unittest.TestCase):
    def test_halt_keeps_flattening_positions(self):
        cfg = Config()
        cfg.pairs = ["BTC_USDT"]
        cfg.futures = {}
        runs = tempfile.mkdtemp()                   # keep saves off live state
        eng = Engine(cfg, live=False, client=FakeFutClient(), runs_dir=runs)
        eng.log = Log(eng.runs)                     # and logs out of the real
        # runs/logs jsonl (a halt test here leaked "force_close BTC_USDT
        # @99.95" into the live log on 09-17/09-18 and looked like a ghost)
        eng.state.pairs["BTC_USDT"]["price"] = 100.0
        eng.brokers["BTC_USDT"].pos.base = 0.5
        eng.risk.halted = True
        eng.risk.halt_reason = "test halt"
        eng._halt_flatten()
        self.assertLessEqual(eng.brokers["BTC_USDT"].pos.base, 1e-9)
        self.assertTrue(eng.state.halted)


# ----------------------------------------------------------------- brain
class TestBrainStateRoundtrip(unittest.TestCase):
    def test_ema_states_survive_save_load(self):
        cfg = BrainConfig()
        b1 = FlyBrain(cfg)
        ramp = [[1_700_000_000 + i * 60, 100.0 + i, 100.0 + i,
                 99.0 + i, 100.0 + i, 1.0] for i in range(60)]
        for _ in range(5):
            b1.observe(ramp, 0.01)
        p = os.path.join(tempfile.mkdtemp(), "b.npz")
        b1.save(p)
        b2 = FlyBrain(cfg)
        b2.load(p)
        for attr in ("hs_up_s", "hs_dn_s", "r2_s", "fd_up_s", "fd_dn_s",
                     "r2f_s"):
            self.assertAlmostEqual(getattr(b2, attr), getattr(b1, attr),
                                   msg=attr)
        t1 = b1.observe(ramp, 0.0)                   # seamless continuation
        t2 = b2.observe(ramp, 0.0)
        self.assertEqual(t1["decision"], t2["decision"])
        self.assertEqual(t1["diff_hz"], t2["diff_hz"])


# ----------------------------------------------------------------- backtest
class TestBacktestStats(unittest.TestCase):
    @staticmethod
    def _rows():
        # 60 flat, a ramp, 60 flat, a crash, then a big RECOVERY that must
        # never be used to settle a run halted during the crash
        rows = []
        ts = 1_700_000_000
        for i in range(140):
            rows.append([ts + i * 60, 100.0, 100.0, 100.0, 100.0, 1.0])
        for i in range(40):                          # ramp to 130
            px = 100.0 + i * 0.75
            rows.append([ts + (140 + i) * 60, px, px, px, px, 1.0])
        for i in range(40):                          # crash 130 -> 55
            px = 130.0 - i * 1.875
            rows.append([ts + (180 + i) * 60, px, px, px, px, 1.0])
        for i in range(20):                          # "future" recovery
            px = 55.0 + i * 4.75
            rows.append([ts + (220 + i) * 60, px, px, px, px, 1.0])
        return rows

    def test_halted_run_settles_at_halt_price_not_the_future(self):
        from flygate.backtest import run_backtest
        cfg = Config()
        cfg.risk = RiskConfig(daily_loss_halt=5, max_orders_per_day=0,
                              cooldown_s=0)
        res = run_backtest(cfg, "TEST_USDT", self._rows(), mode="random",
                           verbose=False)
        if res["halted"]:
            last_px = res["trade_log"][-1]["price"] if res["trade_log"] else 0
            self.assertLess(last_px, 100.0)     # never the recovery candles
        # either way the run must complete and report consistent numbers
        self.assertGreaterEqual(res["trades"], 0)

    def test_win_rate_counts_adds_into_the_trip(self):
        from flygate.backtest import run_backtest
        cfg = Config()
        cfg.risk = RiskConfig(max_orders_per_day=0, cooldown_s=0)
        res = run_backtest(cfg, "TEST_USDT", self._rows(), mode="random",
                           verbose=False)
        # re-derive trips from the log with the fixed accumulator: all buys
        # (adds) and all sells (partials) belong to ONE trip until flat
        cost = proc = qty = rounds = wins = 0.0
        for t in res["trade_log"]:
            if t["side"] == "BUY":
                cost += t["quote"]
                qty += t["base"]
            else:
                proc += t["quote"]
                qty -= t["base"]
                if qty <= 1e-12:
                    rounds += 1
                    wins += proc > cost
                    cost = proc = qty = 0.0
        if len(res["trade_log"]) == res["trades"]:   # log not truncated
            self.assertEqual(int(rounds), res["round_trips"])
            self.assertEqual(int(wins),
                             round(res["win_rate_pct"] * res["round_trips"] / 100))


if __name__ == "__main__":
    unittest.main()
