"""资金费率漏洞的纸面执行测试机（实验 #5 —— 只模拟，不下单）。

雷达只回答"漏洞在哪"；这一台回答"我们真能吃到吗"。它对每个候选开一对
纸面腿（同所 买现货 + 空永续），拿真实订单簿走深度成交，然后 7x24 盯四件
纸上算不出来的事：

  1. 两腿真实滑点（开仓和平仓都走订单簿深度，不是最优价自欺）
  2. 资金费率翻负（空腿从收钱变付钱的时刻，逐次结算记账）
  3. 空腿保证金压力（2x 下价格 +50% 强平；本机在 +25% 就报警，>30% 撤退）
  4. 实现 APR vs 进场时名义 APR 的差（衰减 = 漏洞关门的速度）

执行所（同所现货+永续两腿）：
  gate    —— 主执行所（本机直连；独有标的多）
  binance —— 费率只有 Gate 一半（现货 0.1% vs 0.2%），经系统代理可达；
             代理一关，本轮自动缺席，gate 照跑（优雅降级）。轻量轮询
             （3 个批量调用/15min），远离雷达时代踩过的 418 限流雷区。
  bybit/okx —— 只进持续性统计，不做执行。

出场规则沿用 arb_sim P2 的语义：跟踪 7 次结算年化均值 < 5% 离场、持有
>45 天强制离场、费率连续 2 次观测为负立即离场。
毕业门径：≥3 个已平仓组合的实现APR ≥ 进场APR的一半且无保证金告警，
才允许 50U 真金白银的最小实腿。
用法：python3 strategies/carry_paper.py [--once]
产出：runs/radar/carry.jsonl + runs/radar/reports/carry.md + carry_state.json
"""
from __future__ import annotations

import math
import sys
import time

from common import (append_jsonl, fmt_pct, fmt_ts, http_json, read_jsonl_tail,
                    read_state, write_report, write_state)
from funding_radar import ann_funding, fetch_bybit, fetch_gate, fetch_okx

SCAN_S = 15 * 60
LEG_USD = 50.0            # 每条腿的纸面名义（既真实又亏不心疼）
LEVERAGE = 2.0            # 空腿保证金模拟倍数（2x -> 强平约 +50%）
MAX_POSITIONS = 3
MIN_VOL = 3_000_000
APR_MIN_MEAN = 0.30       # 持续性门槛：观测均值 ≥ 30%（比雷达展示的 15% 更挑）
APR_MIN_LAST = 0.15       # 且当前值 ≥ 15%
APR_MIN_ABS = 0.10        # 历史最差观测也 ≥ 10%（日内波动允许，整段崩塌不允许）
APR_TRAP = 3.0            # >300% 年化 = 挤兑区，trap_screener 语义，跳过
G_EXIT = 0.05             # 跟踪 7 次结算年化均值 < 5% -> 离场
G_MAX_HOLD_D = 45
NEG_FLIPS_EXIT = 2        # 费率连续 2 次观测为负 -> 离场
PROBE_USD = LEG_USD       # 试单深度按真实腿大小走
MARGIN_WARN = 1.25        # 价格涨过进场价 25% -> 保证金告警（强平约 50%）
ENTRY_COST_MAX = 0.005    # 两腿过桥费 >0.5% 不吃

GATE_SPOT = "https://api.gateio.ws/api/v4/spot"
GATE_FUT = "https://api.gateio.ws/api/v4/futures/usdt"
BIN_SPOT = "https://api.binance.com/api/v3"
BIN_FUT = "https://fapi.binance.com/fapi/v1"

# 每个执行所：符号构造、盘口/行情地址、taker 费率
VENUES = {
    "gate": {
        "spot_sym": lambda b: b + "_USDT",
        "fut_sym": lambda b: b + "_USDT",
        "spot_book": lambda s: f"{GATE_SPOT}/order_book?currency_pair={s}&limit=30",
        "fut_book": lambda s: f"{GATE_FUT}/order_book?contract={s}&limit=30",
        "fee_spot": 0.002, "fee_fut": 0.0005,
    },
    "binance": {
        "spot_sym": lambda b: b + "USDT",
        "fut_sym": lambda b: b + "USDT",
        "spot_book": lambda s: f"{BIN_SPOT}/depth?symbol={s}&limit=30",
        "fut_book": lambda s: f"{BIN_FUT}/depth?symbol={s}&limit=30",
        "fee_spot": 0.001, "fee_fut": 0.0005,
    },
}


def walk_book(book, side: str, usd: float) -> tuple:
    """沿订单簿深度吃单。返回 (均价, 最差价, 吃到的USD, 层)。
    Gate 现货簿是 [[px, amt],...]，永续簿是 [{"p":..,"s":..},...]；Binance
    两边都是 [[px, amt],...] —— 前两种格式都兼容。"""
    levels = book.get("asks" if side == "ask" else "bids") or []
    filled_usd, filled_amt, worst, used = 0.0, 0.0, 0.0, 0
    for lv in levels:
        if isinstance(lv, dict):
            px_s, amt_s = lv.get("p"), lv.get("s")
        else:
            px_s, amt_s = lv[0], lv[1]
        px, amt = float(px_s), float(amt_s)
        if px <= 0 or amt <= 0:
            continue
        take_usd = min(usd - filled_usd, px * amt)
        filled_usd += take_usd
        filled_amt += take_usd / px
        worst = px
        used += 1
        if filled_usd >= usd - 1e-9:
            break
    avg = filled_usd / filled_amt if filled_amt > 0 else 0.0
    return avg, worst, filled_usd, used


def _top(book, side: str) -> tuple:
    lv = (book.get(side) or [None])[0]
    if lv is None:
        return 0.0, 0.0
    if isinstance(lv, dict):
        return float(lv.get("p") or 0), float(lv.get("s") or 0)
    return float(lv[0]), float(lv[1])


def mid_of(book) -> float:
    a, _ = _top(book, "asks")
    b, _ = _top(book, "bids")
    return (a + b) / 2 if (a > 0 and b > 0) else 0.0


def probe_legs(venue: str, base: str) -> dict | None:
    """真实深度试单：两条腿各吃 LEG_USD。任何一条腿吃不满（灰尘/幻影
    流动性）返回 None —— 这是本机最核心的过滤器。"""
    v = VENUES[venue]
    try:
        s_book = http_json(v["spot_book"](v["spot_sym"](base)),
                           timeout=15, retries=2)
        f_book = http_json(v["fut_book"](v["fut_sym"](base)),
                           timeout=15, retries=2)
        s_ask, _, s_fill, _ = walk_book(s_book, "ask", PROBE_USD)
        f_bid, _, f_fill, _ = walk_book(f_book, "bid", PROBE_USD)
        if s_fill < PROBE_USD * 0.98 or f_fill < PROBE_USD * 0.98:
            return None                      # 一条腿吃不满 = 灰尘市场
        s_mid, f_mid = mid_of(s_book), mid_of(f_book)
        if s_mid <= 0 or f_mid <= 0:
            return None
        cost = (s_ask / s_mid - 1.0) + (1.0 - f_bid / f_mid)
        if cost > ENTRY_COST_MAX:
            return None                      # 过桥费太贵，漏洞被成本吃掉
        return {"spot_px": s_ask, "fut_px": f_bid, "spot_mid": s_mid,
                "fut_mid": f_mid, "entry_cost": cost}
    except Exception:
        return None


# ----------------------------------------------------------------- 行情拉取
def fetch_binance() -> list:
    """轻量轮询（3 个批量调用/15min）：经系统代理可达；代理一关抛错，
    cycle 捕获后 binance 本轮缺席，gate 照跑。"""
    prem = http_json(f"{BIN_FUT}/premiumIndex", timeout=20, retries=3)
    interval = {}
    try:
        info = http_json(f"{BIN_FUT}/fundingInfo", timeout=20, retries=3)
        for c in info:
            if c.get("fundingIntervalHours"):
                interval[c["symbol"]] = float(c["fundingIntervalHours"])
    except Exception:
        pass                     # 只有非 8h 间隔的例外才在列表里；缺省 8h
    vols = {}
    try:
        for t in http_json(f"{BIN_FUT}/ticker/24hr", timeout=20, retries=3):
            vols[t.get("symbol")] = float(t.get("quoteVolume") or 0)
    except Exception:
        pass
    rows = []
    for d in prem:
        sym = d.get("symbol") or ""
        if not sym.endswith("USDT"):
            continue
        try:
            rows.append({"ts": int(time.time()), "exchange": "binance",
                         "contract": sym, "base": sym[:-4],
                         "funding_rate": float(d.get("lastFundingRate") or 0),
                         "interval_h": interval.get(sym, 8.0),
                         "funding_ann": ann_funding(float(d.get("lastFundingRate") or 0),
                                                    interval.get(sym, 8.0)),
                         "basis": 0.0,
                         "vol24h_usd": vols.get(sym, 0.0)})
        except Exception:
            continue
    return rows


def venue_marks(venue: str, spot_syms: set,
                fut_syms: set) -> tuple[dict, dict]:
    """(fut_marks, spot_marks)，键为该所自己的符号。fut 行含 funding_rate/
    mark_price，spot 行含 last/highest_bid —— 两个所归一到同一接口。"""
    try:
        if venue == "gate":
            ft = {t["contract"]: t for t in
                  (http_json(f"{GATE_FUT}/tickers", timeout=15, retries=2) or [])
                  if t.get("contract") in fut_syms}
            sm = {t["currency_pair"]: t for t in
                  (http_json(f"{GATE_SPOT}/tickers", timeout=15, retries=2) or [])
                  if t.get("currency_pair") in spot_syms}
            return ft, sm
        if venue == "binance":
            ft = {d["symbol"]: {"funding_rate": d.get("lastFundingRate"),
                                "mark_price": d.get("markPrice")}
                  for d in (http_json(f"{BIN_FUT}/premiumIndex",
                                      timeout=20, retries=2) or [])
                  if d.get("symbol") in fut_syms}
            sm = {t["symbol"]: {"last": t.get("bidPrice"),
                                "highest_bid": t.get("bidPrice"),
                                "lowest_ask": t.get("askPrice")}
                  for t in (http_json(f"{BIN_SPOT}/ticker/bookTicker",
                                      timeout=20, retries=2) or [])
                  if t.get("symbol") in spot_syms}
            return ft, sm
    except Exception:
        pass
    return {}, {}


def spot_universe(venue: str) -> set:
    """该所全部现货符号（同所对冲的先决条件）。"""
    try:
        if venue == "gate":
            return {t["currency_pair"] for t in
                    (http_json(f"{GATE_SPOT}/tickers", timeout=15, retries=2) or [])}
        if venue == "binance":
            return {t["symbol"] for t in
                    (http_json(f"{BIN_SPOT}/ticker/bookTicker",
                               timeout=20, retries=2) or [])}
    except Exception:
        pass
    return set()


# ----------------------------------------------------------------- 候选
def candidates(rows: list, pers: dict, spot_universes: dict) -> list:
    """同所现货必须存在；量大；当前肥 + 历史持续肥 + 最差也不塌；非挤兑区。"""
    out = []
    for r in rows:
        venue = r["exchange"]
        if venue not in VENUES:
            continue
        spot_sym = VENUES[venue]["spot_sym"](r["base"])
        if r["vol24h_usd"] < MIN_VOL or spot_sym not in spot_universes.get(venue, set()):
            continue
        if not (APR_MIN_LAST <= r["funding_ann"] <= APR_TRAP):
            continue
        p = pers.get(f"{venue}:{r['contract']}", {})
        if p.get("mean", 0) < APR_MIN_MEAN or p.get("min", 0) < APR_MIN_ABS:
            continue
        out.append((venue, r, p))
    return out


def open_position(venue: str, r: dict, p: dict, now: int) -> dict | None:
    probe = probe_legs(venue, r["base"])
    if probe is None:
        return None                     # 深度不足 / 过桥费 >0.5%：不吃
    v = VENUES[venue]
    interval_h = float(r["interval_h"]) or 8.0
    return {"opened": now, "venue": venue, "base": r["base"],
            "contract": v["fut_sym"](r["base"]),
            "spot_sym": v["spot_sym"](r["base"]),
            "fut_sym": v["fut_sym"](r["base"]),
            "notional": LEG_USD, "leverage": LEVERAGE,
            "spot_entry": probe["spot_px"], "fut_entry": probe["fut_px"],
            "entry_cost": round(probe["entry_cost"], 6),
            "interval_h": interval_h,
            "apr_entry": round(r["funding_ann"], 4),
            "apr_obs_mean": round(p.get("mean", 0), 4),
            "funding_received": 0.0, "settlements": 0, "neg_flips": 0,
            "last_settle": now + int(interval_h * 3600),
            "max_px_rise": 1.0, "trailing": [r["funding_ann"]]}


def settle_and_mark(pos: dict, fut_t: dict | None, spot_t: dict | None,
                    now: int) -> dict:
    """结算（按经过的整周期逐次记账）+ 两条腿盯市 + 保证金压力。"""
    interval_s = pos["interval_h"] * 3600
    rate_now = float(fut_t.get("funding_rate") or 0) if fut_t else 0.0
    while now - pos["last_settle"] >= interval_s:
        pos["last_settle"] += interval_s
        pos["funding_received"] += rate_now * pos["notional"]   # 空腿：正费率收钱
        pos["settlements"] += 1
        pos["trailing"].append(ann_funding(rate_now, pos["interval_h"]))
    pos["trailing"] = pos["trailing"][-7:]
    fpx = float(fut_t.get("mark_price") or 0) if fut_t else 0.0
    spx = float(spot_t.get("last") or 0) if spot_t else 0.0
    if fpx > 0 and pos["fut_entry"] > 0:
        pos["max_px_rise"] = max(pos["max_px_rise"], fpx / pos["fut_entry"])
    if rate_now < 0:
        pos["neg_flips"] += 1
    else:
        pos["neg_flips"] = 0
    pos["mark"] = {"spot": spx, "fut": fpx, "rate_ann": round(
        ann_funding(rate_now, pos["interval_h"]), 4)}
    # 空腿盈亏 = (entry - mark) * size；现货腿 = (mark - entry) * size —— 互相抵消
    size_f = pos["notional"] / pos["fut_entry"]
    size_s = pos["notional"] / pos["spot_entry"]
    pos["pnl_legs"] = {"spot": round((spx - pos["spot_entry"]) * size_s, 4),
                       "fut": round((pos["fut_entry"] - fpx) * size_f, 4)}
    return pos


def should_exit(pos: dict) -> str | None:
    if pos["max_px_rise"] >= MARGIN_WARN and pos["max_px_rise"] > 1.30:
        return "margin stress (>30% adverse)"
    if pos["neg_flips"] >= NEG_FLIPS_EXIT:
        return "funding flipped negative"
    days = (time.time() - pos["opened"]) / 86400
    if days > G_MAX_HOLD_D:
        return "max hold"
    if len(pos["trailing"]) >= 3:
        if sum(pos["trailing"]) / len(pos["trailing"]) < G_EXIT:
            return "trailing funding decayed"
    return None


def close_position(pos: dict, now: int) -> dict:
    """两条腿按真实盘口深度反向平掉（现货卖吃 bids、永续买吃 asks）——
    出场滑点同样是测量对象。深度吃不满则退回标记价并如实记录。"""
    v = VENUES[pos["venue"]]
    size_f = pos["notional"] / pos["fut_entry"]
    size_s = pos["notional"] / pos["spot_entry"]
    spx = fpx = 0.0
    try:
        s_book = http_json(v["spot_book"](pos["spot_sym"]),
                           timeout=15, retries=2)
        f_book = http_json(v["fut_book"](pos["fut_sym"]),
                           timeout=15, retries=2)
        s_mid = mid_of(s_book) or pos["spot_entry"]
        f_mid = mid_of(f_book) or pos["fut_entry"]
        s_bid, _, s_fill, _ = walk_book(s_book, "bid", size_s * s_mid)
        f_ask, _, f_fill, _ = walk_book(f_book, "ask", size_f * f_mid)
        if s_fill >= size_s * s_mid * 0.98 and s_bid > 0:
            spx = s_bid
        if f_fill >= size_f * f_mid * 0.98 and f_ask > 0:
            fpx = f_ask
    except Exception:
        pass
    if spx <= 0:
        spx = (pos.get("mark") or {}).get("spot") or pos["spot_entry"]
    if fpx <= 0:
        fpx = (pos.get("mark") or {}).get("fut") or pos["fut_entry"]
    fees = (pos["notional"] * v["fee_spot"] * 2
            + pos["notional"] * v["fee_fut"] * 2)
    pnl_spot = (spx - pos["spot_entry"]) * size_s
    pnl_fut = (pos["fut_entry"] - fpx) * size_f
    realized = pos["funding_received"] + pnl_spot + pnl_fut - fees \
        - pos["notional"] * pos["entry_cost"]
    days = (now - pos["opened"]) / 86400
    apr_real = realized / pos["notional"] / max(days, 0.04) * 365
    return {"closed": now, "venue": pos["venue"], "base": pos["base"],
            "contract": pos["contract"],
            "reason": pos.get("exit_reason", "?"), "days": round(days, 2),
            "funding_received": round(pos["funding_received"], 4),
            "settlements": pos["settlements"], "neg_flips": pos["neg_flips"],
            "pnl_spot": round(pnl_spot, 4), "pnl_fut": round(pnl_fut, 4),
            "fees": round(fees, 4), "entry_cost": pos["entry_cost"],
            "realized": round(realized, 4),
            "apr_real": round(apr_real, 4), "apr_entry": pos["apr_entry"],
            "max_px_rise": round(pos["max_px_rise"], 3)}


# ----------------------------------------------------------------- 主循环
def cycle() -> dict:
    now = int(time.time())
    st = read_state("carry_state.json",
                    default={"positions": {}, "closed": []}) or \
        {"positions": {}, "closed": []}
    rows, venue_errors = [], {}
    for name, fn in (("gate", fetch_gate), ("bybit", fetch_bybit),
                     ("okx", fetch_okx), ("binance", fetch_binance)):
        try:
            rows += fn()
        except Exception as e:
            venue_errors[name] = str(e)[:120]
    # 雷达不扫 binance —— 纸面机自己喂持续性档案（大流量标的才入册）
    for r in rows:
        if r["exchange"] == "binance" and r["vol24h_usd"] >= MIN_VOL:
            append_jsonl("funding.jsonl", r)
    pers = {}
    for r in read_jsonl_tail("funding.jsonl", 40000):
        if "contract" not in r:
            continue
        k = f"{r.get('exchange', 'gate')}:{r['contract']}"
        pers.setdefault(k, []).append(r["funding_ann"])
    pers = {k: {"n": len(v), "mean": sum(v) / len(v), "min": min(v)}
            for k, v in pers.items() if len(v) >= 4}

    events = []
    held = st["positions"]
    # ---- 盯市 + 出场检查（按所分组取行情） ----
    by_venue = {}
    for p in held.values():
        by_venue.setdefault(p["venue"], []).append(p)
    marks = {}
    for venue, ps in by_venue.items():
        marks[venue] = venue_marks(venue,
                                   {p["spot_sym"] for p in ps},
                                   {p["fut_sym"] for p in ps})
    for key in list(held):
        pos = held[key]
        ft, sm = marks.get(pos["venue"], ({}, {}))
        pos = settle_and_mark(pos, ft.get(pos["fut_sym"]),
                              sm.get(pos["spot_sym"]), now)
        why = should_exit(pos)
        if why:
            pos["exit_reason"] = why
            closed = close_position(pos, now)
            st["closed"].append(closed)
            st["closed"] = st["closed"][-40:]
            del held[key]
            events.append({"ts": now, "event": "close", **closed})
            append_jsonl("carry.jsonl", {"ts": now, "event": "close", **closed})
    # ---- 开仓（有空位才开；同所同币不重复） ----
    if len(held) < MAX_POSITIONS:
        spot_universes = {}
        for venue in VENUES:
            if any(r["exchange"] == venue for r in rows):
                spot_universes[venue] = spot_universe(venue)
        for venue, r, p in candidates(rows, pers, spot_universes):
            key = f"{venue}:{r['contract']}"
            if key in held or len(held) >= MAX_POSITIONS:
                continue
            pos = open_position(venue, r, p, now)
            if pos:
                held[key] = pos
                events.append({"ts": now, "event": "open", "venue": venue,
                               "base": r["base"], "contract": pos["contract"],
                               "apr_entry": pos["apr_entry"],
                               "entry_cost": pos["entry_cost"]})
                append_jsonl("carry.jsonl", {"ts": now, "event": "open",
                                             "venue": venue, "base": r["base"],
                                             "contract": pos["contract"],
                                             "apr_entry": pos["apr_entry"],
                                             "entry_cost": pos["entry_cost"]})
    st["positions"] = held
    write_state("carry_state.json", st)
    return {"ts": now, "state": st, "events": events, "n_rows": len(rows),
            "venue_errors": venue_errors}


def md_report(res: dict) -> str:
    st = res["state"]
    lines = [f"# 资金费率纸面执行测试机 — {fmt_ts(time.time())}", "",
             f"- 纸面腿：每腿 {LEG_USD:.0f}U，{LEVERAGE:.0f}x 空腿保证金模拟，"
             f"最多 {MAX_POSITIONS} 个组合（同所：买现货+空永续）",
             f"- 执行所：{'/'.join(VENUES)}；本轮行情 {res['n_rows']} 行；"
             f"事件 {len(res['events'])} 个", ""]
    if res.get("venue_errors"):
        lines.append(f"- ⚠️ 行情缺席：{', '.join(f'{k}({v[:40]})' for k, v in res['venue_errors'].items())}")
        lines.append("")
    lines += ["## 在场组合", ""]
    if not st["positions"]:
        lines.append("（无）")
    lines.append("| 所 | 合约 | 进场APR | 实测APR | 已收资金 | 结算 | 翻负 | 最大涨幅 | 腿盈亏 | 过桥费 |")
    lines.append("|---|---|---|---|---|---|---|---|---|---|")
    for key, p in st["positions"].items():
        legs = p.get("pnl_legs", {})
        lines.append(f"| {p['venue']} | {p['contract']} "
                     f"| {fmt_pct(p['apr_entry'])} "
                     f"| {fmt_pct(p.get('mark', {}).get('rate_ann'))} "
                     f"| {p['funding_received']:.4f} | {p['settlements']} "
                     f"| {p['neg_flips']} | {p['max_px_rise']:.2f}x "
                     f"| {legs.get('spot', 0):+.2f}/{legs.get('fut', 0):+.2f} "
                     f"| {fmt_pct(p['entry_cost'])} |")
    lines.append("")
    lines += ["## 已平仓（毕业证据机）", ""]
    if not st["closed"]:
        lines.append("（还没有平仓记录 —— 证据在攒）")
    else:
        lines.append("| 所 | 合约 | 天数 | 原因 | 已收资金 | 实现盈亏 | 实现APR | 进场APR | 最大涨幅 |")
        lines.append("|---|---|---|---|---|---|---|---|---|")
        for c in st["closed"]:
            lines.append(f"| {c['venue']} | {c['contract']} | {c['days']} "
                         f"| {c['reason']} | {c['funding_received']:.4f} "
                         f"| {c['realized']:+.4f} | {fmt_pct(c['apr_real'])} "
                         f"| {fmt_pct(c['apr_entry'])} | {c['max_px_rise']}x |")
        tot = sum(c["realized"] for c in st["closed"])
        lines.append("", f"**累计实现：{tot:+.4f} U**", "")
    lines += ["> 毕业门径：≥3 个已平仓组合的实现APR ≥ 进场APR的一半且无保证金告警，",
              "> 才允许 50U 真金白银的最小实腿。"]
    return "\n".join(lines) + "\n"


def main():
    once = "--once" in sys.argv
    while True:
        try:
            res = cycle()
            write_report("carry.md", md_report(res))
            held = len(res["state"]["positions"])
            print(f"[carry] {fmt_ts(time.time())} held={held} "
                  f"events={len(res['events'])} "
                  f"errors={list(res.get('venue_errors') or {}) or 'none'}",
                  flush=True)
        except Exception as e:
            append_jsonl("carry.jsonl", {"ts": int(time.time()),
                                         "error": str(e)[:160]})
            print(f"[carry] cycle_error: {str(e)[:160]}", flush=True)
        if once:
            break
        time.sleep(SCAN_S)


if __name__ == "__main__":
    main()
