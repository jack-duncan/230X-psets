# ALGO2e team controller

- Goal: one Python3.9+ controller for a shared RIT ALGO2e account; fee-aware market making, bounded trend positioning/protective exits and screened pairs.
- Standard library only. Local Client API; default CLI is read-only. Standalone RUN_ALGO2E.bat/menu contains the same source and offline tests.
- Local validation:51 tests passed under Python3.9.6, including mock HTTP, delayed fills/cancels, signed fees, pair legs and eight synthetic300-tick risk rounds. Synthetic fills do not measure profitability.
- Delivery check: embedded code bytes/hashes matched modules; batch Python bootstrap ran help and read-only mock preflight; no credential logging or writes in the check. Native Windows cmd and classroom server execution unverified.
- Private market data, historical logs and detailed private performance analysis intentionally excluded from this repository. No existing repository strategy content used.
- Run validation: python -m unittest discover -s algo2e/team_controller -p "test_*.py"; or RUN_ALGO2E.bat menu5.
- Rebuild: python algo2e/team_controller/build_onefile.py. One operator/writer across all team members.
- Known limits: unknown insertion outcome halts; non-atomic API/recovery state requires manual verification if unresolved; no guaranteed fills or P&L. Pair relationships may never pass the screen.
- Next: review new directory, then dedicated-account practice on the actual Client before final use.
- Publishing validation: staged diff contains only new team_controller files and required handoffs; source bytes match the tested delivery. Batch files normalize to LF in Git and check out as CRLF via local .gitattributes. git diff --cached --check passed.
