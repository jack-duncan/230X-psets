"""
RIT ALGO2e market maker: independent Avellaneda-Stoikov quotes on CNR, RY and AC,
a shared allocation layer, a markout / trend guard, and a manual flatten-first stop.

Run with the ALGO2e case loaded in the RIT client (from this folder):
    RIT_API_KEY=<key> uv run --with requests python mm_algo2e.py
    ... mm_algo2e.py --calibrate     # random quote distances, logs fills for fitting k and A
Unit tests:
    uv run --with requests python -m unittest test_mm_algo2e -v

Stop: Ctrl-C in this terminal (or create a file named STOP next to this script), wait for
"flat", THEN stop the case in the RIT client -- once the case is stopped the API cannot trade.

Prices are integer cents for orders and the book; the AS model runs in dollars.
Time is the case clock (ticks) from the API, never wall time, except short execution timers.
Every PLACEHOLDER value below must be set from calibration (section 9 of the design).
"""
import csv
import math
import os
import random
import sys
import time
import traceback
from collections import defaultdict, deque

import requests

HERE = os.path.dirname(os.path.abspath(__file__))
try:
    from dotenv import load_dotenv
    load_dotenv(os.path.join(HERE, "..", ".env"))
except ImportError:
    pass

# ================================ CONFIG ================================
BASE_URL           = os.environ.get("RIT_BASE_URL", "http://localhost:10006/v1")
HTTP_TIMEOUT       = 1.0
RATE_LIMIT_RETRIES = 3
BOOK_LIMIT         = 100
ORDER_RATE         = 5          # order commands/s; overridden by the API's api_orders_per_second
LOOP_SLEEP         = 0.15       # wall seconds
STOP_FILE          = os.path.join(HERE, "STOP")
LOG_EVENTS_PATH    = os.path.join(HERE, "log_events.csv")
LOG_STATE_PATH     = os.path.join(HERE, "log_state.csv")
LOG_FILLS_PATH     = os.path.join(HERE, "log_fills.csv")

TICKERS = ("CNR", "RY", "AC")
TICK_C  = 1                     # tick size, cents
CASE_LENGTH = 300               # ticks; overridden by the API

# Per-asset model parameters. PLACEHOLDERS -- fit from calibration.
#   A: fill rate at zero distance, k: decay per dollar of distance, rho: passive rebate $/share
#   (negative = fee, widens quotes), sigma0: initial per-tick mid volatility, $.
PARAMS = {
    "CNR": {"A": 1.0, "k": 100.0, "rho": 0.002,  "sigma0": 0.010},
    "RY":  {"A": 1.0, "k": 100.0, "rho": -0.002, "sigma0": 0.010},
    "AC":  {"A": 1.0, "k": 100.0, "rho": 0.002,  "sigma0": 0.010},
}
GAMMA        = 2e-5             # PLACEHOLDER, one value for all assets (per-share risk aversion)

# Drift / volatility (windows in case ticks)
TAU_EFF_MAX  = 10               # drift skew horizon
DRIFT_WINDOW = 15
DRIFT_SHRINK = 0.5
MU_MAX       = 0.003            # PLACEHOLDER, $/tick cap on shrunk drift
SIGMA_WINDOW = 30
SIGMA_MIN    = 0.001
MICRO_SIZE_CAP = 20000

# Gating on net edge per fill, dollars. PLACEHOLDERS -- set from realized P&L per fill after markout.
THETA_ON  = 0.010
THETA_OFF = 0.006

# Allocation
B_TOTAL       = 30000           # PLACEHOLDER total gross inventory budget
CAP_FLOOR     = 2000
CAP_CEIL      = 20000           # must stay <= HARD_POS
SOFT_CAP      = 15000
SCORE_WINDOW  = 15              # ticks

# Sizing
CLIP      = 1500                # PLACEHOLDER base clip
TIER1     = 0.5                 # |q|/cap at which the adding side drops to 50%
TIER2     = 1.0                 # ... and to 0%
MAX_ORDER = 5000
MIN_TOPUP = 400
HARD_POS  = 25000

# Markout guard
MARKOUT_TICKS     = 5
MARKOUT_WINDOW    = 30          # ticks of resolved markouts that count
MARKOUT_MIN_FILLS = 3
MARKOUT_TH        = 0.003       # PLACEHOLDER $/share: avg below -TH widens the side, below -2TH also halves it
# Aggregate trend guard
Z_TH               = 0.5        # PLACEHOLDER drift z-score |mu/sigma|
AGG_MARKOUT_TH     = 0.002      # PLACEHOLDER
AGG_MIN_FILLS      = 5
TREND_RELEASE_TICKS = 5

# Endgame: inventory is marked at the terminal price, so no forced flatten.
TAPER_ENABLED = False           # enable only if the terminal mark turns out to be a touch/last-trade haircut
TAPER_TICKS   = 10

# Data freshness
STALE_BOOK_S = 1.0

# Calibration mode
CAL_DELTA_TICKS  = (1, 2, 3, 4, 5)
CAL_REDRAW_TICKS = 5

# Flatten routine
FLAT_PASSIVE_S = 3.0
# ========================================================================

BUY, SELL = "BUY", "SELL"
NOT_FOUND = object()
_EPS = 1e-9
_events = None
_fills_log = None


def log_event(kind, detail):
    if _events is not None:
        _events.log(kind, detail)


def to_cents(price):
    return int(round(float(price) * 100))


def fmt_px(cents):
    return "" if cents is None else f"{cents / 100:.2f}"


def clamp(x, lo, hi):
    return max(lo, min(hi, x))


# ============================ PURE LOGIC ================================

def compute_fv(best_bid, best_ask, bid_size, ask_size, cap=MICRO_SIZE_CAP):
    """Microprice in cents; plain mid when either top size is missing."""
    if not bid_size or not ask_size or bid_size <= 0 or ask_size <= 0:
        return (best_bid + best_ask) / 2
    qb, qa = min(bid_size, cap), min(ask_size, cap)
    return (best_bid * qa + best_ask * qb) / (qa + qb)


def as_quote(s, pos, tau, sigma, mu, gamma, k, rho):
    """AS reservation price r, optimal spread, and fee-adjusted half distance (all dollars)."""
    tau = max(tau, 0.0)
    var_tau = gamma * sigma * sigma * tau
    r = s + mu * min(tau, TAU_EFF_MAX) - pos * var_tau
    spread = var_tau + (2.0 / gamma) * math.log1p(gamma / k)
    half = max(0.0, spread / 2 - rho)
    return r, spread, half


def net_edge(spread, rho):
    return spread / 2 + rho


def update_gate(on, edge):
    """Start quoting at edge >= THETA_ON, stop only below THETA_OFF."""
    return edge >= THETA_OFF if on else edge >= THETA_ON


def quote_prices(r, half, best_bid, best_ask, bid_extra=0, ask_extra=0):
    """Cents: round outward, stay within 1 tick of the touch, never cross, then widen by extras."""
    bid = math.floor((r - half) * 100 + _EPS)
    ask = math.ceil((r + half) * 100 - _EPS)
    bid = min(bid, best_bid + TICK_C)
    ask = max(ask, best_ask - TICK_C)
    bid = min(bid, best_ask - TICK_C)
    ask = max(ask, best_bid + TICK_C)
    bid -= bid_extra * TICK_C
    ask += ask_extra * TICK_C
    if ask < bid + TICK_C:
        ask = bid + TICK_C
    return bid, ask


def tier_factor(abs_pos, cap):
    if cap <= 0:
        return 0.0
    f = abs_pos / cap
    return 1.0 if f < TIER1 else 0.5 if f < TIER2 else 0.0


def adding_side(pos):
    return BUY if pos > 0 else SELL if pos < 0 else None


def compute_sizes(pos, cap, clip=CLIP, guard_level=0, guard_dir=None, mk_level=None, taper=False):
    """Target resting qty per side. mk_level: {side: 0|1|2} from the markout guard."""
    mk_level = mk_level or {}
    add = adding_side(pos)
    facing = SELL if guard_dir == "UP" else BUY if guard_dir == "DOWN" else None
    out = {}
    for side in (BUY, SELL):
        adding = pos == 0 or side == add
        q = clip * tier_factor(abs(pos), cap) if adding else clip
        if guard_level >= 1 and side == facing:
            q *= 0.5 if guard_level == 1 else 0.0
        if guard_level >= 2 and pos != 0 and side == add:
            q = 0.0
        if mk_level.get(side, 0) >= 2:
            q *= 0.5
        if taper:
            q *= 0.5
        out[side] = int(q)
    out[BUY] = max(0, min(out[BUY], HARD_POS - pos))
    out[SELL] = max(0, min(out[SELL], HARD_POS + pos))
    return out


def allocate(scores, b_total=B_TOTAL, floor=CAP_FLOOR, ceil=CAP_CEIL):
    tot = sum(scores.values())
    n = len(scores)
    return {k: clamp(b_total * (v / tot if tot > 0 else 1.0 / n), floor, ceil) for k, v in scores.items()}


def flow_score(A, k, half, edge):
    """Expected net P&L rate at the AS distance."""
    return A * math.exp(-k * half) * max(edge, 0.0)


def weighted_markout(entries, t, window, min_fills):
    """entries: (t_resolved, markout, qty). Qty-weighted mean over the window, None if too few fills."""
    recent = [(m, q) for ts, m, q in entries if ts >= t - window]
    if len(recent) < min_fills:
        return None
    tot = sum(q for _, q in recent)
    return sum(m * q for m, q in recent) / tot if tot > 0 else None


def markout_level(avg):
    if avg is None or avg >= -MARKOUT_TH:
        return 0
    return 2 if avg < -2 * MARKOUT_TH else 1


def update_guard(prev, zs, agg_markout, t):
    """prev = (level, dir, until). Level 1: halve the facing side. Level 2: facing off + reduce-only."""
    level, direction, until = prev
    ups = sum(1 for z in zs if z > Z_TH)
    downs = sum(1 for z in zs if z < -Z_TH)
    drift_dir = "UP" if ups >= 2 else "DOWN" if downs >= 2 else None
    bad = agg_markout is not None and agg_markout < -AGG_MARKOUT_TH
    new_dir = drift_dir
    if new_dir is None and bad:
        zsum = sum(zs)
        new_dir = "UP" if zsum > 0 else "DOWN" if zsum < 0 else None
    if new_dir is not None:
        trig = 2 if (drift_dir and bad) else 1
        if t <= until:
            trig = max(trig, level)
        return trig, new_dir, t + TREND_RELEASE_TICKS
    if level and t > until:
        return 0, None, until
    return level, direction, until


def update_stats(mu_ewma, var_ewma, last_mid, dt, mid):
    """One EWMA step over dt >= 1 ticks of mid change. Returns (mu_ewma, var_ewma)."""
    d = mid - last_mid
    a_mu, a_var = 2 / (DRIFT_WINDOW + 1), 2 / (SIGMA_WINDOW + 1)
    return mu_ewma + a_mu * (d / dt - mu_ewma), var_ewma + a_var * (d * d / dt - var_ewma)


def shrunk_drift(mu_ewma):
    return clamp(DRIFT_SHRINK * mu_ewma, -MU_MAX, MU_MAX)


def diff_orders(resting, desired, min_topup=MIN_TOPUP, max_order=MAX_ORDER):
    """Reconcile one side. Cancels any order not at the target price, trims excess newest first, tops up."""
    if desired is None or desired[1] <= 0:
        return {"cancel": [o["id"] for o in resting], "place": [], "price": None, "kept": 0}
    price, want = desired
    cancel = [o["id"] for o in resting if o["price"] != price]
    at_price = sorted((o for o in resting if o["price"] == price), key=lambda o: o["id"], reverse=True)
    kept = sum(o["qty"] for o in at_price)
    for o in at_price:
        if kept <= want:
            break
        cancel.append(o["id"])
        kept -= o["qty"]
    place = []
    shortfall = want - kept
    if shortfall > 0 and (kept == 0 or shortfall >= min_topup):
        while shortfall > 0:
            q = min(shortfall, max_order)
            place.append(q)
            shortfall -= q
    return {"cancel": cancel, "place": place, "price": price, "kept": kept}


def detect_fills(tracked, open_orders):
    """Per-order fills from cumulative quantity_filled. Returns ([(oid, action, qty, price)], vanished, new_tracked)."""
    now_open = {o["id"]: o for o in open_orders}
    fills, vanished = [], []
    for oid, old in tracked.items():
        still = now_open.get(oid)
        if still is None:
            vanished.append(old)
        elif still["filled"] > old["filled"]:
            fills.append((oid, old["action"], still["filled"] - old["filled"], old["price"]))
    return fills, vanished, {o["id"]: dict(o) for o in open_orders}


def build_book_ex_own(bids, asks, own_ids=(), own_trader=None):
    def aggregate(entries):
        levels = defaultdict(int)
        for o in entries:
            if o.get("order_id") in own_ids or (own_trader and o.get("trader_id") == own_trader):
                continue
            remaining = int(round(o["quantity"] - (o.get("quantity_filled") or 0)))
            if remaining > 0:
                levels[to_cents(o["price"])] += remaining
        return levels
    return sorted(aggregate(bids).items(), reverse=True), sorted(aggregate(asks).items())


def flatten_targets(pos, best_bid, best_ask):
    """Reducing limit for the full position, one tick inside the opposite side."""
    if pos > 0:
        return {BUY: None, SELL: (best_bid + TICK_C, pos)}
    if pos < 0:
        return {BUY: (best_ask - TICK_C, -pos), SELL: None}
    return {BUY: None, SELL: None}


def case_over(t, status, case_length):
    """STOPPED mid-case is a pause (clock frozen); a finished case is STOPPED with the clock reset to 0."""
    if t is not None and t >= case_length:
        return True
    return status == "STOPPED" and t == 0


# ============================== API LAYER ===============================

class Api:
    def __init__(self, key):
        self.session = requests.Session()
        self.session.headers.update({"X-API-Key": key})
        self.order_rate = ORDER_RATE
        self._order_times = deque()

    def _throttle(self):
        while True:
            now = time.monotonic()
            while self._order_times and now - self._order_times[0] >= 1.0:
                self._order_times.popleft()
            if len(self._order_times) < self.order_rate:
                break
            time.sleep(1.0 - (now - self._order_times[0]) + 0.005)
        self._order_times.append(time.monotonic())

    def _call(self, method, path, params=None, allow_404=False):
        for _ in range(RATE_LIMIT_RETRIES + 1):
            try:
                resp = self.session.request(method, BASE_URL + path, params=params, timeout=HTTP_TIMEOUT)
            except requests.RequestException as e:
                log_event("API_ERROR", f"{method} {path}: {type(e).__name__}")
                return None
            if resp.status_code == 429:
                wait = self._retry_wait(resp)
                log_event("RATE_LIMIT", f"{method} {path}: waiting {wait:.2f}s")
                time.sleep(wait)
                continue
            if allow_404 and resp.status_code == 404:
                return NOT_FOUND
            if not resp.ok:
                log_event("API_ERROR", f"{method} {path}: HTTP {resp.status_code} {resp.text[:160]}")
                return None
            try:
                return resp.json()
            except ValueError:
                log_event("API_ERROR", f"{method} {path}: non-JSON response")
                return None
        log_event("API_ERROR", f"{method} {path}: still rate limited after {RATE_LIMIT_RETRIES} retries")
        return None

    @staticmethod
    def _retry_wait(resp):
        try:
            wait = resp.json().get("wait")
            if wait is not None:
                return float(wait)
        except (ValueError, AttributeError):
            pass
        try:
            return float(resp.headers.get("Retry-After", 0.5))
        except ValueError:
            return 0.5

    @staticmethod
    def _unwrap(body):
        return body["value"] if isinstance(body, dict) and "value" in body else body

    def case(self):
        return self._call("GET", "/case")

    def trader(self):
        return self._call("GET", "/trader")

    def securities(self):
        """{ticker: security dict} for our three tickers, or None."""
        body = self._unwrap(self._call("GET", "/securities"))
        if not isinstance(body, list):
            return None
        return {s["ticker"]: s for s in body if s.get("ticker") in TICKERS}

    def book(self, ticker):
        body = self._call("GET", "/securities/book", {"ticker": ticker, "limit": BOOK_LIMIT})
        return body if isinstance(body, dict) else None

    def open_orders(self):
        """{ticker: [order]} for our tickers, or None on failure."""
        body = self._unwrap(self._call("GET", "/orders", {"status": "OPEN"}))
        if not isinstance(body, list):
            return None
        out = {t: [] for t in TICKERS}
        for o in body:
            if o.get("ticker") not in out or o.get("price") is None:
                continue
            filled = int(round(o.get("quantity_filled") or 0))
            remaining = int(round(o["quantity"])) - filled
            if remaining > 0:
                out[o["ticker"]].append({"id": o["order_id"], "action": o["action"], "price": to_cents(o["price"]),
                                         "qty": remaining, "filled": filled})
        return out

    def order(self, order_id):
        body = self._call("GET", f"/orders/{order_id}", allow_404=True)
        return body if isinstance(body, dict) else None

    def place_limit(self, ticker, action, qty, price_cents):
        self._throttle()
        body = self._call("POST", "/orders", {"ticker": ticker, "type": "LIMIT", "quantity": int(qty),
                                              "action": action, "price": f"{price_cents / 100:.2f}"})
        return body if isinstance(body, dict) and "order_id" in body else None

    def place_market(self, ticker, action, qty):
        self._throttle()
        body = self._call("POST", "/orders", {"ticker": ticker, "type": "MARKET",
                                              "quantity": int(qty), "action": action})
        return int(round(body.get("quantity_filled", 0))) if isinstance(body, dict) else 0

    def cancel(self, order_id):
        """'ok', 'gone' (already filled/cancelled) or 'failed'."""
        self._throttle()
        body = self._call("DELETE", f"/orders/{order_id}", allow_404=True)
        if body is NOT_FOUND:
            return "gone"
        return "ok" if isinstance(body, dict) and body.get("success", True) else "failed"

    def cancel_all(self, ticker):
        self._throttle()
        return self._call("POST", "/commands/cancel", {"ticker": ticker}) is not None


# ============================ STATE & LOGS ==============================

class Asset:
    def __init__(self, ticker):
        p = PARAMS[ticker]
        self.ticker = ticker
        self.p = p
        self.pos = 0
        self.open_orders = []
        self.tracked = {}
        self.meta = {}                       # order id -> (tick placed, quote distance in ticks)
        self.delta_ticks = {BUY: None, SELL: None}
        self.best_bid = self.best_ask = None
        self.bid_size = self.ask_size = 0
        self.mid = None                      # dollars
        self.last_book_ok = 0.0
        self.stat_t = None
        self.stat_mid = None
        self.mu_ewma = 0.0
        self.var_ewma = p["sigma0"] ** 2
        self.score = None
        self.gate_on = False
        self.cap = B_TOTAL / len(TICKERS)
        self.pending = deque()               # fills awaiting a markout
        self.mk = {BUY: deque(maxlen=50), SELL: deque(maxlen=50)}
        self.mk_level = {BUY: 0, SELL: 0}
        self.cal = {BUY: [None, -10**9], SELL: [None, -10**9]}   # [delta ticks, tick drawn]
        self.fills_n = 0
        self.fill_shares = 0
        self.max_abs_pos = 0
        self.snap = {}

    @property
    def sigma(self):
        return max(math.sqrt(self.var_ewma), SIGMA_MIN)

    @property
    def mu(self):
        return shrunk_drift(self.mu_ewma)

    @property
    def z(self):
        return self.mu / self.sigma


class Ctx:
    def __init__(self, calibrate=False):
        self.now = time.monotonic()
        self.t = None
        self.status = None
        self.case_length = CASE_LENGTH
        self.trader_id = None
        self.calibrate = calibrate
        self.assets = {t: Asset(t) for t in TICKERS}
        self.guard = (0, None, -1)
        self.agg_mk = deque(maxlen=100)
        self.nlv_start = None
        self.nlv = None
        self.last_tick = None
        self.last_logged = {}
        self.mkt_orders = 0
        self.mkt_shares = 0
        self.guard_ticks = defaultdict(int)
        self.rng = random.Random()


class CsvLog:
    def __init__(self, path, header):
        self.f = open(path, "w", newline="")
        self.w = csv.writer(self.f)
        self.w.writerow(header)
        self.f.flush()

    def row(self, values):
        self.w.writerow(values)
        self.f.flush()

    def close(self):
        self.f.close()


class EventLog(CsvLog):
    def __init__(self, path, ctx):
        super().__init__(path, ["t", "type", "detail"])
        self.ctx = ctx

    def log(self, kind, detail):
        self.row([self.ctx.t, kind, detail])
        print(f"[t={self.ctx.t}] {kind}: {detail}")


STATE_HEADER = ["t", "ticker", "pos", "mid", "s", "r", "spread", "half", "edge", "gate", "sigma", "mu", "z",
                "cap", "score", "bid", "bid_qty", "ask", "ask_qty", "guard_level", "guard_dir",
                "mk_buy", "mk_sell", "nlv"]
FILLS_HEADER = ["t_fill", "t_markout", "ticker", "side", "qty", "price", "mid_at_fill", "delta_ticks",
                "rest_ticks", "markout"]


def log_state_rows(ctx, state_log):
    if ctx.t is None:
        return
    for a in ctx.assets.values():
        s = a.snap
        if not s or ctx.last_logged.get(a.ticker) == ctx.t:
            continue
        ctx.last_logged[a.ticker] = ctx.t
        state_log.row([ctx.t, a.ticker, a.pos, f"{a.mid:.4f}" if a.mid else "", s.get("s", ""), s.get("r", ""),
                       s.get("spread", ""), s.get("half", ""), s.get("edge", ""), a.gate_on,
                       f"{a.sigma:.5f}", f"{a.mu:.5f}", f"{a.z:.3f}", f"{a.cap:.0f}",
                       "" if a.score is None else f"{a.score:.5f}",
                       fmt_px(s.get("bid")), s.get("bid_qty", 0), fmt_px(s.get("ask")), s.get("ask_qty", 0),
                       ctx.guard[0], ctx.guard[1] or "", a.mk_level[BUY], a.mk_level[SELL],
                       "" if ctx.nlv is None else f"{ctx.nlv:.2f}"])


# ============================ STATE REFRESH =============================

def note_position(a, pos):
    a.pos = pos
    a.max_abs_pos = max(a.max_abs_pos, abs(pos))


def record_fill(ctx, a, oid, action, qty, price):
    placed_t, delta = a.meta.get(oid, (ctx.t, None))
    rest = None if ctx.t is None or placed_t is None else ctx.t - placed_t
    a.fills_n += 1
    a.fill_shares += qty
    a.pending.append({"t": ctx.t, "side": action, "qty": qty, "px": price, "mid": a.mid,
                      "delta": delta, "rest": rest})
    mid = "" if a.mid is None else f"{a.mid:.4f}"
    log_event("FILL", f"{a.ticker} {action} {qty} @ {fmt_px(price)} mid={mid} (pos {a.pos:+d})")


def note_open_orders(api, ctx, a, orders):
    """Record passive fills since the last read; vanished orders are looked up, never assumed filled."""
    fills, vanished, a.tracked = detect_fills(a.tracked, orders)
    a.open_orders = orders
    for oid, action, qty, price in fills:
        record_fill(ctx, a, oid, action, qty, price)
    for old in vanished:
        final = api.order(old["id"])
        if not isinstance(final, dict):
            log_event("API_ERROR", f"{a.ticker}: could not look up order {old['id']}; fills not counted")
            continue
        extra = int(round(final.get("quantity_filled") or 0)) - old["filled"]
        if extra > 0:
            record_fill(ctx, a, old["id"], old["action"], extra, old["price"])
        a.meta.pop(old["id"], None)


def track_placed(ctx, a, order, action, qty, price):
    oid = order["order_id"]
    a.meta[oid] = (ctx.t, a.delta_ticks.get(action))
    filled = int(round(order.get("quantity_filled") or 0))
    if filled > 0:
        record_fill(ctx, a, oid, action, filled, price)
    a.tracked[oid] = {"id": oid, "action": action, "price": price, "qty": qty - filled, "filled": filled}


def book_fresh(a):
    return a.best_bid is not None and time.monotonic() - a.last_book_ok <= STALE_BOOK_S


def refresh_state(api, ctx):
    ctx.now = now = time.monotonic()
    case = api.case()
    if case:
        ctx.t = case.get("tick", ctx.t)
        ctx.status = case.get("status", ctx.status)

    secs = api.securities()
    if secs:
        for t, sec in secs.items():
            if "position" in sec:
                note_position(ctx.assets[t], int(round(sec["position"])))

    orders = api.open_orders()
    if orders is not None:
        for t, a in ctx.assets.items():
            note_open_orders(api, ctx, a, orders[t])

    for a in ctx.assets.values():
        raw = api.book(a.ticker)
        if raw is None:
            continue
        bids, asks = build_book_ex_own(raw.get("bids") or [], raw.get("asks") or [],
                                       {o["id"] for o in a.open_orders}, ctx.trader_id)
        if bids and asks:
            (a.best_bid, a.bid_size), (a.best_ask, a.ask_size) = bids[0], asks[0]
            a.mid = (a.best_bid + a.best_ask) / 200
            a.last_book_ok = now


# ============================== EXECUTION ===============================

def reconcile(api, ctx, a, targets):
    """Diff desired quotes against resting orders: cancels first, then top-ups, never past HARD_POS."""
    plans = {side: diff_orders([o for o in a.open_orders if o["action"] == side], targets.get(side))
             for side in (BUY, SELL)}
    qty_by_id = {o["id"]: o["qty"] for o in a.open_orders}
    stuck = {BUY: 0, SELL: 0}
    for side, plan in plans.items():
        for oid in plan["cancel"]:
            if api.cancel(oid) == "failed":
                stuck[side] += qty_by_id.get(oid, 0)
    for side, plan in plans.items():
        resting = plan["kept"] + stuck[side]
        for q in plan["place"]:
            room = HARD_POS - (a.pos + resting) if side == BUY else HARD_POS + (a.pos - resting)
            if q > room:
                log_event("EXPOSURE_SKIP", f"{a.ticker} {side} {q} shrunk to {max(0, room)} (pos={a.pos}, resting={resting})")
                q = room
            if q <= 0:
                break
            order = api.place_limit(a.ticker, side, q, plan["price"])
            if order is not None:
                log_event("POST", f"{a.ticker} {side} {q} @ {fmt_px(plan['price'])} (id {order['order_id']})")
                track_placed(ctx, a, order, side, q, plan["price"])
                resting += q


def send_market(api, ctx, a, action, qty, reason):
    filled_total, remaining = 0, qty
    while remaining > 0:
        q = min(remaining, MAX_ORDER)
        filled = api.place_market(a.ticker, action, q)
        ctx.mkt_orders += 1
        ctx.mkt_shares += filled
        log_event("MARKET", f"{reason}: {a.ticker} {action} {q}, filled {filled}")
        filled_total += filled
        remaining -= q
        if filled == 0:
            break
    secs = api.securities()
    if secs and a.ticker in secs and "position" in secs[a.ticker]:
        note_position(a, int(round(secs[a.ticker]["position"])))
    else:
        note_position(a, a.pos + (filled_total if action == BUY else -filled_total))
    return filled_total


def cancel_until_clear(api, ctx, a, attempts=5):
    for _ in range(attempts):
        api.cancel_all(a.ticker)
        orders = api.open_orders()
        secs = api.securities()
        if secs and a.ticker in secs and "position" in secs[a.ticker]:
            note_position(a, int(round(secs[a.ticker]["position"])))
        if orders is not None:
            note_open_orders(api, ctx, a, orders[a.ticker])
            if not orders[a.ticker]:
                return True
    return False


# ============================ MODEL & QUOTING ===========================

def on_new_tick(ctx):
    """Advance per-asset mid-change EWMAs once per case tick (dt covers skipped ticks)."""
    for a in ctx.assets.values():
        if a.mid is None:
            continue
        if a.stat_t is not None and ctx.t > a.stat_t:
            a.mu_ewma, a.var_ewma = update_stats(a.mu_ewma, a.var_ewma, a.stat_mid, ctx.t - a.stat_t, a.mid)
        if a.stat_t is None or ctx.t > a.stat_t:
            a.stat_t, a.stat_mid = ctx.t, a.mid


def resolve_markouts(ctx):
    for a in ctx.assets.values():
        while a.pending and a.mid is not None and ctx.t >= a.pending[0]["t"] + MARKOUT_TICKS:
            f = a.pending.popleft()
            mk = (a.mid - f["px"] / 100) if f["side"] == BUY else (f["px"] / 100 - a.mid)
            a.mk[f["side"]].append((ctx.t, mk, f["qty"]))
            ctx.agg_mk.append((ctx.t, mk, f["qty"]))
            if _fills_log is not None:
                _fills_log.row([f["t"], ctx.t, a.ticker, f["side"], f["qty"], fmt_px(f["px"]),
                                "" if f["mid"] is None else f"{f['mid']:.4f}",
                                "" if f["delta"] is None else f"{f['delta']:.2f}",
                                "" if f["rest"] is None else f["rest"], f"{mk:.5f}"])
        for side in (BUY, SELL):
            a.mk_level[side] = markout_level(
                weighted_markout(a.mk[side], ctx.t, MARKOUT_WINDOW, MARKOUT_MIN_FILLS))


def asset_model(ctx, a):
    s = compute_fv(a.best_bid, a.best_ask, a.bid_size, a.ask_size) / 100
    tau = max(0, ctx.case_length - (ctx.t or 0))
    r, spread, half = as_quote(s, a.pos, tau, a.sigma, a.mu, GAMMA, a.p["k"], a.p["rho"])
    return {"s": s, "tau": tau, "r": r, "spread": spread, "half": half, "edge": net_edge(spread, a.p["rho"])}


def calibration_targets(ctx, a, m):
    """Random quote distance per side (1-5 ticks from s), fixed clip, no model, no gating."""
    px = {}
    for side in (BUY, SELL):
        d, drawn = a.cal[side]
        if d is None or ctx.t - drawn >= CAL_REDRAW_TICKS:
            a.cal[side] = [ctx.rng.choice(CAL_DELTA_TICKS), ctx.t]
        d = a.cal[side][0]
        a.delta_ticks[side] = d
        px[side] = (math.floor(m["s"] * 100 - d * TICK_C + _EPS) if side == BUY
                    else math.ceil(m["s"] * 100 + d * TICK_C - _EPS))
    bid = min(px[BUY], a.best_bid + TICK_C, a.best_ask - TICK_C)
    ask = max(px[SELL], a.best_ask - TICK_C, a.best_bid + TICK_C)
    ask = max(ask, bid + TICK_C)
    qty = compute_sizes(a.pos, SOFT_CAP)
    return {BUY: (bid, qty[BUY]) if qty[BUY] > 0 else None,
            SELL: (ask, qty[SELL]) if qty[SELL] > 0 else None}


def as_targets(ctx, a, m):
    level, direction, _ = ctx.guard
    if not a.gate_on and a.pos == 0:
        return {BUY: None, SELL: None}
    bid, ask = quote_prices(m["r"], m["half"], a.best_bid, a.best_ask,
                            bid_extra=1 if a.mk_level[BUY] >= 1 else 0,
                            ask_extra=1 if a.mk_level[SELL] >= 1 else 0)
    cap = min(a.cap, SOFT_CAP)
    taper = TAPER_ENABLED and m["tau"] <= TAPER_TICKS
    qty = compute_sizes(a.pos, cap, CLIP, level, direction, a.mk_level, taper)
    if not a.gate_on:  # edge too thin to add inventory, but never strand a position
        add = adding_side(a.pos)
        qty[add] = 0
    a.delta_ticks[BUY] = (m["s"] * 100 - bid) / TICK_C
    a.delta_ticks[SELL] = (ask - m["s"] * 100) / TICK_C
    return {BUY: (bid, qty[BUY]) if qty[BUY] > 0 else None,
            SELL: (ask, qty[SELL]) if qty[SELL] > 0 else None}


def step(api, ctx):
    if ctx.status != "ACTIVE":
        return
    live = []
    for a in ctx.assets.values():
        if book_fresh(a):
            live.append(a)
        else:
            a.snap = {}
            if a.open_orders:
                log_event("STALE_BOOK", f"{a.ticker}: book stale, cancelling")
                api.cancel_all(a.ticker)
    if not live:
        return

    new_tick = ctx.t != ctx.last_tick
    if new_tick:
        on_new_tick(ctx)
        trader = api.trader()
        if trader and trader.get("nlv") is not None:
            ctx.nlv = trader["nlv"]
            if ctx.nlv_start is None:
                ctx.nlv_start = ctx.nlv
    resolve_markouts(ctx)

    models = {a.ticker: asset_model(ctx, a) for a in live}
    if not ctx.calibrate:
        for a in live:
            m = models[a.ticker]
            a.gate_on = update_gate(a.gate_on, m["edge"])
            if new_tick:
                sc = flow_score(a.p["A"], a.p["k"], m["half"], m["edge"])
                alpha = 2 / (SCORE_WINDOW + 1)
                a.score = sc if a.score is None else a.score + alpha * (sc - a.score)
        scored = {t: (ctx.assets[t].score or 0.0) for t in TICKERS}
        for t, cap in allocate(scored).items():
            ctx.assets[t].cap = cap
        agg = weighted_markout(ctx.agg_mk, ctx.t, MARKOUT_WINDOW, AGG_MIN_FILLS)
        ctx.guard = update_guard(ctx.guard, [ctx.assets[t].z for t in TICKERS], agg, ctx.t)
        if new_tick and ctx.guard[0]:
            ctx.guard_ticks[ctx.guard[0]] += 1

    for a in live:
        m = models[a.ticker]
        targets = calibration_targets(ctx, a, m) if ctx.calibrate else as_targets(ctx, a, m)
        reconcile(api, ctx, a, targets)
        b, k = targets.get(BUY), targets.get(SELL)
        a.snap = {"s": f"{m['s']:.4f}", "r": f"{m['r']:.4f}", "spread": f"{m['spread']:.4f}",
                  "half": f"{m['half']:.4f}", "edge": f"{m['edge']:.4f}",
                  "bid": b[0] if b else None, "bid_qty": b[1] if b else 0,
                  "ask": k[0] if k else None, "ask_qty": k[1] if k else 0}
    ctx.last_tick = ctx.t


# =============================== STOP ===================================

def flatten_all(api, ctx):
    """CANCEL -> PASSIVE (reducing limits one tick inside) -> MARKET, over all tickers, until flat.

    Blocking. A second Ctrl-C raises out of here and leaves orders/positions live.
    Returns True when flat with nothing resting, False if the case stopped trading first.
    """
    print("Flattening all tickers (Ctrl-C again aborts and leaves orders/positions live)...")
    assets = list(ctx.assets.values())
    phase, passive_start = "CANCEL", None
    while True:
        refresh_state(api, ctx)
        if ctx.status != "ACTIVE":
            log_event("FLATTEN", f"case status {ctx.status}; cannot trade, stopping with "
                                 + ", ".join(f"{a.ticker}={a.pos}" for a in assets))
            return False
        if all(a.pos == 0 for a in assets):
            if all(cancel_until_clear(api, ctx, a) for a in assets):
                return True
            time.sleep(LOOP_SLEEP)
            continue
        if phase == "CANCEL":
            if all(cancel_until_clear(api, ctx, a) for a in assets):
                phase, passive_start = "PASSIVE", time.monotonic()
                log_event("FLATTEN", "orders cleared; passive exit")
            else:
                time.sleep(LOOP_SLEEP)
                continue
        if phase == "PASSIVE":
            if time.monotonic() - passive_start < FLAT_PASSIVE_S:
                for a in assets:
                    if a.pos == 0:
                        if a.open_orders:
                            cancel_until_clear(api, ctx, a)
                    elif book_fresh(a):
                        reconcile(api, ctx, a, flatten_targets(a.pos, a.best_bid, a.best_ask))
                    elif a.open_orders:
                        cancel_until_clear(api, ctx, a)
                time.sleep(LOOP_SLEEP)
                continue
            phase = "MARKET"
            log_event("FLATTEN", "passive window over; going to market")
        if phase == "MARKET":
            for a in assets:
                if a.pos == 0:
                    continue
                if not cancel_until_clear(api, ctx, a):
                    continue
                if a.pos != 0:  # a rejected/zero-fill market order is simply retried next loop
                    send_market(api, ctx, a, SELL if a.pos > 0 else BUY, abs(a.pos), "FLATTEN")
            time.sleep(LOOP_SLEEP)


# ============================== MAIN LOOP ===============================

def startup(api, ctx):
    print("Waiting for trader info...")
    while True:
        trader = api.trader()
        if trader and trader.get("trader_id"):
            ctx.trader_id = trader["trader_id"]
            break
        time.sleep(0.5)

    print("Waiting for the case to be ACTIVE with two-sided books on all tickers...")
    while True:
        refresh_state(api, ctx)
        if ctx.status == "ACTIVE" and all(book_fresh(a) for a in ctx.assets.values()):
            break
        time.sleep(LOOP_SLEEP)

    case = api.case() or {}
    ticks, periods = case.get("ticks_per_period"), case.get("total_periods") or 1
    if ticks:
        ctx.case_length = int(ticks) * int(periods)
    secs = api.securities() or {}
    rates = [int(s["api_orders_per_second"]) for s in secs.values() if s.get("api_orders_per_second")]
    if rates:
        api.order_rate = max(1, min(rates))
    trader = api.trader()
    ctx.nlv_start = trader["nlv"] if trader and trader.get("nlv") is not None else None
    ctx.nlv = ctx.nlv_start
    print(f"Started at t={ctx.t}: case_length={ctx.case_length}, start NLV={ctx.nlv_start}, "
          f"positions={ {a.ticker: a.pos for a in ctx.assets.values()} }, order rate={api.order_rate}/s, "
          f"mode={'CALIBRATE' if ctx.calibrate else 'AS'}")


def run(api, ctx, state_log):
    while True:
        if os.path.exists(STOP_FILE):
            log_event("STOP_FILE", "STOP sentinel found: flattening")
            return "STOP"
        refresh_state(api, ctx)
        if case_over(ctx.t, ctx.status, ctx.case_length):
            return "OVER"
        step(api, ctx)
        log_state_rows(ctx, state_log)
        time.sleep(LOOP_SLEEP)


def print_summary(api, ctx):
    trader = api.trader()
    nlv = trader["nlv"] if trader and trader.get("nlv") is not None else ctx.nlv
    pnl = None if nlv is None or ctx.nlv_start is None else nlv - ctx.nlv_start
    print("\n========== ALGO2e run summary ==========")
    print(f"P&L (NLV change):  {'n/a' if pnl is None else f'{pnl:,.2f}'}")
    print(f"Market orders:     {ctx.mkt_orders} ({ctx.mkt_shares:,} shares)")
    print(f"Trend guard ticks: L1={ctx.guard_ticks[1]} L2={ctx.guard_ticks[2]}")
    for a in ctx.assets.values():
        mk = weighted_markout(list(a.mk[BUY]) + list(a.mk[SELL]), ctx.t or 0, 10**9, 1)
        print(f"  {a.ticker:<4} pos={a.pos:+,d} max|pos|={a.max_abs_pos:,} fills={a.fills_n} "
              f"shares={a.fill_shares:,} avg markout={'n/a' if mk is None else f'{mk:+.4f}'}")


def main():
    global _events, _fills_log
    key = os.environ.get("RIT_API_KEY")
    if not key:
        sys.exit("Set RIT_API_KEY (environment variable or ../.env).")
    if os.path.exists(STOP_FILE):
        os.remove(STOP_FILE)
        print("Removed stale STOP file.")
    ctx = Ctx(calibrate="--calibrate" in sys.argv)
    _events = EventLog(LOG_EVENTS_PATH, ctx)
    _fills_log = CsvLog(LOG_FILLS_PATH, FILLS_HEADER)
    state_log = CsvLog(LOG_STATE_PATH, STATE_HEADER)
    api = Api(key)
    try:
        try:
            startup(api, ctx)
            outcome = run(api, ctx, state_log)
            if outcome == "STOP":
                flatten_all(api, ctx)
                print("Flat. Now stop the case in the RIT client.")
        except KeyboardInterrupt:
            log_event("MANUAL_STOP", "Ctrl-C: flattening")
            if flatten_all(api, ctx):
                print("Flat. Now stop the case in the RIT client.")
        except Exception:
            log_event("CRASH", traceback.format_exc().strip().splitlines()[-1])
            traceback.print_exc()
            flatten_all(api, ctx)
    except KeyboardInterrupt:
        print("Flatten aborted; orders/positions may still be live.")
    finally:
        print_summary(api, ctx)
        state_log.close()
        _fills_log.close()
        _events.close()


if __name__ == "__main__":
    main()
