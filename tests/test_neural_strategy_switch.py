"""Tests for neural strategy switching and multi-slot entry time schedule parsing (bug-04)."""

from __future__ import annotations

import os
import tempfile
import unittest
from datetime import time
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


if __name__ == "__main__":
    unittest.main()
