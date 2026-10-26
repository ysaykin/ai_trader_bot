# Implementation task plan

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
