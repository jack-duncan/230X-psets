"""Order-book reconstruction from EBS currency data (EUR/USD, USD/JPY, EUR/JPY).

The two raw files are wide: one row per feed message, with a block of columns per
currency pair and only the pairs that actually updated on that row populated (a row
can carry one, two or three pairs).  This module turns them into the same objects
``mbo.py`` produces for the equities, so the two panels can be analysed side by side:

    * ``trades``  -- the trade tape, signed, with the quote prevailing before the print
    * ``quotes``  -- the BBO, one row per change in (bid, ask, bid size, ask size)
    * ``grid``    -- the book sampled at the end of every second, plus a profile of
                     resting depth by distance from the mid

What the EBS feed is, and how that differs from ITCH:

    * Messages are **price-level updates, not individual orders**.  A row says "this
      side, this price, now holds this much"; ``SIZE = 0`` removes the level.  There
      are therefore no order ids, no adds and cancels to count separately, and no way
      to see an individual dealer's order -- ``NUM_PARTCP`` gives only how many quotes
      make up the level.
    * The book is **capped at ten levels a side**.  Depth beyond the tenth level is
      not merely absent from the file, it is unobservable, so any depth measure here
      is a lower bound in a way the ITCH measures are not.
    * Timestamps are stamped to **100 ms**, against nanoseconds for ITCH, and many
      updates share a timestamp.  The book is therefore snapshotted once per distinct
      timestamp, after the whole batch of updates carrying it has been applied --
      applying them one at a time would manufacture crossed books that the feed never
      showed.
    * The trade file is a **separate feed that lags the book**: at a print's own
      timestamp the book has already moved to reflect it.  See ``LAG`` below.

Sizes are in millions of the base currency throughout (EBS quotes in millions, and
every size in the file is a whole number of them).  Prices are held as integer ticks
so that book keys are exact.
"""

from __future__ import annotations

from array import array
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pandas as pd

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
ORDER_FILE = DATA_DIR / "ORDER CURRENCY.csv"
TRADE_FILE = DATA_DIR / "TRADE CURRENCY.csv"
CACHE_DIR = DATA_DIR / "cache"

DATE = "2012-01-25"
OPEN = pd.Timestamp(f"{DATE} 09:30")
CLOSE = pd.Timestamp(f"{DATE} 16:00")

# Tick size per pair, i.e. the price increment the feed quotes on.  EBS carried five
# decimals on EUR/USD and three on the yen crosses in 2012, so a tick is a tenth of a
# pip on all three.
TICK = {"EUR/USD": 1e-5, "USD/JPY": 1e-3, "EUR/JPY": 1e-3}
PAIRS = tuple(TICK)

# The base currency of each pair -- the currency its SIZE is denominated in.
BASE = {"EUR/USD": "EUR", "USD/JPY": "USD", "EUR/JPY": "EUR"}

MILLION = 1_000_000
NS = 1_000_000_000
N_SEC = int((CLOSE - OPEN).total_seconds())
N_BUCKETS = 101              # depth profile: 0..100 half-ticks (50 ticks) from the mid

# How far back to look for the quote "prevailing" at a print.  The trade feed is
# published separately from the book and arrives after it: matched at zero lag only
# 47% of EUR/USD prints sit at or outside the touch and the mean buy-minus-sell price
# difference is 0.33 spreads, which would be absurd for a market with no hidden
# liquidity.  Backing the match up moves both sharply -- 85% and 0.91 spreads at
# 200 ms -- and the optimum is 200 ms on all three pairs independently.  Anything from
# 100 ms to 2 s gives much the same answer; the choice is not delicate.
LAG = pd.Timedelta("200ms")

_COLS = ["BUY_SELL_FLAG", "PRICE", "SIZE"]


# --------------------------------------------------------------------------- load


def _read(path: Path, pair: str, extra: tuple[str, ...] = ()) -> dict[str, np.ndarray]:
    """Slice one pair out of a wide EBS file as plain numpy arrays.

    Only the rows where this pair actually carries a price are kept; prices become
    integer ticks and sizes whole millions of the base currency.
    """
    cols = [*_COLS, *extra]
    usecols = ["Time"] + [f"EBS_BOOK::{pair}.{c}" for c in cols]
    df = pd.read_csv(path, usecols=usecols, low_memory=False)
    df.columns = ["Time"] + cols if df.columns[0] == "Time" else df.columns
    df = df.rename(columns={f"EBS_BOOK::{pair}.{c}": c for c in cols})
    df = df[df["PRICE"].notna()]

    ts = pd.to_datetime(df["Time"], format="%Y/%m/%d %H:%M:%S.%f")
    out = {
        "ts": ts.to_numpy().astype("datetime64[ns]").astype(np.int64),
        "side": df["BUY_SELL_FLAG"].to_numpy().astype(np.int8),   # 0 = bid, 1 = ask
        "px": np.rint(df["PRICE"].to_numpy() / TICK[pair]).astype(np.int64),
        "sz": np.rint(df["SIZE"].to_numpy() / MILLION).astype(np.int64),
    }
    for c in extra:
        out[c.lower()] = df[c].to_numpy()
    return out


# --------------------------------------------------------------------------- replay


def _replay(ts, side, px, sz):
    """Single pass over the level updates, maintaining the ten-deep book.

    Price levels live in ``bids`` / ``asks`` (integer tick -> resting millions).  With
    only ten levels a side the best price is found by scanning the dict, which beats
    maintaining a heap at this size.  Updates sharing a timestamp are applied as one
    batch and the book is recorded once at the end of it.
    """
    bids: dict[int, int] = {}
    asks: dict[int, int] = {}

    q_ts, q_bb, q_ba, q_bs, q_as = (array("q") for _ in range(5))
    g_bb, g_ba = np.zeros(N_SEC, np.int64), np.zeros(N_SEC, np.int64)
    g_bs, g_as = np.zeros(N_SEC, np.int64), np.zeros(N_SEC, np.int64)
    g_bt, g_at = np.zeros(N_SEC, np.int64), np.zeros(N_SEC, np.int64)
    prof_b = np.zeros((N_SEC, N_BUCKETS), np.int32)
    prof_a = np.zeros((N_SEC, N_BUCKETS), np.int32)

    bb = ba = 0
    last = (0, 0, 0, 0)
    slot, bound = 0, int(OPEN.value) + NS
    n_crossed = 0

    def snapshot(k):
        """Record the book as it stands into grid slot ``k``."""
        g_bb[k], g_ba[k] = bb, ba
        if not (bb and ba) or bb >= ba:
            return
        g_bs[k], g_as[k] = bids[bb], asks[ba]
        # Whole visible book, exactly.  The profile below only spans +/-50 ticks of the
        # mid, which is ample for the part (f) band but drops the touch itself on the
        # rare EUR/JPY second whose spread exceeds 100 ticks; these totals never do.
        g_bt[k], g_at[k] = sum(bids.values()), sum(asks.values())
        two_mid = bb + ba
        row_b, row_a = prof_b[k], prof_a[k]
        for p, s in bids.items():                 # distance below the mid, in half-ticks
            b = two_mid - 2 * p
            if 0 <= b < N_BUCKETS:
                row_b[b] += s
        for p, s in asks.items():
            b = 2 * p - two_mid
            if 0 <= b < N_BUCKETS:
                row_a[b] += s

    n = len(ts)
    i = 0
    while i < n:
        j = i                                     # extent of this timestamp's batch
        t = ts[i]
        while j < n and ts[j] == t:
            j += 1

        for k in range(i, j):
            book = asks if side[k] else bids
            if sz[k] == 0:
                book.pop(px[k], None)
            else:
                book[px[k]] = sz[k]

        bb = max(bids) if bids else 0
        ba = min(asks) if asks else 0
        if bb and ba and bb >= ba:                # momentary lock or cross
            n_crossed += 1

        # close out every whole second that ended before this batch
        while t >= bound and slot < N_SEC:
            snapshot(slot)
            slot += 1
            bound += NS

        cur = (bb, ba, bids.get(bb, 0), asks.get(ba, 0))
        if cur != last:
            q_ts.append(t); q_bb.append(cur[0]); q_ba.append(cur[1])
            q_bs.append(cur[2]); q_as.append(cur[3])
            last = cur
        i = j

    while slot < N_SEC:                           # carry the closing book to the last bars
        snapshot(slot)
        slot += 1

    npq = lambda a: np.frombuffer(a, dtype=np.int64)
    return (
        {"ts": npq(q_ts), "bid": npq(q_bb), "ask": npq(q_ba),
         "bid_sz": npq(q_bs), "ask_sz": npq(q_as)},
        {"bid": g_bb, "ask": g_ba, "bid_sz": g_bs, "ask_sz": g_as,
         "bid_tot": g_bt, "ask_tot": g_at},
        prof_b, prof_a, n_crossed,
    )


# --------------------------------------------------------------------------- assembly


@dataclass
class Book:
    """One currency pair's reconstructed trading day.

    Field-for-field the same shape as ``mbo.Day``, so the statistics in ``hfstats``
    apply unchanged to either panel.  Sizes are millions of the base currency.
    """

    symbol: str
    trades: pd.DataFrame        # ts index: price, size, sign, bid, ask, mid, value
    quotes: pd.DataFrame        # ts index: bid, ask, bid_sz, ask_sz, mid, spread
    grid: pd.DataFrame          # 1-second index: BBO, mid, spread, and whole-book depth
    events: pd.DataFrame        # 1-minute index: counts of updates / deletes / trades
    depth_bid: np.ndarray       # (seconds, 101) resting size by half-tick from the mid
    depth_ask: np.ndarray       # (seconds, 101) same on the ask side
    tick: float
    n_crossed: int              # batches that left the book locked or crossed

    def __repr__(self) -> str:
        return (f"<Book {self.symbol}: {len(self.trades):,} trades, "
                f"{len(self.quotes):,} quote updates>")


def _index(ts_ns: np.ndarray) -> pd.DatetimeIndex:
    """Nanosecond epochs -> a naive index on the session clock.

    The files carry no timezone and the homework only ever refers to 9:30-4:00, so the
    timestamps are left as they are rather than asserting a zone we cannot verify.
    """
    return pd.DatetimeIndex(ts_ns.astype("datetime64[ns]"))


def _quote_frame(ts, bid, ask, bid_sz, ask_sz, tick, tot=None) -> pd.DataFrame:
    """Assemble a BBO frame, blanking moments when a side is empty or the book crossed.

    ``tot``, when given, adds the size resting across all ten visible levels a side.
    """
    ok = (bid > 0) & (ask > 0) & (ask > bid)
    b = np.where(ok, bid * tick, np.nan)
    a = np.where(ok, ask * tick, np.nan)
    df = pd.DataFrame(
        {"bid": b, "ask": a,
         "bid_sz": np.where(ok, bid_sz, np.nan), "ask_sz": np.where(ok, ask_sz, np.nan),
         "mid": (b + a) / 2, "spread": a - b},
        index=_index(ts),
    )
    if tot is not None:
        df["bid_tot"] = np.where(ok, tot[0], np.nan)
        df["ask_tot"] = np.where(ok, tot[1], np.nan)
    return df


def _prevailing(quotes: pd.DataFrame, when: pd.DatetimeIndex) -> pd.DataFrame:
    """The BBO in force ``LAG`` before each timestamp in ``when``.

    ``searchsorted`` rather than ``reindex`` because prints share timestamps freely.
    """
    q = quotes.dropna(subset=["mid"])
    idx = np.searchsorted(q.index.to_numpy(), (when - LAG).to_numpy(), side="right") - 1
    out = q.iloc[np.maximum(idx, 0)][["bid", "ask", "mid"]].to_numpy(copy=True)
    out[idx < 0] = np.nan                          # prints before the first clean quote
    return pd.DataFrame(out, columns=["bid", "ask", "mid"], index=when)


def _sign_trades(price, flag, bid, ask):
    """Aggressor sign: +1 buy, -1 sell.

    ``BUY_SELL_FLAG`` means the same thing on the trade feed as on the book feed --
    0 is the bid, 1 the ask -- so flag 1 is the ask being lifted (a buy) and flag 0 the
    bid being hit (a sell).  That reading is what the data shows: against the quote
    from ``LAG`` earlier, flag-1 prints sit 0.9 spreads above flag-0 prints.  Prints
    with no quote to compare against fall back to the flag alone.
    """
    sign = np.where(flag == 1, 1, -1).astype(np.int8)
    mid = (bid + ask) / 2
    quote_rule = np.sign(price - mid)
    agrees = np.isnan(mid) | (quote_rule == 0) | (quote_rule == sign)
    return sign, ~agrees


def _event_counts(ts, sz, trade_ts):
    """Per-minute counts of level updates, level deletions and trades.

    The ITCH panel counts adds and cancels here.  EBS publishes neither: a level
    update is the net effect of any number of dealer actions, so these are counts of
    *messages*, not of orders, and are not comparable to the ITCH figures in level --
    only in shape through the day.
    """
    edges = np.arange(int(OPEN.value), int(CLOSE.value) + 1, 60 * NS)
    n_min = len(edges) - 1
    upd = np.histogram(ts[sz > 0], bins=edges)[0]
    dele = np.histogram(ts[sz == 0], bins=edges)[0]
    trd = np.histogram(trade_ts, bins=edges)[0]
    return edges[:-1], np.column_stack([upd, dele, trd])


def build(pair: str, *, refresh: bool = False) -> Book:
    """Reconstruct ``pair``'s day, caching the result under ``data/cache``."""
    cache = CACHE_DIR / f"fx-{pair.replace('/', '')}.npz"
    if cache.exists() and not refresh:
        z = np.load(cache)
        q = {k: z[f"q_{k}"] for k in ("ts", "bid", "ask", "bid_sz", "ask_sz")}
        g = {k: z[f"g_{k}"] for k in ("bid", "ask", "bid_sz", "ask_sz",
                                      "bid_tot", "ask_tot")}
        prof_b, prof_a = z["prof_b"], z["prof_a"]
        ev_ts, ev_counts = z["ev_ts"], z["ev_counts"]
        t_ts, t_px, t_sz, t_fl = z["t_ts"], z["t_px"], z["t_sz"], z["t_fl"]
        n_crossed = int(z["n_crossed"])
    else:
        o = _read(ORDER_FILE, pair)
        tr = _read(TRADE_FILE, pair)
        q, g, prof_b, prof_a, n_crossed = _replay(o["ts"], o["side"], o["px"], o["sz"])
        ev_ts, ev_counts = _event_counts(o["ts"], o["sz"], tr["ts"])
        t_ts, t_px, t_sz, t_fl = tr["ts"], tr["px"], tr["sz"], tr["side"]
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        np.savez(cache, prof_b=prof_b, prof_a=prof_a, ev_ts=ev_ts, ev_counts=ev_counts,
                 t_ts=t_ts, t_px=t_px, t_sz=t_sz, t_fl=t_fl, n_crossed=n_crossed,
                 **{f"q_{k}": v for k, v in q.items()},
                 **{f"g_{k}": v for k, v in g.items()})

    tick = TICK[pair]
    quotes = _quote_frame(q["ts"], q["bid"], q["ask"], q["bid_sz"], q["ask_sz"], tick)

    grid_ts = np.arange(int(OPEN.value), int(CLOSE.value), NS)
    grid = _quote_frame(grid_ts, g["bid"], g["ask"], g["bid_sz"], g["ask_sz"], tick,
                        tot=(g["bid_tot"], g["ask_tot"]))

    t_idx = _index(t_ts)
    prev = _prevailing(quotes, t_idx)
    px = t_px * tick
    sign, disagrees = _sign_trades(px, t_fl, prev.bid.to_numpy(), prev.ask.to_numpy())
    trades = pd.DataFrame(
        {"price": px, "size": t_sz, "sign": sign,
         "bid": prev.bid.to_numpy(), "ask": prev.ask.to_numpy(),
         "mid": prev.mid.to_numpy(),
         "sign_disagrees": disagrees},
        index=t_idx,
    )

    events = pd.DataFrame(ev_counts, index=_index(ev_ts),
                          columns=["updates", "deletes", "trades"])

    return Book(pair, trades, quotes, grid, events, prof_b, prof_a, tick, n_crossed)


def load(*pairs: str, refresh: bool = False) -> dict[str, Book]:
    """Build every pair and attach the USD notional of each trade.

    ``value`` is dollar volume, which needs a cross rate for the pairs that are not
    quoted against USD: EUR/USD trades are already sized in EUR and priced in USD,
    USD/JPY trades are sized in USD outright, and EUR/JPY trades are sized in EUR and
    converted at the EUR/USD mid prevailing at the print.  Units are USD millions.
    """
    pairs = pairs or PAIRS
    books = {p: build(p, refresh=refresh) for p in pairs}

    eurusd = books.get("EUR/USD")
    for p, bk in books.items():
        size = bk.trades["size"]
        if BASE[p] == "USD":
            bk.trades["value"] = size.astype(float)
        elif p == "EUR/USD":
            bk.trades["value"] = size * bk.trades.price
        elif eurusd is not None:                   # EUR base, non-USD quote
            rate = _prevailing(eurusd.quotes, bk.trades.index).mid
            bk.trades["value"] = size * rate.to_numpy()
        else:
            bk.trades["value"] = np.nan            # EUR/JPY built without EUR/USD loaded
    return books


if __name__ == "__main__":                        # python fx.py -> warm the cache
    for _p, _bk in load(refresh=True).items():
        print(f"{_bk!r}  crossed batches: {_bk.n_crossed}")


# --------------------------------------------------------------- triangular arbitrage


def quote_age(book: "Book", when: pd.DatetimeIndex) -> np.ndarray:
    """Seconds since ``book`` last published a clean quote, at each of ``when``.

    A triangular comparison across three feeds that update independently is only as
    good as the stalest quote in it, so this is what tells an apparent misalignment
    apart from one pair simply not having moved for a while.
    """
    q = book.quotes.dropna(subset=["mid"]).index.to_numpy()
    w = when.to_numpy()
    prev = q[np.maximum(np.searchsorted(q, w, side="right") - 1, 0)]
    return (w - prev) / np.timedelta64(1, "s")


def _runs(flag: np.ndarray) -> np.ndarray:
    """Lengths, in samples, of each maximal run of ``True`` in ``flag``."""
    if not flag.any():
        return np.zeros(0, dtype=np.int64)
    padded = np.concatenate(([False], flag, [False]))
    edge = np.flatnonzero(padded[1:] != padded[:-1])
    return edge[1::2] - edge[::2]


def triangular(books: dict[str, "Book"], *, freq: str = "1s",
               cost_ticks: float = 0.0) -> pd.DataFrame:
    """Part (l): triangular arbitrage between EUR/USD, USD/JPY and EUR/JPY.

    EUR/JPY can be traded directly or manufactured through the dollar, so the two
    prices must agree.  Buying a synthetic euro means paying the EUR/USD ask and then
    the USD/JPY ask; selling one means taking both bids:

        synthetic ask = ask(EUR/USD) * ask(USD/JPY)
        synthetic bid = bid(EUR/USD) * bid(USD/JPY)

    which leaves two ways to be paid, both requiring all three legs at once:

        ``direct_cheap``   buy EUR/JPY at its ask, sell the synthetic  -> synth bid - ask
        ``direct_rich``    sell EUR/JPY at its bid, buy the synthetic  -> bid - synth ask

    Quantity is set by the thinnest leg.  Every leg is converted to millions of euro,
    which for the USD/JPY leg means dividing its USD size by the EUR/USD rate, so the
    figure is the size of the round trip that can actually be put on -- not the size
    of any one quote.

    ``cost_ticks`` charges that many EUR/JPY ticks against each opportunity, as a
    crude stand-in for brokerage and for the slippage of legging three books; at zero
    the result is the gross deviation, which is an upper bound on what was earnable.

    The three feeds update independently, so sampling them on a common clock is what
    makes this comparison possible at all -- and also its main caveat: the last quote
    in a second is used for each pair, and quotes that were never simultaneously live
    within that second can appear to line up here.
    """
    eu, uj, ej = (books[p].grid for p in ("EUR/USD", "USD/JPY", "EUR/JPY"))
    if freq != "1s":
        eu, uj, ej = (g.resample(freq).last() for g in (eu, uj, ej))

    df = pd.DataFrame(index=ej.index)
    df["synth_bid"] = eu.bid * uj.bid
    df["synth_ask"] = eu.ask * uj.ask
    df["direct_bid"] = ej.bid
    df["direct_ask"] = ej.ask
    df["mid"] = ej.mid

    cost = cost_ticks * TICK["EUR/JPY"]
    df["edge_cheap"] = df.synth_bid - df.direct_ask - cost   # buy direct, sell synthetic
    df["edge_rich"] = df.direct_bid - df.synth_ask - cost    # sell direct, buy synthetic

    # Size of the round trip, in millions of euro: the EUR/USD and EUR/JPY legs are
    # already sized in euro, the USD/JPY leg is sized in dollars.
    df["qty_cheap"] = np.minimum.reduce([
        ej.ask_sz.to_numpy(), eu.bid_sz.to_numpy(),
        (uj.bid_sz / eu.bid).to_numpy()])
    df["qty_rich"] = np.minimum.reduce([
        ej.bid_sz.to_numpy(), eu.ask_sz.to_numpy(),
        (uj.ask_sz / eu.ask).to_numpy()])

    both = df[["edge_cheap", "edge_rich"]]
    df["edge"] = both.max(axis=1)
    df["direction"] = np.where(df.edge_cheap >= df.edge_rich, "cheap", "rich")
    df["qty"] = np.where(df.direction == "cheap", df.qty_cheap, df.qty_rich)
    df["open"] = df.edge > 0
    df["edge_bps"] = 1e4 * df.edge / df["mid"]
    # edge is JPY per euro and qty is millions of euro, so their product is millions of
    # JPY; the USD/JPY mid converts that to dollars.
    df["usdjpy_mid"] = uj.mid
    df["profit_usd"] = np.where(df.open, df.edge * df.qty / df.usdjpy_mid, 0.0)
    # Age of the stalest of the three quotes, so an apparent edge can be checked
    # against the possibility that one leg simply had not updated.
    df["age_s"] = np.maximum.reduce([quote_age(books[p], df.index) for p in PAIRS])
    return df


def triangular_summary(tri: pd.DataFrame) -> pd.Series:
    """Frequency, duration and size of the opportunities found by ``triangular``.

    Profit assumes the whole displayed quantity is filled on all three legs at the
    quoted prices -- the most generous assumption available, and so an upper bound.
    Duration counts consecutive sampled intervals in which the edge stays positive,
    which at one-second sampling can only resolve durations down to a second: an edge
    that opened and closed inside one second is one second here.
    """
    ok = tri[["synth_bid", "synth_ask", "direct_bid", "direct_ask"]].notna().all(axis=1)
    live = tri[ok]
    hit = live[live.open]

    runs = _runs(live.open.to_numpy())
    step = pd.Timedelta(live.index.freq or (live.index[1] - live.index[0])).total_seconds()

    return pd.Series({
        "seconds observed": len(live),
        "seconds with an opportunity": len(hit),
        "frequency (% of seconds)": 100 * len(hit) / max(len(live), 1),
        "episodes": len(runs),
        "mean duration (s)": runs.mean() * step if len(runs) else np.nan,
        "median duration (s)": np.median(runs) * step if len(runs) else np.nan,
        "max duration (s)": runs.max() * step if len(runs) else np.nan,
        "mean edge (bps)": hit.edge_bps.mean(),
        "max edge (bps)": hit.edge_bps.max(),
        "mean quantity (mm EUR)": hit.qty.mean(),
        "median quantity (mm EUR)": hit.qty.median(),
        "total profit (USD mm)": hit.profit_usd.sum(),
        "cheap-side episodes": int((hit.direction == "cheap").sum()),
        "rich-side episodes": int((hit.direction == "rich").sum()),
    })
