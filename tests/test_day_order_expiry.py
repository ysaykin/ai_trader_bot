"""Expired DAY remainders release capacity without changing filled inventory."""
from datetime import datetime, timezone
from types import SimpleNamespace
from pathlib import Path
from unittest.mock import Mock
from unittest.mock import patch

import pytest

from dashboard.monitoring import order_activity
from dashboard.positions import read_packages
from strategies.iron_condor import SPXIronCondor
from strategies.position_ledger import LedgerLeg, PositionLedger, asset_key


CREATED = "2026-11-09T15:00:00Z"
NOW = datetime(2026, 11, 10, 13, tzinfo=timezone.utc)


@pytest.fixture
def ledger(tmp_path):
    instance = PositionLedger(tmp_path / "orders.sqlite3")
    yield instance
    instance.close()


def submit(ledger, *, duration="day", created=CREATED, filled=0):
    assets = [SimpleNamespace(asset_type="option", symbol="XSP", expiration="2026-11-20",
                              strike=strike, right="put") for strike in (700, 705)]
    legs = [LedgerLeg(str(i), asset_key(asset), quantity) for i, (asset, quantity)
            in enumerate(zip(assets, (2, -2)))]
    ledger.record_submission("old", "Bro", "open", legs, duration=duration, created_at=created)
    if filled:
        for leg in legs:
            ledger.record_fill(leg.order_id, filled if leg.requested_signed_quantity > 0 else -filled, 1)
    return [SimpleNamespace(asset=asset, quantity=filled * sign)
            for asset, sign in zip(assets, (1, -1))] if filled else []


def strategy_for(ledger):
    strategy = SPXIronCondor.__new__(SPXIronCondor)
    strategy._name = "Bro"
    strategy.parameters = {}
    strategy._position_ledger = ledger
    strategy.broker = SimpleNamespace(market="NYSE")
    strategy.get_datetime = Mock(return_value=NOW)
    strategy.get_order = Mock(return_value=None)
    strategy.cancel_order = Mock()
    strategy._cancellation_retries = {}
    strategy._record_filled_entry = Mock()
    strategy._record_filled_addition = Mock()
    return strategy


def test_missing_expired_day_order_releases_reservation_and_false_review(ledger):
    submit(ledger)
    strategy = strategy_for(ledger)
    assert ledger.portfolio_defined_risk() == 1000
    assert strategy._reconcile_inflight_orders([], positions=[]) is False
    basket = ledger.get_basket("old")
    assert basket.terminal_status == "canceled"
    assert not ledger.is_inflight(basket)
    assert ledger.portfolio_defined_risk() == 0
    assert ledger.open_positions("Bro") == ()
    rows = order_activity({"orders_status": "ready", "raw_orders": [],
                           "ledger_packages": read_packages(Path(ledger.path))}, now=NOW)
    assert len(rows) == 1
    assert rows[0]["status"] == "Canceled / DAY expired"
    assert not rows[0]["needs_review"]
    assert not rows[0]["pending"]
    assert "reservation released" in rows[0]["evidence"]
    strategy.cancel_order.assert_not_called()


def test_expired_partial_releases_only_remainder_and_keeps_fills(ledger):
    positions = submit(ledger, filled=1)
    strategy = strategy_for(ledger)
    owned, risk = ledger.all_owned_quantities(), ledger.portfolio_defined_risk()
    assert strategy._reconcile_inflight_orders([], positions=positions) is False
    basket = ledger.get_basket("old")
    assert basket.state == "open"
    assert basket.terminal_status == "canceled"
    assert ledger.all_owned_quantities() == owned
    assert ledger.portfolio_defined_risk() == risk - 500


@pytest.mark.parametrize("duration,created,positions", [
    ("gtc", CREATED, []),
    ("day", "2026-11-10T12:00:00Z", []),
    ("day", CREATED, None),
])
def test_current_gtc_and_unavailable_inventory_remain_unresolved(ledger, duration, created, positions):
    submit(ledger, duration=duration, created=created)
    strategy_for(ledger)._reconcile_inflight_orders([], positions=positions)
    assert ledger.is_inflight(ledger.get_basket("old"))


def test_unexplained_broker_inventory_keeps_reservation(ledger):
    positions = submit(ledger, filled=1)
    positions[0].quantity = 2
    strategy_for(ledger)._reconcile_inflight_orders([], positions=positions)
    assert ledger.is_inflight(ledger.get_basket("old"))


def test_broker_active_order_is_not_inferred_canceled(ledger):
    submit(ledger)
    parent = SimpleNamespace(identifier="broker", status="open", time_in_force="day",
                             child_orders=[], is_active=lambda: True)
    ledger.bind_broker_submission("old", parent)
    strategy = strategy_for(ledger)
    strategy._reconcile_inflight_orders([parent], positions=[])
    assert ledger.is_inflight(ledger.get_basket("old"))


def test_catchup_is_idempotent(ledger):
    submit(ledger)
    strategy = strategy_for(ledger)
    assert strategy._reconcile_inflight_orders([], positions=[]) is False
    first = ledger.get_basket("old")
    assert strategy._reconcile_inflight_orders([], positions=[]) is False
    assert ledger.get_basket("old") == first


def test_unknown_active_order_keeps_unidentified_reservation(ledger):
    submit(ledger)
    active = SimpleNamespace(identifier="unknown", status="open", child_orders=[], tag=None)
    strategy_for(ledger)._reconcile_inflight_orders([active], positions=[])
    assert ledger.is_inflight(ledger.get_basket("old"))


def test_expired_close_keeps_opening_inventory_and_exit_intent(ledger):
    positions = submit(ledger, filled=2)
    opening = ledger.get_basket("old")
    ledger.update_position(opening.position_id, exit_reason="profit_target")
    ledger.record_submission("close", "Bro", "close", [
        LedgerLeg("close:" + str(i), leg["asset_key"], -leg["filled_signed_quantity"])
        for i, leg in enumerate(opening.legs)], position_id=opening.position_id, created_at=CREATED)
    before = ledger.all_owned_quantities(), ledger.portfolio_defined_risk()
    assert strategy_for(ledger)._reconcile_inflight_orders([], positions=positions) is False
    assert ledger.get_basket("close").terminal_status == "canceled"
    assert (ledger.all_owned_quantities(), ledger.portfolio_defined_risk()) == before
    assert ledger.get_position(opening.position_id).exit_reason == "profit_target"


def test_cleanup_preview_and_apply_are_idempotent(ledger):
    from tools.reconcile_day_orders import reconcile
    submit(ledger)
    preview = reconcile(ledger, orders=[], positions=[], now=NOW)
    assert preview[0]["result"] == "eligible"
    assert ledger.is_inflight(ledger.get_basket("old"))
    applied = reconcile(ledger, orders=[], positions=[], now=NOW, apply=True)
    assert applied[0]["result"] == "canceled"
    assert reconcile(ledger, orders=[], positions=[], now=NOW, apply=True) == []


def test_cleanup_keeps_unrecorded_executions_even_when_account_net_is_zero(ledger):
    from tools.reconcile_day_orders import reconcile
    submit(ledger)
    ledger.bind_broker_submission("old", SimpleNamespace(identifier="known", child_orders=[]))
    report = reconcile(ledger, orders=[{"id": "known", "status": "canceled", "duration": "day",
                                      "exec_quantity": 1}], positions=[], now=NOW, apply=True)
    assert report[0]["result"] == "retained"
    assert "executions" in report[0]["reason"]
    assert ledger.is_inflight(ledger.get_basket("old"))


def test_worker_reconciles_cancellation_after_strategy_sleeps(ledger):
    from strategies.recovery import RecoveryCoordinator
    submit(ledger)
    ledger.bind_broker_submission('old', SimpleNamespace(identifier='broker'))
    client = Mock(account=ledger.record_scope.account_id, sandbox=True)
    client._fetch_all_orders.return_value = []
    client._fetch_all_positions.return_value = []
    client._get.return_value.json.return_value = {'order': {
        'id': 'broker', 'status': 'canceled', 'duration': 'day',
        'leg': [{'option_symbol': 'XSP261120P00700000', 'exec_quantity': 0},
                {'option_symbol': 'XSP261120P00705000', 'exec_quantity': 0}]}}
    worker = RecoveryCoordinator(ledger=ledger)
    with patch('dashboard.broker_client.get_broker_client', return_value=client), \
            patch('strategies.recovery.datetime') as clock:
        clock.now.return_value = NOW
        assert worker.run_step()
        assert not worker.run_step()
    assert not ledger.is_inflight(ledger.get_basket('old'))
    assert ledger.all_owned_quantities() == {}
    assert ledger.portfolio_defined_risk() == 0
    assert client._get.call_count == 1
    client._submit_once.assert_not_called()
    client.emergency_flatten_positions.assert_not_called()


def test_idle_worker_does_not_poll_broker_without_ended_orders(ledger):
    from strategies.recovery import RecoveryCoordinator
    with patch('dashboard.broker_client.get_broker_client') as get:
        assert not RecoveryCoordinator(ledger=ledger).run_step()
    get.assert_not_called()
