# ---
# jupyter:
#   jupytext:
#     formats: py:percent,ipynb
#     text_representation:
#       extension: .py
#       format_name: percent
#       format_version: '1.3'
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
from statsmodels.stats.diagnostic import acorr_ljungbox
from statsmodels.tsa.stattools import acf, pacf

import mbo

MINUTE = "1min"
FREQS = ("1s", MINUTE)

# The Nasdaq opening cross prints once, unsigned, and is ~30x a normal trade (671k
# shares for NVDA). Left in, it dominates the first minute of every trade statistic.
INCLUDE_CROSS = False

# Approximate, from the latest filings — the only input here that is not in the data.
SHARES_OUT = {"NVDA": 24.4e9, "GPRO": 0.16e9}

days = mbo.load("NVDA", "GPRO")
nvda, gpro = days["NVDA"], days["GPRO"]


def tape(day):
    """The trade tape used throughout, with or without the opening cross."""
    return day.trades if INCLUDE_CROSS else day.trades[~day.trades.is_cross]


def summary(df):
    """Descriptive statistics, one row per variable."""
    return df.describe().T


nvda, gpro

# %% [markdown]
# ## Choice of stocks
#
# Size shows up in the data long before we reach the statistics: NVDA trades ~12x as
# often, but GPRO's quoted spread is ~90x wider in relative terms.

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
def dollar_volume_per_minute(day):
    """Part (a): dollar volume traded in each minute of the session."""
    return tape(day)[["value"]].resample(MINUTE).sum().rename(
        columns={"value": "dollar_volume"})


# %%
a_nvda = dollar_volume_per_minute(nvda)
summary(a_nvda)

# %%
a_gpro = dollar_volume_per_minute(gpro)
summary(a_gpro)

# %% [markdown]
# ## Part (b) — number of trades and number of orders per minute
#
# Trades are `T` prints; orders are `A` (add-order) messages, which only a full-depth
# feed like ITCH exposes. Cancels are shown alongside — the ratio of cancels to adds is
# the usual read on how much of the book is fleeting.

# %%
def counts_per_minute(day):
    """Part (b): trade prints, new orders and cancels arriving in each minute."""
    return pd.DataFrame({
        "trades": tape(day).price.resample(MINUTE).count().reindex(
            day.events.index, fill_value=0),
        "orders": day.events.adds,
        "cancels": day.events.cancels,
    })


# %%
b_nvda = counts_per_minute(nvda)
summary(b_nvda)

# %%
b_gpro = counts_per_minute(gpro)
summary(b_gpro)

# %% [markdown]
# ## Part (c) — open, close, high and low prices per minute
#
# Trade-price OHLC. Minutes with no prints come back as `NaN` rather than being
# forward-filled, so the counts show how often each name actually trades.

# %%
def ohlc_per_minute(day):
    """Part (c): open / high / low / close of the trade price in each minute."""
    return tape(day).price.resample(MINUTE).ohlc()


# %%
c_nvda = ohlc_per_minute(nvda)
summary(c_nvda)

# %%
c_gpro = ohlc_per_minute(gpro)
summary(c_gpro)

# %% [markdown]
# ## Part (d) — VWAP per minute
#
# Size-weighted average trade price, `sum(p*q) / sum(q)` within the minute.

# %%
def vwap_per_minute(day):
    """Part (d): volume-weighted average trade price in each minute."""
    g = tape(day).resample(MINUTE)
    return (g.value.sum() / g["size"].sum()).to_frame("vwap")


# %%
d_nvda = vwap_per_minute(nvda)
summary(d_nvda)

# %%
d_gpro = vwap_per_minute(gpro)
summary(d_gpro)

# %% [markdown]
# ## Part (e) — BBO spread and depth per minute
#
# Averaged over end-of-second book snapshots, so every second carries equal weight —
# a time-weighted average, rather than one weighted by how often each name requotes.

# %%
def spread_depth_per_minute(day):
    """Part (e): quoted spread and displayed size at the BBO, per minute."""
    g = day.grid
    return pd.DataFrame({
        "spread": g.spread,
        "spread_bps": 1e4 * g.spread / g.mid,
        "bid_sz": g.bid_sz,
        "ask_sz": g.ask_sz,
        "depth": g.bid_sz + g.ask_sz,
    }).resample(MINUTE).mean()


# %%
e_nvda = spread_depth_per_minute(nvda)
summary(e_nvda)

# %%
e_gpro = spread_depth_per_minute(gpro)
summary(e_gpro)

# %% [markdown]
# ## Part (f) — depth within twice the day's average spread
#
# Shares resting within one average spread either side of the mid, i.e. inside a band
# of **twice** the day's mean quoted spread. The best bid sits half a spread from the
# mid, so the band is empty exactly when the prevailing spread exceeds twice its
# average — `frac_empty` reports how often that happens. Note the degenerate case: if a
# name's spread is always one tick, the band reaches no further than the BBO and (f)
# just reproduces the depth from (e).

# %%
def band_depth_per_minute(day):
    """Part (f): resting size inside +/- one average spread of the mid, per minute."""
    half_width = day.grid.spread.mean()               # half of "twice the average spread"
    k = int(np.floor(half_width / 0.005))             # profile buckets are half a cent wide
    inside = day.depth_bid[:, :k + 1].sum(1) + day.depth_ask[:, :k + 1].sum(1)
    inside = pd.Series(inside, index=day.grid.index)

    out = pd.DataFrame({
        "band_depth": inside.resample(MINUTE).mean(),
        "frac_empty": inside.eq(0).resample(MINUTE).mean(),
    })
    out.attrs["band"] = 2 * half_width
    return out


# %%
f_nvda = band_depth_per_minute(nvda)
print(f"band = ${f_nvda.attrs['band']:.4f} wide")
summary(f_nvda)

# %%
f_gpro = band_depth_per_minute(gpro)
print(f"band = ${f_gpro.attrs['band']:.4f} wide")
summary(f_gpro)

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
# halved — computed here in closed form rather than by looping OLS over 390 minutes.

# %%
def price_impact_per_minute(day, horizon="5s", min_trades=10):
    """Part (g): per-minute slope of the 5s midquote return (bps) on trade sign."""
    tr = tape(day).dropna(subset=["mid"])
    q = day.quotes.dropna(subset=["mid"])
    q = q[~q.index.duplicated(keep="last")].mid

    ahead = tr.index + pd.Timedelta(horizon)
    fwd = q.reindex(ahead, method="ffill").to_numpy(copy=True)
    fwd[ahead > q.index[-1]] = np.nan          # prints with no full horizon left in the day

    df = pd.DataFrame({"ret": 1e4 * np.log(fwd / tr.mid.to_numpy()),
                       "sign": tr["sign"].to_numpy().astype(float)}, index=tr.index)
    df["cross"] = df.ret * df["sign"]

    g = df.groupby(pd.Grouper(freq=MINUTE))
    m, n = g.mean(), g.ret.count()
    # slope = cov(ret, sign) / var(sign), and var(sign) = 1 - mean(sign)^2 since sign^2 = 1
    var_sign = 1 - m["sign"] ** 2
    lam = (m.cross - m.ret * m["sign"]) / var_sign
    ok = (n >= min_trades) & (var_sign > 1e-9)     # need both buys and sells to identify it
    return pd.DataFrame({
        "lambda_bps": lam.where(ok),
        "alpha_bps": (m.ret - lam * m["sign"]).where(ok),
        "n_trades": n,
    })


# %%
g_nvda = price_impact_per_minute(nvda)
summary(g_nvda)

# %%
g_gpro = price_impact_per_minute(gpro)
summary(g_gpro)

# %% [markdown]
# ## Part (h) — one-second and one-minute midquote and transaction price series
#
# The last quote and the last print in each interval. Intervals with no trade inherit
# the previous price, so the two series stay on a common clock.

# %%
def price_series(day, freq):
    """Part (h): last midquote and last trade price in each interval of `freq`."""
    return pd.DataFrame({
        "mid": day.grid.mid.resample(freq).last(),
        "trade": tape(day).price.resample(freq).last(),
    }).ffill().dropna()


# %%
h_nvda = {f: price_series(nvda, f) for f in FREQS}
pd.concat({f: summary(p) for f, p in h_nvda.items()}, names=["freq", "series"])

# %%
h_gpro = {f: price_series(gpro, f) for f in FREQS}
pd.concat({f: summary(p) for f, p in h_gpro.items()}, names=["freq", "series"])

# %% [markdown]
# ## Part (i) — log returns
#
# $r_t = \log(p_t) - \log(p_{t-1})$, for both the midquote and the transaction series.

# %%
def log_returns(prices):
    """Part (i): first differences of log prices."""
    return np.log(prices).diff().dropna()


# %%
i_nvda = {f: log_returns(p) for f, p in h_nvda.items()}
pd.concat({f: summary(1e4 * r).rename(index="{} (bps)".format)
           for f, r in i_nvda.items()}, names=["freq", "series"])

# %%
i_gpro = {f: log_returns(p) for f, p in h_gpro.items()}
pd.concat({f: summary(1e4 * r).rename(index="{} (bps)".format)
           for f, r in i_gpro.items()}, names=["freq", "series"])

# %% [markdown]
# ## Part (j) — realized variance
#
# $RV = \sum_t r_t^2$ over the session, for each series and sampling frequency. Gaps
# between the midquote and transaction figures, and between the two frequencies, are
# where microstructure noise shows up — the finer the sampling, and the closer to the
# traded price rather than the mid, the more of the variance is noise rather than signal.

# %%
def realized_variance(returns):
    """Part (j): summed squared returns, with the daily and annualised volatility."""
    rv = (returns ** 2).sum()
    return pd.DataFrame({
        "n": returns.count(),
        "realized_var": rv,
        "realized_vol": np.sqrt(rv),
        "annualized_vol": np.sqrt(252 * rv),
    })


# %%
pd.concat({f: realized_variance(r) for f, r in i_nvda.items()}, names=["freq", "series"])

# %%
pd.concat({f: realized_variance(r) for f, r in i_gpro.items()}, names=["freq", "series"])

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
def autocorrelation(returns, lags=10):
    """Part (k): Box-Pierce Q with its p-value, plus the ACF and PACF at each lag."""
    out = {}
    for col in returns:
        r = returns[col].dropna()
        bp = acorr_ljungbox(r, lags=lags, boxpierce=True)
        out[col, "acf"] = acf(r, nlags=lags, fft=True)[1:]
        out[col, "pacf"] = pacf(r, nlags=lags)[1:]
        out[col, "bp_stat"] = bp["bp_stat"].to_numpy()
        out[col, "bp_pvalue"] = bp["bp_pvalue"].to_numpy()
    return pd.DataFrame(out, index=pd.RangeIndex(1, lags + 1, name="lag"))


# %%
pd.concat({f: autocorrelation(r) for f, r in i_nvda.items()}, names=["freq", "lag"])

# %%
pd.concat({f: autocorrelation(r) for f, r in i_gpro.items()}, names=["freq", "lag"])
