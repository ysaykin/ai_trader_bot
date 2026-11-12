"""Explicitly reconcile filled manual packages without attaching a strategy."""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
import hashlib
import json
import math
import re
from typing import Any, Mapping

from strategies.position_ledger import PositionLedger
from strategies.option_payoff import covered_option_loss
from strategies.runtime_records import RecordScope

MANUAL_OWNER = "MANUAL"


def _integer(value: Any, *, positive: bool = False) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        raise ValueError("Expected an integral broker quantity or identity")
    try:
        number = Decimal(str(value))
    except InvalidOperation as error:
        raise ValueError("Expected an integral broker quantity or identity") from error
    if not number.is_finite() or number != number.to_integral_value() or positive and number <= 0:
        raise ValueError("Expected an integral broker quantity or identity")
    return int(number)


@dataclass(frozen=True)
class ManualLeg:
    broker_id: str
    symbol: str
    asset_key: str
    quantity: int
    price: float
    side: str


@dataclass(frozen=True)
class ManualOrder:
    broker_id: str
    created_at: str
    legs: tuple[ManualLeg, ...]

    @property
    def package_id(self) -> str:
        return f"manual:{self.broker_id}"


def parse_manual_order(order: Mapping[str, Any]) -> ManualOrder:
    """Accept only complete, untagged, defined-risk opening executions."""
    if order.get("class") != "multileg" or order.get("status") != "filled" or order.get("tag"):
        raise ValueError("Manual import requires an untagged, filled multileg order")
    parent_id = str(_integer(order.get("id"), positive=True))
    if _integer(order.get("quantity"), positive=True) != _integer(order.get("exec_quantity"), positive=True):
        raise ValueError("Parent execution is incomplete")
    created = datetime.fromisoformat(str(order.get("create_date", "")).replace("Z", "+00:00"))
    if created.tzinfo is None:
        raise ValueError("Broker creation time must include its timezone")
    rows = order.get("leg")
    if not isinstance(rows, list) or len(rows) not in (2, 4):
        raise ValueError("Manual import requires two or four option legs")
    legs = []
    for row in rows:
        if not isinstance(row, dict) or row.get("status") != "filled":
            raise ValueError("Every manual leg must have confirmed fills")
        side = row.get("side")
        if side not in {"buy_to_open", "sell_to_open"}:
            raise ValueError("Manual import accepts opening orders only")
        quantity = _integer(row.get("quantity"), positive=True)
        if _integer(row.get("exec_quantity"), positive=True) != quantity:
            raise ValueError("Manual leg execution is incomplete")
        symbol = str(row.get("option_symbol", ""))
        match = re.fullmatch(r"([A-Z.]+)(\d{6})([CP])(\d{8})", symbol)
        if match is None:
            raise ValueError("Invalid broker option symbol")
        underlying, expiration, right, strike = match.groups()
        contract = dict(symbol=underlying, expiration=datetime.strptime(expiration, "%y%m%d").date().isoformat(),
                        right="call" if right == "C" else "put", strike=int(strike) / 1000)
        raw_price = row.get("avg_fill_price")
        if isinstance(raw_price, bool) or not isinstance(raw_price, (int, float, str)):
            raise ValueError("Missing manual fill price")
        price = float(raw_price)
        if not math.isfinite(price) or price < 0 or contract["strike"] <= 0:
            raise ValueError("Invalid manual fill price or strike")
        legs.append(ManualLeg(str(_integer(row.get("id"), positive=True)), symbol,
                              json.dumps(contract, sort_keys=True, separators=(",", ":")),
                              quantity if side == "buy_to_open" else -quantity, price, side))
    if (len({leg.broker_id for leg in legs}) != len(legs)
            or parent_id in {leg.broker_id for leg in legs}
            or len({leg.asset_key for leg in legs}) != len(legs)):
        raise ValueError("Manual contracts and order identities must be unique")
    risk = covered_option_loss([(json.loads(leg.asset_key), leg.quantity) for leg in legs],
                               -sum(leg.quantity * leg.price * 100 for leg in legs))
    if risk is None:
        raise ValueError("Manual package must have defined risk")
    return ManualOrder(parent_id, created.astimezone(timezone.utc).isoformat(), tuple(sorted(legs, key=lambda leg: leg.broker_id)))


def import_manual_order(ledger: PositionLedger, *, order: Mapping[str, Any], broker_positions: list[dict[str, Any]],
                        scope: RecordScope, confirmed_manual: bool, apply: bool = False) -> dict[str, Any]:
    """Atomically retain explicit manual ownership, fills and provider evidence.

    The caller holds the execution gate while obtaining fresh account-specific
    order and position GETs. This function never submits or manages an order.
    """
    if confirmed_manual is not True:
        raise ValueError("Explicit manual confirmation is required")
    if scope != ledger.record_scope:
        raise ValueError("Broker and ledger scope differ")
    parsed = parse_manual_order(order)
    actual: dict[str, int] = defaultdict(int)
    for row in broker_positions:
        actual[str(row["symbol"])] += _integer(row["quantity"])
    with ledger._lock:
        connection = ledger._connection
        connection.execute("BEGIN IMMEDIATE" if apply else "BEGIN")
        try:
            existing = connection.execute("SELECT * FROM packages WHERE package_id=?", (parsed.package_id,)).fetchone()
            if existing is not None:
                evidence = ledger.get_broker_evidence(f"manual-order:{parsed.broker_id}")
                position = ledger.get_position(parsed.package_id)
                if (existing["strategy_id"] != MANUAL_OWNER or existing["broker_parent_id"] != parsed.broker_id
                        or position.settings.get("management_mode") != "manual" or evidence is None
                        or parse_manual_order(evidence["payload"]["raw_response"]) != parsed):
                    raise ValueError("Manual execution conflicts with existing ownership")
                recorded = {str(leg["broker_order_id"]): (str(leg["asset_key"]), int(leg["filled_signed_quantity"]), leg["fill_price"])
                            for leg in ledger.get_basket(parsed.package_id).legs}
                expected = {leg.broker_id: (leg.asset_key, leg.quantity, leg.price) for leg in parsed.legs}
                if recorded != expected:
                    raise ValueError("Manual fills conflict with retained execution")
            else:
                identities = [parsed.broker_id, *(leg.broker_id for leg in parsed.legs)]
                placeholders = ",".join("?" for _ in identities)
                if (connection.execute("SELECT 1 FROM packages WHERE broker_parent_id=?", (parsed.broker_id,)).fetchone()
                        or connection.execute(f"SELECT 1 FROM legs WHERE broker_order_id IN ({placeholders})", identities).fetchone()
                        or connection.execute("SELECT 1 FROM positions WHERE position_id=?", (parsed.package_id,)).fetchone()):
                    raise ValueError("Broker order is already assigned to ledger ownership")
                keys = {leg.asset_key for leg in parsed.legs}
                if any(ledger.is_inflight(basket) and any(leg["asset_key"] in keys for leg in basket.legs)
                       for basket in ledger.open_baskets()):
                    raise ValueError("Working orders make manual inventory ambiguous")
            owned = ledger.all_owned_quantities()
            for leg in parsed.legs:
                projected = owned.get(leg.asset_key, 0) + (0 if existing is not None else leg.quantity)
                if actual.get(leg.symbol, 0) != projected:
                    raise ValueError(f"Broker inventory differs on {leg.symbol}")
            result = dict(result="already_imported" if existing is not None else "eligible", owner=MANUAL_OWNER,
                          package_id=parsed.package_id, broker_parent_id=parsed.broker_id, contracts=len(parsed.legs))
            if apply and existing is None:
                now = datetime.now(timezone.utc).isoformat()
                settings = json.dumps(dict(management_mode="manual", confirmed_manual=True, broker_order_id=parsed.broker_id))
                connection.execute("INSERT INTO positions (position_id,strategy_id,trade_id,settings,created_at) VALUES (?,?,?,?,?)",
                                   (parsed.package_id, MANUAL_OWNER, parsed.package_id, settings, parsed.created_at))
                cash = round(sum(-leg.quantity * leg.price * 100 for leg in parsed.legs), 2)
                connection.execute(
                    "INSERT INTO packages (package_id,strategy_id,intent,state,opening_cash_flow,created_at,updated_at,"
                    "broker_parent_id,duration,trade_id,position_id,submission_started) VALUES (?,?,'open','open',?,?,?,?,?,?,?,1)",
                    (parsed.package_id, MANUAL_OWNER, cash, parsed.created_at, now, parsed.broker_id,
                     str(order.get("duration") or "day"), parsed.package_id, parsed.package_id))
                for leg in parsed.legs:
                    local_id = f"{parsed.package_id}:{leg.broker_id}"
                    connection.execute(
                        "INSERT INTO legs (order_id,package_id,asset_key,requested_signed_quantity,filled_signed_quantity,"
                        "fill_price,status,local_order_id,broker_order_id,broker_parent_id,side,created_at,updated_at) "
                        "VALUES (?,?,?,?,?,?,'filled',?,?,?,?,?,?)",
                        (local_id, parsed.package_id, leg.asset_key, leg.quantity, leg.quantity, leg.price,
                         local_id, leg.broker_id, parsed.broker_id, leg.side, parsed.created_at, now))
                payload = json.dumps(dict(scope=scope.to_dict(), confirmed_manual=True, owner=MANUAL_OWNER,
                                          raw_response=order, broker_positions=broker_positions), sort_keys=True, allow_nan=False)
                connection.execute("INSERT INTO broker_evidence VALUES (?,?,?,?,?,?,?,?,?,?)",
                                   (f"manual-order:{parsed.broker_id}", scope.broker, scope.account_id, scope.environment,
                                    "manual_execution", parsed.broker_id, "operator-confirmed-broker-order", now,
                                    payload, hashlib.sha256(payload.encode()).hexdigest()))
                result["result"] = "imported"
            if apply:
                connection.commit()
            else:
                connection.rollback()
            return result
        except BaseException:
            connection.rollback()
            raise
