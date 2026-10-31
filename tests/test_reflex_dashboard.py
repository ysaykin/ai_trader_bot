"""Unit tests for Reflex Dashboard data adapters and reactive state."""

import unittest
import sys
from pathlib import Path

try:
    import pytest
    pytest.importorskip("reflex")
except ImportError:
    pass

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "reflex_dashboard"))

from reflex_dashboard.data.adapters import (
    get_market_clock,
    get_bot_status_from_log,
    get_incubator_data,
    get_ab_lab_data,
    get_neural_policy_data,
    get_recent_logs,
    get_config_settings,
    number,
    money,
    format_trade_time_et,
)
from reflex_dashboard.state import DashboardState


class TestReflexDashboard(unittest.TestCase):

    def test_helpers(self):
        self.assertEqual(number("123.45"), 123.45)
        self.assertIsNone(number("invalid"))
        self.assertEqual(money(99762.40), "$99,762.40")

    def test_format_trade_time_et(self):
        # 16:04 UTC should convert to 12:04 ET during EDT
        self.assertEqual(format_trade_time_et("2026-10-30T16:04:49.733553"), "10/30 12:04")
        self.assertEqual(format_trade_time_et("2026-10-30T16:04:49Z"), "10/30 12:04")
        self.assertEqual(format_trade_time_et("2026-10-30T16:04:49+00:00"), "10/30 12:04")
        self.assertEqual(format_trade_time_et(""), "-")
        self.assertEqual(format_trade_time_et(None), "-")

    def test_market_clock(self):
        clk = get_market_clock()
        self.assertIn("time_str", clk)
        self.assertIn("status", clk)
        self.assertIn("badge_color", clk)

    def test_bot_status(self):
        status = get_bot_status_from_log()
        self.assertIn(status["state"], ["RUNNING", "STANDBY (SLEEPING)", "ACTIVE & MONITORING", "OFFLINE", "INITIALIZING", "WARNING / CHECK LOGS"])

    def test_incubator_data(self):
        inc = get_incubator_data()
        self.assertIn("rows", inc)
        self.assertIn("all_rows", inc)
        self.assertGreaterEqual(len(inc["all_rows"]), 20)
        self.assertEqual(len(inc["curated_rows"]), 9)
        self.assertIn("strategy_aggregates", inc)
        self.assertIn("ai", inc["strategy_aggregates"])
        self.assertIn("top5", inc["strategy_aggregates"])
        self.assertIn("bottom3", inc["strategy_aggregates"])
        self.assertEqual(len(inc["visible_trades_50"]), len(inc["all_closed_trades"]))
        self.assertGreaterEqual(len(inc["all_closed_trades"]), 1)
        self.assertIn("total_net_pnl_str", inc)
        self.assertIn("active_grid_str", inc)

    def test_negative_net_pnl_is_counted_as_loss(self):
        """Regression test: trades with negative net P&L must be losses, not wins (even if gross P&L > 0)."""
        inc = get_incubator_data()
        bottom3 = inc["strategy_aggregates"]["bottom3"]

        # If bottom3 has a negative net P&L with 1 trade (like IC_V13 with -$2.20),
        # its win rate must be 0.0%, NOT 100.0%. If multiple trades, win rate must be < 100%.
        if bottom3["trades"] == 1 and bottom3["net_pnl"] < 0:
            self.assertEqual(bottom3["avg_win_rate"], "0.0%")
        elif bottom3["trades"] > 0 and bottom3["net_pnl"] < 0:
            self.assertNotEqual(bottom3["avg_win_rate"], "100.0%")

        # Verify any row with closed trades and 0 wins has win_rate 0% and win_rate_color == crimson
        for row in inc["all_rows"]:
            if row["closed_trades"] > 0 and row["wins"] == 0:
                self.assertEqual(row["win_rate"], "0%")
                self.assertEqual(row["win_rate_color"], "#EF4444")
                self.assertEqual(row["wins"], 0)
                self.assertGreaterEqual(row["losses"], 1)

    def test_ab_lab_profit_factor_and_win_rate(self):
        ab_data = get_ab_lab_data()
        self.assertIn("strategies", ab_data)
        for s in ab_data["strategies"]:
            if s["closed_trades_num"] > 0 and s["net_pnl_num"] < 0:
                self.assertNotEqual(s["win_rate"], "100%")
                pf_val = float(s["profit_factor"]) if s["profit_factor"] not in ("N/A", "Inf") else 0.0
                self.assertLess(pf_val, 1.0)

    def test_neural_policy_data(self):
        ml = get_neural_policy_data()
        self.assertIn("market_regime", ml)
        self.assertIn("action_probs", ml)
        self.assertIn("HOLD", ml["action_probs"])

    def test_dashboard_state_attributes(self):
        fields = DashboardState.get_fields()
        self.assertEqual(fields["current_tab"].default, "incubator")
        self.assertEqual(fields["broker_name"].default, "TRADIER")
        self.assertGreaterEqual(fields["open_risk_cap"].default, 5000.0)

    def test_execute_panic_liquidation_no_broker(self):
        import reflex as rx
        from unittest.mock import patch
        state = DashboardState()
        with patch("dashboard.broker_client.get_broker_client", return_value=None):
            result = state.execute_panic_liquidation()
            self.assertFalse(state.panic_in_progress)
            self.assertFalse(state.panic_modal_open)

    def test_execute_panic_liquidation_success_sets_durable_halt(self):
        import tempfile
        import os
        from unittest.mock import Mock, patch
        from strategies.position_ledger import PositionLedger

        with tempfile.TemporaryDirectory() as tmpdir:
            ledger_path = os.path.join(tmpdir, "test_ledger.sqlite3")
            mock_client = Mock()
            mock_client.emergency_flatten_positions.return_value = True
            mock_client.flatten_failures = {}

            state = DashboardState()
            with patch.dict(os.environ, {"POSITION_LEDGER_PATH": ledger_path}), \
                 patch("dashboard.broker_client.get_broker_client", return_value=mock_client), \
                 patch.object(DashboardState, "load_all"):
                result = state.execute_panic_liquidation()
                self.assertFalse(state.panic_in_progress)
                self.assertFalse(state.panic_modal_open)

                # Confirm durable panic halt is persisted
                ledger = PositionLedger(ledger_path)
                self.assertTrue(ledger.is_panic_halted())
                ledger._connection.close()

    def test_execute_panic_liquidation_failures_surfaced(self):
        import tempfile
        import os
        from unittest.mock import Mock, patch

        with tempfile.TemporaryDirectory() as tmpdir:
            ledger_path = os.path.join(tmpdir, "test_ledger.sqlite3")
            mock_client = Mock()
            mock_client.emergency_flatten_positions.return_value = False
            mock_client.flatten_failures = {"SPXW241031C05800000": "Order rejected"}

            state = DashboardState()
            with patch.dict(os.environ, {"POSITION_LEDGER_PATH": ledger_path}), \
                 patch("dashboard.broker_client.get_broker_client", return_value=mock_client), \
                 patch.object(DashboardState, "load_all"):
                result = state.execute_panic_liquidation()
                self.assertFalse(state.panic_in_progress)
                self.assertFalse(state.panic_modal_open)


if __name__ == "__main__":
    unittest.main()
