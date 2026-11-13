#!/usr/bin/env python3
"""Retain scoped broker GET responses for an offline execution-history repair.

Completed order captures are reusable across runs. Request-budget exhaustion is
reported and can be retried later without discarding the completed evidence.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import sys

ROOT = Path(__file__).resolve().parents[1] if Path(__file__).is_file() else Path.cwd()
sys.path.insert(0, str(ROOT))


def capture(ledger_path: Path, output: Path, *, reads, scope) -> dict:
    output.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(ledger_path.resolve().as_uri() + "?mode=ro", uri=True) as db:
        bound = db.execute("SELECT broker,account_id,environment FROM record_scope").fetchone()
        if bound != (scope.broker, scope.account_id, scope.environment):
            raise ValueError("Capture credentials do not match the ledger's account scope")
        ids = [row[0] for row in db.execute(
            "SELECT DISTINCT broker_parent_id FROM packages p WHERE broker_parent_id IS NOT NULL "
            "AND EXISTS(SELECT 1 FROM legs l WHERE l.package_id=p.package_id AND l.filled_signed_quantity!=0) "
            "ORDER BY broker_parent_id")]
    report = {"requested_orders": len(ids), "captured": 0, "reused": 0, "unavailable": []}
    for order_id in ids:
        # Broker identities are filenames only after strict validation.
        if not str(order_id).isdigit():
            report["unavailable"].append({"order_id": order_id, "reason": "Non-provider order identity"})
            continue
        target = output / f"order-{order_id}.json"
        if target.exists():
            previous = json.loads(target.read_text())
            if previous.get("scope") != scope.to_dict() or str(previous.get("order_id")) != str(order_id):
                raise ValueError("Existing order capture has a different scope or identity")
            report["reused"] += 1
            continue
        try:
            payload = reads.get(f"/accounts/{scope.account_id}/orders/{order_id}", fresh=True,
                                caller="execution-evidence-capture", timeout=6)
            order = payload.get("order")
            if not isinstance(order, dict) or str(order.get("id")) != str(order_id):
                raise ValueError("Provider did not return the requested order")
            body = {"scope": scope.to_dict(), "order_id": str(order_id), "payload": payload,
                    "acquired_at": datetime.now(timezone.utc).isoformat(),
                    "source": "https://sandbox.tradier.com/v1" if reads.sandbox else "https://api.tradier.com/v1"}
            target.write_text(json.dumps(body, sort_keys=True, indent=2))
            report["captured"] += 1
        except Exception as error:
            # Shared reads redact provider transport errors and credentials.
            report["unavailable"].append({"order_id": str(order_id), "reason": str(error)})
            if "budget" in str(error).lower():
                report["remaining_after_budget"] = len(ids) - report["captured"] - report["reused"]
                break
    report["files"] = {path.name: hashlib.sha256(path.read_bytes()).hexdigest()
                       for path in sorted(output.glob("order-*.json"))}
    (output / "capture-report.json").write_text(json.dumps(report, sort_keys=True, indent=2))
    return report


def main() -> int:
    from dotenv import load_dotenv
    load_dotenv(ROOT / ".env")
    from strategies.broker_reads import get_broker_reads
    from strategies.runtime_records import RecordScope
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ledger", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    scope = RecordScope.from_env()
    token = os.getenv("TRADIER_ACCESS_TOKEN") or os.getenv("TRADIER_API_KEY") or ""
    reads = get_broker_reads(token, scope.account_id, scope.environment == "paper")
    report = capture(args.ledger, args.output, reads=reads, scope=scope)
    print(json.dumps({key: value for key, value in report.items() if key != "files"}, indent=2))
    return int(bool(report["unavailable"]))


if __name__ == "__main__":
    raise SystemExit(main())
