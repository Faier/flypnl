"""W1 离线判决 (docs/UNIFIED.md §5): Jev 方向读出有没有预测力——上实盘/纸面前的第一道统计关。

做法：回放缓存的 K 线，在果蝇 DN 发出 BUY/SELL 的时刻（= 生产环境里 Jev 会被
问到的唯一时刻）构建去标识 state，问一次 Jev 三类问题，然后对照未来 horizon 根
K 线的真实涨跌。产出 UNIFIED.md 要求的两个数：
  1) 与 tilt 决策的一致率
  2) Jev 方向概率 与 后续涨跌的相关性
外加 0.55 阈值否决的模拟（被砍掉的入场事后是赚是亏）。

用法：
  python3 scripts/jev_w1_verdict.py                       # ZEC 合约 5m, ~200 样本
  python3 scripts/jev_w1_verdict.py --limit 1             # 冒烟：1 次真实调用
  python3 scripts/jev_w1_verdict.py --mock                # 全链路空跑（无网络）
  python3 scripts/jev_w1_verdict.py --pairs BTC_USDT:1m,fut_ZEC_USDT:5m

答案落 runs/jev_cache.jsonl（key=sha256(model+state+questions)），重跑零成本。
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
                                  default_questions,
                                  validate_no_identifiers)

FLY2TXT = {BUY: "long", SELL: "short"}
SIDESIGN = {BUY: 1.0, SELL: -1.0}


def _mock_transport(payload):
    """确定性假答案：概率无信息（均匀），用于无 key 的管线验证。"""
    return {"answers": {
        "direction": {"choice": "flat",
                      "probabilities": {"long": 0.34, "flat": 0.33, "short": 0.33},
                      "confidence": 0.5},
        "strength": {"score": 5},
        "valid_setup": {"noul": 0.5},
    }}


def collect_samples(cfg, pair, interval, horizon: int, min_gap: int, limit: int):
    """回放一只蝇，采集 DN 触发时刻的 (state, 未来收益)。frozen=True：确定性重放。"""
    rows = load_cache(pair, interval)
    if len(rows) < cfg.brain.window + horizon + 10:
        print(f"[skip ] {pair}:{interval} 只有 {len(rows)} 根")
        return []
    brain = FlyBrain(cfg.brain, tag=pair)
    fee_rt = 2.0 * cfg.risk.taker_fee * cfg.risk.order_notional
    out, last_ts_i = [], -(10 ** 9)
    span_days = (rows[-1][0] - rows[0][0]) / 86400
    for i in range(cfg.brain.window, len(rows) - horizon):
        hist = rows[:i + 1]
        tel = brain.observe(hist, 0.0, frozen=True, equity=100.0,
                            fee_roundtrip=fee_rt)
        d = tel["decision"]
        if d not in (BUY, SELL) or i - last_ts_i < min_gap:
            continue
        state = build_state(tel)
        bad = validate_no_identifiers(state)
        if bad:                      # 防前视最后闸门：带标识的样本必须拒发
            print(f"[WARN ] {pair} 泄漏样本 {bad}，已跳过")
            continue
        close_now, close_fwd = rows[i][4], rows[i + horizon][4]
        out.append({"pair": pair, "interval": interval, "i": i, "ts": rows[i][0],
                    "side": FLY2TXT[d], "close": close_now,
                    "fwd_pct": (close_fwd / close_now - 1) * 100.0,
                    "state": state})
        last_ts_i = i
        if len(out) >= limit:
            break
    print(f"[collect] {pair}:{interval} {len(rows)}根/{span_days:.1f}天 -> "
          f"{len(out)} 个 DN 触发样本")
    return out


def ask_all(client, samples, sleep_s: float):
    """逐个问 Jev（缓存命中不花钱不耗时），答案直接挂到样本上。

    返回 (n_net_calls, n_degraded)。"""
    calls, degrade = 0, 0
    q = default_questions()
    t0 = time.time()
    for n, s in enumerate(samples, 1):
        ans = client.ask_or_none(s["state"], q)
        if ans is None:
            degrade += 1
            continue
        calls += 1
        s["ans"] = ans
        if n % 20 == 0:
            rate = calls / max(time.time() - t0, 1e-9)
            print(f"  .. {n}/{len(samples)} 样本, {calls} 次调用, "
                  f"{rate:.1f} 次/s, 已耗 {time.time()-t0:.0f}s")
        if sleep_s:
            time.sleep(sleep_s)
    return calls, degrade


def verdict(samples) -> dict:
    """UNIFIED.md W1 的两个数 + 0.55 否决模拟。signed_fwd = 按方向取符号的后续收益。"""
    rows = []
    for s in samples:
        ans = s.get("ans")
        if ans is None:
            continue
        direction = ans.get("direction", {})
        probs = direction.get("probabilities", {})
        p = float(probs.get(s["side"], 0.0))
        rows.append({"pair": s["pair"], "side": s["side"],
                     "signed_fwd": s["fwd_pct"] * SIDESIGN[BUY if s["side"] == "long" else SELL],
                     "fwd": s["fwd_pct"], "p_side": p,
                     "choice": direction.get("choice"),
                     "agree": direction.get("choice") == s["side"],
                     "flip": direction.get("choice") in
                             ({"long": "short", "short": "long"}[s["side"]],),
                     "veto": p < 0.55,
                     "noul": float(ans.get("valid_setup", {}).get("noul", 0.0)),
                     "strength": ans.get("strength", {}).get("score")})
    if not rows:
        return {"n": 0}
    n = len(rows)
    agree = sum(r["agree"] for r in rows)
    vetoed = [r for r in rows if r["veto"]]
    kept = [r for r in rows if not r["veto"]]

    def mean(xs):
        return sum(xs) / len(xs) if xs else 0.0

    # Pearson corr(p_side, signed_fwd)
    ps = [r["p_side"] for r in rows]
    fs = [r["signed_fwd"] for r in rows]
    mp, mf = mean(ps), mean(fs)
    cov = sum((a - mp) * (b - mf) for a, b in zip(ps, fs))
    varp = sum((a - mp) ** 2 for a in ps) ** 0.5
    varf = sum((b - mf) ** 2 for b in fs) ** 0.5
    corr = cov / (varp * varf) if varp > 1e-12 and varf > 1e-12 else 0.0

    buckets = {}
    for lo, hi in ((0.0, 0.4), (0.4, 0.55), (0.55, 0.7), (0.7, 1.01)):
        b = [r for r in rows if lo <= r["p_side"] < hi]
        buckets[f"{lo:.2f}-{hi:.2f}"] = {
            "n": len(b), "mean_signed_fwd_pct": round(mean([r["signed_fwd"] for r in b]), 4),
            "win_rate": round(mean([r["signed_fwd"] > 0 for r in b]), 3)} if b else {"n": 0}

    return {
        "n": n,
        "agree_rate": round(agree / n, 3),
        "flip_rate": round(sum(r["flip"] for r in rows) / n, 3),
        "corr_p_vs_signed_fwd": round(corr, 4),
        "mean_signed_fwd_all_pct": round(mean(fs), 4),
        "mean_signed_fwd_when_noul_true_pct": round(
            mean([r["signed_fwd"] for r in rows if r["noul"] >= 0.5]), 4),
        "veto_at_0.55": {
            "n_vetoed": len(vetoed), "n_kept": len(kept),
            "vetoed_mean_signed_fwd_pct": round(mean([r["signed_fwd"] for r in vetoed]), 4),
            "kept_mean_signed_fwd_pct": round(mean([r["signed_fwd"] for r in kept]), 4),
            "vetoed_win_rate": round(mean([r["signed_fwd"] > 0 for r in vetoed]), 3),
            "kept_win_rate": round(mean([r["signed_fwd"] > 0 for r in kept]), 3),
        },
        "buckets_p_side_vs_signed_fwd": buckets,
        "noul_true_rate": round(mean([r["noul"] >= 0.5 for r in rows]), 3),
        "per_pair": _per_pair(rows),
    }


def _per_pair(rows):
    out = {}
    for r in rows:
        out.setdefault(r["pair"], []).append(r)
    return {k: {"n": len(v),
                "agree_rate": round(sum(x["agree"] for x in v) / len(v), 3),
                "mean_signed_fwd_pct": round(
                    sum(x["signed_fwd"] for x in v) / len(v), 4)}
            for k, v in out.items()}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--pairs", default="fut_ZEC_USDT:5m",
                    help="逗号分隔 pair:interval，如 fut_ZEC_USDT:5m,BTC_USDT:1m")
    ap.add_argument("--limit", type=int, default=200, help="最多采集多少 DN 样本")
    ap.add_argument("--horizon", type=int, default=15, help="未来收益看多少根 K 线")
    ap.add_argument("--min-gap", type=int, default=12, help="样本最小间隔（根）")
    ap.add_argument("--sleep", type=float, default=0.3, help="相邻 API 调用间隔秒")
    ap.add_argument("--mock", action="store_true", help="假传输，无网络全链路验证")
    args = ap.parse_args()

    # .env 里的 TYPESAFE_API_KEY（同引擎 load_env 约定）
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
    samples = []
    for spec in args.pairs.split(","):
        pair, _, interval = spec.strip().partition(":")
        samples += collect_samples(cfg, pair, interval or "5m",
                                   args.horizon, args.min_gap, args.limit)
    if not samples:
        sys.exit("没有采集到 DN 触发样本——检查缓存数据新鲜度")

    # mock 答案绝不能写进真实缓存（否则同 state 重跑会命中假答案）
    client = JevClient(cache_path=None if args.mock else
                       os.path.join(ROOT, "runs", "jev_cache.jsonl"),
                       transport=_mock_transport if args.mock else None)
    calls, degrade = ask_all(client, samples, args.sleep)
    if degrade:
        print(f"[warn ] {degrade} 次调用失败降级（不计入判决）")
    v = verdict(samples)

    print("\n===== Jev W1 离线判决 =====")
    print(f"样本 n={v['n']}  (真实调用 {calls} 次, 其余命中缓存)"
          if not args.mock else f"样本 n={v['n']}  (MOCK, 未联网)")
    print(f"① 与 tilt 决策一致率 : {v.get('agree_rate')}")
    print(f"② corr(Jev p, 后续方向收益) : {v.get('corr_p_vs_signed_fwd')}")
    vs = v.get("veto_at_0.55", {})
    print(f"③ 0.55 否决模拟 : 砍 {vs.get('n_vetoed')} 笔 "
          f"(事后均值 {vs.get('vetoed_mean_signed_fwd_pct')}%), "
          f"留 {vs.get('n_kept')} 笔 (事后均值 {vs.get('kept_mean_signed_fwd_pct')}%)")
    print(f"   noul(valid_setup)=true 占比 {v.get('noul_true_rate')}")
    for k, b in v.get("buckets_p_side_vs_signed_fwd", {}).items():
        if b.get("n"):
            print(f"   p∈{k}: n={b['n']} 均值{b['mean_signed_fwd_pct']:+.3f}% "
                  f"胜率{b['win_rate']}")
    out = os.path.join(ROOT, "runs", "reports",
                       f"jev_w1_{time.strftime('%Y%m%d_%H%M')}.json")
    with open(out, "w") as f:
        json.dump({"verdict": v, "mock": args.mock, "args": vars(args)}, f,
                  ensure_ascii=False, indent=1)
    print(f"\n报告 -> {out}")
    print("\n判读：② corr 明显>0 且 p≥0.55 桶均值明显优于低桶 → Jev 有读出价值；"
          "corr≈0 或 veto 桶反而更好 → W1 不通过，别接进引擎。")


if __name__ == "__main__":
    main()
