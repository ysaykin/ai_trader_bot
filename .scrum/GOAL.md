# Project Goal: Self-Improving Lumibot Trading Cockpit, 20-Strategy Incubator & Neural Policy Engine

Outcome:
A self-improving automated options trading platform for Lumibot SPX/XSP Iron Condors running on OrbStack, combining a 20-variant simultaneous strategy incubator, 5-minute intraday trajectory logging, a multi-head neural network policy for dynamic position scaling and profit-taking, counterfactual strategy evaluation, and a Champion/Challenger continual learning loop strictly bounded by automated risk rules ($5k global limit and 30%->5% tiered single-trade risk).

Acceptance:
- [x] Living User Features Catalog established and maintained (`USER_FEATURES.md`).
- [x] Sliding scale single-trade risk cap (30% at $5k down to 5% at scale) with contract auto-scaling.
- [x] $5,000 global open position risk limit.
- [x] Real-time trading overview dashboard GUI with live telemetry and config management.
- [x] **Strategy Pipeline**: 20-variant simultaneous Iron Condor incubator with per-strategy position isolation and shared `MarketDataHub` cache (`EXP-01` to `EXP-05`).
- [x] **Trading & Evaluation Pipeline**: Granular position actions (`ADD`, `TRIM`, `DYNAMIC_PT`, `FLATTEN`) and counterfactual regret / MFE / MAE evaluation across all 20 strategies (`ML-07`).
- [x] **Logging Pipeline**: 5-minute intraday State-Action-Reward trajectory logger (`logs/trajectories.jsonl`) with EOD hindsight-optimal relabeling (`ML-04`).
- [x] **Learning Pipeline**: Multi-head neural policy network (`ml/policy_net.py`) encoding market dynamics + cross-strategy signals to decide position scaling, profit-taking, and strategy selection (`ML-01` to `ML-06`).
- [x] **Policy Training & Inference Bridge**: Offline trajectory trainer (`ml/train.py`), live normalization/imputation bridge (`ml/inference.py`), and transactional position ledger (`strategies/position_ledger.py`).
- [x] **Tradier Multileg Reconciliation & Ledger Rollover**: Durable SQLite parent/child order tracking, multi-day rollover persistence, close order repricing, and crash-recovery fault containment (`BRK-01`).
- [x] **Local Import Hygiene & Packaging Isolation**: Relocated legacy prototype shim to prevent namespace shadowing, hardened configuration error exits, and established multi-package validation (`SYS-01`).
- [x] **Confirmed Opening Credit Safety & Roll/DTE Exit Precedence**: Require finite confirmed entry credit without adopting limit prices, retain contract ownership during quote delays, and reach roll/DTE targets before valuation gates (`ENG-08`).
- [x] **Tradier Credential Protection & Log Redaction**: Redact account numbers and sensitive tokens from connection logs, and support configurable polling intervals (`BRK-04`).
- [x] **Jev-Bot Lifecycle & Isolated Subprocess Verification**: Comprehensive lifecycle step validation, signal cleanup, and isolated multi-entrypoint testing (`SYS-02`).
- [ ] **Self-Improving Bot Loop**: Automated post-market continual learner with Purged Walk-Forward CV and Champion vs. Challenger shadow promotion gate (`ML-08`).
