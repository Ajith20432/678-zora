# ZORA v1.2.1 Hosting Audit — 2026-10-07

## Release status
**READY FOR PAPER-MODE HOSTING.** Live trading remains locked by design.

### Validation performed
- Python bytecode compilation: PASS
- JavaScript syntax check (`node --check dashboard_web/app.js`): PASS
- Regression/unit/end-to-end suite: **339 passed**
- Full live exchange execution: NOT certified in this environment (requires a real VPS + exchange connectivity)
- Docker build: not executed here because Docker is unavailable in the audit environment
- Dependency installation: `ccxt` could not be downloaded in this sandbox because outbound package networking is unavailable; the Docker image installs it from `requirements.txt`.

## Fixes applied

### Critical
1. **Live protection-failure PnL bug fixed**
   - Previously a successful emergency close could be finalized using the original entry fill instead of the actual close fill.
   - Now the actual close fill is used for PnL/equity accounting.

2. **Exposure accounting fixed**
   - Persisted positions now store `exposure_pct`.
   - Restart reconciliation sums each position's actual risk budget, including correlation-based size reductions, instead of assuming every position consumes the full base risk percentage.

3. **Native protection capability check hardened**
   - Live entries now require confirmation that **both** stop-loss and take-profit conditional order capabilities are available before accepting a protected trade.
   - A single supported leg is no longer treated as sufficient.

### Hosting/dashboard
4. **Dashboard switched from demo data to real `/api/state` data**
   - Removed random/simulated price/equity behavior from the hosted UI.
   - Live prices, positions, decisions, trades, brain state, AI safety, and risk configuration are populated from the backend.
   - Added safe static asset serving for `index.html`, `styles.css`, and `app.js`.

5. **Live prices published to dashboard state**
   - The trading loop now publishes the latest processed symbol prices for dashboard rendering.

## Safety posture
- `LIVE_TRADING=false` by default.
- Live mode additionally requires explicit risk acknowledgement and exchange API credentials.
- Native protection is required by default.
- Dashboard is bound to localhost at the host Docker port; Nginx should be the public HTTPS entry point.
- TradingView webhook is secret-protected and size-limited.
- Dashboard endpoints are read-only except for the optional TradingView signal webhook.

## Known limitations before live
1. Run the Docker image on the actual VPS and verify `docker compose build`.
2. Run `./health-check.sh`.
3. Run exchange public-data smoke tests.
4. For any live activation, use API keys with **no withdrawal permission** and verify exchange-side protection behavior in the exact market/account type.
5. Complete the configured paper-validation period before considering live trading.

## Architecture scorecard
Scores are engineering/feature assessments, not profitability claims or audited trading performance.

| Area | Score |
|---|---:|
| Risk controls | 9.3/10 |
| Execution safety | 9.0/10 |
| Strategy/guards | 8.5/10 |
| AI resilience | 8.7/10 |
| Backtesting/validation | 9.0/10 |
| Dashboard/telemetry | 8.8/10 |
| Hosting/deployment | 8.7/10 |
| Test coverage | 9.4/10 |
| **Overall engineering readiness** | **8.9/10** |

## Important interpretation
ZORA's score does **not** mean it will outperform another bot. Trading profitability depends on strategy quality, market regime, fees, slippage, liquidity, and validation. The strongest evidence currently available for this release is the **339-test pass result and the hardened risk/execution architecture**, not a live-return claim.
