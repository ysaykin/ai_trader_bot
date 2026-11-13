"""Read-only trade ownership, valuation and expiration analysis for the UI.

Broker positions are net inventory. Only confirmed ledger fills establish which
trade owns a contract. This module never places orders or changes the ledger.
"""
from __future__ import annotations

import json
import math
import sqlite3
from collections import defaultdict
from contextlib import closing
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any, Iterable, Mapping

from dashboard.presentation import finite_number, format_numeric, numeric_tone
from strategies.option_payoff import covered_option_loss


COLORS = {"negative": "#EF4444", "positive": "#10B981", "neutral": "#94A3B8"}
STRUCTURES = {"iron_condor": "Iron condor", "bull_put": "Bull put vertical",
              "bear_call": "Bear call vertical", "put_condor": "Put condor", "call_condor": "Call condor",
              "adjusted": "Adjusted / incomplete",
              "unattributed": "Unattributed contract"}


def display(value: float | None, *, percent: bool = False, currency: bool = True) -> dict[str, str]:
    rounded = round(value, 2) if value is not None else None
    return {"text": format_numeric(rounded, currency=currency and not percent) + ("%" if percent and rounded is not None else ""),
            "color": COLORS[numeric_tone(rounded)]}


@dataclass(frozen=True, order=True)
class Contract:
    underlying: str
    expiry: str
    right: str
    strike: float

    @classmethod
    def from_key(cls, key: str) -> Contract:
        value = json.loads(key)
        expiry = date.fromisoformat(value["expiration"][:10]).isoformat()
        strike = finite_number(value["strike"])
        right = str(value["right"]).lower()
        if strike is None or strike <= 0 or not ("put" in right or "call" in right):
            raise ValueError("Invalid option contract")
        return cls(str(value["symbol"]), expiry, "P" if "put" in right else "C", strike)

    @property
    def symbol(self) -> str:
        return f"{self.underlying}{date.fromisoformat(self.expiry):%y%m%d}{self.right}{round(self.strike * 1000):08d}"

    def intrinsic(self, price: float) -> float:
        return max(self.strike - price if self.right == "P" else price - self.strike, 0)


@dataclass
class Inventory:
    quantity: int = 0
    cost: float = 0.0
    known: bool = True


@dataclass
class Trade:
    id: str
    strategy: str
    trade_id: str
    legs: dict[Contract, Inventory] = field(default_factory=dict)
    opening: dict[Contract, Inventory] = field(default_factory=dict)
    realized: float = 0.0
    fill_count: int = 0
    known: bool = True
    pending_close: bool = False


def read_packages(path: Path) -> list[dict[str, Any]]:
    """Read one consistent SQLite snapshot without migrations or file creation."""
    with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)) as connection:
        connection.row_factory = sqlite3.Row
        connection.execute("BEGIN")
        packages = [dict(row) for row in connection.execute("SELECT * FROM packages ORDER BY created_at, package_id")]
        legs: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for row in connection.execute("SELECT * FROM legs ORDER BY order_id"):
            legs[row["package_id"]].append(dict(row))
        for package in packages:
            package["legs"] = legs[package["package_id"]]
        return packages


def quote_symbols(packages: Iterable[Mapping[str, Any]], *, as_of: date | None = None) -> set[str]:
    today = (as_of or date.today()).isoformat()
    symbols = set()
    for package in packages:
        if package["state"] in {"open", "partial", "closing"}:
            for leg in package["legs"]:
                if leg["filled_signed_quantity"]:
                    try:
                        contract = Contract.from_key(leg["asset_key"])
                        if contract.expiry >= today:
                            symbols.add(contract.symbol)
                            symbols.add(contract.underlying)
                    except (ValueError, KeyError, TypeError):
                        continue
    return symbols


def structure(legs: Mapping[Contract, Inventory]) -> str:
    active = sorted((contract, lot.quantity) for contract, lot in legs.items() if lot.quantity)
    if len({(c.underlying, c.expiry) for c, _ in active}) != 1:
        return "adjusted"
    puts = sorted((c.strike, q) for c, q in active if c.right == "P")
    calls = sorted((c.strike, q) for c, q in active if c.right == "C")
    balanced = len({abs(q) for _, q in active}) == 1
    if balanced:
        for rights, label in ((puts, "put_condor"), (calls, "call_condor")):
            if len(rights) == 4 and rights[0][1] > 0 > rights[1][1] and rights[2][1] < 0 < rights[3][1]:
                return label
    if balanced and len(puts) == 2 and puts[0][1] > 0 > puts[1][1]:
        if not calls:
            return "bull_put"
        if len(calls) == 2 and calls[0][1] < 0 < calls[1][1] and puts[1][0] < calls[0][0]:
            return "iron_condor"
    if balanced and not puts and len(calls) == 2 and calls[0][1] < 0 < calls[1][1]:
        return "bear_call"
    return "adjusted"


def defined_risk(legs: Mapping[Contract, Inventory]) -> float | None:
    """Opening risk, before fees, summed over independent underlying/expiry groups."""
    groups: dict[tuple[str, str], dict[Contract, Inventory]] = defaultdict(dict)
    for contract, lot in legs.items():
        if not lot.known:
            return None
        if lot.quantity:
            groups[(contract.underlying, contract.expiry)][contract] = lot
    total = 0.0
    for group in groups.values():
        if structure(group) == "adjusted":
            return None
        loss = covered_option_loss([
            ({"symbol": c.underlying, "expiration": c.expiry, "right": "put" if c.right == "P" else "call",
              "strike": c.strike}, lot.quantity) for c, lot in group.items()], -sum(lot.cost for lot in group.values()))
        if loss is None:
            return None
        total += loss
    return total


def build_positions(packages: list[Mapping[str, Any]], broker_positions: list[Mapping[str, Any]] | None,
                    quotes: Mapping[str, Mapping[str, Any]], *, fee_rate: float = .40,
                    reported_fees: Mapping[str, float] | None = None,
                    reported_fee_contracts: Mapping[str, float] | None = None,
                    as_of: date | None = None) -> tuple[list[dict[str, Any]], list[str]]:
    """Reconstruct separate trade lots and reconcile their sum to broker inventory."""
    trades: dict[tuple[str, str], Trade] = {}
    today = as_of or date.today()
    package_owner: dict[str, tuple[str, str]] = {}
    issues: list[str] = []
    uncertain_owners: set[str] = set()
    for package in packages:
        if package["state"] in {"expired", "archived"}:
            continue
        owner, pid = str(package["strategy_id"]), str(package["package_id"])
        fills = []
        try:
            requested = [(Contract.from_key(l["asset_key"]), int(l["requested_signed_quantity"]), None) for l in package["legs"]]
            for leg in package["legs"]:
                quantity = int(leg["filled_signed_quantity"])
                if quantity:
                    price = finite_number(leg["fill_price"])
                    fills.append((Contract.from_key(leg["asset_key"]), quantity, price if price is not None and price >= 0 else None))
        except (ValueError, KeyError, TypeError):
            uncertain_owners.add(owner)
            issues.append(f"{owner}: invalid ledger contract or fill")
            continue
        key = (owner, str(package.get("trade_id") or pid))
        replacement = package.get("replacement_of")
        if replacement in package_owner:
            key = package_owner[replacement]
        opening = package["intent"] in {"open", "add"}
        if opening:
            if not fills:
                continue  # An unfilled order is not an owned position.
            trade = trades.setdefault(key, Trade(f"{key[0]}:{key[1]}", owner, key[1]))
        else:
            if key not in trades:
                matching = fills or requested
                candidates = [k for k, t in trades.items() if t.strategy == owner and matching and all(
                    c in t.legs and t.legs[c].quantity * q < 0 and abs(q) <= abs(t.legs[c].quantity)
                    for c, q, _ in matching)]
                if len(candidates) != 1:
                    if fills:
                        uncertain_owners.add(owner)
                        issues.append(f"{owner}: closing fills have no unique trade link")
                    continue
                key = candidates[0]
            trade = trades[key]
            trade.pending_close |= package["state"] in {"submitted", "partial", "closing"}
        package_owner[pid] = key
        for contract, quantity, price in fills:
            lot = trade.legs.setdefault(contract, Inventory())
            known = price is not None
            trade.known &= known
            trade.fill_count += abs(quantity)
            if opening:
                if lot.quantity and lot.quantity * quantity < 0:
                    trade.known = False
                lot.quantity += quantity
                lot.cost += quantity * (price or 0) * 100
                lot.known &= known
                original = trade.opening.setdefault(contract, Inventory())
                original.quantity += quantity
                original.cost += quantity * (price or 0) * 100
                original.known &= known
            elif not lot.quantity or lot.quantity * quantity >= 0 or abs(quantity) > abs(lot.quantity):
                trade.known = False
                uncertain_owners.add(owner)
                issues.append(f"{owner}: close fill exceeds attributable inventory")
            else:
                removed = lot.cost * abs(quantity / lot.quantity)
                trade.realized += -quantity * (price or 0) * 100 - removed
                lot.quantity += quantity
                lot.cost -= removed

    owned: dict[str, int] = defaultdict(int)
    expired_symbols = set()
    for trade in trades.values():
        active_contracts = [c for c, lot in trade.legs.items() if lot.quantity]
        wholly_expired = bool(active_contracts) and all(c.expiry < today.isoformat() for c in active_contracts)
        for contract, lot in trade.legs.items():
            owned[contract.symbol] += lot.quantity
            if lot.quantity and wholly_expired:
                expired_symbols.add(contract.symbol)
    broker = {str(p["symbol"]): p for p in broker_positions or []}
    broker_quantity = {symbol: finite_number(p.get("quantity_num", p.get("quantity"))) for symbol, p in broker.items()}
    mismatches = {symbol for symbol in owned.keys() | broker.keys()
                  if broker_positions is None or broker_quantity.get(symbol, 0) is None
                  or not math.isclose(owned.get(symbol, 0), broker_quantity.get(symbol, 0) or 0, abs_tol=1e-8)}
    # Absent expired inventory still needs settlement evidence, but is distinct
    # from a discrepancy in contracts that can currently be traded.
    absent_expired = {s for s in expired_symbols if broker_quantity.get(s, 0) == 0} if broker_positions is not None else set()
    unexpected_mismatches = mismatches - absent_expired
    if broker_positions is None:
        issues.append("Broker positions unavailable; ownership cannot be reconciled")
    elif unexpected_mismatches:
        issues.append(f"Broker/ledger quantity mismatch on {len(unexpected_mismatches)} contract(s)")
    rows = []
    for trade in trades.values():
        active = {c: lot for c, lot in trade.legs.items() if lot.quantity}
        if not active:
            continue
        awaiting_settlement = all(c.expiry < today.isoformat() for c in active)
        shape = structure(active)
        reconciled = not awaiting_settlement and trade.strategy not in uncertain_owners and not any(c.symbol in mismatches for c in active)
        legs = []
        for contract, lot in sorted(active.items()):
            q = quotes.get(contract.symbol, {}) if contract.expiry >= today.isoformat() else {}
            mark = finite_number(q.get("mark"))
            if mark is not None and mark < 0:
                mark = None
            pnl = lot.quantity * mark * 100 - lot.cost if mark is not None and lot.known and reconciled else None
            legs.append({"symbol": contract.symbol, "underlying": contract.underlying, "expiry": contract.expiry,
                         "right": contract.right, "strike": contract.strike, "quantity": lot.quantity,
                         "cost": lot.cost if lot.known else None, "mark": mark, "pnl": pnl,
                         "delta": (finite_number(q.get("delta")) * lot.quantity * 100) if finite_number(q.get("delta")) is not None else None,
                         "theta": (finite_number(q.get("theta")) * lot.quantity * 100) if finite_number(q.get("theta")) is not None else None,
                         "quote_time": str(q.get("timestamp") or "Timestamp unavailable")})
        inconsistent_marks = False
        if shape != "adjusted" and all(l["mark"] is not None for l in legs):
            # A complete credit spread cannot be worth more than its widest
            # wing or have positive signed liquidation value. Asynchronous or
            # crossed leg marks can otherwise imply losses beyond defined risk.
            prices = [0.0, *(c.strike for c in active), max(c.strike for c in active) * 2]
            bounds = [sum(c.intrinsic(s) * lot.quantity * 100 for c, lot in active.items()) for s in prices]
            marked_value = sum(l["quantity"] * l["mark"] * 100 for l in legs)
            inconsistent_marks = marked_value < min(bounds) - .01 or marked_value > max(bounds) + .01
            if inconsistent_marks:
                issues.append(f"{trade.strategy}: quoted basket value is outside defined payoff bounds; marked P&L unavailable")
                for leg in legs:
                    leg["pnl"] = None
        complete = trade.known and reconciled and all(l["pnl"] is not None for l in legs)
        open_pnl = sum(l["pnl"] for l in legs) if complete else None
        known_fees = (reported_fees or {}).get(trade.trade_id)
        if reported_fee_contracts is not None and reported_fee_contracts.get(trade.trade_id) != trade.fill_count:
            known_fees = None
        fees = known_fees if known_fees is not None else trade.fill_count * fee_rate
        realized = trade.realized if trade.known and reconciled else None
        total = open_pnl + realized - fees if complete and realized is not None else None
        opening_risk = defined_risk(trade.opening)
        scopes = {(c.underlying, c.expiry) for c in active}
        underlying = ", ".join(sorted({c.underlying for c in active}))
        expiry = ", ".join(sorted({c.expiry for c in active}))
        dte = (date.fromisoformat(expiry) - today).days if len(scopes) == 1 else None
        cash = -sum(l.cost for l in active.values()) + trade.realized if trade.known else None
        rows.append({"id": trade.id, "trade_id": trade.trade_id, "strategy": trade.strategy,
                     "structure": shape, "structure_label": STRUCTURES[shape], "underlying": underlying,
                     "expiry": expiry, "dte": dte, "quantity": max(abs(l.quantity) for l in active.values()),
                     "legs": legs, "open_pnl": open_pnl, "realized_pnl": realized, "fees": fees,
                     "estimated_fees": known_fees is None, "net_pnl": total, "opening_risk": opening_risk,
                     "return_pct": total / opening_risk * 100 if total is not None and opening_risk else None,
                     "cash_flow": cash, "reconciled": reconciled,
                     "remaining_entry_cash": -sum(l.cost for l in active.values()) if trade.known else None,
                     "liquidation_value": sum(l["quantity"] * l["mark"] * 100 for l in legs) if complete else None,
                     "delta": sum(l["delta"] for l in legs) if all(l["delta"] is not None for l in legs) else None,
                     "theta": sum(l["theta"] for l in legs) if all(l["theta"] is not None for l in legs) else None,
                     "spot": finite_number(quotes.get(underlying, {}).get("mark")),
                     "awaiting_settlement": awaiting_settlement,
                     "status": "Awaiting settlement" if awaiting_settlement else "Closing" if trade.pending_close else "Open",
                     "quality": "Settlement unverified" if awaiting_settlement else "Reconciliation needed" if not reconciled else "Inconsistent quotes" if inconsistent_marks else "Incomplete quotes" if not complete else "Quoted marks",
                     "scope": f"{underlying} · {expiry}" if len(scopes) == 1 else "Mixed expiries",
                     "payoff_eligible": reconciled and trade.known and shape != "adjusted" and len(scopes) == 1})
    # Do not hide inventory absent from the ledger, or invent a strategy owner.
    for symbol in sorted(mismatches & broker.keys()):
        p = broker[symbol]
        residual = (broker_quantity[symbol] or 0) - owned.get(symbol, 0)
        if not residual:
            continue
        # If ownership has the wrong side or exceeds the broker quantity, show
        # the actual broker lot, not a fabricated opposite position.
        if abs(residual) > abs(broker_quantity[symbol] or 0) or residual * (broker_quantity[symbol] or 0) < 0:
            residual = broker_quantity[symbol] or 0
        if not residual:
            continue
        quote = quotes.get(symbol, {})
        leg = {"symbol": symbol, "quantity": residual, "cost": None,
               "mark": finite_number(quote.get("mark")), "pnl": None, "delta": None, "theta": None,
               "quote_time": str(quote.get("timestamp") or "Timestamp unavailable")}
        rows.append({"id": f"unattributed:{symbol}", "trade_id": symbol, "strategy": "Unattributed",
                     "structure": "unattributed", "structure_label": STRUCTURES["unattributed"],
                     "underlying": symbol, "expiry": "Unknown", "dte": None, "quantity": abs(residual),
                     "legs": [leg], "open_pnl": None, "realized_pnl": None, "fees": None,
                     "estimated_fees": False, "net_pnl": None, "opening_risk": None, "return_pct": None,
                     "cash_flow": None, "remaining_entry_cash": None, "liquidation_value": None,
                     "delta": None, "theta": None, "reconciled": False, "status": "Unattributed",
                     "quality": "Reconciliation needed", "scope": "Unknown", "payoff_eligible": False})
    awaiting_count = sum(r.get("awaiting_settlement", False) for r in rows)
    if awaiting_count:
        issues.append(f"{awaiting_count} expired position(s) awaiting settlement evidence; excluded from open P&L. Net since opening remains unavailable.")
    return sorted(rows, key=lambda r: (r["strategy"], r["expiry"], r["id"])), sorted(set(issues))


def summarize(rows: list[dict[str, Any]]) -> dict[str, Any]:
    def total(key: str) -> float | None:
        return sum(r[key] for r in rows) if all(r[key] is not None for r in rows) else None
    pnl, risk = total("net_pnl"), total("opening_risk")
    current = [r for r in rows if not r.get("awaiting_settlement", False)]
    open_pnl = sum(r["open_pnl"] for r in current) if all(r["open_pnl"] is not None for r in current) else None
    if rows and not current:
        open_pnl = None
    return {"count": len(rows), "marked": sum(r["net_pnl"] is not None for r in rows),
            "open_count": len(current), "awaiting_settlement": len(rows) - len(current),
            "open_pnl": open_pnl, "realized_pnl": total("realized_pnl"),
            "fees": total("fees"), "net_pnl": pnl, "opening_risk": risk,
            "return_pct": pnl / risk * 100 if pnl is not None and risk else None,
            "estimated_fees": any(r["estimated_fees"] for r in rows)}


def payoff_at(row: Mapping[str, Any], price: float) -> float:
    if not row["payoff_eligible"]:
        raise ValueError("Position is not eligible for an expiration payoff")
    return row["cash_flow"] - row["fees"] + sum(
        max(leg["strike"] - price if leg["right"] == "P" else price - leg["strike"], 0)
        * leg["quantity"] * 100 for leg in row["legs"])


def payoff_bounds(row: Mapping[str, Any]) -> dict[str, Any]:
    if not row["payoff_eligible"]:
        return {"max_profit": None, "max_loss": None, "breakevens": []}
    strikes = sorted({leg["strike"] for leg in row["legs"]})
    points = [0.0, *strikes, strikes[-1] * 2]
    values = [payoff_at(row, price) for price in points]
    roots = set()
    for a, b, va, vb in zip(points, points[1:], values, values[1:]):
        if va * vb < 0:
            roots.add(round(a - va * (b - a) / (vb - va), 2))
        elif va == 0 and a:
            roots.add(a)
    return {"max_profit": max(values), "max_loss": min(values), "breakevens": sorted(roots)}


def payoff_figure(rows: list[dict[str, Any]], scope: str, units: str = "Dollars"):
    """Compare only positions sharing an underlying and actual expiration."""
    import plotly.graph_objects as go
    eligible = [r for r in rows if r["payoff_eligible"] and r["scope"] == scope]
    fig = go.Figure()
    if not eligible:
        return fig
    strikes = sorted({leg["strike"] for r in eligible for leg in r["legs"]})
    span = max(strikes[-1] - strikes[0], strikes[0] * .02)
    low, high = max(0, strikes[0] - span * .3), strikes[-1] + span * .3
    prices = sorted({round(low + (high - low) * i / 160, 4) for i in range(161)} | set(strikes))
    palette = ["#00D4B2", "#A89AFF", "#FFB657", "#60A5FA", "#F472B6"]
    for index, strategy in enumerate(sorted({r["strategy"] for r in eligible})):
        group = [r for r in eligible if r["strategy"] == strategy]
        risk = summarize(group)["opening_risk"]
        if units != "Dollars" and not risk:
            continue
        values = [sum(payoff_at(r, price) for r in group) * (100 / risk if units != "Dollars" else 1) for price in prices]
        texts = [f'<span style="color:{display(v)["color"]}">{display(v, percent=units != "Dollars")["text"]}</span>' for v in values]
        fig.add_trace(go.Scatter(x=prices, y=values, text=texts, mode="lines", name="_".join(strategy.split("_")[:2]),
                                line={"color": palette[index % len(palette)], "width": 2.5, "dash": ["solid", "dash", "dot"][index % 3]},
                                hovertemplate="Underlying $%{x:.2f}<br>P&L %{text}<extra>%{fullData.name}</extra>"))
    for strike in strikes:
        fig.add_vline(x=strike, line_color="#1F273D", line_width=1, layer="below")
    spot = eligible[0].get("spot")
    if spot is not None and low <= spot <= high:
        fig.add_vline(x=spot, line_dash="dash", line_color="#94A3B8", annotation_text=f"Spot ${spot:.2f}")
    all_values = [v for trace in fig.data for v in trace.y]
    if all_values:
        ymin, ymax = min(all_values), max(all_values)
        ticks = sorted({0.0, *(ymin + (ymax - ymin) * i / 4 for i in range(5))})
        fig.update_yaxes(tickvals=ticks, ticktext=[
            f'<span style="color:{display(v)["color"]}">{display(v, percent=units != "Dollars")["text"]}</span>' for v in ticks])
    fig.add_hline(y=0, line_color="#94A3B8", line_width=1)
    fig.update_layout(template="plotly_dark", paper_bgcolor="#10141E", plot_bgcolor="#10141E",
                      margin={"l": 60, "r": 16, "t": 20, "b": 55}, height=330,
                      xaxis_title="Underlying price at expiration ($)",
                      yaxis_title="Total P&L (% opening risk)" if units != "Dollars" else "Total P&L ($)",
                      legend={"orientation": "h", "y": 1.12}, hovermode="x unified")
    return fig
