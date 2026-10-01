# ---
# jupyter:
#   jupytext:
#     formats: py:percent,ipynb
#     text_representation:
#       extension: .py
#       format_name: percent
#       format_version: '1.3'
#       jupytext_version: 1.19.5
#   kernelspec:
#     display_name: Python 3 (ipykernel)
#     language: python
#     name: python3
# ---

# %% [markdown]
# # PS1 — Equities: intraday descriptive statistics
#
# Two Nasdaq-listed stocks at opposite ends of the size distribution, over one full
# regular session (**2026-09-02, 09:30–16:00 ET**):
#
# | | ticker | price on the day | market cap |
# |---|---|---|---|
# | mega cap | **NVDA** | \$219 → \$224 | ~\$5.5T |
# | micro cap | **GPRO** | \$1.22 → \$1.71 (+40%) | ~\$0.27B |
#
# The source is Databento MBO for `XNAS.ITCH` — every add, cancel, fill and trade
# message Nasdaq published, one row each. `mbo.py` replays those messages into a
# full limit order book and hands back three objects per symbol:
#
# * `trades` — the trade tape, signed, with the quote prevailing at each print
# * `quotes` — the BBO, one row per change
# * `grid` — the book sampled at the end of every second, plus resting depth by
#   distance from the mid
#
# Parts (a)–(k) are computed by `hfstats.py`, which is written once against these
# objects and is also what the currencies notebook runs, so the two panels are
# measured with one definition of every statistic.
#
# Three things about the raw feed worth stating up front, because they shape the
# numbers below:
#
# 1. **An execution emits three records** — `T` (the trade), `F` (the fill against the
#    resting order) and `C` (which removes the shares). Only `A` and `C` move the book;
#    counting `F` as well would double the depth removed, and counting it as a trade
#    would double the volume.
# 2. **`T.side` is the aggressor**, except for non-displayed executions and the cross,
#    which arrive unsigned (22% of NVDA prints). Those are signed with the quote rule.
# 3. **The book has to be built from the pre-market.** Databento snapshots the book only
#    at UTC midnight, so the request runs from there (20:00 ET the evening before, when
#    Nasdaq's book is empty) to the close and the pre-market is replayed before the
#    first 09:30 snapshot; only the regular session is recorded. Started from an empty
#    book at 09:30 instead, 0.3% of NVDA's and 0.8% of GPRO's session cancels reference
#    an order never seen — resting pre-open orders, which are *not* re-added after the
#    cross — and the resting depth within \$0.50 of the mid is understated by 7% (NVDA)
#    and 37% (GPRO) at 09:31, still 2% and 22% at 10:00, and 0.2% and 8% on average over
#    the day. Touch depth and spreads are unaffected for NVDA, and GPRO's touch by <1%.
#    With the full-day replay every session cancel references an order seen added.

# %%
import numpy as np
import pandas as pd

import hfstats as hs
import mbo
import plots
from hfstats import FREQS, MINUTE, summary, tape

# Approximate, from the latest filings — the only input here that is not in the data.
SHARES_OUT = {"NVDA": 24.4e9, "GPRO": 0.16e9}

days = mbo.load("NVDA", "GPRO")
nvda, gpro = days["NVDA"], days["GPRO"]

days

# %% [markdown]
# ## Choice of stocks
#
# Size shows up in the data long before we reach the statistics: NVDA trades ~12x as
# often and turns over ~180x the dollars, but GPRO's quoted spread is ~93x wider in
# relative terms. GPRO also happened to rally 40% on this day, from \$1.22 to \$1.71
# in a steady climb between noon and 15:30 — worth keeping in mind wherever a GPRO
# statistic is quoted in basis points of a moving price. The opening cross
# is excluded from the tape throughout (`hfstats.tape`): it prints once, unsigned, and
# is ~30x a normal trade, so left in it dominates the first minute of every trade
# statistic.

# %%
pd.DataFrame({
    d.symbol: {
        "last trade": tape(d).price.iloc[-1],
        "shares outstanding": SHARES_OUT[d.symbol],
        "market cap ($bn)": SHARES_OUT[d.symbol] * tape(d).price.iloc[-1] / 1e9,
        "trades": len(tape(d)),
        "share volume": tape(d)["size"].sum(),
        "dollar volume ($mm)": tape(d).value.sum() / 1e6,
        "quote updates": len(d.quotes),
        "mean spread ($)": d.grid.spread.mean(),
        "mean spread (bps)": (1e4 * d.grid.spread / d.grid.mid).mean(),
    } for d in (nvda, gpro)
})

# %% [markdown]
# ## Part (a) — dollar trading volume per minute
#
# Traded notional, `price x size` summed within each minute.

# %%
a = {s: hs.dollar_volume_per_minute(d) for s, d in days.items()}
pd.concat({s: summary(v) for s, v in a.items()}, names=["symbol", "series"]).round(0)

# %% [markdown]
# ## Part (b) — number of trades and number of orders per minute
#
# Trades are `T` prints; orders are `A` (add-order) messages, which only a full-depth
# feed like ITCH exposes — these are the *displayed* orders, which is what the FAQ
# asks for (hidden orders carry no add message and are seen only when they execute).
# Cancels are shown alongside: the ratio of cancels to adds is the usual read on how
# much of the book is fleeting.

# %%
b = {s: hs.counts_per_minute(d) for s, d in days.items()}
pd.concat({s: summary(v) for s, v in b.items()}, names=["symbol", "series"]).round(1)

# %%
pd.DataFrame({s: {
    "orders per trade": v.adds.sum() / max(v.trades.sum(), 1),
    "cancels per add": v.cancels.sum() / max(v.adds.sum(), 1),
} for s, v in b.items()}).round(2)

# %% [markdown]
# ## Part (c) — open, close, high and low prices
#
# Both readings: the session's OHLC, and the per-minute trade-price OHLC. Minutes with
# no prints come back as `NaN` rather than being forward-filled, so the `count` column
# shows how often each name actually trades.

# %%
pd.DataFrame({s: hs.ohlc_daily(d) for s, d in days.items()})

# %%
c = {s: hs.ohlc_per_minute(d) for s, d in days.items()}
pd.concat({s: summary(v) for s, v in c.items()}, names=["symbol", "series"]).round(4)

# %% [markdown]
# ## Part (d) — VWAP per minute
#
# Size-weighted average trade price, `sum(p*q) / sum(q)` within the minute.

# %%
d_ = {s: hs.vwap_per_minute(d) for s, d in days.items()}
pd.concat({s: summary(v) for s, v in d_.items()}, names=["symbol", "series"]).round(4)

# %% [markdown]
# ## Part (e) — BBO spread and depth per minute
#
# Averaged over end-of-second book snapshots, so every second carries equal weight —
# a time-weighted average, rather than one weighted by how often each name requotes.

# %%
e = {s: hs.spread_depth_per_minute(d) for s, d in days.items()}
pd.concat({s: summary(v) for s, v in e.items()}, names=["symbol", "series"]).round(4)

# %% [markdown]
# ## Part (f) — depth within twice the day's average spread
#
# Shares resting within one average spread either side of the mid, i.e. inside a band
# of **twice** the day's mean quoted spread. The best bid sits half a spread from the
# mid, so the band is empty exactly when the prevailing spread exceeds twice its
# average — `frac_empty` reports how often that happens. Note the degenerate case: if a
# name's spread is always one tick, the band reaches no further than the BBO and (f)
# just reproduces the depth from (e). GPRO — one tick wide in 99.6% of seconds — is
# exactly that case; NVDA's 1.7-cent average spread puts the band one level past the
# touch.

# %%
f = {s: hs.band_depth_per_minute(d) for s, d in days.items()}
for s, v in f.items():
    print(f"{s}: band = ${v.attrs['band']:.4f} wide")
pd.concat({s: summary(v) for s, v in f.items()}, names=["symbol", "series"]).round(4)

# %% [markdown]
# ## Part (g) — 5-second price impact per minute
#
# Within each minute, regress the 5-second midquote return following each print on the
# sign of that print:
#
# $$10^4 \cdot \log\!\big(m_{t+5s}/m_t\big) \;=\; \alpha + \lambda \, s_t + \varepsilon_t,
# \qquad s_t \in \{-1, +1\}$$
#
# $\lambda$ is the midquote move a buy pushes through, in basis points. Because $s_t$ is
# binary the slope is just the difference in mean response between buys and sells,
# halved — computed in closed form rather than by looping OLS over 390 minutes. Minutes
# with fewer than ten prints, or with trades on one side only, cannot identify it and
# are left blank.

# %%
g = {s: hs.price_impact_per_minute(d) for s, d in days.items()}
pd.concat({s: summary(v) for s, v in g.items()}, names=["symbol", "series"]).round(4)

# %% [markdown]
# The mean of per-minute slopes weights a thin minute as much as a busy one. The
# pooled alternative — every print in the session in one regression, with Newey–West
# standard errors for the overlapping 5-second windows — is the single number an
# economist would quote, and it comes with a t-statistic.

# %%
pd.DataFrame({s: hs.price_impact_pooled(d) for s, d in days.items()}).T.round(4)

# %% [markdown]
# ## Part (h) — one-second and one-minute midquote and transaction price series
#
# The last quote and the last print in each interval. Intervals with no trade inherit
# the previous price, so the two series stay on a common clock.

# %%
h = {s: {fq: hs.price_series(d, fq) for fq in FREQS} for s, d in days.items()}
pd.concat({(s, fq): summary(v) for s, d_ in h.items() for fq, v in d_.items()},
          names=["symbol", "freq", "series"]).round(4)

# %% [markdown]
# ## Part (i) — log returns
#
# $r_t = \log(p_t) - \log(p_{t-1})$, in basis points, for both series.

# %%
i = {s: {fq: hs.log_returns(v) for fq, v in d_.items()} for s, d_ in h.items()}
pd.concat({(s, fq): summary(1e4 * v) for s, d_ in i.items() for fq, v in d_.items()},
          names=["symbol", "freq", "series"]).round(4)

# %% [markdown]
# ## Part (j) — realized variance
#
# $RV = \sum_t r_t^2$ over the session, for each series and sampling frequency. Gaps
# between the midquote and transaction figures, and between the two frequencies, are
# where microstructure noise shows up — the finer the sampling, and the closer to the
# traded price rather than the mid, the more of the variance is noise rather than signal.

# %%
pd.concat({(s, fq): hs.realized_variance(v) for s, d_ in i.items() for fq, v in d_.items()},
          names=["symbol", "freq", "series"])

# %% [markdown]
# ### Volatility signature
#
# RV of each series against the interval it is sampled at, in bps². For a price
# contaminated by bid–ask bounce this curve falls as the interval lengthens; the share
# of intervals with no price change shows how much of the high-frequency figure is
# instead being held *down* by discreteness (the currencies notebook finds that effect
# dominates for EBS mids).

# %%
_freqs = ["1s", "2s", "5s", "10s", "30s", MINUTE, "2min", "5min"]


def _signature(series):
    """RV in bps^2, and the share of intervals with no price change, by frequency."""
    out = {}
    for fq in _freqs:
        r = np.log(series.resample(fq).last().ffill().dropna()).diff().dropna()
        out[fq] = {"RV (bps^2)": 1e8 * (r ** 2).sum(), "zero returns (%)": 100 * (r == 0).mean()}
    return pd.DataFrame(out).T


pd.concat({(s, k): _signature(src) for s, d in days.items()
           for k, src in (("mid", d.grid.mid), ("trade", tape(d).price))},
          axis=1, names=["symbol", "series", ""]).round(1)

# %% [markdown]
# ## Part (k) — autocorrelation of returns
#
# The Box–Pierce $Q$ statistic tests whether the first $k$ autocorrelations are jointly
# zero; the ACF and partial ACF show where any dependence sits. Two things to read off:
# whether transaction returns are more negatively autocorrelated at lag 1 than midquote
# returns (the bid-ask bounce, which only bites when sampling is fast enough that
# consecutive observations straddle the spread), and whether the sign flips between the
# one-second and one-minute clocks.

# %%
pd.concat({(s, fq): hs.autocorrelation(v) for s, d_ in i.items() for fq, v in d_.items()},
          names=["symbol", "freq", "lag"]).round(4)

# %% [markdown]
# ## Intraday plots
#
# The liquidity measures — spread, BBO depth and price impact — per five-minute
# interval, then the variance and lag-1 autocorrelation of one-minute transaction
# returns per thirty-minute interval, then the ACF of each return series to ten lags.
# One panel per stock, because the two share units but differ in level by orders of
# magnitude.

# %%
liq5 = {s: plots.liquidity_by_interval(d) for s, d in days.items()}
pd.concat({s: summary(v) for s, v in liq5.items()}, names=["symbol", "series"]).round(3)

# %%
plots.plot_liquidity(days, "spread_bps", save="eq_spread_bps_5min");

# %%
plots.plot_liquidity(days, "depth", save="eq_depth_5min");

# %%
plots.plot_liquidity(days, "lambda_bps", save="eq_lambda_bps_5min");

# %% [markdown]
# ### One-minute transaction returns by half hour
#
# Thirty one-minute returns per interval, so the lag-1 autocorrelation is estimated
# on 29 pairs and the dashed lines at ±1.96/√n show how little a single half hour can
# say on its own; the shape across the day is the thing to read.

# %%
ret30 = {s: plots.returns_by_interval(d) for s, d in days.items()}
pd.concat(ret30, axis=1, names=["symbol", ""]).round(3)

# %%
plots.plot_returns(days, "variance_bps2", save="eq_variance_bps2_30min");

# %%
plots.plot_returns(days, "ac1", save="eq_ac1_30min");

# %% [markdown]
# ### Autocorrelation functions
#
# One row per stock, midquote on the left and transaction price on the right; the
# shaded band is ±1.96/√n.

# %%
for _fq in FREQS:
    plots.plot_acf({f"{s} {k}": i[s][_fq][k] for s in days for k in ("mid", "trade")},
                   lags=10, save=f"eq_acf_{_fq}", title=f"ACF of {_fq} log returns")

# %% [markdown]
# ## Summary statistics table
#
# Every part at the daily level, one column per stock; the same `hfstats.headline`
# column the currencies notebook produces, so the two panels stack into one table.
# Dollar volume is in dollars and depth in shares here, against USD millions and
# millions of base currency for the pairs, so only the relative rows compare across.

# %%
table = pd.DataFrame({s: hs.headline(d) for s, d in days.items()})
table.to_csv(plots.FIG_DIR / "summary_equities.csv")
table.round(4)

# %% [markdown]
# ## Observations
#
# 1. **Size sets the spread, but not the dollar depth.** NVDA quotes 1.7 cents wide
#    (0.76 bps) and GPRO 1.0 cent (71 bps): a 93x gap in relative terms that is almost
#    entirely the tick. GPRO sits at the one-cent minimum in 99.6% of seconds, so its
#    spread in basis points is just the inverse of its price — 85 bps in the first five
#    minutes at \$1.22, 59 bps in the last at \$1.70 — and the "narrowing" through the
#    day in the spread figure is the rally, not a liquidity change. NVDA is one tick
#    wide only 41% of the time, so its spread does carry information. Depth at the touch
#    is where the tick-constraint prediction bites: GPRO rests 50,700 shares at the BBO
#    against NVDA's 462 — but in dollars that is \$73k against \$104k, the same order of
#    magnitude, not "far thinner". A one-cent spread on a \$1.50 stock is such a wide
#    gate that the queue behind it is long; the band measure (f) shows the same thing,
#    since for GPRO the ±1-spread band never reaches past the touch. Volume tells the
#    size story more honestly: \$5.1bn against \$28mm on the day, 892 against 75 prints
#    a minute, 14 displayed orders per trade against 5, and a cancel-to-add ratio of
#    about one for both — nearly every displayed order is cancelled rather than filled.
#    NVDA's tape is 74% odd lots (median print 25 shares); GPRO's median print is a
#    round lot.
# 2. **Impact is larger for GPRO in basis points and smaller in cents.** Pooled over the
#    session with HAC errors, λ is 0.46 bps for NVDA (t = 28) and 26.7 bps for GPRO
#    (t = 28); the per-minute means are 0.33 and 22.7 bps, lower because thin minutes
#    weigh as much as busy ones. In price units that is 1.0 cent for NVDA — a signed
#    trade moves the mid by about one tick, roughly 0.6 of its spread, within five
#    seconds — and 0.4 cents for GPRO, less than half a tick. GPRO's mid can only move
#    in half-cent steps, so 26.7 bps is the *probability* that a trade shifts the touch
#    times the 29–41 bps that one half-tick is worth at its price; its λ falls through
#    the afternoon in the figure for the same mechanical reason its spread does.
#    NVDA's λ is highest in the first five minutes (0.55 bps) with spikes to 1.3 at
#    09:55 and 1.2 at 11:00, and lowest into the close (0.24).
# 3. **Bounce is weak in both names; what shows up instead is discreteness and a
#    transitory component at one minute.** For NVDA, transaction RV at one second
#    exceeds midquote RV by only 1.8% (33,019 against 32,435 bps²), and the lag-1
#    autocorrelation of one-second returns is *positive* for both series (+0.017 trade,
#    +0.025 mid, Box–Pierce p < 0.01): with 15 prints a second, mostly odd lots inside
#    or at a 1.7-cent spread, the last trade of each second is essentially the touch,
#    and what the ACF picks up is gradual quote adjustment, not Roll's bounce. The
#    volatility signature is two-sided: the midquote RV *rises* from 32,400 bps² at one
#    second to 35,000 at thirty seconds (21% of one-second mid returns are exactly zero,
#    so discreteness holds the fine-grid figure down, as it does for the EBS mids) and
#    then falls to 27,300 at one minute and 22,300 at five, where a transitory component
#    averages out. That component is visible directly: the lag-1 autocorrelation of
#    one-minute returns is −0.12 for the mid and −0.12 for trades (p = 0.02), negative in
#    nine of thirteen half hours and −0.51 in 11:30–12:00, and it is in the *mid*, so it
#    is not bid–ask bounce. GPRO is the discreteness case: 97% of its one-second
#    midquote returns and 94% of its transaction returns are zero, yet its RV falls
#    monotonically from 2.83 million bps² at one second to 2.40 million at thirty
#    seconds and is flat after that, its one-second returns are negatively
#    autocorrelated in both series (−0.02, p < 0.001), and trade RV exceeds mid RV at
#    every frequency — the standard bounce signature, faint but there, from a print
#    that alternates between a bid and an ask one cent apart. Its one-minute returns
#    show no autocorrelation at all (p = 0.45). The session RV is 1.8% for NVDA and
#    16.8% for GPRO — a 40% day.
# 4. **The U is half a U.** NVDA's opening half hour is the extreme of everything:
#    spread 1.8 bps in the first five minutes against 0.66 at noon, depth 200 shares
#    against 360, and the variance of one-minute returns 237 bps² against 15 in the
#    15:00 half hour — sixteen times. But there is no second arm. Spread and impact fall
#    monotonically to their lows in the last five minutes (0.53 bps, 0.24 bps) while
#    depth triples to 1,410 shares and the busiest minute of the day is 15:59 with
#    675k shares: the closing auction attracts resting liquidity and volume without
#    widening anything, the opposite of the open. The variance ticks up only slightly in
#    the last half hour (30 against 15 bps²). The classic pattern is a U in *volume*
#    and an L in *cost*. GPRO's day is shaped by its own news rather than the clock:
#    half-hour variance peaks at 12,900 bps² in 13:30–14:00, during the rally, not at
#    the open (10,600), and the largest one-minute move (+4.3%) is at 13:55. While it
#    trends the lag-1 autocorrelation of its one-minute returns is positive in most half
#    hours (+0.22, +0.16, +0.20), the same momentum-while-trending sign the EUR/USD
#    series shows going into the FOMC release. Its cancel-to-add ratio is 1.03 for the
#    day; NVDA's is 0.97, with the 09:30 minute alone carrying 160k add messages against a
#    median of 9,900 — the fleeting-liquidity burst is an opening phenomenon.
