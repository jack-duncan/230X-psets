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
# # PS1 — Currencies: intraday descriptive statistics
#
# The three EBS pairs required by the homework, over one full session
# (**2012-01-25, 09:30–16:00**):
#
# | | pair | ~rate | quoted in |
# |---|---|---|---|
# | major | **EUR/USD** | 1.297 → 1.311 | USD per EUR |
# | major | **USD/JPY** | 78.06 → 77.74 | JPY per USD |
# | cross | **EUR/JPY** | 101.26 → 101.94 | JPY per EUR |
#
# They are not three independent instruments: EUR/JPY is the product of the other two,
# which is what makes part (l) possible.
#
# `fx.py` replays the EBS level-update feed into a limit order book and hands back the
# same objects `mbo.py` produces for the equities, so `hfstats.py` computes parts
# (a)–(k) for both panels from a single definition.
#
# Four features of this feed shape everything below, and differ sharply from ITCH:
#
# 1. **Level updates, not orders.** A message says "this side, this price, now holds
#    this much"; `SIZE = 0` removes the level. There are no order ids, so there is no
#    add/cancel count to report — which is why the homework asks for order counts on
#    ITCH only. What we can count is *messages*, and those net together an unknown
#    number of dealer actions.
# 2. **The book is capped at ten levels a side.** Depth past the tenth level is not
#    missing from the file, it is unobservable. Every depth number here is a lower
#    bound in a way the ITCH numbers are not.
# 3. **Timestamps are stamped to 100 ms**, against nanoseconds for ITCH, and many
#    updates share one. The book is snapshotted once per timestamp, after the whole
#    batch carrying it has been applied — applying updates one at a time invents
#    crossed books the feed never showed.
# 4. **The trade tape lags the book.** Matched at a print's own timestamp, only 47% of
#    EUR/USD prints sit at or outside the touch and buys average just 0.33 spreads
#    above sells — implausible for a market with no hidden liquidity. Backing the
#    match up 200 ms gives 85% and 0.91 spreads, and 200 ms is the optimum on all
#    three pairs independently. `fx.LAG` applies that offset.

# %%
import numpy as np
import pandas as pd

import fx
import hfstats as hs
import plots
from hfstats import FREQS, MINUTE, summary

books = fx.load()
eurusd, usdjpy, eurjpy = (books[p] for p in fx.PAIRS)

books

# %% [markdown]
# ## The three pairs
#
# Sizes are in **millions of the base currency** (EUR for EUR/USD and EUR/JPY, USD for
# USD/JPY) — EBS quotes in millions and every size in the file is a whole number of
# them. `dollar volume` converts to USD millions, which for EUR/JPY needs the EUR/USD
# mid prevailing at each print.
#
# The ordering is the one FX microstructure predicts: the two majors are cheaper and
# far more active than the cross, and EUR/JPY — which can always be manufactured from
# the other two — is the widest of the three.

# %%
overview = pd.DataFrame({
    p: {
        "tick size": b.tick,
        "trades": len(b.trades),
        "volume (mm base)": b.trades["size"].sum(),
        "dollar volume (USD mm)": b.trades.value.sum(),
        "quote updates": len(b.quotes),
        "mean spread (ticks)": b.grid.spread.mean() / b.tick,
        "mean spread (bps)": (1e4 * b.grid.spread / b.grid.mid).mean(),
        "mean BBO depth (mm)": (b.grid.bid_sz + b.grid.ask_sz).mean(),
        "mean book depth (mm)": (b.grid.bid_tot + b.grid.ask_tot).mean(),
        "crossed batches": b.n_crossed,
    } for p, b in books.items()
})
overview.round(3)

# %% [markdown]
# ### Data quality
#
# Two things worth stating before the statistics rest on them.
#
# **Momentary crossed books.** Applying a timestamp's updates as one batch removes
# almost all crossing, but 153 EUR/USD batches (0.2%), 65 USD/JPY and 3 EUR/JPY still
# leave the book locked or crossed. These are blanked out of the quote and grid frames
# rather than repaired, so they show up as the small `NaN` share below.
#
# **Trade signs.** The aggressor flag is the exchange's own label and is used as the
# sign. `sign_disagrees` counts prints whose price sits on the far side of the
# 200 ms-prior mid from that flag — the residual disagreement after correcting for the
# tape lag, and a fair measure of how much of the signing is genuinely uncertain.

# %%
pd.DataFrame({
    p: {
        "grid seconds": len(b.grid),
        "grid NaN (%)": 100 * b.grid.mid.isna().mean(),
        "trades with no prior quote": int(b.trades.mid.isna().sum()),
        "sign disagrees with quote (%)": 100 * b.trades.sign_disagrees.mean(),
        "buys at/above prior ask (%)": 100 * b.trades.eval("sign == 1 and price >= ask").sum()
                                       / max((b.trades["sign"] == 1).sum(), 1),
        "sells at/below prior bid (%)": 100 * b.trades.eval("sign == -1 and price <= bid").sum()
                                        / max((b.trades["sign"] == -1).sum(), 1),
    } for p, b in books.items()
}).round(2)

# %% [markdown]
# ## Part (a) — dollar trading volume per minute
#
# Traded notional in USD millions, summed within each minute.

# %%
a = {p: hs.dollar_volume_per_minute(b) for p, b in books.items()}
pd.concat({p: summary(v) for p, v in a.items()}, names=["pair", "series"]).round(2)

# %% [markdown]
# ## Part (b) — number of trades and messages per minute
#
# Trades are prints on the deal feed. `updates` and `deletes` are book messages —
# **not** order counts, for the reason given at the top; the homework restricts order
# counts to ITCH precisely because EBS cannot supply them. The delete share is still
# informative as the EBS analogue of a cancel-to-add ratio.

# %%
b_ = {p: hs.counts_per_minute(bk) for p, bk in books.items()}
pd.concat({p: summary(v) for p, v in b_.items()}, names=["pair", "series"]).round(2)

# %%
pd.DataFrame({p: {
    "messages per trade": (v.updates.sum() + v.deletes.sum()) / max(v.trades.sum(), 1),
    "delete share of messages": v.deletes.sum() / (v.updates.sum() + v.deletes.sum()),
} for p, v in b_.items()}).round(2)

# %% [markdown]
# ## Part (c) — open, close, high and low prices
#
# Both readings: the session's OHLC, and the per-minute trade-price OHLC. Minutes with
# no print stay `NaN` rather than being forward-filled, so the `count` column shows how
# often each pair actually trades — EUR/JPY prints in only a small minority of minutes.

# %%
pd.DataFrame({p: hs.ohlc_daily(b) for p, b in books.items()})

# %%
c = {p: hs.ohlc_per_minute(b) for p, b in books.items()}
pd.concat({p: summary(v) for p, v in c.items()}, names=["pair", "series"]).round(5)

# %% [markdown]
# ## Part (d) — VWAP per minute
#
# Size-weighted average trade price within the minute.

# %%
d = {p: hs.vwap_per_minute(b) for p, b in books.items()}
pd.concat({p: summary(v) for p, v in d.items()}, names=["pair", "series"]).round(5)

# %% [markdown]
# ## Part (e) — BBO spread and depth per minute
#
# Averaged over end-of-second snapshots, so every second carries equal weight.
# `book_depth` is the whole ten-level book, which the fixed-depth EBS feed gives us and
# ITCH would require summing an unbounded number of levels to match.

# %%
e = {p: hs.spread_depth_per_minute(b) for p, b in books.items()}
pd.concat({p: summary(v) for p, v in e.items()}, names=["pair", "series"]).round(4)

# %% [markdown]
# ## Part (f) — depth within twice the day's average spread
#
# Size resting within one average spread either side of the mid, i.e. inside a band of
# **twice** the day's mean quoted spread. The best bid sits half the prevailing spread
# from the mid, so the band is empty exactly when the spread exceeds twice its average
# — `frac_empty` reports how often that happens.

# %%
f = {p: hs.band_depth_per_minute(b) for p, b in books.items()}
for p, v in f.items():
    print(f"{p}: band = {v.attrs['band']:.5f} "
          f"({v.attrs['band'] / books[p].tick:.1f} ticks) wide")
pd.concat({p: summary(v) for p, v in f.items()}, names=["pair", "series"]).round(4)

# %% [markdown]
# ## Part (g) — 5-second price impact per minute
#
# Within each minute, regress the 5-second midquote return following each print on the
# sign of that print:
#
# $$10^4 \cdot \log\!\big(m_{t+5s}/m_t\big) \;=\; \alpha + \lambda \, s_t + \varepsilon_t,
# \qquad s_t \in \{-1, +1\}$$
#
# $\lambda$ is the midquote move a buy pushes through, in basis points. Minutes with
# fewer than ten prints, or with trades on only one side, cannot identify it and are
# dropped — which for EUR/JPY removes almost the whole session, so its impact estimate
# rests on very few minutes and should be read with that in mind.

# %%
g = {p: hs.price_impact_per_minute(b) for p, b in books.items()}
pd.concat({p: summary(v) for p, v in g.items()}, names=["pair", "series"]).round(4)

# %% [markdown]
# The mean of per-minute slopes weights a thin minute as much as a busy one. The
# pooled alternative — every print in the session in one regression, with Newey–West
# standard errors for the overlapping 5-second windows — is the single number an
# economist would quote, and it comes with a t-statistic.

# %%
pd.DataFrame({p: hs.price_impact_pooled(b) for p, b in books.items()}).T.round(4)

# %% [markdown]
# ## Part (h) — one-second and one-minute midquote and transaction price series
#
# The last quote and the last print in each interval. Intervals with no trade inherit
# the previous price, so the two series stay on a common clock.

# %%
h = {p: {fq: hs.price_series(b, fq) for fq in FREQS} for p, b in books.items()}
pd.concat({(p, fq): summary(v) for p, d_ in h.items() for fq, v in d_.items()},
          names=["pair", "freq", "series"]).round(5)

# %% [markdown]
# ## Part (i) — log returns
#
# $r_t = \log(p_t) - \log(p_{t-1})$, in basis points, for both series.

# %%
i = {p: {fq: hs.log_returns(v) for fq, v in d_.items()} for p, d_ in h.items()}
pd.concat({(p, fq): summary(1e4 * v) for p, d_ in i.items() for fq, v in d_.items()},
          names=["pair", "freq", "series"]).round(4)

# %% [markdown]
# ## Part (j) — realized variance
#
# $RV = \sum_t r_t^2$ over the session. Two separate microstructure effects show up
# here and they push in **opposite** directions, so it is worth separating them.
#
# * **Across series, at a fixed frequency:** transaction RV exceeds midquote RV
#   (EUR/USD at 1s: 2927 vs 2320 bps²). That is bid–ask bounce — consecutive prints
#   straddle the spread, adding variance the mid never sees.
# * **Across frequencies, for the midquote:** RV *rises* as sampling coarsens rather
#   than falling. That is the reverse of the usual equity signature, and the cause is
#   in the next cell: at one second the mid simply has not moved yet.

# %%
pd.concat({(p, fq): hs.realized_variance(v) for p, d_ in i.items() for fq, v in d_.items()},
          names=["pair", "freq", "series"])

# %% [markdown]
# ### Volatility signature
#
# RV of the midquote against the interval it is sampled at. For a price contaminated
# by bid–ask bounce this curve falls as the interval lengthens; here it *rises* for
# both majors, roughly doubling for USD/JPY between one second and five minutes.
#
# The reason is in the last column: at one-second sampling **43% of EUR/USD seconds and
# 64% of USD/JPY seconds carry a midquote return of exactly zero**. With a spread of
# about one pip and a tick a tenth that size, the EBS mid is a coarse, sticky object —
# over one second it usually has not moved at all. Those zeros drag the high-frequency
# RV *below* the true variance, and the shortfall unwinds as sampling coarsens and each
# interval contains at least one real quote revision.
#
# So at one second these pairs are dominated by price discreteness and stale quotes,
# not by the bounce that dominates a comparable equity series. The bounce is still
# present — it is what separates the transaction column from the midquote column at any
# given frequency — but it is the smaller of the two effects on the mid.

# %%
_freqs = ["1s", "2s", "5s", "10s", "30s", MINUTE, "2min", "5min"]


def _signature(series):
    """RV in bps^2, and the share of intervals with no price change, by frequency."""
    out = {}
    for fq in _freqs:
        r = np.log(series.resample(fq).last().ffill().dropna()).diff().dropna()
        out[fq] = {"RV (bps^2)": 1e8 * (r ** 2).sum(), "zero returns (%)": 100 * (r == 0).mean()}
    return pd.DataFrame(out).T


pd.concat({p: _signature(b.grid.mid) for p, b in books.items()},
          axis=1, names=["pair", ""]).round(1)

# %% [markdown]
# ## Part (k) — autocorrelation of returns
#
# Box–Pierce $Q$ tests whether the first $k$ autocorrelations are jointly zero; the ACF
# and PACF show where any dependence sits. The thing to look for is bid–ask bounce: a
# negative lag-1 autocorrelation in transaction returns that is absent, or much weaker,
# in midquote returns, and that fades as the sampling interval lengthens past the point
# where consecutive observations straddle the spread.

# %%
pd.concat({(p, fq): hs.autocorrelation(v) for p, d_ in i.items() for fq, v in d_.items()},
          names=["pair", "freq", "lag"]).round(4)

# %% [markdown]
# ## Part (l) — triangular arbitrage
#
# EUR/JPY can be traded directly or manufactured through the dollar, so the two prices
# must agree. Buying a synthetic euro means paying the EUR/USD ask and then the
# USD/JPY ask; selling one means taking both bids:
#
# $$\text{synthetic ask} = a_{\text{EUR/USD}} \cdot a_{\text{USD/JPY}}, \qquad
#   \text{synthetic bid} = b_{\text{EUR/USD}} \cdot b_{\text{USD/JPY}}$$
#
# which leaves two ways to be paid, each needing all three legs at once:
#
# * **direct cheap** — buy EUR/JPY at its ask, sell the synthetic: $\text{synth bid} - a_{\text{EUR/JPY}}$
# * **direct rich** — sell EUR/JPY at its bid, buy the synthetic: $b_{\text{EUR/JPY}} - \text{synth ask}$
#
# Quantity is set by the thinnest leg, with every leg converted to millions of euro —
# so it is the size of the round trip that can be put on, not the size of any one
# quote. All three feeds are sampled on the common one-second clock the homework asks
# for.

# %%
tri = fx.triangular(books)
fx.triangular_summary(tri).round(4)

# %% [markdown]
# ### Are these real, or artefacts?
#
# Two ways the headline number could be spurious, both checked rather than assumed.
#
# **Stale quotes.** Sampling three independently-updating feeds on a common clock can
# line up quotes that were never simultaneously live. If that drove the result, the
# opportunity seconds would sit behind *older* quotes than average. They sit behind
# newer ones — the median stalest leg is 1.4 s at an opportunity against 2.1 s across
# all seconds — so the deviations arrive when the market is moving, not when it is
# asleep. That is the opposite of the staleness signature.
#
# **Costs and latency.** Charging each round trip a few EUR/JPY ticks stands in
# crudely for brokerage and for the slippage of legging three books. The count decays
# smoothly rather than collapsing, which is what a real but thin edge looks like.

# %%
pd.DataFrame({
    "median stalest leg (s)": {
        "at an opportunity": tri.age_s[tri.open.fillna(False)].median(),
        "all seconds": tri.age_s.median(),
    },
    "p90 stalest leg (s)": {
        "at an opportunity": tri.age_s[tri.open.fillna(False)].quantile(0.9),
        "all seconds": tri.age_s.quantile(0.9),
    },
}).round(2)

# %%
pd.DataFrame({
    f"{c} ticks": fx.triangular_summary(fx.triangular(books, cost_ticks=c))
    for c in (0, 0.5, 1, 2, 3, 5)
}).loc[["seconds with an opportunity", "frequency (% of seconds)", "episodes",
        "mean edge (bps)", "mean quantity (mm EUR)", "total profit (USD mm)"]].round(4)

# %% [markdown]
# ### Where the deviations sit
#
# The distribution of the edge across all live seconds, and the size of the round trip
# available when one is open.

# %%
live = tri[tri[["synth_bid", "synth_ask", "direct_bid", "direct_ask"]].notna().all(axis=1)]
pd.DataFrame({
    "edge (bps)": live.edge_bps.describe(
        percentiles=[0.01, 0.05, 0.25, 0.5, 0.75, 0.95, 0.99]),
    "edge (bps), open only": live.loc[live.open, "edge_bps"].describe(
        percentiles=[0.01, 0.05, 0.25, 0.5, 0.75, 0.95, 0.99]),
    "quantity (mm EUR), open only": live.loc[live.open, "qty"].describe(
        percentiles=[0.01, 0.05, 0.25, 0.5, 0.75, 0.95, 0.99]),
}).round(4)

# %% [markdown]
# ## Intraday plots
#
# The liquidity measures — spread, BBO depth and price impact — per five-minute
# interval, then the variance and lag-1 autocorrelation of one-minute transaction
# returns per thirty-minute interval, then the ACF of each return series to ten lags.
# The two dotted lines mark the FOMC statement (12:30) and the release of the
# Committee's first-ever rate projections (14:00), both on this date; the clock
# evidence for reading the file's timestamps as Eastern time is in the observations.
# One panel per pair, because the pairs share units but not levels.

# %%
_fomc = {"FOMC 12:30": pd.Timestamp(f"{fx.DATE} 12:30"),
         "SEP 14:00": pd.Timestamp(f"{fx.DATE} 14:00")}
liq5 = {p: plots.liquidity_by_interval(b) for p, b in books.items()}
pd.concat({p: summary(v) for p, v in liq5.items()}, names=["pair", "series"]).round(3)

# %%
plots.plot_liquidity(books, "spread_bps", marks=_fomc, save="fx_spread_bps_5min");

# %%
plots.plot_liquidity(books, "depth", marks=_fomc, save="fx_depth_5min");

# %%
plots.plot_liquidity(books, "lambda_bps", marks=_fomc, save="fx_lambda_bps_5min");

# %% [markdown]
# ### One-minute transaction returns by half hour
#
# Thirty one-minute returns per interval, so the lag-1 autocorrelation is estimated
# on 29 pairs and the dashed lines at ±1.96/√n show how little a single half hour can
# say on its own; the shape across the day is the thing to read.

# %%
ret30 = {p: plots.returns_by_interval(b) for p, b in books.items()}
pd.concat(ret30, axis=1, names=["pair", ""]).round(3)

# %%
plots.plot_returns(books, "variance_bps2", marks=_fomc, save="fx_variance_bps2_30min");

# %%
plots.plot_returns(books, "ac1", marks=_fomc, save="fx_ac1_30min");

# %% [markdown]
# ### Autocorrelation functions
#
# One row per pair, midquote on the left and transaction price on the right; the
# shaded band is ±1.96/√n. Ten lags is enough for every series to have settled.

# %%
for _fq in FREQS:
    plots.plot_acf({f"{p} {s_}": i[p][_fq][s_] for p in books for s_ in ("mid", "trade")},
                   lags=10, save=f"fx_acf_{_fq}", title=f"ACF of {_fq} log returns")

# %% [markdown]
# ## Summary statistics table
#
# The homework asks for one table: every part at the daily level, one column per
# pair. Parts (a)–(g) are the mean of their per-minute values, so each row is the
# average minute; (h)–(k) are whole-session figures at both sampling frequencies.
# `hfstats.headline` builds the same column for the equities, so the two panels stack
# into one table. Levels are comparable within this panel; against the equities only
# the relative rows — spreads in basis points, realized variance, autocorrelations —
# mean the same thing, because sizes here are millions of base currency, not shares.

# %%
table = pd.DataFrame({p: hs.headline(b) for p, b in books.items()})
table.to_csv(plots.FIG_DIR / "summary_currencies.csv")
table.round(4)

# %% [markdown]
# ## Observations
#
# **1. The liquidity ordering is exactly the one the triangle implies.** EUR/USD quotes
# at 0.86 bps, USD/JPY at 1.01, EUR/JPY at 1.84 — the cross costs roughly as much as
# its two legs added together, which is what it should cost if the only way to make a
# price in it is to lay it off through the dollar. The same ordering runs through every
# other measure: EUR/JPY has the thinnest touch (3.5 mm against 4.1 and 4.4), the
# shallowest ten-level book (37 mm against ~50), the largest price impact (0.53 bps
# against 0.22), and the fewest trades (1,542 against 14,535).
#
# **2. The cross is quoted far more than it is traded.** EUR/JPY carries 116 book
# messages per print, against 25 for EUR/USD and 30 for USD/JPY, despite being much the
# quietest pair. A market maker in the cross has to re-quote whenever *either* leg
# moves, so its message traffic is driven by the majors' volatility rather than by its
# own trading — an order-flow cost with no corresponding revenue, and part of why the
# cross is quoted wider.
#
# **3. Bounce and staleness pull the variance in opposite directions.** At a fixed
# frequency, transaction RV exceeds midquote RV (EUR/USD at 1s: 2927 vs 2320 bps²) and
# transaction returns are negatively autocorrelated at lag 1 while midquote returns are
# not — textbook bid–ask bounce. But across frequencies the midquote signature *rises*
# rather than falls, because at one second 43–64% of midquote returns are exactly zero:
# with a one-pip spread and a tenth-pip tick, the EBS mid usually has not moved. The
# high-frequency RV is therefore biased *down* by discreteness, not up by noise, which
# is the opposite of the equity case and worth keeping in mind before quoting any
# one-second variance as an estimate of true volatility.
#
# **4. EUR/JPY's one-minute returns are strongly mean-reverting, and it is not simply
# non-trading.** Its one-minute autocorrelation is −0.17 for the midquote and −0.15 for
# transactions, against essentially zero for both majors (t = −3.4 on 390
# observations). Forward-filling is the obvious suspect, since the cross prints in only
# 79% of minutes against 97–100% for the majors — but that cannot explain the
# *midquote* figure, because EUR/JPY updates its quote 1.5 times a second and only 2.3%
# of its one-minute mid returns are zero. What fits instead is a transitory component in
# the cross's quoted mid: with a spread twice as wide and a book a quarter thinner,
# where the cross's mid sits inside its spread is itself noisy, and that noise reverses.
# The realized variance corroborates it — EUR/JPY's midquote RV *falls* from 2317 bps²
# at one minute to 1655 at five, as the transitory part averages out, while both majors
# rise over the same range. The practical reading is that the cross's mid is a
# materially noisier estimate of its efficient price than either major's, which is the
# same fact as its wider spread seen from the return side.
#
# **5. Triangular arbitrage exists, and is about as small as theory says it should be.**
# Gross deviations appear in 0.39% of seconds across 63 episodes, with a median
# duration of one second (max seven), a mean edge of 0.29 bps and a median round-trip
# size of 0.77 mm EUR — about $3,100 for the whole session, before any cost, assuming
# all three legs fill in full at the quoted price. Charging a few EUR/JPY ticks per
# round trip roughly halves it. The edge is real rather than a sampling artefact: the
# quotes behind an opportunity are *fresher* than average (median stalest leg 1.4 s
# against 2.1 s), which is the opposite of the staleness signature. For scale: across
# all live seconds the median deviation is −1.58 bps, i.e. the triangle normally sits
# inside itself by about the width of the cross's own 1.84 bps spread, and even when it
# does open the mean edge is 0.29 bps — a sixth of that spread — with the largest of the
# day, 1.78 bps, still short of one full spread. The no-arbitrage relation therefore
# holds to within the cost of crossing the cross, and the residual is a latency race
# rather than a return available to a price-taker.
#
# **6. Both the deviations and the volatility cluster in the same window, and the
# window is the FOMC.** The opportunities are concentrated in the first half hour
# (22 of the 91 open seconds) and after 13:00 (53 of 91), which is also where the
# day's largest moves sit — EUR/USD rallies 1.1% over the session, most of it between
# 12:25 and 14:30. The file carries no timezone, but the clock identifies itself: on
# 2012-01-25 the Committee released its first-ever rate projections at 14:00 ET, and
# EUR/USD drops 20 bps in the 14:00–14:01 minute exactly; the statement itself was
# scheduled for 12:30 ET, and the tape goes from zero prints in the 12:26 minute to
# 231 in 12:28 as the mid jumps 17 bps — so the timestamps are Eastern, and the day's
# volatility is the Fed's. The spread plot shows the same thing from the liquidity
# side: EUR/JPY's five-minute spread spikes to 2.6 bps in the 12:25 interval, its
# widest of the day, and impact λ in all three pairs peaks in the 12:30 and 14:00
# intervals. The mechanism is the standard one: the triangle breaks when prices move
# fast enough that the three books adjust at different speeds, so arbitrage frequency
# is a symptom of information arriving, not of a market that is generally mispriced.
#
# **7. The intraday pattern is announcement-shaped rather than U-shaped.** On the
# two majors the spread is narrowest late in the morning (0.68 bps for EUR/USD at
# 10:55, 0.77 for USD/JPY at 11:45) and widens through the afternoon, ending 30–40%
# wider than its morning low; the cross is different again, tightest in the first
# ten minutes and widest — 2.6 bps, twice its mean — in the 12:25 interval that
# contains the pre-statement jump. Depth shows no open-of-day trough at all: the
# thinnest five-minute touch on each pair falls mid-session (11:20, 13:55, 15:40),
# and EUR/JPY's touch is *deepest* at the open. Impact λ peaks on all three pairs in
# the 12:25 and 14:00 intervals, and the half-hourly variance of one-minute returns
# is five to ten times its mid-morning level in the 12:30–13:30 and 14:00–14:30
# windows. FX has no opening auction and no overnight close on a 24-hour market, so
# the equity U-shape has no reason to appear; what shapes this day is the scheduled
# release, and the liquidity measures respond to it the way the models say — spreads
# and impact up, as the adverse-selection component rises when information arrives.
# The lag-1 autocorrelation of one-minute transaction returns is negative in most half
# hours, the bounce signature, but flips to +0.57 for EUR/USD in the 12:00–12:30
# window, where the price trends into the release: at one-minute sampling a fast
# one-directional move reads as momentum, and the sign of the autocorrelation says
# which of the two effects, bounce or drift, is winning in that half hour.
