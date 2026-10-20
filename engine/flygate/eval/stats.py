"""Post-selection statistics for the protocol's arm zoo.

为什么需要这一层：protocol 跑 N 个臂然后读最好的那个 —— 挑出来的冠军天生
虚高（15 只猴子抛硬币，总有一只连对 60 次）。这组工具把"试了 N 次"扣掉，
让"学习比冻结好 0.2pp"这种结论第一次变得可判定：

  PSR      给定一条收益序列，真实 Sharpe 超过基准的概率（Bailey & López de
           Prado 2012；用偏度/峰度修正正态假设的失效）
  DSR      冠军的 PSR，但基准换成"N 个臂里最大 Sharpe 的期望值"
           （Bailey & López de Prado 2014，selection 扣除）
  PBO      CSCV 组合对称交叉验证：样本内冠军在样本外掉到中位数以下的频率
           （Bailey et al. 2017）——PBO≈0.5 意味着"样本内择优"纯属噪声
  MinTRL   要让 PSR 达到 1-α 所需的最少观测数（"还需要几根 K 线才能下结论"）
  hac_t    配对差均值的 Newey-West t 统计（自相关稳健，逐根 K 线的配对差
           不是独立样本）

全部 numpy + 标准库（statistics.NormalDist 提供 Φ/Φ⁻¹），无新依赖、无随机数。
输入是 per-candle 权益收益序列——protocol 的 raw 结果里就有（equity_curve）。
诚实声明：这些检验假设序列平稳；手续费地板使收益呈"零散+尖峰"分布，偏度/
峰度修正会让判定偏保守——宁可保守。
"""
from __future__ import annotations

import math
from statistics import NormalDist

import numpy as np

_N = NormalDist()
_EULER_GAMMA = 0.5772156649015329


def sharpe(returns) -> float:
    """Per-period Sharpe (not annualized — the probabilities below don't care)."""
    r = np.asarray(returns, dtype=float)
    if r.size < 3:
        return float("nan")
    sd = float(r.std(ddof=1))
    return float(r.mean() / sd) if sd > 1e-15 else float("nan")


def _moments(r: np.ndarray) -> tuple[float, float]:
    """(skewness γ3, kurtosis γ4 with normal = 3)."""
    sd = float(r.std(ddof=1))
    if sd <= 1e-15:
        return float("nan"), float("nan")
    z = (r - r.mean()) / sd
    return float((z ** 3).mean()), float((z ** 4).mean())


def psr(returns, sr_benchmark: float = 0.0) -> float:
    """Probability that the true Sharpe exceeds sr_benchmark."""
    r = np.asarray(returns, dtype=float)
    if r.size < 3:
        return float("nan")
    sr = sharpe(r)
    if not math.isfinite(sr):
        return float("nan")
    g3, g4 = _moments(r)
    if not math.isfinite(g3):
        return float("nan")
    denom = 1.0 - g3 * sr + (g4 - 1.0) / 4.0 * sr * sr
    if denom <= 1e-12:            # extreme moments: the estimate is worthless
        return float("nan")
    z = (sr - sr_benchmark) * math.sqrt(r.size - 1) / math.sqrt(denom)
    return _N.cdf(z)


def expected_max_sharpe(n_trials: int, var_sr: float) -> float:
    """E[max SR] over n_trials independent strategies whose SR estimates have
    cross-trial variance var_sr (Bailey & López de Prado 2014, eq. 3-5)."""
    if n_trials < 2 or not math.isfinite(var_sr) or var_sr <= 0.0:
        return 0.0
    return math.sqrt(var_sr) * (
        (1.0 - _EULER_GAMMA) * _N.inv_cdf(1.0 - 1.0 / n_trials)
        + _EULER_GAMMA * _N.inv_cdf(1.0 - 1.0 / (n_trials * math.e)))


def dsr(returns, n_trials: int, var_sr: float) -> float:
    """Deflated Sharpe Ratio: PSR against the expected MAX Sharpe of n_trials
    strategies. A DSR > 0.95 survives "we tried N things and picked this one"."""
    return psr(returns, sr_benchmark=expected_max_sharpe(n_trials, var_sr))


def min_trl(returns, sr_benchmark: float = 0.0, alpha: float = 0.05) -> float:
    """Minimum track record length: observations needed for PSR > 1-α.
    inf when the observed SR does not already beat the benchmark."""
    r = np.asarray(returns, dtype=float)
    if r.size < 3:
        return float("inf")
    sr = sharpe(r)
    g3, g4 = _moments(r)
    if not math.isfinite(sr) or not math.isfinite(g3) or sr <= sr_benchmark:
        return float("inf")
    denom = 1.0 - g3 * sr + (g4 - 1.0) / 4.0 * sr * sr
    if denom <= 0.0:
        return float("inf")
    z = _N.inv_cdf(1.0 - alpha)
    return 1.0 + denom * (z / (sr - sr_benchmark)) ** 2


def cscv_pbo(returns_matrix, n_blocks: int = 6) -> float:
    """Probability of Backtest Overfitting via CSCV (Bailey et al. 2017).

    returns_matrix: T×N — T aligned per-candle returns, N strategies/arms.
    Split the T rows into n_blocks even blocks; for every way to choose half
    the blocks as in-sample: rank arms by SR on IS, take the best, measure its
    OOS relative rank ω, logit λ = log(ω/(1−ω)). PBO = Pr(λ ≤ 0) — how often
    the in-sample champion is below the OOS median. ≈0.5 means selection is
    noise; the ~0.1–0.2 band is the usual floor for honestly different arms.
    """
    R = np.asarray(returns_matrix, dtype=float)
    T, N = R.shape
    if N < 2 or T < n_blocks * 2:
        return float("nan")
    rows = R[-n_blocks * (T // n_blocks):]          # trim head to even blocks
    blocks = np.array_split(rows, n_blocks)
    logits = []
    for mask in _half_masks(n_blocks):
        is_r = np.concatenate([b for b, m in zip(blocks, mask) if m])
        oos_r = np.concatenate([b for b, m in zip(blocks, mask) if not m])
        sr_is = _col_sharpe(is_r)
        if not np.isfinite(sr_is).any():
            continue
        best = int(np.nanargmax(sr_is))
        sr_oos = _col_sharpe(oos_r)
        if not np.isfinite(sr_oos[best]):
            logits.append(-math.inf)             # champion not even ranked OOS
            continue
        smaller = float((sr_oos < sr_oos[best]).sum())
        equal = float((sr_oos == sr_oos[best]).sum())
        omega = (smaller + 0.5 * equal) / N       # relative rank in (0,1)
        omega = min(max(omega, 1e-3), 1.0 - 1e-3)
        logits.append(math.log(omega / (1.0 - omega)))
    if not logits:
        return float("nan")
    below = sum(1 for lam in logits if lam <= 0.0)
    return below / len(logits)


def _half_masks(n_blocks: int):
    """Every way to pick half the blocks as in-sample (C(S, S/2) masks)."""
    from itertools import combinations
    for pick in combinations(range(n_blocks), n_blocks // 2):
        yield [i in pick for i in range(n_blocks)]


def _col_sharpe(R: np.ndarray) -> np.ndarray:
    mu = R.mean(axis=0)
    sd = R.std(axis=0, ddof=1)
    out = np.full(R.shape[1], np.nan)
    ok = sd > 1e-15
    out[ok] = mu[ok] / sd[ok]
    return out


def hac_t(diffs, lag: int | None = None) -> dict:
    """Newey-West HAC t-statistic of the mean of an aligned difference series.

    Per-candle paired returns overlap (positions persist across bars), so the
    OLS standard error understates uncertainty; the Bartlett-kernel NW
    correction widens it. se_hac/se_ols >> 1 is the signature of that overlap.
    lag defaults to the ⌈T^(1/3)⌉ rule of thumb."""
    d = np.asarray(diffs, dtype=float)
    T = d.size
    if T < 10:
        return {"t": float("nan"), "se_ols": float("nan"), "se_hac": float("nan"),
                "lag": 0, "n": int(T), "mean": float("nan")}
    if lag is None:
        lag = max(1, int(round(T ** (1.0 / 3.0))))
    lag = int(min(lag, T - 2))
    mu = float(d.mean())
    e = d - mu
    g0 = float((e * e).mean())
    se_ols = math.sqrt(g0 / T)
    nw = g0
    for l in range(1, lag + 1):
        gl = float((e[l:] * e[:-l]).mean())
        w = 1.0 - l / (lag + 1.0)                 # Bartlett kernel
        nw += 2.0 * w * gl
    se_hac = math.sqrt(max(nw, 0.0) / T)
    t = mu / se_hac if se_hac > 0 else float("nan")
    return {"t": float(t), "se_ols": se_ols, "se_hac": se_hac,
            "lag": int(lag), "n": int(T), "mean": mu}


def arm_stats(returns) -> dict:
    """Per-arm summary the protocol prints: SR, PSR(0), MinTRL."""
    r = np.asarray(returns, dtype=float)
    return {"sr": round(sharpe(r), 6) if r.size >= 3 else float("nan"),
            "psr": round(psr(r), 4),
            "min_trl": (lambda v: v if math.isinf(v) else round(v, 1))(
                min_trl(r)),
            "n": int(r.size)}
