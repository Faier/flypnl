"""A股观察层（实验 #4，只读纸面信号，不接券商不下单）。

两个模块：
  1. ETF 双动量轮动纸面组合：10 万虚拟本金，每周一收盘后调仓
     （持有 60 日动量 Top2 且动量>0，否则空仓），日度盯市，
     目标 = 用真实数据积累一条"该信号值不值得实盘"的曲线。
  2. 转债估值水位：全市场转债中位价/中位溢价率每日记录。
     调研结论：中位价回落到 124 以下双低轮动才重新值得做——
     这个模块就是等那个信号亮的。

数据源：东方财富公开接口（无鉴权）。接口属"公共爬取"性质，
字段变了会自动降级为"数据源失效"并继续跑其他模块。

用法：python3 strategies/ashare_watch.py [--once]   （默认每小时检查，17:05 后跑当日）
产出：runs/radar/ashare.jsonl + runs/radar/reports/ashare.md + ashare_state.json
"""
from __future__ import annotations

import statistics
import sys
import time

from common import (append_jsonl, fmt_pct, fmt_ts, http_json, read_jsonl_tail,
                    read_state, write_report, write_state)

# (secid前缀, 代码, 名称, 是否风险资产——黄金/债券类不参与动量排名)
ETFS = [
    ("1", "510300", "沪深300", True), ("1", "510500", "中证500", True),
    ("1", "512100", "中证1000", True), ("1", "588000", "科创50", True),
    ("0", "159915", "创业板", True), ("1", "512480", "半导体", True),
    ("1", "515030", "新能源车", True), ("1", "512690", "酒", True),
    ("1", "512880", "证券", True), ("1", "513100", "纳指", True),
    ("1", "513050", "中概互联", True), ("1", "518880", "黄金", False),
]
TOP_N = 2
START_VALUE = 100_000.0
# 东财会掐非浏览器 UA（connection closed without response）——必须装成浏览器
EM_HEADERS = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126 Safari/537.36",
              "Referer": "https://quote.eastmoney.com/"}


def kline_closes(prefix: str, code: str, n: int = 140) -> list:
    """东财优先；被封/失效时降级腾讯行情（ifzq.gtimg.cn，公开 JSON）。
    10-05 起东财对本机长时间封锁，兜底源保证实验#4 不断流。"""
    try:
        return _kline_eastmoney(prefix, code, n)
    except Exception:
        return _kline_tencent(prefix, code, n)


def _kline_eastmoney(prefix: str, code: str, n: int) -> list:
    url = (f"https://push2his.eastmoney.com/api/qt/stock/kline/get?"
           f"secid={prefix}.{code}&fields1=f1,f2,f3&fields2=f51,f53"
           f"&klt=101&fqt=1&beg=0&end=20500101&lmt={n}")
    d = http_json(url, timeout=20, headers=EM_HEADERS)
    kl = (d.get("data") or {}).get("klines") or []
    out = []
    for row in kl:
        parts = row.split(",")
        out.append((parts[0], float(parts[1])))    # (date, close)
    return out


def _kline_tencent(prefix: str, code: str, n: int) -> list:
    sym = ("sh" if prefix == "1" else "sz") + code
    url = (f"https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?"
           f"param={sym},day,,,{n},qfq")
    d = http_json(url, timeout=20, headers=EM_HEADERS)
    data = d.get("data") or {}
    node = data.get(sym) or {}
    kl = node.get("qfqday") or node.get("day") or []
    return [(row[0], float(row[2])) for row in kl]   # (date, close)


def momentum(closes: list, n: int) -> float | None:
    if len(closes) <= n:
        return None
    return closes[-1][1] / closes[-1 - n][1] - 1


def cb_waterline() -> dict | None:
    """转债全市场实时价（东财行情板块 MK0354，分页）；中位价是轮动实验的启动闸门。
    溢价率字段该接口不带——观察期先盯价格水位，够用。"""
    px, pn = [], 1
    total = None
    while True:
        url = (f"https://push2.eastmoney.com/api/qt/clist/get?pn={pn}&pz=100&po=1"
               "&np=1&fltt=2&invt=2&fid=f2&fs=b:MK0354&fields=f12,f14,f2")
        d = http_json(url, timeout=20, headers=EM_HEADERS)
        data = d.get("data") or {}
        rows = data.get("diff") or []
        total = total or data.get("total") or 0
        if not rows:
            break
        px += [float(r["f2"]) for r in rows
               if isinstance(r.get("f2"), (int, float)) and 50 < r["f2"] < 500]
        if pn * 100 >= total or pn >= 15:
            break
        pn += 1
    if len(px) < 50:
        return None
    return {"n": len(px), "median_price": statistics.median(px),
            "median_premium": None,
            "below_115": sum(1 for p in px if p < 115)}


def step_once() -> dict:
    today = time.strftime("%Y-%m-%d")
    st = read_state("ashare_state.json",
                    {"value": START_VALUE, "week": "", "holdings": {},
                     "last_day": "", "prev_close": {}, "marked_date": ""})
    series = {}
    for prefix, code, name, risk in ETFS:
        try:
            series[code] = kline_closes(prefix, code)
            time.sleep(1.0)            # 东财会掐连续快请求（connection closed）
        except Exception as e:
            append_jsonl("ashare.jsonl", {"ts": int(time.time()),
                                          "fetch_error": f"{code}:{str(e)[:100]}"})
    all_ok = len(series) == len(ETFS)   # 数据不全 → 本轮不盯市不调仓，下小时重试
    mom = {c: (momentum(s, 60), momentum(s, 20)) for c, s in series.items() if s}

    # ---- 盯市（按 K 线日期幂等：同一天多次运行只计一次）----------------
    day_ret = 0.0
    data_date = max((s[-1][0] for s in series.values()), default="")
    held_ok = all(c in series for c in st["holdings"])   # 持仓腿数据必须齐才盯市
    if data_date and held_ok and data_date != st.get("marked_date"):
        for c, w in st["holdings"].items():
            s = series.get(c) or []
            if not s:
                continue
            px_today = s[-1][1]
            px_prev = st["prev_close"].get(c) or px_today
            day_ret += w * (px_today / px_prev - 1)
            st["prev_close"][c] = px_today
        st["value"] = round(st["value"] * (1 + day_ret), 2)
        st["marked_date"] = data_date

    # ---- 调仓：ISO 周变化时（周一收盘后）；数据不全的周不动 -------------
    week = time.strftime("%G-W%V")
    trades = []
    if all_ok and week != st["week"] and today > (st.get("last_day") or ""):
        ranked = sorted(((c, m[0]) for c, m in mom.items()
                         if m[0] is not None and any(risk and c == code
                                                     for _, code, _, risk in ETFS)),
                        key=lambda x: -x[1])
        picks = [c for c, r in ranked if r > 0][:TOP_N]
        new_h = {c: round(1.0 / TOP_N, 4) for c in picks} if picks else {}
        trades = sorted(set(new_h) ^ set(st["holdings"]))
        st["holdings"], st["week"] = new_h, week
    if all_ok:
        st["last_day"] = today          # 干净跑完才盖日戳；脏数据当天重试
    write_state("ashare_state.json", st)

    cb = None
    try:
        cb = cb_waterline()
    except Exception as e:
        append_jsonl("ashare.jsonl", {"ts": int(time.time()),
                                      "cb_error": str(e)[:100]})

    rec = {"ts": int(time.time()), "date": today, "value": st["value"],
           "day_ret": day_ret, "holdings": st["holdings"],
           "mom60": {c: m[0] for c, m in mom.items() if m[0] is not None},
           "mom20": {c: m[1] for c, m in mom.items() if m[1] is not None},
           "cb": cb, "trades": trades}
    append_jsonl("ashare.jsonl", rec)
    return rec


def md_summary(rec: dict) -> str:
    hist = [h for h in read_jsonl_tail("ashare.jsonl", 800) if "value" in h]
    v0 = hist[0]["value"] if hist else START_VALUE
    val, tot = rec["value"], rec["value"] / v0 - 1
    cb = rec.get("cb")
    lines = [f"# A股观察层 — {fmt_ts(time.time())}", "",
             f"## ETF 双动量纸面组合（虚拟 {START_VALUE:,.0f} 起）", "",
             f"- 组合现值 **{val:,.2f}**（观察期累计 {fmt_pct(tot)}，{len(hist)} 个交易日）",
             f"- 当日 {fmt_pct(rec.get('day_ret', 0))}；持仓 "
             f"{rec.get('holdings') or '空仓（动量全负=防守）'}",
             "", "| ETF | 60日动量 | 20日动量 |", "|---|---|---|"]
    for code, name in [(c, n) for _, c, n, _ in ETFS]:
        m60 = (rec.get("mom60") or {}).get(code)
        m20 = (rec.get("mom20") or {})
        lines.append(f"| {name}({code}) | {fmt_pct(m60)} | {fmt_pct(m20.get(code))} |")
    lines += ["", "## 转债估值水位（等 <124 元再启动轮动实验）", ""]
    if cb:
        pm = (f" / 中位溢价率 {cb['median_premium']*100:.1f}%"
              if cb.get("median_premium") is not None else "")
        gate = "🟢 已到启动区" if cb["median_price"] < 124 else "🔴 未到（继续等）"
        lines += [f"- 中位价 **{cb['median_price']:.1f}** 元{pm}"
                  f" / <115 元个数 {cb['below_115']}"
                  f"（样本 {cb['n']}）→ {gate}"]
    else:
        lines.append("- 数据源失效（东财接口字段变更），本轮跳过——不阻塞其他模块")
    lines += ["", "> 打新（零成本正期望）：不需要代码，需要的是券商 App 每日申购动作——",
              "> 市值打新 + 转债打新全部参与，这是当前 A股最实的制度红利。"]
    return "\n".join(lines) + "\n"


def main():
    once = "--once" in sys.argv
    while True:
        try:
            # 每小时醒一次；当日 17:05（A股收盘后）还没跑过就跑
            now = time.localtime()
            done_day = read_state("ashare_state.json", {}).get("last_day", "")
            if once or (now.tm_hour >= 17 and now.tm_min >= 5
                        and time.strftime("%Y-%m-%d") != done_day):
                rec = step_once()
                write_report("ashare.md", md_summary(rec))
                print(f"[ashare] {fmt_ts(time.time())} value={rec['value']} "
                      f"holdings={list(rec['holdings'])}", flush=True)
                if once:
                    break
        except Exception as e:
            append_jsonl("ashare.jsonl", {"ts": int(time.time()),
                                          "scan_error": str(e)[:160]})
            print(f"[ashare] error: {str(e)[:160]}", flush=True)
            if once:
                break
        time.sleep(3600)


if __name__ == "__main__":
    main()
