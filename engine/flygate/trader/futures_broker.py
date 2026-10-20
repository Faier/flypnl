"""Futures broker for Gate USDT-margined perpetuals (net position mode).

Contract semantics (different from spot):
  - sizes are integer CONTRACTS; value = size x quanto_multiplier x price
  - a market BUY while short covers the short first (net mode) — flips are
    just "order the delta to the target position"
  - leverage is set per contract once (2x here); liquidation ~50% adverse,
    stops sit far inside
  - futures margin lives in a SEPARATE account (manual transfer from spot)

Before any RISK-INCREASING order this broker refuses to trade unless
  (a) the configured quanto matches the contract's real quanto_multiplier
      (verify_contract — mismatch = refuse, unreachable endpoint = loud warning
      and continue, see the docstring there for why the asymmetry is
      deliberate), and
  (b) the leverage could be SET and READ BACK, and its implied liquidation
      distance is farther out than the StopGuard trigger (_ensure_leverage).
Every size, notional, PnL, fee and the daily-loss halt derive from self.quanto,
so a stale multiplier silently scales the real position while risk keeps
measuring the old units (2026-09-15 audit).

Paper mode simulates fills at candle price with fee+slip, no API needed.
"""
from __future__ import annotations

import time

from ..data.gate_client import GateError
from .broker import Trade
from .stopguard import DEFAULT_DIST

LEVERAGE = 2               # intended per-contract leverage (isolated, 1-way)
# [INFERENCE] Gate USDT-perp maintenance-margin rate at the lowest notional
# tier; used to keep the liquidation-distance check on the conservative side.
# It is deliberately NOT 0: liquidation always lands a little closer than the
# naive 1/leverage, and the brake must still be inside it.
MAINT_MARGIN_RATE = 0.005


class QuantoMismatch(GateError):
    """The configured contract multiplier disagrees with the exchange's."""


class FuturesBroker:
    def __init__(self, contract: str, budget: float, risk_cfg, client,
                 quanto: float, paper: bool = True, fee: float = 0.0005,
                 leverage: int = LEVERAGE, guard_dist: float = DEFAULT_DIST):
        self.pair = contract
        self.contract = contract
        self.paper = paper
        self.client = client
        self.quanto = quanto          # BTC per contract
        self.risk = risk_cfg
        self.fee = float(fee)         # taker is per-contract on Gate (ZEC 0.05%, PONS 0.075%)
        self.slip = 0.0003
        self.cash = float(budget)     # strategy margin allocation
        self.size = 0                 # signed contracts held
        self.entry = 0.0              # avg entry price
        self.trades: list[Trade] = []
        self.leverage = max(1, int(leverage))   # the ONLY leverage we may hold
        self.guard_dist = float(guard_dist)     # StopGuard trigger distance
        self._leverage_ok = False     # leverage set AND read back AND checked
        self._contract_ok = False     # quanto confirmed against the exchange
        self._warned_at = 0.0         # rate-limit for the unreachable warning
        self.trade_blocked = ""       # non-empty -> refuse to OPEN (sticky)
        self.last_refusal = ""        # why the last order attempt was refused
        self.sync_dirty = False       # last post-order sync failed -> runner re-syncs
        # time-limited OPEN freeze after an exchange-side margin rejection
        # (auto-expires; 0.0 = clear). Sticky trade_blocked stays reserved for
        # the quanto mismatch, which only a config fix may lift.
        self.margin_backoff_until = 0.0
        self._margin_warned_at = 0.0  # one prompt per backoff window

    # ------------------------------------------------------------------ api
    def verify_contract(self) -> None:
        """Cross-check self.quanto against the contract's quanto_multiplier.

        ASYMMETRY (deliberate, and the reason this cannot just fail-closed):
          - the endpoint ANSWERS and disagrees  -> QuantoMismatch; refuse to
            trade. Every size/PnL/fee/halt number in this broker is derived
            from the multiplier, so the "risk" being managed would not be the
            risk actually on the exchange.
          - the endpoint is UNREACHABLE (or answers without the field) -> log
            a prominent warning and CONTINUE. A DNS hiccup or a Gate outage
            must not stop the process from flattening an open position, and the
            multiplier cannot be checked offline by definition.
        Cheap and sticky: one successful check per process (self._contract_ok),
        a confirmed mismatch is cached in self.trade_blocked, and a failing
        endpoint is retried on the next attempt but warns at most once
        every _warn's interval (600s), so an outage cannot spam the console."""
        if self._contract_ok:
            return
        if self.trade_blocked:
            raise QuantoMismatch(self.trade_blocked)
        try:
            d = self.client._request(
                "GET", f"/futures/usdt/contracts/{self.contract}")
        except Exception as e:
            self._warn(f"cannot read {self.contract} contract rules ({e!s:.100})"
                       f" — proceeding on config quanto={self.quanto}")
            return
        raw = (d or {}).get("quanto_multiplier")
        if raw is None:
            self._warn(f"/futures/usdt/contracts/{self.contract} answered "
                       f"without quanto_multiplier — cannot verify config "
                       f"quanto={self.quanto}")
            return
        try:
            exch = float(raw)
        except (TypeError, ValueError):
            self._warn(f"unparsable quanto_multiplier {raw!r} for "
                       f"{self.contract} — cannot verify config "
                       f"quanto={self.quanto}")
            return
        if abs(exch - self.quanto) > 1e-9 * max(1.0, abs(exch)):
            self.trade_blocked = (
                f"quanto MISMATCH for {self.contract}: config quanto="
                f"{self.quanto} but exchange quanto_multiplier={exch} — "
                f"every size/PnL/fee/halt would be measured in the wrong units, "
                f"refusing to trade (fix config.json)")
            raise QuantoMismatch(self.trade_blocked)
        self._contract_ok = True

    def _warn(self, msg: str, every_s: float = 600.0):
        now = time.time()
        if now - self._warned_at < every_s:
            return
        self._warned_at = now
        print(f"[fut] WARNING: {msg}", flush=True)

    def _set_leverage(self, lev: int | None = None) -> dict:
        """POST the per-contract leverage. Returns the exchange's reply.

        [INFERENCE: Gate answers this endpoint with the updated POSITION record,
        which carries "leverage" — that reply is the primary read-back source in
        _reported_leverage.] The endpoint takes params via QUERY string, not
        JSON body. Failures are NOT swallowed any more: an order must never
        leave the machine at an unknown leverage (2026-09-15 audit)."""
        lev = self.leverage if lev is None else max(1, int(lev))
        return self.client._request(
            "POST", f"/futures/usdt/positions/{self.contract}/leverage",
            query={"leverage": lev}, auth=True)

    @staticmethod
    def _leverage_of(row) -> int | None:
        """Leverage from one API record, or None when it does not say.
        [INFERENCE: Gate reports "leverage" as a string on position records and
        uses "0" for CROSS margin, where no per-contract leverage exists — that
        is reported as "unknown", and unknown means refuse.]"""
        if not isinstance(row, dict):
            return None
        try:
            v = int(float(row.get("leverage")))
        except (TypeError, ValueError):
            return None
        return v if v > 0 else None

    def _reported_leverage(self, resp=None) -> int | None:
        """Leverage the EXCHANGE says is in force, from the first source that
        answers: the leverage POST reply, the positions list (strict contract
        match) and finally the single-position endpoint."""
        v = self._leverage_of(resp)
        if v is not None:
            return v
        try:
            v = self._leverage_of(self._my_position() or {})
        except Exception:
            v = None
        if v is not None:
            return v
        try:
            row = self.client._request(
                "GET", f"/futures/usdt/positions/{self.contract}", auth=True)
        except Exception:
            return None
        return self._leverage_of(row or {})

    def _liq_outside_stop(self, lev: int) -> tuple[bool, str]:
        """Is the StopGuard trigger inside the liquidation distance?

        At leverage L an isolated position is liquidated after about 1/L of
        adverse move (minus the maintenance margin); the disaster brake sits at
        extreme*(1 - guard_dist). With dist >= 1/L - mm the trigger is at or
        beyond liquidation, i.e. the position gets liquidated before the brake
        can ever fire — refuse to open. Measured from ENTRY, the worst case
        (a favorable extreme only makes the trigger sit further inside)."""
        dist = self.guard_dist
        if dist <= 0:
            return True, ""
        frac = 1.0 / max(1, int(lev)) - MAINT_MARGIN_RATE
        if frac <= dist:
            return False, (
                f"StopGuard trigger {dist * 100:.2f}% from the extreme would sit "
                f"at/beyond the {lev}x liquidation distance {frac * 100:.2f}% "
                f"— the disaster brake could never fire, refusing to open")
        return True, ""

    def _ensure_leverage(self) -> tuple[bool, str]:
        """Set + VERIFY the leverage, and sanity-check it against the trigger.

        Returns (ok, reason); (False, why) means: do NOT send the order. The
        single check that used to be a bare print is now a hard gate — a
        position opened at a leverage other than self.leverage can be
        liquidated inside the StopGuard trigger, so the disaster brake would be
        dead exactly when it is needed (2026-09-15 audit)."""
        if self.paper:
            return True, ""
        if self._leverage_ok:
            return True, ""
        try:
            resp = self._set_leverage()
        except Exception as e:
            return False, (f"leverage POST failed ({e!s:.100}) — leverage "
                           f"unknown, order not sent")
        got = self._reported_leverage(resp)
        if got is None:
            return False, (f"leverage read-back unavailable for {self.contract} "
                           f"(leverage reply, positions list and position "
                           f"endpoint all silent) — cannot confirm "
                           f"{self.leverage}x, order not sent")
        if got != self.leverage:
            return False, (f"leverage read-back {got}x != intended "
                           f"{self.leverage}x on {self.contract} — order not sent")
        ok, why = self._liq_outside_stop(got)
        if not ok:
            return False, why
        self._leverage_ok = True
        return True, ""

    def _preflight(self, reduce_only: bool) -> tuple[bool, str]:
        """Everything that must hold before an order may leave the machine.

        A risk-REDUCING order (reduce_only) is never blocked by these checks: a
        quanto mismatch or an unreadable leverage only mis-prices our books, and
        refusing to flatten would leave the position open — the exact failure
        mode this audit is about. The mismatch is reported loudly instead."""
        try:
            self.verify_contract()
        except QuantoMismatch as e:
            if not reduce_only:
                return False, str(e)
            print(f"[fut] WARNING: {e} — allowing reduce-only exit", flush=True)
        if reduce_only:
            return True, ""
        if self.margin_backoff_until > time.time():
            why = ("margin-backoff: the exchange rejected a previous order on "
                   "insufficient available margin — entries stay frozen "
                   f"{int(self.margin_backoff_until - time.time())}s "
                   "(reduce-only exits are unaffected)")
            return False, why
        return self._ensure_leverage()

    def _public_ticker(self) -> dict:
        return self.client._request(
            "GET", "/futures/usdt/tickers",
            query={"contract": self.contract})[0]

    def price(self) -> float:
        return float(self._public_ticker()["last"])

    def contracts_for(self, notional: float, px: float) -> int:
        return max(0, int(notional / (self.quanto * px)))

    # ---------------------------------------------------------------- orders
    def _market(self, size: int, reason: str, px_ref: float,
                reduce_only: bool = False) -> Trade | None:
        """size signed: +buy / -sell contracts. reduce_only lets the exchange
        enforce "this order may only shrink the position" — a flatten against
        a position that is already gone is REJECTED instead of opening a
        reverse position."""
        if size == 0:
            return None
        ok, why = self._preflight(reduce_only)
        self.last_refusal = "" if ok else why
        if not ok:
            print(f"[fut] {self.contract} order REFUSED: {why}", flush=True)
            return None
        if self.paper:
            direction = 1 if size > 0 else -1
            px = px_ref * (1 + direction * self.slip)
            notional = abs(size) * self.quanto * px
            fee = notional * self.fee
            # net against existing position
            prev_size = self.size
            closing = min(abs(size), abs(prev_size)) if prev_size * size < 0 else 0
            pnl = 0.0
            if closing:
                pnl = closing * self.quanto * (px - self.entry) * (1 if prev_size > 0 else -1)
            self.cash += pnl - fee
            new_size = prev_size + size
            opened = abs(size) - closing
            if opened > 0:
                if prev_size == 0 or prev_size * size > 0:
                    total_q = abs(self.size) * self.quanto + opened * self.quanto
                    old_q = abs(self.size) * self.quanto
                    self.entry = (self.entry * old_q + px * opened * self.quanto) / total_q if total_q else px
                else:
                    self.entry = px
            self.size = new_size
            side = "BUY" if size > 0 else "SELL"
            t = Trade(time.time(), f"FUT:{self.contract}", side, px,
                      abs(size) * self.quanto, notional, fee, reason,
                      "fut-paper" if self.paper else "fut-live")
            self.trades.append(t)
            return t
        # ---- live: net-mode market order, then sync from the exchange
        # (position endpoint is the authority for size AND entry price).
        # The leverage was already SET and READ BACK by _preflight above —
        # nothing may reach the order endpoint without it.
        # A margin rejection (INSUFFICIENT_*) must NOT propagate: it used to
        # surface in the runner's generic handler and get re-fired EVERY
        # candle (2026-09-29 ZEC: 10 posts in ~2h, margin 24 > available 0).
        # Freeze risk-INCREASING orders for 30 min instead — reduce-only
        # exits are unaffected and the window self-heals. Unknown failures
        # keep the loud path so bugs stay visible.
        body = {"contract": self.contract, "size": size, "price": "0",
                "tif": "ioc", "reduce_only": bool(reduce_only)}
        try:
            o = self.client._request(
                "POST", "/futures/usdt/orders", body=body, auth=True)
        except Exception as e:
            msg = str(e)
            now = time.time()
            if "INSUFFICIENT" in msg.upper() or "MARGIN" in msg.upper():
                self.margin_backoff_until = now + 1800
                if now - self._margin_warned_at > 1800:
                    self._margin_warned_at = now
                    print(f"[fut] WARNING: order rejected on margin — entries"
                          f" frozen 30min: {msg[:100]}", flush=True)
                self.last_refusal = msg
                return None
            raise          # broker outage / auth / unknown: keep raising
        time.sleep(0.6)
        px = px_ref
        try:
            o = self.client._request(
                "GET", f"/futures/usdt/orders/{o['id']}",
                query={"contract": self.contract}, auth=True)
            px = float(o.get("fill_price") or 0) or px_ref
        except Exception:
            pass
        # ---- book by the position DELTA (actual filled size), never by the
        # requested size: an IOC market order can partial-fill or be rejected
        # outright, and realized PnL of externally closed size belongs to the
        # exchange anyway (2026-09-15 audit: closing 10 filled 4 was booked
        # as a full 10-lot win, corrupting the daily halt and dopamine)
        try:
            mine = self._my_position()
            prev = self.size
            new_size = int(mine.get("size") or 0) if mine else 0
            filled = new_size - prev             # signed net effect of this order
            closing = min(abs(filled), abs(prev)) if prev * filled < 0 else 0
            if closing:
                pnl = closing * self.quanto * (px - self.entry) * (1 if prev > 0 else -1)
                self.cash += pnl
            self.cash -= abs(filled) * self.quanto * px * self.fee
            self.size = new_size
            self.entry = float(mine.get("entry_price") or 0) if mine else 0.0
            if filled == 0:
                return None                      # rejected / nothing filled
        except Exception as e:
            print(f"[fut] post-order sync failed: {e}", flush=True)
            self.sync_dirty = True
            return None
        side = "BUY" if size > 0 else "SELL"
        t = Trade(time.time(), f"FUT:{self.contract}", side, px,
                  abs(filled) * self.quanto, abs(filled) * self.quanto * px,
                  abs(filled) * self.quanto * px * self.fee, reason, "fut-live")
        self.trades.append(t)
        return t

    def _my_position(self) -> dict | None:
        """The position endpoint IGNORES the contract filter (returns all
        contracts) — match STRICTLY by name and never fall back to rows[0]:
        another contract's record must not be imported as ours (2026-09-15
        audit probe: asking for ZEC returned BTC's 500-lot position). No match
        means the exchange says we are flat on this contract."""
        rows = self.client._request(
            "GET", "/futures/usdt/positions",
            query={"contract": self.contract}, auth=True) or []
        mine = [r for r in rows if r.get("contract") == self.contract]
        return mine[0] if mine else None

    # convenience ------------------------------------------------------
    def open_long(self, contracts: int, px: float, reason=""):
        return self._market(+contracts, reason, px)

    def open_short(self, contracts: int, px: float, reason=""):
        return self._market(-contracts, reason, px)

    def close_all(self, px: float, reason=""):
        """Flatten with reduce_only: if the position is already gone (manual
        close on the app, stop trigger, liquidation), the exchange rejects
        the order instead of letting it open a REVERSE position."""
        if self.size == 0:
            return None
        return self._market(-self.size, reason, px, reduce_only=True)

    def close_half(self, px: float, reason=""):
        half = int(abs(self.size) / 2)
        if half == 0:
            return None
        return self._market(-half if self.size > 0 else half, reason, px,
                            reduce_only=True)

    # ---------------------------------------------------------------- state
    def position_notional(self, px: float) -> float:
        return abs(self.size) * self.quanto * px

    def unrealized(self, px: float) -> float:
        if self.size == 0:
            return 0.0
        return self.size * self.quanto * (px - self.entry)

    def equity(self, px: float) -> float:
        return self.cash + self.unrealized(px)

    def sync_position(self):
        """Runtime re-sync of size/entry from the exchange (the authority)
        WITHOUT touching local cash — catches manual closes on the app, stop
        fills and liquidations so the local model can never act on a stale
        position (e.g. flatten into an already-flat account and open a
        reverse one). Absent record = flat on this contract."""
        pos = self._my_position()
        if pos:
            self.size = int(pos.get("size") or 0)
            self.entry = float(pos.get("entry_price") or 0)
        else:
            self.size, self.entry = 0, 0.0
        self.sync_dirty = False

    def sync_live(self):
        """Pull real position/margin (authority = exchange)."""
        if self.paper:
            return
        self.sync_position()
        acc = self.client._request("GET", "/futures/usdt/accounts", auth=True)
        avail = float(acc.get("available") or 0)
        # cap strategy margin at our budget
        self.cash = min(avail, self.cash if self.cash > 0 else avail)
