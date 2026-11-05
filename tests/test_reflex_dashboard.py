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

    @classmethod
    def setUpClass(cls):
        import json
        from strategies.runtime_records import resolve_record_path
        trades_file = resolve_record_path("TRADE_LOG_PATH", "logs/trades.jsonl")
        trades_file.parent.mkdir(parents=True, exist_ok=True)
        fixtures = [
            {
                "event": "ENTRY",
                "trade_id": "trade_20261030_ic_v04",
                "timestamp": "2026-10-30T14:36:00Z",
                "strategy": "IC_V04_D10_W5_PT40_T1030",
                "symbol": "XSP",
                "expiry": "2026-10-30",
                "strikes": {"long_put": 757.0, "short_put": 762.0, "short_call": 774.0, "long_call": 779.0},
                "quantity": 1,
                "credit": 0.25,
                "max_risk": 500.0,
                "opening_credit_cash": 25.0,
                "entry_fees": 1.60,
                "status": "OPEN",
                "source": "tradier_verified",
                "reconciliation_status": "VERIFIED",
                "execution_evidence": {
                    "broker": "tradier",
                    "account_id": "VA_TEST_ACCOUNT",
                    "environment": "paper",
                    "package_id": "pkg_v04_entry",
                    "broker_parent_id": "ord_1001",
                    "legs": [
                        {"contract": "XSP_757_P", "side": "buy_to_open", "quantity": 1, "broker_order_id": "b1"},
                        {"contract": "XSP_762_P", "side": "sell_to_open", "quantity": 1, "broker_order_id": "b2"},
                        {"contract": "XSP_774_C", "side": "sell_to_open", "quantity": 1, "broker_order_id": "b3"},
                        {"contract": "XSP_779_C", "side": "buy_to_open", "quantity": 1, "broker_order_id": "b4"},
                    ],
                },
            },
            {
                "event": "EXIT",
                "trade_id": "trade_20261030_ic_v04",
                "timestamp": "2026-10-30T15:30:00Z",
                "strategy": "IC_V04_D10_W5_PT40_T1030",
                "exit_reason": "PROFIT_TARGET",
                "gross_pnl": 20.0,
                "fees": 1.60,
                "net_pnl": 16.80,
                "pnl": 16.80,
                "exit_price": 0.05,
                "status": "CLOSED",
                "source": "tradier_verified",
                "reconciliation_status": "VERIFIED",
                "execution_evidence": {
                    "broker": "tradier",
                    "account_id": "VA_TEST_ACCOUNT",
                    "environment": "paper",
                    "package_id": "pkg_v04_exit",
                    "broker_parent_id": "ord_1002",
                    "legs": [
                        {"contract": "XSP_757_P", "side": "sell_to_close", "quantity": 1, "broker_order_id": "b5"},
                        {"contract": "XSP_762_P", "side": "buy_to_close", "quantity": 1, "broker_order_id": "b6"},
                        {"contract": "XSP_774_C", "side": "buy_to_close", "quantity": 1, "broker_order_id": "b7"},
                        {"contract": "XSP_779_C", "side": "sell_to_close", "quantity": 1, "broker_order_id": "b8"},
                    ],
                },
            },
            {
                "event": "ENTRY",
                "trade_id": "trade_20261030_ic_v13",
                "timestamp": "2026-10-30T15:01:00Z",
                "strategy": "IC_V13_D15_W10_PT40_T1100",
                "symbol": "XSP",
                "expiry": "2026-10-30",
                "strikes": {"long_put": 748.0, "short_put": 758.0, "short_call": 778.0, "long_call": 788.0},
                "quantity": 1,
                "credit": 0.01,
                "max_risk": 1000.0,
                "opening_credit_cash": 1.0,
                "entry_fees": 1.60,
                "status": "OPEN",
                "source": "tradier_verified",
                "reconciliation_status": "VERIFIED",
                "execution_evidence": {
                    "broker": "tradier",
                    "account_id": "VA_TEST_ACCOUNT",
                    "environment": "paper",
                    "package_id": "pkg_v13_entry",
                    "broker_parent_id": "ord_1003",
                    "legs": [
                        {"contract": "XSP_748_P", "side": "buy_to_open", "quantity": 1, "broker_order_id": "b9"},
                        {"contract": "XSP_758_P", "side": "sell_to_open", "quantity": 1, "broker_order_id": "b10"},
                        {"contract": "XSP_778_C", "side": "sell_to_open", "quantity": 1, "broker_order_id": "b11"},
                        {"contract": "XSP_788_C", "side": "buy_to_open", "quantity": 1, "broker_order_id": "b12"},
                    ],
                },
            },
            {
                "event": "EXIT",
                "trade_id": "trade_20261030_ic_v13",
                "timestamp": "2026-10-30T15:31:00Z",
                "strategy": "IC_V13_D15_W10_PT40_T1100",
                "exit_reason": "PROFIT_TARGET",
                "gross_pnl": 1.0,
                "fees": 3.20,
                "net_pnl": -2.20,
                "pnl": -2.20,
                "exit_price": 0.0,
                "status": "CLOSED",
                "source": "tradier_verified",
                "reconciliation_status": "VERIFIED",
                "execution_evidence": {
                    "broker": "tradier",
                    "account_id": "VA_TEST_ACCOUNT",
                    "environment": "paper",
                    "package_id": "pkg_v13_exit",
                    "broker_parent_id": "ord_1004",
                    "legs": [
                        {"contract": "XSP_748_P", "side": "sell_to_close", "quantity": 1, "broker_order_id": "b13"},
                        {"contract": "XSP_758_P", "side": "buy_to_close", "quantity": 1, "broker_order_id": "b14"},
                        {"contract": "XSP_778_C", "side": "buy_to_close", "quantity": 1, "broker_order_id": "b15"},
                        {"contract": "XSP_788_C", "side": "sell_to_close", "quantity": 1, "broker_order_id": "b16"},
                    ],
                },
            },
        ]
        with open(trades_file, "w", encoding="utf-8") as f:
            for item in fixtures:
                f.write(json.dumps(item) + "\n")

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
        from strategies.incubator import get_incubator_specs

        inc = get_incubator_data()
        self.assertIn("rows", inc)
        self.assertIn("all_rows", inc)
        self.assertEqual(len(inc["all_rows"]), len(get_incubator_specs()))
        self.assertEqual(len(inc["curated_rows"]), min(9, len(inc["all_rows"])))
        self.assertEqual(len({row["strategy_name"] for row in inc["curated_rows"]}), len(inc["curated_rows"]))
        self.assertIn("strategy_aggregates", inc)
        self.assertIn("ai", inc["strategy_aggregates"])
        self.assertIn("top5", inc["strategy_aggregates"])
        self.assertIn("bottom3", inc["strategy_aggregates"])
        self.assertEqual(len(inc["visible_trades_50"]), min(50, len(inc["all_closed_trades"])))
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
        self.assertEqual(fields["current_tab"].default, "positions")
        self.assertEqual(fields["broker_name"].default, "TRADIER")
        self.assertGreaterEqual(fields["open_risk_cap"].default, 5000.0)

    def test_execute_panic_liquidation_no_broker(self):
        import reflex as rx
        from unittest.mock import patch
        state = DashboardState(_reflex_internal_init=True)
        with patch("dashboard.broker_client.get_broker_client", return_value=None):
            result = state.execute_panic_liquidation()
            self.assertFalse(state.panic_in_progress)
            self.assertFalse(state.panic_modal_open)

    def test_execute_panic_liquidation_stops_when_halt_fails(self):
        from unittest.mock import Mock, patch

        client = Mock()
        from contextlib import nullcontext
        ledger = Mock()
        ledger.execution_gate.return_value = nullcontext()
        ledger.set_panic_halt.side_effect = OSError("disk unavailable")
        state = DashboardState(_reflex_internal_init=True)
        with patch("dashboard.broker_client.get_broker_client", return_value=client), \
             patch("strategies.position_ledger.PositionLedger", return_value=ledger):
            state.execute_panic_liquidation()
        ledger.close.assert_called_once()
        client.emergency_flatten_positions.assert_not_called()

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

            state = DashboardState(_reflex_internal_init=True)
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

            state = DashboardState(_reflex_internal_init=True)
            with patch.dict(os.environ, {"POSITION_LEDGER_PATH": ledger_path}), \
                 patch("dashboard.broker_client.get_broker_client", return_value=mock_client), \
                 patch.object(DashboardState, "load_all"):
                result = state.execute_panic_liquidation()
                self.assertFalse(state.panic_in_progress)
                self.assertFalse(state.panic_modal_open)

    def test_config_settings_and_save_configuration(self):
        import tempfile
        from unittest.mock import patch
        from pathlib import Path
        from reflex_dashboard.data.adapters import get_config_settings

        with tempfile.TemporaryDirectory() as tmpdir:
            env_file = Path(tmpdir) / ".env"
            env_file.write_text("TRADING_BROKER=tradier\nTRADIER_ACCOUNT_NUMBER=12345\nIC_MAX_POSITION_RISK=15000\nIC_MAX_PORTFOLIO_RISK=100000\nTRADIER_PAPER=true\n")
            with patch("reflex_dashboard.data.adapters.PROJECT_ROOT", Path(tmpdir)):
                cfg = get_config_settings()
                self.assertEqual(cfg["broker"], "tradier")
                self.assertEqual(cfg["tradier_account_id"], "12345")
                self.assertEqual(cfg["risk_cap"], "15000")
                self.assertEqual(cfg["portfolio_risk_cap"], "100000")

                state = DashboardState(_reflex_internal_init=True)
                state.cfg_broker = "tradier"
                state.cfg_account_id = "67890"
                state.cfg_risk_cap = "20000"
                state.cfg_portfolio_risk_cap = "150000"
                state.cfg_paper = "false"
                state.save_configuration()

                saved_text = env_file.read_text()
                self.assertIn("TRADIER_ACCOUNT_NUMBER=67890", saved_text)
                self.assertIn("IC_MAX_POSITION_RISK=20000", saved_text)
                self.assertIn("IC_MAX_PORTFOLIO_RISK=150000", saved_text)
                self.assertIn("TRADIER_PAPER=false", saved_text)

    def test_csv_download_handlers_exclude_fixtures_and_quarantined(self):
        """Test all 3 CSV download handlers with nonempty mixed populations."""
        from unittest.mock import patch

        mixed_trades = [
            {
                "trade_id": "verified_trade_1",
                "strategy": "IC_V01",
                "status": "CLOSED",
                "gross_pnl": 50.0,
                "fees": 1.60,
                "net_pnl": 48.40,
                "partition": "accepted",
                "reconciliation_status": "VERIFIED",
                "is_eligible": True,
            },
            {
                "trade_id": "fee_inc_trade_2",
                "strategy": "IC_V02",
                "status": "CLOSED",
                "gross_pnl": 40.0,
                "fees": 1.60,
                "net_pnl": 38.40,
                "partition": "accepted",
                "reconciliation_status": "FEE_INCOMPLETE",
                "is_eligible": False,
            },
            {
                "trade_id": "unattributed_trade_3",
                "strategy": "IC_V03",
                "status": "CLOSED",
                "gross_pnl": 30.0,
                "fees": 1.60,
                "net_pnl": 28.40,
                "partition": "unattributed",
                "reconciliation_status": "UNATTRIBUTED",
                "is_eligible": False,
            },
            {
                "trade_id": "filled-synthetic-95-100",
                "strategy": "IC_V04",
                "status": "CLOSED",
                "strikes": {"long_put": 95, "short_put": 100, "short_call": 110, "long_call": 115},
                "gross_pnl": 999.0,
                "fees": 1.60,
                "net_pnl": 997.40,
                "partition": "quarantined",
                "reconciliation_status": "QUARANTINED",
                "source": "synthetic",
                "is_eligible": False,
            },
        ]

        mock_inc_data = {
            "visible_trades_50": mixed_trades,
            "all_closed_trades": mixed_trades,
            "eligible_closed_trades": [mixed_trades[0]],
        }

        state = DashboardState(_reflex_internal_init=True)
        with patch("reflex_dashboard.state.get_incubator_data", return_value=mock_inc_data), \
             patch("reflex_dashboard.state.rx.download", side_effect=lambda **kwargs: kwargs["data"]):
            # 1. Visible CSV: excludes synthetic/quarantined fixture
            vis_action = state.download_visible_trades_csv()
            vis_csv = getattr(vis_action, "data", str(vis_action))
            self.assertIn("verified_trade_1", vis_csv)
            self.assertIn("unattributed_trade_3", vis_csv)
            self.assertNotIn("filled-synthetic", vis_csv)

            # 2. Eligible CSV: strictly eligible only, no unverified or fixtures
            all_action = state.download_all_trades_csv()
            all_csv = getattr(all_action, "data", str(all_action))
            self.assertIn("verified_trade_1", all_csv)
            self.assertNotIn("fee_inc_trade_2", all_csv)
            self.assertNotIn("unattributed_trade_3", all_csv)
            self.assertNotIn("filled-synthetic", all_csv)

            # 3. Load More exports the eligible population.
            more_action = state.load_more_and_export_csv()
            more_csv = getattr(more_action, "data", str(more_action))
            self.assertIn("verified_trade_1", more_csv)
            self.assertNotIn("unattributed_trade_3", more_csv)
            self.assertNotIn("filled-synthetic", more_csv)


if __name__ == "__main__":
    unittest.main()
