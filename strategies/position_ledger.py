"""Transactional ownership ledger for multi-leg option packages."""

from __future__ import annotations

import json
import fcntl
from contextlib import contextmanager
import math
import os
import sqlite3
import threading
import uuid
from collections import defaultdict
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

from strategies.iron_condor_math import max_defined_loss

ACTIVE_STATES = frozenset({"submitted", "partial", "open", "closing", "awaiting_settlement"})
TERMINAL_STATES = frozenset({"closed", "canceled", "expired", "failed", "rejected"})


@dataclass(frozen=True)
class LedgerLeg:
    """One requested leg in an atomic broker package."""

    order_id: str
    asset_key: str
    requested_signed_quantity: int
    broker_order_id: str | None = None
    broker_parent_id: str | None = None

    @property
    def local_order_id(self) -> str:
        """Return the immutable application identifier (legacy alias: ``order_id``)."""
        return self.order_id


@dataclass(frozen=True)
class BasketRecord:
    """Ledger view of one submitted package and its legs."""

    package_id: str
    strategy_id: str
    intent: str
    state: str
    opening_cash_flow: float | None
    closing_cash_flow: float | None
    legs: tuple[Mapping[str, Any], ...]
    broker_parent_id: str | None = None
    broker_tag: str | None = None
    duration: str | None = None
    limit_price: float | None = None
    trade_id: str | None = None
    created_at: str | None = None
    updated_at: str | None = None
    replacement_of: str | None = None
    reprice_count: int = 0
    position_id: str | None = None
    terminal_status: str | None = None
    submission_started: int | None = None


@dataclass(frozen=True)
class PositionRecord:
    """One independently managed position, including its pending submissions."""

    position_id: str
    strategy_id: str
    trade_id: str | None
    settings: Mapping[str, Any]
    exit_reason: str | None
    mfe_pnl: float
    mae_pnl: float
    created_at: str


def asset_key(asset: Any) -> str:
    """Return a stable key for a LumiBot option asset or asset-like object."""
    payload = {
        "expiration": str(getattr(asset, "expiration", "")),
        "right": str(getattr(asset, "right", "")).lower(),
        "strike": float(getattr(asset, "strike", 0.0)),
        "symbol": str(getattr(asset, "symbol", "")),
    }
    return json.dumps(payload, separators=(",", ":"), sort_keys=True)


def resolve_ledger_path(path: str | Path | None = None) -> Path:
    """Resolve a runtime ledger path consistently across working directories."""
    from strategies.runtime_records import (
        is_protected_runtime_path,
        is_test_mode_active,
        resolve_record_path,
    )

    value = str(path).strip() if path is not None else os.getenv("POSITION_LEDGER_PATH", "").strip()
    if value == ":memory:":
        raise ValueError("The runtime ledger must be file-backed")

    if path is not None:
        candidate = Path(value).expanduser()
        if not candidate.is_absolute():
            candidate = Path(__file__).resolve().parent.parent / candidate
        resolved = candidate.resolve()
        if is_test_mode_active() and is_protected_runtime_path(resolved):
            raise PermissionError(
                f"Explicit runtime path to protected production ledger '{resolved}' is rejected in test mode."
            )
        return resolved
    return resolve_record_path("POSITION_LEDGER_PATH", "logs/position_ledger.sqlite3")


class PositionLedger:
    """Persist package submissions and confirmed fills in SQLite transactions."""

    def __init__(self, path: str | Path = ":memory:", *, read_only: bool = False) -> None:
        self.path = ":memory:" if str(path) == ":memory:" else str(resolve_ledger_path(path))
        if not read_only and self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        if read_only and self.path != ":memory:":
            database = Path(self.path).as_uri() + "?mode=ro"
            uri_flag = True
        else:
            database = self.path
            uri_flag = False
        self._connection = sqlite3.connect(database, check_same_thread=False, uri=uri_flag)
        self._connection.row_factory = sqlite3.Row
        self._lock = threading.RLock()
        if not read_only:
            self._initialize()

    @contextmanager
    def execution_gate(self):
        """Serialize submissions and emergency liquidation across processes."""
        if self.path == ":memory:":
            with self._lock:
                yield
            return
        lock_path = self.path + ".execution.lock"
        with open(lock_path, "a+") as lock_file:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)

    def close(self) -> None:
        """Close the database connection."""
        with self._lock:
            self._connection.close()

    def risk_snapshot(self) -> dict[str, Any]:
        """Read ownership and defined risk from one SQLite snapshot."""
        with self._lock:
            try:
                self._connection.execute("BEGIN")
                try:
                    tables = {
                        row[0]
                        for row in self._connection.execute(
                            "SELECT name FROM sqlite_master WHERE type='table'"
                        ).fetchall()
                    }
                    if "packages" not in tables:
                        return {"strategy_risk": {}, "portfolio_risk": 0.0, "owned_quantities": {}}
                    owners = [str(row[0]) for row in self._connection.execute(
                        "SELECT DISTINCT strategy_id FROM packages"
                    )]
                    risks = {owner: self.portfolio_defined_risk(owner) if "positions" in tables else None
                             for owner in owners}
                    quantities = {owner: self.owned_quantities(owner) for owner in owners}
                    portfolio = None if any(value is None for value in risks.values()) else sum(risks.values())
                    return {"strategy_risk": risks, "portfolio_risk": portfolio, "owned_quantities": quantities}
                finally:
                    self._connection.rollback()
            except sqlite3.OperationalError:
                return {"strategy_risk": {}, "portfolio_risk": None, "owned_quantities": {}}

    def _initialize(self) -> None:
        """Create or migrate the ledger without replacing existing rows."""
        with self._lock, self._connection:
            self._connection.executescript(
                """
                PRAGMA foreign_keys = ON;
                CREATE TABLE IF NOT EXISTS packages (
                    package_id TEXT PRIMARY KEY,
                    strategy_id TEXT NOT NULL,
                    intent TEXT NOT NULL,
                    state TEXT NOT NULL,
                    opening_cash_flow REAL,
                    closing_cash_flow REAL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS legs (
                    order_id TEXT PRIMARY KEY,
                    package_id TEXT NOT NULL REFERENCES packages(package_id),
                    asset_key TEXT NOT NULL,
                    requested_signed_quantity INTEGER NOT NULL,
                    filled_signed_quantity INTEGER NOT NULL DEFAULT 0,
                    fill_price REAL,
                    status TEXT NOT NULL,
                    UNIQUE(package_id, asset_key)
                );
                CREATE TABLE IF NOT EXISTS system_state (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS positions (
                    position_id TEXT PRIMARY KEY,
                    strategy_id TEXT NOT NULL,
                    trade_id TEXT,
                    settings TEXT NOT NULL DEFAULT '{}',
                    exit_reason TEXT,
                    mfe_pnl REAL NOT NULL DEFAULT 0,
                    mae_pnl REAL NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS journal_events (
                    package_id TEXT NOT NULL REFERENCES packages(package_id),
                    event TEXT NOT NULL,
                    PRIMARY KEY(package_id, event)
                );
                CREATE TABLE IF NOT EXISTS recovery_jobs (
                    job_id TEXT PRIMARY KEY,
                    scope TEXT NOT NULL,
                    target_id TEXT,
                    status TEXT NOT NULL,
                    payload TEXT NOT NULL DEFAULT '{}',
                    attempts INTEGER NOT NULL DEFAULT 0,
                    last_error TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS recovery_worker_heartbeat (
                    worker_id TEXT PRIMARY KEY,
                    hostname TEXT,
                    pid INTEGER,
                    last_heartbeat TEXT NOT NULL,
                    is_active INTEGER NOT NULL DEFAULT 1
                );
                CREATE TABLE IF NOT EXISTS emergency_submissions (
                    intent_id TEXT PRIMARY KEY,
                    symbol TEXT NOT NULL,
                    side TEXT NOT NULL,
                    quantity INTEGER NOT NULL,
                    price REAL,
                    broker_id TEXT,
                    status TEXT NOT NULL,
                    cumulative_fills INTEGER NOT NULL DEFAULT 0,
                    last_error TEXT,
                    payload TEXT NOT NULL DEFAULT '{}',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS settlement_values (
                    expiration TEXT NOT NULL,
                    symbol TEXT NOT NULL DEFAULT 'XSP',
                    value REAL NOT NULL,
                    source_url TEXT NOT NULL,
                    retrieved_at TEXT NOT NULL,
                    response_hash TEXT NOT NULL,
                    category TEXT NOT NULL,
                    PRIMARY KEY (expiration, symbol)
                );
                CREATE TABLE IF NOT EXISTS broker_cash_evidence (
                    activity_id TEXT PRIMARY KEY,
                    account_scope TEXT NOT NULL,
                    expiration TEXT NOT NULL,
                    symbol TEXT NOT NULL DEFAULT 'XSP',
                    cash_cents INTEGER NOT NULL,
                    disposition_confirmed INTEGER NOT NULL DEFAULT 1,
                    description TEXT,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS fee_evidence (
                    identity TEXT PRIMARY KEY,
                    trade_id TEXT,
                    package_id TEXT,
                    position_id TEXT,
                    amount_cents INTEGER,
                    complete INTEGER NOT NULL DEFAULT 1,
                    category TEXT NOT NULL DEFAULT 'execution',
                    provenance TEXT,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS settlement_reconciliations (
                    position_id TEXT PRIMARY KEY,
                    strategy_id TEXT NOT NULL,
                    trade_id TEXT,
                    expiration TEXT NOT NULL,
                    outcome TEXT NOT NULL,
                    settlement_cash_cents INTEGER,
                    gross_cents INTEGER,
                    fees_cents INTEGER,
                    net_cents INTEGER,
                    known_fees_cents INTEGER NOT NULL DEFAULT 0,
                    fees_complete INTEGER NOT NULL DEFAULT 1,
                    reconciled_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS outbox (
                    outbox_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    event_type TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pending',
                    created_at TEXT NOT NULL,
                    dispatched_at TEXT
                );
                """
            )
            self._add_columns(
                "packages",
                {
                    "broker_parent_id": "TEXT",
                    "broker_tag": "TEXT",
                    "duration": "TEXT",
                    "limit_price": "REAL",
                    "trade_id": "TEXT",
                    "replacement_of": "TEXT",
                    "reprice_count": "INTEGER NOT NULL DEFAULT 0",
                    "position_id": "TEXT",
                    "terminal_status": "TEXT",
                    "submission_started": "INTEGER",
                },
            )
            self._add_columns(
                "legs",
                {
                    "local_order_id": "TEXT",
                    "broker_order_id": "TEXT",
                    "broker_parent_id": "TEXT",
                    "side": "TEXT",
                    "created_at": "TEXT",
                    "updated_at": "TEXT",
                },
            )
            self._connection.execute(
                "UPDATE legs SET local_order_id = order_id WHERE local_order_id IS NULL"
            )
            self._connection.execute(
                "UPDATE packages SET duration = 'day' WHERE duration IS NULL"
            )
            self._connection.execute(
                """UPDATE legs SET side = CASE
                       WHEN requested_signed_quantity > 0 AND package_id IN
                            (SELECT package_id FROM packages WHERE intent IN ('open','add'))
                           THEN 'buy_to_open'
                       WHEN requested_signed_quantity < 0 AND package_id IN
                            (SELECT package_id FROM packages WHERE intent IN ('open','add'))
                           THEN 'sell_to_open'
                       WHEN requested_signed_quantity > 0 THEN 'buy_to_close'
                       ELSE 'sell_to_close'
                   END
                   WHERE side IS NULL"""
            )
            self._connection.executescript(
                """
                CREATE INDEX IF NOT EXISTS packages_strategy_state
                    ON packages(strategy_id, state);
                CREATE INDEX IF NOT EXISTS packages_broker_parent
                    ON packages(broker_parent_id);
                CREATE INDEX IF NOT EXISTS packages_broker_tag
                    ON packages(broker_tag);
                CREATE INDEX IF NOT EXISTS packages_position
                    ON packages(position_id);
                CREATE INDEX IF NOT EXISTS legs_broker_order
                    ON legs(broker_order_id);
                CREATE INDEX IF NOT EXISTS legs_broker_parent
                    ON legs(broker_parent_id);
                CREATE INDEX IF NOT EXISTS settlement_values_exp
                    ON settlement_values(expiration);
                CREATE INDEX IF NOT EXISTS broker_cash_exp
                    ON broker_cash_evidence(expiration);
                CREATE INDEX IF NOT EXISTS fee_evidence_trade
                    ON fee_evidence(trade_id);
                CREATE INDEX IF NOT EXISTS fee_evidence_pos
                    ON fee_evidence(position_id);
                CREATE INDEX IF NOT EXISTS outbox_status
                    ON outbox(status);
                """
            )
            self._migrate_position_links()
            self._position_schema_ready = True

    def _add_columns(self, table: str, definitions: Mapping[str, str]) -> None:
        existing = {
            str(row["name"])
            for row in self._connection.execute(f"PRAGMA table_info({table})").fetchall()
        }
        for column, definition in definitions.items():
            if column not in existing:
                self._connection.execute(
                    f"ALTER TABLE {table} ADD COLUMN {column} {definition}"
                )

    def _migrate_position_links(self) -> None:
        """Replay legacy inventory and link only adjustments with a unique owner."""
        inventories: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
        owners: dict[str, str] = {}
        trades: dict[str, str | None] = {}
        links: dict[str, str] = {}
        rows = self._connection.execute(
            "SELECT * FROM packages ORDER BY rowid"
        ).fetchall()
        for row in rows:
            package = self._basket_from_row(row)
            pid = package.position_id
            if pid is None and package.intent == "open":
                pid = package.package_id
            if pid is None and package.replacement_of:
                pid = links.get(package.replacement_of)
            if pid is None:
                candidates = []
                for candidate, inventory in inventories.items():
                    if owners[candidate] != package.strategy_id:
                        continue
                    if package.trade_id and trades[candidate] == package.trade_id:
                        candidates.append(candidate)
                        continue
                    direction = 1 if package.intent == "add" else -1
                    if all(inventory.get(str(leg["asset_key"]), 0)
                           * int(leg["requested_signed_quantity"]) * direction > 0
                           for leg in package.legs):
                        candidates.append(candidate)
                if len(candidates) == 1:
                    pid = candidates[0]
            if pid is None:
                continue
            self._connection.execute(
                "INSERT OR IGNORE INTO positions(position_id,strategy_id,trade_id,created_at) VALUES(?,?,?,?)",
                (pid, package.strategy_id, package.trade_id, package.created_at),
            )
            self._connection.execute(
                "UPDATE packages SET position_id = ? WHERE package_id = ? AND position_id IS NULL",
                (pid, package.package_id),
            )
            if package.position_id is None and package.trade_id and package.intent in {"open", "add"}:
                self._connection.execute(
                    "INSERT OR IGNORE INTO journal_events VALUES(?,?)",
                    (package.package_id, "ENTRY" if package.intent == "open" else "ADDITION"),
                )
            links[package.package_id] = pid
            owners[pid] = package.strategy_id
            trades[pid] = package.trade_id or trades.get(pid)
            if package.state != "expired":
                for leg in package.legs:
                    inventories[pid][str(leg["asset_key"])] += int(leg["filled_signed_quantity"])

    def _submission_position(self, package_id, strategy_id, intent, legs,
                             position_id, replacement_of, now, settings, trade_id=None) -> str:
        if intent == "open":
            pid = position_id or package_id
            self._connection.execute(
                "INSERT INTO positions(position_id,strategy_id,trade_id,settings,created_at) VALUES(?,?,?,?,?)",
                (pid, strategy_id, str(trade_id) if trade_id is not None else None, json.dumps(dict(settings or {}), allow_nan=False), now),
            )
            return pid
        if position_id is None and replacement_of:
            position_id = self.get_basket(replacement_of).position_id
        if position_id is None:
            direction = 1 if intent == "add" else -1
            candidates = []
            for position in self.open_positions(strategy_id):
                inventory = self.position_quantities(position.position_id)
                if all(inventory.get(leg.asset_key, 0) * leg.requested_signed_quantity * direction > 0
                       for leg in legs):
                    candidates.append(position.position_id)
            if len(candidates) != 1:
                raise ValueError("an adjustment requires an unambiguous position_id")
            position_id = candidates[0]
        position = self.get_position(position_id)
        if position.strategy_id != strategy_id:
            raise ValueError("position belongs to another strategy")
        return position_id

    def get_position(self, position_id: str) -> PositionRecord:
        with self._lock:
            row = self._connection.execute(
                "SELECT * FROM positions WHERE position_id = ?", (position_id,)
            ).fetchone()
            if row is None:
                raise KeyError(f"unknown position_id: {position_id}")
            return PositionRecord(
                str(row["position_id"]), str(row["strategy_id"]), row["trade_id"],
                json.loads(row["settings"]), row["exit_reason"], float(row["mfe_pnl"]),
                float(row["mae_pnl"]), str(row["created_at"]),
            )

    def position_baskets(self, position_id: str) -> tuple[BasketRecord, ...]:
        with self._lock:
            rows = self._connection.execute(
                "SELECT * FROM packages WHERE position_id = ? ORDER BY rowid", (position_id,)
            ).fetchall()
            return tuple(self._basket_from_row(row) for row in rows)

    def open_positions(self, strategy_id: str | None = None) -> tuple[PositionRecord, ...]:
        with self._lock:
            query = """SELECT position_id FROM positions WHERE position_id IN (
                SELECT p.position_id FROM packages p JOIN legs l USING(package_id)
                WHERE p.state != 'expired' AND p.position_id IS NOT NULL
                GROUP BY p.position_id,l.asset_key HAVING SUM(l.filled_signed_quantity) != 0
                UNION
                SELECT position_id FROM packages
                WHERE state IN ('submitted','partial','closing') AND terminal_status IS NULL
            )"""
            args = ()
            if strategy_id is not None:
                query += " AND strategy_id = ?"
                args = (strategy_id,)
            query += " ORDER BY created_at,position_id"
            ids = [str(row[0]) for row in self._connection.execute(query, args)]
            return tuple(self.get_position(pid) for pid in ids)

    @staticmethod
    def is_inflight(basket: BasketRecord) -> bool:
        return basket.state in {"submitted", "partial", "closing"} and basket.terminal_status is None

    def _position_accounting(self, position_id: str, through_package: str | None = None):
        quantities: dict[str, int] = defaultdict(int)
        costs: dict[str, float] = defaultdict(float)
        removed: dict[str, float] = defaultdict(float)
        valid = True
        for basket in self.position_baskets(position_id):
            if basket.state == "expired":
                continue
            for leg in basket.legs:
                quantity = int(leg["filled_signed_quantity"])
                if not quantity:
                    continue
                key = str(leg["asset_key"])
                price = leg["fill_price"]
                if price is None:
                    valid = False
                    continue
                if basket.intent in {"open", "add"}:
                    if quantities[key] * quantity < 0:
                        valid = False
                    quantities[key] += quantity
                    costs[key] += quantity * float(price) * 100
                elif quantities[key] * quantity >= 0 or abs(quantity) > abs(quantities[key]):
                    valid = False
                    quantities[key] += quantity
                else:
                    basis = costs[key] * abs(quantity / quantities[key])
                    removed[basket.package_id] -= basis
                    costs[key] -= basis
                    quantities[key] += quantity
            if basket.package_id == through_package:
                break
        return ({key: q for key, q in quantities.items() if q}, costs, removed, valid)

    def quantities_after(self, package_id: str) -> dict[str, int]:
        basket = self.get_basket(package_id)
        with self._lock:
            return self._position_accounting(basket.position_id, through_package=package_id)[0]

    def unreported_baskets(self, strategy_id: str) -> tuple[BasketRecord, ...]:
        with self._lock:
            rows = self._connection.execute(
                "SELECT * FROM packages WHERE strategy_id = ? AND state != 'expired' ORDER BY rowid",
                (strategy_id,),
            ).fetchall()
            result = []
            for row in rows:
                basket = self._basket_from_row(row)
                event = {"open": "ENTRY", "add": "ADDITION"}.get(basket.intent, "REALIZATION")
                if (basket.position_id and not self.is_inflight(basket)
                        and any(int(leg["filled_signed_quantity"]) for leg in basket.legs)
                        and not self.journaled(basket.package_id, event)):
                    result.append(basket)
            return tuple(result)

    def position_quantities(self, position_id: str) -> dict[str, int]:
        with self._lock:
            rows = self._connection.execute(
                """SELECT l.asset_key,SUM(l.filled_signed_quantity) AS quantity
                   FROM legs l JOIN packages p USING(package_id)
                   WHERE p.position_id = ? AND p.state != 'expired' GROUP BY l.asset_key""",
                (position_id,),
            ).fetchall()
            return {str(row[0]): int(row[1]) for row in rows if row[1]}

    def position_opening_cash(self, position_id: str) -> float:
        with self._lock:
            _, costs, _, valid = self._position_accounting(position_id)
            if not valid:
                raise ValueError("position inventory cannot be attributed")
            return round(-sum(costs.values()), 2)

    def closing_opening_cash(self, package_id: str) -> float:
        basket = self.get_basket(package_id)
        if not basket.position_id:
            raise ValueError("closing package has no position owner")
        with self._lock:
            _, _, removed, valid = self._position_accounting(basket.position_id)
            if not valid:
                raise ValueError("closing fill exceeds attributable inventory")
            return round(removed.get(package_id, 0.0), 2)

    def update_position(self, position_id: str, *, settings: Mapping[str, Any] | None = None,
                        exit_reason: str | None = None, mfe_pnl: float | None = None,
                        mae_pnl: float | None = None) -> None:
        values: dict[str, Any] = {}
        if settings is not None:
            values["settings"] = json.dumps(dict(settings), allow_nan=False)
        if exit_reason is not None:
            values["exit_reason"] = exit_reason
        if mfe_pnl is not None:
            values["mfe_pnl"] = float(mfe_pnl)
        if mae_pnl is not None:
            values["mae_pnl"] = float(mae_pnl)
        if values:
            with self._lock, self._connection:
                result = self._connection.execute(
                    "UPDATE positions SET " + ",".join(f"{key} = ?" for key in values) + " WHERE position_id = ?",
                    (*values.values(), position_id),
                )
                if result.rowcount != 1:
                    raise KeyError(position_id)

    def clear_exit_reason(self, position_id: str) -> None:
        with self._lock, self._connection:
            result = self._connection.execute(
                "UPDATE positions SET exit_reason = NULL WHERE position_id = ?", (position_id,)
            )
            if result.rowcount != 1:
                raise KeyError(position_id)

    def journaled(self, package_id: str, event: str) -> bool:
        with self._lock:
            return self._connection.execute(
                "SELECT 1 FROM journal_events WHERE package_id = ? AND event = ?", (package_id, event)
            ).fetchone() is not None

    def mark_journaled(self, package_id: str, event: str) -> None:
        with self._lock, self._connection:
            self._connection.execute("INSERT OR IGNORE INTO journal_events VALUES(?,?)", (package_id, event))

    def assert_can_submit(self, strategy_id: str, intent: str, legs: Iterable[LedgerLeg], *,
                          position_id: str | None, max_positions: int = 1,
                          strategy_cap: float, portfolio_cap: float | None = None,
                          single_trade_cap: float | None = None,
                          allocation_cap: float | None = None,
                          min_spacing_seconds: float = 0, now: datetime | None = None) -> None:
        """Validate a reservation while the caller holds execution_gate through submission."""
        legs = tuple(legs)
        if self.is_panic_halted():
            raise RuntimeError("Durable panic halt is active")
        if intent in {"open", "add"}:
            own_risk = self.portfolio_defined_risk(strategy_id)
            all_risk = self.portfolio_defined_risk()
            proposed = self._package_width_and_quantity(
                [(json.loads(leg.asset_key), leg.requested_signed_quantity) for leg in legs]
            )
            if proposed is None or own_risk is None or all_risk is None:
                raise RuntimeError("defined risk is unknown; new exposure is blocked")
            added = proposed[0] * proposed[1] * 100
            gross_position = added
            if intent == "add":
                if position_id is None:
                    raise ValueError("addition requires position_id")
                position = self.get_position(position_id)
                inventory = self.position_quantities(position_id)
                if (position.strategy_id != strategy_id or position.exit_reason
                        or set(inventory) != {leg.asset_key for leg in legs}
                        or any(inventory[leg.asset_key] * leg.requested_signed_quantity <= 0 for leg in legs)):
                    raise RuntimeError("addition must retain its position's contracts and direction")
                held_shape = self._package_width_and_quantity([(json.loads(k), q) for k, q in inventory.items()])
                if held_shape is None or any(self.is_inflight(b) for b in self.position_baskets(position_id)):
                    raise RuntimeError("position has unresolved submissions")
                gross_position += held_shape[0] * held_shape[1] * 100
            if single_trade_cap is not None and gross_position > single_trade_cap:
                raise RuntimeError("single-position risk limit exceeded")
            if allocation_cap is not None and gross_position > allocation_cap:
                raise RuntimeError("position allocation limit exceeded")
            if own_risk + added > strategy_cap:
                raise RuntimeError("per-strategy risk cap exceeded")
            if portfolio_cap is not None and all_risk + added > portfolio_cap:
                raise RuntimeError("portfolio risk cap exceeded")
            if intent == "open":
                if len(self.open_positions(strategy_id)) >= max_positions:
                    raise RuntimeError("position concurrency limit reached")
                latest = self._connection.execute(
                    """SELECT MAX(created_at) FROM packages WHERE strategy_id = ? AND intent = 'open'
                       AND (state IN ('submitted','partial','open','closing','closed')
                            OR EXISTS(SELECT 1 FROM legs l WHERE l.package_id = packages.package_id
                                      AND l.filled_signed_quantity != 0))""", (strategy_id,)
                ).fetchone()[0]
                if latest and min_spacing_seconds > 0:
                    previous = datetime.fromisoformat(latest)
                    current = now or datetime.now(timezone.utc)
                    if previous.tzinfo is None:
                        previous = previous.replace(tzinfo=timezone.utc)
                    if current.tzinfo is None:
                        current = current.replace(tzinfo=timezone.utc)
                    if (current - previous).total_seconds() < min_spacing_seconds:
                        raise RuntimeError("entry spacing has not elapsed")
            # Include individual owners and unfilled opening legs. Account-net
            # quantities alone can hide a conflicting reservation.
            directions: dict[str, set[int]] = defaultdict(set)
            for position in self.open_positions():
                for key, quantity in self.position_quantities(position.position_id).items():
                    directions[key].add(1 if quantity > 0 else -1)
            for basket in self.open_baskets():
                if self.is_inflight(basket) and basket.intent in {"open", "add"}:
                    for leg in basket.legs:
                        remaining = int(leg["requested_signed_quantity"]) - int(leg["filled_signed_quantity"])
                        if remaining:
                            directions[str(leg["asset_key"])].add(1 if remaining > 0 else -1)
            for leg in legs:
                sign = 1 if leg.requested_signed_quantity > 0 else -1
                if -sign in directions[leg.asset_key]:
                    raise RuntimeError("opposing leg conflicts with held or pending inventory")
        else:
            if position_id is None:
                raise ValueError("close or trim requires position_id")
            available = self.position_quantities(position_id)
            if any(self.is_inflight(b) and b.intent in {"open", "add"}
                   for b in self.position_baskets(position_id)):
                raise RuntimeError("opening order must be terminal before closing its inventory")
            for basket in self.position_baskets(position_id):
                if self.is_inflight(basket) and basket.intent in {"close", "trim"}:
                    for leg in basket.legs:
                        key = str(leg["asset_key"])
                        available[key] = available.get(key, 0) + int(leg["requested_signed_quantity"]) - int(leg["filled_signed_quantity"])
            for leg in legs:
                quantity = available.get(leg.asset_key, 0)
                if quantity * leg.requested_signed_quantity >= 0 or abs(leg.requested_signed_quantity) > abs(quantity):
                    raise RuntimeError("close exceeds unreserved position inventory")

    def reserve_submission(self, package_id: str, strategy_id: str, intent: str,
                           legs: Iterable[LedgerLeg], *, limits: Mapping[str, Any],
                           position_id: str, **submission: Any) -> None:
        """Check one consistent ledger snapshot and persist its reservation atomically.

        The caller holds execution_gate until the broker submission returns.
        BEGIN IMMEDIATE also excludes fill writers using other connections while
        capacity and risk are checked, before record_submission commits the row.
        """
        legs = tuple(legs)
        with self._lock:
            self._connection.execute("BEGIN IMMEDIATE")
            try:
                self.assert_can_submit(strategy_id, intent, legs, position_id=position_id, **limits)
                self.record_submission(package_id, strategy_id, intent, legs,
                                       position_id=position_id, submission_started=0, **submission)
            except BaseException:
                self._connection.rollback()
                raise

    def mark_submission_started(self, package_id: str) -> None:
        """Persist uncertainty before invoking the broker submission."""
        with self._lock, self._connection:
            self._connection.execute("UPDATE packages SET submission_started=1 WHERE package_id=?", (package_id,))

    def record_submission(
        self,
        package_id: str,
        strategy_id: str,
        intent: str,
        legs: Iterable[LedgerLeg | Mapping[str, Any]],
        *,
        duration: str = "day",
        limit_price: float | None = None,
        broker_tag: str | None = None,
        replacement_of: str | None = None,
        reprice_count: int = 0,
        created_at: datetime | str | None = None,
        position_id: str | None = None,
        trade_id: str | None = None,
        settings: Mapping[str, Any] | None = None,
        submission_started: int | None = None,
    ) -> None:
        """Atomically record a broker package before considering any leg owned."""
        if not getattr(self, "_position_schema_ready", False):
            self._initialize()
        normalized = tuple(self._coerce_leg(leg) for leg in legs)
        if len(normalized) not in (2, 4):
            raise ValueError("an option package must contain exactly two legs (vertical) or four legs (iron condor)")
        if len({leg.order_id for leg in normalized}) != len(normalized):
            raise ValueError("package order identifiers must be unique")
        if len({leg.asset_key for leg in normalized}) != len(normalized):
            raise ValueError("package assets must be unique")
        if not package_id or not strategy_id:
            raise ValueError("package_id and strategy_id are required")
        normalized_intent = intent.strip().lower()
        if normalized_intent not in {"open", "add", "trim", "close"}:
            raise ValueError(f"unsupported package intent: {intent}")
        price = None if limit_price is None else float(limit_price)
        if price is not None and (not math.isfinite(price) or price < 0):
            raise ValueError("limit_price must be finite and non-negative")
        now = (
            datetime.now(timezone.utc).isoformat()
            if created_at is None
            else (
                created_at.replace(tzinfo=timezone.utc).isoformat()
                if isinstance(created_at, datetime) and created_at.tzinfo is None
                else (
                    created_at.astimezone(timezone.utc).isoformat()
                    if isinstance(created_at, datetime)
                    else str(created_at)
                )
            )
        )
        with self._lock, self._connection:
            position_id = self._submission_position(
                package_id, strategy_id, normalized_intent, normalized,
                position_id, replacement_of, now, settings, trade_id=trade_id,
            )
            package_data = {
                "package_id": package_id,
                "strategy_id": strategy_id,
                "intent": normalized_intent,
                "state": "submitted",
                "opening_cash_flow": None,
                "closing_cash_flow": None,
                "created_at": now,
                "updated_at": now,
                "broker_parent_id": None,
                "broker_tag": broker_tag,
                "duration": duration.lower(),
                "limit_price": price,
                "trade_id": self.get_position(position_id).trade_id,
                "replacement_of": replacement_of,
                "reprice_count": int(reprice_count),
                "position_id": position_id,
                "terminal_status": None,
                "submission_started": submission_started,
            }
            package_cols = {
                str(col["name"])
                for col in self._connection.execute("PRAGMA table_info(packages)").fetchall()
            }
            active_package_cols = [k for k in package_data if k in package_cols]
            pkg_sql = (
                f"INSERT INTO packages ({', '.join(active_package_cols)}) "
                f"VALUES ({', '.join('?' for _ in active_package_cols)})"
            )
            self._connection.execute(pkg_sql, tuple(package_data[k] for k in active_package_cols))

            leg_cols = {
                str(col["name"])
                for col in self._connection.execute("PRAGMA table_info(legs)").fetchall()
            }
            for leg in normalized:
                leg_data = {
                    "order_id": leg.order_id,
                    "package_id": package_id,
                    "asset_key": leg.asset_key,
                    "requested_signed_quantity": leg.requested_signed_quantity,
                    "filled_signed_quantity": 0,
                    "fill_price": None,
                    "status": "submitted",
                    "local_order_id": leg.order_id,
                    "broker_order_id": leg.broker_order_id,
                    "broker_parent_id": leg.broker_parent_id,
                    "side": self._side_for(normalized_intent, leg.requested_signed_quantity),
                    "created_at": now,
                    "updated_at": now,
                }
                active_leg_cols = [k for k in leg_data if k in leg_cols]
                leg_sql = (
                    f"INSERT INTO legs ({', '.join(active_leg_cols)}) "
                    f"VALUES ({', '.join('?' for _ in active_leg_cols)})"
                )
                self._connection.execute(leg_sql, tuple(leg_data[k] for k in active_leg_cols))

    def bind_broker_submission(self, package_id: str, parent_order: Any) -> BasketRecord:
        """Bind the broker-assigned parent ID and any returned child orders."""
        parent_id = self._order_identifier(parent_order)
        if not parent_id:
            raise ValueError("broker parent order has no identifier")
        children = tuple(getattr(parent_order, "child_orders", None) or ())
        with self._lock, self._connection:
            result = self._connection.execute(
                """UPDATE packages SET broker_parent_id = ?, updated_at = ?
                   WHERE package_id = ?""",
                (parent_id, datetime.now(timezone.utc).isoformat(), package_id),
            )
            if result.rowcount != 1:
                raise KeyError(f"unknown package_id: {package_id}")
            self._connection.execute(
                "UPDATE legs SET broker_parent_id = ? WHERE package_id = ?",
                (parent_id, package_id),
            )
        if children:
            self.bind_broker_children(package_id, children)
        return self.get_basket(package_id)

    def bind_broker_children(
        self, package_id: str, child_orders: Iterable[Any]
    ) -> BasketRecord:
        """Bind broker leg IDs by exact option asset and side."""
        with self._lock, self._connection:
            for child in child_orders:
                broker_id = self._order_identifier(child)
                child_asset = getattr(child, "asset", None)
                child_side = str(getattr(child, "side", "") or "").lower()
                if not broker_id or child_asset is None or not child_side:
                    continue
                key = asset_key(child_asset)
                side_prefix = "buy" if "buy" in child_side else "sell"
                rows = self._connection.execute(
                    """SELECT * FROM legs WHERE package_id = ? AND asset_key = ?
                       AND (lower(side) = ? OR lower(side) LIKE ? || '%')""",
                    (package_id, key, child_side, side_prefix),
                ).fetchall()
                if len(rows) != 1:
                    continue
                self._connection.execute(
                    """UPDATE legs SET broker_order_id = ?, updated_at = ?
                       WHERE order_id = ?""",
                    (
                        broker_id,
                        datetime.now(timezone.utc).isoformat(),
                        str(rows[0]["order_id"]),
                    ),
                )
        return self.get_basket(package_id)

    def resolve_leg(
        self,
        local_id: str | None = None,
        broker_id: str | None = None,
        parent_id: str | None = None,
        asset_key: str | None = None,
        side: str | None = None,
    ) -> Mapping[str, Any] | None:
        """Resolve a callback to one local leg without comparing unlike identities."""
        with self._lock:
            row = None
            if broker_id:
                row = self._connection.execute(
                    "SELECT * FROM legs WHERE broker_order_id = ?", (str(broker_id),)
                ).fetchone()
            if row is None and local_id:
                row = self._connection.execute(
                    """SELECT * FROM legs
                       WHERE local_order_id = ? OR order_id = ?""",
                    (str(local_id), str(local_id)),
                ).fetchone()
            if row is None and parent_id and asset_key:
                query = "SELECT * FROM legs WHERE broker_parent_id = ? AND asset_key = ?"
                parameters: list[Any] = [str(parent_id), str(asset_key)]
                if side:
                    side_prefix = "buy" if "buy" in str(side).lower() else "sell"
                    query += " AND (lower(side) = ? OR lower(side) LIKE ? || '%')"
                    parameters.extend([str(side).lower(), side_prefix])
                rows = self._connection.execute(query, tuple(parameters)).fetchall()
                if len(rows) == 1:
                    row = rows[0]
            return dict(row) if row is not None else None

    def record_fill(
        self,
        order_id: str | None,
        signed_quantity: int,
        fill_price: float,
        *,
        status: str = "filled",
        broker_id: str | None = None,
        parent_id: str | None = None,
        asset_key: str | None = None,
        side: str | None = None,
        cumulative: bool = True,
    ) -> BasketRecord:
        """Record a confirmed fill and return the refreshed, possibly terminal basket."""
        if isinstance(signed_quantity, bool) or not isinstance(signed_quantity, int):
            raise ValueError("signed_quantity must be an integer")
        if signed_quantity == 0:
            raise ValueError("signed_quantity must be non-zero")
        price = float(fill_price)
        if not math.isfinite(price) or price < 0:
            raise ValueError("fill_price must be finite and non-negative")
        with self._lock, self._connection:
            leg = self.resolve_leg(order_id, broker_id, parent_id, asset_key, side)
            if leg is None:
                raise KeyError(f"unknown order identifier: {broker_id or order_id}")
            requested = int(leg["requested_signed_quantity"])
            if requested * signed_quantity <= 0:
                raise ValueError("fill direction does not match requested direction")
            previous_quantity = int(leg["filled_signed_quantity"])
            filled = signed_quantity if cumulative else previous_quantity + signed_quantity
            if abs(filled) < abs(previous_quantity):
                return self.get_basket(str(leg["package_id"]))
            if abs(filled) > abs(requested):
                raise ValueError("fill exceeds requested quantity")
            if filled == previous_quantity:
                return self.get_basket(str(leg["package_id"]))
            previous_price = leg["fill_price"]
            delta = abs(filled) - abs(previous_quantity)
            if cumulative:
                weighted_price = price
            else:
                weighted_price = (
                    price
                    if previous_quantity == 0
                    else (
                        abs(previous_quantity) * float(previous_price)
                        + delta * price
                    )
                    / abs(filled)
                )
            now = datetime.now(timezone.utc).isoformat()
            self._connection.execute(
                """UPDATE legs SET filled_signed_quantity = ?, fill_price = ?,
                   status = ?, broker_order_id = COALESCE(broker_order_id, ?),
                   broker_parent_id = COALESCE(broker_parent_id, ?), updated_at = ?
                   WHERE order_id = ?""",
                (
                    filled,
                    weighted_price,
                    status,
                    broker_id,
                    parent_id,
                    now,
                    str(leg["order_id"]),
                ),
            )
            return self._refresh_package(str(leg["package_id"]))

    def record_terminal_status(
        self, package_id: str, status: str, updated_at: datetime | str | None = None
    ) -> None:
        """Record a broker-confirmed terminal state for a package."""
        normalized = status.strip().lower()
        if normalized not in TERMINAL_STATES:
            raise ValueError(f"non-terminal package state: {status}")
        ts = (
            datetime.now(timezone.utc).isoformat()
            if updated_at is None
            else (
                updated_at.replace(tzinfo=timezone.utc).isoformat()
                if isinstance(updated_at, datetime) and updated_at.tzinfo is None
                else (
                    updated_at.astimezone(timezone.utc).isoformat()
                    if isinstance(updated_at, datetime)
                    else str(updated_at)
                )
            )
        )
        with self._lock, self._connection:
            package = self._connection.execute(
                "SELECT * FROM packages WHERE package_id = ?", (package_id,)
            ).fetchone()
            if package is None:
                raise KeyError(f"unknown package_id: {package_id}")
            strategy_id = str(package["strategy_id"])
            legs = self._connection.execute(
                "SELECT * FROM legs WHERE package_id = ?", (package_id,)
            ).fetchall()
            any_fill = any(int(leg["filled_signed_quantity"]) for leg in legs)
            if any_fill and not self.owned_quantities(strategy_id):
                final_state = "closed"
            else:
                final_state = normalized
            result = self._connection.execute(
                "UPDATE packages SET state = ?, terminal_status = ?, updated_at = ? WHERE package_id = ?",
                (final_state, normalized, ts, package_id),
            )
            if result.rowcount != 1:
                raise KeyError(f"unknown package_id: {package_id}")

    def resolve_partial_package(
        self,
        package_id: str,
        terminal_status: str,
        updated_at: datetime | str | None = None,
    ) -> BasketRecord:
        """Resolve a partially filled package whose broker order reached terminal status."""
        normalized = terminal_status.strip().lower()
        if normalized not in TERMINAL_STATES:
            raise ValueError(f"non-terminal package state: {terminal_status}")
        with self._lock, self._connection:
            package = self._connection.execute(
                "SELECT * FROM packages WHERE package_id = ?", (package_id,)
            ).fetchone()
            if package is None:
                raise KeyError(f"unknown package_id: {package_id}")
            intent = str(package["intent"])
            self._connection.execute(
                "UPDATE packages SET terminal_status = ? WHERE package_id = ?", (normalized, package_id)
            )
            now = (
                datetime.now(timezone.utc).isoformat()
                if updated_at is None
                else (
                    updated_at.replace(tzinfo=timezone.utc).isoformat()
                    if isinstance(updated_at, datetime) and updated_at.tzinfo is None
                    else (
                        updated_at.astimezone(timezone.utc).isoformat()
                        if isinstance(updated_at, datetime)
                        else str(updated_at)
                    )
                )
            )
            strategy_id = str(package["strategy_id"])
            position_id = package["position_id"]
            if intent in {"close", "trim"}:
                self._connection.execute(
                    "UPDATE packages SET state = ?, updated_at = ? WHERE package_id = ?",
                    (normalized, now, package_id),
                )
            elif intent in {"open", "add"}:
                legs = self._connection.execute(
                    "SELECT * FROM legs WHERE package_id = ?", (package_id,)
                ).fetchall()
                any_fill = any(int(leg["filled_signed_quantity"]) for leg in legs)
                ratios = {
                    abs(int(leg["filled_signed_quantity"])) / abs(int(leg["requested_signed_quantity"]))
                    for leg in legs if int(leg["requested_signed_quantity"])
                }
                balanced = len(ratios) == 1 and len(legs) > 0 and all(
                    int(leg["requested_signed_quantity"]) for leg in legs
                )
                if not any_fill:
                    new_state = normalized
                elif (position_id and not self.position_quantities(str(position_id))) or (not self.owned_quantities(strategy_id)):
                    new_state = "closed"
                elif balanced:
                    new_state = "open"
                else:
                    new_state = "partial"
                self._connection.execute(
                    "UPDATE packages SET state = ?, updated_at = ? WHERE package_id = ?",
                    (new_state, now, package_id),
                )
            return self.get_basket(package_id)

    def set_panic_halt(self, halted: bool = True, reason: str = "panic_liquidation") -> None:
        """Persist a durable panic halt to block subsequent entries and trading iterations."""
        now = datetime.now(timezone.utc).isoformat()
        with self._lock, self._connection:
            val = "1" if halted else "0"
            self._connection.execute(
                """INSERT INTO system_state (key, value, updated_at) VALUES ('panic_halt', ?, ?)
                   ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at""",
                (val, now),
            )

    def is_panic_halted(self) -> bool:
        """Check if durable panic halt is active."""
        with self._lock:
            row = self._connection.execute(
                "SELECT value FROM system_state WHERE key = 'panic_halt'"
            ).fetchone()
            if row is None:
                return False
            return str(row["value"]).strip() in ("1", "true", "True")

    # ------------------------------------------------------------------ #
    # Recovery Jobs & Coordinator State
    # ------------------------------------------------------------------ #

    def enqueue_recovery_job(
        self, scope: str, target_id: str | None = None, payload: dict | None = None
    ) -> str:
        """Enqueue a durable recovery job if not already pending or in progress."""
        now = datetime.now(timezone.utc).isoformat()
        payload_json = json.dumps(payload or {})
        with self._lock, self._connection:
            row = self._connection.execute(
                """SELECT job_id FROM recovery_jobs
                   WHERE scope = ? AND (target_id = ? OR (target_id IS NULL AND ? IS NULL))
                   AND status IN ('pending', 'in_progress')""",
                (scope, target_id, target_id),
            ).fetchone()
            if row is not None:
                return str(row["job_id"])
            job_id = uuid.uuid4().hex
            self._connection.execute(
                """INSERT INTO recovery_jobs (job_id, scope, target_id, status, payload, attempts, created_at, updated_at)
                   VALUES (?, ?, ?, 'pending', ?, 0, ?, ?)""",
                (job_id, scope, target_id, payload_json, now, now),
            )
            return job_id

    def claim_next_recovery_job(
        self, worker_id: str, lock_timeout_seconds: float = 30.0
    ) -> dict[str, Any] | None:
        """Claim the oldest pending or timed-out recovery job."""
        now = datetime.now(timezone.utc)
        now_str = now.isoformat()
        with self._lock, self._connection:
            cursor = self._connection.execute(
                """SELECT * FROM recovery_jobs
                   WHERE status = 'pending'
                   ORDER BY created_at ASC LIMIT 1"""
            )
            row = cursor.fetchone()
            if row is None:
                cursor = self._connection.execute(
                    """SELECT * FROM recovery_jobs
                       WHERE status = 'in_progress'
                       ORDER BY updated_at ASC LIMIT 1"""
                )
                cand = cursor.fetchone()
                if cand is not None:
                    try:
                        updated_at = datetime.fromisoformat(cand["updated_at"])
                        if updated_at.tzinfo is None:
                            updated_at = updated_at.replace(tzinfo=timezone.utc)
                        if (now - updated_at).total_seconds() > lock_timeout_seconds:
                            row = cand
                    except Exception:
                        row = cand
            if row is None:
                return None
            job_id = row["job_id"]
            attempts = int(row["attempts"]) + 1
            self._connection.execute(
                """UPDATE recovery_jobs
                   SET status = 'in_progress', attempts = ?, updated_at = ?
                   WHERE job_id = ?""",
                (attempts, now_str, job_id),
            )
            job_dict = dict(row)
            job_dict["attempts"] = attempts
            job_dict["status"] = "in_progress"
            try:
                job_dict["payload"] = json.loads(row["payload"])
            except Exception:
                job_dict["payload"] = {}
            return job_dict

    def update_recovery_job(
        self,
        job_id: str,
        status: str,
        last_error: str | None = None,
        payload: dict | None = None,
    ) -> None:
        """Update recovery job terminal status or retry error."""
        now_str = datetime.now(timezone.utc).isoformat()
        with self._lock, self._connection:
            if payload is not None:
                self._connection.execute(
                    """UPDATE recovery_jobs
                       SET status = ?, last_error = ?, payload = ?, updated_at = ?
                       WHERE job_id = ?""",
                    (status, last_error, json.dumps(payload), now_str, job_id),
                )
            else:
                self._connection.execute(
                    """UPDATE recovery_jobs
                       SET status = ?, last_error = ?, updated_at = ?
                       WHERE job_id = ?""",
                    (status, last_error, now_str, job_id),
                )

    def list_recovery_jobs(
        self, scope: str | None = None, status: str | None = None
    ) -> list[dict[str, Any]]:
        """List recovery jobs with optional scope and status filters."""
        query = "SELECT * FROM recovery_jobs WHERE 1=1"
        params: list[Any] = []
        if scope:
            query += " AND scope = ?"
            params.append(scope)
        if status:
            query += " AND status = ?"
            params.append(status)
        query += " ORDER BY created_at ASC"
        with self._lock:
            rows = self._connection.execute(query, params).fetchall()
            results = []
            for r in rows:
                item = dict(r)
                try:
                    item["payload"] = json.loads(r["payload"])
                except Exception:
                    item["payload"] = {}
                results.append(item)
            return results

    def acquire_worker_exclusion(
        self, worker_id: str, timeout_seconds: float = 15.0
    ) -> bool:
        """Enforce single-worker exclusion via heartbeat lease."""
        now = datetime.now(timezone.utc)
        now_str = now.isoformat()
        with self._lock, self._connection:
            rows = self._connection.execute(
                "SELECT * FROM recovery_worker_heartbeat WHERE is_active = 1"
            ).fetchall()
            for r in rows:
                if str(r["worker_id"]) != worker_id:
                    try:
                        hb = datetime.fromisoformat(r["last_heartbeat"])
                        if hb.tzinfo is None:
                            hb = hb.replace(tzinfo=timezone.utc)
                        if (now - hb).total_seconds() < timeout_seconds:
                            return False
                    except Exception:
                        pass
            self._connection.execute(
                """INSERT INTO recovery_worker_heartbeat (worker_id, pid, hostname, last_heartbeat, is_active)
                   VALUES (?, ?, ?, ?, 1)
                   ON CONFLICT(worker_id) DO UPDATE SET last_heartbeat = excluded.last_heartbeat, is_active = 1""",
                (worker_id, os.getpid(), os.uname().nodename if hasattr(os, "uname") else "localhost", now_str),
            )
            return True

    def update_worker_heartbeat(self, worker_id: str) -> None:
        """Update heartbeat timestamp for active worker."""
        now_str = datetime.now(timezone.utc).isoformat()
        with self._lock, self._connection:
            self._connection.execute(
                "UPDATE recovery_worker_heartbeat SET last_heartbeat = ? WHERE worker_id = ?",
                (now_str, worker_id),
            )

    def release_worker(self, worker_id: str) -> None:
        """Release worker exclusion lease on graceful shutdown."""
        with self._lock, self._connection:
            self._connection.execute(
                "UPDATE recovery_worker_heartbeat SET is_active = 0 WHERE worker_id = ?",
                (worker_id,),
            )

    # ------------------------------------------------------------------ #
    # Emergency Submissions Tracking
    # ------------------------------------------------------------------ #

    def persist_emergency_intent(
        self,
        intent_id: str,
        symbol: str,
        side: str,
        quantity: int,
        price: float | None = None,
        payload: dict | None = None,
    ) -> None:
        """Persist submission intent BEFORE dispatching order to broker."""
        now_str = datetime.now(timezone.utc).isoformat()
        with self._lock, self._connection:
            self._connection.execute(
                """INSERT INTO emergency_submissions (intent_id, symbol, side, quantity, price, status, payload, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, 'intent', ?, ?, ?)
                   ON CONFLICT(intent_id) DO UPDATE SET updated_at = excluded.updated_at""",
                (intent_id, symbol, side, quantity, price, json.dumps(payload or {}), now_str, now_str),
            )

    def update_emergency_submission(
        self,
        intent_id: str,
        broker_id: str | None = None,
        status: str = "submitted",
        cumulative_fills: int = 0,
        last_error: str | None = None,
    ) -> None:
        """Record returned broker ID, acceptance, fills, or errors."""
        now_str = datetime.now(timezone.utc).isoformat()
        with self._lock, self._connection:
            self._connection.execute(
                """UPDATE emergency_submissions
                   SET broker_id = COALESCE(?, broker_id), status = ?, cumulative_fills = ?, last_error = ?, updated_at = ?
                   WHERE intent_id = ?""",
                (broker_id, status, cumulative_fills, last_error, now_str, intent_id),
            )

    def get_unresolved_emergency_submissions(
        self, symbol: str | None = None
    ) -> list[dict[str, Any]]:
        """Fetch submissions that are still in-flight, unresolved, or partially filled."""
        query = """SELECT * FROM emergency_submissions
                   WHERE status IN ('intent', 'submitted', 'unresolved', 'partially_filled')"""
        params: list[Any] = []
        if symbol:
            query += " AND symbol = ?"
            params.append(symbol)
        with self._lock:
            rows = self._connection.execute(query, params).fetchall()
            return [dict(r) for r in rows]

    def set_trade_id(self, package_id: str, trade_id: str) -> None:
        """Attach every submission in a position to the same journal identity."""
        with self._lock, self._connection:
            basket = self.get_basket(package_id)
            if not basket.position_id:
                raise ValueError("package has no position owner")
            existing = self.get_position(basket.position_id).trade_id
            if existing and existing != str(trade_id):
                raise ValueError("a position's trade_id is immutable")
            self._connection.execute(
                "UPDATE positions SET trade_id = ? WHERE position_id = ?",
                (str(trade_id), basket.position_id),
            )
            self._connection.execute(
                "UPDATE packages SET trade_id = ? WHERE position_id = ?",
                (str(trade_id), basket.position_id),
            )

    def active_trade_id(self, strategy_id: str, position_id: str | None = None) -> str | None:
        """Return a unique active identity; several positions require explicit selection."""
        if position_id is not None:
            position = self.get_position(position_id)
            if position.strategy_id != strategy_id:
                raise ValueError("position belongs to another strategy")
            return position.trade_id
        positions = self.open_positions(strategy_id)
        return positions[0].trade_id if len(positions) == 1 else None

    def stale_day_baskets(
        self, strategy_id: str, trading_date: date
    ) -> tuple[BasketRecord, ...]:
        """Return active DAY packages created before ``trading_date``."""
        return tuple(
            basket
            for basket in self.open_baskets(strategy_id)
            if (basket.duration or "").lower() == "day"
            and basket.created_at is not None
            and datetime.fromisoformat(basket.created_at).date() < trading_date
        )

    def get_basket(self, package_id: str) -> BasketRecord:
        """Return an active or terminal package."""
        with self._lock:
            package = self._connection.execute(
                "SELECT * FROM packages WHERE package_id = ?", (package_id,)
            ).fetchone()
            if package is None:
                raise KeyError(f"unknown package_id: {package_id}")
            return self._basket_from_row(package)

    def latest_basket(
        self, strategy_id: str, intents: Iterable[str] | None = None
    ) -> BasketRecord | None:
        """Return the most recently updated package for a strategy."""
        query = "SELECT * FROM packages WHERE strategy_id = ?"
        parameters: list[Any] = [strategy_id]
        normalized = tuple(str(intent).lower() for intent in (intents or ()))
        if normalized:
            query += f" AND intent IN ({','.join('?' for _ in normalized)})"
            parameters.extend(normalized)
        query += " ORDER BY updated_at DESC, rowid DESC LIMIT 1"
        with self._lock:
            row = self._connection.execute(query, tuple(parameters)).fetchone()
            return self._basket_from_row(row) if row is not None else None

    def record_settlement_value(self, settlement: Any) -> None:
        """Store verified index settlement value in SQLite transaction."""
        from decimal import Decimal
        exp_str = str(settlement.expiration)[:10]
        val = (
            float(settlement.value)
            if isinstance(settlement.value, (Decimal, int, float))
            else float(str(settlement.value))
        )
        retrieved = str(
            getattr(settlement, "retrieved_at", datetime.now(timezone.utc).isoformat())
        )
        with self._lock, self._connection:
            self._connection.execute(
                """INSERT OR REPLACE INTO settlement_values
                   (expiration, symbol, value, source_url, retrieved_at, response_hash, category)
                   VALUES (?, 'XSP', ?, ?, ?, ?, ?)""",
                (
                    exp_str,
                    val,
                    str(getattr(settlement, "source_url", "cboe.com")),
                    retrieved,
                    str(getattr(settlement, "response_hash", "")),
                    str(getattr(settlement, "category", "official")),
                ),
            )

    def record_broker_cash_evidence(self, evidence: Any) -> None:
        """Store broker cash evidence in SQLite transaction."""
        exp_str = str(evidence.expiration)[:10]
        now = datetime.now(timezone.utc).isoformat()
        with self._lock, self._connection:
            self._connection.execute(
                """INSERT OR REPLACE INTO broker_cash_evidence
                   (activity_id, account_scope, expiration, symbol, cash_cents, disposition_confirmed, description, created_at)
                   VALUES (?, ?, ?, 'XSP', ?, ?, ?, ?)""",
                (
                    str(evidence.activity_id),
                    str(evidence.account_scope),
                    exp_str,
                    int(evidence.cash_cents),
                    1 if getattr(evidence, "disposition_confirmed", True) else 0,
                    str(getattr(evidence, "description", "")),
                    now,
                ),
            )

    def record_fee_evidence(
        self,
        fee: Any,
        *,
        trade_id: str | None = None,
        package_id: str | None = None,
        position_id: str | None = None,
    ) -> None:
        """Store fee evidence in SQLite transaction."""
        now = datetime.now(timezone.utc).isoformat()
        with self._lock, self._connection:
            self._connection.execute(
                """INSERT OR REPLACE INTO fee_evidence
                   (identity, trade_id, package_id, position_id, amount_cents, complete, category, provenance, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    str(fee.identity),
                    trade_id,
                    package_id,
                    position_id,
                    int(fee.amount_cents) if fee.amount_cents is not None else None,
                    1 if getattr(fee, "complete", True) else 0,
                    str(getattr(fee, "category", "execution")),
                    str(getattr(fee, "provenance", "")),
                    now,
                ),
            )

    def get_settlement_value(self, expiration: date | str, symbol: str = "XSP") -> Any | None:
        """Retrieve stored settlement value for an expiration date."""
        from decimal import Decimal
        from strategies.xsp_settlement import SettlementValue
        exp_str = str(expiration)[:10]
        with self._lock:
            row = self._connection.execute(
                "SELECT * FROM settlement_values WHERE expiration = ? AND symbol = ?",
                (exp_str, symbol),
            ).fetchone()
            if row is None:
                return None
            try:
                exp_date = datetime.strptime(exp_str, "%Y-%m-%d").date()
            except Exception:
                exp_date = expiration if isinstance(expiration, date) else date.fromisoformat(exp_str)
            try:
                retrieved_dt = datetime.fromisoformat(str(row["retrieved_at"]))
            except Exception:
                retrieved_dt = datetime.now(timezone.utc)
            return SettlementValue(
                expiration=exp_date,
                value=Decimal(str(row["value"])),
                source_url=str(row["source_url"]),
                retrieved_at=retrieved_dt,
                response_hash=str(row["response_hash"]),
                category=str(row["category"]),
            )

    def get_broker_cash_evidence(
        self,
        expiration: date | str,
        account_scope: str | None = None,
        symbol: str = "XSP",
    ) -> list[Any]:
        """Retrieve stored broker cash evidence for an expiration date."""
        from strategies.xsp_settlement import BrokerCashEvidence
        exp_str = str(expiration)[:10]
        query = "SELECT * FROM broker_cash_evidence WHERE expiration = ? AND symbol = ?"
        args: list[Any] = [exp_str, symbol]
        if account_scope is not None:
            query += " AND account_scope = ?"
            args.append(account_scope)
        with self._lock:
            rows = self._connection.execute(query, tuple(args)).fetchall()
            results = []
            for row in rows:
                try:
                    exp_date = datetime.strptime(exp_str, "%Y-%m-%d").date()
                except Exception:
                    exp_date = expiration if isinstance(expiration, date) else date.fromisoformat(exp_str)
                results.append(
                    BrokerCashEvidence(
                        account_scope=str(row["account_scope"]),
                        activity_id=str(row["activity_id"]),
                        expiration=exp_date,
                        cash_cents=int(row["cash_cents"]),
                        disposition_confirmed=bool(row["disposition_confirmed"]),
                    )
                )
            return results

    def get_fee_evidence(
        self,
        trade_id: str | None = None,
        position_id: str | None = None,
    ) -> list[Any]:
        """Retrieve stored fee evidence for a trade or position."""
        from strategies.xsp_settlement import FeeEvidence
        query = "SELECT * FROM fee_evidence WHERE 1=1"
        args: list[Any] = []
        if trade_id is not None:
            query += " AND trade_id = ?"
            args.append(trade_id)
        if position_id is not None:
            query += " AND position_id = ?"
            args.append(position_id)
        with self._lock:
            rows = self._connection.execute(query, tuple(args)).fetchall()
            return [
                FeeEvidence(
                    identity=str(row["identity"]),
                    amount_cents=int(row["amount_cents"]) if row["amount_cents"] is not None else None,
                    complete=bool(row["complete"]),
                    category=str(row["category"]),
                    provenance=str(row["provenance"] or ""),
                )
                for row in rows
            ]

    def get_settlement_reconciliation(self, position_id: str) -> dict[str, Any] | None:
        """Return reconciled settlement record for a position if available."""
        with self._lock:
            row = self._connection.execute(
                "SELECT * FROM settlement_reconciliations WHERE position_id = ?",
                (position_id,),
            ).fetchone()
            return dict(row) if row is not None else None

    def reconcile_settlement(
        self,
        strategy_id: str,
        as_of_date: date,
        *,
        position_id: str | None = None,
        settlement: Any | None = None,
        broker_cash: Any | None = None,
        fees: Iterable[Any] | None = None,
        fee_coverage_complete: bool = True,
    ) -> list[Any]:
        """Transactionally reconcile past-expiration positions against broker evidence."""
        from strategies.xsp_settlement import (
            parse_asset_key_to_leg,
            reconcile_lifecycle,
        )

        if settlement is not None:
            self.record_settlement_value(settlement)
        if broker_cash is not None:
            self.record_broker_cash_evidence(broker_cash)
        if fees is not None:
            for f in fees:
                self.record_fee_evidence(f, position_id=position_id)

        now = datetime.now(timezone.utc).isoformat()
        results = []

        with self._lock, self._connection:
            query = "SELECT * FROM packages WHERE strategy_id = ? AND state != 'expired'"
            args = [strategy_id]
            if position_id is not None:
                query += " AND position_id = ?"
                args.append(position_id)
            query += " ORDER BY created_at, package_id"
            packages = self._connection.execute(query, tuple(args)).fetchall()

            pos_groups: dict[str, list[sqlite3.Row]] = defaultdict(list)
            for pkg in packages:
                pid = str(pkg["position_id"]) if pkg["position_id"] else str(pkg["package_id"])
                pos_groups[pid].append(pkg)

            for pid, pkgs in pos_groups.items():
                pkg_ids = [str(p["package_id"]) for p in pkgs]
                all_legs = []
                for p in pkgs:
                    p_legs = self._connection.execute(
                        "SELECT * FROM legs WHERE package_id = ?", (str(p["package_id"]),)
                    ).fetchall()
                    all_legs.extend(p_legs)

                if not all_legs:
                    continue

                if not all(self._is_expired_asset_key(str(l["asset_key"]), as_of_date) for l in all_legs):
                    continue

                try:
                    first_key = json.loads(str(all_legs[0]["asset_key"]))
                    exp_date = datetime.strptime(str(first_key["expiration"])[:10], "%Y-%m-%d").date()
                except Exception:
                    continue

                active_settlement = settlement or self.get_settlement_value(exp_date)
                cash_evidence_list = (
                    [broker_cash]
                    if broker_cash is not None
                    else self.get_broker_cash_evidence(exp_date)
                )

                if active_settlement is None or not cash_evidence_list:
                    for p_id in pkg_ids:
                        self._connection.execute(
                            "UPDATE packages SET state = 'awaiting_settlement', updated_at = ? WHERE package_id = ? AND state != 'awaiting_settlement'",
                            (now, p_id),
                        )
                    continue

                active_cash = cash_evidence_list[0]

                leg_quantities: dict[str, int] = defaultdict(int)
                for l in all_legs:
                    leg_quantities[str(l["asset_key"])] += int(l["filled_signed_quantity"])

                remaining_legs = [
                    parse_asset_key_to_leg(k, q)
                    for k, q in leg_quantities.items()
                    if q != 0
                ]

                trade_id = next((p["trade_id"] for p in pkgs if p["trade_id"]), None)
                opening_cash_dollars = sum(float(p["opening_cash_flow"] or 0.0) for p in pkgs if p["intent"] in {"open", "add"})
                closing_cash_dollars = sum(float(p["closing_cash_flow"] or 0.0) for p in pkgs if p["intent"] in {"trim", "close"})
                opening_cash_cents = int(round(opening_cash_dollars * 100))
                closing_cash_cents = int(round(closing_cash_dollars * 100))

                stored_fees = self.get_fee_evidence(trade_id=trade_id, position_id=pid)
                active_fees = fees if fees is not None else stored_fees

                lifecycle = reconcile_lifecycle(
                    opening_cash_cents=opening_cash_cents,
                    closing_cash_cents=closing_cash_cents,
                    remaining_legs=remaining_legs,
                    settlement=active_settlement,
                    broker_cash=active_cash,
                    fees=active_fees,
                    fee_coverage_complete=fee_coverage_complete,
                    expiration_passed=True,
                )

                if lifecycle.reconciliation_complete:
                    for p_id in pkg_ids:
                        self._connection.execute(
                            "UPDATE packages SET state = 'expired', updated_at = ? WHERE package_id = ?",
                            (now, p_id),
                        )
                    self._connection.execute(
                        """INSERT OR REPLACE INTO settlement_reconciliations
                           (position_id, strategy_id, trade_id, expiration, outcome,
                            settlement_cash_cents, gross_cents, fees_cents, net_cents,
                            known_fees_cents, fees_complete, reconciled_at)
                           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                        (
                            pid,
                            strategy_id,
                            trade_id,
                            str(exp_date),
                            lifecycle.outcome,
                            lifecycle.settlement_cash_cents,
                            lifecycle.gross_cents,
                            lifecycle.fees_cents,
                            lifecycle.net_cents,
                            lifecycle.known_fees_cents,
                            1 if lifecycle.fees_complete else 0,
                            now,
                        ),
                    )
                    outbox_payload = {
                        "event": "EXIT",
                        "trade_id": trade_id or pid,
                        "strategy": strategy_id,
                        "exit_reason": lifecycle.outcome,
                        "outcome": lifecycle.outcome,
                        "gross_pnl": (lifecycle.gross_cents / 100.0) if lifecycle.gross_cents is not None else None,
                        "fees": (lifecycle.fees_cents / 100.0) if lifecycle.fees_cents is not None else (lifecycle.known_fees_cents / 100.0),
                        "net_pnl": (lifecycle.net_cents / 100.0) if lifecycle.net_cents is not None else None,
                        "known_fees": lifecycle.known_fees_cents / 100.0,
                        "fees_complete": lifecycle.fees_complete,
                        "status": "CLOSED",
                        "source": "broker_verified",
                        "reconciliation_status": "VERIFIED" if lifecycle.fees_complete else "FEE_INCOMPLETE",
                    }
                    self._connection.execute(
                        "INSERT INTO outbox (event_type, payload, status, created_at) VALUES ('EXIT', ?, 'pending', ?)",
                        (json.dumps(outbox_payload), now),
                    )
                    for p_id in pkg_ids:
                        self._connection.execute(
                            "INSERT OR IGNORE INTO journal_events (package_id, event) VALUES (?, 'EXIT')",
                            (p_id,),
                        )
                    results.append(lifecycle)
                else:
                    for p_id in pkg_ids:
                        self._connection.execute(
                            "UPDATE packages SET state = 'awaiting_settlement', updated_at = ? WHERE package_id = ? AND state != 'awaiting_settlement'",
                            (now, p_id),
                        )
            return results

    def expire_strategy_positions(
        self,
        strategy_id: str,
        as_of_date: date,
        position_id: str | None = None,
        *,
        settlement: Any | None = None,
        broker_cash: Any | None = None,
        fees: Iterable[Any] | None = None,
        fee_coverage_complete: bool = True,
    ) -> int:
        """Mark past-expiration packages expired after broker confirms settlement."""
        with self._lock:
            before_query = "SELECT COUNT(*) FROM packages WHERE strategy_id = ? AND state = 'expired'"
            before_args = [strategy_id]
            if position_id is not None:
                before_query += " AND position_id = ?"
                before_args.append(position_id)
            before_count = self._connection.execute(before_query, tuple(before_args)).fetchone()[0]

            self.reconcile_settlement(
                strategy_id,
                as_of_date,
                position_id=position_id,
                settlement=settlement,
                broker_cash=broker_cash,
                fees=fees,
                fee_coverage_complete=fee_coverage_complete,
            )

            after_count = self._connection.execute(before_query, tuple(before_args)).fetchone()[0]
            return max(0, after_count - before_count)

    def flush_outbox(self, log_path: Path | None = None) -> int:
        """Dispatch pending outbox records to trades.jsonl atomically."""
        from trade_logger import record_trade_exit
        from strategies.runtime_records import resolve_record_path

        target_path = (
            Path(log_path)
            if log_path is not None
            else resolve_record_path("TRADE_LOG_PATH", "logs/trades.jsonl")
        )
        dispatched = 0
        now = datetime.now(timezone.utc).isoformat()
        with self._lock:
            rows = self._connection.execute(
                "SELECT outbox_id, event_type, payload FROM outbox WHERE status = 'pending' ORDER BY outbox_id"
            ).fetchall()
            for row in rows:
                oid = row["outbox_id"]
                data = json.loads(row["payload"])
                if row["event_type"] == "EXIT":
                    record_trade_exit(
                        trade_id=data.get("trade_id", ""),
                        strategy_name=data.get("strategy", ""),
                        exit_reason=data.get("exit_reason", "CASH_SETTLED"),
                        pnl=data.get("net_pnl")
                        if data.get("net_pnl") is not None
                        else (
                            (data.get("gross_pnl") or 0.0)
                            - (data.get("fees") or 0.0)
                        ),
                        gross_pnl=data.get("gross_pnl"),
                        fees=data.get("fees"),
                        net_pnl=data.get("net_pnl"),
                        log_path=target_path,
                        source=data.get("source", "broker_verified"),
                    )
                with self._connection:
                    self._connection.execute(
                        "UPDATE outbox SET status = 'dispatched', dispatched_at = ? WHERE outbox_id = ?",
                        (now, oid),
                    )
                dispatched += 1
        return dispatched

    def owned_quantities(self, strategy_id: str) -> dict[str, int]:
        """Return net confirmed fills attributable to one logical strategy."""
        with self._lock:
            rows = self._connection.execute(
                """SELECT l.asset_key, SUM(l.filled_signed_quantity) AS quantity
                   FROM legs AS l JOIN packages AS p USING (package_id)
                   WHERE p.strategy_id = ? AND p.state != 'expired'
                   GROUP BY l.asset_key""",
                (strategy_id,),
            ).fetchall()
        return {
            str(row["asset_key"]): int(row["quantity"])
            for row in rows
            if int(row["quantity"] or 0) != 0
        }

    def all_owned_quantities(self) -> dict[str, int]:
        """Return net confirmed fills across every strategy in the ledger."""
        with self._lock:
            rows = self._connection.execute(
                """SELECT l.asset_key, SUM(l.filled_signed_quantity) AS quantity
                   FROM legs AS l JOIN packages AS p USING (package_id)
                   WHERE p.state != 'expired' GROUP BY l.asset_key"""
            ).fetchall()
        return {
            str(row["asset_key"]): int(row["quantity"])
            for row in rows
            if int(row["quantity"] or 0) != 0
        }

    def active_opening_cash(self, strategy_id: str) -> float:
        """Return the sum of remaining position bases without merging entry lots."""
        return round(sum(self.position_opening_cash(p.position_id)
                         for p in self.open_positions(strategy_id)), 2)

    def open_baskets(self, strategy_id: str | None = None) -> tuple[BasketRecord, ...]:
        """Return active packages, optionally restricted to one strategy owner."""
        query = "SELECT * FROM packages WHERE state IN ('submitted','partial','open','closing','awaiting_settlement')"
        parameters: tuple[str, ...] = ()
        if strategy_id is not None:
            query += " AND strategy_id = ?"
            parameters = (strategy_id,)
        query += " ORDER BY created_at, package_id"
        with self._lock:
            packages = self._connection.execute(query, parameters).fetchall()
            return tuple(self._basket_from_row(package) for package in packages)

    def portfolio_defined_risk(self, strategy_id: str | None = None) -> float | None:
        """Sum independent position risk and conservative unfilled reservations."""
        positions = self.open_positions(strategy_id)
        attributed: dict[str, int] = defaultdict(int)
        total = 0.0
        for position in positions:
            quantities, costs, _, valid = self._position_accounting(position.position_id)
            if not valid:
                return None
            for key, quantity in quantities.items():
                attributed[key] += quantity
            if quantities:
                try:
                    shape = self._package_width_and_quantity(
                        [(json.loads(key), quantity) for key, quantity in quantities.items()]
                    )
                except (KeyError, TypeError, ValueError):
                    return None
                if shape is None:
                    return None
                total += max_defined_loss(*shape, -sum(costs.values()))
            for basket in self.position_baskets(position.position_id):
                if self.is_inflight(basket) and basket.intent in {"open", "add"}:
                    remaining = []
                    try:
                        for leg in basket.legs:
                            quantity = int(leg["requested_signed_quantity"]) - int(leg["filled_signed_quantity"])
                            if quantity:
                                remaining.append((json.loads(str(leg["asset_key"])), quantity))
                    except (KeyError, TypeError, ValueError):
                        return None
                    if remaining:
                        shape = self._package_width_and_quantity(remaining)
                        if shape is None:
                            return None
                        total += shape[0] * shape[1] * 100
        # Never hide unlinked legacy fills behind attributed positions.
        actual = self.owned_quantities(strategy_id) if strategy_id is not None else self.all_owned_quantities()
        if actual != {key: quantity for key, quantity in attributed.items() if quantity}:
            return None
        if any(b.position_id is None and self.is_inflight(b) for b in self.open_baskets(strategy_id)):
            return None
        return round(total, 2)

    @classmethod
    def _package_width_and_quantity(
        cls,
        legs: list[tuple[dict[str, Any], int]],
    ) -> tuple[float, int] | None:
        if len({(asset.get("symbol"), asset.get("expiration")) for asset, _ in legs}) != 1:
            return None
        if len(legs) == 4:
            return cls._condor_width_and_quantity(legs)
        if len(legs) == 2:
            return cls._vertical_width_and_quantity(legs)
        return None

    @staticmethod
    def _vertical_width_and_quantity(
        legs: list[tuple[dict[str, Any], int]],
    ) -> tuple[float, int] | None:
        if len(legs) != 2:
            return None
        leg1, leg2 = legs[0], legs[1]
        right1 = str(leg1[0].get("right", "")).lower()
        right2 = str(leg2[0].get("right", "")).lower()
        if ("put" in right1 and "put" not in right2) or ("call" in right1 and "call" not in right2):
            return None
        qty1 = int(leg1[1])
        qty2 = int(leg2[1])
        if abs(qty1) != abs(qty2) or abs(qty1) <= 0 or (qty1 > 0 and qty2 > 0) or (qty1 < 0 and qty2 < 0):
            return None
        width = abs(float(leg1[0]["strike"]) - float(leg2[0]["strike"]))
        return (width, abs(qty1)) if width > 0 else None

    @staticmethod
    def _condor_width_and_quantity(
        legs: list[tuple[dict[str, Any], int]],
    ) -> tuple[float, int] | None:
        if len(legs) != 4:
            return None
        puts = sorted(
            (item for item in legs if "put" in item[0]["right"]),
            key=lambda item: item[0]["strike"],
        )
        calls = sorted(
            (item for item in legs if "call" in item[0]["right"]),
            key=lambda item: item[0]["strike"],
        )
        quantities_abs = {abs(quantity) for _, quantity in legs}
        if len(puts) != 2 or len(calls) != 2 or len(quantities_abs) != 1:
            return None
        if float(puts[1][0]["strike"]) > float(calls[0][0]["strike"]):
            return None
        quantity = quantities_abs.pop()
        if quantity <= 0 or not (
            puts[0][1] == quantity
            and puts[1][1] == -quantity
            and calls[0][1] == -quantity
            and calls[1][1] == quantity
        ):
            return None
        width = max(
            float(puts[1][0]["strike"]) - float(puts[0][0]["strike"]),
            float(calls[1][0]["strike"]) - float(calls[0][0]["strike"]),
        )
        return (width, quantity) if width > 0 else None

    @staticmethod
    def _is_expired_asset_key(key: str, as_of_date: date) -> bool:
        try:
            decoded = json.loads(key)
            return datetime.strptime(str(decoded["expiration"])[:10], "%Y-%m-%d").date() < as_of_date
        except (KeyError, TypeError, ValueError, json.JSONDecodeError):
            return False

    def _refresh_package(self, package_id: str) -> BasketRecord:
        package = self._connection.execute(
            "SELECT * FROM packages WHERE package_id = ?", (package_id,)
        ).fetchone()
        legs = self._connection.execute(
            "SELECT * FROM legs WHERE package_id = ?", (package_id,)
        ).fetchall()
        if package is None or not legs:
            raise KeyError(f"unknown package_id: {package_id}")
        cash = sum(
            -int(leg["filled_signed_quantity"]) * float(leg["fill_price"] or 0.0) * 100.0
            for leg in legs
        )
        fully_filled = all(
            int(leg["filled_signed_quantity"]) == int(leg["requested_signed_quantity"])
            for leg in legs
        )
        any_fill = any(int(leg["filled_signed_quantity"]) for leg in legs)
        balanced = sum(int(leg["filled_signed_quantity"]) for leg in legs) == 0
        intent = str(package["intent"])
        strategy_id = str(package["strategy_id"])
        state = (
            ("open" if intent in {"open", "add"} else "closed")
            if fully_filled and balanced
            else ("partial" if any_fill else "submitted")
        )
        opening_cash = cash if intent in {"open", "add"} and any_fill else None
        closing_cash = cash if intent in {"trim", "close"} and any_fill else None
        now = datetime.now(timezone.utc).isoformat()
        self._connection.execute(
            """UPDATE packages SET state = ?, opening_cash_flow = ?,
               closing_cash_flow = ?, updated_at = ? WHERE package_id = ?""",
            (state, opening_cash, closing_cash, now, package_id),
        )
        position_id = package["position_id"]
        if state == "closed" and intent in {"trim", "close"}:
            if position_id and not self.position_quantities(str(position_id)) and not any(
                self.is_inflight(b) for b in self.position_baskets(str(position_id))
                if b.package_id != package_id
            ):
                self._connection.execute(
                    """UPDATE packages SET state = 'closed', updated_at = ?
                       WHERE position_id = ? AND intent IN ('open','add')
                       AND state IN ('open','partial')""", (now, position_id),
                )
            if not self.owned_quantities(strategy_id) and not any(
                self.is_inflight(b) for b in self.open_baskets(strategy_id)
                if b.package_id != package_id
            ):
                self._connection.execute(
                    """UPDATE packages SET state = 'closed', updated_at = ?
                       WHERE strategy_id = ? AND intent IN ('open','add')
                       AND state IN ('open','partial')""", (now, strategy_id),
                )
        return self.get_basket(package_id)

    def _basket_from_row(self, package: sqlite3.Row) -> BasketRecord:
        legs = self._connection.execute(
            "SELECT * FROM legs WHERE package_id = ? ORDER BY order_id",
            (package["package_id"],),
        ).fetchall()
        row = dict(package)
        return BasketRecord(
            package_id=str(row["package_id"]),
            strategy_id=str(row["strategy_id"]),
            intent=str(row["intent"]),
            state=str(row["state"]),
            opening_cash_flow=row.get("opening_cash_flow"),
            closing_cash_flow=row.get("closing_cash_flow"),
            legs=tuple(dict(leg) for leg in legs),
            broker_parent_id=row.get("broker_parent_id"),
            broker_tag=row.get("broker_tag"),
            duration=row.get("duration"),
            limit_price=row.get("limit_price"),
            trade_id=row.get("trade_id"),
            created_at=row.get("created_at"),
            updated_at=row.get("updated_at"),
            replacement_of=row.get("replacement_of"),
            reprice_count=int(row.get("reprice_count") or 0),
            position_id=row.get("position_id"),
            terminal_status=row.get("terminal_status"),
            submission_started=row.get("submission_started"),
        )

    @staticmethod
    def _side_for(intent: str, signed_quantity: int) -> str:
        if intent in {"open", "add"}:
            return "buy_to_open" if signed_quantity > 0 else "sell_to_open"
        return "buy_to_close" if signed_quantity > 0 else "sell_to_close"

    @staticmethod
    def _order_identifier(order: Any) -> str | None:
        identifier = getattr(order, "identifier", None) or getattr(order, "id", None)
        return str(identifier) if identifier not in (None, "") else None

    @staticmethod
    def _coerce_leg(leg: LedgerLeg | Mapping[str, Any]) -> LedgerLeg:
        if isinstance(leg, LedgerLeg):
            result = leg
        elif hasattr(leg, "order_id") and hasattr(leg, "asset_key") and hasattr(leg, "requested_signed_quantity"):
            result = LedgerLeg(
                order_id=str(getattr(leg, "order_id")),
                asset_key=str(getattr(leg, "asset_key")),
                requested_signed_quantity=int(getattr(leg, "requested_signed_quantity")),
                broker_order_id=(str(getattr(leg, "broker_order_id")) if getattr(leg, "broker_order_id", None) else None),
                broker_parent_id=(str(getattr(leg, "broker_parent_id")) if getattr(leg, "broker_parent_id", None) else None),
            )
        else:
            local_id = leg.get("local_order_id", leg.get("order_id"))
            result = LedgerLeg(
                order_id=str(local_id),
                asset_key=str(leg["asset_key"]),
                requested_signed_quantity=int(leg["requested_signed_quantity"]),
                broker_order_id=(str(leg["broker_order_id"]) if leg.get("broker_order_id") else None),
                broker_parent_id=(str(leg["broker_parent_id"]) if leg.get("broker_parent_id") else None),
            )
        if not result.order_id or not result.asset_key or result.requested_signed_quantity == 0:
            raise ValueError("each ledger leg needs an id, asset key, and non-zero quantity")
        return result


__all__ = ["BasketRecord", "LedgerLeg", "PositionLedger", "PositionRecord", "asset_key", "resolve_ledger_path"]
