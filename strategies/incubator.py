"""
20-Variant Iron Condor Strategy Incubator
==========================================

Defines a deterministic 20-variant parameter grid (`IC_V01` .. `IC_V20`) across
5 DTE tiers (0, 7, 14, 21, 30 DTE) with 4 setup variations per tier:
  - Setup 1 (Conservative Far-OTM): Delta 0.10, Wing 2.0, PT 30%, SL 1.5x, Entry 10:00
  - Setup 2 (Moderate Core Theta): Delta 0.12, Wing 3.0, PT 35%, SL 2.0x, Entry 10:15
  - Setup 3 (Standard 15-Delta Benchmark): Delta 0.15, Wing 5.0, PT 50%, SL 2.0x, Entry 10:30
  - Setup 4 (High-Yield 20-Delta): Delta 0.20, Wing 5.0, PT 50%, SL 2.5x, Entry 11:00

All 20 strategies share a single `MarketDataHub` instance so option chain and
quote lookups are deduplicated across the 5-minute execution tick, and each
strategy tracks its own owned option legs to prevent cross-strategy interference.
"""

from __future__ import annotations

import logging
import os
from typing import Any

try:
    from strategies.iron_condor import SPXIronCondor
except ImportError:
    SPXIronCondor = Any  # type: ignore[misc, assignment]
from strategies.market_data_hub import MarketDataHub, get_shared_market_data_hub

logger = logging.getLogger(__name__)


# 20 distinct Iron Condor strategy configurations spanning 5 DTE tiers
# (0, 7, 14, 21, 30 DTE) with 4 setup variations per tier.
INCUBATOR_GRID_20: list[dict[str, Any]] = [
    # --- Tier 1: 0 DTE (Same-Day Expiration) ---
    {"strategy_name": "IC_V01_0DTE_D10_W2_PT30_T1000", "short_delta": 0.10, "wing_width": 2.0, "profit_target": 0.30, "stop_loss_mult": 1.5, "entry_time": "10:00", "dte": 0},
    {"strategy_name": "IC_V02_0DTE_D12_W3_PT35_T1015", "short_delta": 0.12, "wing_width": 3.0, "profit_target": 0.35, "stop_loss_mult": 2.0, "entry_time": "10:15", "dte": 0},
    {"strategy_name": "IC_V03_0DTE_D15_W5_PT50_T1030", "short_delta": 0.15, "wing_width": 5.0, "profit_target": 0.50, "stop_loss_mult": 2.0, "entry_time": "10:30", "dte": 0},
    {"strategy_name": "IC_V04_0DTE_D20_W5_PT50_T1100", "short_delta": 0.20, "wing_width": 5.0, "profit_target": 0.50, "stop_loss_mult": 2.5, "entry_time": "11:00", "dte": 0},

    # --- Tier 2: 7 DTE (Weekly Swing) ---
    {"strategy_name": "IC_V05_7DTE_D10_W2_PT30_T1000", "short_delta": 0.10, "wing_width": 2.0, "profit_target": 0.30, "stop_loss_mult": 1.5, "entry_time": "10:00", "dte": 7},
    {"strategy_name": "IC_V06_7DTE_D12_W3_PT35_T1015", "short_delta": 0.12, "wing_width": 3.0, "profit_target": 0.35, "stop_loss_mult": 2.0, "entry_time": "10:15", "dte": 7},
    {"strategy_name": "IC_V07_7DTE_D15_W5_PT50_T1030", "short_delta": 0.15, "wing_width": 5.0, "profit_target": 0.50, "stop_loss_mult": 2.0, "entry_time": "10:30", "dte": 7},
    {"strategy_name": "IC_V08_7DTE_D20_W5_PT50_T1100", "short_delta": 0.20, "wing_width": 5.0, "profit_target": 0.50, "stop_loss_mult": 2.5, "entry_time": "11:00", "dte": 7},

    # --- Tier 3: 14 DTE (Bi-Weekly Swing) ---
    {"strategy_name": "IC_V09_14DTE_D10_W2_PT30_T1000", "short_delta": 0.10, "wing_width": 2.0, "profit_target": 0.30, "stop_loss_mult": 1.5, "entry_time": "10:00", "dte": 14},
    {"strategy_name": "IC_V10_14DTE_D12_W3_PT35_T1015", "short_delta": 0.12, "wing_width": 3.0, "profit_target": 0.35, "stop_loss_mult": 2.0, "entry_time": "10:15", "dte": 14},
    {"strategy_name": "IC_V11_14DTE_D15_W5_PT50_T1030", "short_delta": 0.15, "wing_width": 5.0, "profit_target": 0.50, "stop_loss_mult": 2.0, "entry_time": "10:30", "dte": 14},
    {"strategy_name": "IC_V12_14DTE_D20_W5_PT50_T1100", "short_delta": 0.20, "wing_width": 5.0, "profit_target": 0.50, "stop_loss_mult": 2.5, "entry_time": "11:00", "dte": 14},

    # --- Tier 4: 21 DTE (Three-Week Swing) ---
    {"strategy_name": "IC_V13_21DTE_D10_W2_PT30_T1000", "short_delta": 0.10, "wing_width": 2.0, "profit_target": 0.30, "stop_loss_mult": 1.5, "entry_time": "10:00", "dte": 21},
    {"strategy_name": "IC_V14_21DTE_D12_W3_PT35_T1015", "short_delta": 0.12, "wing_width": 3.0, "profit_target": 0.35, "stop_loss_mult": 2.0, "entry_time": "10:15", "dte": 21},
    {"strategy_name": "IC_V15_21DTE_D15_W5_PT50_T1030", "short_delta": 0.15, "wing_width": 5.0, "profit_target": 0.50, "stop_loss_mult": 2.0, "entry_time": "10:30", "dte": 21},
    {"strategy_name": "IC_V16_21DTE_D20_W5_PT50_T1100", "short_delta": 0.20, "wing_width": 5.0, "profit_target": 0.50, "stop_loss_mult": 2.5, "entry_time": "11:00", "dte": 21},

    # --- Tier 5: 30 DTE (Monthly Core) ---
    {"strategy_name": "IC_V17_30DTE_D10_W2_PT30_T1000", "short_delta": 0.10, "wing_width": 2.0, "profit_target": 0.30, "stop_loss_mult": 1.5, "entry_time": "10:00", "dte": 30},
    {"strategy_name": "IC_V18_30DTE_D12_W3_PT35_T1015", "short_delta": 0.12, "wing_width": 3.0, "profit_target": 0.35, "stop_loss_mult": 2.0, "entry_time": "10:15", "dte": 30},
    {"strategy_name": "IC_V19_30DTE_D15_W5_PT50_T1030", "short_delta": 0.15, "wing_width": 5.0, "profit_target": 0.50, "stop_loss_mult": 2.0, "entry_time": "10:30", "dte": 30},
    {"strategy_name": "IC_V20_30DTE_D20_W5_PT50_T1100", "short_delta": 0.20, "wing_width": 5.0, "profit_target": 0.50, "stop_loss_mult": 2.5, "entry_time": "11:00", "dte": 30},
]


def get_incubator_specs(limit: int | None = None) -> list[dict[str, Any]]:
    """Return a copy of the 20-strategy incubator parameter specifications."""
    specs = [dict(spec) for spec in INCUBATOR_GRID_20]
    if limit is not None:
        return specs[: max(0, int(limit))]
    return specs


_HOST_DELEGATED_METHODS = (
    "get_datetime",
    "get_last_price",
    "get_quote",
    "get_chains",
    "get_expiration",
    "get_strikes",
    "get_historical_prices",
    "get_greeks",
    "get_positions",
    "get_orders",
    "get_order",
    "create_order",
    "submit_order",
    "cancel_order",
)


def bind_follower_to_host(follower: SPXIronCondor, host: SPXIronCondor) -> SPXIronCondor:
    """Delegate broker I/O methods on a follower variant to the primary Lumibot host strategy."""
    follower._host_strategy = host
    follower.broker = getattr(host, "broker", None)
    for method_name in _HOST_DELEGATED_METHODS:
        if hasattr(host, method_name):
            setattr(follower, method_name, getattr(host, method_name))
    return follower


def attach_strategies_to_host(strategies: list[SPXIronCondor]) -> SPXIronCondor:
    """Attach follower strategies (`strategies[1:]`) to `strategies[0]` so Lumibot `Trader`
    runs a single `StrategyExecutor` while executing all 20 variants every 5-minute tick.
    """
    if not strategies:
        raise ValueError("strategies list must be non-empty")
    host = strategies[0]
    followers = list(strategies[1:])
    for follower in followers:
        bind_follower_to_host(follower, host)
    host._attached_strategies = followers
    host._strategy_lookup = {
        str(getattr(strategy, "name", type(strategy).__name__)): strategy
        for strategy in strategies
    }
    ledger = getattr(host, "_position_ledger", None)
    if ledger is not None:
        for strategy in strategies:
            strategy._position_ledger = ledger
    return host


def build_strategy_variant(
    broker: Any,
    spec: dict[str, Any],
    market_data_hub: MarketDataHub | None = None,
    budget: float = 100_000.0,
    host_strategy: SPXIronCondor | None = None,
    position_ledger: Any | None = None,
) -> SPXIronCondor:
    """
    Instantiate a single `SPXIronCondor` variant with locked per-variant parameters
    so `initialize()` preserves its unique configuration rather than overwriting
    with global `.env` defaults.
    """
    hub = market_data_hub or get_shared_market_data_hub()
    base_symbol = os.getenv("IC_SYMBOL", SPXIronCondor.parameters["symbol"])
    max_risk = float(os.getenv("IC_MAX_POSITION_RISK", str(SPXIronCondor.parameters["max_position_risk"])))

    merged_params = dict(SPXIronCondor.parameters)
    merged_params["symbol"] = base_symbol
    merged_params["max_position_risk"] = max_risk
    merged_params["quantity"] = 1
    merged_params.update(spec)
    merged_params["market_data_hub"] = hub
    if position_ledger is not None:
        merged_params["position_ledger"] = position_ledger

    strategy_name = str(merged_params.get("strategy_name", "SPXIronCondor"))
    variant_overrides = set(spec.keys()) | {
        "quantity", "market_data_hub", "position_ledger", "strategy_name"
    }

    if host_strategy is not None:
        strategy = object.__new__(SPXIronCondor)
        bind_follower_to_host(strategy, host_strategy)
    else:
        try:
            strategy = SPXIronCondor(
                broker=broker,
                budget=budget,
                name=strategy_name,
                parameters=dict(merged_params),
            )
        except TypeError:
            # Support lightweight test doubles whose Strategy.__init__ accepts no kwargs
            strategy = SPXIronCondor()

    try:
        strategy.name = strategy_name
    except AttributeError:
        strategy._name = strategy_name
    try:
        strategy.parameters = dict(merged_params)
    except AttributeError:
        strategy._parameters = dict(merged_params)
        strategy.__dict__["parameters"] = dict(merged_params)
    strategy._variant_overrides = variant_overrides
    strategy._market_data_hub = hub
    if position_ledger is not None:
        strategy._position_ledger = position_ledger
    return strategy


def build_ab_strategies(
    broker: Any,
    market_data_hub: MarketDataHub | None = None,
    budget: float = 100_000.0,
    position_ledger: Any | None = None,
) -> list[SPXIronCondor]:
    """Build 2 concurrent strategies (`Strategy_A_Conservative` vs `Strategy_B_Aggressive`) for A/B mode."""
    hub = market_data_hub or get_shared_market_data_hub()
    spec_a = {
        "strategy_name": os.getenv("AB_STRAT_A_NAME", "Strategy_A_Conservative"),
        "short_delta": float(os.getenv("AB_STRAT_A_DELTA", "0.10")),
        "wing_width": float(os.getenv("AB_STRAT_A_WIDTH", "2.0")),
        "profit_target": float(os.getenv("AB_STRAT_A_PT", "0.25")),
        "stop_loss_mult": float(os.getenv("AB_STRAT_A_SL", "1.5")),
        "entry_time": os.getenv("IC_ENTRY_TIME", "10:00"),
    }
    spec_b = {
        "strategy_name": os.getenv("AB_STRAT_B_NAME", "Strategy_B_Aggressive"),
        "short_delta": float(os.getenv("AB_STRAT_B_DELTA", "0.20")),
        "wing_width": float(os.getenv("AB_STRAT_B_WIDTH", "5.0")),
        "profit_target": float(os.getenv("AB_STRAT_B_PT", "0.50")),
        "stop_loss_mult": float(os.getenv("AB_STRAT_B_SL", "2.5")),
        "entry_time": os.getenv("IC_ENTRY_TIME", "10:00"),
    }
    strat_a = build_strategy_variant(
        broker=broker, spec=spec_a, market_data_hub=hub, budget=budget,
        position_ledger=position_ledger,
    )
    strat_b = build_strategy_variant(
        broker=broker, spec=spec_b, market_data_hub=hub, budget=budget,
        host_strategy=strat_a, position_ledger=position_ledger,
    )
    return [strat_a, strat_b]


def build_incubator_strategies(
    broker: Any,
    market_data_hub: MarketDataHub | None = None,
    budget: float = 100_000.0,
    limit: int | None = None,
    position_ledger: Any | None = None,
) -> list[SPXIronCondor]:
    """
    Build all 20 Iron Condor incubator strategies attached to a shared `MarketDataHub`.
    """
    hub = market_data_hub or get_shared_market_data_hub()
    specs = get_incubator_specs(limit=limit)
    strategies: list[SPXIronCondor] = []
    for idx, spec in enumerate(specs):
        host = strategies[0] if idx > 0 else None
        strategies.append(
            build_strategy_variant(
                broker=broker,
                spec=spec,
                market_data_hub=hub,
                budget=budget,
                host_strategy=host,
                position_ledger=position_ledger,
            )
        )
    logger.info(
        "Built %d Iron Condor incubator variants sharing MarketDataHub (TTL=%ds)",
        len(strategies),
        hub.ttl_seconds,
    )
    return strategies


def register_incubator_with_trader(
    trader: Any,
    broker: Any,
    market_data_hub: MarketDataHub | None = None,
    budget: float = 100_000.0,
    limit: int | None = None,
) -> list[SPXIronCondor]:
    """Instantiate and register all 20 incubator strategies with a Lumibot `Trader`."""
    strategies = build_incubator_strategies(
        broker=broker,
        market_data_hub=market_data_hub,
        budget=budget,
        limit=limit,
    )
    if strategies:
        host = attach_strategies_to_host(strategies)
        trader.add_strategy(host)
    return strategies
