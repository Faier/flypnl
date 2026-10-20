"""实验 A 离线判决：Jev 当教学归因判官（P3, docs/UNIFIED.md）有没有判别力。

背景（已实测的问题）：punish 主导的教学曾把果蝇教到拒绝开仓
（punish_deadband_mult 注释, 09-29 实测 14 punish vs 1 reward）。
假设：一笔亏损可能是"入场形态本来就错"（该教回避），也可能是
"形态对但路径噪声"（不该教）。Jev 的 noul（不饱和通道, W1 实测）
若能把前者从后者里分出来，就值得接进 engine._close_trip 教学闸。

预注册判据（跑之前写下，跑完照判）：
  rL = 亏损 trip 被判 attributable 的比例; rW = 盈利 trip 的同比例。
  有用: rL - rW >= 0.20 且 n_loss >= 25 且 n_win >= 25;
  否则: 不接引擎（Jev 分不清"该罚的"和"运气差的"，装上也只是噪声教学）。

用法:
  python3 scripts/jev_attribution_offline.py --mock      # 空跑验证
  python3 scripts/jev_attribution_offline.py             # ZEC 5m 真实判决
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from flygate.brain.flybrain import FlyBrain, BUY, SELL          # noqa: E402
from flygate.config import Config                               # noqa: E402
from flygate.data.history import load_cache                     # noqa: E402
from flygate.decide.jev import JevClient                        # noqa: E402
from flygate.decide.state import (build_state,                  # noqa: E402
                                  validate_no_identifiers)

HARD_STOP = 0.035      # 同实盘 hard_stop_pct
TIMEOUT_BARS = 96      # 8h @5m：超时按市价离场
FEE_RT = 0.0016        # 合约往返成本估 2x(taker0.05%+slip0.03%)
MIN_GAP = 6            # 相邻入场的最小间隔（根）


def _mock_transport(payload):
    return {"answers": {"attributable": {"noul": 0.5}}}


def collect_trips(cfg, pair, interval):
    """简化 trip 回放（只多，硬止损/反信号/超时三出口）。

    归因判决只关心"亏的 trip vs 赚的 trip 各被怎么判"，不追求复刻完整离场梯。"""
    rows = load_cache(pair, interval)
    if len(rows) < cfg.brain.window + TIMEOUT_BARS + 10:
        print(f"[skip ] {pair}:{interval} 数据不足")
        return []
    brain = FlyBrain(cfg.brain, tag=pair)
    fee_rt = 2.0 * cfg.risk.taker_fee * cfg.risk.order_notional
    trips, last_entry_i = [], -(10 ** 9)
    span_days = (rows[-1][0] - rows[0][0]) / 86400
    pos = None
    for i in range(cfg.brain.window, len(rows)):
        cur = rows[i]
        if pos is not None:
            px = cur[4]
            mfe = max(pos["mfe"], (px / pos["px"] - 1) * 100)
            mae = min(pos["mae"], (px / pos["px"] - 1) * 100)
            exit_reason = None
            tel_probe = None
            if px <= pos["px"] * (1 - HARD_STOP):
                exit_reason = "hard_stop"
            elif i - pos["i"] >= TIMEOUT_BARS:
                exit_reason = "timeout"
            else:
                # 反信号离场：重放观测一次拿决策（与入场同一只脑）
                tel_probe = brain.observe(rows[:i + 1], 0.0, frozen=True,
                                          equity=100.0, fee_roundtrip=fee_rt)
                if tel_probe["decision"] == SELL:
                    exit_reason = "counter_signal"
            if exit_reason:
                pnl = (px / pos["px"] - 1) * 100 - FEE_RT * 100
                pos.update(pnl=pnl, mfe=mfe, mae=mae, bars=i - pos["i"],
                           exit=exit_reason)
                trips.append(pos)
                pos = None
                last_entry_i = i
            continue
        tel = brain.observe(rows[:i + 1], 0.0, frozen=True, equity=100.0,
                            fee_roundtrip=fee_rt)
        if tel["decision"] == BUY and i - last_entry_i >= MIN_GAP:
            st = build_state(tel)
            if validate_no_identifiers(st):
                continue
            pos = {"pair": pair, "i": i, "px": cur[4], "state": st,
                   "mfe": 0.0, "mae": 0.0}
    print(f"[collect] {pair}:{interval} {len(rows)}根/{span_days:.1f}天 -> "
          f"{len(trips)} 个 trip "
          f"(亏 {sum(1 for t in trips if t['pnl'] < 0)} / "
          f"赚 {sum(1 for t in trips if t['pnl'] >= 0)})")
    return trips


def attrib_question():
    return {"attributable": {
        "type": "noul",
        "instructions": (
            "You are auditing one completed trade of a trend-following system. "
            "Given ONLY the de-identified feature snapshot at entry and the "
            "outcome path, judge whether the OUTCOME was caused by the entry "
            "setup being structurally wrong (entering a late or extended leg, "
            "or a chop regime), rather than routine random path noise after a "
            "valid setup. Judge the setup, not the luck."),
        "criteria": {
            "true": "entry was structurally bad: late/extended leg, chop regime, "
                    "features already showed incoherence",
            "false": "entry setup was valid; outcome within normal random "
                     "variation for this setup class",
        },
    }}


def ask_all(client, trips, sleep_s):
    calls, degrade = 0, 0
    q = attrib_question()
    t0 = time.time()
    for n, t in enumerate(trips, 1):
        state = dict(t["state"])
        state.update(trip_pnl_pct=round(t["pnl"], 3),
                     mfe_pct=round(t["mfe"], 3), mae_pct=round(t["mae"], 3),
                     bars_held=int(t["bars"]), exit_reason=t["exit"])
        bad = validate_no_identifiers(state)
        if bad:
            print(f"[WARN ] 泄漏字段 {bad}，跳过")
            continue
        ans = client.ask_or_none(state, q)
        if ans is None:
            degrade += 1
            continue
        calls += 1
        t["attributable"] = float(ans.get("attributable", {}).get("noul", 0.5)) >= 0.5
        if n % 25 == 0:
            print(f"  .. {n}/{len(trips)} trips, {calls} 次调用, "
                  f"{time.time()-t0:.0f}s")
        if sleep_s:
            time.sleep(sleep_s)
    return calls, degrade


def verdict(trips) -> dict:
    done = [t for t in trips if "attributable" in t]
    loss = [t for t in done if t["pnl"] < 0]
    win = [t for t in done if t["pnl"] >= 0]
    if not loss or not win:
        return {"n": len(done), "n_loss": len(loss), "n_win": len(win)}
    rL = sum(t["attributable"] for t in loss) / len(loss)
    rW = sum(t["attributable"] for t in win) / len(win)
    # 教学反事实：现行规则 = 所有亏损都发惩罚;归因闸 = 只罚 attributable 的亏损
    punished_now = len(loss)
    punished_gate = sum(t["attributable"] for t in loss)
    rewards = len(win)
    return {
        "n": len(done), "n_loss": len(loss), "n_win": len(win),
        "rL_attribution_rate_on_losses": round(rL, 3),
        "rW_false_attribution_rate_on_wins": round(rW, 3),
        "gap_rL_minus_rW": round(rL - rW, 3),
        "punish_reward_ratio_now": round(punished_now / max(rewards, 1), 2),
        "punish_reward_ratio_gated": round(punished_gate / max(rewards, 1), 2),
        "pass": bool((rL - rW) >= 0.20 and len(loss) >= 25 and len(win) >= 25),
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--pairs", default="fut_ZEC_USDT:5m")
    ap.add_argument("--sleep", type=float, default=0.25)
    ap.add_argument("--mock", action="store_true")
    args = ap.parse_args()

    env = os.path.join(ROOT, ".env")
    if os.path.exists(env):
        for line in open(env):
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, _, v = line.partition("=")
                os.environ.setdefault(k.strip(), v.strip())
    if not args.mock and not os.environ.get("TYPESAFE_API_KEY"):
        sys.exit("缺 TYPESAFE_API_KEY：写进 .env 或 export，或用 --mock 空跑")

    cfg = Config.load(None)
    trips = []
    for spec in args.pairs.split(","):
        pair, _, itv = spec.strip().partition(":")
        trips += collect_trips(cfg, pair, itv or "5m")
    if not trips:
        sys.exit("没有采集到 trip")

    client = JevClient(cache_path=None if args.mock else
                       os.path.join(ROOT, "runs", "jev_cache.jsonl"),
                       transport=_mock_transport if args.mock else None)
    calls, degrade = ask_all(client, trips, args.sleep)
    if degrade:
        print(f"[warn ] {degrade} 次调用失败降级")
    v = verdict(trips)

    print("\n===== 实验 A：Jev 教学归因判官 离线判决 =====")
    print(f"trip n={v['n']} (亏 {v['n_loss']} / 赚 {v['n_win']})")
    print(f"rL 亏损被判该罚 : {v.get('rL_attribution_rate_on_losses')}")
    print(f"rW 盈利被误判   : {v.get('rW_false_attribution_rate_on_wins')}")
    print(f"判别差 rL-rW    : {v.get('gap_rL_minus_rW')}  (门槛 >= 0.20)")
    print(f"punish:reward 现行 {v.get('punish_reward_ratio_now')} "
          f"-> 归因闸后 {v.get('punish_reward_ratio_gated')}")
    print(f"判决: {'通过——值得接进 engine._close_trip' if v.get('pass') else '不通过——别接，Jev 分不清该罚的和运气差的'}")

    out = os.path.join(ROOT, "runs", "reports",
                       f"jev_attrib_{time.strftime('%Y%m%d_%H%M')}.json")
    with open(out, "w") as f:
        json.dump({"verdict": v, "mock": args.mock, "args": vars(args)}, f,
                  ensure_ascii=False, indent=1)
    print(f"报告 -> {out}")


if __name__ == "__main__":
    main()
