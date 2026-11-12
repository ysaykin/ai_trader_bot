"""Session waits must survive a suspended host without losing reconciliation."""
from datetime import datetime, timezone
import os
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest

from lumibot.brokers.tradier import Tradier


def test_real_broker_wait_rechecks_after_host_resume():
    from strategies.session_runtime import install_session_waits

    broker = Tradier.__new__(Tradier)
    broker.is_market_open = Mock(side_effect=[False, True])
    broker.get_time_to_open = Mock(return_value=17 * 3600)
    # The host resumes during market hours after its first timed sleep.
    broker.sleep = Mock()
    broker.should_continue = Mock(return_value=True)
    strategy = SimpleNamespace(_reconcile_session_orders=Mock())
    install_session_waits(broker)
    broker._await_market_to_open(strategy=strategy)
    broker.sleep.assert_called_once_with(30)
    strategy._reconcile_session_orders.assert_called_once()


def test_close_wait_keeps_lead_time_and_reconciles_between_polls():
    from strategies.session_runtime import install_session_waits

    broker = Tradier.__new__(Tradier)
    broker.is_market_open = Mock(return_value=True)
    broker.get_time_to_close = Mock(side_effect=[120, 45, 20])
    broker.sleep = Mock()
    broker.should_continue = Mock(return_value=True)
    strategy = SimpleNamespace(_reconcile_session_orders=Mock())
    install_session_waits(broker)
    broker._await_market_to_close(timedelta=.5, strategy=strategy)
    assert [call.args[0] for call in broker.sleep.call_args_list] == [30, 15]
    assert strategy._reconcile_session_orders.call_count == 3


def test_session_wait_installation_is_idempotent_and_shutdown_interrupts():
    from strategies.session_runtime import install_session_waits

    broker = Tradier.__new__(Tradier)
    broker.is_market_open = Mock(return_value=False)
    broker.get_time_to_open = Mock(return_value=100)
    broker.sleep = Mock()
    broker.should_continue = Mock(return_value=False)
    install_session_waits(broker)
    installed = broker._await_market_to_open
    install_session_waits(broker)
    assert broker._await_market_to_open is installed
    broker._await_market_to_open()
    broker.sleep.assert_not_called()


def test_preopen_lead_time_returns_without_waiting():
    from strategies.session_runtime import install_session_waits

    broker = Tradier.__new__(Tradier)
    broker.is_market_open = Mock(return_value=False)
    broker.get_time_to_open = Mock(return_value=30)
    broker.sleep = Mock()
    broker.should_continue = Mock(return_value=True)
    install_session_waits(broker)
    broker._await_market_to_open(timedelta=1)
    broker.sleep.assert_not_called()


def test_entrypoint_installs_session_guard_on_the_real_broker_instance():
    import main

    broker = Tradier.__new__(Tradier)
    broker._get_stream_object = Mock(return_value=Mock())
    broker._launch_stream = Mock()
    with patch.dict(os.environ, {"TRADING_BROKER": "tradier", "TRADIER_ACCESS_TOKEN": "test-token",
                                 "TRADIER_ACCOUNT_NUMBER": "test-account", "TRADIER_PAPER": "true"}), \
         patch("lumibot.brokers.Tradier", return_value=broker), \
         patch("strategies.broker_reads.install_sdk_reads"):
        assert main.get_broker() is broker
    assert broker._session_waits_installed is True
    broker._launch_stream.assert_called_once()


@pytest.mark.parametrize("iteration,expected", [(False, False), (True, True)])
def test_health_distinguishes_broker_polling_from_strategy_execution(tmp_path, iteration, expected):
    from strategies.session_runtime import strategy_health

    log = tmp_path / "trading.log"
    lines = ["2026-11-11 08:00:00 INFO Starting SPX Iron Condor bot"]
    if iteration:
        lines.append("2026-11-11 11:59:00 INFO Executing the on_trading_iteration lifecycle method")
    lines.append('2026-11-11 12:00:00 INFO LUMIBOT_TELEMETRY {"broker_snapshot": {}}')
    log.write_text("\n".join(lines))
    result = strategy_health(log, now=datetime(2026, 11, 11, 17, tzinfo=timezone.utc))
    assert result["healthy"] is expected
    assert result["last_broker_poll"] == "2026-11-11T12:00:00-05:00"


def test_old_iteration_before_restart_does_not_prove_current_process_healthy(tmp_path):
    from strategies.session_runtime import strategy_health

    log = tmp_path / "trading.log"
    log.write_text("2026-11-11 11:59:00 INFO Executing the on_trading_iteration lifecycle method\n"
                   "2026-11-11 12:00:00 INFO Starting SPX Iron Condor bot\n")
    assert not strategy_health(log, now=datetime(2026, 11, 11, 17, 20, tzinfo=timezone.utc))["healthy"]


def test_health_allows_closed_session_and_startup_grace(tmp_path):
    from strategies.session_runtime import strategy_health

    log = tmp_path / "trading.log"
    log.write_text("2026-11-11 12:00:00 INFO Starting SPX Iron Condor bot\n")
    assert strategy_health(log, now=datetime(2026, 11, 11, 17, 1, tzinfo=timezone.utc))["healthy"]
    assert strategy_health(log, now=datetime(2026, 11, 11, 22, tzinfo=timezone.utc))["healthy"]
    assert not strategy_health(tmp_path / "missing", now=datetime(2026, 11, 11, 17, tzinfo=timezone.utc))["healthy"]
    assert strategy_health(log, now=datetime(2026, 11, 27, 18, 1, tzinfo=timezone.utc))["state"] == "standby"


def test_wait_hook_reconciles_host_and_followers_once_without_creating_inventory(tmp_path):
    from strategies.broker_reads import _fresh
    from strategies.position_ledger import PositionLedger, LedgerLeg
    from test_day_order_expiry import submit, strategy_for

    ledger = PositionLedger(tmp_path / "orders.sqlite3")
    try:
        submit(ledger)
        basket = ledger.get_basket("old")
        ledger.record_submission("follower", "Other", "open", [
            LedgerLeg("other-" + str(i), leg["asset_key"], leg["requested_signed_quantity"])
            for i, leg in enumerate(basket.legs)], created_at=basket.created_at)
        host, follower = strategy_for(ledger), strategy_for(ledger)
        follower._name = "Other"
        host._attached_strategies = [follower]
        def fresh_empty():
            assert _fresh.get() is True
            return []
        host.get_positions = Mock(side_effect=fresh_empty)
        host.get_orders = Mock(side_effect=fresh_empty)
        before = ledger.all_owned_quantities()
        host._reconcile_session_orders()
        assert ledger.get_basket("old").terminal_status == "canceled"
        assert ledger.get_basket("follower").terminal_status == "canceled"
        assert ledger.all_owned_quantities() == before
        host._reconcile_session_orders()
        assert host.get_positions.call_count == 1
    finally:
        ledger.close()


def test_wait_hook_retains_reservations_without_inventory_evidence(tmp_path):
    from strategies.position_ledger import PositionLedger
    from test_day_order_expiry import submit, strategy_for

    ledger = PositionLedger(tmp_path / "orders.sqlite3")
    try:
        submit(ledger)
        host = strategy_for(ledger)
        host.get_positions = Mock(return_value=None)
        host.get_orders = Mock(return_value=[])
        host._reconcile_session_orders()
        assert ledger.is_inflight(ledger.get_basket("old"))
    finally:
        ledger.close()
