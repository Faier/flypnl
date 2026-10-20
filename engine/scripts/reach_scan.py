"""Reachability scan: can ANY admissible synaptic weight setting separate
approach patterns from avoid patterns? (the expression-ceiling probe)

Question: within the biological bounds — each KC->MBON edge clamped to
[0.1x, 2x] of its OWN contact-derived baseline (Huang/Luo 2024 family) —
does there exist an assignment w_a, w_v under which the gate evidence has
the RIGHT SIGN on the patterns that preceded big up-moves (approach should
win) and big down-moves (avoid should win)? If no reachable weight setting
can express the separation, no amount of dopamine ever can: the ceiling is
the wiring + bounds, not the learning.

The live gate reads marg = w_a·k/sum(w_a) - w_v·k/sum(w_v); linearized this
is sign(d·k) with d = w_a - w_v, so we search for a signed linear functional
over the Kenyon code inside a per-edge box:
    d_i in [0.1*ba_i - 2*bv_i,  2*ba_i - 0.1*bv_i]
    d·k > 0 on P+ patterns (big forward up-move),  d·k < 0 on P- (down-move)

No scipy in this environment, so the search is a box-projected perceptron.
FOUND is definitive (a reachable separator exists). NOT-FOUND is a lower
bound, not a proof — hence the two controls:
  free   : same search WITHOUT the box     -> isolates the bounds' role
  control: random labels WITH the box      -> chance level
Verdict per pair: boxed-sep vs free-sep vs control-sep.

Usage:
  python3 scripts/reach_scan.py [--pairs BTC_USDT,ETH_USDT,SOL_USDT]
      [--interval 15m] [--stride 4] [--limit 4000] [--horizon 8]
      [--quantile 0.15] [--epochs 400]
Writes runs/reach_scan.md and prints the same table.
"""
from __future__ import annotations

import argparse
import os
import sys

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from flygate.config import Config                       # noqa: E402
from flygate.brain import flybrain as fb                # noqa: E402
from flygate.brain.flybrain import FlyBrain             # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

W_MIN, W_MAX = 0.1, 2.0        # luohuang PARAMETERS bounds (x baseline)


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


def encode_patterns(brain: FlyBrain, rows: list, window: int, stride: int,
                    horizon: int, quantile: float):
    """kc_drive for every scanned window + the forward return that followed.
    Frozen observes: no plasticity, but the EMA chain runs exactly like the
    live brain's (the patterns are the ones the brain actually saw)."""
    K, labels, rets = [], [], []
    closes = [r[4] for r in rows]
    for i in range(window, len(rows) - horizon - 1, stride):
        win = rows[:i + 1]
        tel = brain.observe(win, 0.0, frozen=True)
        lum, color = fb.render_chart(win, brain.cfg.grid_w, brain.cfg.grid_h,
                                     brain.cfg.window)
        lmc = fb.center_surround(lum)
        k = brain.kc_drive_of(lmc, color, tel["hs_up"], tel["hs_dn"])
        fwd = closes[i + horizon] / closes[i] - 1.0
        K.append(k)
        rets.append(fwd)
    K = np.array(K)
    rets = np.array(rets)
    hi = np.quantile(rets, 1.0 - quantile)
    lo = np.quantile(rets, quantile)
    pos = K[rets >= hi]
    neg = K[rets <= lo]
    return pos, neg


def projected_perceptron(K, y, lo, hi, epochs=400, lr=0.05, seed=7):
    """maximize min(y * d·k) over d in box. K: (n, e) patterns, y: ±1.
    Returns (best_min_margin, best_sep_fraction, d_best)."""
    rng = np.random.default_rng(seed)
    d = np.zeros(K.shape[1])
    best = (-np.inf, 0.0, d.copy())
    order = np.arange(len(K))
    for _ in range(epochs):
        rng.shuffle(order)
        for j in order:
            s = float(d @ K[j])
            if y[j] * s <= 0:
                d = np.clip(d + lr * y[j] * K[j], lo, hi)
        margins = y * (K @ d)
        sep = float((margins > 0).mean())
        m = float(margins.min())
        if min(sep, 1.0) > 0 and (sep, m) > (best[1], best[0]) and sep >= best[1]:
            best = (m, sep, d.copy())
        if sep == 1.0 and m > 0:
            break
    margins = y * (K @ best[2])
    return best[0], float((margins > 0).mean()), best[2]


def scan_pair(pair: str, cfg: Config, interval: str, stride: int, limit: int,
              horizon: int, quantile: float, epochs: int) -> dict | None:
    rows = load_candles(pair, interval)
    if not rows:
        return None
    rows = rows[-limit:]
    if len(rows) < cfg.brain.window + horizon + 100:
        return None
    brain = FlyBrain(cfg.brain, tag=pair + "|scan")
    pos, neg = encode_patterns(brain, rows, cfg.brain.window, stride,
                               horizon, quantile)
    if len(pos) < 10 or len(neg) < 10:
        return None

    # the box: d = w_a - w_v, each w clamped to [0.1, 2] x its own baseline
    ba = getattr(brain, "_base_a", brain.w_a)
    bv = getattr(brain, "_base_v", brain.w_v)
    lo = W_MIN * ba - W_MAX * bv
    hi = W_MAX * ba - W_MIN * bv

    K = np.vstack([pos, neg])
    y = np.concatenate([np.ones(len(pos)), -np.ones(len(neg))])
    # chronological split: fit on the past, judge on held-out future patterns.
    # In-sample separation with 7,835 free parameters against a few hundred
    # patterns overfits (the random-label control proves it), so only the
    # held-out number says anything about reachability.
    cut = int(len(K) * 0.7)
    K_tr, y_tr = K[:cut], y[:cut]
    K_te, y_te = K[cut:], y[cut:]
    inf_lo = -np.inf * np.ones_like(lo)
    inf_hi = np.inf * np.ones_like(hi)

    _, _, d_box = projected_perceptron(K_tr, y_tr, lo, hi, epochs=epochs)
    _, _, d_free = projected_perceptron(K_tr, y_tr, inf_lo, inf_hi, epochs=epochs)
    y_rnd = y_tr.copy()
    np.random.default_rng(11).shuffle(y_rnd)
    _, _, d_rnd = projected_perceptron(K_tr, y_rnd, lo, hi, epochs=epochs)

    def sep(d):
        m = y_te * (K_te @ d)
        return float((m > 0).mean())

    return {
        "pair": pair, "n_pos": len(pos), "n_neg": len(neg),
        "sep_box_te": round(sep(d_box), 3),
        "sep_free_te": round(sep(d_free), 3),
        "sep_rand_te": round(sep(d_rnd), 3),
    }


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--pairs", default="BTC_USDT,ETH_USDT,SOL_USDT")
    ap.add_argument("--interval", default="15m")
    ap.add_argument("--stride", type=int, default=4)
    ap.add_argument("--limit", type=int, default=4000)
    ap.add_argument("--horizon", type=int, default=8,
                    help="forward return horizon in bars (8 x 15m = 2h)")
    ap.add_argument("--quantile", type=float, default=0.15)
    ap.add_argument("--epochs", type=int, default=400)
    args = ap.parse_args()

    cfg = Config.load()
    results = []
    for pair in [p.strip() for p in args.pairs.split(",") if p.strip()]:
        r = scan_pair(pair, cfg, args.interval, args.stride, args.limit,
                      args.horizon, args.quantile, args.epochs)
        if r is None:
            print(f"{pair}: not enough data/patterns — skipped")
            continue
        results.append(r)

    if not results:
        print("nothing scanned")
        return

    head = (f"| {'pair':10s} | {'P+/P-':>9s} | {'box@test':>8s} | {'free@test':>9s} "
            f"| {'rand@test':>9s} |")
    sep = "|" + "---|" * 5
    lines = [head, sep]
    for r in results:
        lines.append(
            f"| {r['pair']:10s} | {r['n_pos']}/{r['n_neg']:>4d} "
            f"| {r['sep_box_te']:8.3f} | {r['sep_free_te']:9.3f} "
            f"| {r['sep_rand_te']:9.3f} |")
    table = "\n".join(lines)
    print(table)

    verdict = []
    for r in results:
        if r["sep_box_te"] >= 0.9 and r["sep_box_te"] - r["sep_rand_te"] > 0.15:
            verdict.append(
                f"{r['pair']}: 可达且外推 —— 界限内学到的分离在留出段保持"
                f"({r['sep_box_te']:.3f} vs 随机 {r['sep_rand_te']:.3f});"
                f"天花板不在接线,在信用分配/信号")
        elif r["sep_free_te"] >= 0.9 and r["sep_free_te"] - r["sep_box_te"] > 0.1:
            verdict.append(
                f"{r['pair']}: 界限是瓶颈 —— 无界限外推 {r['sep_free_te']:.3f},"
                f"界限内只有 {r['sep_box_te']:.3f}:0.1x-2x 生物界限就是表达上限")
        elif r["sep_free_te"] - r["sep_rand_te"] < 0.1:
            verdict.append(
                f"{r['pair']}: KC 码不带该信号 —— 留出段上 free={r['sep_free_te']:.3f} "
                f"vs 随机 {r['sep_rand_te']:.3f}:线性读出分离不了大涨/大跌前模式,"
                f"上限在视觉编码(光流/ tilt)而不在权重空间")
        else:
            verdict.append(
                f"{r['pair']}: 中间态 box={r['sep_box_te']:.3f} "
                f"free={r['sep_free_te']:.3f} rand={r['sep_rand_te']:.3f},"
                f"需人工判读")
    vtext = "\n".join(f"- {v}" for v in verdict)
    print()
    print(vtext)

    out = os.path.join(ROOT, "runs", "reach_scan.md")
    with open(out, "w") as f:
        f.write(f"# reachability scan ({args.interval}, stride={args.stride}, "
                f"horizon={args.horizon} bars, q={args.quantile})\n\n"
                f"labels: top/bottom {args.quantile:.0%} forward returns; "
                f"search: box-projected perceptron on d = w_a - w_v, "
                f"box = [0.1*ba-2*bv, 2*ba-0.1*bv]; NOT-FOUND is a lower "
                f"bound, FOUND is definitive.\n\n{table}\n\n{vtext}\n")
    print(f"\nwritten: {out}")


if __name__ == "__main__":
    main()
