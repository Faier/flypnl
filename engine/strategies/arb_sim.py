"""费率套利模拟执行账本 —— 实验#2 的毕业证据机（只读历史结算，无任何下单）。

回答一个问题：『如果在这类标的上做 同所现货+空永续，扣完成本到底赚多少？』
用交易所真实结算记录（不是快照均值）回测两条策略：
  P1 无脑持有：全程收费率，进出各付一次成本
  P2 门控进出 ：trailing 7 次结算的年化均值 ≥15% 进场，<5% 或持有 >45 天离场
                   （费率翻负自动撤退=吃费率策略的生死线）

成本模型（往返，占名义本金比例）：
  bybit  现货 0.1% + 永续 0.055% + 滑点 0.05% → 单边 0.205%，往返 0.41%
  okx    同上量级 → 0.41%
对照组：BTC/ETH/SOL/ZEC/DOGE 主流币（无聊但诚实——它们是"费率收益"的标尺）。
gate 无历史结算接口，其标的暂不入模（雷达自建时序负责）。

用法：python3 strategies/arb_sim.py
产出：runs/radar/reports/arb_sim.md（每次运行覆盖）
"""
from __future__ import annotations

import statistics
import sys
import time

from common import fmt_ts, http_json, write_report
from funding_history import hist_bybit, hist_okx   # 复用已验证的历史拉取

FEE_RT = {"bybit": 0.0041, "okx": 0.0041}   # 往返成本（现货+永续+滑点，双边）
G_ENTER = 0.15        # P2：trailing 年化均值 ≥15% 进场
G_EXIT = 0.05         # <5% 离场（费率塌了就走）
G_MAX_HOLD_D = 45     # 最长持有（到期强制出，重新满足可再进）
TRAIL_N = 7           # trailing 窗口（结算次数）
MIN_SETTLE = 30       # 样本太少不入表

SIM = [
    # 候选（历史体检的幸存者）
    ("bybit", "BTWUSDT"), ("bybit", "USUSDT"), ("bybit", "MARSCOINUSDT"),
    ("bybit", "NIULAIUSDT"), ("bybit", "INXUSDT"), ("bybit", "SKHYNIXUSDT"),
    ("bybit", "XAUUSDT"), ("bybit", "ZECUSDT"),
    ("okx", "SOON-USDT-SWAP"), ("okx", "PONS-USDT-SWAP"),
    ("okx", "DGAI-USDT-SWAP"), ("okx", "MUBARAK-USDT-SWAP"),
    ("okx", "BEAT-USDT-SWAP"), ("okx", "XAU-USDT-SWAP"), ("okx", "ZEC-USDT-SWAP"),
    # 对照组（主流币标尺）
    ("bybit", "BTCUSDT"), ("bybit", "ETHUSDT"), ("bybit", "SOLUSDT"),
    ("bybit", "DOGEUSDT"), ("okx", "BTC-USDT-SWAP"), ("okx", "ETH-USDT-SWAP"),
    ("okx", "SOL-USDT-SWAP"), ("okx", "DOGE-USDT-SWAP"),
]
FETCH = {"bybit": hist_bybit, "okx": hist_okx}


def interval_h(hist: list) -> float:
    ts = [t for t, _ in hist]
    return statistics.median(b - a for a, b in zip(ts, ts[1:])) / 3_600_000


def p1_constant(hist: list, fee_rt: float) -> dict:
    ts = [t for t, _ in hist]
    rates = [r for _, r in hist]
    step = interval_h(hist)
    gross = sum(rates)                                   # 每次结算=名义的 rate 倍
    net = gross - fee_rt
    days = (ts[-1] - ts[0]) / 86400_000
    return {"net": net, "apr": net / days * 365 if days > 0 else 0.0,
            "days": days, "worst": min(rates) * (24 / step) * 365,
            "gross": gross}


def p2_gated(hist: list, fee_rt: float) -> dict:
    """状态机：trail(最近 TRAIL_N 次年化) ≥G 进场；<G_EXIT 或超 45 天离场。"""
    ts = [t for t, _ in hist]
    step = interval_h(hist)
    ann = [r * (24 / step) * 365 for r in (x[1] for x in hist)]
    in_pos, entry_i, accrued, trips = False, 0, 0.0, []
    n_settles_in = 0
    for i, a in enumerate(ann):
        if not in_pos:
            if len(ann[:i + 1]) >= TRAIL_N and \
                    statistics.mean(ann[max(0, i - TRAIL_N + 1):i + 1]) >= G_ENTER:
                in_pos, entry_i, accrued, n_settles_in = True, i, 0.0, 0
        else:
            accrued += hist[i][1]                        # 收的是 rate（名义分数）
            n_settles_in += 1
            days_in = (ts[i] - ts[entry_i]) / 86400_000
            trail = statistics.mean(ann[max(0, i - TRAIL_N + 1):i + 1])
            if trail < G_EXIT or days_in > G_MAX_HOLD_D:
                trips.append(accrued - fee_rt)
                in_pos = False
    if in_pos:                                           # 期末仍持仓 → 按浮盈记
        trips.append(accrued - fee_rt)
    days = (ts[-1] - ts[0]) / 86400_000
    net = sum(trips)
    return {"net": net, "apr": net / days * 365 if days > 0 else 0.0,
            "days": days, "trips": trips, "n": len(trips)}


def run():
    rows = []
    for venue, sym in SIM:
        try:
            hist = FETCH[venue](sym)
        except Exception as e:
            print(f"[arbsim] {venue}:{sym} fetch error {str(e)[:80]}", flush=True)
            continue
        if len(hist) < MIN_SETTLE:
            rows.append((venue, sym, None, None, len(hist), None))
            continue
        p1 = p1_constant(hist, FEE_RT[venue])
        p2 = p2_gated(hist, FEE_RT[venue])
        rows.append((venue, sym, p1, p2, len(hist), interval_h(hist)))
        time.sleep(0.15)

    ctl = [r for r in rows if r[0] == "bybit" and r[1] in ("BTCUSDT", "ETHUSDT")]
    ctl_apr = statistics.mean([r[2]["apr"] for r in ctl if r[2]]) if ctl else 0.0

    lines = [f"# 费率套利模拟账本 — {fmt_ts(time.time())}", "",
             f"> 成本：往返 {FEE_RT['bybit']:.2%}（现货+永续+滑点）。对照组(BTC/ETH)均值年化 "
             f"**{ctl_apr*100:+.1f}%** —— 这是'什么都不挑'的标尺。毕业线：P1 与 P2 年化均 >15% "
             f"且 > 对照组 2 倍。gate 标的无历史结算接口，暂不入模。", "",
             "| 所 | 合约 | 样本 | 间隔h | P1毛 | **P1净年化** | P2次数 | **P2净年化** | P2最差战役 | 判定 |",
             "|---|---|---|---|---|---|---|---|---|---|"]
    verdicts = []
    for venue, sym, p1, p2, n, step in rows:
        if p1 is None:
            lines.append(f"| {venue} | {sym} | {n}(<{MIN_SETTLE}) | | | | | | | 样本不足 |")
            continue
        worst = min(p2["trips"]) * 100 if p2["trips"] else 0.0
        v = (p1["apr"] > 0.15 and p2["apr"] > 0.15
             and p1["apr"] > 2 * max(ctl_apr, 0.01))
        verdicts.append(v)
        lines.append(
            f"| {venue} | {sym} | {n} | {step:.0f} | {p1['gross']*100:+.1f}% "
            f"| **{p1['apr']*100:+.1f}%** | {p2['n']} | **{p2['apr']*100:+.1f}%** "
            f"| {worst:+.2f}% | {'✅' if v else '—'} |")
    n_pass = sum(verdicts)
    lines += ["", f"## 判定：{n_pass}/{len(verdicts)} 过线",
              "",
              "- P1（无脑持有）代表「把它当收租底仓」的年化；P2（门控）代表「只在费率肥的时候进出」。",
              "- 主流币对照组就是地板：一个候选连 BTC 的费率都打不过，就没资格占用本金。",
              "- 本账本全部为历史模拟，未含爆仓/插针尾部；上真钱前 P1/P2 还要连续两周为正。"]
    write_report("arb_sim.md", "\n".join(lines) + "\n")
    print(f"[arbsim] done: {len(rows)} targets, {n_pass} PASS", flush=True)


if __name__ == "__main__":
    run()
