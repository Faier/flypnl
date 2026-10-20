"""Structured logging for FlyGate.

One JSON object per line (JSONL) in runs/logs/flygate-YYYYMMDD.jsonl:
    {"ts":..., "level":"INFO", "cat":"TRADE", "event":"fill", "pair":..., ...}

Categories: LIFE (lifecycle) / TRADE (orders & fills) / EXIT (exit ladder)
            / RISK (halts, caps) / BRAIN (decisions) / NET / ERR
Console keeps a short human line for important events only; the JSONL file
is the authoritative, machine-parseable record.
"""
from __future__ import annotations

import json
import os
import time

_LEVELS = ("DEBUG", "INFO", "WARN", "ERROR")
# categories that also echo to stdout (keep pm2 console useful, not noisy)
_ECHO = {"TRADE", "EXIT", "RISK", "LIFE", "ERR"}


class Log:
    def __init__(self, runs_dir: str):
        self.dir = os.path.join(runs_dir, "logs")
        os.makedirs(self.dir, exist_ok=True)

    def _file(self) -> str:
        return os.path.join(self.dir,
                            f"flygate-{time.strftime('%Y%m%d')}.jsonl")

    def write(self, level: str, cat: str, event: str, **fields):
        rec = {"ts": round(time.time(), 3),
               "time": time.strftime("%F %T"),
               "level": level, "cat": cat, "event": event}
        rec.update(fields)
        try:
            with open(self._file(), "a") as f:
                f.write(json.dumps(rec, ensure_ascii=False, default=str) + "\n")
        except Exception:
            pass
        if cat in _ECHO or level == "ERROR":
            brief = " ".join(f"{k}={v}" for k, v in fields.items()
                             if k not in ("reason",) and v is not None)
            print(f"[{level[:1]}{time.strftime('%H:%M:%S')}] {cat}/{event} {brief}",
                  flush=True)

    # convenience
    def info(self, cat, event, **f):   self.write("INFO", cat, event, **f)
    def warn(self, cat, event, **f):   self.write("WARN", cat, event, **f)
    def error(self, cat, event, **f):  self.write("ERROR", cat, event, **f)


def read_logs(runs_dir: str, day: str | None = None, cat: str | None = None,
              level: str | None = None, last: int = 50) -> list[dict]:
    """Read recent log records, newest last."""
    d = os.path.join(runs_dir, "logs")
    files = sorted(f for f in os.listdir(d) if f.endswith(".jsonl"))
    if day:
        files = [f for f in files if day in f]
    out = []
    for fn in files[-2:]:
        for line in open(os.path.join(d, fn)):
            try:
                r = json.loads(line)
            except Exception:
                continue
            if cat and r.get("cat") != cat:
                continue
            if level and r.get("level") != level:
                continue
            out.append(r)
    return out[-last:]


def format_rec(r: dict) -> str:
    base = f"{r.get('time','?')} [{r.get('level','?'):5s}] {r.get('cat','?')}/{r.get('event','?')}"
    extra = " ".join(f"{k}={v}" for k, v in r.items()
                     if k not in ("ts", "time", "level", "cat", "event"))
    return f"{base}  {extra}".rstrip()


class _NoLog:
    def error(self, *a, **k): print(f"[ERR] {k}", flush=True)


def _nolog():
    return _NoLog()
