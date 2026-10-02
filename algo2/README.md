# algo2 — RIT ALGO2 (single stock, ALGO) market making

The predecessor case to ALGO2e. `algo2_mm.py` is the single-stock bot; its
lessons and code carried into `../algo2e/algo2e_mm.py`.

- `logs/` — five runs on 2026-09-24 (practice 19:28, 19:46, 19:55, 20:02 and the
  graded 20:09 case). The graded run's `.log` is not included: the bot kept
  running for six days after the client closed and wrote 637 MB of error lines,
  over GitHub's 100 MB file limit. Its `_ticks.csv` and `_trades.csv` are here.
- `reference/` — the ALGO2 case brief, the RIT Python REST API tutorial, its
  parsing appendix, the speed-bump tutorial, and the Excel cockpit file.
