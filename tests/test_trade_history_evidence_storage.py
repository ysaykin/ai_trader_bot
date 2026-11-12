"""Additive storage and retained content integrity checks."""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from strategies.position_ledger import PositionLedger
from strategies.runtime_records import RecordScope
from trade_history_fixtures import SCOPE, ACQUIRED, evidence_bundle


@pytest.mark.parametrize('damage', ['amount', 'activity', 'scope'])
def test_invalid_raw_fees_cannot_certify_live_settlement(tmp_path: Path, damage: str) -> None:
    from datetime import date
    from strategies.xsp_settlement import FeeEvidence, create_settlement_value, create_broker_cash_evidence
    from trade_history_fixtures import retain_settlement

    bundle = evidence_bundle(tmp_path, events=('ENTRY',), fees=False)
    ledger = PositionLedger(bundle['ledger'], scope=SCOPE)
    try:
        pid, event_id = 'position-retained-1', 'position-retained-1:SETTLEMENT'
        retain_settlement(ledger, pid, create_settlement_value('2026-11-05', '700'),
                          create_broker_cash_evidence(SCOPE.account_id, 'activity', '2026-11-05', 0, True))
        raw = dict(commission_cents=80, activity_id='activity')
        if damage == 'amount':
            raw['commission_cents'] = 99999
        elif damage == 'activity':
            raw['activity_id'] = 'UNRELATED'
        else:
            raw['scope'] = {**SCOPE.to_dict(), 'account_id': 'OTHER'}
        payload = dict(scope=SCOPE.to_dict(), event_id=event_id, position_id=pid, trade_id='retained-1',
                       amount_cents=80, complete=True, raw_response=raw)
        ledger.record_broker_evidence('bad-fee', scope=SCOPE, kind='fee', provider_identity='bad-fee',
                                      acquisition_source='provider-export', acquired_at=ACQUIRED, payload=payload)
        fee = FeeEvidence('bad-fee', 80, True, 'settlement')
        with pytest.raises(ValueError, match='Fee record'):
            ledger.record_fee_evidence(fee, trade_id='retained-1', position_id=pid,
                                       raw_evidence_id='bad-fee', event_id=event_id)
        # Revalidate rows written by the older validator before certifying fees.
        with ledger._connection:
            ledger._connection.execute(
                'INSERT INTO fee_evidence '
                '(identity,trade_id,position_id,amount_cents,complete,category,created_at,raw_evidence_id,event_id) '
                'VALUES (?,?,?,80,1,?,?,?,?)',
                ('bad-fee', 'retained-1', pid, 'settlement', ACQUIRED, 'bad-fee', event_id))
        assert ledger.expire_strategy_positions('IronCondor_V31', date(2026, 11, 6)) == 1
        reconciliation = ledger.get_settlement_reconciliation(pid)
        assert reconciliation['fees_complete'] == 0
        assert reconciliation['fees_cents'] is None and reconciliation['net_cents'] is None
        assert reconciliation['known_fees_cents'] == 0
        assert ledger.position_quantities(pid) == {}
        payload = json.loads(ledger._connection.execute('SELECT payload FROM outbox').fetchone()[0])
        assert payload['reconciliation_status'] == 'FEE_INCOMPLETE'
        assert payload['fees'] is None and payload['net_pnl'] is None
    finally:
        ledger.close()


@pytest.mark.parametrize('supplied_amount', [1, 80])
def test_supplied_fees_must_match_retained_amounts(tmp_path: Path, supplied_amount: int) -> None:
    from datetime import date
    from strategies.xsp_settlement import FeeEvidence, create_settlement_value, create_broker_cash_evidence
    from trade_history_fixtures import retain_settlement

    bundle = evidence_bundle(tmp_path, events=('ENTRY',), fees=False)
    ledger = PositionLedger(bundle['ledger'], scope=SCOPE)
    try:
        pid = 'position-retained-1'
        fee = FeeEvidence('actual-fee', 80, True, 'settlement')
        retain_settlement(ledger, pid, create_settlement_value('2026-11-05', '700'),
                          create_broker_cash_evidence(SCOPE.account_id, 'activity', '2026-11-05', 0, True), fee=fee)
        supplied = FeeEvidence(fee.identity, supplied_amount, True, 'settlement')
        assert ledger.expire_strategy_positions('IronCondor_V31', date(2026, 11, 6), fees=(supplied,)) == 1
        reconciliation = ledger.get_settlement_reconciliation(pid)
        assert ledger.get_fee_evidence(position_id=pid)[0].amount_cents == 80
        assert reconciliation['known_fees_cents'] == 80
        assert reconciliation['fees_complete'] == int(supplied_amount == 80)
        assert reconciliation['fees_cents'] == (80 if supplied_amount == 80 else None)
        assert (reconciliation['net_cents'] is None) == (supplied_amount != 80)
    finally:
        ledger.close()


@pytest.mark.parametrize('damage', ['amount', 'contract', 'quantity', 'disposition', 'scope'])
def test_contradictory_raw_cash_rejected_and_inventory_retained(tmp_path: Path, damage: str) -> None:
    from datetime import date
    from strategies.xsp_settlement import create_settlement_value, create_broker_cash_evidence
    from trade_history_fixtures import retain_settlement

    bundle = evidence_bundle(tmp_path, events=('ENTRY',), fees=False)
    ledger = PositionLedger(bundle['ledger'], scope=SCOPE)
    try:
        pid = 'position-retained-1'
        settlement = create_settlement_value('2026-11-05', '700')
        cash = create_broker_cash_evidence(SCOPE.account_id, 'activity', '2026-11-05', 0, True)
        links = retain_settlement(ledger, pid, settlement, cash, link=False)
        payload = ledger.get_broker_evidence(links['cash_evidence_id'])['payload']
        payload['activity_id'] = payload['raw_response']['activity_id'] = 'old-activity'
        raw = payload['raw_response']
        if damage == 'amount':
            raw['cash_cents'] = 999999
        elif damage == 'contract':
            raw['legs'][0]['contract']['symbol'] = 'SPX'
        elif damage == 'quantity':
            raw['legs'][0]['signed_quantity'] = 999
        elif damage == 'disposition':
            raw['disposition_confirmed'] = False
        else:
            raw['scope'] = {**SCOPE.to_dict(), 'account_id': 'OTHER'}
        ledger.record_broker_evidence('old-cash', scope=SCOPE, kind='cash', provider_identity='old-activity',
                                      acquisition_source='provider-export', acquired_at=ACQUIRED, payload=payload)
        cash = create_broker_cash_evidence(SCOPE.account_id, 'old-activity', '2026-11-05', 0, True)
        with pytest.raises(ValueError, match='Cash activity'):
            ledger.record_broker_cash_evidence(cash, raw_evidence_id='old-cash')
        allocation = dict(position_id=pid, trade_id='retained-1', **{**links, 'cash_evidence_id': 'old-cash'}, allocated_cash_cents=0)
        with pytest.raises(ValueError, match='attribution'):
            ledger.link_settlement_evidence(**allocation)
        # Replay rows persisted by the older validator, retaining valid content hashes.
        with ledger._connection:
            ledger._connection.execute(
                'INSERT INTO broker_cash_evidence '
                'SELECT ?,account_scope,expiration,symbol,cash_cents,disposition_confirmed,description,created_at,? '
                'FROM broker_cash_evidence WHERE activity_id=?', ('old-activity', 'old-cash', 'activity'))
            ledger._connection.execute(
                'INSERT INTO settlement_evidence_links VALUES (?,?,?,?,?,?)',
                tuple(allocation[key] for key in ('position_id','event_id','trade_id','settlement_evidence_id','cash_evidence_id','allocated_cash_cents')))
        quantities = ledger.position_quantities(pid)
        risk = ledger.portfolio_defined_risk('IronCondor_V31')
        assert ledger.expire_strategy_positions('IronCondor_V31', date(2026, 11, 6)) == 0
        assert ledger.position_quantities(pid) == quantities
        assert ledger.portfolio_defined_risk('IronCondor_V31') == risk
        assert ledger.get_basket('retained-1-0').state == 'awaiting_settlement'
        assert ledger._connection.execute('SELECT count(*) FROM outbox').fetchone()[0] == 0
    finally:
        ledger.close()


def test_migration_and_restart_preserve_ownership_and_cash(tmp_path: Path) -> None:
    bundle = evidence_bundle(tmp_path)
    with sqlite3.connect(bundle['ledger']) as db:
        before = db.execute('SELECT * FROM packages').fetchall(), db.execute('SELECT * FROM legs').fetchall()
    for _ in range(2):
        ledger = PositionLedger(bundle['ledger'], scope=SCOPE)
        assert ledger.get_broker_evidence('execution-retained-1-0')['payload']['raw_response']['status'] == 'filled'
        assert len(ledger.realization_evidence('retained-1-0:ENTRY')) == 2
        assert not ledger._connection.execute('PRAGMA foreign_key_check').fetchall()
        ledger.close()
    with sqlite3.connect(bundle['ledger']) as db:
        assert before == (db.execute('SELECT * FROM packages').fetchall(), db.execute('SELECT * FROM legs').fetchall())


def test_legacy_pending_outbox_replays_before_new_events_and_deduplicates(tmp_path: Path) -> None:
    from datetime import date
    from strategies.runtime_records import classify_trade, load_ledger_execution_evidence
    from strategies.xsp_settlement import create_settlement_value, create_broker_cash_evidence
    from trade_history_fixtures import retain_settlement

    bundle = evidence_bundle(tmp_path, events=('ENTRY',), fees=False)
    ledger = PositionLedger(bundle['ledger'], scope=SCOPE)
    legacy = dict(event='EXIT', trade_id='old', strategy='owner', exit_reason='EXPIRED_WORTHLESS',
                  gross_pnl=1., fees=None, net_pnl=None, outcome='EXPIRED_WORTHLESS',
                  known_fees=0., fees_complete=False, status='CLOSED')
    with ledger._connection:
        ledger._connection.execute(
            "INSERT INTO outbox (event_type,payload,status,created_at) VALUES ('EXIT',?,'pending',?)",
            (json.dumps(legacy), ACQUIRED))
    ledger.close()
    ledger = PositionLedger(bundle['ledger'], scope=SCOPE)
    try:
        retain_settlement(ledger, 'position-retained-1', create_settlement_value('2026-11-05', '700'),
                          create_broker_cash_evidence(SCOPE.account_id, 'activity', '2026-11-05', 0, True))
        assert ledger.expire_strategy_positions('IronCondor_V31', date(2026, 11, 6)) == 1
        quantities = ledger.all_owned_quantities()
        assert ledger.flush_outbox(bundle['journal']) == 2
        events = [json.loads(line) for line in bundle['journal'].read_text().splitlines()]
        old, new = events[-2:]
        assert old['event_id'].startswith('legacy-outbox:1:')
        assert old['scope'] == SCOPE.to_dict() and old['evidence_refs'] == []
        assert old['source'] == 'unattributed'
        assert old['fees'] is None and old['net_pnl'] is None
        assert new['event_id'] == 'position-retained-1:SETTLEMENT'
        result = classify_trade([old], load_ledger_execution_evidence(bundle['ledger']), expected_scope=SCOPE, as_of_date='2026-11-06')
        assert result['partition'] == 'unattributed' and result['is_eligible'] is False
        # Retry the append-after-crash window for both old and current payloads.
        with ledger._connection:
            ledger._connection.execute("UPDATE outbox SET status='pending'")
        original = bundle['journal'].read_bytes()
        assert ledger.flush_outbox(bundle['journal']) == 2
        assert bundle['journal'].read_bytes() == original
        assert ledger.flush_outbox(bundle['journal']) == 0
        assert ledger.all_owned_quantities() == quantities
    finally:
        ledger.close()


def test_immutable_idempotent_and_scope_checked(tmp_path: Path) -> None:
    ledger = PositionLedger(tmp_path/'ledger', scope=SCOPE)
    kwargs = dict(scope=SCOPE, kind='execution', provider_identity='123', acquisition_source='provider-export', acquired_at=ACQUIRED, payload={'raw_response':{'id':123}})
    ledger.record_broker_evidence('ev', **kwargs)
    ledger.record_broker_evidence('ev', **kwargs)
    with pytest.raises(ValueError, match='Conflicting'):
        ledger.record_broker_evidence('ev', **{**kwargs,'payload':{'changed':True}})
    with pytest.raises(ValueError, match='Conflicting'):
        ledger.record_broker_evidence('other', **kwargs)
    with pytest.raises(ValueError, match='scope'):
        ledger.record_broker_evidence('wrong', **{**kwargs,'scope':RecordScope('live','tradier','OTHER')})
    with pytest.raises(sqlite3.IntegrityError, match='immutable'):
        ledger._connection.execute("UPDATE broker_evidence SET sha256='wrong'")
    with pytest.raises(sqlite3.IntegrityError):
        ledger._connection.execute("INSERT INTO realization_evidence_links VALUES ('e','t','missing','p','l','ev',1)")
    ledger.close()


def test_legacy_read_only_does_not_migrate(tmp_path: Path) -> None:
    path = tmp_path/'legacy'
    with sqlite3.connect(path) as db:
        db.execute('CREATE TABLE packages (package_id TEXT PRIMARY KEY)')
    before = path.read_bytes()
    ledger = PositionLedger(path, read_only=True)
    ledger.close()
    assert path.read_bytes() == before


def test_ownership_and_duplicate_allocation(tmp_path: Path) -> None:
    bundle=evidence_bundle(tmp_path)
    ledger=PositionLedger(bundle['ledger'],scope=SCOPE)
    link=ledger.realization_evidence('retained-1-0:ENTRY')[0]
    ledger.link_realization_evidence(**link)
    with pytest.raises(ValueError, match='Conflicting'):
        ledger.link_realization_evidence(**{**link,'allocated_signed_quantity':2})
    with pytest.raises(ValueError, match='ownership'):
        ledger.link_realization_evidence(**{**link,'trade_id':'other'})
    ledger.close()


def test_additive_migration_of_legacy_filled_package_preserves_risk_and_cash(tmp_path: Path) -> None:
    from types import SimpleNamespace
    from strategies.position_ledger import asset_key
    path=tmp_path/'legacy-filled.sqlite3'
    with sqlite3.connect(path) as db:
        db.executescript('''CREATE TABLE packages (
          package_id TEXT PRIMARY KEY,strategy_id TEXT NOT NULL,intent TEXT NOT NULL,state TEXT NOT NULL,
          opening_cash_flow REAL,closing_cash_flow REAL,created_at TEXT NOT NULL,updated_at TEXT NOT NULL);
          CREATE TABLE legs (order_id TEXT PRIMARY KEY,package_id TEXT NOT NULL REFERENCES packages(package_id),
          asset_key TEXT NOT NULL,requested_signed_quantity INTEGER NOT NULL,filled_signed_quantity INTEGER NOT NULL,
          fill_price REAL,status TEXT NOT NULL,UNIQUE(package_id,asset_key));''')
        db.execute("INSERT INTO packages VALUES ('legacy','owner','open','open',200,NULL,?,?)",(ACQUIRED,ACQUIRED))
        for index,(strike,quantity) in enumerate(((690,1),(695,-1))):
            key=asset_key(SimpleNamespace(symbol='XSP',expiration='2026-11-05',right='put',strike=strike))
            db.execute('INSERT INTO legs VALUES (?,?,?,?,?,?,?)',(str(index),'legacy',key,quantity,quantity,1.,'filled'))
    for _ in range(2):
        ledger=PositionLedger(path,scope=SCOPE)
        basket=ledger.get_basket('legacy')
        assert basket.opening_cash_flow==200.
        assert sorted(ledger.position_quantities(basket.position_id).values())==[-1,1]
        assert ledger.portfolio_defined_risk('owner')==500.
        assert ledger._connection.execute('SELECT count(*) FROM broker_evidence').fetchone()[0]==0
        ledger.close()
