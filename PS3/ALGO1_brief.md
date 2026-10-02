# ALGO1 (Algorithmic Arbitrage): work summary

## 1. The case
One stock, CRZY, trades on two exchanges: `CRZY_M` (Main) and `CRZY_A` (Alternate). Positions net across them. Limits: 10,000 shares per order and 25,000 gross/net, with no commissions. The intended edge is the *crossed market*, where one exchange's bid is above the other's ask. When that happens you buy on the cheap exchange, sell on the expensive one, and end up flat.

## 2. Original design (`algo1_arbitrage.py`)
The bot runs two strategies in one loop, controlled live from an Excel cockpit (`ALGO1_cockpit.xlsx`: Control / Live / Trades sheets).

- **ARB.** Every loop reads both order books with your own orders excluded.
  - `size_cross()` walks the cheap exchange's asks and the expensive exchange's bids level by level. It takes size only while every share clears `Min edge` after `2 x Cost/share` (discussion Q1).
  - Size is capped at `ARB max qty` and at the remaining position room.
  - It sends two **marketable LIMIT** orders at the prices it saw, never MARKET, so a quote that has vanished costs nothing. It then cancels whatever did not fill.
  - Any one-legged leftover (the *residual*) is closed straight away. After a losing or one-legged arb, ARB pauses.
- **MM.** The bot posts a bid and an ask on both exchanges around the consolidated mid.
  - Half-spread = `max(MM half spread, MM vol mult x mid range over 3 s, MM min spread / 2)`.
  - Inventory skew moves both quotes by `0.03$ per 1,000 shares`.
  - Quotes are clamped so they never cross the other exchange.
  - Beyond `MM max inv`, the side that would add inventory is pulled.
- **Plumbing.**
  - Throttle: a sliding-window cap of 80 calls/s with 429 retry.
  - Fast lane (books every loop) and slow lane (`/case`, `/securities`, `/limits`, `/orders` every 0.25 s).
  - Each run writes four logs: `.log`, `_ticks.csv`, `_trades.csv`, `_orders.csv`.
  - `Flatten now` and `Panic` controls.

## 3. Run 1: original code (19:22, ticks 2–112)
**Result: NLV +$3,498.** There were 26 ARB attempts and only 1 completed (+$60). MM made 39 recorded fills (8.5k shares).

Three bugs showed up in the log:

1. **The POST response is a pre-match snapshot.** `GET /orders/{id}` returns 404 for orders that have filled, and new orders appear in `GET /orders` 0.1–0.6 s late.
   - Example at t=21: both ARB legs had actually filled. The re-read returned 404, so the code assumed "sold 0" and "unwound" a residual that did not exist, with another limit sell plus a 2,000-share market sell.
   - That turned a clean +$60 arb into a 2,000-share short. Most "ARB MISS" lines were this pattern.
2. **Quotes the bot lost track of.** 401 MM quotes dropped out of `/orders` (median 0.22 s after posting) and the bot stopped tracking them.
   - It re-posted duplicates, and the forgotten quotes filled later without the bot seeing it.
   - 23 of the 29 orders still open at the end were such forgotten quotes, and inventory blew through `MM max inv`.
3. **Position double-count.** `/securities` reports the *aggregated* CRZY position identically on both tickers, and the code added them together, so "net" was 2x the truth.
   - The swings the log shows (−8,000 → +12,360 → −17,440) were really about −4k → +6k → −8.7k.

The +$3.5k came from the price direction, not from the strategy working.

**Fix patch 1.**
- `_settle()` cancels each leg, then polls OPEN / TRANSACTED / CANCELLED (`find_order`, `done_orders`) until the order is final.
- `sync_fills()` looks missing quotes up in the TRANSACTED/CANCELLED lists and only gives up after 6 s. Cancelled quotes go into a `settling` list so late partial fills are still counted, and fill prices come from VWAP deltas.
- The orphan sweep cancels any OPEN order of yours that nothing is tracking.
- The residual is taken from the position change, and the unwind only market-orders what the position confirms.
- New `Hard flat |net|` guard (5,000).
- `cancel_all` is verified at shutdown.
- `ARB pause` 5 s → 2 s.

## 4. Run 2: patch 1 (20:04, ticks 0–77)
**Result: NLV −$3,555.**

- The double-count (bug 3, not yet fixed) combined with the new hard-flat guard:
  - The bot saw 2x its real position, so the guard market-sold 2x and flipped the position to the other side.
  - Two seconds later it saw the flipped position, again doubled, and flipped back.
  - This happened **18 times**, each crossing the spread on 5–9k shares. That is essentially the whole loss.
- ARB went 0 for 7. The residual computed from the position change was also doubled, and MM fills landing in the same window polluted it (e.g. +8,000 when the orders said +2,000).
- MM by itself was slightly positive: +$319 marked to mid 3 s after each fill.
- The patch-1 mechanics worked: no 404 confusion and no lost quotes.

**Fix patch 2.**
- `net_position()` reads one ticker, and it is now the single source for net, room, skew, flatten and the hard flat.
- The ARB residual comes from the order fills (bought − sold). The position is only a fallback when the server never listed a leg.
- The unwind uses the fill count when it is known.

## 5. Run 3: patch 2, the final code (20:15, ticks 157–299)
This was the same case as run 2. The bot restarted at tick 157 and ran until the simulation stopped.

- **Mechanics: clean.** 0 accounting mismatches, 0 hard flats, 5 leaked-order sweeps. The true position stayed between −2.5k and +4.7k.
- **Result: about −$1.6k.** NLV went from about −$12.07k at the start (that is where the realized P&L stood) to −$13.68k at tick 285.
- **Where it came from:**
  - **ARB: 0 of 17 completed, 7 one-legged.** The crossed quote was always gone before our order arrived, so only the normal-side leg filled. Each one was then unwound at a 2–6¢ loss, about −$0.5k in total.
  - **MM spread capture: about +$3.8k** (268 fills, 265k shares, +1.2¢/share marked to mid 3 s later). The quoting itself made money.
  - **MM inventory: about −$5k** (the remainder). The price fell from 12.84 to 9.93 (−23%) while the book was mostly long; 0.03$/1k skew does not lean against a trend like that.
- **End.** At tick 299 the server rejected orders ("Simulation must be running") and the bot waited. When the RIT client closed it logged a connection-refused error. That is harmless.

## 6. Corrections to numbers given during the session
- RIT also reports **realized P&L** aggregated on both tickers, so the `real=` in the log and the Excel "Realized" cell are **doubled**. Trader NLV is the reliable figure.
  - Run 3 lost about **$1.6k, not $3k**.
  - The unexplained loss between run 2's end and run 3's start is about **$8.5k, not $20.6k**. It is still unexplained: there is no log for ticks 77–157, so it was either manual trading or another copy of the script.
- Run 1's position swings were half what the log shows (see §3).

## 7. Lessons
1. **Never trust the POST response or a single order lookup.** Reconcile against all three status lists, with a grace period.
2. **Know what the server aggregates.** Position and realized P&L are both per-security-group, not per-ticker.
3. **ARB was not winnable at this latency.** Crosses lasted about 0.2 s (median), against a 0.4–0.9 s round trip for our two sequential legs, so we only ever got the leg nobody wanted (discussion Q3). Depth was never the constraint: top-of-book was usually several thousand shares (Q4). The one-legged unwind logic is exactly what Q5's "exchange halted" scenario needs.
4. **MM edge is real, but inventory risk dominates in a trend.** It needs a much stronger skew or a trend circuit-breaker. The same lesson repeated in ALGO2.
5. **Still open in the final code.**
   - Realized is still summed (doubled) in the tick CSV, the Excel cockpit and the log line.
   - `net_position()` adds the two tickers if they momentarily differ.
   - ARB legs go out one after the other instead of in parallel.
   - `ORDER_VISIBLE_SEC` is defined but unused.
