"""Exchange-side disaster brake for futures: one price-triggered close order
kept live per contract (spot NOT covered — /spot/price_orders rejects this
account with INVALID_PARAM put.account on every variant tried, and a resting
spot limit sell would freeze the base balance and block the engine's own
market exits; spot is 1x with a 40U cap, so it stays runner-protected only).

The runner's own trailing stop (3-10%, candle-close based) always fires first
when online; this trigger sits beyond it at extreme*(1±dist) so it only
executes when the process / network / machine is DEAD. Refreshed every
candle, re-placed well before its 24h expiration (a trigger whose watermark
hasn't moved for a day used to silently expire), reconciled at startup so a
stale trigger can never outlive its position. close=True means it can only
flatten, never open. Only orders carrying OUR text tag are ever canceled —
the account owner's manual triggers are not ours to delete.

INVARIANT (enforced by FuturesBroker, not here): the trigger must sit INSIDE
the position's liquidation distance. At leverage L an isolated position is
liquidated after roughly 1/L of adverse move (minus maintenance margin), so a
position opened at a leverage whose liquidation is CLOSER than `dist` would
liquidate before this brake could fire. FuturesBroker therefore refuses to
open unless 1/L - maintenance > dist (see FuturesBroker._liq_outside_stop).
"""
from __future__ import annotations

import time

RENEW_AFTER_S = 20 * 3600        # re-place before the 24h expiration lapses
GUARD_TEXT = "t-flygate-guard"
DEFAULT_DIST = 0.10              # disaster distance; mirrors atr_trail_max


class StopGuard:
    def __init__(self, client, log=None, dist: float = DEFAULT_DIST):
        self.client = client
        self.log = log
        self.dist = dist          # disaster distance beyond the extreme watermark
        self._id = None           # open trigger order id (ours)
        self._last = None         # (contract, rule, price) currently live
        self._step = None         # order_price_round of the contract
        self._placed_at = 0.0     # when the live trigger was created

    def _now(self) -> float:
        return time.time()

    # ------------------------------------------------------------------
    def _price_step(self, contract: str) -> float:
        if self._step is None:
            try:
                d = self.client._request("GET", f"/futures/usdt/contracts/{contract}")
                self._step = float(d.get("order_price_round") or 0.1)
            except Exception:
                self._step = 0.1
        return self._step

    def _w(self, cat, event, **kw):
        if self.log:
            self.log.warn(cat, event, **kw)

    # ------------------------------------------------------------------
    def cancel(self):
        if self._id is not None:
            try:
                self.client._request(
                    "DELETE", f"/futures/usdt/price_orders/{self._id}", auth=True)
                self._id = None
            except Exception as e:
                # keep _id: the order may still be live on the exchange —
                # the next refresh retries the cancel instead of forgetting
                # it and stacking a second trigger
                self._w("GUARD", "cancel_failed", err=str(e)[:80])
        self._last = None

    def reconcile(self, contract: str):
        """Startup: cancel THIS STRATEGY's triggers on the contract (matched
        by the t-flygate-guard text tag) — a stale trigger must not outlive
        its position. Orders placed manually by the account owner are NOT
        ours and are left alone."""
        try:
            rows = self.client._request(
                "GET", "/futures/usdt/price_orders",
                query={"status": "open", "contract": contract}, auth=True) or []
            for o in rows:
                init = o.get("initial", {})
                if (init.get("contract") == contract
                        and init.get("text") == GUARD_TEXT):
                    try:
                        self.client._request(
                            "DELETE", f"/futures/usdt/price_orders/{o['id']}", auth=True)
                    except Exception:
                        pass
        except Exception as e:
            self._w("GUARD", "reconcile_failed", err=str(e)[:80])
        self._id = None
        self._last = None

    # ------------------------------------------------------------------
    def refresh(self, contract: str, size: int, extreme: float):
        """size = signed contracts; extreme = favorable watermark (high for
        long / low for short). Flat position -> just cancel."""
        try:
            if size == 0 or extreme <= 0:
                if self._id is not None:
                    self.cancel()
                return
            step = self._price_step(contract)
            if size > 0:    # long: close when price drops to high*(1-dist)
                rule, price = 2, extreme * (1 - self.dist)
            else:           # short: close when price rises to low*(1+dist)
                rule, price = 1, extreme * (1 + self.dist)
            price = max(step, round(price / step) * step)
            want = (contract, rule, price)
            # unchanged AND young -> nothing to do. The trigger expires after
            # 24h; once it ages past RENEW_AFTER_S we re-place it even if the
            # target hasn't moved, so a parked watermark can't go naked
            # (2026-09-15 audit: same-target refresh made 0 requests for
            # 86401 simulated seconds while the exchange order had lapsed)
            if (want == self._last and self._id is not None
                    and self._now() - self._placed_at < RENEW_AFTER_S):
                return
            if self._id is not None:
                self.cancel()
                if self._id is not None:     # cancel failed — retry next candle
                    return
            o = self.client._request(
                "POST", "/futures/usdt/price_orders", body={
                    "initial": {"contract": contract, "size": 0, "price": "0",
                                "close": True, "tif": "ioc", "text": GUARD_TEXT},
                    "trigger": {"rule": rule, "price": f"{price:.10g}",
                                "expiration": 86400}}, auth=True)
            self._id = o.get("id")
            self._last = want
            self._placed_at = self._now()
            if self.log:
                self.log.info("GUARD", "stop_set", contract=contract,
                              rule=">=" if rule == 1 else "<=",
                              price=price, expiration="24h",
                              mode="disaster-brake")
        except Exception as e:
            self._last = None      # force a fresh place on the next refresh
            self._w("GUARD", "refresh_failed", err=str(e)[:80])
