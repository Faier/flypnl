"""Read-only futures suitability scanner — recommends, never trades.

Ranks Gate USDT perpetuals by how well they fit THIS engine's style:
sustained staircases (rolling 60-close R^2 — the same coherence statistic
the brain's lobula readout computes on chart pixels, flybrain.chart_tilt,
r2 = sxy^2/(sxx*syy)), enough range for the +5% scale target to be reachable,
and acceptable funding/fee/spread cost. All endpoints public, no keys.

Usage:  python3 -m flygate scan [--top 12] [--min-vol 3.0] [--max-cand 40]
"""
from __future__ import annotations

import json
import os
import time

import numpy as np

from .data.gate_client import GateClient

WINDOW = 60          # candles per R^2 window — matches the brain's chart width
R2_GATE = 0.85       # the engine's own buy-gate coherence level


def rolling_r2(closes: list[float], w: int = WINDOW, step: int = 5):
    """Pearson-fit R^2 over rolling windows (same statistic as chart_tilt,
    applied to closes instead of rendered pixels). Returns (r2s, slopes)."""
    c = np.asarray(closes, dtype=float)
    n = len(c)
    if n < w + 1:
        return [], []
    x = np.arange(w, dtype=float)
    xm = x.mean()
    xx = x - xm
    sxx = float((xx * xx).sum())
    r2s, slopes = [], []
    for i in range(0, n - w + 1, step):
        y = c[i:i + w]
        ym = y.mean()
        d = y - ym
        sxy = float((xx * d).sum())
        syy = float((d * d).sum())
        denom = sxx * syy
        r2s.append(min(1.0, sxy * sxy / denom) if denom > 1e-18 else 0.0)
        slopes.append(sxy / sxx if sxx else 0.0)
    return r2s, slopes


def _candles(client: GateClient, contract: str, minutes: int) -> list:
    to = int(time.time() // 60) * 60 - 60
    frm = to - 60 * minutes
    raw = client._request("GET", "/futures/usdt/candlesticks",
                          query={"contract": contract, "interval": "1m",
                                 "from": frm, "to": to}) or []
    return sorted(raw, key=lambda r: r["t"])


def scan(top: int = 12, min_vol_m: float = 3.0, max_cand: int = 40,
         minutes: int = 990, runs_dir: str | None = None) -> list[dict]:
    client = GateClient()
    specs = {c["name"]: c for c in
             client._request("GET", "/futures/usdt/contracts")
             if not c.get("in_delisting")}
    tickers = {t["contract"]: t for t in
               client._request("GET", "/futures/usdt/tickers")}

    # ---- prefilter on the two bulk replies -------------------------------
    cands = []
    for name, tk in tickers.items():
        if name not in specs or tk.get("volume_24h_quote") is None:
            continue
        vol_q = float(tk["volume_24h_quote"])
        bid, ask, last = (float(tk.get("highest_bid") or 0),
                          float(tk.get("lowest_ask") or 0),
                          float(tk.get("last") or 0))
        if vol_q < min_vol_m * 1e6 or not (bid > 0 < ask and last > 0):
            continue
        cands.append({"name": name, "vol_q": vol_q, "bid": bid,
                      "ask": ask, "last": last})
    cands.sort(key=lambda d: -d["vol_q"])
    cands = cands[:max_cand]

    rows = []
    for cd in cands:
        name = cd["name"]
        spec, tk = specs[name], tickers[name]
        try:
            kl = _candles(client, name, minutes)
        except Exception:
            time.sleep(0.3)
            try:
                kl = _candles(client, name, minutes)
            except Exception:
                continue
        if len(kl) < WINDOW + 60:
            continue
        closes = [float(k["c"]) for k in kl]
        opens = [float(k["o"]) for k in kl]
        hi = max(float(k["h"]) for k in kl)
        lo = min(float(k["l"]) for k in kl)
        # worst single-minute jump (close vs open) — flags open-snap gap risk
        # on stock/ETF-session contracts (e.g. SOXL: index frozen outside US
        # hours, snaps hard at the session open / earnings)
        jump = max((abs(c / o - 1) for o, c in zip(opens, closes) if o > 0),
                   default=0.0)
        rng = (hi - lo) / cd["last"]                        # lookback range
        r2s, slopes = rolling_r2(closes)
        coh85 = (sum(1 for r in r2s if r >= R2_GATE) / len(r2s)) if r2s else 0.0
        mean_r2 = float(np.mean(r2s)) if r2s else 0.0
        up_bias = (sum(1 for r, s in zip(r2s, slopes)
                       if r >= R2_GATE and s > 0) /
                   max(1, sum(1 for r in r2s if r >= R2_GATE)))

        # signed funding for a LONG perp holder, normalized by the contract's
        # OWN interval: the old `abs(...) * 6` assumed 4h (doubling the cost of
        # every default-8h contract) and abs() punished contracts where a long
        # position is PAID to hold (negative funding) — strategies/funding_radar.py
        # already reads this exact field (funding_radar.py:49)
        interval_s = float(spec.get("funding_interval") or 28800)
        fund_day = float(tk.get("funding_rate") or 0) * (86400.0 / max(interval_s, 60.0))
        taker = float(spec.get("taker_fee_rate") or 0)
        spread = (cd["ask"] - cd["bid"]) / cd["last"]

        energy = coh85 * rng                                 # tradable energy
        cost = fund_day + 2 * taker + spread                 # signed round-trip drag
        fit = energy / (1.0 + 10.0 * cost)

        if coh85 >= 0.06 and rng >= 0.06:
            why = "台阶充足" if up_bias > 0.35 else "台阶充足(偏下行)"
        elif rng >= 0.10:
            why = "波动大但散(插针型)"
        else:
            why = "太安静"
        if jump >= 0.012:
            why += ",跳空风险"
        rows.append({"contract": name, "vol_musd": round(cd["vol_q"] / 1e6, 1),
                     "range_pct": round(rng * 100, 1),
                     "coh85_pct": round(coh85 * 100, 1),
                     "mean_r2": round(mean_r2, 2),
                     "up_bias": round(up_bias, 2),
                     "jump_pct": round(jump * 100, 2),
                     "funding_day_pct": round(fund_day * 100, 3),
                     "taker_pct": round(taker * 100, 3),
                     "spread_pct": round(spread * 100, 3),
                     "energy": round(energy, 5), "cost": round(cost, 5),
                     "fit": round(fit, 5), "verdict": why})
        time.sleep(0.15)

    rows.sort(key=lambda d: -d["fit"])

    if runs_dir:
        rp = os.path.join(runs_dir, "reports")
        os.makedirs(rp, exist_ok=True)
        out = os.path.join(rp, time.strftime("scan_%Y%m%d_%H%M.json"))
        with open(out, "w") as f:
            json.dump({"scanned_at": time.time(), "rows": rows}, f,
                      indent=1, ensure_ascii=False)
        print(f"[scan] report -> {out}")
    return rows


def run_scan(cfg, args) -> None:
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    rows = scan(top=args.top, min_vol_m=args.min_vol,
                max_cand=args.max_cand, runs_dir=os.path.join(root, "runs"))
    cur = cfg.futures.get("contract", "")
    hdr = (f"{'contract':<16}{'fit':>7}{'range%':>8}{'coh85%':>8}{'jump%':>7}"
           f"{'fund/d%':>9}{'taker%':>8}{'sprd%':>7}  verdict")
    print(hdr)
    print("-" * len(hdr))
    for r in rows[:args.top]:
        star = " *" if r["contract"] == cur else ""
        print(f"{r['contract'] + star:<16}{r['fit']:>7.4f}{r['range_pct']:>8}"
              f"{r['coh85_pct']:>8}{r['jump_pct']:>7}"
              f"{r['funding_day_pct']:>9}"
              f"{r['taker_pct']:>8}{r['spread_pct']:>7}  {r['verdict']}")
    print("\nfit = 台阶能量(coh85×range) / 成本惩罚; * = 当前在跑的合约; "
          "只推荐不下单")
