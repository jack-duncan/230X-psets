#!/usr/bin/env python3
"""
smoke_mock.py - smoke test for mock_rit.py.  Starts nothing itself; expects a
mock already running on http://localhost:9999/v1 (override with --base).

    python mock_rit.py                # in another terminal
    python smoke_mock.py
"""
import argparse
import sys
import time

import requests

ap = argparse.ArgumentParser()
ap.add_argument("--base", default="http://localhost:9999/v1")
args = ap.parse_args()
BASE = args.base
H = {"X-API-Key": "smoke"}
s = requests.Session()
s.headers.update(H)

results = []


def check(name, ok, detail=""):
    results.append(ok)
    print("%s  %s%s" % ("PASS" if ok else "FAIL", name, ("  -- " + str(detail)) if detail else ""), flush=True)
    return ok


# 0. auth
r = requests.get(BASE + "/case")
check("401 without X-API-Key", r.status_code == 401 and r.json().get("code") == "UNAUTHORIZED",
      "%s %s" % (r.status_code, r.text[:80]))

# 1. case
r = s.get(BASE + "/case")
c = r.json()
check("GET /case", r.status_code == 200 and c.get("status") == "ACTIVE" and "tick" in c,
      "tick=%s status=%s ticks_per_period=%s" % (c.get("tick"), c.get("status"), c.get("ticks_per_period")))

# 1b. make the test independent of earlier activity: flatten any existing position
dbg0 = s.get(BASE + "/_debug").json()
while dbg0["position"] != 0:
    q = min(5000, abs(int(dbg0["position"])))
    s.post(BASE + "/orders", params={"ticker": "ALGO", "type": "MARKET", "quantity": q,
                                     "action": "SELL" if dbg0["position"] > 0 else "BUY"})
    time.sleep(0.15)
    dbg0 = s.get(BASE + "/_debug").json()
fees0, rebates0 = dbg0["fees"], dbg0["rebates"]

# 2. book + securities
r = s.get(BASE + "/securities/book", params={"ticker": "ALGO", "limit": 5})
book = r.json()
ok = r.status_code == 200 and book["bids"] and book["asks"] \
    and book["bids"][0]["price"] < book["asks"][0]["price"] \
    and all(k in book["asks"][0] for k in ("order_id", "trader_id", "quantity", "quantity_filled", "vwap", "status", "tick"))
best_ask = book["asks"][0]["price"]
best_bid = book["bids"][0]["price"]
check("GET /securities/book", ok, "bid=%.2f ask=%.2f ask_trader=%s" % (best_bid, best_ask, book["asks"][0]["trader_id"]))

r = s.get(BASE + "/securities", params={"ticker": "ALGO"})
sec = r.json()[0]
check("GET /securities", r.status_code == 200 and sec["ticker"] == "ALGO"
      and sec["api_orders_per_second"] == 10 and sec["trading_fee"] == 0.01 and sec["limit_order_rebate"] == 0.005,
      "last=%.2f bid=%.2f ask=%.2f pos=%s" % (sec["last"], sec["bid"], sec["ask"], sec["position"]))
pos0 = sec["position"]
realized0 = sec["realized"]

# 3. LIMIT buy at best ask -> should fill immediately
t0 = time.time()
r = s.post(BASE + "/orders", params={"ticker": "ALGO", "type": "LIMIT", "quantity": 100,
                                     "action": "BUY", "price": best_ask})
o = r.json()
check("POST LIMIT buy at best ask returns order dict", r.status_code == 200 and o.get("order_id")
      and o.get("type") == "LIMIT" and o.get("action") == "BUY" and o.get("trader_id") == "test_trader",
      "id=%s status=%s filled=%s vwap=%s (%s snapshot)" % (
          o.get("order_id"), o.get("status"), o.get("quantity_filled"), o.get("vwap"),
          "pre-match" if o.get("quantity_filled") == 0 else "post-match"))
oid = o["order_id"]

# 4. immediate GET /orders/{id} -> 404 (filled and/or pre-lag) or OPEN
r = s.get(BASE + "/orders/%d" % oid)
check("immediate GET /orders/{id} is 404 or OPEN", r.status_code == 404
      or (r.status_code == 200 and r.json().get("status") == "OPEN"),
      "%s %s (%.0f ms after POST)" % (r.status_code, r.text[:90], (time.time() - t0) * 1000))

# 5. after the listing lag it must show as TRANSACTED
time.sleep(0.6)
r = s.get(BASE + "/orders", params={"status": "TRANSACTED"})
lst = r.json()
mine = [x for x in lst if x["order_id"] == oid]
check("GET /orders?status=TRANSACTED lists it after 0.6 s", bool(mine) and mine[0]["status"] == "TRANSACTED"
      and mine[0]["quantity_filled"] == 100 and mine[0]["vwap"] is not None,
      "found=%s filled=%s vwap=%s" % (bool(mine), mine[0]["quantity_filled"] if mine else None,
                                       mine[0]["vwap"] if mine else None))
r = s.get(BASE + "/orders/%d" % oid)
check("GET /orders/{id} of filled order is 404", r.status_code == 404 and r.json().get("code") == "ORDER_NOT_FOUND",
      "%s %s" % (r.status_code, r.text[:80]))

# 6. P&L: position +100, realized == rebate only (0.005 * 100)
r = s.get(BASE + "/securities", params={"ticker": "ALGO"})
sec = r.json()[0]
filled = mine[0]["quantity_filled"] if mine else 0
check("position/realized after limit fill", sec["position"] == pos0 + filled
      and abs(sec["realized"] - realized0 - 0.005 * filled) < 1e-6,
      "pos=%s realized=%.4f (expected +%.4f rebate) unrealized=%.4f" % (
          sec["position"], sec["realized"], 0.005 * filled, sec["unrealized"]))

# 7. burst of 12 orders (deep bids that will not fill) -> expect 429s
far = round(best_bid - 0.50, 2)
codes = []
bodies = []
for i in range(12):
    r = s.post(BASE + "/orders", params={"ticker": "ALGO", "type": "LIMIT", "quantity": 100,
                                         "action": "BUY", "price": far})
    codes.append(r.status_code)
    bodies.append(r.json())
n429 = codes.count(429)
n200 = codes.count(200)
rl = [b for c, b in zip(codes, bodies) if c == 429]
check("burst of 12 POSTs hits 429 rate limit", n429 >= 1 and n200 <= 10
      and all(b.get("code") == "RATE_LIMIT" and "wait" in b for b in rl),
      "codes=%s wait=%s" % (codes, rl[0].get("wait") if rl else None))
burst_ids = [b["order_id"] for c, b in zip(codes, bodies) if c == 200]

# 8. lag: the burst orders should not be listed yet, but be listed after the lag
r = s.get(BASE + "/orders", params={"status": "OPEN"})
early = {x["order_id"] for x in r.json()}
time.sleep(0.6)
r = s.get(BASE + "/orders", params={"status": "OPEN"})
late = {x["order_id"] for x in r.json()}
check("listing lag: burst orders appear in OPEN list only after the lag",
      not (set(burst_ids) <= early) and set(burst_ids) <= late,
      "immediately visible=%d/%d, after 0.6s visible=%d/%d" % (
          len(early & set(burst_ids)), len(burst_ids), len(late & set(burst_ids)), len(burst_ids)))

r = s.get(BASE + "/orders/%d" % burst_ids[0])
check("GET /orders/{id} of open visible order", r.status_code == 200 and r.json()["status"] == "OPEN",
      "%s" % r.status_code)

# 9. DELETE one, then cancel all
r = s.delete(BASE + "/orders/%d" % burst_ids[0])
check("DELETE /orders/{id}", r.status_code == 200 and r.json().get("success") is True, r.text[:60])
r = s.delete(BASE + "/orders/%d" % burst_ids[0])
check("DELETE again -> 404", r.status_code == 404, r.text[:60])

r = s.post(BASE + "/commands/cancel", params={"all": 1})
cx = r.json().get("cancelled_order_ids", [])
check("POST /commands/cancel?all=1", r.status_code == 200 and set(cx) == set(burst_ids[1:]),
      "cancelled=%s" % cx)
r = s.get(BASE + "/orders", params={"status": "OPEN"})
check("no OPEN orders after cancel all", r.status_code == 200 and r.json() == [], "open=%d" % len(r.json()))
r = s.get(BASE + "/orders", params={"status": "CANCELLED"})
cids = {x["order_id"] for x in r.json()}
check("cancelled orders listed under status=CANCELLED", set(burst_ids) <= cids, "n=%d" % len(cids))

# 10. MARKET sell to flatten -> TRANSACTED with fill, fee applied
time.sleep(1.1)  # let the order-rate window clear
r = s.post(BASE + "/orders", params={"ticker": "ALGO", "type": "MARKET", "quantity": int(filled),
                                     "action": "SELL"})
mo = r.json()
check("POST MARKET sell returns TRANSACTED with fill", r.status_code == 200 and mo.get("status") == "TRANSACTED"
      and mo.get("quantity_filled") == filled and mo.get("vwap") is not None,
      "status=%s filled=%s vwap=%s" % (mo.get("status"), mo.get("quantity_filled"), mo.get("vwap")))
r = s.get(BASE + "/_debug")
dbg = r.json()
r = s.get(BASE + "/securities", params={"ticker": "ALGO"})
sec = r.json()[0]
exp_real = realized0 + 0.005 * filled - 0.01 * filled + (mo.get("vwap", 0) - mine[0]["vwap"]) * filled
check("flat position, fees/rebates and realized correct",
      sec["position"] == 0 and abs(dbg["fees"] - fees0 - 0.01 * filled) < 1e-6
      and abs(dbg["rebates"] - rebates0 - 0.005 * filled) < 1e-6 and abs(sec["realized"] - exp_real) < 1e-4
      and abs(sec["unrealized"]) < 1e-9,
      "pos=%s realized=%.4f expected=%.4f fees+=%.4f rebates+=%.4f" % (
          sec["position"], sec["realized"], exp_real, dbg["fees"] - fees0, dbg["rebates"] - rebates0))

# 11. misc endpoints
r = s.get(BASE + "/trader")
check("GET /trader", r.status_code == 200 and r.json().get("trader_id") == "test_trader" and "nlv" in r.json(),
      r.text[:80])
r = s.get(BASE + "/limits")
check("GET /limits", r.status_code == 200 and r.json()[0]["name"] == "LIMIT-STOCK"
      and r.json()[0]["gross_limit"] == 25000, r.text[:120])
r = s.post(BASE + "/orders", params={"ticker": "ALGO", "type": "LIMIT", "quantity": 6000,
                                     "action": "BUY", "price": far})
check("quantity 6000 -> 400", r.status_code == 400, r.text[:80])
r = s.post(BASE + "/orders", params={"ticker": "ALGO", "type": "LIMIT", "quantity": 100, "action": "BUY"})
check("LIMIT without price -> 400", r.status_code == 400, r.text[:80])
check("GET /_debug", set(dbg) >= {"fair", "position", "cash", "realized", "unrealized", "fees", "rebates",
                                  "n_bot_orders", "n_bot_fills", "bot_volume", "tick"},
      "fair=%.3f tick=%s n_bot_orders=%s n_bot_fills=%s bot_volume=%s" % (
          dbg["fair"], dbg["tick"], dbg["n_bot_orders"], dbg["n_bot_fills"], dbg["bot_volume"]))

n = len(results)
p = sum(results)
print("\n%d/%d checks passed -> %s" % (p, n, "ALL PASS" if p == n else "FAILURES"), flush=True)
sys.exit(0 if p == n else 1)
