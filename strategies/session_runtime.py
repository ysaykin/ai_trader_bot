"""Bounded session waits and strategy health independent of broker polling."""
from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
import json
import logging
from pathlib import Path
import threading
from types import MethodType

from strategies.order_sessions import EASTERN, session_bounds

log = logging.getLogger(__name__)


class IterationWatchdog:
    """Alert once per stalled strategy, independent of broker telemetry."""

    def __init__(self, strategies, *, clock=None):
        self.strategies = tuple(strategies)
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.started_at = self.clock()
        self._alerted = set()
        self._stop = threading.Event()
        self._thread = None

    def check(self) -> tuple[str, ...]:
        now = self.clock()
        bounds = session_bounds(now.astimezone(EASTERN).date())
        if bounds is None or not bounds[0] <= now < bounds[1]:
            self._alerted.clear()
            return ()
        stalled = []
        for strategy in self.strategies:
            last = getattr(strategy, "_last_iteration_completed_at", None)
            reference = max(bounds[0], self.started_at, last or self.started_at)
            name = str(strategy.name)
            if (now - reference).total_seconds() > 600:
                stalled.append(name)
                if name not in self._alerted:
                    log.critical("TRADING_ITERATION_STALLED strategy=%s last_completed=%s: no completed iteration for ten minutes during market hours; inspect or restart the bot.", name, last)
                    self._alerted.add(name)
            elif name in self._alerted:
                log.info("Trading iterations resumed for %s.", name)
                self._alerted.remove(name)
        return tuple(stalled)

    def start(self) -> None:
        if self._thread is not None:
            return
        def run():
            while not self._stop.wait(30):
                try:
                    self.check()
                except Exception:
                    log.exception("Trading iteration watchdog could not check strategy health")
        self._thread = threading.Thread(target=run, name="iteration-watchdog", daemon=True)
        self._thread.start()

    def close(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2)


def install_session_waits(broker) -> None:
    """Adapt one live broker instance without changing installed SDK source.

    A suspended Docker VM can stop its monotonic clock while wall time advances.
    Recompute the market boundary after each short sleep instead of sleeping a
    whole night. Reconciliation runs on the existing strategy executor thread.
    """
    if getattr(broker, "_session_waits_installed", False):
        return

    def wait(self, minutes, strategy, *, closing):
        while self.should_continue():
            if bool(self.is_market_open()) != closing:
                return
            reconcile = getattr(strategy, "_reconcile_session_orders", None)
            if reconcile is not None:
                reconcile()
            remaining = (self.get_time_to_close() if closing else self.get_time_to_open()) - 60 * (minutes or 0)
            if remaining <= 0:
                return
            self.sleep(min(remaining, 30))

    def await_open(self, timedelta=None, strategy=None):
        return wait(self, timedelta, strategy, closing=False)

    def await_close(self, timedelta=None, strategy=None):
        return wait(self, timedelta, strategy, closing=True)

    broker._await_market_to_open = MethodType(await_open, broker)
    broker._await_market_to_close = MethodType(await_close, broker)
    broker._session_waits_installed = True


def strategy_health(log_path: Path, *, now: datetime | None = None) -> dict:
    """Require strategy iterations during the session, allowing ten minutes to start."""
    now = now or datetime.now(timezone.utc)
    started = iteration = poll = None
    try:
        lines = log_path.read_text(errors="replace").splitlines()
    except OSError as error:
        return {"healthy": False, "state": "unknown", "reason": str(error)}
    for line in lines:
        try:
            stamp = datetime.strptime(line[:19], "%Y-%m-%d %H:%M:%S").replace(tzinfo=EASTERN)
        except ValueError:
            continue
        if "Starting SPX Iron Condor bot" in line:
            started, iteration = stamp, None
        if "Executing the on_trading_iteration lifecycle method" in line:
            iteration = stamp
        if "LUMIBOT_TELEMETRY" in line and "broker_snapshot" in line:
            poll = stamp
    result = {"last_iteration": iteration.isoformat() if iteration else None,
              "last_broker_poll": poll.isoformat() if poll else None}
    bounds = session_bounds(now.astimezone(EASTERN).date())
    if bounds is None or not bounds[0] <= now < bounds[1]:
        return {**result, "healthy": True, "state": "standby", "reason": "Outside regular market session"}
    grace = max(bounds[0], started or bounds[0]) + timedelta(minutes=10)
    recent = iteration is not None and 0 <= (now - iteration).total_seconds() <= 600
    healthy = recent or now < grace
    return {**result, "healthy": healthy, "state": "active" if recent else "starting" if healthy else "stalled",
            "reason": "Recent strategy iteration" if recent else "Startup grace period" if healthy else
                      "No strategy iteration within ten minutes; broker polling does not establish strategy health"}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    from strategies.runtime_records import resolve_record_path
    result = strategy_health(resolve_record_path("TRADING_LOG_PATH", "logs/trading.log"))
    print(json.dumps(result))
    return int(args.check and not result["healthy"])


if __name__ == "__main__":
    raise SystemExit(main())
