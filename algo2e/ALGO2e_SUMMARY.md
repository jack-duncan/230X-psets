# ALGO2e market maker — summary (Hashim, 2026-10-01)

Bot: `algo2e_mm.py`. One Python process per ticker (CNR, RY, AC), limit orders only,
Excel cockpit optional. Built from the single-stock ALGO2 bot (`legacy/algo2_mm.py`)
after one live ALGO2e practice run and a read of the 2022 grading data for this case.

## The case, as the server actually runs it

| | CNR | RY | AC |
|---|---|---|---|
| Price | ~$161 | ~$102 | ~$23 |
| Quoted spread (median / p95) | 1c / 1c | 4c / 38c, widest late in the case | 1c / 2c |
| Touch size (median) | 2,000 | 1,500 | 1,500 |
| Mid move per 10 s (std) | 1.5c | 17c | 7c |
| Taker fee / maker rebate (server) | 0.27c / +0.50c | **−0.14c (taker is paid) / −0.34c (maker pays)** | 0.15c / +0.26c |

Limits: 25,000 gross and net per account, enforced by rejection. Server max order
50,000, 500 orders/s, reads effectively unlimited. Practice cases ran 300 ticks at
~10 s per tick. Cross-correlation between the three names is at most 0.22 over 15 s,
so there is no pairs edge; RY's own mean reversion is the opportunity.

The 2022 Rotman grading sheet for this exact case (46 students, 6 runs): every top
finisher traded RY only, 4–8M shares per run, paying more in taker fees than it
earned in rebates (crossing on RY, where taking is paid). Three-stock passive
quoters were profitable a third of the time. Under 100k shares = minimum grade. One
losing run cuts the final grade sharply, so avoiding a loss beats chasing a peak.

## Live practice run (ticks 259–289, ~5 min of quoting, first build)

| | CNR | RY | AC |
|---|---|---|---|
| Fills / shares | 25 / 27k | 308 / 680k | 62 / 55k |
| Realized | −$119 | **+$35,118** | −$1,637 |
| Capture vs mid at fill | +0.35c/sh | +6.6c/sh (passive 9.0c) | −0.49c/sh |
| Adverse selection 10 s later | −0.12c | −1.5c (passive −0.2c) | −3.6c |

Account NLV +$33,363. RY passive quotes earned 9c per share with almost no adverse
selection: the price swings 10–20c per second through resting orders and reverts.
Two things cost money: (1) RY hard-reduce crosses lost $7.2k because the position
estimate lagged the server by up to 13k shares and two crosses traded against our
own bids; inventory reached +15k / −13k against a 3k cap. (2) AC quotes rested 3c
behind the touch in a 1c book and only filled when swept. Two things were left on
the table: 98% of RY fills consumed the whole order (demand exceeded our 2,000
size), and the bots went flat at tick 290 because the end timer counted ticks, not
seconds, missing 100 s of RY's widest regime.

## What the v2 bot does

Per ticker, every loop (~60 Hz live): read the book with own orders excluded, update
the local fill record from the open/filled/cancelled order lists, then:

- **Fair and spread.** `fair = mid + 0.3·(microprice − mid) + 0.3·drift`; half-spread
  `max(Min spread/2, Vol mult × mid range over 3 s)`. Bid joins `min(fair − half,
  best bid + improve)`, ask symmetric; `improve` = 1 tick when the market spread
  allows. A resting order is kept unless it is too aggressive, too far behind, or
  over the size cap, so queue position is preserved.
- **Inventory by size.** Adding side shrinks linearly to zero at `Max inv`; reducing
  side grows with inventory but a full sweep can flip us by at most half a quote.
  Beyond `Hard reduce` the excess is crossed with a marketable limit sized to the
  visible touch, after our own opposite side has been pulled; one cross in flight.
- **Position safety.** Fills in the order response are booked instantly; orders with
  an unconfirmed cancel still count as exposure; sizing uses whichever of server
  position (polled every 100 ms) and local estimate is larger in magnitude, so a
  stale number can only make us reduce early. `/limits` gross and net stop adding
  on every ticker at 23,000.
- **Taking on RY (`Take edge`).** If the ask sits 6c below a 5 s EWMA fair value
  while the bid has not moved (an ask outlier), buy it with a marketable limit;
  symmetric for the bid. RY pays 0.14c/share to take. Adding takes stop at the cap.
- **End of case in seconds** (tick length measured live): 12 s before the end the
  adding side is pulled and the reducing side keeps quoting passively; 4 s before
  the end anything left is flattened with limits one tick through the far side.
  No market orders anywhere. The process stops by itself 60 s after the case ends.

Per-ticker defaults (`TICKER_OVERRIDES`): RY quote 4,000, max inventory 7,000, hard
reduce 10,000, gross cap 12,000, min spread 3c, vol-scaled half-spread, take edge
6c; CNR and AC quote 1,000, max inventory 2,000, join the touch (no minimum
half-spread). The three caps sum below the 25k limit.

## Running it

Edit the top of `algo2e_mm.py`: `API_KEY`, `PORT`, optionally `API_READS_PER_SEC`
(160) and `API_WRITES_PER_SEC` (0 = server value), `TICKERS`. Run
`python algo2e_mm.py` with no arguments; it starts one console per ticker. Ctrl+C in
a window cancels that ticker and stops it. Each process writes `logs/algo2e_<T>_<stamp>`
`.log`, `_ticks.csv` (one row per loop), `_trades.csv` (one row per fill) and
`_orders.csv`. With xlwings + Excel it also opens `ALGO2e_cockpit_<T>.xlsx` whose
yellow cells are live parameters (`Enabled`, sizes, caps, `Flatten now`, `Panic`).

Offline test: `python mock_rit.py --port 9999` (single-asset mock of the RIT REST
API: listing lag, pre-match POST snapshot, 404 on filled orders, 429s, fees) then
run the bot with `TICKERS = ["ALGO"]`.

## Verification

Compiles; eight unit tests of the new guards; four mock runs including a full case
with end mode and auto-stop; a stress run forcing hard-reduce crosses; a mock with an
instrumented matching engine reported zero self-trades across 23 crosses. The RY
take logic has not yet been exercised on the live server; watch `TAKE` lines.

## Logs

`logs/2026-10-01_practice/` holds the live practice run (first build) that the
numbers above come from.
