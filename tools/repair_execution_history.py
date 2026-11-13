#!/usr/bin/env python3
"""Build and verify an evidence-only repair before activating stopped writers.

The repair may add provenance and mark fees unknown. It cannot alter recorded
fills, ownership, cash flows, exit reasons or settlements. Every original event
is retained, including records that remain unattributed.
"""
from __future__ import annotations

import argparse
from collections import Counter
from datetime import date, datetime
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import sys
import tempfile
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1] if Path(__file__).is_file() else Path.cwd()
sys.path.insert(0, str(ROOT))

from strategies.position_ledger import PositionLedger
from strategies.runtime_records import RecordScope, classify_trade, is_fixture_trade, load_ledger_execution_evidence
from strategies.scoped_recovery import contract_symbol


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def financial_fingerprint(path: Path) -> str:
    """Exclude new evidence/broker aliases, but preserve every financial record."""
    selections = {
        "packages": "package_id,strategy_id,intent,state,opening_cash_flow,closing_cash_flow,position_id,trade_id",
        "legs": "order_id,package_id,asset_key,requested_signed_quantity,filled_signed_quantity,fill_price,status",
        "positions": "*", "system_state": "*", "journal_events": "*",
        "settlement_values": "*", "broker_cash_evidence": "*", "settlement_reconciliations": "*",
        "fee_evidence": "*", "outbox": "*", "recovery_jobs": "*", "emergency_submissions": "*",
    }
    with sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True) as db:
        records = {table: sorted(db.execute(f"SELECT {columns} FROM {table}").fetchall(), key=repr)
                   for table, columns in selections.items()}
    return hashlib.sha256(json.dumps(records, sort_keys=True).encode()).hexdigest()


def read_events(paths: list[Path]) -> list[dict]:
    records = []
    identities = {}
    for path in paths:
        for line in path.read_text().splitlines():
            if not line.strip():
                continue
            event = json.loads(line)
            identity = event.get("event_id")
            if identity and identity in identities:
                if identities[identity] != event:
                    raise ValueError("Conflicting duplicate journal event")
                continue
            if identity:
                identities[identity] = event
            records.append(event)
    return records


def enrich_event(ledger: PositionLedger, event: dict, captures: Path) -> dict:
    if is_fixture_trade(event):
        return event
    package_id = event.get("package_id") or str(event.get("event_id", "")).split(":")[0]
    if not package_id:
        intent = {"ENTRY": "open", "ADDITION": "add", "ADJUSTMENT": "trim", "EXIT": "close"}.get(event.get("event"))
        matches = ledger._connection.execute(
            "SELECT package_id FROM packages p WHERE trade_id=? AND strategy_id=? AND intent=? "
            "AND EXISTS(SELECT 1 FROM legs l WHERE l.package_id=p.package_id AND l.filled_signed_quantity!=0)",
            (event.get("trade_id"), event.get("strategy"), intent),
        ).fetchall()
        if len(matches) != 1:
            return event
        package_id = matches[0][0]
    try:
        basket = ledger.get_basket(package_id)
    except KeyError:
        return event
    if (not basket.broker_parent_id or basket.trade_id != event.get("trade_id")
            or basket.strategy_id != event.get("strategy")):
        return event
    parent_id = str(basket.broker_parent_id)
    if any(char not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-" for char in parent_id):
        raise ValueError("Invalid broker identity")
    capture_path = captures / f"order-{parent_id}.json"
    if not capture_path.exists():
        return event
    capture = json.loads(capture_path.read_text())
    scope = ledger.record_scope
    expected_source = "https://api.tradier.com/v1" if scope.environment == "live" else "https://sandbox.tradier.com/v1"
    if capture.get("scope") != scope.to_dict() or capture.get("source") != expected_source:
        raise ValueError("Captured provider scope differs from ledger")
    acquired = datetime.fromisoformat(capture["acquired_at"].replace("Z", "+00:00"))
    if acquired.utcoffset() is None:
        raise ValueError("Capture time requires a timezone")
    raw = capture["payload"].get("order")
    if not isinstance(raw, dict) or str(raw.get("id")) != parent_id:
        raise ValueError("Captured order identity differs from ledger")
    children = raw.get("leg") or [raw]
    children = [children] if isinstance(children, dict) else children
    expected = {contract_symbol(leg["asset_key"]): leg for leg in basket.legs}
    if len(children) != len(expected):
        raise ValueError("Captured order has different contracts")
    observations = []
    seen = set()
    for child in children:
        symbol = child.get("option_symbol") or child.get("symbol")
        leg = expected.get(symbol)
        if leg is None or symbol in seen or not child.get("id"):
            raise ValueError("Captured leg cannot be assigned to the package")
        seen.add(symbol)
        sign = 1 if leg["requested_signed_quantity"] > 0 else -1
        suffix = "open" if basket.intent in {"open", "add"} else "close"
        side = ("buy_to_" if sign > 0 else "sell_to_") + suffix
        executed = float(child.get("exec_quantity", 0))
        if (child.get("side") != side or executed * sign != leg["filled_signed_quantity"]
                or float(child.get("quantity", 0)) != abs(leg["requested_signed_quantity"])
                or (executed and abs(float(child["avg_fill_price"]) - float(leg["fill_price"])) > 1e-8)):
            raise ValueError("Captured executions differ from recorded fills; manual reconciliation required")
        observation = SimpleNamespace(identifier=str(child["id"]), side=side, _raw=child,
                                      asset=SimpleNamespace(**json.loads(leg["asset_key"]), asset_type="option", multiplier=100))
        observations.append((leg, observation))
    ledger.bind_broker_children(package_id, [observation for _, observation in observations])
    for leg, observation in observations:
        if leg["filled_signed_quantity"]:
            ledger.capture_order_evidence(observation, leg, scope=scope,
                                          parent_response=raw if raw.get("leg") else None,
                                          acquired_at=capture["acquired_at"])
    event_id = event.get("event_id")
    if not event_id:
        existing_ids = ledger._connection.execute(
            "SELECT DISTINCT event_id FROM realization_evidence_links WHERE package_id=? AND trade_id=? AND position_id=?",
            (package_id, event["trade_id"], basket.position_id),
        ).fetchall()
        if len(existing_ids) > 1:
            return event
        event_id = existing_ids[0][0] if existing_ids else f"{package_id}:{'REALIZATION' if event['event'] == 'EXIT' else event['event']}"
    fields = ledger.attach_package_evidence(package_id, event_id, event["trade_id"])
    if len(fields["legs"]) != sum(bool(leg["filled_signed_quantity"]) for leg in basket.legs):
        raise ValueError("Not every recorded execution has attributable retained evidence")
    enriched = {**event, **fields, "scope": scope.to_dict(), "event_id": event_id}
    # Existing numeric fees are estimates until actual-fee evidence covers them.
    for field in ("entry_fees", "fees"):
        if field in enriched and enriched[field] is not None:
            enriched["estimated_" + field] = enriched[field]
            enriched[field] = None
    if "net_pnl" in enriched:
        enriched["net_pnl"] = None
    if "pnl" in enriched:
        enriched["pnl"] = None
    return enriched


def classifications(events: list[dict], ledger: Path, scope: RecordScope, as_of: str) -> dict:
    evidence = load_ledger_execution_evidence(ledger, expected_scope=scope, as_of_date=as_of)
    grouped = {}
    for event in events:
        grouped.setdefault(event["trade_id"], []).append(event)
    return {trade: classify_trade(rows, evidence, expected_scope=scope, as_of_date=as_of)
            for trade, rows in grouped.items()}


def build(ledger_path: Path, journals: list[Path], captures: Path, output: Path, as_of: str) -> dict:
    date.fromisoformat(as_of)
    output.mkdir(parents=True, exist_ok=False)
    source_hash = digest(ledger_path)
    original_fingerprint = financial_fingerprint(ledger_path)
    original_events = read_events(journals)
    for index, path in enumerate(journals):
        shutil.copy2(path, output / f"input-{index}.jsonl")
    staged = output / "ledger.sqlite3"
    with sqlite3.connect(ledger_path.resolve().as_uri() + "?mode=ro", uri=True) as source:
        row = source.execute("SELECT broker,account_id,environment FROM record_scope").fetchone()
        scope = RecordScope(broker=row[0], account_id=row[1], environment=row[2])
        with sqlite3.connect(staged) as target:
            source.backup(target)
    ledger = PositionLedger(staged, scope=scope)
    try:
        events = [enrich_event(ledger, event, captures) for event in original_events]
    finally:
        ledger.close()
    if digest(ledger_path) != source_hash or financial_fingerprint(staged) != original_fingerprint:
        raise ValueError("Source changed during planning or repair altered financial records")
    candidate = output / "trades.jsonl"
    candidate.write_text("".join(json.dumps(event, sort_keys=True) + "\n" for event in events))
    verdicts = classifications(events, staged, scope, as_of)
    manifest = {"version": 1, "as_of": as_of, "scope": scope.to_dict(),
                "ledger_source": str(ledger_path.resolve()), "ledger_source_sha256": source_hash,
                "financial_fingerprint": original_fingerprint,
                "journals": {str(path.resolve()): digest(path) for path in journals},
                "originals": {f"input-{index}.jsonl": digest(path) for index, path in enumerate(journals)},
                "files": {"ledger.sqlite3": digest(staged), "trades.jsonl": digest(candidate)},
                "classifications": verdicts,
                "summary": dict(Counter(value["reconciliation_status"] for value in verdicts.values())),
                "events": len(events), "enriched_events": sum(a != b for a, b in zip(original_events, events))}
    (output / "manifest.json").write_text(json.dumps(manifest, sort_keys=True, indent=2))
    verify(output)
    return manifest


def verify(output: Path) -> dict:
    manifest = json.loads((output / "manifest.json").read_text())
    if manifest["version"] != 1:
        raise ValueError("Unsupported evidence-repair manifest")
    for name, expected in manifest["files"].items():
        if name not in {"ledger.sqlite3", "trades.jsonl"} or digest(output / name) != expected:
            raise ValueError("Repair artifact hash mismatch")
    if financial_fingerprint(output / "ledger.sqlite3") != manifest["financial_fingerprint"]:
        raise ValueError("Repair altered financial records")
    for name, expected in manifest["originals"].items():
        if Path(name).name != name or digest(output / name) != expected:
            raise ValueError("Frozen original journal hash mismatch")
    original = read_events([output / name for name in manifest["originals"]])
    events = read_events([output / "trades.jsonl"])
    if len(original) != len(events):
        raise ValueError("Repair removed or added journal events")
    mutable = {"scope", "event_id", "position_id", "package_id", "legs", "evidence_refs", "fee_provenance", "fees_complete",
               "entry_fees", "fees", "net_pnl", "pnl", "estimated_entry_fees", "estimated_fees"}
    for before, after in zip(original, events):
        if before.get("event_id") and before["event_id"] != after.get("event_id"):
            raise ValueError("Repair changed an existing event identity")
        if {k: v for k, v in before.items() if k not in mutable} != {k: v for k, v in after.items() if k not in mutable}:
            raise ValueError("Repair changed original trade facts")
        for field in ("entry_fees", "fees", "net_pnl", "pnl"):
            if field in after and after[field] != before.get(field) and after[field] is not None:
                raise ValueError("Repair manufactured financial totals")
    scope = RecordScope(**{key: manifest["scope"][key] for key in ("environment", "broker", "account_id")})
    if classifications(events, output / "ledger.sqlite3", scope, manifest["as_of"]) != manifest["classifications"]:
        raise ValueError("Repair classification recomputation differs")
    return manifest


def activate(output: Path, target_journal: Path, *, writers_stopped: bool) -> None:
    if not writers_stopped:
        raise ValueError("Stop bot/recovery writers before activating evidence repair")
    manifest = verify(output)
    ledger = Path(manifest["ledger_source"])
    if str(target_journal.resolve()) not in manifest["journals"]:
        raise ValueError("Target journal is outside the reviewed inputs")
    ledger_applied = digest(ledger) == manifest["files"]["ledger.sqlite3"]
    journal_applied = digest(target_journal) == manifest["files"]["trades.jsonl"]
    if ledger_applied and journal_applied:
        return
    if journal_applied or (not ledger_applied and digest(ledger) != manifest["ledger_source_sha256"]) or any(
            digest(Path(path)) != expected for path, expected in manifest["journals"].items()):
        raise ValueError("Runtime changed after planning; rebuild the repair")
    if any(Path(str(ledger) + suffix).exists() for suffix in ("-wal", "-shm", "-journal")):
        raise ValueError("Ledger still has SQLite sidecars; stop all writers")
    backup = output / "before-activation"
    if not backup.exists():
        if ledger_applied:
            raise ValueError("Partially applied repair has no original ledger backup")
        backup.mkdir()
        shutil.copy2(ledger, backup / "ledger.sqlite3")
    if digest(backup / "ledger.sqlite3") != manifest["ledger_source_sha256"]:
        raise ValueError("Original ledger backup differs from the reviewed source")
    for index, path in enumerate(manifest["journals"]):
        target = backup / f"journal-{index}.jsonl"
        if not target.exists():
            shutil.copy2(path, target)
        if digest(target) != manifest["journals"][path]:
            raise ValueError("Original journal backup differs from the reviewed source")
    for source, target in ((output / "ledger.sqlite3", ledger), (output / "trades.jsonl", target_journal)):
        with tempfile.NamedTemporaryFile(dir=target.parent, delete=False) as temporary:
            temporary.write(source.read_bytes())
            temporary.flush()
            os.fsync(temporary.fileno())
            name = temporary.name
        try:
            os.replace(name, target)
            directory = os.open(target.parent, os.O_RDONLY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        finally:
            if os.path.exists(name):
                os.unlink(name)
    (output / "activated.json").write_text(json.dumps({"ledger": str(ledger), "journal": str(target_journal)}))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    plan = commands.add_parser("build")
    plan.add_argument("--ledger", type=Path, required=True)
    plan.add_argument("--journals", nargs="+", type=Path, required=True)
    plan.add_argument("--captures", type=Path, required=True)
    plan.add_argument("--output", type=Path, required=True)
    plan.add_argument("--as-of", required=True)
    check = commands.add_parser("verify")
    check.add_argument("--output", type=Path, required=True)
    apply = commands.add_parser("activate")
    apply.add_argument("--output", type=Path, required=True)
    apply.add_argument("--target-journal", type=Path, required=True)
    apply.add_argument("--writers-stopped", action="store_true")
    args = parser.parse_args()
    if args.command == "build":
        result = build(args.ledger, args.journals, args.captures, args.output, args.as_of)
        print(json.dumps({key: result[key] for key in ("summary", "events", "enriched_events")}, indent=2))
    elif args.command == "verify":
        verify(args.output)
        print("Evidence repair verified")
    else:
        activate(args.output, args.target_journal, writers_stopped=args.writers_stopped)
        print("Evidence repair activated with original records backed up")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
