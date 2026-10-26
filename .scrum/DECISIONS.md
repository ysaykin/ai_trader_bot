# Architectural Decisions

## ADR-01: 5-Pillar Self-Improving Neural Strategy Architecture (2026-10-26)
- **Context**: Running a single Iron Condor strategy provides only one observation per trading day and uses static entry/exit thresholds that fail when market regimes shift intraday.
- **Decision**:
  1. **Strategy Pipeline**: Run 20 concurrent parameterized Iron Condor variants (`IC_V01`..`IC_V20`) backed by a shared `MarketDataHub` cache and isolated per-strategy leg accounting.
  2. **Trading & Evaluation Pipeline**: Expand strategy execution from binary entry/exit to support `ADD_POSITION`, `TRIM_POSITION`, `ADJUST_PROFIT_TARGET`, and `FLATTEN_NOW`, evaluated via Counterfactual Regret and MFE/MAE capture efficiency.
  3. **Logging Pipeline**: Record 5-minute intraday State-Action-Reward trajectories (`logs/trajectories.jsonl`) capturing market regime features and cross-strategy stress across all 20 variants.
  4. **Learning Pipeline**: Train a Multi-Head Neural Policy Network (`ml/policy_net.py`) that maps market dynamics + 20-grid signals to position scaling, dynamic profit-taking, and strategy selection.
  5. **Self-Improving Bot Loop**: Retrain a Challenger model daily after market close using Purged Walk-Forward CV and require out-of-sample Shadow Mode outperformance before promoting Challenger to Champion, while keeping `RSK-01` ($5k cap) and `RSK-02` (30%->5% sliding scale) as an immutable risk boundary.

## ADR-02: Dependency-Free Recurrent Multi-Head Policy Network (2026-10-26)
- **Context**: Live trading and dashboard containers (`lumibot-bot` and `lumibot-dashboard`) require deterministic, low-latency 5-minute policy evaluation without coupling live order execution to heavyweight deep-learning runtimes or GPU dependencies.
- **Decision**:
  1. **Pure-Python Inference Core (`ml/policy_net.py`)**: Implemented `MultiHeadPolicyNetwork` using immutable `tuple` vectors/matrices, seeded Glorot/Xavier initialization, and strict finiteness and shape checks on all inputs and weights.
  2. **Shared Temporal Regime Encoder**: Processes `[batch][time][feature]` sequences through a `tanh` recurrent hidden state (`regime_size=32` default) to produce an order-sensitive latent regime embedding.
  3. **Three Specialized Policy Heads**:
     - **Position Scaling Head**: Softmax probabilities across `ADD_POSITION`, `HOLD`, `TRIM_POSITION`, and `FLATTEN_NOW`.
     - **Dynamic Profit-Target Head**: Numerically stable sigmoid scaled to `[0.15, 0.80]` (`MIN_PROFIT_TARGET`..`MAX_PROFIT_TARGET`).
     - **Contextual Strategy Selector Head**: Softmax probabilities across the 20 incubator variants (`strategy_count=20`).
  4. **Decoupled Weight Serialization**: Added `state_dict()` and validated `load_state_dict()` hooks so offline training (`ml/train.py`, `ml/continual_learner.py`) can optimize parameters out-of-band and load plain numeric checkpoints into live inference.
  5. **Isolated Test Discovery**: Scoped `pytest` `testpaths = ["tests"]` in `pyproject.toml` so automated verification runs deterministic offline unit and regression suites cleanly.
- **Trade-offs**: Pure-Python matrix arithmetic avoids multi-gigabyte ML dependencies and guarantees fast single-sequence inference at 5-minute cadence, while batch gradient optimization is deferred to the offline training pipeline (`ml/train.py`).
