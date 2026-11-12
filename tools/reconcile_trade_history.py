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
import fcntl
import uuid
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from strategies.runtime_records import (
    RecordScope,
    CLASSIFIER_VERSION, EVIDENCE_CONTRACT_VERSION,
    journal_event_conflicts, _contract_identity, _raw_execution_matches, execution_payload_is_valid,
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
    """Return no certified IDs without the journal events needed for classification."""
    # A ledger alone cannot establish that each journal realization matches it.
    return set()


def _read_journal(path: Path) -> tuple[list[tuple[dict[str, Any], bytes]], list[dict[str, Any]]]:
    """Keep raw bytes, ordering, and duplicate occurrences for audit and rebuild."""
    records=[]
    errors=[]
    for number,line in enumerate(path.read_bytes().splitlines(keepends=True),1):
        if not line.strip():
            continue
        try:
            event=json.loads(line)
            if not isinstance(event,dict) or not isinstance(event.get("trade_id"),str) or not event["trade_id"] or not isinstance(event.get("event_id"),str) or not event["event_id"] or event.get("event") not in {"ENTRY","ADDITION","ADJUSTMENT","EXIT"}:
                raise ValueError("missing or invalid event identity")
            records.append((event,line))
        except (ValueError,TypeError,UnicodeError) as error:
            errors.append(dict(line=number,error=str(error),raw_hex=line.hex()))
    return records,errors


def _classifications(records: list[tuple[dict[str, Any], bytes]], ledger_path: Path,
                     scope: RecordScope, as_of_date: str) -> tuple[dict[str, list[dict[str, Any]]],dict[str,dict[str,Any]]]:
    evidence=load_ledger_execution_evidence(ledger_path,expected_scope=scope,as_of_date=as_of_date)
    evidence["conflicting_event_ids"]=journal_event_conflicts([event for event,_ in records])
    grouped:dict[str,list[dict[str,Any]]]={}
    for event,_ in records:
        grouped.setdefault(event["trade_id"],[]).append(event)
    return grouped,{tid:classify_trade(events,evidence,expected_scope=scope,as_of_date=as_of_date)
                    for tid,events in grouped.items()}


def partition_trade_journal(journal_path: Path, ledger_path: Path, *, expected_scope: RecordScope | None = None,
                            as_of_date: str | None = None) -> tuple[dict[str,Any],dict[str,Any],dict[str,Any]]:
    """Partition complete sequences while retaining unsupported history for audit."""
    if not journal_path.exists():
        return {},{},{}
    scope=expected_scope or RecordScope.from_env()
    as_of=as_of_date or datetime.datetime.now(datetime.timezone.utc).date().isoformat()
    records,_=_read_journal(journal_path)
    grouped,classifications=_classifications(records,ledger_path,scope,as_of)
    return tuple({tid:events for tid,events in grouped.items() if classifications[tid]["partition"]==name}
                 for name in ("accepted","unattributed","quarantined"))


def summarize_partition(trades: dict[str,list[dict[str,Any]]]) -> dict[str,Any]:
    """Sum journal-reported terminal amounts, without certifying lifecycle arithmetic."""
    exits=[next((event for event in reversed(events) if event.get("event")=="EXIT"),None) for events in trades.values()]
    exits=[event for event in exits if event is not None]
    summary:dict[str,Any]=dict(trades_count=len(trades),closed_count=len(exits),amount_basis="journal-reported terminal amounts")
    for field in ("gross_pnl","fees","net_pnl"):
        values=[event.get(field) for event in exits]
        summary[field]=None if any(value is None for value in values) else round(sum(float(value) for value in values),2)
    return summary


def command_report(journal_path: Path, ledger_path: Path, *, expected_scope: RecordScope | None = None,
                   as_of_date: str | None = None) -> dict[str,Any]:
    scope=expected_scope or RecordScope.from_env()
    as_of=as_of_date or datetime.datetime.now(datetime.timezone.utc).date().isoformat()
    records,errors=_read_journal(journal_path)
    grouped,classified=_classifications(records,ledger_path,scope,as_of)
    report=dict(journal_path=str(journal_path),ledger_path=str(ledger_path),scope=scope.to_dict(),as_of_date=as_of,
                total_trades=len(grouped),parse_error_count=len(errors),parse_errors=errors,
                partitions={name:summarize_partition({tid:events for tid,events in grouped.items() if classified[tid]["partition"]==name})
                            for name in ("accepted","unattributed","quarantined")},classifications=classified)
    print(json.dumps(report,indent=2,sort_keys=True))
    return report


def _source_identity(path: Path) -> dict[str,int]:
    stat=path.stat()
    return dict(device=stat.st_dev,inode=stat.st_ino,size=stat.st_size,mtime_ns=stat.st_mtime_ns)


def _scope_from_manifest(manifest: dict[str,Any]) -> RecordScope:
    value=manifest["scope"]
    if not all(value.get(key) for key in ("broker","account_id","environment")):
        raise ValueError("explicit scope required")
    return RecordScope(**{key:value[key] for key in ("broker","account_id","environment")})


def command_build(journal_path: Path, ledger_path: Path, backup_dir: Path, rebuild_dir: Path,
                  *, expected_scope: RecordScope | None = None, as_of_date: str | None = None) -> Path:
    """Freeze source bytes and the full evidence database before classification."""
    scope=expected_scope or RecordScope.from_env()
    as_of=as_of_date or datetime.datetime.now(datetime.timezone.utc).date().isoformat()
    datetime.date.fromisoformat(as_of)
    if not journal_path.is_file() or not ledger_path.is_file():
        raise FileNotFoundError("Journal and evidence ledger must exist")
    if rebuild_dir.exists() and any(rebuild_dir.iterdir()):
        raise ValueError("Use an empty rebuild directory to preserve previous recovery artifacts")
    rebuild_dir.mkdir(parents=True,exist_ok=True)
    backup_dir.mkdir(parents=True,exist_ok=True)
    backup=backup_dir/f"trades_original_{uuid.uuid4().hex}.jsonl"
    with journal_path.open('rb') as source:
        fcntl.flock(source,fcntl.LOCK_SH)
        identity=_source_identity(journal_path)
        raw=source.read()
        backup.write_bytes(raw)
        if identity!=_source_identity(journal_path):
            raise ValueError("Source journal changed during backup")
    snapshot=rebuild_dir/'evidence.sqlite3'
    with sqlite3.connect(ledger_path.resolve().as_uri()+'?mode=ro',uri=True) as source:
        with sqlite3.connect(snapshot) as destination:
            source.backup(destination)
    records,errors=_read_journal(backup)
    grouped,classified=_classifications(records,snapshot,scope,as_of)
    report_path=rebuild_dir/'classifications.json'
    report_path.write_text(json.dumps(classified,sort_keys=True,indent=2))
    error_path=rebuild_dir/'parse_errors.json'
    error_path.write_text(json.dumps(errors,sort_keys=True,indent=2))
    files={}
    for name in ('accepted','unattributed','quarantined'):
        path=rebuild_dir/f'trades.{name}.jsonl'
        path.write_bytes(b''.join(line for event,line in records if classified[event['trade_id']]['partition']==name))
        partition={tid:events for tid,events in grouped.items() if classified[tid]['partition']==name}
        files[name]=dict(path=str(path.resolve()),sha256=sha256_file(path),summary=summarize_partition(partition))
    manifest=dict(version=2,classifier_version=CLASSIFIER_VERSION,evidence_contract_version=EVIDENCE_CONTRACT_VERSION,
                  scope=scope.to_dict(),as_of_date=as_of,original_journal=str(journal_path.resolve()),source_identity=identity,
                  original_sha256=hashlib.sha256(raw).hexdigest(),ledger_path=str(ledger_path.resolve()),
                  backup_path=str(backup.resolve()),backup_sha256=sha256_file(backup),
                  evidence_snapshot=dict(path=str(snapshot.resolve()),sha256=sha256_file(snapshot)),
                  classification_report=dict(path=str(report_path.resolve()),sha256=sha256_file(report_path)),
                  parse_errors=dict(path=str(error_path.resolve()),sha256=sha256_file(error_path),count=len(errors)),
                  files=files,activation_status='built',
                  rollback_command=f'python3 tools/reconcile_trade_history.py rollback --rebuild-dir {rebuild_dir.resolve()} --journal {journal_path.resolve()}')
    path=rebuild_dir/'manifest.json'
    path.write_text(json.dumps(manifest,sort_keys=True,indent=2))
    print(f'Build complete: {path}; parse errors: {len(errors)}')
    return path


def command_verify(rebuild_dir: Path, ledger_path: Path | None = None) -> bool:
    """Recompute partitions and reporting amounts from the frozen v2 contract."""
    try:
        manifest=json.loads((rebuild_dir/'manifest.json').read_text())
        if (manifest.get('version')!=2 or manifest.get('classifier_version')!=CLASSIFIER_VERSION
                or manifest.get('evidence_contract_version')!=EVIDENCE_CONTRACT_VERSION):
            raise ValueError('unsupported manifest or classifier/evidence version')
        scope=_scope_from_manifest(manifest)
        datetime.date.fromisoformat(manifest['as_of_date'])
        if ledger_path is not None and ledger_path.resolve()!=Path(manifest['ledger_path']).resolve():
            raise ValueError('ledger destination mismatch')
        for key in ('evidence_snapshot','classification_report','parse_errors'):
            item=manifest[key]
            if Path(item['path']).resolve().parent!=rebuild_dir.resolve() or sha256_file(Path(item['path']))!=item['sha256']:
                raise ValueError(key+' path/hash mismatch')
        backup=Path(manifest['backup_path'])
        if sha256_file(backup)!=manifest['backup_sha256'] or manifest['backup_sha256']!=manifest['original_sha256']:
            raise ValueError('backup does not match original journal')
        snapshot=Path(manifest['evidence_snapshot']['path'])
        evidence=load_ledger_execution_evidence(snapshot,expected_scope=scope,as_of_date=manifest['as_of_date'])
        if evidence['diagnostics'] or evidence['invalid_evidence']:
            raise ValueError('missing or corrupt retained evidence schema/content')
        if any(evidence['scope'].get(k)!=scope.to_dict()[k] for k in ('broker','account_id','environment')):
            raise ValueError('snapshot scope mismatch')
        records,errors=_read_journal(backup)
        if errors or manifest['parse_errors']['count'] or json.loads(Path(manifest['parse_errors']['path']).read_text())!=errors:
            raise ValueError('malformed lines or missing event identities prevent activation')
        grouped,classified=_classifications(records,snapshot,scope,manifest['as_of_date'])
        if json.loads(Path(manifest['classification_report']['path']).read_text())!=classified:
            raise ValueError('classification report mismatch')
        for name in ('accepted','unattributed','quarantined'):
            item=manifest['files'][name]
            path=Path(item['path'])
            wanted=b''.join(line for event,line in records if classified[event['trade_id']]['partition']==name)
            part={tid:events for tid,events in grouped.items() if classified[tid]['partition']==name}
            if (path.resolve()!=rebuild_dir.resolve()/f'trades.{name}.jsonl' or sha256_file(path)!=item['sha256']
                    or path.read_bytes()!=wanted or item['summary']!=summarize_partition(part)):
                raise ValueError(name+' assignment, ordering, multiplicity or summary mismatch')
        if backup.stat().st_size and not Path(manifest['files']['accepted']['path']).stat().st_size:
            raise ValueError('nonempty history cannot be replaced by an empty accepted partition')
        print('Precondition & Integrity Verification: PASSED')
        return True
    except (OSError,ValueError,KeyError,TypeError,AttributeError) as error:
        print(f'Verification failed: {error}',file=sys.stderr)
        return False


def _atomic_replace_bytes(target: Path, raw: bytes) -> None:
    temporary=target.with_name(f'.{target.name}.{uuid.uuid4().hex}.tmp')
    try:
        with temporary.open('xb') as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary,target)
        descriptor=os.open(target.parent,os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    finally:
        if temporary.exists():
            temporary.unlink()


def command_activate(journal_path: Path, rebuild_dir: Path, ledger_path: Path | None = None,
                     *, writers_stopped: bool = False) -> bool:
    """Require stopped writers, then recheck source identity under its append lock."""
    if not writers_stopped:
        print('Activation requires stopped journal and ledger writers.',file=sys.stderr)
        return False
    if not command_verify(rebuild_dir,ledger_path):
        return False
    try:
        manifest_path=rebuild_dir/'manifest.json'
        manifest=json.loads(manifest_path.read_text())
        if manifest['activation_status'] not in {'built','activating'}:
            raise ValueError('build is already activated or rolled back')
        if journal_path.resolve()!=Path(manifest['original_journal']).resolve():
            raise ValueError('journal destination mismatch')
        with journal_path.open('rb') as source:
            fcntl.flock(source,fcntl.LOCK_EX)
            if manifest['activation_status']=='activating' and sha256_file(journal_path)==manifest['files']['accepted']['sha256']:
                manifest['activation_status']='activated'
                _atomic_replace_bytes(manifest_path,json.dumps(manifest,sort_keys=True,indent=2).encode())
                return True
            if (_source_identity(journal_path)!=manifest['source_identity']
                    or os.fstat(source.fileno()).st_ino!=journal_path.stat().st_ino
                    or hashlib.sha256(source.read()).hexdigest()!=manifest['original_sha256']):
                raise ValueError('journal changed since build')
            # Recheck the artifacts while the source lock is held.
            if not command_verify(rebuild_dir,ledger_path):
                return False
            accepted=Path(manifest['files']['accepted']['path']).read_bytes()
            if hashlib.sha256(accepted).hexdigest()!=manifest['files']['accepted']['sha256']:
                raise ValueError('accepted records changed during activation')
            manifest['activation_status']='activating'
            _atomic_replace_bytes(manifest_path,json.dumps(manifest,sort_keys=True,indent=2).encode())
            try:
                _atomic_replace_bytes(journal_path,accepted)
            except OSError:
                if sha256_file(journal_path)==manifest['original_sha256']:
                    manifest['activation_status']='built'
                    _atomic_replace_bytes(manifest_path,json.dumps(manifest,sort_keys=True,indent=2).encode())
                raise
        manifest['activation_status']='activated'
        manifest['activated_at']=datetime.datetime.now(datetime.timezone.utc).isoformat()
        _atomic_replace_bytes(manifest_path,json.dumps(manifest,sort_keys=True,indent=2).encode())
        print(f'Activated {journal_path}. Raw backup retained at {manifest["backup_path"]}')
        return True
    except (OSError,ValueError,KeyError,TypeError) as error:
        print(f'Activation aborted: {error}',file=sys.stderr)
        return False


def command_rollback(rebuild_dir: Path, journal_path: Path) -> bool:
    """Restore raw bytes only while the active history has no subsequent appends."""
    try:
        manifest_path=rebuild_dir/'manifest.json'
        manifest=json.loads(manifest_path.read_text())
        if manifest.get('version')!=2 or manifest['activation_status'] not in {'activated','activating','rolling_back'}:
            raise ValueError('no v2 activation to roll back')
        if journal_path.resolve()!=Path(manifest['original_journal']).resolve():
            raise ValueError('journal destination mismatch')
        backup=Path(manifest['backup_path']).read_bytes()
        if hashlib.sha256(backup).hexdigest()!=manifest['backup_sha256']:
            raise ValueError('backup hash mismatch')
        with journal_path.open('rb') as source:
            fcntl.flock(source,fcntl.LOCK_EX)
            if manifest['activation_status']=='rolling_back' and sha256_file(journal_path)==manifest['backup_sha256']:
                manifest['activation_status']='rolled_back'
                _atomic_replace_bytes(manifest_path,json.dumps(manifest,sort_keys=True,indent=2).encode())
                return True
            if os.fstat(source.fileno()).st_ino!=journal_path.stat().st_ino or hashlib.sha256(source.read()).hexdigest()!=manifest['files']['accepted']['sha256']:
                raise ValueError('journal changed since activation; preserve both histories and reconcile forward')
            manifest['activation_status']='rolling_back'
            _atomic_replace_bytes(manifest_path,json.dumps(manifest,sort_keys=True,indent=2).encode())
            _atomic_replace_bytes(journal_path,backup)
        manifest['activation_status']='rolled_back'
        _atomic_replace_bytes(manifest_path,json.dumps(manifest,sort_keys=True,indent=2).encode())
        print(f'Rollback restored {journal_path}')
        return True
    except (OSError,ValueError,KeyError,TypeError) as error:
        print(f'Rollback aborted: {error}',file=sys.stderr)
        return False


def command_import_evidence(export_path: Path, ledger_path: Path, *, expected_scope: RecordScope) -> int:
    """Validate a provider export against existing ownership before committing links."""
    from strategies.position_ledger import PositionLedger
    bundle=json.loads(export_path.read_text())
    if bundle.get('evidence_contract_version')!=EVIDENCE_CONTRACT_VERSION or bundle.get('scope')!=expected_scope.to_dict():
        raise ValueError('explicit export scope and evidence contract v2 required')
    target=PositionLedger(ledger_path,scope=expected_scope)
    staged=PositionLedger(':memory:',scope=expected_scope)
    tables=('broker_evidence','realization_evidence_links','settlement_evidence_links','fee_evidence','settlement_values','broker_cash_evidence')
    try:
        target._connection.backup(staged._connection)
        for row in bundle.get('evidence',[]):
            if row['kind'] == 'execution' and not execution_payload_is_valid(row['payload']):
                raise ValueError('invalid execution payload shape')
            staged.record_broker_evidence(row['evidence_id'],scope=expected_scope,kind=row['kind'],provider_identity=row['provider_identity'],
                                          acquisition_source=row['acquisition_source'],acquired_at=row['acquired_at'],payload=row['payload'])
        for link in bundle.get('realization_links',[]):
            staged.link_realization_evidence(**link)
        for link in bundle.get('settlement_links',[]):
            staged.link_settlement_evidence(**link)
        for table, key, kind in (("fee_evidence","fee_records","fee"),
                                 ("settlement_values","settlement_records","settlement"),
                                 ("broker_cash_evidence","cash_records","cash")):
            columns=[r[1] for r in staged._connection.execute(f'PRAGMA table_info({table})')]
            for record in bundle.get(key,[]):
                if set(record)!=set(columns):
                    raise ValueError("export record columns do not match evidence contract")
                raw=staged.get_broker_evidence(record.get("raw_evidence_id"))
                if not raw or raw["kind"]!=kind:
                    raise ValueError("export record requires retained raw evidence")
                payload=raw["payload"]
                if kind=="fee" and (payload.get("event_id")!=record["event_id"] or payload.get("trade_id")!=record["trade_id"]
                                   or payload.get("amount_cents")!=record["amount_cents"] or payload.get("complete")!=bool(record["complete"])):
                    raise ValueError("fee export does not match retained payload")
                if kind in {"settlement","cash"} and (payload.get("expiration")!=record["expiration"] or payload.get("symbol")!=record["symbol"]):
                    raise ValueError("settlement/cash export contract mismatch")
                values=tuple(record[k] for k in columns)
                same=staged._connection.execute(f'SELECT 1 FROM {table} WHERE '+ ' AND '.join(f'{k} IS ?' for k in columns),values).fetchone()
                if not same:
                    with staged._connection:
                        staged._connection.execute(f'INSERT INTO {table} ({",".join(columns)}) VALUES ({",".join("?" for _ in columns)})',values)
        for row in bundle.get("evidence",[]):
            if row["payload"].get("scope")!=expected_scope.to_dict() or not row["payload"].get("raw_response"):
                raise ValueError("retained export payload must include exact scope and raw response")
            if row["kind"]=="execution" and not any(link["evidence_id"]==row["evidence_id"] for link in bundle.get("realization_links",[])):
                raise ValueError("execution export requires validated ownership links")
        # Validate each imported execution link using the same production verifier.
        ev=load_ledger_execution_evidence_from_connection(staged)
        for link in bundle.get('realization_links',[]):
            row=ev['retained'][link['evidence_id']]
            payload=row['payload']
            leg=next((item for item in payload.get('legs',[]) if item.get('leg_id')==link['leg_id']),{})
            package=ev['packages'][link['package_id']]
            local=next(item for item in package['legs'] if item['order_id']==link['leg_id'])
            if (row['kind']!='execution' or payload.get('trade_id')!=link['trade_id'] or payload.get('position_id')!=link['position_id']
                    or payload.get('package_id')!=link['package_id'] or payload.get('parent_order_id')!=package['broker_parent_id']
                    or leg.get('order_id')!=local['broker_order_id'] or leg.get('side')!=local['side']
                    or leg.get('signed_quantity')!=link['allocated_signed_quantity'] or leg.get('signed_quantity')!=local['filled_signed_quantity']
                    or _contract_identity(leg.get('contract'))!=_contract_identity({**json.loads(local['asset_key']),'multiplier':100})
                    or not _raw_execution_matches(payload,leg)):
                raise ValueError('export execution ownership/contract mismatch')
        with target._connection:
            inserted=0
            for table in tables:
                columns=[r[1] for r in staged._connection.execute(f'PRAGMA table_info({table})')]
                for row in staged._connection.execute(f'SELECT * FROM {table}'):
                    values=tuple(row)
                    existing=target._connection.execute(f'SELECT 1 FROM {table} WHERE '+ ' AND '.join(f'{key} IS ?' for key in columns),values).fetchone()
                    if existing:
                        continue
                    target._connection.execute(f'INSERT INTO {table} ({",".join(columns)}) VALUES ({",".join("?" for _ in columns)})',values)
                    inserted+=1
        print(f'Imported {inserted} immutable evidence/link records')
        return inserted
    finally:
        staged.close()
        target.close()


def load_ledger_execution_evidence_from_connection(ledger: Any) -> dict[str,Any]:
    """Read a staged import through the same read-only loader used by production."""
    import tempfile
    with tempfile.TemporaryDirectory(prefix='lumibot-evidence-import-') as directory:
        snapshot=Path(directory)/'evidence.sqlite3'
        with sqlite3.connect(snapshot) as destination:
            ledger._connection.backup(destination)
        return load_ledger_execution_evidence(snapshot)


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
    parser=argparse.ArgumentParser(description="Offline retained-evidence reconciliation and recovery")
    subs=parser.add_subparsers(dest="command",required=True)
    for name in ("report","build","verify","activate","rollback","import-evidence","reconcile-settlement"):
        command=subs.add_parser(name)
        if name in {"report","build","activate","rollback","reconcile-settlement"}:
            command.add_argument("--journal",type=Path,default=DEFAULT_JOURNAL)
        if name in {"report","build","import-evidence","reconcile-settlement"}:
            command.add_argument("--ledger",type=Path,default=DEFAULT_LEDGER)
        if name in {"build","verify","activate","rollback"}:
            command.add_argument("--rebuild-dir",type=Path,default=DEFAULT_REBUILD_DIR)
        if name in {"report","build","import-evidence"}:
            for field in ("broker","account-id","environment"):
                command.add_argument("--"+field,required=True)
        if name in {"report","build","reconcile-settlement"}:
            command.add_argument("--as-of-date",required=name!="reconcile-settlement")
        if name=="build":
            command.add_argument("--backup-dir",type=Path,default=DEFAULT_BACKUP_DIR)
        if name=="activate":
            command.add_argument("--writers-stopped",action="store_true",required=True)
        if name=="import-evidence":
            command.add_argument("--export",type=Path,required=True)
    args=parser.parse_args()
    scope=RecordScope(args.environment,args.broker,args.account_id) if hasattr(args,"environment") else None
    try:
        if args.command=="report":
            command_report(args.journal,args.ledger,expected_scope=scope,as_of_date=args.as_of_date)
        elif args.command=="build":
            command_build(args.journal,args.ledger,args.backup_dir,args.rebuild_dir,expected_scope=scope,as_of_date=args.as_of_date)
        elif args.command=="verify":
            sys.exit(0 if command_verify(args.rebuild_dir) else 1)
        elif args.command=="activate":
            sys.exit(0 if command_activate(args.journal,args.rebuild_dir,writers_stopped=args.writers_stopped) else 1)
        elif args.command=="rollback":
            sys.exit(0 if command_rollback(args.rebuild_dir,args.journal) else 1)
        elif args.command=="import-evidence":
            command_import_evidence(args.export,args.ledger,expected_scope=scope)
        else:
            command_reconcile_settlement(args.ledger,args.journal,args.as_of_date)
    except (OSError,ValueError,KeyError,TypeError,sqlite3.Error) as error:
        parser.exit(1,f"Error: {error}\n")


if __name__=="__main__":
    main()
