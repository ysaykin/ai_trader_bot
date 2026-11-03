"""Offline trajectory trainer for the dependency-free policy network."""

from __future__ import annotations

import argparse
import json
import math
import os
import random
from collections import defaultdict
from collections.abc import Mapping, Sequence
from pathlib import Path
from statistics import median
from typing import Any, Final

from ml.features import finite_number
from ml.policy_net import (
    MAX_PROFIT_TARGET,
    MIN_PROFIT_TARGET,
    POLICY_ACTIONS,
    MultiHeadPolicyNetwork,
)

MODEL_SCHEMA_VERSION: Final = 1
FEATURE_PATHS: Final[tuple[str, ...]] = (
    "market.vix",
    "market.vix_term_ratio",
    "market.vwap_distance",
    "market.orb_30m_range",
    "market.atr_pct",
    "market.rsi_14",
    "market.price_velocity_15m",
    "market.minutes_to_close",
    "grid.pnl_dispersion",
    "grid.put_pressure",
    "grid.call_pressure",
    "grid.delta_net",
    "grid.gamma_net",
    "grid.theta_net",
    "grid.vega_net",
)


def snapshot_features(
    snapshot: Mapping[str, Any],
    feature_names: Sequence[str] = FEATURE_PATHS,
) -> tuple[float | None, ...]:
    """Extract the ordered causal feature vector stored in a trajectory row."""
    values: list[float | None] = []
    for path in feature_names:
        current: Any = snapshot
        for part in path.split("."):
            current = current.get(part) if isinstance(current, Mapping) else None
        values.append(finite_number(current))
    return tuple(values)


def train_policy(
    trajectory_path: str | Path,
    model_path: str | Path,
    *,
    sequence_length: int = 12,
    regime_size: int = 32,
    epochs: int = 40,
    learning_rate: float = 0.02,
    seed: int = 0,
    sessions: Sequence[str] | None = None,
    strategy_names: Sequence[str] | None = None,
) -> dict[str, Any]:
    """Fit policy encoder and heads from causal snapshots joined to hindsight labels."""
    if sequence_length < 1 or regime_size < 1 or epochs < 1:
        raise ValueError("sequence_length, regime_size, and epochs must be positive")
    if not math.isfinite(learning_rate) or learning_rate <= 0:
        raise ValueError("learning_rate must be finite and positive")
    rows = _read_rows(Path(trajectory_path))
    snapshots = {
        str(row["record_id"]): row
        for row in rows
        if row.get("event") == "SNAPSHOT" and row.get("record_id")
    }
    if sessions is not None:
        allowed_sessions = set(sessions)
        snapshots = {
            record_id: snap for record_id, snap in snapshots.items()
            if str(snap.get("session") or "") in allowed_sessions
        }
    labels = [row for row in rows if row.get("event") == "HINDSIGHT_LABEL"]
    if strategy_names is None:
        strategy_names = _strategy_names(snapshots.values())
    else:
        strategy_names = tuple(strategy_names)
    examples = _examples(snapshots, labels, strategy_names, sequence_length)
    if not examples:
        raise ValueError("no complete labelled trajectory examples were found")

    raw_ticks = [tick for sequence, *_ in examples for tick in sequence]
    impute, means, scales = _normalization(raw_ticks)
    normalized = [
        (
            tuple(_normalize_tick(tick, impute, means, scales) for tick in sequence),
            POLICY_ACTIONS.index(action),
            profit_target,
            strategy_index,
        )
        for sequence, action, profit_target, strategy_index in examples
    ]
    network = MultiHeadPolicyNetwork(
        feature_count=len(FEATURE_PATHS),
        regime_size=regime_size,
        strategy_count=len(strategy_names),
        strategy_names=strategy_names,
        seed=seed,
    )
    state = _mutable_state(network.state_dict())
    generator = random.Random(seed)
    last_loss = 0.0
    for _ in range(epochs):
        generator.shuffle(normalized)
        loss = 0.0
        for sequence, action_idx, profit_target, strategy_index in normalized:
            loss += _train_step(
                state,
                sequence,
                action_idx,
                profit_target,
                strategy_index,
                learning_rate,
            )
        last_loss = loss / len(normalized)
    network.load_state_dict(state)
    artifact: dict[str, Any] = {
        "schema_version": MODEL_SCHEMA_VERSION,
        "feature_names": list(FEATURE_PATHS),
        "sequence_length": sequence_length,
        "normalization": {
            "impute": list(impute),
            "mean": list(means),
            "scale": list(scales),
        },
        "network": {
            "feature_count": len(FEATURE_PATHS),
            "regime_size": regime_size,
            "strategy_count": len(strategy_names),
            "strategy_names": list(strategy_names),
            "state_dict": network.state_dict(),
        },
        "training": {
            "epochs": epochs,
            "examples": len(normalized),
            "final_loss": last_loss,
            "seed": seed,
        },
    }
    destination = Path(model_path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_suffix(destination.suffix + ".tmp")
    temporary.write_text(
        json.dumps(artifact, allow_nan=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    temporary.replace(destination)
    return artifact


def _read_rows(path: Path) -> list[dict[str, Any]]:
    if not path.is_file():
        raise FileNotFoundError(path)
    rows: list[dict[str, Any]] = []
    with path.open(encoding="utf-8") as stream:
        for line_number, line in enumerate(stream, start=1):
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError as error:
                raise ValueError(f"invalid JSON on trajectory line {line_number}") from error
            if isinstance(row, dict) and row.get("schema_version") == 1:
                rows.append(row)
    return rows


def _strategy_names(snapshots: Sequence[Mapping[str, Any]] | Any) -> tuple[str, ...]:
    names: set[str] = set()
    for snapshot in snapshots:
        strategies = snapshot.get("strategies")
        if isinstance(strategies, Mapping):
            names.update(str(name) for name in strategies)
    if not names:
        raise ValueError("trajectory snapshots do not contain strategy names")
    return tuple(sorted(names))


def _examples(
    snapshots: Mapping[str, Mapping[str, Any]],
    labels: Sequence[Mapping[str, Any]],
    strategy_names: tuple[str, ...],
    sequence_length: int,
) -> list[tuple[tuple[tuple[float | None, ...], ...], str, float, int]]:
    by_session: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for row in snapshots.values():
        by_session[str(row.get("session") or "")].append(row)
    session_lookup: dict[str, tuple[list[Mapping[str, Any]], int]] = {}
    for session_rows in by_session.values():
        ordered = sorted(session_rows, key=lambda row: str(row.get("timestamp", "")))
        for index, row in enumerate(ordered):
            session_lookup[str(row["record_id"])] = (ordered, index)

    examples = []
    for label in labels:
        source_id = str(label.get("source_id", ""))
        if source_id not in session_lookup:
            continue
        by_strategy = label.get("strategies")
        if not isinstance(by_strategy, Mapping) or not by_strategy:
            continue
        best = str(label.get("best_strategy") or "")
        if best not in strategy_names or not isinstance(by_strategy.get(best), Mapping):
            continue
        target = by_strategy[best]
        action = str(target.get("action", ""))
        profit_target = finite_number(target.get("profit_target"))
        if action not in POLICY_ACTIONS or profit_target is None:
            continue
        ordered, index = session_lookup[source_id]
        end = index + 1
        start = max(0, end - sequence_length)
        sequence = [snapshot_features(row) for row in ordered[start:end]]
        if not sequence:
            continue
        sequence = [sequence[0]] * (sequence_length - len(sequence)) + sequence
        examples.append(
            (
                tuple(sequence),
                action,
                min(max(profit_target, MIN_PROFIT_TARGET), MAX_PROFIT_TARGET),
                strategy_names.index(best),
            )
        )
    return examples


def _normalization(
    ticks: Sequence[Sequence[float | None]],
) -> tuple[tuple[float, ...], tuple[float, ...], tuple[float, ...]]:
    columns = list(zip(*ticks))
    impute = tuple(
        median(present) if (present := [value for value in column if value is not None]) else 0.0
        for column in columns
    )
    filled = [tuple(impute[index] if value is None else value for index, value in enumerate(tick)) for tick in ticks]
    means = tuple(sum(column) / len(column) for column in zip(*filled))
    scales = tuple(
        math.sqrt(sum((value - means[index]) ** 2 for value in column) / len(column)) or 1.0
        for index, column in enumerate(zip(*filled))
    )
    return impute, means, scales


def _normalize_tick(
    tick: Sequence[float | None],
    impute: Sequence[float],
    means: Sequence[float],
    scales: Sequence[float],
) -> tuple[float, ...]:
    return tuple(
        ((impute[index] if value is None else value) - means[index]) / scales[index]
        for index, value in enumerate(tick)
    )


def _mutable_state(state: Mapping[str, Any]) -> dict[str, list[Any]]:
    return {
        name: [list(row) for row in value]
        if value and isinstance(value[0], tuple)
        else list(value)
        for name, value in state.items()
    }


def _softmax(logits: Sequence[float]) -> list[float]:
    largest = max(logits)
    exponentials = [math.exp(value - largest) for value in logits]
    total = sum(exponentials)
    return [value / total for value in exponentials]


def _sigmoid(value: float) -> float:
    if value >= 0:
        return 1.0 / (1.0 + math.exp(-value))
    exponential = math.exp(value)
    return exponential / (1.0 + exponential)


def _train_step(
    state: dict[str, list[Any]],
    sequence: Sequence[Sequence[float]],
    action_index: int,
    profit_target: float,
    strategy_index: int,
    learning_rate: float,
) -> float:
    input_weights = state["encoder_input_weights"]
    recurrent_weights = state["encoder_recurrent_weights"]
    encoder_bias = state["encoder_bias"]
    regime_size = len(encoder_bias)
    feature_count = len(input_weights)

    states: list[list[float]] = [[0.0] * regime_size]
    for tick in sequence:
        previous = states[-1]
        hidden = [
            math.tanh(
                encoder_bias[col]
                + sum(tick[row] * input_weights[row][col] for row in range(feature_count))
                + sum(previous[row] * recurrent_weights[row][col] for row in range(regime_size))
            )
            for col in range(regime_size)
        ]
        states.append(hidden)
    embedding = states[-1]

    action_weights = state["action_weights"]
    action_bias = state["action_bias"]
    action_logits = [
        action_bias[col] + sum(embedding[row] * action_weights[row][col] for row in range(regime_size))
        for col in range(len(action_bias))
    ]
    action_probs = _softmax(action_logits)
    action_grad = [
        prob - (1.0 if col == action_index else 0.0)
        for col, prob in enumerate(action_probs)
    ]
    action_loss = -math.log(max(action_probs[action_index], 1e-12))

    profit_weights = state["profit_weights"]
    profit_bias = state["profit_bias"]
    profit_logit = profit_bias[0] + sum(embedding[row] * profit_weights[row][0] for row in range(regime_size))
    profit_sig = _sigmoid(profit_logit)
    span = MAX_PROFIT_TARGET - MIN_PROFIT_TARGET
    normalized_target = (profit_target - MIN_PROFIT_TARGET) / span
    profit_error = profit_sig - normalized_target
    profit_grad = 2.0 * profit_error * profit_sig * (1.0 - profit_sig)
    profit_loss = profit_error * profit_error

    strategy_weights = state["strategy_weights"]
    strategy_bias = state["strategy_bias"]
    strategy_logits = [
        strategy_bias[col] + sum(embedding[row] * strategy_weights[row][col] for row in range(regime_size))
        for col in range(len(strategy_bias))
    ]
    strategy_probs = _softmax(strategy_logits)
    strategy_grad = [
        prob - (1.0 if col == strategy_index else 0.0)
        for col, prob in enumerate(strategy_probs)
    ]
    strategy_loss = -math.log(max(strategy_probs[strategy_index], 1e-12))

    dh = [
        sum(action_grad[col] * action_weights[row][col] for col in range(len(action_grad)))
        + profit_grad * profit_weights[row][0]
        + sum(strategy_grad[col] * strategy_weights[row][col] for col in range(len(strategy_grad)))
        for row in range(regime_size)
    ]

    for col, grad in enumerate(action_grad):
        action_bias[col] -= learning_rate * grad
        for row, hidden in enumerate(embedding):
            action_weights[row][col] -= learning_rate * grad * hidden

    profit_bias[0] -= learning_rate * profit_grad
    for row, hidden in enumerate(embedding):
        profit_weights[row][0] -= learning_rate * profit_grad * hidden

    for col, grad in enumerate(strategy_grad):
        strategy_bias[col] -= learning_rate * grad
        for row, hidden in enumerate(embedding):
            strategy_weights[row][col] -= learning_rate * grad * hidden

    grad_input = [[0.0] * regime_size for _ in range(feature_count)]
    grad_recurrent = [[0.0] * regime_size for _ in range(regime_size)]
    grad_bias = [0.0] * regime_size
    for step_idx in range(len(sequence) - 1, -1, -1):
        current_hidden = states[step_idx + 1]
        previous_hidden = states[step_idx]
        tick = sequence[step_idx]
        dz = [
            max(-5.0, min(5.0, dh[col] * (1.0 - current_hidden[col] * current_hidden[col])))
            for col in range(regime_size)
        ]
        for col in range(regime_size):
            grad_bias[col] += dz[col]
        for row in range(feature_count):
            x_val = tick[row]
            for col in range(regime_size):
                grad_input[row][col] += x_val * dz[col]
        for row in range(regime_size):
            h_val = previous_hidden[row]
            for col in range(regime_size):
                grad_recurrent[row][col] += h_val * dz[col]
        dh = [
            sum(dz[col] * recurrent_weights[row][col] for col in range(regime_size))
            for row in range(regime_size)
        ]

    for col in range(regime_size):
        encoder_bias[col] -= learning_rate * grad_bias[col]
    for row in range(feature_count):
        for col in range(regime_size):
            input_weights[row][col] -= learning_rate * grad_input[row][col]
    for row in range(regime_size):
        for col in range(regime_size):
            recurrent_weights[row][col] -= learning_rate * grad_recurrent[row][col]

    return action_loss + profit_loss + strategy_loss


def main(argv: Sequence[str] | None = None) -> int:
    """Train a policy artifact from a trajectory JSONL file."""
    default_seq = int((os.getenv("ML_SEQUENCE_LENGTH") or "12").strip())
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("trajectory_path", type=Path)
    parser.add_argument("model_path", type=Path)
    parser.add_argument("--sequence-length", type=int, default=default_seq)
    parser.add_argument("--epochs", type=int, default=40)
    parser.add_argument("--learning-rate", type=float, default=0.02)
    args = parser.parse_args(argv)
    train_policy(
        args.trajectory_path,
        args.model_path,
        sequence_length=args.sequence_length,
        epochs=args.epochs,
        learning_rate=args.learning_rate,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
