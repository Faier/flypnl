"""Connectome-inspired circuit motifs, each independently toggleable via
BrainConfig / RiskConfig flags (default OFF — live behavior unchanged until
validated). Validated on real cached 1m data by experiments_motifs.py.

  RingAttractor    central-complex (ellipsoid-body style) heading memory:
                   units on a ring with local excitation + global inhibition
                   keep ONE stable activity bump; new evidence applies torque
                   to the bump instead of overwriting state, so one or two
                   noisy candles cannot flip the trend direction — the bump
                   must physically travel around the ring.
  LoomingDetector  LC4/LC6-style expansion detector: the fly's escape reflex
                   tracks how FAST a visual threat EXPANDS (dθ/dt), not its
                   size. Here threat = drawdown from the watermark; the
                   detector fires on the deepening RATE scaled by recent
                   noise — a slow -5% grind never panics, a 3-candle
                   accelerating dump fires early.
"""
from __future__ import annotations

import numpy as np


class RingAttractor:
    """N direction units on a ring. φ=0 means UP, φ=π means DOWN; heading =
    cos(bump angle) ∈ [-1, +1]; strength = bump concentration ∈ [0, 1]."""

    def __init__(self, n: int = 12, tau: float = 6.0, w_inh: float = 0.35):
        self.n = n
        self.tau = tau
        self.phi = np.linspace(0.0, 2.0 * np.pi, n, endpoint=False)
        # Mexican-hat coupling: cos(Δφ) - inhibition → excite within ~±70°,
        # suppress the opposite side (global-inhibition approximation)
        d = np.angle(np.exp(1j * (self.phi[:, None] - self.phi[None, :])))
        W = np.cos(d) - w_inh
        np.fill_diagonal(W, 0.0)
        self.W = np.clip(W, 0.0, None) / max(np.clip(W, 0.0, None).sum(), 1e-9) \
            - np.clip(W, None, 0.0) / max(-np.clip(W, None, 0.0).sum(), 1e-9)
        # seed a weak bump pointing flat (φ=π/2) — no directional prior
        self.a = np.full(n, 0.15)
        self.a[n // 4] += 0.5
        self.heading = 0.0
        self.strength = 0.0

    def step(self, evidence: float) -> tuple[float, float]:
        """evidence ∈ [-1, +1]: + = up-staircase pressure, - = down. Returns
        (heading, strength) after one integration step."""
        # external drive peaks at the UP unit for positive evidence, DOWN for
        # negative — torque on the bump, not a reset of state
        drive = np.zeros(self.n)
        if evidence > 0:
            drive[0] = evidence
        elif evidence < 0:
            drive[self.n // 2] = -evidence
        rec = self.W @ self.a
        self.a = self.a + (1.0 / self.tau) * (
            -self.a + np.clip(rec + 0.9 * drive, 0.0, None))
        s = self.a.sum()
        if s > 1e-9:
            self.a /= s
        z = (self.a * np.exp(1j * self.phi)).sum()
        self.heading = float(np.clip(np.cos(np.angle(z)) * abs(z), -1.0, 1.0))
        self.strength = float(abs(z))
        return self.heading, self.strength


class LoomingDetector:
    """Threat expansion detector. side=+1 for a LONG (threat = drop below the
    high watermark), -1 for a SHORT (threat = rise above the low watermark)."""

    def __init__(self, k: float = 2.8, noise_win: int = 40, floor: float = 0.004):
        self.k = k
        self.noise_win = noise_win
        self.floor = floor          # minimum threat depth before it can fire
        self.mark = None            # favorable watermark
        self.prev_tau = 0.0
        self.rates: list[float] = []
        self.last_score = 0.0

    def reset(self):
        self.mark = None
        self.prev_tau = 0.0
        self.rates = []
        self.last_score = 0.0

    def step(self, px: float, side: int = 1) -> str | None:
        """Advance one candle; returns 'loom' when the threat is expanding
        fast relative to recent noise (the escape call), else None."""
        if px <= 0:
            return None
        if self.mark is None:
            self.mark = px
        self.mark = max(self.mark, px) if side > 0 else min(self.mark, px)
        tau = max(0.0, (self.mark - px) / self.mark) if side > 0 \
            else max(0.0, (px - self.mark) / max(self.mark, 1e-9))
        rate = tau - self.prev_tau
        self.prev_tau = tau
        self.rates.append(rate)
        self.rates = self.rates[-self.noise_win:]
        noise = float(np.std(self.rates)) + 1e-6
        self.last_score = max(0.0, rate) / noise
        if self.last_score >= self.k and tau >= self.floor:
            self.prev_tau = tau      # consume the spike so it fires once
            return "loom"
        return None
