# MFE 230X — Homework 1: ITCH and EBS descriptive statistics

*Draft for the bCourses submission. Every number below is produced by
`ps1/notebooks/currencies.ipynb` or `ps1/notebooks/equities.ipynb`, both executed
in the repo; figures are in `ps1/figures/` and the two summary tables in
`ps1/figures/summary_*.csv`.*

## 1. Data

| panel | source | instruments | session |
|---|---|---|---|
| equities | Nasdaq TotalView-ITCH via Databento MBO (`XNAS.ITCH`) | NVDA (mega cap, ~\$5.5T), GPRO (micro cap, ~\$0.27B; +40% on the day) | 2026-09-02, 09:30–16:00 ET |
| currencies | EBS level-update and deal feeds (OneTick export) | EUR/USD, USD/JPY, EUR/JPY | 2012-01-25, 09:30–16:00 ET |

The two panels are on different days because the EBS file supplied with the course is
from 2012 while the ITCH data was downloaded fresh; every statistic is computed by
the same code (`hfstats.py`, `plots.py`) from the same reconstructed objects — a
signed trade tape, a BBO series and an end-of-second book grid — so the two panels
are measured identically even though they are not contemporaneous.

**ITCH.** Every add, cancel, fill and trade message is replayed into a full limit
order book. Databento snapshots the book only at UTC midnight, so the request runs
from there (20:00 ET the evening before, when Nasdaq's book is empty) to the close and
the pre-market is replayed before the first 09:30 snapshot; only the regular session
is recorded. This matters: resting pre-open orders are not re-added after the cross,
and a book started empty at 09:30 misses 37% of GPRO's resting depth within \$0.50 of
the mid at 09:31 (7% for NVDA), still 22% at 10:00, and 8% on average over the day;
touch depth and spreads are essentially unaffected. With the full-day replay every
session cancel references an order that was seen added. An execution emits three records (`T`, `F`, `C`); only `A` and `C` move
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
both sampling frequencies. For (g) the pooled row is one regression over every print
in the session with Newey–West standard errors (20 lags), which weights busy minutes
by their prints rather than equally; it is the number to quote, with its t-statistic. Levels are in each panel's own units — dollars and shares
for the stocks, USD millions and millions of base currency for the pairs — so only
the relative rows (bps, autocorrelations) compare across panels.

| part / variable | EUR/USD | USD/JPY | EUR/JPY | NVDA | GPRO |
|---|---:|---:|---:|---:|---:|
| (a) dollar volume / min | 79.36 | 24.80 | 7.2682 | 13,103,791 | 72,105 |
| (b) trades / min | 37.27 | 15.49 | 3.9538 | 891.75 | 75.16 |
| (b) orders or book messages / min | 918.49 | 459.70 | 460.11 | 25,480 | 866.36 |
| (c) open | 1.2974 | 78.05 | 101.24 | 218.87 | 1.220 |
| (c) high | 1.3121 | 78.29 | 101.95 | 227.94 | 1.790 |
| (c) low | 1.2947 | 77.56 | 101.20 | 218.47 | 1.180 |
| (c) close | 1.3113 | 77.73 | 101.92 | 224.40 | 1.705 |
| (d) VWAP, session | 1.3032 | 77.93 | 101.52 | 224.35 | 1.5191 |
| (e) spread | 0.0001 | 0.0079 | 0.0187 | 0.0170 | 0.0100 |
| (e) spread (bps) | 0.8548 | 1.0130 | 1.8415 | 0.7574 | 70.66 |
| (e) BBO depth | 4.0511 | 4.4141 | 3.4630 | 462.35 | 50,682 |
| (f) depth within 2x avg spread | 15.62 | 13.76 | 14.07 | 718.40 | 50,682 |
| (f) share of time band empty (%) | 0.4786 | 0.8504 | 0.1282 | 1.0470 | 0.0000 |
| (g) impact lambda (bps), mean of minutes | 0.2215 | 0.2229 | 0.5253 | 0.3339 | 22.71 |
| (g) minutes identified | 352.00 | 209.00 | 41.00 | 390.00 | 252.00 |
| (g) impact lambda (bps), pooled HAC | 0.2672 | 0.2758 | 0.5743 | 0.4608 | 26.65 |
| (g) pooled t-stat | 17.98 | 14.78 | 12.27 | 27.62 | 27.54 |
| (j) RV mid 1s (bps^2) | 2,318 | 1,506 | 2,210 | 32,435 | 2,827,123 |
| (j) RV trade 1s (bps^2) | 2,927 | 2,106 | 2,347 | 33,019 | 2,864,420 |
| (j) RV mid 1min (bps^2) | 2,858 | 2,860 | 2,317 | 27,306 | 2,545,496 |
| (j) RV trade 1min (bps^2) | 2,960 | 2,916 | 2,400 | 27,280 | 2,683,408 |
| (k) AC1 mid 1s | 0.0227 | 0.0244 | 0.0373 | 0.0250 | -0.0215 |
| (k) AC1 trade 1s | -0.0211 | -0.0132 | 0.0147 | 0.0174 | -0.0224 |
| (k) AC1 mid 1min | 0.0053 | -0.0003 | -0.1747 | -0.1183 | 0.0384 |
| (k) AC1 trade 1min | -0.0052 | -0.0034 | -0.1487 | -0.1205 | 0.0309 |

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

And from the equity panel:

5. **`eq_spread_bps_5min.png` — quoted spread per 5-minute interval.** NVDA opens at
   1.8 bps and is below 0.8 within twenty minutes, ending the day at 0.53 with no rise
   into the close. GPRO's line falls from 85 to 59 bps, but its spread is one cent in
   99.6% of seconds: the line is the inverse of a price that rose 40%.
6. **`eq_depth_5min.png` — BBO depth per 5-minute interval.** NVDA's touch holds
   200 shares in the first five minutes, 360 at noon and 1,410 in the last five: the
   closing auction attracts resting liquidity rather than draining it.
7. **`eq_variance_bps2_30min.png` — variance of 1-minute transaction returns per half
   hour.** NVDA's opening half hour is 237 bps² against 15 at 15:00, sixteen times,
   with only a slight uptick at the close. GPRO peaks at 13:30–14:00, in its rally, not
   at the open.
8. **`eq_ac1_30min.png` — lag-1 autocorrelation per half hour.** Negative for NVDA in
   nine of thirteen half hours, −0.51 in 11:30–12:00; positive for GPRO in most of the
   half hours it spent trending.

The rest of the set: `fx_depth_5min.png`, `fx_ac1_30min.png`, `fx_acf_1s.png`,
`eq_lambda_bps_5min.png`, `eq_acf_1s.png`, `eq_acf_1min.png`.

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

**Size sets the equity spread, but the tick sets GPRO's.** NVDA quotes 1.7 cents
wide (0.76 bps) and GPRO 1.0 cent (71 bps), a 93x gap in relative terms that is almost
entirely the one-cent minimum: GPRO sits at one tick in 99.6% of seconds, so its
spread in basis points is the inverse of its price and its "narrowing" from 85 to
59 bps through the day is the rally. NVDA is one tick wide 41% of the time, so its
spread carries information. Depth is where the tick-constraint prediction bites:
GPRO rests 50,700 shares at the touch against NVDA's 462, which in dollars is \$73k
against \$104k — the same order of magnitude, not "far thinner". A one-cent gate on a
\$1.50 stock is wide enough that the queue behind it is long, and for GPRO the part (f)
band never reaches past the touch. Volume tells the size story: \$5.1bn against
\$28mm on the day, 892 against 75 prints a minute, 14 displayed orders per trade
against 5, a cancel-to-add ratio near one for both, and a NVDA tape that is 74% odd
lots with a median print of 25 shares.

**Impact is larger for GPRO in basis points and smaller in cents.** Pooled over the
session with HAC errors, λ is 0.46 bps for NVDA and 26.7 bps for GPRO (t = 28 for
both); the per-minute means are 0.33 and 22.7. In price units that is one cent for
NVDA — a signed trade moves the mid about one tick, 0.6 of its spread, within five
seconds — and 0.4 cents for GPRO, under half a tick. GPRO's mid moves only in
half-cent steps, so 26.7 bps is the probability a trade shifts the touch times the
29–41 bps a half-tick is worth at its price. The currencies sit in between in relative
terms: 0.27–0.57 bps of impact on spreads of 0.9–1.8 bps, so a print moves the EBS
mid by a third of the spread where a NVDA print moves it by 0.6 and a GPRO print by
0.4 — the three markets are closer in impact-to-spread than any level comparison
suggests.

**Bounce is weak in both stocks; discreteness and a one-minute transitory component
show up instead.** For NVDA, transaction RV at one second exceeds midquote RV by only
1.8% (33,019 against 32,435 bps²) and the lag-1 autocorrelation of one-second returns
is *positive* for both series (+0.017 trade, +0.025 mid, Box–Pierce p < 0.01): with
fifteen mostly odd-lot prints a second inside a 1.7-cent spread, the last trade of a
second is essentially the touch, and the ACF is picking up gradual quote adjustment,
not Roll's bounce. The volatility signature is two-sided — the midquote RV rises from
32,400 bps² at one second to 35,000 at thirty seconds (21% of one-second mid returns
are exactly zero, the same discreteness bias as the EBS mids), then falls to 27,300
at one minute and 22,300 at five as a transitory component averages out. That
component is visible directly: one-minute returns have a lag-1 autocorrelation of
−0.12 for the mid and for trades (p = 0.02), negative in nine of thirteen half hours
and −0.51 in 11:30–12:00, and since it is in the *mid* it is not bid–ask bounce.
GPRO is the discreteness case taken to its limit — 97% of one-second midquote returns
and 94% of transaction returns are zero — yet its RV falls monotonically from 2.83
million bps² at one second to 2.40 million at thirty seconds, its one-second returns
are negatively autocorrelated in both series (−0.02, p < 0.001), and trade RV exceeds
mid RV at every frequency: the standard bounce signature, faint but present, from a
print alternating between a bid and an ask one cent apart. Its one-minute returns
show no autocorrelation (p = 0.45). Session RV is 1.8% for NVDA and 16.8% for GPRO.

**The equity U is half a U.** NVDA's opening half hour is the extreme of everything:
spread 1.8 bps in the first five minutes against 0.66 at noon, touch depth 200 shares
against 360, the variance of one-minute returns 237 bps² against 15 in the 15:00 half
hour, and 160k add messages in the 09:30 minute against a median of 9,900. But there
is no second arm. Spread and impact fall monotonically to their lows in the last five
minutes (0.53 bps and 0.24 bps) while depth rises to 1,410 shares and the busiest
minute of the day is 15:59 with 675k shares: the closing auction attracts resting
liquidity and volume without widening anything, the opposite of the open. The classic
pattern here is a U in volume and an L in cost. GPRO's day is shaped by its own news
rather than the clock: half-hour variance peaks at 12,900 bps² in 13:30–14:00 during
the rally, not at the open (10,600), the largest one-minute move (+4.3%) is at 13:55,
and while it trends the lag-1 autocorrelation of its one-minute returns is positive
in most half hours (+0.22, +0.16, +0.20) — the same momentum-while-trending sign
EUR/USD shows going into the FOMC release. Set beside the currency day, the contrast
is the one the framework predicts: the market with an opening auction and an
overnight close has its cost extreme at the open; the 24-hour market has none, and
both markets have their variance extremes wherever the information arrived.

## 5. Status

| item | state |
|---|---|
| currency parts (a)–(l), summary table, figures, observations | done, reproducible from the repo |
| equity parts (a)–(k), summary table, figures, observations | done, reproducible from the repo given a Databento key (`README.md`); the download was free on a standard account |
| combined summary table | section 2 above; the two panels' `ps1/figures/summary_*.csv` stack row for row |
| Datalore share | not done |
