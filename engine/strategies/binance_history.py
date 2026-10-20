"""Binance 现货 K 线历史拉取（经系统代理）→ flygate 缓存格式。

为什么需要它：Gate 的现货 K 线接口只保留约 1 个月纵深（15m 实测 33 天），
1m 只有 6 天。而"学习有没有用"的长窗判决需要 100+ 天的数据 —— Binance
的 klines 全历史可分页拉取（1000 根/页）。币种市场几乎是同一个市场
（跨所现货价差 <5bp），用 Binance 的 OHLCV 喂果蝇做**测量**是诚实的；
只要在报告里注明数据源即可。执行仍在 Gate（那是引擎的所）。

⚠️ 数据源标记：本脚本写入 data/cache/{PAIR}_{interval}.csv，与 Gate 缓存
同目录同格式。注意不要对同一 (pair, interval) 混用两个数据源。
用法：python3 strategies/binance_history.py BTC ETH SOL [--days 112] [--interval 15m]
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

import urllib.request

CACHE = Path(__file__).with_name("..").resolve() / "data" / "cache"
SPOT = "https://api.binance.com/api/v3/klines"


def fetch_klines(symbol: str, interval: str, days: float) -> list:
    """分页拉取 [openTime, o, h, l, c, v, ...]（ms 时间戳），转 flygate 行。"""
    end = int(time.time() * 1000)
    start = end - int(days * 86400 * 1000)
    out, cursor = [], start
    while cursor < end:
        url = (f"{SPOT}?symbol={symbol}&interval={interval}"
               f"&startTime={cursor}&endTime={end}&limit=1000")
        req = urllib.request.Request(url, headers={"User-Agent": "flygate/0.1"})
        with urllib.request.urlopen(req, timeout=30) as r:
            batch = json_loads(r.read())
        if not batch:
            break
        for k in batch:
            out.append([int(k[0]) // 1000, float(k[1]), float(k[2]),
                        float(k[3]), float(k[4]), float(k[5])])
        cursor = batch[-1][0] + 1
        if len(batch) < 1000:
            break
        time.sleep(0.2)
    return out


def json_loads(b):
    import json
    return json.loads(b)


def save(pair: str, interval: str, rows: list) -> tuple[str, int]:
    """写成 flygate 缓存 CSV（去重、按 ts 排序）。"""
    CACHE.mkdir(parents=True, exist_ok=True)
    p = CACHE / f"{pair}_{interval}.csv"
    old = {}
    if p.exists():
        import csv
        for r in csv.reader(open(p)):
            if r and r[0].isdigit():
                old[int(r[0])] = r
    for r in rows:
        old[r[0]] = [str(x) for x in r]
    merged = [old[k] for k in sorted(old)]
    import csv
    with open(p, "w", newline="") as f:
        w = csv.writer(f)
        w.writerows(merged)
    return str(p), len(merged)


def main():
    args = sys.argv[1:]
    days = float(args[args.index("--days") + 1]) if "--days" in args else 112.0
    interval = args[args.index("--interval") + 1] if "--interval" in args else "15m"
    bases = [a for a in args if not a.startswith("--")
             and a not in (days, interval)
             and not str(a).replace(".", "").isdigit()]
    for base in bases:
        symbol = base + "USDT"
        try:
            rows = fetch_klines(symbol, interval, days)
        except Exception as e:
            print(f"[binance-hist] {symbol}: FAIL {str(e)[:100]}")
            continue
        if not rows:
            print(f"[binance-hist] {symbol}: no data (delisted or wrong symbol)")
            continue
        path, n = save(base + "_USDT", interval, rows)
        span = (rows[-1][0] - rows[0][0]) / 86400
        print(f"[binance-hist] {symbol} {interval}: {len(rows)} new, "
              f"cache {n} rows, {span:.0f} 天 -> {path}")


if __name__ == "__main__":
    main()
