"""Historical kline fetcher with a local CSV cache.

The fly brain does not do offline training, but history is still needed for:
  1. the chart window rendered for the retina each observation,
  2. backtests / replay validation,
  3. control runs (buy & hold, random, frozen synapses).
"""
from __future__ import annotations

import csv
import os
import time

from .gate_client import GateClient, GateError, INTERVALS

CACHE_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))), "data", "cache")


def cache_path(pair: str, interval: str) -> str:
    return os.path.join(CACHE_DIR, f"{pair}_{interval}.csv")


def load_cache(pair: str, interval: str) -> list:
    """Rows of [ts(int), o, h, l, c, v] as floats, ascending, deduped."""
    p = cache_path(pair, interval)
    if not os.path.exists(p):
        return []
    seen, rows = set(), []
    with open(p) as f:
        for r in csv.reader(f):
            if len(r) != 6:
                continue
            ts = int(float(r[0]))
            if ts in seen:
                continue
            seen.add(ts)
            rows.append([ts] + [float(x) for x in r[1:]])
    rows.sort(key=lambda x: x[0])
    return rows


def append_cache(pair: str, interval: str, rows: list):
    os.makedirs(CACHE_DIR, exist_ok=True)
    p = cache_path(pair, interval)
    have = {r[0] for r in load_cache(pair, interval)}
    new = [r for r in rows if int(r[0]) not in have]
    if not new:
        return 0
    with open(p, "a", newline="") as f:
        w = csv.writer(f)
        for r in sorted(new, key=lambda x: float(x[0])):
            w.writerow([str(int(float(r[0])))] + [repr(float(x)) for x in r[1:]])
    return len(new)


def fetch_history(client: GateClient, pair: str, interval: str,
                  days: float, verbose: bool = True) -> list:
    """Backfill klines into the cache, return full cached series.

    Gate only serves the most recent 10000 points per interval, so the request
    is clamped (10000 x 1m ~ 6.9 days). For longer history use a bigger
    interval (5m -> ~34 days)."""
    step = INTERVALS[interval]
    horizon = int(time.time()) - 9000 * step     # Gate: ~10000 points max, page-safe
    want_from = max(int(time.time()) - int(days * 86400), horizon)
    to = int(time.time())
    fetched = 0
    while True:
        try:
            batch = client.klines(pair, interval, _to=to)
        except GateError as e:
            if "10000" in str(e) or "too long ago" in str(e):
                break                             # hit Gate's history horizon
            raise
        if not batch:
            break
        fetched += append_cache(pair, interval, batch)
        oldest = int(float(batch[0][0]))
        if oldest <= want_from or len(batch) < 2:
            break
        to = oldest - 1
        if verbose:
            print(f"  {pair} {interval}: back to {time.strftime('%Y-%m-%d %H:%M', time.localtime(oldest))}")
        time.sleep(0.15)  # be polite to public rate limits
    rows = load_cache(pair, interval)
    rows = [r for r in rows if r[0] >= want_from]
    if verbose:
        print(f"  {pair}: {len(rows)} candles cached (+{fetched} new)")
    return rows


def latest_closed(rows: list, interval: str, now: float | None = None) -> list:
    """Rows up to the most recent *closed* candle (Gate returns the forming
    candle as the last row with partial volume)."""
    now = now or time.time()
    step = INTERVALS[interval]
    last_closed_ts = int(now // step) * step - step
    return [r for r in rows if r[0] <= last_closed_ts]
