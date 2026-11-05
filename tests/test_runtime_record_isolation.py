"""Comprehensive tests for process-local path isolation, RecordScope, sticky guards, and subprocess propagation."""
import os
import socket
import sqlite3
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch
import pytest

from strategies.position_ledger import resolve_ledger_path
from strategies.runtime_records import (
    RecordScope,
    is_protected_runtime_path,
    is_test_mode_active,
    resolve_record_path,
    test_record_root as get_test_record_root,
    validate_append_event,
)

ROOT = Path(__file__).resolve().parent.parent


def test_defaults_survive_environment_clearing() -> None:
    root = get_test_record_root()
    assert root is not None
    with patch.dict("os.environ", {}, clear=True):
        assert resolve_record_path("TRADE_LOG_PATH", "logs/trades.jsonl") == root / "logs/trades.jsonl"
        assert resolve_ledger_path() == root / "logs/position_ledger.sqlite3"


def test_explicit_path_takes_precedence_outside_protected(tmp_path: Path) -> None:
    custom = tmp_path / "journal"
    with patch.dict("os.environ", {"TRADE_LOG_PATH": str(custom)}, clear=True):
        assert resolve_record_path("TRADE_LOG_PATH", "logs/trades.jsonl") == custom


def test_explicit_protected_runtime_path_rejected_in_test_mode() -> None:
    prod_path = ROOT / "logs/trades.jsonl"
    with patch.dict("os.environ", {"TRADE_LOG_PATH": str(prod_path)}, clear=True):
        with pytest.raises(PermissionError, match="Explicit runtime path to protected production record"):
            resolve_record_path("TRADE_LOG_PATH", "logs/trades.jsonl")

    prod_ledger = ROOT / "logs/position_ledger.sqlite3"
    with patch.dict("os.environ", {"POSITION_LEDGER_PATH": str(prod_ledger)}, clear=True):
        with pytest.raises(PermissionError, match="Explicit runtime path to protected production"):
            resolve_ledger_path(str(prod_ledger))


def test_record_scope_validation() -> None:
    scope = RecordScope(environment="paper", broker="tradier", account_id="VA123456")
    assert scope.environment == "paper"
    assert scope.scope_key == "tradier_paper_VA123456"

    # Case normalization
    scope_upper = RecordScope(environment="LIVE", broker="Tradier", account_id="VA999")
    assert scope_upper.environment == "live"
    assert scope_upper.broker == "tradier"

    # Invalid environment
    with pytest.raises(ValueError, match="Invalid RecordScope environment"):
        RecordScope(environment="invalid_env")


def test_rejection_of_cross_scope_appends(tmp_path: Path) -> None:
    journal = tmp_path / "scoped_trades.jsonl"
    paper_scope = RecordScope(environment="paper", broker="tradier", account_id="VA_PAPER")
    live_scope = RecordScope(environment="live", broker="tradier", account_id="VA_LIVE")

    event_paper = {"event": "ENTRY", "trade_id": "t1", "quantity": 1}
    assert validate_append_event(journal, event_paper, scope=paper_scope) is True

    # Write first event
    with open(journal, "a", encoding="utf-8") as f:
        import json
        f.write(json.dumps(event_paper) + "\n")

    # Appending from different scope must be rejected
    event_live = {"event": "ENTRY", "trade_id": "t2", "quantity": 1}
    with pytest.raises(ValueError, match="Cross-scope append rejected"):
        validate_append_event(journal, event_live, scope=live_scope)


def test_rejection_of_conflicting_duplicate_events(tmp_path: Path) -> None:
    journal = tmp_path / "dup_trades.jsonl"
    scope = RecordScope(environment="test", broker="mock", account_id="test_acc")

    event1 = {"event": "ENTRY", "trade_id": "t1", "quantity": 1, "timestamp": "2026-11-04T12:00:00"}
    assert validate_append_event(journal, event1, event_id="EV_001", scope=scope) is True

    with open(journal, "a", encoding="utf-8") as f:
        import json
        f.write(json.dumps(event1) + "\n")

    # Identical duplicate event_id is skipped cleanly (returns False)
    event_dup = dict(event1)
    assert validate_append_event(journal, event_dup, event_id="EV_001", scope=scope) is False

    # Conflicting duplicate (different quantity) must be rejected
    event_conflict = dict(event1)
    event_conflict["quantity"] = 5
    with pytest.raises(ValueError, match="Conflicting duplicate event rejected"):
        validate_append_event(journal, event_conflict, event_id="EV_001", scope=scope)


def test_sticky_write_guard_blocks_production_files(tmp_path: Path) -> None:
    prod_journal = ROOT / "logs/trades.jsonl"
    with pytest.raises(PermissionError, match="Test write guard blocked write access"):
        open(prod_journal, "a")

    # Symlink protection
    symlink_path = tmp_path / "symlink_to_prod.jsonl"
    try:
        symlink_path.symlink_to(prod_journal)
        with pytest.raises(PermissionError, match="Test write guard blocked write access"):
            open(symlink_path, "w")
    finally:
        if symlink_path.is_symlink() or symlink_path.exists():
            symlink_path.unlink()


def test_sqlite_write_guard_allows_readonly_blocks_write() -> None:
    prod_ledger = ROOT / "logs/position_ledger.sqlite3"
    # Write connection must be blocked
    with pytest.raises(PermissionError, match="Test write guard blocked non-read-only connection"):
        sqlite3.connect(str(prod_ledger))

    # Read-only URI connection must succeed
    uri = f"{prod_ledger.resolve().as_uri()}?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    try:
        assert conn.execute("SELECT 1").fetchone() == (1,)
    finally:
        conn.close()


def test_network_guard_blocks_external_brokers() -> None:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        # Connecting to external broker host must fail closed
        with pytest.raises(RuntimeError, match="Test network guard blocked external network connection"):
            s.connect(("api.tradier.com", 443))
    finally:
        s.close()


def test_subprocess_propagation_survives_environment_clearing() -> None:
    root = get_test_record_root()
    assert root is not None

    # Child Python process launched with completely empty environment
    cmd = [
        sys.executable,
        "-c",
        (
            "import os, sys\n"
            "from pathlib import Path\n"
            "sys.path.insert(0, '" + str(ROOT) + "')\n"
            "from strategies.runtime_records import resolve_record_path, is_test_mode_active\n"
            "assert is_test_mode_active(), 'Test mode must be active in child process'\n"
            "p = resolve_record_path('TRADE_LOG_PATH', 'logs/trades.jsonl')\n"
            "assert '" + str(root) + "' in str(p), f'Expected test root in {p}'\n"
            "print('SUCCESS')\n"
        ),
    ]
    res = subprocess.run(cmd, env={}, capture_output=True, text=True)
    assert res.returncode == 0, f"Child process failed: {res.stderr}\nStdout: {res.stdout}"
    assert "SUCCESS" in res.stdout


import unittest
import tempfile


class TestRuntimeRecordIsolationUnittest(unittest.TestCase):
    def test_unittest_isolation_and_scope(self) -> None:
        root = get_test_record_root()
        self.assertIsNotNone(root)
        with patch.dict("os.environ", {}, clear=True):
            self.assertEqual(resolve_record_path("TRADE_LOG_PATH", "logs/trades.jsonl"), root / "logs/trades.jsonl")
            self.assertEqual(resolve_ledger_path(), root / "logs/position_ledger.sqlite3")

    def test_unittest_protected_record_write_blocked(self) -> None:
        prod_journal = ROOT / "logs/trades.jsonl"
        with self.assertRaises(PermissionError):
            open(prod_journal, "a")

    def test_unittest_cross_scope_rejection(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            journal = Path(td) / "scoped.jsonl"
            scope_a = RecordScope(environment="paper", broker="tradier", account_id="acc1")
            scope_b = RecordScope(environment="live", broker="tradier", account_id="acc2")
            self.assertTrue(validate_append_event(journal, {"event": "ENTRY", "trade_id": "t1"}, scope=scope_a))
            with open(journal, "a") as f:
                import json
                f.write(json.dumps({"event": "ENTRY", "trade_id": "t1", "scope": scope_a.to_dict()}) + "\n")
            with self.assertRaises(ValueError):
                validate_append_event(journal, {"event": "ENTRY", "trade_id": "t2"}, scope=scope_b)



def test_sqlite_uri_and_filesystem_mutations_are_guarded(tmp_path: Path) -> None:
    import sqlite3

    protected = ROOT / "logs" / "trades.jsonl"
    with pytest.raises(PermissionError):
        sqlite3.connect(protected.as_uri() + "?mode=rwc", uri=True)
    with pytest.raises(PermissionError):
        os.replace(tmp_path / "missing", protected)
    with pytest.raises(PermissionError):
        os.unlink(protected)


def test_connect_ex_blocks_external_hosts() -> None:
    import socket

    with socket.socket() as client:
        with pytest.raises(RuntimeError, match="network guard"):
            client.connect_ex(("192.0.2.1", 443))
