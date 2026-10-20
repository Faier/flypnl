# -*- coding: utf-8 -*-
"""Fly-LSH: Locality Sensitive Hashing inspired by the Drosophila olfactory circuit
(Dasgupta, Stevens, Navlakha, Science 2017: 'A neural algorithm for fundamental computing').

Biological architecture:
  Projection Neurons (PN, d dims) -> Kenyon Cells (KC, m dims, m >> d) -> APL global inhibition -> MBON
  1. Expansion: random sparse binary projection (each KC connects to ~10-18% of PNs).
  2. Winner-Take-All (WTA): strong local/global feedback inhibition suppresses all
     but the top k% (typically 5-10%) most active Kenyon cells.
  3. Sparse Binary Fingerprint: produces an ultra-sparse binary hash code that preserves
     neighborhood similarity faster and more accurately than traditional Gaussian random projection.
"""
from __future__ import annotations

import numpy as np


class FlyLSH:
    """Fruit fly olfactory Locality Sensitive Hasher."""

    def __init__(
        self,
        input_dim: int,
        kc_dim: int = 400,
        sampling_ratio: float = 0.15,
        sparse_frac: float = 0.08,
        seed: int = 42,
    ):
        self.input_dim = int(input_dim)
        self.kc_dim = int(kc_dim)
        self.sampling_ratio = float(sampling_ratio)
        self.sparse_frac = float(sparse_frac)
        self.k_active = max(1, int(round(self.kc_dim * self.sparse_frac)))

        rng = np.random.RandomState(seed)
        # Sparse binary projection matrix: each KC samples sampling_ratio fraction of inputs
        mask = rng.rand(self.kc_dim, self.input_dim) < self.sampling_ratio
        empty = mask.sum(axis=1) == 0
        if empty.any():
            for idx in np.where(empty)[0]:
                mask[idx, rng.randint(0, self.input_dim)] = True
        self.proj_matrix = mask.astype(np.float32)

    def hash(self, vec: np.ndarray) -> np.ndarray:
        """Compute sparse binary hash code of length kc_dim.
        Returns: 1D bool numpy array of length kc_dim with exactly k_active True bits."""
        x = np.asarray(vec, dtype=np.float32).ravel()
        if len(x) != self.input_dim:
            if len(x) < self.input_dim:
                x = np.pad(x, (0, self.input_dim - len(x)), mode="edge")
            else:
                indices = np.linspace(0, len(x) - 1, self.input_dim).astype(int)
                x = x[indices]

        # Antennal lobe / projection neuron normalization (mean-centered)
        x_norm = x - np.mean(x)
        std = np.std(x_norm)
        if std > 1e-7:
            x_norm /= std

        # Kenyon cell sparse projection
        kc_drive = self.proj_matrix @ x_norm

        # Winner-Take-All (WTA) inhibitory circuit
        out = np.zeros(self.kc_dim, dtype=bool)
        if self.k_active >= self.kc_dim:
            out[:] = True
        else:
            top_indices = np.argpartition(kc_drive, -self.k_active)[-self.k_active:]
            out[top_indices] = True
        return out

    @staticmethod
    def similarity(h1: np.ndarray, h2: np.ndarray) -> float:
        """Jaccard similarity between two binary hash vectors."""
        intersection = np.logical_and(h1, h2).sum()
        union = np.logical_or(h1, h2).sum()
        if union == 0:
            return 1.0
        return float(intersection / union)

    @staticmethod
    def hamming_similarity(h1: np.ndarray, h2: np.ndarray) -> float:
        """Normalized similarity based on overlapping active bits (Overlap / k_active)."""
        active1 = int(np.sum(h1))
        active2 = int(np.sum(h2))
        target_k = max(active1, active2, 1)
        intersection = int(np.logical_and(h1, h2).sum())
        return float(intersection / target_k)


class FlyPatternRadar:
    """Historical market pattern classifier and similarity radar using FlyLSH."""

    def __init__(self, window_size: int = 24, kc_dim: int = 400, seed: int = 42):
        self.window_size = window_size
        # Feature vector: price curve (window_size) + returns (window_size-1) + vols (window_size)
        self.input_dim = window_size * 3 - 1
        self.hasher = FlyLSH(input_dim=self.input_dim, kc_dim=kc_dim, seed=seed)
        self.archetypes = self._init_archetypes()

    def _extract_feature_vector(self, candles: list) -> np.ndarray:
        """Extract multi-scale visual/normalized feature vector from candles [[ts,o,h,l,c,v], ...]."""
        if not candles or len(candles) < 2:
            return np.zeros(self.input_dim, dtype=np.float32)

        data = candles[-self.window_size:]
        closes = np.array([float(c[4]) for c in data])
        highs = np.array([float(c[2]) for c in data])
        lows = np.array([float(c[3]) for c in data])
        vols = np.array([float(c[5]) if len(c) > 5 else 1.0 for c in data])

        # 1. Price trajectory normalized to [-1, 1]
        p_min, p_max = lows.min(), highs.max()
        span = (p_max - p_min) or 1e-7
        norm_price = (closes - p_min) / span * 2.0 - 1.0
        if len(norm_price) < self.window_size:
            norm_price = np.pad(norm_price, (self.window_size - len(norm_price), 0), mode="edge")

        # 2. Return profile
        if len(closes) > 1:
            rets = np.diff(closes) / np.maximum(closes[:-1], 1e-7)
        else:
            rets = np.zeros(1)
        ret_std = np.std(rets) or 1e-4
        rets_norm = np.clip(rets / ret_std, -3.0, 3.0) / 3.0
        target_ret_len = self.window_size - 1
        if len(rets_norm) < target_ret_len:
            rets_norm = np.pad(rets_norm, (target_ret_len - len(rets_norm), 0), mode="constant")
        elif len(rets_norm) > target_ret_len:
            rets_norm = rets_norm[-target_ret_len:]

        # 3. Volume distribution normalized
        v_mean = np.mean(vols) or 1.0
        norm_vols = np.clip(vols / v_mean, 0.0, 3.0) / 3.0
        if len(norm_vols) < self.window_size:
            norm_vols = np.pad(norm_vols, (self.window_size - len(norm_vols), 0), mode="constant")

        return np.concatenate([norm_price, rets_norm, norm_vols]).astype(np.float32)

    def _init_archetypes(self) -> list[dict]:
        """Synthesize archetypes from canonical candle sequences."""
        w = self.window_size
        archetypes = []

        # 1. 突破拉升 (Bullish Breakout)
        c1 = []
        for i in range(w):
            p = 100.0 + (i * 0.2 if i < w // 2 else (w // 2) * 0.2 + (i - w // 2) * 1.5)
            v = 50.0 if i < w // 2 else 200.0
            c1.append([1000 + i * 60, p - 0.2, p + 0.5, p - 0.4, p + 0.3, v])
        archetypes.append({
            "name": "突破拉升", "key": "BULL_BREAKOUT",
            "en": "Bullish Breakout", "win_rate": 0.68, "bias": "BUY",
            "candles": c1,
        })

        # 2. 破位急跌 (Bearish Breakdown)
        c2 = []
        for i in range(w):
            p = 100.0 - (i * 0.2 if i < w // 2 else (w // 2) * 0.2 + (i - w // 2) * 1.5)
            v = 50.0 if i < w // 2 else 220.0
            c2.append([1000 + i * 60, p + 0.2, p + 0.4, p - 0.5, p - 0.3, v])
        archetypes.append({
            "name": "破位急跌", "key": "BEAR_BREAKDOWN",
            "en": "Bearish Breakdown", "win_rate": 0.24, "bias": "SELL",
            "candles": c2,
        })

        # 3. 探底反弹 (Oversold Bottom Reversal)
        c3 = []
        for i in range(w):
            p = 100.0 - i * 1.0 if i < w // 2 else 100.0 - (w // 2) * 1.0 + (i - w // 2) * 1.2
            v = 80.0 if i != w // 2 else 250.0
            c3.append([1000 + i * 60, p - 0.3, p + 0.8, p - 0.8, p + 0.2, v])
        archetypes.append({
            "name": "探底企稳反弹", "key": "OVERSOLD_REVERSAL",
            "en": "Oversold Reversal", "win_rate": 0.64, "bias": "BUY",
            "candles": c3,
        })

        # 4. 冲高受阻 (Top Resistance / Divergence)
        c4 = []
        for i in range(w):
            p = 100.0 + i * 1.0 if i < w // 2 else 100.0 + (w // 2) * 1.0 - (i - w // 2) * 1.1
            v = 80.0 if i != w // 2 else 230.0
            c4.append([1000 + i * 60, p + 0.3, p + 0.9, p - 0.7, p - 0.2, v])
        archetypes.append({
            "name": "冲高受阻回落", "key": "TOP_RESISTANCE",
            "en": "Top Resistance Pullback", "win_rate": 0.32, "bias": "SELL",
            "candles": c4,
        })

        # 5. 窄幅整理 (Low Volatility Squeeze)
        c5 = []
        for i in range(w):
            p = 100.0 + np.sin(i / 2.0) * 0.3
            c5.append([1000 + i * 60, p - 0.1, p + 0.2, p - 0.2, p + 0.1, 40.0])
        archetypes.append({
            "name": "窄幅蓄势整理", "key": "SQUEEZE_RANGE",
            "en": "Low-Vol Squeeze", "win_rate": 0.50, "bias": "HOLD",
            "candles": c5,
        })

        # Precompute hashes
        for arch in archetypes:
            vec = self._extract_feature_vector(arch["candles"])
            arch["hash"] = self.hasher.hash(vec)
        return archetypes

    def match(self, candles: list) -> dict:
        """Fingerprint current candle window and match against fly brain pattern archetypes."""
        if not candles or len(candles) < 3:
            return {
                "regime": "等待数据", "regime_en": "Waiting for Data",
                "key": "WAIT", "similarity": 0.0, "win_rate": 0.50,
                "bias": "HOLD", "active_kcs": 0, "fingerprint_hex": "",
                "top_matches": [], "radar_scores": {},
            }

        vec = self._extract_feature_vector(candles)
        h = self.hasher.hash(vec)
        active_indices = np.where(h)[0].tolist()

        scored = []
        radar_scores = {}
        for arch in self.archetypes:
            sim = self.hasher.hamming_similarity(h, arch["hash"])
            jaccard = self.hasher.similarity(h, arch["hash"])
            # Blend similarity: Hamming overlap gives clean ratio, Jaccard adds conservative penalty
            score = round(float(0.7 * sim + 0.3 * jaccard), 3)
            scored.append({
                "name": arch["name"], "key": arch["key"], "en": arch["en"],
                "similarity": score,
                "hamming": round(float(sim), 3),
                "jaccard": round(float(jaccard), 3),
                "win_rate": arch["win_rate"], "bias": arch["bias"],
            })
            radar_scores[arch["key"]] = score

        scored.sort(key=lambda x: x["similarity"], reverse=True)
        best = scored[0]

        # Compact hex fingerprint representation for UI
        packed = np.packbits(h[:64]) if len(h) >= 64 else np.packbits(h)
        hex_fp = packed.tobytes().hex()

        return {
            "regime": best["name"],
            "regime_en": best["en"],
            "key": best["key"],
            "similarity": best["similarity"],
            "win_rate": best["win_rate"],
            "bias": best["bias"],
            "active_kcs": len(active_indices),
            "active_sample": active_indices[:16],
            "fingerprint_hex": hex_fp,
            "top_matches": scored[:3],
            "radar_scores": radar_scores,
        }
