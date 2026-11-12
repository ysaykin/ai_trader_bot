"""Replay the quote, stop and restart failures from the November loss review."""
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest

from strategies.broker_reads import BrokerReadUnavailable, install_sdk_reads
from strategies.iron_condor_math import evaluate_mfe_ratchet
from test_concurrent_positions import NOW, ledger as ledger, strategy as strategy, strategy_entry


def quotes(strategy, short=2.0, long=.1, *, wide=False, missing=False):
    def quote(asset):
        if missing and asset.strike == 90:
            return None
        value = long if asset.strike in {90, 115} else short
        return {"bid": .5 if wide and asset.strike == 110 else value,
                "ask": 20 if wide and asset.strike == 110 else value,
                "bid_date": int(strategy.get_datetime().timestamp() * 1000)}
    strategy.get_quote = Mock(side_effect=quote)


@pytest.mark.parametrize("missing", [False, True])
def test_junk_quotes_cannot_trigger_stop_or_update_excursions(strategy, ledger, missing):
    pid = strategy_entry(strategy)
    quotes(strategy, wide=True, missing=missing)
    strategy._close_all_options = Mock()
    for minute in (0, 5):
        strategy.get_datetime.return_value = NOW + timedelta(minutes=minute)
        strategy._manage_position(pid)
    strategy._close_all_options.assert_not_called()
    assert strategy._runtime(pid).mfe_pnl == 0
    assert strategy._runtime(pid).mae_pnl == 0


def test_stop_requires_two_distinct_valid_readings(strategy, ledger):
    pid = strategy_entry(strategy)
    quotes(strategy)
    strategy._close_all_options = Mock()
    strategy._manage_position(pid)
    strategy._close_all_options.assert_not_called()
    # Reusing one cached provider reading cannot confirm a trigger.
    strategy._manage_position(pid)
    strategy._close_all_options.assert_not_called()
    strategy.get_datetime.return_value = NOW + timedelta(minutes=5)
    strategy._manage_position(pid)
    assert strategy._close_all_options.call_args.kwargs["exit_reason"] == "STOP_LOSS"


def test_invalid_reading_resets_stop_confirmation(strategy, ledger):
    pid = strategy_entry(strategy)
    quotes(strategy)
    strategy._close_all_options = Mock()
    strategy._manage_position(pid)
    strategy.get_datetime.return_value = NOW + timedelta(minutes=5)
    quotes(strategy, wide=True)
    strategy._manage_position(pid)
    quotes(strategy)
    strategy.get_datetime.return_value = NOW + timedelta(minutes=10)
    strategy._manage_position(pid)
    strategy._close_all_options.assert_not_called()


@pytest.mark.parametrize("gap,cached", [(5, True), (11, False)])
def test_cached_or_old_reading_cannot_confirm_stop(strategy, ledger, gap, cached):
    pid = strategy_entry(strategy)
    quotes(strategy)
    fetch = strategy.get_quote.side_effect
    def quote(asset):
        result = fetch(asset)
        if cached:
            result["bid_date"] = int(NOW.timestamp() * 1000)
        return result
    strategy.get_quote.side_effect = quote
    strategy._close_all_options = Mock()
    strategy._manage_position(pid)
    strategy.get_datetime.return_value = NOW + timedelta(minutes=gap)
    strategy._manage_position(pid)
    strategy._close_all_options.assert_not_called()


def test_opening_window_defers_non_expiry_pnl_exits(strategy, ledger):
    pid = strategy_entry(strategy)
    quotes(strategy)
    strategy._close_all_options = Mock()
    for minutes in (-25, -20):  # 09:35 and 09:40 Eastern
        strategy.get_datetime.return_value = NOW + timedelta(minutes=minutes)
        strategy._manage_position(pid)
    strategy._close_all_options.assert_not_called()


def test_saved_stop_intent_waits_through_opening_window(strategy, ledger):
    pid = strategy_entry(strategy)
    ledger.update_position(pid, exit_reason="STOP_LOSS")
    strategy._hydrate_from_ledger()
    quotes(strategy)
    strategy.get_datetime.return_value = NOW - timedelta(minutes=20)
    strategy._submit_package = Mock(return_value=[])
    strategy._manage_position(pid)
    strategy._submit_package.assert_not_called()
    assert ledger.get_position(pid).exit_reason == "STOP_LOSS"


def test_expiry_day_stops_remain_available_during_opening_window(strategy, ledger):
    pid = strategy_entry(strategy)
    quotes(strategy)
    strategy._close_all_options = Mock()
    for minute in (0, 5):
        strategy.get_datetime.return_value = NOW.replace(day=20, hour=14, minute=35) + timedelta(minutes=minute)
        strategy._manage_position(pid)
    assert strategy._close_all_options.call_args.kwargs["exit_reason"] == "STOP_LOSS"


def test_impossible_profit_cannot_arm_trailing_exit(strategy, ledger):
    pid = strategy_entry(strategy)
    quotes(strategy, short=.1, long=1.0)
    strategy._close_all_options = Mock()
    strategy._manage_position(pid)
    assert strategy._runtime(pid).mfe_pnl <= strategy._runtime(pid).entry_credit_cash
    strategy._close_all_options.assert_not_called()


def test_stop_close_does_not_fall_back_to_wing_width_on_junk_quotes(strategy, ledger):
    pid = strategy_entry(strategy)
    quotes(strategy, wide=True)
    strategy._submit_package = Mock()
    strategy._close_all_options(exit_reason="STOP_LOSS", position_id=pid)
    strategy._submit_package.assert_not_called()
    assert strategy._runtime(pid).exit_requested  # Retain management and intent.


def test_expiry_close_can_override_quote_blocked_stop(strategy, ledger):
    pid = strategy_entry(strategy)
    quotes(strategy, wide=True)
    strategy._submit_package = Mock(return_value=[])
    strategy._close_all_options(exit_reason="STOP_LOSS", position_id=pid)
    strategy._submit_package.assert_not_called()
    strategy._is_expiring_today = Mock(return_value=True)
    strategy._emergency_close_debit_fallback = Mock(return_value=1.2)
    strategy.before_market_closes()
    assert strategy._submit_package.call_args.kwargs["price"] == 1.2
    assert strategy._runtime(pid).pending_exit_reason == "EOD_CLOSE"


def test_clean_stop_close_limit_is_near_validated_mid(strategy, ledger):
    pid = strategy_entry(strategy)
    quotes(strategy)
    strategy._submit_package = Mock(return_value=[])
    strategy._close_all_options(exit_reason="STOP_LOSS", position_id=pid)
    assert strategy._submit_package.call_args.kwargs["price"] == pytest.approx(3.8)


def test_paper_entries_keep_the_quoted_credit_by_default(strategy, ledger, monkeypatch):
    monkeypatch.delenv("SANDBOX_CREDIT_DISCOUNT_PCT", raising=False)
    monkeypatch.setenv("TRADIER_PAPER", "true")
    pid = strategy_entry(strategy)
    quotes(strategy, short=.7)
    assert strategy._package_price(strategy._runtime(pid).owned_legs) == pytest.approx(1.2)


def test_breakeven_noise_exit_is_disabled():
    assert evaluate_mfe_ratchet(100, 35, -5) is None
    assert evaluate_mfe_ratchet(100, 48, 28) == "MFE_TRAIL_LOCK"


def test_crash_does_not_flatten_host_or_follower(strategy, ledger):
    pid = strategy_entry(strategy)
    strategy._close_all_options = Mock()
    follower = SimpleNamespace(on_abrupt_closing=Mock())
    strategy._attached_strategies = [follower]
    strategy.on_abrupt_closing()
    strategy._close_all_options.assert_not_called()
    assert ledger.get_position(pid).exit_reason is None
    follower.on_abrupt_closing.assert_called_once()


def test_previous_session_abrupt_intent_expires_without_liquidation(strategy, ledger):
    pid = strategy_entry(strategy)
    ledger.update_position(pid, exit_reason="ABRUPT_CLOSE")
    strategy._hydrate_from_ledger()
    strategy.get_datetime.return_value = NOW + timedelta(days=1)
    quotes(strategy, short=.65)
    strategy._close_all_options = Mock()
    strategy._manage_position(pid)
    strategy._close_all_options.assert_not_called()
    assert ledger.get_position(pid).exit_reason is None
    assert not strategy._runtime(pid).exit_requested


def test_after_hours_crash_intent_does_not_survive_into_next_session(strategy, ledger):
    pid = strategy_entry(strategy)
    strategy.get_datetime.return_value = NOW.replace(hour=22)
    quotes(strategy, short=.65)
    strategy._close_all_options(exit_reason="ABRUPT_CLOSE", position_id=pid)
    parent = strategy.orders[-1]
    parent.status = "canceled"
    ledger.record_terminal_status(ledger.position_baskets(pid)[-1].package_id, "canceled")
    strategy.get_datetime.return_value = NOW + timedelta(days=1)
    strategy._close_all_options = Mock()
    strategy._manage_position(pid)
    strategy._close_all_options.assert_not_called()
    assert ledger.get_position(pid).exit_reason is None


def test_legacy_breakeven_intent_is_withdrawn(strategy, ledger):
    pid = strategy_entry(strategy)
    ledger.update_position(pid, exit_reason="BREAKEVEN_RATCHET")
    strategy._hydrate_from_ledger()
    quotes(strategy, short=.65)
    strategy._close_all_options = Mock()
    strategy._manage_position(pid)
    strategy._close_all_options.assert_not_called()
    assert ledger.get_position(pid).exit_reason is None


def test_old_abrupt_order_is_canceled_before_retiring_intent(strategy, ledger):
    pid = strategy_entry(strategy)
    quotes(strategy, short=.65)
    strategy._close_all_options(exit_reason="ABRUPT_CLOSE", position_id=pid)
    parent = strategy.orders[-1]
    closing = ledger.position_baskets(pid)[-1]
    before = ledger.position_quantities(pid)
    strategy.get_datetime.return_value = NOW + timedelta(days=1)
    strategy._close_all_options = Mock()
    strategy._manage_position(pid)
    strategy.cancel_order.assert_called_once_with(parent)
    strategy._close_all_options.assert_not_called()
    assert ledger.is_inflight(ledger.get_basket(closing.package_id))
    assert ledger.position_quantities(pid) == before
    parent.status = "canceled"
    ledger.record_terminal_status(closing.package_id, "canceled")
    strategy._manage_position(pid)
    assert ledger.get_position(pid).exit_reason is None


def test_two_wings_cannot_both_be_marked_at_maximum_loss(strategy, ledger):
    pid = strategy_entry(strategy)
    quotes(strategy, short=4.9)
    strategy._close_all_options = Mock()
    strategy._manage_position(pid)
    strategy._close_all_options.assert_not_called()
    assert strategy._runtime(pid).mae_pnl == 0


def test_payoff_bounds_preserve_quantity_and_allow_only_tick_rounding(strategy, ledger):
    from strategies.exit_safety import bounded_option_value
    from test_concurrent_positions import option_legs
    legs = option_legs(quantity=3)
    assert bounded_option_value(legs, [.1, 2.6, 2.6, .1]) == -1500
    assert bounded_option_value(legs, [.1, 2.61, 2.6, .1]) == -1500
    assert bounded_option_value(legs, [.1, 2.7, 2.6, .1]) is None
    assert bounded_option_value(legs, [.1, .2]) is None


def test_sdk_poll_budget_failure_skips_cycle_without_false_inventory():
    inventory = [object()]
    def polling():
        broker._pull_positions(None)
        inventory.clear()  # Must never reach mutation after a failed read.
    broker = SimpleNamespace(_pull_positions=Mock(side_effect=BrokerReadUnavailable(
        "standard request budget unavailable")), do_polling=polling)
    with patch("strategies.broker_reads.get_broker_reads", return_value=Mock()):
        install_sdk_reads(broker, "test", "test")
    broker.do_polling()
    assert len(inventory) == 1
    with pytest.raises(BrokerReadUnavailable):
        broker._pull_positions(None)  # Execution consumers must still fail closed.


def test_stop_cooldown_blocks_entries_and_adds_after_runtime_rebuild(strategy, ledger):
    pid = strategy_entry(strategy)
    quotes(strategy)
    strategy._close_all_options(exit_reason="STOP_LOSS", position_id=pid)
    until = ledger.entry_cooldown_until(strategy._strategy_id())
    assert until == NOW + timedelta(minutes=60)
    # A fresh in-memory strategy must consult the same durable cooldown.
    strategy._hydrate_from_ledger()
    strategy.get_datetime.return_value = NOW + timedelta(minutes=5)
    legs = strategy._runtime(pid).owned_legs
    for intent in ("open", "add"):
        with pytest.raises(RuntimeError, match="stop cooldown"):
            strategy._submit_package(legs, intent=intent, order_type="credit", price=1.2, position_id=pid)


def test_cooldown_is_scoped_and_cannot_be_shortened(ledger):
    ledger.block_entries_until("one", NOW + timedelta(hours=1))
    ledger.block_entries_until("one", NOW + timedelta(minutes=5))
    assert ledger.entry_cooldown_until("one") == NOW + timedelta(hours=1)
    assert ledger.entry_cooldown_until("other") is None


def test_cooldown_survives_closing_and_reopening_database(tmp_path):
    from strategies.position_ledger import PositionLedger
    path = tmp_path / "ledger.sqlite3"
    original = PositionLedger(path)
    try:
        original.block_entries_until("owner", NOW + timedelta(hours=1))
    finally:
        original.close()
    restored = PositionLedger(path)
    try:
        assert restored.entry_cooldown_until("owner") == NOW + timedelta(hours=1)
    finally:
        restored.close()


def test_confirmed_stop_fill_extends_cooldown_after_position_is_closed(strategy, ledger):
    pid = strategy_entry(strategy)
    quotes(strategy)
    strategy._close_all_options(exit_reason="STOP_LOSS", position_id=pid)
    parent = strategy.orders[-1]
    strategy.get_datetime.return_value = NOW + timedelta(minutes=10)
    for child in parent.child_orders:
        price = 2.0 if child.side == "buy_to_close" else .1
        child.status = "filled"
        child.filled_quantity = child.quantity
        child.avg_fill_price = price
        strategy.on_filled_order(None, child, price, child.quantity, 100)
    assert ledger.position_quantities(pid) == {}
    assert ledger.entry_cooldown_until(strategy._strategy_id()) == NOW + timedelta(minutes=70)


def test_minimum_credit_is_enforced_at_submission_boundary(strategy, ledger):
    from test_concurrent_positions import option_legs
    with pytest.raises(ValueError, match="credit must be at least"):
        strategy._submit_package(option_legs(), intent="open", order_type="credit", price=.26)
    assert ledger.open_positions(strategy._strategy_id()) == ()
    strategy.submit_order.assert_not_called()


def test_add_minimum_credit_uses_existing_position_width_after_switch(strategy, ledger):
    from test_concurrent_positions import option_legs
    strategy.parameters["wing_width"] = 10
    pid = strategy_entry(strategy, legs=option_legs(strikes=(85, 95, 110, 120)))
    strategy.parameters["wing_width"] = 5
    with pytest.raises(ValueError, match="credit must be at least 1.00"):
        strategy._submit_package(strategy._runtime(pid).owned_legs, intent="add",
                                 position_id=pid, order_type="credit", price=.7)


def test_profit_limit_preserves_target_after_fees(strategy, ledger):
    pid = strategy_entry(strategy)
    position = strategy._runtime(pid)
    position.pending_exit_reason = "PROFIT_TARGET"
    assert strategy._profit_close_debit_ceiling(position, position.owned_legs) == .8


def test_watchdog_alerts_once_on_stall_and_resets_after_real_iteration(caplog):
    from strategies.session_runtime import IterationWatchdog
    clock = [NOW]
    host = SimpleNamespace(name="host")
    follower = SimpleNamespace(name="follower")
    watchdog = IterationWatchdog([host, follower], clock=lambda: clock[0])
    clock[0] += timedelta(minutes=11)
    host._last_iteration_completed_at = clock[0]
    # Broker activity is deliberately irrelevant to this check.
    follower._last_broker_poll = clock[0]
    assert watchdog.check() == ("follower",)
    assert watchdog.check() == ("follower",)
    assert caplog.text.count("TRADING_ITERATION_STALLED") == 1
    follower._last_iteration_completed_at = clock[0]
    assert watchdog.check() == ()
    clock[0] += timedelta(minutes=11)
    host._last_iteration_completed_at = clock[0]
    assert watchdog.check() == ("follower",)
    assert caplog.text.count("TRADING_ITERATION_STALLED") == 2


@pytest.mark.parametrize("hour,minute", [(14, 35), (22, 0)])
def test_watchdog_allows_opening_grace_and_closed_session(hour, minute):
    from strategies.session_runtime import IterationWatchdog
    clock = [NOW.replace(hour=13)]
    watchdog = IterationWatchdog([SimpleNamespace(name="host")], clock=lambda: clock[0])
    clock[0] = NOW.replace(hour=hour, minute=minute)
    assert watchdog.check() == ()
