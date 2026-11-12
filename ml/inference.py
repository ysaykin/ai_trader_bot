"""Validated normalization and live inference bridge for policy artifacts."""

from __future__ import annotations

import json
import logging
import math
from collections import deque
from collections.abc import Mapping
from dataclasses import replace
from pathlib import Path
from typing import Any

from ml.policy_net import MultiHeadPolicyNetwork, PolicyRecommendation
from ml.train import MODEL_SCHEMA_VERSION, snapshot_features

logger = logging.getLogger(__name__)


class LivePolicyController:
    """Load one model artifact and queue recommendations from completed snapshots."""

    def __init__(
        self,
        model_path: str | Path,
        strategies: Mapping[str, Any] | None = None,
        *,
        expected_sequence_length: int | None = None,
    ) -> None:
        self.model_path = Path(model_path)
        artifact = self._load_artifact(self.model_path)
        self.feature_names = tuple(artifact["feature_names"])
        self.sequence_length = int(artifact["sequence_length"])
        if expected_sequence_length is not None and self.sequence_length != expected_sequence_length:
            raise ValueError(
                f"model sequence_length ({self.sequence_length}) does not match "
                f"configured ML_SEQUENCE_LENGTH ({expected_sequence_length})"
            )
        normalization = artifact["normalization"]
        self.impute = self._finite_vector(normalization["impute"], "normalization.impute")
        self.mean = self._finite_vector(normalization["mean"], "normalization.mean")
        self.scale = self._finite_vector(normalization["scale"], "normalization.scale")
        feature_count = len(self.feature_names)
        if not (
            self.sequence_length > 0
            and len(self.impute) == len(self.mean) == len(self.scale) == feature_count
            and all(value > 0 for value in self.scale)
        ):
            raise ValueError("model normalization schema is incompatible")
        network = artifact["network"]
        if int(network["feature_count"]) != feature_count:
            raise ValueError("network feature_count does not match feature_names")
        names = tuple(str(name) for name in network["strategy_names"])
        self.network = MultiHeadPolicyNetwork(
            feature_count=feature_count,
            regime_size=int(network["regime_size"]),
            strategy_count=int(network["strategy_count"]),
            strategy_names=names,
        )
        self.network.load_state_dict(network["state_dict"])
        self.strategies = dict(strategies or {})
        self._neural_strategies = tuple(
            strategy for strategy in self.strategies.values()
            if getattr(strategy, "pure_neural_mode", False)
            or getattr(strategy, "parameters", {}).get("pure_neural_mode", False)
        )
        self.candidate_strategy_names = names
        if self._neural_strategies:
            active_names = {
                name for name, strategy in self.strategies.items()
                if not getattr(strategy, "parameters", {}).get("management_only", False)
                and not any(strategy is neural for neural in self._neural_strategies)
            }
            self.candidate_strategy_names = tuple(name for name in names if name in active_names)
            if not self.candidate_strategy_names:
                raise ValueError("policy model has no active entry strategies available for the neural picker")
        self._candidate_offsets = tuple(
            index for index, name in enumerate(names) if name in self.candidate_strategy_names
        )
        unknown = set(self.strategies) - set(names)
        if unknown:
            logger.info(
                "Model defines %d strategies; %d roster names are absent from the model: %s",
                len(names),
                len(unknown),
                sorted(unknown),
            )
        self._sequence: deque[tuple[float, ...]] = deque(maxlen=self.sequence_length)
        self._session: str | None = None
        self.last_recommendation: PolicyRecommendation | None = None
        if self._neural_strategies:
            logger.info(
                "Neural picker loaded model=%s owners=%d candidates=%d sequence_length=%d",
                self.model_path, len(self._neural_strategies),
                len(self.candidate_strategy_names), self.sequence_length,
            )

    @property
    def has_sufficient_data(self) -> bool:
        """True when sufficient temporal sequence ticks have been accumulated for valid neural inference."""
        return len(self._sequence) >= self.sequence_length

    @property
    def current_ticks(self) -> int:
        """Number of temporal ticks currently accumulated in the session buffer."""
        return len(self._sequence)

    @property
    def required_ticks(self) -> int:
        """Number of temporal ticks required before the neural network can trade."""
        return self.sequence_length

    def on_snapshot(self, snapshot: Mapping[str, Any]) -> PolicyRecommendation | None:
        """Normalize a completed snapshot, predict, and queue its selected action."""
        if snapshot.get("event") != "SNAPSHOT" or not snapshot.get("complete"):
            return None
        session = snapshot.get("session")
        if session is not None and str(session) != self._session:
            self._sequence.clear()
            self._session = str(session)
        raw = snapshot_features(snapshot, self.feature_names)
        tick = tuple(
            ((self.impute[index] if value is None else value) - self.mean[index])
            / self.scale[index]
            for index, value in enumerate(raw)
        )
        self._sequence.append(tick)
        if len(self._sequence) < self.sequence_length:
            return None
        recommendation = self.network.predict(tuple(self._sequence))
        if self._neural_strategies:
            # Preserve the model's probabilities; constrain only the executable choice.
            offset = max(
                self._candidate_offsets,
                key=recommendation.strategy_probabilities.__getitem__,
            )
            recommendation = replace(
                recommendation, strategy_index=offset + 1,
                strategy_name=self.network.strategy_names[offset],
            )
        self.last_recommendation = recommendation
        strategy = self.strategies.get(recommendation.strategy_name)
        if not self._neural_strategies and strategy is not None:
            strategy._queued_policy_action = (
                recommendation.action,
                recommendation.profit_target,
            )
        for strategy in self._neural_strategies:
            strategy._queued_policy_action = (
                recommendation.action,
                recommendation.profit_target,
            )
            strategy._queued_strategy_switch = recommendation.strategy_name
            strategy._last_neural_recommendation = recommendation
        if self._neural_strategies:
            logger.info(
                "Neural picker selected %s; action=%s profit_target=%.3f candidates=%d",
                recommendation.strategy_name, recommendation.action,
                recommendation.profit_target, len(self.candidate_strategy_names),
            )
        return recommendation

    @staticmethod
    def _load_artifact(path: Path) -> Mapping[str, Any]:
        if not path.is_file():
            raise FileNotFoundError(f"policy model does not exist: {path}")
        try:
            artifact = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as error:
            raise ValueError(f"policy model is not valid JSON: {path}") from error
        required = {"schema_version", "feature_names", "sequence_length", "normalization", "network"}
        if not isinstance(artifact, Mapping) or set(artifact) < required:
            raise ValueError("policy model is missing required fields")
        if artifact["schema_version"] != MODEL_SCHEMA_VERSION:
            raise ValueError(f"unsupported policy model schema: {artifact['schema_version']}")
        return artifact

    @staticmethod
    def _finite_vector(values: Any, name: str) -> tuple[float, ...]:
        if isinstance(values, (str, bytes)) or not isinstance(values, list):
            raise ValueError(f"{name} must be a JSON array")
        result = []
        for value in values:
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise ValueError(f"{name} contains a non-real value")
            number = float(value)
            if not math.isfinite(number):
                raise ValueError(f"{name} contains a non-finite value")
            result.append(number)
        return tuple(result)


__all__ = ["LivePolicyController"]
