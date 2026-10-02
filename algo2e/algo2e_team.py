# -*- coding: utf-8 -*-
"""
TEAM BUILD (giancarlo/algo2e, 2026-10-02). Hashim's algo2e_mm.py with RY unchanged, plus:
  * CNR: passive at the touch, one side at a time - only the side the order book favours
    (`Imb gate`). Real data: favoured fills +3.4c / +2.7c per share after fees in two heats,
    unfavoured -2.9c / -5.8c.
  * AC: same gate, tiny size, scaled down by live depth, volatility and our own fill edge,
    and switched off while the book is thin or one level is a wall (`Thin touch kill`,
    `Wall kill x`) - the heat-2 pump pattern.
  * Limit allocator: each ticker gets a fixed share of the account budget (`Budget share`
    x `Account gross stop`), scaled by its live health in [0, 1]. Shares sum below 1 with
    RY's own cap, so the three processes never need to talk to each other.
  Evidence and scripts: algo3/FINDINGS_PRACTICE.md and algo3/analysis/ (our repo).
-------------------------------------------------------------------------------
RIT ALGO2e v2 - one process per ticker (CNR / RY / AC), LIMIT orders only, per-ticker tuning
========================================================================

V2 (2026-10-01, ALGO2e: CNR / RY / AC) - what changed vs the single-stock build
  * one process per ticker; per-ticker tuning in TICKER_OVERRIDES (RY = wide, volatile,
    the P&L engine: big quotes; CNR/AC = 1c-locked thin books: join the touch small)
  * position safety: fills reported by the POST response are booked at once; orders
    with an unconfirmed cancel still count as exposure; quotes are sized off the larger
    of server position / local estimate; a reducing sweep flips us by at most half a
    quote; a hard-reduce cross pulls our own opposite side first and is sized to the
    visible touch; account-wide gross/net from /limits stops adding on every ticker
  * `Take edge` (RY): marketable LIMIT against a one-sided dislocated touch (RY pays
    0.14c/share to TAKE liquidity and charges 0.34c to provide)
  * end of case in SECONDS (tick length measured live): reducing side stays passive
    until `End cross secs`, then LIMITs one tick through the far side; no MARKET orders
  * stops by itself 60 s after the case ends or when the client logs out
  * API_READS_PER_SEC / API_WRITES_PER_SEC caps at the top

CASE FACTS (ALGO2 case brief; ALGO2e differs: see server values in the Connected line)
  one stock ALGO | 5,000 shares per order | 25,000 gross/net position limit
  (10c/share fine beyond) | MARKET orders pay 1c/share | filled LIMIT orders
  EARN 0.5c/share rebate | 300 s.  => never send a MARKET order unless the
  cockpit explicitly allows it; even to take liquidity we use a LIMIT priced
  at the far side (it fills instantly and is still a LIMIT for the rebate).

STRATEGY (every loop, ~20-40x per second)
  fair  = mid + micro_w*(microprice - mid) + drift_lean*(mid - mid a few s ago)
  r     = fair - skew,  skew = `Skew $ at max inv` * net / `Max inv`
  half  = max(`Min spread`/2, `Vol mult` * range of mid over `Vol window`)
  bid   = min(r - half, best_bid + improve)   ask = max(r + half, best_ask - improve)
          improve = 1 tick when the market spread >= `Improve if spread >=`
          (step inside the best to be first in line), else join the best.
  size  adding side shrinks linearly to 0 at |net| = `Max inv`;
        reducing side grows with |net| (`Reduce boost`), max 5,000/order,
        `Layers` extra levels 1 tick further out.
  Beyond `Hard reduce |net|` the reducing side is priced AT the far side
  (marketable LIMIT) for the excess.  In the last `End flat secs` the adding
  side is pulled and the book is flattened the same way.
  QUEUE PRIORITY: a resting order is only replaced when it is (a) more
  aggressive than fair allows, or (b) >= `Requote lag ticks` behind the
  desired price, or (c) the wrong size by > 50%.  Otherwise it stays put.

LESSONS FROM ALGO1 BAKED IN
  * POST /orders returns a pre-match snapshot and GET /orders lags ~0.1-0.6 s;
    GET /orders/{id} 404s for filled orders.  Orders are reconciled against
    OPEN + TRANSACTED + CANCELLED lists with a grace period; never "vanish".
  * Position: /securities is authoritative (one ticker here), and a fast
    estimate (last authoritative + fills seen since) drives the quotes.
  * Orphan sweep: any OPEN order of ours that nothing tracks is cancelled.
  * Excel (xlwings COM, slow) runs in its own thread - it never blocks trading.
  * Order rate limit read from /securities (api_orders_per_second) and
    respected with a sliding window; 429s retried with the server's wait.

LOGS  (folder `logs` next to this file, one set per run - send them all)
  algo2_<stamp>.log         events: params, every replace decision (why),
                            every fill, marks 3 s later, errors
  algo2_<stamp>_ticks.csv   one row per loop: book, fair, our quotes, net, P&L
  algo2_<stamp>_trades.csv  one row per fill
  algo2_<stamp>_orders.csv  our final order list

RUN (Spyder or `python algo2_mm.py`): set API_KEY / PORT.  Stop = Ctrl+C /
  red square or Panic=1 in Excel -> cancels everything and (if allowed)
  flattens.  The cockpit `ALGO2_cockpit.xlsx` is created next to the script.
"""

import os
import sys
import csv
import math
import bisect
import time
import threading
import traceback
from collections import deque
from concurrent.futures import ThreadPoolExecutor

import requests

# =============================================================================
# CONFIG
# =============================================================================
API_KEY = "YOUR API KEY HERE"      # <-- put your key here (RIT status bar, API icon)
PORT = 9999                        # <-- and the port
API_READS_PER_SEC = 160            # cap on GET calls per second for THIS copy (book, orders, case, limits)
API_WRITES_PER_SEC = 0             # cap on order commands per second (POST/cancel); 0 = use the server's
                                   # api_orders_per_second (500 on the ALGO2e server). 3 copies => 3x these.
TICKERS = ["RY", "CNR", "AC"]      # run `python algo2e_mm.py` (no arguments): it starts one
                                   # copy of itself per ticker, each in its own console.
                                   # CNR and AC lost money in both live runs (-$1.8k, -$2.5k);
                                   # add them back with TICKERS = ["RY", "CNR", "AC"] if wanted

TICKER = TICKERS[0]
# internal:  python algo2e_mm.py <TICKER> [API_KEY] [PORT]  runs ONE ticker
if len(sys.argv) > 1:
    TICKER = sys.argv[1].upper()
if len(sys.argv) > 2:
    API_KEY = sys.argv[2]
if len(sys.argv) > 3:
    PORT = int(sys.argv[3])
SWEEP_ORPHANS = False   # True = cancel untracked OPEN orders on OUR ticker (dangerous if a
                        # teammate trades this ticker by hand on the same account)
TICK = 0.01
CASE_MAX_ORDER = 5000
CASE_LIMIT = 25000

DEFAULTS = [
    # (name, default, note)                        -- all editable live in Excel
    ("Enabled",               1,     "1 = trade, 0 = cancel all and watch"),
    ("Quote size",            1000,  "shares per quote per side (before inventory scaling)"),
    ("Layers",                1,     "price levels per side (each 1 tick further out)"),
    ("Min spread",            0.01,  "our ask - our bid never tighter than this $"),
    ("Vol mult",              0.0,   "half spread >= this x (mid range over Vol window)"),
    ("Vol window",            3.0,   "seconds of mid history for the vol term"),
    ("Improve if spread >=",  0.02,  "step 1 tick inside the best when market spread >= this (0 = always join)"),
    ("Micro weight",          0.3,   "0..1 pull of fair toward the size-weighted microprice"),
    ("Drift window",          2.0,   "seconds: fair leans by Drift lean x (mid now - mid then)"),
    ("Drift lean",            0.3,   "0 = ignore trend, 1 = full lean"),
    ("Max inv",               2000,  "adding side is 0 here; skew is full here"),
    ("Skew $ at max inv",     0.04,  "$ both quotes shift when |net| = Max inv (only if spread >= Skew if spread)"),
    ("Skew if spread >=",     0.02,  "price skew only in markets at least this wide; tighter = size skew only"),
    ("Reduce boost",          0.5,   "reducing side size += this x |net|"),
    ("Hard reduce |net|",     3500,  "beyond this, cross the spread (LIMIT at far side) for the excess"),
    ("Requote lag ticks",     3,     "cancel a resting order once it is this many ticks behind the touch"),
    ("Aggressive ticks",      1.5,   "cancel a resting order once it is this many ticks INSIDE our desired top"),
    ("Requote min sec",       0.5,  "min seconds between replacements on one side"),
    ("Gross cap",             4000,  "self-imposed cap on |net| + open qty on a side, THIS ticker"),
    ("Account gross stop",    23000, "stop adding on every ticker when /limits gross (all tickers) reaches this"),
    ("Take edge",             0,     "0 = off. Take with a marketable LIMIT when the far touch is this $ beyond fair_ref (RY: taking is PAID)"),
    ("Fair window",           5.0,   "seconds: EWMA of mid used as fair_ref for Take edge"),
    ("Take max inv",          0,     "takes that ADD inventory allowed only while |net| < this (0 = Max inv)"),
    ("Mark secs",             10,    "seconds after a fill at which the edge is marked"),
    ("End flat secs",         12,    "SECONDS before the end: pull adding side, keep the reducing side passive"),
    ("End cross secs",        4,     "SECONDS before the end: flatten with LIMITs 1 tick through the far side, re-posted every 0.5 s (no MARKET)"),
    ("Allow market orders",   0,     "1 = MARKET also allowed for Flatten now / Panic / script stop (costs 1c/share)"),
    ("Poll seconds",          0.0,   "extra sleep per loop (0 = as fast as the rate limit allows)"),
    ("Book depth",            10,    "levels pulled per side"),
    ("Flatten now",           0,     "1 = cancel + flatten to 0 with far-side LIMITs (auto resets)"),
    ("Panic",                 0,     "1 = cancel, flatten, STOP script"),
    # --- team build: imbalance gate, health-scaled sizing, limit allocator (0 = off) ---
    ("Imb gate",              0.0,   "0 = off. Quote the bid only if touch imbalance >= this, the ask only if <= -this"),
    ("Imb reduce frac",       0.5,   "the reducing side ignores the gate once |net| >= this x Max inv"),
    ("Budget share",          0.0,   "0 = off. This ticker may use this share of Account gross stop, times health"),
    ("Health window",         60.0,  "seconds of book history for the depth / touch / volatility measures"),
    ("Depth ref",             0,     "0 = off. Top-5 depth per side (largest level excluded) at which depth health = 1"),
    ("Vol ref $",             0.0,   "0 = off. RMS of 10 s mid changes at which vol health = 1 (above: ref / sd)"),
    ("Maker rebate $",        0.0,   "per-share passive rebate (brief), added to the fill mark-out for the edge health"),
    ("Edge floor $",          0.0,   "0 = off. Edge health falls linearly from 1 at 0 to 0 at this (negative) net edge"),
    ("Edge min shares",       20000, "marked shares needed before the edge health is used"),
    ("Thin touch kill",       0,     "0 = off. No adding while the median touch size (Health window) is below this"),
    ("Wall kill x",           0,     "0 = off. No adding while the largest book level > this x median touch size"),
    ("Anchor kill %",         0.0,   "0 = off. No adding while the mid is more than this % from its median since the heat began"),
]
# per-ticker tuning (from the 2026-10-01 live run: RY wide/volatile = the P&L engine;
# CNR/AC 1c-locked thin books = join the touch small for the rebate only)
TICKER_OVERRIDES = {
    "RY":  {"Quote size": 5000, "Max inv": 10000, "Hard reduce |net|": 13000, "Gross cap": 15000,
            "Min spread": 0.03, "Vol mult": 1.0, "Requote lag ticks": 8, "Aggressive ticks": 5,
            "Requote min sec": 1.0, "Take edge": 0.03, "Reduce boost": 0.75},
    # team build. Budgets: RY 15,000 (Hashim's cap) + CNR 0.15 x 23,000 + AC 0.05 x 23,000 = 19,600 < 23,000
    "CNR": {"Quote size": 1500, "Max inv": 3000, "Hard reduce |net|": 4000, "Gross cap": 3500,
            "Min spread": 0.01, "Vol mult": 0.0, "Book depth": 40,
            "Imb gate": 0.3, "Budget share": 0.15, "Depth ref": 15000, "Vol ref $": 0.08,
            "Maker rebate $": 0.0023, "Edge floor $": -0.005, "Edge min shares": 20000},
    "AC":  {"Quote size": 500, "Max inv": 1000, "Hard reduce |net|": 1500, "Gross cap": 1150,
            "Min spread": 0.01, "Vol mult": 0.0, "Requote min sec": 0.3, "Book depth": 40,
            "Imb gate": 0.3, "Budget share": 0.05, "Depth ref": 10000, "Vol ref $": 0.05,
            "Maker rebate $": 0.0011, "Edge floor $": -0.005, "Edge min shares": 5000,
            "Thin touch kill": 5000, "Wall kill x": 50, "Anchor kill %": 5.0},
}
PARAM_ORDER = [d[0] for d in DEFAULTS]
PARAM_DEFAULTS = dict((d[0], d[1]) for d in DEFAULTS)
PARAM_DEFAULTS.update(TICKER_OVERRIDES.get(TICKER, {}))
PARAM_NOTES = dict((d[0], d[2]) for d in DEFAULTS)

USE_EXCEL = True
EXCEL_FILE = "ALGO2e_cockpit_%s.xlsx" % TICKER
LOG_DIR = "logs"

MAX_CALLS_PER_SEC = API_READS_PER_SEC   # read cap (orders have their own cap)
DEFAULT_ORDERS_PER_SEC = 20    # overwritten from /securities api_orders_per_second
SLOW_LANE_SEC = 0.10           # /case /securities /limits refresh (server position every 100 ms)
DONE_LOOKUP_SEC = 0.20         # how often missing orders are looked up in TRANSACTED/CANCELLED
ORDER_VISIBLE_SEC = 5.0        # server lists a new order late: no conclusions before this
ORDER_LOST_SEC = 15.0          # LIVE: fills are listed 3-5 s late (per tick); never forget early (server drops
                               # orders silently when the per-second order limit is hit)
MARK_SEC = 10.0                # mark each fill against the mid this long after (overridden by `Mark secs`)


# =============================================================================
# Logging
# =============================================================================
class RunLog(object):
    TICK_COLS = ["time", "tick", "loop_ms", "status", "net", "net_est", "room_buy", "room_sell",
                 "bid", "bid_sz", "ask", "ask_sz", "mid", "micro", "drift", "vol", "fair", "skew",
                 "half", "our_bid", "our_bid_qty", "our_ask", "our_ask_qty", "n_open",
                 "fills", "volume", "realized", "unrealized", "nlv", "calls_ps", "orders_ps",
                 "n429", "replaces", "acct_gross", "acct_net", "fair_ref", "settling"]
    TRADE_COLS = ["time", "tick", "side", "qty", "price", "fair", "mid", "bid", "ask",
                  "net_before", "order_id", "age_s", "reason"]

    def __init__(self, folder):
        os.makedirs(folder, exist_ok=True)
        self.stamp = time.strftime("%Y%m%d_%H%M%S")
        base = os.path.join(folder, "algo2e_%s_%s" % (TICKER, self.stamp))
        self.paths = {"log": base + ".log", "ticks": base + "_ticks.csv",
                      "trades": base + "_trades.csv", "orders": base + "_orders.csv"}
        self.f = open(self.paths["log"], "w")
        self.ticks_f = open(self.paths["ticks"], "w", newline="")
        self.ticks = csv.DictWriter(self.ticks_f, fieldnames=self.TICK_COLS)
        self.ticks.writeheader()
        self.trades_f = open(self.paths["trades"], "w", newline="")
        self.trades = csv.DictWriter(self.trades_f, fieldnames=self.TRADE_COLS)
        self.trades.writeheader()
        self.tick = ""
        self.last = ""
        self.t0 = time.time()
        self.lock = threading.Lock()

    def event(self, msg, console=True):
        line = "%s +%7.2fs t=%-3s %s" % (time.strftime("%H:%M:%S"),
                                          time.time() - self.t0, self.tick, msg)
        with self.lock:
            self.f.write(line + "\n")
            self.f.flush()
            self.last = msg
        if console:
            print(line)

    def tick_row(self, d):
        self.ticks.writerow(dict((k, d.get(k, "")) for k in self.TICK_COLS))

    def trade_row(self, d):
        self.trades.writerow(dict((k, d.get(k, "")) for k in self.TRADE_COLS))
        self.trades_f.flush()

    def dump_orders(self, orders):
        if not orders:
            return
        cols = sorted(set(k for o in orders for k in o.keys()))
        with open(self.paths["orders"], "w", newline="") as fh:
            w = csv.DictWriter(fh, fieldnames=cols)
            w.writeheader()
            for o in orders:
                w.writerow(o)

    def close(self):
        for fh in (self.f, self.ticks_f, self.trades_f):
            try:
                fh.close()
            except Exception:
                pass


LOG = None


def log(msg, console=True):
    if LOG:
        LOG.event(msg, console)
    else:
        print(time.strftime("%H:%M:%S"), msg)


# =============================================================================
# REST client
# =============================================================================
class ApiError(Exception):
    pass


class RIT(object):
    """Sliding-window throttles (all calls, and order commands separately),
    429 retry honouring the server's `wait`, thread-local sessions so the two
    sides of a quote can be posted in parallel."""

    def __init__(self, key, port):
        self.base = "http://localhost:%d/v1" % port
        self.key = key
        self.local = threading.local()
        self.lock = threading.Lock()
        self.stamps = deque()
        self.ostamps = deque()
        self.orders_per_sec = DEFAULT_ORDERS_PER_SEC
        self.max_calls = MAX_CALLS_PER_SEC
        self.last_recover = time.time()
        self.n_calls = 0
        self.n_429 = 0

    def _session(self):
        s = getattr(self.local, "s", None)
        if s is None:
            s = requests.Session()
            s.headers.update({"X-API-Key": self.key})
            self.local.s = s
        return s

    def _throttle(self, is_order):
        while True:
            with self.lock:
                now = time.time()
                while self.stamps and now - self.stamps[0] > 1.0:
                    self.stamps.popleft()
                while self.ostamps and now - self.ostamps[0] > 1.0:
                    self.ostamps.popleft()
                wait = 0.0
                if now - self.last_recover > 1.0 and self.max_calls < MAX_CALLS_PER_SEC:
                    self.max_calls += 1
                    self.last_recover = now
                if not is_order and len(self.stamps) >= self.max_calls:
                    wait = 1.0 - (now - self.stamps[0]) + 0.002
                if is_order and len(self.ostamps) >= self.orders_per_sec:
                    wait = max(wait, 1.0 - (now - self.ostamps[0]) + 0.002)
                if is_order and self.ostamps:
                    gap = 1.0 / self.orders_per_sec * 0.9
                    wait = max(wait, gap - (now - self.ostamps[-1]))
                if wait <= 0:
                    if is_order:
                        self.ostamps.append(now)
                    else:
                        self.stamps.append(now)
                    self.n_calls += 1
                    return
            time.sleep(min(wait, 0.2))

    def order_budget(self):
        """Order commands we may still send this second without blocking."""
        with self.lock:
            now = time.time()
            while self.ostamps and now - self.ostamps[0] > 1.0:
                self.ostamps.popleft()
            b = self.orders_per_sec - len(self.ostamps)
            if self.ostamps and now - self.ostamps[-1] < 1.0 / self.orders_per_sec * 0.9:
                b = min(b, 0)
            return b

    def calls_last_sec(self):
        with self.lock:
            now = time.time()
            while self.stamps and now - self.stamps[0] > 1.0:
                self.stamps.popleft()
            while self.ostamps and now - self.ostamps[0] > 1.0:
                self.ostamps.popleft()
            return len(self.stamps), len(self.ostamps)

    def _call(self, method, path, params, is_order=False, retries=5):
        r = None
        for _ in range(retries):
            self._throttle(is_order)
            r = self._session().request(method, self.base + path, params=params, timeout=3)
            if r.status_code != 429:
                return r
            self.n_429 += 1
            with self.lock:
                self.max_calls = max(40, int(self.max_calls * 0.9))
            try:
                wait = float(r.json().get("wait", 0.1))
            except Exception:
                wait = 0.1
            time.sleep(min(max(wait, 0.02), 1.0))
        return r

    def _get(self, path, **params):
        r = self._call("GET", path, params)
        if not r.ok:
            raise ApiError("GET %s -> %s: %s" % (path, r.status_code, r.text[:200]))
        return r.json()

    def case(self):
        return self._get("/case")

    def trader(self):
        return self._get("/trader")

    def limits(self):
        return self._get("/limits")

    def security(self):
        s = self._get("/securities", ticker=TICKER)
        return s[0] if isinstance(s, list) else s

    def book(self, limit):
        return self._get("/securities/book", ticker=TICKER, limit=limit)

    def orders(self, status="OPEN"):
        return self._get("/orders", status=status)

    def order(self, oid):
        return self._get("/orders/%d" % int(oid))

    def done_orders(self):
        out = {}
        for st in ("TRANSACTED", "CANCELLED"):
            for o in self.orders(st):
                out[o["order_id"]] = o
        return out

    def _post_order(self, params):
        t0 = time.time()
        r = self._call("POST", "/orders", params, is_order=True)
        ms = (time.time() - t0) * 1000
        if r.ok:
            o = r.json()
            log("ORDER ok %s -> id=%s filled=%s status=%s (%.0f ms)"
                % (params, o.get("order_id"), o.get("quantity_filled"), o.get("status"), ms),
                console=False)
            return o
        log("! ORDER FAIL %s: %s %s" % (params, r.status_code, r.text[:160]))
        return None

    def limit_order(self, action, qty, price):
        return self._post_order({"ticker": TICKER, "type": "LIMIT", "quantity": int(qty),
                                 "action": action, "price": round(price, 2)})

    def market_order(self, action, qty):
        log("! MARKET order suppressed (%s %d): this case allows LIMIT orders only" % (action, qty))
        return None

    def cancel_ids(self, ids):
        if not ids:
            return []
        r = self._call("POST", "/commands/cancel", {"ids": ",".join(str(i) for i in ids)},
                       is_order=True)
        try:
            return r.json().get("cancelled_order_ids", [])
        except Exception:
            return []

    def cancel_all(self):
        for i in range(5):
            r = self._call("POST", "/commands/cancel", {"ticker": TICKER}, is_order=True)
            log("cancel_all(%s) -> %s %s" % (TICKER, r.status_code, r.text[:120]), console=False)
            time.sleep(0.3)
            try:
                if not [o for o in self.orders("OPEN") if o.get("ticker") == TICKER]:
                    return True
            except ApiError:
                pass
        return False


# =============================================================================
# Excel cockpit - runs in its own thread, talks to the loop through `shared`
# =============================================================================
class Shared(object):
    def __init__(self):
        self.lock = threading.Lock()
        self.params = dict(PARAM_DEFAULTS)
        self.live = {}
        self.trades = deque()
        self.stop = False

    def get_params(self):
        with self.lock:
            return dict(self.params)

    def set_params(self, p):
        with self.lock:
            self.params.update(p)

    def set_live(self, d):
        with self.lock:
            self.live = d

    def add_trade(self, row):
        with self.lock:
            self.trades.append(row)


class Cockpit(threading.Thread):
    LIVE_LABELS = ["Updated", "Tick", "Case status", "NLV", "Realized (RIT)", "Unrealized (RIT)",
                   "Net position", "Net (fast est.)", "Fills", "Volume", "Fill edge/sh (3s mark)",
                   "Bid", "Bid size", "Ask", "Ask size", "Mid", "Fair", "Drift", "Vol range",
                   "Our bid", "Our bid qty", "Our ask", "Our ask qty", "Open orders",
                   "Replaces/min", "Account gross", "Account net", "Fair ref",
                   "API calls/s", "Orders/s", "Rate-limit hits", "Loop ms", "Last message"]
    TRADE_HEADER = ["Time", "Tick", "Side", "Qty", "Price", "Fair", "Net before", "Reason"]

    def __init__(self, path, shared):
        threading.Thread.__init__(self, name="cockpit")
        self.daemon = True
        self.path, self.shared = path, shared
        self.ready = threading.Event()
        self.error = None

    def run(self):
        try:
            try:
                import pythoncom
                pythoncom.CoInitialize()
            except Exception:
                pass
            import xlwings as xw
            new = not os.path.exists(self.path)
            self.wb = xw.Book() if new else xw.Book(self.path)
            if new:
                self.wb.save(self.path)
            self._setup()
            tr = self.wb.sheets["Trades"]
            self.trade_row = tr.range("A1").end("down").row + 1
            if self.trade_row > 100000:
                self.trade_row = 2
        except Exception as e:
            self.error = e
            self.ready.set()
            return
        self.ready.set()
        last_live = 0.0
        while not self.shared.stop:
            try:
                self._read_params()
                now = time.time()
                if now - last_live > 0.25:
                    last_live = now
                    self._write_live()
                    self._flush_trades()
            except Exception as e:
                log("! cockpit: %s" % e, console=False)
            time.sleep(0.25)

    def _sheet(self, name):
        if name in [s.name for s in self.wb.sheets]:
            return self.wb.sheets[name]
        return self.wb.sheets.add(name, after=self.wb.sheets[-1])

    def _setup(self):
        ctl = self._sheet("Control")
        labels = ctl.range("A2:A%d" % (1 + len(PARAM_ORDER))).value
        if labels != PARAM_ORDER:
            ctl.clear()
            ctl.range("A1").value = [["Parameter", "Value", "Notes"]]
            ctl.range("A2").value = [[k, PARAM_DEFAULTS[k], PARAM_NOTES[k]] for k in PARAM_ORDER]
            ctl.range("B2:B%d" % (1 + len(PARAM_ORDER))).color = (255, 255, 153)
            ctl.range("A1:C1").font.bold = True
            ctl.autofit()
        live = self._sheet("Live")
        live.clear()
        live.range("A1").value = [[k, ""] for k in self.LIVE_LABELS]
        live.range("A1:A%d" % len(self.LIVE_LABELS)).font.bold = True
        live.range("B1:B%d" % len(self.LIVE_LABELS)).color = (220, 235, 255)
        live.autofit()
        tr = self._sheet("Trades")
        if tr.range("A1:H1").value != self.TRADE_HEADER:
            tr.clear()
            tr.range("A1").value = [self.TRADE_HEADER]
            tr.range("A1:H1").font.bold = True
            tr.autofit()
        names = [s.name for s in self.wb.sheets]
        if "Sheet1" in names and len(names) > 3:
            self.wb.sheets["Sheet1"].delete()
        self.wb.sheets["Control"].activate()

    def _read_params(self):
        ctl = self.wb.sheets["Control"]
        vals = ctl.range("B2:B%d" % (1 + len(PARAM_ORDER))).value
        p = {}
        for k, v in zip(PARAM_ORDER, vals):
            if v is not None:
                try:
                    p[k] = float(v)
                except (TypeError, ValueError):
                    pass
        self.shared.set_params(p)
        if p.get("Flatten now"):
            ctl.range("B%d" % (2 + PARAM_ORDER.index("Flatten now"))).value = 0

    def _write_live(self):
        with self.shared.lock:
            values = dict(self.shared.live)
        if values:
            self.wb.sheets["Live"].range("B1").value = [[values.get(k, "")] for k in self.LIVE_LABELS]

    def _flush_trades(self):
        rows = []
        with self.shared.lock:
            while self.shared.trades:
                rows.append(self.shared.trades.popleft())
        if rows:
            self.wb.sheets["Trades"].range("A%d" % self.trade_row).value = rows
            self.trade_row += len(rows)


# =============================================================================
# Helpers
# =============================================================================
def rnd(px):
    return round(round(px / TICK) * TICK, 2)


def f2(x):
    return "" if x is None else x


def levels(side, me):
    out = []
    for o in side:
        if o.get("trader_id") == me:
            continue
        rem = (o.get("quantity") or 0) - (o.get("quantity_filled") or 0)
        if rem > 0:
            out.append((o["price"], rem))
    return out


def book_sides(b):
    if not isinstance(b, dict):
        raise ApiError("unexpected book: %r" % (b,))
    bid = b.get("bids", b.get("bid"))
    ask = b.get("asks", b.get("ask"))
    if bid is None or ask is None:
        raise ApiError("book keys %s" % list(b.keys()))
    return bid, ask


def agg_levels(lv):
    """Per-order levels (book order) -> [(price, total qty)] per price, best first."""
    out = []
    for px, q in lv:
        if out and abs(out[-1][0] - px) < 1e-9:
            out[-1] = (px, out[-1][1] + q)
        else:
            out.append((px, q))
    return out


class Health(object):
    """Live market measures for one ticker -> sizing factor in [0, 1] (team build).
    depth: top-5 levels per side minus the largest one (a wall is not depth we can rely on),
    the thinner side counts. touch: mean size at the best bid and ask. vol: sd of 10 s mid
    moves (RMS, so drift counts). anchor: median mid since the heat began. edge: our own fills marked `Mark secs` later, plus the maker rebate."""
    def __init__(self):
        self.hist = deque()                   # (t, depth, touch, wall, mid)
        self.anchor = []                      # sorted 1 s mid samples since start (one process = one heat)
        self.anchor_t = 0.0

    def update(self, now, ab, aa, window):
        if not ab or not aa:
            return
        d = []
        for side in (ab[:5], aa[:5]):
            qs = [q for _, q in side]
            d.append(sum(qs) - max(qs))
        wall = max(q for _, q in ab[:5] + aa[:5])
        self.hist.append((now, min(d), (ab[0][1] + aa[0][1]) / 2.0, wall, (ab[0][0] + aa[0][0]) / 2.0))
        if now - self.anchor_t >= 1.0:
            bisect.insort(self.anchor, self.hist[-1][4])
            self.anchor_t = now
        while self.hist and now - self.hist[0][0] > window:
            self.hist.popleft()

    def _vol10(self):
        h, out, j = list(self.hist), [], 0
        for t, _, _, _, m in h:
            while j < len(h) and h[j][0] <= t - 10.0:
                j += 1
            if j > 0:
                out.append(m - h[j - 1][4])
        if len(out) < 20:
            return 0.0
        return math.sqrt(sum(x * x for x in out) / len(out))     # RMS: a steady ramp counts as risk

    def factors(self, p, edge_net, marked):
        if len(self.hist) < 2 or self.hist[-1][0] - self.hist[0][0] < 10.0:
            return {"health": 0.0, "why": "warm-up"}           # no adding in the first 10 s
        med = lambda xs: sorted(xs)[len(xs) // 2]
        depth = med([x[1] for x in self.hist]); touch = med([x[2] for x in self.hist])
        wall, vol = self.hist[-1][3], self._vol10()
        f = {"depth": 1.0, "vol": 1.0, "edge": 1.0}
        if p["Depth ref"] > 0:
            f["depth"] = min(1.0, depth / float(p["Depth ref"]))
        if p["Vol ref $"] > 0 and vol > 0:
            f["vol"] = min(1.0, p["Vol ref $"] / vol)
        if p["Edge floor $"] < 0 and marked >= p["Edge min shares"]:
            f["edge"] = max(0.0, min(1.0, 1.0 - edge_net / p["Edge floor $"]))
        why = ""
        if p["Thin touch kill"] > 0 and touch < p["Thin touch kill"]:
            why = "thin touch %d" % touch
        if p["Wall kill x"] > 0 and touch > 0 and wall > p["Wall kill x"] * touch:
            why = "wall %d = %.0fx touch" % (wall, wall / touch)
        if p["Anchor kill %"] > 0 and len(self.anchor) >= 10:
            anc = self.anchor[len(self.anchor) // 2]
            dev = abs(self.hist[-1][4] / anc - 1.0) * 100.0
            if dev > p["Anchor kill %"]:
                why = "%.1f%% from anchor %.2f" % (dev, anc)
        hv = 0.0 if why else f["depth"] * f["vol"] * f["edge"]
        f.update({"health": hv, "why": why, "depth_sh": depth, "touch_sh": touch, "vol10": vol, "edge_net": edge_net})
        return f


def chunks(qty, max_order):
    out = []
    qty = int(qty)
    while qty > 0:
        c = min(qty, int(max_order))
        out.append(c)
        qty -= c
    return out


# =============================================================================
# Order tracking + fills
# =============================================================================
class Orders(object):
    """Everything we have ever posted this run, reconciled against the server."""

    def __init__(self, api, shared):
        self.api, self.shared = api, shared
        self.live = {}            # id -> rec (OPEN as far as we know)
        self.settling = {}        # id -> rec (cancel sent, final fill unknown)
        self.untracked_seen = {}  # id -> first time seen untracked
        self.fills, self.volume = 0, 0
        self.signed_fills = 0     # cumulative signed shares filled (our own complete record)
        self.last_fill_time = 0.0
        self.last_post_time = 0.0
        self.pending_marks = []
        self.mark_total, self.mark_qty = 0.0, 0
        self.last_done_lookup = 0.0
        self.ctx = {}
        self.mark_sec = MARK_SEC

    # ---- bookkeeping ------------------------------------------------------
    def track(self, o, side, price, qty, layer, reason):
        rec = {"id": o["order_id"], "side": side, "price": price, "qty": int(qty), "layer": layer,
               "filled": 0, "vwap": None, "t": time.time(), "last_open": time.time(), "reason": reason}
        self.live[rec["id"]] = rec
        self.last_post_time = time.time()
        # the POST response is usually a pre-match snapshot, but when it does report a fill
        # (marketable LIMITs) book it NOW: the position estimate must not wait for the listing
        try:
            if int(o.get("quantity_filled") or 0) > 0:
                self._apply(rec, o, LOG.tick if LOG else "")
                if o.get("status") and o.get("status") != "OPEN":
                    self.live.pop(rec["id"], None)
        except Exception as e:
            log("! filled_at_post: %s" % e)
        return rec

    def settling_qty(self, side):
        """Shares still possibly live on a side after we sent a cancel (unconfirmed)."""
        return sum(max(0, r["qty"] - r["filled"]) for r in list(self.settling.values())
                   if r["side"] == side and not r.get("cancel_ok"))

    def confirm_cancels(self, ids):
        """The server acknowledged these cancels: their unfilled rest can no longer trade."""
        for oid in ids or []:
            rec = self.settling.get(oid)
            if rec is not None:
                rec["cancel_ok"] = True

    def open_qty(self, side):
        return sum(r["qty"] - r["filled"] for r in list(self.live.values()) if r["side"] == side)

    def exposure(self, side):
        """live + settling: what could still fill on this side."""
        return self.open_qty(side) + self.settling_qty(side)

    def on_side(self, side):
        return sorted((r for r in list(self.live.values()) if r["side"] == side),
                      key=lambda r: r["layer"])

    def retire(self, rec):
        self.live.pop(rec["id"], None)
        rec["t_cancel"] = time.time()
        self.settling[rec["id"]] = rec

    def _apply(self, rec, o, tick):
        filled = int(o.get("quantity_filled") or 0)
        if filled > rec["filled"]:
            v1 = o.get("vwap") or rec["price"]
            v0 = rec["vwap"] or rec["price"]
            d = filled - rec["filled"]
            px = (v1 * filled - v0 * rec["filled"]) / d
            rec["filled"], rec["vwap"] = filled, v1
            self._fill(rec, d, round(px, 4), tick)

    def _fill(self, rec, qty, price, tick):
        self.fills += 1
        self.volume += qty
        sign = 1 if rec["side"] == "BUY" else -1
        self.signed_fills += sign * qty
        self.last_fill_time = time.time()
        c = self.ctx
        age = time.time() - rec["t"]
        log("FILL %s %d @ %.2f | fair %s mid %s bid %s ask %s | net_est %s | age %.1fs | %s"
            % (rec["side"], qty, price, f2(c.get("fair")), f2(c.get("mid")), f2(c.get("bid")),
               f2(c.get("ask")), c.get("net_est"), age, rec["reason"]))
        self.pending_marks.append((time.time(), rec["side"], qty, price))
        if LOG:
            LOG.trade_row({"time": time.strftime("%H:%M:%S"), "tick": tick, "side": rec["side"],
                           "qty": qty, "price": price, "fair": f2(c.get("fair")),
                           "mid": f2(c.get("mid")), "bid": f2(c.get("bid")), "ask": f2(c.get("ask")),
                           "net_before": c.get("net_est"), "order_id": rec["id"],
                           "age_s": round(age, 2), "reason": rec["reason"]})
        self.shared.add_trade([time.strftime("%H:%M:%S"), tick, rec["side"], qty, price,
                               f2(c.get("fair")), c.get("net_est"), rec["reason"]])

    def mark_fills(self, mid):
        if mid is None:
            return
        now = time.time()
        keep = []
        for rec in self.pending_marks:
            t, side, qty, price = rec
            if now - t < self.mark_sec:
                keep.append(rec)
                continue
            adv = (mid - price) * (1 if side == "BUY" else -1)
            self.mark_total += adv * qty
            self.mark_qty += qty
            log("MARK %s %d @ %.2f -> mid %.3f: %+.3f/sh" % (side, qty, price, mid, adv),
                console=False)
        self.pending_marks = keep

    def mark_edge(self):
        return self.mark_total / self.mark_qty if self.mark_qty else 0.0

    # ---- reconciliation (one GET per loop, done-lists occasionally) --------
    def sync(self, tick, now):
        try:
            open_ = dict((o["order_id"], o) for o in self.api.orders("OPEN"))
        except ApiError as e:
            log("! sync: %s" % e)
            return None
        missing = [r for r in list(self.live.values()) if r["id"] not in open_]
        need_done = bool(self.settling) or any(
            now - r["t"] > 0.05 for r in missing)
        done = None
        if need_done and now - self.last_done_lookup >= DONE_LOOKUP_SEC:
            self.last_done_lookup = now
            try:
                done = self.api.done_orders()
            except ApiError as e:
                log("! done_orders: %s" % e)
                done = None
        for rec in list(self.live.values()):
            o = open_.get(rec["id"])
            if o is not None:
                rec["last_open"] = now
            if o is None:
                if done is None:
                    continue
                o = done.get(rec["id"])
                if o is None:
                    if now - rec.get("last_open", rec["t"]) > ORDER_LOST_SEC:
                        log("! order %s (%s %d @ %.2f) never listed in %.1fs - server dropped it? "
                            "re-posting" % (rec["id"], rec["side"], rec["qty"], rec["price"], ORDER_LOST_SEC))
                        del self.live[rec["id"]]
                    continue
            self._apply(rec, o, tick)
            if o.get("status") != "OPEN":
                del self.live[rec["id"]]
        if self.settling and done is not None:
            for oid, rec in list(self.settling.items()):
                o = done.get(oid)
                if o is not None:
                    self._apply(rec, o, tick)
                    del self.settling[oid]
                elif oid in open_:
                    o = open_[oid]
                    self._apply(rec, o, tick)
                    if now - rec["t_cancel"] > 1.0:       # cancel did not take: retry
                        self.api.cancel_ids([oid])
                        rec["t_cancel"] = now
                elif now - rec["t_cancel"] > ORDER_LOST_SEC:
                    del self.settling[oid]
        self._sweep(open_, now)
        return open_

    def _sweep(self, open_, now):
        mine = set(self.live) | set(self.settling)
        for oid in list(self.untracked_seen):
            if oid not in open_ or oid in mine:
                del self.untracked_seen[oid]
        stale = []
        if not SWEEP_ORPHANS:
            return
        for oid in open_:
            if oid in mine or open_[oid].get("ticker") != TICKER:
                continue
            t0 = self.untracked_seen.setdefault(oid, now)
            if now - t0 >= 0.5:
                stale.append(oid)
        if stale:
            got = self.api.cancel_ids(stale)
            log("ORPHAN cancel %s -> %s" % ([(open_[i]["action"], open_[i]["price"],
                                              open_[i]["quantity"]) for i in stale][:5], got))
            for oid in stale:
                self.untracked_seen.pop(oid, None)

    def cancel_side(self, side, reason):
        recs = [r for r in list(self.live.values()) if r["side"] == side]
        if recs:
            for r in recs:
                self.retire(r)
            self.confirm_cancels(self.api.cancel_ids([r["id"] for r in recs]))
            log("CANCEL %s x%d (%s)" % (side, len(recs), reason), console=False)

    def cancel_all_tracked(self, reason):
        recs = list(self.live.values())
        if recs:
            self.api.cancel_ids([r["id"] for r in recs])
            for r in recs:
                self.retire(r)
            log("CANCEL all x%d (%s)" % (len(recs), reason))


# =============================================================================
# Quoting engine
# =============================================================================
class Quoter(object):
    def __init__(self, api, orders):
        self.api, self.orders = api, orders
        self.mid_hist = deque()
        self.last_replace = {"BUY": 0.0, "SELL": 0.0}
        self.replaces = 0
        self.replace_times = deque()
        self.pool = ThreadPoolExecutor(max_workers=2)
        self.pending = {"BUY": None, "SELL": None}   # in-flight cancel/post job per side
        self.fair_ref = None                          # EWMA of mid (`Fair window`)
        self.fair_t = None
        self.last_take = {"BUY": 0.0, "SELL": 0.0}
        self.acct_gross = 0.0                         # from /limits (all tickers)
        self.acct_net = 0.0
        self.imb = None                               # team build: touch imbalance (others' orders)
        self.health_now = None                        # team build: Health.factors() dict

    def record_mid(self, mid, now, window, fair_window=5.0):
        if mid is not None:
            self.mid_hist.append((now, mid))
            if self.fair_ref is None:
                self.fair_ref, self.fair_t = mid, now
            else:
                dt = max(0.0, now - self.fair_t)
                a = 1.0 - math.exp(-dt / max(fair_window, 0.2))
                self.fair_ref += a * (mid - self.fair_ref)
                self.fair_t = now
        while self.mid_hist and now - self.mid_hist[0][0] > window:
            self.mid_hist.popleft()

    def vol_range(self):
        if len(self.mid_hist) < 2:
            return 0.0
        m = [x[1] for x in self.mid_hist]
        return max(m) - min(m)

    def drift(self, now, window):
        if len(self.mid_hist) < 2:
            return 0.0
        cur = self.mid_hist[-1][1]
        then = None
        for t, m in self.mid_hist:
            if now - t <= window:
                then = m
                break
        if then is None:
            then = self.mid_hist[0][1]
        return cur - then

    def compute(self, p, bb, bs, ba, as_, net, now):
        """Return dict with fair/skew/half and desired quotes per side:
        {side: [(price, qty, kind), ...]} (kind = 'passive'|'cross')."""
        mid = (bb + ba) / 2.0
        micro = (bb * as_ + ba * bs) / float(bs + as_) if (bs + as_) > 0 else mid
        drift = self.drift(now, p["Drift window"])
        fair = mid + p["Micro weight"] * (micro - mid) + p["Drift lean"] * drift
        max_inv = max(p["Max inv"], 1.0)
        cap = p["Gross cap"]
        base = int(p["Quote size"])
        hh = self.health_now
        if p["Budget share"] > 0 and hh is not None:
            # allocator: a fixed share of the account budget, scaled by live health
            budget = p["Budget share"] * p["Account gross stop"] * hh["health"]
            cap = min(cap, budget)
            max_inv = max(min(max_inv, budget), 1.0)
            base = int(base * hh["health"])
            if base < 100:
                base = 0
        inv = max(-1.5, min(1.5, net / max_inv))
        # in a tick-constrained market a price skew just takes us off the touch
        # (no fills at all); there the skew lives in the sizes only
        wide = ba - bb >= p["Skew if spread >="] - 1e-9
        skew = p["Skew $ at max inv"] * inv if wide else 0.0
        r = fair - skew
        vol = self.vol_range()
        half = max(p["Min spread"] / 2.0, p["Vol mult"] * vol)
        improve = TICK if (p["Improve if spread >="] <= 0 or
                           ba - bb >= p["Improve if spread >="] - 1e-9) else 0.0
        bid_px = min(rnd(r - half), rnd(bb + improve), rnd(ba - TICK))
        ask_px = max(rnd(r + half), rnd(ba - improve), rnd(bb + TICK))
        min_sp = max(p["Min spread"], TICK)
        if ask_px - bid_px < min_sp - 1e-9:
            if net > 0:                       # long: keep the ask, push the bid down
                bid_px = rnd(ask_px - min_sp)
            elif net < 0:
                ask_px = rnd(bid_px + min_sp)
            else:
                bid_px, ask_px = rnd(r - min_sp / 2.0), rnd(r + min_sp / 2.0)
                if ask_px - bid_px < min_sp - 1e-9:
                    ask_px = rnd(bid_px + min_sp)

        a = abs(net)
        room_buy = int(cap - net)            # shares we may still buy before net hits +cap
        room_sell = int(cap + net)           # ... sell before net hits -cap
        # account-wide guard (/limits gross and net cover ALL tickers / all copies of the bot)
        acct_stop = p["Account gross stop"]
        acct_room = max(0, int(acct_stop - self.acct_gross))
        if net >= 0:
            room_buy = min(room_buy, acct_room)
        if net <= 0:
            room_sell = min(room_sell, acct_room)
        if self.acct_net > acct_stop:
            room_buy = min(room_buy, 0)
        if self.acct_net < -acct_stop:
            room_sell = min(room_sell, 0)
        layers = max(1, int(p["Layers"]))
        # per-layer size and a cap on the TOTAL resting on each side:
        #   adding side   -> shrinks to 0 at |net| = Max inv, total <= Max inv - |net|
        #   reducing side -> grows with |net|, total <= |net| + base (a sweep through
        #                    every layer flips us by at most one base size)
        size_add = int(base * max(0.0, 1.0 - a / max_inv))
        tot_add = int(max(0.0, max_inv - a))
        size_red = int(base + p["Reduce boost"] * a)
        tot_red = int(a + base // 2)
        size_red = min(size_red, tot_red)
        if net > 0:
            buy_sz, buy_tot, sell_sz, sell_tot = size_add, tot_add, size_red, tot_red
        elif net < 0:
            buy_sz, buy_tot, sell_sz, sell_tot = size_red, tot_red, size_add, tot_add
        else:
            buy_sz = sell_sz = base
            buy_tot = sell_tot = base * layers
        buy_tot, sell_tot = min(buy_tot, room_buy), min(sell_tot, room_sell)

        want = {"BUY": [], "SELL": []}
        left_b, left_s = buy_tot, sell_tot
        for i in range(layers):
            qb, qs = min(buy_sz, left_b), min(sell_sz, left_s)
            if qb > 0:
                want["BUY"].append((rnd(bid_px - i * TICK), qb, "passive"))
                left_b -= qb
            if qs > 0:
                want["SELL"].append((rnd(ask_px + i * TICK), qs, "passive"))
                left_s -= qs
        # team build: imbalance gate - quote only the side the book favours; the reducing
        # side is exempt once inventory is large (or the allocator has shrunk max_inv)
        g = p["Imb gate"]
        if g > 0:
            imb = self.imb if self.imb is not None else 0.0
            red_ok = a >= p["Imb reduce frac"] * max_inv
            if not (imb >= g or (net < 0 and red_ok)):
                want["BUY"] = []
            if not (imb <= -g or (net > 0 and red_ok)):
                want["SELL"] = []
        # inventory beyond the hard line: cross for the excess (LIMIT at far side),
        # replacing the passive reducing layers
        hard = p["Hard reduce |net|"]
        if a > hard:
            excess = int(a - min(max_inv, hard))
            if net > 0:
                want["BUY"] = []                           # never add while crossing
                want["SELL"] = [(bb, int(min(excess, room_sell, max(bs, 100))), "cross")]
            else:
                want["SELL"] = []
                want["BUY"] = [(ba, int(min(excess, room_buy, max(as_, 100))), "cross")]
        return {"mid": mid, "micro": micro, "drift": drift, "fair": fair, "skew": skew,
                "half": half, "vol": vol, "want": want, "room_buy": room_buy,
                "room_sell": room_sell, "bid_px": bid_px, "ask_px": ask_px,
                "tot": {"BUY": buy_tot, "SELL": sell_tot}, "fair_ref": self.fair_ref}

    def take(self, p, q, bb, bs, ba, as_, net, now, tick):
        """Marketable LIMIT against a dislocated touch (RY: taking liquidity is PAID).
        BUY at the ask when it is `Take edge` below fair_ref (5 s EWMA of mid), SELL at the
        bid when it is `Take edge` above. Backtest on both RY runs: +7.2c/sh (wide regime)
        and +3.8c/sh (1c regime) vs mid 10 s later at edge 0.03. One take per side per
        0.5 s; adding takes capped by `Take max inv`."""
        edge = p["Take edge"]
        if edge <= 0 or self.fair_ref is None or len(self.mid_hist) < 5:
            return
        fr = self.fair_ref
        base = int(p["Quote size"])
        tmax = p["Take max inv"] or p["Max inv"]
        for side, px, sz, ok in (("BUY", ba, as_, ba <= fr - edge),
                                 ("SELL", bb, bs, bb >= fr + edge)):
            if not ok or now - self.last_take[side] < 0.5:
                continue
            adding = (side == "BUY" and net >= 0) or (side == "SELL" and net <= 0)
            if adding and abs(net) >= tmax:
                continue
            room = q["room_buy"] if side == "BUY" else q["room_sell"]
            if adding:
                room = min(room, int(tmax - abs(net)))
            qty = int(min(sz, base, room, CASE_MAX_ORDER))
            if qty < 100:
                continue
            # never trade through our own order on the other side: resting, or with a cancel
            # still in flight (settling), or with a post/cancel job still running
            opp = "SELL" if side == "BUY" else "BUY"
            if self.pending[opp] is not None and not self.pending[opp].done():
                continue
            crosses = (lambda r: r["price"] <= px) if side == "BUY" else (lambda r: r["price"] >= px)
            if any(crosses(r) for r in self.orders.on_side(opp)):
                continue
            if any(crosses(r) for r in list(self.orders.settling.values())
                   if r["side"] == opp and not r.get("cancel_ok") and r["qty"] > r["filled"]):
                continue
            if self.api.order_budget() < 1:
                continue
            self.last_take[side] = now
            o = self.api.limit_order(side, qty, px)
            if o:
                self.orders.track(o, side, px, qty, 0, "take")
                log("TAKE %s %d @ %.2f | fair_ref %.3f bid %.2f ask %.2f net %d" % (side, qty, px, fr, bb, ba, net))

    def reconcile(self, p, q, tick, now):
        """Order-level reconciliation, built for thick tick-constrained books:
          * an order that is more aggressive than the desired top is cancelled
            (urgent - risk);
          * an order more than `Requote lag ticks` behind the touch is cancelled
            (non-urgent - it is dead weight);
          * orders in between are KEPT - they hold a queue position that becomes
            the front of the book when the touch moves back;
          * if nothing of ours rests AT the desired top price, a new order is
            posted there immediately (this is the race for a fresh price level);
          * total resting per side is capped (furthest-behind cancelled first).
        Nothing is cancelled unless there is order budget to re-post."""
        for side in ("BUY", "SELL"):
            fut = self.pending[side]
            if fut is not None:
                if not fut.done():
                    continue                     # cancel/post for this side still in flight
                self.pending[side] = None
                try:
                    fut.result()
                except Exception as e:
                    log("! post job %s: %s" % (side, e))
            want = q["want"][side]
            have = self.orders.on_side(side)
            if not want:
                if have:
                    self.orders.cancel_side(side, "pull side")
                continue
            top_px, top_qty, kind = want[0]
            sgn = 1 if side == "BUY" else -1
            # --- crossing order (hard inventory reduction) -------------------
            if kind == "cross":
                if have and have[0].get("reason") == "cross":
                    continue
                opp = "SELL" if side == "BUY" else "BUY"
                if self.orders.on_side(opp) or self.pending[opp] is not None:
                    continue                 # opposite side still being pulled: do not self-trade
                if self.orders.settling_qty(side) > 0:
                    continue                 # a cancel on this side is unconfirmed: wait one sync
                ids = [r["id"] for r in have]
                for r in have:
                    self.orders.retire(r)
                self._launch(side, ids, want, "hard reduce", tick, now)
                continue
            drop, keep = [], []
            lag = p["Requote lag ticks"] * TICK
            for r in have:
                behind = (top_px - r["price"]) * sgn        # >0 = behind the touch
                if r.get("reason") == "cross":
                    drop.append((r, "cross done"))
                elif behind < -p["Aggressive ticks"] * TICK:
                    drop.append((r, "too aggressive"))
                elif behind > lag + 1e-9:
                    drop.append((r, "%d ticks behind" % round(behind / TICK)))
                else:
                    keep.append(r)
            # --- size cap: cancel furthest-behind first --------------------------
            cap = q["tot"][side]
            rem = lambda r: r["qty"] - r["filled"]
            keep.sort(key=lambda r: (top_px - r["price"]) * sgn)    # nearest first
            total = sum(rem(r) for r in keep) + self.orders.settling_qty(side)
            while keep and total > cap + 1:
                r = keep.pop()
                total -= rem(r)
                drop.append((r, "over size cap"))
            at_top = sum(rem(r) for r in keep if abs(r["price"] - top_px) < TICK / 2)
            missing = min(top_qty, cap - (total - at_top)) - at_top
            post = []
            if missing >= max(100, 0.25 * top_qty):
                post.append((top_px, int(missing), "top"))
            for px, qty, _ in want[1:]:
                if not any(abs(r["price"] - px) < TICK / 2 for r in keep):
                    room = cap - total - sum(x[1] for x in post)
                    if room >= max(100, 0.25 * qty):
                        post.append((px, int(min(qty, room)), "layer"))
            urgent = any(why in ("too aggressive", "cross done") for _, why in drop) or \
                (post and post[0][2] == "top" and at_top == 0)
            if not urgent and now - self.last_replace[side] < p["Requote min sec"]:
                continue
            budget = self.api.order_budget()
            need = (1 if drop else 0) + sum(len(chunks(x[1], CASE_MAX_ORDER)) for x in post)
            if budget < need:
                if not urgent:
                    continue
                post = post[:max(0, budget - (1 if drop else 0))]
            if drop:
                for r, why in drop:
                    self.orders.retire(r)
                log("CANCEL %s %s" % (side, [(r["price"], rem(r), why) for r, why in drop]),
                    console=False)
            if post:
                self.last_replace[side] = now
                self.replaces += 1
                self.replace_times.append(now)
            if drop or post:
                self._launch(side, [r["id"] for r, _ in drop], post,
                             "top" if at_top == 0 else "refill", tick, now)
        while self.replace_times and now - self.replace_times[0] > 60:
            self.replace_times.popleft()

    def _launch(self, side, cancel_ids, post, reason, tick, now):
        """Cancel + post for one side in a worker thread. The main loop keeps
        reading the book while the (rate-limited) order commands go out; the
        side is skipped until the job is done so nothing is posted twice."""
        def job():
            if cancel_ids:
                self.orders.confirm_cancels(self.api.cancel_ids(cancel_ids))
            if post:
                self._post_side(side, post, reason, tick)
        self.pending[side] = self.pool.submit(job)

    def wait_jobs(self):
        for side, fut in self.pending.items():
            if fut is not None:
                try:
                    fut.result(timeout=5)
                except Exception as e:
                    log("! post job %s: %s" % (side, e))
                self.pending[side] = None

    def _post_side(self, side, want, reason, tick):
        for layer, (px, qty, kind) in enumerate(want):
            for c in chunks(qty, CASE_MAX_ORDER):
                o = self.api.limit_order(side, c, px)
                if o:
                    self.orders.track(o, side, px, c, layer, kind if kind == "cross" else reason)
                    log("POST %s %d @ %.2f (%s/%s)" % (side, c, px, kind, reason), console=False)


# =============================================================================
# Flatten (LIMIT at far side; MARKET only if allowed)
# =============================================================================
def flatten(api, orders, net, bb, ba, allow_market, reason):
    """`net` is ignored for sizing: always re-read the server position and never
    stack a second flatten while one is still resting."""
    if any(r["reason"].startswith("flatten") for r in orders.live.values()):
        return
    try:
        net = int(api.security().get("position") or 0)
    except ApiError as e:
        log("! flatten: %s" % e)
        return
    if net == 0:
        return
    side = "SELL" if net > 0 else "BUY"
    px = bb if net > 0 else ba
    if px is None:
        if allow_market:
            for c in chunks(abs(net), CASE_MAX_ORDER):
                api.market_order(side, c)
        return
    for c in chunks(abs(net), CASE_MAX_ORDER):
        o = api.limit_order(side, c, px)
        if o:
            orders.track(o, side, px, c, 0, "flatten:" + reason)
    log("FLATTEN %s %d @ %.2f (%s)" % (side, abs(net), px, reason))


# =============================================================================
# Main
# =============================================================================
def main():
    global LOG, CASE_MAX_ORDER
    try:
        here = os.path.dirname(os.path.abspath(__file__))
    except NameError:
        here = os.getcwd()
    LOG = RunLog(os.path.join(here, LOG_DIR))
    print("Log files:\n  " + "\n  ".join(LOG.paths.values()))

    api = RIT(API_KEY, PORT)
    try:
        case = api.case()
        trader = api.trader()
        sec = api.security()
    except Exception as e:
        log("Cannot reach RIT at localhost:%d - client running? key right?\n%s" % (PORT, e))
        return
    me = trader["trader_id"]
    CASE_MAX_ORDER = int(sec.get("max_trade_size") or CASE_MAX_ORDER)
    ops = sec.get("api_orders_per_second")
    if API_WRITES_PER_SEC > 0:
        api.orders_per_sec = int(API_WRITES_PER_SEC)
    elif ops:
        api.orders_per_sec = max(int(ops), 10)
    log("Connected: %s %s (%s) | case '%s' | tick %s/%s | status %s | fee %s rebate %s | "
        "max order %s | orders/s %s (using %d) | reads/s cap %d"
        % (trader.get("first_name"), trader.get("last_name"), me, case.get("name"), case.get("tick"),
           case.get("ticks_per_period"), case.get("status"), sec.get("trading_fee"),
           sec.get("limit_order_rebate"), sec.get("max_trade_size"), ops, api.orders_per_sec,
           API_READS_PER_SEC))
    log("SECURITY %s" % sec, console=False)
    try:
        log("LIMITS %s" % api.limits(), console=False)
    except Exception as e:
        log("! limits: %s" % e)

    shared = Shared()
    cockpit = None
    if USE_EXCEL:
        cockpit = Cockpit(os.path.join(here, EXCEL_FILE), shared)
        cockpit.start()
        cockpit.ready.wait(20)
        if cockpit.error:
            log("Excel cockpit unavailable (%s) - running with DEFAULTS" % cockpit.error)
            cockpit = None
        else:
            log("Excel cockpit open: %s" % os.path.join(here, EXCEL_FILE))

    orders = Orders(api, shared)
    quoter = Quoter(api, orders)
    health = Health()
    last_health_log = 0.0
    p = shared.get_params()
    p_logged = {}
    status, tick = case.get("status"), case.get("tick", 0)
    tpp = case.get("ticks_per_period", 300)
    net, net_est, nlv, realized, unreal = 0, 0, None, 0.0, 0.0
    net_offset, off_since = int(sec.get("position") or 0), None
    sec_per_tick, tick_seen_at, tick_seen = None, None, None
    stopped_since, n401 = None, 0
    net_prev, net_changed_at, est_prev, est_changed_at = None, 0.0, None, 0.0
    last_slow, last_tick, last_flat = 0.0, -1, 0.0
    last_status = "ACTIVE"
    stop_reason = "stopped by user"
    n_err = 0
    bb = ba = None
    end_mode = False

    try:
        while True:
            try:
                t_loop = time.time()
                slow = t_loop - last_slow >= SLOW_LANE_SEC
                if slow:
                    last_slow = t_loop
                    p = shared.get_params()
                    changed = dict((k, v) for k, v in p.items() if p_logged.get(k) != v)
                    if changed:
                        log("PARAMS %s" % changed)
                        p_logged = dict(p)
                    case = api.case()
                    status, tick = case.get("status"), case.get("tick", 0)
                    tpp = case.get("ticks_per_period", tpp)
                    LOG.tick = tick
                    n401 = 0
                    if tick != tick_seen:
                        if tick_seen is not None and tick > tick_seen:
                            d = (t_loop - tick_seen_at) / (tick - tick_seen)
                            sec_per_tick = d if sec_per_tick is None else 0.7 * sec_per_tick + 0.3 * d
                        tick_seen, tick_seen_at = tick, t_loop
                    try:
                        lim = api.limits()
                        if lim:
                            quoter.acct_gross = float(lim[0].get("gross") or 0.0)
                            quoter.acct_net = float(lim[0].get("net") or 0.0)
                    except Exception as e:
                        log("! limits: %s" % e, console=False)
                    orders.mark_sec = p.get("Mark secs", MARK_SEC) or MARK_SEC
                    sec = api.security()
                    net = int(sec.get("position") or 0)
                    # /securities already contains fills the order listing has not
                    # shown us yet, so it cannot simply be added to recent fills.
                    # Our own fill record is complete; re-anchor it to the server
                    # only when nothing has been posted or filled for a while.
                    # A fill changes the server position immediately but reaches
                    # our record only after the listing lag (<= ~0.5 s).  So if the
                    # server position has not moved for 0.8 s, every fill it
                    # contains is already in our record and a mismatch is real.
                    if net != net_prev:
                        net_prev, net_changed_at = net, t_loop
                    est_now = orders.signed_fills + net_offset
                    if est_now != est_prev:
                        est_prev, est_changed_at = est_now, t_loop
                    quiet = (t_loop - net_changed_at > 6.0 and t_loop - est_changed_at > 6.0
                             and t_loop - orders.last_post_time > 6.0
                             and t_loop - orders.last_fill_time > 6.0)
                    if quiet and est_now != net:
                        log("position resync: est %d -> server %d (adj %+d)"
                            % (est_now, net, net - est_now), console=False)
                        net_offset = net - orders.signed_fills
                        off_since = None
                    elif est_now != net:
                        off_since = off_since or t_loop
                        if t_loop - off_since > 12.0:
                            log("! position est %d vs server %d for 12s - forcing resync" % (est_now, net))
                            net_offset = net - orders.signed_fills
                            off_since = None
                    else:
                        off_since = None
                    realized = sec.get("realized") or 0.0
                    unreal = sec.get("unrealized") or 0.0

                if p["Panic"]:
                    log("PANIC - cancelling, flattening, stopping")
                    quoter.wait_jobs()
                    api.cancel_all()
                    orders.live = {}
                    net = api.security().get("position") or 0
                    flatten(api, orders, int(net), bb, ba, p["Allow market orders"], "panic")
                    stop_reason = "panic"
                    break
                if status != "ACTIVE":
                    if status != last_status:
                        log("case %s - waiting (cancelling quotes)" % status)
                        quoter.wait_jobs()
                        orders.cancel_all_tracked("case " + str(status))
                        stopped_since = t_loop
                    last_status = status
                    if status == "STOPPED" and tick >= tpp - 1 and stopped_since and t_loop - stopped_since > 60:
                        log("case over for 60 s - stopping")
                        stop_reason = "case over"
                        break
                    time.sleep(0.5)
                    continue
                if last_status != "ACTIVE":
                    log("case ACTIVE - trading")
                    last_status = "ACTIVE"

                # --- book (own orders excluded) ------------------------------
                bids, asks = book_sides(api.book(int(p["Book depth"])))
                lb, la = levels(bids, me), levels(asks, me)
                if not lb or not la:
                    time.sleep(0.05)
                    continue
                bb, bs = lb[0]
                ba, as_ = la[0]
                quoter.record_mid((bb + ba) / 2.0, t_loop, p["Vol window"], p["Fair window"])
                ab, aa = agg_levels(lb), agg_levels(la)
                quoter.imb = (ab[0][1] - aa[0][1]) / float(ab[0][1] + aa[0][1])
                health.update(t_loop, ab, aa, p["Health window"])

                # --- our orders / fills --------------------------------------
                orders.ctx = {"fair": None, "mid": round((bb + ba) / 2.0, 3), "bid": bb, "ask": ba,
                              "net_est": net_est}
                orders.sync(tick, t_loop)
                net_est = orders.signed_fills + net_offset
                orders.ctx["net_est"] = net_est
                orders.mark_fills((bb + ba) / 2.0)
                quoter.health_now = health.factors(p, orders.mark_edge() + p["Maker rebate $"], orders.mark_qty)
                if t_loop - last_health_log > 5.0 and p["Budget share"] > 0:
                    last_health_log = t_loop
                    hn = quoter.health_now
                    log("HEALTH %.2f %s | imb %+.2f depth %s touch %s vol10 %s edge %s" % (
                        hn["health"], hn.get("why", ""), quoter.imb, hn.get("depth_sh", "-"), hn.get("touch_sh", "-"),
                        ("%.3f" % hn["vol10"]) if "vol10" in hn else "-", ("%+.4f" % hn["edge_net"]) if "edge_net" in hn else "-"))

                # --- quotes --------------------------------------------------
                active = bool(p["Enabled"])
                # size off whichever of server position / local estimate is larger in magnitude:
                # a stale number must make us reduce too early, never add past the caps
                net_q = net if (abs(net) > abs(net_est) and net * net_est >= 0) else net_est
                q = quoter.compute(p, bb, bs, ba, as_, net_q, t_loop)
                orders.ctx["fair"] = round(q["fair"], 4)
                ticks_left = tpp - tick
                spt = sec_per_tick or 1.0
                secs_left = ticks_left * spt
                if p["Flatten now"] and t_loop - last_flat > 1.5:
                    p["Flatten now"] = 0
                    quoter.wait_jobs()
                    shared.set_params({"Flatten now": 0})
                    orders.cancel_all_tracked("flatten now")
                    flatten(api, orders, net_est, bb, ba, p["Allow market orders"], "excel")
                    last_flat = t_loop
                elif not active:
                    if orders.live:
                        quoter.wait_jobs()
                        orders.cancel_all_tracked("disabled")
                elif secs_left <= p["End flat secs"]:
                    if not end_mode:
                        end_mode = True
                        quoter.wait_jobs()
                        log("END MODE: %d ticks (%.0f s) left - pulling adding side, reducing passively"
                            % (ticks_left, secs_left))
                    q["want"]["BUY" if net_q >= 0 else "SELL"] = []
                    final = secs_left <= p["End cross secs"]
                    if not final:
                        # keep earning: reducing side stays passive at the touch until the last seconds
                        quoter.reconcile(p, q, tick, t_loop)
                    elif t_loop - last_flat > 0.5:
                        last_flat = t_loop
                        net = int(api.security().get("position") or 0)
                        if net != 0:
                            quoter.wait_jobs()
                            orders.cancel_all_tracked("end flatten")
                            if True:
                                # last ticks: LIMIT one tick through the far side (no MARKET
                                # orders in this case), re-posted every 0.5 s until flat
                                side = "SELL" if net > 0 else "BUY"
                                px = rnd((bb - TICK) if net > 0 else (ba + TICK))
                                for c in chunks(abs(net), CASE_MAX_ORDER):
                                    o = api.limit_order(side, c, px)
                                    if o:
                                        orders.track(o, side, px, c, 0, "flatten:final")
                                log("END LIMIT flatten %+d @ %.2f (%d ticks left)" % (net, px, ticks_left))
                            else:
                                flatten(api, orders, net, bb, ba, False, "end")
                        elif orders.live:
                            orders.cancel_all_tracked("end flat done")
                elif t_loop - last_flat > 1.5:
                    quoter.reconcile(p, q, tick, t_loop)
                    quoter.take(p, q, bb, bs, ba, as_, net_q, t_loop, tick)

                # --- logging -------------------------------------------------
                our_b = orders.on_side("BUY")
                our_s = orders.on_side("SELL")
                cps, ops_ = api.calls_last_sec()
                loop_ms = round((time.time() - t_loop) * 1000, 1)
                LOG.tick_row({
                    "time": "%.3f" % t_loop, "tick": tick, "loop_ms": loop_ms, "status": status,
                    "net": net, "net_est": net_est, "room_buy": q["room_buy"], "room_sell": q["room_sell"],
                    "bid": bb, "bid_sz": bs, "ask": ba, "ask_sz": as_, "mid": round(q["mid"], 4),
                    "micro": round(q["micro"], 4), "drift": round(q["drift"], 4),
                    "vol": round(q["vol"], 4), "fair": round(q["fair"], 4), "skew": round(q["skew"], 4),
                    "half": round(q["half"], 4),
                    "our_bid": our_b[0]["price"] if our_b else "", "our_bid_qty": orders.open_qty("BUY"),
                    "our_ask": our_s[0]["price"] if our_s else "", "our_ask_qty": orders.open_qty("SELL"),
                    "n_open": len(orders.live), "fills": orders.fills, "volume": orders.volume,
                    "realized": realized, "unrealized": unreal, "nlv": f2(nlv),
                    "calls_ps": cps, "orders_ps": ops_, "n429": api.n_429, "replaces": quoter.replaces,
                    "acct_gross": quoter.acct_gross, "acct_net": quoter.acct_net,
                    "fair_ref": round(quoter.fair_ref, 4) if quoter.fair_ref else "",
                    "settling": orders.settling_qty("BUY") + orders.settling_qty("SELL"),
                })
                shared.set_live({
                    "Updated": time.strftime("%H:%M:%S"), "Tick": tick,
                    "Case status": status + ("" if active else "  (DISABLED)") + ("  END MODE" if end_mode else ""),
                    "NLV": f2(nlv), "Realized (RIT)": realized, "Unrealized (RIT)": unreal,
                    "Net position": net, "Net (fast est.)": net_est,
                    "Fills": orders.fills, "Volume": orders.volume,
                    "Fill edge/sh (3s mark)": round(orders.mark_edge(), 4),
                    "Bid": bb, "Bid size": bs, "Ask": ba, "Ask size": as_,
                    "Mid": round(q["mid"], 3), "Fair": round(q["fair"], 3), "Drift": round(q["drift"], 3),
                    "Vol range": round(q["vol"], 3),
                    "Our bid": our_b[0]["price"] if our_b else "", "Our bid qty": orders.open_qty("BUY"),
                    "Our ask": our_s[0]["price"] if our_s else "", "Our ask qty": orders.open_qty("SELL"),
                    "Open orders": len(orders.live), "Replaces/min": len(quoter.replace_times),
                    "Account gross": quoter.acct_gross, "Account net": quoter.acct_net,
                    "Fair ref": round(quoter.fair_ref, 3) if quoter.fair_ref else "",
                    "API calls/s": cps, "Orders/s": ops_, "Rate-limit hits": api.n_429,
                    "Loop ms": loop_ms, "Last message": LOG.last,
                })
                if tick != last_tick and tick % 15 == 0:
                    try:
                        nlv = api.trader().get("nlv")
                    except Exception:
                        pass
                    log("[tick %3d] NLV=%s real=%.2f unreal=%.2f | fills=%d vol=%d edge=%+.4f/sh | "
                        "net=%d est=%d | open=%d repl/min=%d | api/s=%d ord/s=%d 429=%d loop=%.0fms"
                        % (tick, f2(nlv), realized, unreal, orders.fills, orders.volume,
                           orders.mark_edge(), net, net_est, len(orders.live),
                           len(quoter.replace_times), cps, ops_, api.n_429, loop_ms))
                last_tick = tick
                if p["Poll seconds"] > 0:
                    time.sleep(p["Poll seconds"])

            except KeyboardInterrupt:
                raise
            except Exception as e:
                n_err += 1
                msg = str(e)
                if "401" in msg or "Must be logged in" in msg or "must be running" in msg.lower():
                    n401 += 1
                    log("! server says: %s (%d in a row)" % (msg[:120], n401))
                    if n401 >= 20:
                        log("client logged out / case not running - stopping")
                        stop_reason = "client logged out"
                        break
                    time.sleep(0.5)
                    continue
                if n_err <= 50 or n_err % 100 == 0:
                    log("! LOOP ERROR #%d\n%s" % (n_err, traceback.format_exc()))
                time.sleep(0.3)
    except KeyboardInterrupt:
        stop_reason = "interrupted"
        print("\nInterrupted.")
    finally:
        shared.stop = True
        try:
            quoter.wait_jobs()
            api.cancel_all()
            sec = api.security()
            net = int(sec.get("position") or 0)
            if net and p.get("Allow market orders"):
                for c in chunks(abs(net), CASE_MAX_ORDER):
                    api.market_order("SELL" if net > 0 else "BUY", c)
                log("final MARKET flatten %d" % net)
            elif net:
                log("NOTE: stopped with net %d (Allow market orders = 0, so not flattened)" % net)
        except Exception as e:
            log("cleanup failed: %s" % e)
        try:
            nlv = api.trader().get("nlv")
            sec = api.security()
            log("FINAL pos=%s realized=%s unrealized=%s volume=%s"
                % (sec.get("position"), sec.get("realized"), sec.get("unrealized"), sec.get("volume")))
        except Exception as e:
            log("final state failed: %s" % e)
        try:
            allo = []
            for st in ("OPEN", "TRANSACTED", "CANCELLED"):
                allo += api.orders(st)
            LOG.dump_orders(allo)
            log("Dumped %d orders" % len(allo))
        except Exception as e:
            log("order dump failed: %s" % e)
        log("DONE (%s). NLV=%s | fills=%d vol=%d edge=%+.4f/sh | replaces=%d | errors=%d | "
            "api calls=%d 429s=%d"
            % (stop_reason, f2(nlv), orders.fills, orders.volume, orders.mark_edge(),
               quoter.replaces, n_err, api.n_calls, api.n_429))
        print("Log files:\n  " + "\n  ".join(LOG.paths.values()))
        LOG.close()


def launch_all():
    """No ticker argument given: start one process per ticker (own console window)."""
    import subprocess
    here = os.path.abspath(__file__)
    flags = getattr(subprocess, "CREATE_NEW_CONSOLE", 0) if os.name == "nt" else 0
    procs = []
    for t in TICKERS:
        print("starting", t)
        procs.append(subprocess.Popen([sys.executable, here, t, API_KEY, str(PORT)],
                                      cwd=os.path.dirname(here), creationflags=flags))
    print("%d bots started (one window each). Ctrl+C in a window stops that ticker." % len(procs))
    try:
        for pr in procs:
            pr.wait()
    except KeyboardInterrupt:
        print("launcher interrupted; the bot windows keep running until stopped there")


if __name__ == "__main__":
    if len(sys.argv) > 1:
        main()
    else:
        launch_all()
