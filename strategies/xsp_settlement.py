"""Pure XSP settlement and lifecycle calculations.

Evidence acquisition and attribution are deliberately separate from these
calculations: a computed intrinsic value is not broker confirmation.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import Iterable


@dataclass(frozen=True)
class SettlementValue:
    expiration: date
    value: Decimal
    source_url: str
    retrieved_at: datetime
    response_hash: str
    category: str


@dataclass(frozen=True)
class SettlementLeg:
    expiration: date
    right: str
    strike: Decimal
    signed_quantity: int


@dataclass(frozen=True)
class BrokerCashEvidence:
    account_scope: str
    activity_id: str
    expiration: date
    cash_cents: int
    disposition_confirmed: bool


@dataclass(frozen=True)
class FeeEvidence:
    identity: str
    amount_cents: int | None
    complete: bool
    category: str = "execution"
    provenance: str = ""


@dataclass(frozen=True)
class LifecycleResult:
    outcome: str
    reconciliation_complete: bool
    fees_complete: bool
    settlement_cash_cents: int | None
    gross_cents: int | None
    fees_cents: int | None
    net_cents: int | None
    known_fees_cents: int = 0
    fee_evidence: tuple[FeeEvidence, ...] = ()


def _decimal(value: Decimal | float | int | str) -> Decimal:
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError) as error:
        raise ValueError("invalid monetary value") from error
    if not result.is_finite():
        raise ValueError("monetary values must be finite")
    return result


def parse_asset_key_to_leg(key: str, signed_quantity: int) -> SettlementLeg:
    """Parse JSON or OCC option asset key into a SettlementLeg."""
    try:
        data = json.loads(key)
        exp_str = str(data["expiration"])[:10]
        exp_date = datetime.strptime(exp_str, "%Y-%m-%d").date()
        right = str(data["right"]).lower()
        strike = _decimal(data["strike"])
        return SettlementLeg(
            expiration=exp_date,
            right=right,
            strike=strike,
            signed_quantity=signed_quantity,
        )
    except Exception:
        pass

    m = re.search(r"([A-Z]+)\s*(\d{2})(\d{2})(\d{2})([CPcp])(\d{8})", str(key))
    if m:
        _, yy, mm, dd, cp, strike_raw = m.groups()
        exp_date = date(2000 + int(yy), int(mm), int(dd))
        right = "call" if cp.upper() == "C" else "put"
        strike = Decimal(strike_raw) / Decimal(1000)
        return SettlementLeg(
            expiration=exp_date,
            right=right,
            strike=strike,
            signed_quantity=signed_quantity,
        )
    raise ValueError(f"Unable to parse asset key into SettlementLeg: {key}")


def create_settlement_value(
    expiration: date | str,
    value: Decimal | float | int | str,
    source_url: str = "cboe.com",
    retrieved_at: datetime | None = None,
    response_hash: str = "settlement-hash",
    category: str = "official",
) -> SettlementValue:
    """Convenience factory for SettlementValue."""
    exp = (
        datetime.strptime(str(expiration)[:10], "%Y-%m-%d").date()
        if isinstance(expiration, str)
        else expiration
    )
    retrieved = retrieved_at or datetime.now(timezone.utc)
    return SettlementValue(
        expiration=exp,
        value=_decimal(value),
        source_url=source_url,
        retrieved_at=retrieved,
        response_hash=response_hash,
        category=category,
    )


def create_broker_cash_evidence(
    account_scope: str,
    activity_id: str,
    expiration: date | str,
    cash_cents: int,
    disposition_confirmed: bool = True,
) -> BrokerCashEvidence:
    """Convenience factory for BrokerCashEvidence."""
    exp = (
        datetime.strptime(str(expiration)[:10], "%Y-%m-%d").date()
        if isinstance(expiration, str)
        else expiration
    )
    return BrokerCashEvidence(
        account_scope=account_scope,
        activity_id=activity_id,
        expiration=exp,
        cash_cents=int(cash_cents),
        disposition_confirmed=bool(disposition_confirmed),
    )


def calculate_settlement_cash(
    legs: Iterable[SettlementLeg], settlement: SettlementValue
) -> tuple[int, bool]:
    """Return signed cash in cents and whether every remaining leg is worthless."""
    value = _decimal(settlement.value)
    if value < 0:
        raise ValueError("settlement value must be nonnegative")
    total = Decimal(0)
    worthless = True
    for leg in legs:
        if leg.expiration != settlement.expiration:
            raise ValueError("leg expiration does not match settlement")
        strike = _decimal(leg.strike)
        if strike < 0 or isinstance(leg.signed_quantity, bool) or not isinstance(leg.signed_quantity, int) or not leg.signed_quantity:
            raise ValueError("invalid strike or quantity")
        right = leg.right.lower()
        if right == "call":
            intrinsic = max(value - strike, Decimal(0))
        elif right == "put":
            intrinsic = max(strike - value, Decimal(0))
        else:
            raise ValueError("invalid option right")
        worthless = worthless and intrinsic == 0
        total += Decimal(leg.signed_quantity) * intrinsic * 100
    return int((total * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP)), worthless


def reconcile_lifecycle(
    *,
    opening_cash_cents: int,
    closing_cash_cents: int,
    remaining_legs: Iterable[SettlementLeg],
    settlement: SettlementValue | None,
    broker_cash: BrokerCashEvidence | None,
    fees: Iterable[FeeEvidence] = (),
    fee_coverage_complete: bool = True,
    expiration_passed: bool = False,
) -> LifecycleResult:
    """Calculate a lifecycle only when the caller has attributed its evidence.

    ``fee_coverage_complete`` asserts that every execution and settlement fee
    component has been accounted for, including explicit confirmed zeroes.
    """
    legs = tuple(remaining_legs)
    fee_items = tuple(fees)
    if any(item.amount_cents is not None and item.amount_cents < 0 for item in fee_items):
        raise ValueError("fees must be nonnegative")
    if len({item.identity for item in fee_items}) != len(fee_items) or any(not item.identity for item in fee_items):
        raise ValueError("fee evidence identities must be unique and nonempty")

    complete_fees = fee_coverage_complete and all(
        item.complete and item.amount_cents is not None for item in fee_items
    )
    known_fee_total = sum(item.amount_cents for item in fee_items if item.amount_cents is not None)
    fee_total = known_fee_total if complete_fees else None

    expected = None
    reconciled = False
    if not legs:
        outcome = "CLOSED_BY_TRADE"
        reconciled = True
        expected = 0
    else:
        outcome = "EXPIRED_AWAITING_SETTLEMENT" if expiration_passed else "OPEN"
        if settlement is not None:
            expected, worthless = calculate_settlement_cash(legs, settlement)
            if (
                expiration_passed
                and broker_cash is not None
                and broker_cash.account_scope
                and broker_cash.activity_id
                and broker_cash.disposition_confirmed
                and broker_cash.expiration == settlement.expiration
                and broker_cash.cash_cents == expected
            ):
                reconciled = True
                outcome = "EXPIRED_WORTHLESS" if worthless else "CASH_SETTLED"

    gross = (
        opening_cash_cents + closing_cash_cents + expected
        if reconciled and expected is not None
        else None
    )
    net = gross - fee_total if gross is not None and fee_total is not None else None
    return LifecycleResult(
        outcome=outcome,
        reconciliation_complete=reconciled,
        fees_complete=complete_fees,
        settlement_cash_cents=expected,
        gross_cents=gross,
        fees_cents=fee_total,
        net_cents=net,
        known_fees_cents=known_fee_total,
        fee_evidence=fee_items,
    )
