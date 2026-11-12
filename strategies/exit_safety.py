"""Quote and payoff bounds shared by option marks and package execution."""
from __future__ import annotations

from collections import defaultdict
import math
from typing import Any


def bounded_option_value(legs: list[tuple[Any, int]], marks: list[float | None]) -> float | None:
    """Reject inconsistent marks and bound each covered wing by its payoff.

    A two-wing condor also has a joint bound: both wings cannot lose their full
    width at the same underlying price. Incomplete or unbalanced inventory has
    no quote-driven P&L signal here and remains under recovery management.
    """
    if len(legs) not in {2, 4} or len(marks) != len(legs):
        return None
    if len({abs(q) for _, q in legs}) != 1 or not legs[0][1]:
        return None
    if any(mark is None or not math.isfinite(mark) or mark < 0 for mark in marks):
        return None
    if len({(str(a.symbol), str(a.expiration)) for a, _ in legs}) != 1:
        return None
    wings = defaultdict(list)
    for (asset, quantity), mark in zip(legs, marks):
        right = str(asset.right).lower()
        if right not in {"put", "call"} or not math.isfinite(float(asset.strike)):
            return None
        wings[right].append((asset, quantity, mark))

    def bounds(items):
        points = {0.0, *(float(a.strike) for a, _, _ in items)}
        values = [sum(q * 100 * max(float(a.strike) - spot if str(a.right).lower() == "put"
                                   else spot - float(a.strike), 0)
                      for a, q, _ in items) for spot in points]
        return min(values), max(values)

    tolerance = .02 * 100 * abs(legs[0][1])  # At most two cents of rounding noise.
    value = 0.0
    for items in wings.values():
        if len(items) != 2 or sum(q for _, q, _ in items) != 0:
            return None
        low, high = bounds(items)
        raw = sum(q * mark * 100 for _, q, mark in items)
        if raw < low - tolerance or raw > high + tolerance:
            return None
        value += min(high, max(low, raw))
    low, high = bounds([item for wing in wings.values() for item in wing])
    if value < low - tolerance or value > high + tolerance:
        return None
    return min(high, max(low, value))
