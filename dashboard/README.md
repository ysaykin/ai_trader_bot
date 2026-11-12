# Lumibot Trading Dashboard

Docker Compose runs the Reflex dashboard at `http://localhost:8501`.
Its primary views are Overview, Positions, Orders, Strategies, System health,
and Settings. A/B and benchmark research flows are hidden.

Overview separates marked open P&L from verified closed results after fees.
Orders joins stored broker IDs to the ownership ledger and retains unconfirmed
reservations even when the broker's recent-order list omits them. System health
shows receipt age, provider trade age, missing features and recorded strategy
actions. A current cache response does not establish current market data.

DAY orders leave the pending count and default Orders view at their regular
exchange session close, including early closes. The bot requests cancellation
of identified DAY orders at close and catches missed closes during subsequent
reconciliation. When an ended DAY order is absent or terminal and fresh broker
inventory matches its contracts, reconciliation cancels the unfilled remainder
and releases its reservation. Recorded fills, held risk and settlement evidence
remain intact. Canceled orders stay in All recent orders. GTC orders, active
broker orders, unavailable snapshots and contract quantity conflicts retain
their reservations and appear under Needs reconciliation.

Preview existing paper-account reservations with `tools/reconcile_day_orders.py`.
Pass `--apply` to persist eligible cancellations locally. The command reads fresh
broker orders and positions and never submits or cancels broker orders.

The broker rechecks market time every 30 seconds while waiting for an opening or
closing boundary. These waits reconcile host and follower orders under the shared
execution gate with fresh broker evidence. A host or Docker suspension therefore
cannot leave the bot asleep for the remainder of a previously calculated overnight
delay after it resumes.

The bot's Compose health check runs every minute. During the regular exchange
session, ten minutes without a strategy iteration marks it unhealthy, even if
broker polling continues. The check allows a ten-minute startup grace period and
uses the exchange calendar for holidays and early closes. Inspect it directly:

```bash
docker exec lumibot-bot python -m strategies.session_runtime --check
```

Manual imports accept confirmed, fully filled, covered two-leg and four-leg
combinations, including put condors. The ledger and dashboard calculate risk from
the same expiration payoff. Imports retain the `MANUAL` owner, verify account
inventory, and are safe to repeat. Each new order still requires explicit manual
confirmation. Unsupported or unidentified executions continue to block new risk.
Missing settlement evidence retains expired positions and leaves final P&L
unavailable until evidence is recorded.

Run the prevention regressions without broker access:

```bash
python tools/run_offline_tests.py tests/test_session_runtime.py tests/test_manual_positions.py tests/test_day_order_expiry.py tests/test_xsp_settlement_lifecycle.py
```

VIX and VIX9D use Tradier. Alpaca does not currently supply spot index data.
Expired quotes cannot value current positions. Missing models and unverified
trade results display as unavailable.

Run the monitoring and accounting regressions from an environment with the
dashboard dependencies:

```bash
python tools/verify_dashboard_monitoring.py tests/test_dashboard_monitoring.py tests/test_dashboard_integrity.py tests/test_reflex_dashboard.py tests/test_comparison_dashboard.py tests/test_position_workspace.py
```

The runner uses a temporary source copy without credentials or runtime records.
The Streamlit implementation documented below remains in the repository.

The interactive Streamlit dashboard (`dashboard/app.py`) provides real-time mission control, capital safety oversight, and forward-testing performance tracking for the Lumibot trading platform.

For the comprehensive documentation on the dashboard mission, UI architecture, and all supported features, see:
- 📖 **[DASHBOARD.md](../DASHBOARD.md)**: Full UI specification, architecture, risk gauges, 20-grid incubator, neural policy tab, and operational runbook.
- 📋 **[USER_FEATURES.md](../USER_FEATURES.md)**: Master User Features Catalog across the entire engine and UI.

### Quick Start

```bash
# Build and run the dashboard container
docker compose up -d --build lumibot-dashboard

# Access the interface
open http://localhost:8501
```
