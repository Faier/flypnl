"""Daily analytics and performance attribution for FlyGate.

Aggregates persistent ledger, per-day JSONL logs, broker states, and futures runner
into clean daily summaries:
- Daily initial & closing equity, net PnL, total trade counts
- Per-asset breakdown: buy/sell volume, filled prices, fees, net profit/loss, and exit reasons
"""
from __future__ import annotations

import glob
import json
import os
import time
from collections import defaultdict


def get_daily_summary(runs_dir: str) -> list[dict]:
    """Compile daily reports for all tracked calendar days, sorted descending (newest first)."""
    ledger_path = os.path.join(runs_dir, "ledger.json")
    ledger = {}
    if os.path.exists(ledger_path):
        try:
            with open(ledger_path, "r", encoding="utf-8") as f:
                ledger = json.load(f)
        except Exception:
            pass

    ledger_days = ledger.get("days", {})

    # Load all trade events and exits from logs
    log_dir = os.path.join(runs_dir, "logs")
    log_files = sorted(glob.glob(os.path.join(log_dir, "flygate-*.jsonl")))

    events_by_day = defaultdict(list)
    for lf in log_files:
        try:
            with open(lf, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        rec = json.loads(line)
                        t_str = rec.get("time", "")
                        day = t_str.split(" ")[0] if " " in t_str else ""
                        if day:
                            events_by_day[day].append(rec)
                    except Exception:
                        continue
        except Exception:
            pass

    # Load all trades directly from broker state files (authoritative execution history)
    broker_states = {}
    trades_by_day = defaultdict(lambda: defaultdict(list))
    for bf in glob.glob(os.path.join(runs_dir, "*USDT.broker.json")):
        base_name = os.path.basename(bf)
        if base_name.startswith("shadow."):
            continue
        pair = base_name.replace(".broker.json", "")
        try:
            with open(bf, "r", encoding="utf-8") as f:
                d = json.load(f)
                broker_states[pair] = d
                for t in d.get("trades", []):
                    ts = float(t.get("ts", 0))
                    if ts > 0:
                        t_day = time.strftime("%Y-%m-%d", time.localtime(ts))
                        trades_by_day[t_day][pair].append(t)
        except Exception:
            pass

    # Read futures state
    fut_state = {}
    fut_path = os.path.join(runs_dir, "fut.state.paper.json")
    if os.path.exists(fut_path):
        try:
            with open(fut_path, "r", encoding="utf-8") as f:
                fut_state = json.load(f)
        except Exception:
            pass

    # Read risk state for today anchor
    risk_state = {}
    risk_path = os.path.join(runs_dir, "risk.json")
    if os.path.exists(risk_path):
        try:
            with open(risk_path, "r", encoding="utf-8") as f:
                risk_state = json.load(f)
        except Exception:
            pass

    today_str = time.strftime("%Y-%m-%d")
    all_days = sorted(set(list(ledger_days.keys()) + list(events_by_day.keys())), reverse=True)

    results = []
    # Base allocations
    default_allocs = {
        "BTC_USDT": 100.0,
        "ETH_USDT": 100.0,
        "SOL_USDT": 100.0,
        "PONS_USDT": 40.0,
        "DOGE_USDT": 100.0,
        "XRP_USDT": 100.0,
    }

    for idx, day in enumerate(all_days):
        day_events = events_by_day.get(day, [])
        l_entry = ledger_days.get(day, {})

        fills = [e for e in day_events if e.get("event") == "fill"]
        trips = [e for e in day_events if e.get("event") == "trip_dopamine"]
        fut_events = [e for e in day_events if e.get("event") in ("fut_close", "fut_fatigue", "futures_started")]

        # Determine equity
        end_eq = l_entry.get("end_eq")
        if end_eq is None:
            # If today and ledger hasn't written end_eq yet
            if day == today_str and risk_state.get("day_start_equity") is not None:
                end_eq = risk_state.get("peak_equity", 740.0)
            else:
                end_eq = 740.0

        # Start equity is previous day's end equity, or day_start_equity from risk
        if day == today_str and risk_state.get("day_start_equity"):
            start_eq = float(risk_state["day_start_equity"])
        elif idx + 1 < len(all_days) and ledger_days.get(all_days[idx + 1], {}).get("end_eq"):
            start_eq = float(ledger_days[all_days[idx + 1]]["end_eq"])
        else:
            start_eq = 740.0

        net_pnl = round(end_eq - start_eq, 4)

        # Pair level aggregation
        pair_agg = defaultdict(lambda: {
            "buys": 0, "sells": 0,
            "buy_vol": 0.0, "sell_vol": 0.0,
            "fee": 0.0, "realized": 0.0,
            "buy_prices": [], "sell_prices": [],
            "reasons": set(),
        })

        # Prefer broker recorded trades for exact precision
        day_broker_trades = trades_by_day.get(day, {})
        has_broker_trades = any(len(v) > 0 for v in day_broker_trades.values())

        if has_broker_trades:
            for p, tr_list in day_broker_trades.items():
                for t in tr_list:
                    s = t.get("side", "").upper()
                    q = float(t.get("quote", 0.0) or 0.0)
                    px = float(t.get("price", 0.0) or 0.0)
                    fee = float(t.get("fee", 0.0) or 0.0)
                    r = t.get("reason", "")
                    pair_agg[p]["fee"] += fee
                    if s == "BUY":
                        pair_agg[p]["buys"] += 1
                        pair_agg[p]["buy_vol"] += q
                        if px > 0:
                            pair_agg[p]["buy_prices"].append(px)
                    elif s == "SELL":
                        pair_agg[p]["sells"] += 1
                        pair_agg[p]["sell_vol"] += q
                        if px > 0:
                            pair_agg[p]["sell_prices"].append(px)
                        if r:
                            pair_agg[p]["reasons"].add(r)
        else:
            for f in fills:
                p = f.get("pair")
                s = f.get("side", "").upper()
                q = float(f.get("quote", 0.0) or 0.0)
                px = float(f.get("price", 0.0) or 0.0)
                fee = float(f.get("fee", 0.0) or 0.0)
                r = f.get("reason", "")
                if not p:
                    continue
                pair_agg[p]["fee"] += fee
                if s == "BUY":
                    pair_agg[p]["buys"] += 1
                    pair_agg[p]["buy_vol"] += q
                    if px > 0:
                        pair_agg[p]["buy_prices"].append(px)
                elif s == "SELL":
                    pair_agg[p]["sells"] += 1
                    pair_agg[p]["sell_vol"] += q
                    if px > 0:
                        pair_agg[p]["sell_prices"].append(px)
                    if r:
                        pair_agg[p]["reasons"].add(r)

        # Map trip realized pnl
        for tr in trips:
            p = tr.get("pair")
            if p in pair_agg:
                pair_agg[p]["realized"] += float(tr.get("realized", 0.0) or 0.0)

        # Build pair detail list
        pairs_list = []
        for pair_name in sorted(set(list(default_allocs.keys()) + list(pair_agg.keys()))):
            data = pair_agg[pair_name]
            b_info = broker_states.get(pair_name, {})
            current_cash = float(b_info.get("cash", default_allocs.get(pair_name, 100.0)))
            current_base = float(b_info.get("base", 0.0))
            alloc = default_allocs.get(pair_name, 100.0)

            # Pair pnl today
            if day == today_str:
                p_pnl = round(current_cash - alloc, 4)
            else:
                p_pnl = round(data["realized"] - data["fee"], 4)

            avg_buy = sum(data["buy_prices"]) / len(data["buy_prices"]) if data["buy_prices"] else 0.0
            avg_sell = sum(data["sell_prices"]) / len(data["sell_prices"]) if data["sell_prices"] else 0.0

            clean_reasons = []
            for r in sorted(data["reasons"]):
                if "diff" in r:
                    clean_reasons.append("神经动能平仓 (" + r.replace("fly DN ", "") + ")")
                elif "reflex" in r:
                    clean_reasons.append("仿生避险闪避 (" + r + ")")
                elif "trend-fatigue" in r:
                    clean_reasons.append("趋势衰竭止盈减仓")
                else:
                    clean_reasons.append(r)

            pairs_list.append({
                "pair": pair_name,
                "name": pair_name.replace("_USDT", ""),
                "buys": data["buys"],
                "sells": data["sells"],
                "buy_vol": round(data["buy_vol"], 2),
                "sell_vol": round(data["sell_vol"], 2),
                "fee": round(data["fee"], 4),
                "pnl": p_pnl,
                "hold_base": current_base if day == today_str else 0.0,
                "current_cash": round(current_cash, 2) if day == today_str else alloc,
                "avg_buy_px": round(avg_buy, 4 if avg_buy < 100 else 2),
                "avg_sell_px": round(avg_sell, 4 if avg_sell < 100 else 2),
                "reasons": clean_reasons or ["-"]
            })

        # Futures summary
        fut_pnl = 0.0
        fut_cash = 200.0
        if day == today_str and fut_state:
            fut_cash = float(fut_state.get("cash", 200.0))
            fut_pnl = round(fut_cash - float(fut_state.get("day_start_eq", 200.0)), 4)

        results.append({
            "date": day,
            "is_today": (day == today_str),
            "start_equity": round(start_eq, 2),
            "end_equity": round(end_eq, 2),
            "net_pnl": net_pnl,
            "trades_count": len(fills),
            "trips_count": len(trips),
            "da_reward": int(l_entry.get("da_reward", 0)),
            "da_punish": int(l_entry.get("da_punish", 0)),
            "futures": {
                "contract": "ZEC_USDT",
                "budget": 200.0,
                "cash": round(fut_cash, 2),
                "pnl": fut_pnl,
                "events_count": len(fut_events)
            },
            "pairs": pairs_list
        })

    return results


def format_daily_cli(runs_dir: str, days: int = 7) -> str:
    """Format an attractive CLI report (safe for all Windows code pages)."""
    summaries = get_daily_summary(runs_dir)
    if days and days > 0:
        summaries = summaries[:days]
    lines = []
    lines.append("=" * 82)
    lines.append("             [FlyGate] 量化交易系统 - 每日盈亏战报与持仓透视")
    lines.append("=" * 82)

    if not summaries:
        lines.append("暂无交易日记录。")
        return "\n".join(lines)

    for s in summaries:
        tag = "[今日]" if s["is_today"] else "[历史]"
        pnl_str = f"{s['net_pnl']:+.2f} USDT"
        lines.append(f"\n* 日期: {s['date']} {tag} | 期初: {s['start_equity']:.2f} U | 期末: {s['end_equity']:.2f} U | 净盈亏: {pnl_str}")
        lines.append(f"   交易概览: 完成买卖 {s['trades_count']} 笔 (闭环 {s['trips_count']} 轮) | 突触奖惩: +{s['da_reward']} 蜜糖 / -{s['da_punish']} 惩罚")
        lines.append("-" * 82)
        lines.append(f"{'标的':<9} | {'买入':<6} | {'卖出':<6} | {'买入量(U)':<10} | {'卖出量(U)':<10} | {'净盈亏(U)':<10} | {'核心决策/出场原因'}")
        lines.append("-" * 82)

        for p in s["pairs"]:
            p_pnl = f"{p['pnl']:+.2f} U" if p['pnl'] != 0 else " 0.00 U"
            reasons_str = "; ".join(p['reasons'][:2])
            lines.append(f"{p['name']:<9} | {p['buys']:<6} | {p['sells']:<6} | {p['buy_vol']:<10.2f} | {p['sell_vol']:<10.2f} | {p_pnl:<10} | {reasons_str}")

        fut = s.get("futures", {})
        if fut:
            f_pnl = f"{fut.get('pnl', 0.0):+.2f} U"
            lines.append(f"{'ZEC(合约)':<9} | {'-':<6} | {'-':<6} | {'-':<10} | {'-':<10} | {f_pnl:<10} | 趋势相干性衰减主动平仓")

        lines.append("-" * 82)

    return "\n".join(lines)
