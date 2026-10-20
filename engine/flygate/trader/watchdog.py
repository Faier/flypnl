"""Hang watchdog: heartbeat files + faulthandler evidence + SIGKILL relaunch.

The engine froze four times (09-14 .. 09-17, 1h41m-8h36m, all luckily
flat): a network call — prime suspect getaddrinfo, which urllib socket
timeouts do NOT bound — blocks a loop thread forever while the process
still looks alive to pm2. Two layers fix it:

  HangGuard (this file, in-process): every loop thread touches a heartbeat
  file each iteration; the guard thread watches them and when one goes
  stale dumps ALL thread stacks (faulthandler — catches the hang in the
  act), then SIGKILLs the process so pm2 relaunches a fresh engine.

  engine.sh backstop: the same mtime check in shell, for the case where
  the Python guard itself is starved (a true GIL deadlock). The shell
  cannot dump stacks, so its threshold deliberately sits ABOVE the
  in-process one — python normally wins the race and the evidence lands
  in runs/logs/hang_tracebacks.log first.

The heartbeat files double as a liveness probe for health checks: hb.*
mtime within ~10s = both loops turning. fut.state.json alone is too
coarse since the 5m TF switch (it only rewrites on candle closes).
"""
from __future__ import annotations

import faulthandler
import os
import signal
import threading
import time


class Beat:
    """One loop thread's heartbeat file (runs/hb.<name>)."""

    def __init__(self, path: str):
        self.path = path

    def beat(self):
        try:
            with open(self.path, "w") as f:
                f.write(f"{time.time():.3f}\n")
        except OSError:
            pass                      # a failed probe must not kill the loop


class HangGuard(threading.Thread):
    def __init__(self, runs_dir: str, log=None, stall_s: float = 180.0,
                 boot_grace_s: float = 300.0, poll_s: float = 10.0):
        super().__init__(daemon=True, name="hangguard")
        self.runs = runs_dir
        self._log = log
        self.stall_s = stall_s        # a healthy loop beats every ~5s; one
        # iteration may legally hold a few sequential 10s-timeout calls, so
        # 3 minutes with zero beats means hung, not slow
        self.boot_grace_s = boot_grace_s   # startup sync (position/trades
        # restore, first klines) may hold a loop before its first beat
        self.poll_s = poll_s
        self._t0 = time.time()
        self._beats: dict[str, Beat] = {}

    def register(self, name: str) -> Beat:
        hb = Beat(os.path.join(self.runs, f"hb.{name}"))
        self._beats[name] = hb
        return hb

    def stale(self, now: float | None = None) -> dict:
        """{name: age} for every heartbeat past its threshold. A thread
        that has never beaten only counts after the boot grace (its file
        does not exist yet)."""
        now = time.time() if now is None else now
        out = {}
        for name, hb in self._beats.items():
            try:
                age = now - os.path.getmtime(hb.path)
            except OSError:
                if now - self._t0 > self.boot_grace_s:
                    out[name] = now - self._t0
                continue
            if age > self.stall_s:
                out[name] = age
        return out

    def run(self):
        while True:
            time.sleep(self.poll_s)
            stale = self.stale()
            if not stale:
                continue
            ages = ", ".join(f"{k} {v:.0f}s" for k, v in sorted(stale.items()))
            # evidence FIRST — the stacks of the hung threads are the whole
            # point; the kill only comes after the dump is on disk
            dump = os.path.join(self.runs, "logs", "hang_tracebacks.log")
            try:
                os.makedirs(os.path.dirname(dump), exist_ok=True)
                with open(dump, "a") as f:
                    f.write(f"\n=== hang {time.strftime('%Y-%m-%d %H:%M:%S')}"
                            f" stale: {ages} ===\n")
                    f.flush()
                    faulthandler.dump_traceback_later(1.0, repeat=False, file=f)
                    time.sleep(3.0)   # let the dump land before SIGKILL
                    faulthandler.cancel_dump_traceback_later()
                    f.write("=== end dump ===\n")
            except Exception:
                pass
            if self._log is not None:
                try:
                    self._log.error("LIFE", "hang_kill", stale=ages,
                                    action="SIGKILL, pm2 relaunches")
                except Exception:
                    pass
            os.kill(os.getpid(), signal.SIGKILL)
