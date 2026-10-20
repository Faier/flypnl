"""Polymarket 结构套利雷达（实验 #3，只读观察，无钱包无密钥）。

两类可执行的结构性机会（全部用订单簿最优价计算，不用中价自欺）：
  A. 单市场双边：买 YES@ask + 买 NO@ask，若合计 < 1 - 费率 → 无风险
     （持有 YES+NO 对，结算时兑换 $1）。这就是 2026-03 起 taker 费
     0.75–1.8% 之后还活着的"确定性价差"。
  B. negRisk 互斥组：同事件下所有候选的 YES@ask 合计 < 1 - 费率 → 买全组；
     > 1 + 费率 → 卖全组（需要更大资金，只记录）。

观察期目标：验证调研里的说法（41% 市场出现过套利 / bot 年化 20-60%）
在 2026-09 的费率环境下还剩多少：记录"机会出现的频率、深度、持续时长"。

用法：python3 strategies/polymarket_radar.py [--once]
产出：runs/radar/polymarket.jsonl + runs/radar/reports/polymarket.md
"""
from __future__ import annotations

import json
import sys
import time

from common import append_jsonl, fmt_ts, http_json, read_jsonl_tail, write_report

GAMMA = "https://gamma-api.polymarket.com"
CLOB = "https://clob.polymarket.com"
SCAN_S = 60 * 60
MAX_EVENTS = 30          # 按 24h 量排序取前 N 个事件
MAX_MKTS_PER_EV = 30
FEE = 0.018              # 最坏类目 taker 费（对净盈利），观察期按最坏算
THRESH = 1 - FEE - 0.005  # 扣费后至少 0.5% 空间才算"机会"


def _best(book: dict, side: str) -> tuple:
    """订单簿某侧的最优价与深度（price/size 均为字符串）。"""
    lv = book.get(side) or []
    if not lv:
        return None, 0.0
    px = float(lv[0]["price"])
    if side == "asks":
        px = min(float(x["price"]) for x in lv)
    else:
        px = max(float(x["price"]) for x in lv)
    depth = sum(float(x.get("size") or 0) for x in lv if abs(float(x["price"]) - px) < 1e-9)
    return px, depth


def _yes_no_tokens(m: dict) -> tuple:
    try:
        toks = json.loads(m.get("clobTokenIds") or "[]")
        outs = json.loads(m.get("outcomes") or "[]")
        if len(toks) == 2 and outs[:2] == ["Yes", "No"]:
            return toks[0], toks[1]
    except Exception:
        pass
    return None, None


def scan_once() -> dict:
    try:
        evs = http_json(GAMMA + "/events?active=true&closed=false&limit=100"
                        "&order=volume24hr&ascending=false", timeout=30)
    except Exception:
        evs = http_json(GAMMA + "/events?active=true&closed=false&limit=100", timeout=30)
    if not isinstance(evs, list):
        raise RuntimeError(f"gamma events unexpected: {str(evs)[:120]}")

    now = int(time.time())
    stats = {"ts": now, "events_seen": len(evs), "double_side": [], "neg_groups": []}

    # ---- A. 单市场双边（按事件量序扫描，控制请求数）--------------------
    scanned = 0
    for ev in evs:
        if scanned >= 120:
            break
        for m in (ev.get("markets") or [])[:2]:
            if scanned >= 120 or not m.get("active") or m.get("closed"):
                continue
            yt, nt = _yes_no_tokens(m)
            if not yt:
                continue
            scanned += 1
            try:
                by = http_json(f"{CLOB}/book?token_id={yt}", timeout=15)
                bn = http_json(f"{CLOB}/book?token_id={nt}", timeout=15)
            except Exception as e:
                append_jsonl("polymarket.jsonl", {"ts": now, "book_error": str(e)[:120]})
                continue
            ya, _ = _best(by, "asks")
            na, _ = _best(bn, "asks")
            if ya is None or na is None:
                continue
            cost = ya + na
            if cost < 1:                     # 有意义的双边（扣费后才有肉）
                stats["double_side"].append({
                    "question": (m.get("question") or "")[:80],
                    "yes_ask": ya, "no_ask": na, "cost": cost,
                    "edge_gross": 1 - cost, "edge_net": 1 - cost - FEE,
                    "opportunity": cost <= THRESH})
    # ---- B. negRisk 互斥组 ----------------------------------------------
    neg = [e for e in evs if e.get("negRisk")][:MAX_EVENTS]
    for ev in neg:
        mkts = [m for m in (ev.get("markets") or [])
                if m.get("active") and not m.get("closed")][:MAX_MKTS_PER_EV]
        legs, total = [], 0.0
        ok = True
        for m in mkts:
            yt, _ = _yes_no_tokens(m)
            if not yt:
                ok = False
                break
            try:
                book = http_json(f"{CLOB}/book?token_id={yt}", timeout=15)
            except Exception:
                ok = False
                break
            a, d = _best(book, "asks")
            if a is None:
                ok = False
                break
            legs.append({"q": (m.get("question") or "")[:50], "ask": a, "depth": d})
            total += a
        if ok and legs:
            stats["neg_groups"].append({
                "title": (ev.get("title") or "")[:70], "n": len(legs),
                "sum_yes_ask": total, "edge_gross": 1 - total,
                "edge_net": 1 - total - FEE,
                "opportunity": total <= THRESH, "legs": legs if total <= 1.02 else []})
    append_jsonl("polymarket.jsonl", stats)
    return stats


def md_summary(stats: dict) -> str:
    hist = read_jsonl_tail("polymarket.jsonl", 500)
    real = [h for h in hist if "events_seen" in h]
    n_scans = len(real)
    n_opp_ds = sum(1 for h in real for d in h.get("double_side", []) if d.get("opportunity"))
    n_opp_ng = sum(1 for h in real for g in h.get("neg_groups", []) if g.get("opportunity"))
    near = [(d["question"], d["cost"]) for h in real[-10:] for d in h.get("double_side", [])
            if 1 <= d["cost"] < 1.01]
    lines = [f"# Polymarket 结构套利雷达 — {fmt_ts(time.time())}", "",
             f"- 累计 {n_scans} 次扫描；双边机会（≤{THRESH:.3f}）出现 {n_opp_ds} 次，"
             f"negRisk 组机会出现 {n_opp_ng} 次",
             "", "## 本次扫描：双边（买 YES + 买 NO < $1）", "",
             "| 市场 | YES@ask | NO@ask | 合计 | 毛空间 | 扣费 | 机会 |",
             "|---|---|---|---|---|---|---|"]
    ds = sorted(stats.get("double_side", []), key=lambda d: d["cost"])[:12]
    if not ds:
        lines.append("| （本次无 cost<1 的市场） | | | | | | |")
    for d in ds:
        lines.append(f"| {d['question']} | {d['yes_ask']:.3f} | {d['no_ask']:.3f} "
                     f"| {d['cost']:.3f} | {d['edge_gross']*100:.1f}% "
                     f"| {d['edge_net']*100:.1f}% | {'✅' if d['opportunity'] else '—'} |")
    lines += ["", "## 本次扫描：negRisk 互斥组（Σ YES@ask vs $1）", "",
              "| 事件 | 候选数 | ΣYES@ask | 毛空间 | 扣费 | 机会 |", "|---|---|---|---|---|---|"]
    for g in sorted(stats.get("neg_groups", []), key=lambda g: g["sum_yes_ask"])[:10]:
        lines.append(f"| {g['title']} | {g['n']} | {g['sum_yes_ask']:.3f} "
                     f"| {g['edge_gross']*100:.1f}% | {g['edge_net']*100:.1f}% "
                     f"| {'✅' if g['opportunity'] else '—'} |")
    opp = [g for g in stats.get("neg_groups", []) if g.get("opportunity") and g.get("legs")]
    if opp:
        lines += ["", "### 机会组的腿深度（防尘单：最优卖价的挂单量，单位=份）", ""]
        for g in opp[:3]:
            lines.append(f"**{g['title']}**（合计 {g['sum_yes_ask']:.3f}）：")
            for leg in sorted(g["legs"], key=lambda x: x["ask"])[:8]:
                lines.append(f"- {leg['ask']:.3f} × {leg['depth']:.0f} 份 — {leg['q']}")
            lines.append("")
    if near:
        lines += ["", "## 接近但不够（最近 10 次扫描，1.000–1.010）——费率/价差改善的候选观察名单", ""]
        for q, c in near[:8]:
            lines.append(f"- {c:.4f} {q}")
    lines += ["", f"> 费率假设：taker {FEE:.2%}（最坏类目，2026-03 起）；门径 {THRESH:.3f} = 1−费−0.5%。",
              "> 观察期只回答一个问题：机会出现频率 × 可执行深度，够不够覆盖轮询成本和结算锁定期。"]
    return "\n".join(lines) + "\n"


def main():
    once = "--once" in sys.argv
    while True:
        try:
            s = scan_once()
            write_report("polymarket.md", md_summary(s))
            print(f"[pmark] {fmt_ts(time.time())} double={len(s['double_side'])} "
                  f"neg={len(s['neg_groups'])}", flush=True)
        except Exception as e:
            append_jsonl("polymarket.jsonl", {"ts": int(time.time()), "scan_error": str(e)[:160]})
            print(f"[pmark] scan_error: {str(e)[:160]}", flush=True)
        if once:
            break
        time.sleep(SCAN_S)


if __name__ == "__main__":
    main()
