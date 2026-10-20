"""Unit tests for Fly-LSH and FlyPatternRadar."""
from __future__ import annotations

import numpy as np
import pytest

from flygate.brain.lsh import FlyLSH, FlyPatternRadar


def test_fly_lsh_sparsity_and_determinism():
    dim = 64
    kc_dim = 400
    sparse_frac = 0.05
    hasher = FlyLSH(input_dim=dim, kc_dim=kc_dim, sparse_frac=sparse_frac, seed=123)

    v1 = np.random.randn(dim)
    h1 = hasher.hash(v1)

    # 1. Sparsity check: exactly top 5% (20 bits out of 400) should be True
    expected_active = int(round(kc_dim * sparse_frac))
    assert np.sum(h1) == expected_active

    # 2. Determinism check: same input yields exact same hash
    h1_again = hasher.hash(v1)
    assert np.array_equal(h1, h1_again)

    # 3. Locality preservation: a small perturbation of v1 has high similarity,
    # whereas an independent random vector has lower similarity
    v_near = v1 + np.random.randn(dim) * 0.05
    v_far = -v1 + np.random.randn(dim) * 0.5
    h_near = hasher.hash(v_near)
    h_far = hasher.hash(v_far)

    sim_near = hasher.hamming_similarity(h1, h_near)
    sim_far = hasher.hamming_similarity(h1, h_far)

    assert sim_near > sim_far


def test_fly_pattern_radar_match():
    radar = FlyPatternRadar(window_size=20, kc_dim=200, seed=42)

    # Synthesize an obvious uptrend
    candles_up = []
    base_price = 100.0
    for i in range(25):
        p = base_price + i * 2.0
        candles_up.append([1000 + i * 60, p - 0.5, p + 1.0, p - 1.0, p + 0.5, 100.0 + i * 5])

    res_up = radar.match(candles_up)
    assert "regime" in res_up
    assert "similarity" in res_up
    assert res_up["similarity"] > 0.0
    assert "active_kcs" in res_up
    assert len(res_up["top_matches"]) == 3
    # Breakout or oversold reversal should be top candidate
    assert res_up["key"] in ("BULL_BREAKOUT", "OVERSOLD_REVERSAL")

    # Synthesize an obvious downtrend
    candles_dn = []
    for i in range(25):
        p = base_price - i * 2.0
        candles_dn.append([1000 + i * 60, p + 0.5, p + 1.0, p - 1.0, p - 0.5, 100.0 + i * 5])

    res_dn = radar.match(candles_dn)
    assert res_dn["key"] in ("BEAR_BREAKDOWN", "TOP_RESISTANCE")
