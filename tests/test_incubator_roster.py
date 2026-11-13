"""Offline coverage for entry selection and retirement of held strategies."""

import json
import os
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from test_regressions import entrypoint, incubator, iron_condor, market_data_hub


class IncubatorRosterTests(unittest.TestCase):
    def setUp(self):
        environment = patch.dict(os.environ, {}, clear=True)
        environment.start()
        self.addCleanup(environment.stop)
        constructor = patch.object(incubator, "SPXIronCondor", iron_condor.SPXIronCondor)
        constructor.start()
        self.addCleanup(constructor.stop)

    def test_default_compares_distinct_dynamics_with_one_plain_control(self):
        specs = incubator.get_incubator_specs()
        self.assertEqual(
            [spec["strategy_name"].split("_")[1] for spec in specs],
            ["V25", "V26", "V27", "V28", "V29", "V22", "V19"],
        )
        self.assertEqual(len({spec["structure"] for spec in specs[:5]}), 5)
        self.assertEqual(sum(spec.get("structure", "iron_condor") == "iron_condor" and not spec.get("pure_neural_mode") for spec in specs), 1)
        self.assertFalse(any(spec.get("is_testing_variant") for spec in specs))

    def test_count_limits_keep_dynamic_peers_first(self):
        for profile in ("dynamic", "full_grid"):
            with self.subTest(profile=profile):
                self.assertTrue(incubator.get_incubator_specs(limit=1, profile=profile)[0]["is_strategybro_variant"])
                self.assertEqual([s["strategy_name"].split("_")[1] for s in incubator.get_incubator_specs(limit=5, profile=profile)], ["V25", "V26", "V27", "V28", "V29"])
                self.assertEqual(incubator.get_incubator_specs(limit=0, profile=profile), [])
                self.assertEqual(incubator.get_incubator_specs(limit=-1, profile=profile), [])

    def test_full_grid_and_static_test_opt_ins_remain_available(self):
        with patch.dict(os.environ, {"INCUBATOR_PROFILE": "full_grid"}):
            specs = incubator.get_incubator_specs()
        self.assertEqual(len(specs), 29)
        self.assertEqual(len({s["strategy_name"] for s in specs}), 29)
        self.assertTrue({s["strategy_name"] for s in incubator.INCUBATOR_GRID_20}.issubset({s["strategy_name"] for s in specs}))
        self.assertEqual(len(incubator.get_incubator_specs(include_testing=True, include_verticals=True)), 10)
        self.assertEqual(len(incubator.get_incubator_specs(profile="full_grid", include_testing=False, include_verticals=False)), 26)

    def test_enable_flags_and_global_schedule_are_honored(self):
        with patch.dict(os.environ, {"STRATEGYBRO_ENABLED": "false", "TRENDFLOW_ENABLED": "false", "NEURAL_STRATEGY_ENABLED": "false", "IC_ENTRY_TIME": "10:00, 14:00"}):
            specs = incubator.get_incubator_specs()
        self.assertEqual([s["strategy_name"].split("_")[1] for s in specs], ["V27", "V28", "V29", "V19"])
        self.assertTrue(all(s["entry_time"] == "10:00, 14:00" for s in specs))
        with patch.dict(os.environ, {"INCUBATOR_PROFILE": "typo"}):
            with self.assertRaisesRegex(ValueError, "INCUBATOR_PROFILE"):
                incubator.get_incubator_specs()

    def test_retired_owners_are_managed_beyond_entry_count_limit(self):
        retired_name = incubator.INCUBATOR_GRID_20[0]["strategy_name"]
        ledger = iron_condor.PositionLedger(":memory:")
        self.addCleanup(ledger.close)
        for index, name in enumerate((retired_name, retired_name, "IC_V26_10DTE_TRENDFLOW_DYNAMIC")):
            legs = [
                iron_condor.LedgerLeg(f"{index}-{strike}", json.dumps({"symbol": "XSP", "expiration": "2026-11-06", "right": "put", "strike": strike}), quantity)
                for strike, quantity in ((575.0, 1), (580.0, -1))
            ]
            ledger.record_submission(f"package-{index}", name, "open", legs, limit_price=1.0)
        with patch.dict(os.environ, {"TRENDFLOW_ENABLED": "false"}):
            strategies = incubator.build_incubator_strategies(broker=Mock(), limit=1, position_ledger=ledger)
        self.assertEqual(len(strategies), 3)
        self.assertTrue(strategies[0].parameters["is_strategybro_variant"])
        self.assertFalse(strategies[0].parameters.get("management_only", False))
        self.assertEqual({s.name for s in strategies[1:]}, {retired_name, "IC_V26_10DTE_TRENDFLOW_DYNAMIC"})
        for manager in strategies[1:]:
            self.assertTrue(manager.parameters["management_only"])
            self.assertIs(manager._position_ledger, ledger)
            self.assertIs(manager._host_strategy, strategies[0])
        ledger._connection.execute("UPDATE packages SET state = 'canceled'")
        self.assertEqual(len(incubator.build_incubator_strategies(broker=Mock(), limit=1, position_ledger=ledger)), 1)

    def test_recovery_preserves_owner_names_after_configuration_changes(self):
        ledger = Mock()
        ledger.open_baskets.return_value = [SimpleNamespace(strategy_id="IC_V25_90DTE_STRATEGYBRO_DYNAMIC")]
        with patch.dict(os.environ, {"STRATEGYBRO_DTE": "120", "STRATEGYBRO_ENABLED": "false"}):
            managers = incubator._get_management_specs([], ledger)
        self.assertEqual(managers[0]["strategy_name"], "IC_V25_90DTE_STRATEGYBRO_DYNAMIC")
        ledger.open_baskets.return_value = [SimpleNamespace(strategy_id="unknown-owner")]
        with self.assertRaisesRegex(ValueError, "unknown-owner"):
            incubator._get_management_specs([], ledger)


class ManagementOnlyTests(unittest.TestCase):
    def setUp(self):
        self.strategy = iron_condor.SPXIronCondor()
        self.strategy.parameters = {"management_only": True}
        self.strategy._position_ledger = Mock()
        self.strategy._position_ledger.is_panic_halted.return_value = False
        self.strategy._position_ledger.entry_cooldown_until.return_value = None
        self.strategy._position_ledger.open_positions.return_value = []
        self.strategy._reconcile_broker_state = Mock(return_value=True)
        self.strategy.get_datetime = Mock(return_value=datetime(2026, 11, 2, 15, 0, tzinfo=timezone.utc))
        self.strategy._manage_position = Mock()
        self.strategy._is_entry_window = Mock(return_value=True)

    def test_held_positions_continue_normal_management(self):
        self.strategy._position_states = {"held": iron_condor.PositionState(position_id="held", position_open=True)}
        self.strategy._run_trading_iteration()
        self.strategy._reconcile_broker_state.assert_called_once()
        self.strategy._manage_position.assert_called_once()

    def test_flat_retired_strategies_cannot_reenter_or_add_exposure(self):
        self.strategy._position_open = False
        self.strategy._run_trading_iteration()
        self.strategy._is_entry_window.assert_not_called()
        self.assertFalse(self.strategy._can_add_contracts())
        self.strategy._apply_neural_strategy_switch("IC_V25_90DTE_STRATEGYBRO_DYNAMIC")
        self.assertNotIn("_active_switched_strategy", self.strategy.__dict__)


class ComparisonPipelineTests(unittest.TestCase):
    def test_research_and_policy_use_entries_while_execution_retains_managers(self):
        entries = [SimpleNamespace(name=spec["strategy_name"], parameters=spec)
                   for spec in incubator.get_incubator_specs(profile="dynamic")]
        retired = SimpleNamespace(name="IC_V01_0DTE_D10_W2_PT30_T0945", parameters={"management_only": True})
        variants = [*entries, retired]
        controller = Mock()
        controller_type = Mock(return_value=controller)
        modules = {
            "strategies.iron_condor": iron_condor,
            "strategies.incubator": SimpleNamespace(build_incubator_strategies=Mock(return_value=variants), build_ab_strategies=Mock()),
            "strategies.market_data_hub": market_data_hub,
            "ml.inference": SimpleNamespace(LivePolicyController=controller_type),
        }
        with tempfile.TemporaryDirectory() as temporary, patch.dict("sys.modules", modules), \
             patch.dict(os.environ, {"ML_POLICY_ENABLED": "true", "POSITION_LEDGER_PATH": str(Path(temporary) / "ledger.sqlite3"),
                                     "TRAJECTORY_LOG_PATH": str(Path(temporary) / "research.jsonl")}, clear=True):
            strategies = entrypoint.build_strategies_for_mode(Mock(), mode="incubator")
        self.assertEqual(strategies, variants)
        self.assertEqual(entries[0]._trajectory_logger.names, tuple(s.name for s in entries))
        self.assertEqual(controller_type.call_args.args[1], {s.name: s for s in entries})
        self.assertTrue(all(s._trajectory_logger is entries[0]._trajectory_logger for s in entries))
        self.assertFalse(hasattr(retired, "_trajectory_logger"))
        self.assertFalse(hasattr(retired, "_policy_controller"))


if __name__ == "__main__":
    unittest.main()
