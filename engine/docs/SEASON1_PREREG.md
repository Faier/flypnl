# Season 1 — Pre-Registered Protocol

**《我给一只果蝇的脑子 $750 让它炒币》/ "I gave a fruit-fly brain $750 to trade crypto"**

Status: **PRE-REGISTERED before Season 1 day 0. Published so it cannot be
edited after the fact.** 本文件在赛季开始前公示；开赛后只允许追加（appendix），
不允许改动下面的判据。

---

## 0. Honest disclosure (read this first / 先读这段)

The upstream project's own evidence says a fly brain does **not** have a proven
trading edge: every backtest arm trails its matched-exposure benchmark, the
dopamine learning channel is inert-to-harmful, and the decision reduces to a
lobula tilt scalar (R² ≈ 0.994). **This experiment is a live test of that
claim, published including its failure. It is not investment advice, it is not
a signal service, and no outcome here should be copied by anyone.**
项目自身证据表明蝇脑没有可证明的交易优势；这是一场公开的"证伪实验"，不是
荐股、不是跟单信号。

## 1. Subjects / 被试

| Arm | What | Money | Notes |
|---|---|---|---|
| 6 × LEARNING FLY | one independent brain per pair: BTC, ETH, SOL, DOGE, XRP, PONS | **100 USDT paper each** | `python -m flygate paper --shadow` |
| 6 × SHADOW FLY (control) | same market, same risk rules, plasticity OFF | 100 USDT paper each | the A/B twin — the whole point |
| 1 × FUTURES FLY | ZEC_USDT perp, long-only, hard stop | **200 USDT paper** | `futures.paper = true` |
| 1 × REAL SATELLITE (optional) | one configured pair on the operator's own money | **≤ 50 USDT real** | hard caps in §3; runs with `FLYGATE_RUNS` in its own state dir |

The satellite is the only real money in Season 1. Everything else is paper on
real market data with real fee/slippage modelling (0.2% round trip).

## 2. Pre-registered endpoint / 预注册判据

**Primary endpoint:** the paired equity difference (learning fly − shadow fly)
per pair, and its 6-pair mean, measured at **day 30** from Season 1 day 0, on
the frozen configuration in §3 (no mid-season parameter changes, see §4).

- **CONFIRMED:** ≥ 4 of 6 pairs show a positive gap at day 30.
- **NOT CONFIRMED:** otherwise — including "positive but not in 4 pairs".
- Secondary (reported, never used to declare victory): days-in-market, trade
  count, dopamine events/day, max drawdown, fees paid.

**The kill rule / 杀死规则:** if at day 30 the endpoint is NOT CONFIRMED, the
strategy is retired on stream, the config is archived, and no "one more tweak"
extension is granted. A Season 2 would have to be a *new* pre-registration.

## 3. Frozen configuration / 冻结参数

Spot risk (per pair): `order_notional = 10`, `cooldown_s = 300`,
`daily_loss_halt = 21`, `peak_drawdown_halt_pct = 20`, position cap = budget.
Futures fly: `max_contracts = 12`, `hard_stop_pct = 0.035`, `no_flip = true`,
`interval = 5m`, `entry_mode = regime`.
Satellite: `order_notional = 5`, `max_orders_per_day = 2`, `cooldown_s = 900`,
`daily_loss_halt = 5`, `peak_drawdown_halt_pct = 10`, no futures.

All of these are visible in `config.json` / `config.satellite.json` at the
published commit hash of day 0.

## 4. Change policy / 违规定义

- **Allowed:** bug fixes that restore intended behaviour, and changes made
  *between seasons*. Every such commit must be reported in that day's post.
- **Violation:** any change to entry/exit logic, thresholds, or risk limits
  during Season 1. A violation day is published as a violation day and the
  day-30 endpoint is reported **with and without** those days.
- **Never allowed:** deleting a losing day, editing published ledger files
  (the hash chain in `ledger/` makes silent edits detectable), or restarting
  the clock to reset a drawdown.

## 5. Disclosure mechanics / 可信度机制

1. **Daily ledger.** `scripts/publish_ledger.py` writes one `ledger/<date>.json`
   per day containing every broker state, plus a SHA-256 chained to the
   previous day's digest (a tamper-evident append-only record). Committed to a
   public repository daily.
2. **Anyone can recompute.** The raw state files and this protocol are enough
   to reproduce every equity number; nothing is screenshot-only.
3. **Losses are published at the same cadence as wins.** A season with no
   losing posts will be treated as a stopped feed.
4. **No cherry-picking:** the aggregate table in `ledger/LEDGER.md` always
   carries all six pairs and both arms.

## 6. Fees & frictions / 成本口径

Paper fills model 0.2% round-trip (spot) and 0.05%/side (futures) plus 3 bp
slippage. Paper equity is therefore a *lower bound* on live performance, and the
real satellite is the only place where exchange reality (funding, partial
fills, API outages) shows up — those events get their own posts.

---

*机器可读的验证入口：*
```
python3 scripts/publish_ledger.py --runs runs --runs "$FLYGATE_RUNS" --git
```
