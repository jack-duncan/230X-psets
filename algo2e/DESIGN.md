# ALGO2 v2 — multi-asset market maker for RIT (limit orders only)

Target: today's class case. 4 stocks (seen so far: CNR, RY, AC, + one more), one
account per trader (or per group — see "Group mode"), limit orders only, everyone
in the room competing for the same fills.

## 1. What the records say (evidence base)

Source: five ALGO2 runs on 2026-09-24 (practice 19:28, 19:46, 19:55, 20:02 and
the graded 20:09 case), the ALGO1 write-up (PS3), the HW3 report (teammates), the
RIT screenshot at the close, and Giancarlo's ALGO1 bot.

| Fact | Evidence | Design consequence |
|---|---|---|
| Case ran 300 ticks in 20 min (4.07 s/tick); practice runs had 1-4 s/tick | ticks csv, screenshot | Measure tick length live; all timers in seconds, not ticks |
| Matching is continuous, not per tick (fills land 0.1-4 s after a tick boundary, uniform) | ticks csv fill rows | Speed matters at all times, not only at tick boundaries |
| Book is thick: 37k-455k shares at the touch, spread = 1 tick 99% of the time | ticks csv bid_sz/ask_sz, spread | We rarely get to "improve"; fills come from (a) being early at a level that becomes the touch, (b) the level being swept. Queue position is the asset: never cancel an order at the touch without a risk reason |
| Server rebate 1.5c/share on filled resting limit orders (brief says 0.5c); fee 1c on takers | log header `fee 0.01 rebate 0.015`, /securities | Read fee/rebate from /securities. With a 1-tick spread the rebate is bigger than the spread: volume is the P&L driver as long as adverse selection < ~1.5-2c |
| Fill edge marked 3 s later: +0.3 to +0.5 c/sh (positive) | log `edge=` | Quoting is profitable; inventory is what loses |
| Graded case: price 19.98 -> 21.25 (tick 165) -> 20.04. Bot was short -9k to -12k into the rally, lost $6.2k by tick 120; ALGO1 lost $4.7k the same way | 200928 log, ALGO1_brief | Trend circuit-breaker and strong size skew are mandatory; lean fair with the trend; never let the reducing side go empty |
| Manual trading after the bot was disabled reached -65,000 shares -> $4,000 fine | 200928 ticks net=-65000, screenshot Fines 4,000 | Hard caps on position + open quantity, account-wide, with buffer. Fines are 10c/share: one breach wipes a day of rebates |
| Loop: 76-78 loops/s, 168 calls/s (self-capped at 170), 0 x 429 | ticks csv | Reads are effectively unlimited (teammate: "reads are not rate-limited"); budget ~170 calls/s safely |
| Orders: POST round trip 7-35 ms typical, rare 230-900 ms outliers; 10 orders/s per ticker incl. cancels (teammate measured); `api_orders_per_second` field = 5 but 20/s did not drop orders | logs, HW3 p.18 | Per-ticker order budget (token bucket, 8/s default), priority queue: risk cancels > new-level race > refills. Post/cancel in worker threads so a slow POST never stalls the book loop |
| New orders appear in GET /orders 0.1-0.6 s late; filled orders 404 on GET /orders/{id}; server "drops" orders silently under rate pressure | ALGO1_brief, logs `never listed in 2.5s` | Keep v1 reconciliation (OPEN+TRANSACTED+CANCELLED with grace, 15 s before forgetting), per ticker |
| `/securities` bid_size/ask_size = TOTAL size of the whole side, not the touch | HW3 p.14 (Giancarlo) | Use /securities only for last/position; touch sizes and queue position come from /securities/book |
| Position from /securities lags fills by ~hundreds of ms | HW3 p.18 | Fast position estimate = server anchor + own fills since; resync when quiet (v1 logic) |
| Cockpit COM errors ("RPC server unavailable") when Excel closes; `'NoneType' has no attribute 'Range'` | logs | Cockpit optional; params.json hot-reload fallback; cockpit thread errors rate-limited in the log |
| Bot kept running 6 days after the client closed, 637 MB log | 200928 log | Stop when /case unreachable for 60 s; cap log spam |

## 2. Objective

Maximise filled volume at or near the touch on all 4 names (rebate + spread) while
keeping inventory small, flat at the close, and inside the limits at all times.
Secondary: use the cross-sectional structure (common factor, pairs) both to avoid
adverse selection (lead-lag) and to lean quotes toward mean reversion of a pair
spread.

## 3. Architecture (speed first)

```
main thread      : startup, discovery (/case /trader /securities /limits), param hot-reload,
                   slow lane (4 Hz): /case /securities(all) /limits /trader, end-of-case logic,
                   watchdog, shutdown
per-ticker thread: fast loop ~25-40 Hz: GET /securities/book?ticker=X&limit=D
                   -> microstructure state -> Quoter.compute -> Quoter.reconcile
                   -> cancel/post jobs submitted to the ticker's own 2-thread pool
                   (the loop never waits for an order round trip)
orders thread    : GET /orders?status=OPEN at ~8 Hz, TRANSACTED/CANCELLED at ~4 Hz,
                   fill detection by quantity_filled deltas (vwap-weighted), settling of
                   cancels, orphan sweep (off in group mode), fast position per ticker
analytics thread : 10 Hz: mid histories -> vol, drift, factor return, pair betas,
                   spread z-scores -> leans published to the quoters
cockpit thread   : xlwings (optional) or params.json; writes Live sheet 4 Hz
log thread       : queue -> .log / _ticks.csv / _trades.csv / _pairs.csv (no I/O in hot loops)
```

HTTP: one persistent `http.client.HTTPConnection` per thread (keep-alive, no
`requests` overhead), reconnect on error, JSON via `json.loads`. Call budget:
4 tickers x 30 book/s = 120 + orders 12 + slow lane 16 = ~150 calls/s < 170.
`Book depth` default 20 orders/side (RIT returns individual orders; thick levels
mean 20 orders may be one or two price levels — enough for touch, size at touch
and our own queue position).

Order rate: per-ticker token bucket (`Orders/s per ticker`, default 8; cancels
count). The server's `X-Wait-Until` header and 429 `wait` are honoured. Each
reconcile pass computes needed actions, sorts by priority, and spends only the
budget available without blocking:
1. risk: cancel orders that are too aggressive vs fair / breach caps / wrong side in trend guard
2. race: post at a new best price when nothing of ours rests there
3. refill: top up size at the touch, layers behind the touch

## 4. Quoting per ticker

Inputs per loop: best bid/ask and sizes (own orders excluded), our queue position
(shares ahead of our order at its price), mid history, position estimate, leans.

```
mid    = (bb + ba)/2
micro  = (bb*ask_sz + ba*bid_sz)/(bid_sz+ask_sz)
fair   = mid + micro_w*(micro - mid) + lean_trend + lean_factor + lean_pairs
vol    = range of mid over Vol window (s)
half   = max(Min spread/2, Vol mult * vol)
skew   = Skew$ * clamp(pos/Max inv, -1.5, 1.5)   (price skew only if spread >= 2 ticks;
                                                   in 1-tick markets skew is by size)
r      = fair - skew
bid_px = min(r - half, bb + improve, ba - tick);   ask_px = max(r + half, ba - improve, bb + tick)
improve = 1 tick if spread >= Improve if spread (else join)
```
Sizes: adding side `base * (1 - |pos|/Max inv)` (0 at Max inv), reducing side
`base + Reduce boost*|pos|`, each chunked at max_trade_size (read from server),
layers 1 tick further out (`Layers`, default 2) sized `Layer frac` of the top.
Totals per side capped by room: `min(Max inv - |pos|, gross room share)`.

Trend guard (new): `tr = EWMA return over Trend window / vol_per_window`.
- |tr| > `Trend pull` : pull the side the trend runs over (asks in an up-move,
  bids in a down-move) unless that side reduces inventory; the reducing side joins
  the touch with boosted size. Fair leans with the trend (`Drift lean`).
- jump guard: if |mid move in 1 s| > `Jump cents`, cancel both adding-side orders and
  stop adding for `Jump cooldown` s (Alexander's volatility guard).
- hard reduce: |pos| > `Hard reduce` -> marketable limit at the far side for
  (|pos| - Max inv), max one in flight, respects far-side size.

Queue discipline (new, the main speed edge in a thick 1-tick book):
- an order AT the touch is never cancelled for a price reason; only for risk/caps
- when the touch moves to a new better price and nothing of ours is there -> post
  immediately (race), keep the old order as a layer if within `Requote lag ticks`
- when our order is > `Requote lag ticks` behind, cancel (dead weight) only if
  budget allows and nothing urgent is pending
- replace cadence per side >= `Requote min sec` unless urgent

## 5. Cross-sectional signals (analytics thread)

- 1 s mid returns per ticker, rolling 90 s window.
- Factor: equal-weight index return over the last `Factor window` (1-2 s). Lean
  for ticker i: `Factor lean * beta_i * (index move not yet in i)`. Protects asks
  when the other three jump up.
- Pairs: for every pair (i,j), rolling correlation of 1 s returns (window 120 s);
  if corr > `Pair min corr` (0.6) compute beta by OLS on log mids over the window,
  spread s = ln P_i - beta ln P_j, z = (s - mean)/std. Lean: fair_i -=
  `Pair lean` * z * std_s * P_i, fair_j += beta-scaled opposite. Sizes: on the
  rich leg bigger asks/smaller bids, cheap leg the reverse (`Pair size tilt`).
  Entry at |z| > `Pair z enter`, released below `Pair z exit`.
- Optional `Pair take` (default 0): if |z| > `Pair z take` and expected reversion
  > spread + 2 x fee, send marketable limits on both legs, small size, max one
  pair position at a time, unwound when |z| < exit. Off by default: taking pays
  the fee and crosses the spread; the lean already harvests reversion passively.
- Signals are published as per-ticker lean values + a pairs csv row every second.

## 6. Limits, group mode, end of case

- `/limits` is authoritative (account-wide). Per ticker cap `Max inv` (default
  gross_limit/N x 0.3) and account gross guard: sum|pos| + sum open adding qty
  <= gross_limit - `Limit buffer` (2,000). Beyond it adding sizes scale down to 0
  across all tickers; reducing continues.
- Group mode (`Shared account` = 1): orphan sweep OFF (teammates' orders), never
  flatten more than our own tracked fills imply unless `Flatten server position`
  is set. Each teammate runs disjoint `Tickers`.
- End: `End flat secs` before the end (seconds, from measured tick length) stop
  adding; reducing side joins the touch; `End cross secs` before the end cross
  with marketable limits at the far side (walk 1 tick deeper every second until
  flat). No MARKET orders anywhere. Status != ACTIVE -> cancel all, wait.

## 7. Logs and cockpit

Same files as v1 plus a `ticker` column; `_pairs.csv`; per-ticker Live block in
Excel; params in `Control` sheet (global) with per-ticker override rows
(`Enabled <T>`, `Quote size <T>`, `Max inv <T>`), mirrored to `params.json` so the
bot runs identically on a machine without Excel.

## 8. Testing

`mock_rit_multi.py` (4 correlated tickers, trends, jumps, rivals at the touch,
per-ticker order rate limits, account-wide limits/fines, no market orders) ->
scenario runs: calm, trend up/down, jump, pair divergence, rival race, rate-limit
pressure, case stop/restart, end-of-case flatten. Pass criteria: no limit
breach, flat at close, positive edge per share, no exceptions, order budget never
exceeded, loop >= 20 Hz per ticker.
