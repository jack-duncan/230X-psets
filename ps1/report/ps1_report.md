# MFE 230X — Homework 1: ITCH and EBS descriptive statistics

*Draft for the bCourses submission. The currency half is complete and every number
below is produced by `ps1/notebooks/currencies.ipynb`; the equity half is written
as a template, to be filled once `equities.ipynb` has been run against the Databento
download (see "Status" at the end). Figures are in `ps1/figures/`.*

## 1. Data

| panel | source | instruments | session |
|---|---|---|---|
| equities | Nasdaq TotalView-ITCH via Databento MBO (`XNAS.ITCH`) | NVDA (mega cap, ~\$5T), GPRO (micro cap, ~\$0.2B) | 2026-09-02, 09:30–16:00 ET |
| currencies | EBS level-update and deal feeds (OneTick export) | EUR/USD, USD/JPY, EUR/JPY | 2012-01-25, 09:30–16:00 ET |

The two panels are on different days because the EBS file supplied with the course is
from 2012 while the ITCH data was downloaded fresh; every statistic is computed by
the same code (`hfstats.py`, `plots.py`) from the same reconstructed objects — a
signed trade tape, a BBO series and an end-of-second book grid — so the two panels
are measured identically even though they are not contemporaneous.

**ITCH.** Every add, cancel, fill and trade message is replayed into a full limit
order book. An execution emits three records (`T`, `F`, `C`); only `A` and `C` move
the book and only `T` counts as a trade, so nothing is double-counted. `T.side` is the
aggressor for lit executions; non-displayed executions and the opening cross arrive
unsigned and are signed with the quote rule. The opening cross is dropped from the
trade tape (one unsigned print ~30x a normal trade, which would otherwise dominate the
first minute of every statistic). Order counts in part (b) are displayed add-order
messages, as the FAQ asks — hidden orders have no add message and are seen only when
they execute.

**EBS.** The book feed carries price-level updates, not orders (`SIZE = 0` deletes a
level), and shows ten levels a side. Timestamps are stamped to 100 ms and many
updates share one, so a timestamp's updates are applied as one batch and the book is
recorded once at the end of it — applied one at a time they produce crossed books the
feed never showed. The deal feed lags the book: matched at its own timestamp only 47%
of EUR/USD prints sit at or outside the touch, matched to the quote 200 ms earlier
85% do, and 200 ms is the optimum on all three pairs independently, so the quote
"prevailing" at a print is taken 200 ms before it. Trade sizes use the `SIZE` field
(the per-print fill; `TOTAL_SIZE` is larger on 18% of prints and appears to be the
size of the whole deal a print belongs to). Sizes are millions of base currency;
dollar volume converts EUR/JPY through the EUR/USD mid prevailing at the print. The
file has no timezone, but the clock identifies itself: EUR/USD drops 20 bps in the
14:00–14:01 minute exactly, which is when the FOMC's first-ever rate projections were
released on 2012-01-25, so the stamps are Eastern time.

## 2. Summary statistics

Every part at the daily level, one column per instrument. Parts (a)–(g) are the mean
of their per-minute values (the "average minute"); (h)–(k) are whole-session figures at
both sampling frequencies. Levels are in each panel's own units — dollars and shares
for the stocks, USD millions and millions of base currency for the pairs — so only
the relative rows (bps, autocorrelations) compare across panels.

| part / variable | EUR/USD | USD/JPY | EUR/JPY | NVDA | GPRO |
|---|---:|---:|---:|---:|---:|
| (a) dollar volume / min | 79.36 | 24.80 | 7.2682 | – | – |
| (b) trades / min | 37.27 | 15.49 | 3.9538 | – | – |
| (b) orders or book messages / min | 918.49 | 459.70 | 460.11 | – | – |
| (c) open | 1.2974 | 78.05 | 101.24 | – | – |
| (c) high | 1.3121 | 78.29 | 101.95 | – | – |
| (c) low | 1.2947 | 77.56 | 101.20 | – | – |
| (c) close | 1.3113 | 77.73 | 101.92 | – | – |
| (d) VWAP, session | 1.3032 | 77.93 | 101.52 | – | – |
| (e) spread | 0.0001 | 0.0079 | 0.0187 | – | – |
| (e) spread (bps) | 0.8548 | 1.0130 | 1.8415 | – | – |
| (e) BBO depth | 4.0511 | 4.4141 | 3.4630 | – | – |
| (f) depth within 2x avg spread | 15.62 | 13.76 | 14.07 | – | – |
| (f) share of time band empty (%) | 0.4786 | 0.8504 | 0.1282 | – | – |
| (g) impact lambda (bps), mean of minutes | 0.2215 | 0.2229 | 0.5253 | – | – |
| (g) minutes identified | 352.00 | 209.00 | 41.00 | – | – |
| (j) RV mid 1s (bps^2) | 2,318 | 1,506 | 2,210 | – | – |
| (j) RV trade 1s (bps^2) | 2,927 | 2,106 | 2,347 | – | – |
| (j) RV mid 1min (bps^2) | 2,858 | 2,860 | 2,317 | – | – |
| (j) RV trade 1min (bps^2) | 2,960 | 2,916 | 2,400 | – | – |
| (k) AC1 mid 1s | 0.0227 | 0.0244 | 0.0373 | – | – |
| (k) AC1 trade 1s | -0.0211 | -0.0132 | 0.0147 | – | – |
| (k) AC1 mid 1min | 0.0053 | -0.0003 | -0.1747 | – | – |
| (k) AC1 trade 1min | -0.0052 | -0.0034 | -0.1487 | – | – |

*NVDA / GPRO columns: `ps1/figures/summary_equities.csv`, written when `equities.ipynb` runs.*

The full per-minute distributions (count, mean, sd, quartiles) of every variable are
in the notebooks; the currency ones are all in `currencies.ipynb`, section by section.

### Part (l) — triangular arbitrage (one-second series)

EUR/JPY can be bought directly or manufactured as EUR/USD × USD/JPY. The synthetic ask
is the product of the two asks and the synthetic bid the product of the two bids;
an opportunity exists when the direct ask is below the synthetic bid ("direct cheap")
or the direct bid is above the synthetic ask ("direct rich"). Quantity is the
thinnest of the three legs, each converted to millions of euro.

| | value |
|---|---|
| seconds with all three quotes live | 23,322 |
| seconds with an opportunity | 91 (0.39%) |
| episodes (runs of consecutive seconds) | 63 |
| duration: mean / median / max | 1.44 s / 1 s / 7 s |
| edge: mean / max | 0.29 bps / 1.78 bps |
| tradable quantity: mean / median | 1.04 / 0.77 mm EUR |
| total gross profit, all legs filled in full | ≈ \$3,100 |
| split | 59 seconds direct-cheap, 32 direct-rich |

Charging each round trip 1 / 2 / 3 / 5 EUR/JPY ticks for brokerage and legging
slippage leaves 59 / 46 / 39 / 14 opportunity seconds and \$2,200 / \$1,600 / \$1,100 /
\$600 of gross profit. Across all live seconds the median deviation is −1.58 bps: the
triangle normally sits *inside* itself by about the width of the cross's own spread
(1.84 bps), and even the largest opening of the day is short of one full spread.

## 3. Figures

All figures are drawn per instrument on a shared clock (the instruments share units
but differ in level by orders of magnitude). Dotted lines on the currency figures mark
the FOMC statement (12:30 ET) and the projections release (14:00 ET).

The four most informative, chosen from the full set:

1. **`fx_spread_bps_5min.png` — quoted spread per 5-minute interval.** The majors are
   tightest late morning and drift wider through the afternoon; the cross is twice as
   wide as either leg all day and spikes to 2.6 bps in the 12:25 interval.
2. **`fx_lambda_bps_5min.png` — 5-second price impact λ per 5-minute interval.**
   Peaks on all three pairs in the 12:25 and 14:00 intervals. EUR/JPY's series has
   gaps: with 1,542 prints in the day, many intervals have too few trades on both
   sides to identify a slope.
3. **`fx_variance_bps2_30min.png` — variance of 1-minute transaction returns per
   half hour.** Five to ten times its mid-morning level in 12:30–13:30 and 14:00–14:30.
4. **`fx_acf_1min.png` — ACF of 1-minute returns to 10 lags.** Flat inside the band
   for both majors; EUR/JPY alone has a lag-1 coefficient of −0.17 (mid) and −0.15
   (trade), five standard errors from zero.

The rest of the set: `fx_depth_5min.png`, `fx_ac1_30min.png`, `fx_acf_1s.png`, and
the equity counterparts `eq_*.png` once that notebook has run.

## 4. Observations

**Liquidity is ordered the way the triangle implies.** EUR/USD quotes at 0.86 bps,
USD/JPY at 1.01, EUR/JPY at 1.84 — the cross costs roughly what its two legs cost
together, which is what it should cost if the only way to make a price in it is to lay
it off through the dollar. The same ordering runs through every measure: EUR/JPY has
the thinnest touch (3.5 mm against 4.1 and 4.4), the shallowest ten-level book (37 mm
against ~50), the largest impact (0.53 bps against 0.22) and the fewest trades
(1,542 against 14,535). The cross is quoted far more than it is traded — 116 book
messages per print against 25–30 for the majors — because a market maker in it must
requote whenever *either* leg moves, an order-handling cost with no revenue attached
that is part of why it is quoted wider.

**Bounce and discreteness pull the variance in opposite directions.** At a fixed
frequency, transaction RV exceeds midquote RV (EUR/USD at 1 s: 2,927 against 2,320
bps²) and 1-second transaction returns are negatively autocorrelated at lag 1 while
midquote returns are positively autocorrelated — the bid–ask bounce of Roll's model
on one side, gradual quote adjustment on the other. But across frequencies the
midquote signature *rises* as sampling coarsens (EUR/USD 2,320 → 2,858 → 3,274 bps²
from 1 s to 1 min to 5 min) instead of falling, because at one second 43% of EUR/USD
and 64% of USD/JPY midquote returns are exactly zero: with a one-pip spread and a
tenth-pip tick the EBS mid usually has not moved. High-frequency RV here is biased
*down* by discreteness rather than up by noise — the opposite of the standard equity
volatility signature — which is worth remembering before quoting any one-second
variance as an estimate of true volatility.

**EUR/JPY's one-minute returns mean-revert, and it is not just non-trading.** Lag-1
autocorrelation is −0.17 for its midquote and −0.15 for its transactions against
zero for both majors; the Box–Pierce test rejects at ten lags (p = 0.02 and 0.01).
Forward-filling is the obvious suspect since the cross prints in only 79% of minutes,
but that cannot explain the *midquote* figure: EUR/JPY requotes 1.5 times a second and
only 2.3% of its one-minute mid returns are zero. What fits is a transitory component
in the cross's quoted mid — with a spread twice as wide and a book a quarter thinner,
where its mid sits inside its spread is itself noisy, and that noise reverses. The
realized variance says the same thing: EUR/JPY's midquote RV *falls* from 2,317 bps²
at one minute to 1,655 at five as the transitory part averages out, while both majors
rise over the same range.

**Triangular arbitrage exists and is about as small as it should be.** Deviations
appear in 0.4% of seconds across 63 episodes, last a median of one second, average
0.29 bps on a median round trip of 0.77 mm EUR, and sum to about \$3,100 for the
session before any cost — on the most generous assumption, that all three legs fill
in full at the quoted price in the same second. The quotes behind an opportunity are
*fresher* than average (median stalest leg 1.4 s against 2.1 s across all seconds),
which is the opposite of what stale-quote artefacts would produce, so these are real
moments of misalignment. But they are smaller than the cross's spread, they close
within a second, and they cluster exactly when prices move fastest — 22 of the 91
seconds in the first half hour and 53 after 13:00. The no-arbitrage relation holds to
within the cost of crossing the cross; what is left is a latency race between the
three books, not a return available to a price-taker.

**The day is announcement-shaped, not U-shaped.** On the majors the spread is
narrowest late in the morning (0.68 bps for EUR/USD at 10:55) and widens through the
afternoon; depth shows no opening trough at all, and the thinnest five-minute touch on
each pair falls mid-session. Impact peaks in the 12:25 and 14:00 intervals and
half-hourly variance is five to ten times its mid-morning level after 12:30. A
24-hour market has no opening auction and no overnight close on this clock, so the
equity U has no reason to appear; what shapes this day is the scheduled release, and
the liquidity measures respond the way the adverse-selection models say they should —
spreads and impact up when information arrives. The lag-1 autocorrelation of
one-minute transaction returns is negative in most half hours (bounce) but flips to
+0.57 for EUR/USD in 12:00–12:30, where the price trends into the release: at
one-minute sampling a fast one-directional move reads as momentum, and the sign says
which of the two effects is winning in that half hour.

**Equities — to be written from the numbers.** The framework predicts: GPRO's spread
one to two orders of magnitude wider in basis points than NVDA's, tick-constrained
from below (one cent on a \$1.54 stock is 65 bps) so that its depth is large in shares
but small in dollars; λ larger for GPRO in basis points, and for NVDA often a fraction
of a tick so that the mid moves only when the touch empties; transaction RV above
midquote RV and negative lag-1 transaction autocorrelation at one second for NVDA,
with GPRO showing the same zero-return pile-up as the EBS mids; and a genuine U in
spread, impact and variance with the open highest, since this market does have an
opening auction and an overnight close. Each of these should be checked against the
tables and either kept with its number or replaced by what the data actually show.

## 5. Status

| item | state |
|---|---|
| currency parts (a)–(l), summary table, figures, observations | done, reproducible from the repo |
| equity parts (a)–(k) code, figures code, summary table code | written, smoke-tested on stand-in data, **not yet run on ITCH** — needs a Databento key (`README.md`) |
| equity observations | template above; write after the run |
| combined summary table | both panels write `ps1/figures/summary_*.csv`; stack the two once the equity one exists |
| Datalore share | not done |
