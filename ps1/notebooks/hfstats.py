"""Homework 1 parts (a)-(k), written once and applied to either panel.

Every function takes a reconstructed day -- ``mbo.Day`` for the Nasdaq stocks or
``fx.Book`` for the EBS currency pairs -- and returns a frame indexed by the interval
it is computed over.  The two classes expose the same fields (``trades``, ``quotes``,
``grid``, ``events``, ``depth_bid``, ``depth_ask``), so nothing here needs to know
which market it is looking at.

The units do differ, and no function tries to hide that:

    * equities -- price in dollars, size in shares, ``value`` in dollars
    * currencies -- price in the quote currency, size in millions of the base
      currency, ``value`` in millions of USD

so levels are comparable within a panel and only relative measures (spreads in basis
points, autocorrelations, normalised impact) are comparable across them.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
from statsmodels.stats.diagnostic import acorr_ljungbox
from statsmodels.tsa.stattools import acf, pacf

MINUTE = "1min"
FREQS = ("1s", MINUTE)


def tape(day) -> pd.DataFrame:
    """The trade tape used throughout, with the Nasdaq opening cross removed.

    The cross prints once, unsigned, and is ~30x a normal trade (671k shares for
    NVDA); left in it dominates the first minute of every trade statistic.  The EBS
    tape has no such print and is returned unchanged.
    """
    tr = day.trades
    return tr[~tr.is_cross] if "is_cross" in tr.columns else tr


def bucket_width(day) -> float:
    """Width of one depth-profile bucket, in price units.

    Both reconstructions bin resting size by distance from the mid in half-ticks, so
    this is half the market's tick: half a cent for the stocks, half a tenth-of-a-pip
    for the currencies.
    """
    return getattr(day, "tick", 0.01) / 2


def summary(df: pd.DataFrame) -> pd.DataFrame:
    """Descriptive statistics, one row per variable."""
    return df.describe().T


# --------------------------------------------------------------------------- (a)-(g)


def dollar_volume_per_minute(day) -> pd.DataFrame:
    """Part (a): dollar volume traded in each minute of the session."""
    return tape(day)[["value"]].resample(MINUTE).sum().rename(
        columns={"value": "dollar_volume"})


def counts_per_minute(day) -> pd.DataFrame:
    """Part (b): trade prints and order-flow messages arriving in each minute.

    ITCH gives true order counts -- ``adds`` are new orders, ``cancels`` their
    removal, and the ratio between them is the usual read on how much of the book is
    fleeting.  EBS publishes price-level updates instead, so its ``updates`` and
    ``deletes`` are message counts that net together an unknown number of dealer
    actions.  The homework asks for order counts on ITCH only, for exactly this
    reason: the two are comparable in shape through the day, not in level.
    """
    ev = day.events
    out = {"trades": tape(day).price.resample(MINUTE).count().reindex(
        ev.index, fill_value=0)}
    out.update({c: ev[c] for c in ev.columns if c != "trades"})
    return pd.DataFrame(out)


def ohlc_per_minute(day) -> pd.DataFrame:
    """Part (c): open / high / low / close of the trade price in each minute.

    Minutes with no prints stay ``NaN`` rather than being forward-filled, so the
    counts show how often each instrument actually trades.
    """
    return tape(day).price.resample(MINUTE).ohlc()


def ohlc_daily(day) -> pd.Series:
    """Part (c): the session's open, high, low and close."""
    px = tape(day).price
    return pd.Series({"open": px.iloc[0], "high": px.max(),
                      "low": px.min(), "close": px.iloc[-1]}, name=day.symbol)


def vwap_per_minute(day) -> pd.DataFrame:
    """Part (d): volume-weighted average trade price in each minute."""
    g = tape(day).resample(MINUTE)
    return (g.value.sum() / g["size"].sum()).to_frame("vwap")


def spread_depth_per_minute(day) -> pd.DataFrame:
    """Part (e): quoted spread and displayed size at the BBO, per minute.

    Averaged over end-of-second book snapshots, so every second carries equal weight
    -- a time-weighted average, rather than one weighted by how often each instrument
    requotes.  Where the feed publishes a fixed-depth book (EBS, ten levels a side)
    the whole visible book is reported alongside the touch.
    """
    g = day.grid
    out = {
        "spread": g.spread,
        "spread_bps": 1e4 * g.spread / g.mid,
        "bid_sz": g.bid_sz,
        "ask_sz": g.ask_sz,
        "depth": g.bid_sz + g.ask_sz,
    }
    if "bid_tot" in g.columns:
        out["book_depth"] = g.bid_tot + g.ask_tot
    return pd.DataFrame(out).resample(MINUTE).mean()


def band_depth_per_minute(day) -> pd.DataFrame:
    """Part (f): resting size inside +/- one average spread of the mid, per minute.

    The band is *twice* the day's mean quoted spread wide, so it reaches one average
    spread either side of the mid.  The best bid sits half the prevailing spread from
    the mid, so the band is empty exactly when that spread exceeds twice its average
    -- ``frac_empty`` reports how often that happens, which is the behaviour the
    homework's parenthetical describes.

    Note the degenerate case: for an instrument whose spread is nearly always one
    tick, the band reaches no further than the touch and (f) simply reproduces (e).
    """
    half_width = day.grid.spread.mean()               # half of "twice the average spread"
    k = int(np.floor(half_width / bucket_width(day)))
    k = min(k, day.depth_bid.shape[1] - 1)
    inside = day.depth_bid[:, :k + 1].sum(1) + day.depth_ask[:, :k + 1].sum(1)
    inside = pd.Series(inside, index=day.grid.index).where(day.grid.mid.notna())

    out = pd.DataFrame({
        "band_depth": inside.resample(MINUTE).mean(),
        "frac_empty": inside.eq(0).resample(MINUTE).mean(),
    })
    out.attrs["band"] = 2 * half_width
    return out


def price_impact_per_minute(day, horizon="5s", min_trades=10) -> pd.DataFrame:
    """Part (g): per-minute slope of the 5s midquote return (bps) on trade sign.

    Within each minute, regress the midquote return over the ``horizon`` following
    each print on the sign of that print,

        1e4 * log(m[t+5s] / m[t]) = alpha + lambda * s[t],   s[t] in {-1, +1}

    so ``lambda`` is the midquote move a buy pushes through, in basis points.  Because
    the regressor is binary the slope is just the difference in mean response between
    buys and sells, halved, which is computed here in closed form rather than by
    looping OLS over 390 minutes.
    """
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


# --------------------------------------------------------------------------- (h)-(k)


def price_series(day, freq: str) -> pd.DataFrame:
    """Part (h): last midquote and last trade price in each interval of ``freq``.

    Intervals with no trade inherit the previous price, so the two series stay on a
    common clock.
    """
    return pd.DataFrame({
        "mid": day.grid.mid.resample(freq).last(),
        "trade": tape(day).price.resample(freq).last(),
    }).ffill().dropna()


def log_returns(prices: pd.DataFrame) -> pd.DataFrame:
    """Part (i): first differences of log prices."""
    return np.log(prices).diff().dropna()


def realized_variance(returns: pd.DataFrame) -> pd.DataFrame:
    """Part (j): summed squared returns, with the daily and annualised volatility."""
    rv = (returns ** 2).sum()
    return pd.DataFrame({
        "n": returns.count(),
        "realized_var": rv,
        "realized_vol": np.sqrt(rv),
        "annualized_vol": np.sqrt(252 * rv),
    })


def autocorrelation(returns: pd.DataFrame, lags: int = 10) -> pd.DataFrame:
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


# --------------------------------------------------------------------------- roll-up


def describe_day(day) -> dict:
    """One row of headline statistics, for the cross-instrument summary table."""
    tr = tape(day)
    g = day.grid
    return {
        "trades": len(tr),
        "volume": tr["size"].sum(),
        "dollar volume": tr.value.sum(),
        "quote updates": len(day.quotes),
        "mean spread": g.spread.mean(),
        "mean spread (bps)": (1e4 * g.spread / g.mid).mean(),
        "mean BBO depth": (g.bid_sz + g.ask_sz).mean(),
        "open": tr.price.iloc[0],
        "close": tr.price.iloc[-1],
        "high": tr.price.max(),
        "low": tr.price.min(),
    }
