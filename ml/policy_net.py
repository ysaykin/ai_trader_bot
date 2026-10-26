"""Dependency-free multi-head neural policy network for Iron Condor control.

The network is intentionally an inference-focused building block.  Offline
training code can update its parameters through ``load_state_dict`` without
coupling live trading to a heavyweight machine-learning runtime.
"""

from __future__ import annotations

import math
import random
from dataclasses import dataclass
from numbers import Real
from typing import Any, Final, Mapping, Sequence, TypeAlias, cast


Vector: TypeAlias = tuple[float, ...]
Matrix: TypeAlias = tuple[Vector, ...]
StateValue: TypeAlias = Vector | Matrix

POLICY_ACTIONS: Final[tuple[str, ...]] = (
    "ADD_POSITION",
    "HOLD",
    "TRIM_POSITION",
    "FLATTEN_NOW",
)
MIN_PROFIT_TARGET: Final = 0.15
MAX_PROFIT_TARGET: Final = 0.80


@dataclass(frozen=True)
class PolicyBatch:
    """Outputs produced for a batch of temporal state sequences."""

    regime_embeddings: Matrix
    action_probabilities: Matrix
    profit_targets: Vector
    strategy_probabilities: Matrix


@dataclass(frozen=True)
class PolicyRecommendation:
    """Human- and execution-friendly recommendation for one state sequence."""

    action: str
    action_probabilities: Mapping[str, float]
    profit_target: float
    strategy_index: int
    strategy_name: str
    strategy_probabilities: Vector
    regime_embedding: Vector


class MultiHeadPolicyNetwork:
    """Small recurrent encoder with scaling, profit, and strategy heads.

    Input to :meth:`forward` is a batch of sequences with shape
    ``[batch][time][feature]``.  The final recurrent state is the shared regime
    embedding consumed by all three heads.
    """

    _PARAMETER_NAMES: Final[tuple[str, ...]] = (
        "encoder_input_weights",
        "encoder_recurrent_weights",
        "encoder_bias",
        "action_weights",
        "action_bias",
        "profit_weights",
        "profit_bias",
        "strategy_weights",
        "strategy_bias",
    )

    def __init__(
        self,
        feature_count: int,
        *,
        regime_size: int = 32,
        strategy_count: int = 20,
        strategy_names: Sequence[str] | None = None,
        seed: int = 0,
    ) -> None:
        if feature_count < 1:
            raise ValueError("feature_count must be positive")
        if regime_size < 1:
            raise ValueError("regime_size must be positive")
        if strategy_count < 1:
            raise ValueError("strategy_count must be positive")

        self.feature_count = feature_count
        self.regime_size = regime_size
        self.strategy_count = strategy_count
        self.strategy_names = self._validate_strategy_names(strategy_names)

        generator = random.Random(seed)
        self._parameters: dict[str, StateValue] = {
            "encoder_input_weights": self._random_matrix(
                generator, feature_count, regime_size
            ),
            "encoder_recurrent_weights": self._random_matrix(
                generator, regime_size, regime_size
            ),
            "encoder_bias": self._zeros(regime_size),
            "action_weights": self._random_matrix(
                generator, regime_size, len(POLICY_ACTIONS)
            ),
            "action_bias": self._zeros(len(POLICY_ACTIONS)),
            "profit_weights": self._random_matrix(generator, regime_size, 1),
            "profit_bias": self._zeros(1),
            "strategy_weights": self._random_matrix(
                generator, regime_size, strategy_count
            ),
            "strategy_bias": self._zeros(strategy_count),
        }

    def forward(self, sequences: Sequence[Sequence[Sequence[Real]]]) -> PolicyBatch:
        """Run a batch of temporal states through the shared encoder and heads."""
        batch = self._validate_batch(sequences)
        embeddings = tuple(self._encode(sequence) for sequence in batch)

        action_logits = self._linear_batch(
            embeddings,
            self._matrix("action_weights"),
            self._vector("action_bias"),
        )
        action_probabilities = tuple(self._softmax(row) for row in action_logits)

        profit_logits = self._linear_batch(
            embeddings,
            self._matrix("profit_weights"),
            self._vector("profit_bias"),
        )
        target_span = MAX_PROFIT_TARGET - MIN_PROFIT_TARGET
        profit_targets = tuple(
            MIN_PROFIT_TARGET + target_span * self._sigmoid(row[0])
            for row in profit_logits
        )

        strategy_logits = self._linear_batch(
            embeddings,
            self._matrix("strategy_weights"),
            self._vector("strategy_bias"),
        )
        strategy_probabilities = tuple(
            self._softmax(row) for row in strategy_logits
        )

        return PolicyBatch(
            regime_embeddings=embeddings,
            action_probabilities=action_probabilities,
            profit_targets=profit_targets,
            strategy_probabilities=strategy_probabilities,
        )

    def predict(self, sequence: Sequence[Sequence[Real]]) -> PolicyRecommendation:
        """Return the highest-probability policy recommendation for one sequence."""
        output = self.forward([sequence])
        action_probabilities = output.action_probabilities[0]
        strategy_probabilities = output.strategy_probabilities[0]
        action_index = max(
            range(len(action_probabilities)), key=action_probabilities.__getitem__
        )
        strategy_offset = max(
            range(len(strategy_probabilities)),
            key=strategy_probabilities.__getitem__,
        )
        return PolicyRecommendation(
            action=POLICY_ACTIONS[action_index],
            action_probabilities=dict(zip(POLICY_ACTIONS, action_probabilities)),
            profit_target=output.profit_targets[0],
            strategy_index=strategy_offset + 1,
            strategy_name=self.strategy_names[strategy_offset],
            strategy_probabilities=strategy_probabilities,
            regime_embedding=output.regime_embeddings[0],
        )

    def state_dict(self) -> dict[str, StateValue]:
        """Return an immutable copy of the trainable network parameters."""
        return dict(self._parameters)

    def load_state_dict(self, parameters: Mapping[str, Any]) -> None:
        """Load tuple or JSON-list parameters after strict shape/value checks."""
        expected_names = set(self._PARAMETER_NAMES)
        received_names = set(parameters)
        if received_names != expected_names:
            missing = sorted(expected_names - received_names)
            extra = sorted(received_names - expected_names)
            raise ValueError(f"state_dict keys differ; missing={missing}, extra={extra}")

        validated: dict[str, StateValue] = {}
        for name in self._PARAMETER_NAMES:
            expected = self._parameters[name]
            value = parameters[name]
            validated[name] = self._validate_parameter(name, value, expected)
        self._parameters = validated

    def _validate_strategy_names(
        self, strategy_names: Sequence[str] | None
    ) -> tuple[str, ...]:
        if strategy_names is None:
            return tuple(
                f"strategy_{index:02d}"
                for index in range(1, self.strategy_count + 1)
            )
        names = tuple(str(name).strip() for name in strategy_names)
        if len(names) != self.strategy_count:
            raise ValueError("strategy_names must match strategy_count")
        if any(not name for name in names) or len(set(names)) != len(names):
            raise ValueError("strategy_names must be non-empty and unique")
        return names

    def _validate_batch(
        self, sequences: Sequence[Sequence[Sequence[Real]]]
    ) -> tuple[Matrix, ...]:
        if not sequences:
            raise ValueError("sequences must contain at least one sequence")
        batch: list[Matrix] = []
        for batch_index, sequence in enumerate(sequences):
            if (
                isinstance(sequence, (str, bytes))
                or not isinstance(sequence, Sequence)
                or not sequence
            ):
                raise ValueError(f"sequence {batch_index} must contain at least one tick")
            ticks: list[Vector] = []
            for time_index, tick in enumerate(sequence):
                if isinstance(tick, (str, bytes)) or not isinstance(tick, Sequence):
                    raise ValueError(f"tick {batch_index}:{time_index} must be numeric")
                if len(tick) != self.feature_count:
                    raise ValueError(
                        f"tick {batch_index}:{time_index} has {len(tick)} features; "
                        f"expected {self.feature_count}"
                    )
                if any(isinstance(value, bool) or not isinstance(value, Real) for value in tick):
                    raise ValueError(
                        f"tick {batch_index}:{time_index} contains a non-real value"
                    )
                values = tuple(float(value) for value in tick)
                if not all(math.isfinite(value) for value in values):
                    raise ValueError(
                        f"tick {batch_index}:{time_index} contains a non-finite value"
                    )
                ticks.append(values)
            batch.append(tuple(ticks))
        return tuple(batch)

    def _encode(self, sequence: Matrix) -> Vector:
        input_weights = self._matrix("encoder_input_weights")
        recurrent_weights = self._matrix("encoder_recurrent_weights")
        bias = self._vector("encoder_bias")
        hidden = self._zeros(self.regime_size)
        for tick in sequence:
            from_input = self._vector_matrix_product(tick, input_weights)
            from_history = self._vector_matrix_product(hidden, recurrent_weights)
            hidden = tuple(
                math.tanh(input_value + history_value + bias_value)
                for input_value, history_value, bias_value in zip(
                    from_input, from_history, bias
                )
            )
        return hidden

    @staticmethod
    def _linear_batch(inputs: Matrix, weights: Matrix, bias: Vector) -> Matrix:
        return tuple(
            tuple(
                value + offset
                for value, offset in zip(
                    MultiHeadPolicyNetwork._vector_matrix_product(row, weights),
                    bias,
                )
            )
            for row in inputs
        )

    @staticmethod
    def _vector_matrix_product(vector: Vector, matrix: Matrix) -> Vector:
        output_size = len(matrix[0])
        return tuple(
            sum(vector[row] * matrix[row][column] for row in range(len(vector)))
            for column in range(output_size)
        )

    @staticmethod
    def _softmax(logits: Vector) -> Vector:
        largest = max(logits)
        exponentials = tuple(math.exp(value - largest) for value in logits)
        denominator = sum(exponentials)
        return tuple(value / denominator for value in exponentials)

    @staticmethod
    def _sigmoid(value: float) -> float:
        if value >= 0:
            return 1.0 / (1.0 + math.exp(-value))
        exponential = math.exp(value)
        return exponential / (1.0 + exponential)

    @staticmethod
    def _zeros(size: int) -> Vector:
        return (0.0,) * size

    @staticmethod
    def _random_matrix(
        generator: random.Random, input_size: int, output_size: int
    ) -> Matrix:
        limit = math.sqrt(6.0 / (input_size + output_size))
        return tuple(
            tuple(generator.uniform(-limit, limit) for _ in range(output_size))
            for _ in range(input_size)
        )

    def _matrix(self, name: str) -> Matrix:
        value = self._parameters[name]
        if not value or not isinstance(value[0], tuple):
            raise RuntimeError(f"parameter {name} is not a matrix")
        return cast(Matrix, value)

    def _vector(self, name: str) -> Vector:
        value = self._parameters[name]
        if value and isinstance(value[0], tuple):
            raise RuntimeError(f"parameter {name} is not a vector")
        return cast(Vector, value)

    @staticmethod
    def _validate_parameter(
        name: str, value: Any, expected: StateValue
    ) -> StateValue:
        if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
            raise ValueError(f"parameter {name} must be a sequence")
        expected_is_matrix = bool(expected) and isinstance(expected[0], tuple)
        value_is_matrix = bool(value) and isinstance(value[0], Sequence) and not isinstance(
            value[0], (str, bytes)
        )
        if expected_is_matrix != value_is_matrix or len(value) != len(expected):
            raise ValueError(f"parameter {name} has the wrong shape")

        if expected_is_matrix:
            expected_matrix = cast(Matrix, expected)
            expected_columns = len(expected_matrix[0])
            if any(
                isinstance(row, (str, bytes))
                or not isinstance(row, Sequence)
                or len(row) != expected_columns
                for row in value
            ):
                raise ValueError(f"parameter {name} has the wrong shape")
            if any(
                isinstance(item, bool) or not isinstance(item, Real)
                for row in value
                for item in row
            ):
                raise ValueError(f"parameter {name} contains a non-real value")
            normalized: StateValue = tuple(
                tuple(float(item) for item in row) for row in value
            )
        else:
            if any(isinstance(item, bool) or not isinstance(item, Real) for item in value):
                raise ValueError(f"parameter {name} contains a non-real value")
            normalized = tuple(float(item) for item in value)

        flattened = (
            (item for row in normalized for item in row)
            if expected_is_matrix
            else iter(normalized)
        )
        if not all(math.isfinite(item) for item in flattened):
            raise ValueError(f"parameter {name} contains a non-finite value")
        return normalized


__all__ = [
    "MAX_PROFIT_TARGET",
    "MIN_PROFIT_TARGET",
    "POLICY_ACTIONS",
    "MultiHeadPolicyNetwork",
    "PolicyBatch",
    "PolicyRecommendation",
]
