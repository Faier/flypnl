"""FlyBrain 遥测 tel → 去标识英文 state；三类问题模板；泄漏自检。

防前视是硬约束（docs/UNIFIED.md §4）:
- 白名单制：只放行已知安全的数值/布尔字段；字符串一律不收 ——
  从源头杜绝日期、币名、绝对价格混进 state（LLM 可能"记得"历史，
  带标识的回测结论全部作废）。
- validate_no_identifiers() 是最后一道闸：命中黑名单键或日期样式
  就报违规清单，调用方必须拒发（引擎里 warn + 跳过）。
"""
from __future__ import annotations

import math
import re

# 白名单：允许进入 state 的字段（全部是相对量/率/计数，无标识、无绝对价格）
ALLOWED_FIELDS = frozenset({
    # 双尺度小叶读出（flybrain.py chart_tilt / HS/FD EMA）
    "tilt_wide", "tilt_fast", "r2_wide", "r2_fast",
    "hs_up", "hs_dn", "fd_up", "fd_dn", "r2_s", "r2f_s",
    # 下行神经元（决策遥测）
    "diff_hz", "dn_up_hz", "dn_dn_hz",
    # 蘑菇体 / 可塑性健康
    "kc_active", "kc_sparse_frac", "mbon_margin", "edge_saturation",
    # 市况相对量（调用方从 K 线算好再传入，绝不传原始价）
    "window_ret_pct", "realized_vol_60", "vol_scale",
    # 持仓上下文
    "unrealized_pct", "time_in_market_bars",
})
HOLDINGS = ("flat", "long", "short")

_DENY_KEY = re.compile(
    r"^(ts|time|datetime|date|pair|symbol|ticker|price|open|high|low|close|"
    r"entry|cost|cash|equity|budget|notional)$", re.I)
_DATE_LIKE = re.compile(r"\d{4}-\d{2}-\d{2}|\d{2}:\d{2}(:\d{2})?")
_SYMBOL_LIKE = re.compile(r"(?<![A-Za-z])(BTC|ETH|SOL|XRP|DOGE|USDT|USD|CNY|RMB|PONS)(?![A-Za-z])", re.I)


def _num(v):
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)


def build_state(tel: dict, holding: str = "flat",
                unrealized_pct: float | None = None,
                extra: dict | None = None) -> dict:
    """白名单导出：非白名单键直接丢弃（不是报错——引擎里 tel 每次都在变）。"""
    out: dict = {}
    for k, v in (tel or {}).items():
        if k not in ALLOWED_FIELDS:
            continue
        if isinstance(v, bool):
            out[k] = v
        elif _num(v):
            out[k] = round(float(v), 6)
    if holding in HOLDINGS:
        out["holding"] = holding
    if _num(unrealized_pct):
        out["unrealized_pct"] = round(float(unrealized_pct), 6)
    for k, v in (extra or {}).items():
        if k in ALLOWED_FIELDS:
            if isinstance(v, bool):
                out[k] = v
            elif _num(v):
                out[k] = round(float(v), 6)
    return out


def validate_no_identifiers(state) -> list:
    """返回违规清单；非空 = 调用方必须拒发（防前视最后闸门）。"""
    bad: list = []

    def walk(obj, path):
        if isinstance(obj, dict):
            for k, v in obj.items():
                p = f"{path}.{k}"
                if _DENY_KEY.match(str(k)):
                    bad.append(p)
                    continue
                walk(v, p)
        elif isinstance(obj, (list, tuple)):
            for i, v in enumerate(obj):
                walk(v, f"{path}[{i}]")
        elif isinstance(obj, str):
            if _DATE_LIKE.search(obj):
                bad.append(f"{path} (date-like)")
            if _SYMBOL_LIKE.search(obj):
                bad.append(f"{path} (symbol-like)")

    walk(state, "$")
    return bad


def default_questions() -> dict:
    """三类问题（全英文常量；confidence 只记录不路由——实测会饱和到 1.000）。

    一次请求并行三问：state 只读一次，64k 预算内任意组合。
    """
    return {
        "direction": {
            "type": "choice",
            "instructions": (
                "You are the readout of a chart-derived feature vector from a "
                "trend-following system. Choose the exposure that maximizes expected "
                "return over the next 15 bars AFTER round-trip fees."
            ),
            "criteria": {
                "long": "coherent up-drift: expected upside > 3x round-trip cost",
                "flat": "no coherent edge, coherence collapsing, or two-sided chop",
                "short": "coherent down-drift: expected downside > 3x round-trip cost",
            },
        },
        "strength": {
            "type": "score",
            "instructions": ("Rate how strong and clean this setup is, independent of "
                             "direction. Drives position sizing, not direction."),
            "criteria": ["no signal", "very weak", "weak", "below average", "average",
                         "above average", "strong", "very strong", "rare clean setup"],
        },
        "valid_setup": {
            "type": "noul",
            "instructions": ("Is this a pre-registered, rule-described setup this system "
                             "is allowed to trade at all?"),
            "criteria": {"true": "shape matches a pre-registered setup class",
                         "false": "ambiguous, chasing, or chop regime"},
        },
    }
