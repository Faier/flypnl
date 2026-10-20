"""Polymarket 事件定价腿：Jev noul vs 市价 → edge；Brier/分箱校准报告。

与现货/合约腿的本质区别：事件市场没有"带趋势的图"，价格本身就是概率——
果蝇网络在此退场，Jev 的 noul（回答"会否发生"）是原生形态。
执行通道不在本包：先跑校准（Brier + 分箱对角线），过统计门再谈纸面下单。

防前视边界：事件标题/描述是"题目本身"，允许是任意文本；
但解析日期必须换算成相对天数 days_to_resolve（绝对日期不进 state）。
"""
from __future__ import annotations

BET_THRESHOLD = 0.08  # |edge| 超过此值才产生信号（覆盖摩擦/滑点；先纸面再谈实盘）


def price_event(client, title: str, description: str, days_to_resolve: int,
                market_yes_price: float, extra_context: str | None = None,
                threshold: float = BET_THRESHOLD, use_cache: bool = True) -> dict:
    """问一次 noul，返回 {p_model, market_yes_price, edge, signal}。

    signal: yes（模型概率显著高于市价 → 做多 YES）/ no（显著低于 → 做空 YES）/ pass
    """
    state = {"task": "event_resolution",
             "title": str(title),
             "description": str(description),
             "days_to_resolve": int(days_to_resolve)}
    if extra_context:
        state["context"] = str(extra_context)[:2000]
    questions = {
        "p_yes": {
            "type": "noul",
            "instructions": ("Will this event resolve YES? Judge only from the title, "
                             "description and context. Return a calibrated probability; "
                             "do not cheerlead and do not hedge."),
            "criteria": {"true": "the event resolves YES",
                         "false": "the event resolves NO"},
        },
    }
    ans = client.ask(state, questions, use_cache=use_cache)
    p = float(ans["p_yes"]["noul"])
    if not 0.0 <= p <= 1.0:
        raise ValueError(f"noul out of range: {p}")
    mkt = float(market_yes_price)
    edge = p - mkt
    signal = "yes" if edge > threshold else ("no" if edge < -threshold else "pass")
    return {"p_model": round(p, 4), "market_yes_price": round(mkt, 4),
            "edge": round(edge, 4), "signal": signal}


def brier(rows) -> float:
    """Brier 分数：rows = [(p_model, outcome 0/1), ...]。越低越好，0.25 = 无知基线。"""
    rows = list(rows)
    if not rows:
        raise ValueError("empty rows")
    return sum((float(p) - float(o)) ** 2 for p, o in rows) / len(rows)


def reliability_bins(rows, n: int = 5) -> list:
    """按预测概率等宽分箱 → 每箱 {lo, hi, n, p_mean, outcome_rate}。

    校准好 = p_mean ≈ outcome_rate（对角线）。这是 Polymarket 腿的**验收判据**：
    不需要赚钱就能判死刑或继续。
    """
    bins: list = [[] for _ in range(n)]
    for p, o in rows:
        i = min(n - 1, max(0, int(float(p) * n)))
        bins[i].append((float(p), float(o)))
    out = []
    for i, b in enumerate(bins):
        lo, hi = i / n, (i + 1) / n
        if not b:
            out.append({"bin": i, "lo": round(lo, 2), "hi": round(hi, 2), "n": 0})
            continue
        out.append({"bin": i, "lo": round(lo, 2), "hi": round(hi, 2), "n": len(b),
                    "p_mean": round(sum(x for x, _ in b) / len(b), 4),
                    "outcome_rate": round(sum(o for _, o in b) / len(b), 4)})
    return out


def calibration_report(rows, n: int = 5) -> dict:
    """一行调用得到验收包：Brier（精确值，供下游数学）+ 分箱（显示值已取整）。"""
    return {"brier": brier(rows), "bins": reliability_bins(rows, n=n),
            "n": len(list(rows))}
