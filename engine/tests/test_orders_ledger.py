"""orders.jsonl — the durable per-fill ledger.

Covers:
  - a confirmed fill lands as one JSON line (Trade schema, spot + futures)
  - a torn/corrupt tail line is skipped on read, never fatal
  - pair (case-insensitive) / mode filters and the limit behave exactly
  - seq continues across a restart (a new ledger counts existing rows)
  - a broker WITHOUT a ledger writes nothing — backtests must stay clean
  - a broker WITH a ledger records BUY and SELL fills (paper restart drops
    in-memory trades; the ledger is what keeps the history)
  - recording must never break trading: an unwritable path degrades to None
"""
import os
import tempfile
import unittest

from flygate.orders import OrdersLedger, orders_path, read_orders
from flygate.trader.broker import PaperBroker


class _Risk:
    min_notional = 1.0
    slippage = 0.0
    taker_fee = 0.0005


class TestOrdersLedger(unittest.TestCase):
    def setUp(self):
        self.runs = tempfile.mkdtemp(prefix="flygate-orders-")

    def test_record_and_read_roundtrip(self):
        led = OrdersLedger(self.runs)

        class T:
            def as_dict(self):
                return {"ts": 1.0, "time": "x", "pair": "BTC_USDT",
                        "side": "BUY", "price": 100.0, "base": 0.1,
                        "quote": 10.0, "fee": 0.005, "reason": "test",
                        "mode": "paper"}

        row = led.record(T())
        self.assertEqual(row["seq"], 1)
        rows = read_orders(self.runs)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["pair"], "BTC_USDT")
        self.assertEqual(rows[0]["reason"], "test")

    def test_filters_and_limit(self):
        led = OrdersLedger(self.runs)
        for i in range(5):
            led.record({"pair": "BTC_USDT", "mode": "paper",
                        "side": "BUY", "i": i})
        led.record({"pair": "ETH_USDT", "mode": "paper", "side": "SELL"})
        led.record({"pair": "FUT:ZEC_USDT", "mode": "fut-paper",
                    "side": "BUY"})
        self.assertEqual(len(read_orders(self.runs)), 7)
        # pair filter is case-insensitive, futures rows match on FUT: prefix
        self.assertEqual(len(read_orders(self.runs, pair="btc_usdt")), 5)
        self.assertEqual(len(read_orders(self.runs, pair="FUT:ZEC_USDT")), 1)
        self.assertEqual(len(read_orders(self.runs, mode="fut-paper")), 1)
        # newest last; limit keeps the TAIL
        self.assertEqual(read_orders(self.runs, limit=2)[-1]["pair"],
                         "FUT:ZEC_USDT")

    def test_torn_tail_line_is_skipped(self):
        p = orders_path(self.runs)
        with open(p, "w", encoding="utf-8") as f:
            f.write('{"pair": "BTC_USDT", "mode": "paper"}\n')
            f.write('{"pair": "BTC_USDT", "mod')      # torn final write
        rows = read_orders(self.runs)
        self.assertEqual(len(rows), 1)

    def test_seq_survives_restart(self):
        OrdersLedger(self.runs).record({"pair": "A", "mode": "paper"})
        row = OrdersLedger(self.runs).record({"pair": "A", "mode": "paper"})
        self.assertEqual(row["seq"], 2)

    def test_broker_without_ledger_writes_nothing(self):
        b = PaperBroker("BTC_USDT", 100.0, _Risk())
        self.assertIsNotNone(b.buy(50.0, 100.0, "test"))
        self.assertFalse(os.path.exists(orders_path(self.runs)))

    def test_broker_with_ledger_records_buy_and_sell(self):
        b = PaperBroker("BTC_USDT", 100.0, _Risk())
        b.ledger = OrdersLedger(self.runs)
        self.assertIsNotNone(b.buy(50.0, 100.0, "fly DN test"))
        self.assertIsNotNone(b.sell(110.0, "exit"))
        rows = read_orders(self.runs)
        self.assertEqual([r["side"] for r in rows], ["BUY", "SELL"])
        self.assertEqual(rows[0]["reason"], "fly DN test")
        self.assertEqual(rows[0]["mode"], "paper")

    def test_record_failure_never_raises(self):
        led = OrdersLedger(self.runs)
        led.path = os.path.join(self.runs, "no", "such", "dir",
                                "orders.jsonl")
        # makedirs would create it — point at a file-as-directory instead
        blocker = os.path.join(self.runs, "blocker")
        with open(blocker, "w", encoding="utf-8") as f:
            f.write("x")
        led.path = os.path.join(blocker, "orders.jsonl")
        self.assertIsNone(led.record({"pair": "A"}))


if __name__ == "__main__":
    unittest.main()
