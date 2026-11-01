"""Transactional ownership ledger for multi-leg option packages."""

from __future__ import annotations

import json
import fcntl
from contextlib import contextmanager
import math
import sqlite3
import threading
from collections import defaultdict
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

from strategies.iron_condor_math import max_defined_loss

ACTIVE_STATES = frozenset({"submitted", "partial", "open", "closing"})
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


def asset_key(asset: Any) -> str:
    """Return a stable key for a LumiBot option asset or asset-like object."""
    payload = {
        "expiration": str(getattr(asset, "expiration", "")),
        "right": str(getattr(asset, "right", "")).lower(),
        "strike": float(getattr(asset, "strike", 0.0)),
        "symbol": str(getattr(asset, "symbol", "")),
    }
    return json.dumps(payload, separators=(",", ":"), sort_keys=True)


class PositionLedger:
    """Persist package submissions and confirmed fills in SQLite transactions."""

    def __init__(self, path: str | Path = ":memory:", *, read_only: bool = False) -> None:
        self.path = str(path)
        if not read_only and self.path != ":memory:":
            Path(self.path).expanduser().parent.mkdir(parents=True, exist_ok=True)
        if read_only and self.path != ":memory:":
            database = Path(self.path).expanduser().resolve().as_uri() + "?mode=ro"
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
        lock_path = str(Path(self.path).expanduser().resolve()) + ".execution.lock"
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
                    risks = {owner: self.portfolio_defined_risk(owner) for owner in owners}
                    quantities = {owner: self.owned_quantities(owner) for owner in owners}
                    portfolio = None if any(value is None for value in risks.values()) else sum(risks.values())
                    return {"strategy_risk": risks, "portfolio_risk": portfolio, "owned_quantities": quantities}
                finally:
                    self._connection.rollback()
            except sqlite3.OperationalError:
                return {"strategy_risk": {}, "portfolio_risk": 0.0, "owned_quantities": {}}

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
                CREATE INDEX IF NOT EXISTS legs_broker_order
                    ON legs(broker_order_id);
                CREATE INDEX IF NOT EXISTS legs_broker_parent
                    ON legs(broker_parent_id);
                """
            )

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
    ) -> None:
        """Atomically record a broker package before considering any leg owned."""
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
                "trade_id": None,
                "replacement_of": replacement_of,
                "reprice_count": int(reprice_count),
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
                rows = self._connection.execute(
                    """SELECT * FROM legs WHERE package_id = ? AND asset_key = ?
                       AND lower(side) = ?""",
                    (package_id, key, child_side),
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
                    query += " AND lower(side) = ?"
                    parameters.append(str(side).lower())
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
            result = self._connection.execute(
                "UPDATE packages SET state = ?, updated_at = ? WHERE package_id = ?",
                (normalized, ts, package_id),
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
                balanced = sum(int(leg["filled_signed_quantity"]) for leg in legs) == 0
                if any_fill and balanced:
                    self._connection.execute(
                        "UPDATE packages SET state = 'open', updated_at = ? WHERE package_id = ?",
                        (now, package_id),
                    )
                else:
                    self._connection.execute(
                        "UPDATE packages SET state = ?, updated_at = ? WHERE package_id = ?",
                        (normalized, now, package_id),
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

    def set_trade_id(self, package_id: str, trade_id: str) -> None:
        """Attach the durable journal identity to a package."""
        with self._lock, self._connection:
            result = self._connection.execute(
                """UPDATE packages SET trade_id = ?, updated_at = ?
                   WHERE package_id = ?""",
                (
                    str(trade_id),
                    datetime.now(timezone.utc).isoformat(),
                    package_id,
                ),
            )
            if result.rowcount != 1:
                raise KeyError(f"unknown package_id: {package_id}")

    def active_trade_id(self, strategy_id: str) -> str | None:
        """Return the journal ID associated with the currently owned trade."""
        if not self.owned_quantities(strategy_id):
            return None
        with self._lock:
            row = self._connection.execute(
                """SELECT trade_id FROM packages WHERE strategy_id = ?
                   AND trade_id IS NOT NULL ORDER BY updated_at DESC LIMIT 1""",
                (strategy_id,),
            ).fetchone()
        return str(row["trade_id"]) if row is not None else None

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

    def expire_strategy_positions(self, strategy_id: str, as_of_date: date) -> int:
        """Mark past-expiration packages expired after broker confirms settlement."""
        updated = 0
        now = datetime.now(timezone.utc).isoformat()
        with self._lock, self._connection:
            packages = self._connection.execute(
                "SELECT package_id FROM packages WHERE strategy_id = ? AND state != 'expired'",
                (strategy_id,),
            ).fetchall()
            for package in packages:
                package_id = str(package["package_id"])
                legs = self._connection.execute(
                    "SELECT asset_key FROM legs WHERE package_id = ?", (package_id,)
                ).fetchall()
                if legs and all(
                    self._is_expired_asset_key(str(leg["asset_key"]), as_of_date)
                    for leg in legs
                ):
                    self._connection.execute(
                        "UPDATE packages SET state = 'expired', updated_at = ? WHERE package_id = ?",
                        (now, package_id),
                    )
                    updated += 1
        return updated

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
        """Return opening cash attributable to the remaining open contracts."""
        quantities = self.owned_quantities(strategy_id)
        if not quantities:
            return 0.0
        active_groups: dict[tuple[str, str], int] = defaultdict(int)
        for key, qty in quantities.items():
            if qty != 0:
                try:
                    decoded = json.loads(key)
                    active_groups[(decoded["symbol"], decoded["expiration"])] = max(
                        active_groups[(decoded["symbol"], decoded["expiration"])], abs(qty)
                    )
                except Exception:
                    pass
        if not active_groups:
            return 0.0

        total_cash = 0.0
        with self._lock:
            rows = self._connection.execute(
                """SELECT * FROM packages WHERE strategy_id = ?
                   AND intent IN ('open','add') AND state IN ('open', 'partial')
                   AND opening_cash_flow IS NOT NULL""",
                (strategy_id,),
            ).fetchall()

        for grp, remaining_qty in active_groups.items():
            grp_cash = 0.0
            grp_opened_qty = 0
            for row in rows:
                basket = self._basket_from_row(row)
                basket_groups = set()
                for leg in basket.legs:
                    try:
                        ak = json.loads(leg["asset_key"])
                        basket_groups.add((ak["symbol"], ak["expiration"]))
                    except Exception:
                        pass
                if grp in basket_groups:
                    b_qty = max(
                        (abs(int(leg["filled_signed_quantity"])) for leg in basket.legs),
                        default=0,
                    )
                    if b_qty > 0:
                        grp_opened_qty += b_qty
                        grp_cash += float(basket.opening_cash_flow or 0.0)
            if grp_opened_qty > 0:
                total_cash += grp_cash * min(remaining_qty / grp_opened_qty, 1.0)

        return round(total_cash, 2)

    def open_baskets(self, strategy_id: str | None = None) -> tuple[BasketRecord, ...]:
        """Return active packages, optionally restricted to one strategy owner."""
        query = "SELECT * FROM packages WHERE state IN ('submitted','partial','open','closing')"
        parameters: tuple[str, ...] = ()
        if strategy_id is not None:
            query += " AND strategy_id = ?"
            parameters = (strategy_id,)
        query += " ORDER BY created_at, package_id"
        with self._lock:
            packages = self._connection.execute(query, parameters).fetchall()
            return tuple(self._basket_from_row(package) for package in packages)

    def portfolio_defined_risk(self, strategy_id: str | None = None) -> float | None:
        """Return filled and pending defined risk, or ``None`` if any basket is imbalanced."""
        query = "SELECT DISTINCT strategy_id FROM packages"
        parameters: tuple[str, ...] = ()
        if strategy_id is not None:
            query += " WHERE strategy_id = ?"
            parameters = (strategy_id,)
        with self._lock:
            strategy_ids = [
                str(row["strategy_id"])
                for row in self._connection.execute(query, parameters).fetchall()
            ]
        total = 0.0
        for owner in strategy_ids:
            quantities = self.owned_quantities(owner)
            if quantities:
                groups: dict[tuple[str, str], list[tuple[dict[str, Any], int]]] = defaultdict(list)
                for key, quantity in quantities.items():
                    try:
                        decoded = json.loads(key)
                        groups[(decoded["symbol"], decoded["expiration"])].append((decoded, quantity))
                    except (KeyError, TypeError, ValueError, json.JSONDecodeError):
                        return None
                with self._lock:
                    rows = self._connection.execute(
                        """SELECT l.asset_key, l.filled_signed_quantity, l.fill_price
                           FROM legs l JOIN packages p ON p.package_id = l.package_id
                           WHERE p.strategy_id = ? AND p.intent IN ('open','add')
                           AND p.state IN ('open','partial')""", (owner,),
                    ).fetchall()
                for group, legs in groups.items():
                    cash = 0.0
                    opened: dict[str, int] = defaultdict(int)
                    for row in rows:
                        asset = json.loads(row["asset_key"])
                        if (asset["symbol"], asset["expiration"]) == group:
                            qty = int(row["filled_signed_quantity"])
                            opened[row["asset_key"]] += qty
                            cash -= qty * float(row["fill_price"] or 0.0) * 100
                    opened_qty = max((abs(q) for q in opened.values()), default=0)
                    remaining_qty = max(abs(q) for _, q in legs)
                    opening_credit = cash * min(remaining_qty / opened_qty, 1.0) if opened_qty else 0.0
                    basket_shape = self._package_width_and_quantity(legs)
                    if basket_shape is None:
                        return None
                    width, quantity = basket_shape
                    total += max_defined_loss(width, quantity, opening_credit)
            for basket in self.open_baskets(owner):
                if basket.state == "submitted" and basket.intent in {"open", "add"}:
                    pending_legs = []
                    for leg in basket.legs:
                        try:
                            pending_legs.append(
                                (json.loads(str(leg["asset_key"])), int(leg["requested_signed_quantity"]))
                            )
                        except (KeyError, TypeError, ValueError, json.JSONDecodeError):
                            return None
                    basket_shape = self._package_width_and_quantity(pending_legs)
                    if basket_shape is None:
                        return None
                    width, quantity = basket_shape
                    total += max_defined_loss(width, quantity, 0.0)
        return total

    @classmethod
    def _package_width_and_quantity(
        cls,
        legs: list[tuple[dict[str, Any], int]],
    ) -> tuple[float, int] | None:
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
        if state == "closed" and intent in {"trim", "close"}:
            pkg_legs = self._connection.execute(
                "SELECT asset_key FROM legs WHERE package_id = ?", (package_id,)
            ).fetchall()
            target_groups = set()
            for pleg in pkg_legs:
                try:
                    ak = json.loads(pleg["asset_key"])
                    target_groups.add((ak["symbol"], ak["expiration"]))
                except Exception:
                    pass

            owned = self.owned_quantities(strategy_id)
            for grp in target_groups:
                grp_owned = sum(
                    abs(qty) for ak_str, qty in owned.items()
                    if json.loads(ak_str).get("symbol") == grp[0] and json.loads(ak_str).get("expiration") == grp[1]
                ) if owned else 0
                if grp_owned == 0:
                    inflight_rows = self._connection.execute(
                        """SELECT l.asset_key FROM packages p
                           JOIN legs l ON l.package_id = p.package_id
                           WHERE p.strategy_id = ? AND p.state IN ('submitted','partial','closing')
                           AND p.package_id != ?""",
                        (strategy_id, package_id),
                    ).fetchall()
                    grp_inflight = any(
                        json.loads(r["asset_key"]).get("symbol") == grp[0]
                        and json.loads(r["asset_key"]).get("expiration") == grp[1]
                        for r in inflight_rows
                    )
                    if not grp_inflight:
                        candidates = self._connection.execute(
                            """SELECT DISTINCT p.package_id FROM packages p
                               JOIN legs l ON l.package_id = p.package_id
                               WHERE p.strategy_id = ? AND p.intent IN ('open','add')
                               AND p.state IN ('open','partial')""",
                            (strategy_id,),
                        ).fetchall()
                        for cand in candidates:
                            c_id = cand["package_id"]
                            c_legs = self._connection.execute(
                                "SELECT asset_key FROM legs WHERE package_id = ?", (c_id,)
                            ).fetchall()
                            c_groups = {
                                (json.loads(cl["asset_key"]).get("symbol"), json.loads(cl["asset_key"]).get("expiration"))
                                for cl in c_legs
                            }
                            if grp in c_groups:
                                self._connection.execute(
                                    """UPDATE packages SET state = 'closed', updated_at = ?
                                       WHERE package_id = ?""",
                                    (now, c_id),
                                )
            if not self.owned_quantities(strategy_id):
                inflight = self._connection.execute(
                    """SELECT 1 FROM packages WHERE strategy_id = ?
                       AND state IN ('submitted','partial','closing') LIMIT 1""",
                    (strategy_id,),
                ).fetchone()
                if inflight is None:
                    self._connection.execute(
                        """UPDATE packages SET state = 'closed', updated_at = ?
                           WHERE strategy_id = ? AND state = 'open'""",
                        (now, strategy_id),
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


__all__ = ["BasketRecord", "LedgerLeg", "PositionLedger", "asset_key"]
