#!/usr/bin/env python3
"""
algo2e_range.py  -  Range (mean-reversion) trader for RIT ALGO2e (CNR, RY, AC)

Idea (the "buy low in the range, sell high, put the limit where the swing is"
approach), with a stop-loss added:

  1. For each stock, track the average price and its normal swing (std dev)
     over the last LOOKBACK_SEC seconds.
  2. z = (price - average) / swing.  z = -2 means "unusually cheap".
  3. ENTRY: price is stretched (|z| >= ENTRY_Z, at least MIN_DEV_TICKS from the
     average, and the stock is not drifting hard in that direction)
       -> take a BIG position betting on the bounce back
          (size = RISK_PER_TRADE / swing, capped at MAX_POS shares).
     The most stretched stocks get the gross budget first.
  4. TAKE PROFIT: price back near the average (|z| <= EXIT_Z) -> unwind passively.
  5. STOP-LOSS: price keeps going STOP_STD swings past the entry price,
     or the trade is older than MAX_HOLD_SEC -> cross out, then cool down.
  6. Entries/take-profits use passive limit orders at the touch (rebates on CNR/AC).
     RY entries cross, because taking liquidity in RY PAYS you.
  7. Safety: gross cap below the 25,000 limit (so orders are never rejected),
     $ drawdown kill switch, no new trades in the last 30 ticks, flat by the end.

Few requests per second (one /securities call gives prices for all stocks),
so it is not hurt much by the server rate limit.

Run:   python algo2e_range.py        (Ctrl+C cancels all orders and exits)
Log:   algo2e_range_<time>.csv  (every signal and every position change)
"""

import csv
import os
import statistics
import time
from collections import deque
from datetime import datetime

import requests

# ============================ CONNECTION ============================
BASE_URL = os.environ.get("RIT_URL", "http://136.152.32.17:23422")
RIT_USERNAME = os.environ.get("RIT_USER", "ch_006")
RIT_PASSWORD = os.environ.get("RIT_PASS", "IITmadras@5")
AUTH = (RIT_USERNAME, RIT_PASSWORD)
API_KEY = os.environ.get("RIT_API_KEY", "")   # leave blank for username/password

# ============================== CASE ================================
TICKERS = ["CNR", "RY", "AC"]
TICK = 0.01
FEES = {   # per share, from the case brief
    "CNR": {"active_fee": 0.0027, "passive_rebate": 0.0023},
    "RY":  {"active_fee": -0.0014, "passive_rebate": -0.0020},
    "AC":  {"active_fee": 0.0015, "passive_rebate": 0.0011},
}
EXIT_PRIORITY = sorted(TICKERS, key=lambda t: FEES[t]["active_fee"])   # RY, AC, CNR
CROSS_ENTRY = {"CNR": False, "RY": True, "AC": False}   # RY: taking pays a rebate

# ============================== PACING ==============================
LOOP_SEC = 0.5
REQ_RATE = 6.0
REQ_BURST = 2
CASE_POLL_SEC = 1.0
NEW_ORDER_GRACE_SEC = 2.0

# ============================== SIGNAL ==============================
LOOKBACK_SEC = 60        # window for average and swing
WARMUP_SEC = 30          # need this much history before trading a stock
ENTRY_Z = 1.75           # enter when price is this many swings from average
EXIT_Z = 0.25            # take profit when back within this many swings
MIN_DEV_TICKS = 3        # ...and at least this many ticks from average
STD_FLOOR_TICKS = 2      # swing is never treated as smaller than 2 ticks
TREND_BLOCK = 1.5        # no entry against a drift bigger than this many swings
STOP_STD = 2.5           # stop-loss: price moved this many swings past entry
MAX_HOLD_SEC = 120       # time stop
COOLDOWN_SEC = 30        # after a stop, leave the stock alone this long

# =============================== SIZE ===============================
RISK_PER_TRADE = 1_500   # $ lost if a full position moves 1 swing against you
MAX_POS = 20_000         # max shares in one stock
GROSS_CAP = 22_000       # total shares across stocks (case limit 25,000)
MAX_ORDER = 5_000
MIN_TRADE = 100
CROSS_SLIP_TICKS = 2     # marketable orders may walk 2 ticks into the book

# ============================== SAFETY ==============================
KILL_DRAWDOWN = 5_000
KILL_PAUSE_SEC = 20
MAX_KILLS = 2
STOP_ENTRIES_TICKS_LEFT = 30
CROSS_OUT_TICKS_LEFT = 10
# ====================================================================


def log(msg):
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)


def px(x):
    return round(round(x / TICK) * TICK, 2)


def sgn(x):
    return (x > 0) - (x < 0)


class Bucket:
    def __init__(self, rate, burst):
        self.rate, self.burst = rate, burst
        self.tokens, self.t = float(burst), time.monotonic()
        self.blocked_until = 0.0

    def take(self):
        while True:
            now = time.monotonic()
            if now < self.blocked_until:
                time.sleep(self.blocked_until - now)
                continue
            self.tokens = min(self.burst, self.tokens + (now - self.t) * self.rate)
            self.t = now
            if self.tokens >= 1:
                self.tokens -= 1
                return
            time.sleep((1 - self.tokens) / self.rate)


class API:
    def __init__(self):
        self.s = requests.Session()
        if API_KEY:
            self.s.headers["X-API-Key"] = API_KEY
        else:
            self.s.auth = AUTH
        self.buckets = {}
        self.throttled = 0
        self.throttled_by = {}

    def _call(self, method, path, key, **params):
        b = self.buckets.setdefault(key, Bucket(REQ_RATE, REQ_BURST))
        for _ in range(5):
            b.take()
            try:
                r = self.s.request(method, BASE_URL.rstrip("/") + "/v1" + path,
                                   params=params, timeout=2)
            except requests.RequestException as e:
                log(f"network error on {key}: {e}")
                time.sleep(0.2)
                continue
            if r.status_code == 429:
                try:
                    wait = float(r.json().get("wait", 0.2))
                except Exception:
                    wait = 0.2
                b.blocked_until = time.monotonic() + wait
                self.throttled += 1
                self.throttled_by[key] = self.throttled_by.get(key, 0) + 1
                continue
            if r.ok:
                return r.json() if r.content else {}
            if r.status_code == 404 and method == "DELETE":
                return {"_gone": True}
            log(f"{method} {path} {params} -> {r.status_code} {r.text[:150]}")
            return None
        return None

    def case(self):         return self._call("GET", "/case", "case")
    def trader(self):       return self._call("GET", "/trader", "trader")
    def securities(self):   return self._call("GET", "/securities", "securities")
    def open_orders(self):  return self._call("GET", "/orders", "orders", status="OPEN")
    def cancel(self, oid):  return self._call("DELETE", f"/orders/{oid}", "orders")
    def cancel_all(self):   return self._call("POST", "/commands/cancel", "cancel", all=1)

    def post(self, t, action, qty, price):
        return self._call("POST", "/orders", "orders", ticker=t, type="LIMIT",
                          quantity=int(qty), action=action, price=price)


class Leg:
    """State for one stock."""

    def __init__(self, t):
        self.t = t
        self.hist = deque()                 # (time, mid)
        self.bid = self.ask = self.mid = None
        self.mean = self.std = self.z = self.drift = None
        self.state = "FLAT"                 # FLAT / LONG / SHORT
        self.target = 0
        self.urgent = False                 # True -> exits cross the spread
        self.entry_mid = self.entry_std = self.entry_time = None
        self.cool_until = 0.0
        self.order = None                   # our one working order for this stock
        self.realized = self.unrealized = 0.0

    def update_stats(self, now):
        while self.hist and now - self.hist[0][0] > LOOKBACK_SEC:
            self.hist.popleft()
        if (self.mid is None or len(self.hist) < 20
                or now - self.hist[0][0] < WARMUP_SEC):
            self.z = None
            return
        mids = [m for _, m in self.hist]
        self.mean = statistics.fmean(mids)
        self.std = max(statistics.pstdev(mids), STD_FLOOR_TICKS * TICK)
        self.z = (self.mid - self.mean) / self.std
        third = max(1, len(mids) // 3)
        self.drift = (statistics.fmean(mids[-third:]) - statistics.fmean(mids[:third])) / self.std


class Bot:
    def __init__(self, api):
        self.api = api
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        self.logf = open(f"algo2e_range_{stamp}.csv", "w", newline="")
        self.w = csv.writer(self.logf)
        self.w.writerow(["wall_time", "tick", "event", "ticker", "detail",
                         "position", "target", "mid", "mean", "std", "z"])
        self.reset_run()

    def reset_run(self):
        self.L = {t: Leg(t) for t in TICKERS}
        self.pos = {t: 0 for t in TICKERS}
        self.prev_pos = None
        self.nlv = self.peak = None
        self.kills = 0
        self.paused_until = 0.0
        self.no_more_entries = False
        self.tick = -1
        self.mode = "WAIT"
        self._case = None
        self._case_t = 0.0
        self.last_trader = 0.0
        self.last_status = 0.0

    # ------------------------------------------------------------ logging
    def event(self, ev, leg, detail=""):
        f = lambda v, n=4: "" if v is None else f"{v:.{n}f}"
        self.w.writerow([datetime.now().strftime("%H:%M:%S.%f")[:-3], self.tick, ev, leg.t,
                         detail, self.pos[leg.t], leg.target, f(leg.mid), f(leg.mean),
                         f(leg.std), f(leg.z, 2)])
        self.logf.flush()

    # --------------------------------------------------------------- loop
    def run(self):
        tr = self.api.trader()
        if not tr:
            raise RuntimeError("Could not reach /trader - check URL/login")
        log(f"connected as trader_id={tr.get('trader_id')}  (range strategy)")
        self.api.cancel_all()
        while True:
            t0 = time.monotonic()
            try:
                self.step()
            except KeyboardInterrupt:
                raise
            except Exception as e:
                log(f"loop error: {e!r}")
            dt = time.monotonic() - t0
            if dt < LOOP_SEC:
                time.sleep(LOOP_SEC - dt)

    def step(self):
        now = time.monotonic()
        if self._case is None or now - self._case_t >= CASE_POLL_SEC:
            self._case = self.api.case()
            self._case_t = now
        case = self._case
        if not case or case.get("status") != "ACTIVE":
            self._case = None
            if now - self.last_status > 5:
                log("waiting for case to be ACTIVE...")
                self.last_status = now
            return
        tick = case.get("tick", 0)
        tpp = case.get("ticks_per_period", 300)
        if tick < self.tick:
            log("new run detected - resetting")
            self.api.cancel_all()
            self.reset_run()
        self.tick = tick
        ticks_left = tpp - tick

        # one call: prices + positions for every stock
        secs = self.api.securities()
        if not secs:
            return
        for s in secs:
            t = s.get("ticker")
            if t not in self.L:
                continue
            leg = self.L[t]
            self.pos[t] = int(s.get("position", 0) or 0)
            leg.realized = s.get("realized", 0) or 0
            leg.unrealized = s.get("unrealized", 0) or 0
            bid, ask = s.get("bid"), s.get("ask")
            if bid and ask and ask > bid:
                leg.bid, leg.ask = bid, ask
                leg.mid = (bid + ask) / 2
                leg.hist.append((now, leg.mid))

        if now - self.last_trader >= 1.0:
            tr = self.api.trader()
            if tr:
                self.nlv = tr.get("nlv")
            self.last_trader = now

        oo = self.api.open_orders()
        if oo is not None:
            self.refresh_orders(oo, now)
        self.log_position_changes()

        for leg in self.L.values():
            leg.update_stats(now)
        self.check_kill(now)

        if ticks_left <= CROSS_OUT_TICKS_LEFT or now < self.paused_until:
            self.mode = "CROSS-OUT" if ticks_left <= CROSS_OUT_TICKS_LEFT else "KILL-PAUSE"
            for leg in self.L.values():
                if leg.state != "FLAT" or self.pos[leg.t] != 0:
                    leg.state, leg.target, leg.urgent = "FLAT", 0, True
        else:
            entries_ok = ticks_left > STOP_ENTRIES_TICKS_LEFT and not self.no_more_entries
            self.mode = "TRADING" if entries_ok else "EXITS-ONLY"
            for leg in self.L.values():
                self.decide(leg, now, entries_ok)

        # execute: position-reducing legs first (frees gross), then most stretched
        def order_key(leg):
            q = self.pos[leg.t]
            reducing = abs(leg.target) < abs(q) or sgn(leg.target) != sgn(q)
            return (0 if reducing else 1, -abs(leg.z or 0))
        legs = sorted(sorted(self.L.values(), key=lambda l: EXIT_PRIORITY.index(l.t)), key=order_key)
        for leg in legs:
            self.execute(leg, now)

        if now - self.last_status >= 2.0:
            self.status(tpp)
            self.last_status = now

    # ------------------------------------------------------------ signals
    def decide(self, leg, now, entries_ok):
        q = self.pos[leg.t]
        if leg.state == "FLAT" and q == 0 and leg.target == 0:
            leg.urgent = False
        if leg.z is None:
            return

        if leg.state == "LONG":
            if leg.mid <= leg.entry_mid - STOP_STD * leg.entry_std:
                self.stop(leg, now, "STOP-LOSS")
            elif now - leg.entry_time > MAX_HOLD_SEC:
                self.stop(leg, now, "TIME-STOP")
            elif leg.z >= -EXIT_Z:
                self.take_profit(leg)
        elif leg.state == "SHORT":
            if leg.mid >= leg.entry_mid + STOP_STD * leg.entry_std:
                self.stop(leg, now, "STOP-LOSS")
            elif now - leg.entry_time > MAX_HOLD_SEC:
                self.stop(leg, now, "TIME-STOP")
            elif leg.z <= EXIT_Z:
                self.take_profit(leg)

        if leg.state == "FLAT" and entries_ok and now >= leg.cool_until and not leg.urgent:
            dev = leg.mid - leg.mean
            far_enough = abs(dev) >= MIN_DEV_TICKS * TICK - 1e-9
            if leg.z <= -ENTRY_Z and far_enough and leg.drift > -TREND_BLOCK:
                self.enter(leg, now, +1)
            elif leg.z >= ENTRY_Z and far_enough and leg.drift < TREND_BLOCK:
                self.enter(leg, now, -1)

    def enter(self, leg, now, direction):
        size = min(MAX_POS, int(RISK_PER_TRADE / leg.std / 100) * 100)
        if size < MIN_TRADE:
            return
        leg.state = "LONG" if direction > 0 else "SHORT"
        leg.target = direction * size
        leg.entry_mid, leg.entry_std, leg.entry_time = leg.mid, leg.std, now
        log(f"ENTER {leg.t} {leg.state} target {leg.target:+d}  "
            f"(price {leg.mid:.2f} vs avg {leg.mean:.2f}, z={leg.z:+.2f}, swing {leg.std*100:.1f}c)")
        self.event("ENTER", leg, leg.state)

    def take_profit(self, leg):
        log(f"TAKE PROFIT {leg.t}: back near average (price {leg.mid:.2f}, avg {leg.mean:.2f})")
        self.event("TAKE_PROFIT", leg)
        leg.state, leg.target = "FLAT", 0

    def stop(self, leg, now, why):
        log(f"{why} {leg.t}: price {leg.mid:.2f}, entry {leg.entry_mid:.2f} - crossing out, "
            f"cooldown {COOLDOWN_SEC}s")
        self.event(why, leg)
        leg.state, leg.target, leg.urgent = "FLAT", 0, True
        leg.cool_until = now + COOLDOWN_SEC

    # ------------------------------------------------------------- safety
    def check_kill(self, now):
        if self.nlv is None:
            return
        if self.peak is None or self.nlv > self.peak:
            self.peak = self.nlv
        if now < self.paused_until:
            return
        if self.peak - self.nlv > KILL_DRAWDOWN:
            self.kills += 1
            self.peak = self.nlv
            if self.kills <= MAX_KILLS:
                log(f"KILL SWITCH #{self.kills}: down ${KILL_DRAWDOWN:,} from peak - "
                    f"flatten, pause {KILL_PAUSE_SEC}s")
                self.paused_until = now + KILL_PAUSE_SEC
            else:
                log("drawdown again after max kills - exits only for the rest of the run")
                self.no_more_entries = True

    # ------------------------------------------------------------- orders
    def refresh_orders(self, open_list, now):
        by_id = {o.get("order_id"): o for o in open_list}
        for leg in self.L.values():
            o = leg.order
            if o is None:
                continue
            live = by_id.get(o["id"])
            if live is not None:
                o["seen"] = True
                o["remaining"] = int(live.get("quantity", o["qty"])) - int(live.get("quantity_filled", 0))
                if o["remaining"] <= 0:
                    leg.order = None
            elif o["seen"] or now - o["posted"] > NEW_ORDER_GRACE_SEC:
                leg.order = None          # filled or cancelled

    def cancel_order(self, leg):
        if leg.order is not None:
            self.api.cancel(leg.order["id"])
            leg.order = None

    def extra_exposure(self, q, side, qty):
        """How much an order would ADD to |position| if fully filled."""
        new = q + (qty if side == "BUY" else -qty)
        return max(0, abs(new) - abs(q))

    def gross_room(self, t):
        used = sum(abs(p) for p in self.pos.values())
        for leg in self.L.values():
            if leg.t != t and leg.order is not None:
                used += self.extra_exposure(self.pos[leg.t], leg.order["side"], leg.order["remaining"])
        return max(0, GROSS_CAP - used)

    def execute(self, leg, now):
        t, q = leg.t, self.pos[leg.t]
        need = leg.target - q
        if abs(need) < MIN_TRADE or leg.bid is None or leg.ask is None:
            self.cancel_order(leg)
            return
        side = "BUY" if need > 0 else "SELL"
        increasing = abs(leg.target) > abs(q)
        aggressive = leg.urgent or (increasing and CROSS_ENTRY[t])
        if aggressive:
            price = leg.ask + CROSS_SLIP_TICKS * TICK if side == "BUY" else leg.bid - CROSS_SLIP_TICKS * TICK
        else:
            price = leg.bid if side == "BUY" else leg.ask
        price = px(price)

        qty = min(MAX_ORDER, abs(need))
        reducing_part = min(qty, abs(q)) if sgn(need) == -sgn(q) else 0
        qty = min(qty, reducing_part + self.gross_room(t))
        qty = int(qty / 100) * 100 if qty >= 100 else 0
        if qty < MIN_TRADE:
            self.cancel_order(leg)
            return

        o = leg.order
        if o is not None:
            same = o["side"] == side and abs(o["price"] - price) < 1e-9
            fresh_cross = o["aggr"] and o["side"] == side and now - o["posted"] < 1.0
            if same or fresh_cross:
                return                     # keep it working
            self.cancel_order(leg)

        res = self.api.post(t, side, qty, price)
        if res and "order_id" in res:
            leg.order = {"id": res["order_id"], "side": side, "price": price, "qty": qty,
                         "remaining": qty, "aggr": aggressive, "seen": False,
                         "posted": time.monotonic()}

    def log_position_changes(self):
        if self.prev_pos is not None:
            for t in TICKERS:
                d = self.pos[t] - self.prev_pos[t]
                if d:
                    leg = self.L[t]
                    verb = "BOUGHT" if d > 0 else "SOLD  "
                    log(f"TRADE {t:<3} {verb} {abs(d):>6}  -> position {self.pos[t]:+d}  "
                        f"(mid {leg.mid:.2f})" if leg.mid else f"TRADE {t} {d:+d}")
                    self.event("TRADE", leg, f"{d:+d}")
        self.prev_pos = dict(self.pos)

    # ------------------------------------------------------------- status
    def status(self, tpp):
        parts = []
        for t in TICKERS:
            leg = self.L[t]
            z = "warmup" if leg.z is None else f"z={leg.z:+.2f}"
            sw = "" if leg.std is None else f" swing={leg.std*100:.1f}c"
            parts.append(f"{t} {leg.state:<5} pos={self.pos[t]:+d}/{leg.target:+d} {z}{sw} "
                         f"pnl={leg.realized + leg.unrealized:+,.0f}")
        nlv = f"{self.nlv:,.0f}" if self.nlv is not None else "-"
        by = self.api.throttled_by if hasattr(self.api, "throttled_by") else {}
        thr = f"{getattr(self.api, 'throttled', 0)}" + (
            " (" + ",".join(f"{k}:{v}" for k, v in by.items()) + ")" if by else "")
        log(f"tick {self.tick}/{tpp} {self.mode} nlv={nlv} kills={self.kills} 429s={thr} | "
            + " | ".join(parts))


def main(api=None):
    bot = Bot(api or API())
    try:
        bot.run()
    except KeyboardInterrupt:
        log("stopping - cancelling all orders (positions are NOT closed)")
        bot.api.cancel_all()
    finally:
        bot.logf.close()


if __name__ == "__main__":
    main()