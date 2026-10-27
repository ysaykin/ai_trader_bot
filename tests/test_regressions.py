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

        self.get_broker({
            "TRADING_BROKER": "tradier",
            "TRADIER_API_KEY": "test-token",
            "TRADIER_ACCOUNT": "VA21359019",
            "TRADIER_PAPER": "false",
            "TRADIER_IS_PAPER": "true",
            "TRADIER_SANDBOX": "1",
        })
        self.assertFalse(entrypoint._test_brokers.Tradier.call_args.args[0]["PAPER"])

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
            patch.object(entrypoint, "_run_once", side_effect=[RuntimeError("boom"), None]) as run_once,
            patch.object(entrypoint.time, "sleep") as sleep,
            patch.dict(os.environ, {"PROCESS_RESTART_BACKOFF_SECONDS": "0.25"}, clear=True),
        ):
            entrypoint.run()
        self.assertEqual(run_once.call_count, 2)
        sleep.assert_called_once_with(0.25)

        with patch.object(entrypoint, "_run_once", side_effect=KeyboardInterrupt) as run_once:
            with self.assertRaises(KeyboardInterrupt):
                entrypoint.run()
        run_once.assert_called_once()

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
    ) -> None:
        assets = [
            SimpleNamespace(
                asset_type="option", symbol="XSP", expiration=expiration,
                strike=strike, right=right,
            )
            for strike, right in ((95, "put"), (100, "put"), (110, "call"), (115, "call"))
        ]
        signed = (quantity, -quantity, -quantity, quantity)
        package_id = f"filled-{id(self)}-{quantity}-{len(self.strategy._position_ledger.open_baskets())}"
        legs = []
        for index, (asset, signed_quantity) in enumerate(zip(assets, signed), start=1):
            key = iron_condor.asset_key(asset)
            self.strategy._asset_registry[key] = asset
            legs.append(iron_condor.LedgerLeg(f"{package_id}:{index}", key, signed_quantity))
        self.strategy._position_ledger.record_submission(
            package_id, self.strategy._strategy_id(), "open", legs
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
        self.assertEqual(self.strategy._entry_credit, 0.0)
        with patch("time.sleep"):
            for _ in range(7):
                self.strategy.on_trading_iteration()
        self.assertTrue(self.strategy._position_open)
        self.strategy.submit_order.assert_not_called()
        self.strategy.before_market_closes()
        self.strategy.submit_order.assert_called_once()
        for order in self.strategy._closing_orders:
            order.is_active.return_value = False
            self.strategy.on_filled_order(None, order, 1.0, order.quantity, 100)
        self.strategy.get_positions.return_value = []
        self.strategy.on_trading_iteration()
        self.assertFalse(self.strategy._position_open)

    def test_close_submission_keeps_position_active_and_avoids_duplicates(self) -> None:
        self.open_filled_position()
        self.strategy._close_all_options()
        self.assertTrue(self.strategy._position_open)
        self.strategy._close_all_options()
        self.strategy.submit_order.assert_called_once()

    def test_missing_quote_cannot_trigger_a_false_profit_target(self) -> None:
        self.open_filled_position()
        self.strategy.get_quote.side_effect = None
        self.strategy.get_quote.return_value = None
        self.strategy._manage_position()
        self.strategy.submit_order.assert_not_called()
        self.assertTrue(self.strategy._position_open)

    def test_missing_credit_is_not_replaced_with_an_estimate(self) -> None:
        self.open_filled_position()
        self.strategy._entry_credit = 0.0
        self.strategy._estimate_credit = Mock(return_value=0.5)
        self.strategy._manage_position()
        self.assertEqual(self.strategy._entry_credit, 0.0)
        self.strategy._estimate_credit.assert_not_called()
        self.assertTrue(self.strategy._position_open)

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
        self.strategy.parameters.update(wing_width=2, quantity=1)
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
        self.assertEqual(strat.parameters["quantity"], 2)
        self.assertEqual(sorted(q for _, q in strat._owned_legs), [-2, -2, 2, 2])
        self.assertAlmostEqual(strat._entry_credit, 0.80)
        self.assertAlmostEqual(strat._entry_credit_cash, 160.0)

        # 3. TRIM_POSITION (50% scale-out) submits an atomic debit package; ownership updates on fills
        res = strat.apply_policy_action("TRIM_POSITION")
        self.assertEqual(res, "TRIM_POSITION")
        self.assertEqual(strat.parameters["quantity"], 2)
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
        strat.parameters["wing_width"] = 10.0  # $1,000 per contract -> 2 contracts = $2,000 > $1,500 (30% of $5k)
        res = strat.apply_policy_action("ADD_POSITION")
        self.assertEqual(res, "HOLD")
        self.assertEqual(strat.parameters["quantity"], 1)


if __name__ == "__main__":
    unittest.main()
