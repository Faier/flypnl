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
进场三道门（2026-10-21 P0 实盘前优化）：
  a) 净盈利门（Hummingbot profitability-after-fees 思路）：进场 APR 摊平
     开+平全部成本（两腿手续费×2 + 过桥费）的回本期 >7 天 → 不吃。
  b) maker 两段式进场：先挂盘口内侧限价（过桥费≈0），两腿 mid 都穿越
     限价才成交（paper 保守判定，双腿齐备）；30 分钟过期后，仅当 taker
     回本期也过门才降级市价吃单。出场永远 taker——纪律的及时性优先。
  c) 结算网格对齐：结算锚 UTC 整点网格（修复旧 bug：进场后第一次真实
     结算曾被漏记、整本账延后一个周期）。
P1（实盘第一个月加，2026-10-21）：
  4) 跨所比价进场：同币多所过门 → 只留持续性均值最高的执行所。
  5) 基差/保证金监控：基差逐轮记录极端值；空腿浮亏 +25% 报警再平衡
     （补保证金或减半现货腿），+30% 维持撤退线。
  6) 候选分散：最多 2 仓、同币跨所单仓、腿名义 40U 对齐 pilot。
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
LEG_USD = 40.0            # 每条腿的纸面名义（对齐实盘 pilot 的 40U 现货腿）
LEVERAGE = 2.0            # 空腿保证金模拟倍数（2x -> 强平约 +50%）
MAX_POSITIONS = 2         # P1-6 候选分散：同时最多 2 个组合（原 3）
MAX_BASES = 2             # 同币跨所也只能有一仓（防同一风险敞口 ×2）
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
# ---- P0 优化（2026-10-21，实盘前三项）----
# 1) 净盈利门（Hummingbot profitability-after-fees 思路）：进场 APR摊不平
#    开+平全部成本的不吃。回本期 = 成本/日收益，门 = 7 天。
# 2) maker 两段式进场：先挂盘口内侧限价（过桥费≈0），mid 穿越才成交；
#    过期且 taker 回本期过关才降级 taker 立即吃。出场永远 taker（纪律>成本）。
# 3) 结算网格对齐：结算时刻是 UTC 整点网格，记账锚到绝对网格而不是
#    "进场时刻+interval"——旧写法把进场后第一次真实结算漏掉、整体延后
#    一个周期（少记一桶费率）。
BE_MAX_DAYS = 7.0         # 回本期门：摊平全部成本 >7 天 → 不吃
MAKER_TTL_S = 2 * SCAN_S  # 挂单有效期（2 个扫描周期 = 30 分钟）
MAKER_INSIDE = 0.3        # 挂价深入价差的位置（0.3 = 内侧 30% 处）
# ---- P1 优化（2026-10-21，实盘第一个月加）----
# 4) 跨所比价进场（aoki-hjp/funding-rate-arbitrage 思路）：同币在多个执行所
#    都过门时，只留持续性观测均值最高的所——瞬时肥的挤兑所自动被淘汰。
# 5) 基差/保证金监控：delta 中性下基差（fut mark − spot last）是两腿外的
#    第三敞口，逐轮记录极端值；空腿浮亏 +25% 发再平衡报警（补保证金或
#    减半现货腿），+30% 仍是撤退线（should_exit 原样）。
# 6) 候选分散：最多 2 仓、同币任何所只允许一仓、腿名义对齐 40U pilot。

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
        s_limit, f_limit = maker_limits(s_book, f_book)
        return {"spot_px": s_ask, "fut_px": f_bid, "spot_mid": s_mid,
                "fut_mid": f_mid, "entry_cost": cost,
                "spot_limit": s_limit, "fut_limit": f_limit}
    except Exception:
        return None


# ------------------------------------------------------------- P0 纯函数
def round_trip_cost(venue: str, entry_cost: float, maker: bool = False) -> float:
    """开+平全周期成本（占名义比例）：两腿手续费 + 过桥费。

    maker 档保守假设手续费与 taker 相同（Gate 现货标准档 maker=taker），
    maker 的真实优势在过桥费≈0（挂内侧成交，不付冲击成本）；期货 maker
    实际更低，上实盘前按账户实际费率改这里。"""
    v = VENUES[venue]
    fees = v["fee_spot"] + v["fee_fut"]
    return entry_cost + 2.0 * fees


def break_even_days(entry_apr: float, cost_frac: float) -> float:
    """回本期（天）：全部成本 ÷ 进场 APR 的日化。APR≤0 → 永不回本。"""
    daily = entry_apr / 365.0
    return cost_frac / daily if daily > 0 else float("inf")


def next_settle_ts(now: int, interval_s: int) -> int:
    """下一结算时刻。结算锚在 UTC 整点网格（epoch 0 = 00:00 UTC，
    4h/8h 桶都整除），与进场时刻无关。"""
    return ((now // interval_s) + 1) * interval_s


def maker_limits(spot_book: dict, fut_book: dict) -> tuple:
    """两腿 maker 限价：挂在各自盘口价差内侧 30% 处——
    现货买入限价（bid 上方）、永续卖出限价（ask 下方）。"""
    s_a, _ = _top(spot_book, "asks")
    s_b, _ = _top(spot_book, "bids")
    f_a, _ = _top(fut_book, "asks")
    f_b, _ = _top(fut_book, "bids")
    spot_limit = s_b + (s_a - s_b) * MAKER_INSIDE
    fut_limit = f_a - (f_a - f_b) * MAKER_INSIDE
    return spot_limit, fut_limit


def maker_crossed(spot_mid: float, fut_mid: float,
                  spot_limit: float, fut_limit: float) -> bool:
    """两腿 mid 都穿越限价才算 maker 成交（paper 保守判定：两条腿必须
    同轮齐备——真实世界里单腿成交是腿风险，宁可少记）。"""
    return (spot_mid > 0 and fut_mid > 0
            and spot_mid <= spot_limit and fut_mid >= fut_limit)


def pick_best_venue(cands: dict) -> tuple:
    """跨所比价进场：同币在多个执行所过门时只留一个。

    选持续性观测均值（pers.mean，非瞬时值）最高的所；瞬时费率尖峰的
    挤兑所过得了瞬时门但均值低，自动淘汰。返回 (保留的 cands, 淘汰记录)。"""
    by_base = {}
    for key, (venue, r, p) in cands.items():
        by_base.setdefault(r["base"], []).append((key, venue, r, p))
    kept, drops = {}, []
    for base, lst in by_base.items():
        lst.sort(key=lambda t: t[3].get("mean", 0.0), reverse=True)
        k0, v0, r0, p0 = lst[0]
        kept[k0] = (v0, r0, p0)
        for key, venue, r, p in lst[1:]:
            drops.append({"base": base, "dropped": key, "kept": k0,
                          "mean_dropped": round(p.get("mean", 0.0), 4),
                          "mean_kept": round(p0.get("mean", 0.0), 4)})
    return kept, drops


def base_in_use(held: dict, pending: dict, base: str) -> bool:
    """同币单仓：该币已持仓或挂单中（任何所）→ 不再开第二仓。"""
    return (any(p.get("base") == base for p in held.values())
            or any(p.get("base") == base for p in pending.values()))


def basis_pct(spot: float, fut_mark: float) -> float:
    """基差 = (永续标记价 − 现货价) / 现货价。delta 中性下这是两腿之外
    的第三敞口：进场时锁定的基差在平仓前可以漂移。"""
    return (fut_mark - spot) / spot if spot > 0 else 0.0


def fut_short_loss_frac(entry: float, mark: float) -> float:
    """空腿浮亏占名义比例（价格上涨为正；下跌腿浮盈不报警）。"""
    return max(0.0, mark / entry - 1.0) if entry > 0 else 0.0


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


def open_position(venue: str, r: dict, p: dict, now: int,
                  probe: dict, mode: str = "taker") -> dict:
    """开仓登记。probe 由调用方探明后注入（maker 模式成交价=挂价、
    过桥费记 0；taker 模式=吃深度的实价实费）。结算锚对齐 UTC 网格：
    last_settle = 下一结算时刻，进场后第一次真实结算不再漏记。"""
    v = VENUES[venue]
    interval_h = float(r["interval_h"]) or 8.0
    interval_s = int(interval_h * 3600)
    if mode == "maker":
        spot_px, fut_px, cost = probe["spot_limit"], probe["fut_limit"], 0.0
    else:
        spot_px, fut_px, cost = probe["spot_px"], probe["fut_px"], probe["entry_cost"]
    be = break_even_days(r["funding_ann"],
                         round_trip_cost(venue, cost, maker=(mode == "maker")))
    return {"opened": now, "venue": venue, "base": r["base"],
            "contract": v["fut_sym"](r["base"]),
            "spot_sym": v["spot_sym"](r["base"]),
            "fut_sym": v["fut_sym"](r["base"]),
            "notional": LEG_USD, "leverage": LEVERAGE,
            "spot_entry": spot_px, "fut_entry": fut_px,
            "entry_cost": round(cost, 6),
            "entry_mode": mode, "be_days_entry": round(be, 1),
            "interval_h": interval_h,
            "apr_entry": round(r["funding_ann"], 4),
            "apr_obs_mean": round(p.get("mean", 0), 4),
            "funding_received": 0.0, "settlements": 0, "neg_flips": 0,
            # 锚到绝对结算网格（P0-3：旧写法 now+interval 会漏掉进场后
            # 第一次真实结算，整本账延后一个周期）
            "last_settle": next_settle_ts(now, interval_s),
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
    # P1-5: 基差监控（两腿外的第三敞口）——逐轮记录，极端值入档
    if spx > 0 and fpx > 0:
        b = basis_pct(spx, fpx)
        pos["basis_now"] = round(b, 5)
        pos["basis_open"] = pos.get("basis_open",
                                    round(basis_pct(pos["spot_entry"],
                                                    pos["fut_entry"]), 5))
        pos["max_basis_pct"] = round(max(abs(pos.get("max_basis_pct", 0.0)),
                                         abs(b)), 5)
    # P1-5: 空腿浮亏 ≥25% → 再平衡报警（一次即够，cycle 侧发事件）；
    # ≥30% 仍是撤退线（should_exit 原样，纪律优先）
    if fut_short_loss_frac(pos["fut_entry"], fpx) >= MARGIN_WARN - 1.0 \
            and not pos.get("margin_alerted"):
        pos["margin_alerted"] = True
        pos["_new_margin_alert"] = round(fut_short_loss_frac(pos["fut_entry"], fpx), 4)
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
    pending = st.get("pending") or {}
    st["pending"] = pending
    skip_seen = st.get("skip_seen") or {}
    st["skip_seen"] = skip_seen
    # ---- 盯市 + 出场检查（按所分组取行情；挂单中的腿也要行情判穿越） ----
    by_venue = {}
    for p in list(held.values()) + list(pending.values()):
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
        if "_new_margin_alert" in pos:
            adverse = pos.pop("_new_margin_alert")
            ev = {"ts": now, "event": "rebalance_alert", "key": key,
                  "adverse_move": adverse,
                  "basis_now": pos.get("basis_now"),
                  "advice": "补空腿保证金或减半现货腿（30% 仍是撤退线）"}
            events.append(ev)
            append_jsonl("carry.jsonl", ev)
        why = should_exit(pos)
        if why:
            pos["exit_reason"] = why
            closed = close_position(pos, now)
            st["closed"].append(closed)
            st["closed"] = st["closed"][-40:]
            del held[key]
            events.append({"ts": now, "event": "close", **closed})
            append_jsonl("carry.jsonl", {"ts": now, "event": "close", **closed})

    def _open(venue, r, p, probe, mode):
        pos = open_position(venue, r, p, now, probe, mode=mode)
        key = f"{venue}:{r['contract']}"
        held[key] = pos
        ev = {"ts": now, "event": "open", "venue": venue,
              "base": r["base"], "contract": pos["contract"],
              "apr_entry": pos["apr_entry"], "entry_cost": pos["entry_cost"],
              "mode": mode, "be_days": pos["be_days_entry"]}
        events.append(ev)
        append_jsonl("carry.jsonl", ev)

    # ---- 开仓：P0 两段式（maker 优先 → 穿越成交；过期且 taker 过净盈利门
    #      才降级吃单。出场侧永远 taker：纪律的及时性优先于成本） ----
    if len(held) < MAX_POSITIONS:
        spot_universes = {}
        for venue in VENUES:
            if any(r["exchange"] == venue for r in rows):
                spot_universes[venue] = spot_universe(venue)
        cands = {}
        for venue, r, p in candidates(rows, pers, spot_universes):
            cands[f"{venue}:{r['contract']}"] = (venue, r, p)
        # P1-4 跨所比价：同币只留持续性均值最高的执行所
        cands, venue_drops = pick_best_venue(cands)
        for d in venue_drops:
            events.append({"ts": now, "event": "venue_rank", **d})
        # 1) 在挂的 maker 单：费率塌了取消；mid 穿越限价成交；过期降级/放弃
        for key in list(pending):
            if key in held or len(held) >= MAX_POSITIONS:
                pending.pop(key)
                continue
            pend = pending[key]
            row = cands.get(key)
            if row is None:
                pending.pop(key)
                events.append({"ts": now, "event": "maker_cancel", "key": key,
                               "why": "rate decayed below gate"})
                continue
            venue, r, p = row
            if now >= pend["expires"]:
                probe = probe_legs(venue, r["base"])
                if probe is not None:
                    be_t = break_even_days(
                        r["funding_ann"], round_trip_cost(venue, probe["entry_cost"]))
                    if be_t <= BE_MAX_DAYS:
                        pending.pop(key)
                        _open(venue, r, p, probe, "taker")
                        continue
                pending.pop(key)
                events.append({"ts": now, "event": "maker_expired",
                               "key": key})
                continue
            ft, sm = marks.get(venue, ({}, {}))
            spot_mid = float((sm.get(pend["spot_sym"]) or {}).get("last") or 0)
            fut_mid = float((ft.get(pend["fut_sym"]) or {}).get("mark_price") or 0)
            if maker_crossed(spot_mid, fut_mid,
                             pend["spot_limit"], pend["fut_limit"]):
                fill = {"spot_limit": pend["spot_limit"],
                        "fut_limit": pend["fut_limit"]}
                pending.pop(key)
                _open(venue, r, p, fill, "maker")
        # 2) 新候选：净盈利门（maker 和 taker 回本期都 >BE_MAX → 不碰），
        #    过门先挂 maker（省过桥费），jsonl 每 6h 记一次被拒样本防刷屏
        for key, (venue, r, p) in cands.items():
            if key in held or key in pending or len(held) >= MAX_POSITIONS:
                continue
            # P1-6 候选分散：同币任何所已持仓/挂单 → 不开第二仓
            if base_in_use(held, pending, r["base"]):
                continue
            probe = probe_legs(venue, r["base"])
            if probe is None:
                continue
            be_t = break_even_days(
                r["funding_ann"], round_trip_cost(venue, probe["entry_cost"]))
            be_m = break_even_days(
                r["funding_ann"], round_trip_cost(venue, 0.0, maker=True))
            if min(be_t, be_m) > BE_MAX_DAYS:
                if now - skip_seen.get(key, 0) > 6 * 3600:
                    skip_seen[key] = now
                    ev = {"ts": now, "event": "skip_unprofitable", "key": key,
                          "apr": round(r["funding_ann"], 4),
                          "be_days_taker": round(be_t, 1),
                          "be_days_maker": round(be_m, 1)}
                    events.append(ev)
                    append_jsonl("carry.jsonl", ev)
                continue
            v = VENUES[venue]
            pending[key] = {"ts": now, "expires": now + MAKER_TTL_S,
                            "venue": venue, "base": r["base"],
                            "spot_sym": v["spot_sym"](r["base"]),
                            "fut_sym": v["fut_sym"](r["base"]),
                            "spot_limit": probe["spot_limit"],
                            "fut_limit": probe["fut_limit"]}
            ev = {"ts": now, "event": "maker_quote", "key": key,
                  "base": r["base"], "venue": venue,
                  "apr": round(r["funding_ann"], 4),
                  "be_days_maker": round(be_m, 1),
                  "be_days_taker": round(be_t, 1)}
            events.append(ev)
            append_jsonl("carry.jsonl", ev)
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
    lines.append("| 所 | 合约 | 进场APR | 实测APR | 模式 | 回本期 | 已收资金 | 结算 | 翻负 | 基差(开/现/极) | 空腿浮亏 | 最大涨幅 | 腿盈亏 | 过桥费 |")
    lines.append("|---|---|---|---|---|---|---|---|---|---|---|---|---|---|")
    for key, p in st["positions"].items():
        legs = p.get("pnl_legs", {})
        loss = fut_short_loss_frac(p["fut_entry"],
                                   (p.get("mark") or {}).get("fut", 0.0))
        lines.append(f"| {p['venue']} | {p['contract']} "
                     f"| {fmt_pct(p['apr_entry'])} "
                     f"| {fmt_pct(p.get('mark', {}).get('rate_ann'))} "
                     f"| {p.get('entry_mode', 'taker')} "
                     f"| {p.get('be_days_entry', '?')}天 "
                     f"| {p['funding_received']:.4f} | {p['settlements']} "
                     f"| {p['neg_flips']} "
                     f"| {fmt_pct(p.get('basis_open'))}/{fmt_pct(p.get('basis_now'))}/{fmt_pct(p.get('max_basis_pct'))} "
                     f"| {fmt_pct(loss)}{(' ⚠️' if p.get('margin_alerted') else '')} "
                     f"| {p['max_px_rise']:.2f}x "
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
