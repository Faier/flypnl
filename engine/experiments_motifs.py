"""Motif validation on REAL cached 1m data — PONS (the market we actually
trade) as primary, ETH/SOL/BTC spot + ZEC futures as robustness checks.

Combos (each vs the same baseline brain, same risk config, same fees):
  base   — fly as it runs live right now (all motifs off)
  ring   — + ring-attractor heading gate
  mbo    — + MBON mutual-inhibition margin
  loom   — looming exit replaces the fixed flash-candle rule
  replay — + sleep replay (3 passes per day boundary)
  all    — ring + mbo + replay (loom measured separately: it changes the
           exit layer, so it is compared on its own row, not stacked)
"""
import sys, time, json
sys.path.insert(0, '/Users/mac/IdeaProjects/flygate')

from flygate.config import Config
from flygate.backtest import run_backtest
from flygate.backtest_futures import run_dual
from flygate.data.history import load_cache

COMBOS = {
    "base":   {},
    "ring":   {"ring_attractor": True},
    "mbo":    {"mbon_inhibition": True},
    "replay": {"replay_passes": 3},
    "all":    {"ring_attractor": True, "mbon_inhibition": True, "replay_passes": 3},
}
LOOM_FLAG = {"loom_exit": True}

def run_spot(pair, brain_flags):
    rows = load_cache(pair, "1m")
    cfg = Config()
    for k, v in brain_flags.items():
        setattr(cfg.brain, k, v)
    cfg.quote_budget = 100.0
    return run_backtest(cfg, pair, rows, mode="normal", verbose=False), len(rows)

def main():
    results = {}
    print(f"{'pair':11s}{'combo':7s}{'days':>5s}{'ret%':>9s}{'bh%':>8s}{'trips':>6s}"
          f"{'win%':>7s}{'fees':>7s}{'maxDD%':>7s}{'giveback':>9s}")
    for pair in ("PONS_USDT", "ETH_USDT", "SOL_USDT", "BTC_USDT"):
        for combo, flags in COMBOS.items():
            r, n = run_spot(pair, flags)
            results[f"{pair}/{combo}"] = {k: v for k, v in r.items() if k != "equity_curve" and k != "trade_log"}
            print(f"{pair:11s}{combo:7s}{r['days']:5.1f}{r['return_pct']:>+9.2f}"
                  f"{r['buyhold_return_pct']:>+8.2f}{r['trips']:>6d}{r['win_rate_pct']:>7.1f}"
                  f"{r['fees_paid']:>7.2f}{r['max_drawdown_pct']:>7.1f}{r['give_back']:>9.2f}"
                  f"  [{n} candles]")
        # looming exit: same brain as base, only the exit layer swaps
        rows = load_cache(pair, "1m")
        cfg = Config(); cfg.risk.loom_exit = True
        r = run_backtest(cfg, pair, rows, mode="normal", verbose=False)
        results[f"{pair}/loom"] = {k: v for k, v in r.items() if k != "equity_curve" and k != "trade_log"}
        print(f"{pair:11s}{'loom':7s}{r['days']:5.1f}{r['return_pct']:>+9.2f}"
              f"{r['buyhold_return_pct']:>+8.2f}{r['trips']:>6d}{r['win_rate_pct']:>7.1f}"
              f"{r['fees_paid']:>7.2f}{r['max_drawdown_pct']:>7.1f}{r['give_back']:>9.2f}")
    # futures: brain motifs only (loom exit not wired into run_dual)
    frows = load_cache("fut_ZEC_USDT", "1m")
    for combo, flags in (("base", {}), ("all", COMBOS["all"])):
        cfg = Config()
        for k, v in flags.items():
            setattr(cfg.brain, k, v)
        r = run_dual(cfg, frows, mode="dual", budget=30.0, verbose=False)
        results[f"fut_ZEC/{combo}"] = r
        print(f"{'fut_ZEC':11s}{combo:7s}"
              f"{(frows[-1][0]-frows[0][0])/86400:5.1f}{r['ret_pct']:>+9.2f}"
              f"{r['bh_pct']:>+8.2f}{r['rounds']:>6d}{r['win_pct']:>7.1f}"
              f"{'—':>7s}{r['maxdd_pct']:>7.1f}{'—':>9s}")
    with open('/Users/mac/IdeaProjects/flygate/runs/reports/motif_results.json', 'w') as f:
        json.dump(results, f, indent=1)
    print("\nsaved → runs/reports/motif_results.json")

if __name__ == "__main__":
    main()
