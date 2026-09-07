"""Homework 1 figures, written once against ``hfstats`` and applied to either panel.

The homework asks for three kinds of plot:

    * the liquidity measures -- spread, depth and price impact -- through the day,
      one point per five-minute interval (``liquidity_by_interval`` / ``plot_liquidity``)
    * the variance and first autocorrelation of one-minute transaction returns,
      one point per thirty-minute interval (``returns_by_interval`` / ``plot_returns``)
    * the autocorrelation function of the return series out to a lag at which it
      has settled at zero (``plot_acf``)

Every plotting function takes ``{label: day}`` and draws one panel per instrument on
a shared clock, because the instruments inside a panel share units (shares or
millions of base currency) but differ in level by orders of magnitude -- NVDA and
GPRO depth, for instance -- and squeezing them onto one axis hides the shape of the
smaller one.  Figures are saved under ``ps1/figures`` when ``save`` is given a name.
"""

from __future__ import annotations

from pathlib import Path

import matplotlib.dates as mdates
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd

import hfstats as hs

FIG_DIR = Path(__file__).resolve().parent.parent / "figures"

# First three slots of a colourblind-validated categorical palette; one per
# instrument, assigned in the order the panel lists them and never re-cycled.
COLORS = ("#2a78d6", "#eb6834", "#1baf7a")
INK, MUTED, GRID = "#0b0b0b", "#52514e", "#e2e1dc"

FIVE_MIN = "5min"
HALF_HOUR = "30min"

plt.rcParams.update({
    "figure.dpi": 110,
    "axes.spines.top": False, "axes.spines.right": False,
    "axes.edgecolor": MUTED, "axes.labelcolor": INK,
    "axes.grid": True, "grid.color": GRID, "grid.linewidth": 0.6, "axes.axisbelow": True,
    "xtick.color": MUTED, "ytick.color": MUTED,
    "font.size": 9, "axes.titlesize": 10, "legend.frameon": False,
})


# --------------------------------------------------------------------------- data


def liquidity_by_interval(day, freq: str = FIVE_MIN, min_trades: int = 10) -> pd.DataFrame:
    """Spread (bps), BBO depth and price impact, one row per ``freq`` interval.

    Spread and depth are time-weighted means of the end-of-second snapshots inside
    the interval; impact is the part (g) regression re-estimated on the interval's
    prints, so with five minutes of trades it identifies far more often than the
    per-minute version does.
    """
    g = day.grid
    out = pd.DataFrame({
        "spread_bps": (1e4 * g.spread / g.mid).resample(freq).mean(),
        "depth": (g.bid_sz + g.ask_sz).resample(freq).mean(),
    })
    imp = hs.price_impact(day, freq=freq, min_trades=min_trades)
    out["lambda_bps"] = imp.lambda_bps.reindex(out.index)
    out["n_trades"] = imp.n_trades.reindex(out.index).fillna(0).astype(int)
    return out


def returns_by_interval(day, freq: str = HALF_HOUR, series: str = "trade") -> pd.DataFrame:
    """Variance and lag-1 autocorrelation of one-minute log returns, per ``freq``.

    Each thirty-minute interval holds thirty one-minute returns, so the lag-1
    autocorrelation is estimated on 29 pairs and its standard error is roughly
    0.19; the dashed band on the plot marks that.  Variance is in bps^2.
    """
    r = hs.log_returns(hs.price_series(day, hs.MINUTE))[series]
    grp = r.groupby(pd.Grouper(freq=freq))
    return pd.DataFrame({
        "variance_bps2": 1e8 * grp.var(),
        "ac1": grp.apply(lambda x: x.autocorr(1)),
        "n": grp.count(),
    })


# --------------------------------------------------------------------------- drawing


def _clock(ax, index):
    """Format an intraday x-axis as HH:MM over the session."""
    ax.xaxis.set_major_locator(mdates.HourLocator())
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%H:%M"))
    ax.set_xlim(index[0], index[-1] + (index[1] - index[0]))


def _panels(n, height=2.2, width=9):
    fig, axes = plt.subplots(n, 1, figsize=(width, height * n + 0.6), sharex=True,
                             constrained_layout=True)
    return fig, np.atleast_1d(axes)


def _step(ax, s: pd.Series, color: str, label: str | None = None):
    """A step line, drawn so each interval's value spans that interval."""
    ax.step(s.index, s.to_numpy(), where="post", color=color, lw=1.6, label=label)


def _marks(ax, marks: dict[str, pd.Timestamp] | None):
    for name, when in (marks or {}).items():
        ax.axvline(when, color=MUTED, lw=0.8, ls=":")
        ax.annotate(name, (when, 1.0), xycoords=("data", "axes fraction"),
                    xytext=(3, -2), textcoords="offset points", fontsize=7,
                    color=MUTED, va="top")


def plot_liquidity(days: dict, variable: str, freq: str = FIVE_MIN, marks=None,
                   save: str | None = None):
    """One figure for one liquidity measure: a panel per instrument through the day.

    ``variable`` is ``"spread_bps"``, ``"depth"`` or ``"lambda_bps"``.
    """
    label = {"spread_bps": "quoted spread (bps)",
             "depth": "BBO depth (bid + ask)",
             "lambda_bps": "5s price impact  λ  (bps)"}[variable]
    fig, axes = _panels(len(days))
    for ax, (name, day), color in zip(axes, days.items(), COLORS):
        s = liquidity_by_interval(day, freq)[variable]
        _step(ax, s, color)
        if variable == "lambda_bps":
            ax.axhline(0, color=MUTED, lw=0.6)
        ax.set_title(name, loc="left", color=INK)
        _marks(ax, marks)
        _clock(ax, s.index)
    axes[-1].set_xlabel("interval start")
    fig.suptitle(f"{label}, per {freq} interval", x=0.01, ha="left", color=INK)
    return _finish(fig, save)


def plot_returns(days: dict, variable: str, freq: str = HALF_HOUR, marks=None,
                 save: str | None = None):
    """Variance or lag-1 autocorrelation of 1-min transaction returns, per interval."""
    label = {"variance_bps2": "variance of 1-min transaction log returns (bps²)",
             "ac1": "lag-1 autocorrelation of 1-min transaction log returns"}[variable]
    fig, axes = _panels(len(days))
    for ax, (name, day), color in zip(axes, days.items(), COLORS):
        d = returns_by_interval(day, freq)
        s = d[variable]
        x = s.index + (s.index[1] - s.index[0]) / 2          # centre the bar on the interval
        ax.bar(x, s.to_numpy(), width=(s.index[1] - s.index[0]) * 0.86, color=color,
               linewidth=0, zorder=2)
        if variable == "ac1":
            se = 1 / np.sqrt(d.n.clip(lower=1))
            ax.plot(x, 1.96 * se, color=MUTED, lw=0.8, ls="--")
            ax.plot(x, -1.96 * se, color=MUTED, lw=0.8, ls="--")
            ax.axhline(0, color=MUTED, lw=0.6)
        ax.set_title(name, loc="left", color=INK)
        _marks(ax, marks)
        _clock(ax, s.index)
    axes[-1].set_xlabel("interval start")
    fig.suptitle(f"{label}, per {freq} interval", x=0.01, ha="left", color=INK)
    return _finish(fig, save)


def plot_acf(returns: dict[str, pd.Series], lags: int = 10, ncols: int = 2,
             save: str | None = None, title: str | None = None):
    """Sample ACF of each series in ``returns`` (``{label: series}``) to ``lags``.

    The shaded band is +/- 1.96 / sqrt(n), the usual pointwise band under the null
    of no autocorrelation; the homework suggests a lag length at which the ACF has
    settled at zero, and ten is enough for every series here.  Panels are laid out
    one instrument per row -- so with ``ncols = 2`` pass the midquote and transaction
    series of each instrument consecutively -- and colour follows the row.
    """
    from statsmodels.tsa.stattools import acf

    n = len(returns)
    nrows = int(np.ceil(n / ncols))
    fig, axes = plt.subplots(nrows, ncols, figsize=(4.4 * ncols, 2.4 * nrows + 0.5),
                             sharey=True, constrained_layout=True)
    axes = np.atleast_1d(axes).ravel()
    for i, (ax, (name, r)) in enumerate(zip(axes, returns.items())):
        color = COLORS[(i // ncols) % len(COLORS)]
        r = r.dropna()
        rho = acf(r, nlags=lags, fft=True)[1:]
        band = 1.96 / np.sqrt(len(r))
        ax.axhspan(-band, band, color=GRID, alpha=0.6, lw=0, zorder=0)
        ax.bar(np.arange(1, lags + 1), rho, width=0.6, color=color, linewidth=0, zorder=2)
        ax.axhline(0, color=MUTED, lw=0.6, zorder=1)
        ax.set_title(f"{name}  (n = {len(r):,})", loc="left", color=INK)
        ax.set_xticks(np.arange(1, lags + 1))
        ax.set_xlabel("lag")
    for ax in axes[n:]:
        ax.set_visible(False)
    if title:
        fig.suptitle(title, x=0.01, ha="left", color=INK)
    return _finish(fig, save)


def _finish(fig, save):
    if save:
        FIG_DIR.mkdir(parents=True, exist_ok=True)
        fig.savefig(FIG_DIR / f"{save}.png", dpi=160, bbox_inches="tight")
    return fig
