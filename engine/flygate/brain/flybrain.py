"""The fly brain: a cell-type-level reduction of the Drosophila visual ->
mushroom body -> descending-neuron pathway as one vectorized LIF network.

Real circuitry implemented (pathway logic is biological, counts are reduced so a
laptop CPU can run one brain per coin):

  retina      R1-R6 luminance, R8 color          (chart pixels — the fly sees ONLY pixels)
  lamina      LMC center-surround contrast       (spatial high-pass)
  medulla/lobula T4/T5 & tilt detectors         (orientation/tilt of the price
                                                 line — real lobula circuitry:
                                                 flies read target geometry,
                                                 e.g. during chase behavior)
  lobula      plate HS_up / HS_dn pooling cells
  mushroom    Kenyon cells -> MBON-approach /    (sparse random code of the chart;
  body        MBON-avoid, reward DAN (PAM-like)  dopamine-gated KC->MBON plasticity)
              and punishment DAN (PPL-like)
  descending  DN_up / DN_dn / commitment DN      (fixed readout, mirroring StonkFly's
                                                 DNp20 left/right diff + DNpe017 gate)

Learning is ONLINE ONLY (no offline training on history): per-observation equity
delta -> dopamine -> KC->MBON synaptic change, with an eligibility trace carried
across observations so pattern(t) is associated with outcome(t+1).

Honesty note (as StonkFly's docs also state): the reward/punishment -> DAN
assignment is an ENGINEERED convention, not validated fly physiology.

Structural note (first measured 2026-09-28 on 640 observations, BEFORE the
MBON->DN wiring landed): diff = rate(DN_up) - rate(DN_dn) is an affine
function of the two tilt scalars, and a 15-line tilt threshold reproduced the
decisions on 99.8% of observations. RE-MEASURED 2026-09-30 under the live
config (mbon_to_dn=True, mbon_dn_gain=100, BTC/ETH/SOL x 988 frozen 15m
observations, scripts/tilt_monopoly.py): R2(diff ~ tilt) = 0.986-0.988, the
MBON evidence term adds only +0.0001..0.0003 R2, and decisions agree with the
mbon_dn_gain=0 control on 100% of raw-fire observations. The monopoly still
stands: the mushroom body modulates *whether* a decision is taken, never
*which* one. Complementary probe (scripts/reach_scan.py, same date): NO
admissible weight setting — bounds included, search free — separates
pre-up-move from pre-down-move Kenyon patterns on held-out data beyond the
random-label control: the ceiling sits in the visual encoding, upstream of
the weights. Keep both measurements current when the wiring changes.
"""
from __future__ import annotations

import os

import numpy as np

from ..config import BrainConfig
from .circuits import RingAttractor
from .lsh import FlyPatternRadar

BUY, SELL, HOLD = "BUY", "SELL", "HOLD"


# ---------------------------------------------------------------------- vision
SS = 6   # vertical supersampling factor: sub-row price precision for the retina


def render_chart(candles: list, w: int, h: int, window: int):
    """[[ts,o,h,l,c,v],...] -> (luminance[h,w], color[h,w]).

    Mimics StonkFly's display adapter: the brain receives only pixels, never
    prices, indicators or balances. The image is vertically supersampled then
    box-filtered so a sub-row price move still changes pixel mass (anti-aliasing
    the fly retina would get for free from optical blur).
    color: +1 up-candle body, -1 down-candle body.
    """
    H = h * SS
    lum = np.zeros((H, w)); col = np.zeros((H, w))
    data = candles[-min(window, w):]           # never exceed the grid width
    if len(data) < 2:
        return lum[::SS], col[::SS]
    o = np.array([d[1] for d in data]); hi = np.array([d[2] for d in data])
    lo = np.array([d[3] for d in data]); c = np.array([d[4] for d in data])
    pmin, pmax = lo.min(), hi.max()
    span = (pmax - pmin) or 1e-9
    y_of = lambda p: (pmax - p) / span * (H - 1)
    pad = w - len(data)                        # right-align the newest candles
    y_close = [y_of(v) for v in c]
    thick = 2 * SS                        # line thickness ~2 rows after blur
    for i in range(len(data)):
        x = pad + i
        y1, y2 = sorted((y_of(hi[i]), y_of(lo[i])))
        lum[max(0, int(y1)):int(y2) + 1, x] = 0.10            # wick: faint
        yb1, yb2 = sorted((y_of(o[i]), y_of(c[i])))
        a, b = max(0, int(yb1)), int(yb2) + 1
        lum[a:b, x] = np.maximum(lum[a:b, x], 0.3)             # body: dim
        col[a:b, x] = 1.0 if c[i] >= o[i] else -1.0
        # bright close-price polyline (what the motion pathway locks onto)
        yn = y_close[i + 1] if i + 1 < len(data) else y_close[i]
        ya, yb = sorted((y_close[i], yn))
        ya, yb = max(0, int(ya) - thick // 2), int(yb) + thick // 2 + 1
        lum[ya:yb, x] = 1.0
    # optical blur: [1,2,1] along rows, then box-downsample back to h rows
    def _blur(a):
        p = np.pad(a, ((1, 1), (0, 0)), mode="edge")
        return (p[:-2] + 2 * p[1:-1] + p[2:]) / 4.0
    lum = _blur(lum)
    col = _blur(col)
    lum = lum.reshape(h, SS, w).mean(axis=1)
    col = col.reshape(h, SS, w).mean(axis=1)
    return lum, col


def _unit_l1(vec: np.ndarray) -> np.ndarray:
    """Rescale a non-negative trace to unit L1 mass (no-op when empty)."""
    a = np.asarray(vec, dtype=np.float64)
    s = float(a.sum())
    return a / s if s > 1e-12 else a


def center_surround(lum: np.ndarray) -> np.ndarray:
    """Lamina LMC: center - surround mean (contrast), zero-centered."""
    pad = np.pad(lum, 1, mode="edge")
    surround = sum(pad[1 + dy:1 + dy + lum.shape[0], 1 + dx:1 + dx + lum.shape[1]]
                   for dy in (-1, 0, 1) for dx in (-1, 0, 1)) / 9.0
    return lum - surround


def chart_tilt(lum: np.ndarray, tail: int | None = None):
    """Lobula-style tilt readout: least-squares slope + coherence of the line.

    A sliding autoscaled chart hides trend from frame-to-frame motion (the axis
    renormalization cancels it) but shows it as the line's ORIENTATION — and
    orientation-selective detection is real lobula circuitry (flies track the
    geometry of visual targets, e.g. during chase behavior).

    tail: only use the last N columns (small-field FD-cell-like readout, faster
    to lock onto a fresh move than the full wide-field window).

    Returns (slope_norm, r2, centroids[w]): slope_norm normalized so a
    corner-to-corner ramp is ±1 (negative = price rising); r2 is the fit
    coherence in [0,1] — a random-walk window slopes strongly by chance but
    with LOW coherence, so r2 separates real trends from noise.
    """
    h, w = lum.shape
    if tail is not None:
        lum = lum[:, -min(tail, w):]
        w = lum.shape[1]
    rows = np.arange(h, dtype=np.float64)
    cent = np.full(w, np.nan)
    for x in range(w):
        c = lum[:, x]
        m = c.sum()
        if m > 0.3:
            cent[x] = (rows * c).sum() / m
    ok = np.isfinite(cent)
    if ok.sum() < 8:
        return 0.0, 0.0, cent
    xs = np.where(ok)[0].astype(np.float64)
    ys = cent[ok]
    xb, yb = xs.mean(), ys.mean()
    denom = ((xs - xb) ** 2).sum()
    if denom < 1e-9:
        return 0.0, 0.0, cent
    sxy = ((xs - xb) * (ys - yb)).sum()
    syy = ((ys - yb) ** 2).sum()
    slope = sxy / denom
    r2 = 0.0 if syy < 1e-9 else max(0.0, min(1.0, sxy * sxy / (denom * syy)))
    return float(slope / ((h - 1) / (w - 1))), float(r2), cent


# ---------------------------------------------------------------------- brain
class FlyBrain:
    """One brain per trading pair. Call observe() once per closed candle."""

    def __init__(self, cfg: BrainConfig, tag: str = "brain"):
        # --- real-connectome wiring (Phase C, frozen substrate) -----------
        # swaps kc_mask (random PN->KC fan-in) for the REAL extrinsic->KC
        # contact matrix and w_a/w_v for the REAL KC->MBON07/11 edges. The
        # KC population becomes the real substrate (3,679 cells), so n_kc in
        # the active cfg is replaced before anything allocates from it.
        self.real_wired = bool(getattr(cfg, "real_wired", False))
        if self.real_wired:
            import dataclasses as _dc
            from .realwired import load_real_mb
            n_feat0 = ((cfg.grid_h // 5) * (cfg.grid_w // 8)) + 3 + 2 + 3
            try:
                self._real = load_real_mb(n_feat0)
                cfg = _dc.replace(cfg, n_kc=int(self._real["M"].shape[0]))
            except (FileNotFoundError, OSError) as e:
                print(f"[brain ] 提示: 未检测到 data/connectome/ 真实连接组文件，自动平滑切换至合成 800-KC 神经网络: {e}")
                self.real_wired = False
                cfg = _dc.replace(cfg, real_wired=False, rule="none")
        self.cfg = cfg
        self.tag = tag
        self._rng = np.random.default_rng(cfg.seed)

        # --- mushroom body wiring -----------------------------------------
        self.feat_contrast = (8, 5)             # block size (w, h) for LMC pooling
        bh = cfg.grid_h // self.feat_contrast[1]
        bw = cfg.grid_w // self.feat_contrast[0]
        self.n_feat = bh * bw + 3 + 2 + 3
        if self.real_wired:
            self.kc_mask = self._real["M"]      # real contacts, row-max normalized
            # real KC->MBON edges only: zero where the connectome has no edge.
            # Baseline efficacy = contact count / group max; plasticity (Phase
            # D) moves weights within 0.1x-2x of EACH edge's own baseline —
            # the same bounds family as Huang/Luo 2024 / StonkFly.
            self._base_a = self._real["w07"].copy()
            self._base_v = self._real["w11"].copy()
            self.w_a = self._base_a.copy()
            self.w_v = self._base_v.copy()
            self._edge_a = self.w_a > 0
            self._edge_v = self.w_v > 0
        else:
            if cfg.rule != "none":
                raise ValueError("brain.rule='huang2024' needs the real "
                                 "connectome substrate (real_wired=True)")
            self.kc_mask = np.zeros((cfg.n_kc, self.n_feat), dtype=np.float32)
            for i in range(cfg.n_kc):           # sparse random PN->KC fan-in
                idx = self._rng.choice(self.n_feat, size=min(cfg.kc_inputs, self.n_feat),
                                       replace=False)
                self.kc_mask[i, idx] = 1.0
            self.w_a = np.ones(cfg.n_kc)        # KC -> MBON-approach (baseline 1.0)
            self.w_v = np.ones(cfg.n_kc)        # KC -> MBON-avoid
        # --- P1: the published per-edge rule on the real substrate ----------
        self.rule = None
        self._pending_pulse = None
        if self.real_wired and cfg.rule == "huang2024":
            from .luohuang import EdgeRule
            eg = self._real["edge_group"]
            ec = self._real["edge_contacts"]
            gmax = np.array([ec[eg == 0].max(), ec[eg == 1].max()])
            self._edge_kc = self._real["edge_kc"].astype(np.int64)
            self._edge_group = eg
            self._edge_base = ec / gmax[eg]
            self._gain = self._real["gain"]
            dan_names = self._real["dan_names"]
            self._dan_pam = dan_names == "PAM11"
            self._dan_ppl = dan_names == "PPL101"
            self.rule = EdgeRule(len(self._edge_kc), len(self._gain))
        self.elig = np.zeros(cfg.n_kc)          # eligibility: pattern memory
        # action-split eligibility (see BrainConfig.action_gated_elig): reward
        # strengthens the trace of the action that actually earned it
        self.elig_long = np.zeros(cfg.n_kc)
        self.elig_short = np.zeros(cfg.n_kc)

        # --- LIF state: [KC..., MBON_a, MBON_v, DN_up, DN_dn, DN_c] --------
        self.n = cfg.n_kc + 5
        self._v_rest = float(cfg.v_reset)       # config-driven: was a hardcoded -52.0
        self.v = np.full(self.n, self._v_rest)
        self.syn = np.zeros(self.n)
        self.ref = np.zeros(self.n, dtype=np.int32)
        self._perm = None                    # rewired-mode column shuffle

        self.hs_up_s = 0.0
        self.hs_dn_s = 0.0
        self.r2_s = 0.0
        self.fd_up_s = 0.0
        self.fd_dn_s = 0.0
        self.r2f_s = 0.0
        self.obs_count = 0
        self.last_g = 0.5
        self.holding_dir = 0            # stamped by the runner: +1/-1/0
        self.gate_beta = 6.0
        self.kc_drive_scale = 14.0
        self.mb_w = 0.5                         # per-spike KC->MBON coupling
        # ---- optional connectome motifs (validated offline first) ----------
        self.ring = RingAttractor() if cfg.ring_attractor else None
        self.radar = FlyPatternRadar(window_size=cfg.window, seed=42)
        self.memory: list = []                  # (ts, kc_drive) for sleep replay

    # ------------------------------------------------------------------
    def _features(self, lmc: np.ndarray, color: np.ndarray, hs_up: float, hs_dn: float):
        cw, ch = self.feat_contrast
        H, W = lmc.shape
        h2, w2 = (H // ch) * ch, (W // cw) * cw     # crop to divisible region
        pooled = lmc[:h2, :w2].reshape(h2 // ch, ch, w2 // cw, cw).mean(axis=(1, 3)).ravel()
        motion = np.array([hs_up, hs_dn, min(hs_up + hs_dn, 1.0)])
        col_up = float((color > 0).mean()); col_dn = float((color < 0).mean())
        last_dir = [0.0, 0.0, 0.0]                  # [up, flat, down] one-hot
        if hs_up > hs_dn + 0.02:
            last_dir[0] = 1.0
        elif hs_dn > hs_up + 0.02:
            last_dir[2] = 1.0
        else:
            last_dir[1] = 1.0
        return np.concatenate([pooled, motion, [col_up, col_dn], last_dir])

    # ------------------------------------------------------------------
    def kc_drive_of(self, lmc: np.ndarray, color: np.ndarray,
                    hs_up: float, hs_dn: float) -> np.ndarray:
        """Sparse Kenyon-cell code of one observation (unit-max normalized).
        Exposed for offline tools (reachability scans) so the encoding they
        measure cannot drift away from the one the live brain decodes."""
        f = self._features(lmc, color, hs_up, hs_dn)
        x = self.kc_mask @ f
        thresh = np.quantile(x, 1.0 - self.cfg.kc_sparse_frac)
        kc_drive = np.clip(x - thresh, 0, None)
        mx = kc_drive.max()
        if mx > 1e-9:
            kc_drive = kc_drive / mx
        return kc_drive

    # ------------------------------------------------------------------
    def observe(self, candles: list, equity_delta: float,
                frozen: bool = False, equity: float = 0.0,
                fee_roundtrip: float = 0.0) -> dict:
        """Advance one observation: show the chart, apply dopamine for the equity
        delta since the previous observation, decode a decision.

        frozen=True is meant to be an ABLATION — it disables the plasticity and
        nothing else (see BrainConfig.frozen_keeps_gate). equity/fee_roundtrip
        only feed the dopamine threshold, which must clear the round-trip cost
        of a trade before it can teach anything.
        """
        cfg = self.cfg
        lum, color = render_chart(candles, cfg.grid_w, cfg.grid_h, cfg.window)
        if cfg.rewired:
            # control condition: a FIXED random column shuffle destroys the
            # line's orientation (tilt/r2 become noise with the same
            # marginals) while preserving pixel statistics and sparsity
            if self._perm is None:
                self._perm = self._rng.permutation(lum.shape[1])
            lum = lum[:, self._perm]
        lmc = center_surround(lum)

        # memory decay is per-observation: read the real spacing off the
        # candles instead of assuming 1m bars
        dt_s = cfg.obs_interval_s
        if len(candles) >= 2:
            step = float(candles[-1][0]) - float(candles[-2][0])
            if step > 0:
                dt_s = step
        # dual-scale lobula readout:
        #   wide-field HS cells (full window) — stable trend state
        #   small-field FD cells (short tail) — locks onto fresh moves fast
        slope_l, r2_l, _ = chart_tilt(lum)
        slope_s, r2_s_raw, _ = chart_tilt(lum, tail=cfg.fd_tail)
        # coherence gate: random-walk windows slope by chance but with low r2
        up_l = float(np.clip(-slope_l * 2.0, 0.0, 1.0)) * r2_l
        dn_l = float(np.clip(slope_l * 2.0, 0.0, 1.0)) * r2_l
        up_s = float(np.clip(-slope_s * 2.0, 0.0, 1.0)) * r2_s_raw
        dn_s = float(np.clip(slope_s * 2.0, 0.0, 1.0)) * r2_s_raw
        # temporal integration: EMA across observations (state estimation)
        self.hs_up_s = 0.55 * self.hs_up_s + 0.45 * up_l
        self.hs_dn_s = 0.55 * self.hs_dn_s + 0.45 * dn_l
        self.r2_s = 0.6 * self.r2_s + 0.4 * r2_l
        self.fd_up_s = 0.7 * self.fd_up_s + 0.3 * up_s     # FD: faster, shallower EMA
        self.fd_dn_s = 0.7 * self.fd_dn_s + 0.3 * dn_s
        self.r2f_s = 0.7 * self.r2f_s + 0.3 * r2_s_raw
        # combined drive: either scale seeing the staircase is enough (entries
        # fire early on the FD scale; the wide-field scale keeps them honest)
        hs_up = max(self.hs_up_s, self.fd_up_s)
        hs_dn = max(self.hs_dn_s, self.fd_dn_s)
        r2_gate_val = max(self.r2_s, self.r2f_s)

        # --- mushroom body: sparse Kenyon-cell code --------------------------
        kc_drive = self.kc_drive_of(lmc, color, hs_up, hs_dn)
        kc_active = int((kc_drive > 1e-9).sum())

        # --- dopamine & plasticity (online only) -----------------------------
        if frozen:
            dan = None
        elif self.rule is not None:
            # P1: the published per-edge rule replaces the eligibility update.
            # Dopamine becomes a queued DAN pulse delivered during this
            # window's neural time (10ms rate bins) to luohuang.advance.
            dan = self._dopamine_label(equity_delta, equity, fee_roundtrip)
            if dan:
                self._pending_pulse = (dan, self._dan_scale(
                    equity_delta, equity, fee_roundtrip))
        elif cfg.action_gated_elig:
            # reward strengthens the trace of the action that earned it
            dan = self._dopamine_update(self.elig_long, equity_delta, dt_s,
                                        equity, fee_roundtrip,
                                        elig_avoid=self.elig_short)
        else:
            dan = self._dopamine_update(self.elig, equity_delta, dt_s,
                                        equity, fee_roundtrip)

        # --- sleep-replay memory: remember the pattern shown now so a
        # completed trip (or tonight's replay) can pair it with the outcome --
        if (cfg.replay_passes > 0 or cfg.trip_teaching) and not frozen:
            self.memory.append((int(candles[-1][0]), kc_drive.astype(np.float32)))
            if len(self.memory) > 2600:
                self.memory = self.memory[-2600:]

        # gate from MBON evidence
        # real wiring: only subset of KCs projects to each MBON, and weights
        # are contact-derived, so evidence is the DRIVE WEIGHTED by edge mass
        # (normalized by the weight sum) — same [0,1] scale as the synthetic
        # mean-of-active-drives, comparable gates
        if self.real_wired:
            den_a = max(float(np.sum(self.w_a)), 1e-9)
            den_v = max(float(np.sum(self.w_v)), 1e-9)
        else:
            den_a = den_v = max(kc_active, 1)
        a_ev = float(np.dot(self.w_a, kc_drive) / den_a)
        v_ev = float(np.dot(self.w_v, kc_drive) / den_v)
        g = float(1.0 / (1.0 + np.exp(-self.gate_beta * (a_ev - v_ev))))
        self.last_g = g
        self.elig = 0.7 * self.elig + kc_drive                # carry this pattern

        # --- run the LIF network over the compressed window ------------------
        rates = self._run(kc_drive, hs_up, hs_dn, g, dan)
        r_up, r_dn, r_c = rates["dn_up"], rates["dn_dn"], rates["dn_c"]

        diff = r_up - r_dn
        # --- motif: ring-attractor heading gate (CX-style stable trend state;
        # one noisy candle can't flip the bump, it must travel the ring) -----
        ring_ok = True
        ring_h = ring_s = 0.0
        if self.ring is not None and not frozen:
            ring_h, ring_s = self.ring.step(
                float(np.clip((hs_up - hs_dn) * 2.0, -1.0, 1.0)))
            ring_ok = ((ring_h >= 0.5 if diff >= 0 else ring_h <= -0.5)
                       and ring_s >= 0.25)
        vetoed, eff_gate = False, cfg.r2_gate     # legacy-frozen branch skips these
        if frozen and not cfg.frozen_keeps_gate:
            # LEGACY frozen arm (2026-09-15 behavior): also bypassed the MBON
            # and coherence gates, so `frozen` vs `normal` differed by two
            # mechanisms and the difference could not be attributed to the
            # plasticity. Kept only for reproducing the old table.
            decision_ok = True
        else:
            # first entries keep the strict gate; ADDING to an already-aligned
            # position needs less re-proof (the runner stamps holding_dir)
            eff_gate = cfg.r2_gate
            if (cfg.add_r2_gate and cfg.add_r2_gate < cfg.r2_gate
                    and self.holding_dir
                    and self.holding_dir * (1 if diff > 0 else -1) > 0):
                eff_gate = cfg.add_r2_gate
            decision_ok = g > 0.45 and r2_gate_val >= eff_gate
            vetoed = False
            if cfg.mbon_inhibition:
                # motif: MBON mutual inhibition — acting requires a positive
                # MARGIN on the proposed side. The margin STRENGTH grows with
                # learned avoidance (w_v above w_a): a fresh brain with equal
                # synapses has no opinion yet and must not gate itself shut;
                # a punished brain demands clearer approach evidence.
                # veto_tol: near-symmetric synapses make marg ≈ ±1e-9, and
                # the strict sign test flipped on that dust (2026-09-15: a
                # screaming SELL held all night because marg = +ε). MBON
                # arbitration should veto only on CLEARLY contradictory
                # evidence, not on knife-edge zeros.
                # div == 0 means NO learned asymmetry (fresh, frozen, or
                # balanced synapses) — then there is nothing to arbitrate and
                # the veto is skipped entirely. Real-connectome weights make
                # marg jitter at ±0.01 from contact asymmetry alone
                # (measured 2026-09-28: marg p10/p90 = -0.013/+0.004 with div
                # pinned at 0); that is structure, not avoidance, and reading
                # it as a veto locked the real_wired arm at zero trades.
                marg = a_ev - v_ev
                div = float(np.clip((self.w_v.mean() - self.w_a.mean()) / 0.02, 0.0, 1.0))
                req = cfg.mbon_margin * div
                tol = 1e-3
                if div > 0.0:
                    veto_ok = (marg >= req - tol) if diff > 0 \
                        else (marg <= -req + tol)
                    decision_ok = decision_ok and veto_ok
                    vetoed = not veto_ok
        # An exit reduces risk. With action-gated credit the MBON gate applies
        # to ENTRIES only: a brain that learned avoidance must still be able to
        # sell (2026-09-15: a screaming SELL held all night because the gate
        # closed on it). Without the flag the legacy behaviour is preserved.
        exit_ok = True if (cfg.action_gated_elig and not frozen) else decision_ok
        pattern = self.radar.match(candles)
        lsh_ok = True
        if cfg.lsh_gating:
            p_key = pattern.get("key")
            p_sim = pattern.get("similarity", 0.0)
            if p_key in ("TOP_RESISTANCE", "BEAR_BREAKDOWN") and p_sim >= 0.30:
                lsh_ok = False

        decision, block = HOLD, None
        raw_up = diff >= cfg.dn_diff_hz and r_c >= 1.0
        raw_dn = diff <= -cfg.dn_diff_hz and r_c >= 1.0
        if (raw_up or raw_dn) and not ring_ok:
            block = "ring"          # the DN fired; the CX heading gate said no
        elif raw_up and decision_ok and lsh_ok:
            decision = BUY
        elif raw_dn and exit_ok:
            decision = SELL
        elif raw_up or raw_dn:
            # the DN fired but a gate vetoed it — name WHICH gate, so the
            # engine can price every veto against the frozen twin's outcome
            # (runs/counterfactual.jsonl; scripts/cf_report.py does the join)
            if g <= 0.45:
                block = "gate"
            elif r2_gate_val < eff_gate:
                block = "r2"
            elif vetoed:
                block = "marg"
            elif not lsh_ok:
                block = "lsh"       # Fly-LSH pattern radar vetoed the entry
            else:
                block = "gate"

        # action-stamped eligibility: the pattern on screen now is credited to
        # the action taken now, so the reward at t+1 strengthens the pathway
        # that earned it instead of a direction-blind global gain
        if cfg.action_gated_elig and not frozen:
            if decision == BUY:
                self.elig_long = 0.7 * self.elig_long + kc_drive
                self.elig_short *= 0.7
            elif decision == SELL:
                self.elig_short = 0.7 * self.elig_short + kc_drive
                self.elig_long *= 0.7
            else:
                self.elig_long *= 0.7
                self.elig_short *= 0.7

        self.obs_count += 1
        return {
            "decision": decision, "diff_hz": round(diff, 2),
            "dn_up_hz": round(r_up, 2), "dn_dn_hz": round(r_dn, 2),
            "dn_c_hz": round(r_c, 2), "hs_up": round(hs_up, 4), "hs_dn": round(hs_dn, 4),
            "gate": round(g, 4), "dopamine": dan, "kc_active": kc_active,
            "r2": round(r2_gate_val, 3),
            "fd_up": round(self.fd_up_s, 3), "fd_dn": round(self.fd_dn_s, 3),
            "w_a_mean": round(float(self.w_a.mean()), 4),
            "w_v_mean": round(float(self.w_v.mean()), 4),
            "obs": self.obs_count,
            "ring": round(ring_h, 3), "ring_s": round(ring_s, 3),
            "marg": round(a_ev - v_ev, 4),
            "block": block,
            "pattern": pattern,
        }

    # ------------------------------------------------------------------
    def _dopamine_label(self, equity_delta: float, equity: float = 0.0,
                        fee_roundtrip: float = 0.0):
        """Just the gate: does this outcome clear the deadband at all?
        Punishment must clear punish_deadband_mult x the threshold (1.0 =
        symmetric; > 1 punishes only CLEARLY bad outcomes — BrainConfig)."""
        thr = self.cfg.dopamine_threshold(equity, fee_roundtrip)
        if equity_delta >= thr:
            return "reward"
        if equity_delta <= -thr * self.cfg.punish_deadband_mult:
            return "punish"
        return None

    def _dan_scale(self, equity_delta: float, equity: float = 0.0,
                   fee_roundtrip: float = 0.0) -> float:
        """Outcome SIZE as a pulse-rate factor in [1, dan_mag_cap]: |delta|
        relative to the threshold it had to clear. The binary label discards
        the only asymmetry the frozen twin earns with (few huge trend rides
        vs many fee-sized scratches) — a +14U winner and a +0.05U scratch
        used to fire the same 80 Hz. 1.0 (binary semantics) unless
        dan_magnitude_coding is on."""
        if not self.cfg.dan_magnitude_coding:
            return 1.0
        thr = self.cfg.dopamine_threshold(equity, fee_roundtrip)
        if thr <= 1e-12:
            return 1.0
        return float(np.clip(abs(equity_delta) / thr, 1.0, self.cfg.dan_mag_cap))

    def _dopamine_update(self, elig: np.ndarray, equity_delta: float,
                         dt_s: float | None = None, equity: float = 0.0,
                         fee_roundtrip: float = 0.0,
                         elig_avoid: np.ndarray | None = None):
        """One plasticity step: outcome gates whether the remembered pattern's
        approach or avoid synapses grow (PAM/PPL1-style). Shared by the online
        loop and sleep replay.

        dt_s: seconds represented by one observation, used for the forgetting
        term. It defaults to the configured fallback and is read off the candle
        timestamps by observe(); the previous hardcoded 60 s silently forgot
        three times too slowly on a 1m->5m switch.

        elig_avoid: when given, the credit assignment is ACTION-GATED — reward
        grows the approach weight only for the pattern that was on screen when a
        BUY was taken, and the avoid weight only for the SELL trace, instead of
        raising the approach weight after *any* profitable trade (which trained
        the buy gate with money earned by selling).
        """
        cfg = self.cfg
        thr = cfg.dopamine_threshold(equity, fee_roundtrip)
        reward = equity_delta >= thr
        punish = equity_delta <= -thr * cfg.punish_deadband_mult
        if cfg.elig_normalize:
            # unit L1 mass: otherwise a plasticity step is proportional to how
            # much of the (sparse, normalized) Kenyon code happened to be active
            elig = _unit_l1(elig)
            if elig_avoid is not None:
                elig_avoid = _unit_l1(elig_avoid)
        if not (reward or punish):
            pass
        elif elig_avoid is None:
            if reward:      # profit followed the remembered pattern -> approach up
                self.w_a += cfg.eta * elig
                self.w_v -= 0.5 * cfg.eta * elig
            else:           # loss followed it -> avoidance up
                self.w_v += cfg.eta * elig
                self.w_a -= 0.5 * cfg.eta * elig
        else:
            a = np.asarray(elig, dtype=np.float64)
            v = np.asarray(elig_avoid, dtype=np.float64)
            if reward:
                self.w_a += cfg.eta * a        # the buy earned it
                self.w_v += cfg.eta * v        # the exit earned it
            else:
                self.w_a -= 0.5 * cfg.eta * a
                self.w_v -= 0.5 * cfg.eta * v
        if self.real_wired:
            # per-edge bounds RELATIVE to the edge's own contact-derived
            # baseline (0.1x-2x, Huang/Luo-2024/StonkFly family); non-edges
            # stay exactly 0 — the absolute [w_min, w_max] clip would
            # resurrect them (w_min) and the 1.0-baseline decay would inflate
            # weak edges until the wiring dissolves
            self.w_a = np.where(self._edge_a,
                                np.clip(self.w_a, 0.1 * self._base_a,
                                        cfg.w_max * self._base_a), 0.0)
            self.w_v = np.where(self._edge_v,
                                np.clip(self.w_v, 0.1 * self._base_v,
                                        cfg.w_max * self._base_v), 0.0)
        else:
            self.w_a = np.clip(self.w_a, cfg.w_min, cfg.w_max)
            self.w_v = np.clip(self.w_v, cfg.w_min, cfg.w_max)
        dt_s = cfg.obs_interval_s if dt_s is None else dt_s
        decay = np.exp(-dt_s / cfg.memory_tau_s)              # passive forgetting
        if self.real_wired:
            # forgetting decays toward each edge's OWN baseline, not toward
            # the synthetic all-ones baseline
            self.w_a = self.w_a * decay + (1 - decay) * self._base_a
            self.w_v = self.w_v * decay + (1 - decay) * self._base_v
        else:
            self.w_a = self.w_a * decay + (1 - decay)
            self.w_v = self.w_v * decay + (1 - decay)
        return "reward" if reward else ("punish" if punish else None)

    # ------------------------------------------------------------------
    def edge_saturation(self) -> float:
        """Fraction of ACTIVE rule edges pinned near the biological clip
        (|u| >= 0.8; the clips sit at +1.0/-0.9). Past ~1/3 the 0.1x-2x
        bounds — not the data — are deciding the weights: that is the
        substrate's expression ceiling showing. Synthetic (rule=None)
        brains have no per-edge state and return 0.0."""
        if self.rule is None:
            return 0.0
        u = self.rule.u
        n = int((u != 0).sum())
        if not n:
            return 0.0
        return float((np.abs(u[u != 0]) >= 0.8).sum()) / n

    # ------------------------------------------------------------------
    def teach_trip(self, t0: float, t1: float, realized: float,
                   equity: float = 0.0, fee_roundtrip: float = 0.0):
        """Dopamine at TRIP CLOSE: pair every chart pattern that was on
        screen during this completed trade with the trade's REALIZED outcome
        (net of fees). One finished trade = one meaningful dopamine event,
        instead of per-minute mark-to-market noise (measured 2026-09-28:
        the per-minute channel fires 3x in 3 days behind the fee threshold —
        starved). The outcome must still clear the fee deadband: a trade
        that did not cover its own cost teaches avoidance.

        Returns the dopamine label for accounting ("reward"/"punish"/None).
        """
        if self.rule is not None:
            # P1: the per-edge rule consumes DAN PULSES, not pattern pairings —
            # one completed trip = one pulse delivered during the next window
            label = self._dopamine_label(realized, equity, fee_roundtrip)
            if label:
                self._pending_pulse = (label, self._dan_scale(
                    realized, equity, fee_roundtrip))
            return label
        dan = None
        for ts, pat in self.memory:
            if not (t0 <= ts <= t1):
                continue
            pat = np.asarray(pat, dtype=np.float64)
            mx = pat.max()
            if mx <= 1e-9:
                continue
            dan = self._dopamine_update(pat / mx, realized, equity=equity,
                                        fee_roundtrip=fee_roundtrip) or dan
        return dan

    # ------------------------------------------------------------------
    def replay(self, intervals: list, passes: int = 1):
        """Sleep-style consolidation: re-present the chart patterns that were
        on screen during each completed trip, paired with the trip's REALIZED
        outcome — a far stronger signal than per-minute mark-to-market noise
        (the same consolidation flies perform during sleep DAN replays).
        intervals = [(t_from, t_to, realized_quote_pnl), ...]

        Each stored pattern is replayed on its own. Averaging them first (the
        previous behavior) turned a sparse Kenyon code into a dense blur, i.e.
        it trained the brain on a pattern that was never on screen."""
        if passes > 0 and intervals:
            for _ in range(int(passes)):
                for t0, t1, outcome in intervals:
                    vecs = [v for ts, v in self.memory if t0 <= ts <= t1]
                    for pat in vecs:
                        pat = np.asarray(pat, dtype=np.float64)
                        mx = pat.max()
                        if mx <= 1e-9:
                            continue
                        self._dopamine_update(pat / mx, outcome)
        self.memory = self.memory[-100:]     # the consolidated day is forgotten

    # ------------------------------------------------------------------
    def _run(self, kc_drive, hs_up, hs_dn, g, dan) -> dict:
        """Advance the LIF network for cfg.sim_ms of compressed neural time.
        state_carry: keep v/syn/ref between observations (memory across
        decisions instead of waking from rest every minute); between
        observations voltages relax halfway toward rest (60 s real gap)."""
        cfg = self.cfg
        steps = int(cfg.sim_ms / cfg.dt_ms)
        dt = cfg.dt_ms
        decay_m = np.exp(-dt / cfg.tau_m_ms)
        decay_s = np.exp(-dt / cfg.tau_syn_ms)
        ref_steps = max(1, int(cfg.refractory_ms / dt))
        delay = max(1, int(round(cfg.syn_delay_ms / dt)))
        sim_s = cfg.sim_ms / 1000.0
        n_kc = cfg.n_kc
        dan_steps = int(cfg.dopamine_window_ms / dt)

        if cfg.state_carry:
            v, syn, ref = self.v.copy(), self.syn.copy(), self.ref.copy()
        else:
            v = np.full(self.n, self._v_rest)
            syn = np.zeros(self.n)
            ref = np.zeros(self.n, dtype=np.int32)
        hist = [np.zeros(self.n, dtype=bool) for _ in range(delay + 1)]
        counts = np.zeros(self.n)

        ikc = kc_drive * self.kc_drive_scale
        i_dn_up = 14.0 * hs_up + 3.0          # tonic 3 keeps silence at rest
        i_dn_dn = 14.0 * hs_dn + 3.0
        i_dn_c = 12.0 * abs(hs_up - hs_dn) + 4.0
        mbon_to_dn = bool(cfg.mbon_to_dn)
        # P1: 10ms rate bins for the per-edge rule (max bin the rule accepts)
        rule_bins = None
        bin_steps = 0
        if self.rule is not None:
            bin_steps = max(1, int(round(10.0 / cfg.dt_ms)))
            rule_bins = [None] * max(1, -(-steps // bin_steps))

        for t in range(steps):
            d = hist[delay].astype(np.float32)          # delayed spikes
            d_kc = d[:n_kc]
            mb_kc = np.array([np.dot(self.w_a, d_kc),
                              np.dot(self.w_v, d_kc)]) * self.mb_w * 0.05
            mb_in = mb_kc.copy()
            if self.rule is None and dan is not None and t < dan_steps:   # DAN burst raises MBON excitability
                mb_in[0 if dan == "reward" else 1] += 40.0
            # MBON_a excites / MBON_v inhibits the commitment neuron. This term
            # is a PER-TICK value: the old `i_dn[2] += ...` accumulated inside
            # the loop with no reset, so a few MBON_a spikes drove the
            # commitment neuron into saturation for the whole 400 ms window and
            # the r_c >= 1 Hz check stopped meaning anything
            dn_c = i_dn_c + 10.0 * (d[n_kc] - d[n_kc + 1])
            dn_up, dn_dn = i_dn_up, i_dn_dn
            if mbon_to_dn:
                # graded MBON output (its synaptic drive), because these units
                # never reach threshold at rest in this reduction
                dn_up += cfg.mbon_dn_gain * mb_kc[0]
                dn_dn += cfg.mbon_dn_gain * mb_kc[1]
            i_ext = np.concatenate([ikc, mb_in,
                                    np.array([dn_up, dn_dn, dn_c])])

            syn = syn * decay_s + i_ext * (1 - decay_s)
            ref[ref > 0] -= 1
            v += (1 - decay_m) * ((self._v_rest + syn) - v)
            spk = (v >= cfg.v_thresh) & (ref <= 0)
            counts[spk] += 1
            if rule_bins is not None and spk.any():
                b = t // bin_steps
                seg = spk[:n_kc].astype(np.int8)
                rule_bins[b] = seg if rule_bins[b] is None else rule_bins[b] + seg
            v[spk] = self._v_rest
            ref[spk] = ref_steps
            hist.insert(0, spk)
            hist.pop()

        if cfg.state_carry:
            self.v = self._v_rest + (v - self._v_rest) * 0.5  # relax over the gap
            self.syn = syn.copy()
            self.ref = ref.copy()

        if self.rule is not None:
            # P1: deliver the queued DAN pulse through the per-edge rule, then
            # collapse effective edge efficacies back to per-KC evidence
            # weights (same max-normalization as the baseline collapse).
            # advance() runs EVERY window (traces must track neural time even
            # without a pulse — with dan_profile=0 the drive is exactly 0, so
            # only the traces decay); frozen never reaches here with a pulse.
            pulse, self._pending_pulse = self._pending_pulse, None
            # defensive unpack: the pulse used to be a bare label string
            label = pulse[0] if isinstance(pulse, tuple) else pulse
            scale = pulse[1] if isinstance(pulse, tuple) else 1.0
            bin_s = bin_steps * cfg.dt_ms / 1000.0
            dan_profile = np.zeros(len(self._gain))
            if label == "reward":
                dan_profile[self._dan_pam] = cfg.dan_pulse_hz * scale
            elif label == "punish":
                dan_profile[self._dan_ppl] = cfg.dan_pulse_hz * scale
            win_s = cfg.dopamine_window_ms / 1000.0
            for b, seg in enumerate(rule_bins):
                if seg is None:
                    kc_hz_e = np.zeros(len(self._edge_kc))
                else:
                    kc_hz_e = seg[self._edge_kc] / bin_s
                dp = dan_profile if (b + 0.5) * bin_s <= win_s \
                    else np.zeros_like(dan_profile)
                self.rule.advance(kc_hz_e, dp, self._gain, bin_s,
                                  cfg.eta_rule)
            eff = self._edge_base * self.rule.efficacy()
            for grp, attr in ((0, "w_a"), (1, "w_v")):
                m = self._edge_group == grp
                s = np.zeros(self.cfg.n_kc)
                np.add.at(s, self._edge_kc[m], eff[m])
                setattr(self, attr, s / (s.max() or 1.0))

        return {"dn_up": counts[n_kc + 2] / sim_s,
                "dn_dn": counts[n_kc + 3] / sim_s,
                "dn_c": counts[n_kc + 4] / sim_s,
                "mbon_a": counts[n_kc] / sim_s,
                "mbon_v": counts[n_kc + 1] / sim_s,
                "kc_mean": float(counts[:n_kc].mean()) / sim_s}

    # ------------------------------------------------------------------ state
    def save(self, path: str):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        # the EMA trend estimates are decision state, not just telemetry —
        # without them a restart re-converges from zero and the first minutes
        # of decisions don't continue the pre-restart brain (2026-09-15 audit)
        ema = np.array([self.hs_up_s, self.hs_dn_s, self.r2_s,
                        self.fd_up_s, self.fd_dn_s, self.r2f_s])
        extra = {}
        if self.rule is not None:
            extra = {"rule_u": self.rule.u, "rule_w": self.rule.w,
                     "rule_yk": self.rule.y_kc, "rule_yd": self.rule.y_dan}
        np.savez_compressed(path, w_a=self.w_a, w_v=self.w_v, elig=self.elig,
                            elig_long=self.elig_long, elig_short=self.elig_short,
                            obs=self.obs_count, last_g=self.last_g, ema=ema,
                            **extra)

    def load(self, path: str) -> bool:
        if not os.path.exists(path):
            return False
        z = np.load(path, allow_pickle=False)
        if z["w_a"].shape != self.w_a.shape or z["w_v"].shape != self.w_v.shape:
            return False       # wiring population changed (e.g. real_wired);
                               # a saved synthetic state must not load into it
        self.w_a = z["w_a"]; self.w_v = z["w_v"]; self.elig = z["elig"]
        self.obs_count = int(z["obs"]); self.last_g = float(z["last_g"])
        # files written before the 2026-09-28 cleanup still carry a "prev"
        # frame that nothing ever read; ignore it rather than failing
        for key in ("elig_long", "elig_short"):
            if key in z.files:
                setattr(self, key, z[key])
        if "ema" in z.files:
            (self.hs_up_s, self.hs_dn_s, self.r2_s,
             self.fd_up_s, self.fd_dn_s, self.r2f_s) = \
                [float(x) for x in z["ema"]]
        if self.rule is not None:
            for k, dst in (("rule_u", "u"), ("rule_w", "w"),
                           ("rule_yk", "y_kc"), ("rule_yd", "y_dan")):
                if k in z.files and z[k].shape == getattr(self.rule, dst).shape:
                    setattr(self.rule, dst, z[k])
        return True
