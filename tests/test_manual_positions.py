"""Manual broker inventory must release entry locks without becoming a strategy."""
from copy import deepcopy
from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from strategies.position_ledger import PositionLedger
from strategies.runtime_records import RecordScope
from test_regressions import iron_condor, incubator

SCOPE = RecordScope(environment="test", broker="mock", account_id="test_account")


def order():
    legs = []
    for index, (right, strike, side, price) in enumerate([
        ("P", 718, "buy_to_open", 13.33), ("P", 725, "sell_to_open", 13.94),
        ("C", 785, "sell_to_open", 13.80), ("C", 815, "buy_to_open", 4.82),
    ], 1):
        legs.append(dict(id=1000+index, status="filled", option_symbol=f"XSP270219{right}{strike*1000:08d}",
                         side=side, quantity=1, exec_quantity=1, avg_fill_price=price))
    return dict(id=1000, status="filled", **{"class": "multileg"}, quantity=4, exec_quantity=4,
                create_date="2026-11-09T20:02:21.756Z", type="market", duration="day", leg=legs)


def positions(value=None):
    value = value or order()
    return [dict(symbol=leg["option_symbol"], quantity=1 if leg["side"].startswith("buy") else -1)
            for leg in value["leg"]]


def imported(ledger, *, value=None, actual=None, apply=True, **kwargs):
    from strategies.manual_positions import import_manual_order
    return import_manual_order(ledger, order=value or order(), broker_positions=actual if actual is not None else positions(),
                               scope=SCOPE, confirmed_manual=True, apply=apply, **kwargs)


@pytest.fixture
def ledger(tmp_path):
    ledger = PositionLedger(tmp_path / "ledger.sqlite3", scope=SCOPE)
    yield ledger
    ledger.close()


def table_contents(ledger):
    return {row[0]: [tuple(r) for r in ledger._connection.execute(f'SELECT * FROM "{row[0]}" ORDER BY rowid')]
            for row in ledger._connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}


def test_import_releases_real_reconciliation_lock_and_keeps_manual_out_of_roster(ledger):
    from strategies.manual_positions import MANUAL_OWNER
    actual = positions()
    broker_positions = []
    for row in actual:
        symbol = row["symbol"]
        asset = SimpleNamespace(symbol="XSP", expiration="2027-02-19", right="put" if symbol[9] == "P" else "call",
                                strike=int(symbol[-8:])/1000, asset_type="option")
        broker_positions.append(SimpleNamespace(asset=asset, quantity=row["quantity"]))
    strategy = iron_condor.SPXIronCondor()
    strategy.parameters = {**iron_condor.SPXIronCondor.parameters, "position_ledger": ledger}
    strategy.broker = SimpleNamespace(name="Tradier")
    strategy.initialize()
    strategy.get_positions = Mock(return_value=broker_positions)
    strategy.get_orders = Mock(return_value=[])
    strategy.get_datetime = Mock(return_value=datetime(2026, 11, 10, 17, tzinfo=timezone.utc))
    assert strategy._reconcile_broker_state()
    assert strategy._recovery_lock
    imported(ledger)
    assert strategy._reconcile_broker_state()
    assert not strategy._recovery_lock
    assert not strategy._position_states
    assert incubator._get_management_specs([], ledger) == []
    assert ledger.portfolio_defined_risk(MANUAL_OWNER) == pytest.approx(2041)
    assert ledger.portfolio_defined_risk() == pytest.approx(2041)
    assert ledger.get_position("manual:1000").strategy_id == MANUAL_OWNER
    assert not ledger._connection.execute("SELECT * FROM journal_events").fetchall()


def test_preview_and_duplicate_application_do_not_change_records(ledger):
    before = table_contents(ledger)
    assert imported(ledger, apply=False)["result"] == "eligible"
    assert table_contents(ledger) == before
    assert imported(ledger)["result"] == "imported"
    after = table_contents(ledger)
    assert imported(ledger)["result"] == "already_imported"
    assert table_contents(ledger) == after
    retained = ledger.get_broker_evidence("manual-order:1000")
    assert retained["payload"]["raw_response"] == order()
    assert retained["payload"]["confirmed_manual"] is True


@pytest.mark.parametrize("change", ["unfilled", "partial", "tagged", "closing", "fraction", "nonfinite", "duplicate", "wrong_symbol", "naked"])
def test_invalid_provider_observations_are_rejected_atomically(ledger, change):
    value = order()
    if change == "unfilled": value["status"] = "open"
    if change == "partial": value["leg"][0]["exec_quantity"] = 0
    if change == "tagged": value["tag"] = "IC0123456789abcdef01234567"
    if change == "closing": value["leg"][0]["side"] = "buy_to_close"
    if change == "fraction": value["leg"][0]["quantity"] = 1.5
    if change == "nonfinite": value["leg"][0]["avg_fill_price"] = float("nan")
    if change == "duplicate": value["leg"][1]["id"] = value["leg"][0]["id"]
    if change == "wrong_symbol": value["leg"][0]["option_symbol"] = "invalid"
    if change == "naked": value["leg"][0]["side"] = "sell_to_open"
    before = table_contents(ledger)
    with pytest.raises(ValueError): imported(ledger, value=value)
    assert table_contents(ledger) == before


def test_quantity_mismatch_and_wrong_scope_keep_inventory_unchanged(ledger):
    from strategies.manual_positions import import_manual_order
    before = table_contents(ledger)
    with pytest.raises(ValueError, match="inventory"):
        imported(ledger, actual=[])
    with pytest.raises(ValueError, match="scope"):
        import_manual_order(ledger, order=order(), broker_positions=positions(),
                            scope=RecordScope(environment="paper", account_id="other"), confirmed_manual=True, apply=True)
    with pytest.raises(ValueError, match="confirm"):
        import_manual_order(ledger, order=order(), broker_positions=positions(), scope=SCOPE, confirmed_manual=False, apply=True)
    assert table_contents(ledger) == before


def test_transaction_failure_cannot_leave_partial_manual_ownership(ledger):
    ledger._connection.execute("CREATE TRIGGER fail_manual_evidence BEFORE INSERT ON broker_evidence BEGIN SELECT RAISE(ABORT,'injected failure'); END")
    before = table_contents(ledger)
    with pytest.raises(sqlite3.IntegrityError, match="injected failure"):
        imported(ledger)
    assert table_contents(ledger) == before


def test_existing_strategy_parent_cannot_be_reassigned(ledger):
    from strategies.position_ledger import LedgerLeg
    legs = [LedgerLeg(str(i), json.dumps(dict(symbol="XSP", expiration="2027-02-19", right="put", strike=k)), q)
            for i, (k, q) in enumerate([(690, 1), (695, -1)])]
    ledger.record_submission("strategy-package", "strategy", "open", legs)
    ledger._connection.execute("UPDATE packages SET broker_parent_id='1000'")
    ledger._connection.commit()
    before = table_contents(ledger)
    with pytest.raises(ValueError, match="already"):
        imported(ledger)
    assert table_contents(ledger) == before


def test_duplicate_with_changed_execution_is_rejected(ledger):
    imported(ledger)
    before = table_contents(ledger)
    value = deepcopy(order())
    value["leg"][0]["avg_fill_price"] += 1
    with pytest.raises(ValueError, match="conflict"):
        imported(ledger, value=value)
    assert table_contents(ledger) == before


def test_manual_fill_callback_cannot_create_host_strategy_state_or_journal(ledger):
    imported(ledger)
    strategy = iron_condor.SPXIronCondor()
    strategy.parameters = {**iron_condor.SPXIronCondor.parameters, "position_ledger": ledger}
    strategy.broker = SimpleNamespace(name="Tradier")
    strategy.initialize()
    strategy._record_filled_entry = Mock()
    strategy._retain_order_evidence = Mock()
    strategy._hydrate_from_ledger = Mock()
    row = order()["leg"][0]
    asset = SimpleNamespace(symbol="XSP", expiration="2027-02-19", right="put", strike=718.0, asset_type="option")
    callback = SimpleNamespace(identifier=str(row["id"]), asset=asset, side=row["side"],
                               parent_identifier="1000", _raw=row, quantity=1)
    before = table_contents(ledger)
    strategy.on_filled_order(None, callback, row["avg_fill_price"], 1, 100)
    strategy._record_filled_entry.assert_not_called()
    strategy._retain_order_evidence.assert_not_called()
    assert table_contents(ledger) == before
    assert not strategy._position_states


def test_unrelated_unknown_inventory_still_locks_entries_after_manual_import(ledger):
    imported(ledger)
    actual = [SimpleNamespace(asset=SimpleNamespace(asset_type="option", symbol="XSP", expiration="2027-02-19",
                                                   strike=999.0, right="call"), quantity=-1)]
    strategy = iron_condor.SPXIronCondor()
    strategy.parameters = {**iron_condor.SPXIronCondor.parameters, "position_ledger": ledger}
    strategy.broker = SimpleNamespace(name="Tradier")
    strategy.initialize()
    strategy.get_positions = Mock(return_value=actual)
    strategy.get_orders = Mock(return_value=[])
    strategy.get_datetime = Mock(return_value=datetime(2026, 11, 10, 17, tzinfo=timezone.utc))
    assert strategy._reconcile_broker_state()
    assert strategy._recovery_lock


def test_manual_fills_are_counted_in_real_global_allocation_check(ledger):
    imported(ledger)
    strategy = iron_condor.SPXIronCondor()
    strategy.parameters = {**iron_condor.SPXIronCondor.parameters, "position_ledger": ledger,
                           "max_portfolio_risk": 2000, "wing_width": 5}
    strategy.broker = SimpleNamespace(name="Tradier")
    strategy.initialize()
    strategy.get_positions = Mock(return_value=[])
    strategy.portfolio_value = 100000
    assert not strategy._check_allocation()


def test_manual_opening_appears_as_manual_in_position_view(ledger):
    from dashboard.positions import read_packages, build_positions
    imported(ledger)
    rows, issues = build_positions(read_packages(Path(ledger.path)), positions(), {},
                                  as_of=datetime(2026, 11, 10).date())
    assert not issues
    assert len(rows) == 1
    assert rows[0]["strategy"] == "MANUAL"
    assert rows[0]["opening_risk"] == pytest.approx(2041)
    assert rows[0]["cash_flow"] == pytest.approx(959)


def four_put_order():
    value = order()
    value.update(id=2000, quantity=8, exec_quantity=8)
    for index, (leg, strike, price) in enumerate(zip(value["leg"], (724, 729, 775, 800), (17.6, 18.55, 34.3, 48.94)), 1):
        leg.update(id=2000 + index, option_symbol=f"XSP270319P{strike * 1000:08d}",
                   quantity=2, exec_quantity=2, avg_fill_price=price)
    return value


def test_confirmed_four_put_order_imports_once_with_exact_risk_and_manual_ownership(ledger):
    from dashboard.positions import read_packages, build_positions
    value = four_put_order()
    actual = [{"symbol": leg["option_symbol"], "quantity": 2 if leg["side"].startswith("buy") else -2}
              for leg in value["leg"]]
    assert imported(ledger, value=value, actual=actual)["result"] == "imported"
    before = table_contents(ledger)
    assert imported(ledger, value=value, actual=actual)["result"] == "already_imported"
    assert table_contents(ledger) == before
    assert ledger.portfolio_defined_risk() == pytest.approx(2738)
    rows, issues = build_positions(read_packages(Path(ledger.path)), actual, {}, as_of=datetime(2026, 11, 11).date())
    assert not issues
    assert rows[0]["strategy"] == "MANUAL"
    assert rows[0]["opening_risk"] == pytest.approx(2738)
    assert rows[0]["cash_flow"] == pytest.approx(-2738)
    assert rows[0]["structure_label"] == "Put condor"
    assert incubator._get_management_specs([], ledger) == []


def test_four_put_import_still_requires_confirmation_and_covered_quantities(ledger):
    from strategies.manual_positions import parse_manual_order, import_manual_order
    value = four_put_order()
    with pytest.raises(ValueError, match="confirm"):
        import_manual_order(ledger, order=value, broker_positions=[], scope=SCOPE, confirmed_manual=False, apply=True)
    value["leg"][0]["quantity"] = value["leg"][0]["exec_quantity"] = 1
    with pytest.raises(ValueError, match="defined risk"):
        parse_manual_order(value)
