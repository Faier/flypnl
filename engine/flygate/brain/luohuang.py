"""Huang/Luo 2024 基线中心抗 Hebbian 率规则（P1）。

移植自 StonkFly `stonkfly/neural/rule.py`（MIT License），后者把
Huang, Luo et al. 2024 (Nature, doi:10.1038/s41586-024-07819-w) 附录
方程 3.2–3.5 的缩减率模型改接到脉冲连接组的按边实测率上。

规则语义：最近的 KC 活动若跟随多巴胺 → 压低合格边（LTD）；时序相反 → 增强。
全部状态按边存放（真实 KC->MBON07/11 边，7,835 条）；u 是慢记忆态
（1800s 衰减），w 是快滤波后的效能偏差（50ms 滤波），写回权重时用
baseline × (1 + w)，界限 [0.1x, 2x] 基线（u/w 本身在 ±[0.9, 1] 裁剪）。

诚实声明（同 StonkFly）：把该规则用于雄性 CNS 重建的这些边是未经验证的
外推；gain 的 DAN 接触份额归一、脉冲幅度 dan_pulse_hz 是声明性建模选择。
"""
from __future__ import annotations

import math

import numpy as np

PARAMETERS = {
    "trace_kc_seconds": 1.0,
    "trace_dan_seconds": 1.0,
    "memory_decay_seconds": 1800.0,
    "weight_filter_seconds": 0.05,
    "minimum_fraction": 0.1,
    "maximum_fraction": 2.0,
    "maximum_rate_bin_ms": 10.0,
    "source": "https://doi.org/10.1038/s41586-024-07819-w",
    "ported_from": "https://github.com/nftechie/stonkfly (MIT)",
}


class EdgeRule:
    """按边的规则状态 + 单步推进。kc_hz/dan_hz 是本 bin 的实测率（Hz）。"""

    def __init__(self, n_edges: int, n_dan: int):
        self.y_kc = np.zeros(n_edges, dtype=np.float64)
        self.y_dan = np.zeros(n_dan, dtype=np.float64)
        self.u = np.zeros(n_edges, dtype=np.float64)
        self.w = np.zeros(n_edges, dtype=np.float64)

    def advance(self, kc_hz: np.ndarray, dan_hz: np.ndarray, gain: np.ndarray,
                dt_seconds: float, eta: float,
                learning: bool = True, frozen: bool = False) -> None:
        p = PARAMETERS
        if not math.isfinite(dt_seconds) or dt_seconds <= 0 \
                or dt_seconds > p["maximum_rate_bin_ms"] / 1000.0 + 1e-6:
            raise ValueError("rate bins must be 0-10 ms")
        h = dt_seconds
        ak = math.exp(-h / p["trace_kc_seconds"])
        ad = math.exp(-h / p["trace_dan_seconds"])
        kmid = self.y_kc * math.sqrt(ak) + kc_hz * (1 - math.sqrt(ak))
        dmid = self.y_dan * math.sqrt(ad) + dan_hz * (1 - math.sqrt(ad))
        self.y_kc[:] = self.y_kc * ak + kc_hz * (1 - ak)
        self.y_dan[:] = self.y_dan * ad + dan_hz * (1 - ad)
        if frozen or not learning:
            return
        drive = eta * (kc_hz * (gain.T @ dmid) - (gain.T @ dan_hz) * kmid)
        tu = p["memory_decay_seconds"]
        tw = p["weight_filter_seconds"]
        eu = math.exp(-h / tu)
        ew = math.exp(-h / tw)
        c = tu / (tu - tw) * (eu - ew)
        old_u = self.u.copy()
        self.u[:] = old_u * eu + drive * tu * (-math.expm1(-h / tu))
        self.w[:] = self.w * ew + old_u * c + drive * tu * \
            (-math.expm1(-h / tw) - c)
        lo = p["minimum_fraction"] - 1
        hi = p["maximum_fraction"] - 1
        np.clip(self.u, lo, hi, out=self.u)
        np.clip(self.w, lo, hi, out=self.w)

    def efficacy(self) -> np.ndarray:
        """写回系数：baseline × (1 + w)。"""
        return 1.0 + self.w
