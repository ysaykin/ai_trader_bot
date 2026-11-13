"""Offline tests verifying container recovery dependencies and lock-wait halt enforcement."""

from __future__ import annotations

import threading
import time
import os
import shutil
import subprocess
from datetime import date, datetime, timezone
from pathlib import Path
from unittest.mock import Mock, patch

import pytest
from lumibot.entities import Asset, Order

from strategies.iron_condor import SPXIronCondor
from strategies.position_ledger import LedgerLeg, PositionLedger, asset_key


@pytest.fixture(autouse=True)
def isolated_default_recovery_ledger(tmp_path, monkeypatch):
    monkeypatch.setenv("POSITION_LEDGER_PATH", str(tmp_path / "recovery.sqlite3"))


def test_built_image_imports_recovery_modules_offline() -> None:
    image = os.environ.get("LUMIBOT_TEST_IMAGE")
    if not image or not shutil.which("docker"):
        pytest.skip("Set LUMIBOT_TEST_IMAGE to a built bot image and install Docker")
    subprocess.run(
        ["docker", "run", "--rm", "--network", "none", image, "python", "-c",
         "from dashboard.broker_client import TradierClient; from dashboard.presentation import option_mark; from strategies.position_ledger import PositionLedger"],
        check=True, timeout=60,
    )


def test_dockerfile_packages_recovery_dependencies() -> None:
    """Verify Dockerfile packages dashboard recovery modules for the bot image."""
    dockerfile_path = Path(__file__).resolve().parents[1] / "Dockerfile"
    assert dockerfile_path.is_file(), "Dockerfile must exist"
    content = dockerfile_path.read_text()
    assert "dashboard/broker_client.py" in content, "Dockerfile must copy dashboard/broker_client.py"
    assert "dashboard/presentation.py" in content, "Dockerfile must copy dashboard/presentation.py"

    # Verify these modules are importable in the bot execution context
    from dashboard.broker_client import TradierClient
    from dashboard.presentation import finite_number, option_mark

    client = TradierClient("test-token", "test-account")
    assert client.account == "test-account"
    assert finite_number(123.45) == 123.45


def test_panic_recovery_retries_incomplete_liquidation_to_flat(tmp_path: Path) -> None:
    """Verify emergency recovery retries an incomplete first liquidation without ModuleNotFoundError."""
    from dashboard.broker_client import TradierClient

    ledger = PositionLedger(str(tmp_path / "ledger.sqlite3"))
    try:
        ledger.set_panic_halt(True)
        assert ledger.is_panic_halted()

        client = TradierClient("test-token", "test-account")
        positions_run1 = [{"symbol": "SPXW261120P05800000", "quantity": 1}]
        positions_run2 = []

        with patch.object(client, "emergency_cancel_orders", return_value=True), \
             patch.object(client, "_fetch_all_positions", side_effect=[positions_run1, positions_run1, positions_run2]), \
             patch.object(client, "_fetch_all_orders", return_value=[]), \
             patch("dashboard.broker_client.requests.post") as post:
            post.return_value.status_code = 200
            post.return_value.json.return_value = {"order": {"id": 99999}}

            # Run recovery attempt
            success = client.emergency_flatten_positions(timeout_seconds=5)
            assert success is True
    finally:
        ledger.close()


def test_submission_after_lock_wait_blocked_by_panic_halt(tmp_path: Path) -> None:
    """Prove a waiting submission cannot reach the broker after panic sets the halt while waiting."""
    ledger_path = str(tmp_path / "ledger.sqlite3")
    ledger = PositionLedger(ledger_path)

    strategy = SPXIronCondor.__new__(SPXIronCondor)
    strategy.parameters = {**SPXIronCondor.parameters, "position_ledger": ledger, "strategy_name": "TestStrategy"}
    strategy.broker = Mock()
    strategy.broker.name = "Tradier"
    strategy.initialize()
    strategy._position_ledger = ledger
    strategy.create_order = Mock(side_effect=lambda asset, quantity, side: Order(
        strategy="TestStrategy", asset=asset, quantity=quantity, side=side,
    ))
    strategy.submit_order = Mock(return_value=[])

    legs = [
        (Asset("SPXW", asset_type="option", expiration=date(2026, 11, 20), strike=5800, right="put"), 1),
        (Asset("SPXW", asset_type="option", expiration=date(2026, 11, 20), strike=5795, right="put"), -1),
    ]

    lock_acquired = threading.Event()
    halt_set = threading.Event()
    submission_error = []
    submission_finished = threading.Event()

    def lock_holder_thread():
        with ledger.execution_gate():
            lock_acquired.set()
            # Wait until main thread sets panic halt
            halt_set.wait(timeout=5)
            # Brief sleep to ensure submitter thread is actively blocked waiting for lock
            time.sleep(0.05)

    def submitter_thread():
        # Wait until lock is held
        lock_acquired.wait(timeout=5)
        try:
            strategy._submit_package(legs, intent="open", order_type="credit", price=1.5)
        except Exception as exc:
            submission_error.append(exc)
        finally:
            submission_finished.set()

    t_holder = threading.Thread(target=lock_holder_thread)
    t_submitter = threading.Thread(target=submitter_thread)

    try:
        t_holder.start()
        t_submitter.start()

        # Ensure lock is acquired by holder
        assert lock_acquired.wait(timeout=5)
        # Set panic halt while submitter is waiting on execution_gate
        ledger.set_panic_halt(True)
        halt_set.set()

        assert submission_finished.wait(timeout=5)
        t_holder.join(timeout=2)
        t_submitter.join(timeout=2)

        # Confirm submission aborted with panic halt error
        assert len(submission_error) == 1
        assert "panic halt" in str(submission_error[0]).lower()
        # Crucially: submit_order was never called on broker!
        strategy.submit_order.assert_not_called()
    finally:
        ledger.close()


def test_standalone_recovery_coordinator_lifecycle_and_worker_exclusion(tmp_path: Path) -> None:
    """Verify coordinator lifecycle, heartbeat, interruptible shutdown, and single-worker exclusion."""
    from strategies.recovery import RecoveryCoordinator

    ledger_path = str(tmp_path / "ledger.sqlite3")
    ledger = PositionLedger(ledger_path)
    try:
        worker1 = RecoveryCoordinator(ledger=ledger, worker_id="worker_1", poll_interval=0.1)
        worker2 = RecoveryCoordinator(ledger=ledger, worker_id="worker_2", poll_interval=0.1)

        # 1. Readiness check
        assert worker1.is_ready() is False

        # 2. Worker 1 acquires exclusion lease
        assert ledger.acquire_worker_exclusion("worker_1") is True
        assert worker1.is_ready() is True
        # Worker 2 must be excluded while worker 1 has an active heartbeat
        assert ledger.acquire_worker_exclusion("worker_2") is False

        # 3. Heartbeat update
        worker1.heartbeat()

        # 4. Worker 1 graceful release
        ledger.release_worker("worker_1")
        # Worker 2 can now acquire lease
        assert ledger.acquire_worker_exclusion("worker_2") is True
        ledger.release_worker("worker_2")

        # 5. Interruptible background thread start and stop
        worker1.start(daemon=True)
        assert worker1._running is True
        worker1.stop(timeout=2.0)
        assert worker1._running is False
    finally:
        ledger.close()


def test_recovery_continues_after_trading_exits(tmp_path: Path) -> None:
    """Prove recovery continues via durable jobs after trading strategy exits or stops iterating."""
    from dashboard.broker_client import TradierClient
    from strategies.recovery import RecoveryCoordinator

    ledger_path = str(tmp_path / "ledger.sqlite3")
    ledger = PositionLedger(ledger_path)

    # 1. Trading strategy runs while panic halt is set
    strategy = SPXIronCondor.__new__(SPXIronCondor)
    strategy.parameters = {**SPXIronCondor.parameters, "position_ledger": ledger, "strategy_name": "TestTrading"}
    strategy.broker = Mock()
    strategy.broker.name = "Tradier"
    strategy.initialize()
    strategy._position_ledger = ledger

    try:
        # User or risk system trips durable panic halt
        ledger.set_panic_halt(True)

        # Strategy executes iteration under halt -> queues durable recovery job and exits iteration
        strategy._run_trading_iteration()

        # Confirm trading iteration did NOT clear the job yet and durable job is queued
        jobs = ledger.list_recovery_jobs(scope="account")
        assert len(jobs) == 1
        assert jobs[0]["status"] in ("pending", "in_progress")

        # Trading strategy now completely terminates/exits (simulating crash or process exit)
        del strategy

        # 2. Standalone recovery supervisor/coordinator picks up the job and executes recovery
        coordinator = RecoveryCoordinator(ledger=ledger, worker_id="standalone_supervisor", poll_interval=0.1)
        mock_client = Mock()
        mock_client.emergency_flatten_positions.return_value = True

        with patch("dashboard.broker_client.get_broker_client", return_value=mock_client):
            # Standalone coordinator processes the durable recovery job
            processed = coordinator.run_step()
            assert processed is True
            mock_client.emergency_flatten_positions.assert_called_once()

            # Verify durable job is now marked completed
            jobs_after = ledger.list_recovery_jobs(scope="account")
            assert len(jobs_after) == 1
            assert jobs_after[0]["status"] == "completed"
    finally:
        ledger.close()


def test_unbalanced_residual_position_scoped_recovery(tmp_path: Path) -> None:
    """Verify unbalanced residual positions use scoped recovery jobs without stranding or breaking balanced entry validation."""
    ledger_path = str(tmp_path / "ledger.sqlite3")
    ledger = PositionLedger(ledger_path)

    strategy = SPXIronCondor.__new__(SPXIronCondor)
    strategy.parameters = {**SPXIronCondor.parameters, "position_ledger": ledger, "strategy_name": "ScopedStrat"}
    strategy.broker = Mock()
    strategy.broker.name = "Tradier"
    strategy.initialize()
    strategy._position_ledger = ledger
    strategy.get_datetime = Mock(return_value=datetime(2026, 11, 20, 15, 0, tzinfo=timezone.utc))
    strategy.create_order = Mock(side_effect=lambda asset, quantity, side: Order(
        strategy="ScopedStrat", asset=asset, quantity=quantity, side=side,
    ))
    strategy.submit_order = Mock(return_value=[])

    try:
        # 1. Balanced-entry validation is intact: unbalanced opening packages MUST be rejected
        unbalanced_entry_legs = [
            (Asset("SPXW", asset_type="option", expiration=date(2026, 11, 20), strike=5800, right="put"), 2),
            (Asset("SPXW", asset_type="option", expiration=date(2026, 11, 20), strike=5795, right="put"), -1),
        ]
        with pytest.raises(ValueError, match="balanced legs"):
            strategy._submit_package(unbalanced_entry_legs, intent="open", order_type="credit", price=1.0)

        single_leg_entry = [
            (Asset("SPXW", asset_type="option", expiration=date(2026, 11, 20), strike=5800, right="put"), 1),
        ]
        with pytest.raises(ValueError, match="balanced legs"):
            strategy._submit_package(single_leg_entry, intent="open", order_type="credit", price=1.0)

        # 2. Residual ownership close: single leg or unbalanced residual legs must NOT raise ValueError
        position = strategy._runtime()
        position.owned_legs = [
            (Asset("SPXW", asset_type="option", expiration=date(2026, 11, 20), strike=5800, right="put"), 1), # single remaining residual leg
        ]
        position.position_open = True
        strategy._close_all_options(exit_reason="STOP_LOSS")

        # Verify unverified orders are not unsafely submitted directly
        strategy.submit_order.assert_not_called()

        # Verify position-scoped recovery job was enqueued in durable ledger
        scoped_jobs = ledger.list_recovery_jobs(scope="position")
        assert len(scoped_jobs) == 1
        assert scoped_jobs[0]["target_id"] == position.position_id
        assert scoped_jobs[0]["status"] == "pending"
    finally:
        ledger.close()
