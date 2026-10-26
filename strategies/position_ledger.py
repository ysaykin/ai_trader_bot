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
                CREATE INDEX IF NOT EXISTS packages_strategy_state
                    ON packages(strategy_id, state);
                """
            )

    def record_submission(
        self,
        package_id: str,
        strategy_id: str,
        intent: str,
        legs: Iterable[LedgerLeg | Mapping[str, Any]],
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
        now = datetime.now(timezone.utc).isoformat()
        with self._lock, self._connection:
            self._connection.execute(
                "INSERT INTO packages VALUES (?, ?, ?, 'submitted', NULL, NULL, ?, ?)",
                (package_id, strategy_id, normalized_intent, now, now),
            )
            self._connection.executemany(
                """INSERT INTO legs
                   (order_id, package_id, asset_key, requested_signed_quantity,
                    filled_signed_quantity, fill_price, status)
                   VALUES (?, ?, ?, ?, 0, NULL, 'submitted')""",
                [
                    (leg.order_id, package_id, leg.asset_key, leg.requested_signed_quantity)
                    for leg in normalized
                ],
            )

    def record_fill(
        self,
        order_id: str,
        signed_quantity: int,
        fill_price: float,
        *,
        status: str = "filled",
    ) -> None:
        """Add a confirmed fill event and update weighted price and package cash."""
        if isinstance(signed_quantity, bool) or not isinstance(signed_quantity, int):
            raise ValueError("signed_quantity must be an integer")
        if signed_quantity == 0:
            raise ValueError("signed_quantity must be non-zero")
        price = float(fill_price)
        if not math.isfinite(price) or price < 0:
            raise ValueError("fill_price must be finite and non-negative")
        with self._lock, self._connection:
            leg = self._connection.execute(
                "SELECT * FROM legs WHERE order_id = ?", (str(order_id),)
            ).fetchone()
            if leg is None:
                raise KeyError(f"unknown order_id: {order_id}")
            requested = int(leg["requested_signed_quantity"])
            if requested * signed_quantity <= 0:
                raise ValueError("fill direction does not match requested direction")
            previous_quantity = int(leg["filled_signed_quantity"])
            filled = previous_quantity + signed_quantity
            if abs(filled) > abs(requested):
                raise ValueError("fill exceeds requested quantity")
            previous_price = leg["fill_price"]
            weighted_price = (
                price
                if previous_quantity == 0
                else (
                    abs(previous_quantity) * float(previous_price)
                    + abs(signed_quantity) * price
                )
                / abs(filled)
            )
            self._connection.execute(
                """UPDATE legs SET filled_signed_quantity = ?, fill_price = ?, status = ?
                   WHERE order_id = ?""",
                (filled, weighted_price, status, str(order_id)),
            )
            self._refresh_package(str(leg["package_id"]))

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
                    "SELECT asset_key FROM legs WHERE package_id = ?",
                    (package_id,),
                ).fetchall()
                if not legs:
                    continue
                if all(self._is_expired_asset_key(str(leg["asset_key"]), as_of_date) for leg in legs):
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
                   WHERE p.state != 'expired'
                   GROUP BY l.asset_key"""
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
        remaining_qty = max(abs(qty) for qty in quantities.values())
        total_cash = 0.0
        opened_qty = 0
        for basket in self.open_baskets(strategy_id):
            if basket.state == "open" and basket.intent in {"open", "add"}:
                basket_qty = max(
                    (abs(int(leg["filled_signed_quantity"])) for leg in basket.legs),
                    default=0,
                )
                if basket_qty > 0:
                    opened_qty += basket_qty
                    total_cash += float(basket.opening_cash_flow or 0.0)
        if opened_qty <= 0:
            return 0.0
        return total_cash * min(remaining_qty / opened_qty, 1.0)

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
            result = []
            for package in packages:
                legs = self._connection.execute(
                    "SELECT * FROM legs WHERE package_id = ? ORDER BY order_id",
                    (package["package_id"],),
                ).fetchall()
                result.append(
                    BasketRecord(
                        package_id=str(package["package_id"]),
                        strategy_id=str(package["strategy_id"]),
                        intent=str(package["intent"]),
                        state=str(package["state"]),
                        opening_cash_flow=package["opening_cash_flow"],
                        closing_cash_flow=package["closing_cash_flow"],
                        legs=tuple(dict(leg) for leg in legs),
                    )
                )
        return tuple(result)

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
                    pending_legs: list[tuple[dict[str, Any], int]] = []
                    for leg in basket.legs:
                        try:
                            decoded = json.loads(str(leg["asset_key"]))
                            pending_legs.append((decoded, int(leg["requested_signed_quantity"])))
                        except (KeyError, TypeError, ValueError, json.JSONDecodeError):
                            return None
                    basket_shape = self._condor_width_and_quantity(pending_legs)
                    if basket_shape is None:
                        return None
                    width, quantity = basket_shape
                    # Conservative full wing width before an entry or add fills.
                    total += max_defined_loss(width, quantity, 0.0)
        return total

    @staticmethod
    def _condor_width_and_quantity(
        legs: list[tuple[dict[str, Any], int]],
    ) -> tuple[float, int] | None:
        if len(legs) != 4:
            return None
        puts = sorted((item for item in legs if "put" in item[0]["right"]), key=lambda item: item[0]["strike"])
        calls = sorted((item for item in legs if "call" in item[0]["right"]), key=lambda item: item[0]["strike"])
        if len(puts) != 2 or len(calls) != 2:
            return None
        quantities_abs = {abs(quantity) for _, quantity in legs}
        if len(quantities_abs) != 1:
            return None
        quantity = quantities_abs.pop()
        if quantity <= 0:
            return None
        if not (
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
        if width <= 0:
            return None
        return width, quantity

    @staticmethod
    def _is_expired_asset_key(key: str, as_of_date: date) -> bool:
        try:
            decoded = json.loads(key)
            expiration = str(decoded["expiration"])[:10]
            return datetime.strptime(expiration, "%Y-%m-%d").date() < as_of_date
        except (KeyError, TypeError, ValueError, json.JSONDecodeError):
            return False

    def _refresh_package(self, package_id: str) -> None:
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
        if fully_filled and balanced:
            state = "open" if intent in {"open", "add"} else "closed"
        else:
            state = "partial" if any_fill else "submitted"
        opening_cash = cash if intent in {"open", "add"} and any_fill else None
        closing_cash = cash if intent in {"trim", "close"} and any_fill else None
        now = datetime.now(timezone.utc).isoformat()
        self._connection.execute(
            """UPDATE packages
               SET state = ?, opening_cash_flow = ?, closing_cash_flow = ?, updated_at = ?
               WHERE package_id = ?""",
            (
                state,
                opening_cash,
                closing_cash,
                now,
                package_id,
            ),
        )
        if state == "closed" and intent in {"trim", "close"} and not self.owned_quantities(strategy_id):
            inflight = self._connection.execute(
                """SELECT 1 FROM packages
                   WHERE strategy_id = ? AND state IN ('submitted','partial','closing')
                   LIMIT 1""",
                (strategy_id,),
            ).fetchone()
            if inflight is None:
                self._connection.execute(
                    """UPDATE packages SET state = 'closed', updated_at = ?
                       WHERE strategy_id = ? AND state = 'open'""",
                    (now, strategy_id),
                )

    @staticmethod
    def _coerce_leg(leg: LedgerLeg | Mapping[str, Any]) -> LedgerLeg:
        if isinstance(leg, LedgerLeg):
            result = leg
        else:
            result = LedgerLeg(
                order_id=str(leg["order_id"]),
                asset_key=str(leg["asset_key"]),
                requested_signed_quantity=int(leg["requested_signed_quantity"]),
            )
        if not result.order_id or not result.asset_key or result.requested_signed_quantity == 0:
            raise ValueError("each ledger leg needs an id, asset key, and non-zero quantity")
        return result


__all__ = ["BasketRecord", "LedgerLeg", "PositionLedger", "asset_key"]
