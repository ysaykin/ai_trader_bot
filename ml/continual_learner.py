"""Post-market continual learning pipeline with Purged Walk-Forward CV & Shadow Promotion (ML-08).

Implements Pillar 5 of the Self-Improving Iron Condor Architecture:
  1. Post-Market Ingestion (reconciles EOD trajectories and applies hindsight labels).
  2. Purged Walk-Forward CV (splits sessions chronologically with a purge window to prevent temporal leakage).
  3. Challenger Training (fits candidate network on expanding replay buffer).
  4. Out-of-Sample Shadow Evaluation (evaluates Champion vs. Challenger on holdout sessions).
  5. Automated Promotion Gate (safely promotes Challenger to Champion with lineage tracking & rollback).
"""

from __future__ import annotations

import argparse
import json
import math
import shutil
from collections import defaultdict
from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Final

from ml.features import finite_number
from ml.policy_net import (
    MAX_PROFIT_TARGET,
    MIN_PROFIT_TARGET,
    MultiHeadPolicyNetwork,
)
from ml.train import (
    FEATURE_PATHS,
    MODEL_SCHEMA_VERSION,
    _examples,
    _normalization,
    _normalize_tick,
    _read_rows,
    _strategy_names,
    train_policy,
)
from trade_logger import relabel_session

DEFAULT_TRAJECTORY_PATH: Final = Path("logs/trajectories.jsonl")
DEFAULT_MODELS_DIR: Final = Path("logs/models")
CHAMPION_FILENAME: Final = "policy.json"
LINEAGE_FILENAME: Final = "model_lineage.json"
ARCHIVE_SUBDIR: Final = "archive"


def purged_walk_forward_split(
    sessions: Sequence[str],
    val_fraction: float = 0.25,
    purge_count: int = 1,
) -> tuple[list[str], list[str], list[str]]:
    """Split chronological session dates into train, purge, and validation folds.

    Purging the boundary session(s) between train and test sets eliminates
    temporal lookahead / autocorrelation leakage in multi-day options dynamics.
    """
    clean_sessions = sorted(dict.fromkeys(s for s in sessions if s and s.strip()))
    n = len(clean_sessions)
    if n == 0:
        return [], [], []
    if n == 1:
        return list(clean_sessions), [], []
    if n == 2:
        return [clean_sessions[0]], [], [clean_sessions[1]]

    val_count = max(1, math.ceil(n * val_fraction))
    val_sessions = clean_sessions[-val_count:]
    remaining = clean_sessions[:-val_count]

    if purge_count > 0 and len(remaining) > 1:
        purge_actual = min(purge_count, len(remaining) - 1)
        purged_sessions = remaining[-purge_actual:]
        train_sessions = remaining[:-purge_actual]
    else:
        purged_sessions = []
        train_sessions = remaining

    return train_sessions, purged_sessions, val_sessions


def evaluate_model_on_examples(
    model_artifact: Mapping[str, Any],
    examples: Sequence[tuple[tuple[tuple[float | None, ...], ...], str, float, int]],
) -> dict[str, Any]:
    """Evaluate a trained model artifact on raw sequence examples."""
    if not examples:
        return {
            "samples": 0,
            "loss": float("nan"),
            "action_accuracy": float("nan"),
            "profit_target_mae": float("nan"),
            "strategy_accuracy": float("nan"),
        }

    norm = model_artifact["normalization"]
    impute = norm["impute"]
    means = norm["mean"]
    scales = norm["scale"]
    net_cfg = model_artifact["network"]
    network = MultiHeadPolicyNetwork(
        feature_count=int(net_cfg["feature_count"]),
        regime_size=int(net_cfg["regime_size"]),
        strategy_count=int(net_cfg["strategy_count"]),
        strategy_names=tuple(str(s) for s in net_cfg["strategy_names"]),
    )
    network.load_state_dict(net_cfg["state_dict"])

    action_names = ("ADD_POSITION", "HOLD", "TRIM_POSITION", "FLATTEN_NOW")
    target_span = MAX_PROFIT_TARGET - MIN_PROFIT_TARGET

    total_loss = 0.0
    action_correct = 0
    strategy_correct = 0
    pt_abs_errors: list[float] = []

    for sequence, true_action, true_pt, true_strat_idx in examples:
        norm_seq = tuple(
            _normalize_tick(tick, impute, means, scales) for tick in sequence
        )
        batch = network.forward([norm_seq])
        action_probs = batch.action_probabilities[0]
        pred_pt = batch.profit_targets[0]
        strat_probs = batch.strategy_probabilities[0]

        true_action_idx = action_names.index(true_action)
        action_loss = -math.log(max(action_probs[true_action_idx], 1e-12))

        norm_target = (true_pt - MIN_PROFIT_TARGET) / target_span
        norm_pred = (pred_pt - MIN_PROFIT_TARGET) / target_span
        pt_loss = (norm_pred - norm_target) ** 2

        strat_loss = -math.log(max(strat_probs[true_strat_idx], 1e-12))
        total_loss += (action_loss + pt_loss + strat_loss)

        pred_action_idx = max(range(len(action_probs)), key=action_probs.__getitem__)
        if pred_action_idx == true_action_idx:
            action_correct += 1

        pred_strat_idx = max(range(len(strat_probs)), key=strat_probs.__getitem__)
        if pred_strat_idx == true_strat_idx:
            strategy_correct += 1

        pt_abs_errors.append(abs(pred_pt - true_pt))

    n = len(examples)
    return {
        "samples": n,
        "loss": total_loss / n,
        "action_accuracy": action_correct / n,
        "profit_target_mae": sum(pt_abs_errors) / n,
        "strategy_accuracy": strategy_correct / n,
    }


class ContinualLearner:
    """Manages the daily EOD retraining, walk-forward validation, and Champion/Challenger promotion gate."""

    def __init__(
        self,
        trajectories_path: str | Path = DEFAULT_TRAJECTORY_PATH,
        models_dir: str | Path = DEFAULT_MODELS_DIR,
        sequence_length: int = 12,
        regime_size: int = 32,
    ) -> None:
        self.trajectories_path = Path(trajectories_path)
        self.models_dir = Path(models_dir)
        self.sequence_length = sequence_length
        self.regime_size = regime_size
        self.champion_path = self.models_dir / CHAMPION_FILENAME
        self.lineage_path = self.models_dir / LINEAGE_FILENAME
        self.archive_dir = self.models_dir / ARCHIVE_SUBDIR

    def _ensure_dirs(self) -> None:
        self.models_dir.mkdir(parents=True, exist_ok=True)
        self.archive_dir.mkdir(parents=True, exist_ok=True)

    def load_lineage(self) -> dict[str, Any]:
        """Read model lineage and historical promotion evaluations."""
        if self.lineage_path.exists():
            try:
                with self.lineage_path.open("r", encoding="utf-8") as f:
                    return json.load(f)
            except Exception:
                pass
        return {
            "schema_version": 1,
            "active_champion_version": 0,
            "promotions_count": 0,
            "evaluations": [],
        }

    def save_lineage(self, lineage: Mapping[str, Any]) -> None:
        """Atomically persist updated model lineage."""
        self._ensure_dirs()
        tmp = self.lineage_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(lineage, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        tmp.replace(self.lineage_path)

    def relabel_completed_sessions(self) -> int:
        """Relabel all completed sessions in trajectories.jsonl that have terminal marks."""
        if not self.trajectories_path.exists():
            return 0
        rows = _read_rows(self.trajectories_path)
        sessions_with_end = {
            str(r.get("session")) for r in rows
            if r.get("event") == "SESSION_END" and r.get("session")
        }
        total_relabeled = 0
        for s in sorted(sessions_with_end):
            try:
                total_relabeled += relabel_session(s, log_path=self.trajectories_path)
            except Exception:
                pass
        return total_relabeled

    def run_continual_learning(
        self,
        *,
        epochs: int = 40,
        learning_rate: float = 0.02,
        val_fraction: float = 0.25,
        purge_count: int = 1,
        force_promote: bool = False,
    ) -> dict[str, Any]:
        """Execute the end-to-end post-market learning and promotion cycle."""
        self._ensure_dirs()
        lineage = self.load_lineage()

        # Step 1: Ingest & Relabel
        relabeled = self.relabel_completed_sessions()

        # Step 2: Read trajectories & partition examples
        rows = _read_rows(self.trajectories_path)
        snapshots = {
            str(r["record_id"]): r for r in rows
            if r.get("event") == "SNAPSHOT" and r.get("record_id")
        }
        labels = [r for r in rows if r.get("event") == "HINDSIGHT_LABEL"]
        strategy_names = _strategy_names(snapshots.values())

        by_session = defaultdict(list)
        for r in snapshots.values():
            s = str(r.get("session") or "")
            if s:
                by_session[s].append(r)

        all_sessions = sorted(by_session.keys())
        train_sessions, purged_sessions, val_sessions = purged_walk_forward_split(
            all_sessions, val_fraction=val_fraction, purge_count=purge_count
        )

        train_snapshots = {
            k: v for k, v in snapshots.items()
            if str(v.get("session") or "") in set(train_sessions)
        }
        train_examples = _examples(train_snapshots, labels, strategy_names, self.sequence_length)

        val_snapshots = {
            k: v for k, v in snapshots.items()
            if str(v.get("session") or "") in set(val_sessions)
        }
        val_examples = _examples(val_snapshots, labels, strategy_names, self.sequence_length)

        # Require nonempty, disjoint training and validation sets
        if not train_examples or not val_examples or not set(train_sessions).isdisjoint(set(val_sessions)):
            now_str = datetime.now(timezone.utc).isoformat()
            current_version = int(lineage.get("active_champion_version", 0))
            eval_record = {
                "timestamp": now_str,
                "status": "REJECTED_INSUFFICIENT_DISJOINT_DATA",
                "reasons": ["Non-empty and disjoint training and validation sets are required for evaluation and promotion"],
                "version": current_version,
                "train_sessions": train_sessions,
                "purged_sessions": purged_sessions,
                "val_sessions": val_sessions,
                "train_examples_count": len(train_examples) if train_examples else 0,
                "val_examples_count": len(val_examples) if val_examples else 0,
                "challenger_metrics": None,
                "champion_metrics": None,
            }
            lineage.setdefault("evaluations", []).append(eval_record)
            self.save_lineage(lineage)
            return eval_record

        # Step 3: Train Challenger Model
        challenger_path = self.models_dir / "challenger_latest.json"
        challenger_artifact = train_policy(
            trajectory_path=self.trajectories_path,
            model_path=challenger_path,
            sequence_length=self.sequence_length,
            regime_size=self.regime_size,
            epochs=epochs,
            learning_rate=learning_rate,
            sessions=train_sessions,
            strategy_names=strategy_names,
        )

        # Step 4: Shadow Mode Evaluation on Out-of-Sample Holdout
        challenger_metrics = evaluate_model_on_examples(challenger_artifact, val_examples)

        champion_metrics: dict[str, Any] | None = None
        champion_exists = self.champion_path.exists()
        if champion_exists:
            try:
                with self.champion_path.open("r", encoding="utf-8") as f:
                    champ_art = json.load(f)
                champion_metrics = evaluate_model_on_examples(champ_art, val_examples)
            except Exception:
                champion_metrics = None

        # Step 5: Automated Promotion Gate
        # Challenger qualifies for promotion if:
        # - Challenger validation metrics are finite and valid, AND
        #   - No active champion exists, OR
        #   - force_promote=True, OR
        #   - Validation loss improved (lower loss), OR
        #   - Validation action accuracy improved with no loss degradation > 5%
        promote = False
        reasons: list[str] = []

        ch_loss = challenger_metrics.get("loss") if challenger_metrics else None
        ch_acc = challenger_metrics.get("action_accuracy") if challenger_metrics else None

        if ch_loss is None or not math.isfinite(ch_loss) or ch_acc is None or not math.isfinite(ch_acc):
            promote = False
            reasons.append(f"Rejecting promotion: challenger validation metrics are invalid or non-finite (loss={ch_loss}, acc={ch_acc})")
        elif not champion_exists or champion_metrics is None:
            promote = True
            reasons.append("Initial champion bootstrap")
        elif force_promote:
            promote = True
            reasons.append("Force promotion requested")
        else:
            c_loss = champion_metrics.get("loss")
            c_acc = champion_metrics.get("action_accuracy")

            if c_loss is None or not math.isfinite(c_loss) or c_acc is None or not math.isfinite(c_acc):
                promote = True
                reasons.append("Non-finite baseline metrics on champion; promoting valid challenger")
            else:
                loss_delta = ch_loss - c_loss
                acc_delta = ch_acc - c_acc

                if ch_loss < c_loss:
                    promote = True
                    reasons.append(f"Validation loss improved: {ch_loss:.4f} vs {c_loss:.4f} (delta: {loss_delta:.4f})")
                elif ch_acc > c_acc and loss_delta <= 0.05 * c_loss:
                    promote = True
                    reasons.append(f"Validation action accuracy improved: {ch_acc:.1%} vs {c_acc:.1%} (delta: {acc_delta:+.1%})")
                else:
                    reasons.append(f"Challenger did not outperform Champion on holdout (Loss: {ch_loss:.4f} vs {c_loss:.4f}, Acc: {ch_acc:.1%} vs {c_acc:.1%})")

        now_str = datetime.now(timezone.utc).isoformat()
        current_version_raw = lineage.get("active_champion_version", 0)
        try:
            current_version = int(str(current_version_raw).split("->")[0])
        except (ValueError, TypeError):
            current_version = 0

        if promote:
            new_version = current_version + 1
            # Archive previous champion if it exists
            if champion_exists:
                archive_name = f"champion_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}_v{current_version}.json"
                archive_dest = self.archive_dir / archive_name
                try:
                    shutil.copy2(self.champion_path, archive_dest)
                except Exception:
                    pass

            # Promote challenger to active policy.json
            shutil.copy2(challenger_path, self.champion_path)
            status = "PROMOTED"
            lineage["active_champion_version"] = new_version
            lineage["promotions_count"] = lineage.get("promotions_count", 0) + 1
        else:
            new_version = current_version
            status = "REJECTED_SHADOW_RETAINED"

        eval_record = {
            "timestamp": now_str,
            "status": status,
            "reasons": reasons,
            "version": new_version,
            "train_sessions": train_sessions,
            "purged_sessions": purged_sessions,
            "val_sessions": val_sessions,
            "train_examples_count": len(train_examples),
            "val_examples_count": len(val_examples),
            "challenger_metrics": challenger_metrics,
            "champion_metrics": champion_metrics,
        }
        lineage.setdefault("evaluations", []).append(eval_record)
        self.save_lineage(lineage)

        return eval_record

    def rollback(self, target_archive_or_version: str | int) -> bool:
        """Rollback active champion to an archived version."""
        self._ensure_dirs()
        lineage = self.load_lineage()

        if isinstance(target_archive_or_version, int) or str(target_archive_or_version).isdigit():
            ver = int(target_archive_or_version)
            matches = list(self.archive_dir.glob(f"*_v{ver}.json"))
            if not matches:
                return False
            source = matches[-1]
        else:
            source = self.archive_dir / str(target_archive_or_version)
            if not source.exists():
                return False
            import re
            m = re.search(r"_v(\d+)\.json$", source.name)
            ver = int(m.group(1)) if m else 0

        current_ver_raw = lineage.get("active_champion_version", 0)
        try:
            current_ver = int(str(current_ver_raw).split("->")[0])
        except (ValueError, TypeError):
            current_ver = 0

        # Archive current state
        archive_name = f"champion_pre_rollback_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}_v{current_ver}.json"
        try:
            shutil.copy2(self.champion_path, self.archive_dir / archive_name)
        except Exception:
            pass

        shutil.copy2(source, self.champion_path)
        lineage["active_champion_version"] = ver
        lineage["active_champion_status"] = f"{current_ver}->rollback"
        lineage.setdefault("evaluations", []).append({
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "status": "ROLLBACK",
            "rollback_from_version": current_ver,
            "rollback_to_version": ver,
            "source": str(source.name),
        })
        self.save_lineage(lineage)
        return True


def main() -> None:
    parser = argparse.ArgumentParser(description="Run daily post-market continual learning pipeline (ML-08).")
    parser.add_argument("--trajectories", default=str(DEFAULT_TRAJECTORY_PATH), help="Path to trajectories.jsonl")
    parser.add_argument("--models-dir", default=str(DEFAULT_MODELS_DIR), help="Directory to store model artifacts and lineage")
    parser.add_argument("--epochs", type=int, default=40, help="Training epochs for challenger")
    parser.add_argument("--force-promote", action="store_true", help="Force promotion regardless of holdout delta")
    args = parser.parse_args()

    learner = ContinualLearner(
        trajectories_path=args.trajectories,
        models_dir=args.models_dir,
    )
    result = learner.run_continual_learning(
        epochs=args.epochs,
        force_promote=args.force_promote,
    )
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
