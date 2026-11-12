"""Payoff bounds for covered option combinations at one expiry."""
from __future__ import annotations

import math
from typing import Any, Iterable, Mapping


def covered_option_loss(legs: Iterable[tuple[Mapping[str, Any], int]], cash: float) -> float | None:
    """Maximum loss before fees, or unknown for uncovered/mixed-scope inventory.

    Supports balanced two- and four-leg combinations. Incomplete fills remain
    unknown. A piecewise linear option payoff has its minimum at zero or a strike.
    Requiring covering longs for each right also excludes an unbounded call
    tail and deliberately keeps uncovered puts outside supported inventory.
    """
    legs = list(legs)
    if len(legs) not in {2, 4} or len({abs(q) for _, q in legs}) != 1 or not math.isfinite(cash):
        return None
    if len({(asset["symbol"], asset["expiration"]) for asset, _ in legs}) != 1:
        return None
    if any(asset["right"] not in {"put", "call"} or not math.isfinite(float(asset["strike"]))
           or float(asset["strike"]) <= 0 for asset, _ in legs):
        return None
    if any(sum(q for asset, q in legs if asset["right"] == right) < 0 for right in ("put", "call")):
        return None
    points = {0.0, *(float(asset["strike"]) for asset, _ in legs)}
    minimum = min(cash + sum(q * 100 * max(
        float(asset["strike"]) - spot if asset["right"] == "put" else spot - float(asset["strike"]), 0)
        for asset, q in legs) for spot in points)
    return round(max(0.0, -minimum), 2)
