"""Tests for neural strategy switching and multi-slot entry time schedule parsing (bug-04)."""

from __future__ import annotations

import os
import tempfile
import unittest
from datetime import datetime, time, timezone
from unittest.mock import Mock, patch

from strategies.iron_condor import SPXIronCondor, parse_run_times
from strategies.position_ledger import PositionLedger


class TestNeuralStrategySwitch(unittest.TestCase):
    def setUp(self) -> None:
        self.tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmpdir.cleanup)
        self.ledger = PositionLedger(os.path.join(self.tmpdir.name, "test_ledger.sqlite3"))
        self.addCleanup(self.ledger._connection.close)

        self.strategy = SPXIronCondor.__new__(SPXIronCondor)
        self.strategy._name = "IC_V22_NEURAL_PURE"
        self.strategy.parameters = {
            "symbol": "SPX",
            "quantity": 1,
            "structure": "iron_condor",
            "wing_width": 5.0,
            "short_delta": 0.15,
            "dte": 0,
            "profit_target": 0.50,
            "stop_loss_mult": 2.0,
            "entry_time": "09:45",
        }
        self.strategy.structure = "iron_condor"
        self.strategy._position_ledger = self.ledger
        self.strategy._entry_times = [time(9, 45)]
        self.strategy._entry_time = time(9, 45)

    def test_apply_neural_strategy_switch_with_comma_separated_schedule(self) -> None:
        # Incubator specs typically have multi-slot comma-separated entry times
        # e.g. "09:45, 10:00, 10:15, 10:30, 11:00"
        schedule_str = "09:45, 10:00, 10:15, 10:30, 11:00"
        mock_spec = {
            "strategy_name": "IC_V01_0DTE_D10_W2_PT30_T0945",
            "short_delta": 0.10,
            "wing_width": 2.0,
            "dte": 0,
            "profit_target": 0.30,
            "stop_loss_mult": 1.5,
            "structure": "iron_condor",
            "entry_time": schedule_str,
        }

        with patch("strategies.incubator.get_incubator_specs", return_value=[mock_spec]):
            # This must not raise ValueError
            self.strategy._apply_neural_strategy_switch("IC_V01_0DTE_D10_W2_PT30_T0945")

        self.assertEqual(self.strategy.parameters["short_delta"], 0.10)
        self.assertEqual(self.strategy.parameters["wing_width"], 2.0)
        self.assertEqual(self.strategy.parameters["stop_loss_mult"], 1.5)

        # Check both _entry_times and _entry_time are updated
        expected_times = [time(9, 45), time(10, 0), time(10, 15), time(10, 30), time(11, 0)]
        self.assertEqual(self.strategy._entry_times, expected_times)
        self.assertEqual(self.strategy._entry_time, time(9, 45))

    def test_apply_neural_strategy_switch_blocked_when_panic_halted(self) -> None:
        self.ledger.set_panic_halt(True)
        initial_delta = self.strategy.parameters["short_delta"]

        mock_spec = {
            "strategy_name": "IC_V01_0DTE_D10_W2_PT30_T0945",
            "short_delta": 0.10,
            "wing_width": 2.0,
            "dte": 0,
            "profit_target": 0.30,
            "stop_loss_mult": 1.5,
            "structure": "iron_condor",
            "entry_time": "10:00",
        }

        with patch("strategies.incubator.get_incubator_specs", return_value=[mock_spec]):
            self.strategy._apply_neural_strategy_switch("IC_V01_0DTE_D10_W2_PT30_T0945")

        # Parameters must remain unchanged because switch was blocked by panic halt
        self.assertEqual(self.strategy.parameters["short_delta"], initial_delta)

    def test_pure_neural_blocks_entries_when_controller_absent(self) -> None:
        self.strategy.pure_neural_mode = True
        self.strategy._policy_controller = None
        self.strategy._position_open = False
        self.strategy._current_datetime = Mock(return_value=datetime(2026, 11, 2, 10, 0, tzinfo=timezone.utc))
        self.strategy._run_entry_rules = Mock()

        # on_trading_iteration must return early and not run entry rules
        self.strategy.on_trading_iteration()
        self.strategy._run_entry_rules.assert_not_called()

    def test_main_does_not_attach_controller_when_ml_policy_disabled(self) -> None:
        from main import build_strategies_for_mode
        from pathlib import Path

        dummy_model = Path(self.tmpdir.name) / "policy.json"
        dummy_model.write_text("{}")

        pure_neural = SPXIronCondor.__new__(SPXIronCondor)
        pure_neural._name = "IC_V22_NEURAL_PURE"
        pure_neural.pure_neural_mode = True
        pure_neural.parameters = {"pure_neural_mode": True}

        with patch("strategies.incubator.build_incubator_strategies", return_value=[pure_neural]), \
             patch.dict(os.environ, {
                 "ML_POLICY_ENABLED": "false",
                 "ML_MODEL_PATH": str(dummy_model),
                 "POSITION_LEDGER_PATH": str(Path(self.tmpdir.name) / "ledger.sqlite3"),
             }):
            broker = Mock()
            broker.name = "Tradier"
            strategies = build_strategies_for_mode(broker, mode="incubator")
            for strat in strategies:
                self.assertIsNone(getattr(strat, "_policy_controller", None))


if __name__ == "__main__":
    unittest.main()
