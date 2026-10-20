"""Offline imitation pretraining — hindsight labels -> dopamine.

The brain's plasticity takes ONE scalar outcome per observation. Online, that
scalar is its own equity delta (real money tuition). Here we replay history
and feed it a TEACHER scalar computed with hindsight: for every candle, the
sign of the next-H-candle return (deadband for chop). The brain is rewarded
when its visual lean agrees with what actually happens next, punished when it
contradicts, and punished mildly for confident leans inside chop.

No gradients, no torch — this reuses the exact biological plasticity rule
(_dopamine_update), so the pretrained brain is drop-in compatible with the
live runner (same npz). Months of "experience" for the price of electricity.

Usage: python3 -m flygate train --tag FUT_ZEC_USDT --contract ZEC_USDT \
           [--days 7] [--epochs 3] [--horizon 30]
"""
from __future__ import annotations

import os
import time

import numpy as np

from .brain.flybrain import FlyBrain
from .config import Config
from .data.gate_client import GateClient

TEACHER_SCALE = 0.15      # reward magnitude (>> dopamine_deadband 0.003)
CHOP_PUNISH = 0.08        # milder: don't traumatize the brain out of chop


def hindsight_labels(rows: list, horizon: int, band: float) -> np.ndarray:
    """+1 if next-H close rises > band, -1 if falls, 0 inside the deadband."""
    closes = np.array([r[4] for r in rows], dtype=float)
    n = len(rows)
    labels = np.zeros(n)
    if n <= horizon:
        return labels
    fwd = closes[horizon:] / closes[:-horizon] - 1.0
    labels[:-horizon] = np.where(fwd > band, 1.0,
                                 np.where(fwd < -band, -1.0, 0.0))
    return labels


def fetch_fut_candles(client: GateClient, contract: str, days: float) -> list:
    """1m perp candles, newest chunk of `days` (Gate caps ~10000 points total)."""
    to = int(time.time() // 60) * 60 - 60
    frm = max(to - int(days * 1440) * 60, to - 9900 * 60)   # hard clamp: 10000 max
    rows = []
    while frm < to:
        seg_to = min(to, frm + 999 * 60)
        raw = client._request("GET", "/futures/usdt/candlesticks",
                              query={"contract": contract, "interval": "1m",
                                     "from": frm, "to": seg_to}) or []
        rows.extend({"t": r["t"], "o": float(r["o"]), "h": float(r["h"]),
                     "l": float(r["l"]), "c": float(r["c"])} for r in raw)
        frm = seg_to + 60
        time.sleep(0.2)
    # dedupe + sort + normalize to engine rows [[ts,o,h,l,c,vol]]
    seen, out = set(), []
    for r in sorted(rows, key=lambda x: x["t"]):
        if r["t"] in seen:
            continue
        seen.add(r["t"])
        out.append([int(r["t"]), r["o"], r["h"], r["l"], r["c"], 0.0])
    return out


def train(tag: str, candles: list, cfg: Config, epochs: int = 3,
          horizon: int = 30, band: float = 0.004, verbose: bool = True):
    """Imitation-train a fresh brain on `candles`. Returns (brain, history)."""
    labels = hindsight_labels(candles, horizon, band)
    brain = FlyBrain(cfg.brain, tag=tag)
    warm = cfg.brain.window
    history = []
    for ep in range(epochs):
        agree = tot = nreward = npunish = nchop = 0
        for i in range(warm, len(candles)):
            view = candles[:i + 1]
            label = labels[i]
            lean = brain.hs_up_s - brain.hs_dn_s      # visual lean BEFORE this obs
            if label == 0.0:
                if abs(lean) > 0.1:                  # only punish real confidence
                    delta = -CHOP_PUNISH * min(abs(lean), 1.0)
                    nchop += 1
                else:
                    delta = 0.0
            else:
                delta = TEACHER_SCALE * label * lean
            tel = brain.observe(view, delta)
            if delta >= cfg.brain.dopamine_deadband:
                nreward += 1
            elif delta <= -cfg.brain.dopamine_deadband:
                npunish += 1
            if label != 0.0:
                tot += 1
                agree += int(np.sign(lean) == np.sign(label))
        rec = {"epoch": ep + 1,
               "agreement_pct": round(100.0 * agree / max(tot, 1), 1),
               "reward_steps": nreward, "punish_steps": npunish,
               "chop_punish_steps": nchop,
               "w_a_mean": round(float(brain.w_a.mean()), 4),
               "w_v_mean": round(float(brain.w_v.mean()), 4),
               "gate_g": round(brain.last_g, 3), "obs": brain.obs_count}
        history.append(rec)
        if verbose:
            print(f"[train:{tag}] epoch {ep+1}/{epochs} "
                  f"lean-vs-future agreement {rec['agreement_pct']}% "
                  f"(reward {nreward} / punish {npunish} / chop {nchop}) "
                  f"w_a {rec['w_a_mean']} w_v {rec['w_v_mean']}")
    return brain, history


def cmd_train(cfg: Config, args) -> None:
    client = GateClient()
    print(f"[train] fetching {args.contract} 1m perp candles (~{args.days}d)...")
    candles = fetch_fut_candles(client, args.contract, args.days)
    print(f"[train] {len(candles)} candles, horizon {args.horizon}m, "
          f"band {args.band*100:.1f}%, epochs {args.epochs}")
    if len(candles) < cfg.brain.window + 500:
        raise SystemExit("not enough history for pretraining")
    brain, hist = train(args.tag, candles, cfg, epochs=args.epochs,
                        horizon=args.horizon, band=args.band)
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    out_dir = os.path.join(root, "runs", "train")
    os.makedirs(out_dir, exist_ok=True)
    out = os.path.join(out_dir, f"{args.tag}.pretrained.npz")
    brain.save(out)
    print(f"[train] saved -> {out}")
    print(f"[train] swap into live with: cp {out} runs/fut.brain.npz "
          f"(after stopping the engine)")
