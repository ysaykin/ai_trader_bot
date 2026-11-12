"""
Dynamic strategy comparison incubator.

Compare StrategyBro with dynamic peers, Neural Pure, and one plain IC control.
INCUBATOR_PROFILE=full_grid restores the parameter sweep. All strategies share
market data and retain their own ledger ownership. Excluded owners with active
packages continue managing positions without opening or adding exposure.
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
from strategies.manual_positions import MANUAL_OWNER

logger = logging.getLogger(__name__)


DEFAULT_EXPANDED_RUN_TIMES = (
    "09:45, 09:50, 09:55, 10:00, 10:15, 10:30, 10:45, "
    "11:00, 11:15, 11:30, 11:45, 12:00, 12:15, 12:30, 12:45, "
    "13:00, 13:15, 13:30, 13:45, 14:00, 14:15, 14:30, 14:45, "
    "15:00, 15:15, 15:30"
)

# 20 distinct Iron Condor strategy configurations spanning 5 DTE tiers
# (0, 7, 14, 21, 30 DTE) with 4 setup variations per tier.
INCUBATOR_GRID_20: list[dict[str, Any]] = [
    # --- Tier 1: 0 DTE (Same-Day Expiration) ---
    {"strategy_name": "IC_V01_0DTE_D10_W2_PT30_T0945", "short_delta": 0.10, "wing_width": 2.0, "profit_target": 0.30, "stop_loss_mult": 1.3, "entry_time": DEFAULT_EXPANDED_RUN_TIMES, "dte": 0},
    {"strategy_name": "IC_V02_0DTE_D12_W3_PT35_T1000", "short_delta": 0.12, "wing_width": 3.0, "profit_target": 0.35, "stop_loss_mult": 1.3, "entry_time": DEFAULT_EXPANDED_RUN_TIMES, "dte": 0},
    {"strategy_name": "IC_V03_0DTE_D15_W5_PT50_T1015", "short_delta": 0.15, "wing_width": 5.0, "profit_target": 0.50, "stop_loss_mult": 1.3, "entry_time": DEFAULT_EXPANDED_RUN_TIMES, "dte": 0},
    {"strategy_name": "IC_V04_0DTE_D20_W5_PT50_T1030", "short_delta": 0.20, "wing_width": 5.0, "profit_target": 0.50, "stop_loss_mult": 1.3, "entry_time": DEFAULT_EXPANDED_RUN_TIMES, "dte": 0},

    # --- Tier 2: 7 DTE (Weekly Swing) ---
    {"strategy_name": "IC_V05_7DTE_D10_W2_PT30_T0945", "short_delta": 0.10, "wing_width": 2.0, "profit_target": 0.30, "stop_loss_mult": 1.3, "entry_time": DEFAULT_EXPANDED_RUN_TIMES, "dte": 7},
    {"strategy_name": "IC_V06_7DTE_D12_W3_PT35_T1000", "short_delta": 0.12, "wing_width": 3.0, "profit_target": 0.35, "stop_loss_mult": 1.3, "entry_time": DEFAULT_EXPANDED_RUN_TIMES, "dte": 7},
    {"strategy_name": "IC_V07_7DTE_D15_W5_PT50_T1015", "short_delta": 0.15, "wing_width": 5.0, "profit_target": 0.50, "stop_loss_mult": 1.3, "entry_time": DEFAULT_EXPANDED_RUN_TIMES, "dte": 7},
    {"strategy_name": "IC_V08_7DTE_D20_W5_PT50_T1030", "short_delta": 0.20, "wing_width": 5.0, "profit_target": 0.50, "stop_loss_mult": 1.3, "entry_time": DEFAULT_EXPANDED_RUN_TIMES, "dte": 7},

    # --- Tier 3: 14 DTE (Bi-Weekly Swing) ---
    {"strategy_name": "IC_V09_14DTE_D10_W2_PT30_T0945", "short_delta": 0.10, "wing_width": 2.0, "profit_target": 0.30, "stop_loss_mult": 1.3, "entry_time": DEFAULT_EXPANDED_RUN_TIMES, "dte": 14},
    {"strategy_name": "IC_V10_14DTE_D12_W3_PT35_T1000", "short_delta": 0.12, "wing_width": 3.0, "profit_target": 0.35, "stop_loss_mult": 1.3, "entry_time": DEFAULT_EXPANDED_RUN_TIMES, "dte": 14},
    {"strategy_name": "IC_V11_14DTE_D15_W5_PT50_T1015", "short_delta": 0.15, "wing_width": 5.0, "profit_target": 0.50, "stop_loss_mult": 1.3, "entry_time": DEFAULT_EXPANDED_RUN_TIMES, "dte": 14},
    {"strategy_name": "IC_V12_14DTE_D20_W5_PT50_T1030", "short_delta": 0.20, "wing_width": 5.0, "profit_target": 0.50, "stop_loss_mult": 1.3, "entry_time": DEFAULT_EXPANDED_RUN_TIMES, "dte": 14},

    # --- Tier 4: 21 DTE (Three-Week Swing) ---
    {"strategy_name": "IC_V13_21DTE_D10_W2_PT30_T0945", "short_delta": 0.10, "wing_width": 2.0, "profit_target": 0.30, "stop_loss_mult": 1.3, "entry_time": DEFAULT_EXPANDED_RUN_TIMES, "dte": 21},
    {"strategy_name": "IC_V14_21DTE_D12_W3_PT35_T1000", "short_delta": 0.12, "wing_width": 3.0, "profit_target": 0.35, "stop_loss_mult": 1.3, "entry_time": DEFAULT_EXPANDED_RUN_TIMES, "dte": 21},
    {"strategy_name": "IC_V15_21DTE_D15_W5_PT50_T1015", "short_delta": 0.15, "wing_width": 5.0, "profit_target": 0.50, "stop_loss_mult": 1.3, "entry_time": DEFAULT_EXPANDED_RUN_TIMES, "dte": 21},
    {"strategy_name": "IC_V16_21DTE_D20_W5_PT50_T1030", "short_delta": 0.20, "wing_width": 5.0, "profit_target": 0.50, "stop_loss_mult": 1.3, "entry_time": DEFAULT_EXPANDED_RUN_TIMES, "dte": 21},

    # --- Tier 5: 30 DTE (Monthly Core) ---
    {"strategy_name": "IC_V17_30DTE_D10_W2_PT30_T0945", "short_delta": 0.10, "wing_width": 2.0, "profit_target": 0.30, "stop_loss_mult": 1.3, "entry_time": DEFAULT_EXPANDED_RUN_TIMES, "dte": 30},
    {"strategy_name": "IC_V18_30DTE_D12_W3_PT35_T1000", "short_delta": 0.12, "wing_width": 3.0, "profit_target": 0.35, "stop_loss_mult": 1.3, "entry_time": DEFAULT_EXPANDED_RUN_TIMES, "dte": 30},
    {"strategy_name": "IC_V19_30DTE_D15_W5_PT50_T1015", "short_delta": 0.15, "wing_width": 5.0, "profit_target": 0.50, "stop_loss_mult": 1.3, "entry_time": DEFAULT_EXPANDED_RUN_TIMES, "dte": 30},
    {"strategy_name": "IC_V20_30DTE_D20_W5_PT50_T1030", "short_delta": 0.20, "wing_width": 5.0, "profit_target": 0.50, "stop_loss_mult": 1.3, "entry_time": DEFAULT_EXPANDED_RUN_TIMES, "dte": 30},
]


def get_testing_variant_spec() -> dict[str, Any]:
    """Return spec for the experimental testing variant (25 DTE default, easily customizable)."""
    try:
        dte = int(os.getenv("TESTING_DTE", "25").strip())
    except ValueError:
        dte = 25
    try:
        delta = float(os.getenv("TESTING_DELTA", "0.15").strip())
    except ValueError:
        delta = 0.15
    try:
        wing = float(os.getenv("TESTING_WING", "5.0").strip())
    except ValueError:
        wing = 5.0
    try:
        pt = float(os.getenv("TESTING_PT", "0.50").strip())
    except ValueError:
        pt = 0.50
    try:
        sl = float(os.getenv("TESTING_SL", "1.3").strip())
    except ValueError:
        sl = 1.3
    entry_time = os.getenv("TESTING_ENTRY_TIME", os.getenv("IC_ENTRY_TIME", DEFAULT_EXPANDED_RUN_TIMES)).strip() or DEFAULT_EXPANDED_RUN_TIMES
    structure = os.getenv("TESTING_STRUCTURE", "iron_condor").strip().lower()
    clean_time = entry_time.split(",")[0].strip().replace(":", "")
    name = os.getenv(
        "TESTING_STRATEGY_NAME",
        f"IC_V21_{dte}DTE_D{int(delta*100):02d}_W{int(wing)}_PT{int(pt*100)}_T{clean_time}",
    )
    return {
        "strategy_name": name,
        "short_delta": delta,
        "wing_width": wing,
        "profit_target": pt,
        "stop_loss_mult": sl,
        "entry_time": entry_time,
        "dte": dte,
        "structure": structure,
        "is_testing_variant": True,
    }


def get_vertical_specs() -> list[dict[str, Any]]:
    """Return specifications for vertical spread test variants (Bull Put & Bear Call)."""
    vertical_entry = os.getenv("VERTICAL_ENTRY_TIME", os.getenv("IC_ENTRY_TIME", DEFAULT_EXPANDED_RUN_TIMES)).strip() or DEFAULT_EXPANDED_RUN_TIMES
    return [
        {
            "strategy_name": "IC_V23_7DTE_BULL_PUT_D15_W5",
            "short_delta": 0.15,
            "wing_width": 5.0,
            "profit_target": 0.50,
            "stop_loss_mult": 1.3,
            "entry_time": vertical_entry,
            "dte": 7,
            "structure": "bull_put",
        },
        {
            "strategy_name": "IC_V24_7DTE_BEAR_CALL_D15_W5",
            "short_delta": 0.15,
            "wing_width": 5.0,
            "profit_target": 0.50,
            "stop_loss_mult": 1.3,
            "entry_time": vertical_entry,
            "dte": 7,
            "structure": "bear_call",
        },
    ]


def get_neural_pure_spec() -> dict[str, Any]:
    """Return spec for the purely neural network managed strategy."""
    try:
        dte = int(os.getenv("NEURAL_STRATEGY_DTE", "7").strip())
    except ValueError:
        dte = 7
    try:
        delta = float(os.getenv("NEURAL_STRATEGY_DELTA", "0.15").strip())
    except ValueError:
        delta = 0.15
    try:
        wing = float(os.getenv("NEURAL_STRATEGY_WING", "5.0").strip())
    except ValueError:
        wing = 5.0
    entry_time = os.getenv("NEURAL_STRATEGY_ENTRY_TIME", os.getenv("IC_ENTRY_TIME", DEFAULT_EXPANDED_RUN_TIMES)).strip() or DEFAULT_EXPANDED_RUN_TIMES
    name = os.getenv("NEURAL_STRATEGY_NAME", f"IC_V22_NEURAL_PURE_{dte}DTE").strip() or "IC_V22_NEURAL_PURE"
    return {
        "strategy_name": name,
        "short_delta": delta,
        "wing_width": wing,
        "profit_target": 0.50,  # dynamic baseline, actively governed by policy net
        "stop_loss_mult": 1.3,
        "entry_time": entry_time,
        "dte": dte,
        "pure_neural_mode": True,
    }


def get_strategybro_spec() -> dict[str, Any]:
    """Return spec for StrategyBro 90 DTE Dynamic Iron Condor vs Vertical."""
    try:
        dte = int(os.getenv("STRATEGYBRO_DTE", "90").strip())
    except ValueError:
        dte = 90
    try:
        roll_dte = int(os.getenv("STRATEGYBRO_ROLL_DTE", "30").strip())
    except ValueError:
        roll_dte = 30
    try:
        wing = float(os.getenv("STRATEGYBRO_WING", "15.0").strip())
    except ValueError:
        wing = 15.0
    try:
        pt = float(os.getenv("STRATEGYBRO_PT", "0.50").strip())
    except ValueError:
        pt = 0.50
    try:
        sl = float(os.getenv("STRATEGYBRO_SL", "1.3").strip())
    except ValueError:
        sl = 1.3
    try:
        otm_pct = float(os.getenv("STRATEGYBRO_OTM_PCT", "0.07").strip())
    except ValueError:
        otm_pct = 0.07

    entry_time = os.getenv("STRATEGYBRO_ENTRY_TIME", os.getenv("IC_ENTRY_TIME", DEFAULT_EXPANDED_RUN_TIMES)).strip() or DEFAULT_EXPANDED_RUN_TIMES
    name = os.getenv("STRATEGYBRO_STRATEGY_NAME", f"IC_V25_{dte}DTE_STRATEGYBRO_DYNAMIC").strip() or f"IC_V25_{dte}DTE_STRATEGYBRO_DYNAMIC"

    return {
        "strategy_name": name,
        "short_delta": 0.15,
        "wing_width": wing,
        "profit_target": pt,
        "stop_loss_mult": sl,
        "entry_time": entry_time,
        "dte": dte,
        "roll_dte": roll_dte,
        "otm_pct": otm_pct,
        "otm_put_pct": otm_pct,
        "otm_call_pct": otm_pct,
        "structure": "dynamic_sd",
        "is_strategybro_variant": True,
        "enable_mfe_ratchet": True,
    }


def get_trendflow_spec() -> dict[str, Any]:
    """Return spec for TrendFlow 10 DTE Dynamic Momentum Credit Vertical (`IC_V26`)."""
    try:
        dte = int(os.getenv("TRENDFLOW_DTE", "10").strip())
    except ValueError:
        dte = 10
    try:
        roll_dte = int(os.getenv("TRENDFLOW_ROLL_DTE", "3").strip())
    except ValueError:
        roll_dte = 3
    try:
        delta = float(os.getenv("TRENDFLOW_DELTA", "0.16").strip())
    except ValueError:
        delta = 0.16
    try:
        wing = float(os.getenv("TRENDFLOW_WING", "5.0").strip())
    except ValueError:
        wing = 5.0
    try:
        pt = float(os.getenv("TRENDFLOW_PT", "0.50").strip())
    except ValueError:
        pt = 0.50
    try:
        sl = float(os.getenv("TRENDFLOW_SL", "1.3").strip())
    except ValueError:
        sl = 1.3
    try:
        min_sd = float(os.getenv("TRENDFLOW_MIN_SD", "0.35").strip())
    except ValueError:
        min_sd = 0.35

    entry_time = (
        os.getenv("TRENDFLOW_ENTRY_TIME", os.getenv("IC_ENTRY_TIME", DEFAULT_EXPANDED_RUN_TIMES)).strip()
        or DEFAULT_EXPANDED_RUN_TIMES
    )
    name = (
        os.getenv("TRENDFLOW_STRATEGY_NAME", f"IC_V26_{dte}DTE_TRENDFLOW_DYNAMIC").strip()
        or f"IC_V26_{dte}DTE_TRENDFLOW_DYNAMIC"
    )
    return {
        "strategy_name": name,
        "short_delta": delta,
        "wing_width": wing,
        "profit_target": pt,
        "stop_loss_mult": sl,
        "entry_time": entry_time,
        "dte": dte,
        "roll_dte": roll_dte,
        "min_trend_sd": min_sd,
        "structure": "trendflow_dynamic",
        "is_trendflow_variant": True,
        "enable_mfe_ratchet": True,
    }


def get_skew_dynamic_spec() -> dict[str, Any]:
    """Return spec for Asymmetric Skew Dynamic 14 DTE Iron Condor (`IC_V27`)."""
    try:
        dte = int(os.getenv("SKEW_DYNAMIC_DTE", "14").strip())
    except ValueError:
        dte = 14
    try:
        roll_dte = int(os.getenv("SKEW_DYNAMIC_ROLL_DTE", "5").strip())
    except ValueError:
        roll_dte = 5
    try:
        put_delta = float(os.getenv("SKEW_DYNAMIC_PUT_DELTA", "0.16").strip())
    except ValueError:
        put_delta = 0.16
    try:
        call_delta = float(os.getenv("SKEW_DYNAMIC_CALL_DELTA", "0.12").strip())
    except ValueError:
        call_delta = 0.12
    try:
        wing = float(os.getenv("SKEW_DYNAMIC_WING", "5.0").strip())
    except ValueError:
        wing = 5.0
    try:
        pt = float(os.getenv("SKEW_DYNAMIC_PT", "0.40").strip())
    except ValueError:
        pt = 0.40
    try:
        sl = float(os.getenv("SKEW_DYNAMIC_SL", "1.3").strip())
    except ValueError:
        sl = 1.3

    entry_time = (
        os.getenv("SKEW_DYNAMIC_ENTRY_TIME", os.getenv("IC_ENTRY_TIME", DEFAULT_EXPANDED_RUN_TIMES)).strip()
        or DEFAULT_EXPANDED_RUN_TIMES
    )
    name = (
        os.getenv("SKEW_DYNAMIC_STRATEGY_NAME", f"IC_V27_{dte}DTE_SKEW_DYNAMIC").strip()
        or f"IC_V27_{dte}DTE_SKEW_DYNAMIC"
    )
    return {
        "strategy_name": name,
        "short_delta": put_delta,
        "put_delta": put_delta,
        "call_delta": call_delta,
        "wing_width": wing,
        "profit_target": pt,
        "stop_loss_mult": sl,
        "entry_time": entry_time,
        "dte": dte,
        "roll_dte": roll_dte,
        "structure": "skew_dynamic",
        "is_skew_dynamic_variant": True,
        "enable_mfe_ratchet": True,
    }


def get_vol_crush_spec() -> dict[str, Any]:
    """Return spec for VolCrush Harvester 21 DTE Dynamic Strategy (`IC_V28`)."""
    try:
        dte = int(os.getenv("VOL_CRUSH_DTE", "21").strip())
    except ValueError:
        dte = 21
    try:
        roll_dte = int(os.getenv("VOL_CRUSH_ROLL_DTE", "7").strip())
    except ValueError:
        roll_dte = 7
    try:
        delta = float(os.getenv("VOL_CRUSH_DELTA", "0.12").strip())
    except ValueError:
        delta = 0.12
    try:
        wing = float(os.getenv("VOL_CRUSH_WING", "5.0").strip())
    except ValueError:
        wing = 5.0
    try:
        pt = float(os.getenv("VOL_CRUSH_PT", "0.45").strip())
    except ValueError:
        pt = 0.45
    try:
        sl = float(os.getenv("VOL_CRUSH_SL", "1.3").strip())
    except ValueError:
        sl = 1.3
    try:
        min_rv_ratio = float(os.getenv("VOL_CRUSH_MIN_RV_RATIO", "1.15").strip())
    except ValueError:
        min_rv_ratio = 1.15

    entry_time = (
        os.getenv("VOL_CRUSH_ENTRY_TIME", os.getenv("IC_ENTRY_TIME", DEFAULT_EXPANDED_RUN_TIMES)).strip()
        or DEFAULT_EXPANDED_RUN_TIMES
    )
    name = (
        os.getenv("VOL_CRUSH_STRATEGY_NAME", f"IC_V28_{dte}DTE_VOL_CRUSH_DYNAMIC").strip()
        or f"IC_V28_{dte}DTE_VOL_CRUSH_DYNAMIC"
    )
    return {
        "strategy_name": name,
        "short_delta": delta,
        "wing_width": wing,
        "profit_target": pt,
        "stop_loss_mult": sl,
        "entry_time": entry_time,
        "dte": dte,
        "roll_dte": roll_dte,
        "min_rv_ratio": min_rv_ratio,
        "structure": "vol_crush_dynamic",
        "is_vol_crush_variant": True,
        "enable_mfe_ratchet": True,
    }


def get_orb_dynamic_spec() -> dict[str, Any]:
    """Return spec for Opening-Range Breakout (ORB) 1 DTE Dynamic Strategy (`IC_V29`)."""
    try:
        dte = int(os.getenv("ORB_DYNAMIC_DTE", "1").strip())
    except ValueError:
        dte = 1
    try:
        delta = float(os.getenv("ORB_DYNAMIC_DELTA", "0.15").strip())
    except ValueError:
        delta = 0.15
    try:
        wing = float(os.getenv("ORB_DYNAMIC_WING", "5.0").strip())
    except ValueError:
        wing = 5.0
    try:
        pt = float(os.getenv("ORB_DYNAMIC_PT", "0.40").strip())
    except ValueError:
        pt = 0.40
    try:
        sl = float(os.getenv("ORB_DYNAMIC_SL", "1.3").strip())
    except ValueError:
        sl = 1.3
    try:
        thresh = float(os.getenv("ORB_DYNAMIC_THRESHOLD_PCT", "0.0030").strip())
    except ValueError:
        thresh = 0.0030

    entry_time = (
        os.getenv("ORB_DYNAMIC_ENTRY_TIME", os.getenv("IC_ENTRY_TIME", DEFAULT_EXPANDED_RUN_TIMES)).strip()
        or DEFAULT_EXPANDED_RUN_TIMES
    )
    name = (
        os.getenv("ORB_DYNAMIC_STRATEGY_NAME", f"IC_V29_{dte}DTE_ORB_DYNAMIC").strip()
        or f"IC_V29_{dte}DTE_ORB_DYNAMIC"
    )
    return {
        "strategy_name": name,
        "short_delta": delta,
        "wing_width": wing,
        "profit_target": pt,
        "stop_loss_mult": sl,
        "entry_time": entry_time,
        "dte": dte,
        "orb_threshold_pct": thresh,
        "structure": "orb_dynamic",
        "is_orb_dynamic_variant": True,
        "enable_mfe_ratchet": True,
    }


def get_dynamic_strategy_specs() -> list[dict[str, Any]]:
    """Return all enabled dynamic regime-adaptive strategy variants (`IC_V26`..`IC_V29`)."""
    specs: list[dict[str, Any]] = []
    if os.getenv("TRENDFLOW_ENABLED", "true").strip().lower() in ("true", "1", "yes", "on"):
        specs.append(get_trendflow_spec())
    if os.getenv("SKEW_DYNAMIC_ENABLED", "true").strip().lower() in ("true", "1", "yes", "on"):
        specs.append(get_skew_dynamic_spec())
    if os.getenv("VOL_CRUSH_ENABLED", "true").strip().lower() in ("true", "1", "yes", "on"):
        specs.append(get_vol_crush_spec())
    if os.getenv("ORB_DYNAMIC_ENABLED", "true").strip().lower() in ("true", "1", "yes", "on"):
        specs.append(get_orb_dynamic_spec())
    return specs


def get_incubator_specs(
    limit: int | None = None,
    include_testing: bool | None = None,
    include_neural: bool = True,
    include_verticals: bool | None = None,
    include_strategybro: bool = True,
    include_dynamic_suite: bool = True,
    *,
    profile: str | None = None,
) -> list[dict[str, Any]]:
    """Select entries, prioritizing dynamic comparisons before count limits.

    The dynamic profile retains one plain IC control. The full_grid profile also
    includes the legacy sweep and static tests. Explicit include flags override
    the profile's static-test defaults.
    """
    selected_profile = (profile if profile is not None else os.getenv("INCUBATOR_PROFILE", "dynamic")).strip().lower()
    if selected_profile not in {"dynamic", "full_grid"}:
        raise ValueError("INCUBATOR_PROFILE must be 'dynamic' or 'full_grid'")
    full_grid = selected_profile == "full_grid"
    if include_testing is None:
        include_testing = full_grid
    if include_verticals is None:
        include_verticals = full_grid
    global_entry_time = os.getenv("IC_ENTRY_TIME", DEFAULT_EXPANDED_RUN_TIMES).strip() or DEFAULT_EXPANDED_RUN_TIMES
    specs = []
    if include_strategybro and os.getenv("STRATEGYBRO_ENABLED", "true").strip().lower() in ("true", "1", "yes", "on"):
        specs.append(get_strategybro_spec())
    if include_dynamic_suite and os.getenv("DYNAMIC_STRATEGIES_ENABLED", "true").strip().lower() in ("true", "1", "yes", "on"):
        specs.extend(get_dynamic_strategy_specs())
    if include_neural and os.getenv("NEURAL_STRATEGY_ENABLED", "true").strip().lower() in ("true", "1", "yes", "on"):
        specs.append(get_neural_pure_spec())
    benchmarks = INCUBATOR_GRID_20 if full_grid else [INCUBATOR_GRID_20[18]]
    for spec in benchmarks:
        s = dict(spec)
        s["entry_time"] = global_entry_time
        specs.append(s)
    if include_testing:
        specs.append(get_testing_variant_spec())
    if include_verticals and os.getenv("VERTICAL_STRATEGIES_ENABLED", "true").strip().lower() in ("true", "1", "yes", "on"):
        specs.extend(get_vertical_specs())
    if limit is not None:
        return specs[: max(0, int(limit))]
    return specs


def _get_management_specs(
    entry_specs: list[dict[str, Any]], position_ledger: Any,
) -> list[dict[str, Any]]:
    """Restore excluded active owners, including disabled strategy families."""
    entry_names = {spec["strategy_name"] for spec in entry_specs}
    retired_names = sorted({basket.strategy_id for basket in position_ledger.open_baskets()} - entry_names - {MANUAL_OWNER})
    if not retired_names:
        return []
    catalog = [
        *INCUBATOR_GRID_20, get_testing_variant_spec(), get_neural_pure_spec(),
        *get_vertical_specs(), get_strategybro_spec(), get_trendflow_spec(),
        get_skew_dynamic_spec(), get_vol_crush_spec(), get_orb_dynamic_spec(),
    ]
    by_name = {spec["strategy_name"]: spec for spec in catalog}
    by_variant = {"_".join(spec["strategy_name"].split("_")[:2]): spec for spec in catalog}
    managers = []
    for name in retired_names:
        spec = by_name.get(name)
        if spec is None and name.startswith("IC_V"):
            spec = by_variant.get("_".join(name.split("_")[:2]))
        if spec is None:
            raise ValueError(f"Cannot restore active ledger owner {name!r}; restore its strategy configuration before starting")
        managers.append({**spec, "strategy_name": name, "management_only": True})
    return managers


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
    runs a single `StrategyExecutor` while executing the selected roster every tick.
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
        strategy = SPXIronCondor(
            broker=broker,
            data_source=broker.data_source,
            budget=budget,
            name=strategy_name,
            parameters=dict(merged_params),
        )

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
    strategy.pure_neural_mode = bool(spec.get("pure_neural_mode", False))
    strategy.is_testing_variant = bool(spec.get("is_testing_variant", False))
    strategy.is_strategybro_variant = bool(spec.get("is_strategybro_variant", False))
    strategy.is_trendflow_variant = bool(spec.get("is_trendflow_variant", False))
    strategy.is_skew_dynamic_variant = bool(spec.get("is_skew_dynamic_variant", False))
    strategy.is_vol_crush_variant = bool(spec.get("is_vol_crush_variant", False))
    strategy.is_orb_dynamic_variant = bool(spec.get("is_orb_dynamic_variant", False))
    strategy.structure = spec.get("structure", "iron_condor")
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
    default_entry = os.getenv("IC_ENTRY_TIME", DEFAULT_EXPANDED_RUN_TIMES).strip() or DEFAULT_EXPANDED_RUN_TIMES
    spec_a = {
        "strategy_name": os.getenv("AB_STRAT_A_NAME", "Strategy_A_Conservative"),
        "short_delta": float(os.getenv("AB_STRAT_A_DELTA", "0.10")),
        "wing_width": float(os.getenv("AB_STRAT_A_WIDTH", "2.0")),
        "profit_target": float(os.getenv("AB_STRAT_A_PT", "0.25")),
        "stop_loss_mult": float(os.getenv("AB_STRAT_A_SL", "1.3")),
        "entry_time": default_entry,
    }
    spec_b = {
        "strategy_name": os.getenv("AB_STRAT_B_NAME", "Strategy_B_Aggressive"),
        "short_delta": float(os.getenv("AB_STRAT_B_DELTA", "0.20")),
        "wing_width": float(os.getenv("AB_STRAT_B_WIDTH", "5.0")),
        "profit_target": float(os.getenv("AB_STRAT_B_PT", "0.50")),
        "stop_loss_mult": float(os.getenv("AB_STRAT_B_SL", "1.3")),
        "entry_time": default_entry,
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
    """Build the entry roster and retain managers for excluded active ledger owners."""
    hub = market_data_hub or get_shared_market_data_hub()
    specs = get_incubator_specs(limit=limit)
    entry_names = [spec["strategy_name"] for spec in specs]
    management_specs = _get_management_specs(specs, position_ledger) if position_ledger is not None else []
    specs.extend(management_specs)
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
        "Built %d incubator strategies sharing MarketDataHub (TTL=%ds)",
        len(strategies),
        hub.ttl_seconds,
    )
    logger.info("Entry-enabled incubator strategies (%d): %s", len(entry_names), ", ".join(entry_names))
    logger.info("Management-only incubator strategies (%d): %s", len(management_specs), ", ".join(spec["strategy_name"] for spec in management_specs) or "none")
    return strategies


def register_incubator_with_trader(
    trader: Any,
    broker: Any,
    market_data_hub: MarketDataHub | None = None,
    budget: float = 100_000.0,
    limit: int | None = None,
) -> list[SPXIronCondor]:
    """Instantiate and register the selected incubator roster with a Lumibot `Trader`."""
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
