"""
20-Variant Iron Condor Strategy Incubator
==========================================

Defines a deterministic 20-variant parameter grid (`IC_V01` .. `IC_V20`) that
explores the 0DTE/1DTE Iron Condor space across:
  - Short-strike delta (`0.08`, `0.12`, `0.15`, `0.20`, `0.25`)
  - Wing width (`2`, `3`, `5`, `10`, `15` points)
  - Profit target (`25%`, `35%`, `50%`, `65%`)
  - Stop-loss multiplier (`1.5x`, `2.0x`, `2.5x`, `3.0x`)
  - Entry time window (`09:45`, `10:00`, `10:30`, `11:00`, `13:00`)

All 20 strategies share a single `MarketDataHub` instance so option chain and
quote lookups are deduplicated across the 5-minute execution tick, and each
strategy tracks its own owned option legs to prevent cross-strategy interference.
"""

from __future__ import annotations

import logging
import os
from typing import Any

from strategies.iron_condor import SPXIronCondor
from strategies.market_data_hub import MarketDataHub, get_shared_market_data_hub

logger = logging.getLogger(__name__)


# 20 distinct Iron Condor strategy configurations spanning conservative far-OTM
# scalpers, balanced core theta collectors, wide-wing high-credit variants, and
# afternoon low-decay variants.
INCUBATOR_GRID_20: list[dict[str, Any]] = [
    # --- Tier 1: Ultra-Conservative Far-OTM (Delta 0.08 - 0.10) ---
    {"strategy_name": "IC_V01_D08_W2_PT25_T0945", "short_delta": 0.08, "wing_width": 2.0,  "profit_target": 0.25, "stop_loss_mult": 1.5, "entry_time": "09:45", "dte": 0},
    {"strategy_name": "IC_V02_D08_W3_PT35_T1000", "short_delta": 0.08, "wing_width": 3.0,  "profit_target": 0.35, "stop_loss_mult": 2.0, "entry_time": "10:00", "dte": 0},
    {"strategy_name": "IC_V03_D10_W2_PT30_T1000", "short_delta": 0.10, "wing_width": 2.0,  "profit_target": 0.30, "stop_loss_mult": 1.5, "entry_time": "10:00", "dte": 0},
    {"strategy_name": "IC_V04_D10_W5_PT40_T1030", "short_delta": 0.10, "wing_width": 5.0,  "profit_target": 0.40, "stop_loss_mult": 2.0, "entry_time": "10:30", "dte": 0},

    # --- Tier 2: Moderate-Conservative (Delta 0.12) ---
    {"strategy_name": "IC_V05_D12_W2_PT25_T0945", "short_delta": 0.12, "wing_width": 2.0,  "profit_target": 0.25, "stop_loss_mult": 1.5, "entry_time": "09:45", "dte": 0},
    {"strategy_name": "IC_V06_D12_W3_PT35_T1000", "short_delta": 0.12, "wing_width": 3.0,  "profit_target": 0.35, "stop_loss_mult": 2.0, "entry_time": "10:00", "dte": 0},
    {"strategy_name": "IC_V07_D12_W5_PT50_T1030", "short_delta": 0.12, "wing_width": 5.0,  "profit_target": 0.50, "stop_loss_mult": 2.0, "entry_time": "10:30", "dte": 0},
    {"strategy_name": "IC_V08_D12_W5_PT35_T1100", "short_delta": 0.12, "wing_width": 5.0,  "profit_target": 0.35, "stop_loss_mult": 2.5, "entry_time": "11:00", "dte": 0},

    # --- Tier 3: Standard Core 15-Delta Benchmark Grid ---
    {"strategy_name": "IC_V09_D15_W2_PT30_T0945", "short_delta": 0.15, "wing_width": 2.0,  "profit_target": 0.30, "stop_loss_mult": 1.5, "entry_time": "09:45", "dte": 0},
    {"strategy_name": "IC_V10_D15_W2_PT50_T1000", "short_delta": 0.15, "wing_width": 2.0,  "profit_target": 0.50, "stop_loss_mult": 2.0, "entry_time": "10:00", "dte": 0},
    {"strategy_name": "IC_V11_D15_W3_PT35_T1000", "short_delta": 0.15, "wing_width": 3.0,  "profit_target": 0.35, "stop_loss_mult": 2.0, "entry_time": "10:00", "dte": 0},
    {"strategy_name": "IC_V12_D15_W5_PT50_T1030", "short_delta": 0.15, "wing_width": 5.0,  "profit_target": 0.50, "stop_loss_mult": 2.0, "entry_time": "10:30", "dte": 0},
    {"strategy_name": "IC_V13_D15_W10_PT40_T1100", "short_delta": 0.15, "wing_width": 10.0, "profit_target": 0.40, "stop_loss_mult": 2.5, "entry_time": "11:00", "dte": 0},
    {"strategy_name": "IC_V14_D15_W3_PT30_T1300", "short_delta": 0.15, "wing_width": 3.0,  "profit_target": 0.30, "stop_loss_mult": 1.5, "entry_time": "13:00", "dte": 0},

    # --- Tier 4: Dynamic High-Theta 20-Delta Grid ---
    {"strategy_name": "IC_V15_D20_W2_PT35_T1000", "short_delta": 0.20, "wing_width": 2.0,  "profit_target": 0.35, "stop_loss_mult": 1.5, "entry_time": "10:00", "dte": 0},
    {"strategy_name": "IC_V16_D20_W3_PT50_T1030", "short_delta": 0.20, "wing_width": 3.0,  "profit_target": 0.50, "stop_loss_mult": 2.0, "entry_time": "10:30", "dte": 0},
    {"strategy_name": "IC_V17_D20_W5_PT65_T1100", "short_delta": 0.20, "wing_width": 5.0,  "profit_target": 0.65, "stop_loss_mult": 2.5, "entry_time": "11:00", "dte": 0},
    {"strategy_name": "IC_V18_D20_W3_PT35_T1300", "short_delta": 0.20, "wing_width": 3.0,  "profit_target": 0.35, "stop_loss_mult": 2.0, "entry_time": "13:00", "dte": 0},

    # --- Tier 5: Aggressive / 1DTE Carry Comparison Grid ---
    {"strategy_name": "IC_V19_D25_W5_PT50_T1030", "short_delta": 0.25, "wing_width": 5.0,  "profit_target": 0.50, "stop_loss_mult": 2.5, "entry_time": "10:30", "dte": 0},
    {"strategy_name": "IC_V20_D15_W5_PT50_1DTE",  "short_delta": 0.15, "wing_width": 5.0,  "profit_target": 0.50, "stop_loss_mult": 3.0, "entry_time": "10:30", "dte": 1},
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
    "get_chains",
    "get_expiration",
    "get_strikes",
    "get_historical_prices",
    "get_greeks",
    "get_positions",
    "get_orders",
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
    return host


def build_strategy_variant(
    broker: Any,
    spec: dict[str, Any],
    market_data_hub: MarketDataHub | None = None,
    budget: float = 100_000.0,
    host_strategy: SPXIronCondor | None = None,
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

    strategy_name = str(merged_params.get("strategy_name", "SPXIronCondor"))
    variant_overrides = set(spec.keys()) | {"quantity", "market_data_hub", "strategy_name"}

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
    return strategy


def build_ab_strategies(
    broker: Any,
    market_data_hub: MarketDataHub | None = None,
    budget: float = 100_000.0,
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
    strat_a = build_strategy_variant(broker=broker, spec=spec_a, market_data_hub=hub, budget=budget)
    strat_b = build_strategy_variant(
        broker=broker, spec=spec_b, market_data_hub=hub, budget=budget, host_strategy=strat_a
    )
    return [strat_a, strat_b]


def build_incubator_strategies(
    broker: Any,
    market_data_hub: MarketDataHub | None = None,
    budget: float = 100_000.0,
    limit: int | None = None,
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
