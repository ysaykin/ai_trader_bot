"""Manifest v2 activation, offline imports, deterministic rebuild, and recovery."""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from strategies.position_ledger import PositionLedger
from tools.reconcile_trade_history import (command_build, command_verify, command_activate, command_rollback,
                                           command_import_evidence, command_report, sha256_file)
from trade_history_fixtures import evidence_bundle, SCOPE, AS_OF


def build(tmp_path: Path, *, malformed: bool=False):
    bundle=evidence_bundle(tmp_path/'source')
    legacy={**bundle['events'][0],'trade_id':'legacy','event_id':'legacy:entry','package_id':'missing'}
    synthetic={**bundle['events'][0],'trade_id':'synthetic','event_id':'synthetic:entry','source':'synthetic'}
    with bundle['journal'].open('a') as stream:
        stream.write(json.dumps(legacy)+'\n'+json.dumps(synthetic)+'\n')
        if malformed:
            stream.write('{broken\n'+json.dumps({'trade_id':'missing-event-id','event':'ENTRY'})+'\n')
    rebuild=tmp_path/'rebuild'
    path=command_build(bundle['journal'],bundle['ledger'],tmp_path/'backup',rebuild,expected_scope=SCOPE,as_of_date=AS_OF)
    return bundle,rebuild,path


def test_verify_rejects_moved_trade_even_if_hashes_are_updated(tmp_path: Path) -> None:
    bundle,rebuild,path=build(tmp_path)
    assert command_verify(rebuild)
    manifest=json.loads(path.read_text())
    accepted=Path(manifest['files']['accepted']['path'])
    unattributed=Path(manifest['files']['unattributed']['path'])
    lines=accepted.read_bytes().splitlines(keepends=True)
    unattributed.write_bytes(unattributed.read_bytes()+lines[0])
    accepted.write_bytes(b''.join(lines[1:]))
    for name,target in (('accepted',accepted),('unattributed',unattributed)):
        manifest['files'][name]['sha256']=sha256_file(target)
    path.write_text(json.dumps(manifest))
    assert not command_verify(rebuild)


@pytest.mark.parametrize('version',[None,1,'2'])
def test_verify_rejects_old_or_invalid_manifest(tmp_path: Path,version) -> None:
    (tmp_path/'manifest.json').write_text(json.dumps({'version':version}))
    assert not command_verify(tmp_path)


def test_activation_requires_stopped_writers_and_fresh_destination(tmp_path: Path) -> None:
    bundle,rebuild,path=build(tmp_path)
    original=bundle['journal'].read_bytes()
    assert not command_activate(bundle['journal'],rebuild)
    other=tmp_path/'other';other.write_bytes(original)
    assert not command_activate(other,rebuild,writers_stopped=True)
    with bundle['journal'].open('ab') as stream:
        stream.write(b'\n')
    assert not command_activate(bundle['journal'],rebuild,writers_stopped=True)
    assert bundle['journal'].read_bytes()==original+b'\n'


def test_malformed_lines_are_backed_up_and_block_activation(tmp_path: Path) -> None:
    bundle,rebuild,path=build(tmp_path,malformed=True)
    manifest=json.loads(path.read_text())
    assert Path(manifest['backup_path']).read_bytes()==bundle['journal'].read_bytes()
    assert manifest['parse_errors']['count']==2
    assert command_report(bundle['journal'],bundle['ledger'],expected_scope=SCOPE,as_of_date=AS_OF)['parse_error_count']==2
    assert not command_verify(rebuild)


@pytest.mark.parametrize('artifact',['evidence_snapshot','classification_report','summary','scope','as_of_date'])
def test_tampering_rejected(tmp_path: Path,artifact: str) -> None:
    bundle,rebuild,path=build(tmp_path)
    manifest=json.loads(path.read_text())
    if artifact=='summary':
        manifest['files']['accepted']['summary']['fees']=999
    elif artifact=='scope':
        manifest['scope']['account_id']='OTHER'
    elif artifact=='as_of_date':
        manifest['as_of_date']='invalid'
    else:
        Path(manifest[artifact]['path']).write_bytes(b'corrupted')
    path.write_text(json.dumps(manifest))
    assert not command_verify(rebuild)


def test_rollback_refuses_post_activation_appends(tmp_path: Path) -> None:
    bundle,rebuild,path=build(tmp_path)
    assert command_activate(bundle['journal'],rebuild,writers_stopped=True)
    with bundle['journal'].open('ab') as stream:
        stream.write(b'new event\n')
    current=bundle['journal'].read_bytes()
    assert not command_rollback(rebuild,bundle['journal'])
    assert bundle['journal'].read_bytes()==current


def test_recheck_under_lock_and_atomic_failure_preserve_source(tmp_path: Path) -> None:
    bundle,rebuild,path=build(tmp_path)
    original=bundle['journal'].read_bytes()
    from tools import reconcile_trade_history as cli
    replace=cli.os.replace
    def fail_replace(source,destination):
        if Path(destination)==bundle['journal']:
            raise OSError('simulated replace failure')
        return replace(source,destination)
    with patch.object(cli.os,'replace',side_effect=fail_replace):
        assert not command_activate(bundle['journal'],rebuild,writers_stopped=True)
    assert bundle['journal'].read_bytes()==original
    assert json.loads(path.read_text())['activation_status']=='built'


def export_and_ownership_only(source: Path, target: Path, export_path: Path) -> None:
    ledger=PositionLedger(target,scope=SCOPE)
    with sqlite3.connect(source) as db:
        db.row_factory=sqlite3.Row
        for table in ('positions','packages','legs'):
            rows=db.execute(f'SELECT * FROM {table}').fetchall()
            for row in rows:
                columns=list(row.keys())
                ledger._connection.execute(f'INSERT INTO {table} ({",".join(columns)}) VALUES ({",".join("?" for _ in columns)})',tuple(row))
        ledger._connection.commit()
        raw=[dict(row) for row in db.execute('SELECT * FROM broker_evidence')]
        for row in raw:
            row['payload']=json.loads(row['payload'])
        bundle=dict(evidence_contract_version='2',scope=SCOPE.to_dict(),evidence=raw,
                    realization_links=[dict(row) for row in db.execute('SELECT * FROM realization_evidence_links')],
                    fee_records=[dict(row) for row in db.execute('SELECT * FROM fee_evidence')])
    ledger.close()
    export_path.write_text(json.dumps(bundle))


def contradict_execution(payload: dict[str, Any], damage: str) -> None:
    """Change provider content without changing its normalized assertions."""
    import copy

    raw = payload['raw_response']
    if damage.startswith('parent'):
        payload['parent_response'] = copy.deepcopy(raw)
        response = payload['parent_response']
    else:
        response = raw
    if damage.endswith('_leg_scope'):
        response = response['leg'][0]
    if damage.endswith('_scope'):
        response['scope'] = {**SCOPE.to_dict(), 'account_id': 'OTHER_ACCOUNT',
                             'scope_key': 'tradier_paper_OTHER_ACCOUNT'}
    elif damage == 'raw_exec_quantity':
        raw['leg'][0]['exec_quantity'] = 0
    elif damage in {'raw_duplicate', 'parent_duplicate'}:
        duplicate = copy.deepcopy(response['leg'][0])
        duplicate['exec_quantity'] = 0
        response['leg'].append(duplicate)
    elif damage == 'parent_exec_quantity':
        response['leg'][0]['exec_quantity'] = 0
    elif damage == 'parent_signed_quantity':
        response['leg'][0]['signed_quantity'] = 2
    elif damage == 'parent_side':
        response['leg'][0]['side'] = 'sell_to_close'
    elif damage == 'parent_contract':
        response['leg'][0]['contract']['strike'] = 999
    elif damage == 'parent_identity':
        response['id'] = 'WRONG-PARENT'
    else:
        next(child for child in raw['leg'] if child['signed_quantity'] > 0)['signed_quantity'] = True


@pytest.mark.parametrize('damage', ['raw_scope', 'raw_leg_scope', 'parent_scope',
                                    'parent_leg_scope', 'raw_exec_quantity', 'raw_signed_quantity',
                                    'raw_duplicate', 'parent_duplicate', 'parent_exec_quantity',
                                    'parent_signed_quantity', 'parent_side', 'parent_contract', 'parent_identity'])
def test_execution_contradictions_rejected_by_import_and_frozen_verifier(tmp_path: Path, damage: str) -> None:
    source = evidence_bundle(tmp_path / 'source')
    target, export = tmp_path / 'imported.sqlite3', tmp_path / 'export.json'
    export_and_ownership_only(source['ledger'], target, export)
    bundle = json.loads(export.read_text())
    for row in bundle['evidence']:
        if row['kind'] == 'execution':
            contradict_execution(row['payload'], damage)
    export.write_text(json.dumps(bundle))
    original = target.read_bytes()
    with pytest.raises(ValueError, match='execution ownership/contract mismatch'):
        command_import_evidence(export, target, expected_scope=SCOPE)
    assert target.read_bytes() == original

    # Older retained evidence must fail too, even with freshly computed hashes.
    ledger = PositionLedger(target, scope=SCOPE)
    try:
        for row in bundle['evidence']:
            ledger.record_broker_evidence(row['evidence_id'], scope=SCOPE, kind=row['kind'],
                                          provider_identity=row['provider_identity'],
                                          acquisition_source=row['acquisition_source'],
                                          acquired_at=row['acquired_at'], payload=row['payload'])
        for link in bundle['realization_links']:
            ledger.link_realization_evidence(**link)
    finally:
        ledger.close()
    original = target.read_bytes()
    report = command_report(source['journal'], target, expected_scope=SCOPE, as_of_date=AS_OF)
    result = report['classifications']['retained-1']
    assert result['partition'] == 'unattributed'
    assert result['is_eligible'] is False
    assert 'RAW_EXECUTION_MISMATCH' in result['reason_codes']
    rebuild = tmp_path / 'rebuild'
    command_build(source['journal'], target, tmp_path / 'backup', rebuild, expected_scope=SCOPE, as_of_date=AS_OF)
    assert command_verify(rebuild) is False
    assert target.read_bytes() == original


@pytest.mark.parametrize('damage', ['null', 'string', 'mixed', 'unhashable_id', 'invalid_quantity', 'invalid_side'])
def test_malformed_journal_legs_preserve_healthy_history_and_raw_records(tmp_path: Path, damage: str) -> None:
    from trade_logger import load_all_trades

    source = evidence_bundle(tmp_path / 'source')
    good_leg = source['events'][0]['legs'][0]
    malformed_legs = {
        'null': [None], 'string': ['bad'], 'mixed': [good_leg, None],
        'unhashable_id': [{**good_leg, 'leg_id': []}],
        'invalid_quantity': [{**good_leg, 'signed_quantity': None}],
        'invalid_side': [{**good_leg, 'side': ['buy_to_open']}],
    }
    legacy = {**source['events'][0], 'trade_id': 'legacy-unknown', 'event_id': 'legacy-unknown:ENTRY',
              'package_id': 'legacy-missing', 'legs': malformed_legs[damage]}
    legacy_line = (json.dumps(legacy) + '\n').encode()
    healthy_bytes = source['journal'].read_bytes()
    source['journal'].write_bytes(healthy_bytes + legacy_line)
    original, ledger_before = source['journal'].read_bytes(), source['ledger'].read_bytes()

    report = command_report(source['journal'], source['ledger'], expected_scope=SCOPE, as_of_date=AS_OF)
    classifications = report['classifications']
    assert classifications['retained-1']['is_eligible'] is True
    bad = classifications['legacy-unknown']
    assert bad['partition'] == 'unattributed' and bad['is_eligible'] is False
    assert 'JOURNAL_LEGS_INVALID' in bad['reason_codes']
    assert command_report(source['journal'], source['ledger'], expected_scope=SCOPE,
                          as_of_date=AS_OF)['classifications'] == classifications
    trades = {trade['trade_id']: trade for trade in load_all_trades(
        source['journal'], ledger_path=source['ledger'], scope=SCOPE, partition='all', as_of_date=AS_OF)}
    assert set(trades) == {'retained-1', 'legacy-unknown'}
    assert trades['retained-1']['is_eligible'] is True
    assert trades['legacy-unknown']['partition'] == 'unattributed'
    assert trades['legacy-unknown']['reason_codes'] == bad['reason_codes']

    rebuild = tmp_path / 'rebuild'
    manifest_path = command_build(source['journal'], source['ledger'], tmp_path / 'backup', rebuild,
                                  expected_scope=SCOPE, as_of_date=AS_OF)
    manifest = json.loads(manifest_path.read_text())
    assert Path(manifest['files']['accepted']['path']).read_bytes() == healthy_bytes
    assert Path(manifest['files']['unattributed']['path']).read_bytes() == legacy_line
    assert json.loads(Path(manifest['parse_errors']['path']).read_text()) == []
    assert command_verify(rebuild) is True
    assert source['journal'].read_bytes() == original
    assert source['ledger'].read_bytes() == ledger_before


@pytest.mark.parametrize(('field', 'value', 'reason'), [
    ('contract.symbol', ['XSP'], 'CONTRACT_MISMATCH'),
    ('contract.symbol', {'name': 'XSP'}, 'CONTRACT_MISMATCH'),
    ('contract.symbol', 42, 'CONTRACT_MISMATCH'),
    ('contract.expiration', ['2026-11-05'], 'CONTRACT_MISMATCH'),
    ('contract.right', ['put'], 'CONTRACT_MISMATCH'),
    ('contract.strike', [690], 'CONTRACT_MISMATCH'),
    ('contract.multiplier', {'value': 100}, 'CONTRACT_MISMATCH'),
    pytest.param('contract.strike', 10 ** 400, 'CONTRACT_MISMATCH', id='overflowed-strike'),
    ('scope', ['paper'], 'JOURNAL_SCOPE_INVALID'),
    ('scope', 'paper', 'JOURNAL_SCOPE_INVALID'),
    ('scope', {**SCOPE.to_dict(), 'environment': ['paper']}, 'JOURNAL_SCOPE_INVALID'),
    ('evidence_refs', [[]], 'EVIDENCE_REFERENCE_MISMATCH'),
    ('evidence_refs', [{}], 'EVIDENCE_REFERENCE_MISMATCH'),
    ('evidence_refs', [None], 'EVIDENCE_REFERENCE_MISMATCH'),
    ('evidence_refs', [True], 'EVIDENCE_REFERENCE_MISMATCH'),
    ('evidence_refs', None, 'EVIDENCE_REFERENCE_MISMATCH'),
])
def test_nested_journal_fields_preserve_healthy_history(
    tmp_path: Path, field: str, value: object, reason: str,
) -> None:
    from trade_logger import load_all_trades

    source = evidence_bundle(tmp_path / 'healthy')
    damaged = evidence_bundle(tmp_path / 'damaged', trade_id='historical-malformed')
    # Both histories have genuine ownership and evidence, so malformed contracts
    # reach the inventory path instead of short-circuiting on missing links.
    ledger = PositionLedger(source['ledger'], scope=SCOPE)
    try:
        with sqlite3.connect(damaged['ledger']) as db:
            db.row_factory = sqlite3.Row
            with ledger._connection:
                for table in ('positions', 'packages', 'legs', 'broker_evidence',
                              'realization_evidence_links', 'fee_evidence'):
                    for row in db.execute(f'SELECT * FROM {table}'):
                        columns = list(row.keys())
                        ledger._connection.execute(
                            f'INSERT INTO {table} ({",".join(columns)}) VALUES ({",".join("?" for _ in columns)})',
                            tuple(row))
    finally:
        ledger.close()
    if field.startswith('contract.'):
        damaged['events'][0]['legs'][0]['contract'][field.split('.')[1]] = value
    else:
        damaged['events'][0][field] = value
    damaged_bytes = ''.join(json.dumps(event) + '\n' for event in damaged['events']).encode()
    healthy_bytes = source['journal'].read_bytes()
    source['journal'].write_bytes(healthy_bytes + damaged_bytes)
    original, ledger_before = source['journal'].read_bytes(), source['ledger'].read_bytes()

    report = command_report(source['journal'], source['ledger'], expected_scope=SCOPE, as_of_date=AS_OF)
    classifications = report['classifications']
    assert classifications['retained-1']['is_eligible'] is True
    bad = classifications['historical-malformed']
    assert bad['partition'] == 'unattributed' and bad['is_eligible'] is False
    assert reason in bad['reason_codes']
    assert command_report(source['journal'], source['ledger'], expected_scope=SCOPE,
                          as_of_date=AS_OF)['classifications'] == classifications
    trades = {trade['trade_id']: trade for trade in load_all_trades(
        source['journal'], ledger_path=source['ledger'], scope=SCOPE, partition='all', as_of_date=AS_OF)}
    assert set(trades) == {'retained-1', 'historical-malformed'}
    assert trades['retained-1']['is_eligible'] is True
    assert trades['historical-malformed']['reason_codes'] == bad['reason_codes']

    rebuild = tmp_path / 'rebuild'
    manifest_path = command_build(source['journal'], source['ledger'], tmp_path / 'backup', rebuild,
                                  expected_scope=SCOPE, as_of_date=AS_OF)
    manifest = json.loads(manifest_path.read_text())
    assert Path(manifest['files']['accepted']['path']).read_bytes() == healthy_bytes
    assert Path(manifest['files']['unattributed']['path']).read_bytes() == damaged_bytes
    assert command_verify(rebuild) is True
    assert source['journal'].read_bytes() == original
    assert source['ledger'].read_bytes() == ledger_before


@pytest.mark.parametrize('metadata', [[None], None, {}, 'provider metadata'])
def test_imported_fee_metadata_cannot_hide_trade_history(tmp_path: Path, metadata: object) -> None:
    from trade_logger import load_all_trades

    source = evidence_bundle(tmp_path / 'source')
    target, export = tmp_path / 'imported.sqlite3', tmp_path / 'export.json'
    export_and_ownership_only(source['ledger'], target, export)
    bundle = json.loads(export.read_text())
    next(row for row in bundle['evidence'] if row['kind'] == 'fee')['payload']['legs'] = metadata
    export.write_text(json.dumps(bundle))
    assert command_import_evidence(export, target, expected_scope=SCOPE) > 0
    report = command_report(source['journal'], target, expected_scope=SCOPE, as_of_date=AS_OF)
    assert report['classifications']['retained-1']['is_eligible'] is True
    trades = load_all_trades(source['journal'], ledger_path=target, scope=SCOPE, partition='all', as_of_date=AS_OF)
    assert len(trades) == 1 and trades[0]['is_eligible'] is True


@pytest.mark.parametrize('damage', ['legs', 'leg_item', 'raw_legs', 'parent'])
def test_import_rejects_invalid_execution_shapes_atomically(tmp_path: Path, damage: str) -> None:
    source = evidence_bundle(tmp_path / 'source')
    target, export = tmp_path / 'imported.sqlite3', tmp_path / 'export.json'
    export_and_ownership_only(source['ledger'], target, export)
    bundle = json.loads(export.read_text())
    payload = next(row for row in bundle['evidence'] if row['kind'] == 'execution')['payload']
    if damage == 'legs':
        payload['legs'] = None
    elif damage == 'leg_item':
        payload['legs'] = [None]
    elif damage == 'raw_legs':
        payload['raw_response']['leg'] = [None]
    else:
        payload['parent_response'] = []
    export.write_text(json.dumps(bundle))
    original = target.read_bytes()
    with pytest.raises(ValueError, match='execution payload'):
        command_import_evidence(export, target, expected_scope=SCOPE)
    assert target.read_bytes() == original


def test_import_actual_fees_upgrades_historical_incomplete_declarations(tmp_path: Path) -> None:
    from strategies.runtime_records import classify_trade, load_ledger_execution_evidence

    source = evidence_bundle(tmp_path / 'source')
    for event in source['events']:
        event['fees_complete'] = False
        event['fee_provenance'] = 'unavailable'
    source['journal'].write_text(''.join(json.dumps(event) + '\n' for event in source['events']))
    original = source['journal'].read_bytes()
    target, export = tmp_path / 'imported.sqlite3', tmp_path / 'export.json'
    export_and_ownership_only(source['ledger'], target, export)
    complete = json.loads(export.read_text())
    execution_only = {**complete, 'evidence': [row for row in complete['evidence'] if row['kind'] != 'fee'], 'fee_records': []}
    export.write_text(json.dumps(execution_only))
    command_import_evidence(export, target, expected_scope=SCOPE)

    def classification() -> dict:
        return classify_trade(source['events'], load_ledger_execution_evidence(target), expected_scope=SCOPE, as_of_date=AS_OF)

    assert classification()['reconciliation_status'] == 'FEE_INCOMPLETE'
    partial = {**complete, 'fee_records': complete['fee_records'][:1]}
    export.write_text(json.dumps(partial))
    command_import_evidence(export, target, expected_scope=SCOPE)
    assert classification()['is_eligible'] is False
    export.write_text(json.dumps(complete))
    command_import_evidence(export, target, expected_scope=SCOPE)
    assert classification()['reconciliation_status'] == 'VERIFIED'
    assert classification()['is_eligible'] is True
    assert source['journal'].read_bytes() == original


def test_full_rebuild_rehearsal(tmp_path: Path) -> None:
    source=evidence_bundle(tmp_path/'source')
    target=tmp_path/'imported.sqlite3'
    export=tmp_path/'provider-export.json'
    export_and_ownership_only(source['ledger'],target,export)
    journal=tmp_path/'journal.jsonl'
    events=source['events']
    legacy={**events[0],'trade_id':'legacy','event_id':'legacy:entry','package_id':'missing'}
    synthetic={**events[0],'trade_id':'synthetic','event_id':'synthetic:entry','source':'synthetic'}
    journal.write_text(''.join(json.dumps(event)+'\n' for event in (events[0],legacy,events[1],events[0],synthetic)))
    original=journal.read_bytes()
    assert command_import_evidence(export,target,expected_scope=SCOPE)>0
    assert command_import_evidence(export,target,expected_scope=SCOPE)==0
    ledger_before=target.read_bytes()
    manifests=[]
    for name in ('a','b'):
        rebuild=tmp_path/name
        path=command_build(journal,target,tmp_path/'backups',rebuild,expected_scope=SCOPE,as_of_date=AS_OF)
        assert command_verify(rebuild)
        manifests.append(json.loads(path.read_text()))
        assert journal.read_bytes()==original
        assert target.read_bytes()==ledger_before
    left,right=manifests
    for name in ('accepted','unattributed','quarantined'):
        assert Path(left['files'][name]['path']).read_bytes()==Path(right['files'][name]['path']).read_bytes()
        assert left['files'][name]['summary']==right['files'][name]['summary']
    assert Path(left['classification_report']['path']).read_bytes()==Path(right['classification_report']['path']).read_bytes()
    assert command_activate(journal,tmp_path/'a',writers_stopped=True)
    assert journal.read_bytes()==Path(left['files']['accepted']['path']).read_bytes()
    assert target.read_bytes()==ledger_before
    assert command_rollback(tmp_path/'a',journal)
    assert journal.read_bytes()==original
    assert target.read_bytes()==ledger_before


def test_import_rejects_wrong_scope_and_ownership_atomically(tmp_path: Path) -> None:
    source=evidence_bundle(tmp_path/'source')
    target=tmp_path/'target'
    export=tmp_path/'export.json'
    export_and_ownership_only(source['ledger'],target,export)
    bundle=json.loads(export.read_text())
    bundle['realization_links'][0]['trade_id']='OTHER'
    export.write_text(json.dumps(bundle))
    before=target.read_bytes()
    with pytest.raises(ValueError,match='ownership'):
        command_import_evidence(export,target,expected_scope=SCOPE)
    assert target.read_bytes()==before


def test_interrupted_activation_metadata_remains_recoverable(tmp_path: Path) -> None:
    from tools import reconcile_trade_history as cli
    bundle,rebuild,path=build(tmp_path)
    original=bundle['journal'].read_bytes()
    atomic=cli._atomic_replace_bytes
    def interrupted(target,raw):
        if target==path and json.loads(raw).get('activation_status')=='activated':
            raise OSError('simulated crash after replacement')
        return atomic(target,raw)
    with patch.object(cli,'_atomic_replace_bytes',side_effect=interrupted):
        assert not command_activate(bundle['journal'],rebuild,writers_stopped=True)
    assert json.loads(path.read_text())['activation_status']=='activating'
    assert command_rollback(rebuild,bundle['journal'])
    assert bundle['journal'].read_bytes()==original


def test_source_is_rechecked_after_journal_lock_acquisition(tmp_path: Path) -> None:
    from tools import reconcile_trade_history as cli
    bundle,rebuild,path=build(tmp_path)
    flock=cli.fcntl.flock
    appended=False
    def competing_append(stream,operation):
        nonlocal appended
        if operation==cli.fcntl.LOCK_EX and not appended:
            with bundle['journal'].open('ab') as writer:
                writer.write(b'\n')
            appended=True
        return flock(stream,operation)
    with patch.object(cli.fcntl,'flock',side_effect=competing_append):
        assert not command_activate(bundle['journal'],rebuild,writers_stopped=True)
    assert json.loads(path.read_text())['activation_status']=='built'
