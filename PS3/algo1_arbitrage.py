# -*- coding: utf-8 -*-
"""
RIT ALGO1 - Arbitrage + Market Making on CRZY (CRZY_M / CRZY_A), Excel cockpit
===============================================================================

TWO STRATEGIES RUN TOGETHER
  ARB  When the two books cross (bid on one exchange > ask on the other) buy on
       the cheap side / sell on the expensive side with two marketable LIMIT
       orders priced at the levels actually seen; anything unfilled is cancelled
       at once, so a vanished quote costs nothing instead of a bad market fill.
       Size walks both books so every share is profitable after costs, capped
       at `ARB max qty`. A losing or one-legged arb pauses ARB for
       `ARB pause sec`. Your own resting quotes are excluded from the walk.
  The script never stops on its own: only Ctrl+C / Spyder stop or Panic=1
  in Excel end it (both cancel quotes and flatten first). While the case is
  PAUSED or STOPPED it waits and resumes when the case is ACTIVE again.
  MM   Rest a bid and an ask on each exchange around the consolidated mid:
           half = max(MM half spread,                       # algo base
                      MM vol mult * (mid range over MM vol window),   # algo widening
                      MM min spread / 2)                    # YOUR override (0 = off)
           bid = mid - half - skew*net/1000 ;  ask = mid + half - skew*net/1000
       clamped so a quote never crosses the other exchange's best price.
       Positive inventory pushes both quotes down, negative up. Beyond
       `MM max inv` the side that would add inventory is pulled.

LOGGING (folder `logs` next to this file, one set of files per run)
  algo1_<stamp>.log         human-readable events: params, every requote,
                            every ARB (expected vs actual), every MM fill and
                            its mark-to-mid 3 s later, errors with tracebacks
  algo1_<stamp>_ticks.csv   one row per loop: books, crosses, quotes, P&L ...
  algo1_<stamp>_trades.csv  one row per ARB round-trip / MM fill
  algo1_<stamp>_orders.csv  every order RIT has for you, dumped at the end
  -> send the whole folder for diagnosis.

API RATE LIMIT (100 calls/s server-side)
  Sliding-window throttle at MAX_CALLS_PER_SEC, 429 retry. Books every loop
  (fast lane); /case, /securities, /limits, /orders every SLOW_LANE_SEC.

POSITION LIMIT
  CRZY positions are aggregated across exchanges (+5000 M / -5000 A = flat).
  Gross/net usage is read from /limits; ARB size and MM quote size are limited
  to the remaining room (minus resting quotes).

EXCEL COCKPIT (xlwings + Excel): ALGO1_cockpit.xlsx next to this file.
  Control - yellow cells, edit live.  Live - refreshed ~5x/s.  Trades - log.

HOW TO RUN (Spyder): set API_KEY / PORT, press F5. Stop = red square / Ctrl+C
  -> cancels all quotes and flattens the net position first.
"""

import sys
import time
import os
import csv
import traceback
from collections import deque

import requests

# =============================================================================
# CONFIG - defaults; everything in DEFAULTS can be overridden live from Excel
# =============================================================================
API_KEY = "YOUR API KEY HERE"
PORT = 9999

TICKER_MAIN = "CRZY_M"
TICKER_ALT = "CRZY_A"
TICKERS = (TICKER_MAIN, TICKER_ALT)
TICK_SIZE = 0.01
CASE_MAX_ORDER = 10000
CASE_GROSS_LIMIT = 25000

DEFAULTS = [
    # (name, default, note)
    ("Enabled",        1,     "1 = trade, 0 = watch only (master kill switch)"),
    ("ARB enabled",    1,     "1 = run crossed-market arbitrage"),
    ("Cost/share",     0.0,   "per-share trading cost PER LEG (Q1)"),
    ("Min edge",       0.01,  "profit/share required after costs before taking a cross"),
    ("ARB max qty",    2000,  "max shares per arbitrage round-trip"),
    ("ARB pause sec",  2.0,   "pause arbitrage this long after a losing/one-legged arb"),
    ("Max order",      10000, "case limit 10,000 per order"),
    ("Gross cap",      15000, "self-imposed cap (case limit 25,000 gross/net)"),
    ("Poll seconds",   0.05,  "delay between book checks"),
    ("Book depth",     20,    "levels pulled per side"),
    ("MM enabled",     0,     "1 = post resting quotes (OFF by default - turn on live)"),
    ("MM on Main",     1,     "quote on CRZY_M"),
    ("MM on Alt",      1,     "quote on CRZY_A"),
    ("MM size",        500,   "shares per quote (each side, each exchange)"),
    ("MM min spread",  0.0,   "YOUR override: bid-ask spread never tighter than this $ (0 = algo decides)"),
    ("MM half spread", 0.04,  "algo's base half spread ($ from mid)"),
    ("MM vol mult",    1.0,   "algo widens: half spread >= this x (mid range over vol window)"),
    ("MM vol window",  3.0,   "seconds of mid history used for the vol spread"),
    ("MM skew per 1k", 0.03,  "$ shift of both quotes per 1,000 shares net inventory"),
    ("MM max inv",     2000,  "stop adding inventory beyond this |net|"),
    ("MM requote sec", 1.0,   "how often quotes are refreshed"),
    ("Hard flat |net|", 5000,  "safety: if |net| reaches this, pull quotes and market-flatten to 0"),
    ("Flatten now",    0,     "1 = cancel quotes + close net position (auto resets)"),
    ("Panic",          0,     "1 = cancel, flatten, STOP script"),
]
PARAM_ORDER = [d[0] for d in DEFAULTS]
PARAM_DEFAULTS = dict((d[0], d[1]) for d in DEFAULTS)
PARAM_NOTES = dict((d[0], d[2]) for d in DEFAULTS)

USE_EXCEL = True
EXCEL_FILE = "ALGO1_cockpit.xlsx"
LOG_DIR = "logs"

MAX_CALLS_PER_SEC = 80        # server cap is 100; keep headroom for order bursts
SLOW_LANE_SEC = 0.25          # /case, /securities, /limits, /orders refresh rate
MM_MARK_SEC = 3.0             # mark each MM fill against the mid this long after
# RIT quirk seen live: a freshly POSTed order takes ~0.1-0.6 s to show up in
# GET /orders (and GET /orders/{id} 404s for filled orders). Never treat
# "not listed" as "gone" before ORDER_VISIBLE_SEC; give up after ORDER_LOST_SEC.
ORDER_VISIBLE_SEC = 1.5
ORDER_LOST_SEC = 6.0
SETTLE_TIMEOUT_SEC = 1.2      # how long an ARB leg may take to show its final state


# =============================================================================
# Logging
# =============================================================================
class RunLog(object):
    TICK_COLS = ["time", "tick", "loop_ms", "status", "net", "gross", "room",
                 "M_bid", "M_bid_sz", "M_ask", "M_ask_sz",
                 "A_bid", "A_bid_sz", "A_ask", "A_ask_sz",
                 "M_last", "A_last", "cross_MA", "cross_AM", "mid", "vol_range",
                 "our_bid_M", "our_ask_M", "our_bid_A", "our_ask_A", "mm_open_qty",
                 "arb_trades", "arb_pnl", "mm_fills", "mm_vol",
                 "realized", "unrealized", "nlv", "calls_ps", "n429"]
    TRADE_COLS = ["time", "tick", "type", "ticker_or_buy_on", "side_or_sell_on",
                  "qty", "price_or_buy_vwap", "sell_vwap", "exp_buy", "exp_sell",
                  "edge", "pnl", "mid", "M_bid", "M_ask", "A_bid", "A_ask", "net_before"]

    def __init__(self, folder):
        os.makedirs(folder, exist_ok=True)
        self.stamp = time.strftime("%Y%m%d_%H%M%S")
        base = os.path.join(folder, "algo1_" + self.stamp)
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

    def event(self, msg, console=True):
        line = "%s +%6.2fs t=%-3s %s" % (time.strftime("%H:%M:%S"),
                                          time.time() - self.t0, self.tick, msg)
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
    """Every call goes through `_call`: sliding-window throttle + 429 retry."""

    def __init__(self, key, port):
        self.base = "http://localhost:%d/v1" % port
        self.s = requests.Session()
        self.s.headers.update({"X-API-Key": key})
        self.stamps = deque()
        self.n_calls = 0
        self.n_429 = 0

    def _throttle(self):
        now = time.time()
        while self.stamps and now - self.stamps[0] > 1.0:
            self.stamps.popleft()
        if len(self.stamps) >= MAX_CALLS_PER_SEC:
            time.sleep(1.0 - (now - self.stamps[0]) + 0.002)
        self.stamps.append(time.time())
        self.n_calls += 1

    def calls_last_sec(self):
        now = time.time()
        while self.stamps and now - self.stamps[0] > 1.0:
            self.stamps.popleft()
        return len(self.stamps)

    def _call(self, method, path, params, retries=6):
        for _ in range(retries):
            self._throttle()
            r = self.s.request(method, self.base + path, params=params)
            if r.status_code != 429:
                return r
            self.n_429 += 1
            try:
                wait = float(r.json().get("wait", 0.1))
            except Exception:
                wait = 0.1
            time.sleep(max(wait, 0.02))
        return r

    def _get(self, path, **params):
        r = self._call("GET", path, params)
        if not r.ok:
            raise ApiError("GET %s -> %s: %s" % (path, r.status_code, r.text))
        return r.json()

    def case(self):
        return self._get("/case")

    def trader(self):
        return self._get("/trader")

    def limits(self):
        return self._get("/limits")

    def securities(self):
        return self._get("/securities")

    def book(self, ticker, limit):
        return self._get("/securities/book", ticker=ticker, limit=limit)

    def orders(self, status="OPEN"):
        return self._get("/orders", status=status)

    def order(self, oid):
        return self._get("/orders/%d" % int(oid))

    def done_orders(self):
        """{id: order} for every TRANSACTED or CANCELLED order of ours."""
        out = {}
        for st in ("TRANSACTED", "CANCELLED"):
            for o in self.orders(st):
                out[o["order_id"]] = o
        return out

    def find_order(self, oid):
        """Order in any state, or None if the server does not list it yet."""
        try:
            return self.order(oid)
        except ApiError:
            pass
        try:
            return self.done_orders().get(oid)
        except ApiError:
            return None

    def positions(self):
        """Net CRZY position (aggregated across both exchanges)."""
        return net_position(dict((x["ticker"], x) for x in self.securities()))

    def _post_order(self, params):
        r = self._call("POST", "/orders", params)
        if r.ok:
            o = r.json()
            log("ORDER ok   %s -> id=%s filled=%s vwap=%s status=%s"
                % (params, o.get("order_id"), o.get("quantity_filled"),
                   o.get("vwap"), o.get("status")), console=False)
            return o
        log("! ORDER FAIL %s: %s %s" % (params, r.status_code, r.text[:200]))
        return None

    def market_order(self, ticker, action, qty):
        return self._post_order({"ticker": ticker, "type": "MARKET",
                                 "quantity": int(qty), "action": action})

    def limit_order(self, ticker, action, qty, price):
        return self._post_order({"ticker": ticker, "type": "LIMIT",
                                 "quantity": int(qty), "action": action,
                                 "price": round(price, 2)})

    def cancel_ids(self, ids):
        if not ids:
            return []
        r = self._call("POST", "/commands/cancel",
                       {"ids": ",".join(str(i) for i in ids)})
        try:
            return r.json().get("cancelled_order_ids", [])
        except Exception:
            return []

    def cancel_all(self):
        """Cancel everything and verify the OPEN list really empties."""
        for i in range(5):
            r = self._call("POST", "/commands/cancel", {"all": 1})
            log("cancel_all -> %s %s" % (r.status_code, r.text[:150]), console=False)
            time.sleep(0.3)
            try:
                left = self.orders("OPEN")
            except ApiError:
                continue
            if not left:
                return True
            log("cancel_all: %d orders still OPEN, retry %d" % (len(left), i + 1))
        return False


# =============================================================================
# Excel cockpit (xlwings)
# =============================================================================
class Cockpit(object):
    LIVE_LABELS = ["Updated", "Tick", "Case status", "NLV",
                   "Realized P&L (RIT)", "Unrealized P&L (RIT)",
                   "ARB trades", "ARB est. P&L", "MM fills", "MM volume",
                   "Pos CRZY_M", "Pos CRZY_A", "Net", "Gross (RIT)", "Room",
                   "M bid", "M bid size", "M ask", "M ask size",
                   "A bid", "A bid size", "A ask", "A ask size",
                   "Cross M->A (buy M, sell A)", "Cross A->M (buy A, sell M)",
                   "Our bid M", "Our ask M", "Our bid A", "Our ask A",
                   "MM eff. spread (ask-bid)", "API calls/sec", "Rate-limit hits",
                   "Last message"]
    TRADE_HEADER = ["Time", "Tick", "Type", "Ticker / Buy on", "Side / Sell on",
                    "Qty", "Price / Buy VWAP", "Sell VWAP", "Edge/share", "P&L"]

    def __init__(self, path):
        import xlwings as xw
        new = not os.path.exists(path)
        self.wb = xw.Book() if new else xw.Book(path)
        if new:
            self.wb.save(path)
        self.params = dict(PARAM_DEFAULTS)
        self._setup()
        tr = self.wb.sheets["Trades"]
        self.trade_row = tr.range("A1").end("down").row + 1
        if self.trade_row > 100000:
            self.trade_row = 2

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
            ctl.range("A2").value = [[k, PARAM_DEFAULTS[k], PARAM_NOTES[k]]
                                     for k in PARAM_ORDER]
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
        if tr.range("A1:J1").value != self.TRADE_HEADER:
            tr.clear()
            tr.range("A1").value = [self.TRADE_HEADER]
            tr.range("A1:J1").font.bold = True
            tr.autofit()
        names = [s.name for s in self.wb.sheets]
        if "Sheet1" in names and len(names) > 3:
            self.wb.sheets["Sheet1"].delete()
        self.wb.sheets["Control"].activate()

    def read_params(self):
        try:
            ctl = self.wb.sheets["Control"]
            vals = ctl.range("B2:B%d" % (1 + len(PARAM_ORDER))).value
            for k, v in zip(PARAM_ORDER, vals):
                if v is not None:
                    self.params[k] = float(v)
            if self.params["Flatten now"]:
                ctl.range("B%d" % (2 + PARAM_ORDER.index("Flatten now"))).value = 0
        except Exception:
            pass
        return self.params

    def write_live(self, values):
        try:
            self.wb.sheets["Live"].range("B1").value = \
                [[values.get(k, "")] for k in self.LIVE_LABELS]
        except Exception:
            pass

    def log_trade(self, row):
        try:
            self.wb.sheets["Trades"].range("A%d" % self.trade_row).value = [row]
            self.trade_row += 1
        except Exception:
            pass


# =============================================================================
# Shared helpers
# =============================================================================
def levels(side, exclude_trader=None):
    """Order list -> [(price, remaining qty), ...], skipping our own orders."""
    out = []
    for o in side:
        if exclude_trader and o.get("trader_id") == exclude_trader:
            continue
        rem = o["quantity"] - (o.get("quantity_filled") or 0)
        if rem > 0:
            out.append((o["price"], rem))
    return out


def net_position(secs):
    """RIT reports the AGGREGATED CRZY position on BOTH tickers (seen live:
    CRZY_M and CRZY_A always carry the identical number, and /limits gross is
    half of their sum). Summing the two doubles the position - which made
    every flatten overshoot by 2x and flip the sign. Use one of them."""
    pm = int(secs.get(TICKER_MAIN, {}).get("position") or 0)
    pa = int(secs.get(TICKER_ALT, {}).get("position") or 0)
    return pm if pm == pa else pm + pa


def book_sides(b):
    """Key names differ between RIT builds: bid/ask or bids/asks."""
    if not isinstance(b, dict):
        raise ApiError("unexpected book response: %r" % (b,))
    bid = b.get("bid", b.get("bids"))
    ask = b.get("ask", b.get("asks"))
    if bid is None or ask is None:
        raise ApiError("book response has keys %s - expected bid/ask or bids/asks"
                       % list(b.keys()))
    return bid, ask


def top(lv):
    return (lv[0][0], lv[0][1]) if lv else (None, None)


def rnd(px):
    return round(round(px / TICK_SIZE) * TICK_SIZE, 2)


def f2(x):
    return "" if x is None else x


def consolidated(lv):
    """(best_bid, best_ask, mid) across both exchanges, own orders excluded."""
    bids = [top(lv[t]["bid"])[0] for t in TICKERS if top(lv[t]["bid"])[0] is not None]
    asks = [top(lv[t]["ask"])[0] for t in TICKERS if top(lv[t]["ask"])[0] is not None]
    if not bids or not asks:
        return None, None, None
    return max(bids), min(asks), (max(bids) + min(asks)) / 2.0


# =============================================================================
# Arbitrage
# =============================================================================
class Opportunity(object):
    def __init__(self, buy_on, sell_on, qty, buy_avg, sell_avg, cost, buy_limit, sell_limit):
        self.buy_on, self.sell_on = buy_on, sell_on
        self.qty, self.buy_avg, self.sell_avg, self.cost = qty, buy_avg, sell_avg, cost
        self.buy_limit, self.sell_limit = buy_limit, sell_limit   # worst levels taken

    @property
    def edge(self):
        return self.sell_avg - self.buy_avg - 2 * self.cost

    @property
    def profit(self):
        return self.edge * self.qty


def size_cross(asks, bids, max_qty, cost, min_edge):
    qty, buy_cost, sell_rev = 0, 0.0, 0.0
    i = j = 0
    ask_left = asks[0][1] if asks else 0
    bid_left = bids[0][1] if bids else 0
    buy_limit = sell_limit = None
    while i < len(asks) and j < len(bids) and qty < max_qty:
        ask_px, bid_px = asks[i][0], bids[j][0]
        if bid_px - ask_px - 2 * cost < min_edge:
            break
        buy_limit, sell_limit = ask_px, bid_px
        take = min(ask_left, bid_left, max_qty - qty)
        qty += take
        buy_cost += take * ask_px
        sell_rev += take * bid_px
        ask_left -= take
        bid_left -= take
        if ask_left == 0:
            i += 1
            ask_left = asks[i][1] if i < len(asks) else 0
        if bid_left == 0:
            j += 1
            bid_left = bids[j][1] if j < len(bids) else 0
    if qty <= 0:
        return None
    return Opportunity("", "", int(qty), buy_cost / qty, sell_rev / qty, cost,
                       buy_limit, sell_limit)


def find_opportunity(lv, max_qty, cost, min_edge):
    cands = []
    o = size_cross(lv[TICKER_MAIN]["ask"], lv[TICKER_ALT]["bid"], max_qty, cost, min_edge)
    if o:
        o.buy_on, o.sell_on = TICKER_MAIN, TICKER_ALT
        cands.append(o)
    o = size_cross(lv[TICKER_ALT]["ask"], lv[TICKER_MAIN]["bid"], max_qty, cost, min_edge)
    if o:
        o.buy_on, o.sell_on = TICKER_ALT, TICKER_MAIN
        cands.append(o)
    return max(cands, key=lambda x: x.profit) if cands else None


def _settle(api, o, deadline=None):
    """Cancel any unfilled remainder of an order and return (filled, vwap).
    The POST response is a pre-match snapshot (filled=0 OPEN even when the
    order filled instantly) and the server lists the order only ~0.1-0.6 s
    later, so poll every status list until it is non-OPEN. Returns
    (None, None) if the server never showed the order within the deadline."""
    if o is None:
        return 0, None
    oid = o["order_id"]
    if deadline is None:
        deadline = time.time() + SETTLE_TIMEOUT_SEC
    api.cancel_ids([oid])
    cur = None
    while True:
        cur = api.find_order(oid)
        if cur is not None and cur.get("status") != "OPEN":
            return int(cur.get("quantity_filled") or 0), cur.get("vwap")
        if time.time() > deadline:
            log("! order %s not settled after %.1fs (last seen %s)"
                % (oid, SETTLE_TIMEOUT_SEC, cur and cur.get("status")))
            if cur is not None:
                api.cancel_ids([oid])
                return int(cur.get("quantity_filled") or 0), cur.get("vwap")
            return None, None
        if cur is not None:                     # listed but still OPEN: re-cancel
            api.cancel_ids([oid])
        time.sleep(0.04)


def stable_positions(api, timeout=0.6):
    """Positions from /securities, re-read until two consecutive reads agree."""
    prev = api.positions()
    t_end = time.time() + timeout
    while time.time() < t_end:
        time.sleep(0.05)
        cur = api.positions()
        if cur == prev:
            return cur
        prev = cur
    return prev


def execute_arb(api, opp):
    """Both legs as marketable LIMIT orders at the prices seen in the book, then
    cancel whatever did not fill. Returns dict with matched qty, vwaps, P&L,
    residual (signed shares left over) and elapsed ms, or None."""
    t0 = time.time()
    pos0 = api.positions()
    buy = api.limit_order(opp.buy_on, "BUY", opp.qty, opp.buy_limit)
    sell = api.limit_order(opp.sell_on, "SELL", opp.qty, opp.sell_limit)
    deadline = time.time() + SETTLE_TIMEOUT_SEC
    bq, bv = _settle(api, buy, deadline)
    sq, sv = _settle(api, sell, deadline)
    # The position change is the ground truth for what we now hold; order
    # lookups only supply the prices. If they disagree, trust the position.
    unknown = bq is None or sq is None
    if unknown:
        # server never listed a leg: fall back to the position change (may
        # include MM fills from the same window, so this is a last resort)
        d_net = stable_positions(api) - pos0
        log("! ARB accounting: orders say bought %s sold %s, position moved %+d "
            "- filling the blank from the position" % (bq, sq, d_net))
        bq = bq if bq is not None else max(d_net + (sq or 0), 0)
        sq = sq if sq is not None else max(bq - d_net, 0)
        bv = bv or opp.buy_limit
        sv = sv or opp.sell_limit
    if bq == 0 and sq == 0:
        return {"matched": 0, "bought": 0, "sold": 0, "bv": None, "sv": None,
                "pnl": 0.0, "residual": 0, "ms": (time.time() - t0) * 1000}
    matched = min(bq, sq)
    pnl = (sv - bv - 2 * opp.cost) * matched if (matched and bv and sv) else 0.0
    return {"matched": matched, "bought": bq, "sold": sq, "bv": bv, "sv": sv,
            "pnl": pnl, "residual": bq - sq, "ms": (time.time() - t0) * 1000}


def unwind_residual(api, lv, residual):
    """Close a one-legged arb leftover right away: marketable limit at the best
    price across both exchanges, then a market order for anything still open."""
    if not residual:
        return 0.0
    action = "SELL" if residual > 0 else "BUY"
    side = "bid" if residual > 0 else "ask"
    best_t, best_px = None, None
    for t in TICKERS:
        px = top(lv[t][side])[0]
        if px is not None and (best_px is None or
                               (px > best_px if residual > 0 else px < best_px)):
            best_t, best_px = t, px
    qty = abs(residual)
    if best_t is None:
        o = api.market_order(TICKER_MAIN, action, qty)
        log("RESIDUAL %+d: no book -> market on %s vwap %s" % (residual, TICKER_MAIN, o and o.get("vwap")))
        return o.get("vwap") if o else None
    pos0 = api.positions()
    o = api.limit_order(best_t, action, qty, best_px)
    filled, vwap = _settle(api, o)
    if filled is None:            # server never listed it: position is all we have
        left = abs(stable_positions(api) - (pos0 - residual))
    else:
        left = qty - filled
    if left > 0:
        o2 = api.market_order(best_t, action, left)
        f2_, v2 = (int(o2.get("quantity_filled") or 0), o2.get("vwap")) if o2 else (0, None)
        log("RESIDUAL %+d: limit %s %d @%.2f filled %s, market %d @ %s"
            % (residual, best_t, qty, best_px, filled, f2_, v2))
    else:
        log("RESIDUAL %+d: closed with limit %s @%.2f" % (residual, best_t, best_px))
    return vwap


# =============================================================================
# Market making
# =============================================================================
class MarketMaker(object):
    def __init__(self, api, cockpit):
        self.api, self.cockpit = api, cockpit
        self.quotes = {}            # (ticker, side) -> dict(id, price, qty, filled, vwap, t)
        self.settling = []          # cancelled quotes whose partial fills are still unknown
        self.untracked_seen = {}    # order_id -> first time seen OPEN but not ours
        self.last_requote = 0.0
        self.fills, self.volume = 0, 0
        self.mid_hist = deque()     # (time, mid) for the volatility spread
        self.eff_half = 0.0
        self.pending_marks = []     # fills waiting for their mark-to-mid
        self.adverse_total = 0.0    # sum of (mark - price)*sign*qty  (negative = picked off)
        self.adverse_qty = 0

    def open_qty(self):
        return sum(q["qty"] - q["filled"] for q in self.quotes.values())

    def our_quote(self, ticker, side):
        q = self.quotes.get((ticker, side))
        return q["price"] if q else ""

    def record_mid(self, mid, window):
        now = time.time()
        if mid is not None:
            self.mid_hist.append((now, mid))
        while self.mid_hist and now - self.mid_hist[0][0] > window:
            self.mid_hist.popleft()

    def vol_range(self):
        if len(self.mid_hist) < 2:
            return 0.0
        m = [x[1] for x in self.mid_hist]
        return max(m) - min(m)

    def _fill(self, ticker, side, qty, price, tick, ctx):
        self.fills += 1
        self.volume += qty
        mid = ctx.get("mid")
        log("MM FILL %s %s %d @ %.2f | mid %s | net_before %s"
            % (ticker, side, qty, price, f2(mid), ctx.get("net")))
        self.pending_marks.append((time.time(), ticker, side, qty, price, mid))
        if LOG:
            LOG.trade_row({"time": time.strftime("%H:%M:%S"), "tick": tick, "type": "MM",
                           "ticker_or_buy_on": ticker, "side_or_sell_on": side,
                           "qty": qty, "price_or_buy_vwap": price, "mid": f2(mid),
                           "M_bid": ctx.get("M_bid"), "M_ask": ctx.get("M_ask"),
                           "A_bid": ctx.get("A_bid"), "A_ask": ctx.get("A_ask"),
                           "net_before": ctx.get("net")})
        if self.cockpit:
            self.cockpit.log_trade([time.strftime("%H:%M:%S"), tick, "MM",
                                    ticker, side, qty, price, "", "", ""])

    def mark_fills(self, mid):
        """MM_MARK_SEC after each fill, compare the fill price with the mid:
        buy filled at p, mid now m -> (m - p) > 0 means we bought below fair."""
        if mid is None:
            return
        now = time.time()
        keep = []
        for rec in self.pending_marks:
            t, ticker, side, qty, price, mid0 = rec
            if now - t < MM_MARK_SEC:
                keep.append(rec)
                continue
            sign = 1 if side == "BUY" else -1
            adv = (mid - price) * sign
            self.adverse_total += adv * qty
            self.adverse_qty += qty
            log("MM MARK %s %s %d @ %.2f -> mid %.3f after %.0fs: %+.3f/share (%+.0f)"
                % (ticker, side, qty, price, mid, MM_MARK_SEC, adv, adv * qty), console=False)
        self.pending_marks = keep

    def _apply(self, key, q, o, tick, ctx):
        """Record the incremental fill between what we knew and order `o`."""
        filled = int(o.get("quantity_filled") or 0)
        if filled > q["filled"]:
            v1 = o.get("vwap") or q["price"]
            v0 = q.get("vwap") or q["price"]
            d = filled - q["filled"]
            px = (v1 * filled - v0 * q["filled"]) / d
            self._fill(key[0], key[1], d, round(px, 4), tick, ctx)
            q["filled"], q["vwap"] = filled, v1

    def sync_fills(self, tick, ctx, now=None):
        """Reconcile our quotes with the server. A quote missing from the OPEN
        list is NOT gone: the server lists new orders late, so look it up in
        TRANSACTED/CANCELLED and only forget it after ORDER_LOST_SEC."""
        now = now or time.time()
        if not self.quotes and not self.settling:
            return
        try:
            open_ = dict((o["order_id"], o) for o in self.api.orders("OPEN"))
        except Exception as e:
            log("! sync_fills: %s" % e)
            return
        done = None
        for key in list(self.quotes.keys()):
            q = self.quotes[key]
            o = open_.get(q["id"])
            if o is None:                                # not listed (yet?) as OPEN
                if done is None:
                    try:
                        done = self.api.done_orders()
                    except Exception as e:
                        log("! done_orders: %s" % e)
                        return
                o = done.get(q["id"])
                if o is None:
                    if now - q["t"] > ORDER_LOST_SEC:
                        log("! MM quote %s id=%s never listed by server in %.0fs - forgetting it"
                            % (key, q["id"], ORDER_LOST_SEC))
                        del self.quotes[key]
                    continue
            self._apply(key, q, o, tick, ctx)
            if o["status"] != "OPEN":
                del self.quotes[key]
        if self.settling:
            if done is None:
                try:
                    done = self.api.done_orders()
                except Exception:
                    done = {}
            for rec in list(self.settling):
                o = done.get(rec["id"])
                if o is not None:
                    self._apply(rec["key"], rec, o, tick, ctx)
                    self.settling.remove(rec)
                elif now - rec["t"] > ORDER_LOST_SEC:
                    self.settling.remove(rec)
        self._sweep_orphans(open_, now)

    def _sweep_orphans(self, open_, now):
        """Cancel any OPEN order of ours that no strategy is tracking (ARB legs
        are settled synchronously, so anything untracked twice in a row is a
        leak). This is what stops forgotten quotes from filling later."""
        mine = set(q["id"] for q in self.quotes.values())
        mine.update(r["id"] for r in self.settling)
        stale = []
        for oid in list(self.untracked_seen):
            if oid not in open_ or oid in mine:
                del self.untracked_seen[oid]
        for oid, o in open_.items():
            if oid in mine:
                continue
            t0 = self.untracked_seen.setdefault(oid, now)
            if now - t0 >= 0.5:
                stale.append(oid)
        if stale:
            got = self.api.cancel_ids(stale)
            log("ORPHAN cancel %d untracked orders %s -> cancelled %s"
                % (len(stale), [(open_[i]["ticker"], open_[i]["action"], open_[i]["price"],
                                 open_[i]["quantity"]) for i in stale][:6], got))
            for oid in stale:
                self.untracked_seen.pop(oid, None)

    def _retire(self, key, q, now):
        """Quote is being cancelled: keep it until its final fill count is known."""
        self.settling.append({"id": q["id"], "key": key, "price": q["price"], "qty": q["qty"],
                              "filled": q["filled"], "vwap": q.get("vwap"), "t": now})

    def cancel_all_quotes(self):
        now = time.time()
        self.api.cancel_ids([q["id"] for q in self.quotes.values()])
        for key, q in self.quotes.items():
            self._retire(key, q, now)
        self.quotes = {}

    def requote(self, p, lv, net, room, tick, now, ctx, active=True):
        if not (p["MM enabled"] and active):
            if self.quotes:
                log("MM disabled -> cancelling %d quotes" % len(self.quotes))
                self.cancel_all_quotes()
            return
        if now - self.last_requote < p["MM requote sec"]:
            return
        self.last_requote = now

        best_bid, best_ask, mid = consolidated(lv)
        if mid is None:
            return
        vol = self.vol_range()
        half = max(p["MM half spread"], p["MM vol mult"] * vol, p["MM min spread"] / 2.0)
        self.eff_half = half
        skew = p["MM skew per 1k"] * net / 1000.0
        raw_bid, raw_ask = rnd(mid - half - skew), rnd(mid + half - skew)
        bid_px = min(raw_bid, rnd(best_ask - TICK_SIZE))
        ask_px = max(raw_ask, rnd(best_bid + TICK_SIZE))
        if ask_px - bid_px < TICK_SIZE - 1e-9:
            ask_px = rnd(bid_px + TICK_SIZE)

        size = int(min(p["MM size"], p["Max order"], CASE_MAX_ORDER))
        want = {}
        for t, flag in ((TICKER_MAIN, "MM on Main"), (TICKER_ALT, "MM on Alt")):
            if not p[flag]:
                continue
            if net < p["MM max inv"]:
                want[(t, "BUY")] = bid_px
            if net > -p["MM max inv"]:
                want[(t, "SELL")] = ask_px

        to_cancel = []
        for key, q in list(self.quotes.items()):
            if key not in want or abs(want[key] - q["price"]) > 1e-9:
                to_cancel.append(q["id"])
                self._retire(key, q, now)
                del self.quotes[key]
        self.api.cancel_ids(to_cancel)

        avail = room - self.open_qty()
        posted = []
        for key, px in want.items():
            if key in self.quotes:
                continue
            qty = int(min(size, avail))
            if qty <= 0:
                break
            o = self.api.limit_order(key[0], key[1], qty, px)
            if o:
                # the POST response is a pre-match snapshot: always track the
                # order and let sync_fills discover what actually happened
                self.quotes[key] = {"id": o["order_id"], "price": px, "qty": qty,
                                    "filled": 0, "vwap": None, "t": now}
                avail -= qty
                posted.append("%s/%s@%.2f" % (key[0][-1], key[1][0], px))
        log("MM requote mid=%.3f bb=%.2f ba=%.2f vol=%.3f half=%.3f (base %.3f vol %.3f "
            "user-min %.3f) skew=%+.3f net=%d -> bid %.2f ask %.2f (raw %.2f/%.2f) "
            "cancel=%d post=%s resting=%d"
            % (mid, best_bid, best_ask, vol, half, p["MM half spread"], p["MM vol mult"] * vol,
               p["MM min spread"] / 2.0, skew, net, bid_px, ask_px, raw_bid, raw_ask,
               len(to_cancel), posted, len(self.quotes)), console=False)


# =============================================================================
# Position management
# =============================================================================
def read_room(api, secs, cap):
    net = net_position(secs)
    try:
        lim = api.limits()[0]
        gross = abs(lim["gross"])
        used = max(gross, abs(lim["net"]))
        cap = min(cap, lim["gross_limit"] or CASE_GROSS_LIMIT,
                  lim["net_limit"] or CASE_GROSS_LIMIT)
    except Exception:
        gross, used = abs(net), abs(net)
        cap = min(cap, CASE_GROSS_LIMIT)
    return int(net), gross, int(cap - used)


def flatten(api, lv, max_order):
    """Close the aggregate net position on whichever exchange prices it best."""
    net = api.positions()
    if net == 0:
        return
    action = "SELL" if net > 0 else "BUY"
    side = "bid" if net > 0 else "ask"
    best_t, best_px = TICKER_MAIN, None
    for t in TICKERS:
        px = top(lv.get(t, {}).get(side, []))[0] if lv else None
        if px is not None and (best_px is None or
                               (px > best_px if net > 0 else px < best_px)):
            best_t, best_px = t, px
    qty = abs(net)
    while qty > 0:
        chunk = min(qty, int(max_order))
        api.market_order(best_t, action, chunk)
        qty -= chunk
    log("Flattened %d on %s (best %s %s)" % (net, best_t, side, f2(best_px)))


# =============================================================================
# Main loop
# =============================================================================
def main():
    global LOG
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
    except Exception as e:
        log("Cannot reach RIT at localhost:%d - client running? key right?\n%s" % (PORT, e))
        return
    me = trader["trader_id"]
    log("Connected: %s %s (%s) | case '%s' | tick %s/%s | status %s | limits enforced %s"
        % (trader["first_name"], trader["last_name"], me, case["name"], case["tick"],
           case["ticks_per_period"], case["status"], case.get("is_enforce_trading_limits")))
    try:
        for s in api.securities():
            log("SECURITY %s" % s, console=False)
        log("LIMITS %s" % api.limits(), console=False)
    except Exception as e:
        log("! could not dump securities/limits: %s" % e)

    cockpit = None
    if USE_EXCEL:
        try:
            cockpit = Cockpit(os.path.join(here, EXCEL_FILE))
            log("Excel cockpit open: %s" % os.path.join(here, EXCEL_FILE))
        except Exception as e:
            log("Excel cockpit unavailable (%s) - running with DEFAULTS" % e)

    mm = MarketMaker(api, cockpit)
    p = dict(PARAM_DEFAULTS)
    p_logged = {}
    arb_pnl, arb_trades, arb_slip, arb_missed = 0.0, 0, 0.0, 0
    arb_pause_until = 0.0
    last_tick, last_live, last_slow = -1, 0.0, 0.0
    lv = {}
    secs, net, gross, room = {}, 0, 0, 0
    status, tick = case["status"], case["tick"]
    nlv = trader.get("nlv")
    stop_reason = "stopped by user"
    last_status = "ACTIVE"
    n_err = 0
    hard_flat_until = 0.0

    try:
        while True:
            try:
                t_loop = time.time()
                slow = t_loop - last_slow >= SLOW_LANE_SEC
                if slow:
                    last_slow = t_loop
                    if cockpit:
                        p = cockpit.read_params()
                    case = api.case()
                    status, tick = case["status"], case["tick"]
                    LOG.tick = tick
                    changed = dict((k, v) for k, v in p.items() if p_logged.get(k) != v)
                    if changed:
                        log("PARAMS %s" % changed)
                        p_logged = dict(p)

                if p["Panic"]:
                    log("PANIC - cancelling, flattening, stopping")
                    api.cancel_all()
                    mm.quotes = {}
                    flatten(api, lv, p["Max order"])
                    stop_reason = "panic"
                    break
                if p["Flatten now"]:
                    log("Flatten requested from Excel")
                    mm.cancel_all_quotes()
                    flatten(api, lv, p["Max order"])
                    p["Flatten now"] = 0
                if status != "ACTIVE":
                    if status != last_status:
                        log("case %s - waiting (quotes cancelled)" % status)
                        mm.cancel_all_quotes()
                    last_status = status
                    time.sleep(0.5)
                    continue
                if last_status != "ACTIVE":
                    log("case ACTIVE - trading")
                    last_status = "ACTIVE"

                # --- books every loop --------------------------------------
                depth = int(p["Book depth"])
                lv = {}
                for t in TICKERS:
                    bid, ask = book_sides(api.book(t, depth))
                    lv[t] = {"bid": levels(bid, me), "ask": levels(ask, me)}
                mb, ma = top(lv[TICKER_MAIN]["bid"]), top(lv[TICKER_MAIN]["ask"])
                ab, aa = top(lv[TICKER_ALT]["bid"]), top(lv[TICKER_ALT]["ask"])
                best_bid, best_ask, mid = consolidated(lv)
                mm.record_mid(mid, p["MM vol window"])
                cross_ma = round(ab[0] - ma[0], 3) if (ab[0] is not None and ma[0] is not None) else ""
                cross_am = round(mb[0] - aa[0], 3) if (mb[0] is not None and aa[0] is not None) else ""
                ctx = {"mid": mid, "net": net, "M_bid": f2(mb[0]), "M_ask": f2(ma[0]),
                       "A_bid": f2(ab[0]), "A_ask": f2(aa[0])}

                # --- slow lane: positions / limits / fills -----------------
                if slow:
                    secs = dict((s["ticker"], s) for s in api.securities())
                    net, gross, room = read_room(api, secs, p["Gross cap"])
                    ctx["net"] = net
                    mm.sync_fills(tick, ctx, t_loop)
                    mm.mark_fills(mid)
                    if abs(net) >= p["Hard flat |net|"] and t_loop >= hard_flat_until:
                        log("!! HARD FLAT: |net|=%d >= %d -> pulling quotes, flattening"
                            % (abs(net), p["Hard flat |net|"]))
                        mm.cancel_all_quotes()
                        flatten(api, lv, p["Max order"])
                        net = stable_positions(api)
                        log("   after hard flat: net=%d" % net)
                        hard_flat_until = t_loop + 2.0
                        arb_pause_until = t_loop + 2.0

                active = bool(p["Enabled"])

                # --- arbitrage ---------------------------------------------
                traded = False
                if active and p["ARB enabled"] and t_loop >= arb_pause_until:
                    max_qty = int(min(p["ARB max qty"], p["Max order"], CASE_MAX_ORDER,
                                      max(room - mm.open_qty(), 0)))
                    opp = find_opportunity(lv, max_qty, p["Cost/share"], p["Min edge"]) \
                        if max_qty > 0 else None
                    if opp and opp.profit > 0:
                        log("ARB OPP buy %d %s lim %.2f (exp %.3f) sell %s lim %.2f (exp %.3f) "
                            "edge %.3f | books M %s/%s A %s/%s | room %d"
                            % (opp.qty, opp.buy_on, opp.buy_limit, opp.buy_avg, opp.sell_on,
                               opp.sell_limit, opp.sell_avg, opp.edge, f2(mb[0]), f2(ma[0]),
                               f2(ab[0]), f2(aa[0]), room), console=False)
                        res = execute_arb(api, opp)
                        traded = True
                        last_slow = 0.0
                        if res is None or res["matched"] == 0:
                            arb_missed += 1
                            arb_pause_until = t_loop + p["ARB pause sec"]
                            log("ARB MISS bought %s sold %s (cross gone) -> pause %.0fs"
                                % (res["bought"] if res else 0, res["sold"] if res else 0,
                                   p["ARB pause sec"]))
                            if res and res["residual"]:
                                unwind_residual(api, lv, res["residual"])
                        else:
                            m, bv, sv, pnl = res["matched"], res["bv"], res["sv"], res["pnl"]
                            slip = (bv - opp.buy_avg) + (opp.sell_avg - sv)
                            arb_pnl += pnl
                            arb_trades += 1
                            arb_slip += slip * m
                            room -= m
                            log("ARB DONE %5d/%d buy %s @%.3f (exp %.3f) | sell %s @%.3f (exp %.3f) "
                                "| edge %+.3f exp %+.3f slip %+.3f/sh | P&L %+.2f | resid %+d | %.0f ms"
                                % (m, opp.qty, opp.buy_on, bv, opp.buy_avg, opp.sell_on, sv,
                                   opp.sell_avg, sv - bv, opp.edge, slip, pnl, res["residual"],
                                   res["ms"]))
                            if res["residual"]:
                                unwind_residual(api, lv, res["residual"])
                            if pnl <= 0 or res["residual"]:
                                arb_pause_until = t_loop + p["ARB pause sec"]
                                log("ARB pause %.0fs (losing or one-legged)" % p["ARB pause sec"])
                            LOG.trade_row({"time": time.strftime("%H:%M:%S"), "tick": tick,
                                           "type": "ARB", "ticker_or_buy_on": opp.buy_on,
                                           "side_or_sell_on": opp.sell_on, "qty": m,
                                           "price_or_buy_vwap": bv, "sell_vwap": sv,
                                           "exp_buy": opp.buy_avg, "exp_sell": opp.sell_avg,
                                           "edge": sv - bv, "pnl": pnl, "mid": f2(mid),
                                           "M_bid": f2(mb[0]), "M_ask": f2(ma[0]),
                                           "A_bid": f2(ab[0]), "A_ask": f2(aa[0]),
                                           "net_before": net})
                            if cockpit:
                                cockpit.log_trade([time.strftime("%H:%M:%S"), tick, "ARB",
                                                   opp.buy_on, opp.sell_on, m, bv, sv,
                                                   sv - bv - 2 * opp.cost, pnl])

                # --- market making -----------------------------------------
                if not traded:
                    mm.requote(p, lv, net, room, tick, time.time(), ctx, active)

                # --- per-loop CSV row --------------------------------------
                realized = sum(secs.get(t, {}).get("realized") or 0 for t in TICKERS)
                unreal = sum(secs.get(t, {}).get("unrealized") or 0 for t in TICKERS)
                LOG.tick_row({
                    "time": "%.3f" % t_loop, "tick": tick,
                    "loop_ms": round((time.time() - t_loop) * 1000, 1), "status": status,
                    "net": net, "gross": gross, "room": room,
                    "M_bid": f2(mb[0]), "M_bid_sz": f2(mb[1]), "M_ask": f2(ma[0]), "M_ask_sz": f2(ma[1]),
                    "A_bid": f2(ab[0]), "A_bid_sz": f2(ab[1]), "A_ask": f2(aa[0]), "A_ask_sz": f2(aa[1]),
                    "M_last": secs.get(TICKER_MAIN, {}).get("last", ""),
                    "A_last": secs.get(TICKER_ALT, {}).get("last", ""),
                    "cross_MA": cross_ma, "cross_AM": cross_am, "mid": f2(mid),
                    "vol_range": round(mm.vol_range(), 4),
                    "our_bid_M": mm.our_quote(TICKER_MAIN, "BUY"), "our_ask_M": mm.our_quote(TICKER_MAIN, "SELL"),
                    "our_bid_A": mm.our_quote(TICKER_ALT, "BUY"), "our_ask_A": mm.our_quote(TICKER_ALT, "SELL"),
                    "mm_open_qty": mm.open_qty(),
                    "arb_trades": arb_trades, "arb_pnl": round(arb_pnl, 2),
                    "mm_fills": mm.fills, "mm_vol": mm.volume,
                    "realized": realized, "unrealized": unreal, "nlv": f2(nlv),
                    "calls_ps": api.calls_last_sec(), "n429": api.n_429,
                })

                # --- Excel live panel --------------------------------------
                now = time.time()
                if cockpit and (traded or now - last_live > 0.2):
                    last_live = now
                    cockpit.write_live({
                        "Updated": time.strftime("%H:%M:%S"), "Tick": tick,
                        "Case status": status + ("" if p["Enabled"] else "  (DISABLED)"),
                        "NLV": f2(nlv),
                        "Realized P&L (RIT)": realized, "Unrealized P&L (RIT)": unreal,
                        "ARB trades": arb_trades, "ARB est. P&L": round(arb_pnl, 2),
                        "MM fills": mm.fills, "MM volume": mm.volume,
                        "Pos CRZY_M": secs.get(TICKER_MAIN, {}).get("position", ""),
                        "Pos CRZY_A": secs.get(TICKER_ALT, {}).get("position", ""),
                        "Net": net, "Gross (RIT)": gross, "Room": room,
                        "M bid": f2(mb[0]), "M bid size": f2(mb[1]), "M ask": f2(ma[0]), "M ask size": f2(ma[1]),
                        "A bid": f2(ab[0]), "A bid size": f2(ab[1]), "A ask": f2(aa[0]), "A ask size": f2(aa[1]),
                        "Cross M->A (buy M, sell A)": cross_ma,
                        "Cross A->M (buy A, sell M)": cross_am,
                        "Our bid M": mm.our_quote(TICKER_MAIN, "BUY"),
                        "Our ask M": mm.our_quote(TICKER_MAIN, "SELL"),
                        "Our bid A": mm.our_quote(TICKER_ALT, "BUY"),
                        "Our ask A": mm.our_quote(TICKER_ALT, "SELL"),
                        "MM eff. spread (ask-bid)": round(2 * mm.eff_half, 3),
                        "API calls/sec": api.calls_last_sec(),
                        "Rate-limit hits": api.n_429,
                        "Last message": LOG.last,
                    })
                if tick != last_tick and tick % 15 == 0:
                    trader = api.trader()
                    nlv = trader["nlv"]
                    adv = mm.adverse_total / mm.adverse_qty if mm.adverse_qty else 0.0
                    log("[tick %3d] NLV=%.2f real=%.2f unreal=%.2f | arb=%d miss=%d "
                        "pnl=%+.2f slip=%+.2f | mm fills=%d vol=%d adverse=%+.4f/sh | net=%d "
                        "room=%d | api/s=%d 429=%d"
                        % (tick, nlv, realized, unreal, arb_trades, arb_missed, arb_pnl,
                           arb_slip, mm.fills, mm.volume, adv, net, room,
                           api.calls_last_sec(), api.n_429))
                last_tick = tick

                if traded:
                    continue
                time.sleep(p["Poll seconds"])

            except KeyboardInterrupt:
                raise
            except Exception:
                n_err += 1
                log("! LOOP ERROR #%d\n%s" % (n_err, traceback.format_exc()))
                time.sleep(0.5)
    except KeyboardInterrupt:
        stop_reason = "interrupted"
        print("\nInterrupted.")
    finally:
        try:
            api.cancel_all()
            flatten(api, lv, p["Max order"])
        except Exception as e:
            log("cleanup failed: %s" % e)
        try:
            nlv = api.trader()["nlv"]
            secs = dict((s["ticker"], s) for s in api.securities())
            for t in TICKERS:
                s = secs.get(t, {})
                log("FINAL %s pos=%s realized=%s unrealized=%s volume=%s"
                    % (t, s.get("position"), s.get("realized"), s.get("unrealized"),
                       s.get("volume")))
        except Exception as e:
            log("final state failed: %s" % e)
        try:
            orders = api.orders()
            LOG.dump_orders(orders)
            log("Dumped %d orders to %s" % (len(orders), LOG.paths["orders"]))
        except Exception as e:
            log("order dump failed: %s" % e)
        adv = mm.adverse_total / mm.adverse_qty if mm.adverse_qty else 0.0
        log("DONE (%s). NLV=%.2f | arb=%d miss=%d pnl=%+.2f slip=%+.2f | mm fills=%d vol=%d "
            "adverse=%+.4f/sh (%+.0f total) | loop errors=%d | api calls=%d 429s=%d"
            % (stop_reason, nlv if nlv is not None else float("nan"), arb_trades, arb_missed,
               arb_pnl, arb_slip, mm.fills, mm.volume, adv, mm.adverse_total, n_err,
               api.n_calls, api.n_429))
        if cockpit:
            cockpit.write_live({"Case status": "ALGO STOPPED (%s)" % stop_reason,
                                "ARB trades": arb_trades, "ARB est. P&L": round(arb_pnl, 2),
                                "MM fills": mm.fills, "MM volume": mm.volume,
                                "NLV": f2(nlv), "Last message": LOG.last,
                                "Updated": time.strftime("%H:%M:%S")})
        print("Log files:\n  " + "\n  ".join(LOG.paths.values()))
        LOG.close()


if __name__ == "__main__":
    main()
