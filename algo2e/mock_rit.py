#!/usr/bin/env python3
"""
mock_rit.py - a stand-alone mock of the Rotman Interactive Trader (RIT) REST API.

Standard library only (Python 3.10).  Listens on http://localhost:<port>/v1 and
requires the header  X-API-Key: <anything non-empty>.

    python mock_rit.py --port 9999 --speed 1.0 --list-lag 0.3 --order-rate 10 \
                       --seed 1 --vol 0.004 --aggr-rate 4

One stock (ALGO, start 10.00, tick 0.01) with a synthetic limit-order book that
tracks a random-walking fair price, synthetic aggressive market orders that sweep
the book (and the bot's resting orders) in price/time priority, RIT-style quirks
(listing lag after POST, pre-match POST snapshot, 404 on non-open orders, 429 rate
limits), and the ALGO2 fee model (MARKET pays 0.01/share, LIMIT earns 0.005/share).
"""
import argparse
import json
import os
import random
import sys
import threading
import time
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

TICKER = "ALGO"
BOT = "test_trader"
ANON = "anon"
START_PRICE = 10.00
TICK_SIZE = 0.01
LEVELS_PER_SIDE = 10
GROSS_LIMIT = 25000
NET_LIMIT = 25000
FINE_PER_SHARE_PER_TICK = 0.10
MARKET_FEE = 0.01
LIMIT_REBATE = 0.005
MAX_TRADE_SIZE = 5000
GLOBAL_CALLS_PER_SEC = 100


def r2(x):
    return round(x, 2)


def cents(p):
    return int(round(p * 100))


# ----------------------------------------------------------------------------
# Order
# ----------------------------------------------------------------------------
class Order:
    __slots__ = ("order_id", "seq", "trader_id", "type", "quantity", "action",
                 "price", "quantity_filled", "notional", "status", "tick",
                 "created", "visible_at")

    def __init__(self, order_id, seq, trader_id, otype, quantity, action, price,
                 tick, visible_at=0.0):
        self.order_id = order_id
        self.seq = seq
        self.trader_id = trader_id
        self.type = otype
        self.quantity = float(quantity)
        self.action = action
        self.price = None if price is None else r2(price)
        self.quantity_filled = 0.0
        self.notional = 0.0
        self.status = "OPEN"
        self.tick = tick
        self.created = time.time()
        self.visible_at = visible_at

    @property
    def remaining(self):
        return self.quantity - self.quantity_filled

    @property
    def vwap(self):
        if self.quantity_filled <= 0:
            return None
        return round(self.notional / self.quantity_filled, 4)

    def to_dict(self):
        return {
            "order_id": self.order_id,
            "period": 1,
            "tick": self.tick,
            "trader_id": self.trader_id,
            "ticker": TICKER,
            "type": self.type,
            "quantity": self.quantity,
            "action": self.action,
            "price": self.price,
            "quantity_filled": self.quantity_filled,
            "vwap": self.vwap,
            "status": self.status,
        }


# ----------------------------------------------------------------------------
# Market / matching engine
# ----------------------------------------------------------------------------
class Market:
    def __init__(self, args):
        self.args = args
        self.lock = threading.RLock()
        self.rng = random.Random(args.seed)
        self.stop_event = threading.Event()

        self.fair = START_PRICE
        self.last = START_PRICE
        self.drift = 0.0
        self.drift_until = 0.0
        self.bid_off = 0.02
        self.ask_off = 0.02

        self.bids = []   # resting Orders, kept sorted best-first (price desc, seq asc)
        self.asks = []   # resting Orders, kept sorted best-first (price asc, seq asc)
        self.next_oid = 1
        self.next_seq = 1

        self.bot_orders = {}  # order_id -> Order (all statuses)
        self.position = 0.0
        self.cash = 0.0
        self.avg_cost = 0.0
        self.fees = 0.0
        self.rebates = 0.0
        self.fines = 0.0
        self.volume = 0.0
        self.bot_volume = 0.0
        self.n_bot_fills = 0
        self.last_fine_tick = 0

        self.order_times = deque()  # accepted POST /orders timestamps (per security)
        self.call_times = {}        # api key -> deque of accepted call timestamps

        self.start_time = time.time()
        self.stopped_announced = False

    # ---- time / case ---------------------------------------------------
    def tick(self):
        elapsed = time.time() - self.start_time
        t = int(elapsed / self.args.speed)
        return min(t, self.args.ticks_per_period)

    def case_status(self):
        return "ACTIVE" if self.tick() < self.args.ticks_per_period else "STOPPED"

    # ---- ids -----------------------------------------------------------
    def _new_order(self, trader, otype, qty, action, price, visible_at=0.0):
        o = Order(self.next_oid, self.next_seq, trader, otype, qty, action, price,
                  self.tick(), visible_at)
        self.next_oid += 1
        self.next_seq += 1
        return o

    # ---- book helpers --------------------------------------------------
    def _insert(self, o):
        if o.action == "BUY":
            self.bids.append(o)
            self.bids.sort(key=lambda x: (-x.price, x.seq))
        else:
            self.asks.append(o)
            self.asks.sort(key=lambda x: (x.price, x.seq))

    def _remove_from_book(self, o):
        book = self.bids if o.action == "BUY" else self.asks
        try:
            book.remove(o)
        except ValueError:
            pass

    def best_bid(self):
        return self.bids[0] if self.bids else None

    def best_ask(self):
        return self.asks[0] if self.asks else None

    # ---- P&L -----------------------------------------------------------
    def _apply_pnl(self, action, qty, price):
        signed = qty if action == "BUY" else -qty
        pos = self.position
        self.cash -= signed * price
        if pos == 0 or (pos > 0) == (signed > 0):
            # increasing / opening in the same direction: blend average cost
            self.avg_cost = (abs(pos) * self.avg_cost + qty * price) / (abs(pos) + qty)
            self.position = pos + signed
        else:
            new_pos = pos + signed
            if new_pos == 0:
                self.avg_cost = 0.0
            elif (new_pos > 0) != (pos > 0):
                # flipped through zero: the remainder opens at this price
                self.avg_cost = price
            # partial close: avg cost unchanged
            self.position = new_pos

    def realized(self):
        # cash already includes fees, rebates and fines
        return self.cash + self.position * self.avg_cost

    def unrealized(self):
        return self.position * (self.last - self.avg_cost)

    def nlv(self):
        return self.cash + self.position * self.last

    # ---- matching ------------------------------------------------------
    def _fill(self, o, qty, price):
        o.quantity_filled += qty
        o.notional += qty * price
        if o.trader_id == BOT:
            self.n_bot_fills += 1
            self.bot_volume += qty
            self._apply_pnl(o.action, qty, price)
            if o.type == "MARKET":
                fee = MARKET_FEE * qty
                self.fees += fee
                self.cash -= fee
            else:
                reb = LIMIT_REBATE * qty
                self.rebates += reb
                self.cash += reb

    def _match(self, taker):
        """Walk the opposite side of the book, best price first, then time priority."""
        book = self.asks if taker.action == "BUY" else self.bids
        i = 0
        while taker.remaining > 0 and i < len(book):
            rest = book[i]
            if taker.price is not None:
                if taker.action == "BUY" and rest.price > taker.price + 1e-9:
                    break
                if taker.action == "SELL" and rest.price < taker.price - 1e-9:
                    break
            q = min(taker.remaining, rest.remaining)
            px = rest.price
            self._fill(rest, q, px)
            self._fill(taker, q, px)
            self.volume += q
            self.last = px
            if rest.remaining <= 1e-9:
                rest.status = "TRANSACTED"
                book.pop(i)
            else:
                i += 1
        if taker.type == "MARKET":
            taker.status = "TRANSACTED" if taker.quantity_filled > 0 else "CANCELLED"
        elif taker.remaining <= 1e-9:
            taker.status = "TRANSACTED"

    # ---- synthetic participants ---------------------------------------
    def _step_fair(self, now):
        rng = self.rng
        if now > self.drift_until:
            if self.drift != 0.0:
                self.drift = 0.0
                self.drift_until = now + rng.uniform(3, 10)  # cool-down
            elif rng.random() < 0.01:  # ~ every 10 s a trend regime starts
                self.drift = rng.choice((-1.0, 1.0)) * 0.002
                self.drift_until = now + rng.uniform(10, 30)
        self.fair += rng.gauss(0.0, self.args.vol) + self.drift
        if rng.random() < 1.0 / 600.0:  # ~ once a minute at 10 steps/s
            self.fair += rng.choice((-1.0, 1.0)) * rng.uniform(0.05, 0.15)
        self.fair = max(self.fair, 0.50)

    def _refresh_book(self):
        rng = self.rng
        if rng.random() < 0.2:
            self.bid_off = rng.uniform(0.01, 0.03)
        if rng.random() < 0.2:
            self.ask_off = rng.uniform(0.01, 0.03)
        bb = cents(self.fair - self.bid_off)
        ba = cents(self.fair + self.ask_off)
        if ba <= bb:
            ba = bb + 1
        bid_targets = [bb - i for i in range(LEVELS_PER_SIDE)]
        ask_targets = [ba + i for i in range(LEVELS_PER_SIDE)]
        bid_set, ask_set = set(bid_targets), set(ask_targets)

        def keep(o, tset):
            if o.trader_id == BOT:
                return True
            if cents(o.price) not in tset:
                return False
            if o.remaining < 100:
                return False
            return rng.random() > 0.02  # a little churn / re-queueing

        purged = [o for o in self.bids if not keep(o, bid_set)]
        for o in purged:
            o.status = "CANCELLED"
        self.bids = [o for o in self.bids if o.status == "OPEN"]
        purged = [o for o in self.asks if not keep(o, ask_set)]
        for o in purged:
            o.status = "CANCELLED"
        self.asks = [o for o in self.asks if o.status == "OPEN"]

        have_b = {cents(o.price) for o in self.bids if o.trader_id == ANON}
        have_a = {cents(o.price) for o in self.asks if o.trader_id == ANON}
        for c in bid_targets:
            if c not in have_b:
                o = self._new_order(ANON, "LIMIT", rng.randint(500, 6000), "BUY", c / 100.0)
                self._match(o)  # may pick off stale bot asks resting below
                if o.status == "OPEN" and o.remaining > 0:
                    self._insert(o)
        for c in ask_targets:
            if c not in have_a:
                o = self._new_order(ANON, "LIMIT", rng.randint(500, 6000), "SELL", c / 100.0)
                self._match(o)
                if o.status == "OPEN" and o.remaining > 0:
                    self._insert(o)

    def _synthetic_aggressor(self):
        rng = self.rng
        p_buy = 0.5 + (0.15 if self.drift > 0 else -0.15 if self.drift < 0 else 0.0)
        action = "BUY" if rng.random() < p_buy else "SELL"
        qty = rng.randint(100, 3000)
        o = self._new_order(ANON, "MARKET", qty, action, None)
        self._match(o)

    def _apply_fines(self):
        t = self.tick()
        while self.last_fine_tick < t:
            self.last_fine_tick += 1
            over = abs(self.position) - GROSS_LIMIT
            if over > 0:
                f = FINE_PER_SHARE_PER_TICK * over
                self.fines += f
                self.cash -= f

    def run(self):
        rng = self.rng
        rate = max(self.args.aggr_rate, 0.01)
        next_aggr = time.time() + rng.expovariate(rate)
        next_status = time.time() + 5.0
        while not self.stop_event.is_set():
            now = time.time()
            if self.case_status() == "STOPPED":
                if not self.stopped_announced:
                    self.stopped_announced = True
                    self._print_status(final=True)
                time.sleep(0.5)
                continue
            with self.lock:
                self._step_fair(now)
                self._refresh_book()
                while next_aggr <= now:
                    self._synthetic_aggressor()
                    next_aggr += rng.expovariate(rate)
                self._apply_fines()
            if now >= next_status:
                self._print_status()
                next_status += 5.0
            time.sleep(0.1)

    def _print_status(self, final=False):
        with self.lock:
            bb = self.best_bid()
            ba = self.best_ask()
            n_open = sum(1 for o in self.bot_orders.values() if o.status == "OPEN")
            print("[{}tick {:>4}] fair={:.3f} bid/ask={}/{} last={:.2f} pos={:+.0f} "
                  "realized={:+.2f} unreal={:+.2f} nlv={:+.2f} fees={:.2f} rebates={:.2f} "
                  "bot_open={} bot_fills={} bot_vol={:.0f}".format(
                      "STOPPED " if final else "", self.tick(), self.fair,
                      "%.2f" % bb.price if bb else "-", "%.2f" % ba.price if ba else "-",
                      self.last, self.position, self.realized(), self.unrealized(),
                      self.nlv(), self.fees, self.rebates, n_open, self.n_bot_fills,
                      self.bot_volume), flush=True)

    # ---- bot API operations (called with lock held) ---------------------
    def bot_place(self, otype, qty, action, price):
        lag = self.args.list_lag
        visible_at = time.time() + self.rng.uniform(0.5 * lag, 5.0 / 3.0 * lag)
        o = self._new_order(BOT, otype, qty, action, price, visible_at)
        self.bot_orders[o.order_id] = o
        pre = o.to_dict()  # pre-match snapshot (the real server's usual response)
        self._match(o)
        if o.type == "LIMIT" and o.status == "OPEN" and o.remaining > 0:
            self._insert(o)
        if o.type == "MARKET" or self.rng.random() < 0.2:
            return o.to_dict()
        return pre

    def bot_cancel(self, oid):
        o = self.bot_orders.get(oid)
        if o is None or o.status != "OPEN":
            return False
        o.status = "CANCELLED"
        self._remove_from_book(o)
        return True

    def bot_list(self, status):
        now = time.time()
        return [o.to_dict() for o in sorted(self.bot_orders.values(), key=lambda x: x.order_id)
                if o.status == status and now >= o.visible_at]

    def bot_get(self, oid):
        o = self.bot_orders.get(oid)
        if o is None or o.status != "OPEN" or time.time() < o.visible_at:
            return None
        return o.to_dict()

    def security_dict(self):
        bb = self.best_bid()
        ba = self.best_ask()
        return {
            "ticker": TICKER,
            "type": "STOCK",
            "position": self.position,
            "vwap": round(self.avg_cost, 4) if self.position != 0 else 0.0,
            "nlv": round(self.position * self.last, 2),
            "last": self.last,
            "bid": bb.price if bb else 0.0,
            "bid_size": sum(o.remaining for o in self.bids if bb and cents(o.price) == cents(bb.price)) if bb else 0.0,
            "ask": ba.price if ba else 0.0,
            "ask_size": sum(o.remaining for o in self.asks if ba and cents(o.price) == cents(ba.price)) if ba else 0.0,
            "volume": self.volume,
            "unrealized": round(self.unrealized(), 4),
            "realized": round(self.realized(), 4),
            "currency": "CAD",
            "total_volume": self.volume,
            "start_price": START_PRICE,
            "min_trade_size": 0,
            "max_trade_size": MAX_TRADE_SIZE,
            "trading_fee": MARKET_FEE,
            "limit_order_rebate": LIMIT_REBATE,
            "api_orders_per_second": self.args.order_rate,
            "execution_delay_ms": 0,
            "quoted_decimals": 2,
            "is_tradeable": True,
            "is_shortable": True,
        }

    def debug_dict(self):
        return {
            "fair": round(self.fair, 4),
            "position": self.position,
            "cash": round(self.cash, 4),
            "realized": round(self.realized(), 4),
            "unrealized": round(self.unrealized(), 4),
            "nlv": round(self.nlv(), 4),
            "fees": round(self.fees, 4),
            "rebates": round(self.rebates, 4),
            "fines": round(self.fines, 4),
            "n_bot_orders": len(self.bot_orders),
            "n_bot_fills": self.n_bot_fills,
            "bot_volume": self.bot_volume,
            "tick": self.tick(),
            "last": self.last,
            "drift": self.drift,
        }

    # ---- rate limiting (lock held) --------------------------------------
    def check_global_rate(self, key):
        now = time.time()
        dq = self.call_times.setdefault(key, deque())
        while dq and now - dq[0] > 1.0:
            dq.popleft()
        if len(dq) >= GLOBAL_CALLS_PER_SEC:
            return max(0.05, round(1.0 - (now - dq[0]), 3))
        dq.append(now)
        return None

    def check_order_rate(self):
        now = time.time()
        dq = self.order_times
        while dq and now - dq[0] > 1.0:
            dq.popleft()
        if len(dq) >= self.args.order_rate:
            return max(0.05, round(1.0 - (now - dq[0]), 3))
        dq.append(now)
        return None


# ----------------------------------------------------------------------------
# HTTP layer
# ----------------------------------------------------------------------------
class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    market = None  # set in main()

    def log_message(self, fmt, *args):  # silence per-request logging
        pass

    def _send(self, code, obj):
        body = json.dumps(obj).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _err(self, code, ecode, message, **extra):
        d = {"code": ecode, "message": message}
        d.update(extra)
        self._send(code, d)

    def do_GET(self):
        self._route("GET")

    def do_POST(self):
        self._route("POST")

    def do_DELETE(self):
        self._route("DELETE")

    def _params(self, u):
        q = {k: v[0] for k, v in parse_qs(u.query).items()}
        n = int(self.headers.get("Content-Length") or 0)
        if n > 0:
            raw = self.rfile.read(n)
            ctype = self.headers.get("Content-Type", "")
            try:
                if "json" in ctype:
                    body = json.loads(raw.decode("utf-8") or "{}")
                    if isinstance(body, dict):
                        for k, v in body.items():
                            q.setdefault(k, str(v))
                else:
                    for k, v in parse_qs(raw.decode("utf-8")).items():
                        q.setdefault(k, v[0])
            except Exception:
                pass
        return q

    def _route(self, method):
        m = self.market
        u = urlparse(self.path)
        q = self._params(u)
        key = self.headers.get("X-API-Key")
        if not key:
            return self._err(401, "UNAUTHORIZED", "Missing or invalid X-API-Key header.")
        path = u.path.rstrip("/")
        if not path.startswith("/v1/"):
            return self._err(404, "NOT_FOUND", "Unknown endpoint.")
        path = path[3:]
        with m.lock:
            wait = m.check_global_rate(key)
            if wait is not None:
                return self._err(429, "RATE_LIMIT",
                                 "API rate limit exceeded (%d calls/s)." % GLOBAL_CALLS_PER_SEC,
                                 wait=wait)
            try:
                return self._dispatch(method, path, q)
            except Exception as e:  # never leave the client hanging
                return self._err(500, "INTERNAL_ERROR", "%s: %s" % (type(e).__name__, e))

    def _dispatch(self, method, path, q):
        m = self.market
        a = m.args
        if method == "GET":
            if path == "/case":
                return self._send(200, {
                    "name": "ALGO2 Market Making", "period": 1, "tick": m.tick(),
                    "ticks_per_period": a.ticks_per_period, "total_periods": 1,
                    "status": m.case_status(), "is_enforce_trading_limits": True,
                })
            if path == "/trader":
                return self._send(200, {"trader_id": BOT, "first_name": "Test",
                                        "last_name": "Trader", "nlv": round(m.nlv(), 4)})
            if path == "/limits":
                return self._send(200, [{
                    "name": "LIMIT-STOCK", "gross": abs(m.position), "net": m.position,
                    "gross_limit": GROSS_LIMIT, "net_limit": NET_LIMIT,
                    "gross_fine": round(m.fines, 4), "net_fine": round(m.fines, 4),
                }])
            if path == "/securities":
                t = q.get("ticker")
                if t and t != TICKER:
                    return self._send(200, [])
                return self._send(200, [m.security_dict()])
            if path == "/securities/book":
                t = q.get("ticker")
                if not t:
                    return self._err(400, "INVALID_PARAMETER", "ticker is required.")
                if t != TICKER:
                    return self._err(400, "INVALID_PARAMETER", "Unknown ticker %s." % t)
                try:
                    lim = int(q.get("limit", 20))
                except ValueError:
                    return self._err(400, "INVALID_PARAMETER", "limit must be an integer.")
                return self._send(200, {"bids": [o.to_dict() for o in m.bids[:lim]],
                                        "asks": [o.to_dict() for o in m.asks[:lim]]})
            if path == "/orders":
                st = q.get("status", "OPEN").upper()
                if st not in ("OPEN", "TRANSACTED", "CANCELLED"):
                    return self._err(400, "INVALID_PARAMETER", "Invalid status.")
                return self._send(200, m.bot_list(st))
            if path.startswith("/orders/"):
                oid = self._oid(path)
                d = m.bot_get(oid) if oid is not None else None
                if d is None:
                    return self._err(404, "ORDER_NOT_FOUND",
                                     "Could not find order with id %s." % (oid if oid is not None else path[8:]))
                return self._send(200, d)
            if path == "/_debug":
                return self._send(200, m.debug_dict())
            return self._err(404, "NOT_FOUND", "Unknown endpoint.")

        if method == "POST":
            if path == "/orders":
                return self._post_order(q)
            if path == "/commands/cancel":
                return self._cancel_cmd(q)
            return self._err(404, "NOT_FOUND", "Unknown endpoint.")

        if method == "DELETE":
            if path.startswith("/orders/"):
                oid = self._oid(path)
                if oid is None or not m.bot_cancel(oid):
                    return self._err(404, "ORDER_NOT_FOUND",
                                     "Could not find order with id %s." % (oid if oid is not None else path[8:]))
                return self._send(200, {"success": True})
            return self._err(404, "NOT_FOUND", "Unknown endpoint.")
        return self._err(405, "METHOD_NOT_ALLOWED", "Method not allowed.")

    @staticmethod
    def _oid(path):
        try:
            return int(path.split("/")[2])
        except (ValueError, IndexError):
            return None

    def _post_order(self, q):
        m = self.market
        if m.case_status() != "ACTIVE":
            return self._err(400, "CASE_NOT_ACTIVE", "The case is not active.")
        ticker = q.get("ticker")
        otype = (q.get("type") or "").upper()
        action = (q.get("action") or "").upper()
        if ticker != TICKER:
            return self._err(400, "INVALID_PARAMETER", "Unknown or missing ticker.")
        if otype not in ("LIMIT", "MARKET"):
            return self._err(400, "INVALID_PARAMETER", "type must be LIMIT or MARKET.")
        if action not in ("BUY", "SELL"):
            return self._err(400, "INVALID_PARAMETER", "action must be BUY or SELL.")
        try:
            qty = float(q.get("quantity", ""))
        except ValueError:
            return self._err(400, "INVALID_PARAMETER", "quantity must be a number.")
        if not (1 <= qty <= MAX_TRADE_SIZE) or qty != int(qty):
            return self._err(400, "INVALID_PARAMETER",
                             "quantity must be an integer between 1 and %d." % MAX_TRADE_SIZE)
        price = None
        if otype == "LIMIT":
            try:
                price = float(q.get("price", ""))
            except ValueError:
                return self._err(400, "INVALID_PARAMETER", "price is required for LIMIT orders.")
            if price <= 0:
                return self._err(400, "INVALID_PARAMETER", "price must be positive.")
        wait = m.check_order_rate()
        if wait is not None:
            return self._err(429, "RATE_LIMIT",
                             "Order rate limit exceeded for %s (%d orders/s)." % (TICKER, m.args.order_rate),
                             wait=wait)
        return self._send(200, m.bot_place(otype, int(qty), action, price))

    def _cancel_cmd(self, q):
        m = self.market
        ids = []
        if q.get("all") in ("1", "true", "True"):
            ids = [o.order_id for o in m.bot_orders.values() if o.status == "OPEN"]
        elif q.get("ids"):
            for s in q["ids"].split(","):
                s = s.strip()
                if s:
                    try:
                        ids.append(int(s))
                    except ValueError:
                        return self._err(400, "INVALID_PARAMETER", "ids must be integers.")
        elif q.get("ticker"):
            if q["ticker"] == TICKER:
                ids = [o.order_id for o in m.bot_orders.values() if o.status == "OPEN"]
        else:
            return self._err(400, "INVALID_PARAMETER", "Provide all=1, ids=... or ticker=...")
        cancelled = [oid for oid in ids if m.bot_cancel(oid)]
        return self._send(200, {"cancelled_order_ids": cancelled})


# ----------------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser(description="Mock RIT REST API server")
    ap.add_argument("--port", type=int, default=9999)
    ap.add_argument("--speed", type=float, default=1.0, help="seconds per tick (default 1.0)")
    ap.add_argument("--seconds-per-tick", type=float, default=None, help="alias for --speed")
    ap.add_argument("--ticks-per-period", type=int, default=300)
    ap.add_argument("--list-lag", type=float, default=0.3,
                    help="mean order-listing lag in seconds; actual lag ~ U(0.5x, 1.67x)")
    ap.add_argument("--order-rate", type=int, default=10, help="api_orders_per_second")
    ap.add_argument("--seed", type=int, default=1)
    ap.add_argument("--vol", type=float, default=0.004, help="fair-price sigma per 100 ms step")
    ap.add_argument("--aggr-rate", type=float, default=4.0,
                    help="synthetic aggressive market orders per second")
    args = ap.parse_args()
    if args.seconds_per_tick is not None:
        args.speed = args.seconds_per_tick
    if args.speed <= 0:
        ap.error("--speed must be > 0")

    market = Market(args)
    Handler.market = market
    ThreadingHTTPServer.allow_reuse_address = True
    ThreadingHTTPServer.daemon_threads = True
    srv = ThreadingHTTPServer(("localhost", args.port), Handler)

    t = threading.Thread(target=market.run, name="market", daemon=True)
    t.start()
    print("mock RIT listening on http://localhost:%d/v1  (pid %d)  ticks=%d speed=%.2fs/tick "
          "list_lag=%.2fs order_rate=%d/s seed=%d vol=%.4f aggr_rate=%.1f/s" % (
              args.port, os.getpid(), args.ticks_per_period, args.speed, args.list_lag,
              args.order_rate, args.seed, args.vol, args.aggr_rate), flush=True)
    try:
        srv.serve_forever(poll_interval=0.2)
    except KeyboardInterrupt:
        pass
    finally:
        market.stop_event.set()
        srv.server_close()
        print("mock RIT stopped.", flush=True)


if __name__ == "__main__":
    main()
