#!/usr/bin/env python3
"""Plan and archive retired trade history without erasing unresolved ownership.

Uses SQLite and local files only. Stop every record writer before applying a
reviewed plan. The archive contains the complete ledger and original journals.
Retained legacy positions remain managed until their broker state is resolved.
"""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from contextlib import closing
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import tempfile


TERMINAL = {"closed", "canceled", "expired", "failed", "rejected"}
VARIANT = re.compile(r"IC_(V\d{2})(?:_|$)")


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def encode(value) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def read_tables(connection) -> dict[str, list[dict]]:
    names = [row[0] for row in connection.execute(
        "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
    )]
    return {name: [dict(row) for row in connection.execute(
        'SELECT * FROM "' + name.replace('"', '""') + '" ORDER BY rowid'
    )] for name in names}


def ownership(tables) -> dict:
    packages = {row["package_id"]: row for row in tables["packages"]}
    totals = defaultdict(Counter)
    for leg in tables["legs"]:
        package = packages[leg["package_id"]]
        if package["state"] != "expired":
            totals[package["strategy_id"]][leg["asset_key"]] += leg["filled_signed_quantity"]
    return {owner: {key: quantity for key, quantity in sorted(counts.items()) if quantity}
            for owner, counts in sorted(totals.items()) if any(counts.values())}


def selected(owner: str, variants: set[str]) -> bool:
    match = VARIANT.match(owner)
    return bool(match and match[1] in variants)


def partition(tables, variants: set[str]) -> dict:
    """Only remove complete terminal chains with zero remaining inventory."""
    packages = tables["packages"]
    groups = defaultdict(list)
    legs = defaultdict(list)
    for leg in tables["legs"]:
        legs[leg["package_id"]].append(leg)
    # Connect by both position and trade identities, including replacements.
    parent = {row["package_id"]: row["package_id"] for row in packages}

    def root(key):
        while parent[key] != key:
            parent[key] = parent[parent[key]]
            key = parent[key]
        return key

    identities = {}
    for row in packages:
        key = row["package_id"]
        for field in ("position_id", "trade_id"):
            identity = row.get(field)
            if identity:
                token = (field, identity)
                if token in identities:
                    parent[root(key)] = root(identities[token])
                identities[token] = key
        replacement = row.get("replacement_of")
        if replacement in parent:
            parent[root(key)] = root(replacement)
    for row in packages:
        groups[root(row["package_id"])].append(row)

    remove = set()
    retained = []
    for rows in groups.values():
        if not any(selected(row["strategy_id"], variants) for row in rows):
            continue
        reasons = set()
        quantities = Counter()
        for row in rows:
            if not selected(row["strategy_id"], variants):
                reasons.add("identity shared with a retained strategy")
            if row["state"] not in TERMINAL:
                reasons.add("unresolved order or position state")
            if row["state"] != "expired":
                for leg in legs[row["package_id"]]:
                    quantities[leg["asset_key"]] += leg["filled_signed_quantity"]
        if any(quantities.values()):
            reasons.add("nonzero owned inventory")
        if reasons:
            retained.append({"strategies": sorted({row["strategy_id"] for row in rows}),
                             "package_ids": sorted(row["package_id"] for row in rows),
                             "reasons": sorted(reasons)})
        else:
            remove.update(row["package_id"] for row in rows)

    retained_packages = [row for row in packages if row["package_id"] not in remove]
    retained_positions = {row.get("position_id") for row in retained_packages}
    retained_trades = {row.get("trade_id") for row in retained_packages}
    remove_positions = {row["position_id"] for row in tables["positions"]
                        if selected(row["strategy_id"], variants)
                        and row["position_id"] not in retained_positions
                        and row.get("trade_id") not in retained_trades - {None}}
    protected_trades = retained_trades - {None}
    protected_trades.update(row["trade_id"] for row in tables["positions"]
                            if row["position_id"] not in remove_positions and row.get("trade_id"))
    return {"package_ids": sorted(remove), "position_ids": sorted(remove_positions),
            "protected_trade_ids": sorted(protected_trades),
            "retained_legacy": sorted(retained, key=lambda item: item["package_ids"])}


def filter_journal(data: bytes, variants: set[str], protected_trades: set[str]):
    kept = []
    removed = Counter()
    for number, line in enumerate(data.splitlines(keepends=True), 1):
        if not line.strip():
            kept.append(line)
            continue
        try:
            row = json.loads(line)
        except (ValueError, UnicodeDecodeError) as error:
            raise ValueError(f"Malformed journal row {number}; refusing to discard evidence") from error
        if not isinstance(row, dict):
            raise ValueError(f"Journal row {number} must be an object")
        owners = {str(row[field]) for field in ("strategy", "strategy_id", "strategy_name") if row.get(field)}
        if len(owners) > 1:
            raise ValueError(f"Conflicting owners on journal row {number}")
        owner = next(iter(owners), "")
        if selected(owner, variants) and row.get("trade_id") not in protected_trades:
            removed[owner] += 1
        else:
            kept.append(line)
    return b"".join(kept), dict(sorted(removed.items()))


def make_plan(ledger: Path, journals: list[Path], variants: set[str]) -> dict:
    with closing(sqlite3.connect(ledger.resolve().as_uri() + "?mode=ro", uri=True)) as connection:
        connection.row_factory = sqlite3.Row
        connection.execute("BEGIN")
        tables = read_tables(connection)
    result = partition(tables, variants)
    journal_plans = []
    for path in journals:
        data = path.read_bytes()
        after, counts = filter_journal(data, variants, set(result["protected_trade_ids"]))
        journal_plans.append({"path": str(path.resolve()), "before_sha256": digest(data),
                              "after_sha256": digest(after), "removed_events": sum(counts.values()),
                              "removed_by_strategy": counts})
    return {"version": 1, "ledger": str(ledger.resolve()), "retire_variants": sorted(variants),
            "ledger_before_sha256": digest(encode(tables)), "journals": journal_plans, **result}


def atomic_write(path: Path, data: bytes) -> None:
    descriptor, temporary = tempfile.mkstemp(prefix=path.name + ".", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, path.stat().st_mode & 0o777 if path.exists() else 0o600)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def deletion_keys(tables, plan) -> dict[str, list[str | int]]:
    packages, positions = set(plan["package_ids"]), set(plan["position_ids"])
    trades = {row["trade_id"] for row in tables["positions"]
              if row["position_id"] in positions and row.get("trade_id")}
    trades.update(row["trade_id"] for row in tables["packages"]
                  if row["package_id"] in packages and row.get("trade_id"))
    keys = {"packages": list(packages), "positions": list(positions)}
    for table, primary in (("legs", "order_id"), ("fee_evidence", "identity"),
                           ("settlement_reconciliations", "position_id"), ("outbox", "outbox_id")):
        keys[table] = []
        for row in tables.get(table, []):
            identity = json.loads(row["payload"]) if table == "outbox" else row
            if (identity.get("package_id") in packages or identity.get("position_id") in positions
                    or identity.get("trade_id") in trades):
                # A conflicting reference could otherwise erase a current owner's evidence.
                for field, removed in (("package_id", packages), ("position_id", positions), ("trade_id", trades)):
                    if identity.get(field) and identity[field] not in removed:
                        raise ValueError(f"Conflicting retained reference in {table}")
                keys[table].append(row[primary])
    return keys


def apply_plan(plan: dict, archive: Path, *, writers_stopped: bool = False) -> dict:
    if not writers_stopped:
        raise ValueError("Stop all record writers before applying; --writers-stopped is required")
    ledger = Path(plan["ledger"])
    journals = [Path(row["path"]) for row in plan["journals"]]
    current = make_plan(ledger, journals, set(plan["retire_variants"]))
    if current != plan:
        raise ValueError("Records changed since planning; generate and review a new plan")
    with closing(sqlite3.connect(ledger)) as connection:
        connection.row_factory = sqlite3.Row
        tables = read_tables(connection)
        # These workers can append or act independently of the main trading loop.
        if any(row["status"] not in {"completed", "failed", "canceled"}
               for row in tables.get("recovery_jobs", [])):
            raise ValueError("Unresolved recovery jobs require review before cleanup")
        if any(row["status"] not in {"filled", "canceled", "rejected", "failed"}
               for row in tables.get("emergency_submissions", [])):
            raise ValueError("Unresolved emergency submissions require review before cleanup")
        if any(row["status"] == "pending" for row in tables.get("outbox", [])):
            raise ValueError("Dispatch pending journal outbox before cleanup")
        keys = deletion_keys(tables, plan)
        archive.mkdir(parents=True, exist_ok=False, mode=0o700)
        with closing(sqlite3.connect(archive / "ledger.sqlite3")) as backup:
            connection.backup(backup)
        originals = [path.read_bytes() for path in journals]
        for index, data in enumerate(originals):
            (archive / f"journal-{index}.jsonl").write_bytes(data)
        (archive / "plan.json").write_bytes(encode(plan) + b"\n")
        replaced = []
        committed = False
        try:
            connection.execute("PRAGMA foreign_keys=ON")
            connection.execute("BEGIN IMMEDIATE")
            if digest(encode(read_tables(connection))) != plan["ledger_before_sha256"]:
                raise ValueError("Ledger changed while acquiring the cleanup lock")
            if connection.execute("PRAGMA foreign_key_check").fetchall():
                raise ValueError("Ledger has existing foreign key violations")
            # Child rows precede packages and positions to preserve foreign keys.
            for table, primary in (("legs", "order_id"), ("fee_evidence", "identity"),
                                   ("settlement_reconciliations", "position_id"), ("outbox", "outbox_id")):
                connection.executemany(f"DELETE FROM {table} WHERE {primary}=?", [(key,) for key in keys[table]])
            connection.executemany("DELETE FROM journal_events WHERE package_id=?", [(key,) for key in plan["package_ids"]])
            for table, primary in (("packages", "package_id"), ("positions", "position_id")):
                connection.executemany(f"DELETE FROM {table} WHERE {primary}=?", [(key,) for key in keys[table]])
            after = read_tables(connection)
            if ownership(after) != ownership(tables):
                raise ValueError("Cleanup would change owned inventory")
            if connection.execute("PRAGMA foreign_key_check").fetchall():
                raise ValueError("Cleanup would break foreign keys")
            for index, path in enumerate(journals):
                if digest(path.read_bytes()) != plan["journals"][index]["before_sha256"]:
                    raise ValueError("Journal changed during cleanup")
                data, _ = filter_journal(originals[index], set(plan["retire_variants"]), set(plan["protected_trade_ids"]))
                if digest(data) != plan["journals"][index]["after_sha256"]:
                    raise ValueError("Journal output differs from reviewed plan")
                if data != originals[index]:
                    replaced.append(index)
                    atomic_write(path, data)
            connection.commit()
            committed = True
        except BaseException:
            if not committed:
                connection.rollback()
                for index in replaced:
                    atomic_write(journals[index], originals[index])
            raise
        verification = {"removed_packages": len(keys["packages"]),
                        "removed_positions": len(keys["positions"]),
                        "removed_journal_events": sum(row["removed_events"] for row in plan["journals"]),
                        "inventory_unchanged": ownership(after) == ownership(tables),
                        "ledger_after_sha256": digest(encode(after)),
                        "retained_legacy_groups": len(plan["retained_legacy"])}
        (archive / "verification.json").write_bytes(encode(verification) + b"\n")
        return verification


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ledger", type=Path)
    parser.add_argument("--journal", type=Path, action="append", default=[])
    parser.add_argument("--retire-variant", action="append", default=[])
    parser.add_argument("--plan", type=Path, help="Write a dry-run plan, without changing records")
    parser.add_argument("--apply-plan", type=Path)
    parser.add_argument("--archive-dir", type=Path)
    parser.add_argument("--writers-stopped", action="store_true")
    args = parser.parse_args()
    if args.apply_plan:
        if not args.archive_dir:
            parser.error("--archive-dir is required with --apply-plan")
        report = apply_plan(json.loads(args.apply_plan.read_text()), args.archive_dir,
                            writers_stopped=args.writers_stopped)
    else:
        if not args.ledger or not args.journal or not args.retire_variant or not args.plan:
            parser.error("Planning requires --ledger, --journal, --retire-variant and --plan")
        variants = set(args.retire_variant)
        if any(not re.fullmatch(r"V(?:0[1-9]|[12][0-9])", value) for value in variants):
            parser.error("Variants must be V01 through V29")
        plan = make_plan(args.ledger, args.journal, variants)
        args.plan.write_bytes(encode(plan) + b"\n")
        report = {"removable_packages": len(plan["package_ids"]),
                  "removable_positions": len(plan["position_ids"]),
                  "removable_journal_events": sum(row["removed_events"] for row in plan["journals"]),
                  "retained_legacy_groups": len(plan["retained_legacy"]),
                  "retained_legacy": plan["retained_legacy"]}
    print(json.dumps(report, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
