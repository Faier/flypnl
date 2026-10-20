"""Live dual-direction futures runner (paper or live).

Runs inside the engine as its own thread, one brain + one FuturesBroker per
contract. Signals are the same FlyBrain staircases; SELL while long flips to
short (net-mode market order covers and opens atomically). Mirrored exits:
flash, adaptive trail, scale-out, fatigue — the exact logic validated in
backtest_futures.

Telemetry is pushed into AppState.pairs["FUT_<contract>"] so the existing
dashboard renders a card with zero frontend changes.
"""
from __future__ import annotations

import json
import os
import threading
import time

import numpy as np

from ..brain.flybrain import FlyBrain, BUY, SELL
from ..data.gate_client import GateError
from ..logs import Log
from ..trader.entry_guards import EntryGuards
from ..trader.exits import ExitManager, PersistRatchet
from ..trader.futures_broker import FuturesBroker
from ..trader.stopguard import StopGuard


def _norm_klines(raw: list) -> list:
    return [[int(r["t"]), float(r["o"]), float(r["h"]), float(r["l"]),
             float(r["c"]), float(r.get("sum", 0) or 0)] for r in raw]


class FuturesRunner(threading.Thread):
    def __init__(self, state, client, cfg, runs_dir, live: bool = False,
                 heartbeat=None):
        super().__init__(daemon=True, name="futures")
        self.state = state
        self.client = client
        self.cfg = cfg
        self.heartbeat = heartbeat      # HangGuard probe: one beat per loop
        # iteration; a stall gets the process killed + relaunched (freeze
        # series 09-14..09-17, see trader/watchdog.py)
        fc = cfg.futures
        self.contract = fc.get("contract", "BTC_USDT")
        # single source of truth for real money: the CLI. Real futures orders
        # happen ONLY when the engine itself is live AND futures.paper is
        # false — `python -m flygate paper` used to trade futures with real
        # money because this flag was read independently (2026-09-15 audit)
        self.paper = bool(fc.get("paper", True)) or not live
        self.budget = float(fc.get("budget", 20))
        self.key = f"FUT_{self.contract}"
        self.runs = runs_dir
        # paper and live must not share state: a paper run must not learn on
        # (or overwrite) the live brain, or inherit the live cash ledger
        sfx = ".paper" if self.paper else ""
        self._state_path = os.path.join(self.runs, f"fut.state{sfx}.json")
        self._brain_path = os.path.join(self.runs, f"fut.brain{sfx}.npz")
        quanto = float(fc.get("quanto", 0.0001))
        self.brain = FlyBrain(cfg.brain, tag=self.key)
        self.ex = ExitManager(cfg.risk)
        # guard_dist is the SAME distance the StopGuard places below/above the
        # watermark: the broker needs it to prove the disaster brake sits inside
        # the liquidation distance of the leverage it is about to use
        self.broker = FuturesBroker(self.contract, self.budget, cfg.risk,
                                    client, quanto, paper=self.paper,
                                    fee=float(fc.get("fee", 0.0005)),
                                    guard_dist=float(cfg.risk.atr_trail_max))
        self.guard = StopGuard(client, dist=float(cfg.risk.atr_trail_max))
        self.cap_contracts = max(1, int(fc.get("max_contracts", 2)))
        self.entry_step = max(1, int(fc.get("entry_step", 1)))
        self.no_flip = bool(fc.get("no_flip", False))  # counter-signal closes
        # the position but does NOT auto-open the reverse leg (the 33d A/B
        # showed 84-98% of closes are brain flips killing trips early); off =
        # classic flip behavior
        self.interval = str(fc.get("interval", "1m"))  # candle TF the brain
        # sees; 15m validated 09-15 (ZEC +26.5 vs +4.7 at 1m on 33d, both
        # halves positive; brain EMAs/window are per-candle so a TF switch
        # must start from a FRESH brain file, not a 1m-trained one)
        self.long_only = bool(fc.get("long_only", False))  # SELL signals may
        # only CLOSE longs, never open shorts. 09-16 12.6mo ZEC: long_only
        # +185.9% vs dual +31.9% at 5m (shorts bled in every window tested);
        # crash months carried by the exit ladder, not by shorts
        self.chase_pct = float(fc.get("chase_pct") or 0) or None  # fresh-entry
        # veto when close > chase_pct above the last chase_n lows — the
        # 09-17/09-18 live campaigns both laddered in at leg-tops after
        # +8-11% runs; 12.6mo A/B: 0.06 → +188.1/dd17.4 vs base +158.5/dd19.0
        # (adds NOT vetoed — that arm lost 35pp; UNI neutral; ~73% of the
        # gain is the 2025-10 blow-off month = blow-off insurance, not alpha)
        self.chase_n = max(2, int(fc.get("chase_n", 20)))
        # ---- anti-chase entry layer (2026-09-24 diagnosis: 09-19 PM / 09-24
        # campaigns bought the day top; chase_pct only catches vertical spikes
        # and adds were entirely unguarded). Shared implementation with the
        # backtest — trader/entry_guards.py; every knob defaults OFF and only
        # enters config.json after the 12mo A/B wins.
        self.guards = EntryGuards.from_config(fc)
        self.entry_mode = str(fc.get("entry_mode") or "stair")  # stair|mr|regime
        # mr = pure mean-reversion (跌多涨，涨多跌) — the brain never opens,
        # SELL counter-signals never close. regime = hard switch: stair while
        # the smoothed coherence says trend, MR owns the range tape (the
        # both-mode blend churned -28%: counter-signals close MR dips at the
        # bottom — one position cannot serve two exit policies)
        self.pb_limit: float = 0.0     # armed soft-pullback retest level
        self.pb_ttl = 0                # candles left before the retest expires
        self._guards_warm = False      # warm EMA/ATR from fetched history once
        # hard stop: flat −pct off entry regardless of the trail ladder —
        # the win-rate lever (2026-09-25 goal: round win rate ≥60%; the
        # floor_tp_sweep frontier showed tight floors buy win rate with
        # total return). Long-only mirror; default off.
        self.hard_stop_pct = float(fc.get("hard_stop_pct") or 0) or None
        # ---- 2026-09-25 optimization layer (shared math with the backtest;
        # every knob defaults OFF = legacy behavior) ----
        # persist ratchet (A2): confirmation-gated profit floor for the gap
        # below the ladder's first rung — live 09-25 peaked +8.1U at +2.1%
        # and gave back toward a trail ~6% under the extreme; the ladder only
        # arms at +3%. pnl must PERSIST >= arm for t consecutive candles
        # (wick-immune, unlike the swept-and-rejected instant floor 2%).
        _pt = int(fc.get("persist_t") or 0)
        self.ratchet = (PersistRatchet(float(fc.get("persist_arm", 0.015)),
                                       _pt,
                                       float(fc.get("persist_lock", 0.002)))
                        if _pt > 0 else None)
        # add decay (B2): rung n sizes entry_step * add_decay**n — stage-1
        # vetoed UNCONDITIONAL add-vetoes (-35pp) but never tested a taper;
        # the 09-25 top-three adds were the part that round-tripped.
        self.add_decay = float(fc.get("add_decay") or 0) or None
        # equity-linked leverage (B1): cap = equity*sizing_lev/(quanto*px),
        # clamped by max_contracts; rung = cap//8. The fixed contract cap is
        # price-blind: 24 contracts = 0.6x budget at ZEC 500, 1.9x at 1590.
        self.sizing_lev = float(fc.get("sizing_lev") or 0) or None
        self.campaign_entries = 0   # rung index for add_decay (persisted)
        self.last_ts = 0
        self.candles: list = []
        self.extreme = 0.0            # high (long) / low (short)
        self.scaled_out = False
        self.r2_high = 0.0
        self.prev_close = 0.0
        self.last_eq = None
        self.day, self.n_today, self.last_order_ts = "", 0, 0.0
        self.day_start_eq, self.halted_day = None, ""
        self.profit_lock = 0.0        # absolute price floor/ceiling after scale trigger
        self._last_pos_sync = 0.0     # hourly re-sync of the exchange position
        self._stop = threading.Event()
        self._preflight()
        self._load()

    def _preflight(self):
        """Startup contract check: the configured quanto MUST match the real
        quanto_multiplier of the contract (docs on the asymmetry — mismatch =
        refuse, endpoint unreachable = warn and continue — live in
        FuturesBroker.verify_contract).

        A confirmed mismatch is sticky: entries stay blocked for the life of
        this process, while reduce-only exits remain allowed so an already open
        position can always be flattened. Silence here would be the dangerous
        outcome, so it is printed AND logged by run()."""
        try:
            self.broker.verify_contract()
        except GateError as e:
            print(f"[fut ] REFUSING TO TRADE {self.contract}: {e}", flush=True)

    # ---------------------------------------------------------------- state
    def _load(self):
        if os.path.exists(self._state_path):
            try:
                d = json.load(open(self._state_path))
                self.broker.cash = d.get("cash", self.budget)
                self.day = d.get("day", "")
                self.day_start_eq = d.get("day_start_eq")
                self.halted_day = d.get("halted_day", "")
                self.profit_lock = d.get("profit_lock", 0.0)
                self.scaled_out = d.get("scaled_out", False)
                self.r2_high = d.get("r2_high", 0.0)
                self.n_today = int(d.get("n_today", 0))
                self.last_order_ts = float(d.get("last_order_ts", 0.0))
                self.pb_limit = float(d.get("pb_limit", 0.0) or 0.0)
                self.pb_ttl = int(d.get("pb_ttl", 0) or 0)
                self.campaign_entries = int(d.get("campaign_entries", 0) or 0)
                if self.ratchet is not None:
                    self.ratchet.load_dict(d.get("ratchet"))
                # extreme IS part of the position's protection — restoring a
                # position without its watermark re-anchored the trail on the
                # entry price and silently widened the stop (2026-09-15 audit)
                self.extreme = float(d.get("extreme", 0.0) or 0.0)
                if not self.paper:
                    self.broker.sync_live()
                    self.broker.cash = d.get("cash", self.budget)
                else:
                    self.broker.size = d.get("size", 0)
                    self.broker.entry = d.get("entry", 0.0)
            except Exception:
                pass
        # an inherited position must not carry a dead watermark: a short's
        # extreme stays min(0.0, px)=0 forever, silently disarming the trail
        if self.broker.size != 0 and self.extreme <= 0:
            self.extreme = self.broker.entry or self.broker.price()
        if os.path.exists(self._brain_path):
            try:
                self.brain.load(self._brain_path)
            except Exception:
                pass
        # live mode: trade history comes from the exchange (survives restarts)
        if not self.paper:
            try:
                from ..trader.broker import Trade
                raw = self.client._request(
                    "GET", "/futures/usdt/my_trades",
                    query={"contract": self.contract, "limit": 50}, auth=True)
                for ft in (raw or []):
                    self.broker.trades.append(Trade(
                        float(ft.get("create_time", 0)),
                        f"FUT:{self.contract}",
                        "BUY" if int(ft.get("size", 0)) > 0 else "SELL",
                        float(ft.get("price", 0)), abs(int(ft.get("size", 0))) * self.broker.quanto,
                        abs(int(ft.get("size", 0))) * self.broker.quanto * float(ft.get("price", 0)),
                        float(ft.get("fee", 0) or 0),
                        f"futures {ft.get('role','')}", "fut-live"))
                self.broker.trades.sort(key=lambda t: t.ts)
                print(f"[fut ] restored {len(self.broker.trades)} fills from exchange",
                      flush=True)
            except Exception as e:
                print(f"[fut ] my_trades restore skipped: {e}", flush=True)

    def _save(self):
        try:
            tmp = self._state_path + ".tmp"
            json.dump({"cash": self.broker.cash, "size": self.broker.size,
                       "entry": self.broker.entry, "extreme": self.extreme,
                       "scaled_out": self.scaled_out, "r2_high": self.r2_high,
                       "day": self.day, "day_start_eq": self.day_start_eq,
                       "halted_day": self.halted_day,
                       "profit_lock": self.profit_lock,
                       "n_today": self.n_today,
                       "last_order_ts": self.last_order_ts,
                       "pb_limit": self.pb_limit,
                       "pb_ttl": self.pb_ttl,
                       "campaign_entries": self.campaign_entries,
                       "ratchet": (self.ratchet.as_dict()
                                   if self.ratchet is not None else None)},
                      open(tmp, "w"))
            os.replace(tmp, self._state_path)   # atomic: the hang watchdog's
            # SIGKILL can land mid-save and must never leave a half-written
            # state file (a failed load silently falls back to defaults)
            self.brain.save(self._brain_path)
        except Exception:
            pass

    # ---------------------------------------------------------------- loop
    def _klines(self) -> list:
        step = {"1m": 60, "5m": 300, "15m": 900, "30m": 1800,
                "1h": 3600, "4h": 14400}.get(self.interval, 60)
        now = time.time()
        to = int(now // step) * step - 1
        # fetch enough history to warm the guard baselines (ext_ref EMA) too
        n_hist = max(self.cfg.brain.window + 10,
                     (self.guards.ext_ref if self.guards else 0) + 10)
        frm = to - step * n_hist
        raw = self.client._request(
            "GET", "/futures/usdt/candlesticks",
            query={"contract": self.contract, "interval": self.interval,
                   "from": frm, "to": to}) or []
        rows = _norm_klines(raw)
        if rows:                       # persist live futures candles too
            try:
                from ..data.history import append_cache
                append_cache(f"fut_{self.contract}", self.interval, rows)
            except Exception:
                pass
        return rows

    def _cooldown_ok(self, ts) -> bool:
        today = time.strftime("%Y-%m-%d", time.localtime(ts))
        if today != self.day:
            self.day, self.n_today = today, 0
        return ((self.cfg.risk.max_orders_per_day <= 0
                 or self.n_today < self.cfg.risk.max_orders_per_day)
                and ts - self.last_order_ts >= self.cfg.risk.cooldown_s)

    def _fuse_overcap(self, px, ts):
        """Position fuse: if size ever exceeds the contract cap, trim back
        immediately. Guards the wrong-size-read bug class (2026-09-13: the
        runner went blind and four unintended orders overshot to -2 at
        cap=1) — worst case is now one candle of overshoot."""
        b = self.broker
        over = abs(b.size) - self.cap_contracts
        if over <= 0:
            return None
        t = b._market(-over if b.size > 0 else over, "position-fuse", px,
                      reduce_only=True)
        if t is not None:
            self.log.warn("RISK", "position_fuse", contract=self.contract,
                          size=b.size, trimmed=over,
                          mode="paper" if self.paper else "LIVE")
            self.n_today += 1
            self.last_order_ts = ts
        return t

    def _push(self, px, tel):
        s, b = self.state, self.broker
        with s.lock:
            s.version += 1
            ps = s.pairs.setdefault(self.key, {})
            ps.update({"price": px,
                       "candles": (self.candles or [])[-120:],
                       "cash": round(b.cash, 4),
                       "position_base": b.size * b.quanto,
                       "position_notional": round(b.position_notional(px), 4),
                       "equity": round(b.equity(px), 4),
                       "telemetry": tel,
                       "trades": [t.as_dict() for t in b.trades[-20:]]})
            # keep the GLOBAL panel alive when only futures run (pairs=[]):
            # with no spot pair the engine's own _push never fires, so the
            # futures thread maintains total equity / peak / curve itself
            total = sum(v.get("equity", 0.0) for v in s.pairs.values())
            s.total_equity = round(total, 4)
            if s.peak_equity < total:
                s.peak_equity = round(total, 4)
            s.equity_curve.append([time.time(), round(total, 4)])
            s.equity_curve = s.equity_curve[-4000:]
            ev = {"ts": time.time(), "pair": self.key, **tel}
            s.events.append(ev)

    def _chased(self, px) -> bool:
        """Fresh-entry veto condition: px sits > chase_pct above the lowest
        low of the last chase_n candles (same semantics as the backtest's
        _chased — window includes the just-closed candle)."""
        w = (self.candles or [])[-self.chase_n:]
        if len(w) < 2:
            return False
        lo = min(c[3] for c in w)
        return lo > 0 and px / lo - 1 > self.chase_pct

    # ---- 2026-09-25 optimization layer helpers ---------------------------
    def _cap_now(self, eq, px) -> int:
        """Contract cap for entries/adds THIS candle: the fixed config cap, or
        the equity-linked cap when sizing_lev is on. Recomputed every candle
        (backtest parity: run_dual sizes cap = lev*equity per candle); the
        position fuse still guards the FIXED config cap — that one is the
        wrong-size-read bug guard, not the economic cap."""
        if not self.sizing_lev or px <= 0:
            return self.cap_contracts
        return max(1, min(self.cap_contracts,
                          int(eq * self.sizing_lev
                              / (self.broker.quanto * px))))

    def _rung(self, cap_now: int) -> int:
        """Contracts for the next entry: equity-linked rung (cap//8 — live
        24cap/3step parity) or the fixed entry_step, geometrically tapered by
        add_decay over the campaign's entry index (0-based: a fresh entry is
        never tapered)."""
        base = max(1, cap_now // 8) if self.sizing_lev else self.entry_step
        if self.add_decay:
            base = max(1, int(round(base
                                    * self.add_decay ** self.campaign_entries)))
        return base

    def _campaign_reset(self):
        """Clear per-campaign state once flat (ratchet latch + decay index).
        Called at every close site that a SAME-CANDLE re-entry can follow
        (flip cover, counter-signal, mr-target, reflex)."""
        self.campaign_entries = 0
        if self.ratchet is not None:
            self.ratchet.reset()

    def run(self):
        self.log = Log(self.runs)
        if self.heartbeat is not None:
            self.heartbeat.beat()   # first beat BEFORE any boot network call
            # (the 09-17 freeze hung on the very first boot request)
        if not self.paper:
            self.guard.log = self.log
            self.guard.reconcile(self.contract)
        self.log.info("LIFE", "futures_started", contract=self.contract,
                      mode="paper" if self.paper else "LIVE", budget=self.budget,
                      leverage=self.broker.leverage,
                      quanto=self.broker.quanto,
                      max_contracts=self.cap_contracts)
        if self.broker.trade_blocked:
            # the preflight found the config multiplier disagreeing with the
            # exchange: the heartbeat above must not read as "trading normally"
            self.log.error("RISK", "fut_quanto_mismatch",
                           contract=self.contract, err=self.broker.trade_blocked)
        consec_err = 0    # escalating sleep on pure-noise failures (SSL hiccups
        # used to hit the log every ~10-20s for hours); capped at 60s so the
        # gap between two beats stays well under HangGuard's 180s threshold
        while not self._stop.is_set():
            try:
                if self.heartbeat is not None:
                    self.heartbeat.beat()
                # global kill switch covers futures too: flatten & idle
                if os.path.exists(os.path.join(self.runs, "KILL")):
                    if self.broker.size != 0:
                        px = self.broker.price()
                        self.broker.close_all(px, reason="global-halt")
                        self._save()
                    if not self.paper:
                        self.guard.cancel()
                    self._push(self.broker.price() or 0,
                               {"decision": "HALT", "note": "global kill switch"})
                    time.sleep(10)
                    continue
                rows = self._klines()
                if rows and rows[-1][0] != self.last_ts:
                    self.last_ts = rows[-1][0]
                    self.candles = rows
                    self._step(rows)
                else:
                    px = rows[-1][4] if rows else (self.broker.price() or 0)
                    if px:
                        self._push(px, self.state.pairs.get(self.key, {})
                                   .get("telemetry", {}))
                time.sleep(5)
                consec_err = 0
            except Exception as e:
                (self.log if hasattr(self, "log") else _nolog()).error("ERR", "fut_loop", err=str(e)[:120])
                consec_err += 1
                time.sleep(min(60, 10 * consec_err))

    def _step(self, rows):
        b, ex = self.broker, self.ex
        px = rows[-1][4]

        # ---- runtime position re-sync (live only) --------------------------
        # manual closes on the app, stop fills and liquidations change the
        # exchange position without telling us; an hourly pull (or a retry
        # after a failed post-order sync) keeps the local model honest
        if not self.paper and (b.sync_dirty
                               or rows[-1][0] - self._last_pos_sync >= 3600):
            self._last_pos_sync = rows[-1][0]
            try:
                b.sync_position()
            except Exception:
                pass

        # ---- daily-loss circuit breaker -----------------------------------
        # mark-to-market loss vs day's first candle >= daily_loss_halt ->
        # flatten once and stay flat until the next day (persisted).
        today = time.strftime("%Y-%m-%d", time.localtime(rows[-1][0]))
        if today != self.day:
            # NOTE: the order counter resets HERE. _cooldown_ok's own day
            # check can never fire (self.day is already stamped above it),
            # so before this line n_today accumulated across days forever
            self.day, self.day_start_eq, self.halted_day = today, None, ""
            self.n_today = 0
        if self.day_start_eq is None:
            self.day_start_eq = b.equity(px)
        halt_amt = float(self.cfg.risk.daily_loss_halt or 0)
        if (halt_amt > 0 and not self.halted_day
                and b.equity(px) - self.day_start_eq <= -halt_amt):
            b.close_all(px, reason="daily-loss-halt")
            self.halted_day = today
            self.n_today += 1
            self.log.info("HALT", "fut_daily_loss", contract=self.contract,
                          day_loss=round(b.equity(px) - self.day_start_eq, 4),
                          halt_at=-halt_amt)

        # ---- mirrored exits on the just-closed candle ---------------------
        if b.size != 0:
            long = b.size > 0
            self.extreme = max(self.extreme, px) if long else min(self.extreme, px)
            if self.ratchet is not None and b.entry:
                self.ratchet.step((px / b.entry - 1) if long
                                  else (b.entry / px - 1))
            trail = ex.trail_distance(rows)
            if long:
                flash = (self.prev_close and
                         px / self.prev_close - 1 <= -self.cfg.risk.flash_candle_drop)
                # protective floor ladder (highest wins):
                #   profit-lock  entry+3% once the +5% scale trigger latched
                #   breakeven    entry+0.2% once peak pnl >= +1.5%
                tstop = self.extreme * (1 - trail) if self.extreme else None
                floor, fwhy = None, None
                if b.entry:
                    if self.profit_lock > 0:
                        floor, fwhy = self.profit_lock, "profit-lock"
                    elif self.cfg.risk.floor_ladder:
                        peak = self.extreme / b.entry - 1
                        fl = max((lv for th, lv in self.cfg.risk.floor_ladder
                                  if peak >= th), default=0.0)
                        if fl > 0:
                            floor, fwhy = b.entry * (1 + fl), "ladder"
                    elif (self.extreme and self.extreme / b.entry - 1
                          >= self.cfg.risk.breakeven_trigger):
                        floor, fwhy = b.entry * (1 + self.cfg.risk.breakeven_buffer), "breakeven"
                    if self.ratchet is not None:   # A2: confirmation floor —
                        rf = self.ratchet.floor(b.entry, True)  # highest wins
                        if rf is not None and (floor is None or rf > floor):
                            floor, fwhy = rf, "ratchet"
                stop = tstop if (tstop is not None and (floor is None or tstop >= floor)) else floor
                trail_hit = stop is not None and px <= stop
                why = None
                if trail_hit:
                    why = fwhy if (floor is not None and stop == floor) else "trail"
                why = "flash-crash" if flash else why
                if not why and self.hard_stop_pct and b.entry \
                        and px / b.entry - 1 <= -self.hard_stop_pct:
                    why = "hard-stop"
            else:
                squeeze = (self.prev_close and
                           px / self.prev_close - 1 >= self.cfg.risk.flash_candle_drop)
                tstop = self.extreme * (1 + trail) if self.extreme else None
                floor, fwhy = None, None
                if b.entry:
                    if self.profit_lock > 0:
                        floor, fwhy = self.profit_lock, "profit-lock"
                    elif self.cfg.risk.floor_ladder:
                        peak = b.entry / self.extreme - 1
                        fl = max((lv for th, lv in self.cfg.risk.floor_ladder
                                  if peak >= th), default=0.0)
                        if fl > 0:
                            floor, fwhy = b.entry * (1 - fl), "ladder"
                    elif (self.extreme and b.entry / self.extreme - 1
                          >= self.cfg.risk.breakeven_trigger):
                        floor, fwhy = b.entry * (1 - self.cfg.risk.breakeven_buffer), "breakeven"
                    if self.ratchet is not None:
                        rf = self.ratchet.floor(b.entry, False)  # lowest binds
                        if rf is not None and (floor is None or rf < floor):
                            floor, fwhy = rf, "ratchet"
                stop = tstop if (tstop is not None and (floor is None or tstop <= floor)) else floor
                trail_hit = stop is not None and px >= stop
                why = None
                if trail_hit:
                    why = fwhy if (floor is not None and stop == floor) else "trail"
                why = "flash-squeeze" if squeeze else why
            if why:
                b.close_all(px, reason=f"reflex:{why}")
                self.log.info("EXIT", "fut_reflex", contract=self.contract,
                              why=why, price=px)
                self.extreme, self.scaled_out = 0.0, False
                self._campaign_reset()
                self.n_today += 1
                self.last_order_ts = rows[-1][0]
            elif not self.scaled_out:
                pnl_pct = (px / b.entry - 1) if long else (b.entry / px - 1)
                if b.entry and pnl_pct >= self.cfg.risk.scale_out_gain:
                    # lock gains into the protective floor whether or not a
                    # half could actually be sold (1-contract half is a no-op)
                    lock = float(self.cfg.risk.profit_lock_gain or 0)
                    if lock > 0:
                        self.profit_lock = (b.entry * (1 + lock) if long
                                            else b.entry * (1 - lock))
                    t = b.close_half(px, reason="scale-out@+5%")
                    # latch either way (a 1-contract half is a no-op; retrying
                    # every candle would spam) — but only a real fill burns
                    # order budget / cooldown
                    self.scaled_out = True
                    if t is not None:
                        self.log.info("EXIT", "fut_scale_out", contract=self.contract, pnl_pct=round(pnl_pct*100, 2))
                        self.n_today += 1
                        self.last_order_ts = rows[-1][0]
        else:
            self.extreme, self.scaled_out, self.profit_lock = 0.0, False, 0.0
            self._campaign_reset()
        self.prev_close = px

        # ---- dopamine & decide -------------------------------------------
        eq = b.equity(px)
        delta = 0.0 if self.last_eq is None else eq - self.last_eq
        self.last_eq = eq
        self.brain.holding_dir = (1 if b.size > 0 else
                                  (-1 if b.size < 0 else 0))
        tel = self.brain.observe(rows, delta)
        # ---- entry guards state (baseline EMA / ATR / leg age / regime) ---
        # first candle set: warm EMA/ATR from the fetched history (regime
        # warms candle-by-candle after — it needs the brain's r2 each step)
        if not self._guards_warm:
            for c in rows[:-1]:
                self.guards.step(c)
            self._guards_warm = True
        self.guards.step(rows[-1], tel.get("r2"))
        r2 = tel.get("r2")
        if r2 and r2 > self.r2_high:
            self.r2_high = r2
        # fatigue half-exit (coherence collapse while positioned)
        if b.size != 0 and (self.r2_high >= self.cfg.risk.fatigue_r2_high
                            and r2 is not None
                            and r2 < self.cfg.risk.fatigue_r2_drop):
            t = b.close_half(px, reason="trend-fatigue")
            self.r2_high = 0.0            # reset the probe either way (anti-spam)
            if t is not None:             # 1-contract half is a no-op: no fill, no budget burn
                self.log.info("EXIT", "fut_fatigue", contract=self.contract, r2=r2)
                self.n_today += 1
                self.last_order_ts = rows[-1][0]

        d = tel["decision"]
        # regime switch: while the smoothed coherence says RANGE, the MR arm
        # owns entries AND exits (a stair SELL at the range bottom is the
        # worst dip-buy exit — the both-mode blend churned -28% on exactly
        # that interaction in the 12mo A/B). Trending tape: stair logic.
        mr_owns = (self.guards.mr_on and self.guards.in_range()
                   and self.entry_mode in ("mr", "regime"))
        if self.halted_day:
            tel["note"] = "daily-loss-halt: flat till next day"
        if self.broker.trade_blocked:
            # entries are refused by the broker's preflight (quanto mismatch);
            # say so on the card instead of showing a runner that "just holds"
            tel["note"] = f"entries blocked: {self.broker.trade_blocked[:120]}"
        if self.broker.margin_backoff_until > time.time():
            # the exchange rejected an open on insufficient margin: entries
            # are frozen by the broker's preflight — say it on the card too
            tel["note"] = (f"margin-backoff: entries frozen "
                           f"{int(self.broker.margin_backoff_until - time.time())}s")
        acted = None
        prev_size = b.size
        cap_now = self._cap_now(eq, px)
        if d in (BUY, SELL) and not self.halted_day \
                and self._cooldown_ok(rows[-1][0]):
            t, closed = None, None
            if d == BUY:
                if self.entry_mode == "mr" or mr_owns:
                    pass               # MR owns the tape: the brain never opens;
                elif b.size < 0:       # dip-buys fire in the extras below
                    # flip = two DISCRETE steps: reduce-only cover first, open
                    # the new leg only once confirmed flat (a one-shot net
                    # delta order could open a fresh short if the cover leg
                    # failed or the position was already gone)
                    closed = b.close_all(px, reason="counter-signal" if self.no_flip
                                         else "flip-cover")
                    if b.size == 0:
                        self._campaign_reset()  # fresh decay index for the flip leg
                    if b.size == 0 and not self.no_flip:
                        t = b.open_long(self._rung(cap_now), px, "flip-long")
                elif b.size == 0:
                    veto = None
                    if self.chase_pct and self._chased(px):
                        # chase guard: signal is valid but we are >chase_pct
                        # above the recent floor — buying the vertical tail
                        # is the leg-top trap; wait for a fresh staircase
                        w = (self.candles or [])[-self.chase_n:]
                        lo = min(c[3] for c in w)
                        tel["note"] = (f"chase-guard: +{round(px / lo - 1, 1)}%"
                                       f" > {int(self.chase_pct * 100)}% in "
                                       f"{self.chase_n} candles")
                        veto = "chase"
                    elif self.guards.extended(px):
                        # A: slow grinds count too — 09-24 sat at the day high
                        # only +2.4%/100min but several ATR above its 16h mean
                        tel["note"] = (f"ext-guard: z={self.guards.z(px):.1f} > "
                                       f"{self.guards.ext_atr} ATR over EMA"
                                       f"{self.guards.ext_ref}")
                        veto = "extended"
                    elif self.guards.leg_too_old():
                        # C: the up-leg is ancient — late-cycle staircase
                        tel["note"] = (f"leg-age: {self.guards.leg_age} candles "
                                       f"> {self.guards.leg_max_age}")
                        veto = "leg-age"
                    if veto is None and self.guards.pullback_atr:
                        # D: arm a soft retest below the signal close instead
                        # of chasing; fills on a later candle CLOSE <= limit
                        lim = self.guards.pullback_limit(px)
                        if lim:
                            self.pb_limit, self.pb_ttl = lim, self.guards.pullback_ttl
                            tel["note"] = (f"retest-armed: buy < {lim:.4g} "
                                           f"({self.pb_ttl} candles)")
                    elif veto is None:
                        t = b.open_long(min(self._rung(cap_now), cap_now), px,
                                        "staircase-up")
                elif b.size < cap_now:
                    if not self.guards.add_ok(px, b.entry):
                        # B: pyramid anchor — the ladder must not stack the
                        # whole cap at the leg-top (09-19 PM: 10 fills at the
                        # 76-83rd pctile of the next 24h)
                        tel["note"] = (f"add-anchor: +{(px / b.entry - 1) * 100:.1f}% "
                                       f"> {self.guards.add_max_atr} ATR from avg")
                    else:
                        # clamp to the cap: an oversized add used to overshoot and
                        # get trimmed back by the position fuse one candle later
                        t = b.open_long(min(self._rung(cap_now),
                                            cap_now - b.size), px,
                                        "add-long")
                else:
                    t = None
            else:
                if d == SELL and b.size == 0:
                    self.pb_limit, self.pb_ttl = 0.0, 0  # trend failed — disarm
                if self.entry_mode == "mr" or mr_owns:
                    pass   # MR owns the tape: SELL staircases never close/flip
                elif self.long_only:   # — exits are mr-target + the ladder
                    # long-only policy: a SELL signal may only close a long
                    if b.size > 0:
                        closed = b.close_all(px, reason="counter-signal")
                        if b.size == 0:
                            self._campaign_reset()
                elif b.size > 0:
                    closed = b.close_all(px, reason="counter-signal" if self.no_flip
                                         else "flip-cover")
                    if b.size == 0:
                        self._campaign_reset()
                    if b.size == 0 and not self.no_flip:
                        t = b.open_short(self._rung(cap_now), px, "flip-short")
                elif b.size == 0:
                    t = b.open_short(min(self._rung(cap_now), cap_now), px,
                                     "staircase-dn")
                elif -b.size < cap_now:
                    t = b.open_short(min(self._rung(cap_now),
                                         cap_now + b.size), px,
                                     "add-short")
                else:
                    t = None
            if closed is not None:
                self.n_today += 1
                self.last_order_ts = rows[-1][0]
                acted = d                     # counter-signal close was a real order
                # counter-signal closes used to be invisible in the jsonl (only
                # the counter ticked) — the 09-17 18:15 flatten left no event
                # line and read like a mystery position change
                self.log.info("TRADE", "fut_close", contract=self.contract,
                              action=d, price=getattr(closed, "price", px),
                              mode="paper" if self.paper else "LIVE")
            if t is not None:
                acted = d
                self.log.info("TRADE", "fut_order", contract=self.contract,
                              action=d, size=b.size, price=getattr(t, "price", px),
                              mode="paper" if self.paper else "LIVE")
                self.n_today += 1
                self.last_order_ts = rows[-1][0]
                # protection resets ONLY on a fresh entry or a confirmed flip;
                # a same-direction add must keep the old watermark, the
                # scale-out latch and the profit lock — the original contracts
                # are still open and still need their floor (2026-09-15 audit:
                # adding at 104 used to erase a 107 high and a 103 lock)
                if prev_size == 0 or prev_size * b.size < 0:
                    self.extreme = px
                    self.scaled_out = False
                    self.profit_lock = 0.0
                    self.campaign_entries = 1
                    if self.ratchet is not None:
                        self.ratchet.reset()
                else:
                    self.campaign_entries += 1
        # ---- MR arm (跌多涨，涨多跌) + armed retest fills -------------------
        if mr_owns and not self.halted_day \
                and self._cooldown_ok(rows[-1][0]):
            if b.size == 0 and self.guards.mr_buy(px, rows[-1], tel):
                t2 = b.open_long(min(self._rung(cap_now), cap_now), px, "mr-dip")
                if t2 is not None:
                    acted = "MR"
                    self.log.info("TRADE", "fut_order", contract=self.contract,
                                  action="MR-BUY", size=b.size,
                                  price=getattr(t2, "price", px),
                                  mode="paper" if self.paper else "LIVE",
                                  z=round(self.guards.z(px), 2))
                    self.n_today += 1
                    self.last_order_ts = rows[-1][0]
                    self.pb_limit, self.pb_ttl = 0.0, 0
                    if prev_size == 0 or prev_size * b.size < 0:
                        self.extreme = px
                        self.scaled_out = False
                        self.profit_lock = 0.0
                        self.campaign_entries = 1
                        if self.ratchet is not None:
                            self.ratchet.reset()
                    else:
                        self.campaign_entries += 1
            elif b.size > 0 and self.guards.mr_target(px):
                c2 = b.close_all(px, reason="mr-target")
                if c2 is not None:
                    acted = acted or "MR"
                    self.log.info("EXIT", "fut_mr_target", contract=self.contract,
                                  price=getattr(c2, "price", px),
                                  z=round(self.guards.z(px), 2),
                                  mode="paper" if self.paper else "LIVE")
                    self.n_today += 1
                    self.last_order_ts = rows[-1][0]
                    self.extreme, self.scaled_out = 0.0, False
                    self.profit_lock = 0.0
                    self._campaign_reset()
        if self.pb_limit > 0:
            # armed soft-retest: fill on a candle CLOSE at/below the limit —
            # the pullback held, the dip is not (yet) a falling knife
            if px <= self.pb_limit and not self.halted_day \
                    and self._cooldown_ok(rows[-1][0]):
                t2 = b.open_long(min(self._rung(cap_now), cap_now), px, "retest-fill")
                if t2 is not None:
                    acted = acted or "PB"
                    self.log.info("TRADE", "fut_order", contract=self.contract,
                                  action="RETEST", size=b.size,
                                  price=getattr(t2, "price", px),
                                  mode="paper" if self.paper else "LIVE")
                    self.n_today += 1
                    self.last_order_ts = rows[-1][0]
                    self.pb_limit, self.pb_ttl = 0.0, 0
                    if prev_size == 0 or prev_size * b.size < 0:
                        self.extreme = px
                        self.scaled_out = False
                        self.profit_lock = 0.0
                        self.campaign_entries = 1
                        if self.ratchet is not None:
                            self.ratchet.reset()
                    else:
                        self.campaign_entries += 1
            else:
                self.pb_ttl -= 1
                if self.pb_ttl <= 0:
                    self.pb_limit, self.pb_ttl = 0.0, 0
        self._fuse_overcap(px, rows[-1][0])
        if not self.paper:
            self.guard.refresh(self.contract, b.size, self.extreme)
        tel["acted"] = acted or "—"
        tel["pos"] = f"{b.size:+d}张"
        tel["dir"] = "LONG" if b.size > 0 else ("SHORT" if b.size < 0 else "FLAT")
        if b.size != 0 and b.entry > 0:
            tel["pnl_pct"] = round(((px / b.entry - 1) if b.size > 0
                                    else (b.entry / px - 1)) * 100, 2)
        else:
            tel["pnl_pct"] = 0.0
        if self.ratchet is not None:
            tel["ratchet"] = (f"{self.ratchet.count}/{self.ratchet.t}"
                              + (" ARMED" if self.ratchet.latched else ""))
        self._save()
        self._push(px, tel)
