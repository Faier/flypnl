"""Re-measure the direction monopoly under the LIVE config.

flybrain.py's structural note (2026-09-28: diff is an affine function of the
tilt scalars, R2 = 0.994, decisions reproduce a 15-line tilt threshold) was
measured BEFORE the MBON->DN graded drive landed (mbon_to_dn=True with
mbon_dn_gain=100 in config.json). This script answers the question that note
can no longer answer: does the mushroom-body path now move the DIRECTION
readout, or does it still only modulate whether a decision is taken?

Method, per pair with a cached kline file:
  run the same brain twice over the same candles, frozen (no plasticity):
    live    : brain config exactly as loaded from config.json
    control : mbon_dn_gain = 0  (MBON drive contributes nothing to DN rates)
  fit  diff ~ a*(hs_up - hs_dn) + b            -> R2        (tilt only)
  fit  diff ~ a*(hs_up - hs_dn) + b + c*marg   -> R2_plus   (tilt + MBON evidence)
  and compare the actual decisions between the two runs.

Reading:
  R2_control ~ 1 and c ~ 0  -> the monopoly stands: learning cannot learn a
                               direction, only a timing (entry gating).
  R2_plus >> R2_live        -> the MBON path now carries direction information;
                               the note is stale and the ceiling moved.

Usage:
  python3 scripts/tilt_monopoly.py [--pairs BTC_USDT,...] [--interval 15m]
                                   [--stride 2] [--limit 6000]
Writes runs/tilt_monopoly.md and prints the same table.
"""
from __future__ import annotations

import argparse
import dataclasses
import json
import os
import sys

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from flygate.config import Config                      # noqa: E402
from flygate.brain.flybrain import FlyBrain            # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def load_candles(pair: str, interval: str) -> list:
    path = os.path.join(ROOT, "data", "cache", f"{pair}_{interval}.csv")
    if not os.path.exists(path):
        return []
    rows = []
    with open(path) as f:
        for line in f:
            p = line.strip().split(",")
            if len(p) >= 6:
                try:
                    rows.append([int(float(p[0]))] + [float(x) for x in p[1:6]])
                except ValueError:
                    continue
    return rows


def ols_r2(y: np.ndarray, X: np.ndarray) -> float:
    """R2 of the least-squares fit y ~ X (X already includes the 1s column)."""
    if len(y) < X.shape[1] + 2:
        return float("nan")
    beta, *_ = np.linalg.lstsq(X, y, rcond=None)
    resid = y - X @ beta
    sst = float(((y - y.mean()) ** 2).sum())
    ssr = float((resid ** 2).sum())
    return 1.0 - ssr / sst if sst > 1e-12 else float("nan")


def measure(pair: str, cfg: Config, interval: str, stride: int,
            limit: int) -> dict | None:
    rows = load_candles(pair, interval)
    if not rows:
        return None
    rows = rows[-limit:]
    w = cfg.brain.window
    if len(rows) < w + 50:
        return None

    live = FlyBrain(cfg.brain, tag=pair + "|live")
    ctrl_cfg = dataclasses.replace(cfg.brain, mbon_dn_gain=0.0)
    ctrl = FlyBrain(ctrl_cfg, tag=pair + "|ctrl")

    hs_diff, diff_live, diff_ctrl, marg, dec_live, dec_ctrl, raw = \
        [], [], [], [], [], [], []
    for i in range(w, len(rows) - 1, stride):
        window = rows[:i + 1]
        tlv = live.observe(window, 0.0, frozen=True)
        tlc = ctrl.observe(window, 0.0, frozen=True)
        hs_diff.append(tlv["hs_up"] - tlv["hs_dn"])
        diff_live.append(tlv["diff_hz"])
        diff_ctrl.append(tlc["diff_hz"])
        marg.append(tlv["marg"])
        dec_live.append(tlv["decision"])
        dec_ctrl.append(tlc["decision"])
        raw.append(abs(tlv["diff_hz"]) >= cfg.brain.dn_diff_hz
                   and tlv["dn_c_hz"] >= 1.0)

    hs_diff = np.array(hs_diff)
    X1 = np.column_stack([np.ones_like(hs_diff), hs_diff])
    X2 = np.column_stack([X1, np.array(marg)])
    r2_ctrl = ols_r2(np.array(diff_ctrl), X1)
    r2_live = ols_r2(np.array(diff_live), X1)
    r2_plus = ols_r2(np.array(diff_live), X2)
    raw = np.array(raw)
    if raw.any():
        agree = float(np.mean(np.array(dec_live)[raw] == np.array(dec_ctrl)[raw]))
    else:
        agree = float("nan")
    return {
        "pair": pair, "n_obs": len(hs_diff),
        "r2_ctrl": round(r2_ctrl, 4), "r2_live": round(r2_live, 4),
        "r2_plus_marg": round(r2_plus, 4),
        "marg_gain": round(r2_plus - r2_live, 4),
        "decision_agreement_raw_fire": None if raw.sum() == 0 else round(agree, 4),
        "n_raw_fire": int(raw.sum()),
    }


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--pairs", default="BTC_USDT,ETH_USDT,SOL_USDT")
    ap.add_argument("--interval", default="15m")
    ap.add_argument("--stride", type=int, default=2)
    ap.add_argument("--limit", type=int, default=6000)
    args = ap.parse_args()

    cfg = Config.load()
    results = []
    for pair in [p.strip() for p in args.pairs.split(",") if p.strip()]:
        r = measure(pair, cfg, args.interval, args.stride, args.limit)
        if r is None:
            print(f"{pair}: no usable cache ({args.interval}) — skipped")
            continue
        results.append(r)

    if not results:
        print("nothing measured")
        return

    head = (f"| {'pair':10s} | {'n_obs':>6s} | {'R2_ctrl':>8s} | {'R2_live':>8s} "
            f"| {'R2_+marg':>9s} | {'marg gain':>9s} | {'raw fire':>8s} | {'agree':>6s} |")
    sep = "|" + "---|" * 8
    lines = [head, sep]
    for r in results:
        agree = r["decision_agreement_raw_fire"]
        lines.append(
            f"| {r['pair']:10s} | {r['n_obs']:6d} | {r['r2_ctrl']:8.4f} "
            f"| {r['r2_live']:8.4f} | {r['r2_plus_marg']:9.4f} "
            f"| {r['marg_gain']:+9.4f} | {r['n_raw_fire']:8d} | "
            f"{'--' if agree is None else f'{agree:6.3f}'} |")
    table = "\n".join(lines)
    print(table)

    verdict = []
    for r in results:
        agree = r["decision_agreement_raw_fire"]
        if agree is not None and agree >= 0.99 and abs(r["marg_gain"]) < 0.01:
            verdict.append(f"{r['pair']}: 垄断仍在 —— diff 仍是 tilt 的仿射函数"
                           f"(R²={r['r2_live']:.3f}),MBON 证据项增量 "
                           f"{r['marg_gain']:+.4f},原始触发点决策一致率 "
                           f"{agree:.3f}:学习只能调制要不要动,调不了往哪边")
        elif r["marg_gain"] >= 0.02:
            verdict.append(f"{r['pair']}: 垄断已破裂 —— MBON 证据带来 "
                           f"+{r['marg_gain']:.3f} 的 R² 增量,方向通路接上了")
        else:
            verdict.append(f"{r['pair']}: 中间态 —— R²_ctrl={r['r2_ctrl']}, "
                           f"marg 增量 {r['marg_gain']:+.4f},需人工判读")
    vtext = "\n".join(f"- {v}" for v in verdict)
    print()
    print(vtext)

    out = os.path.join(ROOT, "runs", "tilt_monopoly.md")
    with open(out, "w") as f:
        f.write(f"# tilt monopoly re-measurement ({args.interval}, "
                f"stride={args.stride}, frozen)\n\n"
                f"live cfg: mbon_to_dn={cfg.brain.mbon_to_dn}, "
                f"mbon_dn_gain={cfg.brain.mbon_dn_gain}, "
                f"rule={cfg.brain.rule}\n\n{table}\n\n{vtext}\n")
    print(f"\nwritten: {out}")


if __name__ == "__main__":
    main()
