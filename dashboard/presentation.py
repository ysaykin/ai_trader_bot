"""Shared formatting and quote validation for dashboard values."""
from __future__ import annotations

import math
from typing import Any


def finite_number(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return result if math.isfinite(result) else None


def format_numeric(value: Any, *, currency: bool = False, decimals: int = 2) -> str:
    number = finite_number(value)
    if number is None:
        return "N/A"
    if round(number, decimals) == 0:
        number = 0.0
    sign = "-" if number < 0 else ""
    return f"{sign}{'$' if currency else ''}{abs(number):,.{decimals}f}"


def numeric_tone(value: Any) -> str:
    number = finite_number(value)
    return "neutral" if number is None or number == 0 else "positive" if number > 0 else "negative"


def option_mark(bid: Any, ask: Any, last: Any = None) -> float | None:
    bid_value, ask_value, last_value = map(finite_number, (bid, ask, last))
    if bid_value is not None and ask_value is not None and 0 <= bid_value <= ask_value:
        return (bid_value + ask_value) / 2
    return last_value if last_value is not None and last_value >= 0 else None
