"""Pure selection, valuation, and defined-risk math for iron condors."""

from __future__ import annotations

import math
import os
from collections.abc import Iterable, Sequence
from numbers import Real


def option_fee_per_contract(default: float = 0.40) -> float:
    """Return configured fee per option contract with safe fallback on missing/malformed input."""
    raw = os.getenv("OPTION_FEE_PER_CONTRACT")
    if raw is None or not str(raw).strip():
        return default
    try:
        val = float(raw)
        return val if val >= 0 else default
    except (TypeError, ValueError):
        return default


def option_execution_fees(signed_quantities: Iterable[Real], fee_rate: Real) -> float:
    """Calculate commissions for confirmed option contract executions."""
    rate = _finite_number(fee_rate, "fee_rate")
    if rate < 0:
        raise ValueError("fee_rate must be nonnegative")
    total = 0
    for quantity in signed_quantities:
        value = _finite_number(quantity, "quantity")
        if not value.is_integer():
            raise ValueError("contract quantities must be integers")
        total += abs(int(value))
    return round(total * rate, 2)


def select_short_strikes(
    strikes: Sequence[Real],
    underlying_price: Real,
    short_delta: Real,
    dte: Real,
    wing_width: Real,
    *,
    annual_volatility: Real = 0.16,
) -> tuple[float | None, float | None]:
    """Select the available OTM strikes nearest a bounded volatility offset."""
    price = _finite_positive(underlying_price, "underlying_price")
    delta = _finite_positive(short_delta, "short_delta")
    width = _finite_positive(wing_width, "wing_width")
    days = max(_finite_number(dte, "dte"), 0.25)
    volatility = _finite_positive(annual_volatility, "annual_volatility")
    available = sorted({_finite_number(strike, "strike") for strike in strikes})
    if not available:
        return None, None

    daily_volatility = volatility / math.sqrt(252.0)
    offset = price * daily_volatility * math.sqrt(days) * (0.15 / delta)
    offset = min(max(offset, width), price * 0.05)
    target_put = price - offset
    target_call = price + offset
    puts = (strike for strike in available if strike <= price)
    calls = (strike for strike in available if strike >= price)
    put = min(puts, key=lambda strike: abs(strike - target_put), default=None)
    call = min(calls, key=lambda strike: abs(strike - target_call), default=None)
    return put, call


def signed_basket_value(
    legs: Iterable[tuple[Real, Real | None]],
    *,
    multiplier: Real = 100.0,
) -> float | None:
    """Return signed marked value in cash, or ``None`` for any missing mark."""
    contract_multiplier = _finite_positive(multiplier, "multiplier")
    total = 0.0
    found = False
    for signed_quantity, mark in legs:
        found = True
        quantity = _finite_number(signed_quantity, "signed_quantity")
        if mark is None or isinstance(mark, bool) or not isinstance(mark, Real):
            return None
        price = float(mark)
        if not math.isfinite(price) or price < 0:
            return None
        total += quantity * price * contract_multiplier
    return total if found else None


def credit_strategy_pnl(
    opening_credit_cash: Real | None,
    signed_current_value: Real | None,
) -> float | None:
    """Return credit-strategy P&L as opening cash plus signed marked value."""
    if opening_credit_cash is None or signed_current_value is None:
        return None
    opening = _finite_number(opening_credit_cash, "opening_credit_cash")
    current = _finite_number(signed_current_value, "signed_current_value")
    return opening + current


def option_midpoint(
    bid: Real | None,
    ask: Real | None,
    last: Real | None = None,
) -> float | None:
    """Return quote midpoint when bid/ask bounds are valid, or fallback to last trade."""
    if bid is not None and ask is not None and not isinstance(bid, bool) and not isinstance(ask, bool):
        try:
            b, a = float(bid), float(ask)
            if math.isfinite(b) and math.isfinite(a) and b >= 0 and a >= b:
                return (b + a) / 2.0
        except (TypeError, ValueError):
            pass
    if last is not None and not isinstance(last, bool):
        try:
            l = float(last)
            if math.isfinite(l) and l >= 0:
                return l
        except (TypeError, ValueError):
            pass
    return None


def position_unrealized_pnl(
    quantity: Real,
    mark_price: Real,
    cost_basis: Real,
    *,
    multiplier: Real = 100.0,
) -> tuple[float, float]:
    """Calculate market value and unrealized P&L for long or short positions.

    Tradier convention:
    - Long: quantity > 0, cost_basis > 0 (cash paid), market_value > 0 (asset).
    - Short: quantity < 0, cost_basis < 0 (cash credit), market_value < 0 (liability).
    In both cases:
        market_value = quantity * mark_price * multiplier
        gain_loss = market_value - cost_basis
    """
    qty = float(quantity)
    price = float(mark_price)
    basis = float(cost_basis)
    mult = float(multiplier)
    market_value = qty * price * mult
    gain_loss = market_value - basis
    return market_value, gain_loss


def max_defined_loss(
    wing_width: Real,
    quantity: Real,
    opening_credit_cash: Real = 0.0,
    *,
    multiplier: Real = 100.0,
) -> float:
    """Return conservative maximum loss for a balanced defined-risk basket."""
    width = _finite_positive(wing_width, "wing_width")
    contracts = _finite_positive(quantity, "quantity")
    credit = _finite_number(opening_credit_cash, "opening_credit_cash")
    contract_multiplier = _finite_positive(multiplier, "multiplier")
    return max(width * contracts * contract_multiplier - max(credit, 0.0), 0.0)


def _finite_number(value: Real, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, Real):
        raise ValueError(f"{name} must be a real number")
    result = float(value)
    if not math.isfinite(result):
        raise ValueError(f"{name} must be finite")
    return result


def _finite_positive(value: Real, name: str) -> float:
    result = _finite_number(value, name)
    if result <= 0:
        raise ValueError(f"{name} must be positive")
    return result


__all__ = [
    "DEFAULT_MFE_TRAIL_LOCK_FRACTION",
    "calculate_realized_vol_ratio",
    "calculate_zscore",
    "credit_strategy_pnl",
    "evaluate_mfe_ratchet",
    "evaluate_strategybro_entry_quality",
    "max_defined_loss",
    "option_execution_fees",
    "option_fee_per_contract",
    "option_midpoint",
    "position_unrealized_pnl",
    "resolve_orb_structure",
    "resolve_skew_condor_deltas",
    "resolve_strategybro_structure",
    "resolve_trendflow_structure",
    "resolve_vol_crush_structure",
    "select_short_strikes",
    "select_short_strikes_otm_pct",
    "select_short_strikes_skew",
    "signed_basket_value",
]


def select_short_strikes_skew(
    strikes: Sequence[Real],
    underlying_price: Real,
    put_delta: Real,
    call_delta: Real,
    dte: Real,
    wing_width: Real,
    *,
    annual_volatility: Real = 0.16,
) -> tuple[float | None, float | None]:
    """Select asymmetric OTM short strikes using distinct put and call deltas."""
    price = _finite_positive(underlying_price, "underlying_price")
    p_delta = _finite_positive(put_delta, "put_delta")
    c_delta = _finite_positive(call_delta, "call_delta")
    width = _finite_positive(wing_width, "wing_width")
    days = max(_finite_number(dte, "dte"), 0.25)
    volatility = _finite_positive(annual_volatility, "annual_volatility")
    available = sorted({_finite_number(strike, "strike") for strike in strikes})
    if not available:
        return None, None

    daily_volatility = volatility / math.sqrt(252.0)
    put_offset = price * daily_volatility * math.sqrt(days) * (0.15 / p_delta)
    call_offset = price * daily_volatility * math.sqrt(days) * (0.15 / c_delta)
    put_offset = min(max(put_offset, width), price * 0.06)
    call_offset = min(max(call_offset, width), price * 0.06)

    target_put = price - put_offset
    target_call = price + call_offset

    available_set = set(available)
    puts = [s for s in available if s <= price and (s - width) in available_set]
    calls = [s for s in available if s >= price and (s + width) in available_set]
    if not puts:
        puts = [s for s in available if s <= price]
    if not calls:
        calls = [s for s in available if s >= price]
    if not puts or not calls:
        return None, None

    put = min(puts, key=lambda s: abs(s - target_put))
    call = min(calls, key=lambda s: abs(s - target_call))
    return put, call


def resolve_trendflow_structure(
    z_score: Real,
    *,
    min_trend_sd: Real = 0.35,
    max_trend_sd: Real = 2.50,
) -> str | None:
    """Resolve trend-following vertical spread structure (momentum complement to StrategyBro):

    - Positive trend (min_trend_sd <= z <= max_trend_sd) -> 'bull_put' (ride upward trend)
    - Negative trend (-max_trend_sd <= z <= -min_trend_sd) -> 'bear_call' (ride downward trend)
    - Neutral / choppy (|z| < min_trend_sd) or blowoff (|z| > max_trend_sd) -> None (skip)
    """
    z = _finite_number(z_score, "z_score")
    min_sd = _finite_positive(min_trend_sd, "min_trend_sd")
    max_sd = _finite_positive(max_trend_sd, "max_trend_sd")
    abs_z = abs(z)
    if abs_z < min_sd or abs_z > max_sd:
        return None
    return "bull_put" if z > 0 else "bear_call"


def resolve_skew_condor_deltas(
    z_score: Real,
    *,
    base_put_delta: Real = 0.16,
    base_call_delta: Real = 0.12,
) -> tuple[float, float]:
    """Resolve asymmetric (put_delta, call_delta) for Skew Dynamic Iron Condor:

    - Bullish drift (z > 0.25): (0.20, 0.10) — harvest rich put skew, keep calls far OTM
    - Bearish drift (z < -0.25): (0.10, 0.18) — protect downside puts, bring calls closer
    - Neutral (|z| <= 0.25): (base_put_delta, base_call_delta)
    """
    z = _finite_number(z_score, "z_score")
    p_base = _finite_positive(base_put_delta, "base_put_delta")
    c_base = _finite_positive(base_call_delta, "base_call_delta")
    if z > 0.25:
        return 0.20, 0.10
    if z < -0.25:
        return 0.10, 0.18
    return p_base, c_base


def calculate_realized_vol_ratio(
    prices: Sequence[Real],
    *,
    short_window: int = 5,
    long_window: int = 20,
) -> float:
    """Compute ratio of short-term realized volatility to long-term realized volatility.

    Returns 1.0 when fewer than `short_window + 1` prices are available or baseline vol is zero.
    """
    valid = [_finite_positive(p, "price") for p in prices]
    if len(valid) < max(short_window + 1, 3):
        return 1.0
    returns = [
        (valid[idx] - valid[idx - 1]) / valid[idx - 1]
        for idx in range(1, len(valid))
    ]
    short_rets = returns[-short_window:]
    long_rets = returns[-min(len(returns), max(long_window, short_window)):]
    if len(short_rets) < 2 or len(long_rets) < 2:
        return 1.0

    def _std(seq: list[float]) -> float:
        m = sum(seq) / len(seq)
        var = sum((x - m) ** 2 for x in seq) / (len(seq) - 1)
        return math.sqrt(max(var, 0.0))

    rv_short = _std(short_rets)
    rv_long = _std(long_rets)
    if rv_long <= 1e-12:
        return 1.0
    return rv_short / rv_long


def resolve_vol_crush_structure(
    z_score: Real,
    rv_ratio: Real,
    *,
    min_rv_ratio: Real = 1.15,
    min_abs_z: Real = 0.90,
) -> str | None:
    """Resolve VolCrush Harvester option structure during volatility expansion regimes:

    - Requires RV expansion (rv_ratio >= min_rv_ratio) or stretched Z-score (|z| >= min_abs_z).
    - Sharp pullback (z <= -min_abs_z): 'bull_put' (sell inflated put skew on panic flush)
    - Sharp spike (z >= 1.10): 'bear_call' (fade extreme overbought vol spike)
    - Elevated realized vol within range: 'iron_condor' (harvest two-sided vol crush)
    """
    z = _finite_number(z_score, "z_score")
    rv = _finite_positive(rv_ratio, "rv_ratio")
    req_rv = _finite_positive(min_rv_ratio, "min_rv_ratio")
    req_z = _finite_positive(min_abs_z, "min_abs_z")
    if rv < req_rv and abs(z) < req_z:
        return None
    if abs(z) > 2.50:
        return None
    if z <= -req_z:
        return "bull_put"
    if z >= max(req_z, 1.10):
        return "bear_call"
    return "iron_condor"


def resolve_orb_structure(
    intraday_return_pct: Real,
    *,
    threshold_pct: Real = 0.0030,
    max_move_pct: Real = 0.025,
) -> str | None:
    """Resolve 1 DTE Opening-Range Breakout (ORB) option structure from session return:

    - Runaway session (|return| > max_move_pct): None (avoid tail day)
    - Upward breakout (return >= +threshold_pct): 'bull_put' (sell puts below rally, no short calls)
    - Downward breakdown (return <= -threshold_pct): 'bear_call' (sell calls above drop, no short puts)
    - Range-bound session (|return| < threshold_pct): 'iron_condor' (collect dual-sided theta)
    """
    ret = _finite_number(intraday_return_pct, "intraday_return_pct")
    thresh = _finite_positive(threshold_pct, "threshold_pct")
    max_move = _finite_positive(max_move_pct, "max_move_pct")
    if abs(ret) > max_move:
        return None
    if ret >= thresh:
        return "bull_put"
    if ret <= -thresh:
        return "bear_call"
    return "iron_condor"


DEFAULT_MFE_TRAIL_LOCK_FRACTION: float = 0.65


def evaluate_mfe_ratchet(
    entry_credit_cash: Real,
    mfe_pnl: Real,
    current_net_pnl: Real,
    *,
    trail_trigger_pct: Real = 0.45,
    trail_lock_fraction: Real = DEFAULT_MFE_TRAIL_LOCK_FRACTION,
) -> str | None:
    """Trail positive profits only. A zero-P&L crossing is not an exit signal."""
    credit = _finite_positive(entry_credit_cash, "entry_credit_cash")
    mfe = _finite_number(mfe_pnl, "mfe_pnl")
    curr = _finite_number(current_net_pnl, "current_net_pnl")
    tr_trig = _finite_positive(trail_trigger_pct, "trail_trigger_pct")
    lock_frac = _finite_positive(trail_lock_fraction, "trail_lock_fraction")

    if mfe <= credit and mfe >= credit * tr_trig and curr <= mfe * lock_frac and curr > 0:
        return "MFE_TRAIL_LOCK"
    return None


def select_short_strikes_otm_pct(
    strikes: Sequence[Real],
    underlying_price: Real,
    otm_put_pct: Real,
    otm_call_pct: Real,
    wing_width: Real,
) -> tuple[float | None, float | None]:
    """Select the available OTM strikes nearest a target percentage distance from price.

    Args:
        strikes: Available strike prices in the chain.
        underlying_price: Current spot price of the underlying asset.
        otm_put_pct: Percentage OTM for short put (e.g. 0.07 for 7% OTM).
        otm_call_pct: Percentage OTM for short call (e.g. 0.07 for 7% OTM).
        wing_width: Distance in points to outer protective wing.

    Returns:
        (put_strike, call_strike) or (None, None) if valid strikes/wings unavailable.
    """
    price = _finite_positive(underlying_price, "underlying_price")
    put_pct = _finite_positive(otm_put_pct, "otm_put_pct")
    call_pct = _finite_positive(otm_call_pct, "otm_call_pct")
    width = _finite_positive(wing_width, "wing_width")
    available = sorted({_finite_number(strike, "strike") for strike in strikes})
    if not available:
        return None, None

    target_put = price * (1.0 - put_pct)
    target_call = price * (1.0 + call_pct)

    available_set = set(available)
    puts = [s for s in available if s <= price and (s - width) in available_set]
    calls = [s for s in available if s >= price and (s + width) in available_set]

    if not puts or not calls:
        return None, None

    put = min(puts, key=lambda s: abs(s - target_put))
    call = min(calls, key=lambda s: abs(s - target_call))
    return put, call


def calculate_zscore(
    prices: Sequence[Real],
    current_price: Real,
) -> float:
    """Calculate Z-score (standard deviations from mean) for price relative to a historical window.

    Z = (current_price - mean(prices)) / std(prices)
    Returns 0.0 if standard deviation is zero or prices sequence is too short (< 2).
    """
    curr = _finite_positive(current_price, "current_price")
    valid_prices = [_finite_positive(p, "price") for p in prices]
    if len(valid_prices) < 2:
        return 0.0

    mean = sum(valid_prices) / len(valid_prices)
    variance = sum((p - mean) ** 2 for p in valid_prices) / (len(valid_prices) - 1)
    if variance <= 1e-12:
        return 0.0
    std = math.sqrt(variance)
    return (curr - mean) / std


def evaluate_strategybro_entry_quality(
    abs_sd: Real,
) -> str:
    """Classify StrategyBro entry quality based on absolute standard deviation.

    - Excellent: <= 0.5 SD
    - Good: 0.5 - 1.0 SD
    - Marginal: 1.0 - 1.5 SD
    - Avoid: > 1.5 SD
    """
    val = abs(_finite_number(abs_sd, "abs_sd"))
    if val <= 0.5:
        return "EXCELLENT"
    if val <= 1.0:
        return "GOOD"
    if val <= 1.5:
        return "MARGINAL"
    return "AVOID"


def resolve_strategybro_structure(
    z_score: Real,
) -> str | None:
    """Determine preferred option structure based on StrategyBro preference rules:

    - Abs SD <= 0.60 -> Iron Condor
    - Abs SD > 0.60 & positive -> Bear Call Vertical (Sell Call Vertical)
    - Abs SD > 0.60 & negative -> Bull Put Vertical (Sell Put Vertical)
    - Abs SD > 1.50 -> None (Avoid Zone: do not trade)
    """
    z = _finite_number(z_score, "z_score")
    abs_z = abs(z)
    if abs_z > 1.5:
        return None  # Avoid zone
    if abs_z <= 0.60:
        return "iron_condor"
    if z > 0:
        return "bear_call"
    return "bull_put"
