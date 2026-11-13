"""Dedicated Panic Liquidation Recovery Supervisor and Coordinator.

Provides a standalone recovery worker with an explicit lifecycle (startup/heartbeat/
graceful shutdown), durable recovery jobs, worker exclusion, readiness checks, and
position-scoped recovery. Runs independently of strategy iteration cadence.
"""
from __future__ import annotations

import logging
import fcntl
import threading
import uuid
import time
from datetime import datetime, timezone

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
        self.ledger = ledger or PositionLedger(resolve_ledger_path())
        self.worker_id = worker_id or f"worker_{uuid.uuid4().hex[:8]}"
        self.poll_interval = max(0.1, poll_interval)
        self._stop_event = threading.Event()
        self._worker_thread: threading.Thread | None = None
        self._running = False
        self._is_active_worker = False
        self._last_day_reconciliation = float('-inf')

    def reconcile_ended_day_orders(self) -> bool:
        """Release stale reservations even while strategies sleep overnight."""
        from strategies.day_orders import reconcile
        from strategies.order_sessions import day_session_ended
        now = datetime.now(timezone.utc)
        pending = [b for b in self.ledger.open_baskets() if self.ledger.is_inflight(b)
                   and (b.duration or '').lower() == 'day' and day_session_ended(b.created_at, now)]
        if not pending or time.monotonic() - self._last_day_reconciliation < 60:
            return False
        self._last_day_reconciliation = time.monotonic()
        from dashboard.broker_client import get_broker_client
        client = get_broker_client()
        if client is None or not client.sandbox or client.account != self.ledger.record_scope.account_id:
            return False
        with self.ledger.execution_gate():
            orders = client._fetch_all_orders()
            positions = client._fetch_all_positions()
            ids = {str(row['id']) for row in orders}
            for basket in pending:
                if basket.broker_parent_id and basket.broker_parent_id not in ids:
                    response = client._get(
                        f'{client.base_url}/accounts/{client.account}/orders/{basket.broker_parent_id}',
                        headers=client.headers, fresh=True)
                    response.raise_for_status()
                    raw = response.json().get('order')
                    if not isinstance(raw, dict) or str(raw.get('id')) != basket.broker_parent_id:
                        raise ValueError('Historical DAY order confirmation unavailable')
                    orders.append(raw)
            report = reconcile(self.ledger, orders=orders, positions=positions, now=now, apply=True)
        changed = [row for row in report if row['result'] == 'canceled']
        if changed:
            logger.info('Released %d ended DAY reservations without changing fills.', len(changed))
        return bool(changed)

    def is_ready(self) -> bool:
        """Readiness probe: verify ledger connectivity and worker state."""
        try:
            rows = self.ledger._connection.execute(
                "SELECT last_heartbeat FROM recovery_worker_heartbeat WHERE is_active=1"
            ).fetchall()
            now = datetime.now(timezone.utc)
            return any(-5 <= (now - datetime.fromisoformat(row[0])).total_seconds() <= 15
                       for row in rows)
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
        if (self._worker_thread and self._worker_thread.is_alive()
                and self._worker_thread is not threading.current_thread()):
            self._worker_thread.join(timeout=timeout)
        logger.info("Recovery coordinator stop requested (worker_id=%s).", self.worker_id)

    def run_step(self) -> bool:
        """Process one pending recovery job or panic halt check. Returns True if work was done."""
        try:
            self.ledger.flush_outbox()
            passive_work = False
            try:
                passive_work = self.reconcile_ended_day_orders()
            except Exception as exc:
                logger.warning('DAY reconciliation retained reservations: %s', exc)
            # 1. If system is panic halted, ensure durable account recovery job exists
            if self.ledger.is_panic_halted():
                self.ledger.enqueue_recovery_job(scope="account")

            # 2. Claim next available job
            job = self.ledger.claim_next_recovery_job(self.worker_id)
            if not job:
                return passive_work

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

                from strategies.scoped_recovery import recover_position
                try:
                    with self.ledger.execution_gate():
                        complete, detail = recover_position(self.ledger, client, job)
                    self.ledger.flush_outbox()
                    self.ledger.update_recovery_job(
                        job_id, status="completed" if complete else "pending", last_error=detail
                    )
                except Exception as exc:
                    self.ledger.update_recovery_job(job_id, status="pending", last_error=str(exc))
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
        try:
            if self.ledger.path != ":memory:":
                lock_file = open(self.ledger.path + ".recovery.lock", "a+")
                try:
                    fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    lock_file.close()
                    lock_file = None
                    return
            self._running = True
            self._run_locked_loop(poll_interval, once)
        finally:
            try:
                if self._is_active_worker:
                    self.ledger.release_worker(self.worker_id)
            except Exception as exc:
                logger.warning('Could not release recovery worker heartbeat: %s', exc)
            finally:
                self._running = False
                self._is_active_worker = False
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


def run_recovery_step(ledger: PositionLedger | None = None) -> bool:
    """Execute one non-blocking recovery step."""
    coordinator = RecoveryCoordinator(ledger=ledger)
    return coordinator.run_step()
