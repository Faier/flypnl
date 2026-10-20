# -*- coding: utf-8 -*-
"""Comprehensive Strategy Experiment on 60-Day 15m Historical Real Data.

Compares:
  A0_baseline     : Standard FlyBrain on 15m
  A1_lsh_gated    : + Fly-LSH pattern radar vetoing TOP_RESISTANCE & BEAR_BREAKDOWN
  A2_trip_teach   : + Trip-close realized P&L dopamine teaching
  A3_combo_pro    : + LSH gating + Trip teaching + Tightened 2.5% profit protection
"""
from __future__ import annotations

import os
import sys
import json
import dataclasses
import numpy as np

from flygate.config import Config
from flygate.backtest import run_backtest
from flygate.data.history import load_cache

PAIRS = ["BTC_USDT", "ETH_USDT", "SOL_USDT", "PONS_USDT"]

def evaluate():
    print("=" * 96)
    print("   FLYGATE QUANTITATIVE HISTORICAL STRATEGY COMPARISON (60-DAY 15m DATA)")
    print("=" * 96)
    print(f"{'PAIR':11s}{'ARM / STRATEGY':16s}{'DAYS':>5s}{'RET%':>8s}{'B&H%':>8s}{'TRADES':>7s}{'WIN%':>6s}{'FEES':>7s}{'MAX_DD':>8s}")
    print("-" * 96)

    summary = {}

    for pair in PAIRS:
        rows = load_cache(pair, "15m")
        if not rows or len(rows) < 200:
            print(f"Skipping {pair}: insufficient candles ({len(rows)})")
            continue

        days = round((rows[-1][0] - rows[0][0]) / 86400, 1)

        # Config templates
        arms = {
            "A0_baseline": {
                "lsh_gating": False,
                "trip_teaching": False,
            },
            "A1_lsh_gated": {
                "lsh_gating": True,
                "trip_teaching": False,
            },
            "A2_trip_teach": {
                "lsh_gating": False,
                "trip_teaching": True,
                "action_gated_elig": True,
                "elig_normalize": True,
            },
            "A3_combo_pro": {
                "lsh_gating": True,
                "trip_teaching": True,
                "action_gated_elig": True,
                "elig_normalize": True,
                "_risk_trail": 0.025,
            },
        }

        for arm_name, params in arms.items():
            cfg = Config()
            cfg.interval = "15m"
            cfg.quote_budget = 100.0
            cfg.brain.obs_interval_s = 900.0
            cfg.brain.mbon_inhibition = True

            # apply brain params
            for k, v in params.items():
                if k.startswith("_risk_"):
                    setattr(cfg.risk, k.replace("_risk_", ""), v)
                else:
                    setattr(cfg.brain, k, v)

            # run backtest
            res = run_backtest(cfg, pair, rows, mode="normal", verbose=False)
            
            k = f"{pair}::{arm_name}"
            summary[k] = {
                "pair": pair,
                "arm": arm_name,
                "days": res["days"],
                "return_pct": res["return_pct"],
                "bh_pct": res["buyhold_return_pct"],
                "trades": res["trades"],
                "win_rate": res["win_rate_pct"],
                "fees": res["fees_paid"],
                "max_dd": res["max_drawdown_pct"],
                "dopamine": res["dopamine_events"],
            }

            print(f"{pair:11s}{arm_name:16s}{res['days']:5.1f}{res['return_pct']:>+8.2f}%"
                  f"{res['buyhold_return_pct']:>+8.2f}%{res['trades']:>7d}"
                  f"{res['win_rate_pct']:>5.1f}%{res['fees_paid']:>7.2f}"
                  f"{res['max_drawdown_pct']:>7.1f}%")
        print("-" * 96)

    # Save artifact
    out_path = os.path.join("runs", "experiments_strategy_60d.json")
    os.makedirs("runs", exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(summary, f, indent=2, ensure_ascii=False)
    print(f"\n[DONE] Saved complete empirical breakdown to {out_path}")

if __name__ == "__main__":
    evaluate()
