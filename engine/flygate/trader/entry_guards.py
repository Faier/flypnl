"""Entry-side guards shared by backtest and the live futures runner.

The 09-17..09-24 live campaigns kept laddering in at leg-tops: the staircase
brain confirms late by construction (r2>=0.85 gate), the chase_pct veto only
catches vertical spikes, and adds are entirely unguarded. This module is the
single implementation of the anti-chase layer so backtest and live can never
drift apart:

  A  extended(px)      — ATR-normalized extension veto for FRESH entries:
                         (px - ema_ext) / atr > ext_atr  -> veto. Covers both
                         vertical legs and slow grinds into the leg-top (the
                         09-24 entry was only +2.4%/100min but sat at the day
                         high, far above its 16h mean).
  B  add_ok()          — pyramid anchor: stop ADDING when price is more than
                         add_max_atr ATR above the campaign's average entry.
                         (The 12.6mo A/B vetoed adds UNCONDITIONALLY and lost
                         35pp; this binds only when extended.)
  C  leg_too_old()     — only young staircases: candles since close crossed
                         above ema_leg must be <= leg_max_age.
  D  pullback_limit()  — soft retest entry: arm a limit chase_pct-free below
                         the signal close; fill at a later candle CLOSE <=
                         limit (identical semantics in backtest and live; no
                         resting order, no live/backtest divergence).
  MR mr_buy/mr_target  — mean-reversion arm (跌多涨，涨多跌): buy a stabilized
                         dip k ATR below the mean in an incoherent tape, sell
                         into strength k' ATR above it. Trend coherence (tel
                         r2 / hs_dn) gates the regime so it will not catch
                         knives in a clean collapse.

All params default off (None) — enabling is via config.json futures block
after the 12mo A/B wins.
"""
from __future__ import annotations


class EntryGuards:
    def __init__(self, ext_atr: float | None = None, ext_ref: int = 200,
                 atr_n: int = 14, add_max_atr: float | None = None,
                 leg_ref: int = 20, leg_max_age: int | None = None,
                 pullback_atr: float | None = None, pullback_ttl: int = 12,
                 mr_buy_atr: float | None = None, mr_sell_atr: float = 0.5,
                 mr_max_r2: float | None = None, mr_max_hs_dn: float = 0.6,
                 regime_r2: float | None = None, regime_n: int = 96):
        self.ext_atr = ext_atr
        self.ext_ref = ext_ref
        self.atr_n = atr_n
        self.add_max_atr = add_max_atr
        self.leg_ref = leg_ref
        self.leg_max_age = leg_max_age
        self.pullback_atr = pullback_atr
        self.pullback_ttl = pullback_ttl
        self.mr_buy_atr = mr_buy_atr
        self.mr_sell_atr = mr_sell_atr
        self.mr_max_r2 = mr_max_r2
        self.mr_max_hs_dn = mr_max_hs_dn
        # regime gate: the 12mo A/B (2026-09-24) showed every unconditional
        # anti-chase veto DESTROYS the trend-month alpha (ext/addcap/age arms
        # +18..54% vs base +79.6%) — the losses live in the range segments,
        # a minority of the year. With regime_r2 set, A/B/C only veto while
        # the SMOOTHED trend coherence sits below it (range tape); a real
        # trend lets the momentum engine run free. Note the entry candle's
        # own r2 is useless here: the brain already requires r2>=0.85 to
        # signal, so the regime must be measured as an EMA over regime_n
        # candles (96 = 8h at 5m).
        self.regime_r2 = regime_r2
        self.regime_n = regime_n
        # running state (one step per brain-cadence candle)
        self._ema_ext: float | None = None
        self._ema_leg: float | None = None
        self._atr: float | None = None
        self._regime: float | None = None
        self._prev_tr_c = 0.0
        self._prev_close = 0.0
        self._prev_above = False
        self.leg_age = 0

    # ------------------------------------------------------------------ state
    @classmethod
    def from_config(cls, fc: dict) -> "EntryGuards":
        def _opt(key):
            v = fc.get(key)
            # config treats 0/None as "off" for every numeric guard
            return float(v) if v else None

        def _int_opt(key):
            v = fc.get(key)
            return int(v) if v else None

        return cls(ext_atr=_opt("ext_atr"), ext_ref=int(fc.get("ext_ref") or 200),
                   atr_n=int(fc.get("atr_n") or 14),
                   add_max_atr=_opt("add_max_atr"),
                   leg_ref=int(fc.get("leg_ref") or 20),
                   leg_max_age=_int_opt("leg_max_age"),
                   pullback_atr=_opt("pullback_atr"),
                   pullback_ttl=int(fc.get("pullback_ttl") or 12),
                   mr_buy_atr=_opt("mr_buy_atr"),
                   mr_sell_atr=float(fc.get("mr_sell_atr") or 0.5),
                   mr_max_r2=_opt("mr_max_r2"),
                   mr_max_hs_dn=float(fc.get("mr_max_hs_dn") or 0.6),
                   regime_r2=_opt("regime_r2"),
                   regime_n=int(fc.get("regime_n") or 96))

    def step(self, candle: list, r2: float | None = None):
        """Advance EMA/ATR/leg-age/regime with one [ts,o,h,l,c,v] candle;
        r2 is the brain's smoothed trend coherence for this candle."""
        _, o, h, l, c, *_ = (candle + [0] * 6)[:6]
        if c <= 0:
            return
        if r2 is not None:
            if self._regime is None:
                self._regime = float(r2)
            else:
                self._regime += (float(r2) - self._regime) / self.regime_n
        # EMA baselines
        if self._ema_ext is None:
            self._ema_ext = c
        else:
            k = 2.0 / (self.ext_ref + 1)
            self._ema_ext += k * (c - self._ema_ext)
        if self._ema_leg is None:
            self._ema_leg = c
        else:
            k = 2.0 / (self.leg_ref + 1)
            self._ema_leg += k * (c - self._ema_leg)
        # Wilder ATR
        tr = max(h - l, abs(h - self._prev_tr_c), abs(l - self._prev_tr_c)) \
            if self._prev_tr_c else h - l
        if self._atr is None:
            self._atr = tr
        else:                          # Wilder smoothing (EMA alpha=1/n)
            self._atr += (tr - self._atr) / self.atr_n
        self._prev_tr_c = c
        # leg age: consecutive brain candles with close above the leg EMA
        above = c > self._ema_leg
        if above:
            self.leg_age = self.leg_age + 1 if self._prev_above else 1
        else:
            self.leg_age = 0
        self._prev_above = above
        self._prev_close = c

    # ------------------------------------------------------------------ reads
    def atr(self) -> float:
        return self._atr or 0.0

    def in_range(self) -> bool:
        """True while the smoothed coherence says range (or no gate set)."""
        if self.regime_r2 is None:
            return True
        return self._regime is not None and self._regime < self.regime_r2

    def z(self, px: float) -> float:
        """ATR-normalized distance from the long baseline (ext_ref EMA)."""
        if not self._atr or self._atr <= 0 or self._ema_ext is None:
            return 0.0
        return (px - self._ema_ext) / self._atr

    def extended(self, px: float) -> bool:
        """A: fresh-entry veto — price stretched too far above its mean
        (range tape only when regime_r2 gates the guard)."""
        if not self.ext_atr or not self._atr or not self.in_range():
            return False
        return self.z(px) > self.ext_atr

    def add_ok(self, px: float, avg_entry: float) -> bool:
        """B: pyramid anchor — no adds once price ran far from avg cost
        (range tape only when regime_r2 gates the guard)."""
        if not self.add_max_atr or not self._atr or avg_entry <= 0:
            return True
        if self.regime_r2 is not None and not self.in_range():
            return True
        return (px - avg_entry) / self._atr <= self.add_max_atr

    def leg_too_old(self) -> bool:
        """C: only young staircases — the up-leg vs ema_leg is ancient
        (range tape only when regime_r2 gates the guard)."""
        if not self.leg_max_age or not self.in_range():
            return False
        return self.leg_age > self.leg_max_age

    def pullback_limit(self, px: float) -> float | None:
        """D: soft-limit level for a retest entry off a fresh BUY signal."""
        if not self.pullback_atr or not self._atr:
            return None
        return px - self.pullback_atr * self._atr

    # ------------------------------------------------------------------ MR arm
    @property
    def mr_on(self) -> bool:
        return bool(self.mr_buy_atr)

    def mr_buy(self, px: float, candle: list, tel: dict) -> bool:
        """跌多: buy a stabilized dip k ATR below the mean, but never inside a
        coherent collapse (hs_dn high), a trending tape (smoothed regime) or a
        momentarily coherent window (instant r2)."""
        if not self.mr_on:
            return False
        if self.z(px) > -self.mr_buy_atr:
            return False
        o, c = candle[1], candle[4]
        if c <= o:                     # stabilization: the dip candle closed green
            return False
        if tel.get("hs_dn", 0.0) > self.mr_max_hs_dn:
            return False
        if self.mr_max_r2 is not None and (tel.get("r2") or 0.0) > self.mr_max_r2:
            return False
        if self.regime_r2 is not None and not self.in_range():
            return False
        return True

    def mr_target(self, px: float) -> bool:
        """涨起来: price recovered to mr_sell_atr ATR above the mean — sell
        into strength (protective ladder still covers the downside)."""
        if not self.mr_on:
            return False
        return self.z(px) >= self.mr_sell_atr
