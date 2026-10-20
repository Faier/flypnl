"""Backtest / replay engine + the control runs StonkFly's own docs say are
required before claiming anything:

  normal         — full fly brain (motion readout + dopamine plasticity)
  frozen         — plasticity disabled, EVERYTHING else identical (clean ablation)
  frozen_legacy  — the 2026-09-15 arm: plasticity + MBON/r2 gates all bypassed
                   (kept only to reproduce the old table; it differed from
                   `normal` by two mechanisms, so it could not attribute the
                   learning increment to learning)
  random         — seeded random decisions at the same cadence
  buyhold        — buy at the first candle, hold to the end (fee-free, full budget)
  rewired        — orientation-destroying column shuffle, same pixel statistics
  mbon_dn        — normal + MBON_a/v wired to DN_up/DN_dn + action-gated credit
                   assignment (exits ungated). The bold arm: it is the first
                   configuration in which dopamine can change the SIGN of the
                   decision instead of only the entry gate.
  learn          — mbon_dn + unit-L1 eligibility. The ladder normal -> mbon_dn
                   -> learn separates "the wiring can carry learning" from
                   "learning moves the weights enough to matter".

All fills happen at the NEXT candle open after the decision (no look-ahead),
with slippage + taker fees — entries AND exits; the reflex/scale-out exits used
to sell at the signal candle's close while entries waited for the next open,
which made every escape look better than it could have been.

The risk manager is evaluated on EVERY candle, as the live loop does; it used
to run only when the brain proposed an order, so a halted run could report a
drawdown the live engine would never have allowed.
"""
from __future__ import annotations

import json
import os
import time

import numpy as np

from .brain.flybrain import FlyBrain, BUY, SELL
from .brain.circuits import LoomingDetector
from .config import Config
from .trader.broker import PaperBroker
from .trader.risk import RiskManager, reflex_exit, entry_notional
from .trader.exits import ExitManager

MODES = ("normal", "frozen", "frozen_legacy", "random", "buyhold", "rewired",
         "mbon_dn", "learn", "real_wired", "frozen_dn", "real_wired_dn",
         "real_wired_learn", "learn_trip", "real_wired_learn_trip",
         "real_wired_rule_trip")


def run_backtest(cfg: Config, pair: str, rows: list, mode: str = "normal",
                 verbose: bool = True, seed: int | None = None) -> dict:
    if mode not in MODES:
        raise ValueError(f"unknown mode {mode!r}; expected one of {MODES}")
    risk = RiskManager(cfg.risk, kill_file="/dev/null",
                       budget_per_pair=cfg.quote_budget, backtest=True)
    broker = PaperBroker(pair, cfg.quote_budget, cfg.risk)
    if mode == "rewired":
        import dataclasses
        brain = FlyBrain(dataclasses.replace(cfg.brain, rewired=True), tag=pair)
    elif mode == "real_wired":
        import dataclasses
        # Phase C: MaleCNS real wiring, plasticity FROZEN. Differs from
        # `frozen` in exactly one thing (the wiring), from `rewired` in
        # whether the structure is real.
        brain = FlyBrain(dataclasses.replace(cfg.brain, real_wired=True),
                         tag=pair)
    elif mode == "frozen_dn":
        import dataclasses
        # gives the SYNTHETIC frozen brain the MBON->DN graded pathway, so
        # real_wired_dn vs frozen_dn isolates the wiring with the pathway open
        # (with the pathway closed the frozen MB is decision-inert: measured
        # 2026-09-28, real_wired == frozen bit-for-bit on all 4 pairs)
        brain = FlyBrain(dataclasses.replace(cfg.brain, mbon_to_dn=True),
                         tag=pair)
    elif mode == "real_wired_dn":
        import dataclasses
        brain = FlyBrain(dataclasses.replace(cfg.brain, real_wired=True,
                                             mbon_to_dn=True), tag=pair)
    elif mode == "real_wired_learn":
        # Phase D: dopamine learning ON, restricted to real KC->MBON07/11
        # edges (bounds 0.1x-2x per-edge baseline). Compare against `learn`
        # (same learning config, synthetic wiring) and `real_wired_dn`
        # (same wiring, frozen): the paired differences isolate, separately,
        # the learning increment and the wiring increment.
        import dataclasses
        brain = FlyBrain(dataclasses.replace(cfg.brain, real_wired=True,
                                             mbon_to_dn=True,
                                             action_gated_elig=True,
                                             elig_normalize=True), tag=pair)
    elif mode == "real_wired_learn_trip":
        # Phase D + P0: same as real_wired_learn, dopamine taught at TRIP
        # CLOSE from realized P&L instead of per-minute mark noise
        import dataclasses
        brain = FlyBrain(dataclasses.replace(cfg.brain, real_wired=True,
                                             mbon_to_dn=True,
                                             action_gated_elig=True,
                                             elig_normalize=True,
                                             trip_teaching=True), tag=pair)
    elif mode == "real_wired_rule_trip":
        # P1: the Huang/Luo 2024 per-edge rule (luohuang.py) replaces the
        # eligibility update; same trip-close pulse teaching. Compare against
        # real_wired_learn_trip (P0 rule, same substrate+teaching) to ask
        # whether the published learning rule buys anything over the simple one.
        import dataclasses
        brain = FlyBrain(dataclasses.replace(cfg.brain, real_wired=True,
                                             mbon_to_dn=True,
                                             action_gated_elig=True,
                                             elig_normalize=True,
                                             trip_teaching=True,
                                             rule="huang2024"), tag=pair)
    else:
        brain = FlyBrain(cfg.brain, tag=pair)
    if seed is None:
        seed = cfg.brain.seed
    rng = np.random.default_rng(seed)
    # a frozen arm must differ from `normal` in the plasticity only; the legacy
    # arm keeps the old double bypass for reproducing the 2026-09-15 table
    frozen = mode in ("frozen", "frozen_legacy", "real_wired",
                      "frozen_dn", "real_wired_dn")
    if mode == "frozen_legacy":
        import dataclasses as _dc
        brain.cfg = _dc.replace(cfg.brain, frozen_keeps_gate=False)
    elif mode == "mbon_dn":
        import dataclasses as _dc
        brain.cfg = _dc.replace(cfg.brain, mbon_to_dn=True,
                                action_gated_elig=True)
    elif mode == "learn":
        import dataclasses as _dc
        brain.cfg = _dc.replace(cfg.brain, mbon_to_dn=True,
                                action_gated_elig=True,
                                elig_normalize=True)
    elif mode == "learn_trip":
        import dataclasses as _dc
        brain.cfg = _dc.replace(cfg.brain, mbon_to_dn=True,
                                action_gated_elig=True,
                                elig_normalize=True, trip_teaching=True)
    # fee protection for the dopamine threshold: one round trip at the live
    # order size. Without it the deadband sits below the fee, so a break-even
    # trade punished the brain for nothing but the exchange's cut.
    fee_roundtrip = 2.0 * cfg.risk.taker_fee * cfg.risk.order_notional

    warm = cfg.brain.window
    last_eq = None
    halt_px = 0.0                    # price at the moment a halt stopped the run
    pending: list[dict] = []         # orders decided last candle, filled next open
    equity_curve, trades = [], []
    n_dopamine = {"reward": 0, "punish": 0}
    pos_high = 0.0                             # high-watermark for the reflex layer
    prev_close = 0.0
    n_reflex = 0
    exitman = ExitManager(cfg.risk)            # proactive sell-side management
    last_r2 = None
    # per-candle give-back accounting (peak unrealized vs realized per trip)
    give_back = 0.0
    trip_peak = 0.0
    trips = []
    trip_cash0 = 0.0
    n_trips = 0
    sum_realized = 0.0
    sum_peak = 0.0
    loom = LoomingDetector() if getattr(cfg.risk, "loom_exit", False) else None
    entry_ts = 0.0                      # trip interval for sleep replay
    day_trips: list = []                # [(t0, t1, realized)] completed today
    day = ""
    # exposure-matched benchmark: the strategy's own exposure path applied to
    # the market return (see the metric block at the end)
    matched_eq = 1.0
    prev_px_ret = 0.0
    exp_sum = 0.0
    exp_n = 0

    def _close_trip(realized_cash: float, ts: float):
        nonlocal give_back, n_trips, sum_realized, sum_peak, trip_peak, pos_high
        give_back += max(0.0, trip_peak - max(realized_cash, 0.0))
        n_trips += 1
        sum_realized += realized_cash
        sum_peak += trip_peak
        day_trips.append((entry_ts, ts, realized_cash))
        if brain.cfg.trip_teaching and not frozen:
            # one completed trade = one dopamine event, carrying the trade's
            # OWN realized P&L (net of fees) instead of mark-to-market noise.
            # brain.cfg, not cfg.brain: the arm dispatch rewrites the BRAIN's
            # config; the outer Config object still says False
            label = brain.teach_trip(entry_ts, ts, realized_cash,
                                     fee_roundtrip=fee_roundtrip)
            if label:
                n_dopamine[label] += 1
        trip_peak = 0.0
        pos_high = 0.0
        exitman.reset()

    bar_s = float(rows[1][0] - rows[0][0]) if len(rows) > 1 else 60.0
    for i in range(warm, len(rows) - 1):
        hist = rows[:i + 1]
        cur, nxt = rows[i], rows[i + 1]

        # ---- sleep replay: consolidate yesterday's trips at the day boundary
        today = time.strftime("%Y-%m-%d", time.localtime(cur[0]))
        if today != day:
            if day and day_trips and cfg.brain.replay_passes > 0 and mode == "normal":
                brain.replay(day_trips, cfg.brain.replay_passes)
            day, day_trips = today, []

        # ---- execute every pending order at this candle's open --------------
        if pending:
            px = cur[1]
            for od in pending:
                was_flat = broker.pos.base <= 0
                if od["side"] == BUY:
                    if was_flat:
                        trip_cash0 = broker.cash           # basis: cash BEFORE entry
                        trip_peak = 0.0
                        exitman.reset()
                        if loom is not None:
                            loom.reset()
                        entry_ts = cur[0]
                    if broker.cash >= risk.cfg.min_notional:
                        executed = broker.buy(
                            entry_notional(hist, risk.cfg,
                                           risk.cfg.order_notional, bar_s),
                            px, reason=od["reason"])
                        if executed:
                            trades.append(executed.as_dict())
                            risk.record_order(pair, now=cur[0])
                            if broker.pos.base > 0 and pos_high == 0.0:
                                pos_high = px
                else:
                    if broker.position_notional(px) >= risk.cfg.min_notional:
                        executed = broker.sell(px, reason=od["reason"],
                                               frac=od.get("frac"))
                        if executed:
                            trades.append(executed.as_dict())
                            risk.record_order(pair, now=cur[0])
                            if od.get("confirm"):        # latch only on a fill
                                exitman.confirm(od["confirm"])
                            if od.get("kind") == "reflex":
                                n_reflex += 1
                            if broker.pos.base <= 0:
                                _close_trip(broker.cash - trip_cash0, cur[0])
            pending = []

        # ---- mark-to-market equity delta -> dopamine ------------------------
        price = cur[4]
        eq = broker.equity(price)

        # ---- risk guard on EVERY candle, exactly like the live loop --------
        reason = risk.update(eq, now=cur[0])
        if reason:
            risk.halt(reason)
            if broker.pos.base > 0:            # live force-flattens on halt
                t = broker.sell(price, reason=f"bt halt:{reason}")
                if t:
                    trades.append(t.as_dict())
                    risk.record_order(pair, now=cur[0])
                    if broker.pos.base <= 0:
                        _close_trip(broker.cash - trip_cash0, cur[0])
            halt_px = price                    # settle at the price SEEN, not the future
            equity_curve.append([cur[0], broker.equity(price)])
            break

        # ---- exposure path for the matched benchmark ------------------------
        exp = broker.position_notional(price) / max(eq, 1e-9)
        exp_sum += exp
        exp_n += 1
        if prev_px_ret:
            matched_eq *= (1.0 + exp * (price / prev_px_ret - 1.0))
        prev_px_ret = price

        # ---- exits: reactive reflex + proactive scale-out/fatigue ------------
        was_holding = broker.pos.base > 0
        if was_holding:
            pos_high = max(pos_high, price)
            unreal = broker.pos.base * price - broker.pos.cost
            trip_peak = max(trip_peak, unreal)
            why = None
            if mode != "buyhold":
                trail = exitman.trail_distance(hist)
                if loom is not None:
                    # motif: LC4/LC6-style — fire on threat EXPANSION rate
                    # (noise-scaled) instead of the fixed flash-candle level
                    why = loom.step(price, side=1)
                else:
                    why = reflex_exit(prev_close, price, pos_high,
                                      cfg.risk.flash_candle_drop, trail)
                if why:               # full exit (reflex/trail), filled next open
                    pending.append({"side": SELL, "reason": f"reflex:{why}",
                                    "kind": "reflex"})
                else:
                    avg_cost = broker.pos.cost / max(broker.pos.base, 1e-9)
                    reason2 = exitman.check(price, avg_cost, last_r2)
                    if reason2:
                        pending.append({"side": SELL, "reason": reason2,
                                        "frac": 0.5, "kind": "scale",
                                        "confirm": reason2})
        prev_close = price

        delta = 0.0 if last_eq is None else eq - last_eq
        last_eq = eq

        # ---- decide ---------------------------------------------------------
        if mode == "random":
            r = rng.random()
            decision = BUY if r < 0.15 else (SELL if r > 0.85 else "HOLD")
            tel = {"decision": decision, "diff_hz": 0.0}
        elif mode == "buyhold":
            decision = BUY if broker.pos.base == 0 and broker.cash >= risk.cfg.min_notional else "HOLD"
            tel = {"decision": decision, "diff_hz": 0.0}
        else:
            brain.holding_dir = 1 if broker.pos.base > 0 else 0
            tel = brain.observe(hist, delta, frozen=frozen, equity=eq,
                               fee_roundtrip=fee_roundtrip)
            decision = tel["decision"]
            last_r2 = tel.get("r2")
            if tel.get("dopamine"):
                n_dopamine[tel["dopamine"]] += 1

        # ---- risk gate & queue order for next open --------------------------
        if decision in (BUY, SELL) and not any(o["side"] == decision for o in pending):
            ok, _ = risk.allow(pair, decision, broker.position_notional(price),
                               price, eq, now=cur[0])
            if ok:
                pending.append({"side": decision, "reason": f"bt {mode}",
                                "kind": "entry" if decision == BUY else "exit"})
        equity_curve.append([cur[0], broker.equity(price)])

    # ---- close any open position at the end for a fair final number --------
    # a HALTED run settles at the halt candle's price; using the dataset's
    # last close would flatter the result with prices from after the halt
    # (2026-09-15 audit: halt at 10, data ends at 200 -> reported +10%)
    settle_px = halt_px if (risk.halted and halt_px) else rows[-1][4]
    if broker.pos.base > 0:
        t = broker.sell(settle_px,
                        reason="bt halt" if risk.halted else "bt end")
        if t:
            trades.append(t.as_dict())
    final_eq = broker.equity(settle_px)
    if not risk.halted:
        equity_curve.append([rows[-1][0], final_eq])

    # ---- metrics ------------------------------------------------------------
    bh = cfg.quote_budget * rows[-1][4] / rows[warm][4]
    eqs = np.array([e[1] for e in equity_curve])
    peak = np.maximum.accumulate(eqs)
    max_dd = float(((peak - eqs) / peak).max() * 100) if len(eqs) else 0.0
    # a round trip = ALL buys (adds included) vs ALL sells (partials included)
    # until the position is flat again. The old code overwrote open_cost on
    # every BUY, so buy10 + buy10 + sell18 (a net -2 loss) counted as a win
    wins = 0
    rounds = 0
    cost_sum, proceeds, open_qty = 0.0, 0.0, 0.0
    for t in trades:
        if t["side"] == "BUY":
            cost_sum += t["quote"]
            open_qty += t["base"]
        else:
            proceeds += t["quote"]
            open_qty -= t["base"]
            if open_qty <= 1e-12:          # position fully closed = one trip
                rounds += 1
                if proceeds > cost_sum:
                    wins += 1
                cost_sum, proceeds, open_qty = 0.0, 0.0, 0.0
    fees = sum(t["fee"] for t in trades)
    # exposure-matched, fee-charged benchmark. The plain buy&hold figure invests
    # the WHOLE budget from the first candle and pays nothing, while the
    # strategy is capped at order_notional / max_position_notional — so
    # "we beat buy&hold" was mostly saying "we were less invested".
    avg_exposure = exp_sum / exp_n if exp_n else 0.0
    bh_matched_pct = ((matched_eq - 1.0) * 100.0
                      - (fees / max(cfg.quote_budget, 1e-9)) * 100.0)

    result = {
        "pair": pair, "mode": mode, "seed": int(seed), "candles": len(rows) - warm,
        "days": round((rows[-1][0] - rows[warm][0]) / 86400, 2),
        "start_budget": cfg.quote_budget,
        "final_equity": round(final_eq, 4),
        "return_pct": round((final_eq / cfg.quote_budget - 1) * 100, 3),
        "buyhold_return_pct": round((bh / cfg.quote_budget - 1) * 100, 3),
        "avg_exposure": round(avg_exposure, 4),
        "time_in_market_pct": round(avg_exposure * 100, 2),
        "bh_matched_return_pct": round(bh_matched_pct, 3),
        "trades": len(trades), "round_trips": rounds,
        "win_rate_pct": round(wins / rounds * 100, 1) if rounds else 0.0,
        "fees_paid": round(fees, 4), "max_drawdown_pct": round(max_dd, 2),
        "dopamine_events": n_dopamine, "reflex_exits": n_reflex,
        "give_back": round(give_back, 4),
        "trips": n_trips,
        "avg_trip_realized": round(sum_realized / n_trips, 4) if n_trips else 0.0,
        "avg_trip_peak": round(sum_peak / n_trips, 4) if n_trips else 0.0,
        "halted": risk.halted, "halt_reason": risk.halt_reason,
        "equity_curve": equity_curve,
        "trade_log": trades[-60:],
    }
    if verbose:
        print(f"  {pair} [{mode:13s}] {result['days']}d  ret {result['return_pct']:+7.3f}%  "
              f"(buy&hold {result['buyhold_return_pct']:+7.3f}%, matched "
              f"{result['bh_matched_return_pct']:+7.3f}% @ {result['time_in_market_pct']:.1f}% in)  "
              f"trades {len(trades)}  fees {fees:.2f}  maxDD {max_dd:.1f}%  "
              f"win {result['win_rate_pct']}%  DA {n_dopamine}")
    return result


def save_result(result: dict, out_dir: str):
    os.makedirs(out_dir, exist_ok=True)
    name = f"backtest_{result['pair']}_{result['mode']}.json"
    with open(os.path.join(out_dir, name), "w") as f:
        json.dump(result, f, indent=1)
    return name
