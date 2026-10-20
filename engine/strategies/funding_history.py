"""资金费率历史回补 —— 把实验#2 的"14 天观察期"在分钟级压缩掉（只读公开接口）。

用户的正确直觉：能拿历史数据验证的就别攒实时数据。费率历史 bybit/okx
公开可回补（gate 的历史端点实测要签名且返回 404，gate 持续性由雷达
自建时序积累）。本工具：
  1. 从 funding.jsonl 最新快照取每所 Top8 候选 + 观察名单
  2. 回补各家费率历史（bybit 200/页，okx 100/页，最多 ~200 天）
  3. 输出持续性体检表：均值/中位/最小年化、正费率占比、近30天 vs 更早
     （衰减检测），并给毕业判定

用法：python3 strategies/funding_history.py [--once]
产出：runs/radar/reports/funding_history.md（每次运行覆盖）
"""
from __future__ import annotations

import statistics
import sys
import time
from datetime import datetime

from common import fmt_ts, http_json, read_jsonl_tail, write_report

WATCHLIST = ["BTC", "ETH", "SOL", "ZEC", "DOGE", "XRP", "XAU"]
TOP_PER_VENUE = 8
MAX_PAGES = 6            # bybit 200×6=1200条 / okx 100×6=600条
GATE_MEAN_GATE = 0.15    # 毕业线：全程年化均值 ≥15%
GATE_MIN_GATE = 0.075    # 全程最小年化 ≥7.5%（最差结算也不塌）


def _candidates() -> dict:
    """最新快照 → {venue: [(symbol, funding_ann)]}（Top8 + 观察名单）。"""
    latest = {}
    for r in read_jsonl_tail("funding.jsonl", 4000):
        if "contract" in r and r.get("vol24h_usd", 0) >= 3_000_000:
            r.setdefault("exchange", "gate")   # 旧单所时代记录没有 exchange
            latest[r["exchange"] + ":" + r["contract"]] = r
    out: dict[str, list] = {}
    for k, r in latest.items():
        out.setdefault(r["exchange"], []).append(
            (r["contract"], r["funding_ann"], r["base"]))
    sel = {}
    for venue, items in out.items():
        items.sort(key=lambda x: -x[1])
        picks = items[:TOP_PER_VENUE]
        seen = {b for _, _, b in picks}
        for c, a, b in items:
            if b in WATCHLIST and b not in seen:
                picks.append((c, a, b))
                seen.add(b)
        sel[venue] = picks
    return sel


def hist_bybit(sym: str) -> list:
    """[(ts_ms, rate)] 升序，带游标翻页。"""
    out, cursor = [], None
    for _ in range(MAX_PAGES):
        url = ("https://api.bybit.com/v5/market/funding/history?category=linear"
               f"&symbol={sym}&limit=200" + (f"&cursor={cursor}" if cursor else ""))
        d = http_json(url, timeout=20, retries=3)
        lst = (d.get("result") or {}).get("list") or []
        if not lst:
            break
        out += [(int(r["fundingRateTimestamp"]), float(r["fundingRate"]))
                for r in lst]
        cursor = (d.get("result") or {}).get("nextPageToken")
        if not cursor:
            break
        time.sleep(0.1)
    return sorted(out)


def hist_okx(inst: str) -> list:
    """[(ts_ms, rate)] 升序，'after' 翻页（after=最早一条的 ts）。"""
    out, after = [], None
    for _ in range(MAX_PAGES):
        url = ("https://www.okx.com/api/v5/public/funding-rate-history?instId="
               f"{inst}&limit=100" + (f"&after={after}" if after else ""))
        d = http_json(url, timeout=20, retries=3)
        rows = d.get("data") or []
        if not rows:
            break
        out += [(int(r["fundingTime"]), float(r["fundingRate"])) for r in rows]
        after = min(int(r["fundingTime"]) for r in rows)
        if len(rows) < 100:
            break
        time.sleep(0.1)
    return sorted(out)


def analyze(hist: list) -> dict | None:
    """间隔中位数自动推断（不猜 8h），输出持续性统计。"""
    if len(hist) < 30:
        return None
    ts = [t for t, _ in hist]
    rates = [r for _, r in hist]
    step_h = statistics.median(b - a for a, b in zip(ts, ts[1:])) / 3_600_000
    if step_h <= 0:
        return None
    ann = [r * (24.0 / step_h) * 365 for r in rates]
    cutoff = ts[-1] - 30 * 86400_000
    old = [a for t, a in zip(ts, ann) if t < cutoff]
    rec = [a for t, a in zip(ts, ann) if t >= cutoff]
    return {
        "n": len(hist), "days": (ts[-1] - ts[0]) / 86400_000,
        "step_h": step_h,
        "mean": statistics.mean(ann), "median": statistics.median(ann),
        "min": min(ann), "p10": sorted(ann)[max(0, len(ann) // 10)],
        "pos_pct": 100.0 * sum(1 for a in ann if a > 0) / len(ann),
        "old_mean": statistics.mean(old) if old else None,
        "rec_mean": statistics.mean(rec) if rec else None,
        "pass": statistics.mean(ann) >= GATE_MEAN_GATE
                and min(ann) >= GATE_MIN_GATE and (ts[-1] - ts[0]) >= 60 * 86400_000,
    }


FETCH = {"bybit": hist_bybit, "okx": hist_okx}


def run_once():
    cands = _candidates()
    results = []            # (venue, symbol, ann_now, stats)
    for venue, picks in cands.items():
        fn = FETCH.get(venue)
        if fn is None:
            continue        # gate：无公开历史接口 → 雷达自建时序负责
        for sym, ann_now, _base in picks:
            try:
                st = analyze(fn(sym))
                if st:
                    results.append((venue, sym, ann_now, st))
                else:
                    results.append((venue, sym, ann_now, None))
                time.sleep(0.15)
            except Exception as e:
                print(f"[hist] {venue}:{sym} error {str(e)[:80]}", flush=True)

    lines = [f"# 费率历史回补体检 — {fmt_ts(time.time())}", "",
             f"> 数据跨度 bybit ~140 天 / okx ~60 天（各所接口上限内）；间隔按中位数自动推断。",
             f"> 毕业线：全程年化均值 ≥{GATE_MEAN_GATE:.0%} 且最小 ≥{GATE_MIN_GATE:.1%} 且跨度 ≥60 天。",
             "> gate 无公开历史接口——gate 标的靠雷达自建时序（15min/拍）积累。",
             "", "## 体检表（按全程年化均值排序）", "",
             "| 所 | 合约 | 现值年化 | 全程均值 | 中位 | 最差 | P10 | 正费率% | 近30天 | 更早 | 天数 | 毕业 |",
             "|---|---|---|---|---|---|---|---|---|---|---|---|"]
    for venue, sym, ann_now, st in sorted(
            [r for r in results if r[3]], key=lambda x: -x[3]["mean"]):
        s = st
        decay = ""
        if s["old_mean"] and s["rec_mean"]:
            decay = (f"{s['rec_mean']*100:+.0f}% vs {s['old_mean']*100:+.0f}%")
        lines.append(
            f"| {venue} | {sym} | {ann_now*100:+.0f}% | {s['mean']*100:+.1f}% "
            f"| {s['median']*100:+.1f}% | {s['min']*100:+.1f}% | {s['p10']*100:+.1f}% "
            f"| {s['pos_pct']:.0f}% | {decay} | — | {s['days']:.0f} "
            f"| {'✅' if s['pass'] else '—'} |")
    for venue, sym, ann_now, st in [r for r in results if not r[3]]:
        lines.append(f"| {venue} | {sym} | {ann_now*100:+.0f}% | 样本不足(<30) | | | | | | | | — |")
    write_report("funding_history.md", "\n".join(lines) + "\n")
    n_pass = sum(1 for r in results if r[3] and r[3]["pass"])
    print(f"[hist] done: {len(results)} checked, {n_pass} PASS", flush=True)


if __name__ == "__main__":
    run_once()
