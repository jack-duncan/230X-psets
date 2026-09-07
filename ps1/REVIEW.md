# PS1 review — 2026-09-07

Review of the ps1 code against the homework and FAQ, with everything the currency
pipeline computes re-run and checked. Changes made in this pass are listed at the
end; everything else is a finding or a recommendation.

## Verdict

The core is in very good shape. The two order-book reconstructions are careful and
the design decisions are the right ones (batch application of same-timestamp EBS
updates, the 200 ms tape lag, dropping the opening cross, `A`/`C`-only book updates on
ITCH, closed-form impact regression), and the currencies notebook runs clean end to
end and reproduces every number in its text. Apart from one real bug (part (d) VWAP on the
currency panel, item 0 below), the gaps were all *deliverables* rather than analysis: no plots of any kind, no report, no equity outputs, and a
summary table for the currencies only. Those are now filled in as far as the data on
hand allows.

## What I checked and found correct

- **Price impact (g).** The closed-form slope matches explicit OLS to four decimals
  on every minute tested. Pooled over the day with HAC errors, EUR/USD λ = 0.267 bps
  (t = 20) against 0.22 as the mean of per-minute slopes — worth quoting both in the
  report; the per-minute mean weights thin minutes equally.
- **EBS book depth.** The "ten levels a side" claim holds: per timestamp the book
  has a median of 10 and a max of 11 levels a side. Mid-batch it transiently reaches
  20 (new levels arrive before the old are deleted), which is exactly why the batch
  application is necessary.
- **Trade sign convention** (`BUY_SELL_FLAG` 1 = buy) is confirmed by the data:
  flag-1 prints sit 0.9 spreads above flag-0 prints against the 200 ms-prior quote.
- **Unused EBS columns** are safe to ignore: `RECORD_TYPE` is always `R`,
  `TICK_STATUS` always 0, `DELETED_TIME` always empty; all sizes are whole millions.
- **Triangular legs and units** — bid/ask assignment for each direction, the USD/JPY
  leg converted to euro, profit in USD via the USD/JPY mid — are all right.
- **Timezone.** The file has none. The FOMC's 14:00 ET projections release lines up
  with a 20 bps EUR/USD drop in the 14:00–14:01 minute exactly, and the 12:30 ET
  statement with a 17 bps jump and a burst of prints at 12:27–12:28 (zero prints in
  12:26 — the pre-release lull). The stamps are Eastern; observation 6 in the notebook
  called this a hypothesis and it is now stated as evidence.
- **Observation numbers** in the currencies text (spreads, depths, message ratios,
  RVs, autocorrelations, arbitrage counts, staleness comparison) all reproduce.

## Findings

### Fixed in this pass

0. **Part (d) VWAP was wrong for the currencies.** `hfstats.vwap_per_minute` divided
   `value` by `size`, and for the pairs `value` is USD notional: USD/JPY's VWAP came
   out as exactly 1.0 every minute and EUR/JPY's as the EUR/USD rate (~1.30) rather
   than ~101. The committed `currencies.ipynb` shows those numbers in its part (d)
   table. The equities were unaffected because there `value = price x size`. Now
   weights by `price x size` directly; the summary table's session VWAP row was
   corrected the same way.
1. **No figures.** The homework asks for three families of plot — 5-minute liquidity
   (spread, depth, impact), 30-minute variance and lag-1 autocorrelation of 1-minute
   transaction returns, and ACFs — and none existed. `plots.py` now draws all of them
   for either panel; the currency figures are in `ps1/figures/` and embedded in the
   notebook.
2. **`equities.py` had drifted from `hfstats.py`.** It carried its own copies of
   every part (a)–(k) function from before `hfstats` was extracted, and the copies had
   fallen behind: `band_depth_per_minute` lacked the `mid.notna()` guard (seconds
   with an empty side counted as zero depth instead of missing) and the bucket clamp,
   and there was no daily OHLC. It now imports `hfstats` like the currencies notebook,
   so both panels are measured by one definition. Smoke-tested by running every cell
   against the FX books disguised as `mbo.Day` objects — 25/25 pass — but **it has not
   been run on ITCH data**, because there is no Databento key or cache in the repo.
3. **`fx.triangular_summary`** labelled the cheap/rich split as "episodes" while
   counting seconds, and measured run lengths on the frame *after* dropping seconds
   with a blank quote, so two episodes separated only by a blank second would merge.
   Both fixed (the counts on this day are unchanged: 63 episodes, 91 seconds).
4. **No cross-panel summary table.** `hfstats.headline` now builds one column per
   instrument covering every part; both notebooks write it to `ps1/figures/summary_*.csv`.
5. **No report.** `ps1/report/ps1_report.md` is a draft with the currency half
   complete and the equity half templated.
6. Observation 6 upgraded with the clock evidence; a new observation 7 covers the
   intraday plots.

### Open — decisions for the group

7. **Equities not run.** Someone with a Databento key needs to run
   `uv run python ps1/notebooks/mbo.py` (it prints the cost before downloading) and
   then execute `equities.ipynb`. The notebook's markdown quotes numbers (22% unsigned
   NVDA prints, 0.3%/0.8% unknown cancels, 671k-share cross) that must have come from
   an earlier run on someone's machine — worth committing the cache-free outputs so
   the repo holds the evidence.
8. **Pre-open book on ITCH.** The download starts at 09:30 and the book is rebuilt
   from empty. The notebook says Nasdaq "re-adds the pre-open book immediately after
   the opening cross"; I could not verify that from the Databento documentation, and
   the usual advice is to request from the start of the ITCH day (04:00 ET) so
   resting pre-open orders are in the book. The 0.3%/0.8% unknown-cancel figures
   suggest the effect on the *touch* is small, but deep resting orders that are never
   cancelled would be invisible all day and would bias the part (f) band depth down.
   Cheap check: compare the depth profile at 09:31 with 10:00; cheap fix: re-download
   from 04:00 if the cost is tolerable.
9. **Two panels, two dates** (2012 vs 2026). Acceptable given the data supplied, but
   the report should say so up front (the draft does).
10. **Trade size field.** `SIZE` (per-print fill) is used, and `TOTAL_SIZE` — larger on
    18% of prints, summing to 31.6 bn against 23.7 bn — is ignored. `SIZE` is the right
    choice for volume if `TOTAL_SIZE` is the size of the whole deal a print belongs to,
    which is what the pattern looks like, but the semantics are not documented
    anywhere in the repo. Worth a line in the report (added).
11. **Part (g) reporting.** The summary quotes the mean of per-minute λ. Consider also
    reporting the pooled full-day regression with HAC standard errors — it is the
    number an economist would quote, and it comes with a t-statistic.
12. **Datalore.** The homework wants the code shared there; nothing in the repo
    addresses it.

### Minor

- `hfstats.realized_variance` annualises with 252 × session RV, which treats the
  6.5-hour session as the whole day's variance. Fine as a label, but say "session" not
  "daily" if it goes in the report.
- `mbo.Day` has no `tick` attribute, so `hfstats.bucket_width` falls back to half a
  cent via `getattr`. Correct for these two stocks; would silently be wrong for a
  sub-penny-quoted instrument. Consider giving `Day` a `tick = 0.01` field.
- `currencies.py` header still says "Four features of this feed"; there are four, fine
  — but the ten-level statement could add "per timestamp" now that it is verified.

## Changes in this pass

| file | change |
|---|---|
| `ps1/notebooks/plots.py` | new — 5-min liquidity, 30-min return variance/AC1, ACF figures |
| `ps1/notebooks/hfstats.py` | VWAP weighting fix; `price_impact(day, freq)` generalisation; `headline(day)` summary column |
| `ps1/notebooks/fx.py` | `triangular_summary` label and run-length fixes |
| `ps1/notebooks/currencies.py` / `.ipynb` | plots section, headline table, observations 6–7; re-executed |
| `ps1/notebooks/equities.py` / `.ipynb` | imports `hfstats`; plots, headline table, observations template; **not executed** |
| `ps1/figures/` | seven currency figures + `summary_currencies.csv` |
| `ps1/report/ps1_report.md` | report draft |
| `README.md` | mentions `plots.py`, figures, report |
