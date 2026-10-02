# -*- coding: utf-8 -*-
"""
RIT ALGO2 multi-ticker build - one process per ticker (CNR / RY / AC), LIMIT orders only
========================================================================

CASE FACTS (case brief)
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
TICKERS = ["CNR", "RY", "AC"]      # run `python algo2_mm_multi.py` (no arguments): it starts
                                   # one copy of itself per ticker, each in its own console

TICKER = TICKERS[0]
# internal:  python algo2_mm_multi.py <TICKER> [API_KEY] [PORT]  runs ONE ticker
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
    ("Quote size",            2000,  "shares per quote per side (before inventory scaling)"),
    ("Layers",                1,     "price levels per side (each 1 tick further out)"),
    ("Min spread",            0.03,  "our ask - our bid never tighter than this $"),
    ("Vol mult",              1.0,   "half spread >= this x (mid range over Vol window)"),
    ("Vol window",            3.0,   "seconds of mid history for the vol term"),
    ("Improve if spread >=",  0.02,  "step 1 tick inside the best when market spread >= this (0 = always join)"),
    ("Micro weight",          0.3,   "0..1 pull of fair toward the size-weighted microprice"),
    ("Drift window",          2.0,   "seconds: fair leans by Drift lean x (mid now - mid then)"),
    ("Drift lean",            0.3,   "0 = ignore trend, 1 = full lean"),
    ("Max inv",               3000,  "adding side is 0 here; skew is full here"),
    ("Skew $ at max inv",     0.04,  "$ both quotes shift when |net| = Max inv (only if spread >= Skew if spread)"),
    ("Skew if spread >=",     0.02,  "price skew only in markets at least this wide; tighter = size skew only"),
    ("Reduce boost",          0.5,   "reducing side size += this x |net|"),
    ("Hard reduce |net|",     5000,  "beyond this, cross the spread (LIMIT at far side) for the excess"),
    ("Requote lag ticks",     3,     "cancel a resting order once it is this many ticks behind the touch"),
    ("Requote min sec",       0.5,  "min seconds between replacements on one side"),
    ("Gross cap",             7000,  "self-imposed cap on |net| + open qty on a side (case 25,000)"),
    ("End flat secs",         10,    "pull adding side and flatten (far-side LIMITs) this many ticks before the end"),
    ("End market ticks",      3,     "in the last N ticks flatten with LIMITs 1 tick through the far side, re-posted every 0.5 s (no MARKET)"),
    ("Allow market orders",   0,     "1 = MARKET also allowed for Flatten now / Panic / script stop (costs 1c/share)"),
    ("Poll seconds",          0.0,   "extra sleep per loop (0 = as fast as the rate limit allows)"),
    ("Book depth",            10,    "levels pulled per side"),
    ("Flatten now",           0,     "1 = cancel + flatten to 0 with far-side LIMITs (auto resets)"),
    ("Panic",                 0,     "1 = cancel, flatten, STOP script"),
]
PARAM_ORDER = [d[0] for d in DEFAULTS]
PARAM_DEFAULTS = dict((d[0], d[1]) for d in DEFAULTS)
PARAM_NOTES = dict((d[0], d[2]) for d in DEFAULTS)

USE_EXCEL = True
EXCEL_FILE = "ALGO2_cockpit_%s.xlsx" % TICKER
LOG_DIR = "logs"

MAX_CALLS_PER_SEC = 170        # RIT global cap observed ~200/s
DEFAULT_ORDERS_PER_SEC = 20    # overwritten from /securities api_orders_per_second
SLOW_LANE_SEC = 0.25           # /case /securities /limits refresh
DONE_LOOKUP_SEC = 0.20         # how often missing orders are looked up in TRANSACTED/CANCELLED
ORDER_VISIBLE_SEC = 5.0        # server lists a new order late: no conclusions before this
ORDER_LOST_SEC = 15.0          # LIVE: fills are listed 3-5 s late (per tick); never forget early (server drops
                               # orders silently when the per-second order limit is hit)
MARK_SEC = 3.0                 # mark each fill against the mid this long after


# =============================================================================
# Logging
# =============================================================================
class RunLog(object):
    TICK_COLS = ["time", "tick", "loop_ms", "status", "net", "net_est", "room_buy", "room_sell",
                 "bid", "bid_sz", "ask", "ask_sz", "mid", "micro", "drift", "vol", "fair", "skew",
                 "half", "our_bid", "our_bid_qty", "our_ask", "our_ask_qty", "n_open",
                 "fills", "volume", "realized", "unrealized", "nlv", "calls_ps", "orders_ps",
                 "n429", "replaces"]
    TRADE_COLS = ["time", "tick", "side", "qty", "price", "fair", "mid", "bid", "ask",
                  "net_before", "order_id", "age_s", "reason"]

    def __init__(self, folder):
        os.makedirs(folder, exist_ok=True)
        self.stamp = time.strftime("%Y%m%d_%H%M%S")
        base = os.path.join(folder, "algo2_%s_%s" % (TICKER, self.stamp))
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
                if len(self.stamps) >= self.max_calls:
                    wait = 1.0 - (now - self.stamps[0]) + 0.002
                if is_order and len(self.ostamps) >= self.orders_per_sec:
                    wait = max(wait, 1.0 - (now - self.ostamps[0]) + 0.002)
                if is_order and self.ostamps:
                    gap = 1.0 / self.orders_per_sec * 0.9
                    wait = max(wait, gap - (now - self.ostamps[-1]))
                if wait <= 0:
                    self.stamps.append(now)
                    if is_order:
                        self.ostamps.append(now)
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
                   "Replaces/min", "API calls/s", "Orders/s", "Rate-limit hits", "Loop ms",
                   "Last message"]
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

    # ---- bookkeeping ------------------------------------------------------
    def track(self, o, side, price, qty, layer, reason):
        rec = {"id": o["order_id"], "side": side, "price": price, "qty": int(qty), "layer": layer,
               "filled": 0, "vwap": None, "t": time.time(), "last_open": time.time(), "reason": reason}
        self.live[rec["id"]] = rec
        self.last_post_time = time.time()
        return rec

    def open_qty(self, side):
        return sum(r["qty"] - r["filled"] for r in self.live.values() if r["side"] == side)

    def on_side(self, side):
        return sorted((r for r in self.live.values() if r["side"] == side),
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
            if now - t < MARK_SEC:
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
        missing = [r for r in self.live.values() if r["id"] not in open_]
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
        recs = [r for r in self.live.values() if r["side"] == side]
        if recs:
            self.api.cancel_ids([r["id"] for r in recs])
            for r in recs:
                self.retire(r)
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

    def record_mid(self, mid, now, window):
        if mid is not None:
            self.mid_hist.append((now, mid))
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

        base = int(p["Quote size"])
        a = abs(net)
        cap = p["Gross cap"]
        room_buy = int(cap - net)            # shares we may still buy before net hits +cap
        room_sell = int(cap + net)           # ... sell before net hits -cap
        layers = max(1, int(p["Layers"]))
        # per-layer size and a cap on the TOTAL resting on each side:
        #   adding side   -> shrinks to 0 at |net| = Max inv, total <= Max inv - |net|
        #   reducing side -> grows with |net|, total <= |net| + base (a sweep through
        #                    every layer flips us by at most one base size)
        size_add = int(base * max(0.0, 1.0 - a / max_inv))
        tot_add = int(max(0.0, max_inv - a))
        size_red = int(base + p["Reduce boost"] * a)
        tot_red = int(a + base)
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
        # inventory beyond the hard line: cross for the excess (LIMIT at far side),
        # replacing the passive reducing layers
        hard = p["Hard reduce |net|"]
        if a > hard:
            excess = int(a - max_inv)
            if net > 0:
                want["SELL"] = [(bb, min(excess, room_sell), "cross")]
            else:
                want["BUY"] = [(ba, min(excess, room_buy), "cross")]
        return {"mid": mid, "micro": micro, "drift": drift, "fair": fair, "skew": skew,
                "half": half, "vol": vol, "want": want, "room_buy": room_buy,
                "room_sell": room_sell, "bid_px": bid_px, "ask_px": ask_px,
                "tot": {"BUY": buy_tot, "SELL": sell_tot}}

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
                elif behind < -1.5 * TICK:
                    drop.append((r, "too aggressive"))
                elif behind > lag + 1e-9:
                    drop.append((r, "%d ticks behind" % round(behind / TICK)))
                else:
                    keep.append(r)
            # --- size cap: cancel furthest-behind first --------------------------
            cap = q["tot"][side]
            rem = lambda r: r["qty"] - r["filled"]
            keep.sort(key=lambda r: (top_px - r["price"]) * sgn)    # nearest first
            total = sum(rem(r) for r in keep)
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
                self.api.cancel_ids(cancel_ids)
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
    if ops:
        api.orders_per_sec = max(int(ops), 10)   # per ticker (teammate measured 10/s incl. cancels)
    log("Connected: %s %s (%s) | case '%s' | tick %s/%s | status %s | fee %s rebate %s | "
        "max order %s | orders/s %s (using %d)"
        % (trader.get("first_name"), trader.get("last_name"), me, case.get("name"), case.get("tick"),
           case.get("ticks_per_period"), case.get("status"), sec.get("trading_fee"),
           sec.get("limit_order_rebate"), sec.get("max_trade_size"), ops, api.orders_per_sec))
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
    p = shared.get_params()
    p_logged = {}
    status, tick = case.get("status"), case.get("tick", 0)
    tpp = case.get("ticks_per_period", 300)
    net, net_est, nlv, realized, unreal = 0, 0, None, 0.0, 0.0
    net_offset, off_since = int(sec.get("position") or 0), None
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
                        orders.cancel_all_tracked("case " + str(status))
                    last_status = status
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
                quoter.record_mid((bb + ba) / 2.0, t_loop, p["Vol window"])

                # --- our orders / fills --------------------------------------
                orders.ctx = {"fair": None, "mid": round((bb + ba) / 2.0, 3), "bid": bb, "ask": ba,
                              "net_est": net_est}
                orders.sync(tick, t_loop)
                net_est = orders.signed_fills + net_offset
                orders.ctx["net_est"] = net_est
                orders.mark_fills((bb + ba) / 2.0)

                # --- quotes --------------------------------------------------
                active = bool(p["Enabled"])
                q = quoter.compute(p, bb, bs, ba, as_, net_est, t_loop)
                orders.ctx["fair"] = round(q["fair"], 4)
                ticks_left = tpp - tick
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
                elif ticks_left <= p["End flat secs"]:
                    if not end_mode:
                        end_mode = True
                        quoter.wait_jobs()
                        log("END MODE: %d ticks left - pulling adding side, flattening" % ticks_left)
                    q["want"]["BUY" if net_est >= 0 else "SELL"] = []
                    final = ticks_left <= p["End market ticks"]
                    if t_loop - last_flat > (0.5 if final else 1.0):
                        last_flat = t_loop
                        net = int(api.security().get("position") or 0)
                        if net != 0:
                            orders.cancel_all_tracked("end flatten")
                            if final:
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
            except Exception:
                n_err += 1
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
