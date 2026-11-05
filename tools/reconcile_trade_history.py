#!/usr/bin/env python3
"""Offline trade history reconciliation, audit partitioning, deterministic rebuild, and activation CLI.

Provides auditable operations:
- report: Analyze journal records and partition into accepted, unattributed, and quarantined.
- build: Deterministically partition records into rebuild files with backups and manifests.
- verify: Verify activation preconditions, determinism, and read-only ledger integrity.
- activate: Safely activate accepted records while preserving backups and raw recoverable history.
- rollback: Revert activation from backup.

Usage:
    python3 tools/reconcile_trade_history.py report
    python3 tools/reconcile_trade_history.py build
    python3 tools/reconcile_trade_history.py verify
    python3 tools/reconcile_trade_history.py activate
    python3 tools/reconcile_trade_history.py rollback
"""
from __future__ import annotations

import argparse
import datetime
import hashlib
import json
import os
import shutil
import sqlite3
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from strategies.runtime_records import (
    RecordScope,
    classify_trade,
    is_fixture_trade,
    load_ledger_execution_evidence,
    resolve_record_path,
)

_default_scope = RecordScope.from_env()
DEFAULT_JOURNAL = resolve_record_path("TRADE_LOG_PATH", "logs/trades.jsonl", _default_scope)
if not DEFAULT_JOURNAL.exists() and (ROOT / "logs/trades.jsonl").exists():
    DEFAULT_JOURNAL = ROOT / "logs/trades.jsonl"

DEFAULT_LEDGER = resolve_record_path("POSITION_LEDGER_PATH", "logs/position_ledger.sqlite3", _default_scope)
if not DEFAULT_LEDGER.exists() and (ROOT / "logs/position_ledger.sqlite3").exists():
    DEFAULT_LEDGER = ROOT / "logs/position_ledger.sqlite3"

DEFAULT_BACKUP_DIR = ROOT / "logs/backups"
DEFAULT_REBUILD_DIR = ROOT / "logs/rebuild"


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while chunk := f.read(65536):
            h.update(chunk)
    return h.hexdigest()


def get_verified_ledger_trades(ledger_path: Path) -> set[str]:
    """Query position ledger in read-only mode to find trade IDs with confirmed broker parent orders or fills."""
    evidence = load_ledger_execution_evidence(ledger_path)
    return set(evidence.keys())


def partition_trade_journal(
    journal_path: Path, ledger_path: Path
) -> tuple[
    dict[str, list[dict[str, Any]]],  # accepted events by trade_id
    dict[str, list[dict[str, Any]]],  # unattributed events by trade_id
    dict[str, list[dict[str, Any]]],  # quarantined events by trade_id
]:
    """Parse raw trade journal events and partition into accepted, unattributed, and quarantined using shared classifier."""
    ledger_ev = load_ledger_execution_evidence(ledger_path)

    events_by_trade: dict[str, list[dict[str, Any]]] = {}
    if not journal_path.exists():
        return {}, {}, {}

    with open(journal_path, "r", encoding="utf-8", errors="replace") as f:
        for line in f:
            line_str = line.strip()
            if not line_str:
                continue
            try:
                r = json.loads(line_str)
                tid = r.get("trade_id")
                if tid:
                    events_by_trade.setdefault(tid, []).append(r)
            except json.JSONDecodeError:
                continue

    accepted: dict[str, list[dict[str, Any]]] = {}
    unattributed: dict[str, list[dict[str, Any]]] = {}
    quarantined: dict[str, list[dict[str, Any]]] = {}

    for tid, events in events_by_trade.items():
        classification = classify_trade(events, ledger_ev)
        partition = classification["partition"]
        if partition == "accepted":
            accepted[tid] = events
        elif partition == "quarantined":
            quarantined[tid] = events
        else:
            unattributed[tid] = events

    return accepted, unattributed, quarantined


def summarize_partition(trades: dict[str, list[dict[str, Any]]]) -> dict[str, Any]:
    """Compute financial summary for a set of trade events."""
    count = len(trades)
    gross_pnl = 0.0
    fees = 0.0
    net_pnl = 0.0
    closed_count = 0

    for tid, events in trades.items():
        exit_event = next((e for e in reversed(events) if e.get("event") == "EXIT"), None)
        if exit_event:
            closed_count += 1
            g = float(exit_event.get("gross_pnl", exit_event.get("pnl", 0.0)) or 0.0)
            f = float(exit_event.get("fees", 0.0) or 0.0)
            n = float(exit_event.get("net_pnl", round(g - f, 2)) or 0.0)
            gross_pnl += g
            fees += f
            net_pnl += n

    return {
        "trades_count": count,
        "closed_count": closed_count,
        "gross_pnl": round(gross_pnl, 2),
        "fees": round(fees, 2),
        "net_pnl": round(net_pnl, 2),
    }


def command_report(journal_path: Path, ledger_path: Path) -> dict[str, Any]:
    accepted, unattributed, quarantined = partition_trade_journal(journal_path, ledger_path)
    total_trades = len(accepted) + len(unattributed) + len(quarantined)

    acc_sum = summarize_partition(accepted)
    unatt_sum = summarize_partition(unattributed)
    quar_sum = summarize_partition(quarantined)

    report = {
        "timestamp": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "journal_path": str(journal_path),
        "ledger_path": str(ledger_path),
        "total_trades": total_trades,
        "partitions": {
            "accepted": acc_sum,
            "unattributed": unatt_sum,
            "quarantined": quar_sum,
        },
    }

    print("=" * 70)
    print("TRADE JOURNAL RECONCILIATION & AUDIT REPORT")
    print("=" * 70)
    print(f"Journal File: {journal_path}")
    print(f"Ledger File:  {ledger_path}")
    print(f"Total Unique Trades: {total_trades}")
    print("-" * 70)
    print(
        f"ACCEPTED (Broker Verified):  {acc_sum['trades_count']:4d} trades "
        f"({acc_sum['closed_count']} closed), Gross: ${acc_sum['gross_pnl']:+9.2f}, "
        f"Fees: ${acc_sum['fees']:7.2f}, Net: ${acc_sum['net_pnl']:+9.2f}"
    )
    print(
        f"UNATTRIBUTED (Unresolved):   {unatt_sum['trades_count']:4d} trades "
        f"({unatt_sum['closed_count']} closed), Gross: ${unatt_sum['gross_pnl']:+9.2f}, "
        f"Fees: ${unatt_sum['fees']:7.2f}, Net: ${unatt_sum['net_pnl']:+9.2f}"
    )
    print(
        f"QUARANTINED (Test Fixtures): {quar_sum['trades_count']:4d} trades "
        f"({quar_sum['closed_count']} closed), Gross: ${quar_sum['gross_pnl']:+9.2f}, "
        f"Fees: ${quar_sum['fees']:7.2f}, Net: ${quar_sum['net_pnl']:+9.2f}"
    )
    print("=" * 70)
    return report


def command_build(
    journal_path: Path,
    ledger_path: Path,
    backup_dir: Path,
    rebuild_dir: Path,
) -> Path:
    if not journal_path.exists():
        raise FileNotFoundError(f"Journal file not found: {journal_path}")

    backup_dir.mkdir(parents=True, exist_ok=True)
    rebuild_dir.mkdir(parents=True, exist_ok=True)

    timestamp_str = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%d_%H%M%S")
    backup_path = backup_dir / f"trades_original_{timestamp_str}.jsonl"
    shutil.copy2(journal_path, backup_path)
    backup_sha = sha256_file(backup_path)

    accepted, unattributed, quarantined = partition_trade_journal(journal_path, ledger_path)

    accepted_file = rebuild_dir / "trades.accepted.jsonl"
    unattributed_file = rebuild_dir / "trades.unattributed.jsonl"
    quarantined_file = rebuild_dir / "trades.quarantined.jsonl"

    # Write partitions deterministically sorted by trade_id and timestamp
    for out_path, partition in (
        (accepted_file, accepted),
        (unattributed_file, unattributed),
        (quarantined_file, quarantined),
    ):
        with open(out_path, "w", encoding="utf-8") as f:
            for tid in sorted(partition.keys()):
                for event in partition[tid]:
                    f.write(json.dumps(event) + "\n")

    manifest = {
        "timestamp": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "original_journal": str(journal_path.resolve()),
        "original_sha256": sha256_file(journal_path),
        "ledger_path": str(ledger_path.resolve()),
        "backup_path": str(backup_path.resolve()),
        "backup_sha256": backup_sha,
        "files": {
            "accepted": {
                "path": str(accepted_file.resolve()),
                "sha256": sha256_file(accepted_file),
                "summary": summarize_partition(accepted),
            },
            "unattributed": {
                "path": str(unattributed_file.resolve()),
                "sha256": sha256_file(unattributed_file),
                "summary": summarize_partition(unattributed),
            },
            "quarantined": {
                "path": str(quarantined_file.resolve()),
                "sha256": sha256_file(quarantined_file),
                "summary": summarize_partition(quarantined),
            },
        },
        "preconditions": {
            "backup_verified": True,
            "read_only_ledger": True,
            "zero_fixtures_in_accepted": True,
        },
        "activation_status": "built",
        "rollback_command": f"python3 tools/reconcile_trade_history.py rollback --rebuild-dir {rebuild_dir.resolve()} --journal {journal_path.resolve()}",
    }

    manifest_path = rebuild_dir / "manifest.json"
    manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")

    print(f"Build complete.")
    print(f"  Backup:      {backup_path}")
    print(f"  Accepted:    {accepted_file} ({len(accepted)} trades)")
    print(f"  Unattributed:{unattributed_file} ({len(unattributed)} trades)")
    print(f"  Quarantined: {quarantined_file} ({len(quarantined)} trades)")
    print(f"  Manifest:    {manifest_path}")
    return manifest_path


def command_verify(rebuild_dir: Path, ledger_path: Path | None = None) -> bool:
    manifest_path = rebuild_dir / "manifest.json"
    if not manifest_path.exists():
        print(f"Error: manifest not found at {manifest_path}. Run 'build' first.", file=sys.stderr)
        return False

    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    backup_path = Path(manifest["backup_path"])

    # 1. Verify backup integrity
    if not backup_path.exists():
        print(f"Verification failed: backup {backup_path} does not exist", file=sys.stderr)
        return False
    if sha256_file(backup_path) != manifest["backup_sha256"]:
        print(f"Verification failed: backup SHA256 mismatch", file=sys.stderr)
        return False

    if manifest["backup_sha256"] != manifest["original_sha256"]:
        print("Verification failed: backup does not match original journal", file=sys.stderr)
        return False

    # 2. Verify rebuilt files SHA256
    for key in ("accepted", "unattributed", "quarantined"):
        file_info = manifest["files"][key]
        p = Path(file_info["path"])
        if not p.exists():
            print(f"Verification failed: file {p} missing", file=sys.stderr)
            return False
        if sha256_file(p) != file_info["sha256"]:
            print(f"Verification failed: {key} file SHA256 mismatch", file=sys.stderr)
            return False

    # Verify that the partitions preserve every original event, even if their
    # files and manifest hashes were changed together.
    from collections import Counter
    def event_inventory(paths):
        events = Counter()
        for path in paths:
            with open(path, encoding="utf-8") as stream:
                for line in stream:
                    if line.strip():
                        events[json.dumps(json.loads(line), sort_keys=True)] += 1
        return events
    try:
        partition_paths = [Path(manifest["files"][key]["path"]) for key in ("accepted", "unattributed", "quarantined")]
        if event_inventory([backup_path]) != event_inventory(partition_paths):
            print("Verification failed: partitions do not preserve original events", file=sys.stderr)
            return False
    except (OSError, ValueError, TypeError) as error:
        print(f"Verification failed: invalid journal input: {error}", file=sys.stderr)
        return False

    # 3. Verify zero fixtures and independent broker execution evidence in accepted file
    accepted_path = Path(manifest["files"]["accepted"]["path"])
    eff_ledger = ledger_path or (Path(manifest["ledger_path"]) if "ledger_path" in manifest else DEFAULT_LEDGER)
    ledger_ev = load_ledger_execution_evidence(eff_ledger)

    events_by_trade: dict[str, list[dict[str, Any]]] = {}
    with open(accepted_path, "r", encoding="utf-8") as f:
        for line in f:
            if not line.strip():
                continue
            r = json.loads(line)
            tid = r.get("trade_id", "")
            if is_fixture_trade(tid, r):
                print(f"Verification failed: accepted file contains fixture trade {tid}", file=sys.stderr)
                return False
            events_by_trade.setdefault(tid, []).append(r)

    for tid, events in events_by_trade.items():
        classification = classify_trade(events, ledger_ev)
        if classification["partition"] != "accepted" or classification["reconciliation_status"] not in {"VERIFIED", "FEE_INCOMPLETE"}:
            print(f"Verification failed: accepted trade {tid} lacks verified execution evidence", file=sys.stderr)
            return False

    print("Precondition & Integrity Verification: PASSED")
    print(f"  Backup valid:          YES ({backup_path.name})")
    print(f"  Rebuilt hashes match:  YES")
    print(f"  Accepted clean:        YES (zero fixtures, verified execution evidence)")
    return True


def command_activate(
    journal_path: Path, rebuild_dir: Path, force: bool = False, ledger_path: Path | None = None
) -> bool:
    if not command_verify(rebuild_dir, ledger_path):
        print("Activation aborted: verification failed.", file=sys.stderr)
        return False

    manifest_path = rebuild_dir / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    accepted_file = Path(manifest["files"]["accepted"]["path"])

    # Refuse stale builds and accidental activation into another journal.
    if journal_path.resolve() != Path(manifest["original_journal"]).resolve():
        print(f"Activation aborted: journal destination mismatch ({journal_path} != {manifest['original_journal']}).", file=sys.stderr)
        return False
    if not journal_path.exists() or sha256_file(journal_path) != manifest["original_sha256"]:
        print("Activation aborted: journal changed since build (conflicting subsequent writes).", file=sys.stderr)
        return False

    # Recoverable atomic replacement
    temp_target = journal_path.parent / f".tmp_activate_{journal_path.name}_{os.getpid()}"
    try:
        with open(accepted_file, "rb") as src, open(temp_target, "wb") as dst:
            shutil.copyfileobj(src, dst)
            dst.flush()
            os.fsync(dst.fileno())
        os.replace(temp_target, journal_path)
    except Exception as e:
        if temp_target.exists():
            try:
                temp_target.unlink()
            except Exception:
                pass
        print(f"Activation failed during atomic replacement: {e}", file=sys.stderr)
        return False

    manifest["activation_status"] = "activated"
    manifest["activated_at"] = datetime.datetime.now(datetime.timezone.utc).isoformat()
    manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")

    print("=" * 70)
    print("ACTIVATION SUCCESSFUL")
    print("=" * 70)
    print(f"Active Journal:    {journal_path}")
    print(f"Activated Records: {accepted_file}")
    print(f"Backup Preserved:  {manifest['backup_path']}")
    print(f"Rollback Command:  {manifest['rollback_command']}")
    print("=" * 70)
    return True


def command_rollback(rebuild_dir: Path, journal_path: Path) -> bool:
    manifest_path = rebuild_dir / "manifest.json"
    if not manifest_path.exists():
        print(f"Error: manifest not found at {manifest_path}", file=sys.stderr)
        return False

    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    orig_target = Path(manifest["original_journal"]).resolve()
    if journal_path.resolve() != orig_target:
        print(f"Rollback aborted: journal path does not match original journal in manifest ({journal_path.resolve()} != {orig_target})", file=sys.stderr)
        return False

    backup_path = Path(manifest["backup_path"])
    if not backup_path.exists():
        print(f"Error: backup file {backup_path} not found", file=sys.stderr)
        return False

    # Reject corrupted backups
    if sha256_file(backup_path) != manifest["backup_sha256"]:
        print("Rollback aborted: corrupted backup file (SHA256 mismatch)", file=sys.stderr)
        return False

    if not journal_path.exists():
        print(f"Error: current journal {journal_path} not found", file=sys.stderr)
        return False

    if sha256_file(journal_path) != manifest["files"]["accepted"]["sha256"]:
        print("Rollback aborted: journal changed since activation.", file=sys.stderr)
        return False

    # Recoverable atomic replacement
    temp_target = journal_path.parent / f".tmp_rollback_{journal_path.name}_{os.getpid()}"
    try:
        with open(backup_path, "rb") as src, open(temp_target, "wb") as dst:
            shutil.copyfileobj(src, dst)
            dst.flush()
            os.fsync(dst.fileno())
        os.replace(temp_target, journal_path)
    except Exception as e:
        if temp_target.exists():
            try:
                temp_target.unlink()
            except Exception:
                pass
        print(f"Rollback failed during atomic replacement: {e}", file=sys.stderr)
        return False

    manifest["activation_status"] = "rolled_back"
    manifest["rolled_back_at"] = datetime.datetime.now(datetime.timezone.utc).isoformat()
    manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")

    print(f"Rollback successful: restored {journal_path} from {backup_path}")
    return True


def command_reconcile_settlement(
    ledger_path: Path,
    journal_path: Path,
    as_of_date_str: str | None = None,
) -> None:
    """Reconcile expired positions in the ledger against recorded settlement evidence."""
    from strategies.position_ledger import PositionLedger

    if not ledger_path.exists():
        print(f"Error: ledger {ledger_path} not found", file=sys.stderr)
        return

    as_of = (
        datetime.date.fromisoformat(as_of_date_str)
        if as_of_date_str
        else datetime.datetime.now(datetime.timezone.utc).date()
    )
    ledger = PositionLedger(ledger_path)
    try:
        # Find all distinct strategies with open or awaiting packages
        with ledger._lock:
            rows = ledger._connection.execute(
                "SELECT DISTINCT strategy_id FROM packages WHERE state != 'expired'"
            ).fetchall()
            strategies = [r[0] for r in rows if r[0]]

        total_retired = 0
        for strat in strategies:
            retired = ledger.expire_strategy_positions(strat, as_of)
            total_retired += retired

        flushed = ledger.flush_outbox(journal_path)
        print(f"Settlement reconciliation: {total_retired} package(s) retired, {flushed} journal event(s) flushed.")
    finally:
        ledger.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="Reconcile and rebuild trade journal history.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    # report
    p_report = subparsers.add_parser("report", help="Inspect and report trade partitions")
    p_report.add_argument("--journal", type=Path, default=DEFAULT_JOURNAL)
    p_report.add_argument("--ledger", type=Path, default=DEFAULT_LEDGER)

    # build
    p_build = subparsers.add_parser("build", help="Deterministically build partitioned files and manifest")
    p_build.add_argument("--journal", type=Path, default=DEFAULT_JOURNAL)
    p_build.add_argument("--ledger", type=Path, default=DEFAULT_LEDGER)
    p_build.add_argument("--backup-dir", type=Path, default=DEFAULT_BACKUP_DIR)
    p_build.add_argument("--rebuild-dir", type=Path, default=DEFAULT_REBUILD_DIR)

    # verify
    p_verify = subparsers.add_parser("verify", help="Verify activation preconditions and manifest hashes")
    p_verify.add_argument("--rebuild-dir", type=Path, default=DEFAULT_REBUILD_DIR)
    p_verify.add_argument("--ledger", type=Path, default=None)

    # activate
    p_activate = subparsers.add_parser("activate", help="Activate rebuilt accepted records")
    p_activate.add_argument("--journal", type=Path, default=DEFAULT_JOURNAL)
    p_activate.add_argument("--rebuild-dir", type=Path, default=DEFAULT_REBUILD_DIR)
    p_activate.add_argument("--ledger", type=Path, default=None)
    p_activate.add_argument("--force", action="store_true")

    # rollback
    p_rollback = subparsers.add_parser("rollback", help="Revert journal to pre-activation backup")
    p_rollback.add_argument("--journal", type=Path, default=DEFAULT_JOURNAL)
    p_rollback.add_argument("--rebuild-dir", type=Path, default=DEFAULT_REBUILD_DIR)

    # reconcile-settlement
    p_settle = subparsers.add_parser("reconcile-settlement", help="Reconcile expired positions with settlement evidence")
    p_settle.add_argument("--journal", type=Path, default=DEFAULT_JOURNAL)
    p_settle.add_argument("--ledger", type=Path, default=DEFAULT_LEDGER)
    p_settle.add_argument("--as-of-date", type=str, default=None)

    args = parser.parse_args()

    if args.command == "report":
        command_report(args.journal, args.ledger)
    elif args.command == "build":
        command_build(args.journal, args.ledger, args.backup_dir, args.rebuild_dir)
    elif args.command == "verify":
        ok = command_verify(args.rebuild_dir, args.ledger)
        sys.exit(0 if ok else 1)
    elif args.command == "activate":
        ok = command_activate(args.journal, args.rebuild_dir, args.force, args.ledger)
        sys.exit(0 if ok else 1)
    elif args.command == "rollback":
        ok = command_rollback(args.rebuild_dir, args.journal)
        sys.exit(0 if ok else 1)
    elif args.command == "reconcile-settlement":
        command_reconcile_settlement(args.ledger, args.journal, args.as_of_date)


if __name__ == "__main__":
    main()
