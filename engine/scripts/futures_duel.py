"""futures_duel.py — 合约三臂回测决斗（2026-10-24）。

问题：用户感觉「市场空为主，想做空合约」。
项目旧判决（futures_runner.py 注释, 09-16, 12.6 个月 ZEC 5m）：
long_only +185.9% vs 双向 +31.9%——「shorts bled in every window tested」。
本次在近期数据上重审该判决，数字说话。

四臂（同一份 K 线、同一套部署配置 config.json）：
  regime+long_only  当前部署形态（regime 模式 + 只开多,entry_filter 模拟）
  regime 双向        同模式放开做空
  dual 双向          经典双向
  short_only         只开空（纯空头臂,直接回答「能不能做空」）

用法：cd engine && python3 scripts/futures_duel.py [--days 180]
产出：runs/reports/futures_duel.json + 终端对照表
"""
from __future__ import annotations

import json
import os
import sys
import time

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(_HERE))

from flygate.backtest_futures import fetch_futures, run_dual
from flygate.config import Config
from flygate.data.gate_client import GateClient


def main() -> None:
    days = 180.0
    if "--days" in sys.argv:
        days = float(sys.argv[sys.argv.index("--days") + 1])

    cfg = Config.load(os.path.join(os.path.dirname(_HERE), "config.json"))
    contract = cfg.futures.get("contract", "ZEC_USDT")
    interval = str(cfg.futures.get("interval", "5m")).replace("m", "m")
    client = GateClient()          # 公共行情,无 key

    print(f"[duel] 拉取 {contract} {interval} 近 {days:.0f} 天(缓存增量)…")
    t0 = time.time()
    rows = fetch_futures(client, contract, interval, days, f"fut_{contract}")
    if len(rows) < 500:
        print(f"[duel] 数据不足: {len(rows)} 根")
        return
    span_d = (rows[-1][0] - rows[0][0]) / 86400
    print(f"[duel] {len(rows)} 根, {span_d:.0f} 天, "
          f"{time.strftime('%y-%m-%d', time.localtime(rows[0][0]))} → "
          f"{time.strftime('%y-%m-%d', time.localtime(rows[-1][0]))} "
          f"({time.time()-t0:.0f}s)\n")

    arms = [
        ("regime+long_only", "regime", lambda ts, d: d > 0),
        ("regime 双向",      "regime", None),
        ("dual 双向",        "dual",   None),
        ("short_only 纯空",  "short_only", None),
    ]
    print(f"{'臂':<20}{'收益%':>9}{'持有%':>9}{'平仓':>6}{'胜率%':>7}"
          f"{'回撤%':>8}{'峰值/回合%':>11}{'费率':>8}")
    results = {}
    for name, mode, filt in arms:
        r = run_dual(cfg, rows, mode=mode, budget=30.0,
                     entry_filter=filt, verbose=False)
        results[name] = {k: v for k, v in r.items()
                         if k not in ("trades_all", "holding", "curve_vals",
                                      "entry_log", "trade_log")}
        print(f"{name:<20}{r['ret_pct']:>+9.2f}{r['bh_pct']:>+9.2f}"
              f"{r['rounds']:>6}{r['win_pct']:>7.1f}{r['maxdd_pct']:>8.2f}"
              f"{r['avg_trip_peak_pct']:>+11.2f}{r['funding_total']:>8.2f}")

    out = os.path.join(os.path.dirname(_HERE), "runs", "reports",
                       "futures_duel.json")
    os.makedirs(os.path.dirname(out), exist_ok=True)
    meta = {"ts": int(time.time()), "contract": contract,
            "interval": interval, "days": span_d, "candles": len(rows),
            "budget": 30.0, "config": "engine/config.json"}
    with open(out, "w", encoding="utf-8") as f:
        json.dump({"meta": meta, "arms": results}, f,
                  ensure_ascii=False, indent=1)
    print(f"\n[duel] 结果: {out}")


if __name__ == "__main__":
    main()
