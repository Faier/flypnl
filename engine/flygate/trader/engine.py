"""AppState: thread-safe snapshot shared between the engine thread and the
dashboard server, plus the live/paper trading Engine.

Loop per closed candle (all pairs, one observation each):
  poll klines -> price -> mark-to-market equity delta (dopamine!) -> brain.observe
  -> decision -> risk.allow -> broker order -> risk bookkeeping -> persist -> push.
"""
from __future__ import annotations

import atexit
import json
import os
import threading
import time
from collections import deque

import numpy as np

from ..brain.flybrain import FlyBrain, BUY, SELL, HOLD
from ..brain.circuits import LoomingDetector
from ..config import Config
from ..data.gate_client import GateClient
from ..data.history import INTERVALS
from ..logs import Log
from ..storage import atomic_write_json, default_runs_dir, load_json
from .broker import PaperBroker, GateBroker
from .risk import RiskManager, reflex_exit, entry_notional
from .exits import ExitManager
from ..decide.jev import JevClient
from ..decide.state import build_state, validate_no_identifiers, default_questions

RUNS_DIR = None  # set by engine


def reason_short(r: str) -> str:
    return (r or "")[:60]


def _pid_alive(pid: int) -> bool:
    """Is another process holding the lock?

    On Windows os.kill(pid, 0) does not probe — it calls TerminateProcess, so
    it would kill the very engine we are trying to detect. Use tasklist there.
    The output is matched as raw bytes: on a localized Windows (zh-CN emits
    GBK) any text decoding can crash subprocess's reader thread and turn
    stdout into None — which took a whole engine start-up down with it.
    """
    if pid <= 0:
        return False
    if os.name == "nt":
        import subprocess
        try:
            out = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/NH"],
                                 capture_output=True, timeout=5).stdout or b""
        except Exception:
            return True                    # cannot tell -> assume alive
        return str(pid).encode() in out
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


class AppState:
    def __init__(self, pairs: list):
        self.lock = threading.Lock()
        self.version = 0
        self.started = time.time()
        self.mode = "paper"
        self.halted = False
        self.halt_reason = ""
        self.total_equity = 0.0
        self.day_pnl = 0.0
        self.peak_equity = 0.0
        self.equity_curve: list = []            # [[ts, equity], ...]
        self.events: deque = deque(maxlen=200)  # telemetry feed
        self.born = time.time()                 # service start (persistent ledger)
        # frozen shadow twin (P2 self-play): separate paper account, driven by
        # the same candles; the engine fills this when --shadow is on
        self.shadow = {"total_equity": 0.0, "day_pnl": 0.0, "gap": 0.0,
                       "equity_curve": [], "pairs": {}}
        # learning panel: dopamine counts, completed-trip lessons, portfolio
        # synaptic-drift curve and the live A/B gap (learning minus frozen)
        self.learning = {"da_today": {"reward": 0, "punish": 0},
                         "da_total": {"reward": 0, "punish": 0},
                         "trips_today": 0, "w_curve": [], "gap_curve": [],
                         "learner_curve": []}
        self.pairs = {p: {"price": 0.0, "candles": [], "cash": 0.0,
                          "position_base": 0.0, "position_notional": 0.0,
                          "equity": 0.0, "telemetry": {}, "trades": []} for p in pairs}

    def snapshot(self) -> dict:
        with self.lock:
            return {
                "mode": self.mode, "halted": self.halted,
                "halt_reason": self.halt_reason, "started": self.started,
                "born": self.born,
                "now": time.time(), "total_equity": round(self.total_equity, 4),
                "day_pnl": round(self.day_pnl, 4), "peak_equity": round(self.peak_equity, 4),
                "equity_curve": self.equity_curve[-600:],
                "events": list(self.events)[-120:],
                "pairs": {p: dict(v) for p, v in self.pairs.items()},
                "shadow": {"total_equity": self.shadow["total_equity"],
                           "day_pnl": self.shadow["day_pnl"],
                           "gap": self.shadow.get("gap", 0.0),
                           "equity_curve": self.shadow["equity_curve"][-600:],
                           "pairs": dict(self.shadow["pairs"])},
                "learning": {"da_today": dict(self.learning["da_today"]),
                             "da_total": dict(self.learning["da_total"]),
                             "trips_today": self.learning["trips_today"],
                             "sat_now": self.learning.get("sat_now"),
                             "w_curve": self.learning["w_curve"][-600:],
                             "gap_curve": self.learning["gap_curve"][-600:],
                             "learner_curve": self.learning["learner_curve"][-600:]},
            }


class Engine:
    def __init__(self, cfg: Config, live: bool = False, client: GateClient | None = None,
                 runs_dir: str | None = None, shadow: bool = False):
        global RUNS_DIR
        self.cfg = cfg
        # runs_dir is injectable so tests (and a second deployment) never touch
        # the operator's live state directory — the single-instance lock lives
        # here, so two test processes used to fight over the same file
        self.runs = runs_dir or default_runs_dir()
        os.makedirs(self.runs, exist_ok=True)
        RUNS_DIR = self.runs
        self.kill_file = os.path.join(self.runs, "KILL")
        self.log = Log(self.runs)
        # exactly one engine per account: two live processes on the same keys
        # each derive their own cash reserve from the same balance snapshot and
        # both place orders, doubling exposure past every configured cap while
        # the only shared interlock (runs/KILL) is written after the fact
        self._lock_path = os.path.join(self.runs, "engine.lock")
        self._acquire_lock()
        # dashboard control commands are QUEUED and executed by the engine
        # thread: /api/kill used to mutate broker.cash/pos straight from the
        # HTTP thread while the engine was mid-order, so the flatten could sell
        # the pre-buy base or land before the buy it was meant to cancel
        self._broker_lock = threading.RLock()
        self._control: tuple[str, str] | None = None
        # Jev (System One) gate — OFF by default; cache lives under runs/ so
        # replays are deterministic and free (docs/UNIFIED.md §3.5)
        self.jev = (JevClient(cache_path=os.path.join(self.runs, cfg.brain.jev_cache_path),
                              model=cfg.brain.jev_model, base_url=cfg.brain.jev_base_url,
                              timeout_s=cfg.brain.jev_timeout_s)
                    if cfg.brain.jev_enabled else None)

        self.client = client or GateClient(
            key=os.environ.get("FLYGATE_KEY", ""),
            secret=os.environ.get("FLYGATE_SECRET", ""))
        self.live = live and bool(self.client.key)
        # defense in depth for the paper/live split: in paper mode the client
        # itself refuses private write requests, so no code path (futures
        # runner included) can reach a real order endpoint by accident
        # (2026-09-15 audit: `flygate paper` still traded futures live)
        self.client.read_only = not self.live
        # the shadow twin is a PAPER instrument: refuse it wherever real keys
        # are in play, so no misconfiguration can put it near a live endpoint
        if shadow and self.live:
            raise ValueError("shadow twin is paper-only")
        self.state = AppState(cfg.pairs)
        self.state.mode = "live" if self.live else "paper"

        self.risk = RiskManager(cfg.risk, self.kill_file,
                                budget_per_pair=cfg.quote_budget, backtest=False)
        # round-trip cost of one order at the live size: the dopamine deadband
        # must clear this or the exchange's cut alone punishes the brain
        self._fee_roundtrip = 2.0 * cfg.risk.taker_fee * cfg.risk.order_notional
        # restore the counters a restart used to wipe (order cap, cooldown,
        # day anchor, peak): without them a bounce re-armed the daily loss
        # budget from the post-loss equity
        if self.risk.load(os.path.join(self.runs, "risk.json")) \
                and self.risk.halted and not os.path.exists(self.kill_file):
            # counters survived but the operator already cleared the switch
            self.risk.halted = False
            self.risk.halt_reason = ""
        # day anchor stays unset until every capital card has reported its
        # equity (see _cards_ready) — the futures thread lags startup by a
        # network round-trip and would otherwise anchor the day on spot-only
        self.risk.anchor_ready = not bool(self.cfg.futures.get("enable"))
        self.brokers = {}
        self.brains = {}
        # open trips per pair: {"cash0": quote cash before the entry,
        # "entry_ts": candle ts of the entry fill} — the teaching basis
        self._trips: dict[str, dict] = {}
        allocs = {}
        for p in cfg.pairs:
            budget = cfg.budgets.get(p, cfg.quote_budget)
            allocs[p] = budget
            if self.live:
                self.brokers[p] = GateBroker(p, budget, cfg.risk, self.client)
            else:
                self.brokers[p] = PaperBroker(p, budget, cfg.risk)
            brain = FlyBrain(cfg.brain, tag=p)
            # a truncated .npz must not stop the bot from starting; the brain
            # then simply re-converges from zero (its EMAs are decision state)
            try:
                brain.load(os.path.join(self.runs, f"{p}.brain.npz"))
            except Exception as e:
                self.log.warn("LIFE", "brain_state_unreadable", pair=p, err=str(e)[:80])
            self.brains[p] = brain
            if self.live:
                # trade history comes from the EXCHANGE (authoritative — local
                # files get wiped by crash/restart cycles); balances too
                base, quote = p.split("_")
                bals = None
                for attempt in range(5):
                    try:
                        bals = {b["currency"]: float(b["available"])
                                for b in self.client.balances()}
                        break
                    except Exception as e:
                        self.log.warn("NET", "balance_sync_retry", pair=p, attempt=attempt + 1, err=str(e)[:80])
                        time.sleep(4)
                if bals is None:
                    raise RuntimeError(f"cannot sync balances for {p} after retries")
                # per-pair cash reserve: each brain may only spend its own
                # allocation (minus what its position already uses). Multiple
                # pairs share one real USDT balance — without this split they
                # would each think they own ALL the cash and over-deploy.
                try:
                    px0 = float(self.client.ticker(p)["last"])
                except Exception:
                    px0 = 0.0
                pos_val = bals.get(base, 0.0) * px0
                reserve = max(0.0, allocs[p] - pos_val)
                self.brokers[p].cash = reserve
                self.brokers[p].pos.base = bals.get(base, 0.0)
                try:
                    from .broker import Trade
                    for ft in self.client.my_trades(p, limit=100):
                        self.brokers[p].trades.append(Trade(
                            int(float(ft["create_time"])), p, ft["side"].upper(),
                            float(ft["price"]), float(ft["amount"]),
                            float(ft.get("deal", 0) or 0), float(ft.get("fee", 0) or 0),
                            f"exchange {ft.get('role','')}", "live"))
                    self.brokers[p].trades.sort(key=lambda t: t.ts)
                except Exception as e:
                    self.log.warn("LIFE", "trade_history_restore_skipped", pair=p, err=str(e)[:80])
                print(f"[engine] {p}: synced from exchange — "
                      f"cash {self.brokers[p].cash:.2f} {quote}, "
                      f"base {self.brokers[p].pos.base}, "
                      f"{len(self.brokers[p].trades)} fills", flush=True)
            else:
                self.brokers[p].load(os.path.join(self.runs, f"{p}.broker.json"))

        if self.live:
            # scale all reserves down proportionally if they over-subscribe the
            # real shared USDT balance (prevents BALANCE_NOT_ENOUGH rejections)
            try:
                avail = float(next((b for b in self.client.balances()
                                    if b["currency"] == "USDT"),
                                   {"available": 0})["available"])
                total_res = sum(b.cash for b in self.brokers.values())
                if total_res > avail > 0:
                    k = avail / total_res
                    for b in self.brokers.values():
                        b.cash *= k
                    self.log.warn("RISK", "reserves_scaled", factor=round(k, 2), available=round(avail, 2))
            except Exception:
                pass
        self.last_equity = {p: None for p in cfg.pairs}
        self.last_candle_ts = {p: 0 for p in cfg.pairs}
        # motif: LC4/LC6-style exit — fire on threat EXPANSION rate (noise-
        # scaled) instead of the fixed flash-candle level; trailing unchanged
        self.looms = ({p: LoomingDetector() for p in cfg.pairs}
                      if getattr(cfg.risk, "loom_exit", False) else None)
        self.candles: dict[str, list] = {}
        # service ledger: born date + per-day equity/trade record (persistent)
        self.ledger_path = os.path.join(self.runs, "ledger.json")
        led = load_json(self.ledger_path, default=None)
        if led is None:
            led = {"born": time.time(), "days": {}}
            atomic_write_json(led, self.ledger_path)
        led.setdefault("days", {})
        self.ledger = led
        self.state.born = led.get("born", time.time())
        # seed the learning-panel counters from the persistent ledger (a
        # restart must not wipe the dopamine scoreboard)
        try:
            today = time.strftime("%Y-%m-%d")
            tot = {"reward": 0, "punish": 0}
            for d in self.ledger["days"].values():
                tot["reward"] += int(d.get("da_reward", 0))
                tot["punish"] += int(d.get("da_punish", 0))
            td = self.ledger["days"].get(today, {})
            self.state.learning["da_total"] = tot
            self.state.learning["da_today"] = {
                "reward": int(td.get("da_reward", 0)),
                "punish": int(td.get("da_punish", 0))}
            self.state.learning["trips_today"] = int(td.get("trips", 0))
        except Exception:
            pass
        self.pos_high = {p: 0.0 for p in cfg.pairs}   # reflex high-watermarks
        self.prev_close = {p: 0.0 for p in cfg.pairs}
        self.exitman = {p: ExitManager(cfg.risk) for p in cfg.pairs}
        self._halt_announced = False
        self._stop = threading.Event()
        # counterfactual capture (P2 metrics): every MBON veto on the learning
        # fly is parked here until the frozen twin's observation on the SAME
        # candle completes it; scripts/cf_report.py prices each veto against
        # the twin's realized outcome (runs/counterfactual.jsonl)
        self._cf_path = os.path.join(self.runs, "counterfactual.jsonl")
        self._cf_pending: dict = {}
        self._sat_warned = False

        # restore avg-cost basis for positions opened in a previous run
        # (needed by the proactive exits; balances still come from exchange)
        for p in cfg.pairs:
            fp = os.path.join(self.runs, f"{p}.pos.json")
            d = load_json(fp, default=None)
            if isinstance(d, dict):
                self.brokers[p].pos.cost = float(d.get("cost", 0.0))
                # don't re-fire a scale-out that already happened pre-restart
                self.exitman[p].scaled_out = bool(d.get("scaled_out", False))
                # a trip opened before a restart keeps its teaching basis; a
                # file without one (pre-P2 or mid-upgrade) means that open
                # trip simply teaches nothing rather than a wrong pairing
                if self.brokers[p].pos.base > 0 \
                        and d.get("trip_entry_ts") and d.get("trip_cash0") is not None:
                    self._trips[p] = {"cash0": float(d["trip_cash0"]),
                                      "entry_ts": float(d["trip_entry_ts"])}

        # ---- frozen shadow twin (P2 self-play; paper-only) ----------------
        # A second fly per pair, driven by the SAME closed candles, trading
        # its own paper account under the same risk rules, plasticity off.
        # The paired equity curves isolate the live value of teaching — the
        # paper twin of the protocol's `frozen` arm. Never runs in live mode.
        self.shadow_enabled = False
        self.sh_brokers: dict = {}
        self.sh_brains: dict = {}
        self.sh_exitman: dict = {}
        self.sh_looms = None
        self.sh_pos_high: dict = {}
        self.sh_prev_close: dict = {}
        self.sh_last_eq: dict = {}
        self.sh_last_candle_ts: dict = {}
        self.sh_tel: dict = {}
        self.sh_risk: RiskManager | None = None
        if shadow:
            self._shadow_init()

        # inherit a stale kill switch from a previous run (keep its reason)
        if os.path.exists(self.kill_file):
            self.risk.halted = True
            try:
                with open(self.kill_file) as f:
                    self.risk.halt_reason = f.readline().strip() or "kill switch (previous run)"
            except OSError:
                self.risk.halt_reason = "kill switch (previous run)"
            self.log.warn("RISK", "stale_kill_file", reason=self.risk.halt_reason)

    # ------------------------------------------------------------------
    def _acquire_lock(self):
        """One engine per runs/ directory. A stale lock (dead pid) is stolen
        with a warning; a live one is a hard start refusal."""
        try:
            fd = os.open(self._lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            pid = 0
            try:
                with open(self._lock_path) as f:
                    pid = int((f.read() or "0").strip() or 0)
            except (OSError, ValueError):
                pid = 0
            if pid == os.getpid():
                # a second Engine in this process is the same bug as a second
                # process: two instances would each derive their own cash
                # reserve and both place orders. Refuse both.
                raise RuntimeError(
                    f"this process already runs a flygate engine ({self._lock_path})")
            if _pid_alive(pid):
                raise RuntimeError(
                    f"another flygate engine is running (pid {pid}); stop it "
                    f"first, or delete {self._lock_path} if it crashed")
            self.log.warn("LIFE", "stale_engine_lock", pid=pid)
            try:
                os.replace(self._lock_path, self._lock_path + ".stale")
            except OSError:
                pass
            fd = os.open(self._lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        with os.fdopen(fd, "w") as f:
            f.write(str(os.getpid()))
        atexit.register(self._release_lock)

    def _release_lock(self):
        # read the pid with the handle closed before deleting: on Windows
        # os.remove on a file this process still has open raises
        # PermissionError (an OSError), which the old form swallowed silently
        try:
            with open(self._lock_path) as f:
                owner = (f.read() or "").strip()
        except OSError:
            return
        if owner and owner != str(os.getpid()):
            return
        try:
            os.remove(self._lock_path)
        except OSError:
            pass

    # -- control commands (called from the HTTP thread, executed by the loop) --
    def request_kill(self, reason: str = "dashboard"):
        """Ask the engine thread to halt and flatten. Never mutates broker
        state from the requestor's thread."""
        self._control = ("kill", reason)

    def request_resume(self):
        self._control = ("resume", "")

    def _run_control(self) -> bool:
        """Apply a queued control command. Returns True when one ran."""
        cmd, self._control = self._control, None
        if not cmd:
            return False
        kind, reason = cmd
        if kind == "kill":
            self.risk.halt(f"manual kill ({reason})")
            with self._broker_lock:
                self._close_all(f"manual kill ({reason})")
            if self.shadow_enabled:
                # an operator kill stops the whole experiment, twin included
                self.sh_risk.halt(f"manual kill ({reason})")
                self._shadow_flatten(f"manual kill ({reason})")
                self._shadow_save_risk()
            self.log.warn("RISK", "manual_kill", reason=reason)
        else:
            with self._broker_lock:
                self.risk.resume(self._total_equity())
            if self.shadow_enabled:
                self.sh_risk.resume(self._shadow_total())
            self._halt_announced = False
            self.log.info("RISK", "manual_resume", eq=round(self._total_equity(), 2))
        self._persist_risk()
        return True

    def _persist_risk(self):
        try:
            self.risk.save(os.path.join(self.runs, "risk.json"))
        except OSError as e:
            self.log.error("ERR", "risk_persist_failed", err=str(e)[:80])

    def _resolve_unconfirmed(self, pair: str, price: float):
        """An order whose outcome we could not read back: stop guessing. Re-sync
        from the exchange if possible, otherwise halt — the previous behavior
        booked a base=0/quote=0 trade and reported it as 'executed'."""
        b = self.brokers[pair]
        self.log.error("TRADE", "unconfirmed_fill", pair=pair)
        if hasattr(b, "reconcile") and b.reconcile(price):
            self.log.warn("TRADE", "reconciled", pair=pair,
                          cash=round(b.cash, 4), base=b.pos.base)
            return
        self.risk.halt(f"unconfirmed fill on {pair}: reconcile manually")
        self._persist_risk()
        self._push(pair, price,
                   {"decision": "HALT", "note": self.risk.halt_reason}, quiet=True)

    # ------------------------------------------------------------------
    def _last_candle_ts(self, pair: str) -> float:
        rows = self.candles.get(pair)
        return (rows[-1][0] if rows and rows[-1][0] else time.time())

    def _persist_pos(self, pair: str):
        """Position/trip basis snapshot: cost + scale latch + the open trip's
        teaching basis, so a restart resumes the trip instead of forgetting it."""
        b = self.brokers[pair]
        trip = self._trips.get(pair) or {}
        atomic_write_json({"cost": b.pos.cost,
                           "scaled_out": self.exitman[pair].scaled_out,
                           "trip_cash0": trip.get("cash0"),
                           "trip_entry_ts": trip.get("entry_ts")},
                          os.path.join(self.runs, f"{pair}.pos.json"))

    def _close_trip(self, pair: str, ts: float):
        """A trip closed (position back to flat): teach the brain from the
        trade's REALIZED outcome (net of fees) — the engine-side twin of the
        backtest _close_trip (P0). Forced closes (reflex/halt) teach too: a
        loss is a loss regardless of who pulled the trigger."""
        trip = self._trips.pop(pair, None)
        if trip is None:
            return
        self._persist_pos(pair)      # clear the persisted basis before teaching
        with self.state.lock:
            self.state.learning["trips_today"] += 1
        # lesson count persists like the dopamine scoreboard: a restart must
        # not shrink the day's teaching record
        try:
            today = time.strftime("%Y-%m-%d")
            d = self.ledger["days"].setdefault(today, {})
            d["trips"] = int(d.get("trips", 0)) + 1
            atomic_write_json(self.ledger, self.ledger_path)
        except Exception:
            pass
        brain = self.brains[pair]
        if not brain.cfg.trip_teaching:
            return
        try:
            label = brain.teach_trip(trip["entry_ts"], ts,
                                     self.brokers[pair].cash - trip["cash0"],
                                     fee_roundtrip=self._fee_roundtrip)
        except Exception as e:
            self.log.error("LIFE", "trip_teach_failed", pair=pair, err=str(e)[:80])
            return
        if label:
            # weights just changed: persist them out of band so a crash right
            # after a lesson cannot roll the brain back
            brain.save(os.path.join(self.runs, f"{pair}.brain.npz"))
            realized = round(self.brokers[pair].cash - trip["cash0"], 4)
            self.log.info("TRADE", "trip_dopamine", pair=pair, label=label,
                          realized=realized)
            with self.state.lock:
                ln = self.state.learning
                ln["da_total"][label] += 1
                ln["da_today"][label] += 1
                self.state.events.append({
                    "ts": time.time(), "pair": pair, "decision": "HOLD",
                    "dopamine": label,
                    "note": f"整笔教学 realized={realized:+.4f}"})
            # dopamine scoreboard persists in the daily ledger
            try:
                today = time.strftime("%Y-%m-%d")
                d = self.ledger["days"].setdefault(today, {})
                key = "da_reward" if label == "reward" else "da_punish"
                d[key] = d.get(key, 0) + 1
                atomic_write_json(self.ledger, self.ledger_path)
            except Exception:
                pass

    # ------------------------------------------------------------------
    def _reflex_check(self, pair: str, price: float | None = None, arm: str = "main"):
        """Giant-fiber reflex: crash exit bypasses the brain. Called every poll
        tick while a position is open; uses the live ticker price. arm="shadow"
        runs the identical reflex on the frozen twin's paper account."""
        main = arm == "main"
        brokers = self.brokers if main else self.sh_brokers
        pos_high = self.pos_high if main else self.sh_pos_high
        exitman = self.exitman if main else self.sh_exitman
        prev_close = self.prev_close if main else self.sh_prev_close
        looms = self.looms if main else self.sh_looms
        risk = self.risk if main else self.sh_risk
        b = brokers[pair]
        if b.pos.base <= 0:
            pos_high[pair] = 0.0
            if looms:
                looms[pair].reset()
            return None
        px = price
        if px is None:
            try:
                px = float(self.client.ticker(pair)["last"])
            except Exception:
                px = self.state.pairs[pair]["price"]
        if px <= 0:
            return None
        pos_high[pair] = max(pos_high[pair], px)
        trail = exitman[pair].trail_distance(self.candles.get(pair) or [])
        if looms is not None:
            why = looms[pair].step(px, side=1)
            if not why and pos_high[pair] > 0 \
                    and px / pos_high[pair] - 1 <= -trail:
                why = "trailing-stop"
        else:
            why = reflex_exit(prev_close[pair], px, pos_high[pair],
                              self.cfg.risk.flash_candle_drop, trail)
        if why:
            try:
                with self._broker_lock:
                    t = b.sell(px, reason=f"reflex:{why}")
                if t:
                    self.log.info("EXIT", "reflex", pair=pair, arm=arm,
                                  why=why, price=t.price)
                    pos_high[pair] = 0.0
                    risk.record_order(pair)
                    if main and b.pos.base <= 0:
                        self._close_trip(pair, self._last_candle_ts(pair))
                    return t
            except Exception as e:
                self.log.error("EXIT", "reflex_failed", pair=pair, err=str(e)[:80])
        return None

    # ------------------------------------------------------------------
    def _proactive_exit(self, pair: str, arm: str = "main"):
        """Sell-side upgrade: scale-out at +N% or trend-fatigue half exit."""
        main = arm == "main"
        brokers = self.brokers if main else self.sh_brokers
        exitman = self.exitman if main else self.sh_exitman
        risk = self.risk if main else self.sh_risk
        b = brokers[pair]
        if b.pos.base <= 0:
            exitman[pair].reset()
            return
        px = self.state.pairs[pair]["price"]
        if px <= 0:
            return
        avg = b.pos.cost / max(b.pos.base, 1e-9)
        tel = self.state.pairs[pair]["telemetry"] if main \
            else (self.sh_tel.get(pair) or {})
        r2 = tel.get("r2")
        reason = exitman[pair].check(px, avg, r2)
        if reason:
            try:
                with self._broker_lock:
                    t = b.sell(px, reason=reason, frac=0.5)
                if t:
                    exitman[pair].confirm(reason)   # latch only on success
                    self.log.info("EXIT", "proactive_half", pair=pair, arm=arm,
                                  reason=reason, price=t.price)
                    risk.record_order(pair)
                    if main and b.pos.base <= 0:
                        self._close_trip(pair, self._last_candle_ts(pair))
            except Exception as e:
                self.log.error("EXIT", "proactive_failed", pair=pair, err=str(e)[:80])

    # ------------------------------------------------------------------
    def preflight(self) -> bool:
        """Live mode sanity checks before any order (StonkFly-style preflight)."""
        if not self.live:
            print("[preflight] paper mode — nothing to check")
            return True
        try:
            bals_raw = self.client.balances()
        except Exception as e:
            print(f"[preflight] FAILED: cannot query balances: {e}")
            return False
        ok = True
        for p in self.cfg.pairs:
            base, quote = p.split("_")
            try:
                rules = self.client.pair_rules(p)
            except Exception as e:
                print(f"[preflight] FAILED: cannot read rules for {p}: {e}")
                ok = False
                continue
            if rules["trade_status"] != "tradable":
                print(f"[preflight] FAILED: {p} trade_status={rules['trade_status']}")
                ok = False
            qb = next((b for b in bals_raw if b["currency"] == quote), None)
            bb = next((b for b in bals_raw if b["currency"] == base), None)
            need = max(self.cfg.risk.order_notional,
                       rules["min_quote_amount"])
            print(f"[preflight] {p}: status={rules['trade_status']} "
                  f"amount_precision={rules['amount_precision']} "
                  f"min_quote={rules['min_quote_amount']} | "
                  f"{quote}={qb and qb['available']} {base}={bb and bb['available']}")
            if qb is None or float(qb["available"]) < need:
                print(f"[preflight] WARN: {p} needs >= {need} {quote} "
                      f"for the first order")
        print("[preflight] keys must have SPOT trade permission ONLY (no withdrawal).")
        return ok

    # ------------------------------------------------------------------
    def _fetch_candles(self, pair: str) -> list:
        """Latest closed candles as [[ts,o,h,l,c,v],...] floats. Every live
        candle is persisted to the local cache so the REAL trading record
        grows into the future backtest dataset."""
        step = INTERVALS[self.cfg.interval]
        now = time.time()
        to = int(now // step) * step - step          # last closed candle ts
        frm = to - step * (self.cfg.brain.window + 10)
        rows = self.client.klines(pair, self.cfg.interval, _from=frm, _to=to + step - 1)
        if rows:
            from ..data.history import append_cache
            append_cache(pair, self.cfg.interval, rows)
            rows = [[int(float(r[0]))] + [float(x) for x in r[1:6]] for r in rows]
        return rows

    def _total_equity(self) -> float:
        # strategy capital = spot pairs + futures runners (FUT_* cards)
        total = 0.0
        for p, v in self.state.pairs.items():
            if p.startswith("FUT_"):
                total += v.get("equity", 0.0)
            else:
                px = v.get("price") or 0.0
                total += self.brokers.get(p, None) and self.brokers[p].equity(px) or v.get("equity", 0.0)
        return total

    def _cards_ready(self) -> bool:
        """True once every capital card has pushed equity — or 5 min after
        start, so a futures thread that never reports can't leave the
        daily-loss halt disarmed for the whole day."""
        if self.cfg.futures.get("enable"):
            key = "FUT_" + self.cfg.futures.get("contract", "BTC_USDT")
            if not self.state.pairs.get(key, {}).get("equity"):
                return time.time() - self.state.started > 300
        return True

    def _push(self, pair: str, price: float, tel: dict, quiet: bool = False):
        s = self.state
        b = self.brokers[pair]
        with s.lock:
            s.version += 1
            ps = s.pairs[pair]
            ps.update({"price": price,
                       "candles": (self.candles.get(pair) or [])[-120:],
                       "cash": round(b.cash, 4),
                       "position_base": b.pos.base,
                       "position_notional": round(b.pos.base * price, 4),
                       "equity": round(b.equity(price), 4),
                       "telemetry": tel,
                       "trades": [t.as_dict() for t in b.trades[-20:]]})
            if not quiet:                     # HALT heartbeats don't spam the feed
                ev = {"ts": time.time(), "pair": pair, **tel}
                s.events.append(ev)
            total = self._total_equity()
            s.total_equity = round(total, 4)
            if s.peak_equity < total:
                s.peak_equity = round(total, 4)
            if self.risk.day_start_equity:
                s.day_pnl = round(total - self.risk.day_start_equity, 4)
            s.equity_curve.append([time.time(), round(total, 4)])
            s.equity_curve = s.equity_curve[-4000:]
            s.halted = self.risk.halted
            s.halt_reason = self.risk.halt_reason

    # ------------------------------------------------------------------
    def _observe(self, pair: str):
        """One observation for one pair on the newest closed candle."""
        rows = self._fetch_candles(pair)
        if not rows:
            return
        self.candles[pair] = rows
        last = rows[-1]
        price = last[4]
        broker = self.brokers[pair]
        eq = broker.equity(price)
        delta = 0.0 if self.last_equity[pair] is None else eq - self.last_equity[pair]
        self.last_equity[pair] = eq

        # the ablation/dopamine decisions need the account context: the same
        # brain must not behave differently on a 30 USDT card than on a 100 one
        self.brains[pair].holding_dir = 1 if broker.pos.base > 0 else 0
        tel = self.brains[pair].observe(rows, delta, frozen=False, equity=eq,
                                        fee_roundtrip=self._fee_roundtrip)
        tel["price"] = price
        decision = tel["decision"]

        # counterfactual capture: the DN fired but a MBON gate vetoed the
        # entry — park the event; the frozen twin observing this SAME candle
        # completes it with the decision the veto replaced
        if self.shadow_enabled and tel.get("block"):
            stale = self._cf_pending.pop(pair, None)
            if stale:                    # twin never observed that candle
                self._cf_append({**stale, "shadow_decision": None,
                                 "shadow_block": None})
            self._cf_pending[pair] = {
                "ts": int(last[0]), "pair": pair, "block": tel["block"],
                "side": "BUY" if tel["diff_hz"] > 0 else "SELL",
                "price": price, "diff_hz": tel["diff_hz"],
                "gate": tel["gate"], "marg": tel["marg"], "r2": tel["r2"],
                "dn_c_hz": tel["dn_c_hz"], "obs": tel["obs"]}

        # Jev (System One) gate — only consulted when the fly's DN already
        # fired (a few calls/day/pair, ~$0.13/month at 3k-token states);
        # veto-only: it can cancel an order, never invent a direction; any
        # failure degrades to the plain tilt decision (never blocks the loop)
        # ORDER: runs AFTER the MBON gate / CF capture — MBON gate → Jev gate
        # → execution; the veto does not touch the CF row above
        if self.jev is not None and decision in (BUY, SELL):
            state = build_state(tel)
            if validate_no_identifiers(state):
                # 防前视最后闸门：带标识的 state 拒绝外发（LLM 可能记得历史）
                self.log.warn("JEV", "deident_violation", pair=pair,
                              fields=str(validate_no_identifiers(state))[:120])
            else:
                ans = self.jev.ask_or_none(state, default_questions(), log=self.log)
                tel["jev"] = ans if ans is not None else {"error": "unavailable"}
                if ans:
                    probs = ans.get("direction", {}).get("probabilities", {})
                    p_long = float(probs.get("long", 0.0))
                    p_short = float(probs.get("short", 0.0))
                    min_p = self.cfg.brain.jev_min_prob
                    if decision == BUY and p_long < min_p:
                        decision = HOLD
                        tel["jev_veto"] = f"p_long={p_long:.2f}<{min_p}"
                    elif decision == SELL and p_short < min_p:
                        decision = HOLD
                        tel["jev_veto"] = f"p_short={p_short:.2f}<{min_p}"

        executed, note = None, ""
        if decision in (BUY, SELL):
            pos_notional = broker.position_notional(price)
            ok, why = self.risk.allow(pair, decision, pos_notional, price, self._total_equity())
            with self._broker_lock:
                if ok and decision == BUY and broker.cash >= self.cfg.risk.min_notional:
                    was_flat = broker.pos.base <= 0
                    cash0 = broker.cash
                    notional = entry_notional(
                        self.candles.get(pair) or [], self.cfg.risk,
                        self.cfg.risk.order_notional, INTERVALS[self.cfg.interval])
                    executed = broker.buy(notional, price,
                                          reason=f"fly DN diff {tel['diff_hz']}Hz g={tel['gate']}")
                    if executed and was_flat and pair not in self._trips:
                        # trip basis = cash BEFORE the entry (backtest parity);
                        # an ADD mid-trip must not reset basis or entry time
                        self._trips[pair] = {"cash0": cash0, "entry_ts": last[0]}
                elif ok and decision == SELL and pos_notional >= self.cfg.risk.min_notional:
                    executed = broker.sell(price, reason=f"fly DN diff {tel['diff_hz']}Hz")
                    if executed and broker.pos.base <= 0:
                        self._close_trip(pair, last[0])
            if executed:
                # only a real fill burns the daily quota / starts the cooldown:
                # a rejected minimum-size order used to consume both, so a pair
                # could exhaust its whole quota on six no-ops
                self.risk.record_order(pair)
            note = why if not ok else "executed" if executed else "skipped"
        tel["note"] = note
        if executed:
            tel["trade"] = executed.as_dict()
            self.log.info("TRADE", "fill", pair=pair, side=executed.side, price=executed.price,
                  quote=round(executed.quote, 3), fee=round(executed.fee, 4),
                  reason=reason_short(executed.reason))

        # persist brain & broker every observation (resumable, like StonkFly)
        self.brains[pair].save(os.path.join(self.runs, f"{pair}.brain.npz"))
        broker.save(os.path.join(self.runs, f"{pair}.broker.json"))
        self._persist_pos(pair)
        self._persist_risk()
        # daily ledger: one row per calendar day (live evidence accumulates)
        try:
            today = time.strftime("%Y-%m-%d")
            d = self.ledger["days"].setdefault(today, {})
            d["end_eq"] = round(self._total_equity(), 4)
            d["trades"] = sum(len(b.trades) for b in self.brokers.values())
            atomic_write_json(self.ledger, self.ledger_path)
        except Exception:
            pass
        if broker.needs_reconcile:
            self._resolve_unconfirmed(pair, price)
        self._push(pair, price, tel)
        # learning panel: portfolio-mean synaptic drift over time (the frozen
        # twin's curve is flat by construction — that IS the control)
        with self.state.lock:
            wa, wv, ba, bv, sat = self._w_stats()
            self.state.learning["w_curve"].append(
                [time.time(), round(wa, 6), round(wv, 6), round(ba, 6), round(bv, 6),
                 round(sat, 4)])
            del self.state.learning["w_curve"][:-4000]
            self.state.learning["sat_now"] = round(sat, 4)
        # 1/3 of the active edges pinned at the biological clip = the bounds,
        # not the data, are starting to decide the weights
        if sat >= 0.3 and not self._sat_warned:
            self._sat_warned = True
            self.log.warn("LIFE", "edges_saturated", frac=round(sat, 3))

    def _w_stats(self) -> tuple:
        """Portfolio-mean synaptic weights, their anatomical baselines, and
        the fraction of active rule edges pinned near the biological clip
        (the substrate's expression-ceiling gauge)."""
        wa = wv = ba = bv = sat = 0.0
        for b in self.brains.values():
            wa += float(b.w_a.mean())
            wv += float(b.w_v.mean())
            ba += float(getattr(b, "_base_a", b.w_a).mean())
            bv += float(getattr(b, "_base_v", b.w_v).mean())
            sat += b.edge_saturation()
        n = max(len(self.brains), 1)
        return wa / n, wv / n, ba / n, bv / n, sat / n

    def _cf_append(self, row: dict):
        """One counterfactual row into runs/counterfactual.jsonl. Best
        effort: telemetry must never break the trading loop."""
        try:
            with open(self._cf_path, "a") as f:
                f.write(json.dumps(row) + "\n")
        except OSError as e:
            self.log.warn("LIFE", "cf_append_failed", err=str(e)[:80])

    def _close_all(self, reason: str):
        with self._broker_lock:
            for p in self.cfg.pairs:
                b, px = self.brokers[p], self.state.pairs[p]["price"]
                if b.pos.base > 0 and px:
                    try:
                        t = b.sell(px, reason=f"risk halt: {reason}")
                        if t:
                            self.log.warn("RISK", "force_close", pair=p, price=t.price)
                            if b.pos.base <= 0:
                                self._close_trip(p, self._last_candle_ts(p))
                    except Exception as e:
                        self.log.error("RISK", "force_close_failed", pair=p, err=str(e)[:80])
                b.save(os.path.join(self.runs, f"{p}.broker.json"))
            self.risk.save(os.path.join(self.runs, "risk.json"))
        self.state.halted = True
        self.state.halt_reason = reason

    # ------------------------------------------------------------------
    def _halt_flatten(self):
        """While halted, keep trying to flatten anything left open. A halt
        raised inside risk.allow() used to skip _close_all entirely (the
        `not halted` guard saw the flag already set) and the halted branch
        never retried — leaving a live position behind the 'off' switch
        (2026-09-15 audit)."""
        if any(self.brokers[p].pos.base > 0 for p in self.cfg.pairs):
            self._close_all(self.risk.halt_reason)

    # ------------------------------------------------------------------
    # frozen shadow twin (P2 self-play). The twin answers exactly one
    # question with live data: "on this same stream, does teaching the fly
    # change its outcome for the better?" Its only allowed difference from
    # the learning fly is plasticity; everything else — wiring seed, gates,
    # order size, caps, cooldowns, reflex exits, scale-outs — is shared code.
    def _shadow_init(self):
        import dataclasses
        self.shadow_enabled = True
        self.sh_risk = RiskManager(self.cfg.risk,
                                   os.path.join(self.runs, "shadow.KILL"),
                                   budget_per_pair=self.cfg.quote_budget,
                                   backtest=False)
        self.sh_risk.anchor_ready = True    # spot-only total, no futures lag
        if self.sh_risk.load(os.path.join(self.runs, "shadow.risk.json")) \
                and self.sh_risk.halted:
            self.log.warn("RISK", "shadow_starts_halted", reason=self.sh_risk.halt_reason)
        if os.path.exists(os.path.join(self.runs, "shadow.KILL")) \
                and not self.sh_risk.halted:
            self.sh_risk.halted = True
            try:
                with open(os.path.join(self.runs, "shadow.KILL")) as f:
                    self.sh_risk.halt_reason = f.readline().strip() or "kill switch"
            except OSError:
                self.sh_risk.halt_reason = "kill switch (previous run)"
        # identical wiring (same seed / same real substrate); eta=0 + no trip
        # teaching + observe(frozen=True) pins every weight at its baseline —
        # exactly the protocol's `frozen` arm semantics
        sh_cfg = dataclasses.replace(self.cfg.brain, eta=0.0, eta_rule=0.0,
                                     trip_teaching=False, replay_passes=0)
        for p in self.cfg.pairs:
            budget = self.cfg.budgets.get(p, self.cfg.quote_budget)
            self.sh_brokers[p] = PaperBroker(p, budget, self.cfg.risk)
            self.sh_brokers[p].load(os.path.join(self.runs, f"shadow.{p}.broker.json"))
            brain = FlyBrain(sh_cfg, tag=p + "|shadow")
            try:
                brain.load(os.path.join(self.runs, f"shadow.{p}.brain.npz"))
            except Exception as e:
                self.log.warn("LIFE", "shadow_brain_unreadable", pair=p, err=str(e)[:80])
            self.sh_brains[p] = brain
            self.sh_exitman[p] = ExitManager(self.cfg.risk)
            self.sh_pos_high[p] = 0.0
            self.sh_prev_close[p] = 0.0
            self.sh_last_eq[p] = None
            self.sh_last_candle_ts[p] = 0
            d = load_json(os.path.join(self.runs, f"shadow.{p}.pos.json"), default=None)
            if isinstance(d, dict):
                self.sh_brokers[p].pos.cost = float(d.get("cost", 0.0))
                self.sh_exitman[p].scaled_out = bool(d.get("scaled_out", False))
        self.sh_looms = ({p: LoomingDetector() for p in self.cfg.pairs}
                         if getattr(self.cfg.risk, "loom_exit", False) else None)
        with self.state.lock:
            self.state.shadow["pairs"] = {p: {"decision": "--"}
                                          for p in self.cfg.pairs}
        self.log.info("LIFE", "shadow_twin_on", pairs=len(self.cfg.pairs))

    def _shadow_observe(self, pair: str):
        """One frozen-twin observation, on the same closed candle the learning
        fly just consumed (paired windows). Paper account only."""
        rows = self.candles.get(pair)
        if not rows:
            return
        price = rows[-1][4]
        b = self.sh_brokers[pair]
        brain = self.sh_brains[pair]
        eq = b.equity(price)
        delta = 0.0 if self.sh_last_eq[pair] is None else eq - self.sh_last_eq[pair]
        self.sh_last_eq[pair] = eq
        brain.holding_dir = 1 if b.pos.base > 0 else 0
        tel = brain.observe(rows, delta, frozen=True, equity=eq,
                            fee_roundtrip=self._fee_roundtrip)
        tel["price"] = price
        self.sh_tel[pair] = tel
        decision = tel["decision"]
        # complete the learning fly's veto event from this same candle: what
        # the frozen twin would have done instead (cf_report joins the rest)
        cf = self._cf_pending.pop(pair, None)
        if cf is not None:
            cf["shadow_decision"] = decision
            cf["shadow_block"] = tel.get("block")
            self._cf_append(cf)
        if decision in (BUY, SELL):
            ok, why = self.sh_risk.allow(pair, decision, b.position_notional(price),
                                         price, self._shadow_total())
            executed = None
            with self._broker_lock:
                if ok and decision == BUY and b.cash >= self.cfg.risk.min_notional:
                    # identical sizing function as the learner: the twin may
                    # differ in plasticity only
                    notional = entry_notional(
                        rows, self.cfg.risk, self.cfg.risk.order_notional,
                        INTERVALS[self.cfg.interval])
                    executed = b.buy(notional, price, reason="shadow fly")
                elif ok and decision == SELL \
                        and b.position_notional(price) >= self.cfg.risk.min_notional:
                    executed = b.sell(price, reason="shadow fly")
            if executed:
                self.sh_risk.record_order(pair)
                self.log.info("TRADE", "shadow_fill", pair=pair,
                              side=executed.side, price=executed.price)
            tel["note"] = "影子:" + (why if not ok else
                                    "executed" if executed else "skipped")
        else:
            tel["note"] = "影子"
        brain.save(os.path.join(self.runs, f"shadow.{pair}.brain.npz"))
        b.save(os.path.join(self.runs, f"shadow.{pair}.broker.json"))
        atomic_write_json({"cost": b.pos.cost,
                           "scaled_out": self.sh_exitman[pair].scaled_out},
                          os.path.join(self.runs, f"shadow.{pair}.pos.json"))
        with self.state.lock:
            self.state.shadow["pairs"][pair] = {"decision": decision,
                                                "price": price}
        self._shadow_push_total()

    def _shadow_total(self) -> float:
        total = 0.0
        for p in self.cfg.pairs:
            px = self.state.pairs[p]["price"]
            if px <= 0:
                rows = self.candles.get(p)
                px = rows[-1][4] if rows else 0.0
            total += self.sh_brokers[p].equity(px)
        return total

    def _spot_total(self) -> float:
        """Learning fly's SPOT-only equity — the A/B comparator. Must match
        the shadow's coverage exactly: _total_equity() also carries the
        futures card, which would poison the gap with a position the twin
        can never hold."""
        total = 0.0
        for p in self.cfg.pairs:
            px = self.state.pairs[p]["price"]
            if px <= 0:
                rows = self.candles.get(p)
                px = rows[-1][4] if rows else 0.0
            total += self.brokers[p].equity(px)
        return total

    def _shadow_push_total(self):
        # a price still loading after a restart values that position at 0,
        # which would poison the normalized race chart's baseline — skip the
        # sample until every card has a live price
        if any((self.state.pairs[p]["price"] or 0) <= 0 for p in self.cfg.pairs):
            return
        total = self._shadow_total()
        gap = round(self._spot_total() - total, 4)
        with self.state.lock:
            sh = self.state.shadow
            sh["total_equity"] = round(total, 4)
            sh["gap"] = gap
            if self.sh_risk.day_start_equity:
                sh["day_pnl"] = round(total - self.sh_risk.day_start_equity, 4)
            sh["equity_curve"].append([time.time(), round(total, 4)])
            del sh["equity_curve"][:-4000]
            self.state.learning["gap_curve"].append([time.time(), gap])
            del self.state.learning["gap_curve"][:-4000]
            # the learning fly's OWN spot equity — the race chart normalizes
            # this and the shadow curve to % growth from the same start
            self.state.learning["learner_curve"].append(
                [time.time(), round(self._spot_total(), 4)])
            del self.state.learning["learner_curve"][:-4000]

    def _shadow_flatten(self, reason: str):
        with self._broker_lock:
            for p in self.cfg.pairs:
                b = self.sh_brokers[p]
                px = self.state.pairs[p]["price"]
                if b.pos.base > 0 and px:
                    try:
                        b.sell(px, reason=f"shadow halt: {reason}")
                    except Exception as e:
                        self.log.error("RISK", "shadow_flatten_failed",
                                       pair=p, err=str(e)[:80])
                b.save(os.path.join(self.runs, f"shadow.{p}.broker.json"))

    def _shadow_save_risk(self):
        try:
            self.sh_risk.save(os.path.join(self.runs, "shadow.risk.json"))
        except OSError as e:
            self.log.error("ERR", "shadow_risk_persist_failed", err=str(e)[:80])

    # ------------------------------------------------------------------
    def run_forever(self):
        # hang watchdog: both loop threads heartbeat each iteration; a
        # stalled probe dumps all stacks then SIGKILLs us so pm2 relaunches
        # (freeze series 09-14..09-17 — trader/watchdog.py for the design)
        from .watchdog import HangGuard
        guard = HangGuard(self.runs, log=self.log)
        hb = guard.register("engine")
        guard.start()
        hb.beat()
        # optional dual-direction futures runner (own brain, own budget).
        # live=False forces the futures leg to paper regardless of
        # futures.paper — the CLI is the single source of truth for real money
        if self.cfg.futures.get("enable"):
            from .futures_runner import FuturesRunner
            fut = FuturesRunner(self.state, self.client, self.cfg, self.runs,
                                live=self.live, heartbeat=guard.register("futures"))
            fut.start()
        step = INTERVALS[self.cfg.interval]
        print(f"[engine] mode={self.state.mode} pairs={self.cfg.pairs} "
              f"interval={self.cfg.interval} dashboard={self.cfg.host}:{self.cfg.port}")
        while not self._stop.is_set():
            try:
                hb.beat()
                # control commands arrive from the HTTP thread; they are applied
                # here so all broker mutations happen on this one thread
                if self._run_control():
                    continue
                if self.risk.halted:
                    # announce HALT once, then keep prices flowing quietly
                    # (no event-feed spam every poll). The shadow twin pauses
                    # with the main fly so the A/B comparison windows stay
                    # paired — no observations on either side during a halt.
                    if not self._halt_announced:
                        self._halt_announced = True
                        for p in self.cfg.pairs:
                            try:
                                self.candles[p] = self._fetch_candles(p)
                                self._push(p, self.candles[p][-1][4],
                                           {"decision": "HALT", "note": self.risk.halt_reason})
                            except Exception:
                                pass
                        print(f"[engine] HALTED: {self.risk.halt_reason} "
                              f"(点击仪表盘 [恢复] 或删除 runs/KILL)", flush=True)
                    else:
                        for p in self.cfg.pairs:
                            try:
                                self.candles[p] = self._fetch_candles(p)
                                self._push(p, self.candles[p][-1][4],
                                           {"decision": "HALT", "note": self.risk.halt_reason},
                                           quiet=True)
                            except Exception:
                                pass
                    self._halt_flatten()
                    time.sleep(self.cfg.poll_s * 2)
                    continue

                now = time.time()
                for p in self.cfg.pairs:
                    try:
                        rows = self._fetch_candles(p)
                        if rows and rows[-1][0] != self.last_candle_ts[p]:
                            self.last_candle_ts[p] = rows[-1][0]
                            self.candles[p] = rows
                            self.prev_close[p] = rows[-1][4]
                            self._observe(p)
                            if self.shadow_enabled:
                                # the twin consumes the SAME closed candle the
                                # learning fly just did (paired A/B windows)
                                self.sh_prev_close[p] = rows[-1][4]
                                self._shadow_observe(p)
                        else:
                            self.candles[p] = rows or self.candles.get(p, [])
                            px = (rows or [[0, 0, 0, 0, 0]])[-1][4]
                            self._push(p, px, self.state.pairs[p]["telemetry"])
                        # exits: reactive reflex + proactive scale-out/fatigue
                        if self.brokers[p].pos.base > 0:
                            self._reflex_check(p)
                            self._proactive_exit(p)
                        if self.shadow_enabled and self.sh_brokers[p].pos.base > 0:
                            self._reflex_check(p, arm="shadow")
                            self._proactive_exit(p, arm="shadow")
                    except Exception as e:
                        self.log.error("ERR", "loop_error", pair=p, err=str(e)[:120])
                # global risk check on total equity
                total = self._total_equity()
                self.risk.anchor_ready = self._cards_ready()
                reason = self.risk.update(total)
                if reason:
                    # no `not halted` guard here: if the halt was raised inside
                    # risk.allow() during this same iteration, the close below
                    # is exactly what must still happen
                    self.risk.halt(reason)
                    with self._broker_lock:
                        self._close_all(reason)
                    self._persist_risk()
                    print(f"[engine] HALTED: {reason}")
                # shadow twin's own daily-loss/drawdown guards: an independent
                # account with an independent kill switch (shadow.KILL), so a
                # twin blow-up halts the twin, never the learning fly
                if self.shadow_enabled:
                    sh_reason = self.sh_risk.update(self._shadow_total())
                    if sh_reason and not self.sh_risk.halted:
                        self.sh_risk.halt(sh_reason)
                        self._shadow_flatten(sh_reason)
                        self._shadow_save_risk()
                        self.log.warn("RISK", "shadow_halt", reason=sh_reason)
                    self._shadow_push_total()
                # sleep until just after the next candle close
                now = time.time()
                nxt = (int(now // step) + 1) * step + 2
                # max(poll_s, min(x, poll_s)) is ALWAYS poll_s: the intended
                # candle alignment never took effect (2026-09-28)
                self._stop.wait(max(1.0, min(nxt - now, self.cfg.poll_s)))
            except KeyboardInterrupt:
                break
            except Exception as e:
                # keep the engine looping through the unexpected: an error
                # here used to end run_forever silently while the process
                # (dashboard + futures thread) stayed up half-alive
                self.log.error("ERR", "engine_loop", err=str(e)[:120])
                time.sleep(5)
        print("[engine] stopped")

    def stop(self):
        self._stop.set()
        self._release_lock()
