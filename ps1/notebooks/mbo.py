"""Order-book reconstruction from Databento MBO (Nasdaq TotalView-ITCH).

The raw file is one row per book event for the whole 2026-09-02 session.  This
module turns it into the three objects the analysis actually needs:

    * ``trades``  -- the trade tape, signed, with the quote prevailing at each print
    * ``quotes``  -- the BBO, one row per change in (bid, ask, bid size, ask size)
    * ``grid``    -- the book sampled at the end of every second, plus a profile of
                     resting depth by distance from the mid

Reconstruction rules specific to Databento's ITCH mapping:

    * An execution emits three records: ``T`` (the trade), ``F`` (the fill against
      the resting order) and ``C`` (which removes the shares).  Only ``A`` and ``C``
      touch the book -- applying ``F`` as well would remove the liquidity twice.
    * ``T.side`` is the *aggressor*: ``B`` = buy, ``A`` = sell, ``N`` = unspecified
      (non-displayed executions and the opening cross).
    * The file carries no snapshot record, so the book is rebuilt from empty at
      09:30.  Nasdaq re-adds the pre-open book right after the opening cross, so
      only the first fraction of a second is incomplete.
"""

from __future__ import annotations

import heapq
from array import array
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pandas as pd
import pyarrow as pa
import pyarrow.compute as pc
import pyarrow.csv as pacsv
import zstandard

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
MBO_FILE = DATA_DIR / "xnas-itch-20260902.mbo.csv.zst"
CACHE_DIR = DATA_DIR / "cache"

TZ = "America/New_York"
DATE = "2026-09-02"
OPEN = pd.Timestamp(f"{DATE} 09:30", tz=TZ).tz_convert("UTC")
CLOSE = pd.Timestamp(f"{DATE} 16:00", tz=TZ).tz_convert("UTC")

NS = 1_000_000_000            # nanoseconds per second
SCALE = 1_000_000_000         # Databento fixed-point price scale (1e-9 USD)
CENT = 10_000_000             # $0.01 in fixed-point units
N_BUCKETS = 101               # depth profile depth: 0..100 half-cents ($0.50) from the mid

_ACTIONS = ["A", "C", "F", "T", "M", "R"]   # codes 0..5; only A and C move the book
_SIDES = ["A", "B", "N"]                    # 0 = ask, 1 = bid, 2 = none

N_SEC = int((CLOSE - OPEN).total_seconds())
_CHUNK = 1 << 20              # events converted to Python ints per batch during replay


# --------------------------------------------------------------------------- load


def _read_raw() -> pa.Table:
    """Decompress and parse the whole MBO file (all symbols) into an Arrow table."""
    with open(MBO_FILE, "rb") as fh:
        stream = zstandard.ZstdDecompressor().stream_reader(fh)
        return pacsv.read_csv(
            stream,
            read_options=pacsv.ReadOptions(block_size=1 << 26),
            convert_options=pacsv.ConvertOptions(
                include_columns=["ts_recv", "action", "side", "price", "size",
                                 "order_id", "symbol"],
                column_types={
                    "ts_recv": pa.timestamp("ns", tz="UTC"),
                    "action": pa.string(), "side": pa.string(),
                    "price": pa.float64(), "size": pa.int64(),
                    "order_id": pa.uint64(), "symbol": pa.string(),
                },
            ),
        )


def symbols() -> list[str]:
    """Distinct tickers present in the file."""
    return sorted(pc.unique(_read_raw()["symbol"]).to_pylist())


def _columns(table: pa.Table, symbol: str) -> dict[str, np.ndarray]:
    """Slice one symbol out of the table as plain numpy arrays.

    Prices are kept as int64 fixed-point so that book keys are exact; strings are
    mapped to small integer codes so the replay loop never compares text.
    """
    t = table.filter(pc.equal(table["symbol"], symbol))
    return {
        "ts": t["ts_recv"].cast(pa.int64()).to_numpy(),
        "act": pc.index_in(t["action"], value_set=pa.array(_ACTIONS)).to_numpy(zero_copy_only=False),
        "side": pc.index_in(t["side"], value_set=pa.array(_SIDES)).to_numpy(zero_copy_only=False),
        "px": np.rint(t["price"].to_numpy() * SCALE).astype(np.int64),
        "sz": t["size"].to_numpy(),
        "oid": t["order_id"].cast(pa.int64()).to_numpy(),
    }


# --------------------------------------------------------------------------- replay


def _replay(ts, act, side, px, sz, oid):
    """Single pass over the events, maintaining a full limit order book.

    Price levels live in ``bids`` / ``asks`` (fixed-point price -> resting size).
    The best price on each side is tracked with a lazily-deleted heap: a price is
    pushed whenever its level goes from empty to non-empty, and stale entries are
    popped only when they surface at the top.
    """
    orders: dict[int, list] = {}          # order id -> [price, remaining size, is_bid]
    bids: dict[int, int] = {}
    asks: dict[int, int] = {}
    bid_heap: list[int] = []              # negated prices -> max-heap
    ask_heap: list[int] = []
    bb = ba = 0                           # best bid / best ask, 0 when that side is empty

    # BBO change series
    q_ts, q_bb, q_ba, q_bs, q_as = (array("q") for _ in range(5))
    # trade tape, with the quote in force at the print
    t_ts, t_px, t_sz, t_sd, t_bb, t_ba = (array("q") for _ in range(6))
    # end-of-second book snapshots
    g_bb, g_ba = np.zeros(N_SEC, np.int64), np.zeros(N_SEC, np.int64)
    g_bs, g_as = np.zeros(N_SEC, np.int64), np.zeros(N_SEC, np.int64)
    prof_b = np.zeros((N_SEC, N_BUCKETS), np.int64)
    prof_a = np.zeros((N_SEC, N_BUCKETS), np.int64)

    last = (0, 0, 0, 0)
    slot, bound = 0, int(OPEN.value) + NS

    def snapshot(k):
        """Record the book as it stands into grid slot ``k``."""
        g_bb[k], g_ba[k] = bb, ba
        if not (bb and ba):
            return
        g_bs[k], g_as[k] = bids.get(bb, 0), asks.get(ba, 0)
        two_mid = bb + ba
        row_b, row_a = prof_b[k], prof_a[k]
        p = bb                                    # walk the penny grid down from the bid
        while True:
            b = (two_mid - 2 * p) // CENT
            if b >= N_BUCKETS or p <= 0:
                break
            row_b[b] += bids.get(p, 0)
            p -= CENT
        p = ba                                    # and up from the ask
        while True:
            b = (2 * p - two_mid) // CENT
            if b >= N_BUCKETS:
                break
            row_a[b] += asks.get(p, 0)
            p += CENT

    # Iterate over Python ints rather than numpy scalars -- roughly 3x faster -- but
    # convert in chunks so the whole file is never materialised as lists at once.
    for lo in range(0, len(ts), _CHUNK):
        hi = min(lo + _CHUNK, len(ts))
        rows = zip(ts[lo:hi].tolist(), act[lo:hi].tolist(), side[lo:hi].tolist(),
                   px[lo:hi].tolist(), sz[lo:hi].tolist(), oid[lo:hi].tolist())
        for t, a, sd, p, s, o_id in rows:

            # close out every whole second that ended before this event
            while t >= bound and slot < N_SEC:
                snapshot(slot)
                slot += 1
                bound += NS

            if a == 3:                            # T -- trade, no book effect
                t_ts.append(t); t_px.append(p); t_sz.append(s)
                t_sd.append(sd); t_bb.append(bb); t_ba.append(ba)
                continue
            if a != 0 and a != 1:                 # F is bookkeeping; M / R never occur here
                continue

            is_bid = sd == 1
            book = bids if is_bid else asks
            heap = bid_heap if is_bid else ask_heap

            if a == 0:                            # A -- add
                orders[o_id] = [p, s, is_bid]
            else:                                 # C -- cancel or execution removal
                o = orders.get(o_id)
                if o is None:                     # order added before 09:30; not in our book
                    continue
                p = o[0]
                s = min(o[1], s)
                o[1] -= s
                if o[1] <= 0:
                    del orders[o_id]
                s = -s

            prev = book.get(p, 0)
            if prev == 0 and s > 0:
                heapq.heappush(heap, -p if is_bid else p)
            book[p] = prev + s

            # refresh the best price only when the touched level could be the best
            if is_bid:
                if p >= bb or bb == 0:
                    while bid_heap and bids.get(-bid_heap[0], 0) == 0:
                        heapq.heappop(bid_heap)
                    bb = -bid_heap[0] if bid_heap else 0
            else:
                if p <= ba or ba == 0:
                    while ask_heap and asks.get(ask_heap[0], 0) == 0:
                        heapq.heappop(ask_heap)
                    ba = ask_heap[0] if ask_heap else 0

            cur = (bb, ba, bids.get(bb, 0), asks.get(ba, 0))
            if cur != last:
                q_ts.append(t); q_bb.append(cur[0]); q_ba.append(cur[1])
                q_bs.append(cur[2]); q_as.append(cur[3])
                last = cur

    while slot < N_SEC:                           # carry the closing book to the last bars
        snapshot(slot)
        slot += 1

    npq = lambda a: np.frombuffer(a, dtype=np.int64)
    return (
        {"ts": npq(q_ts), "bid": npq(q_bb), "ask": npq(q_ba),
         "bid_sz": npq(q_bs), "ask_sz": npq(q_as)},
        {"ts": npq(t_ts), "price": npq(t_px), "size": npq(t_sz),
         "side": npq(t_sd), "bid": npq(t_bb), "ask": npq(t_ba)},
        {"bid": g_bb, "ask": g_ba, "bid_sz": g_bs, "ask_sz": g_as},
        prof_b, prof_a,
    )


# --------------------------------------------------------------------------- assembly


@dataclass
class Day:
    """One symbol's reconstructed trading day."""

    symbol: str
    trades: pd.DataFrame        # ts index: price, size, sign, bid, ask, mid, is_cross
    quotes: pd.DataFrame        # ts index: bid, ask, bid_sz, ask_sz, mid, spread
    grid: pd.DataFrame          # 1-second index: bid, ask, bid_sz, ask_sz, mid, spread
    events: pd.DataFrame        # 1-minute index: counts of adds / cancels / trades
    depth_bid: np.ndarray       # (seconds, 101) resting bid size by half-cent from the mid
    depth_ask: np.ndarray       # (seconds, 101) same on the ask side

    def __repr__(self) -> str:
        return (f"<Day {self.symbol}: {len(self.trades):,} trades, "
                f"{len(self.quotes):,} quote updates>")


def _sign_trades(price, side, bid, ask):
    """Aggressor sign: +1 buy, -1 sell.

    ITCH gives the aggressor directly for lit executions.  Non-displayed prints and
    the cross arrive unsigned, so those fall back to the quote rule (compare to the
    prevailing mid) and then to a tick test for prints exactly at the mid.
    """
    sign = np.where(side == 1, 1, np.where(side == 0, -1, 0)).astype(np.int8)
    unsigned = sign == 0
    mid = np.where((bid > 0) & (ask > 0), (bid + ask) / 2.0, np.nan)
    quote_rule = np.sign(price - mid)
    sign[unsigned] = np.nan_to_num(quote_rule[unsigned]).astype(np.int8)

    still = sign == 0                                    # at the mid, or no quote: tick test
    if still.any():
        chg = np.sign(np.diff(price, prepend=price[0]))
        tick = pd.Series(np.where(chg == 0, np.nan, chg)).ffill().fillna(1).to_numpy()
        sign[still] = tick[still].astype(np.int8)
    return sign


def _flag_opening_cross(ts, size, side):
    """Mark the Nasdaq opening cross.

    It prints once, unsigned, in the first minute of the session, and dwarfs every
    continuous trade (671k shares for NVDA, 975k for GPRO on this date).
    """
    flag = np.zeros(len(ts), bool)
    early = (ts < int(OPEN.value) + 60 * NS) & (side == 2)
    if early.any():
        idx = np.flatnonzero(early)
        flag[idx[np.argmax(size[idx])]] = True
    return flag


def build(symbol: str, *, refresh: bool = False, table: pa.Table | None = None) -> Day:
    """Reconstruct ``symbol``'s day, caching the result under ``data/cache``."""
    cache = CACHE_DIR / f"{symbol}.npz"
    if cache.exists() and not refresh:
        z = np.load(cache)
        q = {k: z[f"q_{k}"] for k in ("ts", "bid", "ask", "bid_sz", "ask_sz")}
        t = {k: z[f"t_{k}"] for k in ("ts", "price", "size", "side", "bid", "ask")}
        g = {k: z[f"g_{k}"] for k in ("bid", "ask", "bid_sz", "ask_sz")}
        prof_b, prof_a = z["prof_b"], z["prof_a"]
        ev_ts, ev_counts = z["ev_ts"], z["ev_counts"]
    else:
        cols = _columns(_read_raw() if table is None else table, symbol)
        q, t, g, prof_b, prof_a = _replay(**cols)
        ev_ts, ev_counts = _event_counts(cols["ts"], cols["act"])
        CACHE_DIR.mkdir(exist_ok=True)
        np.savez(cache, prof_b=prof_b, prof_a=prof_a, ev_ts=ev_ts, ev_counts=ev_counts,
                 **{f"q_{k}": v for k, v in q.items()},
                 **{f"t_{k}": v for k, v in t.items()},
                 **{f"g_{k}": v for k, v in g.items()})

    quotes = _quote_frame(q["ts"], q["bid"], q["ask"], q["bid_sz"], q["ask_sz"])

    px = t["price"] / SCALE
    bid, ask = t["bid"] / SCALE, t["ask"] / SCALE
    trades = pd.DataFrame(
        {"price": px, "size": t["size"],
         "sign": _sign_trades(px, t["side"], bid, ask),
         "bid": np.where(t["bid"] > 0, bid, np.nan),
         "ask": np.where(t["ask"] > 0, ask, np.nan),
         "is_cross": _flag_opening_cross(t["ts"], t["size"], t["side"])},
        index=_index(t["ts"]),
    )
    trades["mid"] = (trades.bid + trades.ask) / 2
    trades["value"] = trades.price * trades["size"]

    grid_ts = np.arange(int(OPEN.value), int(CLOSE.value), NS)
    grid = _quote_frame(grid_ts, g["bid"], g["ask"], g["bid_sz"], g["ask_sz"])

    events = pd.DataFrame(ev_counts, index=_index(ev_ts),
                          columns=["adds", "cancels", "fills", "trades"])

    return Day(symbol, trades, quotes, grid, events, prof_b, prof_a)


def load(*syms: str, refresh: bool = False) -> dict[str, Day]:
    """Build several symbols at once, decompressing the raw file at most once."""
    table = None
    days = {}
    for sym in syms:
        if (refresh or not (CACHE_DIR / f"{sym}.npz").exists()) and table is None:
            table = _read_raw()
        days[sym] = build(sym, refresh=refresh, table=table)
    return days


def _event_counts(ts, act):
    """Per-minute counts of adds, cancels, fills and trades, straight off the tape."""
    minute = (ts - int(OPEN.value)) // (60 * NS)
    n_min = int((CLOSE - OPEN).total_seconds() // 60)
    counts = np.zeros((n_min, 4), np.int64)
    for code in range(4):
        m = act == code
        counts[:, code] = np.bincount(minute[m], minlength=n_min)[:n_min]
    return np.arange(int(OPEN.value), int(CLOSE.value), 60 * NS), counts


def _index(ts_ns: np.ndarray) -> pd.DatetimeIndex:
    """Nanosecond epochs -> a tz-aware exchange-local index."""
    return pd.DatetimeIndex(ts_ns.astype("datetime64[ns]"), tz="UTC").tz_convert(TZ)


def _quote_frame(ts, bid, ask, bid_sz, ask_sz) -> pd.DataFrame:
    """Assemble a BBO frame, blanking out moments when either side is empty."""
    ok = (bid > 0) & (ask > 0)
    b = np.where(ok, bid / SCALE, np.nan)
    a = np.where(ok, ask / SCALE, np.nan)
    df = pd.DataFrame(
        {"bid": b, "ask": a,
         "bid_sz": np.where(ok, bid_sz, np.nan), "ask_sz": np.where(ok, ask_sz, np.nan),
         "mid": (b + a) / 2, "spread": a - b},
        index=_index(ts),
    )
    return df


if __name__ == "__main__":                        # python mbo.py -> warm the cache
    raw = _read_raw()
    for sym in symbols():
        print(build(sym, refresh=True, table=raw))
