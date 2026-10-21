"""carry_picks.py — 费率选币实盘决策工具（只读，不下单）。

回答一个问题：「今天真金白银能做谁、为什么」。
选币门径与 carry_paper 完全同源（同一套函数，不是另一套标准）：

  1. 持续性门：观测均值 ≥30% 且当前 ≥15% 且历史最差 ≥10%（档案来自
     runs/radar/funding.jsonl，雷达/纸面机持续在攒）
  2. 挤兑排除：瞬时年化 >300% = 挤兑区，跳过
  3. 流动性/深度：24h 量 ≥$3M、同所现货存在、两腿各 40U 深度吃得满
  4. 跨所选所：同币多所过门 → 持续性均值最高者
  5. 净盈利门：回本期（含 0.8% taker 往返成本）≤7 天

输出三张榜：
  ✅ READY — 全门通过，可按 pilot 纪律进场（结算刚过最优）
  ⏳ NEAR  — 差一条门的（差什么、差多少，门关了也知道等多久）
  ⛔ 深度不足 — 费率肥但腿吃不满（灰尘市场，列出来防手痒）

用法：python3 tools/carry_picks.py
产出：终端榜单 + state/carry_picks.json（自用，不进公开 data/——榜单是
自己的决策记录，公开发布就是喊单，红线）。
"""
from __future__ import annotations

import json
import os
import pathlib
import sys
import time

_HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE.parent / "engine" / "strategies"))

from carry_paper import (APR_MIN_ABS, APR_MIN_LAST, APR_MIN_MEAN, APR_TRAP,
                         BE_MAX_DAYS, MIN_VOL, VENUES, break_even_days,
                         candidates, pick_best_venue, probe_legs,
                         round_trip_cost, spot_universe)
from common import read_jsonl_tail
from funding_radar import fetch_bybit, fetch_gate, fetch_okx

try:
    from carry_paper import fetch_binance
except Exception:      # pragma: no cover - 代理不可达时的优雅降级
    fetch_binance = None

STATE = _HERE.parent / "state" / "carry_picks.json"


def persistence_profile(runs_dir: str) -> dict:
    """与 carry_paper 同款：funding.jsonl 尾部 4 万行 → 每合约 {n,mean,min}。
    common.RUNS 即 engine/runs/radar，与雷达/纸面机同一份档案。
    funding.jsonl 缺档时并入 gate_watch 的 4h 桶历史（自建结算序列，
    桶费率年化 = rate × 6 × 365）——PONS 在这里攒了 60+ 桶。"""
    pers = {}
    for r in read_jsonl_tail("funding.jsonl", 40000):
        if "contract" not in r:
            continue
        k = f"{r.get('exchange', 'gate')}:{r['contract']}"
        pers.setdefault(k, []).append(r["funding_ann"])
    gw = _HERE.parent / "state" / "gate_watch.json"
    if gw.exists():
        try:
            hist = json.loads(gw.read_text()).get("history", {})
            vals = [float(v) * 6 * 365 for v in hist.values()]
            if len(vals) >= 4:
                pers.setdefault("gate:PONS_USDT", []).extend(vals)
        except Exception:
            pass
    return {k: {"n": len(v), "mean": sum(v) / len(v), "min": min(v)}
            for k, v in pers.items() if len(v) >= 4}


def main() -> None:
    rows, venue_errors = [], {}
    fetchers = [("gate", fetch_gate), ("bybit", fetch_bybit), ("okx", fetch_okx)]
    if fetch_binance is not None:
        fetchers.append(("binance", fetch_binance))
    for name, fn in fetchers:
        try:
            rows += fn()
        except Exception as e:
            venue_errors[name] = str(e)[:80]

    runs_dir = str(_HERE.parent / "engine" / "runs" / "radar")
    pers = persistence_profile(runs_dir)
    spot_universes = {}
    for venue in VENUES:
        if any(r["exchange"] == venue for r in rows):
            try:
                su = spot_universe(venue)
            except Exception:
                su = set()
            if su:
                spot_universes[venue] = su

    # ---- 三张榜 -------------------------------------------------------
    ready, near, thin = [], [], []
    seen_base = set()
    ranked = []
    for r in rows:
        if r["exchange"] not in VENUES or r["vol24h_usd"] < MIN_VOL:
            continue
        key = f"{r['exchange']}:{r['contract']}"
        p = pers.get(key, {})
        # 近门判定用（未过全门的也进来排）
        ranked.append((r, p))

    def _rec(r, p):
        return {"venue": r["exchange"], "base": r["base"],
                "apr_now": round(r["funding_ann"], 4),
                "mean": round(p.get("mean", 0.0), 4),
                "min": round(p.get("min", 0.0), 4),
                "n": p.get("n", 0)}

    # 全门通过的（candidates + 跨所选所），逐个做深度/净盈利终审
    cands = {f"{v}:{r['contract']}": (v, r, p)
             for v, r, p in candidates(rows, pers, spot_universes)}
    cands, drops = pick_best_venue(cands)
    for _, r, p in cands.values():
        base = r["base"]
        if base in seen_base:       # 跨所选所后仍防重（保险）
            continue
        probe = probe_legs(r["exchange"], base)
        rec = _rec(r, p)
        if probe is None:
            rec["why"] = "深度不足/过桥费>0.5%"
            thin.append(rec)
            continue
        be_t = break_even_days(r["funding_ann"],
                               round_trip_cost(r["exchange"], probe["entry_cost"]))
        be_m = break_even_days(r["funding_ann"],
                               round_trip_cost(r["exchange"], 0.0, maker=True))
        rec.update({"be_taker_d": round(be_t, 1), "be_maker_d": round(be_m, 1),
                    "bridge": round(probe["entry_cost"], 5)})
        if be_t > BE_MAX_DAYS and be_m > BE_MAX_DAYS:
            rec["why"] = f"回本期 {be_m:.0f}~{be_t:.0f} 天 > {BE_MAX_DAYS:.0f}"
            near.append(rec)
        else:
            seen_base.add(base)
            ready.append(rec)

    # 近门：差一条门的（只看执行所、量够的）
    near_keys = {(x["venue"], x["base"]) for x in near}
    for r, p in ranked:
        if (r["exchange"], r["base"]) in near_keys or r["base"] in seen_base:
            continue
        if not (APR_MIN_LAST <= r["funding_ann"] <= APR_TRAP):
            continue
        gaps = []
        if p.get("mean", 0.0) < APR_MIN_MEAN:
            gaps.append(f"均值 {p.get('mean', 0):.0%}<{APR_MIN_MEAN:.0%}")
        if p.get("min", 0.0) < APR_MIN_ABS:
            gaps.append(f"最差 {p.get('min', 0):.0%}<{APR_MIN_ABS:.0%}")
        if not p:
            gaps.append("无档案(新上架)")
        if gaps and len(near) < 8:
            near.append({**_rec(r, p), "why": "；".join(gaps)})

    out = {"ts": int(time.time()), "ready": ready, "near": near,
           "thin": thin[:5], "venue_errors": venue_errors}
    STATE.parent.mkdir(parents=True, exist_ok=True)
    STATE.write_text(json.dumps(out, ensure_ascii=False, indent=1))

    t = time.strftime("%m-%d %H:%M")
    print(f"════════ 费率选币 · {t} ════════")
    if venue_errors:
        print(f"⚠️ 行情缺席: {', '.join(venue_errors)}")
    print(f"\n✅ READY（全门通过，结算刚过进场最优）")
    if ready:
        for x in ready:
            print(f"  {x['venue']:>7} {x['base']:<8} 瞬时{x['apr_now']:+.0%} "
                  f"均值{x['mean']:+.0%}(n={x['n']}) 回本期 taker {x['be_taker_d']}d "
                  f"/ maker {x['be_maker_d']}d 过桥{x['bridge']:.3%}")
    else:
        print("  （无 —— 门全开的候选一个都没有，空仓也是仓位）")
    print(f"\n⏳ NEAR（差一条门）")
    for x in near[:8]:
        print(f"  {x.get('venue', '?'):>7} {x.get('base', '?'):<8} "
              f"瞬时{x['apr_now']:+.0%} 均值{x.get('mean', 0):+.0%} → {x.get('why', '')}")
    if thin:
        print(f"\n⛔ 费率肥但深度不足（防手痒清单）")
        for x in thin:
            print(f"  {x['venue']:>7} {x['base']:<8} 瞬时{x['apr_now']:+.0%} → {x['why']}")
    print(f"\n状态: {STATE}")


if __name__ == "__main__":
    main()
