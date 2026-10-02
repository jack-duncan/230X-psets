# ALGO2e team build (`algo2e_team.py`, branch giancarlo/algo2e)

Hashim's `algo2e_mm.py` with RY unchanged, plus rules for CNR and AC and a limit allocator.
Run it the same way: set `API_KEY` and `PORT` at the top, then `python algo2e_team.py` (one console per ticker).
Restart it for every heat: the AC anchor and the health history are per process.

## Why each rule (real practice data, 2026-10-01; analysis in Giancarlo's algo3 folder, `FINDINGS_PRACTICE.md`)

- RY: Hashim's passive quotes away from the touch. RY mean-reverts in all three practice heats, and his fills
  in heat 1 earned +1.26c a share with a mark-out of about zero.
- CNR: quote at the touch, but only the side the order book favours (`Imb gate` 0.3: the bid only when
  (bid size - ask size)/(bid size + ask size) >= 0.3, the ask only when <= -0.3). Passive fills on the favoured
  side earned +3.4c (heat 2 tape) and +2.7c (our heat 1 fills) a share after fees; unfavoured fills lost -2.9c
  and -5.8c. Past half the inventory cap the reducing side ignores the gate.
- AC: same gate, 500-share quotes, at most about 1,150 shares, and no adding while
  - the median touch size is under 5,000 (`Thin touch kill`; normal heats 10-15k, the pump heat about 2,500),
  - one book level is over 50x the touch (`Wall kill x`; normal heats up to 6x, the pump heat 800x),
  - the mid is more than 5% from its median since the heat began (`Anchor kill %`; normal heats at most 1.5%,
    the pump heat 63%).
  Replayed on the real heat-2 book these block AC 89% of the time, and 100% during the plateau where our first
  bot bought 7,500 shares before the $20 drop; in heat 1 they never fire.

## Limit allocator

Each ticker may use `Budget share` x `Account gross stop` (23,000) x health, where health in [0, 1] is the
product of three live factors:
- depth: top-5 depth per side with the largest level removed (a wall is not depth we can rely on), thinner side,
  relative to `Depth ref`;
- volatility: RMS of 10 s mid moves relative to `Vol ref $` (RMS so a steady ramp counts as risk);
- edge: our own fills marked 10 s later plus the maker rebate; falls to 0 at -0.5c a share once enough shares
  are marked.
Health 0 when a kill switch fires. Quote size scales with health too. Budgets: RY 15,000 (Hashim's cap),
CNR 0.15 x 23,000 = 3,450, AC 0.05 x 23,000 = 1,150, total 19,600, below 23,000 even at full health, so the three
processes never need to coordinate; the account-wide stop at 23,000 stays as a backstop. The reducing side is
always allowed, so a killed ticker can still unwind.

## Tested

Offline only: gate and allocator on synthetic books, health on the real recorded books of heats 1 and 2, and a
45 s run of CNR and AC against a mock RIT server (plumbing only: no errors, gated quotes, fills on both sides).
Not tested on the real server. Parameters are round numbers set from three practice heats, not optimised.
RY's `Take edge` (0.03) is Hashim's newest setting and has not fired live yet.
