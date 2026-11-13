"""Exercise durable residual recovery through broker submission and fill replay."""
from datetime import datetime, timedelta, timezone
from unittest.mock import Mock, patch
import json
import pytest
import requests

from strategies.position_ledger import PositionLedger
from strategies.recovery import RecoveryCoordinator
from test_concurrent_positions import ledger_legs, option_legs, open_position


def test_recovery_readiness_requires_a_live_worker(tmp_path):
    ledger = PositionLedger(tmp_path / "ledger.sqlite3")
    worker = RecoveryCoordinator(ledger=ledger)
    assert worker.is_ready() is False
    assert ledger.acquire_worker_exclusion(worker.worker_id)
    assert worker.is_ready() is True
    old = (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat()
    ledger._connection.execute("UPDATE recovery_worker_heartbeat SET last_heartbeat=?", (old,))
    ledger._connection.commit()
    assert worker.is_ready() is False
    ledger.close()


@pytest.mark.parametrize('interruption', [SystemExit, RuntimeError])
def test_foreground_interruption_releases_lease_for_immediate_restart(tmp_path, interruption):
    ledger = PositionLedger(tmp_path / 'ledger.sqlite3')
    worker = RecoveryCoordinator(ledger=ledger, worker_id='old')
    with patch.object(worker, 'run_step', side_effect=interruption('interrupted')):
        with pytest.raises(interruption):
            worker.run_loop(once=True)
    active = ledger._connection.execute(
        'SELECT is_active FROM recovery_worker_heartbeat WHERE worker_id=?', ('old',)).fetchone()[0]
    assert active == 0
    assert not worker._running and not worker._is_active_worker
    replacement = RecoveryCoordinator(ledger=ledger, worker_id='new')
    with patch.object(replacement, 'run_step') as step:
        replacement.run_loop(once=True)
    step.assert_called_once()
    ledger.close()


def test_foreground_running_state_clears_after_each_once_run(tmp_path):
    ledger = PositionLedger(tmp_path / 'ledger.sqlite3')
    worker = RecoveryCoordinator(ledger=ledger)
    observed = []
    with patch.object(worker, 'run_step', side_effect=lambda: observed.append(worker._running)):
        worker.run_loop(once=True)
        worker.run_loop(once=True)
    assert observed == [True, True]
    assert not worker._running and not worker._is_active_worker
    ledger.close()


def test_new_worker_retires_stale_active_markers(tmp_path):
    ledger = PositionLedger(tmp_path / 'ledger.sqlite3')
    assert ledger.acquire_worker_exclusion('stale')
    old = (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat()
    ledger._connection.execute('UPDATE recovery_worker_heartbeat SET last_heartbeat=?', (old,))
    ledger._connection.commit()
    assert ledger.acquire_worker_exclusion('current')
    active = [r[0] for r in ledger._connection.execute(
        'SELECT worker_id FROM recovery_worker_heartbeat WHERE is_active=1')]
    assert active == ['current']
    ledger.close()


def test_excluded_worker_cannot_release_the_existing_owner(tmp_path):
    import fcntl
    ledger = PositionLedger(tmp_path / 'ledger.sqlite3')
    assert ledger.acquire_worker_exclusion('shared-id')
    with open(ledger.path + '.recovery.lock', 'a+') as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        other = RecoveryCoordinator(ledger=ledger, worker_id='shared-id')
        with patch.object(other, 'run_step') as step:
            other.run_loop(once=True)
        step.assert_not_called()
        active = ledger._connection.execute(
            'SELECT is_active FROM recovery_worker_heartbeat WHERE worker_id=?', ('shared-id',)).fetchone()[0]
        assert active == 1
    ledger.release_worker('shared-id')
    ledger.close()


@pytest.mark.parametrize("with_protection", [False, True])
@pytest.mark.parametrize("shared_contracts", [False, True])
@pytest.mark.parametrize("lost_response", [False, True])
def test_scoped_recovery_records_fill_and_preserves_other_owner(tmp_path, monkeypatch, with_protection, shared_contracts, lost_response):
    journal = tmp_path / 'trades.jsonl'
    monkeypatch.setenv('TRADE_LOG_PATH', str(journal))
    ledger = PositionLedger(tmp_path / "ledger.sqlite3")
    legs = ledger_legs("target", option_legs())
    ledger.record_submission("target", "strategy", "open", legs, trade_id="target-trade")
    ledger.record_fill(legs[1].order_id, -1, .7)
    if with_protection:
        ledger.record_fill(legs[0].order_id, 1, .1)
    ledger.resolve_partial_package("target", "canceled")
    peer = open_position(ledger, "peer", strikes=(90, 95, 110, 115) if shared_contracts else (80, 85, 120, 125), owner="peer")
    expected = ledger.position_quantities(peer)
    job = ledger.enqueue_recovery_job("position", "target")
    from dashboard.positions import Contract
    actual = {Contract.from_key(k).symbol: q for k, q in ledger.all_owned_quantities().items()}
    client = Mock(account=ledger.record_scope.account_id, sandbox=True)
    orders = []
    client._fetch_all_orders.side_effect = lambda: orders.copy()
    client._fetch_all_positions.side_effect = lambda: [dict(symbol=k, quantity=q) for k, q in actual.items() if q]

    def get(url, **kwargs):
        response = Mock(status_code=200)
        if "/markets/quotes" in url:
            stamp = int(datetime.now(timezone.utc).timestamp() * 1000)
            response.json.return_value = {"quotes": {"quote": {"symbol": kwargs["params"]["symbols"], "bid": .4, "ask": .5,
                                                              "bid_date": stamp, "ask_date": stamp}}}
        else:
            response.json.return_value = {"order": orders[-1]}
        return response

    def submit(method, url, **kwargs):
        assert method == "post"
        data = kwargs["data"]
        assert data["quantity"] == "1"
        assert data["side"] == ("sell_to_close" if orders else "buy_to_close")
        assert data["type"] == "limit"
        order = {**data, "id": f"recovery-{len(orders) + 1}", "status": "filled", "exec_quantity": 1,
                 "avg_fill_price": .5}
        orders.append(order)
        actual[data["option_symbol"]] += 1 if data["side"] == "buy_to_close" else -1
        if lost_response and len(orders) == 1:
            raise requests.Timeout("response lost after acceptance")
        return Mock(status_code=200, json=lambda: {"order": {"id": order["id"], "status": "ok"}})

    client.base_url = "https://sandbox.tradier.com/v1"
    client._get.side_effect = get
    client._submit_once.side_effect = submit
    worker = RecoveryCoordinator(ledger=ledger)
    with patch("dashboard.broker_client.get_broker_client", return_value=client):
        for step in range(3):
            worker.run_step()
            if step == 0:
                ledger.close()
                ledger = PositionLedger(tmp_path / "ledger.sqlite3")
                worker = RecoveryCoordinator(ledger=ledger)
    assert ledger.list_recovery_jobs()[0]["job_id"] == job
    assert ledger.list_recovery_jobs()[0]["status"] == "completed"
    assert ledger.position_quantities("target") == {}
    assert ledger.position_quantities(peer) == expected
    assert client._submit_once.call_count == (2 if with_protection else 1)
    client.emergency_flatten_positions.assert_not_called()
    retained = ledger._connection.execute("SELECT payload FROM broker_evidence").fetchall()
    assert len(retained) == (2 if with_protection else 1)
    from strategies.runtime_records import _raw_execution_matches
    for row in retained:
        payload = json.loads(row[0])
        assert all(_raw_execution_matches(payload, leg) for leg in payload["legs"])
    events = [json.loads(line) for line in journal.read_text().splitlines()]
    assert [e['event'] for e in events] == (['ADJUSTMENT', 'EXIT'] if with_protection else ['EXIT'])
    assert all(e['trade_id'] == 'target-trade' and e['strategy'] == 'strategy' for e in events)
    assert all(e['fees'] is None and e['net_pnl'] is None and e['fees_complete'] is False for e in events)
    assert round(sum(e['gross_pnl'] for e in events), 2) == (60 if with_protection else 20)
    assert all(e['evidence_refs'] and len(e['legs']) == 1 for e in events)
    # Replay the durable dispatch after a crash immediately after the append.
    ledger._connection.execute("UPDATE outbox SET status='pending'")
    ledger._connection.commit()
    ledger.flush_outbox()
    assert [json.loads(line) for line in journal.read_text().splitlines()] == events
    ledger.close()
