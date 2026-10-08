# ZORA review report (v1.2.1 -> review build)

## Scope and method
Every Python module, the web dashboard script, Docker/compose, systemd, nginx, deploy/backup scripts and CI were read.
Confirmed findings were reproduced by executing the code (fake exchanges, the real Trader loop with fake candles,
the CLI). Test suite: 353 tests, all passing (run with an offline pytest stand-in, because pytest and ccxt could not be
installed in the review sandbox; run `pytest` and `ruff check .` in your own environment to confirm).

## Findings and status

| # | Sev | Area | Finding | Status |
|---|-----|------|---------|--------|
| 1 | Critical | execution.py | A market order whose reply was lost (network error) was sent again. The exchange could fill both: intended 0.5, filled 1.0 (reproduced). | Fixed: one attempt; outcome marked `unknown_state`; bot refuses new entries and does not resend closes until restart/reconcile. |
| 2 | High | execution.py | `reconcile()` used `contractSize` as a fallback, so a flat position was reported as open and a real one could be hidden (reproduced: reported 0, actual 2). | Fixed (only non-zero `contracts` count). |
| 3 | High | risk.py | Exposure cap counted risk budget, not notional: 6 positions on $1,000 = 150% notional (reproduced). | Fixed: `MAX_TOTAL_NOTIONAL_PCT` (default 100 = no leverage), enforced live and in backtests, restored on restart. |
| 4 | High | bot.py | Entries were decided on the still-forming candle, while the backtester uses closed candles only. | Fixed: signals use closed candles; exits use the latest price. |
| 5 | High | bot.py | On stale data the bot skipped managing open positions (contradicting "guards never block exits"). | Fixed: no new entries on stale data; open positions still managed on last known price. |
| 6 | High | dashboard | Live prices were computed but never published, so dashboard unrealized P&L was always zero (reproduced). | Fixed. |
| 7 | High | docker-compose.yml / .env.example | `.env`'s `DB_PATH=zora.db` overrode the image's `/data` path, so the trading database lived in the container layer and was lost on rebuild/update. | Fixed: `environment:` overrides in compose; Dockerfile sets `LOG_FILE` to /logs. |
| 8 | High | backup.sh | Backed up volume `zora_data`, but Compose prefixes volume names with the project name, so it could archive an empty volume. | Fixed: fixed volume names in compose; backup checks the volume exists. |
| 9 | High | nginx example | Proxied the whole dashboard (no login) to the internet, including `/docs`. | Fixed: only the webhook path is proxied, with a body-size limit; everything else returns 404; TLS note. |
| 10 | Medium | multi_ai.py | A valid JSON reply that is not an object crashed the parser (reproduced). It was contained as a provider failure. | Fixed: treated as a no-verdict fallback. |
| 11 | Medium | optimize.py | The "winner" was picked by test-window results, so the out-of-sample number was biased. | Fixed: winner chosen by train score only. |
| 12 | Medium | dashboard.py | FastAPI webhook read the whole body before checking size (memory exposure). | Fixed (size checked while streaming). Not executed: fastapi is not installed in the sandbox. |
| 13 | Medium | news_feed.py | When all feeds were down, both feeds were re-fetched every cycle (up to ~20 s stall each time). | Fixed: 5-minute backoff. |
| 14 | Medium | README / CLI | `paper-validate` is documented but not a command. | Fixed: docs point to `backtest` and `validate`. |
| 15 | Low | CI | Four unused test imports fail `ruff check` (rule F401). | Fixed. |
| 16 | Low | dashboard app.js | Infinite profit factor showed "Below 1.0"; "Peak equity" was always "—". | Fixed. |
| 17 | Low | bot.py | `/kill` message said "until restart", but the trip is saved and survives a same-day restart. | Message corrected. Behavior decision open (see below). |
| 18 | Low | bot.py | `/ai` contained a dead expression. Startup compared signed contract counts. | Fixed. |
| 19 | Low | deploy/Docker | Healthcheck fails when the dashboard is off (the default with the plain compose path; `deploy.sh` sets the port). | Documented; `deploy.sh` path is correct. |

## Verified by execution
- Order retry: before 2 orders for 1 intent, after 1 (`repro`, plus regression test).
- Reconcile on a derivatives position: before reports 0, after reports 2 (correct).
- Notional: before 6 positions / 150% notional, after 4 / 100%.
- Trader cycle with fresh candles, stale candles, and stale + open position (no exceptions; stop managed on stale data).
- Dashboard publish now includes `prices`.
- `validate` harness and `doctor` pass.

## Not verified here (please check before live use)
- ccxt `featureValue()` for native stop/take-profit capability: confirm with `python bot.py exchange-smoke` on your installed ccxt. If the method does not exist, live entries are refused (fail-closed).
- Docker build/compose/healthcheck and the backup flow: not run (no Docker in the sandbox).
- Real pytest and ruff runs: not run; the offline stand-in ran the suite.
- Live trading on any exchange: not tested. Paper trading only.

## Decisions for you (not changed)
- `/kill` semantics: should a manual kill survive restarts, and should it clear only by an explicit command?
- Fear & Greed scoring jumps from +22 at index 39 to 0 at 40. A smooth ramp would be more consistent.
- `validate` "PASS" only means the run executed and stayed within drawdown limits. It does not show the strategy has an edge. Consider renaming it.
- `MAX_EXPOSURE_PCT` is a risk budget (sum of risk-per-trade %). The name suggests notional. Consider renaming in a future major version.

## Scorecard
See `zora_vs_bots_scorecard.png` and `zora_vs_bots_by_criterion.png`. Weighted scores are subjective and based on this review and public feature information for the competitors (not hands-on tests). Weights: risk controls 15, correctness/tests 15, backtest realism 10, live maturity 15, strategy breadth 10, deployment/security 10, documentation accuracy 10, cost/openness 10, ease of use 5.
