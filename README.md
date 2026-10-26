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
- **Real-Time Trading Cockpit (`dashboard/app.py`)**: Streamlit UI (`http://localhost:8501`) with live Tradier/Alpaca telemetry, visual risk gauges, interactive Plotly payoff curves, A/B comparative scorecards, and credential/parameter configuration.

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
├── USER_FEATURES.md             # Living single-source-of-truth User Features Catalog
├── Dockerfile                   # Trading bot container image
├── Dockerfile.dashboard         # Streamlit dashboard container image
└── docker-compose.yml           # OrbStack / Docker Compose orchestration
```

## Quick Start

1. Copy `.env.example` to `.env` and configure Tradier Sandbox (`TRADIER_ACCESS_TOKEN`, `TRADIER_ACCOUNT_NUMBER`, `TRADIER_PAPER=true`) or Alpaca Paper credentials. `TRADIER_IS_PAPER` and `TRADIER_SANDBOX` remain compatibility aliases. Tradier Sandbox users should select `SPY`; `SPX` and `XSP` require a compatible external `DATA_SOURCE`.
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
   - `TRADING_MODE=incubator` — Full 20-variant (`IC_V01`..`IC_V20`) incubator grid

4. Train a policy artifact and enable it explicitly when ready:

   ```bash
   python3 -m ml.train logs/trajectories.jsonl logs/models/policy.json
   # then set ML_POLICY_ENABLED=true
   ```

Submitted packages are not counted as positions. The strategy waits for fill
callbacks, persists weighted fill prices and signed quantities, and blocks new
entries if the broker reports contracts that the ledger cannot attribute.

## Running Tests

```bash
python3 -m unittest discover -s tests -v
```

## License

MIT License
