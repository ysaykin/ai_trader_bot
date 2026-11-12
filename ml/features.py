"""Causal intraday features. Missing observations remain None, never zero-filled."""

from __future__ import annotations

import math
from datetime import datetime, time, timedelta
from statistics import pstdev
from typing import Any, Iterable, Mapping
from zoneinfo import ZoneInfo

MARKET_TZ = ZoneInfo("America/New_York")
GREEKS = ("delta", "gamma", "theta", "vega")


def finite_number(value: Any) -> float | None:
    """Convert provider scalars to finite JSON numbers."""
    if value is None or isinstance(value, bool):
        return None
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return result if math.isfinite(result) else None


def completed_session_bars(
    bars: Iterable[Mapping[str, Any]], as_of: datetime,
) -> dict[datetime, dict[str, Any]]:
    """Validate, deduplicate and order completed bars in this regular session."""
    if as_of.tzinfo is None:
        raise ValueError("as_of must be timezone-aware")
    now = as_of.astimezone(MARKET_TZ)
    opening = datetime.combine(now.date(), time(9, 30), MARKET_TZ)
    closing = datetime.combine(now.date(), time(16), MARKET_TZ)
    observed: dict[datetime, dict[str, Any]] = {}
    for bar in bars:
        stamp = bar.get("timestamp")
        if isinstance(stamp, str):
            try:
                stamp = datetime.fromisoformat(stamp)
            except ValueError:
                continue
        if not isinstance(stamp, datetime) or stamp.tzinfo is None:
            continue
        stamp = stamp.astimezone(MARKET_TZ)
        if not opening <= stamp < closing or stamp + timedelta(minutes=1) > now:
            continue
        values = {key: finite_number(bar.get(key)) for key in ("high", "low", "close", "volume")}
        if any(values[key] is None or values[key] <= 0 for key in ("high", "low", "close")):
            continue
        if values["high"] < values["low"]:
            continue
        observed[stamp] = values
    return dict(sorted(observed.items()))


def market_features(
    bars: Iterable[Mapping[str, Any]],
    as_of: datetime,
    vix: float | None = None,
    vix9d: float | None = None,
) -> dict[str, float | None]:
    """Calculate features at the latest completed provider bar, even if delayed.

    Bar timestamps identify minute starts. Callers must carry the provider's
    observation time separately from collection time. ATR and RSI use trailing
    14 changes; VWAP requires every minute from the open through that bar.
    """
    observed = completed_session_bars(bars, as_of)
    now = as_of.astimezone(MARKET_TZ)
    opening = datetime.combine(now.date(), time(9, 30), MARKET_TZ)
    closing = datetime.combine(now.date(), time(16), MARKET_TZ)
    stamps = list(observed)
    rows = [observed[stamp] for stamp in stamps]
    result: dict[str, float | None] = dict.fromkeys((
        "vix", "vix_term_ratio", "vwap_distance", "orb_30m_range",
        "atr_pct", "rsi_14", "price_velocity_15m", "minutes_to_close",
    ))
    vix = finite_number(vix)
    vix9d = finite_number(vix9d)
    result["vix"] = vix if vix is not None and vix > 0 else None
    if result["vix"] is not None and vix9d is not None and vix9d > 0:
        result["vix_term_ratio"] = vix9d / vix
    result["minutes_to_close"] = max((closing - now).total_seconds() / 60, 0.0)
    if not rows:
        return result
    price = rows[-1]["close"]
    # A partial-volume history would silently bias VWAP, so require full coverage.
    expected_minutes = int((stamps[-1] - opening).total_seconds() // 60) + 1
    complete_history = list(observed) == [opening + timedelta(minutes=i) for i in range(expected_minutes)]
    if complete_history and all(row["volume"] is not None and row["volume"] >= 0 for row in rows):
        volume = sum(row["volume"] for row in rows)
        if volume > 0:
            vwap = sum((row["high"] + row["low"] + row["close"]) / 3 * row["volume"] for row in rows) / volume
            result["vwap_distance"] = (price - vwap) / vwap
    opening_rows = [observed.get(opening + timedelta(minutes=i)) for i in range(30)]
    if now >= opening + timedelta(minutes=30) and all(row is not None for row in opening_rows):
        result["orb_30m_range"] = (
            max(row["high"] for row in opening_rows)
            - min(row["low"] for row in opening_rows)
        ) / price
    if len(rows) >= 15 and stamps[-1] - stamps[-15] == timedelta(minutes=14):
        changes = [rows[i]["close"] - rows[i - 1]["close"] for i in range(len(rows) - 14, len(rows))]
        gains = sum(max(change, 0) for change in changes)
        losses = sum(max(-change, 0) for change in changes)
        result["rsi_14"] = 100 * gains / (gains + losses) if gains + losses else 50.0
        ranges = [
            max(
                rows[i]["high"] - rows[i]["low"],
                abs(rows[i]["high"] - rows[i - 1]["close"]),
                abs(rows[i]["low"] - rows[i - 1]["close"]),
            )
            for i in range(len(rows) - 14, len(rows))
        ]
        result["atr_pct"] = sum(ranges) / 14 / price
    previous = observed.get(stamps[-1] - timedelta(minutes=15))
    if previous is not None and all(stamps[-1] - timedelta(minutes=i) in observed for i in range(16)):
        result["price_velocity_15m"] = price / previous["close"] - 1
    return result


def grid_features(
    snapshots: Mapping[str, Mapping[str, Any] | None],
    previous: Mapping[str, float | None] | None = None,
) -> dict[str, Any]:
    """Aggregate only this tick's grid, with coverage and missing-Greek metadata."""
    rows = [row for row in snapshots.values() if row is not None]
    pnls = [value for row in rows if (value := finite_number(row.get("unrealized_pnl"))) is not None]
    result: dict[str, Any] = {
        "expected_count": len(snapshots), "observed_count": len(rows),
        "pnl_count": len(pnls), "pnl_dispersion": pstdev(pnls) if pnls else None,
    }
    for wing in ("put_pressure", "call_pressure"):
        values = [value for row in rows if (value := finite_number(row.get(wing))) is not None]
        result[wing] = max(values) if values else None
    for greek in GREEKS:
        values = [finite_number(row.get(greek)) for row in rows]
        # Partial Greeks cannot be presented as a whole-grid exposure.
        total = sum(values) if len(rows) == len(snapshots) and all(value is not None for value in values) else None
        result[f"{greek}_net"] = total
        before = finite_number((previous or {}).get(f"{greek}_net"))
        result[f"{greek}_drift"] = total - before if total is not None and before is not None else None
    return result
