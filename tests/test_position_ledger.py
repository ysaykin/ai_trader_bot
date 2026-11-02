"""Read-only ledger behavior."""
import sqlite3
from pathlib import Path

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


def _gate_worker(cwd, ledger_path, ready, entered, release):
    import os

    os.chdir(cwd)
    os.environ["POSITION_LEDGER_PATH"] = ledger_path
    from strategies.position_ledger import resolve_ledger_path

    ledger = PositionLedger(resolve_ledger_path())
    assert ledger.is_panic_halted()
    ready.set()
    with ledger.execution_gate():
        entered.set()
        release.wait(5)
    ledger.close()


def test_runtime_paths_and_gate_shared_across_working_directories(tmp_path, monkeypatch):
    import multiprocessing
    import os

    from strategies.position_ledger import resolve_ledger_path

    relative = os.path.relpath(tmp_path / "ledger.sqlite", Path(__file__).resolve().parents[1])
    monkeypatch.setenv("POSITION_LEDGER_PATH", relative)
    assert resolve_ledger_path() == tmp_path / "ledger.sqlite"
    ledger = PositionLedger(resolve_ledger_path())
    ledger.set_panic_halt()
    ctx = multiprocessing.get_context("spawn")
    ready, entered, release = ctx.Event(), ctx.Event(), ctx.Event()
    process = ctx.Process(target=_gate_worker, args=(str(tmp_path), relative, ready, entered, release))
    try:
        with ledger.execution_gate():
            process.start()
            assert ready.wait(5)
            assert not entered.wait(0.3)
        assert entered.wait(5)
        other = PositionLedger(resolve_ledger_path())
        try:
            assert other.is_panic_halted()
        finally:
            other.close()
    finally:
        release.set()
        process.join(5)
        if process.is_alive():
            process.terminate()
            process.join()
        ledger.close()
    assert process.exitcode == 0


def test_runtime_resolver_rejects_memory(monkeypatch):
    from strategies.position_ledger import resolve_ledger_path

    monkeypatch.setenv("POSITION_LEDGER_PATH", ":memory:")
    with pytest.raises(ValueError):
        resolve_ledger_path()


def test_terminal_unbalanced_partial_keeps_confirmed_ownership():
    from strategies.position_ledger import LedgerLeg

    ledger = PositionLedger()
    try:
        ledger.record_submission("p", "s", "open", [
            LedgerLeg("a", "a", 2), LedgerLeg("b", "b", -2),
            LedgerLeg("c", "c", 2), LedgerLeg("d", "d", -2),
        ])
        ledger.record_fill("a", 1, 1.0)
        ledger.record_fill("b", -1, 2.0)
        result = ledger.resolve_partial_package("p", "canceled")
        assert result.state == "partial"
        assert [leg["filled_signed_quantity"] for leg in result.legs] == [1, -1, 0, 0]
    finally:
        ledger.close()


def test_terminal_partial_package_retires_on_full_closure_and_restores_mismatch_detection():
    from strategies.position_ledger import LedgerLeg

    ledger = PositionLedger()
    try:
        # 1. Entry order for 2 contracts
        ledger.record_submission("pkg_entry", "strat1", "open", [
            LedgerLeg("e1", "legA", 2),
            LedgerLeg("e2", "legB", -2),
        ])
        # 2. Partially fill 1 contract
        ledger.record_fill("e1", 1, 1.0, cumulative=True)
        ledger.record_fill("e2", -1, 1.0, cumulative=True)
        assert ledger.owned_quantities("strat1") == {"legA": 1, "legB": -1}

        # 3. Residual is canceled at broker
        resolved = ledger.resolve_partial_package("pkg_entry", "canceled")
        assert resolved.terminal_status == "canceled"
        assert not ledger.is_inflight(resolved)

        # 4. Submit closing order for the 1 confirmed contract
        ledger.record_submission("pkg_close", "strat1", "close", [
            LedgerLeg("c1", "legA", -1),
            LedgerLeg("c2", "legB", 1),
        ])
        # 5. Close fills completely
        ledger.record_fill("c1", -1, 0.5, cumulative=True)
        ledger.record_fill("c2", 1, 0.5, cumulative=True)

        # 6. Ownership is now empty
        assert ledger.owned_quantities("strat1") == {}
        # 7. Both packages must be retired to 'closed'
        assert ledger.get_basket("pkg_entry").state == "closed"
        assert ledger.get_basket("pkg_close").state == "closed"
        # 8. open_baskets is empty and inflight is False
        assert ledger.open_baskets("strat1") == ()
        inflight = any(ledger.is_inflight(b) for b in ledger.open_baskets())
        assert inflight is False

        # 9. Verify quantity mismatch detection is unsuppressed
        # If broker still holds contracts, not inflight detects the mismatch
        broker_qty = 1
        ledger_qty = ledger.all_owned_quantities().get("legA", 0)
        mismatch = not inflight and broker_qty != ledger_qty
        assert mismatch is True
    finally:
        ledger.close()
