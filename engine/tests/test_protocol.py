"""Phase 0 measurement harness: clean ablation arms, honest benchmark,
multi-seed CIs, and the restart-safety of the risk/broker state.

These tests pin the properties that make the numbers in the README meaningful:
an ablation that differs by exactly one mechanism, fills that cannot use the
candle that produced the signal, and a benchmark at the strategy's own exposure.
"""
import os
import tempfile
import types
import unittest
from unittest import mock

import numpy as np

from flygate.backtest import run_backtest
from flygate.brain.flybrain import FlyBrain, BUY, SELL, HOLD
from flygate.config import BrainConfig, Config, RiskConfig
from flygate.eval.protocol import bootstrap_ci, paired_diff, run_protocol
from flygate.storage import atomic_write_json, load_json
from flygate.trader.broker import GateBroker, PaperBroker
from flygate.trader.engine import Engine
from flygate.trader.risk import RiskManager


def synth(n=200, drift=0.0, seed=1, start=100.0, vol=0.15):
    rng = np.random.default_rng(seed)
    px, out = start, []
    for i in range(n):
        o = px
        px = max(px * (1 + drift + rng.normal(0, vol / 100)), 1e-6)
        c = px
        hi = max(o, c) * (1 + abs(rng.normal(0, vol / 200)))
        lo = min(o, c) * (1 - abs(rng.normal(0, vol / 200)))
        out.append([1600000000 + i * 60, o, hi, lo, c, 10.0])
    return out


class TestAblationArms(unittest.TestCase):
    def test_frozen_arm_keeps_gates_but_stops_learning(self):
        """frozen=True must switch off plasticity ONLY.

        A punished brain (g well below the 0.45 gate) still sees BUY signals on
        a rising chart: the clean arm must hold them back exactly like the live
        brain, while the legacy arm — which also bypassed the gates — lets them
        through. If these two ever agree on every window, the ablation is
        measuring the gates and not the plasticity, and the README's
        fly-vs-frozen comparison is uninterpretable."""
        cds = synth(240, drift=0.004, vol=0.05)
        clean = FlyBrain(BrainConfig(), tag="clean")
        legacy = FlyBrain(BrainConfig(), tag="legacy")
        for b in (clean, legacy):
            for i in range(70, 150):
                b.observe(cds[:i], equity_delta=-0.5)      # constant punishment
        legacy.cfg = BrainConfig(frozen_keeps_gate=False)
        self.assertLess(clean.last_g, 0.45, "punishment should close the gate")
        self.assertLess(legacy.last_g, 0.45)

        blocked = passed = 0
        for i in range(150, 240):
            t_clean = clean.observe(cds[:i], 0.0, frozen=True)
            t_legacy = legacy.observe(cds[:i], 0.0, frozen=True)
            self.assertEqual(t_clean["gate"], t_legacy["gate"])
            if t_legacy["decision"] == BUY:
                passed += 1
                self.assertEqual(t_clean["decision"], HOLD,
                                 f"window {i}: clean arm ignored its own gate")
            if t_clean["decision"] == HOLD and t_legacy["decision"] == BUY:
                blocked += 1
        self.assertGreater(passed, 0, "no BUY signal to gate on; test data is flat")
        self.assertEqual(blocked, passed)

    def test_frozen_arm_matches_normal_without_dopamine(self):
        """With no outcome delivered, plasticity never fires, so the clean
        frozen arm and the live arm must decide identically — that is what
        makes `frozen` a control for `normal`."""
        cds = synth(200, drift=0.002, vol=0.05)
        a = FlyBrain(BrainConfig(), tag="a")
        b = FlyBrain(BrainConfig(), tag="b")
        for i in range(70, 200):
            self.assertEqual(a.observe(cds[:i], 0.0)["decision"],
                             b.observe(cds[:i], 0.0, frozen=True)["decision"])


class TestDopamineThreshold(unittest.TestCase):
    def test_threshold_clears_fees_and_scales_with_equity(self):
        cfg = BrainConfig(dopamine_deadband=0.01, dopamine_deadband_frac=0.002,
                          dopamine_fee_multiple=3.0)
        self.assertAlmostEqual(cfg.dopamine_threshold(equity=0.0, fee_roundtrip=0.0), 0.01)
        self.assertAlmostEqual(cfg.dopamine_threshold(equity=100.0, fee_roundtrip=0.04), 0.2)
        self.assertAlmostEqual(cfg.dopamine_threshold(equity=10.0, fee_roundtrip=0.04), 0.12)

    def test_fee_sized_delta_does_not_teach(self):
        """The bar is one round trip (10 USDT x 0.2% x 2 = 0.04): an outcome that
        does not cover the cost of the trade that produced it must not teach.
        Measured event rates on real 1m data: 0.0x fee -> 44-98 events/day of
        1-cent noise, 3.0x -> 0 events, 1.0x -> 0-5 events."""
        cds = synth(120, drift=0.001, vol=0.05)
        brain = FlyBrain(BrainConfig(), tag="fee")
        for i in range(70, 110):
            tel = brain.observe(cds[:i], equity_delta=0.03, fee_roundtrip=0.04)
        self.assertIsNone(tel["dopamine"], "a sub-fee delta must not teach")
        tel = brain.observe(cds[:110], equity_delta=0.05, fee_roundtrip=0.04)
        self.assertEqual(tel["dopamine"], "reward")
        brain2 = FlyBrain(BrainConfig(), tag="nofee")
        for i in range(70, 110):
            tel2 = brain2.observe(cds[:i], equity_delta=0.03, fee_roundtrip=0.0)
        self.assertEqual(tel2["dopamine"], "reward")

    def test_memory_decay_follows_candle_spacing(self):
        """Forgetting is per-observation with the real spacing: a 5m series must
        forget three times as fast as a 1m one. The old code hardcoded 60 s."""
        cfg = BrainConfig(memory_tau_s=1800.0)
        out = []
        for dt in (60.0, 300.0):
            b = FlyBrain(cfg, tag=f"dt{dt}")
            b.w_a = np.full(b.w_a.shape, 0.5)
            b.w_v = np.full(b.w_v.shape, 0.5)
            b._dopamine_update(np.zeros(b.w_a.shape), 0.0, dt_s=dt)
            out.append(float(b.w_a.mean()))
        self.assertAlmostEqual(out[0], 0.5 * np.exp(-60 / 1800) + (1 - np.exp(-60 / 1800)), places=9)
        self.assertAlmostEqual(out[1], 0.5 * np.exp(-300 / 1800) + (1 - np.exp(-300 / 1800)), places=9)
        self.assertGreater(out[1], out[0])


class TestBacktestHonesty(unittest.TestCase):
    def test_no_fill_uses_the_signal_candle_close(self):
        """Entries AND exits must fill at a later candle's open. Exits used to
        sell at the close of the candle whose crash produced the signal, which
        is exactly the look-ahead the module docstring promises not to take."""
        rows = synth(160, drift=0.003, vol=0.03)
        crash = rows[:-1]
        i = 140
        px = rows[i - 1][4]
        crash[i] = [rows[i][0], px, px, px * 0.4, px * 0.4, 10.0]   # -60% candle
        res = run_backtest(Config(), "BTC_USDT", crash, mode="normal", verbose=False)
        slip = Config().risk.slippage
        allowed = set()
        for r in crash:
            allowed.add(round(r[1] * (1 + slip), 9))
            allowed.add(round(r[1] * (1 - slip), 9))
        allowed.add(round(crash[-1][4] * (1 - slip), 9))     # final settle
        self.assertGreater(len(res["trade_log"]), 0, "no trades to check")
        for t in res["trade_log"]:
            self.assertIn(round(t["price"], 9), allowed,
                          f"fill at {t['price']} is not a candle open ({t['reason']})")

    def test_benchmark_matches_exposure(self):
        """`buyhold` is one order and hold (order_notional / budget exposure),
        NOT a fully invested portfolio — while `buyhold_return_pct` (the metric
        the README quotes) is full-budget and fee-free. That mismatch is
        exactly why the exposure-matched column exists."""
        cfg = Config()
        rows = synth(220, drift=0.002, vol=0.05)
        hold = run_backtest(cfg, "BTC_USDT", rows, mode="buyhold", verbose=False)
        fly = run_backtest(cfg, "BTC_USDT", rows, mode="normal", verbose=False)
        invested = cfg.risk.order_notional / cfg.quote_budget
        self.assertGreater(hold["avg_exposure"], invested * 0.5)
        self.assertLess(hold["avg_exposure"], invested * 1.5)
        self.assertAlmostEqual(hold["time_in_market_pct"],
                               hold["avg_exposure"] * 100, places=1)
        # the matched benchmark cannot be more extreme than the full-budget one
        self.assertLessEqual(abs(fly["bh_matched_return_pct"]),
                             abs(fly["buyhold_return_pct"]) + 1e-9)
        # buyhold mode cannot beat the full-budget metric at 10% exposure
        self.assertLess(abs(hold["return_pct"]), abs(hold["buyhold_return_pct"]) + 1e-9)

    def test_risk_is_evaluated_every_candle(self):
        """A monotonic decline must halt in the backtest the way it halts live,
        instead of waiting for the brain to propose an order. The halted arm is
        a position holder, because a strategy that never trades cannot lose
        money and therefore cannot trip a loss halt."""
        rows = []
        for i in range(240):
            px = 100.0 * (0.995 ** i)
            rows.append([1600000000 + i * 60, px, px * 1.001, px * 0.999, px, 10.0])
        cfg = Config()
        cfg.risk.daily_loss_halt = 5.0
        res = run_backtest(cfg, "BTC_USDT", rows, mode="buyhold", verbose=False)
        self.assertTrue(res["halted"], "decline never triggered the daily-loss halt")
        self.assertIn("daily loss", res["halt_reason"])

    def test_seed_changes_the_wiring(self):
        rows = synth(200, drift=0.002, vol=0.05)
        a = run_backtest(Config(), "BTC_USDT", rows, mode="normal", verbose=False, seed=1)
        b = run_backtest(Config(), "BTC_USDT", rows, mode="normal", verbose=False, seed=2)
        self.assertEqual(a["seed"], 1)
        self.assertNotEqual(a["trade_log"], b["trade_log"])


class TestStatistics(unittest.TestCase):
    def test_bootstrap_ci(self):
        mean, lo, hi = bootstrap_ci([1.0, 2.0, 3.0, 4.0], seed=0)
        self.assertAlmostEqual(mean, 2.5)
        self.assertLessEqual(lo, mean)
        self.assertLessEqual(mean, hi)
        self.assertEqual(bootstrap_ci([7.0]), (7.0, 7.0, 7.0))
        self.assertTrue(np.isnan(bootstrap_ci([])[0]))

    def test_paired_diff_reports_zero_crossing(self):
        same = paired_diff([1.0, 2.0, 3.0], [1.0, 2.0, 3.0])
        self.assertAlmostEqual(same["mean"], 0.0)
        self.assertTrue(same["crosses_zero"])
        big = paired_diff([5.0, 6.0, 7.0], [-1.0, -2.0, -3.0])
        self.assertFalse(big["crosses_zero"])
        self.assertGreater(big["mean"], 0)

    def test_run_protocol_reports_intervals(self):
        rows = synth(260, drift=0.002, vol=0.08)
        rep = run_protocol(Config(), {"BTC_USDT": rows},
                           arms=("normal", "frozen", "rewired", "buyhold"),
                           seeds=(1, 2), out_dir=None, verbose=False)
        entry = rep["pairs"]["BTC_USDT"]
        for arm in ("normal", "frozen", "rewired", "buyhold"):
            self.assertEqual(entry[arm]["return_pct"]["n"], 2)
            lo = entry[arm]["return_pct"]["lo"]
            hi = entry[arm]["return_pct"]["hi"]
            self.assertLessEqual(lo, hi)
        for key in ("vs_clean_ablation", "wiring_matters", "vs_buyhold"):
            self.assertIn(key, entry)


class TestWiredArm(unittest.TestCase):
    """The bold arm: MBON -> descending neurons + action-gated credit. These
    tests pin the two properties the legacy brain could not have at all."""

    def test_mbon_wiring_carries_weight_into_the_direction(self):
        """Measured 2026-09-28 (rising chart, forgetting off, gain 100):
        dn_up = 87.5 / 92.5 / 97.5 Hz for KC->MBONa weight 0.5 / 1.0 / 2.0, and
        87.5 / 87.5 / 87.5 with the wiring off. Before this wiring the DN rates
        were bit-identical for every weight, so no learned change could ever
        change a decision — the arm is only worth running because of this."""
        cds = synth(200, drift=0.004, vol=0.05)

        def dn_up_for(wired: bool, wa: float) -> float:
            cfg = BrainConfig(mbon_to_dn=wired, action_gated_elig=wired,
                              memory_tau_s=1e9)        # isolate the wiring
            b = FlyBrain(cfg, tag=f"w{wa}")
            b.w_a = np.full(cfg.n_kc, wa)
            b.w_v = np.full(cfg.n_kc, 1.0)
            for i in range(70, 200):
                tel = b.observe(cds[:i], equity_delta=0.0)
            return tel["dn_up_hz"]

        self.assertLess(dn_up_for(True, 0.5), dn_up_for(True, 1.0))
        self.assertLess(dn_up_for(True, 1.0), dn_up_for(True, 2.0))
        self.assertEqual(dn_up_for(False, 0.5), dn_up_for(False, 2.0),
                         "wiring off must ignore the MBON weights entirely")

    def test_forgetting_erases_a_learned_weight_within_one_tau(self):
        """Passive forgetting pulls every weight back to 1.0 with a ~tau time
        constant: after one tau (60 observations at 1m) a forced 0.5 is 93%
        gone. This is the online loop's real capacity limit — measured, 130
        punishments move w_a by only 0.5% at the shipped defaults (eta 0.02,
        tau 1800 s), because the per-observation pull toward baseline (3.3%)
        is ~30x larger than a plasticity step (~0.1%)."""
        cfg = BrainConfig(memory_tau_s=1800.0)
        b = FlyBrain(cfg, tag="decay")
        pat = np.zeros(cfg.n_kc)
        pat[:20] = 1.0
        b.w_a = np.full(cfg.n_kc, 0.5)
        for _ in range(60):
            b._dopamine_update(pat, 0.0, dt_s=60.0)
        expected = 1.0 + (0.5 - 1.0) * np.exp(-60 * 60 / 1800.0)
        self.assertAlmostEqual(float(b.w_a.mean()), expected, places=4)

    def test_exits_are_never_gated_shut(self):
        """An avoidance-dominant brain must still SELL on a coherent downtrend.
        The 2026-09-15 failure was a screaming SELL held all night because one
        gate guarded both directions. The legacy rule still closes both."""
        cds = synth(240, drift=-0.004, vol=0.05)
        wired = BrainConfig(mbon_to_dn=True, action_gated_elig=True,
                            mbon_inhibition=True)
        legacy = BrainConfig(mbon_inhibition=True)
        got = {}
        for name, cfg in (("wired", wired), ("legacy", legacy)):
            b = FlyBrain(cfg, tag=name)
            for i in range(70, 150):
                b.observe(cds[:i], equity_delta=-0.5)
            b.w_a = np.full(b.w_a.shape, cfg.w_min)     # avoidance dominates
            b.w_v = np.full(b.w_v.shape, cfg.w_max)
            decisions = [b.observe(cds[:i], 0.0)["decision"] for i in range(150, 240)]
            got[name] = decisions
        self.assertIn(SELL, got["wired"], "the entry gate held the exit shut")
        self.assertNotIn(SELL, got["legacy"],
                         "legacy rule no longer reproduces the held-SELL failure")

    def test_credit_is_action_gated(self):
        """Reward after a BUY grows the buy trace's weight and leaves the sell
        weight alone — the mirror of the legacy rule, which grew the approach
        weight after *any* profitable trade."""
        cfg = BrainConfig(action_gated_elig=True, dopamine_deadband=0.01)
        pat = np.zeros(cfg.n_kc)
        pat[:10] = 1.0
        b = FlyBrain(cfg, tag="credit")
        b.elig_long = pat.copy()
        b.elig_short = np.zeros_like(pat)
        b._dopamine_update(b.elig_long, 0.5, dt_s=60.0, elig_avoid=b.elig_short)
        self.assertGreater(b.w_a[:10].mean(), 1.0)
        self.assertAlmostEqual(b.w_v[:10].mean(), 1.0)
        # mirror image: a reward on the SELL trace teaches avoidance
        c = FlyBrain(cfg, tag="credit2")
        c.elig_long = np.zeros_like(pat)
        c.elig_short = pat.copy()
        c._dopamine_update(c.elig_long, 0.5, dt_s=60.0, elig_avoid=c.elig_short)
        self.assertGreater(c.w_v[:10].mean(), 1.0)
        self.assertAlmostEqual(c.w_a[:10].mean(), 1.0)

    def test_eligibility_normalization_fixes_the_step_size(self):
        """Measured: unit-L1 eligibility makes one plasticity step 3.1x larger at
        the observed trace mass (0.32 -> 1.0) and, more importantly, makes the
        total step independent of how many Kenyon cells happen to be active —
        the raw rule scales with code sparsity, so a busier chart silently
        learns faster."""
        def step_for(n_active: int, normalize: bool) -> float:
            cfg = BrainConfig(action_gated_elig=True, elig_normalize=normalize)
            b = FlyBrain(cfg, tag=f"{n_active}-{normalize}")
            pat = np.zeros(cfg.n_kc)
            pat[:n_active] = 0.016                    # measured scale
            b.elig_long = pat.copy()
            b.elig_short = np.zeros_like(pat)
            before = b.w_a.copy()
            b._dopamine_update(b.elig_long, 0.5, dt_s=60.0, elig_avoid=b.elig_short)
            return float((b.w_a - before).sum())

        raw20, raw40 = step_for(20, False), step_for(40, False)
        l1_20, l1_40 = step_for(20, True), step_for(40, True)
        self.assertAlmostEqual(raw40, raw20 * 2, places=6)      # scales with code
        self.assertAlmostEqual(l1_40, l1_20, places=6)          # invariant
        self.assertGreater(l1_20, raw20 * 2, "normalization did nothing")

    def test_legacy_path_unchanged_without_the_flag(self):
        """The default configuration must be bit-identical to the previous
        behaviour: this is a branch, not a rewrite."""
        cds = synth(160, drift=0.002, vol=0.05)
        a = FlyBrain(BrainConfig(), tag="legacy")
        for i in range(70, 140):
            tel = a.observe(cds[:i], equity_delta=0.1)
        self.assertEqual(tel["dopamine"], "reward")
        self.assertGreater(tel["w_a_mean"], 1.0)


class TestStateSafety(unittest.TestCase):
    def test_atomic_write_and_corrupt_recovery(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "s.json")
            atomic_write_json({"a": 1}, p)
            self.assertEqual(load_json(p), {"a": 1})
            self.assertEqual(load_json(os.path.join(d, "missing.json"), default={}), {})
            with open(p, "w") as f:
                f.write('{"a": 1')                       # truncated by a crash
            self.assertIsNone(load_json(p))
            self.assertTrue(os.path.exists(p + ".corrupt"))
            self.assertFalse(os.path.exists(p))

    def test_broker_load_survives_truncated_state(self):
        risk = RiskConfig()
        b = PaperBroker("BTC_USDT", 100.0, risk)
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "b.json")
            b.buy(10.0, 100.0)
            b.save(p)
            b2 = PaperBroker("BTC_USDT", 100.0, risk)
            self.assertTrue(b2.load(p))
            self.assertAlmostEqual(b2.pos.base, b.pos.base)
            with open(p, "w") as f:
                f.write('{"cash":')
            b3 = PaperBroker("BTC_USDT", 100.0, risk)
            self.assertFalse(b3.load(p))
            self.assertEqual(b3.cash, 100.0)

    def test_risk_counters_survive_restart(self):
        cfg = RiskConfig(max_orders_per_day=6, cooldown_s=300)
        # the kill path must NOT exist: /dev/null always does, so the old
        # fixture tripped kill_triggered() and every allow() came back
        # "halted: kill switch" instead of exercising the counters
        with tempfile.TemporaryDirectory() as kd:
            kill = os.path.join(kd, "kill")
            r = RiskManager(cfg, kill_file=kill, budget_per_pair=100.0)
            r.update(100.0, now=1_000_000)
            r.record_order("BTC_USDT", now=1_000_000)
            with tempfile.TemporaryDirectory() as d:
                p = os.path.join(d, "risk.json")
                r.save(p)
                r2 = RiskManager(cfg, kill_file=kill, budget_per_pair=100.0)
                self.assertTrue(r2.load(p))
                self.assertEqual(r2.orders_today["BTC_USDT"], 1)
                self.assertAlmostEqual(r2.last_order_ts["BTC_USDT"], 1_000_000)
                self.assertIsNotNone(r2.day_start_equity)
                ok, why = r2.allow("BTC_USDT", "BUY", 0.0, 100.0, 100.0, now=1_000_100)
                self.assertFalse(ok)
                self.assertEqual(why, "cooldown")

    def test_unconfirmed_live_fill_is_not_booked(self):
        """An order we cannot read back must not be recorded as a fill: the old
        path booked base=0/quote=0, reported it as 'executed', and left cash and
        position wrong until a restart."""
        class Client:
            def pair_rules(self, pair):
                return {"amount_precision": 4, "precision": 2,
                        "min_quote_amount": 1.0, "min_base_amount": 1e-4,
                        "trade_status": "tradable"}

            def create_market_order(self, pair, side, amount):
                return {"id": "1", "status": "open"}      # never settles

            def order(self, pair, oid):
                return {"id": "1", "status": "open"}

        b = GateBroker("BTC_USDT", 100.0, RiskConfig(), Client())
        with mock.patch("time.sleep", lambda _s: None):
            t = b.buy(10.0, 100.0)
        self.assertIsNone(t)
        self.assertTrue(b.needs_reconcile)
        self.assertEqual(b.cash, 100.0)
        self.assertEqual(b.pos.base, 0.0)
        self.assertEqual(len(b.trades), 0)


class TestEngineLock(unittest.TestCase):
    def test_second_engine_is_refused_on_the_same_runs_dir(self):
        cfg = Config()
        cfg.pairs = []
        cfg.futures = {}
        client = types.SimpleNamespace(key="")
        with tempfile.TemporaryDirectory() as d:
            lock = os.path.join(d, "engine.lock")
            e1 = Engine(cfg, live=False, client=client, runs_dir=d)
            with self.assertRaises(RuntimeError):
                Engine(cfg, live=False, client=client, runs_dir=d)
            e1.stop()
            self.assertFalse(os.path.exists(lock))
            e2 = Engine(cfg, live=False, client=client, runs_dir=d)
            e2.stop()

    def test_foreign_lock_is_stolen_only_when_its_pid_is_dead(self):
        cfg = Config()
        cfg.pairs = []
        cfg.futures = {}
        client = types.SimpleNamespace(key="")
        with tempfile.TemporaryDirectory() as d:
            lock = os.path.join(d, "engine.lock")
            with open(lock, "w") as f:
                f.write("999999999")                 # pid that cannot exist
            e = Engine(cfg, live=False, client=client, runs_dir=d)
            e.stop()


if __name__ == "__main__":
    unittest.main()
