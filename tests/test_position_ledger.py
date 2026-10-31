"""Read-only ledger behavior."""
import sqlite3

import pytest

from strategies.position_ledger import PositionLedger


def test_read_only_requires_existing_database(tmp_path):
    path = tmp_path / "missing" / "ledger.sqlite"
    with pytest.raises(sqlite3.OperationalError):
        PositionLedger(path, read_only=True)
    assert not path.parent.exists()


def test_read_only_snapshot_and_mutation(tmp_path):
    path = tmp_path / "ledger.sqlite"
    ledger = PositionLedger(path)
    ledger.close()
    ledger = PositionLedger(path, read_only=True)
    try:
        assert ledger.risk_snapshot() == {"strategy_risk": {}, "portfolio_risk": 0, "owned_quantities": {}}
        with pytest.raises(sqlite3.OperationalError):
            ledger._connection.execute("DELETE FROM packages")
    finally:
        ledger.close()


def test_memory_read_only_initializes_cleanly():
    ledger = PositionLedger(":memory:", read_only=True)
    try:
        assert ledger.risk_snapshot() == {"strategy_risk": {}, "portfolio_risk": 0.0, "owned_quantities": {}}
    finally:
        ledger.close()


def test_uninitialized_file_read_only_risk_snapshot_does_not_raise(tmp_path):
    empty_db = tmp_path / "empty.db"
    # Create empty 0-byte file without tables
    empty_db.touch()
    ledger = PositionLedger(empty_db, read_only=True)
    try:
        assert ledger.risk_snapshot() == {"strategy_risk": {}, "portfolio_risk": 0.0, "owned_quantities": {}}
    finally:
        ledger.close()
