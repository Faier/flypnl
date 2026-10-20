"""Configuration: defaults here, overridable by config.json in the project root."""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field, asdict

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


@dataclass
class BrainConfig:
    grid_w: int = 48            # chart -> retina grid width (retinotopic columns)
    grid_h: int = 27            # grid height (rows)
    window: int = 48            # candles shown in the chart (keep == grid_w)
    dt_ms: float = 1.0          # LIF step
    sim_ms: float = 400.0       # compressed neural time advanced per observation
    tau_m_ms: float = 20.0      # membrane time constant
    tau_syn_ms: float = 5.0     # synaptic current decay
    v_reset: float = -52.0
    v_thresh: float = -45.0
    refractory_ms: float = 2.2
    syn_delay_ms: float = 1.8
    n_kc: int = 800             # Kenyon cells (real fly: ~2000/hemisphere)
    kc_inputs: int = 7          # sparse random PN->KC fan-in
    kc_sparse_frac: float = 0.08
    fd_tail: int = 14           # small-field (FD-cell) readout window in columns
    dn_diff_hz: float = 2.0     # decision threshold: rate(DN_up)-rate(DN_dn)
    r2_gate: float = 0.85       # trend-coherence gate for committed decisions
                                # (backtest-tuned on 6d of 1m data: whipsaw control)
    add_r2_gate: float = 0.0    # looser gate for ADDING to an existing aligned
                                # position (first entry keeps r2_gate; 0=disabled,
                                # enable via config.json only after A/B wins)
    dopamine_deadband: float = 0.01   # |equity delta| below this -> no dopamine (quote ccy)
    dopamine_deadband_frac: float = 0.0    # ... plus this fraction of equity (0 = off)
    dopamine_fee_multiple: float = 1.0     # ... and this many round-trip fees (0 = off).
                                   # Measured on 1 day of real 1m data (4 pairs):
                                   # 0.0 -> 44-98 dopamine events/day, all of it
                                   # 1-cent mark-to-market noise; 3.0 -> 0 events
                                   # (the channel dies); 1.0 -> 0-5 events. One
                                   # round trip is the only setting with a
                                   # defensible meaning: an outcome must at least
                                   # cover the cost of the trade that produced it
    dopamine_window_ms: float = 200.0 # reward/punishment pulse-train duration
    obs_interval_s: float = 60.0    # fallback seconds between observations when the
                                    # candles cannot tell us (memory decay uses this;
                                    # hardcoding 60 s silently mis-scales on 5m bars)
    eta: float = 0.02           # plasticity gain
    w_min: float = 0.1          # synaptic bounds (x baseline)
    w_max: float = 2.0
    memory_tau_s: float = 1800.0  # passive memory decay toward baseline
    state_carry: bool = False      # LIF voltages/synapses persist across observations
                                   # (fly-self-driving: 3/20 -> 17/20 from this alone)
    rewired: bool = False          # control mode: destroy the fly's orientation
                                   # geometry (fixed column shuffle), keep stats —
                                   # tests whether the visual structure is load-bearing
    frozen_keeps_gate: bool = True # frozen=True disables ONLY plasticity. Set False
                                   # for the legacy arm that also bypassed the MBON
                                   # and r2 gates (that arm differed from `normal`
                                   # by two mechanisms, so it could not attribute
                                   # the learning increment to learning)
    mbon_to_dn: bool = False       # MBON_a drives DN_up, MBON_v drives DN_dn.
                                   # Real MBON axons converge on descending neurons;
                                   # without this the KC/MBON path can only change
                                   # *whether* a decision is taken, never *which*,
                                   # so no amount of dopamine can learn a direction
    mbon_dn_gain: float = 100.0    # gain of the graded MBON drive onto the DNs.
                                   # Keyed off MBON *spikes* the wiring is inert:
                                   # the MBONs never reach threshold in this
                                   # reduction (measured: 0 spikes in 400 steps),
                                   # so the descending neurons read the MBON's
                                   # synaptic drive instead. Measured response at
                                   # no forgetting (rising chart, dn_up Hz for
                                   # KC->MBONa weight 0.5/1.0/2.0): 87.5/92.5/97.5
                                   # at gain 100, and 87.5/87.5/87.5 at gain <= 40
                                   # — i.e. the wiring only reaches the rate code
                                   # once the gain is this large
    action_gated_elig: bool = False # credit assignment carries the action: the
                                   # eligibility that reward strengthens is the one
                                   # of the action that earned it (buy-trace -> MBON_a,
                                   # sell-trace -> MBON_v), and MBON evidence gates
                                   # ENTRIES only — an exit is never gated shut
    elig_normalize: bool = False   # rescale the eligibility trace to unit L1 mass
                                   # before applying it. Measured: one punish step
                                   # is 3.1x larger (0.32 -> 1.0 trace mass) and,
                                   # more importantly, no longer varies with how
                                   # many Kenyon cells happen to be active. The
                                   # real capacity lever is still memory_tau_s:
                                   # forgetting pulls 3.3% per observation against
                                   # a plasticity step of ~0.1%
    seed: int = 7
    # ---- connectome circuit motifs (OFF by default; validated in
    # experiments_motifs.py before ever enabling live) ----
    ring_attractor: bool = False   # CX-style heading memory gates entries
    mbon_inhibition: bool = False  # MBON mutual inhibition: ambiguous = HOLD
    mbon_margin: float = 0.004     # required approach-vs-avoid evidence margin
    replay_passes: int = 0         # sleep-style offline dopamine replay per day
    lsh_gating: bool = False       # Fly-LSH pattern radar gate: veto entries on resistance/breakdown
    trip_teaching: bool = False    # dopamine at TRIP CLOSE from the trade's
                                   # REALIZED P&L (net of fees), pairing the
                                   # patterns that were on screen during the
                                   # trip — instead of per-minute mark-to-market
                                   # noise. Measured 2026-09-28: the per-minute
                                   # channel fires 3x/3 days behind the fee
                                   # threshold (starved); one completed trade
                                   # is one dopamine event with actual meaning.
    rule: str = "none"             # "huang2024": P1 — the Huang/Luo 2024
                                   # baseline-centered anti-Hebbian rate rule
                                   # (luohuang.py, ported from StonkFly MIT)
                                   # replaces the eligibility update, working
                                   # per-edge on the real KC->MBON substrate.
                                   # Requires real_wired=True.
    eta_rule: float = 0.0          # learning rate of the per-edge rule.
                                   # Calibrated on |u| magnitude ("one punished
                                   # trip ~ -10% efficacy on the traded
                                   # pattern's edges"), NOT on returns.
    dan_pulse_hz: float = 80.0     # declared DAN firing rate during a reward/
                                   # punish pulse (StonkFly injects a 200ms
                                   # current into the PAM/PPL cells; the rate
                                   # that produces is not measured)
    dan_magnitude_coding: bool = False  # scale the pulse rate by outcome SIZE:
                                   # |delta|/threshold, clipped to [1, dan_mag_cap].
                                   # The binary label throws away the only
                                   # asymmetry the frozen twin earns with (few
                                   # huge winners vs many tiny losers) — a +14U
                                   # ride and a +0.05U scratch taught the same
                                   # 80 Hz. Engineered convention (as is the
                                   # pulse itself), kept behind a flag.
    dan_mag_cap: float = 4.0       # upper clip of the magnitude factor
    punish_deadband_mult: float = 1.0  # punishment fires only past mult x the
                                   # reward threshold. 1.0 = symmetric (legacy).
                                   # Measured live 09-29..09-30: 14 punish vs 1
                                   # reward taught w_a down 25% and shut the
                                   # gate (3 roundtrips in 2 days vs the frozen
                                   # twin's 42) — a punishment-dominated
                                   # schedule converges to never trading. 2.0
                                   # punishes only outcomes that are CLEARLY
                                   # bad, not every fee-eating scratch.
    real_wired: bool = False       # MaleCNS real-connectome MB substrate
                                   # (realwired.py): real extrinsic->KC contact
                                   # matrix + real KC->MBON07/11 edges, KC
                                   # population becomes the 3,679 real substrate
                                   # cells. Used by the `real_wired` protocol
                                   # arm (plasticity frozen in Phase C).
    # ---- Jev (TypeSafe System One) readout gate — OFF by default ----
    # docs/UNIFIED.md: fires only when the fly's DN already triggered; cached
    # answers (runs/jev_cache.jsonl) keep backtests deterministic and replays
    # free; ask_or_none degrades to the plain tilt decision on any failure.
    jev_enabled: bool = False      # 总开关；False 时零调用零成本
    jev_model: str = "jev-1.13.0"  # 钉死版本 id，禁用 jev-latest（别名随发版漂移）
    jev_cache_path: str = "jev_cache.jsonl"  # 相对 runs/；回放可复现、重跑免费
    jev_base_url: str = "https://api.typesafe.ai"  # 可指向 lichen/adapter 本地对照
    jev_timeout_s: float = 8.0
    jev_min_prob: float = 0.55     # 方向概率低于此值 → 按 HOLD 处理（只砍单不改向）

    def dopamine_threshold(self, equity: float = 0.0,
                           fee_roundtrip: float = 0.0) -> float:
        """Smallest |equity delta| that counts as reward/punishment.

        A fixed absolute deadband is wrong twice over. It is denominated in
        quote units, so the same brain reacts differently on a 30 USDT card
        than on a 100 USDT one; and it sits *below* the round-trip taker fee of
        a small order (10 USDT x 0.2% x 2 = 0.04 fee against 0.01 deadband), so
        a break-even trade used to fire a punishment out of nothing but cost.
        The fee term keeps fees from teaching, the relative term keeps the
        brain proportional to the account.
        """
        return max(self.dopamine_deadband,
                   self.dopamine_deadband_frac * max(equity, 0.0),
                   self.dopamine_fee_multiple * max(fee_roundtrip, 0.0))


@dataclass
class RiskConfig:
    order_notional: float = 10.0     # quote units per order (like StonkFly's $10 cap)
    max_position_notional: float = 60.0
    max_orders_per_day: int = 6      # per pair (backtest-tuned anti-whipsaw); 0 = unlimited
    cooldown_s: int = 300            # min seconds between orders on one pair
    min_notional: float = 5.0        # exchange minimum order size (quote units)
    daily_loss_halt: float = 20.0    # halt+close all after this much daily loss
    peak_drawdown_halt_pct: float = 20.0
    taker_fee: float = 0.002         # Gate spot VIP0 approx; enable GT discount if you have it
    slippage: float = 0.0005
    flash_candle_drop: float = 0.02  # giant-fiber reflex: single-candle drop exit
    trailing_stop: float = 0.04      # fallback trail (adaptive trail clamps around it)
    # ---- proactive exits (sell-side upgrade) ----
    scale_out_gain: float = 0.05     # lock half the position at +5% unrealized
    fatigue_r2_high: float = 0.80    # coherence must have reached this...
    fatigue_r2_drop: float = 0.50    # ...then collapse below this -> half exit
    atr_trail_mult: float = 1.5      # adaptive trail = k x 60-candle range
    atr_trail_min: float = 0.03
    atr_trail_max: float = 0.10
    breakeven_trigger: float = 0.015  # ratchet arms once peak pnl reaches this...
    breakeven_buffer: float = 0.002   # ...then exit can never fall below entry +/- this
    profit_lock_gain: float = 0.03    # after +5% scale trigger: floor rises to entry +/- this
                                      # (makes the 1-contract scale-out no-op a lock instead)
    floor_ladder: list = field(default_factory=list)  # giveback ladder [(peak_th,
                                      # floor_lv), ...] — floor = highest tier whose peak
                                      # threshold has been reached (user proposal 09-16);
                                      # empty = classic trigger/buffer one-step ratchet
    position_caps: dict = field(default_factory=dict)   # per-pair override of max_position_notional
    loom_exit: bool = False        # LC4/LC6-style: exit on threat EXPANSION rate
                                    # (replaces the fixed flash-candle rule when on)
    # ---- volatility-scaled entry sizing (default off = legacy flat notional;
    # the flat 10U into PONS and BTC alike is a sizing bug, not a strategy) ----
    vol_target_sizing: bool = False  # scale each entry by target/realized vol
    target_daily_vol: float = 0.02   # 2% daily vol per position at scale 1.0
    vol_lookback: int = 60           # candles of closes behind the estimate
    vol_scale_min: float = 0.25      # clamp: a wild coin never bets under 1/4
    vol_scale_max: float = 2.0       # ...and a calm coin never bets over 2x


@dataclass
class Config:
    pairs: list = field(default_factory=lambda: ["BTC_USDT", "ETH_USDT", "SOL_USDT"])
    budgets: dict = field(default_factory=dict)    # per-pair budget override {"PAIR": 30}
    futures: dict = field(default_factory=dict)    # {"enable":true,"contract":"BTC_USDT","budget":20,"paper":true,"max_contracts":2,"quanto":0.0001}
    interval: str = "1m"             # Gate kline interval
    quote_budget: float = 100.0      # paper starting cash per pair (quote units)
    poll_s: int = 5                  # live polling cadence
    host: str = "127.0.0.1"
    port: int = 8787
    brain: BrainConfig = field(default_factory=BrainConfig)
    risk: RiskConfig = field(default_factory=RiskConfig)

    # ------------------------------------------------------------------
    @classmethod
    def load(cls, path: str | None = None) -> "Config":
        path = path or os.path.join(ROOT, "config.json")
        cfg = cls()
        if os.path.exists(path):
            with open(path) as f:
                raw = json.load(f)
            for k, v in raw.items():
                if k == "brain":
                    cfg.brain = BrainConfig(**{**asdict(cfg.brain), **v})
                elif k == "risk":
                    cfg.risk = RiskConfig(**{**asdict(cfg.risk), **v})
                elif k in ("budgets", "futures"):
                    setattr(cfg, k, v)
                else:
                    setattr(cfg, k, v)
        # env override for server binding
        cfg.host = os.environ.get("FLYGATE_HOST", cfg.host)
        cfg.port = int(os.environ.get("FLYGATE_PORT", str(cfg.port)))
        return cfg

    def save_example(self, path: str):
        with open(path, "w") as f:
            json.dump(asdict(self), f, indent=2, ensure_ascii=False)
