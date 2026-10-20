"""资金费率/基差套利雷达 v2 —— 多所对比（只读观察，不下单）。

2026-09-28 换所决策的探测结论（本机实测）：
  - Gate      可达 ~1.0s；独有标的（XAU/MSTR 等代币化股票）；夜间 SSL 抖动
  - Bybit     可达 ~0.9s；深度好；代币化股票永续（CRCL/SKHYNIX）+ 主流
  - OKX       可达 ~0.4s；SWAP tickers 一把抓
  - Binance   不可用作引擎：共享出口 IP 被其限流器反复临时封禁（HTTP 418）
  资金费率套利的选所逻辑 = 跟着机会走：哪个所的费率持续 + 现货/永续两腿
  都有深度，就在哪个所做同所 delta 中性。跨所费率差是价差交易不是无风险
  套利（双边保证金+双向基差风险），只记录不当作 #2 的执行形态。

每 15 分钟扫三所，各所 Top40 + 主流币观察名单强制入册。
用法：python3 strategies/funding_radar.py [--once]
产出：runs/radar/funding.jsonl + runs/radar/reports/funding.md
"""
from __future__ import annotations

import sys
import time

from common import append_jsonl, fmt_pct, fmt_ts, http_json, read_jsonl_tail, write_report

SCAN_S = 15 * 60
MIN_VOL = 3_000_000        # 24h 成交额门槛（USDT），低于此的费率不可执行
TOP_N = 40
WATCHLIST = ["BTC", "ETH", "SOL", "ZEC", "DOGE", "XRP", "XAU"]


def ann_funding(rate: float, interval_h: float) -> float:
    if not rate or not interval_h:
        return 0.0
    return rate * (24.0 / interval_h) * 365.0


def _row(exchange, contract, base, rate, interval_h, mark, idx, vol):
    return {"ts": int(time.time()), "exchange": exchange, "contract": contract,
            "base": base, "funding_rate": rate, "interval_h": interval_h,
            "funding_ann": ann_funding(rate, interval_h),
            "basis": (mark / idx - 1) if (mark > 0 and idx > 0) else 0.0,
            "vol24h_usd": vol}


# ----------------------------------------------------------------- per venue
def fetch_gate() -> list:
    """间隔在 contracts 里，成交量在 tickers 里。重试链 5 次：本机到 Gate
    的链路有周期性 SSL 抖动（15min 周期耗得起）。"""
    cons = http_json("https://api.gateio.ws/api/v4/futures/usdt/contracts",
                     timeout=30, retries=5)
    interval = {c.get("name"): float(c.get("funding_interval") or 28800) / 3600.0
                for c in cons}
    ticks = http_json("https://api.gateio.ws/api/v4/futures/usdt/tickers",
                      timeout=30, retries=5)
    rows = []
    for t in ticks:
        try:
            name = t.get("contract") or ""
            if not name.endswith("_USDT"):
                continue
            vol = float(t.get("volume_24h_quote")
                        or t.get("volume_24h_settle")
                        or t.get("volume_24h") or 0)
            rows.append(_row(
                "gate", name, name[:-5], float(t.get("funding_rate") or 0),
                interval.get(name, 8.0), float(t.get("mark_price") or 0),
                float(t.get("index_price") or 0), vol))
        except Exception:
            continue
    return rows


def fetch_bybit() -> list:
    """tickers 一把抓（含 fundingRate）；间隔在 instruments-info 里（分钟）。"""
    info = http_json("https://api.bybit.com/v5/market/instruments-info"
                     "?category=linear&limit=1000", timeout=25, retries=3)
    interval = {}
    for c in ((info.get("result") or {}).get("list") or []):
        mi = c.get("fundingInterval")
        if mi:
            interval[c["symbol"]] = float(mi) / 60.0
    ticks = http_json("https://api.bybit.com/v5/market/tickers?category=linear",
                      timeout=25, retries=3)
    rows = []
    for t in ((ticks.get("result") or {}).get("list") or []):
        sym = t.get("symbol") or ""
        if not sym.endswith("USDT"):
            continue
        try:
            rows.append(_row(
                "bybit", sym, sym[:-4], float(t.get("fundingRate") or 0),
                interval.get(sym, 8.0), float(t.get("markPrice") or 0),
                float(t.get("indexPrice") or 0),
                float(t.get("turnover24h") or 0)))
        except Exception:
            continue
    return rows


def fetch_okx() -> list:
    """OKX tickers 不带 fundingRate（实测空值）→ 先按成交额选 Top60+观察名单，
    再用 /public/funding-rate 单合约端点定点拉（477 全量太重）。注意：部分
    OKX 合约间隔是 4h 而接口不给——按 8h 年化会低估这类，观察期可接受。"""
    d = http_json("https://www.okx.com/api/v5/market/tickers?instType=SWAP",
                  timeout=25, retries=3)
    rows = []
    for t in (d.get("data") or []):
        inst = t.get("instId") or ""
        if not inst.endswith("-USDT-SWAP"):
            continue
        try:
            px = float(t.get("markPx") or 0) or float(t.get("last") or 0)
            vol = float(t.get("volCcy24h") or 0) * px
            base = inst[:-10]
            rows.append(_row("okx", inst, base, 0.0, 8.0, px,
                             float(t.get("idxPx") or 0) if t.get("idxPx") else 0.0,
                             vol))
        except Exception:
            continue
    rows.sort(key=lambda r: -r["vol24h_usd"])
    picked = rows[:60] + [r for r in rows[60:] if r["base"] in WATCHLIST]
    out = []
    for r in picked:
        try:
            fr = http_json("https://www.okx.com/api/v5/public/funding-rate"
                           f"?instId={r['contract']}", timeout=15, retries=2)
            dd = (fr.get("data") or [{}])[0]
            r["funding_rate"] = float(dd.get("fundingRate") or 0)
            r["funding_ann"] = ann_funding(r["funding_rate"], r["interval_h"])
            out.append(r)
            time.sleep(0.1)            # 限速礼貌（20req/2s 上限内）
        except Exception:
            continue                   # 拉不到费率的丢弃，不写 0 假数据
    return out


# ----------------------------------------------------------------- scan
def scan_once() -> dict:
    now = int(time.time())
    all_rows, errors = [], {}
    for name, fn in (("gate", fetch_gate), ("bybit", fetch_bybit),
                     ("okx", fetch_okx)):
        try:
            rs = fn()
            all_rows += rs
            print(f"[funding] {name}: {len(rs)} rows", flush=True)
        except Exception as e:
            errors[name] = str(e)[:120]
            append_jsonl("funding.jsonl", {"ts": now, "exchange": name,
                                           "scan_error": str(e)[:160]})
    for ex in ("gate", "bybit", "okx"):
        tradable = sorted((r for r in all_rows if r["exchange"] == ex
                           and r["vol24h_usd"] >= MIN_VOL),
                          key=lambda r: -r["funding_ann"])
        for r in tradable[:TOP_N]:
            append_jsonl("funding.jsonl", r)
        watch = [r for r in all_rows if r["exchange"] == ex
                 and r["base"] in WATCHLIST]
        for r in watch:
            append_jsonl("funding.jsonl", r)
    return {"ts": now, "rows": all_rows, "errors": errors}


def persistence() -> dict:
    """自建时序 → (exchange:contract) 的费率统计（持续性证据）。"""
    agg = {}
    for r in read_jsonl_tail("funding.jsonl", 40000):
        if "contract" not in r:        # 跳过 scan_error 等非行情记录
            continue
        k = f"{r.get('exchange', 'gate')}:{r['contract']}"
        agg.setdefault(k, []).append(r["funding_ann"])
    return {k: {"n": len(v), "mean": sum(v) / len(v), "min": min(v),
                "max": max(v)}
            for k, v in agg.items() if len(v) >= 4}


# ----------------------------------------------------------------- report
def md_summary(res: dict) -> str:
    rows = res["rows"]
    now = fmt_ts(time.time())
    pers = persistence()
    lines = [f"# 资金费率雷达（三所）— {now}", ""]
    if res["errors"]:
        lines.append(f"- ⚠️ 本轮失败：{', '.join(f'{k}({v[:40]})' for k, v in res['errors'].items())}")
    tradable = {ex: sorted((r for r in rows if r["exchange"] == ex
                            and r["vol24h_usd"] >= MIN_VOL),
                           key=lambda r: -r["funding_ann"])
                for ex in ("gate", "bybit", "okx")}
    n_hot = sum(1 for v in pers.values()
                if v["mean"] >= 0.15 and v["min"] >= 0.075)
    lines += [f"- 可执行标的：gate {len(tradable['gate'])} / bybit "
              f"{len(tradable['bybit'])} / okx {len(tradable['okx'])}"
              f"（≥{MIN_VOL // 1_000_000}M 成交额）",
              f"- 持续性达标（观测均值≥15% 且最小值≥7.5%）：**{n_hot} 个**"
              f" —— 实验#2 门径：14 天内 ≥3 个", ""]

    # 主流币跨所对比
    lines += ["## 主流币跨所对比（年化费率）", "",
              "| 币 | gate | bybit | okx | 最大价差 |", "|---|---|---|---|---|"]
    for base in WATCHLIST:
        vals = {}
        for ex in tradable:
            r = next((r for r in tradable[ex] if r["base"] == base), None)
            if r is None:
                r = next((r for r in rows if r["exchange"] == ex
                          and r["base"] == base), None)
            if r:
                vals[ex] = r["funding_ann"]
        if not vals:
            continue
        spread = (max(vals.values()) - min(vals.values())) if len(vals) > 1 else 0.0
        cells = " | ".join(fmt_pct(vals.get(ex)) for ex in ("gate", "bybit", "okx"))
        lines.append(f"| {base} | {cells} | {fmt_pct(spread)} |")
    lines.append("")
    lines.append("> 跨所价差 ≠ 无风险套利：两边都是永续（双边保证金+各自基差风险），"
                 "只作观察。执行形态是同所 现货+空永续。")
    lines.append("")

    for ex in ("gate", "bybit", "okx"):
        lines += [f"## {ex} Top10（吃费率方向：买现货+空永续）", "",
                  "| 合约 | 年化 | 单周期 | 间隔h | 基差 | 24h额 | 观测均值 | 样本 |",
                  "|---|---|---|---|---|---|---|---|"]
        for r in tradable[ex][:10]:
            p = pers.get(f"{ex}:{r['contract']}", {})
            lines.append(f"| {r['contract']} | {fmt_pct(r['funding_ann'])} "
                         f"| {fmt_pct(r['funding_rate'])} | {r['interval_h']:.0f} "
                         f"| {fmt_pct(r['basis'])} | {r['vol24h_usd']/1e6:.0f}M "
                         f"| {fmt_pct(p.get('mean'))} | {p.get('n', '—')} |")
        lines.append("")
    lines += ["> 观察期先找持续性：1h/4h 间隔的 ±100% 年化大多是行情噪声，",
              "> 观测均值（自建时序）≥ 名义值一半才算真机会。"]
    return "\n".join(lines) + "\n"


def main():
    once = "--once" in sys.argv
    while True:
        try:
            res = scan_once()
            write_report("funding.md", md_summary(res))
            print(f"[funding] {fmt_ts(time.time())} rows={len(res['rows'])} "
                  f"errors={list(res['errors']) or 'none'}", flush=True)
        except Exception as e:
            append_jsonl("funding.jsonl", {"ts": int(time.time()),
                                           "scan_error": str(e)[:160]})
            print(f"[funding] scan_error: {str(e)[:160]}", flush=True)
        if once:
            break
        time.sleep(SCAN_S)


if __name__ == "__main__":
    main()
