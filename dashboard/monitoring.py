"""Read-only monitoring derived from the dashboard's existing evidence bundle."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from dashboard.presentation import finite_number, format_numeric
from strategies.order_sessions import day_session_ended


ACTIVE_ORDERS = {"open", "pending", "submitted", "partially_filled", "partial", "pending_cancel", "pending_replace", "held"}


def observed_at(value: Any) -> datetime | None:
    try:
        if isinstance(value, (int, float)) or str(value).replace(".", "", 1).isdigit():
            stamp = float(value)
            return datetime.fromtimestamp(stamp / 1000 if stamp > 1e11 else stamp, timezone.utc)
        stamp = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return stamp if stamp.tzinfo else stamp.replace(tzinfo=timezone.utc)
    except (ValueError, TypeError, OverflowError, OSError):
        return None


def age_text(seconds: float | None) -> str:
    if seconds is None:
        return "Unknown"
    if seconds < 60:
        return f"{int(seconds)}s"
    if seconds < 3600:
        return f"{int(seconds // 60)}m {int(seconds % 60)}s"
    return f"{seconds / 3600:.1f}h"


def data_health(telemetry: dict[str, Any], *, symbols: list[str], session_open: bool,
                now: datetime | None = None) -> list[dict[str, str]]:
    now = now or datetime.now(timezone.utc)
    rows = []
    freshness = telemetry.get("freshness", {})
    for key, label in (("balances", "Account balances"), ("positions", "Broker positions"), ("orders", "Broker orders")):
        source = freshness.get(key, {})
        status = str(source.get("status") or "unavailable")
        rows.append({"name": label, "source": telemetry.get("broker", "Broker"),
                     "value": "", "status": status.title(), "color": "jade" if status == "ready" else "amber",
                     "received": age_text(finite_number(source.get("age"))), "provider_age": "N/A",
                     "detail": str(source.get("error") or source.get("synced_at") or "No successful snapshot")})
    for symbol in dict.fromkeys(symbols):
        snapshot = telemetry.get("quote_snapshots", {}).get(symbol, {})
        quote = snapshot.get("data") or {}
        value = finite_number(quote.get("last"))
        stamp = observed_at(quote.get("trade_date"))
        provider_age = max(0, (now - stamp).total_seconds()) if stamp else None
        state = snapshot.get("status", "unavailable")
        status = state.title()
        if state in {"ready", "stale"} and value is not None and value > 0:
            if provider_age is None:
                status = "Timestamp unknown"
            elif not session_open and state == "ready":
                status = "Last session"
            elif provider_age > 90:
                status = "Delayed"
        elif value is None or value <= 0:
            status = "Missing"
        rows.append({"name": symbol, "source": telemetry.get("broker", "Broker"),
                     "value": format_numeric(value) if state in {"ready", "stale"} and value is not None and value > 0 else "N/A",
                     "status": status, "color": "jade" if status == "Ready" else "gray" if status == "Last session" else "amber",
                     "received": age_text(finite_number(snapshot.get("age"))),
                     "provider_age": age_text(provider_age),
                     "detail": str(snapshot.get("error") or (stamp.isoformat() if stamp else "No provider trade timestamp"))})
    return rows


def order_activity(telemetry: dict[str, Any], *, now: datetime | None = None) -> list[dict[str, Any]]:
    """Join by stored broker parent ID only; retain unconfirmed local orders."""
    now = now or datetime.now(timezone.utc)
    packages = telemetry.get("ledger_packages", [])
    by_parent: dict[str, list[dict[str, Any]]] = {}
    for package in packages:
        parent = package.get("broker_parent_id")
        if parent:
            by_parent.setdefault(str(parent), []).append(package)
    results = []
    seen = set()
    fresh = telemetry.get("orders_status") == "ready"

    def ledger_progress(package):
        legs = package.get("legs", [])
        requested = sum(abs(leg.get("requested_signed_quantity", 0)) for leg in legs)
        filled = sum(abs(leg.get("filled_signed_quantity", 0)) for leg in legs)
        return f"{filled}/{requested} leg contracts filled" if legs else "Fill quantities unavailable"

    for raw in telemetry.get("raw_orders", []):
        identity = str(raw.get("id") or "Unknown")
        matches = by_parent.get(identity, [])
        package = matches[0] if len(matches) == 1 else None
        if package:
            seen.add(package["package_id"])
        status = str(raw.get("status") or "unknown").lower()
        pending = status in ACTIVE_ORDERS
        unresolved = bool(package and package.get("state") in {"submitted", "partial", "closing"}
                          and not package.get("terminal_status"))
        duration = str(raw.get("duration") or (package or {}).get("duration") or "").lower()
        ended = duration == "day" and day_session_ended(raw.get("create_date") or (package or {}).get("created_at"), now)
        review = (unresolved and not pending) or status in {"rejected", "error"} or (pending and not package)
        price = finite_number(raw.get("price"))
        average = finite_number(raw.get("avg_fill_price"))
        if finite_number(raw.get("exec_quantity", raw.get("filled_quantity"))) == 0:
            average = None
        results.append({"id": identity, "strategy": str(package["strategy_id"]) if package else "Unattributed",
                        "position_id": str(package.get("position_id") or "Unknown") if package else "Unknown",
                        "intent": str(package.get("intent", "Unknown")).title() if package else "Unknown",
                        "symbol": str(raw.get("symbol") or "Multi-leg package"),
                        "status": "Session ended / broker " + status.replace("_", " ") if ended and pending else status.replace("_", " ").title(),
                        "color": "amber" if review or not fresh or ended and pending else "cyan" if pending else "jade" if status == "filled" else "gray",
                        "progress": ledger_progress(package) if package else
                                    f"{raw.get('exec_quantity', '?')}/{raw.get('quantity', '?')} broker quantity filled",
                        "price": format_numeric(price, currency=True) if price is not None else
                                 "MKT" if str(raw.get("type")).lower() == "market" else "N/A",
                        "average": format_numeric(average, currency=True),
                        "updated": str(raw.get("transaction_date") or raw.get("create_date") or "Unknown"),
                        "evidence": ("Broker still reports active; cancellation/reconciliation required" if fresh else
                                     "Last known broker status active; current confirmation unavailable") if ended and pending else
                                    "Ledger reconciliation pending" if unresolved and not pending else
                                    "Current broker snapshot" if fresh else "Last known broker snapshot",
                        "needs_review": review or ended and pending, "pending": pending and not ended,
                        "session_ended": ended and (pending or unresolved), "source": "broker"})
    for package in packages:
        if package["package_id"] in seen:
            continue
        if package.get("terminal_status") == "canceled":
            expired = package.get("terminal_reason") == "day_session_expired"
            results.append({"id": str(package.get("broker_parent_id") or package["package_id"]),
                            "strategy": str(package["strategy_id"]),
                            "position_id": str(package.get("position_id") or "Unknown"),
                            "intent": str(package.get("intent") or "Unknown").title(), "symbol": "Multi-leg package",
                            "status": "Canceled / DAY expired" if expired else "Canceled", "color": "gray", "progress": ledger_progress(package),
                            "price": format_numeric(package.get("limit_price"), currency=True), "average": "N/A",
                            "updated": str(package.get("updated_at") or "Unknown"),
                            "evidence": ("DAY session ended" if expired else "Broker cancellation recorded") +
                                        "; unfilled reservation released; recorded fills retained",
                            "needs_review": False, "pending": False, "session_ended": False, "source": "ledger"})
            continue
        if (package.get("state") not in {"submitted", "partial", "closing"} or package.get("terminal_status")):
            continue
        ended = str(package.get("duration") or "").lower() == "day" and day_session_ended(package.get("created_at"), now)
        results.append({"id": str(package.get("broker_parent_id") or package["package_id"]),
                        "strategy": str(package["strategy_id"]),
                        "position_id": str(package.get("position_id") or "Unknown"),
                        "intent": str(package.get("intent") or "Unknown").title(), "symbol": "Multi-leg package",
                        "status": "Session ended / unconfirmed" if ended else "Unconfirmed", "color": "amber", "progress": ledger_progress(package),
                        "price": format_numeric(package.get("limit_price"), currency=True), "average": "N/A",
                        "updated": str(package.get("updated_at") or package.get("created_at") or "Unknown"),
                        "evidence": "Absent from broker snapshot; reservation retained" if package.get("broker_parent_id") else
                                    "Broker order identity missing; reservation retained",
                        "needs_review": True, "pending": not ended, "session_ended": ended, "source": "ledger"})
    return sorted(results, key=lambda row: (not (row["source"] == "broker" and row["pending"]),
                                             not row["needs_review"], not row["pending"]))


def strategy_results(incubator: dict[str, Any], positions: list[dict[str, Any]],
                     orders: list[dict[str, Any]], *, inventory_known: bool = True,
                     orders_known: bool = True) -> list[dict[str, Any]]:
    results = []
    for row in incubator.get("all_rows", []):
        name = row["strategy_name"]
        current = [position for position in positions if position["strategy"] == name]
        marked = [p for p in current if not p.get("awaiting_settlement")]
        open_pnl = (sum(p["open_pnl"] for p in marked) if marked and
                    all(p["open_pnl"] is not None for p in marked) else None)
        closed = row["closed_trades"]
        net = row["net_pnl"] if closed else None
        results.append({"name": row["variant"], "strategy": name,
                        "closed": str(closed), "win_rate": row["win_rate"] if closed else "N/A",
                        "net": format_numeric(net, currency=True), "net_value": net,
                        "open": format_numeric(open_pnl, currency=True),
                        "positions": str(len(marked)) if inventory_known else f"{len(marked)} known" if marked else "N/A",
                        "settlements": str(sum(bool(p.get("awaiting_settlement")) for p in current)),
                        "orders": str(sum(o["pending"] for o in orders if o["strategy"] == name)) + (" known" if not orders_known else ""),
                        "status": row["scan_status"], "color": row["scan_status_color"],
                        "outcome": "No verified closed trades" if not closed else
                                   "Positive after fees" if net > 0 else "Negative after fees" if net < 0 else "Break-even after fees",
                        "entry": row["entry_time"]})
    return results


def feature_health(policy: dict[str, Any], *, now: datetime | None = None) -> list[dict[str, str]]:
    snapshot = policy.get("latest_snapshot", {})
    collected = observed_at(snapshot.get("market_as_of") or snapshot.get("timestamp"))
    now = now or datetime.now(timezone.utc)
    collected_age = (now - collected).total_seconds() if collected else None
    market = snapshot.get("market") or {}
    observations = snapshot.get("market_observations") or {}
    rows = []
    for key, label in (("vix", "VIX level"), ("vix_term_ratio", "VIX9D / VIX"),
                       ("vwap_distance", "VWAP distance"), ("orb_30m_range", "Opening range"),
                       ("atr_pct", "ATR"), ("rsi_14", "RSI"), ("price_velocity_15m", "15-minute momentum")):
        value = finite_number(market.get(key))
        source = observations.get(key)
        stamp = observed_at(source.get("as_of")) if source is not None else collected
        age = max(0, (now - stamp).total_seconds()) if stamp else None
        status = ("Missing" if value is None else "Timestamp unknown" if age is None else
                  "Stale" if collected_age is None or collected_age > 600 else
                  "Delayed" if source is not None and age > 120 else
                  "Stale" if age > 600 else "Observed")
        if source and source.get("proxy_for"):
            label += f" ({source['symbol']} proxy)"
        rows.append({"name": label, "value": format_numeric(value, decimals=4), "status": status,
                     "color": "jade" if status == "Observed" else "amber", "age": age_text(age)})
    return rows
