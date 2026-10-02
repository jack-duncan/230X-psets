# Reference material for ALGO2e

Background data used to design `../algo2e_mm.py`, not code we wrote.

- **case-brief/** — the official RIT case brief: securities, fee/rebate table per
  ticker, position limits, order-rate limit.
- **2022-student-scripts/** — a past cohort's scripts for this same case (`RSM434`),
  plus `ALGO2e Performance Evaluation Grades - Quercus.xlsx`, the grading sheet for
  46 participants over 6 runs each. Read together these show what wins the case:
  every top finisher traded one stock (the inverted-fee one) at high volume,
  crossing the spread rather than resting; three-stock passive quoters lost most
  of the time; under 100k shares/run is an automatic minimum grade; one losing run
  hurts the final grade more than a big winning run helps it. See
  `../ALGO2e_SUMMARY.md` for the full breakdown.
- **rsm434-primers/** — the instructor's starter template (`Logic Primer`, with the
  `Code Primer` as the same code with teaching comments), the passive two-sided
  baseline that `algo2e_mm.py` builds past (per-ticker spread thresholds, real
  inventory management, a fast position estimate, taking liquidity where the venue
  pays for it, and an end-of-case flatten).
