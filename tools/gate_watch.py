"""gate_watch.py — PONS/Gate 费率套利盯线脚本（只读，不下单）。

三件事，按毕业协议执行：
  1. 攒历史：Gate 无官方结算历史接口（已验证 400），所以自建结算序列——
     每次运行把当前费率记入 4h 桶；首次运行自动用 flygate 雷达的
     funding.jsonl（Gate PONS 已积累 ~22 天）播种。
  2. 入场门：trailing 7 次结算年化 ≥15% → 报「门开」（未持仓时）。
  3. 撤退线（持仓时，任一触发即报）：
     · 连续 2 次结算费率为负
     · trailing 7 次年化 <5%
     · 现价相对入场价移动 ≥2%（delta 中性下=某条腿亏约 2%）

状态文件 state/gate_watch.json（已 gitignore，私有）。仓位靠手工登记：
  --enter --spot 0.27 --mark 0.271 --notional 40   # 开仓登记
  --exit                                            # 平仓登记
定时跑：cron 每 30 分钟一次即可（费率 4h 一结）。

用法：python3 tools/gate_watch.py [--contract PONS_USDT] [--src <flygate runs>]
"""
from __future__ import annotations

import argparse
import json
import pathlib
import ssl
import statistics
import time
import urllib.request

G_ENTER, G_EXIT, TRAIL = 0.15, 0.05, 7
MAX_NEG_STREAK = 2
MOVE_EXIT = 0.02

STATE = pathlib.Path(__file__).resolve().parent.parent / "state" / "gate_watch.json"
GATE_CONTRACTS = "https://api.gateio.ws/api/v4/futures/usdt/contracts"
GATE_SPOT_TICKER = "https://api.gateio.ws/api/v4/spot/tickers?currency_pair={pair}"


def _get(url: str, timeout: float = 15.0):
    req = urllib.request.Request(url, headers={"User-Agent": "flypnl/0.1"})
    with urllib.request.urlopen(req, timeout=timeout,
                                context=ssl.create_default_context()) as r:
        return json.loads(r.read())


def load_state() -> dict:
    if STATE.exists():
        return json.loads(STATE.read_text())
    return {"history": {}, "position": None}


def save_state(st: dict) -> None:
    STATE.parent.mkdir(parents=True, exist_ok=True)
    STATE.write_text(json.dumps(st, ensure_ascii=False, indent=1))


def seed_from_radar(st: dict, src: pathlib.Path, contract: str) -> int:
    """用 flygate 雷达自建时序播种结算桶（每 4h 桶取均值≈该次结算费率）。"""
    f = src / "radar" / "funding.jsonl"
    if not f.exists():
        return 0
    buckets: dict[int, list[float]] = {}
    with f.open() as fh:
        for line in fh:
            try:
                r = json.loads(line)
            except json.JSONDecodeError:
                continue
            if r.get("exchange") != "gate" or r.get("contract") != contract:
                continue
            rate = r.get("funding_rate")
            if rate is None:
                continue
            b = int(r["ts"]) // 14400 * 14400
            buckets.setdefault(b, []).append(float(rate))
    n = 0
    for b, rates in buckets.items():
        key = str(b)
        if key not in st["history"]:
            st["history"][key] = round(statistics.mean(rates), 8)
            n += 1
    return n


def trailing_ann(st: dict, interval_h: float) -> tuple[float, int]:
    """最近 TRAIL 个已完成桶的年化均值（当前进行中的桶不计入）。"""
    cur_bucket = int(time.time()) // (int(interval_h * 3600)) * int(interval_h * 3600)
    done = sorted(int(k) for k in st["history"] if int(k) < cur_bucket)
    last = done[-TRAIL:]
    if len(last) < TRAIL:
        return float("nan"), len(last)
    rates = [st["history"][str(k)] for k in last]
    return statistics.mean(rates) * (24 / interval_h) * 365, TRAIL


def neg_streak(st: dict, interval_h: float) -> int:
    cur_bucket = int(time.time()) // (int(interval_h * 3600)) * int(interval_h * 3600)
    done = sorted(int(k) for k in st["history"] if int(k) < cur_bucket)
    n = 0
    for k in reversed(done):
        if st["history"][str(k)] < 0:
            n += 1
        else:
            break
    return n


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--contract", default="PONS_USDT")
    ap.add_argument("--src", default="/Users/mac/IdeaProjects/flygate/runs")
    ap.add_argument("--enter", action="store_true", help="登记开仓（配合 --spot/--mark/--notional）")
    ap.add_argument("--exit", dest="do_exit", action="store_true", help="登记平仓")
    ap.add_argument("--spot", type=float, help="开仓时的现货成交价")
    ap.add_argument("--mark", type=float, help="开仓时的永续标记价")
    ap.add_argument("--notional", type=float, help="名义仓位（USDT）")
    a = ap.parse_args()

    pair_spot = a.contract.replace("_USDT", "_USDT")
    st = load_state()

    if a.do_exit:
        st["position"] = None
        save_state(st)
        print("[gate_watch] 仓位已登记平仓。")
        return
    if a.enter:
        if a.spot is None or a.mark is None or a.notional is None:
            raise SystemExit("--enter 需要 --spot --mark --notional")
        st["position"] = {"ts": int(time.time()), "spot": a.spot,
                          "mark": a.mark, "notional": a.notional}
        save_state(st)
        print(f"[gate_watch] 已登记开仓: spot={a.spot} mark={a.mark} notional={a.notional}U")
        return

    # 1) 拉当前费率 + 现货价
    contracts = {c["name"]: c for c in _get(GATE_CONTRACTS)}
    c = contracts.get(a.contract)
    if not c:
        raise SystemExit(f"{a.contract} 不在 Gate 永续表")
    rate = float(c.get("funding_rate") or 0)
    interval_h = (c.get("funding_interval") or 28800) / 3600
    tick = _get(GATE_SPOT_TICKER.format(pair=pair_spot))
    spot_px = float(tick[0]["last"])
    cur_bucket = int(time.time()) // int(interval_h * 3600) * int(interval_h * 3600)
    st["history"][str(cur_bucket)] = round(rate, 8)   # 当前桶快照（不进 trailing）

    # 2) 播种（只在历史薄的时候做，成本可忽略）
    if len(st["history"]) < TRAIL + 2:
        n = seed_from_radar(st, pathlib.Path(a.src), a.contract)
        if n:
            print(f"[gate_watch] 从雷达时序播种 {n} 个结算桶")

    save_state(st)

    # 3) 门与撤退线
    trail, have = trailing_ann(st, interval_h)
    trail_s = f"{trail*100:+.1f}%" if trail == trail else "样本不足"
    cur_ann = rate * (24 / interval_h) * 365
    pos = st["position"]
    lines = [f"[gate_watch] {a.contract} @ {time.strftime('%m-%d %H:%M')}",
             f"  现费率: {rate*100:+.4f}/桶 → 年化 {cur_ann*100:+.1f}%",
             f"  trailing{TRAIL}: {trail_s}（历史 {len(st['history'])} 桶，可用 {have}）"]

    verdict = "观望：门未开"
    if pos is None:
        if trail == trail and have >= TRAIL and trail >= G_ENTER and cur_ann >= G_ENTER:
            verdict = "🟢 门开：可按计划试仓（40U 现货腿 + 60U 保证金 + 50U 储备）"
        elif cur_ann >= G_ENTER > (trail if trail == trail else 1):
            verdict = "🟡 瞬时肥但 trailing 未达标——纪律：不进"
    else:
        move = abs(spot_px / pos["spot"] - 1)
        streak = neg_streak(st, interval_h)
        if streak >= MAX_NEG_STREAK:
            verdict = f"🔴 撤退：费率连续 {streak} 次为负"
        elif trail == trail and have >= TRAIL and trail < G_EXIT:
            verdict = f"🔴 撤退：trailing {trail_s} < 5%"
        elif move >= MOVE_EXIT:
            verdict = f"🔴 撤退：价格移动 {move*100:.2f}% ≥2%（某条腿亏约2%）"
        else:
            verdict = f"✅ 持仓正常（价格偏移 {move*100:.2f}%，负费率连击 {streak}）"
    lines.append(f"  判定: {verdict}")
    print("\n".join(lines))


if __name__ == "__main__":
    main()
