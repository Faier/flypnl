"""FlyGate CLI.

  python -m flygate fetch   [--days 14]              # backfill history for all pairs
  python -m flygate backtest --pair BTC_USDT [--days 7] [--mode normal|frozen|random|buyhold] [--all]
  python -m flygate protocol [--pair BTC_USDT] [--seeds 5] [--arms normal,frozen,...]
                                                     # multi-seed arms + CI + matched benchmark
  python -m flygate paper                              # live Gate data + fake money + dashboard
  python -m flygate live [--preflight-only]            # REAL orders (keys in .env), guards on
"""
from __future__ import annotations

import argparse
import json
import os
import threading
import time

from .config import Config
from .data.gate_client import GateClient
from .data.history import fetch_history, load_cache
from .backtest import run_backtest, save_result
from .trader.engine import Engine
from .storage import default_runs_dir


def load_env():
    env = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env")
    if os.path.exists(env):
        for line in open(env):
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, _, v = line.partition("=")
                os.environ.setdefault(k.strip(), v.strip())


def cmd_fetch(cfg, args):
    client = GateClient()
    for pair in cfg.pairs:
        print(f"[fetch] {pair} {cfg.interval}, {args.days} days")
        fetch_history(client, pair, cfg.interval, days=args.days)


def cmd_backtest(cfg, args):
    pairs = cfg.pairs if (args.all or not args.pair) else [args.pair]
    modes = (["normal", "frozen", "frozen_legacy", "random", "buyhold", "rewired"]
             if args.all_modes else [args.mode])
    out = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "runs")
    for pair in pairs:
        rows = load_cache(pair, cfg.interval)
        if args.days:
            cutoff = time.time() - args.days * 86400
            rows = [r for r in rows if r[0] >= cutoff]
        print(f"[backtest] {pair}: {len(rows)} cached candles "
              f"(fetch first with: python -m flygate fetch)")
        if len(rows) < cfg.brain.window + 50:
            print(f"  not enough data for {pair}, skipping")
            continue
        for mode in modes:
            res = run_backtest(cfg, pair, rows, mode=mode)
            name = save_result(res, out)
        print(f"  -> {out}/")


def cmd_protocol(cfg, args):
    """Multi-seed, multi-arm measurement with bootstrap CIs (Phase 0)."""
    from .eval.protocol import ARMS, run_protocol
    pairs = cfg.pairs if (args.all or not args.pair) else [args.pair]
    arms = [a.strip() for a in args.arms.split(",") if a.strip()] or list(ARMS)
    unknown = [a for a in arms if a not in ARMS]
    if unknown:
        raise SystemExit(f"unknown arm(s) {unknown}; choose from {list(ARMS)}")
    jobs = args.jobs if args.jobs else max(1, (os.cpu_count() or 2) - 1)
    print(f"[protocol] arms={arms} seeds={args.seeds} jobs={jobs}")
    rows_by_pair = {}
    for pair in pairs:
        rows = load_cache(pair, cfg.interval)
        if args.days:
            cutoff = time.time() - args.days * 86400
            rows = [r for r in rows if r[0] >= cutoff]
        rows_by_pair[pair] = rows
        print(f"[protocol] {pair}: {len(rows)} cached candles")
    out = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                       "runs", "reports")
    run_protocol(cfg, rows_by_pair, arms=arms,
                 seeds=tuple(range(1, args.seeds + 1)), out_dir=out,
                 jobs=jobs)


def _run_engine(cfg: Config, live: bool, preflight_only: bool = False,
                shadow: bool = False):
    load_env()
    engine = Engine(cfg, live=live, shadow=shadow)
    from .server.app import Dashboard
    dash = Dashboard(engine)
    httpd = dash.serve(cfg.host, cfg.port)
    print(f"[dash ] http://{cfg.host}:{cfg.port}/")
    if live and preflight_only:
        engine.preflight()
        httpd.shutdown()
        return
    t = threading.Thread(target=engine.run_forever, daemon=True, name="engine")
    t.start()
    try:
        while t.is_alive():
            t.join(1)
    except KeyboardInterrupt:
        print("\n[main ] Ctrl-C — stopping (brain state saved, resumable)")
        engine.stop()
        time.sleep(1)
    httpd.shutdown()


def cmd_paper(cfg, args):
    _run_engine(cfg, live=False, preflight_only=False,
                shadow=bool(getattr(args, "shadow", False)))


def cmd_live(cfg, args):
    load_env()
    if not os.environ.get("FLYGATE_KEY"):
        print("[live ] set FLYGATE_KEY / FLYGATE_SECRET in .env first "
              "(SPOT trade permission ONLY, no withdrawal)")
        return
    _run_engine(cfg, live=True, preflight_only=args.preflight_only)


def main():
    ap = argparse.ArgumentParser(prog="flygate")
    ap.add_argument("--config", default=None, help="path to config.json")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("fetch")
    p.add_argument("--days", type=float, default=14)

    p = sub.add_parser("backtest")
    p.add_argument("--pair", default=None)
    p.add_argument("--days", type=float, default=None)
    p.add_argument("--mode", default="normal",
                   choices=["normal", "frozen", "frozen_legacy", "random",
                            "buyhold", "rewired", "mbon_dn", "learn",
                            "real_wired", "frozen_dn", "real_wired_dn",
                            "real_wired_learn", "learn_trip",
                            "real_wired_learn_trip", "real_wired_rule_trip"])
    p.add_argument("--all", action="store_true", help="all configured pairs")
    p.add_argument("--all-modes", action="store_true", help="full control protocol")

    p = sub.add_parser("protocol", help="multi-seed arms + bootstrap CIs")
    p.add_argument("--pair", default=None)
    p.add_argument("--all", action="store_true", help="all configured pairs")
    p.add_argument("--days", type=float, default=None)
    p.add_argument("--seeds", type=int, default=5, help="seeds 1..N (N trials per arm)")
    p.add_argument("--arms",
                   default="normal,frozen,frozen_legacy,mbon_dn,learn,"
                           "random,buyhold,rewired,real_wired,frozen_dn,"
                           "real_wired_dn,real_wired_learn,learn_trip,"
                           "real_wired_learn_trip,real_wired_rule_trip")
    p.add_argument("--jobs", type=int, default=0,
                   help="parallel processes (0 = cpu_count-1, 1 = serial)")

    p = sub.add_parser("paper")
    p.add_argument("--shadow", action="store_true",
                   help="co-run a frozen twin fly on the same stream "
                        "(paper control arm for the trip-teaching loop)")
    p = sub.add_parser("live")
    p.add_argument("--preflight-only", action="store_true")
    p = sub.add_parser("logs")
    p.add_argument("--cat", default=None, help="filter: LIFE/TRADE/EXIT/RISK/ERR")
    p.add_argument("--level", default=None)
    p.add_argument("--day", default=None, help="e.g. 20260912")
    p.add_argument("--last", type=int, default=30)

    p = sub.add_parser("scan", help="recommend futures contracts (read-only)")
    p.add_argument("--top", type=int, default=12)
    p.add_argument("--min-vol", type=float, default=3.0, help="min 24h quote vol, millions USD")
    p.add_argument("--max-cand", type=int, default=40)

    p = sub.add_parser("train", help="offline imitation pretraining (hindsight -> dopamine)")
    p.add_argument("--tag", required=True, help="brain tag, e.g. FUT_ZEC_USDT")
    p.add_argument("--contract", required=True, help="perp contract to train on, e.g. ZEC_USDT")
    p.add_argument("--days", type=float, default=7)
    p.add_argument("--epochs", type=int, default=3)
    p.add_argument("--horizon", type=int, default=30, help="hindsight label horizon, minutes")
    p.add_argument("--band", type=float, default=0.004, help="label deadband (0.4%%)")

    p = sub.add_parser("radar", help="multi-chain meme token radar (trending scan + GoPlus security audit)")
    p.add_argument("--chain", default="ALL", help="filter by chain: ALL / SOL / BASE / BSC / ARB / ETH / ROBINHOOD")
    p.add_argument("--query", default=None, help="query wallet address or token contract")
    p.add_argument("--add", default=None, help="add wallet address to tracking pool")
    p.add_argument("--alias", default="", help="alias name for added wallet")

    args = ap.parse_args()
    if args.cmd == "radar":
        import sys as _sys
        if hasattr(_sys.stdout, "reconfigure"):
            try:
                _sys.stdout.reconfigure(encoding="utf-8")
            except Exception:
                pass
        import os as _os
        from .radar.tracker import RadarTracker
        runs = default_runs_dir()
        tracker = RadarTracker(runs)

        if args.add:
            w = tracker.wallet_db.add_wallet(args.chain if args.chain != "ALL" else "SOL",
                                             args.add, alias=args.alias)
            print(f"[radar] 已收藏地址: [{w['chain']}] {w['alias']} -> {w['address']}")
            return

        if args.query:
            res = tracker.query(args.chain, args.query)
            import json as _json
            print(f"\n===== [FlyGate 多链雷达查询] {args.chain} =====")
            print(_json.dumps(res, indent=2, ensure_ascii=False))
            return

        # Default: print new-token board and recent scan records
        wallets = tracker.wallet_db.list_wallets(args.chain)
        print("=" * 86)
        print("      [FlyGate] 新币雷达 (SOL / Base / BSC / Arbitrum / ETH · 上线 ≤6h)")
        print("      数据源: DexScreener 行情 + GoPlus 审计 · 只扫新币 · 无跟单信号")
        print("=" * 86)
        if wallets:
            print(f"\n[地址收藏池 ({len(wallets)} 个，仅记录)]:")
            for w in wallets:
                print(f"  {w['chain']:<6} {w['alias']:<24} {w['address']}")

        feed = tracker.get_feed(args.chain, limit=10)
        print(f"\n[最新扫描记录 ({len(feed)} 条)]:")
        print("-" * 86)
        for e in feed:
            t = e["token"]
            sec = e["security"]
            if sec.get("risk_level") == "UNKNOWN":
                sec_tag = "[未审计]"
            else:
                sec_tag = "[安全✔]" if sec.get("is_safe") else "[高危⚠]"
            age = e.get("token_age_text", "")
            src = {"trending": "热门", "new_launch": "新上架", "watchlist": "自选盯盘"}.get(e.get("source"), e.get("source", ""))
            avg = e.get("avg_trade_usd")
            avg_txt = f" | 5M均单: ${avg:,.0f}" if avg else ""
            print(f"[{e['time']}] [{e['chain']}] [{src}] {t['symbol']} ({age}){avg_txt} | 市值: ${t['market_cap']/1000:,.0f}k | {sec_tag} | {e['decision']['badge']}")
            print(f"      合约: {t['address']}")
            print(f"      K线: {e['chart_url']}")
        print("=" * 86)

        picks = tracker.get_picks_summary()
        if picks["total"]:
            print(f"\n[推荐回测追踪] 总样本 {picks['total']} (追踪中 {picks['open']})")
            for h, label in (("h1", " 1h"), ("h6", " 6h"), ("h24", "24h")):
                a = (picks.get("all") or {}).get(h) or {}
                if a.get("n"):
                    print(f"   {label}: 胜率 {a['win_rate']*100:.0f}% (n={a['n']}) · 均值 {a['avg_pct']:+.2f}% · 中位 {a['median_pct']:+.2f}% · 区间 [{a['worst_pct']:+.1f}%, {a['best_pct']:+.1f}%]")
                else:
                    print(f"   {label}: 数据积累中…")
        else:
            print("\n[推荐回测追踪] 已开启：雷达推出新标的即按当时价格纸面记录，1/6/24小时后回填真实涨跌。")
        return
    if args.cmd == "daily":
        import sys as _sys
        if hasattr(_sys.stdout, "reconfigure"):
            try:
                _sys.stdout.reconfigure(encoding="utf-8")
            except Exception:
                pass
        import os as _os
        from .analytics import format_daily_cli, get_daily_summary
        runs = default_runs_dir()
        if getattr(args, "json", False):
            import json as _json
            data = get_daily_summary(runs)
            if args.days and args.days > 0:
                data = data[:args.days]
            print(_json.dumps(data, indent=2, ensure_ascii=False))
        else:
            print(format_daily_cli(runs, days=args.days))
        return
    if args.cmd == "logs":
        import os as _os
        from .logs import read_logs, format_rec
        runs = default_runs_dir()
        for r in read_logs(runs, day=args.day, cat=args.cat,
                           level=args.level, last=args.last):
            print(format_rec(r))
        return
    cfg = Config.load(args.config)
    if args.cmd == "scan":
        from .scanner import run_scan
        run_scan(cfg, args)
        return
    if args.cmd == "train":
        from .train import cmd_train
        cmd_train(cfg, args)
        return
    {"fetch": cmd_fetch, "backtest": cmd_backtest, "protocol": cmd_protocol,
     "paper": cmd_paper, "live": cmd_live}[args.cmd](cfg, args)


if __name__ == "__main__":
    main()
