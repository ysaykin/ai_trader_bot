"""User retirement hides only expired broker-flat positions and preserves facts."""
from datetime import datetime, timedelta, timezone
from pathlib import Path
import json
import sqlite3

import pytest

from dashboard.positions import build_positions, read_packages
from dashboard.monitoring import order_activity
from strategies.position_ledger import LedgerLeg, PositionLedger
from strategies.runtime_records import classify_trade, load_ledger_execution_evidence
from strategies.scoped_recovery import contract_symbol
from tools.archive_legacy_positions import archive_positions, plan_archive
from trade_history_fixtures import SCOPE, evidence_bundle
from test_concurrent_positions import open_position
from trade_logger import load_all_trades

NOW = datetime(2026, 11, 6, 15, tzinfo=timezone.utc)


@pytest.fixture
def setup(tmp_path):
    bundle = evidence_bundle(tmp_path/'original', events=('ENTRY',))
    ledger = PositionLedger(bundle['ledger'], scope=SCOPE)
    old = bundle['events'][0]['position_id']
    owner = bundle['events'][0]['strategy']
    current = open_position(ledger, 'current', owner=owner)
    actual = [{'symbol': contract_symbol(k), 'quantity': q} for k, q in ledger.position_quantities(current).items()]
    evidence = dict(scope=SCOPE.to_dict(), acquired_at=NOW.isoformat(), positions_complete=True,
                    orders_complete=True, positions=actual, orders=[])
    ledger.close()
    return bundle, old, current, evidence, tmp_path/'archive'


def apply(setup, **kwargs):
    bundle, old, _, evidence, archive = setup
    return archive_positions(bundle['ledger'], [old], evidence, archive, now=NOW,
                             journals=[bundle['journal']], writers_stopped=True, **kwargs)


def test_archive_removes_active_tracking_but_preserves_evidence_and_same_owner_current_position(setup):
    bundle, old, current, evidence, archive = setup
    original_journal = bundle['journal'].read_bytes()
    ledger = PositionLedger(bundle['ledger'], read_only=True)
    peer = ledger.position_quantities(current)
    owner = ledger.get_position(current).strategy_id
    before_counts = {t: ledger._connection.execute(f'SELECT COUNT(*) FROM {t}').fetchone()[0]
                     for t in ('legs', 'positions', 'broker_evidence', 'realization_evidence_links', 'fee_evidence')}
    ledger.close()
    before = bundle['ledger'].read_bytes()
    assert len(plan_archive(bundle['ledger'], [old], evidence, now=NOW)['positions']) == 1
    assert bundle['ledger'].read_bytes() == before
    assert apply(setup)['archived_positions'] == 1
    ledger = PositionLedger(bundle['ledger'], scope=SCOPE)
    assert ledger.position_quantities(old) == {}
    assert ledger.position_quantities(current) == peer
    assert ledger.all_owned_quantities() == peer
    assert ledger.owned_quantities(owner) == peer
    assert [p.position_id for p in ledger.open_positions()] == [current]
    assert all(b.position_id != old for b in ledger.open_baskets())
    assert ledger.portfolio_defined_risk() == 380
    assert all(b.position_id != old for b in ledger.unreported_baskets(owner))
    assert ledger.reconcile_settlement(owner, NOW.date(), position_id=old) == []
    assert {t:ledger._connection.execute(f'SELECT COUNT(*) FROM {t}').fetchone()[0] for t in before_counts} == before_counts
    assert ledger._connection.execute('SELECT COUNT(*) FROM settlement_reconciliations').fetchone()[0] == 0
    assert ledger._connection.execute('SELECT COUNT(*) FROM outbox').fetchone()[0] == 0
    metadata = json.loads(ledger._connection.execute(
        'SELECT value FROM system_state WHERE key=?', ('archived_position:'+old,)).fetchone()[0])
    assert metadata['reason'] == 'user_retired_legacy_position_unverified'
    assert Path(metadata['archive_path']) == archive
    rows, issues = build_positions(read_packages(Path(ledger.path)), evidence['positions'], {}, as_of=NOW.date())
    assert {r['trade_id'] for r in rows} == {current}
    assert not any('awaiting settlement' in issue for issue in issues)
    assert all(r['position_id'] != old for r in order_activity({'ledger_packages': read_packages(Path(ledger.path))}))
    ledger.close()

    result = classify_trade(bundle['events'], load_ledger_execution_evidence(bundle['ledger']),
                            expected_scope=SCOPE, as_of_date=NOW.date())
    assert result['reconciliation_status'] == 'ARCHIVED' and not result['is_eligible']
    history = load_all_trades(bundle['journal'], partition='all', ledger_path=bundle['ledger'],
                              scope=SCOPE, as_of_date=NOW.date())
    assert history[0]['status'] == 'ARCHIVED' and not history[0]['is_eligible']
    assert bundle['journal'].read_bytes() == original_journal == (archive/'journal-0.jsonl').read_bytes()
    with sqlite3.connect(archive/'ledger.sqlite3') as backup:
        assert backup.execute("SELECT state FROM packages WHERE position_id=?", (old,)).fetchone()[0] == 'open'
    assert apply(setup)['already_archived']


def test_archive_cannot_erase_another_positions_offsetting_ownership(setup):
    bundle, old, _, evidence, archive = setup
    ledger = PositionLedger(bundle['ledger'], scope=SCOPE)
    basket = ledger.position_baskets(old)[0]
    legs = [LedgerLeg('other:'+str(i), l['asset_key'], -l['filled_signed_quantity']) for i,l in enumerate(basket.legs)]
    ledger.record_submission('other', 'another-owner', 'open', legs)
    for leg in legs:
        ledger.record_fill(leg.order_id, leg.requested_signed_quantity, 1)
    ledger.close()
    with pytest.raises(ValueError, match='Another retained position'):
        apply(setup)
    assert not archive.exists()


@pytest.mark.parametrize('damage', ['held', 'working', 'not_expired', 'stale', 'scope', 'incomplete', 'jobs'])
def test_archive_refuses_incomplete_or_unsafe_evidence_without_mutations(setup, damage):
    bundle, old, current, evidence, archive = setup
    ledger = PositionLedger(bundle['ledger'], scope=SCOPE)
    old_contract = ledger.position_baskets(old)[0].legs[0]['asset_key']
    if damage == 'held':
        evidence['positions'].append({'symbol': contract_symbol(old_contract), 'quantity': 1})
    elif damage == 'working':
        evidence['orders'] = [{'id': 'active', 'status': 'open', 'option_symbol': contract_symbol(old_contract)}]
    elif damage == 'not_expired':
        old = current
    elif damage == 'stale':
        evidence['acquired_at'] = (NOW-timedelta(minutes=3)).isoformat()
    elif damage == 'scope':
        evidence['scope'] = {**evidence['scope'], 'account_id': 'OTHER'}
    elif damage == 'incomplete':
        evidence['positions_complete'] = False
    elif damage == 'jobs':
        ledger.enqueue_recovery_job('position', old)
    ledger.close()
    before = bundle['ledger'].read_bytes()
    with pytest.raises(ValueError):
        archive_positions(bundle['ledger'], [old], evidence, archive, now=NOW, writers_stopped=True)
    assert bundle['ledger'].read_bytes() == before
    assert not archive.exists()


def test_archive_is_atomic_for_multiple_positions_and_requires_stopped_writers(setup):
    bundle, old, current, evidence, archive = setup
    before = bundle['ledger'].read_bytes()
    with pytest.raises(ValueError, match='Stop all record writers'):
        archive_positions(bundle['ledger'], [old], evidence, archive, now=NOW)
    with pytest.raises(ValueError, match='wholly expired'):
        archive_positions(bundle['ledger'], [old, current], evidence, archive, now=NOW, writers_stopped=True)
    assert bundle['ledger'].read_bytes() == before
    assert not archive.exists()


def test_archived_position_cannot_be_resurrected_by_callbacks_or_submissions(setup):
    bundle, old, _, _, _ = setup
    ledger = PositionLedger(bundle['ledger'], scope=SCOPE)
    opening = ledger.position_baskets(old)[0]
    legs = [LedgerLeg('unfilled:'+str(i), l['asset_key'], -l['filled_signed_quantity']) for i,l in enumerate(opening.legs)]
    ledger.record_submission('unfilled', opening.strategy_id, 'close', legs, position_id=old)
    ledger.close()

    apply(setup)
    ledger = PositionLedger(bundle['ledger'], scope=SCOPE)
    leg = opening.legs[0]
    assert ledger.record_fill(leg['order_id'], leg['filled_signed_quantity'], leg['fill_price']).state == 'archived'
    with pytest.raises(ValueError, match='archived position'):
        ledger.record_fill(legs[0].order_id, legs[0].requested_signed_quantity, .5)
    ledger.record_terminal_status('unfilled', 'canceled')
    ledger.resolve_partial_package('unfilled', 'canceled')
    assert ledger.get_basket('unfilled').state == 'archived'
    with pytest.raises(ValueError, match='Archived positions'):
        ledger.record_submission('new-close', opening.strategy_id, 'close', legs, position_id=old)
    assert ledger.position_quantities(old) == {}
    ledger.close()


def test_archive_write_failure_rolls_back_every_package(setup):
    bundle, old, _, _, _ = setup
    ledger = PositionLedger(bundle['ledger'], scope=SCOPE)
    basket = ledger.position_baskets(old)[0]
    legs = [LedgerLeg('unfilled:'+str(i), l['asset_key'], -l['filled_signed_quantity']) for i,l in enumerate(basket.legs)]
    ledger.record_submission('unfilled', basket.strategy_id, 'close', legs, position_id=old)
    ledger._connection.executescript("""CREATE TRIGGER fail_archive BEFORE UPDATE ON packages
        WHEN NEW.state='archived' AND NEW.package_id='unfilled'
        BEGIN SELECT RAISE(ABORT,'injected archive failure'); END;""")
    ledger.close()
    before = bundle['ledger'].read_bytes()
    with pytest.raises(sqlite3.IntegrityError, match='injected archive failure'):
        apply(setup)
    assert bundle['ledger'].read_bytes() == before
