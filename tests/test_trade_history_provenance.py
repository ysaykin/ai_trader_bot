"""The production classifier must require every retained provenance dimension."""
from __future__ import annotations

import copy
import hashlib
import json
import sqlite3
from pathlib import Path

import pytest

from strategies.runtime_records import classify_trade, load_ledger_execution_evidence, is_fixture_trade
from trade_history_fixtures import SCOPE, AS_OF, evidence_bundle


def classify(bundle, events=None, evidence=None):
    return classify_trade(events if events is not None else bundle['events'],
                          evidence if evidence is not None else load_ledger_execution_evidence(bundle['ledger']),
                          expected_scope=SCOPE,as_of_date=AS_OF)


@pytest.mark.parametrize('record', [{'scope': ['paper']}, {'scope': {'environment': ['paper']}},
                                   {'strikes': ['95', '100', '110', '115']}])
def test_malformed_fixture_markers_are_not_synthetic(record: dict[str, object]) -> None:
    assert is_fixture_trade('filled-unknown', record) is False


def test_complete_lifecycle_and_empty_fees(tmp_path: Path) -> None:
    good=evidence_bundle(tmp_path/'good',events=('ENTRY','ADDITION','ADJUSTMENT','EXIT'))
    assert classify(good)['reconciliation_status']=='VERIFIED'
    incomplete=evidence_bundle(tmp_path/'incomplete',fees=False)
    result=classify(incomplete)
    assert result['partition']=='accepted'
    assert result['reconciliation_status']=='FEE_INCOMPLETE'
    assert result['is_eligible'] is False


@pytest.mark.parametrize('remaining', [1, 0, 2, True, None])
def test_partial_close_requires_exact_remaining_inventory(tmp_path: Path, remaining) -> None:
    bundle = evidence_bundle(tmp_path, events=('ENTRY', 'ADDITION', 'ADJUSTMENT', 'EXIT'))
    adjustment = bundle['events'][2]
    adjustment['remaining_quantity'] = remaining
    with sqlite3.connect(bundle['ledger']) as db:
        db.execute("UPDATE packages SET intent='close' WHERE package_id=?", (adjustment['package_id'],))
    result = classify(bundle)
    assert result['is_eligible'] is (type(remaining) is int and remaining == 1)
    if not result['is_eligible']:
        assert 'PARTIAL_CLOSE_REMAINDER_MISMATCH' in result['reason_codes']


@pytest.mark.parametrize('location', ['raw', 'child', 'parent', 'parent_child'])
@pytest.mark.parametrize(('field', 'value'), [('broker', 'other'), ('account_id', 'OTHER'),
                                           ('environment', 'live'), ('scope_key', 'tradier_live_OTHER')])
def test_explicit_execution_scope_fields_must_agree(tmp_path: Path, location: str, field: str, value: str) -> None:
    bundle = evidence_bundle(tmp_path)
    evidence = load_ledger_execution_evidence(bundle['ledger'])
    payload = evidence['retained']['execution-retained-1-0']['payload']
    if location.startswith('parent'):
        payload['parent_response'] = copy.deepcopy(payload['raw_response'])
        response = payload['parent_response']
    else:
        response = payload['raw_response']
    if location in {'child', 'parent_child'}:
        response = response['leg'][0]
    response[field] = value
    result = classify(bundle, evidence=evidence)
    assert result['partition'] == 'unattributed'
    assert result['is_eligible'] is False
    assert 'RAW_EXECUTION_MISMATCH' in result['reason_codes']


@pytest.mark.parametrize('quantity', [0, -1, 2, True, None, 'nan', '1.5'])
def test_explicit_exec_quantity_cannot_disagree_with_signed_quantity(tmp_path: Path, quantity: object) -> None:
    bundle = evidence_bundle(tmp_path)
    evidence = load_ledger_execution_evidence(bundle['ledger'])
    evidence['retained']['execution-retained-1-0']['payload']['raw_response']['leg'][0]['exec_quantity'] = quantity
    result = classify(bundle, evidence=evidence)
    assert result['partition'] == 'unattributed'
    assert 'RAW_EXECUTION_MISMATCH' in result['reason_codes']


@pytest.mark.parametrize('quantity', [1, '1', '1.0'])
def test_matching_provider_quantities_and_optional_scope_are_accepted(tmp_path: Path, quantity: object) -> None:
    bundle = evidence_bundle(tmp_path)
    evidence = load_ledger_execution_evidence(bundle['ledger'])
    for row in evidence['retained'].values():
        if row['kind'] == 'execution':
            raw = row['payload']['raw_response']
            raw['scope'] = {'account_id': SCOPE.account_id}
            for child in raw['leg']:
                child['exec_quantity'] = quantity
    assert classify(bundle, evidence=evidence)['is_eligible'] is True


@pytest.mark.parametrize('location', ['raw', 'parent'])
@pytest.mark.parametrize('conflicting', [False, True])
@pytest.mark.parametrize('duplicate_first', [False, True])
def test_duplicate_provider_child_identity_is_ambiguous(
    tmp_path: Path, location: str, conflicting: bool, duplicate_first: bool,
) -> None:
    bundle = evidence_bundle(tmp_path)
    evidence = load_ledger_execution_evidence(bundle['ledger'])
    payload = evidence['retained']['execution-retained-1-0']['payload']
    response = payload['raw_response']
    if location == 'parent':
        response = payload['parent_response'] = copy.deepcopy(response)
    duplicate = copy.deepcopy(response['leg'][0])
    if conflicting:
        duplicate['exec_quantity'] = 0
    response['leg'].insert(0 if duplicate_first else len(response['leg']), duplicate)
    result = classify(bundle, evidence=evidence)
    assert result['partition'] == 'unattributed'
    assert 'RAW_EXECUTION_MISMATCH' in result['reason_codes']


@pytest.mark.parametrize('damage', ['quantity', 'side', 'contract', 'option_symbol',
                                    'identity', 'child_identity', 'child_parent', 'missing_child'])
def test_supplied_parent_fields_cannot_contradict_execution(tmp_path: Path, damage: str) -> None:
    bundle = evidence_bundle(tmp_path)
    evidence = load_ledger_execution_evidence(bundle['ledger'])
    payload = evidence['retained']['execution-retained-1-0']['payload']
    parent = payload['parent_response'] = copy.deepcopy(payload['raw_response'])
    child = parent['leg'][0]
    if damage == 'quantity':
        child['signed_quantity'] = 2
    elif damage == 'side':
        child['side'] = 'sell_to_close'
    elif damage == 'contract':
        child['contract']['strike'] = 999
    elif damage == 'option_symbol':
        child['option_symbol'] = 'XSP261105P00999000'
    elif damage == 'identity':
        parent['id'] = 'WRONG-PARENT'
    elif damage == 'child_identity':
        child['id'] = 'WRONG-CHILD'
    elif damage == 'child_parent':
        child['parent_order_id'] = 'WRONG-PARENT'
    else:
        parent['leg'].pop(0)
    result = classify(bundle, evidence=evidence)
    assert result['partition'] == 'unattributed'
    assert 'RAW_EXECUTION_MISMATCH' in result['reason_codes']


def test_wrong_explicit_child_parent_cannot_be_overridden_by_parent_membership(tmp_path: Path) -> None:
    bundle = evidence_bundle(tmp_path)
    evidence = load_ledger_execution_evidence(bundle['ledger'])
    payload = evidence['retained']['execution-retained-1-0']['payload']
    payload['parent_response'] = copy.deepcopy(payload['raw_response'])
    for child in payload['parent_response']['leg']:
        child['id'] = child['order_id']
    payload['raw_response']['leg'][0]['parent_order_id'] = 'WRONG-PARENT'
    assert 'RAW_EXECUTION_MISMATCH' in classify(bundle, evidence=evidence)['reason_codes']


@pytest.mark.parametrize('parent_quantity', [0, 1])
def test_cached_parent_may_precede_a_later_child_fill(tmp_path: Path, parent_quantity: int) -> None:
    from strategies.runtime_records import _raw_execution_matches

    bundle = evidence_bundle(tmp_path)
    evidence = load_ledger_execution_evidence(bundle['ledger'])
    for row in evidence['retained'].values():
        if row['kind'] != 'execution':
            continue
        payload = row['payload']
        parent = payload['parent_response'] = copy.deepcopy(payload['raw_response'])
        for child in parent['leg']:
            child['id'] = child.pop('order_id')
            child.pop('signed_quantity')
            child.pop('parent_order_id')
            child['exec_quantity'] = parent_quantity
        # Exercise SDK child responses that rely on retained parent membership.
        payload['legs'] = payload['legs'][:1]
        payload['raw_response'] = copy.deepcopy(parent['leg'][0])
        payload['raw_response']['exec_quantity'] = 1
        assert _raw_execution_matches(payload, payload['legs'][0]) is True


@pytest.mark.parametrize(('dimension','value','reason'),[
    ('broker','other','BROKER_MISMATCH'),('account_id','OTHER','ACCOUNT_MISMATCH'),
    ('environment','live','ENVIRONMENT_MISMATCH'),('package_id','other','PACKAGE_OWNERSHIP_MISMATCH'),
    ('contract',{'symbol':'SPX','expiration':'2026-11-05','right':'put','strike':690,'multiplier':100},'CONTRACT_MISMATCH'),
    ('side','sell_to_close','SIDE_MISMATCH'),('signed_quantity',2,'QUANTITY_MISMATCH'),
])
@pytest.mark.parametrize('missing',[False,True])
def test_seven_dimensions_independently_required(tmp_path: Path,dimension,value,reason,missing) -> None:
    bundle=evidence_bundle(tmp_path)
    events=copy.deepcopy(bundle['events'])
    target=events[0]['scope'] if dimension in ('broker','account_id','environment') else events[0]['legs'][0] if dimension in ('contract','side','signed_quantity') else events[0]
    if missing:
        target.pop(dimension)
    else:
        target[dimension]=value
    result=classify(bundle,events)
    assert result['partition']=='unattributed'
    assert result['is_eligible'] is False
    assert reason in result['reason_codes'] or reason.replace('_MISMATCH','_MISSING') in result['reason_codes']


@pytest.mark.parametrize('intermediate',['ADDITION','ADJUSTMENT'])
def test_unsupported_intermediate_realization_rejects_whole_trade(tmp_path: Path,intermediate) -> None:
    bundle=evidence_bundle(tmp_path)
    unsupported={**bundle['events'][0],'event':intermediate,'event_id':'unverified','package_id':'unverified'}
    assert classify(bundle,[bundle['events'][0],unsupported,bundle['events'][1]])['partition']=='unattributed'


def test_reused_fill_across_realizations_is_rejected(tmp_path: Path) -> None:
    bundle=evidence_bundle(tmp_path)
    evidence=load_ledger_execution_evidence(bundle['ledger'])
    evidence['links'].extend({**link,'event_id':'reused','trade_id':'other'} for link in list(evidence['links']) if link['event_id'].endswith('ENTRY'))
    assert 'REUSED_EXECUTION_QUANTITY' in classify(bundle,evidence=evidence)['reason_codes']


def test_non_execution_allocation_rejects_without_traversing_fee_metadata(tmp_path: Path) -> None:
    bundle = evidence_bundle(tmp_path)
    evidence = load_ledger_execution_evidence(bundle['ledger'])
    evidence['retained']['fee-retained-1-0']['payload']['legs'] = [None]
    evidence['links'][0]['evidence_id'] = 'fee-retained-1-0'
    result = classify(bundle, evidence=evidence)
    assert result['partition'] == 'unattributed'
    assert 'MISSING_RETAINED_EVIDENCE' in result['reason_codes']


def test_conflicting_duplicate_event_ids(tmp_path: Path) -> None:
    bundle=evidence_bundle(tmp_path)
    conflicting={**bundle['events'][0],'quantity':5}
    assert 'CONFLICTING_EVENT_ID' in classify(bundle,[*bundle['events'],conflicting])['reason_codes']
    assert classify(bundle,[*bundle['events'],bundle['events'][0]])['is_eligible'] is True


@pytest.mark.parametrize('assertion',[{'settlement_verified':True},{'broker_parent_id':'plausible-id','broker':'tradier','account_id':'REDACTED','legs':[{'quantity':1,'side':'buy_to_open'}]}])
def test_embedded_assertions_and_orphan_settlement_rows_are_insufficient(tmp_path: Path,assertion) -> None:
    bundle=evidence_bundle(tmp_path)
    evidence=load_ledger_execution_evidence(bundle['ledger'])
    evidence['links']=[]
    evidence['settlements']={'retained-1':{'position_id':'orphan','fees_complete':1}}
    events=copy.deepcopy(bundle['events'])
    for event in events:
        event['execution_evidence']=assertion
    assert classify(bundle,events,evidence)['is_eligible'] is False


def test_missing_raw_or_wrong_retained_scope_rejects(tmp_path: Path) -> None:
    bundle=evidence_bundle(tmp_path)
    for field,value in [('raw_response',{}),('scope',{**SCOPE.to_dict(),'account_id':'OTHER'})]:
        evidence=load_ledger_execution_evidence(bundle['ledger'])
        evidence['retained']['execution-retained-1-0']['payload'][field]=value
        assert classify(bundle,evidence=evidence)['is_eligible'] is False


def test_legacy_loading_is_read_only_and_diagnosed(tmp_path: Path) -> None:
    path=tmp_path/'legacy'
    with sqlite3.connect(path) as db:
        db.execute('CREATE TABLE packages (package_id TEXT)')
    before=path.read_bytes()
    evidence=load_ledger_execution_evidence(path)
    assert any('MISSING_EVIDENCE_SCHEMA' in item for item in evidence['diagnostics'])
    assert path.read_bytes()==before
    assert classify_trade([{'event':'ENTRY','trade_id':'legacy','expiry':'2026-11-05'}],evidence,expected_scope=SCOPE,as_of_date=AS_OF)['partition']=='unattributed'


@pytest.mark.parametrize('legs', [[None], None, {'invalid': 'legs'}])
def test_invalid_retained_execution_is_diagnosed_without_hiding_history(tmp_path: Path, legs: object) -> None:
    from strategies.position_ledger import PositionLedger
    from tools.reconcile_trade_history import command_build, command_verify
    from trade_logger import load_all_trades
    from trade_history_fixtures import ACQUIRED

    bundle = evidence_bundle(tmp_path / 'source')
    ledger = PositionLedger(bundle['ledger'], scope=SCOPE)
    try:
        # Simulate an older retained row without rewriting its content or hash.
        ledger.record_broker_evidence('invalid-execution', scope=SCOPE, kind='execution',
                                      provider_identity='invalid-execution', acquisition_source='provider-export',
                                      acquired_at=ACQUIRED, payload={'legs': legs, 'raw_response': {'id': 'old'}})
    finally:
        ledger.close()
    original = bundle['ledger'].read_bytes()
    evidence = load_ledger_execution_evidence(bundle['ledger'])
    assert evidence['invalid_evidence'] == {'invalid-execution': 'INVALID_EXECUTION_PAYLOAD'}
    assert 'invalid-execution' not in evidence['retained']
    assert classify(bundle, evidence=evidence)['is_eligible'] is True
    trades = load_all_trades(bundle['journal'], ledger_path=bundle['ledger'], scope=SCOPE,
                             partition='all', as_of_date=AS_OF)
    assert len(trades) == 1 and trades[0]['is_eligible'] is True
    evidence['links'][0]['evidence_id'] = 'invalid-execution'
    assert classify(bundle, evidence=evidence)['partition'] == 'unattributed'
    rebuild = tmp_path / 'rebuild'
    command_build(bundle['journal'], bundle['ledger'], tmp_path / 'backup', rebuild,
                  expected_scope=SCOPE, as_of_date=AS_OF)
    assert command_verify(rebuild) is False
    assert bundle['ledger'].read_bytes() == original


def test_names_alone_are_not_fixture_proof() -> None:
    for name in ('test_real_execution','mock_maker','fixture-account','filled-live-order'):
        assert is_fixture_trade(name,{}) is False
    assert is_fixture_trade('filled-123',{'strikes':dict(long_put=95,short_put=100,short_call=110,long_call=115)}) is True


@pytest.mark.parametrize('damage',['missing_official','wrong_account','wrong_expiry','orphan','missing_raw','wrong_cash',
                                   'raw_cash','raw_contract','raw_quantity','raw_disposition','missing_disposition'])
def test_settlement_requires_retained_attributable_evidence(tmp_path: Path,damage: str) -> None:
    from datetime import date
    from strategies.position_ledger import PositionLedger
    from strategies.xsp_settlement import create_settlement_value,create_broker_cash_evidence
    from trade_history_fixtures import retain_settlement
    bundle=evidence_bundle(tmp_path,events=('ENTRY',),fees=False)
    ledger=PositionLedger(bundle['ledger'],scope=SCOPE)
    links=retain_settlement(ledger,'position-retained-1',create_settlement_value('2026-11-05','700'),
                            create_broker_cash_evidence(SCOPE.account_id,'activity','2026-11-05',0,True))
    ledger.expire_strategy_positions('IronCondor_V31',date(2026,11,6))
    ledger.flush_outbox(bundle['journal'])
    ledger.close()
    events=[json.loads(line) for line in bundle['journal'].read_text().splitlines()]
    evidence=load_ledger_execution_evidence(bundle['ledger'])
    assert classify(bundle,events,evidence)['reconciliation_status']=='FEE_INCOMPLETE'
    if damage=='missing_official':
        evidence['retained'].pop(links['settlement_evidence_id'])
    elif damage=='orphan':
        evidence['settlement_links']=[]
    elif damage=='wrong_account':
        evidence['retained'][links['cash_evidence_id']]['account_id']='OTHER'
    elif damage=='wrong_expiry':
        evidence['retained'][links['cash_evidence_id']]['payload']['expiration']='2026-11-04'
    elif damage=='wrong_cash':
        evidence['retained'][links['cash_evidence_id']]['payload']['cash_cents']=100
    elif damage.startswith('raw_') or damage=='missing_disposition':
        raw=evidence['retained'][links['cash_evidence_id']]['payload']['raw_response']
        if damage=='raw_cash':
            raw['cash_cents']=999999
        elif damage=='raw_contract':
            raw['legs'][0]['contract']['symbol']='SPX'
        elif damage=='raw_quantity':
            raw['legs'][0]['signed_quantity']=999
        elif damage=='raw_disposition':
            raw['disposition_confirmed']=False
        else:
            raw.pop('disposition_confirmed')
    else:
        evidence['retained'][links['settlement_evidence_id']]['payload']['raw_response']={}
    assert classify(bundle,events,evidence)['partition']=='unattributed'


@pytest.mark.parametrize('damage',['wrong_amount','wrong_order','empty_raw','missing_fee_rows'])
def test_actual_fee_coverage_requires_matching_retained_provider_fields(tmp_path: Path,damage: str) -> None:
    bundle=evidence_bundle(tmp_path)
    evidence=load_ledger_execution_evidence(bundle['ledger'])
    if damage=='missing_fee_rows':
        evidence['fees']=[]
    else:
        payload=evidence['retained']['fee-retained-1-0']['payload']
        if damage=='wrong_amount':payload['raw_response']['commission']='999.0'
        elif damage=='wrong_order':payload['raw_response']['order_id']='other'
        else:payload['raw_response']={}
    result=classify(bundle,evidence=evidence)
    assert result['partition']=='accepted'
    assert result['reconciliation_status']=='FEE_INCOMPLETE'
    assert result['is_eligible'] is False


def test_versioned_redacted_positive_fixture_uses_production_verifier(tmp_path: Path) -> None:
    from strategies.position_ledger import PositionLedger
    from strategies.xsp_settlement import FeeEvidence
    fixture=json.loads((Path(__file__).parent/'fixtures/trade_history_provenance_v2.json').read_text())
    positive=fixture['positive']
    path=tmp_path/'fixture.sqlite3'
    ledger=PositionLedger(path,scope=SCOPE)
    try:
        with ledger._connection:
            for table in ('positions','packages','legs'):
                for row in positive['ownership'][table]:
                    columns=list(row)
                    ledger._connection.execute(f'INSERT INTO {table} ({",".join(columns)}) VALUES ({",".join("?" for _ in columns)})',tuple(row.values()))
        for row in positive['evidence']:
            ledger.record_broker_evidence(row['evidence_id'],scope=SCOPE,kind=row['kind'],provider_identity=row['provider_identity'],
                                          acquisition_source=row['acquisition_source'],acquired_at=row['acquired_at'],payload=row['payload'])
        for link in positive['realization_links']:
            ledger.link_realization_evidence(**link)
        for row in positive['fee_records']:
            fee=FeeEvidence(row['identity'],row['amount_cents'],bool(row['complete']),row['category'],row['provenance'])
            ledger.record_fee_evidence(fee,trade_id=row['trade_id'],package_id=row['package_id'],position_id=row['position_id'],
                                       raw_evidence_id=row['raw_evidence_id'],event_id=row['event_id'])
    finally:
        ledger.close()
    result=classify_trade(positive['events'],load_ledger_execution_evidence(path),expected_scope=SCOPE,as_of_date=fixture['as_of_date'])
    assert result['is_eligible'] is True
    assert result['reconciliation_status']=='VERIFIED'


@pytest.mark.parametrize(('field','wrong'),[('symbol','SPX'),('expiration','2026-11-04'),('right','call'),('strike',691),('multiplier',10)])
@pytest.mark.parametrize('missing',[False,True])
def test_each_contract_identity_field_is_required(tmp_path: Path,field,wrong,missing) -> None:
    bundle=evidence_bundle(tmp_path)
    events=copy.deepcopy(bundle['events'])
    contract=events[0]['legs'][0]['contract']
    if missing:contract.pop(field)
    else:contract[field]=wrong
    result=classify(bundle,events)
    assert result['partition']=='unattributed'
    assert 'CONTRACT_MISMATCH' in result['reason_codes']


@pytest.mark.parametrize('field',['order_id','parent_order_id'])
@pytest.mark.parametrize('missing',[False,True])
def test_journal_child_and_parent_relationship_must_match(tmp_path: Path,field,missing) -> None:
    bundle=evidence_bundle(tmp_path)
    events=copy.deepcopy(bundle['events'])
    if missing:events[0]['legs'][0].pop(field)
    else:events[0]['legs'][0][field]='other'
    result=classify(bundle,events)
    assert result['partition']=='unattributed'
    assert 'PACKAGE_OWNERSHIP_MISMATCH' in result['reason_codes']
