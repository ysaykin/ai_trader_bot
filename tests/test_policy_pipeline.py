"""Tests for offline policy training and opt-in live inference."""

from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from ml.features import GREEKS, MARKET_TZ
from ml.inference import LivePolicyController
from ml.train import FEATURE_PATHS, train_policy
from trade_logger import TrajectoryLogger, relabel_session


class PolicyPipelineTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.trajectories = self.root / "trajectories.jsonl"
        self.model = self.root / "policy.json"
        snapshots = []
        labels = []
        for index in range(3):
            record_id = f"2026-10-26:{index}:SNAPSHOT"
            snapshots.append(
                {
                    "schema_version": 1,
                    "event": "SNAPSHOT",
                    "session": "2026-10-26",
                    "record_id": record_id,
                    "timestamp": f"2026-10-26T14:{index * 5:02}:00+00:00",
                    "complete": True,
                    "market": {"vix": None if index == 0 else 18 + index, "minutes_to_close": 120 - index * 5},
                    "grid": {"pnl_dispersion": index * 10},
                    "strategies": {"alpha": {}, "beta": {}},
                }
            )
            labels.append(
                {
                    "schema_version": 1,
                    "event": "HINDSIGHT_LABEL",
                    "source_id": record_id,
                    "best_strategy": "alpha" if index < 2 else "beta",
                    "strategies": {
                        "alpha": {"action": "HOLD", "profit_target": 0.3},
                        "beta": {"action": "FLATTEN_NOW", "profit_target": 0.6},
                    },
                }
            )
        self.trajectories.write_text(
            "".join(json.dumps(row) + "\n" for row in [*snapshots, *labels]),
            encoding="utf-8",
        )
        self.snapshots = snapshots

    def test_trainer_writes_schema_complete_json_artifact_and_reduces_loss(self) -> None:
        one_epoch = train_policy(
            self.trajectories, self.model, sequence_length=2, regime_size=4, epochs=1, seed=7
        )
        trained = train_policy(
            self.trajectories, self.model, sequence_length=2, regime_size=4, epochs=15, seed=7
        )
        persisted = json.loads(self.model.read_text(encoding="utf-8"))
        self.assertEqual(persisted["schema_version"], 1)
        self.assertEqual(persisted["feature_names"], list(FEATURE_PATHS))
        self.assertEqual(trained["training"]["examples"], 3)
        self.assertLess(trained["training"]["final_loss"], one_epoch["training"]["final_loss"])
        self.assertEqual(len(persisted["normalization"]["impute"]), len(FEATURE_PATHS))

    def test_live_bridge_imputes_missing_values_queues_action_and_resets_on_new_session(self) -> None:
        train_policy(
            self.trajectories, self.model, sequence_length=2, regime_size=4, epochs=1
        )
        strategies = {"alpha": SimpleNamespace(), "beta": SimpleNamespace()}
        controller = LivePolicyController(
            self.model, strategies, expected_sequence_length=2
        )
        self.assertIsNone(controller.on_snapshot(self.snapshots[0]))
        recommendation = controller.on_snapshot(self.snapshots[1])
        self.assertIsNotNone(recommendation)
        selected = strategies[recommendation.strategy_name]
        self.assertEqual(selected._queued_policy_action[0], recommendation.action)

        next_session_tick = dict(self.snapshots[2], session="2026-10-27")
        self.assertIsNone(controller.on_snapshot(next_session_tick))

    def test_missing_or_incompatible_artifact_fails_closed(self) -> None:
        with self.assertRaises(FileNotFoundError):
            LivePolicyController(self.model)
        self.model.write_text('{"schema_version": 999}', encoding="utf-8")
        with self.assertRaises(ValueError):
            LivePolicyController(self.model)
        train_policy(
            self.trajectories, self.model, sequence_length=2, regime_size=4, epochs=1
        )
        with self.assertRaises(ValueError):
            LivePolicyController(self.model, expected_sequence_length=12)

    def test_trajectory_callback_and_profit_target_relabeling_feed_trainer(self) -> None:
        log_path = self.root / "live_trajectories.jsonl"
        observed: list[tuple[str, int]] = []

        def on_completed(row: dict) -> None:
            lines = log_path.read_text(encoding="utf-8").splitlines()
            observed.append((row["record_id"], len(lines)))

        legs = [
            (SimpleNamespace(symbol="XSP", right=right, strike=strike, expiration="2026-10-26"), signed)
            for right, strike, signed in (("put", 95, 1), ("put", 100, -1), ("call", 110, -1), ("call", 115, 1))
        ]
        strategy = SimpleNamespace(
            name="alpha",
            parameters={"quantity": 1, "wing_width": 5, "max_position_risk": 5000},
            _underlying="XSP",
            _owned_legs=legs,
            _entry_orders=[],
            _entry_credit=2.0,
            _position_open=True,
            _exit_requested=False,
            get_historical_prices=Mock(return_value=None),
            get_greeks=Mock(return_value=dict.fromkeys(GREEKS, 0.1)),
        )

        def mark(pnl: float) -> None:
            strategy._fetch_last_price = Mock(
                side_effect=lambda asset: (
                    {"VIX": 20.0, "VIX9D": 22.0, "XSP": 105.0}[asset]
                    if isinstance(asset, str)
                    else (1.25 - pnl / 200.0 if asset.strike in (100, 110) else 0.25)
                )
            )

        with patch.dict("os.environ", {}, clear=True):
            collector = TrajectoryLogger(["alpha"], log_path, completed_snapshot_callback=on_completed)
            for minute, pnl in ((50, 0.0), (55, 50.0)):
                strategy.get_datetime = Mock(return_value=datetime(2026, 10, 26, 15, minute, tzinfo=MARKET_TZ))
                mark(pnl)
                row = collector.record(strategy, "HOLD")
                self.assertIsNotNone(row)
            strategy._position_open = False
            strategy._owned_legs = []
            strategy.get_datetime = Mock(return_value=datetime(2026, 10, 26, 16, 0, tzinfo=MARKET_TZ))
            mark(100.0)
            collector.record(strategy, "HOLD", session_end=True)

        self.assertEqual(observed[0][1], 1)
        self.assertEqual(relabel_session("2026-10-26", log_path), 0)
        rows = [json.loads(line) for line in log_path.read_text(encoding="utf-8").splitlines()]
        labels = [row for row in rows if row["event"] == "HINDSIGHT_LABEL"]
        self.assertEqual(len(labels), 2)
        self.assertEqual(labels[0]["strategies"]["alpha"]["profit_target"], 0.5)


if __name__ == "__main__":
    unittest.main()
