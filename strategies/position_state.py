"""Position-owned management state and aggregate compatibility telemetry."""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any


@dataclass
class PositionState:
    position_id: str | None = None
    settings: dict[str, Any] = field(default_factory=dict)
    status: str = "SUBMITTED"
    position_open: bool = False
    entry_credit: float = 0.0
    entry_credit_cash: float = 0.0
    entry_orders: list[Any] = field(default_factory=list)
    closing_orders: list[Any] = field(default_factory=list)
    owned_legs: list[tuple[Any, int]] = field(default_factory=list)
    exit_requested: bool = False
    mfe_pnl: float = 0.0
    mae_pnl: float = 0.0
    last_pnl: float = 0.0
    current_trade_id: str | None = None
    pending_exit_reason: str | None = None
    pending_gross_pnl: float | None = None
    pending_fees: float | None = None
    pending_net_pnl: float | None = None
    journaled_adjustments: set[Any] = field(default_factory=set)


def position_attribute(name: str) -> property:
    """Keep existing strategy telemetry consumers while execution selects a position."""
    def get(strategy):
        states = list(getattr(strategy, "_position_states", {}).values())
        if len(states) < 2:
            return getattr(strategy._runtime(), name)
        values = [getattr(state, name) for state in states]
        if name in {"position_open", "exit_requested"}:
            return any(values)
        if name in {"entry_credit_cash", "last_pnl", "mfe_pnl", "mae_pnl"}:
            return sum(values)
        if name in {"entry_orders", "closing_orders"}:
            return [order for orders in values for order in orders]
        if name == "owned_legs":
            from strategies.position_ledger import asset_key
            quantities: dict[str, int] = defaultdict(int)
            assets = {}
            for legs in values:
                for asset, quantity in legs:
                    key = asset_key(asset)
                    assets[key] = asset
                    quantities[key] += quantity
            return [(assets[key], quantity) for key, quantity in quantities.items() if quantity]
        if name == "entry_credit":
            return 0.0  # Multiple independent entries have no single entry price.
        return None

    def set_value(strategy, value):
        setattr(strategy._runtime(), name, value)

    return property(get, set_value)
