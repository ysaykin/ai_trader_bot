"""Validated normalization and live inference bridge for policy artifacts."""

from __future__ import annotations

import json
import math
from collections import deque
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from ml.policy_net import MultiHeadPolicyNetwork, PolicyRecommendation
from ml.train import MODEL_SCHEMA_VERSION, snapshot_features


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
        unknown = set(self.strategies) - set(names)
        if unknown:
            import logging
            logging.getLogger("ml.inference").info(
                "Model defines %d strategies; %d additional strategies %s will receive neural policy broadcasts",
                len(names),
                len(unknown),
                sorted(unknown),
            )
        self._sequence: deque[tuple[float, ...]] = deque(maxlen=self.sequence_length)
        self._session: str | None = None
        self.last_recommendation: PolicyRecommendation | None = None

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
        self.last_recommendation = recommendation
        strategy = self.strategies.get(recommendation.strategy_name)
        if strategy is not None:
            strategy._queued_policy_action = (
                recommendation.action,
                recommendation.profit_target,
            )
        # Always route neural actions, dynamic profit targets, and dynamic strategy switches to pure neural managed strategies
        for s in self.strategies.values():
            if getattr(s, "pure_neural_mode", False) or getattr(s, "parameters", {}).get("pure_neural_mode", False):
                s._queued_policy_action = (
                    recommendation.action,
                    recommendation.profit_target,
                )
                s._queued_strategy_switch = recommendation.strategy_name
                s._last_neural_recommendation = recommendation
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
