"""Price every MBON veto: join runs/counterfactual.jsonl with the frozen
twin's roundtrips.

The engine logs an event every time the learning fly's DN fired but a gate
(g / r2 / marg / ring) vetoed the entry; the frozen twin's decision on that
same candle is attached. This script reconstructs the twin's roundtrips from
its broker files, matches each veto to the twin trade that would have been
the counterfactual (same pair, same side, entry within the candle window),
and prices the veto with the twin's realized outcome:

    veto saved money  <->  the would-be trade lost (realized < 0)
    veto cost money   <->  the would-be trade won  (realized > 0)

Breakdown by block reason answers "what is each gate worth" — the question
the aggregate A/B gap cannot attribute.

Usage: python3 scripts/cf_report.py [--runs runs] [--interval 60]
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from collections import defaultdict

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def load_events(path: str) -> list:
    if not os.path.exists(path):
        return []
    out = []
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue          # a torn final line must not kill the report
    return out


def roundtrips(broker_path: str) -> list:
    """[(entry_ts, exit_ts, side, realized_quote_pnl)] from a broker file.
    Same reconstruction the A/B analysis uses: weighted-average cost basis,
    fees on both legs, partial sells pro-rated."""
    try:
        with open(broker_path) as f:
            d = json.load(f)
    except (OSError, json.JSONDecodeError):
        return []
    pos, cost, entry_ts, out = 0.0, 0.0, None, []
    for t in d.get("trades", []):
        if t["side"] == "BUY":
            pos += t["base"]
            cost += t["quote"] + t["fee"]
            if entry_ts is None:
                entry_ts = t["ts"]
        else:
            if pos <= 0:
                continue
            frac = min(t["base"] / pos, 1.0)
            realized = t["base"] * t["price"] - t["fee"] - cost * frac
            out.append((entry_ts, t["ts"], "BUY", realized))
            cost *= (1.0 - frac)
            pos -= t["base"]
            if pos <= 1e-9:
                pos, cost, entry_ts = 0.0, 0.0, None
    return out


def match(events: list, rts: dict, interval_s: float) -> list:
    """Attach the twin roundtrip whose entry falls in the event's candle
    window: the twin acts seconds after the same candle close, so the entry
    lands within [ts + 0.5*interval, ts + 3*interval]."""
    matched = []
    for ev in events:
        rt = None
        for cand in rts.get(ev.get("pair"), []):
            if cand[2] != ev.get("side"):
                continue
            if ev["ts"] + 0.5 * interval_s <= cand[0] <= ev["ts"] + 3 * interval_s:
                rt = cand
                break
        matched.append((ev, rt))
    return matched


def report(events: list, rts: dict, interval_s: float) -> str:
    matched = match(events, rts, interval_s)
    by_reason = defaultdict(lambda: {"n": 0, "matched": 0, "pnl": 0.0,
                                     "wins": 0, "losses": 0})
    unmatched = 0
    for ev, rt in matched:
        r = by_reason[ev.get("block") or "?"]
        r["n"] += 1
        if rt is None:
            unmatched += 1
            continue
        r["matched"] += 1
        r["pnl"] += rt[3]
        r["wins" if rt[3] > 0 else "losses"] += 1

    lines = []
    lines.append(f"events: {len(events)}  (no twin trade matched: {unmatched})")
    lines.append("")
    lines.append(f"| {'block':6s} | {'vetoes':>6s} | {'matched':>7s} | "
                 f"{'would-win':>9s} | {'would-lose':>10s} | {'twin pnl (U)':>12s} | "
                 f"veto was...|")
    lines.append("|" + "---|" * 6)
    total_saved = 0.0
    for reason, r in sorted(by_reason.items()):
        # veto value = -(pnl of the trade it suppressed): suppressing a winner
        # cost us, suppressing a loser saved us
        saved = -r["pnl"]
        total_saved += saved
        lines.append(
            f"| {reason:6s} | {r['n']:6d} | {r['matched']:7d} "
            f"| {r['wins']:9d} | {r['losses']:10d} | {r['pnl']:+12.4f} | "
            f"{'净省钱' if saved > 0 else '净亏钱'} |")
    lines.append("")
    if total_saved >= 0:
        lines.append(f"veto 净值: +{total_saved:.4f} U —— 门槛在替我们挡亏损单")
    else:
        lines.append(f"veto 净值: {total_saved:.4f} U —— 门槛拦掉的组合单是赚的"
                     f"(冻结蝇赢的那部分)")
    return "\n".join(lines)


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--runs", default=os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "runs"))
    ap.add_argument("--interval", type=int, default=60,
                    help="engine candle interval in seconds (1m default)")
    args = ap.parse_args()

    events = load_events(os.path.join(args.runs, "counterfactual.jsonl"))
    if not events:
        print("no counterfactual events yet — the engine appends to "
              "runs/counterfactual.jsonl whenever a MBON gate vetoes a fired DN")
        return
    rts = {}
    for f in os.listdir(args.runs):
        if f.startswith("shadow.") and f.endswith(".broker.json"):
            pair = f[len("shadow."):-len(".broker.json")]
            rt = roundtrips(os.path.join(args.runs, f))
            if rt:
                rts[pair] = rt
    text = report(events, rts, args.interval)
    print(text)
    out = os.path.join(args.runs, "cf_report.txt")
    with open(out, "w") as f:
        f.write(text + "\n")
    print(f"\nwritten: {out}")


if __name__ == "__main__":
    main()
