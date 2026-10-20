"""Dual-direction (long+short) futures backtest — experiment module.

Validates the low-leverage perp design BEFORE any live futures code:
  signals: same FlyBrain (uptrend staircase -> long, downtrend -> short)
  exits  : mirrored ladder — trail / flash / fatigue / scale-out both ways
  costs  : futures taker 0.05% + slippage 0.03% per fill
  sizing : order_n notional per entry, cap total notional (2x margin assumed,
           liquidation at ~50% adverse move — stops sit far inside it)
Funding rates on majors are ~0.001%/8h and are NOT modeled (immaterial here).
"""
from __future__ import annotations

import time

import numpy as np

from .brain.flybrain import FlyBrain, BUY, SELL
from .config import Config
from .trader.entry_guards import EntryGuards
from .trader.exits import ExitManager, PersistRatchet
from .trader.risk import reflex_exit


def fetch_futures(client, contract: str, interval: str, days: float, cache_pair: str):
    """Fetch & cache futures klines normalized to [ts,o,h,l,c,v]."""
    from .data.history import append_cache, load_cache
    target = int(time.time()) - int(days * 86400)
    for attempt in range(6):
        try:
            have = load_cache(cache_pair, interval)
            if have and have[0][0] <= target:
                break
            to = have[0][0] - 1 if have else int(time.time())
            while True:
                raw = client._request("GET", "/futures/usdt/candlesticks",
                                      query={"contract": contract, "interval": interval,
                                             "limit": 1000, "to": to}) or []
                if not raw:
                    break
                batch = [[int(r["t"]), float(r["o"]), float(r["h"]), float(r["l"]),
                          float(r["c"]), float(r.get("sum", 0) or 0)] for r in raw]
                append_cache(cache_pair, interval, batch)
                oldest = batch[0][0]
                if oldest <= target or len(batch) < 2:
                    break
                to = oldest - 1
                time.sleep(0.15)
            break
        except Exception:
            time.sleep(2)
    return [r for r in load_cache(cache_pair, interval) if r[0] >= target]


def _chased(hist, px, pct, n):
    """True when px sits > pct above the lowest LOW of the last n candles —
    the vertical-extension veto for the chase guard (fresh entries only,
    unless chase_adds)."""
    if len(hist) < 2:
        return False
    lo = min(r[3] for r in hist[-n:])
    return lo > 0 and px / lo - 1 > pct


def run_dual(cfg: Config, rows: list, mode: str = "dual", budget: float = 30.0,
             order_n: float = 10.0, cap: float = 30.0, verbose: bool = True,
             entry_filter=None, sizing_frac=None, floor_mode=None,
             floor_frac=0.5, floor_arm=0.02, brain_every: int = 1,
             exit_when_falling: bool = False, exit_bar=None,
             chase_pct: float | None = None, chase_n: int = 20,
             chase_adds: bool = False, guards: EntryGuards | None = None,
             hard_stop_pct: float | None = None,
             exit_slip: bool = False, funding: list | None = None,
             persist: tuple | None = None, add_decay: float | None = None,
             sizing_lev: float | None = None) -> dict:
    """brain_every: split-clock research mode. 1 = classic (signals AND exits
    every candle). N>1 with 1m rows: the brain observes/decides only on every
    Nth minute (5m signals) while the protective exit ladder (floor/trail/
    scale/flash) checks EVERY 1m candle — answers "should the safety rope
    react faster than the brain thinks?". Trail lookback is scaled to keep the
    same wall-clock window (60 x N candles). Other params:
    entry_filter(ts, direction) -> bool: optional veto on OPENING exposure;
    closing legs are NEVER filtered (None = off).
    sizing_frac: order notional = frac x equity, cap = 1.9 x equity (compounding).
    floor_mode: research floor variants (cur_frac/peak_frac/cur_tier); None = deployed ladder.

    ---- 2026-09-25 honest-cost & optimization kwargs (ALL default OFF: a
    legacy arm reproduces published numbers; sweeps opt in explicitly) ----
    exit_slip: apply `slip` to EXIT fills too. Live exits are market orders
        placed seconds after the triggering candle closes; the legacy model
        filled them AT the close with zero slippage (optimistic in crashes).
    funding: [[ts, rate], ...] ascending — at every funding timestamp crossed
        while positioned: cash -= qty*px*rate*(+1 long / -1 short) (C2).
    persist: (arm, t, lock) -> PersistRatchet: confirmation-gated profit
        floor for the sub-ladder giveback gap (A2; trader/exits.py).
    add_decay: campaign entry n sizes order_n * add_decay**n (B2 taper;
        stage-1 vetoed unconditional add-vetoes, a taper was never tested).
    sizing_lev: equity-linked leverage: cap = lev x max(eq, budget*0.25),
        order_n = cap/8 — parity with the live rung = cap_contracts//8 (B1).
    """
    fee, slip = 0.0005, 0.0003
    brain = FlyBrain(cfg.brain)
    ex = ExitManager(cfg.risk)
    cash, qty, direction, entry = budget, 0.0, 0, 0.0   # direction: +1/-1/0
    extreme = 0.0                 # high (long) / low (short) watermark
    scaled_out, r2_high = False, 0.0
    pending, prev_close, last_eq = None, 0.0, None
    trades, curve = [], []
    holding = []                   # research output: 1/0 per curve bar (positioned)
    brain_rows = []               # brain-cadence candles (split-clock mode)
    brain_prev_close = 0.0        # last brain-candle close (falling detector)
    day, n_today, last_order_ts = "", 0, 0.0
    profit_lock = 0.0            # entry*(1+profit_lock_gain) once scale fires
    wins = rounds = 0
    pb_limit, pb_ttl = None, 0   # armed soft-pullback level + ttl in candles
    open_cost = 0.0
    n_entries = 0                  # entries in the current campaign (decay index)
    rach = PersistRatchet(*persist) if persist else None
    fund_i, funding_paid = 0, 0.0
    entry_log = []                 # research: [ts, notional, rung_idx] per fill
    close_reasons: dict = {}
    # trip telemetry: peak unrealized % and cash-delta return per round trip
    trip_peak, trip_cash0, trip_peaks, trip_reals = 0.0, None, [], []

    def close_pos(px, reason):
        nonlocal cash, qty, direction, extreme, scaled_out, wins, rounds, open_cost
        nonlocal trip_peak, trip_cash0, profit_lock, n_entries
        s = slip if exit_slip else 0.0
        fill = px * (1 - s) if direction > 0 else px * (1 + s)
        pnl = qty * (fill - entry) * direction
        cost = fee * qty * fill
        cash += pnl - cost
        trades.append({"ts": rows[i][0], "side": "close", "dir": "L" if direction > 0 else "S",
                       "px": fill, "pnl": round(pnl - cost, 4), "reason": reason})
        close_reasons[reason.split(":")[-1]] = close_reasons.get(reason.split(":")[-1], 0) + 1
        if open_cost:
            rounds += 1
            wins += pnl - cost > 0
            trip_peaks.append(trip_peak)
            trip_reals.append((cash - trip_cash0) / open_cost * 100
                              if trip_cash0 is not None else 0.0)
        qty, direction, extreme, scaled_out, open_cost = 0.0, 0, 0.0, False, 0.0
        trip_peak, trip_cash0 = 0.0, None
        profit_lock = 0.0
        n_entries = 0
        if rach is not None:
            rach.reset()

    for i in range(cfg.brain.window, len(rows) - 1):
        hist, cur = rows[:i + 1], rows[i]
        brain_candle = (brain_every <= 1
                        or (cur[0] % (brain_every * 60)) == 0)
        if sizing_frac or sizing_lev:            # equity-linked sizing: rescale
            eq_now = cash + (qty * (cur[1] - entry) * direction if qty else 0.0)
            base_eq = max(eq_now, budget * 0.25)  # floor: don't vanish after dd
            if sizing_lev:
                cap = sizing_lev * base_eq
                order_n = cap / 8.0               # live parity: rung = cap//8
            else:
                order_n = sizing_frac * base_eq
                cap = 1.9 * base_eq
        # ---- fill pending at this candle's open (brain cadence) ------------
        if pending and brain_candle:
            px = cur[1] * (1 + slip * (1 if pending in ("long", "addL") else -1))
            today = time.strftime("%Y-%m-%d", time.localtime(cur[0]))
            if today != day:
                day, n_today = today, 0
            if (cfg.risk.max_orders_per_day <= 0 or n_today < cfg.risk.max_orders_per_day) and cur[0] - last_order_ts >= cfg.risk.cooldown_s:
                placed = False
                if pending == "long":
                    if direction == -1:
                        close_pos(px, "flip")
                        placed = True
                    if entry_filter is None or entry_filter(cur[0], 1):
                        on_eff = (order_n * add_decay ** n_entries
                                  if add_decay else order_n)
                        q = on_eff / px
                        cash -= 0  # margin model: pnl only; fee below
                        cash -= fee * on_eff
                        if direction == 0:
                            direction, entry, extreme, scaled_out, open_cost = 1, px, px, False, on_eff
                            trip_peak, trip_cash0 = 0.0, cash
                        else:
                            entry = (entry * qty + px * q) / (qty + q)
                            open_cost += on_eff
                        qty += q
                        entry_log.append([cur[0], round(on_eff, 4), n_entries])
                        n_entries += 1
                        placed = True
                elif pending == "short":
                    if direction == 1:
                        close_pos(px, "flip")
                        placed = True
                    if entry_filter is None or entry_filter(cur[0], -1):
                        on_eff = (order_n * add_decay ** n_entries
                                  if add_decay else order_n)
                        q = on_eff / px
                        cash -= fee * on_eff
                        if direction == 0:
                            direction, entry, extreme, scaled_out, open_cost = -1, px, px, False, on_eff
                            trip_peak, trip_cash0 = 0.0, cash
                        else:
                            entry = (entry * qty + px * q) / (qty + q)
                            open_cost += on_eff
                        qty += q
                        entry_log.append([cur[0], round(on_eff, 4), n_entries])
                        n_entries += 1
                        placed = True
                elif pending == "close":
                    close_pos(px, "signal")
                    placed = True
                elif pending == "close_mr":
                    close_pos(px, "mr-target")
                    placed = True
                if placed:
                    n_today += 1
                    last_order_ts = cur[0]
                    if pending == "long":
                        pb_limit, pb_ttl = None, 0   # filled — disarm the retest
            pending = None

        px = cur[4]
        if funding:                   # C2: funding events crossed since the
            while fund_i < len(funding) and funding[fund_i][0] <= cur[0]:
                if qty:               # last candle, charged on the position
                    pay = (qty * px * funding[fund_i][1]
                           * (1 if direction > 0 else -1))
                    cash -= pay
                    funding_paid += pay
                fund_i += 1
        # ---- multi-leg capital-switch hook (research) ----------------------
        # exit_bar(ts) -> bool: "may this leg be positioned right now?" A leg
        # sharing capital with a primary (e.g. ZEC-flat scavenging) gets
        # entry_filter = not-primary-holding AND exit_bar = same predicate:
        # opens vetoed while the primary holds, force-closed the bar the
        # primary re-enters. None = off, behavior unchanged.
        if qty and exit_bar is not None and not exit_bar(cur[0]):
            close_pos(px, "capital-switch")
        # ---- mirrored exit ladder (now includes the live floor ladder) ----
        # split-clock variants: exits every candle (brain_every>1), or —
        # exit_when_falling — only when the brain clock ticks OR price is
        # below the last brain close (user's "跌的时候小点" idea)
        falling = (qty > 0 and brain_prev_close
                   and ((direction > 0 and px < brain_prev_close)
                        or (direction < 0 and px > brain_prev_close)))
        if qty > 0 and (brain_every <= 1 or brain_candle
                        or (exit_when_falling and falling)):
            now_pnl = (px / entry - 1) if direction > 0 else (entry / px - 1)
            if now_pnl > trip_peak:
                trip_peak = now_pnl
            if rach is not None and entry:   # A2: persistence counter advances
                rach.step(now_pnl)           # every (brain-cadence) candle held
            if direction > 0:
                extreme = max(extreme, px)
                trail = ex.trail_distance(hist, n=60 * brain_every)
                tstop = extreme * (1 - trail) if extreme else None
                floor, fwhy = None, None
                if entry:
                    peak = extreme / entry - 1 if entry else 0.0
                    if floor_mode is not None:
                        # research variants fully replace the deployed ladder
                        if floor_mode == "peak_frac" and peak >= floor_arm:
                            floor, fwhy = entry * (1 + floor_frac * peak), "peak_frac"
                        elif floor_mode == "cur_frac" and now_pnl >= floor_arm:
                            floor, fwhy = entry * (1 + floor_frac * now_pnl), "cur_frac"
                        elif floor_mode == "cur_tier":
                            ct = max((lv for th, lv in
                                      [(floor_arm, floor_frac),
                                       (2 * floor_arm, 2 * floor_frac)]
                                      if now_pnl >= th), default=0.0)
                            if ct > 0:
                                floor, fwhy = entry * (1 + ct), "cur_tier"
                    elif profit_lock > 0:
                        floor, fwhy = profit_lock, "profit-lock"
                    elif cfg.risk.floor_ladder:
                        fl = max((lv for th, lv in cfg.risk.floor_ladder
                                  if peak >= th), default=0.0)
                        if fl > 0:
                            floor, fwhy = entry * (1 + fl), "ladder"
                    elif extreme and peak >= cfg.risk.breakeven_trigger:
                        floor, fwhy = entry * (1 + cfg.risk.breakeven_buffer), "breakeven"
                    if rach is not None:     # confirmation floor below the first
                        rf = rach.floor(entry, True)   # ladder rung; highest wins
                        if rf is not None and (floor is None or rf > floor):
                            floor, fwhy = rf, "ratchet"
                stop = tstop if (tstop is not None and (floor is None or tstop >= floor)) else floor
                why = None
                if stop is not None and px <= stop:
                    why = fwhy if (floor is not None and stop == floor) else "trail"
                flash = (prev_close and px / prev_close - 1 <= -cfg.risk.flash_candle_drop)
                why = "flash-crash" if flash else why
                # hard stop: flat −pct off entry, independent of the trail
                # ladder — the win-rate lever (floor_tp_sweep: floor 2% ->
                # win 57%); long-only mirror (shorts unused in live config)
                if not why and hard_stop_pct and entry \
                        and px / entry - 1 <= -hard_stop_pct:
                    why = "hard-stop"
                if not why and not scaled_out and px / entry - 1 >= cfg.risk.scale_out_gain:
                    half = qty / 2
                    fill = px * (1 - (slip if exit_slip else 0.0))
                    cash += half * (fill - entry) - fee * half * fill
                    qty -= half
                    open_cost /= 2
                    scaled_out = True
                    lock = float(cfg.risk.profit_lock_gain or 0)
                    if lock > 0:
                        profit_lock = entry * (1 + lock)
                    trades.append({"ts": cur[0], "side": "scale", "dir": "L",
                                   "px": px, "pnl": 0, "reason": "scale-out"})
                if why:
                    close_pos(px, f"reflex:{why}")
            else:
                extreme = min(extreme, px)
                trail = ex.trail_distance(hist, n=60 * brain_every)
                c2, e2 = prev_close, extreme
                tstop = extreme * (1 + trail) if extreme else None
                floor, fwhy = None, None
                if entry:
                    peak = entry / extreme - 1 if extreme else 0.0
                    if profit_lock > 0:
                        floor, fwhy = profit_lock, "profit-lock"
                    elif cfg.risk.floor_ladder:
                        fl = max((lv for th, lv in cfg.risk.floor_ladder
                                  if peak >= th), default=0.0)
                        if fl > 0:
                            floor, fwhy = entry * (1 - fl), "ladder"
                    elif extreme and peak >= cfg.risk.breakeven_trigger:
                        floor, fwhy = entry * (1 - cfg.risk.breakeven_buffer), "breakeven"
                    if rach is not None:
                        rf = rach.floor(entry, False)  # shorts: lowest binds
                        if rf is not None and (floor is None or rf < floor):
                            floor, fwhy = rf, "ratchet"
                stop = tstop if (tstop is not None and (floor is None or tstop <= floor)) else floor
                why = None
                if stop is not None and px >= stop:
                    why = fwhy if (floor is not None and stop == floor) else "trail"
                if c2 and c2 > 0 and px / c2 - 1 >= cfg.risk.flash_candle_drop:
                    why = "flash-squeeze"
                if not why and not scaled_out and entry / px - 1 >= cfg.risk.scale_out_gain:
                    half = qty / 2
                    fill = px * (1 + (slip if exit_slip else 0.0))
                    cash += half * (entry - fill) - fee * half * fill
                    qty -= half
                    open_cost /= 2
                    scaled_out = True
                    lock = float(cfg.risk.profit_lock_gain or 0)
                    if lock > 0:
                        profit_lock = entry * (1 - lock)
                    trades.append({"ts": cur[0], "side": "scale", "dir": "S",
                                   "px": px, "pnl": 0, "reason": "scale-out"})
                if why:
                    close_pos(px, f"reflex:{why}")
        prev_close = px

        eq = cash + (qty * (px - entry) * direction if qty else 0.0)
        delta = 0.0 if last_eq is None else eq - last_eq
        last_eq = eq
        curve.append(eq)
        holding.append(1 if qty else 0)
        if not brain_candle:
            continue                    # split-clock: no brain action this minute
        brain_rows.append(cur)          # the brain sees only brain-cadence candles
        brain_prev_close = px           # falling detector anchor
        tel = brain.observe(brain_rows[-(cfg.brain.window + 8):], delta)
        if guards is not None:          # EMA/ATR/leg-age/regime advance with
            guards.step(cur, tel.get("r2"))  # the tape (r2 feeds the regime)
        if tel.get("r2") and tel["r2"] > r2_high:
            r2_high = tel["r2"]
        d = tel["decision"]
        # regime switch: while the smoothed coherence says RANGE, the MR arm
        # owns BOTH entries and exits — a stair SELL at the range bottom is
        # the worst possible dip-buy exit (the both-mode arms churned -28%
        # on exactly that interaction). Trending tape: classic stair logic.
        mr_owns = (guards is not None and guards.mr_on
                   and mode in ("mr", "regime") and guards.in_range())
        # fatigue half-exit (direction-agnostic): coherence collapse
        if qty > 0 and r2_high >= cfg.risk.fatigue_r2_high and (tel.get("r2") or 1) < cfg.risk.fatigue_r2_drop:
            half = qty / 2
            if direction > 0:
                fill = px * (1 - (slip if exit_slip else 0.0))
                cash += half * (fill - entry) - fee * half * fill
            else:
                fill = px * (1 + (slip if exit_slip else 0.0))
                cash += half * (entry - fill) - fee * half * fill
            qty -= half
            open_cost /= 2
            r2_high = 0.0
            trades.append({"ts": cur[0], "side": "fatigue", "dir": "L" if direction > 0 else "S",
                           "px": px, "pnl": 0, "reason": "fatigue-half"})
        if d == BUY:
            if mode == "short_only":
                if direction == -1:
                    pending = "close"    # cover only; a short-only engine
            elif mode == "mr" or mr_owns:
                pass                     # MR owns the tape: the brain never opens,
            elif direction <= 0 or (direction > 0 and qty * px < cap):
                veto = None
                if mode == "dual_noflip" and direction == -1:
                    pending = "close"    # counter-signal while positioned: exit only,
                elif chase_pct and (direction == 0 or chase_adds) \
                        and _chased(hist, cur[4], chase_pct, chase_n):
                    veto = "chase"       # chase guard: veto a fresh entry (or
                    # an add, with chase_adds) in a vertical extension of the
                    # last chase_n candles — the 09-17/09-18 live campaigns
                    # both laddered in at leg-tops after +8-11% runs
                elif guards is not None and direction == 0:
                    if guards.extended(cur[4]):
                        veto = "extended"     # A: px > ext_atr·ATR above the
                    elif guards.leg_too_old():  # baseline EMA — slow grinds into
                        veto = "leg-age"       # the top count too (09-24 was)
                elif guards is not None and direction > 0 \
                        and not guards.add_ok(cur[4], entry):
                    veto = "add-anchor"  # B: pyramid ran > add_max_atr from avg
                if veto is None:         # never insta-reverse (flip kill-rate fix)
                    if (guards is not None and direction == 0
                            and guards.pullback_atr):
                        lim = guards.pullback_limit(cur[4])
                        if lim:          # D: arm a soft retest below the signal
                            pb_limit, pb_ttl = lim, guards.pullback_ttl  # close
                    else:
                        pending = "long"
        elif d == SELL:
            if (mode == "mr" or mr_owns) and direction == 1:
                pass                     # MR owns the tape: a SELL staircase at
            elif mode == "dual_noflip" and direction == 1:  # range bottom is the
                pending = "close"        # worst dip-buy exit — exits are the
            elif mode in ("dual", "dual_noflip", "short_only"):   # mr-target +
                if direction == 0 or (direction < 0 and qty * px < cap):  # ladder
                    pending = "short"
                elif mode == "dual" and direction > 0:
                    pending = "short"    # classic dual keeps the flip
            elif direction > 0:
                pending = "close"
            if pending is not None:
                pb_limit, pb_ttl = None, 0   # trend failed — disarm the retest
        # ---- MR arm (跌多涨，涨多跌) + armed pullback retest -----------------
        if guards is not None and guards.mr_on and mode in ("mr", "regime") \
                and pending is None:
            if mr_owns and direction == 0 and guards.mr_buy(cur[4], cur, tel):
                pending = "long"         # stabilized dip k ATR below the mean
            elif mr_owns and direction > 0 and guards.mr_target(cur[4]):
                pending = "close_mr"     # recovered k' ATR above it — sell the
        if pb_limit is not None and pending is None:  # strength into it
            if cur[4] <= pb_limit:
                pending = "long"         # retest held into the close — enter
                pb_limit, pb_ttl = None, 0
            else:
                pb_ttl -= 1
                if pb_ttl <= 0:
                    pb_limit = None

    px = rows[-1][4]
    if qty > 0:
        close_pos(px, "end")
    eqs = np.array(curve)
    peak = np.maximum.accumulate(eqs)
    res = {
        "mode": mode, "final": round(cash, 3), "ret_pct": round((cash / budget - 1) * 100, 2),
        "bh_pct": round((rows[-1][4] / rows[cfg.brain.window][4] - 1) * 100, 2),
        "trades": len(trades), "rounds": rounds,
        "win_pct": round(100 * wins / rounds, 1) if rounds else 0,
        "avg_trip_peak_pct": round(100 * float(np.mean(trip_peaks)), 2) if trip_peaks else 0.0,
        "trip_peak_p50": round(100 * float(np.percentile(trip_peaks, 50)), 2) if trip_peaks else 0.0,
        "trip_peak_p90": round(100 * float(np.percentile(trip_peaks, 90)), 2) if trip_peaks else 0.0,
        "close_reasons": close_reasons,
        "avg_trip_real_pct": round(float(np.mean(trip_reals)), 2) if trip_reals else 0.0,
        "trip_real_p10": round(float(np.percentile(trip_reals, 10)), 2) if trip_reals else 0.0,
        "funding_total": round(funding_paid, 4),
        "entry_log": entry_log,
        "maxdd_pct": round(float(((peak - eqs) / peak).max() * 100), 2) if len(eqs) else 0,
        "trade_log": trades[-40:],
        # research-only additions (multi-leg idle-capital sims): full trade
        # list with timestamps + per-curve-bar holding flag
        "trades_all": trades,
        "holding": holding,
        "curve_vals": [round(float(x), 4) for x in curve],
    }
    if verbose:
        print(f"  [{mode:4s}] 收益 {res['ret_pct']:+7.2f}%  持有 {res['bh_pct']:+7.2f}%  "
              f"交易{res['trades']:3d} 平仓{res['rounds']:3d} 胜率{res['win_pct']:5.1f}% 回撤{res['maxdd_pct']:5.2f}% "
              f"峰值/回合 {res['avg_trip_peak_pct']:+5.2f}%")
    return res
