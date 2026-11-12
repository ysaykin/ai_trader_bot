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


def build_dashboard_presentation_bundle(snapshots: dict[str, Any]) -> dict[str, Any]:
    """Shared presentation helper for broker telemetry snapshots.

    Retains historical payloads separately from current totals, passes one unified
    bundle with per-resource generations, age, errors, provider timestamps, and
    valuation coverage. Excludes expired values from current totals without fabricating
    zero P&L.
    """
    freshness: dict[str, dict[str, Any]] = {}
    generations: dict[str, Any] = {}
    provider_timestamps: dict[str, Any] = {}
    ages: dict[str, Any] = {}
    errors: list[str] = []

    for key, value in snapshots.items():
        if key in {"balances", "positions", "orders"} or key.startswith("quote:"):
            status = value.get("status")
            synced_at = value.get("synced_at")
            success_at = value.get("success_at")
            age = value.get("age")
            err = value.get("error")
            gen = value.get("generation")

            freshness[key] = {
                "status": status,
                "synced_at": synced_at,
                "success_at": success_at,
                "age": age,
                "error": err,
                "generation": gen,
            }
            generations[key] = gen
            provider_timestamps[key] = success_at or synced_at
            ages[key] = age
            if err:
                errors.append(f"{key}: {err}")

    # 1. Balances
    b_snap = snapshots.get("balances", {})
    b_status = b_snap.get("status", "unknown")
    b_payload = b_snap.get("data")
    historical_balances = b_payload.get("balances", b_payload) if isinstance(b_payload, dict) else None

    equity: float | None = None
    cash: float | None = None
    buying_power: float | None = None
    option_requirement: float | None = None
    open_exposure: float | None = None

    if historical_balances and isinstance(historical_balances, dict):
        margin = historical_balances.get("margin") or historical_balances.get("cash") or {}
        margin = margin if isinstance(margin, dict) else {}
        total_equity = finite_number(historical_balances.get("total_equity", historical_balances.get("equity")))
        total_cash = finite_number(historical_balances.get("total_cash", margin.get("cash")))
        bp = finite_number(margin.get("option_buying_power", historical_balances.get("buying_power")))
        opt_req = finite_number(margin.get("option_requirement", historical_balances.get("option_requirement")))

        if b_status != "expired":
            equity = total_equity
            cash = total_cash
            buying_power = bp
            option_requirement = opt_req
            open_exposure = opt_req

    # 2. Quotes & Marks
    quotes: dict[str, Any] = {}
    marks: dict[str, float | None] = {}
    for key, value in snapshots.items():
        if key.startswith("quote:") and isinstance(value.get("data"), dict):
            sym = key[6:]
            q_data = value["data"]
            quotes[sym] = q_data
            marks[sym] = (option_mark(q_data.get("bid"), q_data.get("ask"), q_data.get("last"))
                          if value.get("status") in {"ready", "stale"} else None)

    # 3. Positions
    p_snap = snapshots.get("positions", {})
    p_status = p_snap.get("status", "unknown")
    p_payload = p_snap.get("data")
    raw_positions: list[dict[str, Any]] | None = None
    formatted_positions: list[dict[str, Any]] = []
    valued_count = 0
    total_positions = 0

    if p_payload is not None and isinstance(p_payload, dict):
        raw = p_payload.get("positions", p_payload)
        pos_list = [] if raw in (None, "null") else raw.get("position", []) if isinstance(raw, dict) else raw if isinstance(raw, list) else []
        raw_positions = [pos_list] if isinstance(pos_list, dict) else pos_list
        total_positions = len(raw_positions)

    if raw_positions is not None:
        for row in raw_positions:
            symbol = str(row.get("symbol", ""))
            quantity = finite_number(row.get("quantity"))
            basis = finite_number(row.get("cost_basis"))
            mark = marks.get(symbol)
            if mark is not None:
                valued_count += 1
            market_value = quantity * mark * 100 if quantity is not None and mark is not None else None
            pnl = market_value - basis if market_value is not None and basis is not None else None
            is_expired = (p_status == "expired")

            formatted_positions.append({
                "symbol": symbol,
                "quantity_num": quantity,
                "cost_basis_num": basis,
                "mark_num": mark,
                "quantity": format_numeric(quantity),
                "cost_basis": format_numeric(basis, currency=True),
                "last_price": format_numeric(mark, currency=True),
                "market_value": format_numeric(market_value, currency=True),
                "unrealized_pl": format_numeric(pnl, currency=True),
                "unrealized_pl_num": pnl,
                "unrealized_pl_color": "#10B981" if (pnl or 0) > 0 else "#EF4444" if (pnl or 0) < 0 else "#94A3B8",
                "is_expired": is_expired,
                "status": p_status,
            })

    valuation_coverage = {
        "total": total_positions,
        "valued": valued_count,
        "coverage_pct": (valued_count / total_positions * 100.0) if total_positions > 0 else 100.0,
    }

    # Unrealized P&L for active totals: exclude expired positions or missing marks
    unrealized_pnl: float | None = None
    if p_status != "expired" and formatted_positions:
        if all(p["unrealized_pl_num"] is not None for p in formatted_positions):
            unrealized_pnl = sum(p["unrealized_pl_num"] for p in formatted_positions)

    # 4. Orders
    o_snap = snapshots.get("orders", {})
    o_status = o_snap.get("status", "unknown")
    o_payload = o_snap.get("data")
    raw_orders: list[dict[str, Any]] = []

    if o_payload and isinstance(o_payload, dict):
        raw_o = o_payload.get("orders", o_payload)
        ord_list = [] if raw_o in (None, "null") else raw_o.get("order", []) if isinstance(raw_o, dict) else raw_o if isinstance(raw_o, list) else []
        raw_orders = [ord_list] if isinstance(ord_list, dict) else ord_list

    formatted_orders: list[dict[str, Any]] = []
    for row in raw_orders:
        price = finite_number(row.get("price", row.get("avg_fill_price", row.get("stop_price"))))
        formatted_orders.append({
            "id": str(row.get("id", "")),
            "symbol": str(row.get("symbol", "")),
            "side": str(row.get("side", row.get("order_type", ""))).upper(),
            "type": str(row.get("strategy", row.get("type", ""))).upper(),
            "quantity": str(row.get("quantity", "")),
            "status": str(row.get("status", "")).upper(),
            "price": format_numeric(price, currency=True) if price is not None else
                     "MKT" if str(row.get("type", "")).lower() == "market" else "N/A",
            "is_expired": (o_status == "expired"),
        })

    return {
        "freshness": freshness,
        "generations": generations,
        "provider_timestamps": provider_timestamps,
        "ages": ages,
        "errors": errors,
        "balances_status": b_status,
        "positions_status": p_status,
        "orders_status": o_status,
        "equity": equity,
        "cash": cash,
        "buying_power": buying_power,
        "option_requirement": option_requirement,
        "open_exposure": open_exposure,
        "historical_balances": historical_balances,
        "positions": formatted_positions,
        "historical_positions": formatted_positions,
        "raw_positions": raw_positions,
        "historical_raw_positions": raw_positions,
        "valuation_coverage": valuation_coverage,
        "unrealized_pnl": unrealized_pnl,
        "orders": formatted_orders,
        "historical_orders": formatted_orders,
        "raw_orders": raw_orders,
        "quotes": quotes,
        "marks": marks,
    }
