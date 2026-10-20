"""Multi-seed, multi-arm measurement harness — the protocol StonkFly's docs and
suifei/flywire-fly-lab both insist on before any claim is made:

  * every arm runs on the SAME candles, several seeds, and reports a mean with
    a bootstrap confidence interval instead of one number;
  * the ablation arms differ in exactly one mechanism (see MODES);
  * every return is compared against a benchmark at the SAME exposure and with
    fees charged, so "we beat buy&hold" cannot come from being 60% invested
    while the benchmark is 100% invested;
  * the wiring control (`rewired`) keeps the pixel statistics and destroys the
    geometry, which is the only way to say whether the visual structure — or
    the connectome built on it — carries the signal.

Seeds vary the brain's random PN->KC wiring, so a seed is a trial over the
model's own randomness, not over the market.

    python -m flygate protocol --pair BTC_USDT --seeds 5
"""
from __future__ import annotations

import json
import os

import numpy as np

from ..backtest import MODES, run_backtest

ARMS = MODES
METRICS = ("return_pct", "bh_matched_return_pct", "buyhold_return_pct",
           "max_drawdown_pct", "fees_paid", "round_trips",
           "time_in_market_pct", "win_rate_pct")


def bootstrap_ci(values, n_boot: int = 2000, alpha: float = 0.05, seed: int = 0):
    """Percentile bootstrap CI of the MEAN over independent trials.

    Returns (mean, lo, hi). One trial returns its own value three times and
    says so by leaving n=1 visible to the caller — a single run is not
    evidence, it is a sample of size one.
    """
    v = np.asarray([x for x in values if x is not None], dtype=float)
    if v.size == 0:
        return float("nan"), float("nan"), float("nan")
    if v.size == 1:
        return float(v[0]), float(v[0]), float(v[0])
    rng = np.random.default_rng(seed)
    idx = rng.integers(0, v.size, size=(n_boot, v.size))
    boots = v[idx].mean(axis=1)
    return float(v.mean()), float(np.quantile(boots, alpha / 2)), \
        float(np.quantile(boots, 1 - alpha / 2))


def summarize(per_seed: list) -> dict:
    out = {}
    for m in METRICS:
        mean, lo, hi = bootstrap_ci([r.get(m) for r in per_seed])
        out[m] = {"mean": round(mean, 4), "lo": round(lo, 4), "hi": round(hi, 4),
                  "n": len(per_seed)}
    out["halted_runs"] = sum(1 for r in per_seed if r.get("halted"))
    out["sign_flips"] = int(sum(1 for r in per_seed if r.get("return_pct", 0) > 0))
    return out


def paired_diff(vals_a, vals_b) -> dict:
    """Mean difference (a - b) of return_pct with a bootstrap CI.

    This is the question the README's table could not answer: not "is the fly
    positive" but "is the fly different from the arm that differs by exactly
    one mechanism", with the interval reported. A CI spanning 0 means the
    mechanism bought nothing measurable."""
    a = np.asarray([v for v in vals_a if v is not None], dtype=float)
    b = np.asarray([v for v in vals_b if v is not None], dtype=float)
    if a.size == 0 or b.size == 0:
        return {"mean": float("nan"), "lo": float("nan"), "hi": float("nan"),
                "crosses_zero": True, "n_a": int(a.size), "n_b": int(b.size)}
    diff = float(a.mean() - b.mean())
    if a.size == 1 and b.size == 1:
        lo = hi = diff
    else:
        rng = np.random.default_rng(1)
        boots = (a[rng.integers(0, a.size, size=(2000, a.size))].mean(axis=1)
                 - b[rng.integers(0, b.size, size=(2000, b.size))].mean(axis=1))
        lo, hi = float(np.quantile(boots, 0.025)), float(np.quantile(boots, 0.975))
    return {"mean": round(diff, 4), "lo": round(lo, 4), "hi": round(hi, 4),
            "crosses_zero": bool(lo <= 0.0 <= hi),
            "n_a": int(a.size), "n_b": int(b.size)}


def _run_one(task):
    """One (pair, arm, seed) backtest. Module-level so it can be pickled by a
    process pool: the harness is one independent run per core."""
    cfg, pair, rows, arm, seed = task
    return run_backtest(cfg, pair, rows, mode=arm, verbose=False, seed=seed)


def _arm_return_series(raw: dict, arm: str, min_len: int) -> np.ndarray | None:
    """Per-candle equity returns of one arm, averaged across seeds (the arm
    grid is identical across seeds; deterministic arms are identical anyway).
    Returns None when the arm has no usable curve or is flat (no trades)."""
    curves = []
    for res in raw.get(arm) or []:
        ec = res.get("equity_curve") or []
        if len(ec) >= min_len:
            curves.append(ec[:min_len])
    if not curves:
        return None
    rets = []
    for ec in curves:
        eq = np.asarray([e[1] for e in ec], dtype=float)
        if eq.size < 3:
            return None
        rets.append(eq[1:] / eq[:-1] - 1.0)
    return np.mean(rets, axis=0)


def _returns_matrix(raw: dict, arms: list) -> tuple[np.ndarray | None, list, list]:
    """T×N aligned per-candle returns (columns = arms) on the common prefix
    every arm shares (a halted arm ends early — the prefix is the intersection),
    plus the lists of used and dropped arms (flat/no-trade arms carry no
    Sharpe information and are excluded from the statistics, recorded)."""
    lens = [len(r.get("equity_curve") or []) for a in arms for r in raw.get(a) or []]
    if not lens:
        return None, [], []
    min_len = min(lens)
    if min_len < 60:
        return None, [], []
    series, used, dropped = {}, [], []
    for arm in arms:
        s = _arm_return_series(raw, arm, min_len)
        if s is None:
            dropped.append(arm)
            continue
        series[arm] = s
        used.append(arm)
    if len(used) < 2:
        return None, used, dropped
    # every arm must be on the same candle grid: check the boundary stamps
    t0s = {(raw[a][0].get("equity_curve") or [(0, None)])[0][0] for a in used}
    t1s = {(raw[a][0].get("equity_curve") or [(0, None)])[min_len - 1][0]
           for a in used}
    if len(t0s) != 1 or len(t1s) != 1:
        return None, used, dropped
    return np.column_stack([series[a] for a in used]), used, dropped


def _post_selection_stats(raw: dict, arms: list) -> dict | None:
    """DSR/PBO/PSR/MinTRL/HAC layer (see eval/stats.py): the answer to "you
    ran N arms and read the best one — what is actually left of that?"."""
    from .stats import arm_stats, cscv_pbo, dsr as deflated_sharpe, hac_t
    from .stats import sharpe as _sr
    R, used, dropped = _returns_matrix(raw, arms)
    if R is None:
        return None
    per_arm = {a: arm_stats(R[:, i]) for i, a in enumerate(used)}
    srs = np.array([_sr(R[:, i]) for i in range(len(used))])
    finite = np.isfinite(srs)
    if finite.sum() < 2:
        return None
    srs_f = srs[finite]
    used_f = [a for a, ok in zip(used, finite) if ok]
    champ = used_f[int(np.argmax(srs_f))]
    var_sr = float(np.var(srs_f, ddof=1))
    out = {
        "used_arms": used_f,
        "dropped_flat_arms": dropped,
        "n_obs": int(R.shape[0]),
        "n_trials": len(used_f),
        "per_arm": per_arm,
        "champion": {"arm": champ,
                     "sr": round(float(srs_f.max()), 6),
                     "dsr": round(deflated_sharpe(
                         R[:, used.index(champ)], len(used_f), var_sr), 4),
                     "var_sr_trials": round(var_sr, 10)},
        "pbo_cscv": {str(nb): round(cscv_pbo(R, nb), 4)
                     for nb in (4, 6, 8, 10) if R.shape[0] >= 2 * nb},
    }
    return out


def _hac_vs_base(R: np.ndarray, used: list, base: str) -> dict | None:
    """Newey-West t-stat of every arm's per-candle paired difference against
    the baseline arm — the autocorrelation-robust version of paired_diff."""
    from .stats import hac_t
    if base not in used or R is None:
        return None
    bi = used.index(base)
    return {a: hac_t(R[:, i] - R[:, bi])
            for i, a in enumerate(used) if i != bi}


def run_protocol(cfg, rows_by_pair: dict, arms=ARMS, seeds=(1, 2, 3, 4, 5),
                 out_dir: str | None = None, verbose: bool = True,
                 jobs: int = 1) -> dict:
    """Run every (pair, arm, seed) combination and aggregate.

    rows_by_pair: {pair: candles}. Pairs with too little history are skipped
    and named in the report rather than silently dropped.
    jobs > 1 spreads the runs over that many processes; each run is independent
    and deterministic, so the aggregate is identical to the serial one."""
    report = {"arms": list(arms), "seeds": list(seeds), "jobs": int(jobs),
              "pairs": {}}
    for pair, rows in rows_by_pair.items():
        if len(rows) < cfg.brain.window + 50:
            report["pairs"][pair] = {"skipped": f"{len(rows)} candles"}
            if verbose:
                print(f"[protocol] {pair}: not enough candles ({len(rows)}), skipped")
            continue
        entry = {}
        tasks = [(cfg, pair, rows, arm, s) for arm in arms for s in seeds]
        if jobs and jobs > 1:
            from concurrent.futures import ProcessPoolExecutor
            with ProcessPoolExecutor(max_workers=int(jobs)) as ex:
                results = list(ex.map(_run_one, tasks))
        else:
            results = [_run_one(t) for t in tasks]
        raw = {arm: [] for arm in arms}
        for (_, _, _, arm, _), res in zip(tasks, results):
            raw[arm].append(res)
        for arm in arms:
            entry[arm] = summarize(raw[arm])
            if verbose:
                s = entry[arm]
                print(f"[protocol] {pair:11s} {arm:13s} ret "
                      f"{s['return_pct']['mean']:+7.3f}% "
                      f"[{s['return_pct']['lo']:+7.3f}, {s['return_pct']['hi']:+7.3f}] "
                      f"matched-bh {s['bh_matched_return_pct']['mean']:+7.3f}% "
                      f"trades {s['round_trips']['mean']:.1f} "
                      f"dd {s['max_drawdown_pct']['mean']:.1f}% "
                      f"n={s['return_pct']['n']}")
        # the comparisons that actually carry information. The baseline is
        # `normal` when present; otherwise the first requested arm (e.g. a
        # wiring-only run has no normal arm and compares against `frozen`).
        base = "normal" if "normal" in raw else arms[0]
        if "frozen" in raw and base in raw:
            entry["vs_clean_ablation"] = paired_diff(
                [r["return_pct"] for r in raw[base]],
                [r["return_pct"] for r in raw["frozen"]])
        if "rewired" in raw and base in raw:
            entry["wiring_matters"] = paired_diff(
                [r["return_pct"] for r in raw[base]],
                [r["return_pct"] for r in raw["rewired"]])
        if "buyhold" in raw and base in raw:
            entry["vs_buyhold"] = paired_diff(
                [r["return_pct"] for r in raw[base]],
                [r["return_pct"] for r in raw["buyhold"]])
        if base in raw:
            # every other arm against the current live configuration, so a new
            # mechanism is judged by its interval and not by its mean
            entry["vs_normal"] = {
                arm: paired_diff([r["return_pct"] for r in raw[arm]],
                                 [r["return_pct"] for r in raw[base]])
                for arm in raw if arm != base}
        # post-selection layer: the champion's DSR, the zoo's PBO, per-arm
        # PSR/MinTRL and HAC t-stats of paired diffs. Without this the best
        # arm in a 15-arm grid is the luckiest monkey, not the best strategy.
        st = _post_selection_stats(raw, list(arms))
        if st is not None:
            entry["post_selection"] = st
            if st["used_arms"] and base in st["used_arms"]:
                Rm, used_m, _ = _returns_matrix(raw, list(arms))
                if Rm is not None:
                    entry["paired_hac_vs_base"] = {
                        a: {"t": round(v["t"], 3),
                            "se_hac": round(v["se_hac"], 8),
                            "se_ols": round(v["se_ols"], 8), "n": v["n"]}
                        for a, v in _hac_vs_base(Rm, used_m, base).items()}
            c = st["champion"]
            if verbose:
                pbo = st["pbo_cscv"]
                print(f"[protocol] {pair:11s} stats      "
                      f"champ {c['arm']:>13s} sr={c['sr']:+.5f} "
                      f"DSR={c['dsr']:.3f} "
                      f"PBO={'/'.join(str(pbo[k]) for k in sorted(pbo, key=int))} "
                      f"minTRL={st['per_arm'][c['arm']]['min_trl']:.0f} bars "
                      f"(n={st['n_obs']}, trials={st['n_trials']})")
        report["pairs"][pair] = entry
    if out_dir:
        os.makedirs(out_dir, exist_ok=True)
        p = os.path.join(out_dir, "protocol_report.json")
        with open(p, "w") as f:
            json.dump(report, f, indent=1)
        if verbose:
            print(f"[protocol] -> {p}")
    return report
