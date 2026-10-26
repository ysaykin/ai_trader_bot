"""Pure selection, valuation, and defined-risk math for iron condors."""

from __future__ import annotations

import math
from collections.abc import Iterable, Sequence
from numbers import Real


def select_short_strikes(
    strikes: Sequence[Real],
    underlying_price: Real,
    short_delta: Real,
    dte: Real,
    wing_width: Real,
    *,
    annual_volatility: Real = 0.16,
) -> tuple[float | None, float | None]:
    """Select the available OTM strikes nearest a bounded volatility offset."""
    price = _finite_positive(underlying_price, "underlying_price")
    delta = _finite_positive(short_delta, "short_delta")
    width = _finite_positive(wing_width, "wing_width")
    days = max(_finite_number(dte, "dte"), 0.25)
    volatility = _finite_positive(annual_volatility, "annual_volatility")
    available = sorted({_finite_number(strike, "strike") for strike in strikes})
    if not available:
        return None, None

    daily_volatility = volatility / math.sqrt(252.0)
    offset = price * daily_volatility * math.sqrt(days) * (0.15 / delta)
    offset = min(max(offset, width), price * 0.05)
    target_put = price - offset
    target_call = price + offset
    puts = (strike for strike in available if strike <= price)
    calls = (strike for strike in available if strike >= price)
    put = min(puts, key=lambda strike: abs(strike - target_put), default=None)
    call = min(calls, key=lambda strike: abs(strike - target_call), default=None)
    return put, call


def signed_basket_value(
    legs: Iterable[tuple[Real, Real | None]],
    *,
    multiplier: Real = 100.0,
) -> float | None:
    """Return signed marked value in cash, or ``None`` for any missing mark."""
    contract_multiplier = _finite_positive(multiplier, "multiplier")
    total = 0.0
    found = False
    for signed_quantity, mark in legs:
        found = True
        quantity = _finite_number(signed_quantity, "signed_quantity")
        if mark is None or isinstance(mark, bool) or not isinstance(mark, Real):
            return None
        price = float(mark)
        if not math.isfinite(price) or price < 0:
            return None
        total += quantity * price * contract_multiplier
    return total if found else None


def credit_strategy_pnl(
    opening_credit_cash: Real | None,
    signed_current_value: Real | None,
) -> float | None:
    """Return credit-strategy P&L as opening cash plus signed marked value."""
    if opening_credit_cash is None or signed_current_value is None:
        return None
    opening = _finite_number(opening_credit_cash, "opening_credit_cash")
    current = _finite_number(signed_current_value, "signed_current_value")
    return opening + current


def max_defined_loss(
    wing_width: Real,
    quantity: Real,
    opening_credit_cash: Real = 0.0,
    *,
    multiplier: Real = 100.0,
) -> float:
    """Return conservative maximum loss for a balanced defined-risk basket."""
    width = _finite_positive(wing_width, "wing_width")
    contracts = _finite_positive(quantity, "quantity")
    credit = _finite_number(opening_credit_cash, "opening_credit_cash")
    contract_multiplier = _finite_positive(multiplier, "multiplier")
    return max(width * contracts * contract_multiplier - max(credit, 0.0), 0.0)


def _finite_number(value: Real, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, Real):
        raise ValueError(f"{name} must be a real number")
    result = float(value)
    if not math.isfinite(result):
        raise ValueError(f"{name} must be finite")
    return result


def _finite_positive(value: Real, name: str) -> float:
    result = _finite_number(value, name)
    if result <= 0:
        raise ValueError(f"{name} must be positive")
    return result


__all__ = [
    "credit_strategy_pnl",
    "max_defined_loss",
    "select_short_strikes",
    "signed_basket_value",
]
