from unittest.mock import patch

from dashboard.broker_client import TradierClient
from strategies.position_ledger import PositionLedger
from strategies.recovery import RecoveryCoordinator


def test_unknown_emergency_submission_blocks_retry(tmp_path, monkeypatch):
    path = str(tmp_path / 'ledger.sqlite3')
    monkeypatch.setenv('POSITION_LEDGER_PATH', path)
    ledger = PositionLedger(path)
    ledger.persist_emergency_intent('unknown', 'ABC', 'sell', 1)
    ledger.close()
    client = TradierClient('token', 'account')
    with patch.object(client, 'emergency_cancel_orders', return_value=True), \
         patch.object(client, '_fetch_all_orders', return_value=[]), \
         patch('dashboard.broker_client.requests.post') as post:
        assert not client.emergency_flatten_positions()
        post.assert_not_called()


def test_scoped_job_is_not_reported_complete(tmp_path):
    ledger = PositionLedger(str(tmp_path / 'ledger.sqlite3'))
    ledger.enqueue_recovery_job('position', 'owner')
    with patch('dashboard.broker_client.get_broker_client', return_value=object()):
        assert RecoveryCoordinator(ledger=ledger).run_step()
    assert ledger.list_recovery_jobs()[0]['status'] == 'pending'
    ledger.close()
