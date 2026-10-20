"""Proactive exit management — the sell-side upgrade.

The original system's exits are all REACTIVE (wait for the downtrend staircase,
wait for a fixed 4% give-back). This module adds the missing proactive exits:

  1. scale-out : lock half the position at +N% unrealized (cost recovery)
  2. fatigue   : the uptrend's coherence (r2) collapsed after being strong ->
                 exit half BEFORE the downtrend is confirmed (fly-native: the
                 brain already computes r2 every minute)
  3. adaptive trail : trailing distance = k x recent 60-candle range, clamped,
                 instead of one fixed % for every coin

All exits REDUCE risk; none are ever blocked by entry caps.
"""
from __future__ import annotations

import numpy as np


class ExitManager:
    def __init__(self, risk_cfg):
        self.cfg = risk_cfg
        self.reset()

    def reset(self):
        self.scaled_out = False
        self.r2_high = 0.0

    # ------------------------------------------------------------------
    def trail_distance(self, candles: list, n: int = 60) -> float:
        """Adaptive trailing distance: k x last-n-candle range, clamped."""
        last = candles[-n:] if candles else []
        if len(last) < 10:
            return self.cfg.trailing_stop
        hi = max(c[2] for c in last)
        lo = min(c[3] for c in last)
        px = last[-1][4]
        if px <= 0:
            return self.cfg.trailing_stop
        rng = (hi - lo) / px
        return float(np.clip(self.cfg.atr_trail_mult * rng,
                             self.cfg.atr_trail_min, self.cfg.atr_trail_max))

    # ------------------------------------------------------------------
    def check(self, price: float, avg_cost: float, r2_now: float | None):
        """Return a reason string for a HALF exit, or None.
        Does NOT latch anything — the caller must confirm() after the sell
        actually fills, otherwise a failed order would swallow the exit."""
        if avg_cost is None or avg_cost <= 0:
            return None
        if r2_now is not None and r2_now > self.r2_high:
            self.r2_high = r2_now

        # (1) scale-out: lock half once unrealized gain reaches the threshold
        if not self.scaled_out and price / avg_cost - 1 >= self.cfg.scale_out_gain:
            return f"scale-out@+{self.cfg.scale_out_gain:.0%}"

        # (2) trend fatigue: coherence collapsed after being confirmed
        if (self.r2_high >= self.cfg.fatigue_r2_high
                and r2_now is not None
                and r2_now < self.cfg.fatigue_r2_drop):
            return "trend-fatigue"

        return None

    def confirm(self, reason: str):
        """Latch the exit AFTER the sell succeeded."""
        if reason and reason.startswith("scale-out"):
            self.scaled_out = True
        elif reason == "trend-fatigue":
            self.r2_high = 0.0          # re-arm only after coherence recovers


# ------------------------------------------------------------------
class PersistRatchet:
    """Confirmation-gated profit lock (2026-09-25: the '+8U gave back' gap).

    The deployed floor ladder only arms at peak >= +3%; the adaptive trail
    anchors k x 60-candle-range below the extreme (~6% on a volatile day).
    A campaign that reaches +2.1% and stalls can therefore round-trip to a
    double-digit-U loss before ANY floor exists — live 09-25: peak +8.1U
    unrealized, trail sat ~5.8% below the extreme the whole time, first
    ladder rung never armed.

    The ratchet closes that gap WITHOUT touching the noise band: unrealized
    pnl (candle-close based) must stay >= `arm` for `t` CONSECUTIVE candles
    — persistence, not a wick — after which a floor at entry*(1 +/- lock)
    stays armed for the rest of the campaign.

    Relationship to swept-and-rejected variants (floor_tp_sweep: floor 2%
    bought win rate 57% with return 91.9 -> 59): those arm INSTANTLY on any
    wick and get triggered by 5m noise. This one is the persistence-gated
    version of the classic breakeven trigger (RiskConfig.breakeven_trigger
    /buffer are exactly arm/lock with t=0) — noise does not latch it, and a
    real trend runs through the +0.2% floor long before it matters.

    Single implementation shared by backtest (run_dual persist= kwarg) and
    live (FuturesRunner) so the two can never drift.
    """

    def __init__(self, arm: float = 0.015, t: int = 12, lock: float = 0.002):
        self.arm = float(arm)
        self.t = int(t)
        self.lock = float(lock)
        self.reset()

    def reset(self):
        self.count = 0
        self.latched = False

    def step(self, pnl_pct) -> None:
        """One call per candle while positioned; pnl_pct is close-based
        (px/entry - 1 for longs, entry/px - 1 for shorts)."""
        if self.latched or pnl_pct is None:
            return
        self.count = self.count + 1 if pnl_pct >= self.arm else 0
        if self.count >= self.t:
            self.latched = True

    def floor(self, entry: float, long: bool = True):
        """Armed floor price, or None while unlatched."""
        if not self.latched or not entry:
            return None
        return entry * (1 + self.lock) if long else entry * (1 - self.lock)

    # ------------------------------------------------------------ persist
    def as_dict(self) -> dict:
        return {"count": self.count, "latched": self.latched}

    def load_dict(self, d) -> None:
        d = d or {}
        self.count = int(d.get("count", 0) or 0)
        self.latched = bool(d.get("latched", False))
