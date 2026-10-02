#!/usr/bin/env python3
"""
sim4_4.py  -  Conservative hybrid trader for RIT ALGO2e (CNR, RY, AC)

GOAL: consistent, positive P&L with small, capped losses (not maximum profit).

How it works (per stock, every LOOP_SEC):
  1. MEASURE   60s average + swing (std), stretch z, trend drift, 10s high/low,
               microprice, and live order-book depth.
  2. CLASSIFY  RANGE   |drift| <= 0.75
               TREND   |drift| >= 1.5, held >= 10s (UP or DOWN)
               UNCLEAR anything else -> no new trades
  3. TRADE     A) Market making (CNR, AC, RANGE mode, flat): quote bid & ask,
                  1,000 shares, inventory cap 3,000, quotes skewed to unload.
               B) Range trades (RANGE mode): bet on the snap-back, scale in
                  1/3 @ z=1.75, 2/3 @ 2.25, full @ 2.75. Take profit at the
                  average AT ENTRY. "Ladder" orders rest at the z=1.75 price
                  in advance, so spikes fill us before reacting bots arrive.
               C) Trend trades (TREND mode): buy a 1-swing dip from the 10s
                  high (sell a 1-swing bounce from the 10s low), trailing stop
                  2 swings behind the best price. Unfilled after 5s -> cancel.
  4. SWITCH    Regime turns IN FAVOUR of a position -> keep it as a trend
               trade; AGAINST -> exit now; trend FADES -> exit patiently.
  5. EXECUTE   Limit orders only. Resting at the touch for entries/quotes/calm
               exits; marketable (2 ticks through) for urgent exits. Calm exits
               escalate to marketable after 10s. RY entries cross (taker rebate).
               Orders within 1 tick of the wanted price are left alone (queue).
  6. RISK      Per trade: stop 2.5 swings, 120s time stop (range), $500 max loss;
                          sizes are set from the stop distance so a stop costs ~$500.
               Per stock: 30s cooldown after a stop; disabled after 2 stops.
               Per run:   +$3,000 -> half size; give back 40% of peak or
                          -$2,000 -> flatten, no more trades this run.
               Sizes capped per stock, 21,500 gross, and by order-book depth.
               Own-fill guard: no scale-in for 2.5s after our own fill.
  7. CLOCK     30s warm-up. Tick 260: market making off, half size.
               Tick 280: no new entries, calm exits. Tick 290: cross out.
               Startup: cross out leftover positions. Ctrl+C: cancel + flatten.
               Connection lost > 3s: cancel everything, pause entries 5s.

Run:   python sim4_4.py
Logs:  algo2e_events_<time>.csv  (every signal, regime change, position change)
       algo2e_fills_<time>.csv   (every filled order with fill price)
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
FEES = {   # per share, from the case brief (negative = reversed)
    "CNR": {"active_fee": 0.0027, "passive_rebate": 0.0023},
    "RY":  {"active_fee": -0.0014, "passive_rebate": -0.0020},
    "AC":  {"active_fee": 0.0015, "passive_rebate": 0.0011},
}
CROSS_ENTRY = {"CNR": False, "RY": True, "AC": False}   # RY: taking pays a rebate
MM_TICKERS = {"CNR", "AC"}                               # resting earns a rebate

# ============================== PACING ==============================
LOOP_SEC = 0.25
REQ_RATE = 10.0          # client-side requests/sec per endpoint group
REQ_BURST = 4
CASE_POLL_SEC = 1.0
TRADER_POLL_SEC = 1.0
FILL_POLL_SEC = 3.0
BOOK_LIMIT = 10          # one book call per loop, rotating through the stocks
NEW_ORDER_GRACE_SEC = 2.0

# ============================== SIGNAL ==============================
LOOKBACK_SEC = 60
WARMUP_SEC = 30
MIN_SAMPLES = 40
STD_FLOOR_TICKS = 2
RANGE_DRIFT = 0.75       # |drift| at or below -> RANGE
TREND_DRIFT = 1.5        # |drift| at or above (held TREND_CONFIRM_SEC) -> TREND
TREND_CONFIRM_SEC = 10
PULLBACK_WINDOW_SEC = 10

# =========================== RANGE ENGINE ===========================
ENTRY_Z = 1.75
SCALE_Z = [ENTRY_Z, 2.25, 2.75]
SCALE_CUM = [1 / 3, 2 / 3, 1.0]
EXIT_Z = 0.25            # take profit within this many swings of the ENTRY average
MIN_DEV_TICKS = 3
EDGE_MULT = 3.0          # expected move must be >= 3x (spread + COST_PAD)
COST_PAD = 0.005         # fees / slippage allowance per share
STOP_STD = 2.5
MAX_HOLD_SEC = 120
OWN_FILL_GUARD_SEC = 2.5
LADDER = True            # rest entry orders at the z=1.75 price in advance

# =========================== TREND ENGINE ===========================
TREND_PULLBACK_STD = 1.0
TRAIL_STD = 2.0
TREND_ENTRY_TIMEOUT_SEC = 5.0

# ========================== MARKET MAKING ===========================
MM_SIZE = 1000
MM_INV_CAP = 3000
MM_SKEW_AT = 1000        # beyond this inventory, back the adding quote off 1 tick

# =============================== SIZE ===============================
RANGE_MAX = {"CNR": 8000, "AC": 8000, "RY": 4000}
TREND_MAX = {"CNR": 6000, "AC": 6000, "RY": 3000}
# Sizes are set so that hitting the stop costs about MAX_TRADE_LOSS:
#   range: scaled entries sit on average ~2.0 swings from the stop
#   trend: trailing stop sits TRAIL_STD swings behind the best price
RANGE_STOP_SWINGS = 2.0
TREND_LOSS_FRAC = 0.8
GROSS_CAP = 21_500       # total exposure incl. pending orders (case limit 25,000)
MAX_ORDER = 5_000
MIN_TRADE = 100
CROSS_SLIP_TICKS = 2
DEPTH_ORDER_FRAC = 0.5   # marketable order <= 50% of depth within 2 ticks
KEEP_TICKS = 1           # leave a resting order alone if within 1 tick of target price

# ============================== EXITS ===============================
CALM_CROSS_SEC = 10      # calm exit not done after this -> cross

# =============================== RISK ===============================
MAX_TRADE_LOSS = 500
COOLDOWN_SEC = 30
MAX_STOPS_PER_STOCK = 2
PROFIT_LOCK_AT = 3_000
PROFIT_LOCK_MULT = 0.5
GIVEBACK_FRAC = 0.40
GIVEBACK_MIN_PEAK = 1_500
RUN_LOSS_LIMIT = 2_000
CONN_LOSS_SEC = 3.0
CONN_PAUSE_SEC = 5.0

# ======================= END OF RUN (ticks left) ====================
REDUCE_TL = 40           # tick 260: market making off, half size
NO_ENTRY_TL = 20         # tick 280: no new entries, calm exits
CROSS_OUT_TL = 10        # tick 290: cross out everything
CANCEL_TL = 3            # tick 297: cancel all orders
# ====================================================================


def log(msg):
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)


def px(x):
    return round(round(x / TICK) * TICK, 2)


def sgn(x):
    return (x > 0) - (x < 0)


def mode_dir(mode):
    return 1 if mode == "UP" else -1 if mode == "DOWN" else 0


def lots(x):
    return int(x / 100) * 100


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

    def case(self):              return self._call("GET", "/case", "case")
    def trader(self):            return self._call("GET", "/trader", "trader")
    def securities(self):        return self._call("GET", "/securities", "securities")
    def book(self, t):           return self._call("GET", "/securities/book", "book", ticker=t, limit=BOOK_LIMIT)
    def open_orders(self):       return self._call("GET", "/orders", "orders_get", status="OPEN")
    def orders_with_status(self, s): return self._call("GET", "/orders", "orders_get", status=s)
    def cancel(self, oid):       return self._call("DELETE", f"/orders/{oid}", "orders")
    def cancel_all(self):        return self._call("POST", "/commands/cancel", "cancel", all=1)

    def post(self, t, action, qty, price):
        return self._call("POST", "/orders", "orders", ticker=t, type="LIMIT",
                          quantity=int(qty), action=action, price=price)


class Leg:
    """Everything the bot knows about one stock."""

    def __init__(self, t):
        self.t = t
        self.hist = deque()                     # (time, mid)
        self.bid = self.ask = self.mid = self.micro = None
        self.mean = self.std = self.z = self.drift = None
        self.hi10 = self.lo10 = None
        self.mode = "WARMUP"                    # WARMUP / RANGE / UP / DOWN / UNCLEAR
        self.trend_dir, self.trend_since = 0, None
        self.depth = {"BUY": None, "SELL": None}    # shares we could trade into: (2 ticks, 3 ticks)
        self.pnl = 0.0                          # realized + unrealized for this stock
        # trade state
        self.kind = None                        # None / "range" / "trend" / "exit"
        self.dir = 0
        self.target = None                      # None = no directional order wanted
        self.exit_mode, self.exit_t = None, 0.0
        self.entry_mid = self.entry_std = self.entry_mean = self.entry_time = None
        self.pnl_entry = 0.0
        self.full_size, self.tier, self.best = 0, -1, None
        # per-stock risk
        self.cool_until = 0.0
        self.stops = 0
        self.disabled = False
        self.last_fill = -1e9

    def update_stats(self, now):
        while self.hist and now - self.hist[0][0] > LOOKBACK_SEC:
            self.hist.popleft()
        if (self.mid is None or len(self.hist) < MIN_SAMPLES
                or now - self.hist[0][0] < WARMUP_SEC):
            self.z, self.mode = None, "WARMUP"
            return
        mids = [m for _, m in self.hist]
        self.mean = statistics.fmean(mids)
        self.std = max(statistics.pstdev(mids), STD_FLOOR_TICKS * TICK)
        sig = self.micro if self.micro is not None else self.mid
        self.z = (sig - self.mean) / self.std
        third = max(1, len(mids) // 3)
        self.drift = (statistics.fmean(mids[-third:]) - statistics.fmean(mids[:third])) / self.std
        recent = [m for ts, m in self.hist if now - ts <= PULLBACK_WINDOW_SEC]
        self.hi10, self.lo10 = max(recent), min(recent)

        # regime with hysteresis and confirmation
        ad, d = abs(self.drift), sgn(self.drift)
        if ad <= RANGE_DRIFT:
            self.mode, self.trend_since = "RANGE", None
        elif ad >= TREND_DRIFT:
            if self.trend_dir != d or self.trend_since is None:
                self.trend_dir, self.trend_since = d, now
            if now - self.trend_since >= TREND_CONFIRM_SEC:
                self.mode = "UP" if d > 0 else "DOWN"
            elif mode_dir(self.mode) != d:
                self.mode = "UNCLEAR"
        else:
            if mode_dir(self.mode) != d or d == 0:
                self.mode = "UNCLEAR"


class Bot:
    def __init__(self, api):
        self.api = api
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        self.logf = open(f"algo2e_events_{stamp}.csv", "w", newline="")
        self.w = csv.writer(self.logf)
        self.w.writerow(["wall_time", "tick", "event", "ticker", "detail", "mode", "kind",
                         "position", "target", "mid", "mean", "std", "z", "drift", "run_pnl"])
        self.fillf = open(f"algo2e_fills_{stamp}.csv", "w", newline="")
        self.fw = csv.writer(self.fillf)
        self.fw.writerow(["wall_time", "tick", "order_id", "ticker", "action", "type",
                          "limit_price", "quantity", "quantity_filled", "fill_vwap", "status"])
        self.fills_seen = set()
        self.my_id = None
        self.reset_run()

    def reset_run(self):
        self.L = {t: Leg(t) for t in TICKERS}
        self.pos = {t: 0 for t in TICKERS}
        self.prev_pos = None
        self.orders = {}                # order_id -> dict
        self.nlv = self.nlv0 = None
        self.run_pnl, self.peak = 0.0, 0.0
        self.halted, self.halt_urg = False, "calm"
        self.size_mult = 1.0
        self.pause_until = 0.0
        self.conn_lost, self.last_ok = False, time.monotonic()
        self.tick = -1
        self.phase = "WAIT"
        self.started = False
        self.cancelled_final = False
        self._case, self._case_t = None, 0.0
        self.last_trader = self.last_fill_poll = self.last_status = 0.0
        self.fill_status_i = 0
        self.book_i = 0

    # ------------------------------------------------------------ logging
    def event(self, ev, leg, detail=""):
        f = lambda v, n=4: "" if v is None else f"{v:.{n}f}"
        self.w.writerow([datetime.now().strftime("%H:%M:%S.%f")[:-3], self.tick, ev, leg.t, detail,
                         leg.mode, leg.kind or "", self.pos[leg.t],
                         "" if leg.target is None else leg.target, f(leg.mid), f(leg.mean),
                         f(leg.std), f(leg.z, 2), f(leg.drift, 2), f"{self.run_pnl:.0f}"])
        self.logf.flush()

    # --------------------------------------------------------------- loop
    def run(self):
        tr = self.api.trader()
        if not tr:
            raise RuntimeError("Could not reach /trader - check URL/login")
        self.my_id = tr.get("trader_id")
        log(f"connected as trader_id={self.my_id}  (sim4_4 conservative hybrid)")
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
            self._case, self._case_t = self.api.case(), now
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
        tl = tpp - tick

        # ---- market data (one call for all stocks) + connection watchdog
        secs = self.api.securities()
        if not secs:
            if not self.conn_lost and now - self.last_ok > CONN_LOSS_SEC:
                log("CONNECTION LOST - will cancel everything and pause when it returns")
                self.conn_lost = True
            return
        if self.conn_lost:
            log(f"connection back - cancelling all orders, pausing entries {CONN_PAUSE_SEC:.0f}s")
            self.api.cancel_all()
            self.orders.clear()
            self.pause_until = now + CONN_PAUSE_SEC
            self.conn_lost = False
        self.last_ok = now

        for s in secs:
            t = s.get("ticker")
            if t not in self.L:
                continue
            leg = self.L[t]
            self.pos[t] = int(s.get("position", 0) or 0)
            leg.pnl = (s.get("realized", 0) or 0) + (s.get("unrealized", 0) or 0)
            bid, ask = s.get("bid"), s.get("ask")
            if bid and ask and ask > bid:
                leg.bid, leg.ask = bid, ask
                leg.mid = (bid + ask) / 2
                bs, as_ = s.get("bid_size") or 0, s.get("ask_size") or 0
                leg.micro = (bid * as_ + ask * bs) / (bs + as_) if bs + as_ > 0 else leg.mid
                leg.hist.append((now, leg.mid))

        if now - self.last_trader >= TRADER_POLL_SEC:
            tr = self.api.trader()
            if tr and tr.get("nlv") is not None:
                self.nlv = tr.get("nlv")
                if self.nlv0 is None:
                    self.nlv0 = self.nlv
            self.last_trader = now

        self.poll_book()
        oo = self.api.open_orders()
        if oo is not None:
            self.refresh_orders(oo, now)
        if now - self.last_fill_poll >= FILL_POLL_SEC:
            self.poll_fills()
            self.last_fill_poll = now
        self.log_position_changes(now)

        if not self.started:
            self.started = True
            held = {t: q for t, q in self.pos.items() if q}
            if held:
                log(f"positions already on the account at start: {held} -> crossing out first")
                for t in held:
                    self.begin_exit(self.L[t], now, "urgent", "START-FLATTEN")

        for leg in self.L.values():
            old = leg.mode
            leg.update_stats(now)
            if leg.mode != old:
                self.event("MODE", leg, f"{old}->{leg.mode}")

        self.check_run_risk()

        # ---- phase of the run
        if tl <= CANCEL_TL:
            self.phase = "DONE"
            if not self.cancelled_final:
                self.api.cancel_all()
                self.orders.clear()
                self.cancelled_final = True
            return
        entries_ok, mm_ok, mult = False, False, self.size_mult
        if tl <= CROSS_OUT_TL:
            self.phase = "CROSS-OUT"
            for leg in self.L.values():
                self.begin_exit(leg, now, "urgent", "END-CROSS-OUT")
        elif tl <= NO_ENTRY_TL:
            self.phase = "CLOSING"
            for leg in self.L.values():
                self.begin_exit(leg, now, "calm", "END-CLOSE")
        elif self.halted:
            self.phase = "HALTED"
            for leg in self.L.values():
                self.begin_exit(leg, now, self.halt_urg, "RUN-HALT")
        else:
            entries_ok = now >= self.pause_until
            if tl <= REDUCE_TL:
                self.phase, mult = "REDUCE", mult * 0.5
            else:
                self.phase, mm_ok = "TRADING", entries_ok
        for leg in self.L.values():
            self.decide(leg, now, entries_ok, mm_ok, mult)

        self.reconcile(now, entries_ok, mm_ok, mult)

        if now - self.last_status >= 2.0:
            self.status(tpp)
            self.last_status = now

    # ------------------------------------------------------------ run risk
    def check_run_risk(self):
        if self.nlv is None or self.nlv0 is None:
            return
        self.run_pnl = self.nlv - self.nlv0
        self.peak = max(self.peak, self.run_pnl)
        if not self.halted:
            if self.run_pnl <= -RUN_LOSS_LIMIT:
                self.halted, self.halt_urg = True, "urgent"
                log(f"RUN LOSS LIMIT: P&L {self.run_pnl:+,.0f} - flattening, no more trades this run")
            elif self.peak >= GIVEBACK_MIN_PEAK and self.run_pnl <= self.peak * (1 - GIVEBACK_FRAC):
                self.halted, self.halt_urg = True, "calm"
                log(f"PROFIT PROTECT: P&L {self.run_pnl:+,.0f} vs peak {self.peak:+,.0f} - "
                    f"locking in, no more trades this run")
        new_mult = PROFIT_LOCK_MULT if self.peak >= PROFIT_LOCK_AT else 1.0
        if new_mult != self.size_mult:
            log(f"PROFIT LOCK: peak P&L {self.peak:+,.0f} - sizes x{new_mult}")
            self.size_mult = new_mult

    # ------------------------------------------------------------ decisions
    def decide(self, leg, now, entries_ok, mm_ok, mult):
        q = self.pos[leg.t]
        if leg.kind == "exit":
            if q == 0:
                self.clear(leg)
            return
        if leg.z is None:
            if leg.kind is None and q != 0:
                self.begin_exit(leg, now, "calm", "NO-SIGNAL")
            return
        md = mode_dir(leg.mode)
        if leg.kind == "range":
            self.manage_range(leg, now, q, md, entries_ok)
        elif leg.kind == "trend":
            self.manage_trend(leg, now, q, md)
        if leg.kind is None:
            if q != 0:
                self.adopt_or_exit(leg, now, q, md, mm_ok)
            elif entries_ok and not leg.disabled and now >= leg.cool_until:
                self.try_entry(leg, now, md, mult)

    def manage_range(self, leg, now, q, md, entries_ok):
        d = leg.dir
        trade_pnl = leg.pnl - leg.pnl_entry
        if trade_pnl <= -MAX_TRADE_LOSS:
            self.stop(leg, now, "MAX-LOSS")
        elif d * (leg.entry_mid - leg.mid) >= STOP_STD * leg.entry_std:
            self.stop(leg, now, "STOP-LOSS")
        elif md == -d:
            self.stop(leg, now, "TREND-AGAINST")
        elif md == d:
            if q == 0:
                self.clear(leg)
                return
            leg.kind, leg.target, leg.best = "trend", q, leg.mid
            leg.entry_time = now
            log(f"CONVERT {leg.t}: trend turned in our favour - holding {q:+d} with trailing stop")
            self.event("CONVERT", leg)
        elif now - leg.entry_time > MAX_HOLD_SEC:
            self.begin_exit(leg, now, "urgent", "TIME-STOP")
        elif d * (leg.mid - leg.entry_mean) >= -EXIT_Z * leg.entry_std:
            self.begin_exit(leg, now, "calm", "TAKE-PROFIT")
        elif entries_ok and leg.mode == "RANGE" and now - leg.last_fill >= OWN_FILL_GUARD_SEC:
            added = False
            while leg.tier < len(SCALE_Z) - 1 and -d * leg.z >= SCALE_Z[leg.tier + 1]:
                leg.tier += 1
                added = True
            if added:
                leg.target = d * lots(leg.full_size * SCALE_CUM[leg.tier])
                log(f"SCALE-IN {leg.t} -> target {leg.target:+d} (level {leg.tier + 1}/3, z={leg.z:+.2f})")
                self.event("SCALE_IN", leg, f"L{leg.tier + 1}")

    def manage_trend(self, leg, now, q, md):
        d = leg.dir
        leg.best = max(leg.best, leg.mid) if d > 0 else min(leg.best, leg.mid)
        trade_pnl = leg.pnl - leg.pnl_entry
        if trade_pnl <= -MAX_TRADE_LOSS:
            self.stop(leg, now, "MAX-LOSS")
        elif d * (leg.best - leg.mid) >= TRAIL_STD * leg.entry_std:
            if trade_pnl < 0:
                self.stop(leg, now, "TRAIL-STOP")
            else:
                self.begin_exit(leg, now, "urgent", "TRAIL-STOP (profit)")
                leg.cool_until = now + 10
        elif md == -d:
            self.stop(leg, now, "TREND-REVERSED")
        elif md != d:
            self.begin_exit(leg, now, "calm", "TREND-FADED")
        elif abs(leg.target) > abs(q) and now - leg.entry_time > TREND_ENTRY_TIMEOUT_SEC:
            if abs(q) < MIN_TRADE:
                log(f"TREND ENTRY TIMEOUT {leg.t}: not filled - waiting for next pullback")
                self.event("ENTRY_TIMEOUT", leg)
                if q == 0:
                    self.clear(leg)
                else:
                    self.begin_exit(leg, now, "calm", "ENTRY-TIMEOUT")
            else:
                leg.target = q

    def adopt_or_exit(self, leg, now, q, md, mm_ok):
        """Flat state but holding shares (market-making inventory or a ladder fill)."""
        d = sgn(q)
        mm_inv_ok = mm_ok and leg.t in MM_TICKERS and leg.mode == "RANGE" and abs(q) <= MM_INV_CAP
        stretched = -d * leg.z >= ENTRY_Z * 0.8
        if md == -d:
            self.begin_exit(leg, now, "urgent", "INVENTORY-AGAINST-TREND")
        elif md == d:
            self.start_trade(leg, now, "trend", d, q, abs(q))
            log(f"ADOPT {leg.t}: {q:+d} in an {leg.mode} trend - holding with trailing stop")
            self.event("ADOPT_TREND", leg)
        elif stretched and leg.mode == "RANGE":
            full = max(abs(q), self.range_full(leg, 1.0))
            self.start_trade(leg, now, "range", d, q, full)
            leg.tier = max([i for i, c in enumerate(SCALE_CUM) if lots(full * c) <= abs(q)] or [0])
            log(f"ADOPT {leg.t}: ladder/quote fill {q:+d} at z={leg.z:+.2f} -> range trade")
            self.event("ADOPT_RANGE", leg)
        elif mm_inv_ok:
            pass                                   # normal market-making inventory
        else:
            self.begin_exit(leg, now, "calm", "UNMANAGED-INVENTORY")

    def try_entry(self, leg, now, md, mult):
        spread = leg.ask - leg.bid
        sig = leg.micro if leg.micro is not None else leg.mid
        if leg.mode == "RANGE":
            dev = sig - leg.mean
            far = abs(dev) >= MIN_DEV_TICKS * TICK - 1e-9
            edge = abs(dev) >= EDGE_MULT * (spread + COST_PAD)
            if abs(leg.z) >= ENTRY_Z and far and edge:
                d = -sgn(leg.z)
                full = self.range_full(leg, mult, d)
                if lots(full * SCALE_CUM[0]) < MIN_TRADE:
                    return
                self.start_trade(leg, now, "range", d, 0, full)
                leg.tier = 0
                while leg.tier < len(SCALE_Z) - 1 and -d * leg.z >= SCALE_Z[leg.tier + 1]:
                    leg.tier += 1
                leg.target = d * lots(full * SCALE_CUM[leg.tier])
                log(f"ENTER RANGE {leg.t} {'LONG' if d > 0 else 'SHORT'} target {leg.target:+d} "
                    f"(level {leg.tier + 1}/3, z={leg.z:+.2f}, swing {leg.std*100:.1f}c)")
                self.event("ENTER_RANGE", leg, f"L{leg.tier + 1}")
        elif md != 0 and leg.hi10 is not None:
            pulled = (leg.mid <= leg.hi10 - TREND_PULLBACK_STD * leg.std) if md > 0 else \
                     (leg.mid >= leg.lo10 + TREND_PULLBACK_STD * leg.std)
            if pulled:
                risk_size = lots(MAX_TRADE_LOSS * TREND_LOSS_FRAC / (TRAIL_STD * leg.std))
                size = self.depth_cap(leg, md, lots(min(TREND_MAX[leg.t], risk_size) * mult))
                if size < MIN_TRADE:
                    return
                self.start_trade(leg, now, "trend", md, 0, size)
                leg.target = md * size
                log(f"ENTER TREND {leg.t} {'LONG' if md > 0 else 'SHORT'} target {leg.target:+d} "
                    f"(pullback in {leg.mode} trend, drift {leg.drift:+.2f})")
                self.event("ENTER_TREND", leg)

    def range_full(self, leg, mult, d=None):
        full = min(RANGE_MAX[leg.t], lots(MAX_TRADE_LOSS / (RANGE_STOP_SWINGS * leg.std)))
        full = lots(full * mult)
        return self.depth_cap(leg, d, full) if d else full

    def depth_cap(self, leg, d, size):
        """Position must be exitable within ~3 ticks of the book on the exit side."""
        dep = leg.depth["SELL" if d > 0 else "BUY"]
        if dep is None:
            return size
        return min(size, max(MIN_TRADE, lots(dep[1])))

    def start_trade(self, leg, now, kind, d, target, full):
        leg.kind, leg.dir, leg.target, leg.full_size = kind, d, target, full
        leg.entry_mid, leg.entry_std, leg.entry_mean = leg.mid, leg.std, leg.mean
        leg.entry_time, leg.pnl_entry, leg.best = now, leg.pnl, leg.mid
        leg.exit_mode = None
        leg.tier = 0

    def stop(self, leg, now, why):
        leg.stops += 1
        leg.cool_until = now + COOLDOWN_SEC
        self.begin_exit(leg, now, "urgent", why)
        if leg.stops >= MAX_STOPS_PER_STOCK and not leg.disabled:
            leg.disabled = True
            log(f"{leg.t}: {leg.stops} stops - no more new trades in {leg.t} this run")
            self.event("DISABLED", leg)

    def begin_exit(self, leg, now, urgency, why):
        if leg.kind is None and self.pos[leg.t] == 0:
            return
        if leg.kind == "exit" and (leg.exit_mode == "urgent" or leg.exit_mode == urgency):
            return
        if leg.kind != "exit":
            leg.exit_t = now
            log(f"EXIT {leg.t} [{why}] {urgency}: position {self.pos[leg.t]:+d}, "
                f"mid {leg.mid if leg.mid else 0:.2f}")
        else:
            log(f"EXIT {leg.t} [{why}] now urgent")
        self.event("EXIT", leg, f"{why}/{urgency}")
        leg.kind, leg.target, leg.exit_mode = "exit", 0, urgency

    def clear(self, leg):
        leg.kind, leg.dir, leg.target = None, 0, None
        leg.exit_mode, leg.tier, leg.best = None, -1, None

    # ------------------------------------------------------------- orders
    def desired(self, leg, now, entries_ok, mm_ok, mult):
        """The orders this stock should have working right now: tag -> spec."""
        D = {}
        if leg.bid is None or leg.ask is None:
            return D
        t, q = leg.t, self.pos[leg.t]

        if leg.target is not None and leg.target != q:
            need = leg.target - q
            side = "BUY" if need > 0 else "SELL"
            reducing = abs(leg.target) < abs(q) and sgn(leg.target) in (0, sgn(q))
            if reducing:
                qty = min(MAX_ORDER, abs(need))
                aggr = leg.kind == "exit" and (
                    leg.exit_mode == "urgent" or now - leg.exit_t >= CALM_CROSS_SEC)
            else:
                qty = lots(min(MAX_ORDER, abs(need)))
                aggr = CROSS_ENTRY[t]
            if qty >= (1 if reducing else MIN_TRADE):
                if aggr:
                    price = leg.ask + CROSS_SLIP_TICKS * TICK if side == "BUY" \
                        else leg.bid - CROSS_SLIP_TICKS * TICK
                    dep = leg.depth[side]
                    if dep is not None:
                        qty = min(qty, max(min(qty, MIN_TRADE), int(dep[0] * DEPTH_ORDER_FRAC)))
                else:
                    price = leg.bid if side == "BUY" else leg.ask
                D["dir"] = dict(side=side, price=px(price), qty=qty, aggr=aggr, reducing=reducing)

        flat_ok = (leg.kind is None and leg.z is not None and not leg.disabled
                   and now >= leg.cool_until and leg.mode == "RANGE")

        # A) market making quotes
        if flat_ok and mm_ok and t in MM_TICKERS:
            size = lots(MM_SIZE * mult)
            if size >= MIN_TRADE:
                bpx = leg.bid - (TICK if q > MM_SKEW_AT else 0)
                apx = leg.ask + (TICK if q < -MM_SKEW_AT else 0)
                if q + size <= MM_INV_CAP:
                    D["mm_bid"] = dict(side="BUY", price=px(bpx), qty=size, aggr=False, reducing=False)
                if q - size >= -MM_INV_CAP:
                    D["mm_ask"] = dict(side="SELL", price=px(apx), qty=size, aggr=False, reducing=False)

        # B) ladder: rest the first range-entry level in advance
        if LADDER and flat_ok and entries_ok:
            dev = ENTRY_Z * leg.std
            spread = leg.ask - leg.bid
            if dev >= EDGE_MULT * (spread + COST_PAD) and dev >= MIN_DEV_TICKS * TICK:
                for d, tag in ((1, "lad_bid"), (-1, "lad_ask")):
                    size = lots(self.range_full(leg, mult, d) * SCALE_CUM[0])
                    lp = px(leg.mean - d * dev)
                    beyond = lp <= leg.bid - TICK if d > 0 else lp >= leg.ask + TICK
                    if size >= MIN_TRADE and beyond:
                        D[tag] = dict(side="BUY" if d > 0 else "SELL", price=lp, qty=size,
                                      aggr=False, reducing=False)
        return D

    def keep(self, o, spec, now):
        if o["side"] != spec["side"]:
            return False
        if spec["aggr"]:
            return o["aggr"] and now - o["posted"] < 0.75
        if o["aggr"]:
            return False
        if abs(o["price"] - spec["price"]) > KEEP_TICKS * TICK + 1e-9:
            return False
        if spec["qty"] > o["remaining"] + 500:          # target grew a lot -> resize
            return False
        return True

    def pending(self, t):
        b = s = 0
        for o in self.orders.values():
            if o["t"] == t:
                if o["side"] == "BUY":
                    b += o["remaining"]
                else:
                    s += o["remaining"]
        return b, s

    def gross_ok(self, t, side, qty):
        total = 0
        for u in TICKERS:
            b, s = self.pending(u)
            if u == t:
                b, s = (b + qty, s) if side == "BUY" else (b, s + qty)
            q = self.pos[u]
            total += max(abs(q + b), abs(q - s))
        return total <= GROSS_CAP

    def reconcile(self, now, entries_ok, mm_ok, mult):
        want = {t: self.desired(self.L[t], now, entries_ok, mm_ok, mult) for t in TICKERS}
        have = set()
        for oid, o in list(self.orders.items()):
            spec = want[o["t"]].get(o["tag"])
            if spec is not None and self.keep(o, spec, now):
                have.add((o["t"], o["tag"]))
            else:
                self.api.cancel(oid)
                self.orders.pop(oid, None)

        todo = []
        for t, D in want.items():
            for tag, spec in D.items():
                if (t, tag) in have:
                    continue
                prio = 0 if (tag == "dir" and spec["reducing"]) else 1 if tag == "dir" \
                    else 2 if tag.startswith("lad") else 3
                todo.append((prio, -abs(self.L[t].z or 0), t, tag, spec))
        todo.sort(key=lambda x: (x[0], x[1]))
        for _, _, t, tag, spec in todo:
            qty = spec["qty"]
            if not spec["reducing"]:
                while qty >= MIN_TRADE and not self.gross_ok(t, spec["side"], qty):
                    qty -= 100
                if qty < MIN_TRADE:
                    continue
            res = self.api.post(t, spec["side"], qty, spec["price"])
            if res and "order_id" in res:
                self.orders[res["order_id"]] = dict(
                    t=t, tag=tag, side=spec["side"], price=spec["price"], qty=qty,
                    remaining=qty, aggr=spec["aggr"], seen=False, posted=time.monotonic())

    def refresh_orders(self, open_list, now):
        live = {o.get("order_id"): o for o in open_list}
        for oid, o in list(self.orders.items()):
            L = live.get(oid)
            if L is not None:
                o["seen"] = True
                o["remaining"] = int(L.get("quantity", o["qty"])) - int(L.get("quantity_filled", 0))
                if o["remaining"] <= 0:
                    del self.orders[oid]
            elif o["seen"] or now - o["posted"] > NEW_ORDER_GRACE_SEC:
                del self.orders[oid]          # filled or cancelled

    def poll_book(self):
        t = TICKERS[self.book_i % len(TICKERS)]
        self.book_i += 1
        bk = self.api.book(t)
        if not bk:
            return
        leg = self.L[t]

        def depth(levels, buy_side_of_book):
            rows = [(lv.get("price"), (lv.get("quantity") or 0) - (lv.get("quantity_filled") or 0))
                    for lv in levels if lv.get("trader_id") != self.my_id]
            rows = [(p, r) for p, r in rows if p is not None and r > 0]
            if not rows:
                return (0, 0)
            best = max(p for p, _ in rows) if buy_side_of_book else min(p for p, _ in rows)
            within = lambda k: sum(r for p, r in rows if abs(p - best) <= k * TICK + 1e-9)
            return (within(2), within(3))

        leg.depth["BUY"] = depth(bk.get("asks", []), False)    # what we can buy into
        leg.depth["SELL"] = depth(bk.get("bids", []), True)    # what we can sell into

    def poll_fills(self):
        status = ("TRANSACTED", "CANCELLED")[self.fill_status_i % 2]
        self.fill_status_i += 1
        rows = self.api.orders_with_status(status)
        if not rows:
            return
        for o in rows:
            oid = o.get("order_id")
            filled = o.get("quantity_filled") or 0
            if oid in self.fills_seen or filled <= 0:
                continue
            self.fills_seen.add(oid)
            self.fw.writerow([datetime.now().strftime("%H:%M:%S.%f")[:-3], o.get("tick", self.tick),
                              oid, o.get("ticker"), o.get("action"), o.get("type"), o.get("price"),
                              o.get("quantity"), filled, o.get("vwap"), o.get("status")])
        self.fillf.flush()

    def log_position_changes(self, now):
        if self.prev_pos is not None:
            for t in TICKERS:
                d = self.pos[t] - self.prev_pos[t]
                if d:
                    leg = self.L[t]
                    leg.last_fill = now
                    self.event("TRADE", leg, f"{d:+d}")
        self.prev_pos = dict(self.pos)

    # ------------------------------------------------------------- shutdown
    def emergency_flatten(self, max_sec=15):
        log("flattening all positions...")
        self.api.cancel_all()
        t_end = time.monotonic() + max_sec
        while time.monotonic() < t_end:
            secs = self.api.securities()
            if not secs:
                time.sleep(0.3)
                continue
            self.api.cancel_all()          # clear any partly filled leftovers before reposting
            left = {}
            for s in secs:
                t = s.get("ticker")
                q = int(s.get("position", 0) or 0)
                if t in TICKERS and q and s.get("bid") and s.get("ask"):
                    left[t] = q
                    side = "SELL" if q > 0 else "BUY"
                    price = px(s["bid"] - 3 * TICK) if q > 0 else px(s["ask"] + 3 * TICK)
                    self.api.post(t, side, min(MAX_ORDER, abs(q)), price)
            if not left:
                log("flat.")
                return
            time.sleep(0.5)
        self.api.cancel_all()
        log("WARNING: could not fully flatten in time - check positions in RIT")

    # ------------------------------------------------------------- status
    def status(self, tpp):
        parts = []
        for t in TICKERS:
            leg = self.L[t]
            z = "  --" if leg.z is None else f"{leg.z:+.1f}"
            kind = leg.kind or ("off" if leg.disabled else "-")
            parts.append(f"{t} {leg.mode:<7} {kind:<5} pos={self.pos[t]:+d} z={z}")
        thr = getattr(self.api, "throttled", 0)
        log(f"tick {self.tick}/{tpp} {self.phase} P&L={self.run_pnl:+,.0f} (peak {self.peak:+,.0f}) "
            f"x{self.size_mult} orders={len(self.orders)} 429s={thr} | " + " | ".join(parts))


def main(api=None):
    bot = Bot(api or API())
    try:
        bot.run()
    except KeyboardInterrupt:
        log("Ctrl+C - cancelling orders and flattening (press Ctrl+C again to abort)")
        try:
            bot.emergency_flatten()
            bot.fill_status_i = 0
            bot.poll_fills()
            bot.poll_fills()
        except KeyboardInterrupt:
            log("aborted - positions may still be open")
    finally:
        bot.logf.close()
        bot.fillf.close()


if __name__ == "__main__":
    main()
