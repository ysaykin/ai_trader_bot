"""Entry candidates must pass wing and credit checks before broker submission."""
from unittest.mock import Mock

from strategies.iron_condor_math import select_short_strikes, select_short_strikes_skew
from test_concurrent_positions import ledger as ledger, strategy as strategy


def test_regular_selection_uses_nearest_complete_wings():
    strikes = [90, 95, 100, 101, 106]
    assert select_short_strikes(strikes, 100, .15, 0, 5) == (95, 101)


def test_skew_selection_does_not_fall_back_to_missing_wings():
    assert select_short_strikes_skew([95, 100, 101], 100, .15, .15, 0, 5) == (None, None)


def test_low_credit_entry_is_a_skip_without_submission_error(strategy, ledger, caplog):
    caplog.set_level("INFO")
    strategy._reconcile_broker_state = Mock(return_value=True)
    strategy._is_entry_window = Mock(return_value=True)
    strategy._check_allocation = Mock(return_value=True)
    strategy._find_expiration = Mock(return_value="2026-11-20")
    strategy._fetch_last_price = Mock(return_value=100)
    strategy._find_short_strikes = Mock(return_value=(95, 110))
    strategy._make_option = Mock(side_effect=lambda strike, expiry, right: Mock(strike=strike))
    strategy._package_price = Mock(return_value=.43)
    strategy._submit_package = Mock(side_effect=ValueError("Atomic package credit must be at least 0.50"))
    strategy._run_trading_iteration()
    strategy._submit_package.assert_not_called()
    assert "below minimum" in caplog.text
    assert "Failed to submit" not in caplog.text
    assert not getattr(strategy, "_position_open_cooldown", False)
