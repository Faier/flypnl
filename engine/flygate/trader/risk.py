"""Risk guards — the layer StonkFly is weakest at. Every order proposal passes
through RiskManager.allow() BEFORE reaching a broker, and the manager can also
FORCE-LIQUIDATE (StonkFly's drawdown halt stops new orders but never closes).

Guards:
  * order notional / position / daily-order-count / cooldown caps
  * per-day loss halt  -> close everything, halt
  * peak drawdown halt -> close everything, halt
  * kill-switch file (runs/KILL) or dashboard button -> close everything, halt

All timestamps are injectable (`now`) so the SAME code runs live (wall clock)
and in backtests (candle timestamps).
"""
from __future__ import annotations

import math
import os
import time

import numpy as np

from ..storage import atomic_write_json, load_json


def reflex_exit(prev_close: float, price: float, pos_high: float,
                flash_drop: float, trailing: float) -> str | None:
    """Giant-fiber escape reflex: in flies, looming visual stimuli trigger a
    jump through a pathway that BYPASSES the brain. Here, a single crashing
    candle or a sharp drop from the position's high-watermark exits immediately
    without waiting for the brain's slow staircase readout to confirm."""
    if prev_close and prev_close > 0 and price / prev_close - 1 <= -flash_drop:
        return "flash-crash"
    if pos_high and pos_high > 0 and price / pos_high - 1 <= -trailing:
        return "trailing-stop"
    return None


def entry_notional(candles, risk_cfg, base_notional: float,
                   bar_seconds: float) -> float:
    """Volatility-scaled entry notional: putting the same 10U into PONS
    (annual vol >400%) and BTC is a sizing bug, not a strategy. Scale
    `base_notional` so each position targets `target_daily_vol`, linear in
    realized vol of the last `vol_lookback` closes, clamped both ways, never
    below the exchange minimum. Returns `base_notional` unchanged when
    `vol_target_sizing` is off — the legacy flat behavior."""
    scale = 1.0
    if getattr(risk_cfg, "vol_target_sizing", False):
        n = max(int(getattr(risk_cfg, "vol_lookback", 60)), 10)
        if candles and len(candles) > n:
            closes = np.asarray([float(c[4]) for c in candles[-(n + 1):]])
            if (closes > 0).all():
                rets = np.diff(np.log(closes))
                vol_bar = float(rets.std(ddof=1))
                if vol_bar > 1e-12:
                    target_bar = (float(risk_cfg.target_daily_vol)
                                  * math.sqrt(bar_seconds / 86400.0))
                    scale = min(max(target_bar / vol_bar,
                                    float(risk_cfg.vol_scale_min)),
                                float(risk_cfg.vol_scale_max))
    return max(base_notional * scale, float(risk_cfg.min_notional))


class RiskManager:
    def __init__(self, risk_cfg, kill_file: str, budget_per_pair: float,
                 backtest: bool = False):
        self.cfg = risk_cfg
        self.kill_file = kill_file
        self.budget = budget_per_pair
        self.backtest = backtest
        self.halted = False
        self.halt_reason = ""
        self.day = ""
        self.orders_today: dict[str, int] = {}
        self.last_order_ts: dict[str, float] = {}
        self.day_start_equity: float | None = None
        self.peak_equity: float | None = None
        # False while a capital card (e.g. the futures runner) hasn't reported
        # yet: anchoring the day on a PARTIAL total silently disarms the
        # daily-loss halt (2026-09-14 live: anchor locked ~30U low, so the 8U
        # halt would have needed a -38U real loss to fire)
        self.anchor_ready: bool = True

    # ------------------------------------------------------------------
    def _roll_day(self, now: float):
        today = time.strftime("%Y-%m-%d", time.localtime(now))
        if today != self.day:
            self.day = today
            self.orders_today.clear()
            self.day_start_equity = None

    def kill_triggered(self) -> bool:
        return not self.backtest and os.path.exists(self.kill_file)

    def update(self, total_equity: float, now: float | None = None,
               ready: bool | None = None) -> str:
        """Track daily anchor & peak; returns halt reason or ''.
        ready=False (default self.anchor_ready) leaves the day anchor unset —
        the daily-loss halt must not arm against a partial total."""
        now = now if now is not None else time.time()
        if ready is None:
            ready = self.anchor_ready
        self._roll_day(now)
        if self.day_start_equity is None and ready:
            self.day_start_equity = total_equity
        if self.peak_equity is None or total_equity > self.peak_equity:
            self.peak_equity = total_equity
        if self.kill_triggered():
            return "kill switch"
        if (self.day_start_equity is not None
                and self.day_start_equity - total_equity >= self.cfg.daily_loss_halt):
            return f"daily loss {self.cfg.daily_loss_halt:g}"
        if self.peak_equity and total_equity < self.peak_equity * (1 - self.cfg.peak_drawdown_halt_pct / 100):
            return f"drawdown {self.cfg.peak_drawdown_halt_pct:g}%"
        return ""

    def halt(self, reason: str):
        if not self.halted:
            self.halted = True
            self.halt_reason = reason
            if not self.backtest:
                try:
                    with open(self.kill_file, "w") as f:
                        f.write(f"halted {time.strftime('%F %T')} {reason}\n")
                except OSError:
                    pass

    def resume(self, total_equity: float):
        self.halted = False
        self.halt_reason = ""
        self.day_start_equity = total_equity
        self.peak_equity = total_equity
        if os.path.exists(self.kill_file):
            os.remove(self.kill_file)

    # ------------------------------------------------------------------
    def allow(self, pair: str, side: str, position_notional: float,
              price: float, equity: float, now: float | None = None) -> tuple[bool, str]:
        now = now if now is not None else time.time()
        if self.halted:
            return False, f"halted: {self.halt_reason}"
        reason = self.update(equity, now)
        if reason:
            self.halt(reason)
            return False, f"halted: {reason}"
        if side == "SELL":
            # exits REDUCE risk: never blocked by entry caps/cooldown (a stuck
            # position through a downtrend is the worst failure mode)
            return True, "ok"
        cap = self.cfg.position_caps.get(pair, self.cfg.max_position_notional)
        n = self.orders_today.get(pair, 0)
        if self.cfg.max_orders_per_day > 0 and n >= self.cfg.max_orders_per_day:
            return False, f"daily order cap ({self.cfg.max_orders_per_day})"
        if now - self.last_order_ts.get(pair, 0) < self.cfg.cooldown_s:
            return False, "cooldown"
        if position_notional + self.cfg.order_notional > cap:
            return False, "position cap"
        if self.cfg.order_notional < self.cfg.min_notional:
            return False, "below exchange minimum"
        return True, "ok"

    def record_order(self, pair: str, now: float | None = None):
        now = now if now is not None else time.time()
        self.orders_today[pair] = self.orders_today.get(pair, 0) + 1
        self.last_order_ts[pair] = now

    # ------------------------------------------------------------------
    def state(self) -> dict:
        """Everything that must survive a restart. These counters used to live
        in RAM only: losing them cleared the daily order cap and the cooldown
        and re-anchored the daily-loss/peak-drawdown halts at the post-loss
        equity, so a bounce after a bad morning could start the day's budget
        over (the futures runner always persisted the same fields)."""
        return {"day": self.day,
                "orders_today": dict(self.orders_today),
                "last_order_ts": dict(self.last_order_ts),
                "day_start_equity": self.day_start_equity,
                "peak_equity": self.peak_equity,
                "halted": self.halted,
                "halt_reason": self.halt_reason}

    def save(self, path: str):
        atomic_write_json(self.state(), path)

    def load(self, path: str) -> bool:
        s = load_json(path)
        if not s:
            return False
        self.day = s.get("day", "")
        self.orders_today = {str(k): int(v) for k, v in (s.get("orders_today") or {}).items()}
        self.last_order_ts = {str(k): float(v) for k, v in (s.get("last_order_ts") or {}).items()}
        dse = s.get("day_start_equity")
        self.day_start_equity = float(dse) if dse is not None else None
        pe = s.get("peak_equity")
        self.peak_equity = float(pe) if pe is not None else None
        self.halted = bool(s.get("halted", False))
        self.halt_reason = s.get("halt_reason", "")
        return True
