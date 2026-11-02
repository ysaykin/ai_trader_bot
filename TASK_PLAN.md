# Implementation task plan

## Position workspace implementation, 2026-11-01

Implement the reviewed position workflow in the active Reflex dashboard. Preserve
the existing execution fixes and the user's uncommitted task-plan updates.

1. Read durable package fills through a read-only SQLite snapshot. Build a pure
   position model in `dashboard/positions.py`, with a stable trade identity,
   signed leg inventory, open and realized P&L, fee provenance, and reconciliation.
2. Join broker contracts and quotes to confirmed ownership. Keep unmatched
   inventory visible and mark missing or conflicting data unavailable.
3. Add strategy/structure/underlying/expiry filters, expandable grouped trades,
   current strategy comparisons, and actual-leg expiration payoff charts.
4. Reuse the shared numeric formatting policy and make Positions the landing view.
5. Verify ledger lifecycle and valuation with deterministic fixtures, compile
   Reflex, exercise the UI, and update backlog statuses only for verified scope.

Design: the UI calls one position-snapshot adapter and derives filtered views
from that immutable snapshot. Financial calculations stay outside Reflex state.
Grouping broker symbols heuristically was rejected because shared contracts can
belong to different trades. Confirmed ledger trade IDs establish ownership;
legacy close fills are linked only when exactly one compatible owner exists.
Uncertain links remain visible as reconciliation issues. Expiration curves are
restricted to one underlying and expiry, with fee estimates labeled separately
from reported fees. No execution or account configuration changes are needed.

Implemented and verified on 2026-11-01. The running dashboard now groups 11
held trades and provides structure/strategy filters, leg expansion, current
strategy comparison and actual-leg expiration graphs. Fees identify their
reported or estimated source. Negative values use shared sign-aware colors.
Read-only ownership and grouped overview tasks are complete. Extended filters,
historical comparisons, theoretical today/mixed-expiry analysis and remaining
legacy telemetry gaps retain explicit follow-ups in `.scrum/TASKS.yaml`.

Verification: 71 tests and 6 subtests pass across `test_position_workspace.py`,
`test_dashboard_integrity.py`, `test_reflex_dashboard.py`, `test_dashboard.py`,
`test_position_ledger.py`, and `test_iron_condor_math.py`. Reflex 0.10.1
`compile --dry` passes. The isolated Python 3.12 environment is under `/tmp`;
existing virtual environments point to Linux interpreters unavailable on macOS.
The dashboard service was restarted to load the external read-only position
module; the trading bot was not restarted. Browser verification also exposed
inconsistent leg marks implying loss beyond the condor's defined payoff range.
Those baskets now show unavailable marked P&L and a quote-quality explanation;
confirmed-fill expiration analysis remains available.

## Task

Implement the approved Tradier configuration, durable iron-condor ownership and
valuation safeguards, offline policy training, opt-in live inference, and their
regression tests. Audit the existing Jev-bot scaffold and keep it explicitly
broker-agnostic until a real execution adapter exists.

## Order of work

1. Add pure iron-condor math and a transactional SQLite position ledger.
2. Wire strict environment parsing, Tradier selection, runtime validation, the
   shared ledger, and optional policy inference into the entrypoint.
3. Change strategy submission and reconciliation so package submission is
   atomic and fills, rather than submissions, establish ownership.
4. Add offline training and live normalization/imputation around the existing
   multi-head policy network, including strict JSON state validation.
5. Align dashboard settings, packaging, container setup, and documentation.
6. Audit the existing Jev-bot modules, configuration loader, entrypoints, and
   tests without claiming that configuration alone provides live brokerage.
7. Run focused tests, the full test suites, compile all modified Python files,
   and inspect the final diff for out-of-scope changes.
8. [Completed] Implement StrategyBro 90 DTE dynamic Iron Condor vs Vertical Spread variant (`IC_V25`) with Z-score regime trigger, % OTM strikes, and 30 DTE roll trigger.
9. [Completed] Expand intraday execution schedule through 3:31 PM ET every 30 minutes across all strategies with hard 15:31 cutoff.
10. [Completed] Enforce per-strategy risk limit isolation ($15k default) decoupled from global portfolio cap ($100k) to prevent peer blocking.
11. [Completed] Implement expiration-specific option chain strike filtering to eliminate invalid cross-expiry strike selection and quote rejections.
12. [Completed] Implement opposing-leg conflict pre-submission gate against owned quantities to prevent broker "Buy to Cover" rejection.
13. [Completed] Enable immediate trade counting upon inception (`total_trades = open + closed`) and live open position streaming in the dashboard.
14. [Completed] Enforce fee-adjusted net P&L win rate calculation so negative net returns are strictly accounted as losses.

## Assumptions and fixed decisions

- `TRADIER_PAPER` has precedence over `TRADIER_IS_PAPER`, then
  `TRADIER_SANDBOX`.
- Missing quotes, unbalanced baskets, or failed reconciliation produce unknown
  valuation/risk and block new exposure.
- A submitted package is not a filled package. Ledger ownership changes only
  when broker fill data confirms signed quantities and prices.
- Policy inference remains disabled unless `ML_POLICY_ENABLED=true`; enabling
  it with a missing or incompatible model is a startup error.
- Tradier Sandbox with SPX or XSP requires a configured non-Tradier data source.
- Existing uncommitted Jev-bot files belong to the user and will be preserved.

## Verification gates

- New unit tests cover math signs, incomplete marks, ledger ownership,
  JSON/list model state, bool rejection, trainer artifacts, inference
  normalization, strict broker booleans, and unmanaged-position recovery.
- Existing regression, dashboard, trajectory, and policy tests remain green.
- `python3 -m py_compile` passes for every modified Python file in both projects.
- Jev-bot's unit suite and CLI one-shot path pass locally.

## Dashboard review and backlog audit, 2026-10-30

Scope: review the running Reflex dashboard, compare position grouping and risk
graphs with thinkorswim and tastytrade, and update `.scrum/TASKS.yaml`.
Dashboard implementation belongs to the pending tasks recorded there.

1. Inspect the live positions and A/B views and trace their data sources.
2. Audit signed-value colors, quote availability, risk labels, and feature claims.
3. Record implementation tasks with priorities, dependencies, and acceptance criteria.
4. Reconcile outdated completion claims without replacing existing user work.
5. Add and run a read-only source audit, validate the backlog, and preview the
   proposed position and strategy comparison workflow with sample data.

Review completed on 2026-10-31. Ten dashboard implementation tasks are pending
in `.scrum/TASKS.yaml`, and feature-catalog reconciliation is reopened. The
follow-up audit separates overlapping legacy telemetry work and links package
valuation to the existing fee, event-aggregation, and ownership fixes.
The source audit runs without importing the app or contacting a broker:
`python3 tools/audit_dashboard_ui.py`.
The sample preview was checked for grouped totals, strategy filtering, leg
expansion, expiration scenarios, risk-normalized comparison, red negative
values, and layout at 320px and 1024px. These checks verify the proposal, not
the pending production dashboard features.

## Top 10 Prioritized Bug Backlog (Frontend & Backend), 2026-10-30

1. **[Completed] [P0] `bug-01-panic-liquidation-import-and-flatten` — Emergency Panic Liquidation crashes with `ImportError` and `TradierClient.emergency_flatten_positions()` is a no-op stub**
   - `reflex_dashboard/reflex_dashboard/state.py` (`execute_panic_liquidation`, lines 475–485) imports non-existent `BrokerClient` from `dashboard.broker_client` and calls `client.close_all_positions()`.
   - `dashboard/broker_client.py` (`TradierClient.emergency_flatten_positions`, lines 256–260) only cancels open orders and returns `True` without submitting closing orders for open positions.
   - *Status: Completed.* Fixed in `reflex_dashboard/reflex_dashboard/state.py` and `dashboard/broker_client.py`.

2. **[Completed] [P0] `bug-02-dynamic-leg-count-fee-accounting` — Vertical spread (2-leg) trades are charged 4-leg Iron Condor fees in `SPXIronCondor` and `trade_logger.py`**
   - `strategies/iron_condor.py` (lines 800, 871, 1824, 1900, 1929) and `trade_logger.py` (lines 467, 566, 645, 670, 674) hardcode `4 * qty * fee_rate` (or `8 * qty * fee_rate` roundtrip) even on 2-leg `bull_put` / `bear_call` vertical spreads, distorting live `net_pnl` exit triggers and logged P&L.
   - *Status: Completed.* Fee calculation dynamically scales with actual leg count across live management and logging.

3. **[Completed] [P0] `bug-03-partial-fill-terminal-order-reconciliation` — Partial fills followed by broker order cancellation/rejection/expiration leave baskets permanently stuck in `partial` state**
   - `strategies/iron_condor.py` (`_reconcile_inflight_orders`, lines 1598–1635) only clears terminal orders when `refreshed.state == "submitted"` and all leg fills are `0`, permanently blocking `_manage_position()`, `apply_policy_action()`, and `_reconcile_broker_state()` if a partially filled order is canceled or expires.
   - *Status: Completed.* `resolve_partial_package` reconciles and unwinds or journals terminal partial packages.

4. **[Completed] [P0] `bug-04-neural-strategy-switch-entry-times` — `_apply_neural_strategy_switch` fails to parse multi-slot `entry_time` strings and leaves `_entry_times` stale**
   - `strategies/iron_condor.py` (lines 1085–1090) runs `map(int, str(target_spec["entry_time"]).split(":"))`, which raises `ValueError` on comma-separated schedules (`"09:45, 09:50, ..."`) and never updates `self._entry_times`.
   - *Status: Completed.* Fixed with `set_run_times()` handling multi-slot schedules during switches.

5. **[Completed] [P1] `bug-05-config-view-env-key-alignment` — Config view reads/writes mismatched `.env` keys (`BROKER`, `TRADIER_ACCOUNT_ID`, `GLOBAL_RISK_CAP`)**
   - `reflex_dashboard/reflex_dashboard/state.py` (lines 493–512) and `reflex_dashboard/reflex_dashboard/data/adapters.py` (lines 1082–1091) write keys that `main.py` ignores (`TRADING_BROKER`, `TRADIER_ACCOUNT_NUMBER`, `IC_MAX_POSITION_RISK`, `IC_MAX_PORTFOLIO_RISK`), and omit `TRADIER_PAPER`.
   - *Status: Completed.* Config view reads and writes canonical runtime `.env` keys and exposes paper mode toggle.

6. **[Completed] [P1] `bug-06-reflex-trade-event-aggregation-and-win-rate` — `get_reconciled_trades()` overwrites trade events via `dict.update()` and dilutes win rate with open positions**
   - `reflex_dashboard/reflex_dashboard/data/adapters.py` (lines 471–504) overwrites entry timestamps and fees on `EXIT` and ignores `ADDITION` and `ADJUSTMENT` cash flows; lines 666 and 761 divide closed wins by `closed + open` trades.
   - *Status: Completed.* Aggregates multi-event trade lifecycle and separates open from closed trades for win rate calculation.

7. **[Completed] [P1] `bug-07-ledger-strike-keys-and-multi-package-attribution` — Ledger entry strike keys mismatch dashboard formatter (`put_1` vs `long_put`) and multi-package cash/risk attribution drops credit**
   - `strategies/iron_condor.py` (lines 1885–1888) writes `f"{right}_{index}"` keys so `format_strikes()` renders `" / | / "`; `strategies/position_ledger.py` (lines 607, 740–744) zeroes `opening_credit` when holding multiple expirations and closes all open packages at once.
   - *Status: Completed.* Standardized canonical strike keys and isolated multi-expiry risk scoping.

8. **[Completed] [P1] `bug-08-trajectory-logger-scaling-and-session-tick-count` — `TrajectoryLogger` omits contract quantity multiplier, hardcodes `$5,000` risk cap, and neural UI counts non-session JSONL lines**
   - `trade_logger.py` (lines 274, 298) caps `max_position_risk` at `5000.0` and forgets `* book["quantity"]` in live P&L state; `reflex_dashboard/reflex_dashboard/data/adapters.py` (lines 1010–1018) counts all historical lines in `trajectories.jsonl` instead of current-session `SNAPSHOT` rows.
   - *Status: Completed.* Quantity-scaled P&L state, configured risk limit respected, and tick counter filtered by current session SNAPSHOTs.

9. **[Partially Completed / Pending Legacy Streamlit Parity] [P1] `bug-09-ui-telemetry-fallbacks-and-sign-colors` — Cost-basis quote fallback fabricates `$0.00` P&L, gauges/header use hardcoded static labels/colors, and negative P&L/drawdown renders in green**
   - `reflex_dashboard/reflex_dashboard/data/adapters.py` (line 304) and `dashboard/broker_client.py` (line 194) substitute cost basis when quotes are missing; `positions_view.py:62`, `ab_lab_view.py:171,243`, `benchmark_view.py:106`, `risk_gauges.py:86,99,154`, and `header.py:63,83` use hardcoded colors and static strings.
   - *Status: Partially Completed.* Reflex dashboard defects are completed under `dashboard-telemetry-validity`, `dashboard-signed-value-formatting`, and `dashboard-configured-risk-telemetry`. Legacy Streamlit view (`dashboard/app.py` / `dashboard/broker_client.py`) parity remains pending.

10. **[Completed] [P2] `bug-10-legacy-react-form-and-mock-backend-accounting` — Legacy React form inputs lack `name` attributes and FastAPI mock backend has inverted cash/credit accounting**
    - `dashboard/src/App.jsx` (lines 41–54, 136–191) uses `FormData` on inputs without `name` attributes and lacks `onSubmit`/`onClick` handlers; `backend/app.py` (lines 23, 53, 79) miscalculates cash, double-multiplies credit by quantity, and subtracts credit from equity.
    - *Status: Completed.* Form state handlers and input bindings added to `App.jsx`, and accounting math corrected in `backend/app.py`.

## Approved bounded implementation, 2026-10-30

Preserve the dirty-tree foundation and audit backlog. Change only `TASK_PLAN.md`,
`main.py`, `.env.example`, private `.env`, `strategies/iron_condor.py`,
`tests/test_regressions.py`, and `tests/test_strike_selection_and_pnl.py`.

1. Capture both worktrees and private backups before editing.
2. Add broker, credit/ownership, roll/DTE, and production-math regressions.
3. Remove submitted-limit credit recovery; require finite positive confirmed cash.
   Move roll/DTE exits before valuation; preserve reconciliation, close retries,
   contract ownership, ledger hydration, and the compatible iteration counter.
4. Redact the Tradier connection log and clarify Sandbox configuration comments.
   Merge only the four canonical broker settings into private `.env`, preserving
   existing credentials and unrelated settings; never install placeholder values.
5. Compile all changed Python files. Run targeted regressions, math and import
   tests, then the complete suite with `RUN_TRADIER_SANDBOX_WRITE_TEST=0`.
   Run `git diff --check` and `git check-ignore .env`. Verify nonblank canonical
   credentials and paper mode without printing their values.

Acceptance: zero new failures, all added regressions pass, no unexpected skips.
Gross valuation coverage does not resolve realized-fee or expiration accounting.
Offline verification cannot authenticate Sandbox credentials.

Rollback: private byte/permission-preserving backups and initial Git status/diffs
are in `/tmp/implementation-backup-k7459nnc` (directory mode 0700). Restore only
planned existing files from their matching backup paths, preserving prior dirty
work. Remove only planned files recorded as initially absent in `missing.json`;
never restore from HEAD or reset either repository.

Initial verification attempt: system Python 3.11.2 has no pytest; existing `.venv`
points to a missing Python 3.12 interpreter. Establish a Python 3.12 development
environment outside the repositories before recording runnable-suite baselines.

### Implementation verification results

- Development environment: Python 3.12.16 at
  `/tmp/lumibot-stage-venv/bin/python3`; installed the existing `.[dev]` dependencies
  without changing dependency manifests.
- Runnable pre-change baseline: 140 passed, 2 failed, 2 skipped (29 subtests
  passed). Both failures were imports of `tests.test_regressions` in the planned
  closing-price test file. They now import the local test module directly;
  closing-price assertions and ledger coverage are retained.
- Added regressions reproduced the account-number logging, limit-credit fallback,
  and unreachable roll-exit defects before the source fix.
- Targeted gate: 94 passed (38 subtests passed) across `test_regressions.py`,
  `test_strike_selection_and_pnl.py`, `test_iron_condor_math.py`, and
  `test_import_hygiene.py`.
- Final full suite: 154 passed, 2 skipped (51 subtests passed). The two skips match
  baseline: optional Reflex dependency is absent, and the Sandbox order-write
  test remains disabled with `RUN_TRADIER_SANDBOX_WRITE_TEST=0`.
- `python3 -m py_compile` passed for all four planned Python files.
- `git check-ignore .env` passed. Private `.env` already contained the four
  requested canonical settings, nonblank credentials, and paper mode; its bytes
  and credentials were preserved. No broker authentication or writes were tried.
- `git diff --check` still reports pre-existing trailing blank lines in
  `.env.example`, `.scrum/DECISIONS.md`, `.scrum/TASKS.yaml`,
  `dashboard/USER_FEATURES.md`, and `strategies/iron_condor_math.py`. These remain
  visible whitespace-gate blockers; no unrelated cleanup was performed.
- Comparison against backed-up staged/unstaged diffs confirms implementation
  changes are confined to planned files and staged work is unchanged. Baseline,
  before-fix regression, and final full-suite output are stored in the private
  backup directory above. No new test failures or new skips remain.

## Approved Tradier routing implementation, 2026-10-31

Baseline: Lumibot `1d0c1aaf264144850ca94a30be5869ae550cd5a8` and Jev-bot
`ec5d3220a9d1bd3083b5644e86b9e9bcf66a9b00`, both clean before execution.
Change only this record, `strategies/iron_condor.py`, `tests/test_regressions.py`,
and new `tests/test_tradier_order_routing.py`. Jev-bot is verification only.

1. Route atomic packages using the normalized registered host name, preserving
   separate package IDs and ledger owners. Reject an empty host name before submission.
2. Recover packages through confirmed parent/child IDs or unique legacy package
   tags. Never use a shared host tag as package identity. Retain unresolved orders
   and owned contracts with a recovery lock, including uncertain closing submissions.
3. Add offline regressions and contract tests against the installed broker's
   guards, serialization, polling, and host callback routing. Do not edit packages.
4. Compile the planned Python paths, run targeted and full suites, verify scope
   and whitespace, and retain the existing broker selection and credit protections.

Pre-change suites: Lumibot 154 passed, 2 skipped, 51 subtests passed; Jev-bot
38 passed, 17 subtests passed, and unittest discovery passed all 38 tests.
Jev-bot's structure test executes root, module, and installed console entrypoints
and checks each completes exactly one iteration and stops.

No authentication or broker writes are authorized by these offline checks.
Panic liquidation, expiration accounting, dynamic fees, partial-fill terminal
transitions, and duplicated journaling remain outside this bounded change.

### Routing implementation verification

- Targeted gate: 109 passed, 47 subtests passed. Command:
  `RUN_TRADIER_SANDBOX_WRITE_TEST=0 .venv/bin/python3 -m pytest -q tests/test_regressions.py tests/test_tradier_order_routing.py tests/test_strike_selection_and_pnl.py tests/test_iron_condor_math.py tests/test_import_hygiene.py`.
- Full gate: 169 passed, 2 skipped, 60 subtests passed. Command:
  `RUN_TRADIER_SANDBOX_WRITE_TEST=0 .venv/bin/python3 -m pytest -q -ra tests`.
  The skips match baseline: Reflex is absent and the Sandbox write test is disabled.
  All five installed-broker contract tests executed and passed without skips.
- Compilation passed under the existing Python 3.12.16 environment:
  `.venv/bin/python3 -m py_compile main.py strategies/iron_condor.py tests/test_regressions.py tests/test_tradier_order_routing.py tests/test_strike_selection_and_pnl.py tests/test_iron_condor_math.py`.
- Jev-bot: `/tmp/jev-bot-stage-venv/bin/python3 -m pytest -q tests` passed
  38 tests and 17 subtests. The same interpreter passed
  `-m unittest discover -s tests -v` and
  `-m py_compile main.py jev_bot/*.py tests/*.py`.
  Its entrypoint structure test executed all three commands and verified one
  iteration and shutdown for each. Its worktree remains clean.
- `git diff --check` and `git check-ignore .env` passed. Changes are confined to
  the four planned Lumibot paths. Credentials, configuration, dependency manifests,
  ledger schema, and installed package sources are unchanged.
- SDK I/O and position reads were mocked in the installed-broker tests.
  Offline verification does not establish credential validity or live readiness.

Package these four files in one isolated commit. While that commit remains HEAD,
rollback with `git -C /workspace/lumibot revert --no-edit HEAD`.

### Implementation progress & verification summary

- **`bug-01-panic-liquidation-import-and-flatten`**: Completed and verified. Resolved `BrokerClient` import error in `DashboardState.execute_panic_liquidation()`. Implemented real execution logic in `TradierClient.emergency_flatten_positions()` canceling working orders, closing short option legs before protective longs with bounded polling until flat, exposing per-symbol failure errors, and enforcing a durable SQLite panic halt (`PositionLedger.set_panic_halt(True)` / `is_panic_halted()`) across strategy iterations and policy actions.
- **`bug-02-dynamic-leg-count-fee-accounting`**: Completed and verified. Implemented resilient fee environment parsing via `option_fee_per_contract()`. Scaled entry, addition, exit, and roundtrip fee accounting dynamically by leg count (`len(legs)` / `len(strikes)`) across `SPXIronCondor`, `trade_logger.py`, and `dashboard/trade_logger.py`, preventing vertical spreads from being overcharged 2x. Scaled entry max risk by confirmed filled quantity.
- **`bug-03-partial-fill-terminal-order-reconciliation`**: Completed and verified. Implemented `PositionLedger.resolve_partial_package()` transitioning partially filled packages with terminal broker orders (`canceled`, `rejected`, `expired`, `failed`) to active `"open"` (for entry/add) or `"canceled"` (for close/trim), unblocking position management and closing submissions. Fixed cumulative fill pricing in `PositionLedger.record_fill` and deduplicated trade journal events.
- **`bug-04-neural-strategy-switch-entry-times`**: Completed and verified. Integrated `self.set_run_times()` into `SPXIronCondor._apply_neural_strategy_switch()` to parse multi-slot comma-separated entry schedules, safely synchronizing `self._entry_times` and `self._entry_time`.
- **Verification**: Complete test suite passed (198 passed, 1 skipped, 76 subtests passed) in `tests/test_reflex_dashboard.py`, `tests/test_iron_condor_execution_safety.py`, `tests/test_trade_logger_win_rate.py`, `tests/test_neural_strategy_switch.py`, and the rest of the test suite. All modified Python files passed `python3 -m py_compile`, and `git diff --check` passed cleanly.

## Focus the dynamic comparison, 2026-11-01

The sandbox currently selects 29 strategies, including the 20 plain IC variants.
Make StrategyBro the first entry strategy and compare it with TrendFlow, Skew
Dynamic, VolCrush, ORB, Neural Pure, and one plain IC control.

1. Make the focused roster the default and retain the full grid as an opt-in.
   Apply count limits after prioritizing StrategyBro and the dynamic peers.
2. Recover excluded ledger owners for position management only. Prevent these
   owners from opening or adding exposure while retaining their normal exits.
3. Add a rerunnable roster inspector and offline selection/recovery coverage.
4. Verify the actual sandbox ledger and runner before activating the new roster.
5. Keep unrelated dashboard work and historical trade/position ownership intact.

Implemented and verified:

- Default entries are IC_V25, V26, V27, V28, V29, V22, and V19 in that order.
  Full-grid and explicit static-test opt-ins remain covered.
- Real ledger inspection identified 22 excluded owners with active packages.
  Startup retained them as management-only strategies and initialized StrategyBro
  as the primary host. Their entry, addition, and strategy-switch paths are blocked.
- Research snapshots and policy routing exclude management-only owners.
- The dashboard displays seven unique comparison rows with StrategyBro first,
  uses the configured count, and calculates win rate from selected strategies.
  Historical journals and the Positions workspace remain available.
- 174 targeted offline tests passed, covering selection, shared data, management
  recovery, execution regressions, trajectories, policy inference, and dashboard
  comparison data. Python compilation and `git diff --check` passed.
- Browser inspection confirmed the seven-row dashboard, and sandbox startup logs
  confirmed seven entry-enabled strategies and 22 management-only owners.
  The market was closed during activation; no trading-performance result is claimed.

### Implementation progress — 2026-11-02

- Applied the shared ledger path resolver and common execution-lock path; configured both Compose services to use the shared mounted ledger.
- Packaged the existing emergency recovery client dependencies in the bot image.
- Retained unbalanced terminal opening fills as partial, using per-leg fill ratios to identify balanced packages.
- Verification: `python3 -m py_compile` passed for the changed Python files; ledger, emergency liquidation and dashboard tests passed (26); execution safety, routing, regression and incubator tests passed (103 plus 50 subtests). Jev-bot baseline tests passed (38 plus 17 subtests) using Lumibot's virtual environment.
- Remaining: complete the broker-confirmed fill recovery, entry-cancellation barrier, expiry pricing, neural opt-in, container smoke suite, and the remaining planned integration tests and documentation. The supplied plan was truncated during the expiry-pricing instructions.


## Independent concurrent positions, 2026-11-02

Implement the approved multi-tranche review and push only after validation.
Preserve existing workspace changes and broker host routing.

Design: a persistent position owns inventory, entry basis, trade identity,
management settings, and exit intent. Every submission has its own package ID
and an explicit position ID. Strategy state contains a map of position runtimes;
management and fill processing select a position explicitly. Aggregate values
are telemetry, never inputs to an individual position's exits.

Alternatives: a dictionary of baskets alone cannot link trims and replacements
to inventory. Separate broker-registered strategies per tranche would change
routing and strategy risk ownership. Keep one strategy and explicit position
ownership in the ledger instead.

1. Add an additive, repeatable ledger migration with conservative legacy links.
   Ambiguous legacy adjustments block new exposure instead of guessing ownership.
2. Calculate remaining credit and risk per position, reserve unfilled entry
   quantities, and enforce capacity, risk, spacing, and contract reservations
   together under the shared execution gate.
3. Scope strategy management, close retries, journals, policy adjustments, and
   restart recovery to one position. Keep broker host tags unchanged.
4. Add deterministic regression tests for distinct/shared strikes, closing A
   while B survives, restart, terminal partial fills, duplicate observations,
   missing broker orders, concurrent submissions, and legacy migration.
5. Run focused and full offline suites, inspect the actual commit contents,
   evaluate failures, and push a dedicated branch when the remote is supplied.

Acceptance: exact position inventory, basis, identity, settings, and exit intent
survive restart; one position's exit cannot alter another; pending quantities
continue consuming capacity and risk. Concurrency is configurable, with a
backward-compatible default of one and a 60-minute entry spacing default.
No private credentials, live configuration, or broker orders are changed.
