# flypnl 🪰

> I connected a fruit-fly brain (connectome SNN, 800 Kenyon cells) to a crypto
> exchange. It trades $150. Full data, honest losses, everything measured.

**flypnl** is one project with two outputs: a **live trading lab** and the
**public record** of what it actually does — the equity curves, the verdicts,
and the posts.

> ⚠️ Honest disclaimer (same culture as [StonkFly](https://github.com/nftechie/stonkfly),
> which inspired the engine): there is **no evidence a fruit-fly brain learns
> profitable trading**. Our own measurement protocol says the fly's directional
> edge does not exist (DSR ≈ 0). What survives is discipline: a funding-carry
> graduation protocol, a momentum defense signal, and honest accounting.
> This is a research diary, not financial advice.

## Layout

```
flypnl/
├── engine/      # the trading lab (full flygate engine: fly-brain SNN, broker,
│   │            #  risk manager, funding-carry graduation protocol, radar,
│   │            #  A-share momentum watch, backtests, 76 tests)
│   └── ...      # python3 -m flygate paper|live|backtest|protocol --help
├── content/     # publishing tools: viral-template library + tweet image generator
├── data/        # PUBLIC datasets exported from the lab (normalized, no balances):
│   │            #  funding_series.csv  — 3-exchange funding-rate time series
│   │            #    (incl. Gate, which has NO official funding-history API)
│   │            #  ashare_momentum.csv — ETF dual-momentum paper portfolio, indexed to 0%
│   │            #  verdicts.md         — every claim we ever measured, incl. failures
├── charts/      # generated images (tweet-sized 1200×675)
├── posts/       # the written record (weekly)
└── tools/
    └── export_from_flygate.py   # lab → public data bridge
```

## The rules of this lab

1. **No claim without a control arm.** Every strategy runs against frozen /
   random / exposure-matched buy-and-hold twins.
2. **No edge without surviving selection.** Champions must pass DSR/PBO/MinTRL,
   not just "look good in-sample".
3. **Percentages go out, absolute balances never do.** All published data is
   normalized; private account state never leaves `engine/` state dirs.
4. **Losses get published too.** Especially the losses.

## Current status (2026-10)

- Fly-brain directional trading: **paper only, no edge declared** (see `data/verdicts.md`)
- Funding-carry leg: graduated candidates exist; entry gate currently closed
- A-share momentum: defense mode (cash + 50% Nasdaq ETF) in a deep bear
- Engine revival pending the documented freeze-storm fix

## Credits

- [StonkFly](https://github.com/nftechie/stonkfly) (MIT) — the original fly-brain
  trading idea; the engine keeps its attribution.
- [FlyWire](https://flywire.ai/) / MaleCNS connectome — the biology.

*Nothing here is financial advice. This is an experiment running on groceries money.*
