"""Validate backlog IDs, dependencies and the strategy audit priority order.

Run with: python3 tools/check_task_backlog.py [path/to/TASKS.yaml]
Reads YAML only. Does not import trading code or contact a broker.
"""

from __future__ import annotations

import argparse
from pathlib import Path

import yaml


def validate(data: dict) -> list[str]:
    errors = []
    tasks = data.get("tasks", [])
    by_id = {}
    for task in tasks:
        task_id = task.get("id")
        if not isinstance(task_id, str) or not task_id:
            errors.append("Task has a missing or invalid ID")
            continue
        if task_id in by_id:
            errors.append(f"Duplicate task ID: {task_id}")
        by_id[task_id] = task
        for field in ("title", "owner", "deliverables"):
            if not task.get(field):
                errors.append(f"{task_id}: missing {field}")
        if task.get("status") not in {"pending", "in_progress", "completed"}:
            errors.append(f"{task_id}: invalid status")
        if task.get("priority") not in {None, "P0", "P1", "P2", "P3"}:
            errors.append(f"{task_id}: invalid priority")

    visited, visiting = set(), set()

    def visit(task_id: str) -> None:
        if task_id in visiting:
            errors.append(f"Dependency cycle at {task_id}")
            return
        if task_id in visited:
            return
        visiting.add(task_id)
        for dependency in by_id[task_id].get("depends_on", []):
            if dependency not in by_id:
                errors.append(f"{task_id}: unknown dependency {dependency}")
            else:
                visit(dependency)
        visiting.remove(task_id)
        visited.add(task_id)

    for task_id in by_id:
        visit(task_id)

    audit = data.get("dashboard_review", {}).get("strategy_integrity_audit", {})
    order = audit.get("priority_order", [])
    if len(order) != len(set(order)):
        errors.append("Audit priority order contains duplicate IDs")
    ranks = {task_id: rank for rank, task_id in enumerate(order)}
    previous_priority = "P0"
    for task_id in order:
        if task_id not in by_id:
            errors.append(f"Audit references unknown task {task_id}")
            continue
        task = by_id[task_id]
        priority = task.get("priority")
        if priority not in {"P0", "P1", "P2", "P3"}:
            errors.append(f"{task_id}: audit task needs a priority")
        else:
            if priority < previous_priority:
                errors.append(f"{task_id}: audit priority is out of order")
            previous_priority = priority
        if task.get("status") == "completed":
            errors.append(f"{task_id}: completed task remains in audit work order")
        for dependency in task.get("depends_on", []):
            if dependency in ranks and ranks[dependency] >= ranks[task_id]:
                errors.append(f"{task_id}: audit order precedes dependency {dependency}")
    return errors


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("path", nargs="?", type=Path,
                        default=Path(__file__).resolve().parents[1] / ".scrum/TASKS.yaml")
    args = parser.parse_args()
    try:
        data = yaml.safe_load(args.path.read_text())
        if not isinstance(data, dict) or not isinstance(data.get("tasks"), list):
            raise ValueError("Expected a YAML mapping with a tasks list")
        errors = validate(data)
    except (OSError, ValueError, yaml.YAMLError) as error:
        parser.exit(1, f"Invalid backlog: {error}\n")
    for error in errors:
        print(f"FAIL: {error}")
    if errors:
        return 1
    count = len(data.get("dashboard_review", {}).get("strategy_integrity_audit", {}).get("priority_order", []))
    print(f"PASS: {len(data['tasks'])} tasks; unique IDs; valid dependencies; no cycles; {count} audit priorities checked")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
