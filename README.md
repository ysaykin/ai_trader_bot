# Lumibot SPX/XSP Iron Condor Trading Cockpit & Neural Policy Engine

Automated iron-condor research and execution project built on LumiBot. Tradier
execution uses one atomic four-leg package, while a durable SQLite ledger assigns
contracts to a strategy only after confirmed fills.

## Key Capabilities

- **Risk Guardrails (`strategies/iron_condor.py`)**: Hard `$5,000` global open exposure cap (`RSK-01`) and sliding-scale single-trade sizing (`30%` down to `5%`, `RSK-02`) with automatic contract quantity scaling (`RSK-03`).
- **20-Variant Strategy Incubator (`strategies/incubator.py`, `strategies/market_data_hub.py`)**: Runs `single`, `ab`, or `incubator` (`IC_V01`..`IC_V20`) modes with ledger-backed strategy ownership and a shared market-data cache.
- **Intraday Trajectory Logger (`trade_logger.py`, `ml/features.py`)**: Records causal 5-minute market regime + 20-grid Greek/wing-pressure snapshots (`logs/trajectories.jsonl`) and performs post-market hindsight-optimal action relabeling.
- **Counterfactual Strategy & Regret Evaluator (`ml/evaluator.py`)**: Computes action regret, cross-grid regret, MFE/MAE capture efficiency, downside Sortino ratio, and 95% CVaR stratified by market regime.
- **Multi-Head Neural Policy Network (`ml/policy_net.py`)**: Dependency-free recurrent temporal regime encoder driving three policy heads:
  1. **Scale-In/Out Action Head**: `ADD_POSITION`, `HOLD`, `TRIM_POSITION`, `FLATTEN_NOW`
  2. **Dynamic Profit Target Head**: Continuous take-profit threshold in `[0.15, 0.80]`
  3. **Contextual Strategy Selector Head**: Probability distribution across all 20 incubator variants
- **Offline training and opt-in inference (`ml/train.py`, `ml/inference.py`)**: Trains JSON artifacts from labelled trajectories, stores feature imputation and normalization, and applies recommendations only when `ML_POLICY_ENABLED=true`.
- **Real-Time Trading Cockpit (`dashboard/app.py`, `DASHBOARD.md`)**: Streamlit UI (`http://localhost:8501`) with live Tradier/Alpaca telemetry, visual risk gauges, 20-strategy incubator leaderboard, neural policy recommendations, interactive Plotly payoff curves, and in-browser config. For full UI documentation, see [DASHBOARD.md](DASHBOARD.md).

## Project Structure

```text
lumibot/
├── main.py                      # Trading bot entrypoint (Tradier / Alpaca; single / ab / incubator modes)
├── trade_logger.py              # Trade journal & 5-minute TrajectoryLogger + EOD hindsight relabeler
├── strategies/
│   ├── iron_condor.py           # SPX/XSP Iron Condor engine, risk guardrails & dynamic policy actions
│   ├── incubator.py             # 20-variant parameter grid & host-follower execution coordinator
│   └── market_data_hub.py       # Shared 5-minute option chain, quote, and intraday bar cache
├── ml/
│   ├── features.py              # Causal intraday market & 20-grid aggregation features
│   ├── evaluator.py             # Counterfactual regret, MFE/MAE efficiency & Sortino/CVaR metrics
│   ├── train.py                 # Offline trajectory trainer and JSON artifact writer
│   ├── inference.py             # Validated live normalization and inference bridge
│   └── policy_net.py            # MultiHeadPolicyNetwork (Regime Encoder + 3 decision heads)
├── dashboard/
│   ├── app.py                   # Streamlit Trading Cockpit & A/B Strategy Lab (:8501)
│   ├── broker_client.py         # Tradier & Alpaca live telemetry client
│   ├── config_manager.py        # .env configuration persistence & connection testers
│   └── payoff_chart.py          # Multi-curve Plotly option payoff visualizations
├── tests/
│   ├── test_dashboard.py        # Dashboard telemetry, settings & benchmark tests
│   ├── test_policy_net.py       # Multi-head neural policy network unit tests
│   ├── test_regressions.py      # Broker selection, risk caps, incubator & leg-isolation regressions
│   └── test_trajectories.py     # Causal feature, trajectory logging & counterfactual evaluator tests
├── DASHBOARD.md                 # UI architecture, mission, risk gauges, and feature user guide
├── USER_FEATURES.md             # Living single-source-of-truth User Features Catalog
├── Dockerfile                   # Trading bot container image
├── Dockerfile.dashboard         # Streamlit dashboard container image
└── docker-compose.yml           # OrbStack / Docker Compose orchestration
```

## Quick Start

1. Copy `.env.example` to `.env` and configure the canonical Tradier Sandbox variables (`TRADING_BROKER=tradier`, `TRADIER_ACCESS_TOKEN`, `TRADIER_ACCOUNT_NUMBER`, `TRADIER_PAPER=true`, and `DATA_SOURCE=tradier`) or Alpaca Paper credentials. This runner obtains SPY, SPX, and XSP data from Tradier.
   ```bash
   cp .env.example .env
   ```

2. Build and launch with Docker Compose (or `make`):
   ```bash
   make build
   make up      # Starts lumibot-bot + lumibot-dashboard (http://localhost:8501)
   make ui      # Starts dashboard only and opens http://localhost:8501
   ```

3. Select execution mode in `.env`:
   - `TRADING_MODE=single` — Single `SPXIronCondor` strategy
   - `TRADING_MODE=ab` — Baseline vs. Challenger A/B execution
   - `TRADING_MODE=incubator` — StrategyBro and dynamic strategy comparison

The default `INCUBATOR_PROFILE=dynamic` roster prioritizes StrategyBro, TrendFlow,
Skew Dynamic, VolCrush, ORB, and Neural Pure, followed by one 30 DTE plain IC
control. Strategy-count limits preserve this priority. Use
`INCUBATOR_PROFILE=full_grid` to opt into the full 29-strategy parameter sweep.

Inspect entries and position recovery without starting a broker:

```bash
python3 tools/check_strategy_roster.py --ledger logs/position_ledger.sqlite3
```

Excluded owners with active ledger packages remain attached for management only.
They cannot open new trades, add contracts, or switch strategies. Historical
ownership is retained, and they stop being included on restart once their
packages are terminal. Unknown owners stop startup with a configuration error.
The dashboard comparison shows the selected entry roster; Positions retains
existing packages. Compare fee-adjusted returns and risk over common periods,
since different DTEs and entry filters produce different trade counts.
Research snapshots and policy routing use only entry-enabled strategies. Neural
Pure uses the existing model artifact; changing the roster does not retrain it.

4. Train a policy artifact and enable it explicitly when ready:

   ```bash
   python3 -m ml.train logs/trajectories.jsonl logs/models/policy.json
   # then set ML_POLICY_ENABLED=true
   ```

Submitted packages are not counted as positions. The strategy waits for fill
callbacks, persists weighted fill prices and signed quantities, and blocks new
entries if the broker reports contracts that the ledger cannot attribute.
Close orders are repriced using `IC_CLOSE_REPRICE_SECONDS`,
`IC_CLOSE_MAX_REPRICES`, and `IC_CLOSE_PRICE_STEP`; market fallback remains
disabled unless `IC_CLOSE_ALLOW_MARKET=true`. Unexpected process failures are
retried after `PROCESS_RESTART_BACKOFF_SECONDS` using the durable ledger.

The opt-in Tradier sandbox test is state-changing: set
`RUN_TRADIER_SANDBOX_WRITE_TEST=1`, `TRADIER_SANDBOX_TEST_UNDERLYING`, and four
comma-separated `TRADIER_SANDBOX_TEST_OPTION_SYMBOLS` only for an account where
submitting and canceling a paper order is acceptable.

## Running Tests

```bash
python3 -m unittest discover -s tests -v
```

## Concurrent positions

Set `IC_MAX_CONCURRENT_POSITIONS=2` to allow two independently managed positions
per strategy. The default remains `1`. `IC_ENTRY_SPACING_MINUTES` defaults to
`60` and applies when concurrency is enabled. Existing strategy, single-position,
allocation, and portfolio risk limits still apply.

Each position has a persistent `position_id`. Its entry, additions, trims, closes,
and replacement orders have separate `package_id` values linked to that position.
Entries retain their own credit, management settings, journal identity, and exit
intent after restart. Broker tags continue routing through the registered host.
The positions dashboard can use the distinct journal trade IDs to group fills.

Pending entries reserve their unfilled risk and occupy a position slot. Pending
closes reserve their remaining contracts. Unknown order outcomes retain those
reservations until broker reconciliation confirms terminal execution quantities.
Freshly confirmed pending orders keep their slot and reserved risk across a DAY
rollover while allowing another independent entry within the configured limits.
Legacy ledger migration links adjustments only when ownership is unambiguous;
unattributed inventory blocks new exposure.

After changing these environment settings, recreate the services with
`docker compose up -d --no-build --force-recreate lumibot-bot lumibot-dashboard`.
Mounted Python source is loaded when the process starts. Verify startup logs for
each strategy's `Entry limits`, then inspect capacity, reserved orders and fresh
held quotes without broker writes:

```bash
docker exec -i lumibot-bot python - --fresh --expect-capacity 2 < tools/check_trading_runtime.py
```

The execution adapter is pinned to LumiBot 4.6.31, the version exercised by the
broker routing and independent-position regressions.

Policy adjustments accept an explicit `position_id` when several positions are
open. An untargeted add, trim, or profit-target adjustment returns `HOLD` in that
case. `FLATTEN_NOW` closes each position separately. Entry quantity remains a
configuration value, so a filled addition cannot enlarge the next entry.
The existing single-basket training schema leaves aggregate P&L labels unavailable
when several positions are open, and records their identities separately.

Run the independent-position regressions without broker writes:

```bash
RUN_TRADIER_SANDBOX_WRITE_TEST=0 python3 -m pytest -q tests/test_concurrent_positions.py tests/test_tradier_order_routing.py
```

## Exit and runtime safeguards

P&L exits require complete option quotes and two distinct readings, at least
30 seconds and at most ten minutes apart. The normal five-minute loop confirms
on its next iteration. A leg's bid/ask gap cannot exceed
the smaller of $1.00 and the larger of $0.25 or 50% of its midpoint. Marks
outside the covered wings' payoff bounds are unavailable and cannot update MFE.
Non-expiry positions defer P&L exits until 15 minutes after the session opens.
An expiry close can override that hold and a pending stop.

Stop limits use validated package quotes. Missing or rejected quotes retain
the stop intent for a later attempt. Explicit liquidation and expiry closes
retain bounded emergency pricing. Profit limits preserve the target after
round-trip fees. The breakeven exit is disabled; the optional positive-profit
trailing exit remains available. Paper entries keep the quoted credit unless
`SANDBOX_CREDIT_DISCOUNT_PCT` is explicitly set.

`IC_STOP_COOLDOWN_MINUTES` defaults to `60`. Stops block that strategy's entries
and additions through the cooldown, which survives restarts and extends after
confirmed stop fills. `IC_MIN_CREDIT_TO_WIDTH` defaults to `0.10`, requiring at
least $0.50 per share for a $5 wing. `IC_MIN_CREDIT` can require a higher credit.

Process failures retain positions and existing exit intents. Legacy
`ABRUPT_CLOSE` intents expire after their session; submitted orders retain their
reservations until broker reconciliation confirms a terminal outcome. The
independent iteration watchdog emits `TRADING_ITERATION_STALLED` at critical
level after ten minutes without a completed market-hours iteration. It logs
once per stall and does not restart the process or send external notifications.

Run the exit and runtime checks in an isolated container with broker networking
disabled and no live records or credentials mounted:

```bash
python3 tools/verify_exit_safeguards.py
```

## License

MIT License
## Shared broker snapshots

The bot and dashboard share `logs/broker_snapshots.sqlite3`, separate from the
position ownership ledger. The bot publishes account snapshots. A dashboard
collector can take over after the bot's lease expires. Browser refreshes read the
shared store and do not make account requests.

Balances refresh every 30 seconds, positions every 15 seconds, idle orders every
30 seconds, active orders every 5 seconds, and demanded quotes every 5 seconds.
Option package pricing and management use native Tradier quote batches. The
`BROKER_*_INTERVAL` settings in `.env.example` control these schedules.
Routine intervals increase fourfold outside weekday session hours when no
orders are pending. Fresh execution reads bypass that scheduling delay.

Failed reads retain the last successful values and their original timestamps.
The dashboard labels stale data and shows each source's age. After
`BROKER_DISPLAY_MAX_AGE`, values are historical and excluded from current totals.
A confirmed empty inventory clears previous holdings. Missing data never implies
an empty account. Broker quote timestamps remain separate from cache age.

The shared request budget counts network attempts across processes, reserves
half the market-data allowance and at least half the standard allowance for
execution, and observes provider quota headers and cooldowns. GET failures have
bounded retries with jitter. Mutations are attempted once and invalidate cached
reads. Execution boundaries require fresh broker evidence and never use retained
stale display values. Existing ownership, cancellation and liquidation gates
still apply.

Both services share a writable logs volume in Compose. A relative
`BROKER_SNAPSHOT_PATH` resolves from the project root in either service. Apply
the cache deployment with `docker compose up -d --no-build`; Compose must recreate
containers whose mounts changed. `docker compose restart` keeps the old mounts,
including the dashboard's previous read-only logs mount. A cache storage failure
shows a warning while readable positions and independent strategy data still load.
The initial integration retains SDK polling; the installed SDK has
no account-event consumer to reconcile reconnects or duplicate events safely.
