"""Standalone recovery worker entrypoint for panic liquidation and scoped recovery."""
from __future__ import annotations

import argparse
import logging
import signal
import sys

from strategies.position_ledger import PositionLedger, resolve_ledger_path
from strategies.recovery import RecoveryCoordinator

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] (RecoveryWorker) %(message)s",
)
logger = logging.getLogger("recovery_worker")


def main() -> int:
    parser = argparse.ArgumentParser(description="Standalone Lumibot Recovery Worker")
    parser.add_argument("--check-readiness", action="store_true", help="Check readiness and exit")
    parser.add_argument("--once", action="store_true", help="Run one recovery step and exit")
    parser.add_argument("--interval", type=float, default=2.0, help="Polling interval in seconds")
    args = parser.parse_args()

    ledger = PositionLedger(resolve_ledger_path(), read_only=args.check_readiness)
    coordinator = RecoveryCoordinator(ledger=ledger, poll_interval=args.interval)

    if args.check_readiness:
        ready = coordinator.is_ready()
        ledger.close()
        logger.info("Recovery readiness: %s", "READY" if ready else "NOT READY")
        return 0 if ready else 1

    def handle_signal(sig, frame):
        logger.info("Received signal %s; shutting down gracefully...", sig)
        coordinator.stop()
        sys.exit(0)

    signal.signal(signal.SIGINT, handle_signal)
    signal.signal(signal.SIGTERM, handle_signal)

    logger.info("Starting standalone recovery worker loop (interval=%.1fs)...", args.interval)
    try:
        coordinator.run_loop(poll_interval=args.interval, once=args.once)
    finally:
        coordinator.stop()
        ledger.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
