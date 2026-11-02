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

    strategy = SPXIronCondor()
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
