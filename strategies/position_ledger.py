"""Transactional ownership ledger for multi-leg option packages."""

from __future__ import annotations

import json
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

    def __init__(self, path: str | Path = ":memory:") -> None:
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).expanduser().parent.mkdir(parents=True, exist_ok=True)
        self._connection = sqlite3.connect(self.path, check_same_thread=False)
        self._connection.row_factory = sqlite3.Row
        self._lock = threading.RLock()
        self._initialize()

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
    ) -> None:
        """Atomically record a broker package before considering any leg owned."""
        normalized = tuple(self._coerce_leg(leg) for leg in legs)
        if len(normalized) != 4:
            raise ValueError("an iron-condor package must contain exactly four legs")
        if len({leg.order_id for leg in normalized}) != 4:
            raise ValueError("package order identifiers must be unique")
        if len({leg.asset_key for leg in normalized}) != 4:
            raise ValueError("package assets must be unique")
        if not package_id or not strategy_id:
            raise ValueError("package_id and strategy_id are required")
        normalized_intent = intent.strip().lower()
        if normalized_intent not in {"open", "add", "trim", "close"}:
            raise ValueError(f"unsupported package intent: {intent}")
        price = None if limit_price is None else float(limit_price)
        if price is not None and (not math.isfinite(price) or price < 0):
            raise ValueError("limit_price must be finite and non-negative")
        now = datetime.now(timezone.utc).isoformat()
        with self._lock, self._connection:
            self._connection.execute(
                """INSERT INTO packages
                   (package_id, strategy_id, intent, state, opening_cash_flow,
                    closing_cash_flow, created_at, updated_at, broker_parent_id,
                    broker_tag, duration, limit_price, trade_id, replacement_of,
                    reprice_count)
                   VALUES (?, ?, ?, 'submitted', NULL, NULL, ?, ?, NULL, ?, ?, ?,
                           NULL, ?, ?)""",
                (
                    package_id,
                    strategy_id,
                    normalized_intent,
                    now,
                    now,
                    broker_tag,
                    duration.lower(),
                    price,
                    replacement_of,
                    int(reprice_count),
                ),
            )
            self._connection.executemany(
                """INSERT INTO legs
                   (order_id, package_id, asset_key, requested_signed_quantity,
                    filled_signed_quantity, fill_price, status, local_order_id,
                    broker_order_id, broker_parent_id, side, created_at, updated_at)
                   VALUES (?, ?, ?, ?, 0, NULL, 'submitted', ?, ?, ?, ?, ?, ?)""",
                [
                    (
                        leg.order_id,
                        package_id,
                        leg.asset_key,
                        leg.requested_signed_quantity,
                        leg.order_id,
                        leg.broker_order_id,
                        leg.broker_parent_id,
                        self._side_for(normalized_intent, leg.requested_signed_quantity),
                        now,
                        now,
                    )
                    for leg in normalized
                ],
            )

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

    def record_terminal_status(self, package_id: str, status: str) -> None:
        """Record a broker-confirmed terminal state for a package."""
        normalized = status.strip().lower()
        if normalized not in TERMINAL_STATES:
            raise ValueError(f"non-terminal package state: {status}")
        with self._lock, self._connection:
            result = self._connection.execute(
                "UPDATE packages SET state = ?, updated_at = ? WHERE package_id = ?",
                (normalized, datetime.now(timezone.utc).isoformat(), package_id),
            )
            if result.rowcount != 1:
                raise KeyError(f"unknown package_id: {package_id}")

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
        query += " ORDER BY updated_at DESC LIMIT 1"
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
        remaining_qty = max(abs(quantity) for quantity in quantities.values())
        total_cash = 0.0
        opened_qty = 0
        with self._lock:
            rows = self._connection.execute(
                """SELECT * FROM packages WHERE strategy_id = ?
                   AND intent IN ('open','add') AND state = 'open'
                   AND opening_cash_flow IS NOT NULL""",
                (strategy_id,),
            ).fetchall()
        for row in rows:
            basket = self._basket_from_row(row)
            basket_qty = max(
                (abs(int(leg["filled_signed_quantity"])) for leg in basket.legs),
                default=0,
            )
            if basket_qty > 0:
                opened_qty += basket_qty
                total_cash += float(basket.opening_cash_flow or 0.0)
        return total_cash * min(remaining_qty / opened_qty, 1.0) if opened_qty else 0.0

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
                opening_credit = self.active_opening_cash(owner) if len(groups) == 1 else 0.0
                for legs in groups.values():
                    basket_shape = self._condor_width_and_quantity(legs)
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
                    basket_shape = self._condor_width_and_quantity(pending_legs)
                    if basket_shape is None:
                        return None
                    width, quantity = basket_shape
                    total += max_defined_loss(width, quantity, 0.0)
        return total

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
        if state == "closed" and intent in {"trim", "close"} and not self.owned_quantities(strategy_id):
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
        return BasketRecord(
            package_id=str(package["package_id"]),
            strategy_id=str(package["strategy_id"]),
            intent=str(package["intent"]),
            state=str(package["state"]),
            opening_cash_flow=package["opening_cash_flow"],
            closing_cash_flow=package["closing_cash_flow"],
            legs=tuple(dict(leg) for leg in legs),
            broker_parent_id=package["broker_parent_id"],
            broker_tag=package["broker_tag"],
            duration=package["duration"],
            limit_price=package["limit_price"],
            trade_id=package["trade_id"],
            created_at=package["created_at"],
            updated_at=package["updated_at"],
            replacement_of=package["replacement_of"],
            reprice_count=int(package["reprice_count"] or 0),
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
