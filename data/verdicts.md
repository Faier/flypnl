# Verdicts — every claim this project has ever measured

> The rule of this lab: no claim without a control arm, no edge without surviving
> post-selection statistics (DSR / PBO / MinTRL). Most experiments here failed.
> That is the point of publishing them.

## 1. Does the fly brain have directional edge? — **NO**

Multi-arm × multi-seed protocol, Gate real 1m data, 0.2% fees + 5bp slippage
(`engine/flygate/eval/protocol.py`, DSR/PBO/MinTRL included):

| Pair | normal | frozen | random | buy&hold (exposure-matched) |
|---|---|---|---|---|
| BTC | −0.478% | −0.478% | −0.403% | −0.275% |
| ETH | −0.497% | −0.497% | −0.401% | −0.242% |
| SOL | −0.484% | −0.484% | −0.415% | −0.415% |
| PONS | −0.257% | −0.257% | −0.392% | −0.239% |

- `normal` ≡ `frozen`: the dopamine learning channel did not fire even once in a day
  (fee-multiple threshold too high). Learning is not "not significant" — it is **inert**.
- Champion Sharpe ratios were all negative → **DSR ≈ 0.00–0.06, MinTRL = ∞**.
  No declarable edge in this window. Seed has zero effect on brain arms — direction
  is decided by a single tilt scalar.

## 2. Does dopamine learning help when it does fire? — **A ±0.3pp coin flip**

1× fee threshold (channel occasionally fires), 3 days × 4 pairs:

- `learn` ≡ `mbon_dn` on 3/4 pairs; SOL flipped sign when the threshold changed.
- One knob flip reversed the sign → the effect is noise-scale, not edge.

Dopamine threshold dose-response (events/day, 1-day real data):

| fee multiple | DA events/day | meaning |
|---|---|---|
| 0.0 | 44–98 | sub-cent tick noise is "teaching" the brain |
| 1.0 | 0–5 | the only meaningful gate: PnL must cover its own costs |
| 3.0 | 0 | channel dead |

## 3. Long-window learning consistency — **BROKEN**

112-day paired equity race (learning fly vs frozen twin): learning consistency
breaks on the long window. Verdict recorded in engine git history, commit `7f6f2d0`.

## 4. What actually passed: funding-carry graduation protocol

Same-exchange spot-long + perp-short (delta-neutral), real settlement history,
round-trip cost 0.41% (OKX/Bybit). Graduation = P1>15% **and** P2>15% **and**
> 2× BTC/ETH control (~+2.4%).

2026-10-20 run: **6/23 pass** (SOON +21.5%/+19.2%, PONS +31.4%/+22.4% net APR,
worst gated battle still positive). The protocol also **killed** the fat-looking
ones: XAU (spot rate +19%) → P2 −19.1%; SKHYNIX (+125% observed mean) → P2 −20.8%.

Entry gate (trailing 7 settlements ≥15% ann) on 2026-10-20: **closed on all 6**.

## 5. A-share ETF dual momentum, deep-bear behavior

60d-momentum Top2-else-cash rule, 12-ETF universe, paper 100k CNY: in a market
where 11/12 ETFs have negative 60d momentum, the rule outputs
**"hold only Nasdaq ETF at 50%, rest cash"** — and it has been net 0.00% over
28 trading days while the broad market fell 20-38%. Defense *is* the signal working.

## 6. Engineering post-mortem (the unglamorous killer)

Unbounded network calls + a global lock + an auto-restart bash loop =
**98 freezes in 4 days** → baseline-anchor bugs → 4 false circuit-breakers →
full shutdown. Fix order, validated three times: root-cause the freezes →
anchor guard + shadow-residue cleanup → re-anchor baseline → *then* revive.
