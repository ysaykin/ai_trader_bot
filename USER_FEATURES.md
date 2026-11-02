# Lumibot Trading Platform — User Features Catalog

This document is the **single source of truth** for all user-facing features supported and planned for the Lumibot automated options trading platform. Every feature maps directly to trading engine capabilities, risk boundaries, and GUI components in the trading dashboard.

---

## Feature Matrix & Status Summary

| ID | Feature Name | Category | Status | Primary User Benefit | GUI Representation |
| :--- | :--- | :--- | :--- | :--- | :--- |
| **ENG-01** | Automated Iron Condor Execution | Strategy Execution | ✅ Active | Hands-free 4-leg options entry | Strategy Card & Active Position Table |
| **ENG-02** | Target Delta Strike Selection | Strategy Execution | ✅ Active | Dynamic strikes based on market volatility | Parameter Slider & Strike Highlights |
| **ENG-03** | Configurable Wing Width | Strategy Execution | ✅ Active | Tailors defined risk to contract type | Numerical Input (e.g. 2pt for XSP) |
| **ENG-04** | Target DTE Selection (0DTE/1DTE) | Strategy Execution | ✅ Active | Day trading vs swing income options | DTE Selector & Expiration Badge |
| **ENG-05** | Timed Entry Window & Hours Guard | Strategy Execution | ✅ Active | Avoids morning opening whip | Countdown Clock & Schedule Indicator |
| **ENG-06** | Automated Profit Target | Strategy Execution | ✅ Active | Locks in gains (e.g. 30% or 50% credit) | Target P&L Line & Execution Alert |
| **ENG-07** | Automated Stop Loss Multiplier | Strategy Execution | ✅ Active | Caps losses (e.g. 1.5x or 2.0x credit) | Stop Level Indicator & Panic Liquidation |
| **ENG-08** | EOD Auto-Close Safety Net | Strategy Execution | ✅ Active | Avoids cash-settlement / pin risk | Market Close Countdown & Auto-Exit Badge |
| **RSK-01** | $5,000 Per-Strategy Open Position Cap | Risk Management | ✅ Active | Absolute drawdown protection per single strategy | Strategy Exposure Gauge (Progress Bar/Dial) |
| **RSK-02** | Tiered Single-Trade Sizing (30% $\to$ 5%) | Risk Management | ✅ Active | Prevents outsized losses per trade | Single-Trade Risk Meter |
| **RSK-03** | Intelligent Quantity Auto-Scaling | Risk Management | ✅ Active | Avoids rejected trades by scaling size | Auto-Scaled Quantity Notification |
| **RSK-04** | Portfolio Allocation Check | Risk Management | ✅ Active | Caps capital committed per trade | Buying Power Headroom Bar |
| **EXP-01** | Multi-Strategy A/B Engine | A/B Testing | ✅ Active | Test baseline vs challenger live | Dual Strategy Overview Panels |
| **EXP-02** | Independent Strategy Parameters | A/B Testing | ✅ Active | Different deltas/DTEs per variant | Strategy Parameter Cards (A & B) |
| **EXP-03** | Strategy Isolation & Order Tagging | A/B Testing | ✅ Active | Unambiguous order & position tracking | Color-coded Strategy Tags (A vs B) |
| **EXP-04** | Side-by-Side Comparative Analytics | A/B Testing | ✅ Active | Clear performance winner selection | Comparative Win Rate & P&L Cards |
| **EXP-05** | 20-Variant Strategy Incubator Grid | A/B Testing | ✅ Active | Forward-test 20 distinct parameter permutations concurrently | Multi-Variant Performance Matrix |
| **EXP-06** | StrategyBro 90 DTE Dynamic Iron Condor vs Vertical | A/B Testing | ✅ Active | Dynamic SD-based structure selection (IC near mean vs Call/Put vertical when stretched) with 30 DTE roll | Dynamic Strategy & SD Zone Pill |
| **BRK-01** | Tradier Sandbox & Live Execution | Broker Connectivity | ✅ Active | Zero-commission index options trading | Connection Status & Account Pill |
| **BRK-02** | Alpaca Paper & Live Execution | Broker Connectivity | ✅ Active | Secondary multi-asset broker support | Broker Selector Dropdown |
| **BRK-03** | OrbStack Native Containerization | DevOps / System | ✅ Active | Instant zero-config local deployment | Container Uptime & Health Indicator |
| **SYS-01** | Upstream Package & Import Hygiene | DevOps / System | ✅ Active | Clean native Python invocation without local directory namespace shadowing | Import verification test suite |
| **SYS-02** | Jev-Bot Multi-Entrypoint Lifecycle Verification | DevOps / System | ✅ Active | Isolated runner testing across CLI, module, and direct execution | Subprocess Lifecycle Test Suite |
| **BRK-04** | Tradier Log Sanitization & Polling Interval | Broker Connectivity | ✅ Active | Zero credential leakage in logs & configurable polling cadence | Connection Log Output & Polling Interval |
| **ENG-08** | Confirmed Opening Credit & Roll/DTE Priority | Core Engine | ✅ Active | Enforce confirmed cash credits & prioritize roll/DTE exits before valuation | Position State & Roll Status Pill |
| **BRK-05** | Tradier Host Routing & Resilient Close Package Retention | Broker Connectivity | ✅ Active | Atomic package routing with host tags, unique ID/legacy tag recovery & uncertain close locks | Order Routing & Package Recovery |
| **BRK-06** | In-Lock Panic Halt & Fill Reconciliation Safety | Broker Connectivity | 🔄 In Progress | In-lock halt evaluation, terminal fill verification, and cancellation barrier | Execution Safety & Order Routing |
| **SYS-03** | Shared Absolute Ledger & Recovery Packaging | DevOps / System | 🔄 In Progress | Common database path, execution lock, and container recovery packaging | Architecture & Container Build |
| **CFG-01** | Interactive Config & Credentials GUI | Configuration | ✅ Active | Edit and test API keys & risk boundaries live in GUI | Dedicated Broker & Strategy Config Tab |
| **GUI-01** | Live Engine Status & Session Clock | Dashboard UI | ✅ Active | Know exactly what the bot is doing | Header Status Bar & Market Clock |
| **GUI-02** | Interactive Visual Risk Dials | Dashboard UI | ✅ Active | At-a-glance capital & cap visibility | Dynamic Risk Gauges (Configured Strategy & Portfolio Caps) |
| **GUI-03** | Active Positions & Greeks Matrix | Dashboard UI | ✅ Active | Live monitoring of all 4 option legs | Interactive Table ($\Delta, \Theta, \text{IV}$) |
| **GUI-04** | Multi-Curve Payoff Explorer | Dashboard UI | ✅ Active | Visualize profit zones & breakevens | Interactive Plotly Payoff Chart |
| **GUI-05** | Real-Time Execution Log Console | Dashboard UI | ✅ Active | Instant auditability without terminal | Streaming Log Viewer with Filters |
| **GUI-06** | One-Click Emergency Control Center | Emergency Controls | ✅ Active | Verified liquidation, durable trading halt, and serialized order submission | Flatten Strategy A/B & Flatten All Buttons |
| **ML-01** | Regime & Volatility Trade-Entry Classifier | Machine Learning | 📋 Planned | Filter out losing trades in trending/high-vol regimes | Model Confidence Score & Go/No-Go Badge |
| **ML-02** | Predictive Early-Exit Sentinel | Machine Learning | 📋 Planned | Salvage capital before reaching hard stop loss | Early Warning Risk Meter & Dynamic Close Alert |
| **ML-03** | Dynamic Delta & Volatility Sizing Optimizer | Machine Learning | 📋 Planned | Adjust strikes based on predicted expected move | Dynamic Delta Recommendation Badge |
| **ML-04** | Intraday Trajectory Logger & Feature Store | Logging & Telemetry | ✅ Active | Log 5-min state-action-reward snapshots across all 20 strategies | Trajectory Stream Status & Replay Counter |
| **ML-05** | Model Decision Explainer & Confidence GUI | Dashboard UI | 📋 Planned | Transparent AI decision-making (why bot entered/scaled/exited) | Dedicated 🧠 ML Intelligence Dashboard Tab |
| **ML-06** | Multi-Head Neural Policy Network | Machine Learning | ✅ Active | Learn dynamic Add / Trim / Profit-Take / Strategy Selection across market regimes | Neural Action Probabilities & Dynamic TP Pill |
| **ML-07** | Counterfactual Strategy & Regret Evaluator | Trading Evaluation | ✅ Active | Evaluate decisions vs all 20 simultaneous variants (MFE/MAE, Sortino, Regret) | Counterfactual Regret Scorecard |
| **ML-08** | Self-Improving Continual Learning Loop | Machine Learning | 📋 Planned | Automated EOD retraining with Champion/Challenger shadow promotion gate | Model Version, Shadow Win-Rate & Promotion Badge |

---

## Detailed User Feature Specifications

### 1. Strategy Execution Engine

#### `ENG-01`: Automated Iron Condor Execution
* **User Story**: As a trader, I want the bot to automatically identify, price, and submit 4-leg Iron Condor orders so I do not need to manually calculate strikes and leg into orders.
* **Supported Underlyings**: `SPX` (Large Index), `XSP` (Mini-SPX Index, European-style cash-settled), `SPY` (ETF).
* **Execution Logic**:
  * Buy Long Put @ $(Strike_{Put} - Wing)$
  * Sell Short Put @ $Strike_{Put}$
  * Sell Short Call @ $Strike_{Call}$
  * Buy Long Call @ $(Strike_{Call} + Wing)$
* **GUI Manifestation**: Real-time position table showing all 4 legs, strike prices, entry credit, and live market mid-price.

#### `ENG-02`: Target Delta Strike Selection
* **User Story**: As a trader, I want to choose my short strike probability (e.g. 15-delta for high win rate, or 20-delta for higher premium) so the bot dynamically targets strikes according to prevailing implied volatility.
* **Parameters**: `IC_SHORT_DELTA` (default: `0.15`).
* **GUI Manifestation**: Interactive delta slider with visual indicators of distance OTM (Out of The Money %) and probability of expiring worthless.

#### `ENG-03`: Configurable Wing Width
* **User Story**: As a trader, I want to configure the spread between short and long strikes to match contract specifications (e.g. 2 points for XSP = \$200 risk; 15 points for SPX = \$1,500 risk).
* **Parameters**: `IC_WING_WIDTH` (default: `2.0` for XSP, `15.0` for SPX).
* **GUI Manifestation**: Numeric input control with real-time calculated maximum risk per contract displayed inline.

#### `ENG-04`: Target DTE Selection (0DTE vs Multi-Day)
* **User Story**: As a trader, I want to choose between 0DTE (intraday expiration) or 1DTE/swing expiration to capture rapid theta decay or trade around macro announcements.
* **Parameters**: `IC_DTE` (default: `0`).
* **GUI Manifestation**: DTE dropdown/pill selector showing exact expiration date (`YYYY-MM-DD`) and days remaining.

#### `ENG-05`: Timed Entry Window & Schedule Guard
* **User Story**: As a trader, I want the bot to support multi-window execution schedules throughout the trading day every 5 minutes from 9:45-10:00 AM and then every 15 minutes until 15:30 so all strategies continuously gather data and re-enter whenever positions are flat.
* **Parameters**: `IC_ENTRY_TIME` (default: `"09:45, 09:50, 09:55, 10:00, 10:15, 10:30, 10:45, 11:00, 11:15, 11:30, 11:45, 12:00, 12:15, 12:30, 12:45, 13:00, 13:15, 13:30, 13:45, 14:00, 14:15, 14:30, 14:45, 15:00, 15:15, 15:30"`).
* **GUI Manifestation**: Real-time entry schedule badge showing active window status (e.g. `09:45 - 15:30 ET (5m/15m)`) with hard cutoff guard at 15:31 to prevent end-of-day assignment or pin risk.

#### `ENG-06` & `ENG-07`: Automated Profit Target & Stop Loss
* **User Story**: As a trader, I want predefined exit rules to lock in profits early (e.g. 30% or 50% of credit) and cut losses if market moves sharply against a wing (e.g. 2.0x credit).
* **Parameters**: `IC_PROFIT_TARGET` (default: `0.30`), `IC_STOP_LOSS_MULT` (default: `2.0`).
* **GUI Manifestation**: Progress bar showing current P&L relative to the Take-Profit and Stop-Loss boundaries, color-coded green/red.

#### `ENG-08`: End-of-Day Auto-Close Safety Net
* **User Story**: As a trader, I want any remaining open 0DTE positions to close prior to market close (3:55 PM ET) to avoid unexpected after-hours moves or assignment issues.
* **GUI Manifestation**: EOD Countdown timer and auto-liquidation safety badge.

---

### 2. Capital & Risk Management

#### `RSK-01`: $5,000 Per-Strategy Open Position Cap
* **User Story**: As a trader, I want an absolute dollar ceiling on the defined risk of any individual strategy variant so that unforeseen market shocks can never threaten more than \$5,000 on any single strategy, while allowing multiple concurrent incubator variants to trade in parallel.
* **Parameters**: `IC_MAX_POSITION_RISK` (default: `5000.0`).
* **Behavior**:
  $$\text{Strategy Exposure} = \text{Existing Variant Exposure} + \text{New Trade Max Risk} \le \$5,000.00$$
* **GUI Manifestation**: Per-Strategy Risk Progress Bar with percentage utilization dial (e.g., "$1,400 / $5,000 Committed (28%)").

#### `RSK-02`: Tiered Single-Trade Sizing (Sliding Scale)
* **User Story**: As a trader, I want single trades restricted to 30% of my risk cap when the account/cap is small ($5k), scaling down to 10% or 5% as capital grows, to prevent single-trade blowups.
* **Parameters**: `IC_MAX_SINGLE_TRADE_PCT` (default: sliding scale).
* **Sliding Scale Schedule**:
  * $\le \$5,000$: **30%** (\$1,500 max per trade)
  * $\le \$10,000$: **20%** (\$2,000 max per trade)
  * $\le \$25,000$: **15%** (\$3,750 max per trade)
  * $\le \$50,000$: **10%** (\$5,000 max per trade)
  * $> \$50,000$: **5%**
* **GUI Manifestation**: Single-Trade Risk Meter showing allowable risk budget vs configured trade risk.

#### `RSK-03`: Intelligent Contract Auto-Scaling
* **User Story**: As a trader, if I set a quantity that exceeds the single-trade risk limit, I want the bot to automatically scale down the contract quantity rather than rejecting the trade.
* **Formula**:
  $$\text{Allowed Qty} = \left\lfloor \frac{\text{Max Risk Cap} \times \text{Single Trade \%}}{\text{Wing Width} \times 100} \right\rfloor$$
* **GUI Manifestation**: Notification pill showing original requested quantity vs auto-scaled safe quantity (e.g., `Qty: 10 ➔ 7 (Safe Scale)`).

---

### 3. A/B Strategy Multi-Execution Lab

#### `EXP-01` & `EXP-02`: Multi-Strategy A/B Engine & Independent Parameters
* **User Story**: As a trader, I want to run two strategy variants simultaneously (e.g., Strategy A: 15-delta baseline vs Strategy B: 20-delta challenger) to see in real-time which parameter set yields higher risk-adjusted return.
* **Architecture**:
  * **Strategy A (Baseline)**: Conservative 15Δ, 30% Profit Target, 2.0x Stop Loss.
  * **Strategy B (Challenger)**: Aggressive 20Δ, 50% Profit Target, 1.5x Stop Loss.
* **GUI Manifestation**: Dual Strategy Overview cards with parameter tabs, toggle switches, and live execution status.

#### `EXP-03`: Isolated Position Tagging
* **User Story**: As a trader, I want orders and positions clearly tagged with their strategy ID so fills and performance metrics are never mixed together.
* **GUI Manifestation**: Color-coded badges (`[Strategy A: Blue]`, `[Strategy B: Orange]`) in the position table and trade activity log.

#### `EXP-04`: Side-by-Side Comparative Analytics
* **User Story**: As a trader, I want a comparison scorecard showing which strategy is winning on Win Rate, Total P&L, Average Trade Duration, and Sharpe Ratio.
* **GUI Manifestation**: Scorecard comparison table and cumulative P&L chart comparing Curve A vs Curve B over time.

#### `EXP-05`: 20-Variant Strategy Incubator Grid & Shared Market Data Hub
* **User Story**: As a quant trader, I want to launch 20 parameter variations of Iron Condors concurrently on my \$100k sandbox account to generate live counterfactual training trajectories across diverse market regimes without financial risk or broker API rate-limiting.
* **Strategy Pipeline Architecture**:
  * **Factorial Parameter Grid**: Varies Short Delta ($\Delta 0.08, 0.12, 0.16, 0.22$), Profit Target ($20\%, 30\%, 50\%, 70\%$), Stop Loss ($1.5\times, 2.0\times, 2.5\times, 3.0\times$), Wing Width ($2.0\text{ pt}, 5.0\text{ pt}$), and Entry Window ($10:00, 11:30, 13:30\text{ ET}$).
  * **Per-Strategy Position & Ledger Isolation**: Each variant (`IC_V01` $\dots$ `IC_V20`) tracks its own leg inventory, unrealized P&L, and Greeks independently so exits in one variant never interfere with others.
  * **Shared `MarketDataHub` Cache**: Fetches underlying quotes, intraday bars, VIX term structure, and option chains once per 5-minute tick and fans them out in-memory to all 20 strategies + the neural network.
  * **Sizing**: 1 contract of `XSP` per variant ($\sim \$200$ risk each), requiring $\sim \$4,000$ margin total out of $\$100,000$ sandbox cash.
* **GUI Manifestation**: Interactive 20-variant performance leaderboard table and multi-curve equity growth chart.

#### `EXP-06`: StrategyBro 90 DTE Dynamic Iron Condor vs Vertical Spread
* **User Story**: As an options quant, I want to trade a 90 DTE dynamic strategy that automatically selects an Iron Condor when price is near the mean (low SD) and dynamically switches to selling the stretched-side Vertical spread (Bear Call when positive stretched, Bull Put when negative stretched), with a 30 DTE roll trigger and quality filter.
* **Core Logic & Preference Rules**:
  * **Entry Quality Filter**: Evaluates the underlying spot Z-score (standard deviations from mean):
    * `Abs SD <= 0.5`: Excellent
    * `0.5 < Abs SD <= 1.0`: Good
    * `1.0 < Abs SD <= 1.5`: Marginal
    * `Abs SD > 1.5`: Avoid (entry suppressed)
  * **Structure Preference**:
    * `Abs SD <= 0.60` $\to$ Enter Iron Condor (neutral theta harvest near mean)
    * `Abs SD > 0.60 & positive` $\to$ Sell Call Vertical (`bear_call`, short call + long protective call)
    * `Abs SD > 0.60 & negative` $\to$ Sell Put Vertical (`bull_put`, short put + long protective put)
  * **Parameters**: 90 DTE target, 15 pt wing width, 7% OTM put/call strikes (`otm_pct=0.07`), 50% profit target, 2.0x stop loss.
  * **Staggered Playbook Management**:
    * **Front (~30 DTE)**: Active roll/management trigger closes the position at 30 DTE remaining (or at 50% net profit target).
    * **Middle (~60 DTE)**: Hold and monitor marked valuation.
    * **Back (~90 DTE)**: Fresh entry only in verified good SD zone (`Abs SD <= 1.5`).

---

### 4. Broker Connectivity & Infrastructure

#### `BRK-01` & `BRK-02`: Multi-Broker Tradier & Alpaca Execution
* **User Story**: As a trader, I want to trade index options via Tradier Sandbox/Live or test stock/ETF options via Alpaca Paper/Live using the same codebase.
* **Parameters**: `TRADING_BROKER=tradier` or `TRADING_BROKER=alpaca`.
* **Key Reconciliation & Safety Capabilities (`BRK-01`)**:
  * **Durable Multileg Parent/Child Reconciliation**: Reconciles multileg parent order IDs with individual child leg fill IDs in SQLite, handling partial fills and asynchronous broker updates.
  * **Multi-Day Position Ledger Rollover**: Maintains position packages across market sessions and restarts without dropping open exposure when broker valuation quotes are temporarily missing.
  * **Dynamic Close Repricing & Limit Protection**: Protects exits from illiquidity via configurable repricing cycles (`IC_CLOSE_REPRICE_SECONDS`, `IC_CLOSE_MAX_REPRICES`, `IC_CLOSE_PRICE_STEP`) before falling back to market orders (`IC_CLOSE_ALLOW_MARKET=false`).
  * **Accurate Aggregate-Cash & Per-Contract Fee Accounting**: Tracks real cash flows, fees (`OPTION_FEE_PER_CONTRACT`), scale-ins (`ADD`), trims (`TRIM`), and net realized P&L.
  * **Process Fault Containment & Automatic Recovery**: Docker `unless-stopped` restart policy and automated retry backoff (`PROCESS_RESTART_BACKOFF_SECONDS`), resuming from durable SQLite ledger without state corruption.
* **GUI Manifestation**: Broker status badge showing current active broker, account number, option approval level, and ping latency.

#### `BRK-03`: Native OrbStack Docker Containerization
* **User Story**: As a developer/trader, I want the bot and dashboard to run inside isolated, lightweight Docker containers on OrbStack without polluting my local host system.
* **GUI Manifestation**: System container uptime badge and memory/CPU resource tracker.

#### `CFG-01`: Interactive Config & Credentials GUI
* **User Story**: As a trader, I want a dedicated configuration tab in the UI to enter, test, and save my API keys (Tradier, Alpaca, Lumiwealth), switch between paper and live trading, tune Iron Condor parameters (delta, wing width, quantity, entry time, profit target, stop loss), and adjust capital risk ceilings ($5k cap, single-trade %) without having to manually edit config files in a terminal.
* **Key Capabilities**:
  * **Interactive Credential Inputs**: Secure password fields for API keys and account numbers.
  * **Live Connection Testers**: "🔌 Test Tradier Connection" and "🔌 Test Alpaca Connection" buttons that make authenticated API calls directly to verify credentials, fetching current equity and buying power instantly.
  * **Direct Persistence**: Saves changes atomically to `.env` with validation.
  * **Optional Botspot Tracking**: Enter optional `LUMIWEALTH_API_KEY` for cloud performance tracking on botspot.trade.
* **GUI Manifestation**: Dedicated `⚙️ Broker & Strategy Config` dashboard tab.

---

### 5. Trading Overview GUI (Dashboard)

#### `GUI-01`: Live Status Header & Market Clock
* Displays current local time, market session state (Pre-Market / Open / Post-Market), next bot iteration time, and account balance / buying power.

#### `GUI-02`: Interactive Visual Risk Gauges
* Real-time radial dials and progress bars for:
  1. **Global Open Position Exposure** (Current \$ vs configured portfolio cap, default \$100,000).
  2. **Single-Trade / Strategy Sizing Utilization** (Current \$ vs configured strategy risk cap, default \$15,000).
  3. **Option Buying Power Headroom**.

#### `GUI-03`: Active Positions & Greeks Matrix
* Interactive real-time table displaying:
  * Strategy Tag
  * Asset / Leg (Long Put, Short Put, Short Call, Long Call)
  * Strike Price & Expiration
  * Current Mid-Price vs Entry Price
  * Greeks: Delta ($\Delta$), Theta ($\Theta$), Implied Volatility ($\text{IV}$)
  * Unrealized P&L (\$ and %)

#### `GUI-04`: Multi-Curve Payoff Explorer
* Interactive Plotly payoff graph illustrating expiration P&L curves across underlying price points, highlighting current price, breakeven boundaries, max profit zone, and max loss wings.
* Supports overlaying Strategy A vs Strategy B curves.

#### `GUI-05`: Real-Time Execution Log Console
* Streaming, searchable, filterable log viewer embedded directly in the dashboard displaying heartbeat check-ins, order submissions, fills, and risk warnings without opening terminal.

#### `GUI-06`: One-Click Emergency Control Center
* Prominent, guarded action buttons:
  * **Flatten Strategy A**: Liquidates only Strategy A's open legs.
  * **Flatten Strategy B**: Liquidates only Strategy B's open legs.
  * **Emergency Flatten All**: Liquidates all open positions across all strategies immediately via verified broker liquidation (canceling working orders, closing short option legs before protective longs, and polling until flat).
  * **Cancel All Orders**: Cancels any unfilled/working orders.
  * **Trading Pause / Durable Panic Halt**: Emergency liquidation engages a durable SQLite panic halt (`set_panic_halt(True)`) and a shared execution gate that blocks concurrent strategy submissions. Strategy iterations retry liquidation while halted; an independent recovery supervisor with startup and shutdown is still pending.

---

### 6. Machine Learning Pipeline & Strategy Intelligence

#### `ML-01`: Regime & Volatility Trade-Entry Classifier
* **User Story**: As a trader, I want an ML model to analyze current market conditions prior to entering an Iron Condor, so the bot only takes trades when market conditions favor mean-reversion and range-bound price action.
* **Problem Solved**: Naive time-based entry (e.g. blindly entering every day at 10:00 AM) suffers major drawdowns on trend days, high-volatility expansions, or post-CPI/FOMC shock sessions.
* **Model Formulation**:
  * **Classification Target**: Binary $Y \in \{0, 1\}$. $Y = 1$ if an Iron Condor reaches profit target ($+30\%$) before stop loss ($-2.0\times$ credit); $Y = 0$ if breached or stopped out.
  * **Input Feature Vector**:
    * **Volatility Structure**: VIX level, VIX 5-day / 20-day ratio, VIX9D/VIX term structure ratio (identifies contango vs backwardation stress), Implied Volatility Rank (IVR).
    * **Intraday Price Dynamics**: Distance to VWAP, 30-minute Opening Range Breakout (ORB) ratio, Average True Range relative to price ($\text{ATR}_{14} / \text{Price}$), 14-period RSI.
    * **Macro/Calendar Filters**: Scheduled high-impact macro releases (CPI, PPI, FOMC, Non-Farm Payrolls), day of week, time of day.
  * **Execution Rule**: If predicted win probability $P(\text{Win}) \ge \text{Confidence Threshold}$ (e.g. $65\%$), permit entry. Otherwise, log `[ML Entry Filter: Win prob 42% < 65% — Abstaining]` and skip trade.
* **GUI Manifestation**: Confidence Score Meter & Go / No-Go Decision Badge on dashboard.

#### `ML-02`: Predictive Early-Exit & Risk Sentinel
* **User Story**: As a trader, I want an ML model monitoring active positions during market hours to predict whether a challenged position has an elevated probability of breaching maximum loss, so the bot can exit early and salvage capital rather than suffering the full $2.0\times$ stop loss.
* **Problem Solved**: Cutting losses at $-50\%$ to $-75\%$ instead of letting full stop-losses hit ($-200\%$) dramatically improves overall strategy Sortino and profit factor.
* **Model Formulation**:
  * **Hazard / Survival Model**: Evaluates the probability of short strike breach within the remaining trading session given current price velocity, delta drift $\Delta_{net}$, and order book momentum.
  * **Trigger**: If $P(\text{Breach}) > 0.70$, issue an early exit command: `[ML Sentinel: High breach probability (78%) — Liquidating early]`.
* **GUI Manifestation**: Real-time Early Warning Risk Gauge with dynamic close alert.

#### `ML-03`: Dynamic Delta & Volatility Sizing Optimizer
* **User Story**: As a trader, I want the bot to automatically adjust its target delta (e.g. from 15Δ down to 10Δ in elevated vol, or up to 20Δ in low-vol calm regimes) based on predicted expected move, maximizing premium collection while keeping pin risk low.
* **GUI Manifestation**: Recommended Delta badge comparing static baseline vs ML-optimized strike targeting.

#### `ML-04`: Intraday Trajectory Logger & Feature Store
* **User Story**: As a quant/developer, I want every 5-minute tick across the market and all 20 simultaneous strategies logged as structured State-Action-Reward trajectories (`logs/trajectories.jsonl`), alongside a reproducible feature pipeline with strict purged walk-forward cross-validation (zero lookahead bias).
* **Logged State Snapshot ($S_t$ every 5 mins)**:
  * **Market Regime Signals**: `vix`, `vix_term_ratio` ($\text{VIX9D}/\text{VIX}$), `vwap_distance`, `orb_30m_range`, `atr_pct`, `rsi_14`, `price_velocity_15m`, `minutes_to_close`.
  * **Cross-Grid Signals**: Real-time unrealized P&L dispersion, wing stress (put vs. call pressure), and aggregate Greek drift ($\Delta_{\text{net}}, \Gamma_{\text{net}}, \Theta_{\text{net}}, \mathcal{V}_{\text{net}}$) across the 20 strategies.
  * **Hindsight Relabeling**: At market close, computes the counterfactual optimal action at each 5-minute timestamp (whether adding, trimming, holding, or taking profit yielded the highest risk-adjusted outcome).
* **Deliverables**: `trade_logger.py`, `logs/trajectories.jsonl`, `ml/features.py`, `ml/train.py`, `ml/backtest.py`, `ml/models/`.

#### `ML-05`: Model Decision Explainer & Confidence GUI
* **User Story**: As a trader, I want total transparency into the neural network's decision-making so I never have to guess why the bot entered, scaled in, trimmed risk, or took profits early.
* **GUI Manifestation**: Dedicated **🧠 ML Strategy Intelligence** dashboard tab showing:
  * Current Latent Market Regime badge (`Low-Vol Pin`, `Mean-Reverting Chop`, `Directional Trend`, `High-Vol Expansion`)
  * Live Action Probability Bars (`Hold`, `Add to Position`, `Trim Position`, `Take Profit / Flatten`)
  * Top Feature Attribution waterfall / bar chart (e.g. VIX Term Structure: $+14\%$, 20-Grid Put Wing Stress: $-28\%$)
  * Historical Neural Decisions vs. Counterfactual Best-of-20 Outcome log.

#### `ML-06`: Multi-Head Neural Policy Network (Dynamic Position & Profit Manager)
* **User Story**: As a portfolio manager, I want a neural network trained on the 20 simultaneous strategy signals and market dynamics to decide the next best action at every 5-minute tick: when to **add to a position**, when to **remove/trim a position**, when to **take profits dynamically**, and **which strategy parameters** fit the current market regime.
* **Neural Architecture (`ml/policy_net.py`)**:
  * **Shared Temporal Regime Encoder**: Encodes trailing 5-minute market features + cross-strategy signals from the 20 variants into a latent regime embedding $z_t$.
  * **Head 1 — Position Sizing & Scaling (`ADD` / `HOLD` / `TRIM` / `FLATTEN`)**:
    * Scales into calm, mean-reverting setups (`ADD_POSITION`) within `RSK-01`/`RSK-02` limits.
    * Scales out (`TRIM_POSITION` by 50% or closes threatened wing) when wider-delta cohorts in the 20-grid show accelerating adverse $\Delta/\Gamma$ drift.
  * **Head 2 — Dynamic Profit-Taking Threshold ($\text{TP}_t \in [0.15, 0.80]$)**:
    * Predicts expected continuation value $Q(S_t, \text{Hold}) - Q(S_t, \text{Exit})$. Takes quick $20\text{--}25\%$ profits in high-gamma chop and lets winners decay to $60\text{--}75\%$ in pinned low-vol sessions.
  * **Head 3 — Contextual Strategy Selector ($A^* \in \{1, \dots, 20\}$)**:
    * Scores all 20 parameter variations given current market context $S_t$ to select optimal delta, wing width, and entry window.
* **GUI Manifestation**: "🎯 Neural Policy Recommendation" panel with live action distribution, dynamic profit target line, and selected strategy variant badge.
* **Deliverables**: `ml/policy_net.py`, `ml/train.py`, `ml/inference.py`, `tests/test_policy_net.py`, `tests/test_policy_pipeline.py`.

#### `ML-07`: Counterfactual Strategy & Regret Evaluator
* **User Story**: As a quant trader, I want the trading pipeline to evaluate every live decision against all 20 simultaneous variants using risk-adjusted and counterfactual metrics, so we measure whether the bot is genuinely making better decisions than static rules.
* **Evaluation Metrics (`ml/evaluator.py`)**:
  * **Counterfactual Regret**: $\text{Regret}_t = \max_{i \in \{1..20\}} R_t(A_i) - R_t(A_{\text{bot}})$, tracking how close the neural policy gets to the hindsight-best strategy each session.
  * **Capture Efficiency (MFE / MAE)**: Ratio of realized profit to Maximum Favorable Excursion (measuring profit-taking timing) and Maximum Adverse Excursion avoided via early trims.
  * **Regime-Stratified Sortino & Profit Factor**: Performance broken down by market regime (`Low-Vol`, `Trend`, `High-Vol`).
* **GUI Manifestation**: Counterfactual Regret Scorecard and MFE/MAE Capture Efficiency gauge.

#### `ML-08`: Self-Improving Continual Learning Loop (Champion vs. Challenger Gate)
* **User Story**: As a trader, I want the bot to automatically learn from each day's 20-strategy execution trajectories after market close and safely promote improved neural network weights without risking untested models in live trading.
* **Self-Improvement Lifecycle (`ml/continual_learner.py`)**:
  1. **Post-Market Ingestion (16:15 ET)**: Reconciles all 20 strategy trajectories from `logs/trajectories.jsonl` and `logs/trades.jsonl`, computing hindsight-optimal scaling and exit labels.
  2. **Offline Policy Update**: Trains a **Challenger Model** on the expanding replay buffer using Purged Walk-Forward Cross-Validation (preventing temporal leakage).
  3. **Shadow Mode Evaluation**: The Challenger runs alongside the active **Champion Model** in shadow mode, logging predicted decisions without executing orders.
  4. **Automated Promotion Gate**: Promotes Challenger $\to$ Champion only when out-of-sample Sortino ratio, drawdown control, and counterfactual regret outperform the Champion over the validation window.
  5. **Immutable Risk Guardrails**: Every neural network action is strictly bounded by deterministic risk checks (`RSK-01` \$5,000 global cap and `RSK-02` 30%$\to$5% single-trade limit) in [`SPXIronCondor._check_allocation`](file:///Users/yevgeniysaykin/Documents/Projects/lumibot/strategies/iron_condor.py#L439-L499).
* **GUI Manifestation**: Model Lineage card showing Active Champion version, Challenger shadow performance delta, and one-click rollback control.

---

### 7. System Architecture & Environment Hygiene

#### `SYS-01`: Upstream Package & Import Hygiene
* **User Story**: As a developer running tests or trading scripts outside Docker, I want `import lumibot` to resolve directly to the upstream installed package without conflict or shadowing from local mock prototypes.
* **Problem Solved**: Untracked local mock directory `./lumibot` shadowed the real package during native `python` and `pytest` runs, causing import errors and broken class resolution.
* **Resolution**: Relocated prototype code to `.legacy/mock_ui_lumibot_shim/` (ignored via `.gitignore`), and implemented `tests/test_import_hygiene.py` to prevent local directory shadowing.

---

### 8. Execution Robustness, Multi-Expiry Trading & Real-Time Telemetry

#### `TRD-04`: Expanded 30-Minute Intraday Trading Schedule
* **User Story**: As a trader, I want strategies to evaluate entry opportunities throughout the entire trading day up until 3:31 PM ET every 30 minutes, so we capture recurring intraday premium opportunities and collect rich performance data.
* **Implementation**: Expanded execution times across all 25 variants to 15 slots (`09:45, 10:00, 10:15, 10:30, 11:00, 11:30, 12:00, 12:30, 13:00, 13:30, 14:00, 14:30, 15:00, 15:30, 15:31`) with a strict hard cutoff at 3:31 PM ET to eliminate late-day assignment risk.

#### `RSK-05`: Per-Strategy Risk Isolation & Portfolio Exposure Cap
* **User Story**: As a portfolio manager running 20+ concurrent variants, I want each strategy to enforce its defined risk cap ($15,000 default) independently against its own positions, so open exposure in one variant does not block unrelated variants, with an overarching $100,000 global portfolio cap.
* **Implementation**: Scoped `portfolio_defined_risk(strategy_id)` per variant, decoupling peer exposure while maintaining global risk checks against `IC_MAX_PORTFOLIO_RISK`.

#### `EXE-02`: Expiration-Specific Strike Filtering
* **User Story**: As a trader running multi-day strategies (7DTE, 14DTE, 21DTE, 30DTE, 90DTE), I want strike selection to only choose strikes that physically exist for that exact expiration date, preventing invalid quote errors and aborted order placements.
* **Implementation**: Updated `_fetch_strikes(chains, expiry=expiry)` to extract strikes directly from `chains["Chains"]["CALL"][exp_str]` and `["PUT"][exp_str]`, eliminating cross-expiration strike bleed from 0DTE union lists.

#### `EXE-03`: Opposing-Leg Conflict Pre-Submission Gate
* **User Story**: As a trader executing concurrent multi-leg strategies on Tradier, I want the bot to reject proposed order legs that directly conflict with open positions on the opposite side of the same contract, avoiding Tradier's "Buy order cannot be placed to cover short position" rejection.
* **Implementation**: Added pre-submission cross-checks against `PositionLedger.all_owned_quantities()` before routing orders to the broker.

#### `DASH-06`: Immediate Trade Inception Counting & Live Open Position Visibility
* **User Story**: As a trader watching the dashboard, I want newly opened trades to appear immediately at the top of the trade log and be counted in strategy summary rows right when entered, without waiting for the trade to close.
* **Implementation**: Formatted open trades with live status tokens (`status="OPEN"`, `net_pnl_str="OPEN"`, cyan styling), prepended them to `visible_trades_50 = (open_all + closed_all)[:50]`, and unified trade counts as `total_trades = open + closed`.

#### `ANL-03`: Fee-Adjusted Net P&L Win Rate Integrity
* **User Story**: As an analyst auditing performance, I want trades with negative net P&L after commission fees to be strictly classified as losses, ensuring win rates never show 100% on net unprofitable trades.
* **Implementation**: Updated win rate and loss counters in `adapters.py` to evaluate strictly against `net_pnl > 0`, reflecting real financial performance.

#### `STRAT-03`: StrategyBro 90 DTE Dynamic Iron Condor vs Vertical Spread Variant (`IC_V25`)
* **User Story**: As a trader, I want a long-dated 90 DTE variant that dynamically shifts between an Iron Condor and a directional Vertical spread based on underlying price Z-score standard deviation, utilizing % OTM strikes and rolling at 30 DTE.
* **Implementation**: Integrated `evaluate_strategybro_entry_quality`, `resolve_strategybro_structure`, `select_short_strikes_otm_pct`, and 30 DTE early roll trigger into `IC_V25_90DTE_STRATEGYBRO_DYNAMIC`.

#### `BRK-04`: Tradier Log Sanitization & Polling Interval Configuration
* **User Story**: As a security-conscious operator, I want Tradier connection logs to strictly redact account numbers and sensitive tokens, while allowing configurable polling cadence between live and paper modes.
* **Implementation**: Redacted `ACCOUNT_NUMBER` from connection log output in `main.py`, logging only paper mode status and polling interval, and parsed `TRADIER_POLLING_INTERVAL` with a 1.0s floor (default 15.0s paper / 5.0s live).

#### `ENG-08`: Confirmed Opening Credit Enforcement & Roll/DTE Exit Precedence
* **User Story**: As an automated trader, I want exit management to require confirmed cash credits rather than falling back to limit prices, strictly retain contract ownership during quote delays, and evaluate roll/DTE targets before valuation checks.
* **Implementation**: Eliminated limit-price credit fallback in `strategies/iron_condor.py`, ensuring only confirmed positive cash credits drive P&L exits while unconfirmed contracts remain open. Reordered `_manage_position` so `roll_dte` exits trigger even if quote marks or credit baselines are temporarily delayed.

#### `SYS-02`: Jev-Bot Multi-Entrypoint Lifecycle Verification
* **User Story**: As a systems engineer running multiple bot repositories, I want Jev-Bot to maintain deterministic lifecycle steps, signal cleanup, and verified invocation across script, module, and installed CLI entrypoints.
* **Implementation**: Added isolated subprocess tests covering root `main.py`, module `-m jev_bot`, and installed console entrypoint `jev-bot`, verifying single-step iteration and graceful termination.

#### `BRK-05`: Tradier Host Routing & Resilient Close Package Retention
* **User Story**: As an automated trader executing atomic multileg packages across multiple strategy variants on Tradier, I want packages to route using the registered host strategy tag while maintaining discrete package identities and ledger owners, resolving orders by verified parent/child IDs rather than shared tags, and locking uncertain closing orders to prevent duplicate submissions during broker latency or timeouts.
* **Implementation**: Added normalized `_broker_routing_tag()` derived from the registered host name in `strategies/iron_condor.py`. Restricted `_find_order_by_tag` to unique legacy package tags (`IC_<hex>`) or verified broker IDs, preventing cross-strategy order collisions. When closing package orders are unresolved or missing from active broker queries, set `_recovery_lock = True` to retain contract ownership and prevent duplicate exit order submissions until broker state confirms terminal status.

#### `SYS-03`: Shared Absolute Ledger Resolution & Emergency Recovery Packaging
* **User Story**: As a platform operator running containerized bot and dashboard services, I want both services to resolve relative ledger paths to the identical shared absolute database and cross-process execution lock, and I want emergency broker recovery dependencies packaged in the trading bot image to prevent `ModuleNotFoundError` during liquidation retries.
* **Implementation**: Added `resolve_ledger_path()` in `strategies/position_ledger.py` anchoring paths to project root, configured `POSITION_LEDGER_PATH=/app/logs/position_ledger.sqlite3` in `docker-compose.yml`, and added `dashboard/broker_client.py` and `dashboard/presentation.py` to `Dockerfile`.

#### `BRK-06`: In-Lock Panic Halt Verification & Fill Reconciliation Safety
* **User Story**: As an automated trader, I want the bot to recheck the durable panic halt immediately inside the cross-process execution lock before submitting orders, and retain terminal opening packages with missing or unbalanced fills as partial until cumulative broker evidence is verified.
* **Implementation**: Relocated panic halt checks in `SPXIronCondor` directly inside `execution_gate()`, enforced verified leg execution evidence before resolving terminal packages in `PositionLedger`, and added a reconciliation check prior to closing restarted entries.
