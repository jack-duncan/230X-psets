"""
Replay a recorded ALGO2e book (data/sim2.csv) through mm_algo2e's real step() loop.

    uv run --with requests python replay.py [--data data/sim2.csv] [--calibrate]
        [--set GAMMA=1e-5 --set THETA_ON=0.005] [--param RY.rho=0.0] [--seed 1]

A FakeApi stands in for the RIT client, so quoting, gating, allocation, guards, reconcile and
fill/markout tracking all run unmodified. One algorithm pass per recorded snapshot (~1 s apart,
~10 per case tick); the clock is simulated, so no sleeping.

FILL MODEL (a rough test, not the real exchange -- there is no trade tape, only cumulative volume):
  * Our order never moves the market; the recorded book is taken as-is.
  * A resting order joins the queue behind the recorded depth at its price (nothing ahead if it
    is inside the spread; the level-3 depth if it is past the visible book). Cancels ahead of us
    are ignored, which is pessimistic.
  * Between snapshots, volume that traded at or through our price (last <= our bid / >= our ask)
    is attributed to `last`: it drains the queue ahead first, then fills us at our own price.
    One snapshot's volume can span several prices, so this is approximate.
  * If the recorded opposite touch crosses our price we are filled at our price, up to the
    visible depth at or through it.
  * Market orders walk the 3 visible levels, then 2 ticks beyond the last one (unused by step()).
Terminal inventory is reported marked at mid and at the touch, since the case's terminal mark
is unverified.
"""
import argparse
import ast
import csv
import os
import statistics
import sys
from collections import Counter
from datetime import datetime

import mm_algo2e as mm
from mm_algo2e import BUY, SELL

HERE = os.path.dirname(os.path.abspath(__file__))
START_NLV = 100000.0
LEVELS = (1, 2, 3)


def num(x):
    return float(x) if x not in ("", None) else None


def load_snapshots(path):
    """[{'ts': seconds, 'tick': int, 'rows': {ticker: parsed row}}], in file order."""
    snaps, by_ts = [], {}
    with open(path, newline="") as f:
        for raw in csv.DictReader(f):
            ts = raw["timestamp"]
            if ts not in by_ts:
                by_ts[ts] = {"ts": datetime.fromisoformat(ts).timestamp(), "tick": int(raw["tick"]), "rows": {}}
                snaps.append(by_ts[ts])
            row = {"bid": num(raw["bid"]), "ask": num(raw["ask"]), "bid_size": num(raw["bid_size"]),
                   "ask_size": num(raw["ask_size"]), "last": num(raw["last"]), "volume": num(raw["volume"]),
                   "bids": [], "asks": []}
            for i in LEVELS:
                bp, bd = num(raw[f"bid_px_{i}"]), num(raw[f"bid_depth_{i}"])
                ap, ad = num(raw[f"ask_px_{i}"]), num(raw[f"ask_depth_{i}"])
                if bp is not None and bd:
                    row["bids"].append((mm.to_cents(bp), int(bd)))
                if ap is not None and ad:
                    row["asks"].append((mm.to_cents(ap), int(ad)))
            by_ts[ts]["rows"][raw["ticker"]] = row
    return [s for s in snaps if len(s["rows"]) == len(mm.TICKERS)]


class Clock:
    """Stands in for the `time` module inside mm_algo2e."""
    def __init__(self):
        self.t = 0.0

    def monotonic(self):
        return self.t

    def sleep(self, s):
        self.t += s


class FakeApi:
    def __init__(self, order_rate=mm.ORDER_RATE):
        self.order_rate = order_rate
        self.row = {}                 # current snapshot rows
        self.prev = {}                # previous snapshot rows
        self.tick = None
        self.orders = {}              # oid -> order dict
        self.next_id = 1
        self.pos = {t: 0 for t in mm.TICKERS}
        self.cash = {t: 0.0 for t in mm.TICKERS}
        self.cmds = 0                 # order commands since the last snapshot
        self.cmd_hist = []
        self.market_calls = 0

    # ---- exchange side ----
    def advance(self, snap):
        self.cmd_hist.append(self.cmds)
        self.cmds = 0
        self.prev, self.row, self.tick = self.row, snap["rows"], snap["tick"]
        for o in list(self.orders.values()):
            if o["status"] == "OPEN" and o["ticker"] in self.row and o["ticker"] in self.prev:
                self._fill_resting(o)

    def _apply(self, o, qty, price_c):
        o["filled"] += qty
        o["remaining"] -= qty
        sign = 1 if o["action"] == BUY else -1
        self.pos[o["ticker"]] += sign * qty
        self.cash[o["ticker"]] -= sign * qty * price_c / 100
        if o["remaining"] <= 0:
            o["status"] = "TRANSACTED"

    def _fill_resting(self, o):
        r, p = self.row[o["ticker"]], self.prev[o["ticker"]]
        if o["action"] == BUY:
            through = sum(d for px, d in r["asks"] if px <= o["price"])
            if through:
                self._apply(o, min(o["remaining"], through), o["price"])
        else:
            through = sum(d for px, d in r["bids"] if px >= o["price"])
            if through:
                self._apply(o, min(o["remaining"], through), o["price"])
        if o["status"] != "OPEN" or r["volume"] is None or p["volume"] is None or r["last"] is None:
            return
        dv = max(0, int(r["volume"] - p["volume"]))
        last_c = mm.to_cents(r["last"])
        traded_through = last_c <= o["price"] if o["action"] == BUY else last_c >= o["price"]
        if dv > 0 and traded_through:
            eaten = min(o["queue"], dv)
            o["queue"] -= eaten
            if dv - eaten > 0:
                self._apply(o, min(o["remaining"], dv - eaten), o["price"])

    def _queue_ahead(self, ticker, action, price):
        r = self.row[ticker]
        levels = r["bids"] if action == BUY else r["asks"]
        for px, d in levels:
            if px == price:
                return d
        if levels and ((action == BUY and price < levels[-1][0]) or (action == SELL and price > levels[-1][0])):
            return levels[-1][1]
        return 0

    # ---- Api interface used by mm_algo2e ----
    def case(self):
        return {"tick": self.tick, "status": "ACTIVE", "ticks_per_period": 300, "total_periods": 1}

    def trader(self):
        nlv = START_NLV + sum(self.cash[t] + self.pos[t] * self._mid(t) for t in mm.TICKERS)
        return {"trader_id": "ME", "nlv": nlv}

    def _mid(self, t):
        r = self.row.get(t)
        if not r or r["bid"] is None or r["ask"] is None:
            r = self.prev.get(t) or r
        return (r["bid"] + r["ask"]) / 2 if r and r["bid"] is not None and r["ask"] is not None else 0.0

    def securities(self):
        return {t: {"ticker": t, "position": self.pos[t], "api_orders_per_second": self.order_rate}
                for t in mm.TICKERS}

    def book(self, ticker):
        r = self.row[ticker]
        if r["bid"] is None or r["ask"] is None:
            return {"bids": [], "asks": []}
        mk = lambda lv: [{"order_id": -(i + 1), "trader_id": "BOT", "price": px / 100, "quantity": d,
                                "quantity_filled": 0} for i, (px, d) in enumerate(lv)]
        return {"bids": mk(r["bids"]), "asks": mk(r["asks"])}

    def open_orders(self):
        out = {t: [] for t in mm.TICKERS}
        for o in self.orders.values():
            if o["status"] == "OPEN":
                out[o["ticker"]].append({"id": o["id"], "action": o["action"], "price": o["price"],
                                         "qty": o["remaining"], "filled": o["filled"]})
        return out

    def order(self, oid):
        o = self.orders.get(oid)
        return None if o is None else {"order_id": oid, "quantity_filled": o["filled"], "status": o["status"]}

    def place_limit(self, ticker, action, qty, price_c):
        self.cmds += 1
        o = {"id": self.next_id, "ticker": ticker, "action": action, "price": price_c, "qty": qty,
             "remaining": qty, "filled": 0, "status": "OPEN", "queue": 0}
        self.next_id += 1
        self.orders[o["id"]] = o
        r = self.row[ticker]
        levels = r["asks"] if action == BUY else r["bids"]
        for px, d in levels:  # marketable at placement: fill against visible depth at our price or better
            if o["remaining"] <= 0:
                break
            if (action == BUY and px <= price_c) or (action == SELL and px >= price_c):
                self._apply(o, min(o["remaining"], d), px)
        if o["status"] == "OPEN":
            o["queue"] = self._queue_ahead(ticker, action, price_c)
        return {"order_id": o["id"], "quantity_filled": o["filled"]}

    def place_market(self, ticker, action, qty):
        self.cmds += 1
        self.market_calls += 1
        r = self.row[ticker]
        levels = list(r["asks"] if action == BUY else r["bids"])
        o = {"id": self.next_id, "ticker": ticker, "action": action, "price": 0, "qty": qty,
             "remaining": qty, "filled": 0, "status": "OPEN", "queue": 0}
        self.next_id += 1
        self.orders[o["id"]] = o
        step = 2 * mm.TICK_C * (1 if action == BUY else -1)
        for px, d in levels:
            if o["remaining"] > 0:
                self._apply(o, min(o["remaining"], d), px)
        if o["remaining"] > 0 and levels:
            self._apply(o, o["remaining"], levels[-1][0] + step)
        o["status"] = "TRANSACTED"
        return o["filled"]

    def cancel(self, oid):
        self.cmds += 1
        o = self.orders.get(oid)
        if o is None or o["status"] != "OPEN":
            return "gone"
        o["status"] = "CANCELLED"
        return "ok"

    def cancel_all(self, ticker):
        self.cmds += 1
        for o in self.orders.values():
            if o["ticker"] == ticker and o["status"] == "OPEN":
                o["status"] = "CANCELLED"
        return True


class QuietEvents:
    def __init__(self, ctx):
        self.ctx = ctx
        self.counts = Counter()
        self.rows = []

    def log(self, kind, detail):
        self.counts[kind] += 1
        self.rows.append((self.ctx.t, kind, detail))


class ListCsv:
    def __init__(self, path, header):
        self.f = open(path, "w", newline="")
        self.w = csv.writer(self.f)
        self.w.writerow(header)

    def row(self, values):
        self.w.writerow(values)

    def close(self):
        self.f.close()


def apply_overrides(sets, params):
    for item in sets:
        k, v = item.split("=", 1)
        if not hasattr(mm, k):
            sys.exit(f"unknown config name {k}")
        setattr(mm, k, ast.literal_eval(v))
    for item in params:
        left, v = item.split("=", 1)
        t, k = left.split(".", 1)
        mm.PARAMS[t][k] = ast.literal_eval(v)


def replay(snaps, calibrate=False, seed=None, out_prefix=None):
    clock = Clock()
    mm.time = clock
    api = FakeApi()
    ctx = mm.Ctx(calibrate=calibrate)
    if seed is not None:
        ctx.rng.seed(seed)
    ctx.trader_id, ctx.case_length = "ME", 300
    ctx.nlv_start = ctx.nlv = START_NLV
    ev = QuietEvents(ctx)
    mm._events = ev
    fills_log = ListCsv(os.path.join(HERE, f"{out_prefix}_fills.csv"), mm.FILLS_HEADER) if out_prefix else None
    state_log = ListCsv(os.path.join(HERE, f"{out_prefix}_state.csv"), mm.STATE_HEADER) if out_prefix else None
    mm._fills_log = fills_log

    equity, gate_on, live = [], Counter(), Counter()
    for snap in snaps:
        clock.t = snap["ts"]
        api.advance(snap)
        mm.refresh_state(api, ctx)
        mm.step(api, ctx)
        if state_log:
            mm.log_state_rows(ctx, state_log)
        equity.append((snap["tick"], api.trader()["nlv"] - START_NLV))
        for a in ctx.assets.values():
            live[a.ticker] += 1
            gate_on[a.ticker] += a.gate_on
    for f in (fills_log, state_log):
        if f:
            f.close()
    return api, ctx, ev, equity, gate_on, live


def report(snaps, api, ctx, ev, equity, gate_on, live):
    last = snaps[-1]["rows"]
    print(f"\nReplayed {len(snaps)} snapshots, ticks {snaps[0]['tick']}-{snaps[-1]['tick']}")
    print("\n=== P&L (marked at last snapshot) ===")
    tot_mid = tot_touch = 0.0
    for t in mm.TICKERS:
        r, pos = last[t], api.pos[t]
        mid = (r["bid"] + r["ask"]) / 2
        touch = (r["bid"] if pos > 0 else r["ask"]) if pos else mid
        pm, pt = api.cash[t] + pos * mid, api.cash[t] + pos * touch
        tot_mid += pm
        tot_touch += pt
        a = ctx.assets[t]
        mk = mm.weighted_markout(list(a.mk[BUY]) + list(a.mk[SELL]), 0, 10**9, 1)
        print(f"{t:<4} pos={pos:+7d} max|pos|={a.max_abs_pos:6d} fills={a.fills_n:4d} shares={a.fill_shares:7d} "
              f"gate_on={gate_on[t] / max(live[t], 1):4.0%} markout={'n/a' if mk is None else f'{mk:+.4f}'} "
              f"P&L mid=${pm:9.2f} touch=${pt:9.2f}")
    print(f"{'ALL':<4} P&L mid=${tot_mid:.2f}  touch=${tot_touch:.2f}")
    if equity:
        peak, mdd = -1e18, 0.0
        for _, e in equity:
            peak = max(peak, e)
            mdd = max(mdd, peak - e)
        print(f"max drawdown (mid-marked equity): ${mdd:.2f}")
    print("\n=== Execution load ===")
    cmds = api.cmd_hist[1:] or [0]
    over = sum(1 for c in cmds if c > api.order_rate)
    print(f"order commands per snapshot: mean={statistics.mean(cmds):.1f} max={max(cmds)} "
          f"| over the {api.order_rate}/s limit in {over / len(cmds):.0%} of snapshots")
    print(f"guard ticks: L1={ctx.guard_ticks[1]} L2={ctx.guard_ticks[2]}")
    print("events:", dict(ev.counts))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", default=os.path.join(HERE, "data", "sim2.csv"))
    ap.add_argument("--calibrate", action="store_true")
    ap.add_argument("--set", action="append", default=[])
    ap.add_argument("--param", action="append", default=[])
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--out", default="log_replay")
    args = ap.parse_args()
    apply_overrides(args.set, args.param)
    snaps = load_snapshots(args.data)
    result = replay(snaps, args.calibrate, args.seed, args.out)
    report(snaps, *result)


if __name__ == "__main__":
    main()
