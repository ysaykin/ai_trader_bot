#!/usr/bin/env python3
"""Replay saved snapshots through the neural picker without a broker connection."""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections import Counter
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def main() -> int:
    from dotenv import load_dotenv

    load_dotenv(ROOT / ".env")
    os.environ.setdefault("LUMIBOT_LAZY_CREDENTIALS", "true")

    from ml.inference import LivePolicyController
    from strategies.incubator import get_incubator_specs
    from strategies.iron_condor import SPXIronCondor
    from strategies.runtime_records import resolve_record_path

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", type=Path, default=Path(os.getenv("ML_MODEL_PATH", "logs/models/policy.json")))
    parser.add_argument("--trajectories", type=Path)
    parser.add_argument("--session", help="Replay this session, defaulting to the latest saved session.")
    parser.add_argument("--require-enabled", action="store_true", help="Also fail unless inference is configured on.")
    args = parser.parse_args()
    trajectory_path = args.trajectories or resolve_record_path("TRAJECTORY_LOG_PATH", "logs/trajectories.jsonl")
    roster_limit = os.getenv("INCUBATOR_STRATEGY_COUNT", "").strip()
    specs = get_incubator_specs(limit=int(roster_limit) if roster_limit.isdigit() else None)
    strategies = {}
    neural = []
    for spec in specs:
        if spec.get("pure_neural_mode"):
            strategy = object.__new__(SPXIronCondor)
            strategy._name = spec["strategy_name"]
            strategy.parameters = {**SPXIronCondor.parameters, **spec}
            strategy.pure_neural_mode = True
            strategy._position_ledger = SimpleNamespace(is_panic_halted=lambda: False)
            neural.append(strategy)
        else:
            strategy = SimpleNamespace(parameters=spec)
        strategies[spec["strategy_name"]] = strategy
    if not neural:
        raise ValueError("the configured roster does not include Neural Pure")
    raw_length = os.getenv("ML_SEQUENCE_LENGTH", "").strip()
    controller = LivePolicyController(
        args.model, strategies,
        expected_sequence_length=int(raw_length) if raw_length else None,
    )
    with trajectory_path.open(encoding="utf-8") as stream:
        snapshots = [row for line in stream if line.strip()
                     if (row := json.loads(line)).get("event") == "SNAPSHOT"]
    session = args.session or max(str(row.get("session", "")) for row in snapshots)
    selected = sorted(
        (row for row in snapshots if row.get("session") == session),
        key=lambda row: row["timestamp"],
    )
    choices: Counter[str] = Counter()
    actions: Counter[str] = Counter()
    for snapshot in selected:
        recommendation = controller.on_snapshot(snapshot)
        if recommendation is None:
            continue
        for strategy in neural:
            if not strategy._apply_neural_strategy_switch(strategy._queued_strategy_switch):
                raise ValueError("neural recommendation could not be applied to the active roster")
        choices[recommendation.strategy_name] += 1
        actions[recommendation.action] += 1
    enabled = os.getenv("ML_POLICY_ENABLED", "false").strip().lower() in {"true", "1", "yes", "on"}
    logging_enabled = os.getenv("TRAJECTORY_LOGGING_ENABLED", "true").strip().lower() in {"true", "1", "yes"}
    peers_untouched = all(
        not hasattr(strategy, "_queued_policy_action")
        for strategy in strategies.values()
        if not strategy.parameters.get("pure_neural_mode")
    )
    passed = bool(choices) and logging_enabled and peers_untouched and (enabled or not args.require_enabled)
    print(json.dumps({
        "offline_replay": "PASS" if passed else "FAIL",
        "policy_enabled": enabled,
        "trajectory_logging_enabled": logging_enabled,
        "model": str(args.model),
        "training": json.loads(args.model.read_text(encoding="utf-8")).get("training"),
        "session": session,
        "complete_snapshots": sum(bool(row.get("complete")) for row in selected),
        "required_ticks": controller.required_ticks,
        "active_candidates": list(controller.candidate_strategy_names),
        "recommendations": sum(choices.values()),
        "selected_strategies": dict(choices),
        "selected_actions": dict(actions),
        "peer_strategies_untouched": peers_untouched,
    }, indent=2))
    return 0 if passed else 1


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, ValueError, KeyError) as error:
        print(f"Neural picker readiness failed: {error}", file=sys.stderr)
        raise SystemExit(1)
