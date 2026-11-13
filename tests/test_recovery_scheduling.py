"""Pending position failures must not starve other jobs or account liquidation."""
from strategies.position_ledger import PositionLedger


def test_retrying_one_position_does_not_starve_another(tmp_path):
    ledger = PositionLedger(tmp_path / "ledger.sqlite3")
    first = ledger.enqueue_recovery_job("position", "first")
    second = ledger.enqueue_recovery_job("position", "second")
    assert ledger.claim_next_recovery_job("worker")["job_id"] == first
    ledger.update_recovery_job(first, "pending", last_error="provider unavailable")
    assert ledger.claim_next_recovery_job("worker")["job_id"] == second
    ledger.close()


def test_account_liquidation_has_priority_over_position_retries(tmp_path):
    ledger = PositionLedger(tmp_path / "ledger.sqlite3")
    ledger.enqueue_recovery_job("position", "first")
    account = ledger.enqueue_recovery_job("account")
    assert ledger.claim_next_recovery_job("worker")["job_id"] == account
    ledger.close()
