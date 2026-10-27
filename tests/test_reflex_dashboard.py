"""Unit tests for Reflex Dashboard data adapters and reactive state."""

import unittest
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "reflex_dashboard"))

from reflex_dashboard.data.adapters import (
    get_market_clock,
    get_bot_status_from_log,
    get_incubator_data,
    get_neural_policy_data,
    get_recent_logs,
    get_config_settings,
    number,
    money,
)
from reflex_dashboard.state import DashboardState


class TestReflexDashboard(unittest.TestCase):

    def test_helpers(self):
        self.assertEqual(number("123.45"), 123.45)
        self.assertIsNone(number("invalid"))
        self.assertEqual(money(99762.40), "$99,762.40")

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
        self.assertEqual(len(inc["visible_trades_50"]), 50)
        self.assertGreaterEqual(len(inc["all_closed_trades"]), 50)
        self.assertIn("total_net_pnl_str", inc)
        self.assertIn("active_grid_str", inc)

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


if __name__ == "__main__":
    unittest.main()
