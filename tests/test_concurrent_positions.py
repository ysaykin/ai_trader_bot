"""Independent position accounting and broker-boundary concurrency regressions."""
from __future__ import annotations

import json
import sqlite3
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from strategies.position_ledger import LedgerLeg, PositionLedger, asset_key
from test_regressions import iron_condor


NOW = datetime(2026, 11, 2, 15, tzinfo=timezone.utc)
PRICES_A = (.1, .7, .7, .1)
PRICES_B = (.1, .4, .4, .1)
CLOSE_PRICES = (.05, .25, .25, .05)


def option_legs(strikes=(90, 95, 110, 115), quantity=1):
    return [(SimpleNamespace(asset_type="option", symbol="XSP", expiration="2026-11-20",
                             strike=strike, right=right), sign * quantity)
            for strike, right, sign in zip(strikes, ("put", "put", "call", "call"), (1, -1, -1, 1))]


def ledger_legs(package, legs):
    return tuple(LedgerLeg(f"{package}:{index}", asset_key(asset), quantity)
                 for index, (asset, quantity) in enumerate(legs))


def open_position(ledger, package, *, strikes=(90, 95, 110, 115), quantity=1,
                  prices=PRICES_A, owner="strategy", **kwargs):
    legs = ledger_legs(package, option_legs(strikes, quantity))
    ledger.record_submission(package, owner, "open", legs, created_at=NOW, **kwargs)
    for leg, price in zip(legs, prices):
        ledger.record_fill(leg.order_id, leg.requested_signed_quantity, price)
    return ledger.get_basket(package).position_id


def close_position(ledger, pid, package="close", quantity=1):
    inventory = ledger.position_quantities(pid)
    legs = [LedgerLeg(f"{package}:{i}", key, -quantity if q > 0 else quantity)
            for i, (key, q) in enumerate(inventory.items())]
    ledger.record_submission(package, "strategy", "close", legs, position_id=pid, created_at=NOW)
    for leg, price in zip(legs, CLOSE_PRICES):
        ledger.record_fill(leg.order_id, leg.requested_signed_quantity,
                           .25 if leg.requested_signed_quantity > 0 else .05)


@pytest.fixture
def ledger():
    instance = PositionLedger()
    yield instance
    instance.close()


def test_distinct_same_expiry_positions_keep_credit_and_risk(ledger):
    open_position(ledger, "a")
    open_position(ledger, "b", strikes=(80, 85, 120, 125), prices=PRICES_B)
    assert ledger.active_opening_cash("strategy") == 180
    assert ledger.portfolio_defined_risk("strategy") == 820
    assert len(ledger.open_positions("strategy")) == 2


@pytest.mark.parametrize("shared_strikes", [False, True])
def test_close_a_preserves_b_exact_basis_and_releases_only_a_slot(ledger, shared_strikes):
    a = open_position(ledger, "a")
    b = open_position(ledger, "b", prices=PRICES_B,
                      strikes=(90, 95, 110, 115) if shared_strikes else (80, 85, 120, 125))
    expected = ledger.position_quantities(b)
    close_position(ledger, a)
    assert ledger.get_basket("a").state == "closed"
    assert ledger.get_basket("b").state == "open"
    assert ledger.position_quantities(a) == {}
    assert ledger.position_quantities(b) == expected
    assert ledger.position_opening_cash(b) == 60
    assert ledger.active_opening_cash("strategy") == 60
    assert ledger.portfolio_defined_risk("strategy") == 440
    assert [p.position_id for p in ledger.open_positions("strategy")] == [b]
    assert ledger.closing_opening_cash("close") == 120


def test_shared_contract_adjustment_requires_explicit_owner(ledger):
    open_position(ledger, "a")
    open_position(ledger, "b", prices=PRICES_B)
    with pytest.raises(ValueError, match="unambiguous position_id"):
        ledger.record_submission("close", "strategy", "close",
                                 ledger_legs("close", [(a, -q) for a, q in option_legs()]))


@pytest.mark.parametrize("terminal, expected", [(False, 880), (True, 380)])
def test_partial_entry_reserves_unfilled_remainder_until_confirmed_terminal(ledger, terminal, expected):
    legs = ledger_legs("partial", option_legs(quantity=2))
    ledger.record_submission("partial", "strategy", "open", legs)
    assert ledger.portfolio_defined_risk("strategy") == 1000
    for leg, price in zip(legs, PRICES_A):
        ledger.record_fill(leg.order_id, 1 if leg.requested_signed_quantity > 0 else -1, price)
    if terminal:
        ledger.resolve_partial_package("partial", "canceled")
    assert ledger.portfolio_defined_risk("strategy") == expected
    assert ledger.is_inflight(ledger.get_basket("partial")) is not terminal


def test_terminal_unbalanced_fill_keeps_inventory_and_blocks_new_risk(ledger):
    legs = ledger_legs("partial", option_legs(quantity=2))
    ledger.record_submission("partial", "strategy", "open", legs)
    ledger.record_fill(legs[0].order_id, 1, .1)
    ledger.resolve_partial_package("partial", "canceled")
    assert ledger.position_quantities("partial") == {legs[0].asset_key: 1}
    assert ledger.portfolio_defined_risk("strategy") is None
    assert not ledger.is_inflight(ledger.get_basket("partial"))


def test_pending_close_reserves_inventory_without_freeing_position_slot(ledger):
    pid = open_position(ledger, "a")
    close = ledger_legs("c", [(a, -q) for a, q in option_legs()])
    ledger.record_submission("c", "strategy", "close", close, position_id=pid)
    with ledger.execution_gate(), pytest.raises(RuntimeError, match="unreserved"):
        ledger.assert_can_submit("strategy", "close", close, position_id=pid, strategy_cap=5000)
    assert len(ledger.open_positions("strategy")) == 1
    assert ledger.portfolio_defined_risk("strategy") == 380


def test_position_settings_identity_and_exit_intent_survive_restart(tmp_path):
    path = tmp_path / "ledger.sqlite"
    ledger = PositionLedger(path)
    a = open_position(ledger, "a", settings={"profit_target": .3, "wing_width": 5})
    b = open_position(ledger, "b", prices=PRICES_B,
                      settings={"profit_target": .6, "wing_width": 5})
    ledger.set_trade_id("a", "trade-a")
    ledger.set_trade_id("b", "trade-b")
    ledger.update_position(b, exit_reason="STOP_LOSS", mfe_pnl=15, mae_pnl=-20)
    close_position(ledger, a)
    ledger.close()
    for _ in range(2):
        ledger = PositionLedger(path)
        try:
            assert ledger.position_opening_cash(b) == 60
            assert ledger.get_position(b).settings["profit_target"] == .6
            assert ledger.get_position(b).exit_reason == "STOP_LOSS"
            assert ledger.get_position(b).mfe_pnl == 15
            assert ledger.get_position(b).mae_pnl == -20
            assert ledger.active_trade_id("strategy", b) == "trade-b"
            assert [p.position_id for p in ledger.open_positions("strategy")] == [b]
        finally:
            ledger.close()


def downgrade_to_legacy(path):
    with sqlite3.connect(path) as connection:
        connection.executescript("""
            DROP INDEX packages_position;
            ALTER TABLE packages DROP COLUMN position_id;
            ALTER TABLE packages DROP COLUMN terminal_status;
            DROP TABLE journal_events;
            DROP TABLE positions;
        """)


def test_legacy_migration_links_unique_adjustments_idempotently(tmp_path):
    path = tmp_path / "legacy.sqlite"
    ledger = PositionLedger(path)
    a = open_position(ledger, "a")
    open_position(ledger, "b", strikes=(80, 85, 120, 125), prices=PRICES_B)
    close_position(ledger, a)
    ledger.close()
    downgrade_to_legacy(path)
    for _ in range(2):
        ledger = PositionLedger(path)
        try:
            assert ledger.get_basket("close").position_id == "a"
            assert ledger.position_opening_cash("b") == 60
            assert ledger.portfolio_defined_risk("strategy") == 440
        finally:
            ledger.close()


def test_legacy_migration_does_not_guess_between_shared_contract_positions(tmp_path):
    path = tmp_path / "ambiguous.sqlite"
    ledger = PositionLedger(path)
    a = open_position(ledger, "a")
    open_position(ledger, "b", prices=PRICES_B)
    close_position(ledger, a)
    ledger.close()
    downgrade_to_legacy(path)
    ledger = PositionLedger(path)
    try:
        assert ledger.get_basket("close").position_id is None
        assert ledger.portfolio_defined_risk("strategy") is None
    finally:
        ledger.close()


@pytest.fixture
def strategy(ledger, monkeypatch):
    import trade_logger
    real_entry = trade_logger.record_trade_entry
    events = []
    def entry(**kwargs):
        events.append(("ENTRY", kwargs))
        return kwargs["trade_id"]
    monkeypatch.setattr(trade_logger, "record_trade_entry", entry)
    for name, event in [("record_trade_exit", "EXIT"), ("record_trade_addition", "ADDITION"),
                        ("record_trade_adjustment", "ADJUSTMENT")]:
        monkeypatch.setattr(trade_logger, name, lambda _event=event, **kwargs: events.append((_event, kwargs)))
    strategy = iron_condor.SPXIronCondor()
    strategy.parameters = {**iron_condor.SPXIronCondor.parameters,
                           "symbol": "XSP", "wing_width": 5, "position_ledger": ledger,
                           "max_concurrent_positions": 2, "entry_spacing_minutes": 0,
                           "max_allocation": 1, "profit_target": .3}
    strategy.broker = SimpleNamespace(name="Tradier")
    strategy.initialize()
    strategy.portfolio_value = 100_000
    strategy.get_datetime = Mock(return_value=NOW)
    strategy.get_positions = Mock(side_effect=lambda: [
        SimpleNamespace(asset=strategy._asset_registry[key], quantity=q)
        for key, q in ledger.all_owned_quantities().items()])
    strategy.orders = []
    strategy.get_orders = Mock(side_effect=lambda: strategy.orders)
    strategy.cancel_order = Mock()
    def order(asset, quantity, side):
        result = SimpleNamespace(asset=asset, quantity=quantity, side=side,
                                 identifier=uuid.uuid4().hex, status="open")
        result.is_active = lambda: result.status in {"open", "partial"}
        return result
    strategy.create_order = Mock(side_effect=order)
    def submit(children, **kwargs):
        parent = SimpleNamespace(identifier=uuid.uuid4().hex, status="open", child_orders=children)
        parent.is_active = lambda: parent.status in {"open", "partial"}
        for child in children:
            child.parent_identifier = parent.identifier
        strategy.orders.append(parent)
        return [parent]
    strategy.submit_order = Mock(side_effect=submit)
    strategy.get_quote = Mock(side_effect=lambda asset: {
        "bid": .1 if asset.strike in {80, 90, 115, 125} else .4,
        "ask": .1 if asset.strike in {80, 90, 115, 125} else .4})
    strategy.journal_events = events
    strategy.real_entry = real_entry
    return strategy


def strategy_entry(strategy, *, prices=PRICES_A, legs=None):
    [parent] = strategy._submit_package(legs or option_legs(), intent="open", order_type="credit", price=1.2)
    strategy._hydrate_from_ledger()
    for child, price in zip(parent.child_orders, prices):
        child.status = "filled"
        child.filled_quantity = child.quantity
        child.avg_fill_price = price
        strategy.on_filled_order(None, child, price, child.quantity, 100)
    parent.status = "filled"
    return parent._position_id


def test_strategy_manages_shared_contract_entries_independently(strategy, ledger):
    a = strategy_entry(strategy)
    b = strategy_entry(strategy, prices=PRICES_B)
    assert strategy._reconcile_broker_state()
    strategy._manage_position(a)
    strategy._manage_position(b)
    closes = [basket for basket in ledger.open_baskets() if basket.intent == "close"]
    assert len(closes) == 1
    assert closes[0].position_id == a
    assert strategy._runtime(a).exit_requested
    assert not strategy._runtime(b).exit_requested
    assert strategy._runtime(b).entry_credit_cash == 60
    assert len([e for e, _ in strategy.journal_events if e == "ENTRY"]) == 2
    assert ledger.get_position(a).trade_id != ledger.get_position(b).trade_id


def test_close_callbacks_and_duplicate_observations_preserve_other_trade(strategy, ledger):
    a = strategy_entry(strategy)
    b = strategy_entry(strategy, prices=PRICES_B)
    strategy._close_all_options(exit_reason="PROFIT_TARGET", position_id=a)
    parent = strategy.orders[-1]
    for child, price in zip(parent.child_orders, CLOSE_PRICES):
        price = .25 if "buy" in child.side else .05
        child.status = "filled"
        child.filled_quantity = child.quantity
        child.avg_fill_price = price
        strategy.on_filled_order(None, child, price, child.quantity, 100)
        strategy.on_filled_order(None, child, price, child.quantity, 100)
    parent.status = "filled"
    assert ledger.position_quantities(a) == {}
    assert ledger.position_opening_cash(b) == 60
    assert ledger.portfolio_defined_risk() == 440
    exits = [event for kind, event in strategy.journal_events if kind == "EXIT"]
    assert len(exits) == 1
    assert exits[0]["trade_id"] == a
    assert exits[0]["opening_credit_cash"] == 120
    assert exits[0]["exit_reason"] == "PROFIT_TARGET"
    assert strategy._runtime(b).current_trade_id == b


def test_policy_requires_owner_and_updates_only_selected_position(strategy, ledger):
    a = strategy_entry(strategy)
    b = strategy_entry(strategy, prices=PRICES_B)
    assert strategy.apply_policy_action("TRIM_POSITION") == "HOLD"
    assert strategy.apply_policy_action("ADJUST_PROFIT_TARGET", .7, position_id=b) == "ADJUST_PROFIT_TARGET"
    assert strategy._runtime(a).settings["profit_target"] == .3
    assert strategy._runtime(b).settings["profit_target"] == .7
    assert ledger.get_position(b).settings["profit_target"] == .7


def test_opposing_pending_leg_is_rejected_before_broker_submission(strategy):
    strategy._submit_package(option_legs(), intent="open", order_type="credit", price=1.2)
    with pytest.raises(RuntimeError, match="opposing leg"):
        strategy._submit_package(option_legs((85, 90, 120, 125)), intent="open", order_type="credit", price=1.2)
    assert strategy.submit_order.call_count == 1


def test_simultaneous_entries_cannot_both_take_last_slot(strategy, ledger):
    strategy.parameters["max_concurrent_positions"] = 1
    start = threading.Barrier(2)
    def submit():
        start.wait(timeout=5)
        try:
            strategy._submit_package(option_legs(), intent="open", order_type="credit", price=1.2)
            return "submitted"
        except RuntimeError as error:
            return str(error)
    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(lambda _: submit(), range(2)))
    assert outcomes.count("submitted") == 1
    assert strategy.submit_order.call_count == 1
    assert len(ledger.open_positions()) == 1


def test_spacing_survives_hydration_and_rejects_repeat_checkpoint(strategy):
    strategy.parameters["entry_spacing_minutes"] = 60
    strategy_entry(strategy)
    strategy._hydrate_from_ledger()
    strategy.get_datetime.return_value = NOW + timedelta(minutes=15)
    with pytest.raises(RuntimeError, match="spacing"):
        strategy._submit_package(option_legs(), intent="open", order_type="credit", price=1.2)
    assert strategy.submit_order.call_count == 1


def test_missing_close_order_retains_owner_without_resubmitting(strategy, ledger):
    a = strategy_entry(strategy)
    b = strategy_entry(strategy, prices=PRICES_B)
    strategy._close_all_options(exit_reason="STOP_LOSS", position_id=a)
    strategy.orders = []
    strategy._runtime(a).closing_orders = []
    strategy._close_all_options(position_id=a)
    assert strategy.submit_order.call_count == 3
    assert strategy._recovery_lock
    assert ledger.get_position(a).exit_reason == "STOP_LOSS"
    assert not strategy._runtime(b).exit_requested


def test_entry_journal_recovers_after_file_write_before_ledger_commit(strategy, ledger, monkeypatch, tmp_path):
    import trade_logger
    path = tmp_path / "trades.jsonl"
    monkeypatch.setattr(trade_logger, "record_trade_entry", lambda **kwargs: strategy.real_entry(log_path=path, **kwargs))
    mark = ledger.mark_journaled
    attempts = []
    def fail_once(package, event):
        attempts.append(event)
        if len(attempts) == 1:
            raise RuntimeError("simulated crash after JSONL append")
        mark(package, event)
    monkeypatch.setattr(ledger, "mark_journaled", fail_once)
    pid = strategy_entry(strategy)
    basket = ledger.position_baskets(pid)[0]
    assert not ledger.journaled(basket.package_id, "ENTRY")
    strategy._record_filled_entry(ledger.get_basket(basket.package_id))
    assert ledger.journaled(basket.package_id, "ENTRY")
    assert len(path.read_text().splitlines()) == 1
    assert json.loads(path.read_text())["trade_id"] == pid


def test_failed_exit_journal_can_retry_in_same_process(strategy, ledger, monkeypatch):
    import trade_logger
    pid = strategy_entry(strategy)
    strategy._close_all_options(exit_reason="STOP_LOSS", position_id=pid)
    parent = strategy.orders[-1]
    monkeypatch.setattr(trade_logger, "record_trade_exit", Mock(side_effect=OSError("disk unavailable")))
    for child in parent.child_orders:
        child.status = "filled"
        strategy.on_filled_order(None, child, .25 if "buy" in child.side else .05, child.quantity, 100)
    basket = ledger.position_baskets(pid)[-1]
    assert not ledger.journaled(basket.package_id, "REALIZATION")
    writer = Mock()
    monkeypatch.setattr(trade_logger, "record_trade_exit", writer)
    strategy._record_filled_exit(basket)
    writer.assert_called_once()
    assert ledger.journaled(basket.package_id, "REALIZATION")


def test_read_only_legacy_risk_is_unknown_until_migrated(tmp_path):
    path = tmp_path / "legacy.sqlite"
    ledger = PositionLedger(path)
    open_position(ledger, "a")
    ledger.close()
    downgrade_to_legacy(path)
    reader = PositionLedger(path, read_only=True)
    try:
        assert reader.risk_snapshot()["portfolio_risk"] is None
        assert reader.risk_snapshot()["owned_quantities"]["strategy"]
    finally:
        reader.close()


def test_corrupt_snapshot_reports_unknown_risk(tmp_path):
    path = tmp_path / "incomplete.sqlite"
    with sqlite3.connect(path) as connection:
        connection.executescript("CREATE TABLE packages(strategy_id TEXT); INSERT INTO packages VALUES('strategy');")
    reader = PositionLedger(path, read_only=True)
    try:
        assert reader.risk_snapshot()["portfolio_risk"] is None
    finally:
        reader.close()


@pytest.mark.parametrize("mixed_expiry", [False, True])
def test_invalid_spread_shape_cannot_understate_reserved_risk(ledger, mixed_expiry):
    legs = option_legs((100, 110, 105, 115))
    if mixed_expiry:
        legs = option_legs()
        legs[-1][0].expiration = "2026-12-20"
    ledger.record_submission("invalid", "strategy", "open", ledger_legs("invalid", legs))
    assert ledger.portfolio_defined_risk() is None


def test_file_backed_gate_enforces_global_cap_across_connections(tmp_path):
    path = tmp_path / "shared.sqlite"
    ledgers = [PositionLedger(path), PositionLedger(path)]
    start = threading.Barrier(2)
    def reserve(index):
        ledger = ledgers[index]
        legs = ledger_legs(str(index), option_legs())
        start.wait(timeout=5)
        with ledger.execution_gate():
            try:
                ledger.assert_can_submit(str(index), "open", legs, position_id=None,
                                         max_positions=2, strategy_cap=5000, portfolio_cap=750)
                ledger.record_submission(str(index), str(index), "open", legs)
                return "submitted"
            except RuntimeError as error:
                return str(error)
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(reserve, range(2)))
        assert results.count("submitted") == 1
        assert ledgers[0].portfolio_defined_risk() == 500
    finally:
        for ledger in ledgers:
            ledger.close()


def test_single_position_limit_is_rechecked_at_submission(strategy):
    strategy.parameters["max_position_risk"] = 1000
    with pytest.raises(RuntimeError, match="single-position"):
        strategy._submit_package(option_legs(), intent="open", order_type="credit", price=1.2)
    strategy.submit_order.assert_not_called()


def test_existing_position_is_managed_before_entering_second_tranche(strategy, ledger):
    a = strategy_entry(strategy)
    strategy._manage_position = Mock()
    strategy._is_entry_window = Mock(return_value=True)
    strategy._find_expiration = Mock(return_value="2026-11-20")
    strategy._find_short_strikes = Mock(return_value=(95, 110))
    strategy.get_last_price = Mock(return_value=100)
    strategy._make_option = lambda strike, expiry, right: SimpleNamespace(
        asset_type="option", symbol="XSP", expiration=expiry, strike=strike, right=right)
    strategy.on_trading_iteration()
    strategy._manage_position.assert_called_once_with(a)
    assert len(ledger.open_positions()) == 2
    assert strategy.parameters["quantity"] == 1


def test_strategy_restart_restores_independent_exit_and_settings(strategy, ledger):
    a = strategy_entry(strategy)
    b = strategy_entry(strategy, prices=PRICES_B)
    strategy._close_all_options(exit_reason="STOP_LOSS", position_id=a)
    restored = iron_condor.SPXIronCondor()
    restored.parameters = {**strategy.parameters, "profit_target": .8, "position_ledger": ledger}
    restored._asset_registry = dict(strategy._asset_registry)
    restored.broker = SimpleNamespace(name="Tradier")
    restored.initialize()
    restored.get_datetime = Mock(return_value=NOW)
    restored.get_orders = Mock(return_value=[])
    restored.get_positions = strategy.get_positions
    restored.get_quote = strategy.get_quote
    restored.submit_order = Mock()
    assert restored._runtime(a).exit_requested
    assert restored._runtime(a).pending_exit_reason == "STOP_LOSS"
    assert restored._runtime(b).settings["profit_target"] == .3
    assert restored._runtime(b).current_trade_id == b
    assert restored._runtime(b).entry_credit_cash == 60
    restored._close_all_options(position_id=a)
    restored.submit_order.assert_not_called()


def test_settlement_of_one_expiry_keeps_other_position_managed(strategy, ledger):
    a = strategy_entry(strategy)
    strategy.parameters["dte"] = 30
    later = option_legs((80, 85, 120, 125))
    for asset, _ in later:
        asset.expiration = "2026-12-20"
    b = strategy_entry(strategy, prices=PRICES_B, legs=later)
    strategy.get_datetime.return_value = NOW + timedelta(days=20)
    strategy.get_positions = Mock(side_effect=lambda: [
        SimpleNamespace(asset=strategy._asset_registry[key], quantity=q)
        for key, q in ledger.position_quantities(b).items()])
    assert strategy._reconcile_broker_state()
    assert ledger.get_basket(ledger.position_baskets(a)[0].package_id).state == "expired"
    assert ledger.position_opening_cash(b) == 60
    assert ledger.portfolio_defined_risk() == 440
    assert strategy._runtime(b).position_open


def test_single_basket_trajectory_cannot_reuse_previous_position_for_multiple_entries(strategy, tmp_path):
    from trade_logger import TrajectoryLogger
    a = strategy_entry(strategy)
    collector = TrajectoryLogger([strategy.name], tmp_path / "trajectory.jsonl")
    collector._cached = lambda kind, *_: .1 if kind == "price" else {}
    first = collector._strategy_state(strategy, NOW, "HOLD")
    assert first["trade_id"] == a
    b = strategy_entry(strategy, prices=PRICES_B)
    combined = collector._strategy_state(strategy, NOW, "HOLD")
    assert combined["trade_id"] is None
    assert combined["pnl"] is None
    assert combined["entry_credit"] is None
    assert combined["max_risk"] == 1000
    assert {position["position_id"] for position in combined["positions"]} == {a, b}
