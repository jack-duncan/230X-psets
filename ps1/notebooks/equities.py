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
# | | ticker | ~price | size |
# |---|---|---|---|
# | mega cap | **NVDA** | \$224 | ~\$5T |
# | micro cap | **GPRO** | \$1.54 | ~\$0.2B |
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
# 3. **There is no snapshot record**, so the book is rebuilt from empty at 09:30.
#    Nasdaq re-adds the pre-open book immediately after the opening cross, so only the
#    first fraction of a second is incomplete — 0.3% of NVDA cancels and 0.8% of GPRO
#    cancels reference an order we never saw added.

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
# often, but GPRO's quoted spread is ~90x wider in relative terms. The opening cross
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
# just reproduces the depth from (e). For NVDA at ~\$224 that is close to the truth.

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
# *To be written once the notebook has been run against the Databento download —
# the points below are what the framework predicts and should be checked against the
# numbers above, then kept, sharpened or discarded.*
#
# 1. **Size and the cost of immediacy.** GPRO's spread in basis points should be one to
#    two orders of magnitude wider than NVDA's, and its depth in dollars far thinner,
#    with the gap widest at the open. Relate this to the inventory and adverse-selection
#    components: at \$1.54 the one-cent tick alone is 65 bps, so GPRO's spread is
#    tick-constrained from below and its depth at the touch should be large in shares
#    but small in dollars.
# 2. **Price impact.** $\lambda$ should be larger for GPRO in basis points; check
#    whether it is larger in *cents* — for NVDA the 5-second impact will often be a
#    fraction of a tick, showing up as a change in the mid only when the touch empties.
# 3. **Bounce versus discreteness.** Compare the volatility signature of the trade
#    series with that of the mid: for NVDA, sampled at one second, the transaction RV
#    should exceed the midquote RV and lag-1 transaction autocorrelation should be
#    negative; for GPRO, with a spread of one tick and a mid that moves in half-cent
#    steps, look for the same zero-return pile-up the EBS mids show.
# 4. **The intraday U.** Spreads and impact should be highest in the first half hour
#    and fall through the day, with a smaller rise into the close; depth the mirror
#    image; variance of one-minute returns highest at the open. Note the fleeting
#    liquidity in the cancel-to-add ratio and how it moves through the day.
