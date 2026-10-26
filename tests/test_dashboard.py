"""Offline tests for dashboard telemetry, attribution, and benchmark math."""
from datetime import date
import importlib.util
import json
import os
from pathlib import Path
import tempfile
from types import ModuleType
import unittest
from unittest.mock import Mock, patch

from dashboard import app
from dashboard import trade_logger


def _load_broker_client() -> ModuleType:
    dotenv = ModuleType("dotenv")
    dotenv.load_dotenv = Mock()
    requests_mod = ModuleType("requests")
    spec = importlib.util.spec_from_file_location(
        "dashboard_broker_client_under_test",
        Path(__file__).resolve().parents[1] / "dashboard" / "broker_client.py",
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    with patch.dict("sys.modules", {"dotenv": dotenv, "requests": requests_mod}):
        spec.loader.exec_module(module)
    return module


class DashboardSettingsTests(unittest.TestCase):
    def test_tradier_aliases_and_explicit_live_selection(self) -> None:
        for credentials in (
            {"TRADIER_ACCESS_TOKEN": "test-token", "TRADIER_ACCOUNT_NUMBER": "VA-TEST"},
            {"TRADIER_API_KEY": "test-token", "TRADIER_ACCOUNT": "VA-TEST"},
        ):
            with self.subTest(credentials=tuple(credentials)):
                settings = app.broker_settings({
                    "TRADING_BROKER": "tradier", **credentials,
                    "TRADIER_IS_PAPER": "false", "TRADIER_SANDBOX": "1",
                })
                self.assertFalse(settings.paper)
                self.assertEqual(settings.base_url, "https://api.tradier.com/v1")
                self.assertNotIn("test-token", repr(settings))

        canonical_live = app.broker_settings({
            "TRADING_BROKER": "tradier",
            "TRADIER_ACCESS_TOKEN": "test-token",
            "TRADIER_ACCOUNT_NUMBER": "VA-TEST",
            "TRADIER_PAPER": "false",
            "TRADIER_IS_PAPER": "true",
            "TRADIER_SANDBOX": "1",
        })
        self.assertFalse(canonical_live.paper)

        blank_canonical_falls_back = app.broker_settings({
            "TRADING_BROKER": "tradier",
            "TRADIER_ACCESS_TOKEN": "test-token",
            "TRADIER_ACCOUNT_NUMBER": "VA-TEST",
            "TRADIER_PAPER": "",
            "TRADIER_IS_PAPER": "true",
        })
        self.assertTrue(blank_canonical_falls_back.paper)

    def test_broker_client_matches_canonical_tradier_paper_precedence(self) -> None:
        broker_client = _load_broker_client()
        with patch.dict(os.environ, {
            "TRADING_BROKER": "tradier",
            "TRADIER_ACCESS_TOKEN": "test-token",
            "TRADIER_ACCOUNT_NUMBER": "VA-TEST",
            "TRADIER_PAPER": "false",
            "TRADIER_IS_PAPER": "true",
            "TRADIER_SANDBOX": "1",
        }, clear=True):
            client = broker_client.get_broker_client()
        self.assertIsNotNone(client)
        self.assertFalse(client.sandbox)

        with patch.dict(os.environ, {
            "TRADING_BROKER": "tradier",
            "TRADIER_ACCESS_TOKEN": "test-token",
            "TRADIER_ACCOUNT_NUMBER": "VA-TEST",
            "TRADIER_PAPER": "",
            "TRADIER_IS_PAPER": "true",
        }, clear=True):
            client = broker_client.get_broker_client()
        self.assertTrue(client.sandbox)

    def test_invalid_or_missing_settings_do_not_create_mock_account(self) -> None:
        for environment in ({}, {"TRADING_BROKER": "unknown"}, {"TRADING_BROKER": "tradier"}, {
            "TRADIER_API_KEY": "test-token", "TRADIER_ACCOUNT": "VA-TEST", "TRADIER_IS_PAPER": "invalid",
        }):
            with self.subTest(environment=tuple(environment)), self.assertRaises(ValueError):
                app.broker_settings(environment)

    def test_explicit_alpaca_wins_over_tradier_credentials(self) -> None:
        settings = app.broker_settings({
            "TRADING_BROKER": "alpaca", "ALPACA_API_KEY": "test-key", "ALPACA_API_SECRET": "test-secret",
            "TRADIER_API_KEY": "tradier-token", "TRADIER_ACCOUNT": "VA-TEST",
        })
        self.assertEqual(settings.name, "alpaca")
        self.assertNotIn("test-secret", repr(settings))

    def test_blank_single_trade_override_matches_engine_tiers(self) -> None:
        for cap, expected in ((5000, .30), (10000, .20), (25000, .15), (50000, .10), (100000, .05)):
            self.assertEqual(app.single_trade_percentage(cap, ""), expected)
        self.assertEqual(app.single_trade_percentage(5000, "0.1"), .1)
        with self.assertRaises(ValueError):
            app.single_trade_percentage(5000, "nan")


class DashboardTelemetryTests(unittest.TestCase):
    settings = app.BrokerSettings("tradier", "test-token", "VA-TEST")

    def test_balances_support_cash_and_margin_without_invented_values(self) -> None:
        margin = app.account_balances({
            "total_equity": 10000, "total_cash": 3000, "account_type": "margin",
            "margin": {"option_buying_power": 2000}, "option_requirement": 1000,
        }, self.settings)
        self.assertEqual(margin["buying_power"], 2000)
        cash = app.account_balances({"account_type": "cash", "cash": {"cash_available": 123}}, self.settings)
        self.assertEqual(cash["buying_power"], 123)
        self.assertIsNone(cash["equity"])
        self.assertIsNone(app.account_balances({}, self.settings)["buying_power"])

    def test_broker_failure_does_not_look_like_flat_account(self) -> None:
        with patch.object(app, "fetch_json", side_effect=RuntimeError("unavailable")):
            snapshot = app.fetch_telemetry(self.settings, "2026-10-01", "2026-10-23")
        self.assertIsNone(snapshot["account"])
        self.assertIsNone(snapshot["positions"])
        self.assertIsNone(snapshot["orders"])
        self.assertEqual(snapshot["quotes"], {})
        self.assertEqual(len(snapshot["errors"]), 6)
        self.assertIsNone(app.open_exposure_value(snapshot["account"], snapshot["positions"]))

    def test_snapshot_joins_full_option_symbols_and_requests_spx_history(self) -> None:
        contract = "XSP261026P00750000"
        payloads = [
            {"balances": {"total_equity": 10000, "total_cash": 3000}},
            {"clock": {"state": "closed", "next_change": "09:30"}},
            {"positions": {"position": {"symbol": contract, "quantity": -1}}},
            {"orders": "null"},
            {"history": {"day": {"date": "2026-10-23", "close": 5800}}},
            {"quotes": {"quote": {"symbol": contract, "last": .5}}},
        ]
        with patch.object(app, "fetch_json", side_effect=payloads) as fetch:
            snapshot = app.fetch_telemetry(self.settings, "2026-10-01", "2026-10-23")
        self.assertEqual(snapshot["errors"], [])
        self.assertEqual(snapshot["orders"], [])
        self.assertIn(contract, snapshot["quotes"])
        self.assertIn(contract, fetch.call_args.args[2]["symbols"])
        self.assertEqual(fetch.call_args_list[4].args[2]["symbol"], "SPX")
        self.assertFalse(snapshot["clock"]["is_open"])

    def test_exposure_stays_unknown_without_position_read_or_requirement(self) -> None:
        self.assertIsNone(app.open_exposure_value({"option_requirement": 0}, None))
        self.assertIsNone(app.open_exposure_value({}, [{"quantity": -1}]))
        self.assertEqual(app.open_exposure_value({}, []), 0)
        self.assertEqual(app.open_exposure_value({"option_requirement": 1200}, [{"quantity": -1}]), 1200)

    def test_position_greeks_use_contract_quote_and_no_inferred_ownership(self) -> None:
        rows = app.position_records([{"symbol": "SPY261026C00500000", "quantity": -1}], {
            "SPY261026C00500000": {"bid": 1, "ask": 2, "greeks": {"delta": .25}},
        })
        self.assertEqual(rows[0]["Strategy"], "Unattributed")
        self.assertEqual(rows[0]["Market Mid"], 1.5)
        self.assertEqual(rows[0]["Delta (Δ)"], .25)
        self.assertIsNone(rows[0]["Theta (Θ)"])
        self.assertIsNone(rows[0]["Unrealized P&L"])


class BenchmarkTests(unittest.TestCase):
    history = [
        {"date": "2026-10-23", "close": 110},
        {"date": "2026-10-21", "close": 100},
        {"date": "2026-10-22", "close": 105},
    ]

    def test_returns_share_dates_and_capital_and_exclude_open_or_outside_trades(self) -> None:
        trades = [
            {"status": "CLOSED", "strategy": "Strategy A", "exit_time": "2026-10-22T16:00:00Z", "pnl": 200},
            {"status": "CLOSED", "strategy": "Strategy B", "exit_time": "2026-10-23T16:00:00Z", "pnl": -50},
            {"status": "CLOSED", "strategy": "SPXIronCondor", "exit_time": "2026-10-22T17:00:00Z", "pnl": 25},
            {"status": "OPEN", "strategy": "Strategy A", "exit_time": "OPEN", "pnl": 9999},
            {"status": "CLOSED", "strategy": "Strategy A", "exit_time": "2026-10-20T16:00:00Z", "pnl": 9999},
        ]
        rows = app.benchmark_comparison(self.history, trades, 10000)
        self.assertEqual(rows[0]["Date"], date(2026, 10, 21))
        self.assertEqual(rows[0]["Journal"], 0)
        self.assertAlmostEqual(rows[-1]["S&P 500 (SPX)"], 10)
        self.assertAlmostEqual(rows[-1]["Journal"], 1.75)
        self.assertEqual(rows[-1]["Strategy A"], 2)
        self.assertEqual(rows[-1]["Strategy B"], -.5)

    def test_missing_history_or_trades_do_not_manufacture_returns(self) -> None:
        self.assertEqual(app.benchmark_comparison([], [], 10000), [])
        rows = app.benchmark_comparison(self.history, [], 10000)
        self.assertNotIn("Journal", rows[-1])
        self.assertNotIn("Strategy A", rows[-1])
        with self.assertRaises(ValueError):
            app.benchmark_comparison(self.history, [], 0)

    def test_timestamp_alignment_uses_new_york_and_exact_strategy_tags(self) -> None:
        trade = {"status": "CLOSED", "exit_time": "2026-10-23T01:00:00Z"}
        self.assertEqual(app.trade_exit_date(trade), date(2026, 10, 22))
        self.assertEqual(app.strategy_tag("Strategy A (Baseline)"), "Strategy A")
        self.assertIsNone(app.strategy_tag("Strategy Alpha"))
        self.assertIsNone(app.strategy_tag("SPY261026C00500000"))
        self.assertIsNone(app.strategy_tag("SPXIronCondor"))


class TradeJournalTests(unittest.TestCase):
    def test_trim_and_final_exit_aggregate_cash_and_fees_once(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "trades.jsonl"
            records = [
                {
                    "event": "ENTRY", "trade_id": "t1", "timestamp": "2026-10-26T10:00:00",
                    "strategy": "IC", "symbol": "XSP", "quantity": 2, "credit": 0.8,
                    "opening_credit_cash": 160.0, "entry_fees": 3.2, "max_risk": 840.0,
                },
                {
                    "event": "ADJUSTMENT", "trade_id": "t1", "timestamp": "2026-10-26T11:00:00",
                    "gross_pnl": 40.0, "fees": 1.6, "net_pnl": 38.4,
                },
                {
                    "event": "EXIT", "trade_id": "t1", "timestamp": "2026-10-26T12:00:00",
                    "gross_pnl": 30.0, "fees": 1.6, "net_pnl": 28.4,
                    "exit_reason": "PROFIT_TARGET",
                },
            ]
            path.write_text("".join(json.dumps(row) + "\n" for row in records))
            [trade] = trade_logger.load_all_trades(path)
        self.assertEqual(trade["gross_pnl"], 70.0)
        self.assertEqual(trade["fees"], 6.4)
        self.assertEqual(trade["net_pnl"], 63.6)
        self.assertEqual(trade["credit_received_cash"], 160.0)


if __name__ == "__main__":
    unittest.main()
