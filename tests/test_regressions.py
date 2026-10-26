"""Offline regressions for broker selection and iron condor position safety."""

import importlib.util
import logging
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from types import ModuleType, SimpleNamespace
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
    strategy.Strategy = type("Strategy", (), {})
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
                })

    def test_explicit_alpaca_selection_wins_over_tradier_credentials(self) -> None:
        self.get_broker({
            "TRADING_BROKER": "alpaca", "TRADIER_API_KEY": "test-token",
            "ALPACA_API_KEY": "alpaca-key", "ALPACA_API_SECRET": "alpaca-secret",
        })
        entrypoint._test_brokers.Tradier.assert_not_called()
        entrypoint._test_brokers.Alpaca.assert_called_once()

    def test_explicit_tradier_requires_credentials(self) -> None:
        for credentials in ({}, {"TRADIER_API_KEY": "your_tradier_api_key_here"}):
            with self.subTest(credentials=credentials), self.assertRaises(RuntimeError):
                self.get_broker({"TRADING_BROKER": "tradier", **credentials})
        entrypoint._test_brokers.Tradier.assert_not_called()

    def test_paper_setting_has_explicit_precedence(self) -> None:
        for settings in ({"TRADIER_IS_PAPER": "false"}, {"TRADIER_SANDBOX": "0"}):
            with self.subTest(settings=settings):
                self.get_broker({
                    "TRADING_BROKER": "tradier", "TRADIER_API_KEY": "test-token",
                    "TRADIER_ACCOUNT": "VA21359019", **settings,
                })
                self.assertFalse(entrypoint._test_brokers.Tradier.call_args.args[0]["PAPER"])

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
        self.strategy.initialize()
        self.strategy.portfolio_value = 100_000.0
        self.strategy.get_datetime = Mock(return_value=datetime(2026, 10, 23, 14, tzinfo=timezone.utc))
        self.asset = SimpleNamespace(asset_type="option", symbol="XSP")
        self.position = SimpleNamespace(asset=self.asset, quantity=-1)
        self.strategy.get_positions = Mock(return_value=[self.position])
        self.strategy.get_orders = Mock(return_value=[])
        self.strategy.get_last_price = Mock(return_value=1.0)
        self.strategy.create_order = Mock(side_effect=lambda asset, quantity, side: Mock(
            asset=asset, quantity=quantity, side=side, is_active=Mock(return_value=True),
        ))
        self.strategy.submit_order = Mock()
        self.strategy.cancel_order = Mock()
        self.strategy._position_open = True

    def test_zero_credit_does_not_access_missing_framework_counter(self) -> None:
        with patch("time.sleep"):
            self.strategy.on_trading_iteration()
        self.assertTrue(self.strategy._position_open)

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
        self.strategy._trajectory_logger = Mock()
        self.strategy._trajectory_logger.record.side_effect = OSError("disk full")
        self.strategy._manage_position = Mock()
        self.strategy.on_trading_iteration()
        self.strategy._manage_position.assert_called_once()
        self.assertTrue(self.strategy._position_open)

    def test_retry_exhaustion_keeps_contracts_managed_until_closed(self) -> None:
        with patch("time.sleep"):
            for _ in range(7):
                self.strategy.on_trading_iteration()
        self.assertTrue(self.strategy._position_open)
        self.strategy.submit_order.assert_called_once()
        self.strategy.before_market_closes()
        self.strategy.submit_order.assert_called_once()
        self.strategy.get_positions.return_value = []
        for order in self.strategy._closing_orders:
            order.is_active.return_value = False
        self.strategy.on_trading_iteration()
        self.assertFalse(self.strategy._position_open)

    def test_close_submission_keeps_position_active_and_avoids_duplicates(self) -> None:
        self.strategy._entry_credit = 1.0
        self.strategy._close_all_options()
        self.assertTrue(self.strategy._position_open)
        self.strategy._close_all_options()
        self.strategy.submit_order.assert_called_once()

    def test_missing_quote_cannot_trigger_a_false_profit_target(self) -> None:
        self.strategy._entry_credit = 1.0
        self.strategy.get_last_price.return_value = None
        self.strategy._manage_position()
        self.strategy.submit_order.assert_not_called()
        self.assertTrue(self.strategy._position_open)

    def test_missing_credit_is_refreshed_on_next_iteration(self) -> None:
        self.strategy._entry_orders = [Mock(asset=Mock(), quantity=2) for _ in range(4)]
        self.strategy._estimate_credit = Mock(return_value=0.5)
        self.strategy._manage_position()
        self.assertEqual(self.strategy._entry_credit, 0.5)
        self.strategy._estimate_credit.assert_called_once_with(
            *(order.asset for order in self.strategy._entry_orders), quantity=2,
        )

    def test_pending_entry_order_prevents_reset_when_no_positions_visible(self) -> None:
        self.strategy.get_positions.return_value = []
        self.strategy.get_orders.return_value = [Mock(is_active=Mock(return_value=True))]
        self.strategy._entry_credit = 1.0
        self.strategy._manage_position()
        self.assertTrue(self.strategy._position_open)

    def test_exit_cancels_pending_entry_even_before_positions_appear(self) -> None:
        order = Mock(is_active=Mock(return_value=True))
        self.strategy._entry_orders = [order]
        self.strategy._exit_requested = True
        self.strategy.get_positions.return_value = []
        self.strategy._manage_position()
        self.strategy.cancel_order.assert_called_once_with(order)
        self.assertTrue(self.strategy._position_open)

    def test_broker_read_failure_keeps_position_managed(self) -> None:
        self.strategy.get_positions.side_effect = RuntimeError("broker unavailable")
        self.strategy._manage_position()
        self.assertTrue(self.strategy._position_open)
        self.strategy.submit_order.assert_not_called()

    def test_failed_close_keeps_position_managed_for_safety_hooks(self) -> None:
        self.strategy.submit_order.side_effect = RuntimeError("broker unavailable")
        self.strategy._close_all_options()
        self.assertTrue(self.strategy._position_open)
        self.assertTrue(self.strategy._exit_requested)

    def test_partial_entry_failure_keeps_submitted_contracts_managed(self) -> None:
        self.strategy._position_open = False
        self.strategy._is_entry_window = Mock(return_value=True)
        self.strategy._check_allocation = Mock(return_value=True)
        self.strategy._find_expiration = Mock(return_value="2026-10-23")
        self.strategy._find_short_strikes = Mock(return_value=(580, 600))
        self.strategy.submit_order.side_effect = [None, RuntimeError("submission failed")]
        self.strategy.on_trading_iteration()
        self.assertTrue(self.strategy._position_open)
        self.assertTrue(self.strategy._entry_orders)

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
        self.strategy.parameters.update(wing_width=2, quantity=1)
        self.strategy.get_last_price.return_value = 49.0
        self.assertFalse(self.strategy._check_allocation())


class IncubatorAndMarketDataHubTests(unittest.TestCase):
    def setUp(self) -> None:
        self.environment = patch.dict(os.environ, {}, clear=True)
        self.environment.start()
        self.addCleanup(self.environment.stop)

    def test_incubator_defines_20_distinct_strategy_variants(self) -> None:
        hub = market_data_hub.MarketDataHub(ttl_seconds=240)
        strategies = incubator.build_incubator_strategies(broker=Mock(), market_data_hub=hub)
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
        strategies = incubator.build_incubator_strategies(broker=Mock(), market_data_hub=hub)
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
            strat.get_strikes = Mock(return_value=[570.0, 575.0, 580.0, 585.0, 590.0, 595.0, 600.0])
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
        strat_a = iron_condor.SPXIronCondor()
        strat_a.parameters = dict(iron_condor.SPXIronCondor.parameters)
        strat_a.initialize()
        strat_a._position_open = True

        leg_a = SimpleNamespace(asset_type="option", symbol="XSP", expiration="2026-10-26", strike=580.0, right="put")
        leg_b = SimpleNamespace(asset_type="option", symbol="XSP", expiration="2026-10-26", strike=570.0, right="put")
        strat_a._owned_legs = [(leg_a, -1)]

        pos_a = SimpleNamespace(asset=leg_a, quantity=-1)
        pos_b = SimpleNamespace(asset=leg_b, quantity=-2)
        strat_a.get_positions = Mock(return_value=[pos_a, pos_b])
        strat_a.get_orders = Mock(return_value=[])
        strat_a.create_order = Mock(side_effect=lambda asset, quantity, side: Mock(
            asset=asset, quantity=quantity, side=side, is_active=Mock(return_value=True),
        ))
        strat_a.submit_order = Mock()

        strat_a._close_all_options()
        # Only leg_a should be closed; leg_b belongs to another strategy variant
        strat_a.create_order.assert_called_once_with(leg_a, 1, "buy")
        strat_a.submit_order.assert_called_once()

    def test_apply_policy_action_respects_risk_guardrails_and_scales_legs(self) -> None:
        strat = iron_condor.SPXIronCondor()
        strat.parameters = dict(iron_condor.SPXIronCondor.parameters)
        strat.parameters.update(wing_width=2.0, quantity=1, max_position_risk=5000.0, max_allocation=1.0)
        strat.initialize()
        strat.portfolio_value = 100_000.0
        strat._position_open = True
        strat._entry_credit = 0.80
        legs = [
            (SimpleNamespace(asset_type="option", symbol="XSP", expiration="2026-10-26", strike=578.0, right="put"), 1),
            (SimpleNamespace(asset_type="option", symbol="XSP", expiration="2026-10-26", strike=580.0, right="put"), -1),
            (SimpleNamespace(asset_type="option", symbol="XSP", expiration="2026-10-26", strike=590.0, right="call"), -1),
            (SimpleNamespace(asset_type="option", symbol="XSP", expiration="2026-10-26", strike=592.0, right="call"), 1),
        ]
        strat._owned_legs = list(legs)
        strat.get_positions = Mock(return_value=[])
        strat.get_last_price = Mock(side_effect=lambda asset: 0.50 if asset.strike in (580.0, 590.0) else 0.10)
        strat.create_order = Mock(side_effect=lambda asset, quantity, side: Mock(
            asset=asset, quantity=quantity, side=side, is_active=Mock(return_value=True),
        ))
        strat.submit_order = Mock()

        # 1. Dynamic Profit Target clamping
        res = strat.apply_policy_action("ADJUST_PROFIT_TARGET", dynamic_profit_target=0.95)
        self.assertEqual(res, "ADJUST_PROFIT_TARGET")
        self.assertEqual(strat.parameters["profit_target"], 0.80)

        # 2. ADD_POSITION (+1 contract) succeeds when within $1,500 single-trade and $5,000 global limits
        res = strat.apply_policy_action("ADD_POSITION")
        self.assertEqual(res, "ADD_POSITION")
        self.assertEqual(strat.parameters["quantity"], 2)
        self.assertEqual([q for _, q in strat._owned_legs], [2, -2, -2, 2])
        self.assertAlmostEqual(strat._entry_credit, 1.60)

        # 3. TRIM_POSITION (50% scale-out) reduces quantity from 2 back to 1
        res = strat.apply_policy_action("TRIM_POSITION")
        self.assertEqual(res, "TRIM_POSITION")
        self.assertEqual(strat.parameters["quantity"], 1)
        self.assertEqual([q for _, q in strat._owned_legs], [1, -1, -1, 1])
        self.assertAlmostEqual(strat._entry_credit, 0.80)

        # 4. ADD_POSITION is blocked when single-trade risk would exceed RSK-02 limit
        strat.parameters["wing_width"] = 10.0  # $1,000 per contract -> 2 contracts = $2,000 > $1,500 (30% of $5k)
        res = strat.apply_policy_action("ADD_POSITION")
        self.assertEqual(res, "HOLD")
        self.assertEqual(strat.parameters["quantity"], 1)


if __name__ == "__main__":
    unittest.main()
