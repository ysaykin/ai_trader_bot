#!/usr/bin/env python3
"""Reproduce order, expired-position and inventory warnings without changing state.

Run with the application's Python environment, for example:
  python tools/audit_reconciliation.py --ledger logs/position_ledger.sqlite3 \
    --snapshots logs/scopes/ACCOUNT_SCOPE/broker_snapshots.sqlite3 --as-of 2026-11-11 --check

An optional --evidence JSON file may contain fresh ``orders`` and ``positions``
lists and ``acquired_at``. It must contain responses for the same account as the
ledger. No broker requests, migrations, order submissions or ledger writes occur.
Exit status with --check is 1 while any of the three warnings remains.
"""
from __future__ import annotations

import argparse
from collections import defaultdict
from contextlib import closing
from datetime import date, datetime, timezone
import json
from pathlib import Path
import sqlite3
import sys

ROOT = Path(__file__).resolve().parents[1] if Path(__file__).is_file() else Path.cwd()
sys.path.insert(0, str(ROOT))

from dashboard.monitoring import order_activity
from dashboard.positions import build_positions, read_packages


def broker_rows(payload, root, child):
    value = payload[root]
    if value in (None, "null"):
        return []
    value = value[child]
    return [value] if isinstance(value, dict) else value


def audit(ledger_path: Path, snapshot_path: Path, *, as_of: date,
          evidence: dict | None = None) -> dict:
    packages = read_packages(ledger_path)
    with closing(sqlite3.connect(snapshot_path.resolve().as_uri() + "?mode=ro", uri=True)) as db:
        db.row_factory = sqlite3.Row
        db.execute("BEGIN")
        latest = db.execute("SELECT scope FROM snapshots WHERE key='positions' ORDER BY success DESC LIMIT 1").fetchone()
        if latest is None:
            raise ValueError("No broker positions snapshot")
        snapshots = {row["key"]: dict(row) for row in db.execute(
            "SELECT * FROM snapshots WHERE scope=? AND key IN ('positions','orders')", (latest["scope"],))}
    positions = broker_rows(json.loads(snapshots["positions"]["payload"]), "positions", "position")
    orders = broker_rows(json.loads(snapshots["orders"]["payload"]), "orders", "order")
    if evidence is not None:
        positions, orders = evidence["positions"], evidence["orders"]
    # A fixed observation time keeps an archived audit deterministic.
    observed = max(row["success"] for row in snapshots.values())
    now = datetime.fromtimestamp(observed, timezone.utc)
    if evidence is not None:
        now = datetime.fromisoformat(evidence["acquired_at"].replace("Z", "+00:00"))
    rows, issues = build_positions(packages, positions, {}, as_of=as_of)
    activity = order_activity({"ledger_packages": packages, "raw_orders": orders,
                               "orders_status": "ready"}, now=now)
    owned = defaultdict(int)
    expired = set()
    for row in rows:
        if row["structure"] == "unattributed":
            continue
        for leg in row["legs"]:
            owned[leg["symbol"]] += leg["quantity"]
            if row.get("awaiting_settlement"):
                expired.add(leg["symbol"])
    actual = defaultdict(float)
    for position in positions:
        actual[position["symbol"]] += float(position["quantity"])
    mismatches = [{"symbol": symbol, "ledger": owned.get(symbol, 0), "broker": actual.get(symbol, 0)}
                  for symbol in sorted(owned.keys() | actual.keys())
                  if owned.get(symbol, 0) != actual.get(symbol, 0)
                  and not (symbol in expired and actual.get(symbol, 0) == 0)]
    awaiting = [{key: row[key] for key in ("strategy", "trade_id", "expiry", "quantity", "cash_flow")}
                for row in rows if row.get("awaiting_settlement")]
    reviews = [row for row in activity if row["needs_review"]]
    with closing(sqlite3.connect(ledger_path.resolve().as_uri() + "?mode=ro", uri=True)) as db:
        evidence_counts = {table: db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                           for table in ("settlement_values", "broker_cash_evidence", "settlement_reconciliations")}
    return {"as_of": as_of.isoformat(), "broker_observed_at": now.isoformat(),
            "broker_source": "supplied evidence" if evidence is not None else "retained snapshot",
            "warning_counts": {"orders_need_review": len(reviews), "awaiting_settlement": len(awaiting),
                               "unexpected_contract_mismatches": len(mismatches)},
            "position_issues": issues, "orders_needing_review": reviews,
            "awaiting_settlement": awaiting, "inventory_mismatches": mismatches,
            "settlement_evidence_counts": evidence_counts}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ledger", type=Path, required=True)
    parser.add_argument("--snapshots", type=Path, required=True)
    parser.add_argument("--as-of", type=date.fromisoformat, required=True)
    parser.add_argument("--evidence", type=Path)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    evidence = json.loads(args.evidence.read_text()) if args.evidence else None
    report = audit(args.ledger, args.snapshots, as_of=args.as_of, evidence=evidence)
    print(json.dumps(report, indent=2))
    return int(args.check and any(report["warning_counts"].values()))


if __name__ == "__main__":
    raise SystemExit(main())
