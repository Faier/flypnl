"""Post-selection statistics (eval/stats.py): PSR / DSR / PBO(CSCV) / MinTRL /
HAC — the layer that decides whether a 15-arm zoo's champion is skill or luck.

Pinned properties (deterministic — the module itself contains no RNG):
  - zero Sharpe is exactly a coin flip (PSR = 0.5), a real edge saturates to 1
  - DSR decreases as the trial count grows (selection is punished)
  - MinTRL is finite for an edge, infinite without one
  - a truly dominant arm drives PBO to ~0; a zoo of identical noise arms
    sits near 0.5 (selection = coin flip)
  - HAC matches OLS on white noise and widens under autocorrelation
"""
import math
import unittest

import numpy as np

from flygate.eval.stats import (arm_stats, cscv_pbo, dsr, expected_max_sharpe,
                                hac_t, min_trl, psr, sharpe)


def noise(n=2000, seed=7, sd=0.001):
    return np.random.default_rng(seed).normal(0.0, sd, n)


def exact_zero_sharpe(n=2000, seed=7, sd=0.001):
    """Sample mean subtracted away — SR is EXACTLY 0, so PSR is exactly 0.5."""
    r = noise(n, seed, sd)
    return r - r.mean()


class TestPSR(unittest.TestCase):
    def test_zero_sharpe_is_a_coin_flip(self):
        self.assertAlmostEqual(psr(exact_zero_sharpe()), 0.5, places=6)

    def test_real_edge_saturates(self):
        r = noise() + 0.3 * 0.001
        self.assertGreater(psr(r), 0.999)

    def test_flat_series_is_nan(self):
        self.assertTrue(math.isnan(psr(np.zeros(100))))
        self.assertTrue(math.isnan(psr(np.array([1.0]))))


class TestDSR(unittest.TestCase):
    def test_zero_trials_degenerates_to_plain_psr(self):
        r = noise()
        self.assertAlmostEqual(dsr(r, 1, 0.01), psr(r), places=9)

    def test_more_trials_deflate_more(self):
        r = noise(seed=3) + 0.02 * 0.001          # a modest-looking edge
        vals = [dsr(r, n, 0.05) for n in (2, 10, 50, 500)]
        self.assertEqual(vals, sorted(vals, reverse=True))
        self.assertLess(vals[-1], vals[0])

    def test_expected_max_sharpe_grows_with_trials(self):
        self.assertEqual(expected_max_sharpe(1, 0.1), 0.0)
        self.assertEqual(expected_max_sharpe(10, 0.0), 0.0)
        self.assertGreater(expected_max_sharpe(100, 0.1),
                           expected_max_sharpe(10, 0.1))


class TestMinTRL(unittest.TestCase):
    def test_edge_has_finite_horizon(self):
        v = min_trl(noise() + 0.1 * 0.001)
        self.assertTrue(math.isfinite(v))
        self.assertGreater(v, 1)

    def test_weaker_edge_needs_more_bars(self):
        strong = min_trl(noise(seed=3) + 0.3 * 0.001)
        weak = min_trl(noise(seed=3) + 0.05 * 0.001)
        self.assertGreater(weak, strong)

    def test_no_edge_is_infinite(self):
        self.assertEqual(min_trl(noise() - 0.01), float("inf"))
        # exact alternating series: sample mean is exactly 0.0 in FP
        self.assertEqual(min_trl(np.tile([-1.0, 1.0], 500)), float("inf"))


class TestPBO(unittest.TestCase):
    def test_dominant_arm_drives_pbo_to_zero(self):
        rng = np.random.default_rng(11)
        T, N = 1200, 4
        R = rng.normal(0, 0.001, (T, N))
        R[:, 0] += 5 * 0.001                       # arm 0 truly dominates
        self.assertLess(cscv_pbo(R, 6), 0.1)

    def test_identical_noise_zoo_sits_near_coin_flip(self):
        rng = np.random.default_rng(5)
        R = rng.normal(0, 0.001, (1200, 4))
        v = cscv_pbo(R, 6)
        self.assertGreaterEqual(v, 0.15)
        self.assertLessEqual(v, 0.85)

    def test_guards(self):
        self.assertTrue(math.isnan(cscv_pbo(np.zeros((100, 1)), 6)))
        self.assertTrue(math.isnan(cscv_pbo(np.zeros((10, 4)), 6)))
        self.assertTrue(math.isnan(cscv_pbo(np.zeros((100, 3)), 100)))


class TestHAC(unittest.TestCase):
    def test_white_noise_hac_matches_ols(self):
        d = noise(4000, seed=9, sd=0.002)
        h = hac_t(d)
        self.assertLess(abs(h["se_hac"] / h["se_ols"] - 1.0), 0.2)

    def test_autocorrelation_widens_the_band(self):
        rng = np.random.default_rng(13)
        e = rng.normal(0, 0.002, 4000)
        d = np.empty(4000)
        d[0] = e[0]
        for i in range(1, 4000):
            d[i] = 0.8 * d[i - 1] + e[i]
        h = hac_t(d)
        self.assertGreater(h["se_hac"] / h["se_ols"], 1.5)

    def test_t_stat_uses_hac_se(self):
        d = noise(500, seed=17) + 0.001
        h = hac_t(d)
        self.assertAlmostEqual(h["t"], h["mean"] / h["se_hac"], places=9)

    def test_tiny_series_is_nan(self):
        self.assertTrue(math.isnan(hac_t(np.array([1.0, 2.0]))["t"]))


class TestArmStats(unittest.TestCase):
    def test_summary_fields(self):
        s = arm_stats(noise(500) + 0.2 * 0.001)
        self.assertGreater(s["psr"], 0.9)
        self.assertTrue(math.isfinite(s["min_trl"]))
        self.assertEqual(s["n"], 500)


if __name__ == "__main__":
    unittest.main()
