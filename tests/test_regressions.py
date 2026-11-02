"""Offline regressions for broker selection and iron condor position safety."""

import importlib.util
import json
import logging
import os
import tempfile
import uuid
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any
import unittest
from unittest.mock import Mock, patch


ROOT = Path(__file__).resolve().parents[1]


def load_module(name: str, path: str) -> ModuleType:
    """Load application code with only external broker boundaries replaced."""
    brokers = ModuleType("lumibot.brokers")
    brokers.Tradier = Mock()
    brokers.Alpaca = Mock()
    traders = ModuleType("lumibot.traders")
    traders.Trader = Mock()
    strategy = ModuleType("lumibot.strategies.strategy")
    strategy.Strategy = type("Strategy", (), {"name": "SPXIronCondor"})
    entities = ModuleType("lumibot.entities")
    entities.Asset = Mock()
    entities.Asset.AssetType.OPTION = "option"
    dotenv = ModuleType("dotenv")
    dotenv.load_dotenv = Mock()
    modules = {
        "lumibot.brokers": brokers,
        "lumibot.traders": traders,
        "lumibot.strategies.strategy": strategy,
        "lumibot.entities": entities,
        "dotenv": dotenv,
    }
    spec = importlib.util.spec_from_file_location(name, ROOT / path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    with (
        patch.dict(os.environ, {}, clear=True),
        patch.dict("sys.modules", modules),
        patch("os.makedirs"),
        patch("logging.handlers.RotatingFileHandler"),
        patch.object(logging.getLogger(), "addHandler"),
        patch.object(logging.getLogger(), "setLevel"),
    ):
        spec.loader.exec_module(module)
    module._test_brokers = brokers
    return module


entrypoint = load_module("trading_entrypoint", "main.py")
iron_condor = load_module("iron_condor_under_test", "strategies/iron_condor.py")
market_data_hub = load_module("market_data_hub_under_test", "strategies/market_data_hub.py")
incubator = load_module("incubator_under_test", "strategies/incubator.py")


class BrokerSelectionTests(unittest.TestCase):
    def get_broker(self, env: dict[str, str]) -> object:
        brokers = entrypoint._test_brokers
        brokers.Tradier.reset_mock()
        brokers.Alpaca.reset_mock()
        with (
            patch.dict(os.environ, env, clear=True),
            patch.dict("sys.modules", {"lumibot.brokers": brokers}),
        ):
            return entrypoint.get_broker()

    def test_tradier_sandbox_supports_both_environment_names(self) -> None:
        for credentials in (
            {"TRADIER_ACCESS_TOKEN": "test-token", "TRADIER_ACCOUNT_NUMBER": "VA21359019"},
            {"TRADIER_API_KEY": "test-token", "TRADIER_ACCOUNT": "VA21359019"},
        ):
            with self.subTest(credentials=tuple(credentials)):
                self.get_broker({"TRADING_BROKER": "tradier", **credentials})
                entrypoint._test_brokers.Tradier.assert_called_once_with({
                    "ACCESS_TOKEN": "test-token", "ACCOUNT_NUMBER": "VA21359019", "PAPER": True,
                }, polling_interval=15.0)

    def test_explicit_alpaca_selection_wins_over_tradier_credentials(self) -> None:
        self.get_broker({
            "TRADING_BROKER": "alpaca", "TRADIER_API_KEY": "test-token",
            "ALPACA_API_KEY": "alpaca-key", "ALPACA_API_SECRET": "alpaca-secret",
        })
        entrypoint._test_brokers.Tradier.assert_not_called()
        entrypoint._test_brokers.Alpaca.assert_called_once()

    def test_explicit_tradier_requires_credentials(self) -> None:
        for credentials in (
            {},
            {"TRADIER_API_KEY": "your_tradier_api_key_here"},
            {"TRADIER_ACCESS_TOKEN": "", "TRADIER_ACCOUNT_NUMBER": "test-account"},
            {"TRADIER_ACCESS_TOKEN": "your_token", "TRADIER_ACCOUNT_NUMBER": "test-account"},
            {"TRADIER_ACCESS_TOKEN": "test-token", "TRADIER_ACCOUNT_NUMBER": "your_account"},
            {"TRADIER_ACCESS_TOKEN": "test-token", "TRADIER_ACCOUNT_NUMBER": " "},
        ):
            with self.subTest(credentials=credentials), self.assertRaises(RuntimeError):
                self.get_broker({"TRADING_BROKER": "tradier", **credentials})
            entrypoint._test_brokers.Tradier.assert_not_called()

    def test_canonical_sandbox_settings_and_explicit_selection_take_precedence(self) -> None:
        self.get_broker({
            "TRADING_BROKER": "tradier",
            "TRADIER_ACCESS_TOKEN": "canonical-token", "TRADIER_ACCOUNT_NUMBER": "canonical-account",
            "TRADIER_API_KEY": "alias-token", "TRADIER_ACCOUNT": "alias-account",
            "TRADIER_PAPER": "true", "TRADIER_IS_PAPER": "false", "TRADIER_SANDBOX": "0",
            "ALPACA_API_KEY": "alpaca-key", "ALPACA_API_SECRET": "alpaca-secret",
        })
        entrypoint._test_brokers.Tradier.assert_called_once_with({
            "ACCESS_TOKEN": "canonical-token", "ACCOUNT_NUMBER": "canonical-account", "PAPER": True,
        }, polling_interval=15.0)
        entrypoint._test_brokers.Alpaca.assert_not_called()

    def test_invalid_paper_settings_fail_before_broker_construction(self) -> None:
        for name in ("TRADIER_PAPER", "TRADIER_IS_PAPER", "TRADIER_SANDBOX"):
            with self.subTest(name=name), self.assertRaises(entrypoint.ConfigurationError):
                self.get_broker({
                    "TRADING_BROKER": "tradier", "TRADIER_ACCESS_TOKEN": "test-token",
                    "TRADIER_ACCOUNT_NUMBER": "test-account", name: "maybe",
                })
            entrypoint._test_brokers.Tradier.assert_not_called()

    def test_connection_log_excludes_credentials(self) -> None:
        with self.assertLogs(entrypoint.logger, level="INFO") as captured:
            self.get_broker({
                "TRADING_BROKER": "tradier", "TRADIER_ACCESS_TOKEN": "private-test-token",
                "TRADIER_ACCOUNT_NUMBER": "private-test-account", "TRADIER_PAPER": "true",
            })
        messages = "\n".join(captured.output)
        self.assertNotIn("private-test-token", messages)
        self.assertNotIn("private-test-account", messages)
        self.assertIn("paper=True", messages)
        self.assertIn("polling_interval=15.0s", messages)

    def test_real_dotenv_loader_supplies_canonical_sandbox_configuration(self) -> None:
        from dotenv import load_dotenv

        brokers = entrypoint._test_brokers
        brokers.Tradier.reset_mock()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / ".env"
            path.write_text(
                "TRADING_BROKER=tradier\nTRADIER_ACCESS_TOKEN=dotenv-token\n"
                "TRADIER_ACCOUNT_NUMBER=dotenv-account\nTRADIER_PAPER=true\n",
                encoding="utf-8",
            )
            with (
                patch.dict(os.environ, {}, clear=True),
                patch.dict("sys.modules", {"lumibot.brokers": brokers}),
            ):
                self.assertTrue(load_dotenv(path))
                entrypoint.get_broker()
        brokers.Tradier.assert_called_once_with({
            "ACCESS_TOKEN": "dotenv-token", "ACCOUNT_NUMBER": "dotenv-account", "PAPER": True,
        }, polling_interval=15.0)

    def test_paper_setting_has_explicit_precedence(self) -> None:
        for settings in ({"TRADIER_IS_PAPER": "false"}, {"TRADIER_SANDBOX": "0"}):
            with self.subTest(settings=settings):
                self.get_broker({
                    "TRADING_BROKER": "tradier", "TRADIER_API_KEY": "test-token",
                    "TRADIER_ACCOUNT": "VA21359019", **settings,
                })
                self.assertFalse(entrypoint._test_brokers.Tradier.call_args.args[0]["PAPER"])

        self.get_broker({
            "TRADING_BROKER": "tradier",
            "TRADIER_API_KEY": "test-token",
            "TRADIER_ACCOUNT": "VA21359019",
            "TRADIER_PAPER": "false",
            "TRADIER_IS_PAPER": "true",
            "TRADIER_SANDBOX": "1",
        })
        self.assertFalse(entrypoint._test_brokers.Tradier.call_args.args[0]["PAPER"])

        self.get_broker({
            "TRADING_BROKER": "tradier", "TRADIER_API_KEY": "test-token",
            "TRADIER_ACCOUNT": "VA21359019", "TRADIER_PAPER": "",
            "TRADIER_IS_PAPER": "false", "TRADIER_SANDBOX": "1",
        })
        self.assertFalse(entrypoint._test_brokers.Tradier.call_args.args[0]["PAPER"])

    def test_runner_rejects_alpaca_before_broker_construction(self) -> None:
        with patch.dict(os.environ, {"TRADING_BROKER": "alpaca"}, clear=True):
            with self.assertRaisesRegex(entrypoint.ConfigurationError, "atomic"):
                entrypoint.validate_runtime_configuration()

    def test_tradier_data_source_supports_indexes_and_rejects_unwired_sources(self) -> None:
        with patch.dict(os.environ, {
            "TRADING_BROKER": "tradier", "TRADIER_PAPER": "true", "IC_SYMBOL": "XSP",
            "TRADIER_ACCESS_TOKEN": "token", "TRADIER_ACCOUNT_NUMBER": "VA-TEST",
            "DATA_SOURCE": "tradier",
        }, clear=True):
            entrypoint.validate_runtime_configuration()
        with patch.dict(os.environ, {
            "DATA_SOURCE": "polygon",
        }, clear=True), self.assertRaisesRegex(RuntimeError, "DATA_SOURCE"):
            entrypoint.validate_runtime_configuration()
        with patch.dict(os.environ, {
            "TRADING_BROKER": "tradier", "TRADIER_PAPER": "true",
        }, clear=True), self.assertRaisesRegex(RuntimeError, "TRADIER_ACCESS_TOKEN"):
            entrypoint.validate_runtime_configuration()
        with patch.dict(os.environ, {
            "TRADING_BROKER": "tradier", "TRADIER_PAPER": "true",
            "TRADIER_ACCESS_TOKEN": "your_token", "TRADIER_ACCOUNT_NUMBER": "VA-TEST",
        }, clear=True), self.assertRaisesRegex(RuntimeError, "TRADIER_ACCESS_TOKEN"):
            entrypoint.validate_runtime_configuration()
        with patch.dict(os.environ, {
            "TRADING_BROKER": "tradier", "TRADIER_PAPER": "maybe",
            "TRADIER_ACCESS_TOKEN": "token", "TRADIER_ACCOUNT_NUMBER": "VA-TEST",
        }, clear=True), self.assertRaises(RuntimeError):
            entrypoint.validate_runtime_configuration()
        with patch.dict(os.environ, {
            "TRADING_BROKER": "tradier", "TRADIER_PAPER": "true", "IC_SYMBOL": "SPY",
            "TRADIER_ACCESS_TOKEN": "token", "TRADIER_ACCOUNT_NUMBER": "VA-TEST",
            "ML_POLICY_ENABLED": "true", "ML_MODEL_PATH": "/nonexistent/policy.json",
        }, clear=True), self.assertRaises(FileNotFoundError):
            entrypoint.validate_runtime_configuration()

    def test_runtime_failure_reconstructs_once_and_shutdown_does_not_restart(self) -> None:
        with (
            patch.object(entrypoint, "validate_runtime_configuration"),
            patch.object(entrypoint, "_run_once", side_effect=[RuntimeError("boom"), None]) as run_once,
            patch.object(entrypoint.time, "sleep") as sleep,
            patch.dict(os.environ, {"PROCESS_RESTART_BACKOFF_SECONDS": "0.25"}, clear=True),
        ):
            entrypoint.run()
        self.assertEqual(run_once.call_count, 2)
        sleep.assert_called_once_with(0.25)

        with patch.object(entrypoint, "validate_runtime_configuration"), patch.object(entrypoint, "_run_once", side_effect=KeyboardInterrupt) as run_once:
            with self.assertRaises(KeyboardInterrupt):
                entrypoint.run()
        run_once.assert_called_once()

    def test_bad_configuration_exits_without_retrying(self) -> None:
        environments = (
            {"DATA_SOURCE": "unsupported"},
            {"TRADING_BROKER": "tradier"},
            {"ML_POLICY_ENABLED": "invalid"},
            {"ML_POLICY_ENABLED": "true", "ML_MODEL_PATH": "/nonexistent/policy.json"},
            {"PROCESS_RESTART_BACKOFF_SECONDS": "invalid"},
        )
        for environment in environments:
            with (
                self.subTest(environment=environment),
                patch.dict(os.environ, environment, clear=True),
                patch.object(entrypoint, "_run_once") as run_once,
                patch.object(entrypoint.time, "sleep") as sleep,
                self.assertRaises(SystemExit) as raised,
            ):
                entrypoint.run()
            self.assertEqual(raised.exception.code, 2)
            run_once.assert_not_called()
            sleep.assert_not_called()

    def test_broker_configuration_error_exits_without_retrying(self) -> None:
        with (
            patch.dict(os.environ, {}, clear=True),
            patch.object(entrypoint, "validate_runtime_configuration"),
            patch.object(entrypoint, "_run_once", side_effect=entrypoint.ConfigurationError("bad broker")) as run_once,
            patch.object(entrypoint.time, "sleep") as sleep,
            self.assertRaises(SystemExit) as raised,
        ):
            entrypoint.run()
        self.assertEqual(raised.exception.code, 2)
        run_once.assert_called_once()
        sleep.assert_not_called()

    def test_incubator_attaches_one_collector_to_all_twenty_strategies(self) -> None:
        variants = [SimpleNamespace(name=f"IC_V{i:02}") for i in range(1, 21)]
        modules = {
            "strategies.iron_condor": iron_condor,
            "strategies.incubator": SimpleNamespace(
                build_incubator_strategies=Mock(return_value=variants),
                build_ab_strategies=Mock(),
            ),
            "strategies.market_data_hub": market_data_hub,
        }
        with tempfile.TemporaryDirectory() as temporary, patch.dict("sys.modules", modules):
            with patch.dict(os.environ, {"TRAJECTORY_LOG_PATH": str(Path(temporary) / "grid.jsonl")}, clear=True):
                strategies = entrypoint.build_strategies_for_mode(Mock(), mode="incubator")
            self.assertEqual(len(strategies), 20)
            collector = strategies[0]._trajectory_logger
            self.assertEqual(len(collector.names), 20)
            self.assertTrue(all(strategy._trajectory_logger is collector for strategy in strategies))

    def test_single_mode_attaches_collector_with_default_strategy_name(self) -> None:
        modules = {
            "strategies.iron_condor": iron_condor,
            "strategies.incubator": incubator,
            "strategies.market_data_hub": market_data_hub,
        }
        with tempfile.TemporaryDirectory() as temporary, patch.dict("sys.modules", modules):
            with patch.dict(os.environ, {"TRAJECTORY_LOG_PATH": str(Path(temporary) / "single.jsonl")}, clear=True):
                strategies = entrypoint.build_strategies_for_mode(Mock(), mode="single")
            self.assertEqual(len(strategies), 1)
            self.assertEqual(strategies[0].name, "SPXIronCondor")
            self.assertEqual(strategies[0]._trajectory_logger.names, ("SPXIronCondor",))


class IronCondorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.environment = patch.dict(os.environ, {}, clear=True)
        self.environment.start()
        self.addCleanup(self.environment.stop)
        self.strategy = iron_condor.SPXIronCondor()
        self.strategy.parameters = dict(iron_condor.SPXIronCondor.parameters)
        self.strategy.broker = SimpleNamespace(name="Tradier")
        self.strategy.initialize()
        self.strategy.portfolio_value = 100_000.0
        self.strategy.get_datetime = Mock(return_value=datetime(2026, 10, 23, 14, tzinfo=timezone.utc))
        self.strategy.get_positions = Mock(return_value=[])
        self.strategy.get_orders = Mock(return_value=[])
        self.strategy.get_last_price = Mock(return_value=1.0)
        self.strategy.get_quote = Mock(return_value={"bid": 0.9, "ask": 1.1})
        self.strategy.create_order = Mock(side_effect=lambda asset, quantity, side: Mock(
            asset=asset, quantity=quantity, side=side, is_active=Mock(return_value=True),
        ))
        self.strategy.submit_order = Mock()
        self.strategy.cancel_order = Mock()
        self.strategy._position_open = False

    def open_filled_position(
        self,
        quantity: int = 1,
        prices: tuple[float, float, float, float] = (0.25, 1.25, 1.25, 0.25),
        expiration: str = "2026-10-26",
        limit_price: float | None = None,
    ) -> None:
        assets = [
            SimpleNamespace(
                asset_type="option", symbol="XSP", expiration=expiration,
                strike=strike, right=right,
            )
            for strike, right in ((95, "put"), (100, "put"), (110, "call"), (115, "call"))
        ]
        signed = (quantity, -quantity, -quantity, quantity)
        package_id = f"filled-{id(self)}-{quantity}-{uuid.uuid4().hex[:8]}"
        legs = []
        for index, (asset, signed_quantity) in enumerate(zip(assets, signed), start=1):
            key = iron_condor.asset_key(asset)
            self.strategy._asset_registry[key] = asset
            legs.append(iron_condor.LedgerLeg(f"{package_id}:{index}", key, signed_quantity))
        self.strategy._position_ledger.record_submission(
            package_id, self.strategy._strategy_id(), "open", legs, limit_price=limit_price
        )
        for leg, price in zip(legs, prices):
            self.strategy._position_ledger.record_fill(
                leg.order_id, leg.requested_signed_quantity, price
            )
        self.strategy._hydrate_from_ledger()
        self.strategy.get_positions.return_value = [
            SimpleNamespace(asset=asset, quantity=signed_quantity)
            for asset, signed_quantity in zip(assets, signed)
        ]
        quote_by_strike = dict(zip((95, 100, 110, 115), prices))
        self.strategy.get_quote.side_effect = lambda asset: {
            "bid": max(quote_by_strike[asset.strike] - 0.05, 0.0),
            "ask": quote_by_strike[asset.strike] + 0.05,
        }

    def test_zero_credit_does_not_access_missing_framework_counter(self) -> None:
        self.open_filled_position(prices=(1.0, 1.0, 1.0, 1.0))
        self.assertEqual(self.strategy._entry_credit, 0.0)
        with patch("time.sleep"):
            self.strategy.on_trading_iteration()
        self.assertTrue(self.strategy._position_open)
        self.assertEqual(self.strategy._iteration_count, 1)

    def test_iteration_counter_supports_framework_initialization_and_ticks(self) -> None:
        strategy = iron_condor.SPXIronCondor.__new__(iron_condor.SPXIronCondor)
        self.assertEqual(strategy.on_trading_iteration_count, 0)
        strategy.on_trading_iteration_count = None
        self.assertEqual(strategy.on_trading_iteration_count, 0)
        strategy.on_trading_iteration_count = 7
        strategy._position_open = False
        strategy._exit_requested = False
        strategy._run_trading_iteration = Mock()
        strategy._record_trajectory = Mock()
        strategy.on_trading_iteration()
        self.assertEqual(strategy.on_trading_iteration_count, 8)
        strategy.on_trading_iteration()
        self.assertEqual(strategy.on_trading_iteration_count, 9)

    def record_pending_close(self, age_seconds: float = 120.0) -> str:
        ledger = self.strategy._position_ledger
        legs = [
            iron_condor.LedgerLeg(f"pending-close-{index}", iron_condor.asset_key(asset), -quantity)
            for index, (asset, quantity) in enumerate(self.strategy._owned_legs)
        ]
        ledger.record_submission(
            "pending-close", self.strategy._strategy_id(), "close", legs,
            broker_tag="pending-close-tag", limit_price=2.0,
        )
        basket = ledger.get_basket("pending-close")
        created = datetime.fromisoformat(basket.created_at)
        self.strategy.get_datetime.return_value = created + timedelta(seconds=age_seconds)
        return basket.package_id

    def test_stale_active_close_cancels_with_naive_strategy_clock(self) -> None:
        self.open_filled_position()
        package_id = self.record_pending_close()
        parent = SimpleNamespace(identifier="broker-close", is_active=lambda: True)
        self.strategy._position_ledger.bind_broker_submission(package_id, parent)
        self.strategy.get_orders.return_value = [parent]
        self.strategy.get_datetime.return_value = self.strategy.get_datetime.return_value.replace(tzinfo=None)
        self.strategy._close_all_options()
        self.strategy.cancel_order.assert_called_once_with(parent)
        self.strategy.submit_order.assert_not_called()
        self.assertTrue(self.strategy._position_open)

    def test_missing_close_identity_retains_ownership_and_blocks_replacement(self) -> None:
        self.open_filled_position()
        owned = self.strategy._position_ledger.owned_quantities(self.strategy._strategy_id())
        package_id = self.record_pending_close()
        ledger = self.strategy._position_ledger
        for _ in range(2):
            self.strategy._close_all_options()
            self.assertEqual(ledger.get_basket(package_id).state, "submitted")
            self.assertTrue(self.strategy._recovery_lock)
            self.assertEqual(ledger.owned_quantities(self.strategy._strategy_id()), owned)
        self.strategy.submit_order.assert_not_called()
        self.assertTrue(self.strategy._position_open)
        self.assertTrue(self.strategy._exit_requested)

    def test_confirmed_close_cancellation_allows_replacement(self) -> None:
        self.open_filled_position()
        package_id = self.record_pending_close()
        ledger = self.strategy._position_ledger
        children = [SimpleNamespace(identifier=f"broker-close-{index}", status="canceled",
                                    asset=self.strategy._asset_registry[leg["asset_key"]],
                                    side=leg["side"], exec_quantity=0, avg_fill_price=0)
                    for index, leg in enumerate(ledger.get_basket(package_id).legs)]
        parent = SimpleNamespace(identifier="broker-close", status="canceled", child_orders=children)
        ledger.bind_broker_submission(package_id, parent)
        self.strategy._reconcile_inflight_orders([parent])
        self.assertEqual(ledger.get_basket(package_id).state, "canceled")
        self.strategy._close_all_options()
        self.strategy.submit_order.assert_called_once()
        replacement = ledger.latest_basket(self.strategy._strategy_id(), {"close"})
        self.assertEqual(replacement.replacement_of, package_id)
        self.assertEqual(replacement.reprice_count, 1)

    def test_terminal_close_without_execution_evidence_stays_inflight(self) -> None:
        self.open_filled_position()
        package_id = self.record_pending_close()
        ledger = self.strategy._position_ledger
        children = [SimpleNamespace(identifier=f"broker-close-{index}", status="canceled",
                                    asset=self.strategy._asset_registry[leg["asset_key"]],
                                    side=leg["side"], avg_fill_price=None)
                    for index, leg in enumerate(ledger.get_basket(package_id).legs)]
        parent = SimpleNamespace(identifier="broker-close", status="canceled", child_orders=children)
        ledger.bind_broker_submission(package_id, parent)
        self.assertTrue(self.strategy._reconcile_inflight_orders([parent]))
        self.assertTrue(ledger.is_inflight(ledger.get_basket(package_id)))

    def test_pending_close_is_retained_during_grace_period(self) -> None:
        self.open_filled_position()
        package_id = self.record_pending_close(age_seconds=59.0)
        self.strategy._close_all_options()
        self.assertEqual(self.strategy._position_ledger.get_basket(package_id).state, "submitted")
        self.strategy.submit_order.assert_not_called()

    def assert_close_is_not_replaced(self, evidence: str) -> None:
        self.open_filled_position()
        package_id = self.record_pending_close()
        ledger = self.strategy._position_ledger
        if evidence == "parent_id":
            ledger.bind_broker_submission(package_id, SimpleNamespace(identifier="broker-close"))
        elif evidence in {"tag", "child_tag"}:
            order = SimpleNamespace(custom_params={"tag": "pending-close-tag"})
            if evidence == "child_tag":
                order = SimpleNamespace(child_orders=[order])
            self.strategy.get_orders.return_value = [order]
        else:
            leg = ledger.get_basket(package_id).legs[0]
            ledger.record_fill(leg["order_id"], leg["requested_signed_quantity"], 0.25)
        self.strategy._close_all_options()
        self.assertIn(ledger.get_basket(package_id).state, {"submitted", "partial"})
        self.strategy.submit_order.assert_not_called()

    def test_broker_parent_id_prevents_replacing_unconfirmed_close(self) -> None:
        self.assert_close_is_not_replaced("parent_id")

    def test_broker_tag_prevents_replacing_unconfirmed_close(self) -> None:
        self.assert_close_is_not_replaced("tag")

    def test_nested_broker_tag_prevents_replacing_unconfirmed_close(self) -> None:
        self.assert_close_is_not_replaced("child_tag")

    def test_partial_fill_prevents_replacing_unconfirmed_close(self) -> None:
        self.assert_close_is_not_replaced("fill")

    def test_close_submission_exception_retains_intent_without_retrying(self) -> None:
        self.open_filled_position()
        owned_before = self.strategy._position_ledger.owned_quantities(self.strategy._strategy_id())
        self.strategy.submit_order.side_effect = RuntimeError("broker unavailable")
        self.strategy._close_all_options()
        failed = self.strategy._position_ledger.latest_basket(self.strategy._strategy_id(), {"close"})
        self.strategy.get_datetime.return_value = datetime.fromisoformat(failed.created_at) + timedelta(seconds=120)
        self.strategy.submit_order.side_effect = None
        self.strategy._close_all_options()
        self.assertEqual(self.strategy.submit_order.call_count, 1)
        self.assertEqual(self.strategy._position_ledger.get_basket(failed.package_id).state, "submitted")
        self.assertTrue(self.strategy._recovery_lock)
        self.assertEqual(self.strategy._position_ledger.owned_quantities(self.strategy._strategy_id()), owned_before)

    def test_zero_credit_contracts_submit_eod_close_with_zero_quotes(self) -> None:
        self.open_filled_position(prices=(1.0, 1.0, 1.0, 1.0))
        self.strategy.get_quote.side_effect = None
        self.strategy.get_quote.return_value = {"bid": 0.0, "ask": 0.0}
        self.strategy.before_market_closes()
        self.strategy.submit_order.assert_called_once()
        self.assertEqual(self.strategy.submit_order.call_args.kwargs["price"], 0.01)
        self.assertTrue(self.strategy._position_open)
        self.assertTrue(self.strategy._exit_requested)
        self.assertEqual(len(self.strategy._owned_legs), 4)

    def test_flat_iteration_and_market_close_both_publish_telemetry(self) -> None:
        collector = Mock()
        self.strategy._trajectory_logger = collector
        self.strategy._position_open = False
        self.strategy._is_entry_window = Mock(return_value=False)
        self.strategy.on_trading_iteration()
        collector.record.assert_called_once_with(self.strategy, "HOLD", session_end=False)
        self.strategy.after_market_closes()
        collector.record.assert_called_with(self.strategy, "HOLD", session_end=True)

    def test_telemetry_failure_preserves_position_management(self) -> None:
        self.open_filled_position()
        self.strategy._trajectory_logger = Mock()
        self.strategy._trajectory_logger.record.side_effect = OSError("disk full")
        self.strategy._manage_position = Mock()
        self.strategy.on_trading_iteration()
        self.strategy._manage_position.assert_called_once()
        self.assertTrue(self.strategy._position_open)

    def test_retry_exhaustion_keeps_contracts_managed_until_closed(self) -> None:
        self.open_filled_position(prices=(1.0, 1.0, 1.0, 1.0))
        ledger = self.strategy._position_ledger
        owned = ledger.owned_quantities(self.strategy._strategy_id())
        self.assertEqual(self.strategy._entry_credit, 0.0)
        with patch("time.sleep"):
            for _ in range(7):
                self.strategy.on_trading_iteration()
        self.assertTrue(self.strategy._position_open)
        self.strategy.submit_order.assert_not_called()
        self.strategy.before_market_closes()
        self.strategy.submit_order.assert_called_once()
        self.assertEqual(ledger.owned_quantities(self.strategy._strategy_id()), owned)
        for order in self.strategy._closing_orders:
            order.is_active.return_value = False
            self.strategy.on_filled_order(None, order, 1.0, order.quantity, 100)
        self.strategy.get_positions.return_value = []
        self.strategy.on_trading_iteration()
        self.assertFalse(self.strategy._position_open)
        self.assertEqual(ledger.owned_quantities(self.strategy._strategy_id()), {})

    def test_close_submission_keeps_position_active_and_avoids_duplicates(self) -> None:
        self.open_filled_position()
        self.strategy._close_all_options()
        self.assertTrue(self.strategy._position_open)
        self.strategy._close_all_options()
        self.strategy.submit_order.assert_called_once()

    def test_missing_quote_cannot_trigger_a_false_profit_target(self) -> None:
        self.open_filled_position()
        ledger = self.strategy._position_ledger
        owned = ledger.owned_quantities(self.strategy._strategy_id())
        self.strategy.get_quote.side_effect = None
        self.strategy.get_quote.return_value = None
        self.strategy._manage_position()
        self.strategy.submit_order.assert_not_called()
        self.assertTrue(self.strategy._position_open)
        self.assertEqual(ledger.owned_quantities(self.strategy._strategy_id()), owned)

    def test_missing_credit_is_not_replaced_with_an_estimate(self) -> None:
        self.open_filled_position(limit_price=2.0)
        self.strategy._entry_credit = 0.0
        self.strategy._entry_credit_cash = 0.0
        self.strategy._estimate_credit = Mock(return_value=0.5)
        self.strategy._publish_snapshot = Mock()
        self.strategy._manage_position()
        self.assertEqual(self.strategy._entry_credit, 0.0)
        self.assertEqual(self.strategy._entry_credit_cash, 0.0)
        self.strategy._estimate_credit.assert_not_called()
        self.strategy._publish_snapshot.assert_called_once_with(pnl=0.0, status="VALUATION_UNAVAILABLE")
        self.strategy.submit_order.assert_not_called()
        self.assertTrue(self.strategy._position_open)

    def test_unavailable_credit_ticks_preserve_ownership_and_prevent_reentry(self) -> None:
        self.open_filled_position(prices=(1.0, 1.0, 1.0, 1.0), limit_price=2.0)
        ledger = self.strategy._position_ledger
        owned = ledger.owned_quantities(self.strategy._strategy_id())
        baskets = ledger.open_baskets(self.strategy._strategy_id())
        for basket in baskets:
            ledger.mark_journaled(basket.package_id, "ENTRY")
        legs = list(self.strategy._owned_legs)
        self.strategy._publish_snapshot = Mock()
        for _ in range(3):
            self.strategy.on_trading_iteration()
            self.assertEqual(ledger.owned_quantities(self.strategy._strategy_id()), owned)
            self.assertEqual(ledger.open_baskets(self.strategy._strategy_id()), baskets)
            self.assertEqual(self.strategy._owned_legs, legs)
            self.assertTrue(self.strategy._position_open)
            self.assertEqual(self.strategy._entry_credit_cash, 0.0)
            self.strategy._publish_snapshot.assert_called_with(pnl=0.0, status="VALUATION_UNAVAILABLE")
        self.strategy.submit_order.assert_not_called()

    def test_invalid_confirmed_credit_skips_valuation_and_mfe(self) -> None:
        self.open_filled_position(limit_price=2.0)
        self.strategy._publish_snapshot = Mock()
        self.strategy._mfe_pnl = 12.0
        self.strategy._mae_pnl = -15.0
        self.strategy._last_pnl = 5.0
        self.strategy._fetch_quote = Mock()
        for credit in (None, 0.0, -1.0, float("nan"), float("inf"), -float("inf")):
            with self.subTest(credit=credit):
                self.strategy._entry_credit_cash = credit
                self.strategy._entry_credit = 0.0
                self.strategy._manage_position()
                self.strategy._publish_snapshot.assert_called_with(pnl=0.0, status="VALUATION_UNAVAILABLE")
                self.assertEqual(self.strategy._entry_credit, 0.0)
                self.assertEqual(self.strategy._mfe_pnl, 12.0)
                self.assertEqual(self.strategy._mae_pnl, -15.0)
                self.assertEqual(self.strategy._last_pnl, 5.0)
        self.strategy._fetch_quote.assert_not_called()
        self.strategy.submit_order.assert_not_called()

    def test_ledger_hydration_restores_confirmed_credit_and_normal_valuation(self) -> None:
        self.open_filled_position(limit_price=3.0)
        self.strategy._entry_credit = self.strategy._entry_credit_cash = 0.0
        self.strategy._publish_snapshot = Mock()
        self.strategy._manage_position()
        self.strategy._publish_snapshot.assert_called_with(pnl=0.0, status="VALUATION_UNAVAILABLE")
        self.strategy.on_trading_iteration()
        self.assertEqual(self.strategy._entry_credit_cash, 200.0)
        self.assertEqual(self.strategy._entry_credit, 2.0)
        self.assertEqual(self.strategy._publish_snapshot.call_args.kwargs["status"], "OPEN")

    def test_roll_exit_is_reachable_without_credit_or_quotes_and_retries(self) -> None:
        self.open_filled_position(prices=(1.0, 1.0, 1.0, 1.0), limit_price=2.0)
        self.strategy.parameters["roll_dte"] = 3
        self.strategy.get_quote.side_effect = None
        self.strategy.get_quote.return_value = None
        owned = self.strategy._position_ledger.owned_quantities(self.strategy._strategy_id())
        self.strategy._manage_position()
        self.assertTrue(self.strategy._exit_requested)
        self.assertEqual(self.strategy._pending_exit_reason, "ROLL_DTE")
        self.strategy.submit_order.assert_not_called()
        self.assertEqual(self.strategy._position_ledger.owned_quantities(self.strategy._strategy_id()), owned)
        self.strategy.get_quote.return_value = {"bid": 0.9, "ask": 1.1}
        self.strategy._manage_position()
        self.strategy.submit_order.assert_called_once()
        self.assertTrue(self.strategy._position_open)
        self.assertEqual(self.strategy._position_ledger.owned_quantities(self.strategy._strategy_id()), owned)

    def test_unavailable_broker_responses_preserve_owned_contracts(self) -> None:
        self.open_filled_position()
        owned = self.strategy._position_ledger.owned_quantities(self.strategy._strategy_id())
        for method in (self.strategy.get_positions, self.strategy.get_orders):
            with self.subTest(method=method):
                previous = method.return_value
                method.return_value = None
                self.strategy._manage_position()
                self.assertTrue(self.strategy._position_open)
                self.assertEqual(self.strategy._position_ledger.owned_quantities(self.strategy._strategy_id()), owned)
                method.return_value = previous
        self.strategy.submit_order.assert_not_called()

    def test_pending_entry_order_prevents_reset_when_no_positions_visible(self) -> None:
        self.strategy._position_open = True
        self.strategy.get_positions.return_value = []
        self.strategy.get_orders.return_value = [Mock(is_active=Mock(return_value=True))]
        self.strategy._entry_credit = 1.0
        self.strategy._manage_position()
        self.assertTrue(self.strategy._position_open)

    def test_exit_cancels_pending_entry_even_before_positions_appear(self) -> None:
        self.strategy._position_open = True
        order = Mock(is_active=Mock(return_value=True))
        self.strategy._entry_orders = [order]
        self.strategy._exit_requested = True
        self.strategy.get_positions.return_value = []
        self.strategy._manage_position()
        self.strategy.cancel_order.assert_called_once_with(order)
        self.assertTrue(self.strategy._position_open)

    def test_broker_read_failure_keeps_position_managed(self) -> None:
        self.strategy._position_open = True
        self.strategy.get_positions.side_effect = RuntimeError("broker unavailable")
        self.strategy._manage_position()
        self.assertTrue(self.strategy._position_open)
        self.strategy.submit_order.assert_not_called()

    def test_failed_close_keeps_position_managed_for_safety_hooks(self) -> None:
        self.open_filled_position()
        self.strategy.submit_order.side_effect = RuntimeError("broker unavailable")
        self.strategy._close_all_options()
        self.assertTrue(self.strategy._position_open)
        self.assertTrue(self.strategy._exit_requested)

    def test_atomic_entry_failure_keeps_submitted_package_managed(self) -> None:
        self.strategy._position_open = False
        self.strategy._is_entry_window = Mock(return_value=True)
        self.strategy._check_allocation = Mock(return_value=True)
        self.strategy._find_expiration = Mock(return_value="2026-10-23")
        self.strategy._find_short_strikes = Mock(return_value=(580, 600))
        self.strategy._make_option = Mock(side_effect=lambda strike, expiry, right: SimpleNamespace(
            asset_type="option", symbol="XSP", expiration=expiry, strike=strike, right=right,
        ))
        self.strategy.get_quote.side_effect = lambda asset: (
            {"bid": 1.20, "ask": 1.30}
            if asset.strike in (580, 600)
            else {"bid": 0.20, "ask": 0.30}
        )
        self.strategy.submit_order.side_effect = RuntimeError("submission failed")
        self.strategy.on_trading_iteration()
        self.assertTrue(self.strategy._position_open)
        self.assertEqual(self.strategy._position_ledger.owned_quantities(self.strategy._strategy_id()), {})
        self.assertEqual(len(self.strategy._position_ledger.open_baskets(self.strategy._strategy_id())), 1)
        submitted = self.strategy.submit_order.call_args.args[0]
        self.assertEqual(len(submitted), 4)
        self.assertTrue(self.strategy.submit_order.call_args.kwargs["is_multileg"])
        self.assertEqual(self.strategy.submit_order.call_args.kwargs["order_type"], "credit")

    def test_multileg_parent_and_child_broker_ids_are_bound_without_fabricated_fills(self) -> None:
        assets = [
            SimpleNamespace(
                asset_type="option", symbol="XSP", expiration="2026-10-26",
                strike=strike, right=right,
            )
            for strike, right in ((95, "put"), (100, "put"), (110, "call"), (115, "call"))
        ]
        local_orders = []

        def create_order(asset, quantity, side):
            order = SimpleNamespace(
                asset=asset, quantity=quantity, side=side,
                identifier=f"local-{len(local_orders) + 1}", custom_params={},
                is_active=lambda: True,
            )
            local_orders.append(order)
            return order

        self.strategy.create_order = Mock(side_effect=create_order)
        children = [
            SimpleNamespace(
                asset=asset, quantity=1, side=side, identifier=f"broker-{index}",
                status="submitted", filled_quantity=0, avg_fill_price=None,
            )
            for index, (asset, side) in enumerate(
                zip(assets, ("buy_to_open", "sell_to_open", "sell_to_open", "buy_to_open")),
                start=1,
            )
        ]
        parent = SimpleNamespace(
            identifier="broker-parent", child_orders=children, status="submitted",
            is_active=lambda: True,
        )
        self.strategy.submit_order = Mock(return_value=[parent])
        returned = self.strategy._submit_package(
            list(zip(assets, (1, -1, -1, 1))),
            intent="open", order_type="credit", price=2.0,
        )
        self.assertEqual(returned, [parent])
        [basket] = self.strategy._position_ledger.open_baskets(self.strategy._strategy_id())
        self.assertEqual(basket.broker_parent_id, "broker-parent")
        self.assertEqual(
            {leg["broker_order_id"] for leg in basket.legs},
            {"broker-1", "broker-2", "broker-3", "broker-4"},
        )

        # A parent-level fill cannot be expanded into four synthetic leg fills.
        self.strategy.on_filled_order(None, parent, 2.0, 1, 100)
        self.assertEqual(self.strategy._position_ledger.owned_quantities(self.strategy._strategy_id()), {})

        with patch("trade_logger.record_trade_entry", return_value="trade-broker-ids"):
            for child, price in zip(children, (0.25, 1.25, 1.25, 0.25)):
                self.strategy.on_filled_order(None, child, price, 1, 100)
        self.assertEqual(
            sorted(self.strategy._position_ledger.owned_quantities(self.strategy._strategy_id()).values()),
            [-1, -1, 1, 1],
        )
        self.assertEqual(self.strategy._current_trade_id, "trade-broker-ids")

    def test_risk_tiers_and_quantity_scaling(self) -> None:
        self.strategy.get_positions.return_value = []
        self.strategy.parameters.update(wing_width=2, max_allocation=1.0)
        for cap, percent, quantity in (
            (5_000, 0.30, 7), (10_000, 0.20, 10), (25_000, 0.15, 18),
            (50_000, 0.10, 25), (100_000, 0.05, 25),
        ):
            with self.subTest(cap=cap):
                self.strategy.parameters.update(max_position_risk=cap, quantity=100)
                self.assertEqual(iron_condor.get_max_single_trade_pct(cap), percent)
                self.assertTrue(self.strategy._check_allocation())
                self.assertEqual(self.strategy.parameters["quantity"], quantity)

    def test_global_cap_blocks_additional_exposure(self) -> None:
        self.strategy.parameters.update(wing_width=2, quantity=1, max_portfolio_risk=5000.0)
        peer_assets = [
            SimpleNamespace(
                asset_type="option", symbol="XSP", expiration="2026-10-26",
                strike=strike, right=right,
            )
            for strike, right in ((95, "put"), (100, "put"), (110, "call"), (115, "call"))
        ]
        peer_legs = [
            iron_condor.LedgerLeg(f"peer-{idx}", iron_condor.asset_key(asset), qty)
            for idx, (asset, qty) in enumerate(zip(peer_assets, (10, -10, -10, 10)), start=1)
        ]
        # 5-point wing * 10 contracts * $100 - $100 credit = $4,900 existing defined risk
        self.strategy._position_ledger.record_submission("peer-pkg", "peer-strategy", "open", peer_legs)
        for leg, price in zip(peer_legs, (0.20, 0.25, 0.25, 0.20)):
            self.strategy._position_ledger.record_fill(leg.order_id, leg.requested_signed_quantity, price)
        self.strategy.get_positions.return_value = [
            SimpleNamespace(asset=asset, quantity=qty)
            for asset, qty in zip(peer_assets, (10, -10, -10, 10))
        ]
        self.assertFalse(self.strategy._check_allocation())

    def test_per_strategy_cap_permits_independent_trades(self) -> None:
        """Peer strategy exposure does not block this strategy when within per-strategy cap."""
        self.strategy.parameters.update(wing_width=2, quantity=1, max_position_risk=5000.0)
        peer_assets = [
            SimpleNamespace(
                asset_type="option", symbol="XSP", expiration="2026-10-26",
                strike=strike, right=right,
            )
            for strike, right in ((95, "put"), (100, "put"), (110, "call"), (115, "call"))
        ]
        peer_legs = [
            iron_condor.LedgerLeg(f"peer-{idx}", iron_condor.asset_key(asset), qty)
            for idx, (asset, qty) in enumerate(zip(peer_assets, (10, -10, -10, 10)), start=1)
        ]
        self.strategy._position_ledger.record_submission("peer-pkg", "peer-strategy", "open", peer_legs)
        for leg, price in zip(peer_legs, (0.20, 0.25, 0.25, 0.20)):
            self.strategy._position_ledger.record_fill(leg.order_id, leg.requested_signed_quantity, price)
        self.strategy.get_positions.return_value = [
            SimpleNamespace(asset=asset, quantity=qty)
            for asset, qty in zip(peer_assets, (10, -10, -10, 10))
        ]
        self.assertTrue(self.strategy._check_allocation())

    def test_unmanaged_broker_position_engages_recovery_lock(self) -> None:
        rogue = SimpleNamespace(
            asset_type="option", symbol="XSP", expiration="2026-10-26", strike=575.0, right="put"
        )
        self.strategy.get_positions.return_value = [SimpleNamespace(asset=rogue, quantity=-1)]
        self.strategy._is_entry_window = Mock(return_value=True)
        self.strategy.on_trading_iteration()
        self.assertTrue(self.strategy._recovery_lock)
        self.assertFalse(self.strategy._check_allocation())
        self.strategy.submit_order.assert_not_called()

    def test_confirmed_post_expiration_clears_state_only_after_expiry(self) -> None:
        self.open_filled_position(expiration="2026-10-23")
        self.strategy.get_positions.return_value = []
        # Same day (not past expiration): missing broker visibility retains ledger state
        self.strategy.get_datetime.return_value = datetime(2026, 10, 23, 19, tzinfo=timezone.utc)
        self.strategy.on_trading_iteration()
        self.assertTrue(self.strategy._position_open)
        # Next calendar day after expiration with confirmed flat broker book: resets cleanly
        self.strategy.get_datetime.return_value = datetime(2026, 10, 24, 14, tzinfo=timezone.utc)
        self.strategy.on_trading_iteration()
        self.assertFalse(self.strategy._position_open)
        self.assertEqual(self.strategy._position_ledger.owned_quantities(self.strategy._strategy_id()), {})

    def test_multi_trade_sequential_open_close_cycles_do_not_deadlock(self) -> None:
        """Verify that closing Trade #1 does not permanently deadlock close attempts on Trade #2."""
        # 1. Open Trade #1
        self.open_filled_position(quantity=1)
        self.assertTrue(self.strategy._position_open)
        self.assertEqual(len(self.strategy._position_ledger.open_baskets(self.strategy._strategy_id())), 1)

        # 2. Close Trade #1
        self.strategy._close_all_options(exit_reason="PROFIT_TARGET")
        self.strategy.submit_order.assert_called_once()
        self.strategy.submit_order.reset_mock()

        # Complete fill of Trade #1 close package
        close_basket = next(
            b for b in self.strategy._position_ledger.open_baskets(self.strategy._strategy_id())
            if b.intent == "close"
        )
        for leg in close_basket.legs:
            self.strategy._position_ledger.record_fill(
                leg["order_id"], int(leg["requested_signed_quantity"]), 0.50
            )
        self.strategy._hydrate_from_ledger()
        self.assertFalse(self.strategy._position_open)
        self.strategy.get_positions.return_value = []
        self.strategy.on_trading_iteration()
        self.assertFalse(self.strategy._position_open)

        # 3. Open Trade #2
        self.open_filled_position(quantity=1)
        self.assertTrue(self.strategy._position_open)

        # 4. Close Trade #2 (must NOT deadlock due to Trade #1's closed package)
        self.strategy._close_all_options(exit_reason="STOP_LOSS")
        self.strategy.submit_order.assert_called_once()
        self.assertEqual(self.strategy._pending_exit_reason, "STOP_LOSS")

        # Complete fill of Trade #2 close package
        self.strategy.submit_order.reset_mock()
        close_basket_2 = next(
            b for b in self.strategy._position_ledger.open_baskets(self.strategy._strategy_id())
            if b.intent == "close"
        )
        for leg in close_basket_2.legs:
            self.strategy._position_ledger.record_fill(
                leg["order_id"], int(leg["requested_signed_quantity"]), 0.50
            )
        self.strategy._hydrate_from_ledger()
        self.assertFalse(self.strategy._position_open)

    def test_multi_trade_expire_then_open_close_cycle(self) -> None:
        """Verify that an expired Trade #1 does not poison reprice count or deadlock Trade #2 close."""
        # 1. Open Trade #1
        self.open_filled_position(quantity=1)
        self.assertTrue(self.strategy._position_open)

        # 2. Advance to expiration day and expire Trade #1
        self.strategy.get_datetime.return_value = datetime(2026, 10, 27, 14, tzinfo=timezone.utc)
        self.strategy._position_ledger.expire_strategy_positions(
            self.strategy._strategy_id(), self.strategy.get_datetime().date()
        )
        self.strategy._hydrate_from_ledger()
        self.strategy.get_positions.return_value = []
        self.strategy.on_trading_iteration()
        self.assertFalse(self.strategy._position_open)

        # 3. Open Trade #2
        self.open_filled_position(quantity=1)
        self.assertTrue(self.strategy._position_open)

        # 4. Close Trade #2
        self.strategy._close_all_options(exit_reason="PROFIT_TARGET")
        self.strategy.submit_order.assert_called_once()
        close_basket = next(
            b for b in self.strategy._position_ledger.open_baskets(self.strategy._strategy_id())
            if b.intent == "close"
        )
        self.assertEqual(close_basket.reprice_count, 0)
        self.assertIsNone(close_basket.replacement_of)

    def test_close_all_options_preserves_pending_exit_reason_and_metadata(self) -> None:
        """Verify that retrying _close_all_options or calling EOD hooks preserves pending reason & P&L."""
        self.open_filled_position(quantity=1)
        self.strategy._close_all_options(
            exit_reason="STOP_LOSS", gross_pnl=-250.0, fees=6.40, net_pnl=-256.40
        )
        self.assertEqual(self.strategy._pending_exit_reason, "STOP_LOSS")
        self.assertEqual(self.strategy._pending_gross_pnl, -250.0)
        self.assertEqual(self.strategy._pending_fees, 6.40)
        self.assertEqual(self.strategy._pending_net_pnl, -256.40)

        # Retry with no arguments (e.g. before_market_closes or on_abrupt_closing)
        self.strategy.before_market_closes()
        self.assertEqual(self.strategy._pending_exit_reason, "STOP_LOSS")
        self.assertEqual(self.strategy._pending_gross_pnl, -250.0)
        self.assertEqual(self.strategy._pending_fees, 6.40)
        self.assertEqual(self.strategy._pending_net_pnl, -256.40)

        self.strategy.on_abrupt_closing()
        self.assertEqual(self.strategy._pending_exit_reason, "STOP_LOSS")


class OrderRoutingRegressionTests(unittest.TestCase):
    setUp = IronCondorTests.setUp

    @staticmethod
    def assets() -> list[SimpleNamespace]:
        return [
            SimpleNamespace(
                asset_type="option", symbol="XSP", expiration="2026-11-20",
                strike=strike, right=right,
            )
            for strike, right in ((95, "put"), (100, "put"), (110, "call"), (115, "call"))
        ]

    def configure_submission(self, strategy: Any) -> None:
        local_orders = []

        def create_order(asset: object, quantity: int, side: str) -> SimpleNamespace:
            order = SimpleNamespace(
                identifier=f"local-{len(local_orders)}", asset=asset,
                quantity=quantity, side=side, custom_params={},
            )
            local_orders.append(order)
            return order

        strategy.create_order = Mock(side_effect=create_order)
        strategy.submit_order = Mock(return_value=[SimpleNamespace(
            identifier="submitted-parent", child_orders=[], custom_params={},
        )])
        strategy.get_positions = Mock(return_value=[])

    def record_package(self, package_id: str, tag: str = "Shared-Host") -> SimpleNamespace:
        children = [
            SimpleNamespace(
                identifier=f"{package_id}-broker-{index}", asset=asset,
                side=side, quantity=1, status="submitted", filled_quantity=0,
                avg_fill_price=None, tag=tag, child_orders=[],
            )
            for index, (asset, side) in enumerate(zip(
                self.assets(), ("buy_to_open", "sell_to_open", "sell_to_open", "buy_to_open"),
            ))
        ]
        ledger = self.strategy._position_ledger
        for child in children:
            self.strategy._asset_registry[iron_condor.asset_key(child.asset)] = child.asset
        ledger.record_submission(
            package_id, self.strategy._strategy_id(), "open",
            [
                iron_condor.LedgerLeg(f"{package_id}-local-{index}", iron_condor.asset_key(child.asset), sign)
                for index, (child, sign) in enumerate(zip(children, (1, -1, -1, 1)))
            ],
            broker_tag=tag,
        )
        parent = SimpleNamespace(
            identifier=f"{package_id}-parent", child_orders=children,
            status="submitted", tag=tag,
        )
        for child in children:
            child.parent_identifier = parent.identifier
        return parent

    def test_single_strategy_uses_normalized_registered_name_everywhere(self) -> None:
        self.strategy.name = "  IC_Host / café-1  "
        self.configure_submission(self.strategy)
        [parent] = self.strategy._submit_package(
            list(zip(self.assets(), (1, -1, -1, 1))),
            intent="open", order_type="credit", price=2.0,
        )
        [basket] = self.strategy._position_ledger.open_baskets()
        self.assertEqual(basket.strategy_id, self.strategy.name)
        self.assertEqual(basket.broker_tag, "IC-Host---caf--1")
        self.assertEqual(parent.tag, basket.broker_tag)
        self.assertEqual(parent.custom_params["tag"], basket.broker_tag)
        self.assertNotEqual(basket.package_id, basket.broker_tag)
        for child in self.strategy.submit_order.call_args.args[0]:
            self.assertEqual(child.tag, basket.broker_tag)
            self.assertEqual(child.custom_params["tag"], basket.broker_tag)
        self.strategy.submit_order.assert_called_once()
        self.assertTrue(self.strategy.submit_order.call_args.kwargs["is_multileg"])
        self.assertEqual(self.strategy._position_ledger.all_owned_quantities(), {})

    def test_empty_registered_host_name_fails_before_recording_or_submitting(self) -> None:
        self.configure_submission(self.strategy)
        self.strategy._host_strategy = SimpleNamespace(name=" \t ")
        with self.assertRaisesRegex(ValueError, "registered strategy name"):
            self.strategy._submit_package(
                list(zip(self.assets(), (1, -1, -1, 1))),
                intent="open", order_type="credit", price=2.0,
            )
        self.assertEqual(self.strategy._position_ledger.open_baskets(), ())
        self.strategy.create_order.assert_not_called()
        self.strategy.submit_order.assert_not_called()

    def test_ab_and_incubator_followers_route_to_host_and_keep_ledger_owner(self) -> None:
        for builder in (incubator.build_ab_strategies, incubator.build_incubator_strategies):
            with self.subTest(builder=builder.__name__):
                ledger = iron_condor.PositionLedger(":memory:")
                self.addCleanup(ledger._connection.close)
                with patch.object(incubator, "SPXIronCondor", iron_condor.SPXIronCondor):
                    strategies = builder(broker=self.strategy.broker, position_ledger=ledger)
                host = strategies[0]
                host.broker = self.strategy.broker
                self.configure_submission(host)
                for follower in strategies[1:]:
                    follower.initialize()
                    incubator.bind_follower_to_host(follower, host)
                    follower._submit_package(
                        list(zip(self.assets(), (1, -1, -1, 1))),
                        intent="open", order_type="credit", price=2.0,
                    )
                    [basket] = ledger.open_baskets(follower.name)
                    self.assertEqual(basket.strategy_id, follower.name)
                    self.assertEqual(basket.broker_tag, host.name.replace("_", "-"))
                    self.assertNotEqual(basket.strategy_id, host.name)
                self.assertEqual(ledger.all_owned_quantities(), {})

    def test_shared_host_tags_never_cross_bind_packages_even_after_rollover(self) -> None:
        first = self.record_package("first")
        second = self.record_package("second")
        self.strategy.get_datetime.return_value = datetime(2026, 11, 1, tzinfo=timezone.utc)
        self.assertTrue(self.strategy._reconcile_inflight_orders([first]))
        self.assertTrue(self.strategy._reconcile_inflight_orders([second, first]))
        for package_id in ("first", "second"):
            basket = self.strategy._position_ledger.get_basket(package_id)
            self.assertIsNone(basket.broker_parent_id)
            self.assertEqual(basket.state, "submitted")
            self.assertTrue(all(leg["broker_order_id"] is None for leg in basket.legs))
        self.assertIsNone(self.strategy._find_order_by_tag([first], "Shared-Host"))

    def test_confirmed_parent_and_child_ids_recover_correct_packages_after_reopen(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "positions.sqlite3"
            ledger = iron_condor.PositionLedger(path)
            self.strategy._position_ledger = ledger
            first = self.record_package("first")
            second = self.record_package("second")
            ledger.bind_broker_submission("first", first)
            # Simulate a submission where only the child IDs were persisted.
            ledger.bind_broker_children("second", second.child_orders)
            ledger._connection.close()
            reopened = iron_condor.PositionLedger(path)
            self.addCleanup(reopened._connection.close)
            self.strategy._position_ledger = reopened
            self.strategy.get_datetime.return_value = datetime.now(timezone.utc)
            self.strategy.get_order = Mock(return_value=None)
            self.assertFalse(self.strategy._reconcile_inflight_orders([second, first]))
            self.assertEqual(reopened.get_basket("first").broker_parent_id, first.identifier)
            self.assertEqual(reopened.get_basket("second").broker_parent_id, second.identifier)
            self.assertEqual(reopened.all_owned_quantities(), {})

    def test_direct_parent_lookup_and_persisted_child_lookup_restore_identity(self) -> None:
        parent = self.record_package("direct")
        ledger = self.strategy._position_ledger
        ledger.bind_broker_submission("direct", parent)
        self.strategy.get_datetime.return_value = datetime.now(timezone.utc)
        self.strategy.get_order = Mock(side_effect=lambda key: {
            parent.identifier: parent,
            **{child.identifier: child for child in parent.child_orders},
        }.get(key))
        self.assertFalse(self.strategy._reconcile_inflight_orders([]))
        self.strategy.get_order.assert_any_call(parent.identifier)

        second = self.record_package("child-only")
        ledger.bind_broker_children("child-only", second.child_orders)
        self.strategy.get_order.side_effect = lambda key: {
            parent.identifier: parent, second.identifier: second,
            **{child.identifier: child for child in second.child_orders},
        }.get(key)
        self.assertFalse(self.strategy._reconcile_inflight_orders([]))
        self.assertEqual(ledger.get_basket("child-only").broker_parent_id, second.identifier)

    def test_legacy_tags_resolve_original_and_sanitized_forms_only_when_unique(self) -> None:
        tag = "IC_0123456789abcdef0123"
        first = self.record_package("legacy", tag)
        second = SimpleNamespace(identifier="different-parent", tag=tag, child_orders=[])
        for stored_tag in (tag, tag.replace("_", "")):
            for broker_tag in (tag, tag.replace("_", "")):
                with self.subTest(stored_tag=stored_tag, broker_tag=broker_tag):
                    first.tag = broker_tag
                    for child in first.child_orders:
                        child.tag = broker_tag
                    orders = [first, first, *first.child_orders]
                    self.assertIs(self.strategy._find_order_by_tag(orders, stored_tag), first)
                    self.assertIsNone(self.strategy._find_order_by_tag([*orders, second], stored_tag))
        self.assertFalse(self.strategy._reconcile_inflight_orders([first]))
        self.assertEqual(self.strategy._position_ledger.get_basket("legacy").broker_parent_id, first.identifier)
        for invalid in ("Shared-Host", "IC_short", "IC_" + "a" * 21):
            self.assertIsNone(self.strategy._find_order_by_tag([first], invalid))

    def test_ambiguous_legacy_tags_and_conflicting_child_parents_retain_basket(self) -> None:
        tag = "IC_0123456789abcdef0123"
        first = self.record_package("legacy", tag)
        second = SimpleNamespace(identifier="other-parent", tag=tag, child_orders=[])
        self.assertTrue(self.strategy._reconcile_inflight_orders([first, second]))
        self.assertIsNone(self.strategy._position_ledger.get_basket("legacy").broker_parent_id)
        self.strategy._position_ledger.bind_broker_children("legacy", first.child_orders)
        second.child_orders = first.child_orders
        self.assertTrue(self.strategy._reconcile_inflight_orders([first, second]))
        self.assertIsNone(self.strategy._position_ledger.get_basket("legacy").broker_parent_id)

    def test_missing_parent_identity_locks_reconciliation_and_preserves_filled_contracts(self) -> None:
        self.record_package("missing")
        ledger = self.strategy._position_ledger
        leg = ledger.get_basket("missing").legs[0]
        ledger.record_fill(leg["order_id"], leg["requested_signed_quantity"], 0.25)
        owned = ledger.all_owned_quantities()
        self.strategy.get_datetime.return_value = datetime(2026, 11, 1, tzinfo=timezone.utc)
        self.assertTrue(self.strategy._reconcile_broker_state())
        self.assertTrue(self.strategy._recovery_lock)
        self.assertEqual(ledger.get_basket("missing").state, "partial")
        self.assertEqual(ledger.all_owned_quantities(), owned)
        self.assertIsNone(ledger.get_basket("missing").broker_parent_id)


class IncubatorAndMarketDataHubTests(unittest.TestCase):
    def setUp(self) -> None:
        self.environment = patch.dict(os.environ, {}, clear=True)
        self.environment.start()
        self.addCleanup(self.environment.stop)

    def test_incubator_defines_20_distinct_strategy_variants(self) -> None:
        hub = market_data_hub.MarketDataHub(ttl_seconds=240)
        with patch.dict(os.environ, {"INCUBATOR_PROFILE": "full_grid", "STRATEGYBRO_ENABLED": "false", "DYNAMIC_STRATEGIES_ENABLED": "false", "NEURAL_STRATEGY_ENABLED": "false"}):
            strategies = incubator.build_incubator_strategies(broker=Mock(), market_data_hub=hub, limit=20)
        self.assertEqual(len(strategies), 20)
        names = {s.name for s in strategies}
        self.assertEqual(len(names), 20)
        # Verify env vars do not clobber explicit variant parameters on initialize()
        with patch.dict(os.environ, {"IC_SHORT_DELTA": "0.99", "IC_WING_WIDTH": "99"}, clear=False):
            for strat in strategies:
                expected_delta = strat.parameters["short_delta"]
                expected_width = strat.parameters["wing_width"]
                strat.initialize()
                self.assertEqual(strat.parameters["short_delta"], expected_delta)
                self.assertEqual(strat.parameters["wing_width"], expected_width)

    def test_market_data_hub_deduplicates_chains_and_prices_across_20_strategies(self) -> None:
        hub = market_data_hub.MarketDataHub(ttl_seconds=240)
        with patch.dict(os.environ, {"INCUBATOR_PROFILE": "full_grid", "STRATEGYBRO_ENABLED": "false", "DYNAMIC_STRATEGIES_ENABLED": "false", "NEURAL_STRATEGY_ENABLED": "false"}):
            strategies = incubator.build_incubator_strategies(broker=Mock(), market_data_hub=hub, limit=20)
        fixed_dt = datetime(2026, 10, 26, 14, 0, tzinfo=timezone.utc)
        chain_call_count = 0
        price_call_count = 0

        def fake_get_chains(asset):
            nonlocal chain_call_count
            chain_call_count += 1
            return {"Chains": {"CALL": {}, "PUT": {}}}

        def fake_get_last_price(asset):
            nonlocal price_call_count
            price_call_count += 1
            return 585.0

        for strat in strategies:
            strat.initialize()
            strat.get_datetime = Mock(return_value=fixed_dt)
            strat.get_chains = Mock(side_effect=fake_get_chains)
            strat.get_expiration = Mock(return_value=["2026-10-26"])
            strat.get_strikes = Mock(return_value=[float(strike) for strike in range(540, 631)])
            strat.get_last_price = Mock(side_effect=fake_get_last_price)

        for strat in strategies:
            exp = strat._find_expiration()
            self.assertEqual(exp, "2026-10-26")
            price = strat._fetch_last_price(strat._underlying)
            self.assertEqual(price, 585.0)
            put_s, call_s = strat._find_short_strikes(price, exp)
            self.assertIsNotNone(put_s)
            self.assertIsNotNone(call_s)

        # Across all 20 strategies, broker chain and underlying price fetched only once!
        self.assertEqual(chain_call_count, 1)
        self.assertEqual(price_call_count, 1)
        stats = hub.get_stats()
        self.assertGreaterEqual(stats["chain_hits"], 39)
        self.assertEqual(stats["price_hits"], 19)

    def test_per_strategy_leg_isolation_prevents_closing_other_variants_positions(self) -> None:
        ledger = iron_condor.PositionLedger(":memory:")
        broker = SimpleNamespace(name="Tradier")
        strat_a = iron_condor.SPXIronCondor()
        strat_a.name = "Strategy_A"
        strat_a.parameters = {**iron_condor.SPXIronCondor.parameters, "position_ledger": ledger}
        strat_a.broker = broker
        strat_a.initialize()

        strat_b = iron_condor.SPXIronCondor()
        strat_b.name = "Strategy_B"
        strat_b.parameters = {**iron_condor.SPXIronCondor.parameters, "position_ledger": ledger}
        strat_b.broker = broker
        strat_b.initialize()

        assets_a = [
            SimpleNamespace(asset_type="option", symbol="XSP", expiration="2026-10-26", strike=s, right=r)
            for s, r in ((575.0, "put"), (580.0, "put"), (590.0, "call"), (595.0, "call"))
        ]
        assets_b = [
            SimpleNamespace(asset_type="option", symbol="XSP", expiration="2026-10-26", strike=s, right=r)
            for s, r in ((570.0, "put"), (575.0, "put"), (595.0, "call"), (600.0, "call"))
        ]
        for label, strat, assets, qty in (
            ("pkg-a", strat_a, assets_a, 1),
            ("pkg-b", strat_b, assets_b, 2),
        ):
            legs = []
            for idx, (asset, signed) in enumerate(zip(assets, (qty, -qty, -qty, qty)), start=1):
                key = iron_condor.asset_key(asset)
                strat._asset_registry[key] = asset
                legs.append(iron_condor.LedgerLeg(f"{label}:{idx}", key, signed))
            ledger.record_submission(label, strat._strategy_id(), "open", legs)
            for leg, price in zip(legs, (0.20, 1.00, 1.00, 0.20)):
                ledger.record_fill(leg.order_id, leg.requested_signed_quantity, price)
            strat._hydrate_from_ledger()

        # Broker reports net positions across both strategies (note 575P and 595C overlap!)
        strat_a.get_positions = Mock(return_value=[
            SimpleNamespace(asset=assets_b[0], quantity=2),
            SimpleNamespace(asset=assets_a[0], quantity=-1),  # +1 (A) + -2 (B) = -1 net!
            SimpleNamespace(asset=assets_a[1], quantity=-1),
            SimpleNamespace(asset=assets_a[2], quantity=-1),
            SimpleNamespace(asset=assets_a[3], quantity=-1),  # +1 (A) + -2 (B) = -1 net!
            SimpleNamespace(asset=assets_b[3], quantity=2),
        ])
        strat_a.get_orders = Mock(return_value=[])
        strat_a.get_quote = Mock(side_effect=lambda asset: (
            {"bid": 0.95, "ask": 1.05} if asset.strike in (580.0, 590.0) else {"bid": 0.15, "ask": 0.25}
        ))
        strat_a.create_order = Mock(side_effect=lambda asset, quantity, side: Mock(
            asset=asset, quantity=quantity, side=side, is_active=Mock(return_value=True),
        ))
        strat_a.submit_order = Mock()

        strat_a._close_all_options()
        strat_a.submit_order.assert_called_once()
        submitted_orders = strat_a.submit_order.call_args.args[0]
        self.assertEqual(len(submitted_orders), 4)
        self.assertTrue(strat_a.submit_order.call_args.kwargs["is_multileg"])
        self.assertEqual(strat_a.submit_order.call_args.kwargs["order_type"], "debit")
        submitted_by_strike = {o.asset.strike: (o.quantity, o.side) for o in submitted_orders}
        self.assertEqual(submitted_by_strike, {
            575.0: (1, "sell_to_close"),
            580.0: (1, "buy_to_close"),
            590.0: (1, "buy_to_close"),
            595.0: (1, "sell_to_close"),
        })
        self.assertEqual(len(ledger.owned_quantities("Strategy_B")), 4)

    def test_apply_policy_action_respects_risk_guardrails_and_scales_legs(self) -> None:
        strat = iron_condor.SPXIronCondor()
        strat.parameters = dict(iron_condor.SPXIronCondor.parameters)
        strat.parameters.update(wing_width=2.0, quantity=1, max_position_risk=5000.0, max_allocation=1.0)
        strat.broker = SimpleNamespace(name="Tradier")
        strat.initialize()
        strat.portfolio_value = 100_000.0
        strat.get_datetime = Mock(return_value=datetime(2026, 10, 26, 15, 0, tzinfo=timezone.utc))
        assets = [
            SimpleNamespace(asset_type="option", symbol="XSP", expiration="2026-10-26", strike=s, right=r)
            for s, r in ((578.0, "put"), (580.0, "put"), (590.0, "call"), (592.0, "call"))
        ]
        open_legs = []
        for idx, (asset, signed) in enumerate(zip(assets, (1, -1, -1, 1)), start=1):
            key = iron_condor.asset_key(asset)
            strat._asset_registry[key] = asset
            open_legs.append(iron_condor.LedgerLeg(f"init:{idx}", key, signed))
        strat._position_ledger.record_submission("init", strat._strategy_id(), "open", open_legs)
        for leg, price in zip(open_legs, (0.10, 0.50, 0.50, 0.10)):
            strat._position_ledger.record_fill(leg.order_id, leg.requested_signed_quantity, price)
        strat._hydrate_from_ledger()

        def sync_broker_positions() -> None:
            owned = strat._position_ledger.owned_quantities(strat._strategy_id())
            strat.get_positions.return_value = [
                SimpleNamespace(asset=strat._asset_registry[k], quantity=q)
                for k, q in owned.items()
            ]

        strat.get_positions = Mock()
        sync_broker_positions()
        strat.get_orders = Mock(return_value=[])
        strat.get_quote = Mock(side_effect=lambda asset: (
            {"bid": 0.45, "ask": 0.55} if asset.strike in (580.0, 590.0) else {"bid": 0.05, "ask": 0.15}
        ))
        strat.create_order = Mock(side_effect=lambda asset, quantity, side: Mock(
            asset=asset, quantity=quantity, side=side, is_active=Mock(return_value=True),
        ))
        strat.submit_order = Mock()

        # 1. Dynamic Profit Target clamping
        res = strat.apply_policy_action("ADJUST_PROFIT_TARGET", dynamic_profit_target=0.95)
        self.assertEqual(res, "ADJUST_PROFIT_TARGET")
        self.assertEqual(strat.parameters["profit_target"], 0.80)

        # 2. ADD_POSITION (+1 contract) submits an atomic credit package; ownership waits for fills
        res = strat.apply_policy_action("ADD_POSITION")
        self.assertEqual(res, "ADD_POSITION")
        self.assertEqual(strat.parameters["quantity"], 1)
        for order in strat._entry_orders:
            order.is_active.return_value = False
            price = 0.50 if order.asset.strike in (580.0, 590.0) else 0.10
            strat.on_filled_order(None, order, price, order.quantity, 100)
        sync_broker_positions()
        self.assertEqual(strat.parameters["quantity"], 1)
        self.assertEqual(max(abs(q) for _, q in strat._owned_legs), 2)
        self.assertEqual(sorted(q for _, q in strat._owned_legs), [-2, -2, 2, 2])
        self.assertAlmostEqual(strat._entry_credit, 0.80)
        self.assertAlmostEqual(strat._entry_credit_cash, 160.0)

        # 3. TRIM_POSITION (50% scale-out) submits an atomic debit package; ownership updates on fills
        res = strat.apply_policy_action("TRIM_POSITION")
        self.assertEqual(res, "TRIM_POSITION")
        self.assertEqual(strat.parameters["quantity"], 1)
        for order in strat._closing_orders:
            order.is_active.return_value = False
            price = 0.50 if order.asset.strike in (580.0, 590.0) else 0.10
            strat.on_filled_order(None, order, price, order.quantity, 100)
        sync_broker_positions()
        self.assertEqual(strat.parameters["quantity"], 1)
        self.assertEqual(sorted(q for _, q in strat._owned_legs), [-1, -1, 1, 1])
        self.assertAlmostEqual(strat._entry_credit, 0.80)
        self.assertAlmostEqual(strat._entry_credit_cash, 80.0)

        # 4. ADD_POSITION is blocked when single-trade risk would exceed RSK-02 limit
        strat.parameters["max_position_risk"] = 1000.0  # $400 combined risk exceeds the $300 single-position limit.
        res = strat.apply_policy_action("ADD_POSITION")
        self.assertEqual(res, "HOLD")
        self.assertEqual(strat.parameters["quantity"], 1)

    def test_tradier_underlying_asset_type_avoids_index_prefix(self) -> None:
        """Tradier quotes index tickers (SPX, XSP) as stocks, avoiding the invalid 'I:' prefix."""
        iron_condor.Asset.AssetType.STOCK = "stock"
        iron_condor.Asset.AssetType.INDEX = "index"
        iron_condor.Asset.side_effect = lambda symbol, asset_type=None, **kwargs: SimpleNamespace(
            symbol=symbol, asset_type=asset_type, **kwargs
        )
        try:
            strat = iron_condor.SPXIronCondor()
            strat.parameters.update(symbol="XSP")
            strat.broker = SimpleNamespace(name="Tradier")
            strat._hydrate_from_ledger = Mock()
            strat.initialize()
            self.assertEqual(strat._underlying.asset_type, "stock")

            # Non-Tradier broker preserves INDEX type for indexes
            strat_other = iron_condor.SPXIronCondor()
            strat_other.parameters.update(symbol="XSP")
            strat_other.broker = SimpleNamespace(name="Alpaca")
            strat_other._hydrate_from_ledger = Mock()
            with patch.dict(os.environ, {"TRADING_BROKER": "alpaca"}, clear=True):
                strat_other.initialize()
            self.assertEqual(strat_other._underlying.asset_type, "index")
        finally:
            iron_condor.Asset.side_effect = None

    def test_position_ledger_schema_mismatch_resilience(self) -> None:
        """PositionLedger dynamically handles legacy 8-column and migrated 15-column schemas."""
        import sqlite3
        from strategies.position_ledger import LedgerLeg, PositionLedger

        # 1. Test against a legacy 8-column table (simulating pre-migration DB or partial rollout)
        connection = sqlite3.connect(":memory:")
        connection.executescript(
            """
            CREATE TABLE packages (
                package_id TEXT PRIMARY KEY,
                strategy_id TEXT NOT NULL,
                intent TEXT NOT NULL,
                state TEXT NOT NULL,
                opening_cash_flow REAL,
                closing_cash_flow REAL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE legs (
                order_id TEXT PRIMARY KEY,
                package_id TEXT NOT NULL,
                asset_key TEXT NOT NULL,
                requested_signed_quantity INTEGER NOT NULL,
                filled_signed_quantity INTEGER NOT NULL DEFAULT 0,
                fill_price REAL,
                status TEXT NOT NULL
            );
            """
        )
        ledger = PositionLedger.__new__(PositionLedger)
        ledger.path = ":memory:"
        ledger._connection = connection
        ledger._connection.row_factory = sqlite3.Row
        ledger._lock = type("DummyLock", (), {"__enter__": lambda s: None, "__exit__": lambda s, *a: None})()

        legs = [
            LedgerLeg(order_id=f"leg-{i}", asset_key=f"k-{i}", requested_signed_quantity=1 if i % 2 == 1 else -1)
            for i in range(1, 5)
        ]
        # Must not raise "table packages has 8 columns but 15 values were supplied"
        ledger.record_submission("pkg-legacy-1", "strat-1", "open", legs, limit_price=1.25)
        row = connection.execute("SELECT * FROM packages WHERE package_id = 'pkg-legacy-1'").fetchone()
        self.assertIsNotNone(row)
        self.assertEqual(row["state"], "submitted")

        # _basket_from_row safely populates defaults when new columns do not exist
        basket = ledger._basket_from_row(row)
        self.assertEqual(basket.package_id, "pkg-legacy-1")
        self.assertIsNone(basket.broker_parent_id)
        self.assertEqual(basket.reprice_count, 0)

        # 2. Test against fully migrated 15-column table
        ledger_full = PositionLedger(":memory:")
        ledger_full.record_submission(
            "pkg-full-1", "strat-1", "open", legs,
            limit_price=2.50, broker_tag="TAG123", replacement_of="pkg-old", reprice_count=1,
        )
        basket_full = ledger_full.get_basket("pkg-full-1")
        self.assertEqual(basket_full.limit_price, 2.50)
        self.assertEqual(basket_full.broker_tag, "TAG123")
        self.assertEqual(basket_full.replacement_of, "pkg-old")
        self.assertEqual(basket_full.reprice_count, 1)

    def test_swing_positions_held_overnight_unless_expiring(self) -> None:
        """Swing positions (DTE > 0) are held overnight and only closed on their expiration day."""
        today = date(2026, 10, 27)
        strat = iron_condor.SPXIronCondor.__new__(iron_condor.SPXIronCondor)
        strat.parameters = {"symbol": "XSP", "dte": 7}
        strat.get_datetime = Mock(return_value=datetime(2026, 10, 27, 15, 55, tzinfo=timezone.utc))
        strat._position_open = True
        strat._attached_strategies = []
        strat._close_all_options = Mock()

        # 1. 7 DTE position expiring in the future (November 3) -> DO NOT close at market close
        future_asset = SimpleNamespace(expiration=date(2026, 11, 3))
        strat._owned_legs = [(future_asset, 1)]
        self.assertFalse(strat._is_expiring_today())
        strat.before_market_closes()
        strat._close_all_options.assert_not_called()

        # 2. Position expiring today (October 27) -> DO close before market close
        today_asset = SimpleNamespace(expiration=today)
        strat._owned_legs = [(today_asset, 1)]
        self.assertTrue(strat._is_expiring_today())
        strat.before_market_closes()
        strat._close_all_options.assert_called_once()

        # 3. 0 DTE position -> DO close before market close even without legs populated
        strat._close_all_options.reset_mock()
        strat.parameters["dte"] = 0
        strat._owned_legs = []
        self.assertTrue(strat._is_expiring_today())
        strat.before_market_closes()
        strat._close_all_options.assert_called_once()

    def test_incubator_5_dte_tiers_configuration(self) -> None:
        """INCUBATOR_GRID_20 covers 5 DTE tiers (0, 7, 14, 21, 30) with 4 setup variations each."""
        self.assertEqual(len(incubator.INCUBATOR_GRID_20), 20)
        expected_dtes = [0, 7, 14, 21, 30]
        actual_dtes = sorted(list({variant["dte"] for variant in incubator.INCUBATOR_GRID_20}))
        self.assertEqual(actual_dtes, expected_dtes)

        for dte in expected_dtes:
            tier_variants = [v for v in incubator.INCUBATOR_GRID_20 if v["dte"] == dte]
            self.assertEqual(len(tier_variants), 4, f"DTE {dte} should have 4 setup variations")
            deltas = [v["short_delta"] for v in tier_variants]
            self.assertEqual(deltas, [0.10, 0.12, 0.15, 0.20])

        # All strategy names must follow IC_V01..IC_V20 sequence
        for idx, variant in enumerate(incubator.INCUBATOR_GRID_20, start=1):
            prefix = f"IC_V{idx:02d}"
            self.assertTrue(
                variant["strategy_name"].startswith(prefix),
                f"Variant {idx} name {variant['strategy_name']} must start with {prefix}",
            )

    def test_incubator_extended_with_25_dte_testing_and_pure_neural_variants(self) -> None:
        """Incubator specs include 20 benchmarks + 25 DTE testing variant + pure neural strategy."""
        specs = incubator.get_incubator_specs(profile="full_grid")
        self.assertGreaterEqual(len(specs), 22)
        testing = next((s for s in specs if s.get("is_testing_variant")), None)
        self.assertIsNotNone(testing, "Testing variant should be present in incubator specs")
        self.assertEqual(testing["dte"], 25)
        self.assertTrue(testing["strategy_name"].startswith("IC_V21"))

        neural = next((s for s in specs if s.get("pure_neural_mode")), None)
        self.assertIsNotNone(neural, "Pure neural strategy should be present in incubator specs")
        self.assertTrue(neural["strategy_name"].startswith("IC_V22"))

    def test_testing_variant_env_overrides(self) -> None:
        """Testing variant parameters can be easily updated via env/UI."""
        with patch.dict(os.environ, {
            "TESTING_DTE": "45",
            "TESTING_DELTA": "0.18",
            "TESTING_WING": "10.0",
            "TESTING_PT": "0.40",
            "TESTING_SL": "2.5",
            "TESTING_ENTRY_TIME": "11:15",
        }, clear=False):
            spec = incubator.get_testing_variant_spec()
            self.assertEqual(spec["dte"], 45)
            self.assertEqual(spec["short_delta"], 0.18)
            self.assertEqual(spec["wing_width"], 10.0)
            self.assertEqual(spec["profit_target"], 0.40)
            self.assertEqual(spec["stop_loss_mult"], 2.5)
            self.assertEqual(spec["entry_time"], "11:15")

    def test_pure_neural_strategy_management_and_routing(self) -> None:
        """Pure neural strategy receives dynamic profit targets and policy actions."""
        strat = iron_condor.SPXIronCondor()
        strat.parameters = {
            "strategy_name": "IC_V22_NEURAL_PURE",
            "pure_neural_mode": True,
            "profit_target": 0.50,
            "quantity": 1,
            "wing_width": 5.0,
        }
        strat.initialize()
        self.assertTrue(strat.pure_neural_mode)

        # Policy dynamic profit target update
        action = strat.apply_policy_action("ADJUST_PROFIT_TARGET", dynamic_profit_target=0.42)
        self.assertEqual(action, "ADJUST_PROFIT_TARGET")
        self.assertAlmostEqual(strat.parameters["profit_target"], 0.42)

    def test_neural_strategy_entry_blocked_without_sufficient_data(self) -> None:
        """Pure neural strategy must hold off trading until sufficient sequence data has been ingested."""
        strat = iron_condor.SPXIronCondor()
        strat.parameters = {
            "strategy_name": "IC_V22_NEURAL_PURE",
            "pure_neural_mode": True,
            "quantity": 1,
            "wing_width": 5.0,
            "entry_time": "10:00",
        }
        strat.initialize()
        mock_controller = Mock()
        mock_controller.has_sufficient_data = False
        mock_controller.current_ticks = 4
        mock_controller.required_ticks = 12
        strat._policy_controller = mock_controller
        strat.get_datetime = Mock(return_value=datetime(2026, 10, 27, 10, 30, tzinfo=timezone.utc))
        strat._reconcile_broker_state = Mock(return_value=True)
        strat._find_expiration = Mock()

        # Execute trading iteration
        strat._run_trading_iteration()

        # Find expiration must not be called because insufficient data blocks entry
        strat._find_expiration.assert_not_called()

    def test_neural_strategy_dynamically_switches_strategy_parameters(self) -> None:
        """Pure neural strategy dynamically switches between all strategy configurations based on signals."""
        strat = iron_condor.SPXIronCondor()
        strat.parameters = {
            "strategy_name": "IC_V22_NEURAL_PURE",
            "pure_neural_mode": True,
            "short_delta": 0.10,
            "wing_width": 2.0,
            "dte": 0,
            "stop_loss_mult": 1.5,
        }
        strat.initialize()

        # Neural net selects IC_V07 (7DTE, delta 0.15, wing 5.0, stop loss 2.0)
        with patch.dict(os.environ, {"INCUBATOR_PROFILE": "full_grid"}):
            strat._apply_neural_strategy_switch("IC_V07_7DTE_D15_W5_PT50_T1030")
        self.assertEqual(strat.parameters["dte"], 7)
        self.assertEqual(strat.parameters["short_delta"], 0.15)
        self.assertEqual(strat.parameters["wing_width"], 5.0)
        self.assertEqual(strat.parameters["stop_loss_mult"], 2.0)

        # Later, market shifts and neural net switches to IC_V16 (21DTE, delta 0.20, wing 5.0, stop loss 2.5)
        with patch.dict(os.environ, {"INCUBATOR_PROFILE": "full_grid"}):
            strat._apply_neural_strategy_switch("IC_V16_21DTE_D20_W5_PT50_T1100")
        self.assertEqual(strat.parameters["dte"], 21)
        self.assertEqual(strat.parameters["short_delta"], 0.20)
        self.assertEqual(strat.parameters["wing_width"], 5.0)
        self.assertEqual(strat.parameters["stop_loss_mult"], 2.5)

    def test_position_ledger_supports_2_leg_vertical_spread_packages(self) -> None:
        """PositionLedger accepts 2-leg vertical spread submissions and calculates defined risk."""
        ledger = iron_condor.PositionLedger(":memory:")
        put_long_key = json.dumps({"symbol": "XSP", "expiration": "2026-11-03", "right": "put", "strike": 575.0})
        put_short_key = json.dumps({"symbol": "XSP", "expiration": "2026-11-03", "right": "put", "strike": 580.0})
        legs = [
            iron_condor.LedgerLeg("order_long_p", put_long_key, 1),
            iron_condor.LedgerLeg("order_short_p", put_short_key, -1),
        ]
        # Should record without raising ValueError
        ledger.record_submission("pkg_bull_put", "STRAT_VERTICAL", "open", legs, limit_price=1.25)
        baskets = ledger.open_baskets("STRAT_VERTICAL")
        self.assertEqual(len(baskets), 1)
        self.assertEqual(baskets[0].package_id, "pkg_bull_put")
        self.assertEqual(len(baskets[0].legs), 2)

        # Defined risk for $5 width 1-contract vertical spread = $5.00 * 100 = $500.00
        risk = ledger.portfolio_defined_risk("STRAT_VERTICAL")
        self.assertEqual(risk, 500.0)

    def test_strategy_builds_and_submits_vertical_spread_legs(self) -> None:
        """SPXIronCondor with structure='bull_put' submits 2 legs instead of 4."""
        strat = iron_condor.SPXIronCondor()
        strat.parameters = {
            "strategy_name": "IC_V23_7DTE_BULL_PUT",
            "symbol": "XSP",
            "structure": "bull_put",
            "quantity": 1,
            "wing_width": 5.0,
            "short_delta": 0.15,
            "dte": 7,
        }
        strat.initialize()
        self.assertEqual(strat.structure, "bull_put")

        # Mock market data methods
        strat.get_datetime = Mock(return_value=datetime(2026, 10, 27, 10, 30, tzinfo=timezone.utc))
        strat._reconcile_broker_state = Mock(return_value=True)
        strat._is_entry_window = Mock(return_value=True)
        strat._check_allocation = Mock(return_value=True)
        strat._find_expiration = Mock(return_value="2026-11-03")
        strat._fetch_last_price = Mock(return_value=585.0)
        strat._find_short_strikes = Mock(return_value=(575.0, 595.0))
        strat._package_price = Mock(return_value=1.50)
        submitted_legs = []

        def fake_submit(legs, **kwargs):
            nonlocal submitted_legs
            submitted_legs = legs
            return [Mock()]

        strat._submit_package = Mock(side_effect=fake_submit)
        strat._run_trading_iteration()

        # Verify only 2 legs were submitted for the vertical spread
        self.assertEqual(len(submitted_legs), 2)
        quantities = [leg[1] for leg in submitted_legs]
        self.assertIn(1, quantities)
        self.assertIn(-1, quantities)

    def test_incubator_specs_includes_vertical_spread_variants(self) -> None:
        """Incubator includes IC_V23 (Bull Put) and IC_V24 (Bear Call) vertical spreads."""
        specs = incubator.get_incubator_specs(include_verticals=True)
        bull_put = next((s for s in specs if s.get("structure") == "bull_put"), None)
        bear_call = next((s for s in specs if s.get("structure") == "bear_call"), None)
        self.assertIsNotNone(bull_put, "Bull Put vertical spread should be in incubator specs")
        self.assertIsNotNone(bear_call, "Bear Call vertical spread should be in incubator specs")
        self.assertTrue(bull_put["strategy_name"].startswith("IC_V23"))
        self.assertTrue(bear_call["strategy_name"].startswith("IC_V24"))

    def test_fetch_strikes_filters_by_specific_expiration(self) -> None:
        """Verify _fetch_strikes filters strikes to the specific target expiration, avoiding invalid strikes."""
        strat = iron_condor.SPXIronCondor()
        strat.parameters = dict(iron_condor.SPXIronCondor.parameters)
        strat._underlying = Mock()
        fake_chains = {
            "Chains": {
                "CALL": {
                    "2026-10-28": [800.0, 801.0, 802.0, 803.0, 804.0, 805.0],
                    "2026-11-28": [790.0, 800.0, 810.0, 820.0],
                },
                "PUT": {
                    "2026-10-28": [800.0, 801.0, 802.0, 803.0, 804.0, 805.0],
                    "2026-11-28": [790.0, 800.0, 810.0, 820.0],
                },
            }
        }
        # Without expiry parameter, fallback returns broker union or empty
        strat.get_strikes = Mock(return_value=[790.0, 800.0, 801.0, 802.0, 803.0, 804.0, 805.0, 810.0, 820.0])
        all_strikes = strat._fetch_strikes(fake_chains)
        self.assertEqual(len(all_strikes), 9)

        # With target expiration, only returns strikes that exist for that expiration
        exp_strikes = strat._fetch_strikes(fake_chains, expiry="2026-11-28")
        self.assertEqual(exp_strikes, [790.0, 800.0, 810.0, 820.0])
        self.assertNotIn(801.0, exp_strikes)

    def test_default_execution_run_times_schedule(self) -> None:
        """Verify execution schedule is 5m from 09:45-10:00 and 15m until 15:30."""
        expected_times = [
            time(9, 45), time(9, 50), time(9, 55), time(10, 0),
            time(10, 15), time(10, 30), time(10, 45),
            time(11, 0), time(11, 15), time(11, 30), time(11, 45),
            time(12, 0), time(12, 15), time(12, 30), time(12, 45),
            time(13, 0), time(13, 15), time(13, 30), time(13, 45),
            time(14, 0), time(14, 15), time(14, 30), time(14, 45),
            time(15, 0), time(15, 15), time(15, 30),
        ]
        self.assertEqual(iron_condor.DEFAULT_RUN_TIMES_LIST, expected_times)
        parsed_expanded = iron_condor.parse_run_times(incubator.DEFAULT_EXPANDED_RUN_TIMES)
        self.assertEqual(parsed_expanded, expected_times)

    def test_is_entry_window_active_slots_and_off_cycle_rejections(self) -> None:
        """Verify _is_entry_window matches 5m open & 15m intraday ticks and rejects off-cycle intervals."""
        strat = iron_condor.SPXIronCondor()
        strat.parameters = dict(iron_condor.SPXIronCondor.parameters)
        strat.initialize()

        # All 26 expected execution slots should be accepted
        expected_slots = [
            (9, 45), (9, 50), (9, 55), (10, 0),
            (10, 15), (10, 30), (10, 45),
            (11, 0), (11, 15), (11, 30), (11, 45),
            (12, 0), (12, 15), (12, 30), (12, 45),
            (13, 0), (13, 15), (13, 30), (13, 45),
            (14, 0), (14, 15), (14, 30), (14, 45),
            (15, 0), (15, 15), (15, 30),
        ]
        base_date = datetime(2026, 10, 30, tzinfo=timezone.utc)
        for h, m in expected_slots:
            tick_time = base_date.replace(hour=h, minute=m, second=5)
            self.assertTrue(
                strat._is_entry_window(tick_time),
                f"Tick at {h:02d}:{m:02d} should be within entry window",
            )

        # Off-cycle 5-minute ticks must be rejected
        off_cycle_slots = [
            (9, 40), (10, 5), (10, 10), (10, 20), (10, 25),
            (10, 35), (10, 40), (10, 50), (10, 55),
            (11, 5), (11, 10), (11, 20), (11, 25),
            (12, 5), (12, 10), (12, 20), (12, 25),
            (13, 5), (13, 10), (13, 20), (13, 25),
            (14, 5), (14, 10), (14, 20), (14, 25),
            (15, 5), (15, 10), (15, 20), (15, 25),
            (15, 35), (15, 40), (16, 0),
        ]
        for h, m in off_cycle_slots:
            tick_time = base_date.replace(hour=h, minute=m, second=0)
            self.assertFalse(
                strat._is_entry_window(tick_time),
                f"Off-cycle tick at {h:02d}:{m:02d} should NOT be in entry window",
            )


if __name__ == "__main__":
    unittest.main()
