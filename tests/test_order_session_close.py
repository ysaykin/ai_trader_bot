"""DAY order cleanup must survive session changes without inventing fills."""
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest

from dashboard.monitoring import order_activity
from strategies.iron_condor import SPXIronCondor
from strategies.order_sessions import day_session_ended
from strategies.position_ledger import LedgerLeg, PositionLedger


def test_ended_day_order_leaves_pending_but_retains_reconciliation():
    package = {"package_id": "local", "strategy_id": "Bro", "state": "submitted",
               "duration": "day", "created_at": "2026-11-09T15:00:00Z", "legs": []}
    with patch("dashboard.monitoring.datetime", wraps=datetime) as clock:
        clock.now.return_value = datetime(2026, 11, 9, 21, tzinfo=timezone.utc)
        rows = order_activity({"ledger_packages": [package], "orders_status": "ready", "raw_orders": []})
    assert rows[0]["pending"] is False
    assert rows[0]["needs_review"] is True
    assert "Session ended" in rows[0]["status"]
    assert "reservation retained" in rows[0]["evidence"]
    assert package["state"] == "submitted"


def test_close_hook_cancels_only_owned_day_orders_and_reconciles():
    ledger = PositionLedger()
    try:
        for identity, owner, duration in [("day", "Bro", "day"), ("gtc", "Bro", "gtc"), ("other", "Other", "day")]:
            ledger.record_submission(identity, owner, "open", [LedgerLeg(identity + "-leg", identity, 1),
                                                               LedgerLeg(identity + "-wing", identity + "-wing", -1)],
                                     duration=duration,
                                     created_at=datetime(2026, 11, 9, 15, tzinfo=timezone.utc))
            ledger.bind_broker_submission(identity, SimpleNamespace(identifier=identity, child_orders=[]))
        strategy = SPXIronCondor.__new__(SPXIronCondor)
        strategy._name = "Bro"
        strategy.parameters = {}
        strategy._position_ledger = ledger
        strategy.broker = SimpleNamespace(market="NYSE")
        strategy.get_datetime = Mock(return_value=datetime(2026, 11, 9, 21, tzinfo=timezone.utc))
        strategy._cancellation_retries = {}
        order = SimpleNamespace(identifier="day", status="open", time_in_force="day", is_active=lambda: True)
        strategy.get_order = Mock(return_value=order)
        strategy.cancel_order = Mock()
        strategy.get_positions = Mock(return_value=[])
        strategy.get_orders = Mock(return_value=[])
        strategy._position_states = {}
        strategy._hydrate_from_ledger = Mock()
        strategy._reconcile_broker_state = Mock(wraps=strategy._reconcile_broker_state)
        strategy._record_trajectory = Mock()
        strategy.after_market_closes()
        strategy.after_market_closes()
        strategy.cancel_order.assert_called_once_with(order)
        assert strategy._reconcile_broker_state.call_count >= 1
        assert ledger.is_inflight(ledger.get_basket("day"))
    finally:
        ledger.close()


@pytest.mark.parametrize("created,now,ended", [
    ("2026-11-09T15:00:00Z", "2026-11-09T20:59:59Z", False),
    ("2026-11-09T15:00:00Z", "2026-11-09T21:00:00Z", True),
    ("2026-07-06T14:00:00Z", "2026-07-06T20:00:00Z", True),
    ("2026-11-27T15:00:00Z", "2026-11-27T17:59:59Z", False),
    ("2026-11-27T15:00:00Z", "2026-11-27T18:00:00Z", True),
    ("2026-11-25T15:00:00Z", "2026-11-26T15:00:00Z", True),
    ("2026-11-26T15:00:00Z", "2026-11-26T21:00:00Z", False),
    ("2026-11-06T15:00:00Z", "2026-11-09T14:00:00Z", True),
    ("2026-11-09T22:00:00Z", "2026-11-10T15:00:00Z", False),
    ("2026-11-09T22:00:00Z", "2026-11-10T21:00:00Z", True),
    ("2026-11-10T00:30:00Z", "2026-11-10T01:00:00Z", False),
    (None, "2026-11-09T21:00:00Z", False),
])
def test_day_session_boundary(created, now, ended):
    assert day_session_ended(created, datetime.fromisoformat(now)) is ended


def test_broker_active_after_close_is_an_exception_and_gtc_stays_pending():
    rows = order_activity({"orders_status": "ready", "raw_orders": [
        {"id": "day", "status": "pending", "duration": "day", "create_date": "2026-11-09T15:00:00Z"},
        {"id": "gtc", "status": "open", "duration": "gtc", "create_date": "2026-11-06T15:00:00Z"},
    ]}, now=datetime(2026, 11, 9, 21, tzinfo=timezone.utc))
    by_id = {row["id"]: row for row in rows}
    assert not by_id["day"]["pending"]
    assert by_id["day"]["needs_review"]
    assert "Broker still reports active" in by_id["day"]["evidence"]
    assert by_id["gtc"]["pending"]


def test_reconciliation_view_keeps_ended_sessions_out_of_pending():
    pytest.importorskip("reflex")
    from reflex_dashboard.state import DashboardState
    state = DashboardState(_reflex_internal_init=True)
    state.execution_orders = [
        {"id": "today", "pending": True, "needs_review": False},
        {"id": "ended", "pending": False, "needs_review": True},
        {"id": "filled", "pending": False, "needs_review": False},
    ]
    assert [row["id"] for row in state.visible_execution_orders] == ["today"]
    state.set_order_view_mode("Needs reconciliation")
    assert [row["id"] for row in state.visible_execution_orders] == ["ended"]
    state.set_order_view_mode("All recent orders")
    assert len(state.visible_execution_orders) == 3


@pytest.mark.parametrize("now,cancel_error,broker_duration", [
    ("2026-11-09T20:59:59Z", None, "day"),
    ("2026-11-09T21:00:00Z", RuntimeError("broker refused cancellation"), "day"),
    ("2026-11-10T15:00:00Z", None, "day"),
    ("2026-11-09T21:00:00Z", None, "gtc"),
])
def test_catchup_respects_close_broker_duration_and_confirmed_partial_fills(now, cancel_error, broker_duration):
    ledger = PositionLedger()
    try:
        ledger.record_submission("partial", "Bro", "open", [LedgerLeg("a", "a", 2), LedgerLeg("b", "b", -2)],
                                 created_at="2026-11-09T15:00:00Z")
        ledger.record_fill("a", 1, 1)
        ledger.record_fill("b", -1, 2)
        parent = SimpleNamespace(identifier="parent", status="open", time_in_force=broker_duration, child_orders=[], is_active=lambda: True)
        ledger.bind_broker_submission("partial", parent)
        strategy = SPXIronCondor.__new__(SPXIronCondor)
        strategy._name = "Bro"
        strategy.parameters = {}
        strategy._position_ledger = ledger
        strategy.broker = SimpleNamespace(market="NYSE")
        strategy.get_datetime = Mock(return_value=datetime.fromisoformat(now))
        strategy._cancellation_retries = {}
        strategy.get_order = Mock(return_value=parent)
        strategy.cancel_order = Mock(side_effect=cancel_error)
        strategy._reconcile_inflight_orders([])
        expected = now >= "2026-11-09T21:00:00Z" and broker_duration == "day"
        assert strategy.cancel_order.call_count == int(expected)
        assert ledger.owned_quantities("Bro") == {"a": 1, "b": -1}
        assert ledger.is_inflight(ledger.get_basket("partial"))
    finally:
        ledger.close()
