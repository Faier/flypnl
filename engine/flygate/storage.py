"""Durable state I/O in one place: temp file + os.replace, and tolerant loads
that never stop the bot from starting.

Every state file used to be written in place with json.dump straight into the
destination, and loaded with an unguarded json.load from Engine.__init__. A
crash or power loss during a save therefore left a truncated file (or two
threads interleaved their writes) and the next start raised JSONDecodeError
before it could trade — the bot needed manual file surgery to come back.
"""
from __future__ import annotations

import json
import os
import tempfile


def atomic_write_json(obj, path: str):
    """Write obj to path atomically (same-directory temp + fsync + replace)."""
    d = os.path.dirname(path) or "."
    os.makedirs(d, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".tmp-", dir=d)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(obj, f, indent=1, ensure_ascii=False)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def load_json(path: str, default=None):
    """Tolerant load: missing -> default; corrupt -> keep the evidence as
    `<path>.corrupt` and return default instead of raising."""
    if not os.path.exists(path):
        return default
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (ValueError, OSError):
        try:
            os.replace(path, path + ".corrupt")
        except OSError:
            pass
        return default


def default_runs_dir() -> str:
    """The state directory: repo-default `runs/`, overridable with FLYGATE_RUNS.

    A second deployment MUST NOT share this directory. Every state file is keyed
    by filename only — `engine.lock` (single-instance interlock), `{pair}.broker.json`
    (cash/position), `{pair}.brain.npz`, `risk.json`, `KILL` — so two processes in
    the same directory either refuse to start (lock) or silently clobber each
    other's equity. That is why the REAL-money satellite account runs with

        FLYGATE_RUNS=~/flygate-satellite/runs python3 -m flygate live

    beside the paper main show without touching its state or its lock."""
    env = os.environ.get("FLYGATE_RUNS")
    if env:
        return os.path.abspath(os.path.expanduser(env))
    return os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "runs")
