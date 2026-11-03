"""Dedicated Panic Liquidation Recovery Supervisor and Coordinator.

Provides a standalone recovery worker with an explicit lifecycle (startup/heartbeat/
graceful shutdown), durable recovery jobs, worker exclusion, readiness checks, and
position-scoped recovery. Runs independently of strategy iteration cadence.
"""
from __future__ import annotations

import logging
import fcntl
import os
import signal
import sys
import threading
import time
import uuid
from typing import Any

from strategies.position_ledger import PositionLedger, resolve_ledger_path

logger = logging.getLogger(__name__)


class RecoveryCoordinator:
    """Coordinates durable recovery jobs independently of trading execution."""

    def __init__(
        self,
        ledger: PositionLedger | None = None,
        worker_id: str | None = None,
        poll_interval: float = 1.0,
    ) -> None:
        self.ledger = ledger or PositionLedger()
        self.worker_id = worker_id or f"worker_{uuid.uuid4().hex[:8]}"
        self.poll_interval = max(0.1, poll_interval)
        self._stop_event = threading.Event()
        self._worker_thread: threading.Thread | None = None
        self._running = False
        self._is_active_worker = False

    def is_ready(self) -> bool:
        """Readiness probe: verify ledger connectivity and worker state."""
        try:
            self.ledger._connection.execute("SELECT 1").fetchone()
            return True
        except Exception as exc:
            logger.warning("Recovery readiness probe failed: %s", exc)
            return False

    def heartbeat(self) -> None:
        """Send worker heartbeat to maintain lease."""
        try:
            self.ledger.update_worker_heartbeat(self.worker_id)
        except Exception as exc:
            logger.warning("Recovery worker heartbeat failed: %s", exc)

    def start(self, daemon: bool = True) -> None:
        """Start the recovery worker in a background thread."""
        if self._running:
            return
        self._stop_event.clear()
        self._running = True
        self._worker_thread = threading.Thread(
            target=self.run_loop, kwargs={"poll_interval": self.poll_interval}, daemon=daemon
        )
        self._worker_thread.start()
        logger.info("Recovery coordinator started (worker_id=%s).", self.worker_id)

    def stop(self, timeout: float = 5.0) -> None:
        """Graceful, interruptible shutdown releasing worker exclusion."""
        if not self._running:
            return
        self._stop_event.set()
        if self._worker_thread and self._worker_thread.is_alive():
            self._worker_thread.join(timeout=timeout)
        self._running = False
        self._is_active_worker = False
        logger.info("Recovery coordinator stopped (worker_id=%s).", self.worker_id)

    def run_step(self) -> bool:
        """Process one pending recovery job or panic halt check. Returns True if work was done."""
        try:
            # 1. If system is panic halted, ensure durable account recovery job exists
            if self.ledger.is_panic_halted():
                self.ledger.enqueue_recovery_job(scope="account")

            # 2. Claim next available job
            job = self.ledger.claim_next_recovery_job(self.worker_id)
            if not job:
                return False

            job_id = job["job_id"]
            scope = job.get("scope", "account")
            target_id = job.get("target_id")

            logger.info("Executing recovery job %s (scope=%s, target_id=%s)", job_id, scope, target_id)

            if scope == "account":
                # Account-wide liquidation
                from dashboard.broker_client import get_broker_client
                client = get_broker_client()
                if client is None:
                    self.ledger.update_recovery_job(
                        job_id, status="pending", last_error="No broker client configured"
                    )
                    return True
                with self.ledger.execution_gate():
                    success = client.emergency_flatten_positions()
                if success:
                    self.ledger.update_recovery_job(job_id, status="completed")
                    logger.info("Account-wide recovery job %s completed successfully.", job_id)
                else:
                    failures = getattr(client, "flatten_failures", {})
                    self.ledger.update_recovery_job(
                        job_id, status="pending", last_error=str(failures)
                    )
                    logger.warning("Account-wide recovery job %s attempt failed: %s", job_id, failures)
                return True

            elif scope == "position":
                # Position-scoped recovery
                from dashboard.broker_client import get_broker_client
                client = get_broker_client()
                if client is None:
                    self.ledger.update_recovery_job(
                        job_id, status="pending", last_error="No broker client configured"
                    )
                    return True

                # Do not acknowledge a position until its owned fills have been
                # reconciled. A broker-wide flatten would affect other owners.
                self.ledger.update_recovery_job(
                    job_id, status="pending", last_error="Scoped fill reconciliation is not implemented"
                )
                return True

            return False
        except Exception as exc:
            logger.error("Error in recovery run_step: %s", exc)
            return False

    def run_loop(self, poll_interval: float = 1.0, once: bool = False) -> None:
        """Main recovery worker loop with worker exclusion and heartbeat."""
        # The OS lock remains held during slow broker calls, when a heartbeat
        # lease alone could expire and admit a second worker.
        lock_file = None
        if self.ledger.path != ":memory:":
            lock_file = open(self.ledger.path + ".recovery.lock", "a+")
            try:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                lock_file.close()
                return
        try:
            self._run_locked_loop(poll_interval, once)
        finally:
            if lock_file:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
                lock_file.close()

    def _run_locked_loop(self, poll_interval: float, once: bool) -> None:
        while not self._stop_event.is_set():
            # Acquire or verify worker exclusion lease
            if not self._is_active_worker:
                acquired = self.ledger.acquire_worker_exclusion(self.worker_id)
                if not acquired:
                    # Another worker is actively holding the lease
                    if once:
                        return
                    self._stop_event.wait(poll_interval)
                    continue
                self._is_active_worker = True

            self.heartbeat()
            self.run_step()

            if once:
                break
            self._stop_event.wait(poll_interval)

        # Release worker lease upon loop exit
        try:
            self.ledger.release_worker(self.worker_id)
        except Exception:
            pass


def run_recovery_step(ledger: PositionLedger | None = None) -> bool:
    """Execute one non-blocking recovery step."""
    coordinator = RecoveryCoordinator(ledger=ledger)
    return coordinator.run_step()
