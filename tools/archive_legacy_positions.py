#!/usr/bin/env python3
"""Archive specific expired paper positions after fresh broker confirmation.

Fills, cash flows, retained evidence and journal events remain historical facts.
Only lifecycle state and an audit marker change. No broker mutations occur.
"""
from __future__ import annotations

import argparse
from contextlib import closing
from datetime import datetime, timezone
import json
import math
from pathlib import Path
import sqlite3
import sys
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from strategies.scoped_recovery import ACTIVE, contract_symbol
from tools.purge_legacy_records import digest, encode, read_tables


def plan_archive(ledger: Path, position_ids: list[str], evidence: dict, *, now: datetime) -> dict:
    if now.utcoffset() is None:
        raise ValueError('Archive time requires a timezone')
    acquired = datetime.fromisoformat(evidence['acquired_at'].replace('Z', '+00:00'))
    if acquired.utcoffset() is None or not -5 <= (now-acquired).total_seconds() <= 120:
        raise ValueError('Fresh broker evidence within 120 seconds is required')
    if evidence.get('positions_complete') is not True or evidence.get('orders_complete') is not True:
        raise ValueError('Complete successful broker positions and orders are required')
    positions, orders = evidence['positions'], evidence['orders']
    if not isinstance(positions, list) or not isinstance(orders, list):
        raise ValueError('Broker positions and orders must be lists')
    with closing(sqlite3.connect(ledger.resolve().as_uri()+'?mode=ro', uri=True)) as db:
        db.row_factory = sqlite3.Row
        db.execute('BEGIN')
        tables = read_tables(db)
    scope = tables['record_scope'][0]
    if scope['environment'] != 'paper' or any(
            evidence['scope'].get(k) != scope[k] for k in ('broker', 'account_id', 'environment')):
        raise ValueError('Broker evidence must match this paper ledger scope')
    if any(r['status'] not in {'completed', 'failed', 'canceled'} for r in tables['recovery_jobs']):
        raise ValueError('Resolve recovery jobs before archiving')
    if any(r['status'] == 'pending' for r in tables['outbox']):
        raise ValueError('Dispatch the pending journal outbox before archiving')
    if any(r['key'] == 'panic_halt' and r['value'] in {'1', 'true', 'True'} for r in tables['system_state']):
        raise ValueError('Panic recovery must finish before archiving')
    if any(r['status'] not in {'filled', 'canceled', 'rejected', 'failed'} for r in tables['emergency_submissions']):
        raise ValueError('Resolve emergency submissions before archiving')
    actual = {}
    for row in positions:
        quantity = float(row['quantity'])
        if not row.get('symbol') or not math.isfinite(quantity):
            raise ValueError('Invalid broker inventory')
        actual[row['symbol']] = actual.get(row['symbol'], 0) + quantity
    selected = []
    as_of = now.astimezone(ZoneInfo('America/New_York')).date()
    for pid in sorted(set(position_ids)):
        position = next((p for p in tables['positions'] if p['position_id'] == pid), None)
        packages = [p for p in tables['packages'] if p['position_id'] == pid]
        if position is None or not packages:
            raise ValueError(f'Unknown position: {pid}')
        if all(p['state'] == 'archived' for p in packages):
            continue
        if any(p['state'] == 'archived' for p in packages):
            raise ValueError('Partially archived position requires review')
        ids = {p['package_id'] for p in packages}
        legs = [l for l in tables['legs'] if l['package_id'] in ids]
        if not legs or any(json.loads(l['asset_key'])['expiration'][:10] >= as_of.isoformat() for l in legs):
            raise ValueError('Only wholly expired positions can be archived')
        symbols = {contract_symbol(l['asset_key']) for l in legs}
        if any(actual.get(symbol, 0) != 0 for symbol in symbols):
            raise ValueError('Broker still holds a requested legacy contract')
        for order in orders:
            if str(order.get('status', '')).lower() not in ACTIVE:
                continue
            children = order.get('leg') or [order]
            if isinstance(children, dict):
                children = [children]
            held = {c.get('option_symbol') for c in children}
            if held & symbols or str(order.get('id')) in {str(p['broker_parent_id']) for p in packages if p['broker_parent_id']}:
                raise ValueError('Broker still has an active legacy order')
        selected.append({'position_id': pid, 'strategy': position['strategy_id'],
                         'package_ids': sorted(ids), 'contracts': sorted(symbols),
                         'previous_states': {p['package_id']: p['state'] for p in packages}})
    archived_contracts = {symbol for p in selected for symbol in p['contracts']}
    packages_by_id = {p['package_id']: p for p in tables['packages']}
    retained = {}
    for leg in tables['legs']:
        package = packages_by_id[leg['package_id']]
        if package['position_id'] in position_ids or package['state'] in {'expired', 'archived'}:
            continue
        key = (package['position_id'], leg['asset_key'])
        retained[key] = retained.get(key, 0) + leg['filled_signed_quantity']
    if any(q and contract_symbol(key) in archived_contracts for (_, key), q in retained.items()):
        raise ValueError('Another retained position owns a requested legacy contract')
    return {'ledger': str(ledger.resolve()), 'scope': evidence['scope'], 'positions': selected,
            'ledger_before_sha256': digest(encode(tables)), 'evidence_sha256': digest(encode(evidence)),
            'archived_at': now.isoformat(), 'reason': 'user_retired_legacy_position_unverified'}


def archive_positions(ledger: Path, position_ids: list[str], evidence: dict, archive: Path,
                      *, now: datetime, journals: list[Path] = (), writers_stopped: bool = False) -> dict:
    if not writers_stopped:
        raise ValueError('Stop all record writers before applying; --writers-stopped is required')
    plan = plan_archive(ledger, position_ids, evidence, now=now)
    if not plan['positions']:
        return {'archived_positions': 0, 'already_archived': True}
    with closing(sqlite3.connect(ledger)) as db:
        db.row_factory = sqlite3.Row
        db.execute('PRAGMA foreign_keys=ON')
        before = read_tables(db)
        if digest(encode(before)) != plan['ledger_before_sha256']:
            raise ValueError('Ledger changed since planning')
        archive.mkdir(parents=True, exist_ok=False, mode=0o700)
        with closing(sqlite3.connect(archive/'ledger.sqlite3')) as backup:
            db.backup(backup)
        for i, path in enumerate(journals):
            (archive/f'journal-{i}.jsonl').write_bytes(path.read_bytes())
        (archive/'broker-evidence.json').write_bytes(encode(evidence)+b'\n')
        (archive/'plan.json').write_bytes(encode(plan)+b'\n')
        with db:
            db.execute('BEGIN IMMEDIATE')
            if digest(encode(read_tables(db))) != plan['ledger_before_sha256']:
                raise ValueError('Ledger changed while acquiring archive lock')
            if db.execute('PRAGMA foreign_key_check').fetchall():
                raise ValueError('Ledger has foreign key violations')
            for position in plan['positions']:
                for package in position['package_ids']:
                    db.execute("UPDATE packages SET state='archived',updated_at=? WHERE package_id=?",
                               (plan['archived_at'], package))
                metadata = {**position, 'archived_at': plan['archived_at'], 'reason': plan['reason'],
                            'archive_path': str(archive.resolve()), 'evidence_sha256': plan['evidence_sha256']}
                db.execute('INSERT INTO system_state(key,value,updated_at) VALUES(?,?,?)',
                           ('archived_position:'+position['position_id'], json.dumps(metadata, sort_keys=True), plan['archived_at']))
            after = read_tables(db)
            changed = {p for position in plan['positions'] for p in position['package_ids']}
            for table in before:
                if table not in {'packages', 'system_state'} and after[table] != before[table]:
                    raise ValueError(f'Archive changed historical facts in {table}')
            for row, old in zip(after['packages'], before['packages']):
                expected = {**old, 'state': 'archived', 'updated_at': plan['archived_at']} if old['package_id'] in changed else old
                if row != expected:
                    raise ValueError('Archive changed an unrelated package or cash flow')
            if after['system_state'][:len(before['system_state'])] != before['system_state']:
                raise ValueError('Archive changed existing system state')
        report = {'archived_positions': len(plan['positions']), 'archived_packages': len(changed),
                  'archive': str(archive.resolve()), 'historical_facts_unchanged': True,
                  'journal_events_unchanged': True, 'settlement_or_pnl_fabricated': False}
        (archive/'verification.json').write_bytes(encode(report)+b'\n')
        return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--ledger', type=Path, required=True)
    parser.add_argument('--position', action='append', required=True)
    parser.add_argument('--broker-evidence', type=Path, required=True)
    parser.add_argument('--apply', action='store_true')
    parser.add_argument('--writers-stopped', action='store_true')
    parser.add_argument('--archive-dir', type=Path)
    parser.add_argument('--journal', type=Path, action='append', default=[])
    args = parser.parse_args()
    evidence = json.loads(args.broker_evidence.read_text())
    now = datetime.now(timezone.utc)
    if args.apply:
        if args.archive_dir is None:
            parser.error('--archive-dir is required with --apply')
        report = archive_positions(args.ledger, args.position, evidence, args.archive_dir, now=now,
                                   journals=args.journal, writers_stopped=args.writers_stopped)
    else:
        report = plan_archive(args.ledger, args.position, evidence, now=now)
    print(json.dumps(report, indent=2))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
