"""Append-only order ledger: `runs/orders.jsonl`.

One JSON line per CONFIRMED fill — spot (paper/live) and futures
(paper/live) both land here, written at fill time. The in-snapshot trade
history could not survive the road: broker.json keeps only the last 500
and a paper restart restored cash but dropped trades entirely; the live
path depended on exchange `my_trades` queries capped at 50-100 rows.
This file is the durable, replayable record those are not.

Wiring: the engine (spot) and FuturesRunner (futures) attach one shared
`OrdersLedger` to each broker as `.ledger`; brokers call `.record(trade)`
on every confirmed fill. Backtests never wire it, so paper-trading
experiments can't pollute the ledger.

Readers: `python3 -m flygate orders`, GET /api/orders, analytics.
"""
from __future__ import annotations

import json
import os
import threading

_LOCK = threading.Lock()


def orders_path(runs_dir: str) -> str:
    return os.path.join(runs_dir, "orders.jsonl")


class OrdersLedger:
    """Appends confirmed fills to orders.jsonl. Recording must never break
    trading: every failure is swallowed by design."""

    def __init__(self, runs_dir: str):
        self.path = orders_path(runs_dir)
        # seq continues across restarts so a row can be cited uniquely
        self._seq = self._existing_rows()

    def _existing_rows(self) -> int:
        try:
            with open(self.path, encoding="utf-8") as f:
                return sum(1 for line in f if line.strip())
        except OSError:
            return 0

    def record(self, trade) -> dict | None:
        try:
            row = trade.as_dict() if hasattr(trade, "as_dict") else dict(trade)
            self._seq += 1
            row["seq"] = self._seq
            line = json.dumps(row, ensure_ascii=False, default=str)
            with _LOCK:
                os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
                with open(self.path, "a", encoding="utf-8") as f:
                    f.write(line + "\n")
            return row
        except Exception:
            return None


def read_orders(runs_dir: str, pair: str | None = None,
                mode: str | None = None, limit: int = 200) -> list[dict]:
    """Read the ledger, oldest last, filtered by exact pair (case-insensitive)
    and/or mode (paper / live / fut-paper / fut-live)."""
    path = orders_path(runs_dir)
    if not os.path.exists(path):
        return []
    want_pair = pair.upper() if pair else None
    out = []
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    r = json.loads(line)
                except ValueError:
                    continue          # a torn tail line must not kill reads
                if want_pair and str(r.get("pair", "")).upper() != want_pair:
                    continue
                if mode and r.get("mode") != mode:
                    continue
                out.append(r)
    except OSError:
        return []
    try:
        limit = max(1, int(limit))
    except (TypeError, ValueError):
        limit = 200
    return out[-limit:]
