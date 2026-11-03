"""Unit tests for post-market continual learner and Champion/Challenger shadow promotion gate (ML-08)."""

from __future__ import annotations

import json
import tempfile
import unittest
from unittest.mock import patch
from pathlib import Path

from ml.continual_learner import ContinualLearner, evaluate_model_on_examples, purged_walk_forward_split


class ContinualLearnerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.root = Path(self.temp_dir.name)
        self.trajectories = self.root / "trajectories.jsonl"
        self.models_dir = self.root / "models"

        # Build synthetic trajectories across 3 sessions
        lines: list[str] = []
        sessions = ["2026-10-26", "2026-10-27", "2026-10-28"]
        for s_idx, session in enumerate(sessions):
            for tick in range(15):
                rec_id = f"{session}:{tick:02d}:SNAPSHOT"
                snap = {
                    "schema_version": 1,
                    "event": "SNAPSHOT",
                    "session": session,
                    "record_id": rec_id,
                    "timestamp": f"{session}T14:{tick * 5:02d}:00+00:00",
                    "complete": True,
                    "market": {
                        "vix": 16.0 + s_idx,
                        "vix_term_ratio": 0.95,
                        "vwap_distance": 0.001 * (tick - 7),
                        "orb_30m_range": 0.003,
                        "atr_pct": 0.002,
                        "rsi_14": 50.0 + tick,
                        "price_velocity_15m": 0.0005,
                        "minutes_to_close": 390 - tick * 5,
                    },
                    "grid": {
                        "pnl_dispersion": tick * 5.0,
                        "put_pressure": 0.1,
                        "call_pressure": 0.1,
                        "delta_net": 0.0,
                        "gamma_net": 0.0,
                        "theta_net": 10.0,
                        "vega_net": -5.0,
                    },
                    "strategies": {
                        "IC_V01": {"trajectory_id": f"traj_{s_idx}_{tick}"},
                        "IC_V02": {"trajectory_id": f"traj_{s_idx}_{tick}"},
                    },
                }
                label = {
                    "schema_version": 1,
                    "event": "HINDSIGHT_LABEL",
                    "source_id": rec_id,
                    "session": session,
                    "best_strategy": "IC_V01" if tick % 2 == 0 else "IC_V02",
                    "strategies": {
                        "IC_V01": {"action": "HOLD", "profit_target": 0.35},
                        "IC_V02": {"action": "TRIM_POSITION", "profit_target": 0.45},
                    },
                }
                lines.append(json.dumps(snap))
                lines.append(json.dumps(label))

            term = {
                "schema_version": 1,
                "event": "SESSION_END",
                "session": session,
                "timestamp": f"{session}T20:00:00+00:00",
                "strategies": {
                    "IC_V01": {"trajectory_id": f"traj_{s_idx}_14", "net_pnl": 120.0},
                    "IC_V02": {"trajectory_id": f"traj_{s_idx}_14", "net_pnl": 80.0},
                },
            }
            lines.append(json.dumps(term))

        self.trajectories.write_text("\n".join(lines) + "\n", encoding="utf-8")

    def test_purged_walk_forward_split(self) -> None:
        sessions = ["2026-10-26", "2026-10-27", "2026-10-28", "2026-10-29", "2026-10-30"]
        train, purge, val = purged_walk_forward_split(sessions, val_fraction=0.4, purge_count=1)
        self.assertEqual(val, ["2026-10-29", "2026-10-30"])
        self.assertEqual(purge, ["2026-10-28"])
        self.assertEqual(train, ["2026-10-26", "2026-10-27"])

        # Edge cases
        self.assertEqual(purged_walk_forward_split([]), ([], [], []))
        self.assertEqual(purged_walk_forward_split(["2026-10-26"]), (["2026-10-26"], [], []))
        self.assertEqual(purged_walk_forward_split(["2026-10-26", "2026-10-27"]), (["2026-10-26"], [], ["2026-10-27"]))

    def test_continual_learning_bootstrap_and_promotion(self) -> None:
        learner = ContinualLearner(
            trajectories_path=self.trajectories,
            models_dir=self.models_dir,
            sequence_length=4,
        )

        # Initial run: should bootstrap Champion
        result = learner.run_continual_learning(epochs=5, force_promote=True)
        self.assertEqual(result["status"], "PROMOTED")
        self.assertEqual(result["version"], 1)
        self.assertTrue((self.models_dir / "policy.json").exists())

        lineage = learner.load_lineage()
        self.assertEqual(lineage["active_champion_version"], 1)
        self.assertEqual(len(lineage["evaluations"]), 1)

        # Second run: shadow evaluation against champion
        result2 = learner.run_continual_learning(epochs=5)
        self.assertIn(result2["status"], ("PROMOTED", "REJECTED_SHADOW_RETAINED"))
        lineage2 = learner.load_lineage()
        self.assertEqual(len(lineage2["evaluations"]), 2)

    def test_rollback_functionality(self) -> None:
        learner = ContinualLearner(
            trajectories_path=self.trajectories,
            models_dir=self.models_dir,
            sequence_length=4,
        )
        # Run 1: Champion v1
        learner.run_continual_learning(epochs=5, force_promote=True)
        # Run 2: Force promote Champion v2 (which archives v1)
        learner.run_continual_learning(epochs=5, force_promote=True)

        lineage = learner.load_lineage()
        self.assertEqual(lineage["active_champion_version"], 2)

        # Check archive exists
        archives = list((self.models_dir / "archive").glob("*_v1.json"))
        self.assertTrue(len(archives) >= 1)

        # Rollback to v1
        rolled = learner.rollback(1)
        self.assertTrue(rolled)
        lineage_after = learner.load_lineage()
        self.assertEqual(lineage_after["active_champion_version"], 1)
        self.assertIn("rollback", str(lineage_after.get("active_champion_status", "")))

        # Subsequent promotion must preserve numeric version arithmetic (no TypeError: str + int)
        result_after_rollback = learner.run_continual_learning(epochs=5, force_promote=True)
        self.assertEqual(result_after_rollback["status"], "PROMOTED")
        self.assertEqual(result_after_rollback["version"], 2)

    def test_rejects_nonfinite_challenger_loss(self) -> None:
        learner = ContinualLearner(
            trajectories_path=self.trajectories,
            models_dir=self.models_dir,
            sequence_length=4,
        )
        # Bootstrap champion
        learner.run_continual_learning(epochs=5, force_promote=True)

        # Mock challenger metrics to have non-finite loss
        with patch("ml.continual_learner.evaluate_model_on_examples") as mock_eval:
            mock_eval.side_effect = [
                {"loss": float("nan"), "action_accuracy": 0.9}, # challenger
                {"loss": 0.5, "action_accuracy": 0.8},          # champion
            ]
            result = learner.run_continual_learning(epochs=1)
            self.assertEqual(result["status"], "REJECTED_SHADOW_RETAINED")
            self.assertTrue(any("non-finite" in r for r in result["reasons"]))

    def test_requires_disjoint_train_and_val_sets(self) -> None:
        # Create trajectories file with only 1 session (cannot split into disjoint train + val with purge)
        one_session_traj = self.root / "one_session.jsonl"
        lines = [line for line in self.trajectories.read_text(encoding="utf-8").splitlines() if "2026-10-26" in line]
        one_session_traj.write_text("\n".join(lines) + "\n", encoding="utf-8")

        learner = ContinualLearner(
            trajectories_path=one_session_traj,
            models_dir=self.models_dir,
            sequence_length=4,
        )
        result = learner.run_continual_learning(epochs=1)
        self.assertEqual(result["status"], "REJECTED_INSUFFICIENT_DISJOINT_DATA")
        self.assertTrue(any("disjoint" in r.lower() for r in result["reasons"]))


if __name__ == "__main__":
    unittest.main()
