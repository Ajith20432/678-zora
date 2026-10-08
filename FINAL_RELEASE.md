# ZORA v1.2.1 Final Hosting Release

## Critical safety fixes in this release
- Persisted daily-loss baseline and kill-switch state across restarts.
- UTC day rollover clears only the daily-loss lock; drawdown lock remains latched.
- Added backward-compatible recovery for legacy databases without persisted risk state.
- Strict PAPER/LIVE DB isolation: PAPER uses DB_PATH; LIVE uses DB_PATH.live unless LIVE_DB_PATH is set.
- Dashboard reads runtime state only from `/api/state`; removed legacy demo/random `dashboard_web/data.js`.

## Validation
- Safety + risk + DB + dashboard tests: 47 passed.
- Full dependency-independent suite: 223 passed.
- Full suite requires `ccxt` to be installed in the host environment; this sandbox does not have it.

## Hosting mode
PAPER remains the default. Do not enable LIVE until exchange credentials, native protection, reconciliation and a small controlled live smoke test are independently verified.
