"""Tests for StrategyBro 90 DTE Dynamic Iron Condor vs Vertical strategy."""

from __future__ import annotations

import importlib.util
import logging
import os
from datetime import datetime, time, timedelta, timezone
from pathlib import Path
from types import ModuleType, SimpleNamespace
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]


def load_module(name: str, path: str) -> ModuleType:
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
    modules[name] = module
    with (
        patch.dict(os.environ, {}, clear=True),
        patch.dict("sys.modules", modules),
        patch("os.makedirs"),
        patch("logging.handlers.RotatingFileHandler"),
        patch.object(logging.getLogger(), "addHandler"),
        patch.object(logging.getLogger(), "setLevel"),
    ):
        spec.loader.exec_module(module)
    return module


iron_condor_math = load_module("iron_condor_math_under_test", "strategies/iron_condor_math.py")
position_ledger = load_module("position_ledger_under_test", "strategies/position_ledger.py")
iron_condor = load_module("iron_condor_under_test", "strategies/iron_condor.py")
incubator = load_module("incubator_under_test", "strategies/incubator.py")


class StrategyBroMathTests(unittest.TestCase):
    def test_entry_quality_classification(self) -> None:
        self.assertEqual(iron_condor_math.evaluate_strategybro_entry_quality(0.2), "EXCELLENT")
        self.assertEqual(iron_condor_math.evaluate_strategybro_entry_quality(0.5), "EXCELLENT")
        self.assertEqual(iron_condor_math.evaluate_strategybro_entry_quality(0.75), "GOOD")
        self.assertEqual(iron_condor_math.evaluate_strategybro_entry_quality(1.0), "GOOD")
        self.assertEqual(iron_condor_math.evaluate_strategybro_entry_quality(1.25), "MARGINAL")
        self.assertEqual(iron_condor_math.evaluate_strategybro_entry_quality(1.5), "MARGINAL")
        self.assertEqual(iron_condor_math.evaluate_strategybro_entry_quality(1.6), "AVOID")
        self.assertEqual(iron_condor_math.evaluate_strategybro_entry_quality(-0.4), "EXCELLENT")
        self.assertEqual(iron_condor_math.evaluate_strategybro_entry_quality(-1.8), "AVOID")

    def test_preference_rules_structure_resolution(self) -> None:
        # Abs SD <= 0.60 -> Iron Condor
        self.assertEqual(iron_condor_math.resolve_strategybro_structure(0.0), "iron_condor")
        self.assertEqual(iron_condor_math.resolve_strategybro_structure(0.3), "iron_condor")
        self.assertEqual(iron_condor_math.resolve_strategybro_structure(-0.55), "iron_condor")
        self.assertEqual(iron_condor_math.resolve_strategybro_structure(0.60), "iron_condor")

        # Abs SD > 0.60 & positive -> Bear Call Vertical (Sell Call Vertical)
        self.assertEqual(iron_condor_math.resolve_strategybro_structure(0.65), "bear_call")
        self.assertEqual(iron_condor_math.resolve_strategybro_structure(1.2), "bear_call")

        # Abs SD > 0.60 & negative -> Bull Put Vertical (Sell Put Vertical)
        self.assertEqual(iron_condor_math.resolve_strategybro_structure(-0.65), "bull_put")
        self.assertEqual(iron_condor_math.resolve_strategybro_structure(-1.2), "bull_put")

        # Abs SD > 1.50 -> Avoid zone (None)
        self.assertIsNone(iron_condor_math.resolve_strategybro_structure(1.51))
        self.assertIsNone(iron_condor_math.resolve_strategybro_structure(-2.0))

    def test_zscore_calculation(self) -> None:
        prices = [100.0, 102.0, 98.0, 101.0, 99.0]
        # mean = 100.0, std = sqrt(2.5) ~ 1.5811
        z = iron_condor_math.calculate_zscore(prices, 100.0)
        self.assertAlmostEqual(z, 0.0, places=3)

        z_high = iron_condor_math.calculate_zscore(prices, 103.16)
        self.assertAlmostEqual(z_high, 2.0, places=1)

        z_low = iron_condor_math.calculate_zscore(prices, 96.84)
        self.assertAlmostEqual(z_low, -2.0, places=1)

        # Empty or single price returns 0.0
        self.assertEqual(iron_condor_math.calculate_zscore([], 100.0), 0.0)
        self.assertEqual(iron_condor_math.calculate_zscore([100.0], 100.0), 0.0)

    def test_otm_pct_strike_selection(self) -> None:
        # SPX at 6000.0: 7% Put OTM = 5580.0, 7% Call OTM = 6420.0
        # Wing width = 15.0 pts
        strikes = [float(s) for s in range(5400, 6605, 5)]
        put, call = iron_condor_math.select_short_strikes_otm_pct(
            strikes=strikes,
            underlying_price=6000.0,
            otm_put_pct=0.07,
            otm_call_pct=0.07,
            wing_width=15.0,
        )
        self.assertEqual(put, 5580.0)
        self.assertEqual(call, 6420.0)
        # Check wings exist
        self.assertIn(put - 15.0, strikes)
        self.assertIn(call + 15.0, strikes)


class StrategyBroExecutionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.ledger = position_ledger.PositionLedger(":memory:")
        self.broker = SimpleNamespace(name="Tradier")

    def _build_strategy(self, **overrides):
        spec = incubator.get_strategybro_spec()
        spec.update(overrides)
        strategy = iron_condor.SPXIronCondor()
        strategy.name = spec["strategy_name"]
        strategy.parameters = {
            **iron_condor.SPXIronCondor.parameters,
            "position_ledger": self.ledger,
            **spec,
        }
        strategy.broker = self.broker
        strategy._position_ledger = self.ledger
        strategy.get_positions = Mock(return_value=[])
        strategy.get_orders = Mock(return_value=[])
        strategy._get_portfolio_value = Mock(return_value=100000.0)
        strategy.initialize()
        return strategy

    def test_spec_defaults_and_env_overrides(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            spec = incubator.get_strategybro_spec()
            self.assertEqual(spec["dte"], 90)
            self.assertEqual(spec["roll_dte"], 30)
            self.assertEqual(spec["wing_width"], 15.0)
            self.assertEqual(spec["profit_target"], 0.50)
            self.assertEqual(spec["stop_loss_mult"], 1.3)
            self.assertEqual(spec["otm_pct"], 0.07)
            self.assertEqual(spec["structure"], "dynamic_sd")
            self.assertTrue(spec["is_strategybro_variant"])

        with patch.dict(os.environ, {
            "STRATEGYBRO_DTE": "120",
            "STRATEGYBRO_ROLL_DTE": "45",
            "STRATEGYBRO_WING": "20.0",
            "STRATEGYBRO_PT": "0.60",
            "STRATEGYBRO_OTM_PCT": "0.08",
        }):
            overridden = incubator.get_strategybro_spec()
            self.assertEqual(overridden["dte"], 120)
            self.assertEqual(overridden["roll_dte"], 45)
            self.assertEqual(overridden["wing_width"], 20.0)
            self.assertEqual(overridden["profit_target"], 0.60)
            self.assertEqual(overridden["otm_pct"], 0.08)

    def test_incubator_specs_includes_strategybro_variant(self) -> None:
        specs = incubator.get_incubator_specs()
        sb = next((s for s in specs if s.get("is_strategybro_variant")), None)
        self.assertIsNotNone(sb, "StrategyBro variant must be present in incubator specs")
        self.assertEqual(sb["dte"], 90)
        self.assertEqual(sb["structure"], "dynamic_sd")

    def test_dynamic_selection_iron_condor_when_sd_low(self) -> None:
        strategy = self._build_strategy(force_sd=0.2)
        now = datetime(2026, 10, 28, 10, 30, tzinfo=timezone.utc)
        strategy.get_datetime = Mock(return_value=now)
        strategy.get_last_price = Mock(return_value=6000.0)
        strategy._fetch_chains = Mock(return_value={"Chains": {}})
        strategy._fetch_expirations = Mock(return_value=["2027-01-26"])
        strategy._fetch_strikes = Mock(return_value=[float(s) for s in range(5400, 6605, 5)])
        strategy._package_price = Mock(return_value=2.50)
        strategy._submit_package = Mock(return_value=[Mock(is_active=lambda: True)])

        strategy._run_trading_iteration()

        strategy._submit_package.assert_called_once()
        legs = strategy._submit_package.call_args[0][0]
        # Low SD -> Iron Condor has 4 legs
        self.assertEqual(len(legs), 4)

    def test_dynamic_selection_bear_call_when_sd_stretched_up(self) -> None:
        strategy = self._build_strategy(force_sd=0.85)
        now = datetime(2026, 10, 28, 10, 30, tzinfo=timezone.utc)
        strategy.get_datetime = Mock(return_value=now)
        strategy.get_last_price = Mock(return_value=6000.0)
        strategy._fetch_chains = Mock(return_value={"Chains": {}})
        strategy._fetch_expirations = Mock(return_value=["2027-01-26"])
        strategy._fetch_strikes = Mock(return_value=[float(s) for s in range(5400, 6605, 5)])
        strategy._package_price = Mock(return_value=1.50)
        strategy._submit_package = Mock(return_value=[Mock(is_active=lambda: True)])

        # Mock option asset creation to verify calls
        def make_mock_option(strike, expiry, right):
            return SimpleNamespace(strike=strike, expiration=expiry, right=right)
        strategy._make_option = Mock(side_effect=make_mock_option)

        strategy._run_trading_iteration()

        strategy._submit_package.assert_called_once()
        legs = strategy._submit_package.call_args[0][0]
        # Positive stretched SD -> Bear Call Vertical has 2 call legs
        self.assertEqual(len(legs), 2)
        rights = [leg[0].right for leg in legs]
        self.assertEqual(rights, ["call", "call"])

    def test_dynamic_selection_bull_put_when_sd_stretched_down(self) -> None:
        strategy = self._build_strategy(force_sd=-0.85)
        now = datetime(2026, 10, 28, 10, 30, tzinfo=timezone.utc)
        strategy.get_datetime = Mock(return_value=now)
        strategy.get_last_price = Mock(return_value=6000.0)
        strategy._fetch_chains = Mock(return_value={"Chains": {}})
        strategy._fetch_expirations = Mock(return_value=["2027-01-26"])
        strategy._fetch_strikes = Mock(return_value=[float(s) for s in range(5400, 6605, 5)])
        strategy._package_price = Mock(return_value=1.50)
        strategy._submit_package = Mock(return_value=[Mock(is_active=lambda: True)])

        def make_mock_option(strike, expiry, right):
            return SimpleNamespace(strike=strike, expiration=expiry, right=right)
        strategy._make_option = Mock(side_effect=make_mock_option)

        strategy._run_trading_iteration()

        strategy._submit_package.assert_called_once()
        legs = strategy._submit_package.call_args[0][0]
        # Negative stretched SD -> Bull Put Vertical has 2 put legs
        self.assertEqual(len(legs), 2)
        rights = [leg[0].right for leg in legs]
        self.assertEqual(rights, ["put", "put"])

    def test_dynamic_selection_avoids_entry_when_sd_too_high(self) -> None:
        strategy = self._build_strategy(force_sd=1.75)
        now = datetime(2026, 10, 28, 10, 30, tzinfo=timezone.utc)
        strategy.get_datetime = Mock(return_value=now)
        strategy.get_last_price = Mock(return_value=6000.0)
        strategy._fetch_chains = Mock(return_value={"Chains": {}})
        strategy._submit_package = Mock()

        strategy._run_trading_iteration()

        strategy._submit_package.assert_not_called()

    def test_roll_trigger_when_dte_reaches_30(self) -> None:
        strategy = self._build_strategy()
        now = datetime(2026, 10, 28, 12, 0, tzinfo=timezone.utc)
        strategy.get_datetime = Mock(return_value=now)
        strategy.get_orders = Mock(return_value=[])

        # Create an owned leg expiring in 25 days (<= 30 DTE roll threshold)
        exp_date = (now + timedelta(days=25)).strftime("%Y-%m-%d")
        asset = SimpleNamespace(
            symbol="XSP",
            asset_type="option",
            expiration=exp_date,
            strike=6000.0,
            right="call",
        )
        strategy._owned_legs = [(asset, -1)]
        strategy.get_positions = Mock(return_value=[SimpleNamespace(asset=asset, quantity=-1)])
        strategy._entry_credit_cash = 300.0
        strategy._position_open = True
        strategy._fetch_quote = Mock(return_value={"bid": 1.0, "ask": 1.2})
        strategy._close_all_options = Mock()

        strategy._manage_position()

        strategy._close_all_options.assert_called_once()
        self.assertEqual(
            strategy._close_all_options.call_args.kwargs.get("exit_reason"),
            "ROLL_DTE",
        )

    def test_run_times_parsing_and_configuration_within_strategy(self) -> None:
        strategy = self._build_strategy()
        # Test default configured expanded run times (every 5m 09:45-10:00, then 15m until 15:30)
        expected = [
            time(9, 45), time(9, 50), time(9, 55), time(10, 0),
            time(10, 15), time(10, 30), time(10, 45),
            time(11, 0), time(11, 15), time(11, 30), time(11, 45),
            time(12, 0), time(12, 15), time(12, 30), time(12, 45),
            time(13, 0), time(13, 15), time(13, 30), time(13, 45),
            time(14, 0), time(14, 15), time(14, 30), time(14, 45),
            time(15, 0), time(15, 15), time(15, 30),
        ]
        self.assertEqual(strategy.run_times, expected)

        # Configure within strategy via setter with flexible user string: "9:45, 10, 1015, and 1030"
        four_slot_expected = [time(9, 45), time(10, 0), time(10, 15), time(10, 30)]
        strategy.set_run_times("9:45, 10, 1015, and 1030")
        self.assertEqual(strategy.run_times, four_slot_expected)
        self.assertEqual(strategy.run_times_str, "09:45, 10:00, 10:15, 10:30")

        # Configure within strategy via property setter with list of strings
        strategy.run_times = ["09:45", "10:00", "10:15", "10:30"]
        self.assertEqual(strategy.run_times, four_slot_expected)

        # Restore default expanded schedule
        strategy.run_times = expected

        # Verify entry window triggers at all configured run times throughout the day up to 15:30
        for target_time in expected:
            dt = datetime(2026, 10, 28, target_time.hour, target_time.minute, tzinfo=timezone.utc)
            self.assertTrue(
                strategy._is_entry_window(dt),
                f"Entry window should be active at run time {target_time}",
            )

        # Verify outside windows: before open (09:30) and after 3:31pm cutoff (15:32 and 16:00)
        outside_before = datetime(2026, 10, 28, 9, 30, tzinfo=timezone.utc)
        outside_after_cutoff = datetime(2026, 10, 28, 15, 32, tzinfo=timezone.utc)
        outside_market_close = datetime(2026, 10, 28, 16, 0, tzinfo=timezone.utc)
        self.assertFalse(strategy._is_entry_window(outside_before))
        self.assertFalse(strategy._is_entry_window(outside_after_cutoff))
        self.assertFalse(strategy._is_entry_window(outside_market_close))

    def test_incubator_grid_covers_configured_run_times(self) -> None:
        """All incubator strategies (including IC_V25..IC_V29) run on the default screening schedule through 15:30."""
        grid = incubator.INCUBATOR_GRID_20
        run_times = {v["entry_time"] for v in grid}
        self.assertIn("15:30", list(run_times)[0])
        self.assertIn("09:45", list(run_times)[0])
        specs = incubator.get_incubator_specs()
        for s in specs:
            self.assertIn("15:30", str(s.get("entry_time", "")))


class DynamicSuiteStrategiesTests(unittest.TestCase):
    """Tests for IC_V26 (TrendFlow), IC_V27 (Skew Dynamic), IC_V28 (VolCrush), IC_V29 (ORB 1DTE), and MFE Ratchet."""

    def setUp(self) -> None:
        self.ledger = position_ledger.PositionLedger(":memory:")
        self.broker = SimpleNamespace(name="Tradier")

    def _build_from_spec(self, spec: dict, **overrides):
        merged_spec = {**spec, **overrides}
        strategy = iron_condor.SPXIronCondor()
        strategy.name = merged_spec["strategy_name"]
        strategy.parameters = {
            **iron_condor.SPXIronCondor.parameters,
            "position_ledger": self.ledger,
            **merged_spec,
        }
        strategy.broker = self.broker
        strategy._position_ledger = self.ledger
        strategy.get_positions = Mock(return_value=[])
        strategy.get_orders = Mock(return_value=[])
        strategy._get_portfolio_value = Mock(return_value=100000.0)
        strategy.initialize()
        return strategy

    def test_dynamic_suite_specs_registered_and_share_default_timing(self) -> None:
        specs = incubator.get_incubator_specs()
        names = [s["strategy_name"] for s in specs]
        self.assertTrue(any(n.startswith("IC_V26_") for n in names))
        self.assertTrue(any(n.startswith("IC_V27_") for n in names))
        self.assertTrue(any(n.startswith("IC_V28_") for n in names))
        self.assertTrue(any(n.startswith("IC_V29_") for n in names))

        for dyn_spec in incubator.get_dynamic_strategy_specs():
            self.assertEqual(dyn_spec["entry_time"], incubator.DEFAULT_EXPANDED_RUN_TIMES)
            self.assertTrue(dyn_spec["enable_mfe_ratchet"])

    def test_trendflow_v26_rides_momentum_and_skips_flat(self) -> None:
        # Math tests
        self.assertEqual(iron_condor_math.resolve_trendflow_structure(0.65), "bull_put")
        self.assertEqual(iron_condor_math.resolve_trendflow_structure(-0.65), "bear_call")
        self.assertIsNone(iron_condor_math.resolve_trendflow_structure(0.10))
        self.assertIsNone(iron_condor_math.resolve_trendflow_structure(2.80))

        # Execution: Bullish momentum -> Bull Put (2 put legs)
        strat = self._build_from_spec(incubator.get_trendflow_spec(), force_sd=0.80)
        now = datetime(2026, 10, 28, 10, 15, tzinfo=timezone.utc)
        strat.get_datetime = Mock(return_value=now)
        strat.get_last_price = Mock(return_value=600.0)
        strat._fetch_chains = Mock(return_value={"Chains": {}})
        strat._fetch_expirations = Mock(return_value=["2026-11-07"])
        strat._fetch_strikes = Mock(return_value=[float(s) for s in range(540, 665, 1)])
        strat._package_price = Mock(return_value=1.20)
        strat._submit_package = Mock(return_value=[Mock(is_active=lambda: True)])
        strat._make_option = Mock(side_effect=lambda strike, expiry, right: SimpleNamespace(strike=strike, expiration=expiry, right=right))

        strat._run_trading_iteration()
        strat._submit_package.assert_called_once()
        legs = strat._submit_package.call_args[0][0]
        self.assertEqual(len(legs), 2)
        self.assertEqual([leg[0].right for leg in legs], ["put", "put"])

        # Execution: Flat market (SD=0.10) -> skips entry
        strat_flat = self._build_from_spec(incubator.get_trendflow_spec(), force_sd=0.10)
        strat_flat.get_datetime = Mock(return_value=now)
        strat_flat.get_last_price = Mock(return_value=600.0)
        strat_flat._fetch_chains = Mock(return_value={"Chains": {}})
        strat_flat._fetch_expirations = Mock(return_value=["2026-11-07"])
        strat_flat._fetch_strikes = Mock(return_value=[float(s) for s in range(540, 665, 1)])
        strat_flat._submit_package = Mock()
        strat_flat._run_trading_iteration()
        strat_flat._submit_package.assert_not_called()

    def test_skew_dynamic_v27_shifts_put_and_call_deltas_with_drift(self) -> None:
        # Bullish drift -> closer put (0.20), farther call (0.10)
        self.assertEqual(iron_condor_math.resolve_skew_condor_deltas(0.50), (0.20, 0.10))
        # Bearish drift -> farther put (0.10), closer call (0.18)
        self.assertEqual(iron_condor_math.resolve_skew_condor_deltas(-0.50), (0.10, 0.18))
        # Neutral -> (0.16, 0.12)
        self.assertEqual(iron_condor_math.resolve_skew_condor_deltas(0.0), (0.16, 0.12))

        strikes = [float(s) for s in range(540, 665, 1)]
        put_bull, call_bull = iron_condor_math.select_short_strikes_skew(
            strikes, 600.0, 0.20, 0.10, 14, 5.0
        )
        # Put should be closer to 600 than Call
        self.assertLess(600.0 - put_bull, call_bull - 600.0)

        # Execution test
        strat = self._build_from_spec(incubator.get_skew_dynamic_spec(), force_sd=0.55)
        now = datetime(2026, 10, 28, 10, 15, tzinfo=timezone.utc)
        strat.get_datetime = Mock(return_value=now)
        strat.get_last_price = Mock(return_value=600.0)
        strat._fetch_chains = Mock(return_value={"Chains": {}})
        strat._fetch_expirations = Mock(return_value=["2026-11-11"])
        strat._fetch_strikes = Mock(return_value=strikes)
        strat._package_price = Mock(return_value=1.45)
        strat._submit_package = Mock(return_value=[Mock(is_active=lambda: True)])
        strat._make_option = Mock(side_effect=lambda strike, expiry, right: SimpleNamespace(strike=strike, expiration=expiry, right=right))

        strat._run_trading_iteration()
        strat._submit_package.assert_called_once()
        legs = strat._submit_package.call_args[0][0]
        self.assertEqual(len(legs), 4)
        self.assertEqual(strat._active_put_delta, 0.20)
        self.assertEqual(strat._active_call_delta, 0.10)

    def test_vol_crush_v28_waits_for_vol_expansion_and_harvests_spike(self) -> None:
        # Low vol & low SD -> None
        self.assertIsNone(iron_condor_math.resolve_vol_crush_structure(0.20, 0.95))
        # Sharp pullback -> Bull Put
        self.assertEqual(iron_condor_math.resolve_vol_crush_structure(-1.15, 1.35), "bull_put")
        # High RV within range -> Iron Condor
        self.assertEqual(iron_condor_math.resolve_vol_crush_structure(0.30, 1.35), "iron_condor")

        # Execution on sharp pullback
        strat = self._build_from_spec(
            incubator.get_vol_crush_spec(),
            force_sd=-1.10,
            force_rv_ratio=1.35,
        )
        now = datetime(2026, 10, 28, 11, 0, tzinfo=timezone.utc)
        strat.get_datetime = Mock(return_value=now)
        strat.get_last_price = Mock(return_value=600.0)
        strat._fetch_chains = Mock(return_value={"Chains": {}})
        strat._fetch_expirations = Mock(return_value=["2026-11-18"])
        strat._fetch_strikes = Mock(return_value=[float(s) for s in range(540, 665, 1)])
        strat._package_price = Mock(return_value=1.35)
        strat._submit_package = Mock(return_value=[Mock(is_active=lambda: True)])
        strat._make_option = Mock(side_effect=lambda strike, expiry, right: SimpleNamespace(strike=strike, expiration=expiry, right=right))

        strat._run_trading_iteration()
        strat._submit_package.assert_called_once()
        legs = strat._submit_package.call_args[0][0]
        self.assertEqual(len(legs), 2)
        self.assertEqual([leg[0].right for leg in legs], ["put", "put"])

    def test_orb_1dte_v29_adapts_to_intraday_breakout_or_range(self) -> None:
        self.assertEqual(iron_condor_math.resolve_orb_structure(0.0050), "bull_put")
        self.assertEqual(iron_condor_math.resolve_orb_structure(-0.0050), "bear_call")
        self.assertEqual(iron_condor_math.resolve_orb_structure(0.0010), "iron_condor")
        self.assertIsNone(iron_condor_math.resolve_orb_structure(0.030))

        # Execution: Morning breakout +0.45% -> enters Bull Put (never sells short calls into rally)
        strat = self._build_from_spec(
            incubator.get_orb_dynamic_spec(),
            force_intraday_return_pct=0.0045,
        )
        now = datetime(2026, 10, 28, 10, 30, tzinfo=timezone.utc)
        strat.get_datetime = Mock(return_value=now)
        strat.get_last_price = Mock(return_value=600.0)
        strat._fetch_chains = Mock(return_value={"Chains": {}})
        strat._fetch_expirations = Mock(return_value=["2026-10-29"])
        strat._fetch_strikes = Mock(return_value=[float(s) for s in range(540, 665, 1)])
        strat._package_price = Mock(return_value=0.95)
        strat._submit_package = Mock(return_value=[Mock(is_active=lambda: True)])
        strat._make_option = Mock(side_effect=lambda strike, expiry, right: SimpleNamespace(strike=strike, expiration=expiry, right=right))

        strat._run_trading_iteration()
        strat._submit_package.assert_called_once()
        legs = strat._submit_package.call_args[0][0]
        self.assertEqual(len(legs), 2)
        self.assertEqual([leg[0].right for leg in legs], ["put", "put"])

    def test_mfe_ratchet_prevents_winners_turning_into_losers(self) -> None:
        # Credit = $100.00. MFE reached $35.00 (35% >= 30% trigger), current net dropped to $0.00 -> BREAKEVEN_RATCHET
        self.assertEqual(
            iron_condor_math.evaluate_mfe_ratchet(100.0, 35.0, 0.0),
            "BREAKEVEN_RATCHET",
        )
        # Credit = $100.00. MFE reached $48.00 (48% >= 45% trigger), current net pulled back to $28.00 (<= 65% of 48 = 31.2) -> MFE_TRAIL_LOCK
        self.assertEqual(
            iron_condor_math.evaluate_mfe_ratchet(100.0, 48.0, 28.0),
            "MFE_TRAIL_LOCK",
        )
        # Still near peak ($42.00 > $31.20) -> None (keep holding toward full PT)
        self.assertIsNone(
            iron_condor_math.evaluate_mfe_ratchet(100.0, 48.0, 42.0),
        )


if __name__ == "__main__":
    unittest.main()
