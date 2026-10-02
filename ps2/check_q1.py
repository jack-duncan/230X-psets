"""Independent re-computation of HW2 Section 1 from the PS1 ITCH caches."""
import sys
import numpy as np
import pandas as pd

sys.path.insert(0, "ps1/notebooks")
import mbo  # noqa: E402

K = 2
OPEN, CLOSE = "09:30", "16:00"


def tsrv(x):
    """Eq. (2) of the assignment; x = log-mid series inside one interval."""
    m = len(x)
    n = m - 1                      # one-tick returns
    if n < K + 1:
        return np.nan, n
    r1 = np.diff(x)
    rk = x[K:] - x[:-K]            # (n-K+1) K-tick returns
    xx_k = (rk ** 2).sum() / K
    xx_all = (r1 ** 2).sum()
    z = (n - K + 1) / K
    return (1 - z / n) ** -1 * (xx_k - z / n * xx_all), n


def build_series(day):
    q = day.quotes.between_time(OPEN, CLOSE, inclusive="left")
    q = q.dropna(subset=["bid", "ask"])
    q = q[(q.bid > 0) & (q.ask > 0) & (q.bid <= q.ask)]           # drop crossed, keep locked
    chg = (q.bid.diff() != 0) | (q.ask.diff() != 0)
    chg.iloc[0] = True
    q = q[chg]                                                      # collapse repeated quotes
    x = 0.5 * (np.log(q.bid.values) + np.log(q.ask.values))
    s = pd.Series(x, index=q.index)
    r = np.diff(x)
    rho1 = np.corrcoef(r[:-1], r[1:])[0, 1]
    n_mid = len(np.unique(np.round((q.bid.values + q.ask.values) / 2, 6)))
    return s, rho1, n_mid, len(day.quotes.between_time(OPEN, CLOSE, inclusive="left"))


def interval_table(s):
    bins = pd.date_range(s.index[0].normalize() + pd.Timedelta("9h30min"),
                         periods=40, freq="10min", tz=s.index.tz)
    rows = []
    for a, b in zip(bins[:-1], bins[1:]):
        x = s[(s.index >= a) & (s.index < b)].values
        v, n = tsrv(x)
        rows.append({"start": a, "n": n, "tsrv_raw": v})
    df = pd.DataFrame(rows).set_index("start")
    df["neg"] = df.tsrv_raw < 0
    df["rv10"] = np.sqrt(df.tsrv_raw.clip(lower=0))
    df["rv30"] = df.rv10.rolling(3).mean()
    df["rv1h"] = df.rv10.rolling(6).mean()
    return df


def hc0_ols(X, y):
    X = np.column_stack([np.ones(len(X)), X])
    beta, *_ = np.linalg.lstsq(X, y, rcond=None)
    e = y - X @ beta
    XtX_inv = np.linalg.inv(X.T @ X)
    meat = X.T @ (X * e[:, None] ** 2)
    se = np.sqrt(np.diag(XtX_inv @ meat @ XtX_inv))
    r2 = 1 - (e ** 2).sum() / ((y - y.mean()) ** 2).sum()
    return beta, beta / se, r2


def oos(beta, X, y, ybar_train):
    X = np.column_stack([np.ones(len(X)), X])
    yhat = X @ beta
    e = y - yhat
    return {"rmse": np.sqrt((e ** 2).mean()) * 1e4, "mae": np.abs(e).mean() * 1e4,
            "r2oos": 1 - (e ** 2).sum() / ((y - ybar_train) ** 2).sum(),
            "rho": np.corrcoef(yhat, y)[0, 1], "neg": int((yhat < 0).sum()),
            "fc_mean": yhat.mean() * 1e4, "act_mean": y.mean() * 1e4}


days = mbo.load("NVDA", "GPRO")
for sym, day in days.items():
    s, rho1, n_mid, n_msgs = build_series(day)
    df = interval_table(s)
    print(f"\n===== {sym} =====")
    print(f"top-of-book rows 09:30-16:00: {n_msgs:,}   distinct quotes: {len(s):,}   "
          f"ratio {n_msgs/len(s):.1f}   distinct mids: {n_mid:,}   rho1(tick r) = {rho1:+.3f}")
    print(f"bins: {len(df)}   ticks/interval median {df.n.median():.0f} min {df.n.min()} max {df.n.max()}")
    rv = df.rv10 * 1e4
    print(f"RV10 bps: mean {rv.mean():.4f} sd {rv.std(ddof=1):.4f} (ddof0 {rv.std(ddof=0):.4f}) "
          f"min {rv.min():.4f} max {rv.max():.4f}   neg TSRV: {df.neg.sum()}")
    print(f"first {rv.iloc[0]:.1f} bps; argmin at {rv.idxmin().strftime('%H:%M')}")
    daily = np.sqrt(df.tsrv_raw.clip(lower=0).sum())
    g = day.grid.between_time(OPEN, CLOSE, inclusive="left").mid.dropna()
    daily_1s = np.sqrt((np.diff(np.log(g.values)) ** 2).sum())
    print(f"daily vol: sum TSRV {daily*100:.2f}%   1-second mid RV {daily_1s*100:.2f}%")

    # ---- forecasting sample
    y = df.rv10.shift(-1)
    data = pd.DataFrame({"y": y, "rv10": df.rv10, "rv30": df.rv30, "rv1h": df.rv1h})
    for j in range(1, 6):
        data[f"lag{j}"] = df.rv10.shift(j)
    data = data.dropna()
    ntr = int(round(len(data) * 2 / 3))
    tr, te = data.iloc[:ntr], data.iloc[ntr:]
    print(f"usable {len(data)} ({data.index[0].strftime('%H:%M')}..{data.index[-1].strftime('%H:%M')}) "
          f"train {len(tr)} (to {tr.index[-1].strftime('%H:%M')}) test {len(te)} "
          f"({te.index[0].strftime('%H:%M')}..{te.index[-1].strftime('%H:%M')})")
    print(f"train zeros (GPRO): {(tr.rv10 == 0).sum()}   train mean y {tr.y.mean()*1e4:.1f} bps "
          f"test mean y {te.y.mean()*1e4:.1f} bps")

    mh = ["rv10", "rv30", "rv1h"]
    b, t, r2 = hc0_ols(tr[mh].values, tr.y.values)
    print("Multi-horizon: c,b10,b30,bh =", np.round(b, 4), " t =", np.round(t, 2), f" R2in {r2:.4f}")
    print("   OOS:", {k: round(v, 4) if isinstance(v, float) else v
                      for k, v in oos(b, te[mh].values, te.y.values, tr.y.mean()).items()})

    ar = ["rv10", "lag1", "lag2", "lag3", "lag4", "lag5"]
    b, t, r2 = hc0_ols(tr[ar].values, tr.y.values)
    print("AR(6): c,phi1..6 =", np.round(b, 4), " t =", np.round(t, 2), f" R2in {r2:.4f}")
    print("   OOS:", {k: round(v, 4) if isinstance(v, float) else v
                      for k, v in oos(b, te[ar].values, te.y.values, tr.y.mean()).items()})
