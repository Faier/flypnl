"""Futures safety regressions (2026-09-15 audit, second round).

All offline: fake exchange responses, no network, no real orders.

Covers:
  P0  a dust order amount is written as a PLAIN decimal ("0.00002000"); the old
      f"{x:.10g}" sent "2e-05", which Gate rejects, and the risk-reducing exits
      only log failures — the position stayed open through a halt
  P0  an amount that rounds to zero is refused instead of sent
  P0  a config quanto that disagrees with the contract's real
      quanto_multiplier aborts BEFORE any order (and before the leverage POST);
      an unreachable contract endpoint only warns and keeps trading; a
      reduce-only flatten is never blocked by the mismatch
  P0  the leverage is set AND read back: mismatch, failed POST or an
      unreadable leverage all mean "no order"
  P0  the StopGuard trigger is checked against the leverage-implied
      liquidation distance — a trigger at/beyond it refuses to open
  P0  the runner refuses to open at startup on a quanto mismatch (sticky)
"""
import contextlib
import io
import json
import tempfile
import time
import unittest

from flygate.brain.flybrain import BUY
from flygate.config import Config, RiskConfig
from flygate.data.gate_client import GateClient, GateError
from flygate.logs import Log
from flygate.trader.engine import AppState
from flygate.trader.futures_broker import FuturesBroker, QuantoMismatch
from flygate.trader.futures_runner import FuturesRunner
from flygate.trader.stopguard import DEFAULT_DIST


# ----------------------------------------------------------------- fakes
class FakeFutClient:
    """Offline futures exchange exposing the fields the safety checks read.

    Every knob maps to a real observation point:
      quanto_multiplier   -> /futures/usdt/contracts/<c> (None = field missing)
      contracts_error     -> that endpoint is unreachable
      leverage            -> the leverage the exchange reports back
      leverage_reply      -> whether the leverage POST reply carries it
      leverage_post_error -> the leverage POST itself fails
      leverage_row        -> a "leverage" field on position records
    """
    key = "k"
    secret = "s"

    def __init__(self, quanto_multiplier=0.01, contracts_error=None,
                 leverage=2, leverage_reply=True, leverage_post_error=None,
                 leverage_row=None, position=None, fill_price=100.0):
        self.contract = "ZEC_USDT"
        self.quanto_multiplier = quanto_multiplier
        self.contracts_error = contracts_error
        self.leverage = leverage
        self.leverage_reply = leverage_reply
        self.leverage_post_error = leverage_post_error
        self.leverage_row = leverage_row
        self.position = position
        self.fill_price = fill_price
        self.orders = []              # bodies sent to /futures/usdt/orders
        self.leverage_posts = []      # query of every leverage POST
        self.contract_reads = 0

    def _request(self, method, path, query=None, body=None, auth=False,
                 timeout=10.0):
        if method == "POST" and path.endswith("/leverage"):
            self.leverage_posts.append(query)
            if self.leverage_post_error:
                raise RuntimeError(self.leverage_post_error)
            return ({"leverage": str(self.leverage)} if self.leverage_reply
                    else {})
        if method == "POST" and path == "/futures/usdt/orders":
            self.orders.append(body)
            prev = int((self.position or {}).get("size") or 0)
            self.position = {"contract": self.contract,
                             "size": prev + int(body["size"]),
                             "entry_price": str(self.fill_price),
                             "liq_price": "0"}
            if self.leverage_row is not None:
                self.position["leverage"] = str(self.leverage_row)
            return {"id": "o1"}
        if method == "GET" and path.startswith("/futures/usdt/contracts/"):
            self.contract_reads += 1
            if self.contracts_error:
                raise RuntimeError(self.contracts_error)
            if self.quanto_multiplier is None:
                return {"order_price_round": "0.1"}
            return {"quanto_multiplier": str(self.quanto_multiplier),
                    "order_price_round": "0.1"}
        if method == "GET" and path == "/futures/usdt/positions":
            return [self.position] if self.position else []
        if method == "GET" and path.startswith("/futures/usdt/positions/"):
            return self.position or {}
        if method == "GET" and path.startswith("/futures/usdt/orders/"):
            return {"fill_price": str(self.fill_price), "left": "0"}
        if method == "POST" and path == "/futures/usdt/price_orders":
            return {"id": "g1"}
        raise AssertionError(f"unexpected request {method} {path}")


class FakeSpotClient(GateClient):
    """The REAL GateClient (so the real order body is built) with the transport
    stubbed and the pair-rules cache seeded."""
    def __init__(self, rules=None):
        super().__init__(key="k", secret="s")
        self._all_pairs = rules or {}
        self.bodies = []              # (method, path, body) of every request

    def _request(self, method, path, query=None, body=None, auth=False,
                 timeout=10.0):
        self.bodies.append((method, path, body))
        return {"id": "s1", "status": "closed"}


def mk_broker(client, paper=False, cash=60.0, **kw):
    return FuturesBroker("ZEC_USDT", cash, RiskConfig(), client,
                         quanto=0.01, paper=paper, **kw)


def mk_cfg(risk=None):
    cfg = Config()
    cfg.pairs = []
    cfg.futures = {"enable": True, "contract": "ZEC_USDT", "budget": 60,
                   "paper": False, "max_contracts": 10, "entry_step": 2,
                   "quanto": 0.01, "fee": 0.0005}
    if risk is not None:
        cfg.risk = risk
    return cfg


def mk_runner(client, risk=None, live=False):
    d = tempfile.mkdtemp()
    r = FuturesRunner(AppState([]), client, mk_cfg(risk), d, live=live)
    r.log = Log(d)
    return r, d


def flat_rows(px, ts, n=80, step=60):
    return [[ts - step * (n - 1 - i), px, px, px, px, 1.0] for i in range(n)]


def step_with(runner, decision, px, ts):
    runner.brain.observe = lambda rows, delta: {"decision": decision,
                                                "r2": 0.9}
    runner._step(flat_rows(px, ts))


# ------------------------------------------------------- order formatting
class TestOrderAmountFormatting(unittest.TestCase):
    def test_dust_sell_is_sent_as_a_plain_decimal(self):
        c = FakeSpotClient({"PONS_USDT": {"amount_precision": 8}})
        c.create_market_order("PONS_USDT", "sell", 2e-05)
        body = c.bodies[-1][2]
        self.assertEqual(body["amount"], "0.00002000")
        self.assertAlmostEqual(float(body["amount"]), 2e-05)
        # the audit's exact reproduction: "amount":"2e-05" must be impossible
        self.assertNotIn("2e-05", json.dumps(body))
        self.assertNotIn("e", body["amount"].lower())

    def test_dust_sell_without_cached_rules_is_still_plain(self):
        # cold rules cache (e.g. a flatten on a fresh process): still no
        # exponent and still the right value
        c = FakeSpotClient()
        c.create_market_order("PONS_USDT", "sell", 2e-05)
        body = c.bodies[-1][2]
        self.assertEqual(body["amount"], "0.00002")
        self.assertNotIn("e", body["amount"].lower())

    def test_normal_amounts_keep_the_pair_precision(self):
        c = FakeSpotClient({"PONS_USDT": {"amount_precision": 3}})
        c.create_market_order("PONS_USDT", "sell", 8.654)
        self.assertEqual(c.bodies[-1][2]["amount"], "8.654")
        c.create_market_order("PONS_USDT", "sell", 12.0)
        self.assertEqual(c.bodies[-1][2]["amount"], "12.000")

    def test_amount_that_rounds_to_zero_is_never_sent(self):
        # integer-precision pair: 2e-05 is 0 base units
        c = FakeSpotClient({"PONS_USDT": {"amount_precision": 0}})
        with self.assertRaises(GateError):
            c.create_market_order("PONS_USDT", "sell", 2e-05)
        self.assertEqual(c.bodies, [])
        # 8-decimal pair but the amount is below even that
        c2 = FakeSpotClient({"PONS_USDT": {"amount_precision": 8}})
        with self.assertRaises(GateError):
            c2.create_market_order("PONS_USDT", "sell", 1e-13)
        self.assertEqual(c2.bodies, [])


# ------------------------------------------------------------ quanto gate
class TestQuantoGuard(unittest.TestCase):
    def test_quanto_mismatch_refuses_before_any_order(self):
        c = FakeFutClient(quanto_multiplier=0.005)      # config claims 0.01
        b = mk_broker(c)
        with self.assertRaises(QuantoMismatch):
            b.verify_contract()
        self.assertIn("0.01", b.trade_blocked)
        self.assertIn("0.005", b.trade_blocked)
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertIsNone(b.open_long(2, 100.0))
        self.assertEqual(c.orders, [])                  # never reached orders
        self.assertEqual(c.leverage_posts, [])          # nor the leverage POST
        self.assertEqual(c.contract_reads, 1)           # sticky: no refetch
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertIsNone(b.open_long(2, 100.0))
        self.assertEqual(c.contract_reads, 1)

    def test_unreachable_contract_endpoint_only_warns_and_still_trades(self):
        c = FakeFutClient(contracts_error="Name or service not known")
        b = mk_broker(c)
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            t = b.open_long(2, 100.0)
        self.assertIsNotNone(t)                         # order WAS sent + booked
        self.assertEqual(len(c.orders), 1)
        self.assertEqual(b.size, 2)
        self.assertEqual(b.trade_blocked, "")           # not blocked
        out = buf.getvalue()
        self.assertIn("WARNING", out)
        self.assertIn("0.01", out)                      # names the config value

    def test_missing_multiplier_field_only_warns(self):
        c = FakeFutClient(quanto_multiplier=None)       # endpoint answered {}
        b = mk_broker(c)
        with contextlib.redirect_stdout(io.StringIO()):
            t = b.open_long(2, 100.0)
        self.assertIsNotNone(t)
        self.assertEqual(len(c.orders), 1)

    def test_quanto_mismatch_still_allows_a_flatten(self):
        # a mismatched multiplier mis-prices our books; it must NEVER strand an
        # open position (the failure mode of the old dust-amount bug)
        c = FakeFutClient(quanto_multiplier=0.005,
                          position={"contract": "ZEC_USDT", "size": 2,
                                    "entry_price": "100.0"})
        b = mk_broker(c)
        b.size, b.entry = 2, 100.0
        with contextlib.redirect_stdout(io.StringIO()) as buf:
            t = b.close_all(100.0, "halt-flatten")
        self.assertIsNotNone(t)
        self.assertEqual(len(c.orders), 1)
        self.assertTrue(c.orders[0]["reduce_only"])
        self.assertEqual(b.size, 0)
        self.assertIn("reduce-only", buf.getvalue())


# ---------------------------------------------------------- leverage gate
class TestLeverageVerification(unittest.TestCase):
    def test_leverage_mismatch_blocks_the_order(self):
        c = FakeFutClient(leverage=5)                   # account sits at 5x
        b = mk_broker(c)
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertIsNone(b.open_long(2, 100.0))
        self.assertEqual(c.orders, [])
        self.assertEqual(c.leverage_posts, [{"leverage": 2}])
        self.assertIn("5x", b.last_refusal)
        self.assertIn("2x", b.last_refusal)

    def test_failed_leverage_post_blocks_the_order(self):
        c = FakeFutClient(leverage_post_error="HTTP 500 bad request")
        b = mk_broker(c)
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertIsNone(b.open_long(2, 100.0))
        self.assertEqual(c.orders, [])
        self.assertIn("leverage POST failed", b.last_refusal)

    def test_unverifiable_leverage_blocks_the_order(self):
        # the leverage reply is empty, the account is flat and the
        # single-position endpoint has nothing to say either
        c = FakeFutClient(leverage_reply=False, position=None)
        b = mk_broker(c)
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertIsNone(b.open_long(2, 100.0))
        self.assertEqual(c.orders, [])
        self.assertIn("read-back unavailable", b.last_refusal)

    def test_leverage_read_back_from_the_position_record_is_accepted(self):
        c = FakeFutClient(leverage_reply=False, leverage_row=2,
                          position={"contract": "ZEC_USDT", "size": 0,
                                    "entry_price": "0", "leverage": "2"})
        b = mk_broker(c)
        with contextlib.redirect_stdout(io.StringIO()):
            t = b.open_long(2, 100.0)
        self.assertIsNotNone(t)
        self.assertEqual(len(c.orders), 1)
        self.assertEqual(b.size, 2)

    def test_verified_leverage_is_not_reposted(self):
        c = FakeFutClient()
        b = mk_broker(c)
        with contextlib.redirect_stdout(io.StringIO()):
            b.open_long(2, 100.0)
            b.open_long(1, 100.0)
        self.assertEqual(len(c.orders), 2)
        self.assertEqual(len(c.leverage_posts), 1)      # set+verify once
        self.assertEqual(c.contract_reads, 1)

    def test_reduce_only_exit_is_never_blocked_by_unknown_leverage(self):
        c = FakeFutClient(leverage_post_error="boom",
                          position={"contract": "ZEC_USDT", "size": 2,
                                    "entry_price": "100.0"})
        b = mk_broker(c)
        b.size, b.entry = 2, 100.0
        with contextlib.redirect_stdout(io.StringIO()):
            t = b.close_all(100.0, "halt-flatten")
        self.assertIsNotNone(t)
        self.assertTrue(c.orders[0]["reduce_only"])
        self.assertEqual(c.leverage_posts, [])          # never even attempted
        self.assertEqual(b.size, 0)                     # position gone


# ----------------------------------------------- liquidation vs stop trigger
class TestLiquidationDistance(unittest.TestCase):
    def test_trigger_inside_liquidation_aborts(self):
        # 11x: liquidation is ~8.6% away, the 10% disaster trigger is BEYOND it
        c = FakeFutClient(leverage=11)
        b = mk_broker(c, leverage=11, guard_dist=0.10)
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertIsNone(b.open_long(2, 100.0))
        self.assertEqual(c.orders, [])
        self.assertIn("liquidation distance", b.last_refusal)

    def test_trigger_wider_than_liquidation_aborts(self):
        # 2x is fine at the shipped 10% distance, but a 60% trigger would sit
        # past the ~49.5% liquidation line
        c = FakeFutClient(leverage=2)
        b = mk_broker(c, leverage=2, guard_dist=0.60)
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertIsNone(b.open_long(2, 100.0))
        self.assertEqual(c.orders, [])

    def test_two_x_with_the_shipped_trigger_still_opens(self):
        c = FakeFutClient(leverage=2)
        b = mk_broker(c, leverage=2, guard_dist=DEFAULT_DIST)
        with contextlib.redirect_stdout(io.StringIO()):
            t = b.open_long(2, 100.0)
        self.assertIsNotNone(t)
        self.assertEqual(len(c.orders), 1)


# ------------------------------------------------------------ runner wiring
class TestRunnerPreflight(unittest.TestCase):
    def test_runner_refuses_to_open_on_a_quanto_mismatch(self):
        risk = RiskConfig(cooldown_s=0, max_orders_per_day=0,
                          daily_loss_halt=0)
        c = FakeFutClient(quanto_multiplier=0.005)
        with contextlib.redirect_stdout(io.StringIO()) as buf:
            r, _ = mk_runner(c, risk=risk, live=True)
        self.assertTrue(r.broker.trade_blocked)
        self.assertIn("0.005", r.broker.trade_blocked)
        self.assertIn("REFUSING TO TRADE", buf.getvalue())
        # the shipped wiring: the broker checks against the SAME distance the
        # StopGuard actually places
        self.assertAlmostEqual(r.broker.guard_dist, r.guard.dist)
        ts = 1_700_000_000
        with contextlib.redirect_stdout(io.StringIO()):
            step_with(r, BUY, 100.0, ts)
        self.assertEqual(c.orders, [])                  # no entry attempted
        self.assertEqual(r.broker.size, 0)
        note = r.state.pairs[r.key]["telemetry"]["note"]
        self.assertIn("entries blocked", note)

    def test_runner_still_trades_when_the_multiplier_matches(self):
        risk = RiskConfig(cooldown_s=0, max_orders_per_day=0,
                          daily_loss_halt=0)
        c = FakeFutClient(quanto_multiplier=0.01)
        with contextlib.redirect_stdout(io.StringIO()):
            r, _ = mk_runner(c, risk=risk, live=True)
            step_with(r, BUY, 100.0, 1_700_000_000)
        self.assertEqual(r.broker.trade_blocked, "")
        self.assertEqual(len(c.orders), 1)
        self.assertEqual(r.broker.size, 2)


# ------------------------------------------------ margin-rejection freeze
class MarginRejectedClient(FakeFutClient):
    """The orders POST is rejected on margin — the exact 2026-09-29 ZEC
    incident (margin 24.05 while available 0.000): the runner used to see
    the raw exception, log it and fire a new POST EVERY candle for 2h."""
    def _request(self, method, path, query=None, body=None, auth=False,
                 timeout=10.0):
        if method == "POST" and path == "/futures/usdt/orders":
            self.orders.append(body)      # one attempt is expected to leave
            raise GateError('HTTP 400 POST /futures/usdt/orders: '
                            '{"label":"INSUFFICIENT_AVAILABLE","message":'
                            '"margin 24.051 while available 0.000"}')
        return super()._request(method, path, query=query, body=body,
                                auth=auth, timeout=timeout)


class MarginBoomClient(FakeFutClient):
    """A NON-margin rejection (auth, exchange outage) keeps the loud path."""
    def _request(self, method, path, query=None, body=None, auth=False,
                 timeout=10.0):
        if method == "POST" and path == "/futures/usdt/orders":
            self.orders.append(body)
            raise GateError("HTTP 500: internal server error")
        return super()._request(method, path, query=query, body=body,
                                auth=auth, timeout=timeout)


class TestMarginRejectionBackoff(unittest.TestCase):
    def test_rejected_order_freezes_entries_objects_never_raises(self):
        c = MarginRejectedClient()
        b = mk_broker(c)
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertIsNone(b.open_long(3, 100.0, "staircase-up"))
        self.assertEqual(len(c.orders), 1)          # exactly ONE post left
        self.assertLessEqual(b.margin_backoff_until - time.time(), 1800)
        self.assertGreaterEqual(b.margin_backoff_until, time.time())
        self.assertIn("INSUFFICIENT", b.last_refusal)

    def test_backoff_blocks_opens_but_not_flattens(self):
        c = MarginRejectedClient()
        b = mk_broker(c)
        with contextlib.redirect_stdout(io.StringIO()):
            b.open_long(3, 100.0, "staircase-up")
        self.assertEqual(len(c.orders), 1)          # frozen preflight: no 2nd post
        ok, why = b._preflight(reduce_only=False)
        self.assertFalse(ok)
        self.assertIn("margin-backoff", why)
        self.assertIn("frozen", why)
        ok, _ = b._preflight(reduce_only=True)      # exits must stay live
        self.assertTrue(ok)
        # flatten during the window: the broker refuses to make it impossible
        with contextlib.redirect_stdout(io.StringIO()):
            t = b.open_long(2, 100.0, "add-long")
        self.assertIsNone(t)
        self.assertEqual(len(c.orders), 1)

    def test_backoff_self_heals_after_the_window(self):
        c = MarginRejectedClient()
        b = mk_broker(c)
        b.margin_backoff_until = time.time() - 1    # expired
        with contextlib.redirect_stdout(io.StringIO()):
            ok, why = b._preflight(reduce_only=False)
        self.assertTrue(ok, why)

    def test_unknown_rejection_still_raises(self):
        c = MarginBoomClient()
        b = mk_broker(c)
        with contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaises(GateError):
                b.open_long(3, 100.0, "staircase-up")


if __name__ == "__main__":
    unittest.main()
