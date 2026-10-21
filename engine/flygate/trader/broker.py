"""Brokers: PaperBroker (default, real market data + fake money) and GateBroker
(live spot orders). Both expose the same tiny interface the engine needs.

Position model follows StonkFly's guardrails: LONG-ONLY spot. BUY spends a fixed
quote notional; SELL closes the whole position. No shorts, no leverage.
"""
from __future__ import annotations

import time

from ..storage import atomic_write_json, load_json


class Position:
    def __init__(self):
        self.base = 0.0        # base currency amount held
        self.cost = 0.0        # quote spent on current position


class Trade:
    def __init__(self, ts, pair, side, price, base, quote, fee, reason, mode):
        self.ts, self.pair, self.side = ts, pair, side
        self.price, self.base, self.quote = price, base, quote
        self.fee, self.reason, self.mode = fee, reason, mode

    def as_dict(self):
        return {"ts": self.ts, "time": time.strftime("%m-%d %H:%M:%S", time.localtime(self.ts)),
                "pair": self.pair, "side": self.side, "price": self.price,
                "base": self.base, "quote": self.quote, "fee": self.fee,
                "reason": self.reason, "mode": self.mode}


class Broker:
    mode = "paper"

    def __init__(self, pair: str, budget: float, risk):
        self.pair = pair
        self.budget = float(budget)      # per-pair allocation (reconcile basis)
        self.cash = float(budget)
        self.pos = Position()
        self.trades: list[Trade] = []
        self.risk = risk
        # OrdersLedger, wired by the engine (never in backtests): every
        # confirmed fill also lands in runs/orders.jsonl
        self.ledger = None
        # set when an order's outcome could not be confirmed against the
        # exchange: the local view is then unknown and must not be traded on
        self.needs_reconcile = False

    # -- interface ------------------------------------------------------
    def buy(self, notional: float, price: float, reason: str = "") -> Trade | None:
        raise NotImplementedError

    def sell(self, price: float, reason: str = "") -> Trade | None:
        raise NotImplementedError

    # -- helpers --------------------------------------------------------
    def equity(self, price: float) -> float:
        return self.cash + self.pos.base * price

    def position_notional(self, price: float) -> float:
        return self.pos.base * price


class PaperBroker(Broker):
    """Fills at the given price with slippage + taker fee (configurable)."""

    def buy(self, notional: float, price: float, reason: str = "") -> Trade | None:
        notional = min(notional, self.cash)
        if notional < self.risk.min_notional:
            return None
        px = price * (1 + self.risk.slippage)
        fee = notional * self.risk.taker_fee
        base = (notional - fee) / px
        self.cash -= notional
        self.pos.base += base
        self.pos.cost += notional
        t = Trade(time.time(), self.pair, "BUY", px, base, notional, fee, reason, "paper")
        self.trades.append(t)
        if self.ledger is not None:
            self.ledger.record(t)
        return t

    def sell(self, price: float, reason: str = "", frac: float | None = None) -> Trade | None:
        if self.pos.base <= 0:
            return None
        amount = self.pos.base if frac is None else self.pos.base * frac
        if amount * price < self.risk.min_notional:
            return None
        px = price * (1 - self.risk.slippage)
        gross = amount * px
        fee = gross * self.risk.taker_fee
        cost_part = self.pos.cost * (amount / self.pos.base)
        self.cash += gross - fee
        self.pos.base -= amount
        self.pos.cost -= cost_part
        t = Trade(time.time(), self.pair, "SELL", px, amount, gross - fee,
                  fee, reason, "paper")
        if self.pos.base < 1e-9:
            self.pos.base = 0.0
            self.pos.cost = 0.0
        self.trades.append(t)
        if self.ledger is not None:
            self.ledger.record(t)
        return t

    # -- persistence -----------------------------------------------------
    def state(self) -> dict:
        return {"cash": self.cash, "base": self.pos.base, "cost": self.pos.cost,
                "trades": [t.as_dict() for t in self.trades[-500:]]}

    def save(self, path: str):
        atomic_write_json(self.state(), path)

    def load(self, path: str) -> bool:
        s = load_json(path)
        if not s:
            return False
        try:
            self.cash = float(s["cash"]); self.pos.base = float(s["base"])
            self.pos.cost = float(s.get("cost", 0.0))
        except (KeyError, TypeError, ValueError):
            return False
        return True


class GateBroker(Broker):
    """Live spot broker on Gate.io. Market buy = quote amount, market sell = base
    amount (Gate v4 conventions). Requires keys with SPOT TRADE permission ONLY.

    Amounts are rounded to each pair's amount_precision and checked against the
    exchange minimums — otherwise Gate rejects the order (e.g. PONS base amount
    must be an INTEGER)."""

    mode = "live"

    def __init__(self, pair: str, budget: float, risk, client):
        super().__init__(pair, budget, risk)
        self.client = client
        self.base_ccy, self.quote_ccy = pair.split("_")
        self._rules = None

    def rules(self) -> dict:
        if self._rules is None:
            self._rules = self.client.pair_rules(self.pair)
        return self._rules

    def _round_base(self, amount: float) -> float:
        """FLOOR to the pair's amount precision — you can never sell more
        than you hold (round-half-up would inflate e.g. 8.654 -> 9)."""
        import math
        p = self.rules()["amount_precision"]
        return math.floor(amount * 10 ** p) / 10 ** p

    def _fmt(self, amount: float) -> str:
        p = self.rules()["amount_precision"]
        return f"{amount:.{p}f}" if p > 0 else str(int(amount))

    def _settle(self, order: dict) -> tuple:
        """Poll a market IOC order until done. Returns
        (avg_price, filled_base, filled_quote, fee, fee_currency, confirmed).

        Gate semantics: filled_amount is BASE units; filled_total is QUOTE
        actually spent/received; unfilled IOC remainder is auto-refunded;
        fee may be charged in base (buys) or quote (sells).

        confirmed=False means we never saw a terminal status (or saw zero
        fills) — the order may still have filled on the exchange, so callers
        MUST NOT book it as a zero-size trade and must treat the local view as
        unknown (2026-09-28: buy() used to record a base=0/quote=0 trade and
        report it as "executed", leaving cash and base wrong until restart).
        """
        oid = order.get("id")
        o = order or {}
        for _ in range(4):
            if o.get("status") in ("finished", "closed"):
                break
            time.sleep(0.5)
            try:
                o = self.client.order(self.pair, oid)
            except Exception:
                continue
        settled = o.get("status") in ("finished", "closed")
        px = float(o.get("avg_deal_price") or 0)
        base = float(o.get("filled_amount") or 0)
        quote = float(o.get("filled_total") or 0)
        fee = float(o.get("fee") or 0)
        ccy = o.get("fee_currency") or ""
        return px, base, quote, fee, ccy, bool(settled and base > 0)

    def buy(self, notional: float, price: float, reason: str = "") -> Trade | None:
        notional = min(notional, self.cash)
        rules = self.rules()
        notional = round(notional, rules["precision"])
        if notional < max(self.risk.min_notional, rules["min_quote_amount"]):
            return None
        o = self.client.create_market_order(self.pair, "buy", notional)
        px, base_gross, quote_spent, fee, fee_ccy, confirmed = self._settle(o)
        if not confirmed:
            # do not invent a fill: the local cash/position view is now unknown
            self.needs_reconcile = True
            return None
        px = px or price
        base_net = base_gross - (fee if fee_ccy == self.base_ccy else 0.0)
        fee_quote = quote_spent * self.risk.taker_fee   # local estimate for records
        self.cash -= quote_spent                        # IOC remainder auto-refunded
        self.pos.base += base_net
        self.pos.cost += quote_spent
        t = Trade(time.time(), self.pair, "BUY", px, base_net, quote_spent,
                  fee_quote, reason, "live")
        self.trades.append(t)
        if self.ledger is not None:
            self.ledger.record(t)
        return t

    def sell(self, price: float, reason: str = "", frac: float | None = None) -> Trade | None:
        if self.pos.base <= 0:
            return None
        rules = self.rules()
        amount = self._round_base(self.pos.base if frac is None
                                  else self.pos.base * frac)
        # exits only need to clear the EXCHANGE minimum (risk.min_notional
        # applies to entries, never blocks risk reduction)
        if amount * price < rules["min_quote_amount"] or amount < rules["min_base_amount"]:
            return None
        o = self.client.create_market_order(self.pair, "sell", amount)
        px, base_sold, quote_gross, fee, fee_ccy, confirmed = self._settle(o)
        if not confirmed:
            self.needs_reconcile = True
            return None
        px = px or price
        fee_quote = fee if fee_ccy == self.quote_ccy else 0.0
        received = quote_gross - fee_quote
        self.cash += received
        t = Trade(time.time(), self.pair, "SELL", px, base_sold, received,
                  fee_quote or quote_gross * self.risk.taker_fee, reason, "live")
        self.pos.base = max(0.0, self.pos.base - base_sold)   # dust may remain
        self.pos.cost = 0.0 if self.pos.base <= 0 else self.pos.cost * max(0.0, 1 - base_sold / (base_sold + self.pos.base))
        self.trades.append(t)
        if self.ledger is not None:
            self.ledger.record(t)
        return t

    # -- reconciliation ----------------------------------------------------
    def reconcile(self, price: float) -> bool:
        """Rebuild cash/position from the exchange, the only authoritative
        source. `price` is used for the position valuation that the per-pair
        cash reserve derives from (same rule as Engine.__init__: a pair may
        only spend its own allocation minus what its position already uses).

        Returns True when a fresh view was obtained; False leaves the caller
        responsible for halting instead of trading on stale numbers."""
        try:
            bals = {b["currency"]: float(b["available"])
                    for b in self.client.balances()}
        except Exception:
            return False
        base_amt = float(bals.get(self.base_ccy, 0.0))
        self.pos.base = base_amt
        self.cash = max(0.0, self.budget - base_amt * max(price, 0.0))
        if base_amt <= 0:
            self.pos.cost = 0.0
        self.needs_reconcile = False
        return True

    # -- persistence (audit trail only; balances always re-sync from exchange) -
    def state(self) -> dict:
        return {"cash": self.cash, "base": self.pos.base, "cost": self.pos.cost,
                "trades": [t.as_dict() for t in self.trades[-500:]]}

    def save(self, path: str):
        atomic_write_json(self.state(), path)

    def load(self, path: str) -> bool:
        s = load_json(path)
        if not s:
            return False
        try:
            self.trades = [Trade(**{k: v for k, v in t.items() if k != "time"})
                           for t in s.get("trades", [])]
        except TypeError:
            return False
        return True
