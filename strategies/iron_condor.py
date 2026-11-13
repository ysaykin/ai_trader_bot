"""
SPX / XSP Iron Condor Strategy for Lumibot
===========================================

Sells a delta-neutral iron condor on SPX (or XSP) options with configurable:
  - DTE (0DTE, 1DTE, weeklies, monthlies)
  - Short-strike delta targeting
  - Wing width
  - Profit target & stop loss
  - Entry time window
  - Position sizing (max allocation)

All parameters are driven from environment variables so you can tune them
without touching code — just edit .env and restart.

Iron Condor Structure:
  BUY  put  @ (short_put_strike - wing_width)   ← long put (protection)
  SELL put  @ short_put_strike                   ← short put (credit)
  SELL call @ short_call_strike                  ← short call (credit)
  BUY  call @ (short_call_strike + wing_width)   ← long call (protection)

The strategy profits when SPX stays between the two short strikes at expiration.
Max profit = net credit received.
Max loss   = wing_width - net credit (per contract).
"""

from __future__ import annotations

import os
import logging
import json
import math
import re
import uuid
from decimal import Decimal
from collections import defaultdict
from datetime import date, datetime, time, timedelta, timezone
from typing import Any, Iterable
from strategies.broker_reads import fresh_broker_reads

from lumibot.strategies.strategy import Strategy
from lumibot.entities import Asset

from strategies.iron_condor_math import (
    DEFAULT_MFE_TRAIL_LOCK_FRACTION,
    calculate_realized_vol_ratio,
    calculate_zscore,
    credit_strategy_pnl,
    evaluate_mfe_ratchet,
    evaluate_strategybro_entry_quality,
    max_defined_loss,
    option_execution_fees,
    option_fee_per_contract,
    resolve_orb_structure,
    resolve_skew_condor_deltas,
    resolve_strategybro_structure,
    resolve_trendflow_structure,
    resolve_vol_crush_structure,
    select_short_strikes,
    select_short_strikes_otm_pct,
    select_short_strikes_skew,
)
from strategies.position_ledger import LedgerLeg, PositionLedger, TERMINAL_STATES, asset_key
from strategies.manual_positions import MANUAL_OWNER
from strategies.position_state import CancellationRetry, PositionState, position_attribute
from strategies.order_sessions import EASTERN, day_session_ended, session_bounds
from strategies.exit_safety import bounded_option_value

PROFIT_BOUNDED_EXIT_REASONS = frozenset({"PROFIT_TARGET", "MFE_TRAIL_LOCK", "BREAKEVEN_RATCHET"})
PNL_EXIT_REASONS = PROFIT_BOUNDED_EXIT_REASONS | {"STOP_LOSS"}

logger = logging.getLogger(__name__)


def parse_single_time(item: Any) -> time:
    """Parse a single time representation (e.g. '09:45', '10', '1015', time(9, 45))."""
    if isinstance(item, time):
        return item
    if isinstance(item, datetime):
        return item.time()
    s = str(item).strip().lower()
    for noise in ("and", "at", "am"):
        if s.startswith(noise + " "):
            s = s[len(noise) + 1:].strip()
        if s.endswith(" " + noise):
            s = s[:-len(noise) - 1].strip()
    s = s.replace("am", "").strip()
    is_pm = "pm" in s
    s = s.replace("pm", "").strip()

    if ":" in s:
        parts = s.split(":")
        h, m = int(parts[0]), int(parts[1])
    elif s.isdigit():
        if len(s) <= 2:
            h, m = int(s), 0
        elif len(s) == 3:
            h, m = int(s[0]), int(s[1:])
        elif len(s) == 4:
            h, m = int(s[:2]), int(s[2:])
        else:
            raise ValueError(f"Invalid time format: {item}")
    else:
        raise ValueError(f"Cannot parse time: {item}")

    if is_pm and h < 12:
        h += 12
    return time(h, m)


DEFAULT_RUN_TIMES_LIST: list[time] = [
    time(9, 45), time(9, 50), time(9, 55), time(10, 0),
    time(10, 15), time(10, 30), time(10, 45),
    time(11, 0), time(11, 15), time(11, 30), time(11, 45),
    time(12, 0), time(12, 15), time(12, 30), time(12, 45),
    time(13, 0), time(13, 15), time(13, 30), time(13, 45),
    time(14, 0), time(14, 15), time(14, 30), time(14, 45),
    time(15, 0), time(15, 15), time(15, 30),
]
DEFAULT_RUN_TIMES_STR: str = ", ".join(t.strftime("%H:%M") for t in DEFAULT_RUN_TIMES_LIST)


def parse_run_times(value: Any) -> list[time]:
    """Parse one or multiple execution run times into a sorted list of unique datetime.time objects.

    Supports comma/semicolon/and-separated strings, lists, tuples, or single times.
    Example: '9:45, 10, 1015, and 1030' -> [time(9, 45), time(10, 0), time(10, 15), time(10, 30)]
    """
    if not value:
        return list(DEFAULT_RUN_TIMES_LIST)
    if isinstance(value, (list, tuple, set)):
        times: list[time] = []
        for v in value:
            times.extend(parse_run_times(v))
        return sorted(list(set(times)))
    if isinstance(value, (time, datetime)):
        return [parse_single_time(value)]

    raw = str(value)
    normalized = raw.replace(" and ", ",").replace(";", ",").replace("&", ",")
    tokens = [tok.strip() for tok in normalized.split(",") if tok.strip()]
    if not tokens:
        tokens = [raw.strip()]

    parsed: list[time] = []
    for tok in tokens:
        try:
            parsed.append(parse_single_time(tok))
        except (ValueError, TypeError) as err:
            logger.warning("Could not parse run time '%s': %s", tok, err)

    return sorted(list(set(parsed))) or list(DEFAULT_RUN_TIMES_LIST)


def get_max_single_trade_pct(max_position_risk: float) -> float:
    """Calculate the max risk percentage allowed for any single trade based on total risk cap.

    Sliding scale:
      <= $5,000: 30% ($1,500 max)
      <= $10,000: 20% ($2,000 max)
      <= $25,000: 15% ($3,750 max)
      <= $50,000: 10% ($5,000 max)
      > $50,000:  5%
    """
    if max_position_risk <= 5000:
        return 0.30
    elif max_position_risk <= 10000:
        return 0.20
    elif max_position_risk <= 25000:
        return 0.15
    elif max_position_risk <= 50000:
        return 0.10
    else:
        return 0.05


class SPXIronCondor(Strategy):
    """
    Automated SPX/XSP Iron Condor strategy.

    Lifecycle:
      1. Wait until the configured entry time window.
      2. If no open iron condor, query the options chain for the target DTE.
      3. Find short strikes near the target delta (or nearest available).
      4. Submit the 4-leg order.
      5. Monitor for profit target or stop loss.
      6. Close at profit target, stop loss, or let expire worthless.
    """

    # ------------------------------------------------------------------ #
    # Default parameters (overridden by env vars in initialize)
    # ------------------------------------------------------------------ #
    parameters = {
        "symbol": "SPX",
        "dte": 0,
        "short_delta": 0.15,
        "wing_width": 15,
        "quantity": 1,
        "profit_target": 0.30,
        "stop_loss_mult": 1.3,
        "entry_time": DEFAULT_RUN_TIMES_STR,
        "max_allocation": 0.03,
        "max_position_risk": 5000.0,
        "structure": "iron_condor",
        "max_concurrent_positions": 1,
        "entry_spacing_minutes": 60.0,
        "stop_cooldown_minutes": 60.0,
        "min_credit_to_width": 0.10,
    }

    _position_open = position_attribute("position_open")
    _entry_credit = position_attribute("entry_credit")
    _entry_credit_cash = position_attribute("entry_credit_cash")
    _entry_orders = position_attribute("entry_orders")
    _closing_orders = position_attribute("closing_orders")
    _owned_legs = position_attribute("owned_legs")
    _exit_requested = position_attribute("exit_requested")
    _mfe_pnl = position_attribute("mfe_pnl")
    _mae_pnl = position_attribute("mae_pnl")
    _last_pnl = position_attribute("last_pnl")
    _current_trade_id = position_attribute("current_trade_id")
    _pending_exit_reason = position_attribute("pending_exit_reason")
    _pending_gross_pnl = position_attribute("pending_gross_pnl")
    _pending_fees = position_attribute("pending_fees")
    _pending_net_pnl = position_attribute("pending_net_pnl")

    _iteration_count: int = 0

    def initialize(self) -> None:
        """Load parameters from env vars (unless explicitly overridden per variant) and set up state."""
        p = self.parameters
        defaults = type(self).parameters

        def _resolve_param(key: str, env_var: str, cast_fn):
            # If the instance parameter was explicitly customized away from the class default
            # (or locked via `_variant_overrides`), preserve the variant-specific value.
            variant_overrides = getattr(self, "_variant_overrides", set())
            if key in variant_overrides or (key in p and key in defaults and p[key] != defaults[key]):
                return cast_fn(p[key])
            env_val = os.getenv(env_var)
            if env_val is not None and env_val != "":
                return cast_fn(env_val)
            return cast_fn(p.get(key, defaults.get(key)))

        p["symbol"] = _resolve_param("symbol", "IC_SYMBOL", str)
        p["dte"] = _resolve_param("dte", "IC_DTE", int)
        p["short_delta"] = _resolve_param("short_delta", "IC_SHORT_DELTA", float)
        p["wing_width"] = _resolve_param("wing_width", "IC_WING_WIDTH", float)
        p["quantity"] = _resolve_param("quantity", "IC_QUANTITY", int)
        p["profit_target"] = _resolve_param("profit_target", "IC_PROFIT_TARGET", float)
        p["stop_loss_mult"] = _resolve_param("stop_loss_mult", "IC_STOP_LOSS_MULT", float)
        p["entry_time"] = _resolve_param("entry_time", "IC_ENTRY_TIME", str)
        p["max_allocation"] = _resolve_param("max_allocation", "IC_MAX_ALLOCATION", float)
        p["max_position_risk"] = _resolve_param("max_position_risk", "IC_MAX_POSITION_RISK", float)
        p["max_concurrent_positions"] = _resolve_param("max_concurrent_positions", "IC_MAX_CONCURRENT_POSITIONS", int)
        p["entry_spacing_minutes"] = _resolve_param("entry_spacing_minutes", "IC_ENTRY_SPACING_MINUTES", float)
        p["stop_cooldown_minutes"] = _resolve_param("stop_cooldown_minutes", "IC_STOP_COOLDOWN_MINUTES", float)
        p["min_credit_to_width"] = _resolve_param("min_credit_to_width", "IC_MIN_CREDIT_TO_WIDTH", float)
        if not math.isfinite(p["stop_cooldown_minutes"]) or p["stop_cooldown_minutes"] < 0:
            raise ValueError("stop cooldown must be finite and nonnegative")
        if not math.isfinite(p["min_credit_to_width"]) or not 0 < p["min_credit_to_width"] < 1:
            raise ValueError("minimum credit to width must be between zero and one")
        if p["max_concurrent_positions"] < 1 or not math.isfinite(p["entry_spacing_minutes"]) or p["entry_spacing_minutes"] < 0:
            raise ValueError("position capacity must be positive and entry spacing nonnegative")
        self._position_states: dict[str, PositionState] = {}
        self._unbound_position = PositionState(settings=dict(p))
        p["structure"] = _resolve_param("structure", "IC_STRUCTURE", str) or "iron_condor"
        self.structure = str(p["structure"]).lower()
        if "strategy_name" in p and p["strategy_name"]:
            try:
                self.name = str(p["strategy_name"])
            except AttributeError:
                self._name = str(p["strategy_name"])
        self._market_data_hub = p.get("market_data_hub", getattr(self, "_market_data_hub", None))
        self.pure_neural_mode = bool(
            p.get("pure_neural_mode", getattr(self, "pure_neural_mode", False))
        )
        self.is_testing_variant = bool(
            p.get("is_testing_variant", getattr(self, "is_testing_variant", False))
        )
        self.is_strategybro_variant = bool(
            p.get("is_strategybro_variant", getattr(self, "is_strategybro_variant", False))
        )
        self.is_trendflow_variant = bool(
            p.get("is_trendflow_variant", getattr(self, "is_trendflow_variant", False))
        )
        self.is_skew_dynamic_variant = bool(
            p.get("is_skew_dynamic_variant", getattr(self, "is_skew_dynamic_variant", False))
        )
        self.is_vol_crush_variant = bool(
            p.get("is_vol_crush_variant", getattr(self, "is_vol_crush_variant", False))
        )
        self.is_orb_dynamic_variant = bool(
            p.get("is_orb_dynamic_variant", getattr(self, "is_orb_dynamic_variant", False))
        )
        self._active_put_delta: float | None = None
        self._active_call_delta: float | None = None
        self._session_anchor_date: date | None = None
        self._session_anchor_price: float | None = None
        if "roll_dte" in p and p["roll_dte"] is not None:
            p["roll_dte"] = int(p["roll_dte"])
        if "otm_pct" in p and p["otm_pct"] is not None:
            p["otm_pct"] = float(p["otm_pct"])
        if "otm_put_pct" in p and p["otm_put_pct"] is not None:
            p["otm_put_pct"] = float(p["otm_put_pct"])
        if "otm_call_pct" in p and p["otm_call_pct"] is not None:
            p["otm_call_pct"] = float(p["otm_call_pct"])

        # Parse entry and execution run times
        raw_times = p.get("run_times") or p.get("entry_times") or p.get("entry_time") or DEFAULT_RUN_TIMES_STR
        self._entry_times = parse_run_times(raw_times)
        self._entry_time = self._entry_times[0] if self._entry_times else time(9, 45)

        # Check frequently so we don't miss the entry window
        try:
            self.sleeptime = "5M"  # every 5 minutes
        except AttributeError:
            self._sleeptime = "5M"

        # Trade state is hydrated from confirmed ledger fills, never account-net positions.
        self._position_ledger = p.get(
            "position_ledger", getattr(self, "_position_ledger", None)
        ) or PositionLedger(":memory:")
        self._asset_registry = getattr(self, "_asset_registry", {})
        self._position_open = False
        self._entry_credit = 0.0
        self._entry_credit_cash = 0.0
        self._entry_orders = []
        self._closing_orders = []
        self._owned_legs = []
        self._exit_requested = False
        self._iteration_count = 0
        self._mfe_pnl = 0.0
        self._mae_pnl = 0.0
        self._last_pnl = 0.0
        self._last_action = None
        self._startup_reconciled = False
        self._recovery_lock = False
        self._current_trade_id = None
        self._pending_exit_reason = None
        self._pending_gross_pnl = None
        self._pending_fees = None
        self._pending_net_pnl = None
        self._terminal_package_times: dict[str, datetime] = {}
        self._cancellation_retries: dict[str, CancellationRetry] = {}
        self._close_reprice_seconds = max(
            float(os.getenv("IC_CLOSE_REPRICE_SECONDS", "60")), 0.0
        )
        self._close_max_reprices = max(
            int(os.getenv("IC_CLOSE_MAX_REPRICES", "3")), 0
        )
        self._close_price_step = max(
            float(os.getenv("IC_CLOSE_PRICE_STEP", "0.05")), 0.0
        )
        self._close_allow_market = os.getenv(
            "IC_CLOSE_ALLOW_MARKET", "false"
        ).strip().lower() in {"true", "1", "yes", "on"}

        # Define the underlying asset
        # Note: Tradier quotes index tickers (SPX, XSP) without the 'I:' index prefix.
        broker_name = getattr(getattr(self, "broker", None), "name", "").lower()
        env_broker = os.getenv("TRADING_BROKER", "").lower()
        is_tradier = "tradier" in broker_name or "tradier" in env_broker
        asset_type = Asset.AssetType.STOCK if is_tradier else (
            Asset.AssetType.INDEX
            if str(p["symbol"]).upper() in {"SPX", "XSP"}
            else Asset.AssetType.STOCK
        )
        self._underlying = Asset(
            symbol=p["symbol"],
            asset_type=asset_type,
        )
        self._hydrate_from_ledger()

        logger.info(
            "Iron Condor initialized: %s | DTE=%d | Δ=%.2f | width=%.0f | "
            "qty=%d | PT=%.0f%% | SL=%.0fx | MaxPosCap=$%.0f",
            p["symbol"], p["dte"], p["short_delta"], p["wing_width"],
            p["quantity"], p["profit_target"] * 100, p["stop_loss_mult"],
            p["max_position_risk"],
        )
        logger.info(
            "[%s] Entry limits: max_positions=%d | spacing_minutes=%.0f | management_only=%s",
            self._strategy_id(), p["max_concurrent_positions"], p["entry_spacing_minutes"],
            bool(p.get("management_only", False)),
        )

        for follower in getattr(self, "_attached_strategies", ()):
            try:
                follower.initialize()
            except Exception as err:
                logger.error("Failed to initialize attached strategy %s: %s", getattr(follower, "name", follower), err)

    # ------------------------------------------------------------------ #
    # Main trading loop
    # ------------------------------------------------------------------ #

    def on_trading_iteration(self) -> None:
        """Called every 5 minutes during market hours."""
        self._iteration_count = int(getattr(self, "_iteration_count", 0)) + 1
        self._last_action = None
        was_open = self._position_open
        try:
            self._run_trading_iteration()
        except Exception as error:
            logger.exception("Unexpected strategy iteration failure: %s", error)
            self._publish_snapshot(
                pnl=float(getattr(self, "_last_pnl", 0.0)),
                status="MANAGEMENT_ERROR",
            )
        finally:
            action = getattr(self, "_last_action", None) or "HOLD"
            if self._exit_requested:
                action = "FLATTEN_NOW"
            elif not was_open and self._position_open:
                action = "ENTER"
            self._record_trajectory(action)
            for follower in getattr(self, "_attached_strategies", ()):
                try:
                    follower.on_trading_iteration()
                except Exception as err:
                    logger.error("Error in attached strategy %s iteration: %s", getattr(follower, "name", follower), err)
            self._last_iteration_completed_at = datetime.now(timezone.utc)

    @property
    def on_trading_iteration_count(self) -> int:
        """Expose iteration count for lifecycle telemetry and backward compatibility."""
        return int(getattr(self, "_iteration_count", 0))

    @on_trading_iteration_count.setter
    def on_trading_iteration_count(self, value: int | None) -> None:
        self._iteration_count = int(value or 0)

    @property
    def run_times(self) -> list[time]:
        """Configured execution run times for this strategy."""
        return list(getattr(self, "_entry_times", None) or DEFAULT_RUN_TIMES_LIST)

    @run_times.setter
    def run_times(self, value: Any) -> None:
        self._entry_times = parse_run_times(value)
        if self._entry_times:
            self._entry_time = self._entry_times[0]
            time_str = ", ".join(t.strftime("%H:%M") for t in self._entry_times)
            self.parameters["entry_time"] = time_str
            self.parameters["run_times"] = time_str

    @property
    def run_times_str(self) -> str:
        """Formatted string representation of configured run times."""
        return ", ".join(t.strftime("%H:%M") for t in self.run_times)

    def set_run_times(self, run_times: Any) -> None:
        """Configure the execution run times for this strategy."""
        self.run_times = run_times

    def _record_trajectory(self, action: str, *, session_end: bool = False) -> None:
        """Keep telemetry failures independent of position management."""
        collector = getattr(self, "_trajectory_logger", None)
        if collector is not None:
            try:
                collector.record(self, action, session_end=session_end)
            except Exception as error:
                logger.warning("Could not persist trajectory: %s", error)

    def _panic_recovery(self) -> None:
        """Retry account liquidation under the shared execution gate."""
        try:
            with self._position_ledger.execution_gate():
                if self._position_ledger.is_panic_halted():
                    self._position_ledger.enqueue_recovery_job(scope="account")
        except Exception as error:
            logger.error("Panic recovery failed: %s", error)

    def _run_trading_iteration(self) -> None:
        """Execute the trading tick before publishing its trajectory observation."""
        halted = self._position_ledger.is_panic_halted()
        reconciled = self._reconcile_broker_state()
        if halted:
            self._panic_recovery()
            return
        if not reconciled:
            return
        now = self.get_datetime()
        queued = getattr(self, "_queued_policy_action", None)
        if queued is not None:
            self._queued_policy_action = None
            self.apply_policy_action(queued[0], dynamic_profit_target=queued[1])

        queued_switch = getattr(self, "_queued_strategy_switch", None)
        if queued_switch is not None:
            self._queued_strategy_switch = None
            self._apply_neural_strategy_switch(queued_switch)

        # --- MANAGEMENT: Check existing position ---
        for position_id in tuple(self._position_states):
            self._manage_position(position_id)
        cooldown = self._position_ledger.entry_cooldown_until(self._strategy_id())
        if cooldown is not None and self._as_aware_utc(self._current_datetime()) < cooldown:
            logger.info("[%s] Stop cooldown blocks new entries until %s.", self._strategy_id(), cooldown.isoformat())
            return
        if len(self._position_ledger.open_positions(self._strategy_id())) >= int(self.parameters.get("max_concurrent_positions", 1)):
            return

        if self.parameters.get("management_only", False):
            return

        if self._recovery_lock:
            logger.error("[%s] Broker ownership or order recovery remains unresolved; new entries are locked.", self._strategy_id())
            return

        # --- DATA SUFFICIENCY CHECK FOR NEURAL STRATEGY ---
        if self.pure_neural_mode:
            controller = getattr(self, "_policy_controller", None)
            if controller is None:
                logger.info(
                    "[%s] Neural policy disabled or controller not attached; blocking new entries.",
                    getattr(self, "name", "NeuralPure"),
                )
                return
            if not controller.has_sufficient_data:
                logger.info(
                    "[%s] Neural network waiting for sufficient data: %d/%d ticks. Skipping entry.",
                    getattr(self, "name", "NeuralPure"),
                    controller.current_ticks,
                    controller.required_ticks,
                )
                return
            if getattr(controller, "last_recommendation", None) is None:
                logger.info(
                    "[%s] Waiting for initial neural recommendation before trading.",
                    getattr(self, "name", "NeuralPure"),
                )
                return
            if not getattr(self, "_active_switched_strategy", None):
                logger.warning("[%s] Neural selection has not resolved to an active strategy; blocking entry.", self._strategy_id())
                return

        # --- COOLDOWN CHECK: Prevent duplicate entry on transient network blips ---
        # If we just checked positions and got an empty result, wait before re-entering
        if hasattr(self, '_position_open_cooldown') and self._position_open_cooldown:
            now_ts = self.get_datetime().timestamp()
            cooldown_until = getattr(self, '_position_open_cooldown_until', 0)
            if now_ts < cooldown_until:
                remaining = cooldown_until - now_ts
                logger.debug(
                    "Cooldown active: %.0fs remaining. Skipping entry check.",
                    remaining,
                )
                return

        # --- ENTRY: Only enter during the configured window ---
        if not self._is_entry_window(now):
            return

        # --- POSITION SIZING CHECK ---
        if not self._check_allocation():
            return

        # --- FIND EXPIRATION ---
        expiry = self._find_expiration()
        if not expiry:
            logger.warning("No suitable expiration found for DTE=%d", self.parameters["dte"])
            return

        # --- GET UNDERLYING PRICE ---
        underlying_price = self._fetch_last_price(self._underlying)
        if not underlying_price:
            logger.warning("Could not get price for %s", self.parameters["symbol"])
            return
        underlying_price = float(underlying_price)

        structure = getattr(self, "structure", "iron_condor").lower()
        self._active_put_delta = None
        self._active_call_delta = None

        # --- PRE-STRIKE DYNAMIC ASYMMETRIC SKEW RESOLUTION (IC_V27) ---
        if structure in ("skew_dynamic", "asymmetric_skew_ic") or getattr(self, "is_skew_dynamic_variant", False):
            z_score, quality = self._calculate_underlying_sd()
            if abs(z_score) > 2.0:
                logger.info(
                    "[%s] Skew Dynamic filter: Abs SD = %.2f > 2.0 (Extreme Blowoff). Skipping entry.",
                    getattr(self, "name", "SkewDynamic"),
                    abs(z_score),
                )
                return
            base_p = float(self.parameters.get("put_delta", self.parameters.get("short_delta", 0.16)))
            base_c = float(self.parameters.get("call_delta", 0.12))
            put_delta, call_delta = resolve_skew_condor_deltas(
                z_score, base_put_delta=base_p, base_call_delta=base_c
            )
            self._active_put_delta = put_delta
            self._active_call_delta = call_delta
            logger.info(
                "[%s] Skew Dynamic IC: SD=%.2f (%s) -> Asymmetric Deltas Put=%.2f / Call=%.2f",
                getattr(self, "name", "SkewDynamic"),
                z_score,
                quality,
                put_delta,
                call_delta,
            )
            structure = "iron_condor"

        # --- FIND SHORT STRIKES ---
        short_put_strike, short_call_strike = self._find_short_strikes(
            underlying_price, expiry
        )
        if not short_put_strike or not short_call_strike:
            logger.warning("Could not find strikes matching target delta")
            return

        # --- CALCULATE WING STRIKES ---
        wing = self.parameters["wing_width"]
        long_put_strike = short_put_strike - wing
        long_call_strike = short_call_strike + wing

        # --- BUILD OPTION ASSETS ---
        long_put = self._make_option(long_put_strike, expiry, "put")
        short_put = self._make_option(short_put_strike, expiry, "put")
        short_call = self._make_option(short_call_strike, expiry, "call")
        long_call = self._make_option(long_call_strike, expiry, "call")

        qty = self.parameters["quantity"]

        if structure in ("dynamic_sd", "strategybro_dynamic") or getattr(self, "is_strategybro_variant", False):
            z_score, quality = self._calculate_underlying_sd()
            if quality == "AVOID":
                logger.info(
                    "[%s] StrategyBro SD quality filter: Abs SD = %.2f > 1.5 (Quality: AVOID). Skipping entry.",
                    getattr(self, "name", "StrategyBro"),
                    abs(z_score),
                )
                return
            resolved_structure = resolve_strategybro_structure(z_score)
            if not resolved_structure:
                logger.info(
                    "[%s] StrategyBro SD quality filter: Abs SD = %.2f in Avoid zone. Skipping entry.",
                    getattr(self, "name", "StrategyBro"),
                    abs(z_score),
                )
                return
            logger.info(
                "[%s] StrategyBro preference: SD=%.2f (Quality: %s) -> Prefer %s",
                getattr(self, "name", "StrategyBro"),
                z_score,
                quality,
                resolved_structure,
            )
            structure = resolved_structure
        elif structure in ("trendflow", "trendflow_dynamic") or getattr(self, "is_trendflow_variant", False):
            z_score, quality = self._calculate_underlying_sd()
            min_sd = float(self.parameters.get("min_trend_sd", 0.35))
            resolved_structure = resolve_trendflow_structure(z_score, min_trend_sd=min_sd)
            if not resolved_structure:
                logger.info(
                    "[%s] TrendFlow filter: SD=%.2f outside momentum band [%.2f, 2.50]. Skipping entry.",
                    getattr(self, "name", "TrendFlow"),
                    z_score,
                    min_sd,
                )
                return
            logger.info(
                "[%s] TrendFlow momentum: SD=%.2f -> Riding trend with %s",
                getattr(self, "name", "TrendFlow"),
                z_score,
                resolved_structure,
            )
            structure = resolved_structure
        elif structure in ("vol_crush", "vol_crush_dynamic") or getattr(self, "is_vol_crush_variant", False):
            z_score, _ = self._calculate_underlying_sd()
            rv_ratio = self._calculate_realized_vol_ratio()
            min_rv = float(self.parameters.get("min_rv_ratio", 1.15))
            min_abs_z = float(self.parameters.get("min_abs_z", 0.90))
            resolved_structure = resolve_vol_crush_structure(
                z_score, rv_ratio, min_rv_ratio=min_rv, min_abs_z=min_abs_z
            )
            if not resolved_structure:
                logger.info(
                    "[%s] VolCrush filter: RV_ratio=%.2f (<%.2f) and |SD|=%.2f (<%.2f). Waiting for vol spike.",
                    getattr(self, "name", "VolCrush"),
                    rv_ratio,
                    min_rv,
                    abs(z_score),
                    min_abs_z,
                )
                return
            logger.info(
                "[%s] VolCrush trigger: RV_ratio=%.2f, SD=%.2f -> Harvesting vol with %s",
                getattr(self, "name", "VolCrush"),
                rv_ratio,
                z_score,
                resolved_structure,
            )
            structure = resolved_structure
        elif structure in ("orb_dynamic", "orb_1dte") or getattr(self, "is_orb_dynamic_variant", False):
            intraday_ret = self._calculate_intraday_return_pct(underlying_price)
            thresh = float(self.parameters.get("orb_threshold_pct", 0.0030))
            resolved_structure = resolve_orb_structure(intraday_ret, threshold_pct=thresh)
            if not resolved_structure:
                logger.info(
                    "[%s] ORB 1DTE filter: Session move %.2f%% exceeds runaway safety cap. Skipping entry.",
                    getattr(self, "name", "ORB_1DTE"),
                    intraday_ret * 100.0,
                )
                return
            logger.info(
                "[%s] ORB 1DTE session move %+.2f%% (thresh %.2f%%) -> Prefer %s",
                getattr(self, "name", "ORB_1DTE"),
                intraday_ret * 100.0,
                thresh * 100.0,
                resolved_structure,
            )
            structure = resolved_structure

        if structure in ("bull_put", "put_spread", "bull_put_vertical"):
            legs = [
                (long_put, int(qty)),
                (short_put, -int(qty)),
            ]
            logger.info(
                "ENTERING Bull Put Vertical Spread on %s (exp %s) @ price %.2f\n"
                "  BUY  %d × P%.0f  |  SELL %d × P%.0f",
                self.parameters["symbol"], expiry, underlying_price,
                qty, long_put_strike, qty, short_put_strike,
            )
        elif structure in ("bear_call", "call_spread", "bear_call_vertical"):
            legs = [
                (short_call, -int(qty)),
                (long_call, int(qty)),
            ]
            logger.info(
                "ENTERING Bear Call Vertical Spread on %s (exp %s) @ price %.2f\n"
                "  SELL %d × C%.0f  |  BUY  %d × C%.0f",
                self.parameters["symbol"], expiry, underlying_price,
                qty, short_call_strike, qty, long_call_strike,
            )
        else:
            legs = [
                (long_put, int(qty)),
                (short_put, -int(qty)),
                (short_call, -int(qty)),
                (long_call, int(qty)),
            ]
            logger.info(
                "ENTERING Iron Condor on %s (exp %s) @ price %.2f\n"
                "  BUY  %d × P%.0f  |  SELL %d × P%.0f  |  "
                "SELL %d × C%.0f  |  BUY  %d × C%.0f",
                self.parameters["symbol"], expiry, underlying_price,
                qty, long_put_strike, qty, short_put_strike,
                qty, short_call_strike, qty, long_call_strike,
            )

        # --- SUBMIT ORDERS ---
        try:
            package_price = self._package_price(legs, order_type="credit")
            if package_price is None or package_price <= 0:
                logger.warning("Complete bid/ask quotes are required before entry submission.")
                return
            min_credit = self._minimum_entry_credit(self.parameters)
            if float(package_price) < min_credit:
                logger.info(
                    "[%s] Package credit $%.2f is below minimum threshold $%.2f (avoiding fee drag). Skipping entry.",
                    getattr(self, "name", "SPXIronCondor"),
                    float(package_price),
                    float(min_credit),
                )
                return
            orders = self._submit_package(
                legs, intent="open", order_type="credit", price=package_price
            )
            self._hydrate_from_ledger()
            position_id = getattr(orders[0], "_position_id", None)
            position = self._runtime(position_id)
            position.entry_orders.extend(orders)
            position.position_open = True
            self._last_action = "ENTER"
            logger.info("%s package submitted at a %.2f credit limit.", structure.upper(), package_price)
            self._publish_position_snapshot(position, pnl=0.0, status="SUBMITTED")

        except Exception as e:
            logger.error("Failed to submit iron condor: %s", e)
            self._hydrate_from_ledger()
            # Set cooldown to prevent duplicate entry on transient network blip
            self._position_open_cooldown = True
            self._position_open_cooldown_until = self.get_datetime().timestamp() + 5.0  # 5 second cooldown

    # ------------------------------------------------------------------ #
    # Position management
    # ------------------------------------------------------------------ #

    def _manage_position(self, position_id: str | None = None) -> None:
        """Check profit target and stop loss on the open iron condor."""
        position = self._runtime(position_id)
        try:
            orders = self.get_orders()
            positions = self.get_positions()
            if positions is None or orders is None:
                logger.warning("Position or order data unavailable; retaining position management.")
                self._reset_exit_signal(position)
                return
            pending_orders = any(
                order.is_active()
                for order in [*orders, *position.entry_orders, *position.closing_orders]
            )
        except Exception as e:
            logger.warning("Could not reconcile open position; retaining management: %s", e)
            self._reset_exit_signal(position)
            return

        if self._retire_legacy_exit(position, orders):
            return

        option_positions = self._filter_owned_option_positions(positions, position_id=position.position_id)
        if not option_positions:
            if position.owned_legs and self._all_owned_legs_expired(position_id=position.position_id) and pending_orders:
                logger.info("Position %s awaits settlement; retaining inventory and unresolved orders.", position.position_id)
                return
            if pending_orders:
                if position.exit_requested:
                    self._close_all_options(position_id=position.position_id,
                        exit_reason=position.pending_exit_reason, gross_pnl=position.pending_gross_pnl,
                        fees=position.pending_fees, net_pnl=position.pending_net_pnl,
                    )
                return
            if position.owned_legs and self._all_owned_legs_expired(position_id=position.position_id):
                retired = self._position_ledger.expire_strategy_positions(
                    self._strategy_id(), self.get_datetime().date(), position_id=position.position_id
                )
                self._hydrate_from_ledger()
                if retired > 0 and not self._position_quantities(position):
                    self._position_ledger.flush_outbox()
                    self._reset_flat_state(position_id=position.position_id)
                    return
                else:
                    logger.info(
                        "Position %s has expired legs but is awaiting settlement evidence; retaining inventory.",
                        position.position_id,
                    )
            if self._position_quantities(position):
                logger.warning("Owned ledger contracts are not visible at the broker; retaining state.")
                return
            if any(
                self._position_ledger.is_inflight(basket)
                for basket in self._position_baskets(position, active_only=True)
            ):
                logger.warning("Package state remains active in ledger; retaining management.")
                return
            self._reset_flat_state(position_id=position.position_id)
            return

        if position.exit_requested:
            self._close_all_options(position_id=position.position_id,
                exit_reason=position.pending_exit_reason, gross_pnl=position.pending_gross_pnl,
                fees=position.pending_fees, net_pnl=position.pending_net_pnl,
            )
            return

        roll_dte = position.settings.get("roll_dte")
        remaining_dte = self._days_to_expiration(position_id=position.position_id)
        if roll_dte is not None and remaining_dte is not None and remaining_dte <= int(roll_dte):
            logger.info(
                "🔄 ROLL / DTE TARGET HIT — closing position at %d DTE left (roll threshold: %d DTE)",
                remaining_dte, int(roll_dte),
            )
            self._close_all_options(position_id=position.position_id, exit_reason="ROLL_DTE")
            return

        if (
            position.entry_credit_cash is None
            or not math.isfinite(position.entry_credit_cash)
        ):
            logger.warning(
                "Filled opening cash is unavailable; retaining contracts without P&L exits."
            )
            self._publish_position_snapshot(position, pnl=0.0, status="VALUATION_UNAVAILABLE")
            # CRITICAL: Do NOT set position.position_open = False or clear _owned_legs.
            # Open positions remain active and will be caught by EOD close.
            return

        leg_quotes = []
        batched_quotes = None
        if getattr(self.broker, "_shared_broker_reads", None) is not None:
            try:
                batched_quotes = self._fetch_quotes([asset for asset, _ in position.owned_legs])
            except Exception as error:
                logger.warning("Could not batch owned option quotes: %s", error)
                batched_quotes = [None] * len(position.owned_legs)
        incomplete = False
        for index, (asset, signed_quantity) in enumerate(position.owned_legs):
            try:
                quote = batched_quotes[index] if batched_quotes is not None else self._fetch_quote(asset)
                midpoint = self._quote_midpoint(quote)
                bounds = self._quote_bounds(quote)
            except Exception as error:
                logger.warning("Could not quote owned option %s: %s", asset, error)
                quote = None
                midpoint, bounds = None, None
            leg_quotes.append({
                "asset": asset,
                "signed_quantity": signed_quantity,
                "midpoint": midpoint,
                "bounds": bounds,
                "quote_time": self._quote_timestamp(quote),
            })
            if midpoint is None:
                incomplete = True
        wing_width = float(position.settings.get("wing_width", 0.0))
        quantity = max((abs(q) for _, q in position.owned_legs), default=1)
        if wing_width <= 0:
            strikes = [float(getattr(a, "strike", 0.0)) for a, _ in position.owned_legs if getattr(a, "strike", None)]
            if len(strikes) >= 2:
                wing_width = max(strikes) - min(strikes)
        defined_loss = (
            max_defined_loss(wing_width, quantity, max(position.entry_credit_cash, 0.0))
            + max(-position.entry_credit_cash, 0.0)
            if wing_width > 0
            else (abs(position.entry_credit_cash) if position.entry_credit_cash < 0 else 0.0)
        )

        current_value = None if incomplete else bounded_option_value(
            position.owned_legs, [item["midpoint"] for item in leg_quotes])
        if current_value is None:
            self._reset_exit_signal(position)
            logger.warning("Incomplete or inconsistent option quotes; retaining contracts without P&L exits.")
            self._publish_position_snapshot(position, pnl=position.last_pnl, status="VALUATION_UNAVAILABLE")
            return
        if self._defer_opening_pnl_exit(position):
            self._reset_exit_signal(position)
            self._publish_position_snapshot(position, pnl=position.last_pnl, status="OPENING_QUOTE_HOLD")
            return
        pnl = credit_strategy_pnl(position.entry_credit_cash, current_value)
        if pnl is None:
            logger.warning("Incomplete option quotes; retaining contracts without P&L exits.")
            self._publish_position_snapshot(position, pnl=0.0, status="VALUATION_UNAVAILABLE")
            return
        fee_rate = option_fee_per_contract()
        roundtrip_fees = round(
            2 * option_execution_fees((quantity for _, quantity in position.owned_legs), fee_rate), 2
        )
        gross_pnl = float(pnl)
        net_pnl = round(gross_pnl - roundtrip_fees, 2)

        position.last_pnl = net_pnl
        position.mfe_pnl = max(float(getattr(position, "mfe_pnl", 0.0)), net_pnl)
        position.mae_pnl = min(float(getattr(position, "mae_pnl", 0.0)), net_pnl)
        self._persist_position(position)
        self._publish_position_snapshot(position, pnl=net_pnl, status="OPEN")

        if position.entry_credit_cash > 0:
            pt_threshold = position.entry_credit_cash * position.settings["profit_target"]
            sl_threshold = -position.entry_credit_cash * position.settings["stop_loss_mult"]
        else:
            pt_threshold = float("inf")
            sl_threshold = -defined_loss if defined_loss > 0 else 0.0

        logger.info(
            "IC P&L: Gross $%.2f | Fees -$%.2f | Net $%.2f | PT threshold: $%.2f | SL threshold: $%.2f",
            gross_pnl, roundtrip_fees, net_pnl, pt_threshold, sl_threshold,
        )

        reason, threshold = None, None
        if net_pnl >= pt_threshold and net_pnl > 0:
            reason, threshold = "PROFIT_TARGET", pt_threshold
        elif sl_threshold < 0 and net_pnl <= sl_threshold:
            reason, threshold = "STOP_LOSS", sl_threshold
        elif bool(position.settings.get("enable_mfe_ratchet", False)) or os.getenv("IC_MFE_RATCHET_ENABLED", "").strip().lower() in ("true", "1", "yes", "on"):
            reason = evaluate_mfe_ratchet(position.entry_credit_cash, position.mfe_pnl, net_pnl)
            threshold = position.mfe_pnl * DEFAULT_MFE_TRAIL_LOCK_FRACTION
        if reason is None:
            self._reset_exit_signal(position)
            return
        if not self._confirm_pnl_exit(position, reason, leg_quotes):
            return
        self._capture_exit_decision(position, reason, gross_pnl, roundtrip_fees, net_pnl, threshold, leg_quotes)
        self._close_all_options(position_id=position.position_id, exit_reason=reason,
                                gross_pnl=gross_pnl, fees=roundtrip_fees, net_pnl=net_pnl)

    @staticmethod
    def _reset_exit_signal(position: PositionState) -> None:
        position.exit_signal_reason = None
        position.exit_signal_at = None
        position.exit_signal_quotes = None

    def _defer_opening_pnl_exit(self, position: PositionState) -> bool:
        if self._is_expiring_today(position_id=position.position_id):
            return False
        now = self._as_aware_utc(self._current_datetime())
        bounds = session_bounds(now.astimezone(EASTERN).date())
        return bounds is not None and bounds[0] <= now < bounds[0] + timedelta(minutes=15)

    def _confirm_pnl_exit(self, position: PositionState, reason: str, quotes: list[dict]) -> bool:
        now = self._as_aware_utc(self._current_datetime())
        stamps = tuple(item["quote_time"] for item in quotes)
        # Provider timestamps identify a cached reading. Without timestamps,
        # require a new management read at least 30 seconds later.
        identity = stamps if any(stamps) else (now.isoformat(),)
        elapsed = (now - position.exit_signal_at).total_seconds() if position.exit_signal_at else None
        if position.exit_signal_reason == reason and elapsed is not None and 30 <= elapsed <= 600:
            if identity != position.exit_signal_quotes:
                return True
            return False
        position.exit_signal_reason = reason
        position.exit_signal_at = now
        position.exit_signal_quotes = identity
        logger.info("Position %s awaits a second valid %s reading.", position.position_id, reason)
        return False

    def _record_exit_log(
        self,
        reason: str,
        pnl: float,
        fees: float | None = None,
        gross_pnl: float | None = None,
        net_pnl: float | None = None,
        position_id: str | None = None,
    ) -> None:
        """Persist trade exit event to logs/trades.jsonl."""
        position = self._runtime(position_id)
        try:
            trade_id = getattr(position, "current_trade_id", None)
            if trade_id:
                from trade_logger import record_trade_exit
                fee_rate = option_fee_per_contract()
                calc_fees = float(
                    fees if fees is not None else option_execution_fees(
                        (quantity for _, quantity in position.owned_legs), fee_rate
                    )
                )
                calc_gross = float(gross_pnl if gross_pnl is not None else pnl)
                calc_net = float(net_pnl if net_pnl is not None else (calc_gross - calc_fees))
                mfe = max(float(getattr(position, "mfe_pnl", 0.0)), calc_net)
                mae = min(float(getattr(position, "mae_pnl", 0.0)), calc_net)
                record_trade_exit(
                    trade_id=trade_id,
                    strategy_name=getattr(self, "name", "SPXIronCondor"),
                    exit_reason=reason,
                    pnl=calc_net,
                    fees=calc_fees,
                    gross_pnl=calc_gross,
                    net_pnl=calc_net,
                    mfe_pnl=mfe,
                    mae_pnl=mae,
                )
                position.current_trade_id = None
        except Exception as e:
            logger.warning("Could not persist trade exit: %s", e)

    def _can_add_contracts(self, add_qty: int = 1, position_id: str | None = None) -> bool:
        """Verify adding `add_qty` contracts respects RSK-01 ($5k cap), RSK-02 (single-trade tier), and allocation."""
        position = self._runtime(position_id)
        if self.parameters.get("management_only", False):
            return False
        if add_qty < 1:
            return False
        wing = float(position.settings.get("wing_width", self.parameters.get("wing_width", 15.0)))
        current_qty = max(
            (abs(int(q)) for _, q in getattr(position, "owned_legs", ())),
            default=int(self.parameters.get("quantity", 1)),
        )
        max_loss_per_contract = wing * 100.0
        added_risk = max_loss_per_contract * add_qty
        combined_trade_risk = max_loss_per_contract * (current_qty + add_qty)

        max_risk_cap = float(self.parameters.get("max_position_risk", 5000.0))
        override_pct = (os.getenv("IC_MAX_SINGLE_TRADE_PCT") or "").strip()
        try:
            parsed_override = float(override_pct) if override_pct else None
        except ValueError:
            parsed_override = None
        single_trade_pct = (
            parsed_override
            if parsed_override is not None and 0 < parsed_override <= 1
            else get_max_single_trade_pct(max_risk_cap)
        )
        max_single_trade_risk = max_risk_cap * single_trade_pct

        if combined_trade_risk > max_single_trade_risk:
            logger.warning(
                "ADD_POSITION blocked: combined trade risk ($%.2f) exceeds single-trade limit ($%.2f).",
                combined_trade_risk, max_single_trade_risk,
            )
            return False

        try:
            if not self._reconcile_broker_state() or self._recovery_lock:
                return False
            existing_exposure = self._position_ledger.portfolio_defined_risk(self._strategy_id())
            if existing_exposure is None:
                logger.warning("ADD_POSITION blocked: ledger contains incomplete defined-risk baskets.")
                return False
        except Exception as e:
            logger.warning("Could not verify exposure for ADD_POSITION: %s", e)
            return False

        if existing_exposure + added_risk > max_risk_cap:
            logger.warning(
                "ADD_POSITION blocked: total exposure ($%.2f) would exceed $%.2f cap.",
                existing_exposure + added_risk, max_risk_cap,
            )
            return False

        portfolio_value = self._get_portfolio_value()
        if portfolio_value is None or portfolio_value <= 0:
            logger.warning("ADD_POSITION blocked: portfolio value is unavailable.")
            return False
        max_allowed_pct = float(portfolio_value) * float(self.parameters.get("max_allocation", 0.03))
        if combined_trade_risk > max_allowed_pct:
            logger.warning(
                "ADD_POSITION blocked: combined trade risk ($%.2f) exceeds allocation ($%.2f).",
                combined_trade_risk, max_allowed_pct,
            )
            return False

        return True

    def _get_portfolio_value(self) -> float | None:
        """Read portfolio_value from self or host strategy fallback."""
        host = getattr(self, "_host_strategy", None)
        target = (
            host
            if (host is not None and "portfolio_value" not in self.__dict__ and "_portfolio_value" not in self.__dict__)
            else self
        )
        try:
            val = target.portfolio_value
            return float(val) if val is not None else None
        except AttributeError:
            val = getattr(target, "_portfolio_value", None)
            return float(val) if val is not None else None

    def apply_policy_action(
        self,
        action: str,
        dynamic_profit_target: float | None = None,
        position_id: str | None = None,
    ) -> str:
        """Execute a granular position management action bounded by immutable RSK-01/RSK-02 guardrails.

        Supported actions:
          - HOLD: Maintain current position and parameters.
          - ADJUST_PROFIT_TARGET: Update profit_target within [0.15, 0.80].
          - ADD_POSITION: Scale in +1 contract if risk guardrails permit.
          - TRIM_POSITION: Close 50% (whole contracts) when quantity >= 2.
          - FLATTEN_NOW: Immediately close all owned option legs.
        """
        normalized = (action or "HOLD").strip().upper()
        if self._position_ledger.is_panic_halted():
            if normalized == "FLATTEN_NOW":
                self._panic_recovery()
                self._last_action = normalized
                return normalized
            return "HOLD"
        if position_id is None and len(getattr(self, "_position_states", {})) > 1:
            if normalized == "FLATTEN_NOW":
                self._close_all_options(exit_reason="NEURAL_EARLY_EXIT")
                self._last_action = normalized
                return normalized
            self._last_action = "HOLD"
            return "HOLD"
        position = self._runtime(position_id)
        if normalized == "FLATTEN_NOW" and self._position_ledger.is_panic_halted():
            self._panic_recovery()
            self._last_action = "FLATTEN_NOW"
            return "FLATTEN_NOW"
        if self._position_ledger.is_panic_halted():
            logger.warning("[%s] Policy action blocked: durable panic halt is active.", self._strategy_id())
            self._last_action = "HOLD"
            return "HOLD"
        if dynamic_profit_target is not None:
            clamped_pt = max(0.15, min(0.80, float(dynamic_profit_target)))
            position.settings["profit_target"] = clamped_pt
            if position.position_id:
                self._position_ledger.update_position(position.position_id, settings={
                    key: value for key, value in position.settings.items() if key in self._management_settings()
                })
            if len(getattr(self, "_position_states", {})) < 2:
                self.parameters["profit_target"] = clamped_pt
            if normalized in ("HOLD", "ADJUST_PROFIT_TARGET"):
                self._last_action = "ADJUST_PROFIT_TARGET"
                return "ADJUST_PROFIT_TARGET"

        if not position.position_open or position.exit_requested:
            self._last_action = "HOLD"
            return "HOLD"

        if normalized == "FLATTEN_NOW":
            self._close_all_options(position_id=position.position_id, exit_reason="NEURAL_EARLY_EXIT")
            self._last_action = "FLATTEN_NOW"
            return "FLATTEN_NOW"

        owned_legs = list(getattr(position, "owned_legs", []))
        if len(owned_legs) not in (2, 4):
            self._last_action = "HOLD"
            return "HOLD"
        if any(
            self._position_ledger.is_inflight(basket)
            for basket in self._position_baskets(position, active_only=True)
        ):
            self._last_action = "HOLD"
            return "HOLD"

        if normalized == "ADD_POSITION":
            add_qty = 1
            if not self._can_add_contracts(add_qty, position_id=position.position_id):
                self._last_action = "HOLD"
                return "HOLD"
            try:
                legs = [
                    (asset, add_qty if signed_quantity > 0 else -add_qty)
                    for asset, signed_quantity in owned_legs
                ]
                price = self._package_price(legs, order_type="credit")
                if price is None or price <= 0:
                    raise ValueError("complete quotes are required to add a package")
                orders = self._submit_package(
                    legs, position_id=position.position_id, intent="add", order_type="credit", price=price
                )
                position.entry_orders.extend(orders)
                self._last_action = "ADD_POSITION"
                return "ADD_POSITION"
            except Exception as e:
                logger.error("Failed to execute ADD_POSITION: %s", e)
                self._last_action = "HOLD"
                return "HOLD"

        if normalized == "TRIM_POSITION":
            current_qty = max(abs(int(q)) for _, q in owned_legs)
            if current_qty < 2:
                self._last_action = "HOLD"
                return "HOLD"
            trim_qty = current_qty // 2
            try:
                legs = [
                    (asset, -trim_qty if signed_quantity > 0 else trim_qty)
                    for asset, signed_quantity in owned_legs
                ]
                price = self._package_price(legs, order_type="debit")
                if price is None or price <= 0:
                    raise ValueError("complete quotes are required to trim a package")
                orders = self._submit_package(
                    legs, position_id=position.position_id, intent="trim", order_type="debit", price=price
                )
                position.closing_orders.extend(orders)
                self._last_action = "TRIM_POSITION"
                return "TRIM_POSITION"
            except Exception as e:
                logger.error("Failed to execute TRIM_POSITION: %s", e)
                self._last_action = "HOLD"
                return "HOLD"

        self._last_action = "HOLD"
        return "HOLD"

    def _apply_neural_strategy_switch(self, target_strategy_name: str) -> bool:
        """Dynamically adapt parameters to match the variant selected by the neural network
        based on real-time market regime, volatility, and cross-strategy signals."""
        if self.parameters.get("management_only", False) or self._position_ledger.is_panic_halted():
            return False
        from strategies.incubator import get_incubator_specs
        specs = {s["strategy_name"]: s for s in get_incubator_specs(include_neural=False)}
        target_spec = specs.get(target_strategy_name)
        if target_spec is None:
            prefix = target_strategy_name.split("_")[0] + "_" + target_strategy_name.split("_")[1] if "_" in target_strategy_name else target_strategy_name
            target_spec = next((s for name, s in specs.items() if name.startswith(prefix)), None)

        if target_spec is None:
            self._active_switched_strategy = None
            logger.warning("Neural picker selected unavailable strategy %s; blocking entry.", target_strategy_name)
            return False

        old_name = getattr(self, "_active_switched_strategy", "Default")
        self._active_switched_strategy = target_spec.get("strategy_name", target_strategy_name)
        self.parameters["short_delta"] = float(target_spec.get("short_delta", self.parameters["short_delta"]))
        self.parameters["wing_width"] = float(target_spec.get("wing_width", self.parameters["wing_width"]))
        self.parameters["dte"] = int(target_spec.get("dte", self.parameters["dte"]))
        self.parameters["stop_loss_mult"] = float(target_spec.get("stop_loss_mult", self.parameters["stop_loss_mult"]))
        self.structure = str(target_spec.get("structure", "iron_condor")).lower()
        self.parameters["structure"] = self.structure
        for key in (
            "roll_dte", "otm_pct", "otm_put_pct", "otm_call_pct",
            "put_delta", "call_delta", "min_trend_sd", "min_rv_ratio",
            "orb_threshold_pct", "enable_mfe_ratchet",
        ):
            self.parameters.pop(key, None)
            if key in target_spec:
                self.parameters[key] = target_spec[key]
        for flag in (
            "is_testing_variant", "is_strategybro_variant", "is_trendflow_variant",
            "is_skew_dynamic_variant", "is_vol_crush_variant", "is_orb_dynamic_variant",
        ):
            enabled = bool(target_spec.get(flag, False))
            self.parameters[flag] = enabled
            setattr(self, flag, enabled)
        entry_time_val = target_spec.get("entry_time") or target_spec.get("run_times") or target_spec.get("entry_times")
        if entry_time_val:
            self.set_run_times(entry_time_val)
        logger.info(
            "[%s] Neural network dynamically switched strategy: %s -> %s (DTE=%d, Δ=%.2f, Wing=$%.1f)",
            getattr(self, "name", "IC_V22_NEURAL_PURE"),
            old_name,
            self._active_switched_strategy,
            self.parameters["dte"],
            self.parameters["short_delta"],
            self.parameters["wing_width"],
        )
        return True

    def _cancel_managed_order(self, order: Any) -> None:
        """Space cancellation attempts while reconciliation retains the reservation."""
        identifier = str(order.identifier)
        raw = getattr(order, "_raw", None)
        # LumiBot maps Tradier's pending and open statuses to the same value.
        status = (raw.get("status") if isinstance(raw, dict) else None) or getattr(order, "status", "")
        status = str(status or "").lower()
        now = self._as_aware_utc(self._current_datetime())
        retry = self._cancellation_retries.get(identifier)
        if retry is not None and retry.status == status and now < retry.retry_at:
            return
        failures = retry.failures if retry is not None and retry.status == status else 0
        try:
            self.cancel_order(order)
        except Exception as error:
            failures += 1
            delay = min(60 * 2 ** min(failures - 1, 4), 900)
            logger.error(
                "[%s] Could not cancel broker order %s (status=%s); retry in %ds; "
                "retaining inventory and order reservation: %s",
                self._strategy_id(), identifier, status, delay, error,
            )
        else:
            failures = 0
            delay = 60
        self._cancellation_retries[identifier] = CancellationRetry(
            status, failures, now + timedelta(seconds=delay),
        )

    @fresh_broker_reads()
    def _close_all_options(
        self,
        exit_reason: str | None = None,
        gross_pnl: float | None = None,
        fees: float | None = None,
        net_pnl: float | None = None,
        position_id: str | None = None,
    ) -> None:
        """Close all open option positions owned by this strategy variant."""
        if position_id is None and len(getattr(self, "_position_states", {})) > 1:
            for pid in tuple(self._position_states):
                self._close_all_options(exit_reason, gross_pnl, fees, net_pnl, position_id=pid)
            return
        position = self._runtime(position_id)
        if self._all_owned_legs_expired(position_id=position.position_id):
            # Calendar expiry prevents further execution, but cannot certify
            # settlement or release unresolved broker order reservations.
            logger.info("Position %s awaits settlement; no further close attempts after expiry.", position.position_id)
            return
        reason = exit_reason or position.pending_exit_reason
        if reason in PNL_EXIT_REASONS and self._defer_opening_pnl_exit(position):
            self._reset_exit_signal(position)
            logger.info("Position %s defers P&L closing during the opening quote hold.", position.position_id)
            return
        prior_reason = position.pending_exit_reason
        if position.exit_requested and position.pending_exit_reason:
            if exit_reason in {"EOD_CLOSE", "FLATTEN_NOW"} or (
                    exit_reason and exit_reason not in PROFIT_BOUNDED_EXIT_REASONS
                    and position.pending_exit_reason in PROFIT_BOUNDED_EXIT_REASONS):
                position.pending_exit_reason = exit_reason
        else:
            position.pending_exit_reason = exit_reason or position.pending_exit_reason or "MANUAL"
        position.exit_requested = True
        position.position_open = True
        if position.pending_exit_reason == "STOP_LOSS" and prior_reason != "STOP_LOSS":
            self._record_stop_cooldown(position)
        self._persist_position(position)
        if gross_pnl is not None:
            position.pending_gross_pnl = gross_pnl
        elif not hasattr(position, "pending_gross_pnl"):
            position.pending_gross_pnl = None

        if fees is not None:
            position.pending_fees = fees
        elif not hasattr(position, "pending_fees"):
            position.pending_fees = None

        if net_pnl is not None:
            position.pending_net_pnl = net_pnl
        elif not hasattr(position, "pending_net_pnl"):
            position.pending_net_pnl = None
        try:
            # Refresh order state and stop entry fills before unwinding their legs.
            broker_orders = self.get_orders() or []
            fetch_order = getattr(self, "get_order", None)
            pending_baskets = [b for b in self._position_baskets(position)
                               if b.intent in {"open", "add"} and self._position_ledger.is_inflight(b)]
            if pending_baskets:
                order_lookup = {
                    str(getattr(o, "identifier", None) or getattr(o, "id", "")): o
                    for o in [*broker_orders, *position.entry_orders]
                    if getattr(o, "identifier", None) or getattr(o, "id", None)
                }
                has_active = False
                for basket in pending_baskets:
                    parent = order_lookup.get(str(basket.broker_parent_id or ""))
                    if parent is None and basket.broker_parent_id and callable(fetch_order):
                        try:
                            parent = fetch_order(str(basket.broker_parent_id))
                        except Exception:
                            parent = None
                    if parent is None and basket.broker_tag:
                        parent = self._find_order_by_tag(broker_orders, basket.broker_tag)
                    if parent is None:
                        for leg in basket.legs:
                            c_id = str(leg.get("broker_order_id") or "")
                            if c_id in order_lookup:
                                c_order = order_lookup[c_id]
                                p_id = getattr(c_order, "parent_identifier", None) or getattr(c_order, "parent_id", None)
                                if p_id and str(p_id) in order_lookup:
                                    parent = order_lookup[str(p_id)]
                                    break
                    if parent is not None:
                        if not basket.broker_parent_id:
                            try:
                                self._position_ledger.bind_broker_submission(basket.package_id, parent)
                                if getattr(parent, "child_orders", None):
                                    self._position_ledger.bind_broker_children(basket.package_id, parent.child_orders)
                            except Exception:
                                pass
                        if parent.is_active():
                            self._cancel_managed_order(parent)
                            has_active = True
                        elif str(getattr(parent, "status", "")).lower() in TERMINAL_STATES:
                            self._reconcile_inflight_orders(broker_orders)
                            if self._position_ledger.is_inflight(self._position_ledger.get_basket(basket.package_id)):
                                term_status = str(getattr(parent, "status", "canceled")).lower()
                                term = term_status if term_status in TERMINAL_STATES else "canceled"
                                self._position_ledger.record_terminal_status(basket.package_id, term)
                        else:
                            has_active = True
                    else:
                        # Only a durable pre-submission marker proves no broker call occurred.
                        # The gate excludes a still-running submitter while checking it.
                        with self._position_ledger.execution_gate():
                            refreshed = self._position_ledger.get_basket(basket.package_id)
                            if refreshed.submission_started == 0:
                                self._position_ledger.record_terminal_status(basket.package_id, "failed")
                            else:
                                has_active = True
                self._hydrate_from_ledger()
                if has_active:
                    return
                if self._reconcile_inflight_orders(broker_orders):
                    self._recovery_lock = True
                    return
                if any(self._position_ledger.is_inflight(self._position_ledger.get_basket(b.package_id))
                       for b in pending_baskets):
                    return
            pending_entries = [order for order in position.entry_orders if order.is_active()]
            if pending_entries:
                for order in pending_entries:
                    self._cancel_managed_order(order)
                return
            active_close = next(
                (
                    basket
                    for basket in self._position_baskets(position, active_only=True)
                    if basket.intent == "close" and self._position_ledger.is_inflight(basket)
                ),
                None,
            )
            if active_close is not None:
                parent = next(
                    (
                        order
                        for order in [*broker_orders, *position.closing_orders]
                        if active_close.broker_parent_id
                        and str(getattr(order, "identifier", None) or getattr(order, "id", ""))
                        == str(active_close.broker_parent_id or "")
                    ),
                    None,
                )
                created = self._as_aware_utc(datetime.fromisoformat(str(active_close.created_at)))
                now = self._as_aware_utc(self._current_datetime())
                age = (now - created).total_seconds()
                if parent is not None and parent.is_active():
                    if age >= self._close_reprice_seconds:
                        self._cancel_managed_order(parent)
                    return
                # Absence and timeouts cannot prove that a submission was rejected.
                # Reconciliation must observe a terminal status before replacement.
                if parent is None:
                    self._recovery_lock = True
                return
            self._hydrate_from_ledger()
            if not position.owned_legs:
                return
            latest_close = self._latest_position_basket(position, {"close"})
            latest_open = self._latest_position_basket(position, {"open", "add"})
            previous = None
            if (
                latest_close is not None
                and latest_open is not None
                and str(latest_close.created_at) >= str(latest_open.created_at)
            ):
                previous = latest_close

            reprice_count = 0
            replacement_of = None
            if previous is not None:
                if previous.state not in {"canceled", "expired", "failed", "rejected"}:
                    return
                ref_time_str = previous.created_at or previous.updated_at
                if ref_time_str:
                    try:
                        ref_time = self._as_aware_utc(datetime.fromisoformat(str(ref_time_str)))
                        now = self._as_aware_utc(self._current_datetime())
                        elapsed = (now - ref_time).total_seconds()
                        if 0 <= elapsed < self._close_reprice_seconds:
                            return
                    except Exception:
                        pass
                reprice_count = previous.reprice_count + 1
                replacement_of = previous.package_id
            legs = [(asset, -signed_quantity) for asset, signed_quantity in position.owned_legs]
            is_balanced = len(legs) in (2, 4) and len({abs(quantity) for _, quantity in legs}) == 1
            if not is_balanced and legs:
                logger.warning(
                    "Position %s has unbalanced residual legs %s; queuing position-scoped recovery job.",
                    position.position_id,
                    legs,
                )
                self._position_ledger.enqueue_recovery_job(
                    scope="position",
                    target_id=position.position_id,
                    payload={"strategy_id": self._strategy_id(), "legs": [(asset_key(a), qty) for a, qty in legs]},
                )
                orders = self._submit_reduce_only_orders(
                    legs,
                    position_id=position.position_id,
                    order_type="debit",
                )
                position.closing_orders.extend(orders)
                return
            try:
                price = self._package_price(legs, order_type="debit")
            except Exception as error:
                logger.warning("Could not price closing package: %s", error)
                price = None
            if price == 0:
                price = 0.01
            if price is None or price < 0:
                if position.pending_exit_reason in PNL_EXIT_REASONS:
                    logger.warning("Validated quotes are required to execute a P&L exit; retaining intent.")
                    return
                urgent = (
                    self._is_expiring_today(position_id=position.position_id)
                    or position.pending_exit_reason in {"EOD_CLOSE", "FLATTEN_NOW"}
                )
                if not urgent:
                    logger.warning("Complete quotes are required before closing the package.")
                    return
                price = self._emergency_close_debit_fallback(legs, position_id=position.position_id)
                if price is None:
                    logger.error("Cannot establish a bounded emergency close price; retaining management.")
                    return
            # Tradier does not support market orders for this multileg route.
            # Keep IC_CLOSE_ALLOW_MARKET compatible, but use a bounded debit limit.
            order_type = "debit"
            width = float(position.settings.get("wing_width", 0.0))
            if not math.isfinite(width) or width <= 0 or not math.isfinite(price):
                logger.error("Cannot establish a bounded close price; retaining management.")
                return
            market_debit = price
            price = min(width, price + min(reprice_count, self._close_max_reprices) * self._close_price_step)
            if position.pending_exit_reason in PROFIT_BOUNDED_EXIT_REASONS:
                ceiling = self._profit_close_debit_ceiling(position, legs)
                if ceiling is None or market_debit > ceiling:
                    self._withdraw_discretionary_exit(position, market_debit=market_debit, ceiling=ceiling)
                    return
                price = min(price, ceiling)
            orders = self._submit_package(
                legs,
                intent="close",
                position_id=position.position_id,
                order_type=order_type,
                price=price,
                replacement_of=replacement_of,
                reprice_count=reprice_count,
            )
            position.closing_orders.extend(orders)
            logger.info("Closing iron-condor package at a %.2f debit limit.", price)
        except Exception as e:
            logger.error("Could not close all option positions; retaining management: %s", e)

    # ------------------------------------------------------------------ #
    # Helpers
    # ------------------------------------------------------------------ #

    def _retire_legacy_exit(self, position: PositionState, orders: list) -> bool:
        """Retire disabled exits without abandoning submitted broker orders."""
        if position.pending_exit_reason not in {"ABRUPT_CLOSE", "BREAKEVEN_RATCHET"} or position.position_id is None:
            return False
        baskets = self._position_baskets(position)
        closes = [basket for basket in baskets if basket.intent == "close"]
        created = closes[-1].created_at if closes else self._position_ledger.get_position(position.position_id).created_at
        if position.pending_exit_reason == "ABRUPT_CLOSE":
            try:
                requested = self._as_aware_utc(datetime.fromisoformat(str(created)))
            except (TypeError, ValueError):
                logger.warning("Cannot date legacy abrupt intent for %s; retaining its reservation.", position.position_id)
                return True
            bounds = session_bounds(requested.astimezone(EASTERN).date())
            if bounds is not None and self._as_aware_utc(self._current_datetime()) < bounds[1]:
                return False
        pending = [basket for basket in closes if self._position_ledger.is_inflight(basket)]
        if pending:
            for basket in pending:
                parent = next((order for order in [*orders, *position.closing_orders]
                               if basket.broker_parent_id and str(getattr(order, "identifier", "")) == str(basket.broker_parent_id)), None)
                if parent is not None and parent.is_active():
                    self._cancel_managed_order(parent)
            # Only terminal broker evidence releases a submitted reservation.
            return True
        self._position_ledger.clear_exit_reason(position.position_id)
        position.pending_exit_reason = None
        position.exit_requested = False
        position.pending_gross_pnl = position.pending_fees = position.pending_net_pnl = None
        position.pending_decision = None
        self._reset_exit_signal(position)
        logger.info("Retired legacy exit intent for position %s.", position.position_id)
        return False

    @staticmethod
    def _quote_timestamp(quote: Any) -> str | None:
        try:
            for name in ("timestamp", "bid_date", "ask_date"):
                value = quote.get(name) if isinstance(quote, dict) else getattr(quote, name, None)
                if value is not None:
                    return value.isoformat() if hasattr(value, "isoformat") else str(value)
        except Exception:
            return None
        return None

    def _record_stop_cooldown(self, position: PositionState) -> None:
        minutes = float(position.settings.get("stop_cooldown_minutes", self.parameters.get("stop_cooldown_minutes", 60)))
        self._position_ledger.block_entries_until(
            self._strategy_id(), self._as_aware_utc(self._current_datetime()) + timedelta(minutes=minutes))

    def _profit_close_debit_ceiling(self, position: PositionState, legs: list[tuple[Asset, int]]) -> float | None:
        """Preserve the profit threshold after round-trip fees and limit rounding."""
        credit = position.entry_credit_cash
        if credit is None or not math.isfinite(credit) or credit <= 0:
            return None
        qty = max((abs(q) for _, q in legs), default=0)
        if qty <= 0:
            return None
        fees = 2 * option_execution_fees((q for _, q in legs), option_fee_per_contract())
        required_profit = 0.0
        if position.pending_exit_reason == "PROFIT_TARGET":
            required_profit = credit * float(position.settings["profit_target"])
        elif position.pending_exit_reason == "MFE_TRAIL_LOCK":
            required_profit = position.mfe_pnl * DEFAULT_MFE_TRAIL_LOCK_FRACTION
        ceiling = (credit - fees - required_profit) / (100.0 * qty)
        return math.floor(ceiling * 100) / 100 if ceiling > 0 else None

    def _withdraw_discretionary_exit(self, position: PositionState, *, market_debit: float, ceiling: float | None) -> None:
        logger.warning("Withdrawing %s exit for %s: market debit %.2f exceeds profit ceiling %s; resuming management.",
                       position.pending_exit_reason, position.position_id, market_debit, ceiling)
        if position.position_id is not None:
            self._position_ledger.clear_exit_reason(position.position_id)
        position.exit_requested = False
        position.pending_exit_reason = None
        position.pending_gross_pnl = position.pending_fees = position.pending_net_pnl = None
        position.pending_decision = None
        self._reset_exit_signal(position)

    def _capture_exit_decision(self, position: PositionState, reason: str, gross: float, fees: float,
                               net: float, threshold: float | None, leg_quotes: list[dict[str, Any]]) -> None:
        position.pending_decision = {
            "reason": reason, "at": self._as_aware_utc(self._current_datetime()).isoformat(),
            "gross_pnl": gross, "fees": fees, "net_pnl": net, "threshold": threshold,
            "legs": [{"asset_key": asset_key(q["asset"]), "signed_quantity": q["signed_quantity"],
                      "bid": q["bounds"][0] if q["bounds"] else None,
                      "ask": q["bounds"][1] if q["bounds"] else None,
                      "mid": q["midpoint"], "quote_time": q.get("quote_time")} for q in leg_quotes],
        }

    def _emergency_close_debit_fallback(self, legs: list[tuple[Asset, int]], position_id: str | None = None) -> float | None:
        """Bound an emergency debit limit strictly by wing_width and credit multiplier."""
        position = self._runtime(position_id)
        try:
            width = float(position.settings.get("wing_width", 0.0))
            multiplier = float(os.getenv("IC_EMERGENCY_CLOSE_MAX_DEBIT_MULT", "3.0"))
            if not math.isfinite(width) or not math.isfinite(multiplier) or width <= 0 or multiplier <= 0:
                return None
            quantity = max((abs(qty) for _, qty in legs), default=1)
            if quantity <= 0:
                return None

            entry_credit_per_share = 0.0
            if getattr(position, "entry_credit_cash", None) and position.entry_credit_cash > 0:
                entry_credit_per_share = position.entry_credit_cash / (quantity * 100.0)
            elif getattr(position, "entry_credit", None) and position.entry_credit > 0:
                entry_credit_per_share = float(position.entry_credit)

            credit_target = entry_credit_per_share * multiplier if entry_credit_per_share > 0 else 0.0

            paired_debit = 0.0
            wings: dict[tuple[Any, Any], dict[str, Any]] = defaultdict(dict)
            for asset, signed_qty in legs:
                right = getattr(asset, "right", None)
                expiration = getattr(asset, "expiration", None)
                if right:
                    right = str(right).lower()
                key = (right, expiration)
                if signed_qty > 0:
                    wings[key]["buy_leg"] = (asset, signed_qty)
                else:
                    wings[key]["sell_leg"] = (asset, signed_qty)

            for key, wing in wings.items():
                buy_item = wing.get("buy_leg")
                sell_item = wing.get("sell_leg")
                if buy_item and sell_item:
                    try:
                        buy_bounds = self._quote_bounds(self._fetch_quote(buy_item[0]))
                    except Exception:
                        buy_bounds = None
                    try:
                        sell_bounds = self._quote_bounds(self._fetch_quote(sell_item[0]))
                    except Exception:
                        sell_bounds = None
                    w_qty = min(abs(buy_item[1]), abs(sell_item[1]))
                    if buy_bounds and sell_bounds:
                        w_debit = max(buy_bounds[1] - sell_bounds[0], 0.0)
                        paired_debit += min(width, w_debit) * (w_qty / quantity)
                    elif self._is_expiring_today(position_id=position.position_id):
                        try:
                            spot = self._fetch_last_price(self._underlying)
                        except Exception:
                            spot = None
                        if spot is not None and math.isfinite(spot) and spot > 0:
                            right = key[0]
                            short_strike = getattr(buy_item[0], "strike", None)
                            if short_strike is not None:
                                intrinsic = (float(short_strike) - spot) if right == "put" else (spot - float(short_strike))
                                if intrinsic > 0:
                                    paired_debit += min(width, intrinsic) * (w_qty / quantity)

            fallback = max(credit_target, paired_debit, 0.01)
            final_price = round(min(width, fallback), 2)
            return final_price if final_price > 0 else round(width, 2)
        except (KeyError, TypeError, ValueError, ZeroDivisionError):
            return None

    def _strategy_id(self) -> str:
        return str(
            getattr(self, "name", None)
            or self.parameters.get("strategy_name")
            or type(self).__name__
        )

    def _current_datetime(self) -> datetime:
        getter = getattr(self, "get_datetime", None)
        if callable(getter):
            try:
                return getter()
            except Exception:
                pass
        return datetime.now(timezone.utc)

    def _broker_routing_tag(self) -> str:
        """Route broker events to the registered host, independently of ledger ownership."""
        host = getattr(self, "_host_strategy", None)
        if host is None:
            host = self
        name = str(getattr(host, "name", None) or "").strip()
        tag = re.sub(r"[^a-zA-Z0-9-]", "-", name)
        if not tag:
            raise ValueError("a registered strategy name is required for broker routing")
        return tag

    def _runtime(self, position_id: str | None = None) -> PositionState:
        """Select one position. Ambiguous management must name its owner."""
        states = getattr(self, "_position_states", None)
        if states is None:
            states = self._position_states = {}
        if position_id is not None:
            if position_id not in states:
                record = self._position_ledger.get_position(position_id)
                states[position_id] = PositionState(
                    position_id=position_id,
                    settings={**self.parameters, **record.settings} if record.settings else self.parameters,
                    current_trade_id=record.trade_id,
                    pending_exit_reason=record.exit_reason,
                    exit_requested=bool(record.exit_reason),
                    mfe_pnl=record.mfe_pnl, mae_pnl=record.mae_pnl,
                )
            return states[position_id]
        if len(states) == 1:
            return next(iter(states.values()))
        if len(states) > 1:
            raise ValueError("multiple positions require an explicit position_id")
        if not hasattr(self, "_unbound_position"):
            self._unbound_position = PositionState(settings=dict(self.parameters))
        self._unbound_position.settings = dict(self.parameters)
        return self._unbound_position

    def _position_quantities(self, position: PositionState) -> dict[str, int]:
        if position.position_id is None:
            return self._position_ledger.owned_quantities(self._strategy_id())
        return self._position_ledger.position_quantities(position.position_id)

    def _position_baskets(self, position: PositionState, *, active_only: bool = False):
        if position.position_id is None:
            baskets = self._position_ledger.open_baskets(self._strategy_id())
        else:
            baskets = self._position_ledger.position_baskets(position.position_id)
        return tuple(b for b in baskets if not active_only or b.state in {"submitted", "partial", "open", "closing"})

    def _latest_position_basket(self, position: PositionState, intents: set[str]):
        return next((b for b in reversed(self._position_baskets(position)) if b.intent in intents), None)

    def _management_settings(self) -> dict[str, Any]:
        keys = ("symbol", "dte", "roll_dte", "wing_width", "quantity", "profit_target",
                "stop_loss_mult", "enable_mfe_ratchet", "stop_cooldown_minutes")
        return {key: self.parameters[key] for key in keys if key in self.parameters}

    def _persist_position(self, position: PositionState) -> None:
        if position.position_id is not None:
            self._position_ledger.update_position(
                position.position_id, exit_reason=position.pending_exit_reason,
                mfe_pnl=position.mfe_pnl, mae_pnl=position.mae_pnl,
            )

    def _hydrate_from_ledger(self) -> None:
        """Rebuild independent runtimes from durable position ownership."""
        records = self._position_ledger.open_positions(self._strategy_id())
        if not hasattr(self, "_position_states"):
            self._position_states = {}
        if len(records) == 1 and not self._position_states:
            # Preserve the single-position telemetry object during first hydration.
            state = self._runtime()
            state.position_id = records[0].position_id
            self._position_states[state.position_id] = state
        active_ids = set()
        for record in records:
            active_ids.add(record.position_id)
            position = self._runtime(record.position_id)
            position.settings = {**self.parameters, **record.settings} if record.settings else self.parameters
            owned = []
            for key, quantity in self._position_ledger.position_quantities(record.position_id).items():
                asset = self._asset_registry.get(key)
                if asset is None:
                    asset = self._asset_from_key(key)
                    self._asset_registry[key] = asset
                owned.append((asset, quantity))
            position.owned_legs = sorted(owned, key=lambda item: (
                str(getattr(item[0], "right", "")), float(getattr(item[0], "strike", 0.0))))
            try:
                position.entry_credit_cash = self._position_ledger.position_opening_cash(record.position_id)
            except ValueError:
                position.entry_credit_cash = None
            quantity = max((abs(q) for _, q in owned), default=0)
            position.entry_credit = (
                position.entry_credit_cash / (100 * quantity)
                if position.entry_credit_cash is not None and quantity
                else 0.0
            )
            position.current_trade_id = record.trade_id
            position.mfe_pnl = record.mfe_pnl
            if position.entry_credit_cash is not None and position.entry_credit_cash > 0 and position.mfe_pnl > position.entry_credit_cash:
                position.mfe_pnl = 0.0
            position.mae_pnl = record.mae_pnl
            position.pending_exit_reason = record.exit_reason or position.pending_exit_reason
            position.exit_requested = bool(position.pending_exit_reason)
            position.position_open = bool(owned) or any(
                self._position_ledger.is_inflight(b) for b in self._position_baskets(position))
        for pid in set(self._position_states) - active_ids:
            del self._position_states[pid]
        if not records:
            self._unbound_position = PositionState(settings=dict(self.parameters))

    def _all_owned_legs_expired(self, position_id: str | None = None) -> bool:
        position = self._runtime(position_id)
        if not position.owned_legs:
            return False
        today = self.get_datetime().date()
        for asset, _ in position.owned_legs:
            raw_exp = getattr(asset, "expiration", None)
            if raw_exp is None:
                return False
            if isinstance(raw_exp, datetime):
                exp_date = raw_exp.date()
            elif isinstance(raw_exp, date):
                exp_date = raw_exp
            else:
                try:
                    exp_date = datetime.strptime(str(raw_exp)[:10], "%Y-%m-%d").date()
                except ValueError:
                    return False
            if exp_date >= today:
                return False
        return True

    def _is_expiring_today(self, position_id: str | None = None) -> bool:
        """Check if any owned leg expires today or earlier."""
        position = self._runtime(position_id)
        if not position.owned_legs:
            return int(position.settings.get("dte", 0)) == 0
        today = self.get_datetime().date()
        for asset, _ in position.owned_legs:
            raw_exp = getattr(asset, "expiration", None)
            if raw_exp is None:
                continue
            if isinstance(raw_exp, datetime):
                exp_date = raw_exp.date()
            elif isinstance(raw_exp, date):
                exp_date = raw_exp
            else:
                try:
                    exp_date = datetime.strptime(str(raw_exp)[:10], "%Y-%m-%d").date()
                except ValueError:
                    continue
            if exp_date <= today:
                return True
        return False

    def _days_to_expiration(self, position_id: str | None = None) -> int | None:
        """Return the minimum days remaining to expiration across owned option legs."""
        position = self._runtime(position_id)
        if not position.owned_legs:
            return None
        today = self.get_datetime().date()
        min_days = None
        for asset, _ in position.owned_legs:
            raw_exp = getattr(asset, "expiration", None)
            if raw_exp is None:
                continue
            if isinstance(raw_exp, datetime):
                exp_date = raw_exp.date()
            elif isinstance(raw_exp, date):
                exp_date = raw_exp
            else:
                try:
                    exp_date = datetime.strptime(str(raw_exp)[:10], "%Y-%m-%d").date()
                except ValueError:
                    continue
            days = (exp_date - today).days
            if min_days is None or days < min_days:
                min_days = days
        return min_days

    def _fetch_historical_closes(self, lookback_bars: int = 30) -> list[float]:
        """Fetch historical daily close prices for the underlying asset."""
        prices: list[float] = []
        fetch = getattr(self, "get_historical_prices", None)
        if fetch is not None:
            try:
                bars = fetch(self._underlying, lookback_bars, timestep="day")
                if bars is None:
                    bars = fetch(self._underlying, lookback_bars, timestep="1 day")
                if bars is not None:
                    frame = getattr(bars, "pandas_df", None)
                    if frame is None:
                        frame = getattr(bars, "df", None)
                    if frame is not None and "close" in frame:
                        prices = [float(p) for p in frame["close"].dropna().values if p > 0]
                    elif isinstance(bars, list):
                        prices = [float(b["close"]) for b in bars if isinstance(b, dict) and "close" in b and float(b["close"]) > 0]
            except Exception as err:
                logger.debug("Could not fetch historical bars: %s", err)
        return prices

    def _calculate_underlying_sd(self, lookback_bars: int = 30) -> tuple[float, str]:
        """Compute the Z-score and entry quality of the underlying asset relative to historical mean.

        Returns:
            (z_score, quality_label) where quality_label is one of:
            "EXCELLENT" (<=0.5 SD), "GOOD" (0.5-1.0 SD), "MARGINAL" (1.0-1.5 SD), or "AVOID" (>1.5 SD).
        """
        forced = (
            os.getenv("STRATEGYBRO_FORCE_SD")
            or os.getenv("DYNAMIC_FORCE_SD")
            or self.parameters.get("force_sd")
        )
        if forced is not None:
            try:
                z = float(forced)
                return z, evaluate_strategybro_entry_quality(z)
            except ValueError:
                pass

        current_price = self._fetch_last_price(self._underlying)
        if current_price is None:
            return 0.0, "EXCELLENT"

        prices = self._fetch_historical_closes(lookback_bars)
        if len(prices) >= 2:
            z = calculate_zscore(prices, current_price)
        else:
            z = 0.0

        return z, evaluate_strategybro_entry_quality(z)

    def _calculate_realized_vol_ratio(self, lookback_bars: int = 25) -> float:
        """Compute ratio of short-term (5d) to medium-term (20d) realized volatility."""
        forced = os.getenv("VOL_CRUSH_FORCE_RV_RATIO") or self.parameters.get("force_rv_ratio")
        if forced is not None:
            try:
                return float(forced)
            except ValueError:
                pass
        prices = self._fetch_historical_closes(lookback_bars)
        current_price = self._fetch_last_price(self._underlying)
        if current_price is not None and float(current_price) > 0:
            prices = [*prices, float(current_price)]
        if len(prices) >= 6:
            return calculate_realized_vol_ratio(prices)
        return 1.0

    def _calculate_intraday_return_pct(self, current_price: float | None = None) -> float:
        """Compute current session return relative to session anchor or prior daily close."""
        forced = os.getenv("ORB_FORCE_RETURN_PCT") or self.parameters.get("force_intraday_return_pct")
        if forced is not None:
            try:
                return float(forced)
            except ValueError:
                pass
        price = current_price if current_price is not None else self._fetch_last_price(self._underlying)
        if price is None or float(price) <= 0:
            return 0.0
        spot = float(price)
        try:
            today = self.get_datetime().date()
        except Exception:
            today = None
        if today is not None and getattr(self, "_session_anchor_date", None) != today:
            prices = self._fetch_historical_closes(5)
            ref = prices[-1] if prices else spot
            self._session_anchor_date = today
            self._session_anchor_price = float(ref)
        anchor = getattr(self, "_session_anchor_price", None)
        if anchor is None or anchor <= 0:
            return 0.0
        return (spot - anchor) / anchor

    def _asset_from_key(self, key: str) -> Asset:
        decoded = json.loads(key)
        return Asset(
            symbol=decoded["symbol"],
            asset_type=Asset.AssetType.OPTION,
            expiration=decoded["expiration"],
            strike=float(decoded["strike"]),
            right=decoded["right"],
        )

    @fresh_broker_reads()
    def _reconcile_session_orders(self) -> None:
        """Resolve reservations while the executor waits outside strategy ticks."""
        managers = (self, *getattr(self, "_attached_strategies", ()))
        if not any(self._position_ledger.is_inflight(b) for b in self._position_ledger.open_baskets()):
            return
        try:
            with self._position_ledger.execution_gate():
                positions, orders = self.get_positions(), self.get_orders()
                if positions is None or orders is None:
                    return
                for manager in managers:
                    manager._reconcile_inflight_orders(orders, positions)
        except Exception as error:
            logger.warning("Session order reconciliation unavailable; reservations retained: %s", error)

    def _reconcile_broker_state(self) -> bool:
        """Reconcile broker visibility without assigning account-net ownership."""
        try:
            positions = self.get_positions()
            orders = self.get_orders()
            if positions is None or orders is None:
                return False
        except Exception as error:
            logger.warning("Broker reconciliation unavailable: %s", error)
            return False
        ambiguous = self._reconcile_inflight_orders(orders, positions)
        for basket in self._position_ledger.unreported_baskets(self._strategy_id()):
            if basket.intent == "open" and basket.state in {"open", "closed"}:
                self._record_filled_entry(basket)
            elif basket.intent == "add" and basket.state in {"open", "closed"}:
                self._record_filled_addition(basket)
            elif basket.intent in {"close", "trim"}:
                self._record_filled_exit(basket)
        self._hydrate_from_ledger()
        broker_options = [
            position
            for position in positions
            if getattr(getattr(position, "asset", None), "asset_type", None)
            == Asset.AssetType.OPTION
            and int(getattr(position, "quantity", 0)) != 0
        ]
        for position in tuple(self._position_states.values()):
            if (position.owned_legs
                    and not any(self._position_ledger.is_inflight(b) for b in self._position_baskets(position))
                    and not self._filter_owned_option_positions(positions, position_id=position.position_id)
                    and self._all_owned_legs_expired(position_id=position.position_id)):
                retired = self._position_ledger.expire_strategy_positions(
                    self._strategy_id(), self.get_datetime().date(), position_id=position.position_id)
                if retired > 0:
                    self._position_ledger.flush_outbox()
                    self._reset_flat_state(position_id=position.position_id)
                else:
                    logger.info(
                        "Startup: Position %s has expired legs but is awaiting settlement evidence; retaining inventory.",
                        position.position_id,
                    )
        self._hydrate_from_ledger()
        ledger_totals = self._position_ledger.all_owned_quantities()
        inflight_ids = {
            str(basket.broker_parent_id)
            for basket in self._position_ledger.open_baskets(self._strategy_id())
            if self._position_ledger.is_inflight(basket) and basket.broker_parent_id
        }
        self._cancellation_retries = {
            identifier: retry for identifier, retry in self._cancellation_retries.items()
            if identifier in inflight_ids
        }
        inflight = any(
            self._position_ledger.is_inflight(basket)
            for basket in self._position_ledger.open_baskets()
        )
        unmanaged = [
            position
            for position in broker_options
            if asset_key(position.asset) not in ledger_totals
            or (
                not inflight
                and int(getattr(position, "quantity", 0))
                != ledger_totals.get(asset_key(position.asset), 0)
            )
        ]
        self._recovery_lock = bool(unmanaged) or ambiguous
        self._startup_reconciled = True
        return True

    @staticmethod
    def _execution_evidence(order, leg):
        raw = getattr(order, "_raw", None) or getattr(order, "raw", None)
        raw = raw if isinstance(raw, dict) else {}
        filled = getattr(order, "exec_quantity", None)
        filled = raw.get("exec_quantity") if filled is None else filled
        price = getattr(order, "avg_fill_price", None)
        price = raw.get("avg_fill_price") if price is None else price
        try:
            count = float(filled)
            quantity = int(count)
            price = float(price) if price is not None else None
            if (not math.isfinite(count) or count != quantity
                    or not abs(int(leg["filled_signed_quantity"])) <= quantity <= abs(int(leg["requested_signed_quantity"]))
                    or quantity and (price is None or not math.isfinite(price) or price < 0)):
                return None
            return quantity, price
        except (TypeError, ValueError, OverflowError):
            return None

    def _reconcile_inflight_orders(self, orders: list, positions: list | None = None) -> bool:
        """Reconcile active packages by broker IDs/tags and flag ambiguous rollover."""
        broker_orders: list[object] = []

        parents_by_child: dict[str, dict[str, Any]] = {}

        def collect(order: Any, parent: Any | None = None) -> None:
            broker_orders.append(order)
            if parent is not None:
                child_id = getattr(order, "identifier", None) or getattr(order, "id", None)
                parent_id = getattr(parent, "identifier", None) or getattr(parent, "id", None)
                if child_id and parent_id:
                    parents_by_child.setdefault(str(child_id), {})[str(parent_id)] = parent
            for child in getattr(order, "child_orders", None) or ():
                collect(child, parent if parent is not None else order)

        for order in orders:
            collect(order)
        order_lookup: dict[str, object] = {}
        for order in broker_orders:
            identifier = getattr(order, "identifier", None) or getattr(order, "id", None)
            if identifier:
                order_lookup[str(identifier)] = order
        fetch_order = getattr(self, "get_order", None)
        broker = getattr(self, "broker", None)
        if type(broker).__module__.startswith("lumibot.brokers.tradier"):
            def fetch_order(identifier):
                with fresh_broker_reads():
                    row = broker._pull_broker_order(identifier)
                return broker._parse_broker_order_dict(row, self.name) if row else None
        terminal_map = {
            "canceled": "canceled",
            "cancelled": "canceled",
            "rejected": "rejected",
            "expired": "expired",
            "error": "failed",
            "failed": "failed",
        }
        ambiguous = False
        broker_quantities: dict[str, int] | None = None
        if positions is not None:
            broker_quantities = {}
            for position in positions:
                asset = getattr(position, "asset", None)
                if getattr(asset, "asset_type", None) == Asset.AssetType.OPTION:
                    key = asset_key(asset)
                    broker_quantities[key] = broker_quantities.get(key, 0) + int(position.quantity)
        today = self.get_datetime().date()
        stale_ids = {
            basket.package_id
            for basket in self._position_ledger.stale_day_baskets(self._strategy_id(), today)
        }
        for basket in self._position_ledger.open_baskets(self._strategy_id()):
            if not self._position_ledger.is_inflight(basket):
                continue
            parent_refreshed = False
            parent = order_lookup.get(str(basket.broker_parent_id or ""))
            if parent is not None and basket.broker_parent_id and type(broker).__module__.startswith("lumibot.brokers.tradier"):
                try:
                    parent = fetch_order(str(basket.broker_parent_id))
                    parent_refreshed = parent is not None
                except Exception:
                    parent = None
                if parent is None:
                    ambiguous = True
                    continue
            if (
                parent is None
                and basket.broker_parent_id
                and callable(fetch_order)
            ):
                try:
                    parent = fetch_order(str(basket.broker_parent_id))
                    parent_refreshed = parent is not None
                except Exception:
                    parent = None
            if parent is None:
                candidates: dict[str, Any] = {}
                for leg in basket.legs:
                    child_id = str(leg.get("broker_order_id") or "")
                    candidates.update(parents_by_child.get(child_id, {}))
                    child = order_lookup.get(child_id)
                    if child is None and child_id and callable(fetch_order):
                        try:
                            child = fetch_order(child_id)
                        except Exception:
                            child = None
                        if child is not None:
                            order_lookup[child_id] = child
                    parent_id = getattr(child, "parent_identifier", None) or getattr(child, "parent_id", None)
                    if parent_id:
                        candidate = order_lookup.get(str(parent_id))
                        if candidate is None and callable(fetch_order):
                            try:
                                candidate = fetch_order(str(parent_id))
                            except Exception:
                                candidate = None
                        if candidate is not None:
                            candidates[str(parent_id)] = candidate
                if basket.broker_parent_id:
                    candidates = {
                        key: value for key, value in candidates.items()
                        if key == str(basket.broker_parent_id)
                    }
                if len(candidates) > 1:
                    ambiguous = True
                    continue
                parent = next(iter(candidates.values()), None)
                if parent is None:
                    parent = self._find_order_by_tag(orders, basket.broker_tag)
                if parent is not None:
                    parent_id = getattr(parent, "identifier", None) or getattr(parent, "id", None)
                    if basket.broker_parent_id and str(parent_id) != str(basket.broker_parent_id):
                        ambiguous = True
                        continue
                    try:
                        self._position_ledger.bind_broker_submission(basket.package_id, parent)
                        basket = self._position_ledger.get_basket(basket.package_id)
                    except (KeyError, ValueError):
                        ambiguous = True
                        continue
            if parent is not None and getattr(parent, "child_orders", None):
                self._position_ledger.bind_broker_children(
                    basket.package_id, parent.child_orders
                )
                basket = self._position_ledger.get_basket(basket.package_id)
            # DAY orders must stop working at the strategy's regular close.
            # Reconciliation also catches a missed close after a restart. A
            # cancellation request never releases ownership or reserved risk.
            market = getattr(broker, "market", None) or "NYSE"
            if (parent is not None and str(getattr(parent, "identifier", None) or getattr(parent, "id", None)) == str(basket.broker_parent_id)
                    and (basket.duration or "").lower() == "day"
                    and day_session_ended(basket.created_at, self._current_datetime(), market=market)):
                raw = getattr(parent, "_raw", None)
                duration = (raw.get("duration") if isinstance(raw, dict) else None) or getattr(parent, "time_in_force", None) or basket.duration
                if str(duration).lower() == "day" and parent.is_active():
                    self._cancel_managed_order(parent)
            leg_statuses: list[str] = []
            complete_evidence = True
            current_children = {str(getattr(child, "identifier", "")): child for child in
                                getattr(parent, "child_orders", None) or ()} if parent_refreshed and type(broker).__module__.startswith("lumibot.brokers.tradier") else {}
            for leg in basket.legs:
                broker_id = leg.get("broker_order_id")
                broker_order = current_children.get(str(broker_id))
                if self._execution_evidence(broker_order, leg) is None:
                    broker_order = None
                if broker_order is None and broker_id and callable(fetch_order):
                    try:
                        broker_order = fetch_order(str(broker_id))
                    except Exception:
                        broker_order = None
                if broker_order is None and not (broker_id and type(broker).__module__.startswith("lumibot.brokers.tradier")):
                    broker_order = order_lookup.get(str(broker_id or ""))
                if broker_order is None:
                    complete_evidence = False
                    continue
                status = str(getattr(broker_order, "status", "") or "").strip().lower()
                if status:
                    leg_statuses.append(status)
                evidence = self._execution_evidence(broker_order, leg)
                if evidence is None:
                    complete_evidence = False
                else:
                    broker_filled, fill_price = evidence
                    self._retain_order_evidence(broker_order, leg, parent)
                    already_filled = abs(int(leg["filled_signed_quantity"]))
                    if broker_filled > already_filled:
                        sign = 1 if int(leg["requested_signed_quantity"]) > 0 else -1
                        filled_basket = self._position_ledger.record_fill(
                            str(leg["order_id"]),
                            sign * broker_filled,
                            fill_price,
                            broker_id=str(broker_id) if broker_id else None,
                            cumulative=True,
                        )
                        if filled_basket.state == "open" and filled_basket.intent == "open":
                            self._record_filled_entry(filled_basket)
                        elif filled_basket.state == "open" and filled_basket.intent == "add":
                            self._record_filled_addition(filled_basket)
                        elif (
                            filled_basket.state == "closed"
                            and filled_basket.intent in {"close", "trim"}
                        ):
                            self._record_filled_exit(filled_basket)
            refreshed = self._position_ledger.get_basket(basket.package_id)
            if complete_evidence and len(leg_statuses) == len(refreshed.legs) and all(
                status in terminal_map for status in leg_statuses
            ):
                term_status = terminal_map[leg_statuses[0]]
                now_dt = self._current_datetime()
                if not hasattr(self, "_terminal_package_times"):
                    self._terminal_package_times = {}
                self._terminal_package_times[refreshed.package_id] = self._as_aware_utc(now_dt)
                if all(int(leg["filled_signed_quantity"]) == 0 for leg in refreshed.legs):
                    self._position_ledger.record_terminal_status(refreshed.package_id, term_status, updated_at=now_dt)
                else:
                    resolved = self._position_ledger.resolve_partial_package(refreshed.package_id, term_status, updated_at=now_dt)
                    if resolved.state == "open" and resolved.intent == "open":
                        self._record_filled_entry(resolved)
                    elif resolved.state == "open" and resolved.intent == "add":
                        self._record_filled_addition(resolved)
                continue
            parent_status = str(getattr(parent, "status", "") or "").lower()
            if complete_evidence and parent_status in terminal_map and len(leg_statuses) == len(refreshed.legs):
                term_status = terminal_map[parent_status]
                now_dt = self._current_datetime()
                if not hasattr(self, "_terminal_package_times"):
                    self._terminal_package_times = {}
                self._terminal_package_times[refreshed.package_id] = self._as_aware_utc(now_dt)
                if all(int(leg["filled_signed_quantity"]) == 0 for leg in refreshed.legs):
                    self._position_ledger.record_terminal_status(refreshed.package_id, term_status, updated_at=now_dt)
                else:
                    resolved = self._position_ledger.resolve_partial_package(refreshed.package_id, term_status, updated_at=now_dt)
                    if resolved.state == "open" and resolved.intent == "open":
                        self._record_filled_entry(resolved)
                    elif resolved.state == "open" and resolved.intent == "add":
                        self._record_filled_addition(resolved)
                continue
            # The recent-order list drops prior sessions. DAY expiry plus a
            # matching fresh inventory snapshot resolves the unfilled remainder
            # even when no historical parent/leg identity can be recovered.
            raw = getattr(parent, "_raw", None)
            duration = ((raw.get("duration") if isinstance(raw, dict) else None)
                        or getattr(parent, "time_in_force", None) or refreshed.duration)
            if (broker_quantities is not None and str(duration or "").lower() == "day"
                    and (parent_status in terminal_map or parent is None and not any(
                        str(getattr(order, "status", "")).lower() in {
                            "open", "pending", "submitted", "partially_filled", "partial",
                            "pending_cancel", "pending_replace", "held", "accepted", "new"}
                        for order in orders))):
                resolved = self._position_ledger.expire_day_order(
                    refreshed.package_id, now=self._current_datetime(),
                    broker_quantities=broker_quantities, market=getattr(broker, "market", None) or "NYSE")
                if resolved is not None:
                    logger.info("[%s] DAY session expired for %s; released unfilled reservation, retained fills",
                                self._strategy_id(), resolved.package_id)
                    continue
            known_reserved_order = (
                parent_refreshed and complete_evidence
                and len(leg_statuses) == len(refreshed.legs)
                and parent.is_active()
            )
            stale_unknown = (
                refreshed.package_id in stale_ids
                and self._position_ledger.is_inflight(refreshed)
                and not known_reserved_order
            )
            if parent is None or stale_unknown or (parent_status in terminal_map and (not complete_evidence or len(leg_statuses) < len(refreshed.legs))):
                # A freshly identified pending order retains its slot and full
                # risk reservation across DAY rollover. Only missing identity or
                # execution evidence requires locking other independent entries.
                ambiguous = True
        return ambiguous

    def _supports_atomic_multileg(self) -> bool:
        broker = getattr(self, "broker", None)
        name = str(getattr(broker, "name", type(broker).__name__)).lower()
        return "tradier" in name

    @fresh_broker_reads()
    def _submit_reduce_only_orders(
        self,
        legs: list[tuple[Asset, int]],
        position_id: str,
        order_type: str = "debit",
        price: float | None = None,
    ) -> list:
        """Leave residuals queued until durable, scoped fill reconciliation exists."""
        logger.error("Scoped recovery for position %s is pending reconciliation; no orders submitted", position_id)
        return []

    def _submit_package(
        self,
        legs: list[tuple[Asset, int]],
        *,
        intent: str,
        order_type: str,
        price: float,
        replacement_of: str | None = None,
        reprice_count: int = 0,
        position_id: str | None = None,
    ) -> list:
        if not self._supports_atomic_multileg():
            raise RuntimeError("broker does not have a verified atomic multi-leg adapter")
        opening = intent in {"open", "add"}
        if opening:
            if len(legs) not in (2, 4) or len({abs(quantity) for _, quantity in legs}) != 1:
                raise ValueError("an atomic package requires 2 or 4 balanced legs with matching quantity")
        elif len(legs) not in (2, 4) or len({abs(quantity) for _, quantity in legs}) != 1:
            # Unbalanced close package requested: route through scoped reduce-only recovery path
            pid = position_id or self._runtime().position_id
            self._position_ledger.enqueue_recovery_job(
                scope="position",
                target_id=pid,
                payload={"strategy_id": self._strategy_id(), "legs": [(asset_key(a), qty) for a, qty in legs]},
            )
            return self._submit_reduce_only_orders(
                legs,
                position_id=pid,
                order_type=order_type,
                price=price,
            )
        package_id = uuid.uuid4().hex
        owner = self._strategy_id()
        broker_tag = self._broker_routing_tag()
        opening = intent in {"open", "add"}
        if intent == "open":
            position_id = position_id or uuid.uuid4().hex
        elif position_id is None:
            position_id = self._runtime().position_id
        orders = []
        ledger_legs = []
        for index, (asset, signed_quantity) in enumerate(legs, start=1):
            if opening:
                side = "buy_to_open" if signed_quantity > 0 else "sell_to_open"
            else:
                side = "buy_to_close" if signed_quantity > 0 else "sell_to_close"
            order = self.create_order(asset, abs(signed_quantity), side)
            order_id = f"{package_id}:{index}"
            for attribute, value in (
                ("_ledger_order_id", order_id),
                ("_package_id", package_id),
                ("_strategy_id", owner),
                ("_position_id", position_id),
                ("tag", broker_tag),
            ):
                setattr(order, attribute, value)
            custom_params = getattr(order, "custom_params", None)
            if isinstance(custom_params, dict):
                custom_params["tag"] = broker_tag
            self._asset_registry[asset_key(asset)] = asset
            orders.append(order)
            ledger_legs.append(LedgerLeg(order_id, asset_key(asset), signed_quantity))
        with self._position_ledger.execution_gate():
            if opening and self._position_ledger.is_panic_halted():
                raise RuntimeError("Atomic package rejected: panic halt is active")
            if opening:
                cooldown = self._position_ledger.entry_cooldown_until(self._strategy_id())
                if cooldown is not None and self._as_aware_utc(self._current_datetime()) < cooldown:
                    raise RuntimeError("Atomic package rejected: stop cooldown is active")
                settings = self._runtime(position_id).settings if intent == "add" else self.parameters
                minimum = self._minimum_entry_credit(settings)
                if order_type != "credit" or not math.isfinite(price) or price < minimum:
                    raise ValueError(f"Atomic package credit must be at least {minimum:.2f}")
            capacity = int(self.parameters.get("max_concurrent_positions", 1))
            cap = float(self.parameters.get("max_position_risk", 5000.0))
            global_cap = self.parameters.get("max_portfolio_risk") or os.getenv("IC_MAX_PORTFOLIO_RISK")
            override = (os.getenv("IC_MAX_SINGLE_TRADE_PCT") or "").strip()
            try:
                pct = float(override) if override else get_max_single_trade_pct(cap)
            except ValueError:
                pct = get_max_single_trade_pct(cap)
            if not math.isfinite(pct) or not 0 < pct <= 1:
                pct = get_max_single_trade_pct(cap)
            portfolio_value = self._get_portfolio_value() if opening else None
            limits = dict(
                max_positions=capacity, strategy_cap=cap,
                portfolio_cap=float(global_cap) if global_cap else None,
                single_trade_cap=cap * pct,
                allocation_cap=portfolio_value * float(self.parameters.get("max_allocation", .03))
                if portfolio_value is not None and portfolio_value > 0 else None,
                min_spacing_seconds=float(self.parameters.get("entry_spacing_minutes", 60)) * 60 if capacity > 1 else 0,
                now=self._current_datetime(),
            )
            if opening:
                positions = self.get_positions()
                if positions is None:
                    raise RuntimeError("broker inventory is unavailable")
                for existing in positions:
                    if getattr(getattr(existing, "asset", None), "asset_type", None) != Asset.AssetType.OPTION:
                        continue
                    for asset, quantity in legs:
                        if asset_key(asset) == asset_key(existing.asset) and int(existing.quantity) * quantity < 0:
                            raise RuntimeError("Atomic package rejected: opposing leg conflicts with broker inventory")
            self._position_ledger.reserve_submission(
                package_id,
                owner,
                intent,
                ledger_legs,
                limits=limits,
                duration="day",
                limit_price=round(float(price), 2),
                broker_tag=broker_tag,
                replacement_of=replacement_of,
                reprice_count=reprice_count,
                created_at=self._current_datetime(),
                position_id=position_id,
                settings={**self._management_settings(), "quantity": max(abs(q) for _, q in legs)},
            )
            submit_kwargs = {
                "is_multileg": True,
                "order_type": order_type,
                "duration": "day",
            }
            if order_type != "market":
                submit_kwargs["price"] = round(float(price), 2)
            self._position_ledger.mark_submission_started(package_id)
            submitted = self.submit_order(orders, **submit_kwargs)
        parents = list(submitted) if isinstance(submitted, (list, tuple)) else []
        if not parents:
            # Some test doubles and older adapters mutate/return the local legs.
            parents = orders
        elif len(parents) == 1:
            parent = parents[0]
            for attribute, value in (
                ("_package_id", package_id),
                ("_strategy_id", owner),
                ("_position_id", position_id),
                ("tag", broker_tag),
            ):
                setattr(parent, attribute, value)
            custom_params = getattr(parent, "custom_params", None)
            if isinstance(custom_params, dict):
                custom_params["tag"] = broker_tag
            try:
                self._position_ledger.bind_broker_submission(package_id, parent)
            except ValueError:
                logger.warning("Submitted broker parent did not expose an identifier yet.")
        return parents

    def on_filled_order(
        self,
        position,
        order,
        price: float,
        quantity: int,
        multiplier: float,
    ) -> None:
        """Record confirmed broker fills and route host callbacks to leg owners."""
        local_id = getattr(order, "_ledger_order_id", None)
        broker_id = getattr(order, "identifier", None) or getattr(order, "id", None)
        parent_id = getattr(order, "parent_identifier", None) or getattr(order, "parent_id", None)
        order_asset = getattr(order, "asset", None)
        resolved = self._position_ledger.resolve_leg(
            local_id=str(local_id) if local_id else None,
            broker_id=str(broker_id) if broker_id else None,
            parent_id=str(parent_id) if parent_id else None,
            asset_key=asset_key(order_asset) if order_asset is not None else None,
            side=str(getattr(order, "side", "") or "").lower() or None,
        )
        owner = self._strategy_id()
        if resolved is not None:
            owner = self._position_ledger.get_basket(str(resolved["package_id"])).strategy_id
        target = getattr(self, "_strategy_lookup", {}).get(owner, self)
        target._record_filled_order(order, price, quantity)

    def on_partially_filled_order(
        self,
        position,
        order,
        price: float,
        quantity: int,
        multiplier: float,
    ) -> None:
        """Record confirmed broker partial fills and route host callbacks to leg owners."""
        local_id = getattr(order, "_ledger_order_id", None)
        broker_id = getattr(order, "identifier", None) or getattr(order, "id", None)
        parent_id = getattr(order, "parent_identifier", None) or getattr(order, "parent_id", None)
        order_asset = getattr(order, "asset", None)
        resolved = self._position_ledger.resolve_leg(
            local_id=str(local_id) if local_id else None,
            broker_id=str(broker_id) if broker_id else None,
            parent_id=str(parent_id) if parent_id else None,
            asset_key=asset_key(order_asset) if order_asset is not None else None,
            side=str(getattr(order, "side", "") or "").lower() or None,
        )
        owner = self._strategy_id()
        if resolved is not None:
            owner = self._position_ledger.get_basket(str(resolved["package_id"])).strategy_id
        target = getattr(self, "_strategy_lookup", {}).get(owner, self)
        target._record_filled_order(order, price, quantity, partial=True)

    def _retain_order_evidence(self, order: Any, leg: dict[str, Any], parent: Any = None) -> None:
        """Capture reporting evidence using only an existing provider response."""
        try:
            from strategies.runtime_records import RecordScope
            broker = getattr(self, "broker", None)
            account = getattr(broker, "_tradier_account_number", None)
            paper = getattr(broker, "_tradier_paper", None)
            if not isinstance(account, str) or not account or not isinstance(paper, bool):
                return
            scope = RecordScope("paper" if paper else "live", "tradier", account)
            parent_response = getattr(parent, "_raw", None)
            if parent is None:
                parent_id = leg.get("broker_parent_id")
                # SDK get_order reads tracked objects; it does not poll the broker.
                get_cached_order = getattr(broker, "get_order", None)
                cached_parent = get_cached_order(parent_id) if parent_id and callable(get_cached_order) else None
                parent_response = getattr(cached_parent, "_raw", None)
                if not isinstance(parent_response, dict) or not parent_response.get("leg"):
                    reads = getattr(broker, "_shared_broker_reads", None)
                    if reads is not None and reads.account == account and reads.sandbox is paper:
                        snapshot = reads.snapshot("orders").get("data")
                        orders = snapshot.get("orders") if isinstance(snapshot, dict) else None
                        rows = orders.get("order", []) if isinstance(orders, dict) else []
                        rows = [rows] if isinstance(rows, dict) else rows
                        rows = rows if isinstance(rows, list) else []
                        matches = [row for row in rows if isinstance(row, dict) and str(row.get("id")) == str(parent_id)]
                        parent_response = matches[0] if len(matches) == 1 else None
            self._position_ledger.capture_order_evidence(
                order, leg, scope=scope, parent_response=parent_response,
            )
        except Exception as error:
            logger.warning("Could not retain reporting evidence; fill ownership remains recorded: %s", error)

    def _journal_evidence(self, basket, event_id: str, trade_id: str) -> dict:
        try:
            metadata = self._position_ledger.attach_package_evidence(basket.package_id,event_id,trade_id)
            metadata["realization_legs"] = metadata.pop("legs")
            metadata["scope"] = self._position_ledger.record_scope
            return metadata
        except Exception as error:
            logger.warning("Could not attach reporting evidence: %s", error)
            return {"fees_complete":False}

    def _record_filled_order(self, order, price: float, quantity: int, *, partial: bool = False) -> None:
        # A multileg parent's aggregate price/quantity is not a contract fill.
        # Child callbacks and broker reconciliation establish inventory.
        if getattr(order, "order_class", None) == "multileg":
            return
        local_id = getattr(order, "_ledger_order_id", None)
        broker_id = getattr(order, "identifier", None) or getattr(order, "id", None)
        parent_id = getattr(order, "parent_identifier", None) or getattr(order, "parent_id", None)
        order_asset = getattr(order, "asset", None)
        side = str(getattr(order, "side", "")).lower()
        resolved = self._position_ledger.resolve_leg(
            local_id=str(local_id) if local_id else None,
            broker_id=str(broker_id) if broker_id else None,
            parent_id=str(parent_id) if parent_id else None,
            asset_key=asset_key(order_asset) if order_asset is not None else None,
            side=side or None,
        )
        if resolved is None:
            logger.warning("Ignoring fill without broker child details; no ledger leg matched.")
            return
        if self._position_ledger.get_basket(str(resolved["package_id"])).strategy_id == MANUAL_OWNER:
            # Manual executions are imported explicitly and never enter a host's
            # strategy journal or position-management state through SDK replay.
            return
        sign = 1 if "buy" in side else -1

        raw_dict = getattr(order, "_raw", None)
        if not isinstance(raw_dict, dict):
            raw_dict = {}

        def numeric(value):
            if isinstance(value, bool) or not isinstance(value, (int, float, str, Decimal)):
                return None
            try:
                value = float(value)
                return value if math.isfinite(value) and value >= 0 else None
            except ValueError:
                return None

        cumulative_quantity = numeric(raw_dict.get("exec_quantity"))
        if cumulative_quantity is None:
            cumulative_quantity = numeric(getattr(order, "exec_quantity", None))
        if not partial and cumulative_quantity is None:
            cumulative_quantity = numeric(getattr(order, "quantity", None)) or numeric(quantity)
        fill_price = numeric(raw_dict.get("avg_fill_price"))
        if fill_price is None:
            fill_price = numeric(getattr(order, "avg_fill_price", None))
        if fill_price is None:
            fill_price = float(price)
        if cumulative_quantity is None or fill_price is None or not cumulative_quantity.is_integer():
            # A slice without cumulative execution evidence cannot be deduplicated.
            self._recovery_lock = True
            logger.warning("Fill lacks cumulative execution evidence; retaining reservation.")
            return
        fill_quantity = int(cumulative_quantity)
        if fill_quantity <= 0:
            return

        self._retain_order_evidence(order, resolved)
        basket = self._position_ledger.record_fill(
            str(resolved["order_id"]),
            sign * fill_quantity,
            fill_price,
            broker_id=str(broker_id) if broker_id else None,
            parent_id=str(parent_id) if parent_id else None,
            asset_key=str(resolved["asset_key"]),
            side=side,
            cumulative=True,
        )
        if basket.state == "open" and basket.intent == "open":
            self._record_filled_entry(basket)
        elif basket.state == "open" and basket.intent == "add":
            self._record_filled_addition(basket)
        elif basket.state == "closed" and basket.intent in {"close", "trim"}:
            self._record_filled_exit(basket)
        self._hydrate_from_ledger()

    def _record_filled_exit(
        self, basket
    ) -> None:
        position = self._runtime(basket.position_id)
        if self._position_ledger.journaled(basket.package_id, "REALIZATION"):
            return
        trade_id = basket.trade_id or getattr(position, "current_trade_id", None)
        if not trade_id:
            return
        try:
            from trade_logger import record_trade_adjustment, record_trade_exit
            closing_cash = float(basket.closing_cash_flow or 0.0)
            fee_rate = option_fee_per_contract()
            close_fees = option_execution_fees(
                (leg["filled_signed_quantity"] for leg in basket.legs), fee_rate
            )
            realized_opening = self._position_ledger.closing_opening_cash(basket.package_id)
            actual_gross = realized_opening + closing_cash
            actual_net = round(actual_gross - close_fees, 2)
            mfe = max(float(getattr(position, "mfe_pnl", 0.0)), actual_net)
            mae = min(float(getattr(position, "mae_pnl", 0.0)), actual_net)
            reason = position.pending_exit_reason or "MANUAL"
            if reason == "STOP_LOSS":
                self._record_stop_cooldown(position)

            remaining = self._position_ledger.quantities_after(basket.package_id)
            remaining_qty = max((abs(value) for value in remaining.values()), default=0)
            if remaining_qty > 0:
                record_trade_adjustment(
                    event_id=f"{basket.package_id}:REALIZATION",
                    **self._journal_evidence(basket, f"{basket.package_id}:REALIZATION", trade_id),
                    trade_id=trade_id,
                    strategy_name=self._strategy_id(),
                    opening_credit_cash=realized_opening,
                    closing_cash=closing_cash,
                    gross_pnl=actual_gross,
                    fees=close_fees,
                    net_pnl=actual_net,
                    remaining_quantity=remaining_qty,
                )
                self._position_ledger.mark_journaled(basket.package_id, "REALIZATION")
                logger.info(
                    "Recorded confirmed trim for %s: Net $%.2f, %d contracts remain.",
                    self._strategy_id(),
                    actual_net,
                    remaining_qty,
                )
                return

            record_trade_exit(
                event_id=f"{basket.package_id}:REALIZATION",
                **self._journal_evidence(basket, f"{basket.package_id}:REALIZATION", trade_id),
                trade_id=trade_id,
                strategy_name=self._strategy_id(),
                exit_reason=reason,
                exit_trigger=reason,
                decision_gross_pnl=position.pending_gross_pnl,
                decision_net_pnl=position.pending_net_pnl,
                decision_evidence=position.pending_decision,
                pnl=actual_net,
                fees=close_fees,
                gross_pnl=actual_gross,
                net_pnl=actual_net,
                opening_credit_cash=realized_opening,
                closing_cash=closing_cash,
                mfe_pnl=mfe,
                mae_pnl=mae,
            )
            self._position_ledger.mark_journaled(basket.package_id, "REALIZATION")
            position.current_trade_id = None
            self._reset_flat_state(position_id=position.position_id)
            logger.info(
                "Recorded confirmed fill exit for %s: Gross $%.2f, Fees -$%.2f, Net Realized $%.2f",
                self._strategy_id(), actual_gross, close_fees, actual_net,
            )
        except Exception as error:
            logger.warning("Could not persist filled trade exit: %s", error)

    def _record_filled_entry(self, basket) -> None:
        position = self._runtime(basket.position_id)
        if self._position_ledger.journaled(basket.package_id, "ENTRY"):
            position.current_trade_id = basket.trade_id or position.current_trade_id
            return
        try:
            from trade_logger import record_trade_entry

            strikes = {}
            for leg in basket.legs:
                asset = json.loads(leg["asset_key"])
                right = str(asset["right"]).lower()
                side = "long" if int(leg["filled_signed_quantity"]) > 0 else "short"
                strikes[f"{side}_{right}"] = asset["strike"]
            opening_cash = float(basket.opening_cash_flow or 0.0)
            quantity = max(abs(int(leg["filled_signed_quantity"])) for leg in basket.legs)
            position.current_trade_id = record_trade_entry(
                event_id=f"{basket.package_id}:ENTRY",
                **self._journal_evidence(basket, f"{basket.package_id}:ENTRY", position.current_trade_id or position.position_id),
                trade_id=position.current_trade_id or position.position_id,
                strategy_name=self._strategy_id(),
                symbol=self.parameters["symbol"],
                expiry=json.loads(basket.legs[0]["asset_key"])["expiration"],
                strikes=strikes,
                quantity=quantity,
                credit=opening_cash / (100.0 * quantity),
                opening_credit_cash=opening_cash,
                entry_fees=option_execution_fees(
                    (leg["filled_signed_quantity"] for leg in basket.legs),
                    option_fee_per_contract(),
                ),
                max_risk=position.settings["wing_width"] * 100 * quantity,
            )
            self._position_ledger.set_trade_id(basket.package_id, position.current_trade_id)
            self._position_ledger.mark_journaled(basket.package_id, "ENTRY")
        except Exception as error:
            logger.warning("Could not persist filled trade entry: %s", error)

    def _record_filled_addition(self, basket) -> None:
        position = self._runtime(basket.position_id)
        if self._position_ledger.journaled(basket.package_id, "ADDITION"):
            return
        trade_id = getattr(position, "current_trade_id", None)
        if not trade_id:
            trade_id = self._position_ledger.active_trade_id(self._strategy_id(), position.position_id)
        if not trade_id:
            logger.warning("Could not journal filled addition without an active trade ID.")
            return
        try:
            from trade_logger import record_trade_addition

            quantity = max(
                abs(int(leg["filled_signed_quantity"])) for leg in basket.legs
            )
            record_trade_addition(
                event_id=f"{basket.package_id}:ADDITION",
                **self._journal_evidence(basket, f"{basket.package_id}:ADDITION", trade_id),
                trade_id=trade_id,
                strategy_name=self._strategy_id(),
                opening_credit_cash=float(basket.opening_cash_flow or 0.0),
                fees=option_execution_fees(
                    (leg["filled_signed_quantity"] for leg in basket.legs),
                    option_fee_per_contract(),
                ),
                added_quantity=quantity,
            )
            self._position_ledger.set_trade_id(basket.package_id, trade_id)
            self._position_ledger.mark_journaled(basket.package_id, "ADDITION")
        except Exception as error:
            logger.warning("Could not persist filled trade addition: %s", error)

    def _minimum_entry_credit(self, settings: dict) -> float:
        """Use one credit floor for candidate checks and the submission boundary."""
        width = float(settings["wing_width"])
        ratio = float(settings.get("min_credit_to_width", self.parameters.get("min_credit_to_width", .10)))
        absolute = float(settings.get("min_credit") or os.getenv("IC_MIN_CREDIT") or 0)
        return max(width * ratio, absolute)

    def _package_price(
        self,
        legs: list[tuple[Asset, int]],
        *,
        order_type: str = "credit",
    ) -> float | None:
        quantities = {abs(quantity) for _, quantity in legs}
        if len(quantities) != 1:
            return None
        try:
            quotes = self._fetch_quotes([asset for asset, _ in legs])
        except Exception as error:
            logger.warning("Package quotes unavailable: %s", error)
            return None
        marks = [
            (signed_quantity, self._quote_midpoint(quote))
            for (_, signed_quantity), quote in zip(legs, quotes)
        ]
        value = bounded_option_value(legs, [mark for _, mark in marks])
        if value is None:
            return None
        quantity = quantities.pop()
        if quantity <= 0:
            return None
        unit_value = value / (100.0 * quantity)
        if order_type == "credit":
            if unit_value >= 0:
                return None
            credit = -unit_value
            # Keep the quoted credit by default, including in paper mode.
            # An explicit simulator discount remains available for experiments.
            discount_pct_str = os.getenv("SANDBOX_CREDIT_DISCOUNT_PCT", "0.0")
            try:
                discount_pct = float(discount_pct_str)
            except ValueError:
                discount_pct = 0.0
            if discount_pct > 0:
                credit = max(round(credit * (1.0 - discount_pct), 2), 0.05)
            return round(credit, 2)
        if order_type == "debit":
            if unit_value > 0:
                return max(unit_value, 0.01)
            # Mid is zero or a net credit (max-profit decay). A closing limit must still
            # be sent; a minimal positive debit is marketable at or better than the market.
            natural = 0.0
            for (_, signed_quantity), quote in zip(legs, quotes):
                bounds = self._quote_bounds(quote)
                if bounds is None:
                    return None
                bid_value, ask_value = bounds
                natural += (ask_value if signed_quantity > 0 else -bid_value) * abs(signed_quantity)
            natural_unit = natural / quantity
            return max(natural_unit / 2.0, 0.01) if natural_unit > 0 else 0.01
        return abs(unit_value)

    @staticmethod
    def _as_aware_utc(value: datetime) -> datetime:
        return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)

    @staticmethod
    def _find_order_by_tag(orders: Iterable[Any], tag: str | None) -> Any | None:
        """Resolve only a unique legacy package tag, never a shared host tag."""
        match = re.fullmatch(r"IC_?([0-9a-f]{20})", tag or "")
        if match is None:
            return None
        tags = {f"IC_{match[1]}", f"IC{match[1]}"}
        candidates: set[str] = set()
        order_lookup: dict[str, Any] = {}
        stack = [(order, order) for order in orders]
        while stack:
            order, parent = stack.pop()
            identifier = getattr(order, "identifier", None) or getattr(order, "id", None)
            if identifier:
                order_lookup[str(identifier)] = order
            custom = getattr(order, "custom_params", None)
            order_tag = getattr(order, "tag", None) or (custom.get("tag") if isinstance(custom, dict) else None)
            if str(order_tag or "") in tags:
                parent_id = getattr(parent, "identifier", None) or getattr(parent, "id", None)
                if order is parent:
                    parent_id = getattr(order, "parent_identifier", None) or getattr(order, "parent_id", None) or parent_id
                if not parent_id:
                    return None
                candidates.add(str(parent_id))
            stack.extend((child, parent) for child in getattr(order, "child_orders", None) or ())
        if len(candidates) != 1:
            return None
        return order_lookup.get(next(iter(candidates)))

    @staticmethod
    def _quote_bounds(quote) -> tuple[float, float] | None:
        if quote is None:
            return None
        if isinstance(quote, dict):
            bid, ask = quote.get("bid"), quote.get("ask")
        else:
            bid, ask = getattr(quote, "bid", None), getattr(quote, "ask", None)
        if isinstance(bid, bool) or isinstance(ask, bool) or bid is None or ask is None:
            return None
        try:
            bid_value, ask_value = float(bid), float(ask)
        except (TypeError, ValueError):
            return None
        if not (math.isfinite(bid_value) and math.isfinite(ask_value)):
            return None
        if bid_value < 0 or ask_value < bid_value or ask_value <= 0:
            return None
        midpoint = (bid_value + ask_value) / 2
        # Reject both large absolute gaps and noisy relative spreads. Cheap
        # options retain a 25-cent allowance; no leg may have a gap above $1.
        if ask_value - bid_value > min(1.0, max(.25, midpoint * .5)) + 1e-9:
            return None
        return bid_value, ask_value

    @classmethod
    def _quote_midpoint(cls, quote) -> float | None:
        bounds = cls._quote_bounds(quote)
        if bounds is None:
            return None
        return (bounds[0] + bounds[1]) / 2.0

    def _reset_flat_state(self, position_id: str | None = None) -> None:
        position = self._runtime(position_id)
        logger.info("Position closed or reconciled flat. Resetting strategy state.")
        position.position_open = False
        position.entry_credit = 0.0
        position.entry_credit_cash = 0.0
        position.entry_orders = []
        position.closing_orders = []
        position.owned_legs = []
        position.exit_requested = False
        position.pending_exit_reason = None
        position.pending_gross_pnl = None
        position.pending_fees = None
        position.pending_net_pnl = None
        position.pending_decision = None
        position.mfe_pnl = 0.0
        self._reset_exit_signal(position)
        position.mae_pnl = 0.0
        position.last_pnl = 0.0
        self._publish_position_snapshot(position, pnl=0.0, status="FLAT")

    def _asset_matches(self, a, b) -> bool:
        """Compare two option assets by symbol, expiration, strike, and right (or equality)."""
        if a == b:
            return True
        for attr in ("symbol", "expiration", "strike", "right"):
            if getattr(a, attr, None) is None or getattr(b, attr, None) is None:
                return False
            if getattr(a, attr) != getattr(b, attr):
                return False
        return True

    def _filter_owned_option_positions(self, positions, position_id: str | None = None) -> list:
        """Return only option positions belonging to this strategy variant (or all if single-strategy fallback)."""
        position = self._runtime(position_id)
        option_positions = [
            p for p in positions
            if hasattr(p.asset, 'asset_type')
            and p.asset.asset_type == Asset.AssetType.OPTION
            and p.quantity != 0
        ]
        owned_legs = getattr(position, "owned_legs", None)
        if not owned_legs:
            return []
        return [
            p for p in option_positions
            if any(self._asset_matches(p.asset, leg_asset) for leg_asset, _ in owned_legs)
        ]

    def _owned_quantity_for_position(self, pos, position_id: str | None = None) -> int:
        """Return signed contract quantity owned by this strategy for `pos.asset`."""
        position = self._runtime(position_id)
        owned_legs = getattr(position, "owned_legs", None)
        if not owned_legs:
            return 0
        for leg_asset, signed_qty in owned_legs:
            if self._asset_matches(pos.asset, leg_asset):
                # Cap by actual broker position magnitude in case of partial fills
                sign = 1 if signed_qty > 0 else -1
                return sign * min(abs(int(signed_qty)), abs(int(pos.quantity)))
        return 0

    def _fetch_last_price(self, asset):
        """Fetch last price via shared `MarketDataHub` when attached, else directly."""
        hub = getattr(self, "_market_data_hub", None)
        if hub is not None:
            return hub.get_last_price(self, asset)
        return self.get_last_price(asset)

    def _fetch_quote(self, asset):
        """Fetch a bid/ask quote; valuation never falls back to last trade."""
        return self.get_quote(asset)

    def _fetch_quotes(self, assets):
        """Use Tradier's native batch API for selected package legs."""
        reads = getattr(getattr(self, "broker", None), "_shared_broker_reads", None)
        if reads is None:
            return [self._fetch_quote(asset) for asset in assets]
        from lumibot.entities import Quote
        from lumibot.tools.helpers import create_options_symbol
        symbols = [create_options_symbol(asset.symbol, asset.expiration, asset.right, asset.strike)
                   if getattr(asset, "asset_type", None) == Asset.AssetType.OPTION
                   else asset.symbol for asset in assets]
        payload = reads.quotes(symbols)
        quotes = {row["symbol"]: row for row in payload["quotes"]["quote"]}
        return [Quote(asset=asset, bid=quotes[symbol].get("bid"), ask=quotes[symbol].get("ask"),
                      price=quotes[symbol].get("last"), timestamp=datetime.now(timezone.utc), raw_data=quotes[symbol])
                for asset, symbol in zip(assets, symbols)]

    def _fetch_chains(self, underlying):
        """Fetch option chains via shared `MarketDataHub` when attached, else directly."""
        hub = getattr(self, "_market_data_hub", None)
        if hub is not None:
            return hub.get_chains(self, underlying)
        return self.get_chains(underlying)

    def _fetch_expirations(self, chains):
        """Fetch option expirations via shared `MarketDataHub` when attached, else directly."""
        hub = getattr(self, "_market_data_hub", None)
        if hub is not None:
            return hub.get_expiration(self, self._underlying, chains=chains)
        return self.get_expiration(chains)

    def _fetch_strikes(self, chains, expiry=None):
        """Fetch option strikes via shared `MarketDataHub` when attached, else directly.

        When `expiry` is specified and `chains` contains expiration-keyed strike dictionaries,
        filters strikes to those actually present on that specific expiration date.
        """
        if chains and isinstance(chains, dict) and "Chains" in chains and expiry:
            exp_candidates = []
            if hasattr(expiry, "strftime"):
                exp_candidates.append(expiry.strftime("%Y-%m-%d"))
                exp_candidates.append(expiry)
            elif isinstance(expiry, str):
                exp_candidates.append(expiry[:10])
                exp_candidates.append(expiry)
            else:
                exp_candidates.append(str(expiry))
                exp_candidates.append(expiry)

            c = chains["Chains"]
            call_map = c.get("CALL") or c.get("call") or {}
            put_map = c.get("PUT") or c.get("put") or {}

            call_strikes = set()
            put_strikes = set()
            for cand in exp_candidates:
                if cand in call_map:
                    call_strikes = set(call_map[cand])
                    break
            for cand in exp_candidates:
                if cand in put_map:
                    put_strikes = set(put_map[cand])
                    break

            valid = call_strikes & put_strikes if (call_strikes and put_strikes) else (call_strikes | put_strikes)
            if valid:
                return sorted(float(s) for s in valid)

        hub = getattr(self, "_market_data_hub", None)
        if hub is not None:
            return hub.get_strikes(self, self._underlying, chains=chains)
        return self.get_strikes(self._underlying, chains=chains)

    def _publish_position_snapshot(self, position: PositionState, *, pnl: float, status: str) -> None:
        position.status = status
        states = list(getattr(self, "_position_states", {}).values())
        if len(states) > 1:
            self._publish_snapshot(pnl=sum(state.last_pnl for state in states), status="MULTIPLE_POSITIONS")
        else:
            self._publish_snapshot(pnl=pnl, status=status)

    def _publish_snapshot(self, pnl: float, status: str) -> None:
        """Publish this strategy variant's state to the shared `MarketDataHub`."""
        hub = getattr(self, "_market_data_hub", None)
        if hub is None:
            return
        name = getattr(self, "name", None) or self.parameters.get("strategy_name", "SPXIronCondor")
        hub.update_strategy_snapshot(
            name,
            {
                "strategy_name": name,
                "status": status,
                "pnl": float(pnl),
                "entry_credit": float(getattr(self, "_entry_credit", 0.0)),
                "short_delta": float(self.parameters.get("short_delta", 0.15)),
                "wing_width": float(self.parameters.get("wing_width", 15)),
                "profit_target": float(self.parameters.get("profit_target", 0.30)),
                "stop_loss_mult": float(self.parameters.get("stop_loss_mult", 1.3)),
                "entry_time": str(self.parameters.get("entry_time", "10:00")),
                "quantity": int(self.parameters.get("quantity", 1)),
                "mfe_pnl": float(getattr(self, "_mfe_pnl", 0.0)),
                "mae_pnl": float(getattr(self, "_mae_pnl", 0.0)),
                "positions": [{
                    "position_id": position.position_id,
                    "trade_id": position.current_trade_id,
                    "status": position.status,
                    "entry_credit_cash": position.entry_credit_cash,
                    "pnl": position.last_pnl if position.status == "OPEN" else None,
                    "quantity": max((abs(q) for _, q in position.owned_legs), default=0),
                    "profit_target": position.settings.get("profit_target"),
                    "stop_loss_mult": position.settings.get("stop_loss_mult"),
                } for position in getattr(self, "_position_states", {}).values()],
            },
        )

    def _is_entry_window(self, now: datetime) -> bool:
        """Allow entry within the configured execution window(s) through 15:31 (3:31 PM ET)."""
        current_time = now.time()

        # Hard cutoff: no new entries allowed past 15:31 (3:31 PM ET)
        if current_time > time(15, 31):
            return False

        if getattr(self, "pure_neural_mode", False):
            # Pure neural strategy trades dynamically during active market hours (09:45 - 15:31) once data is sufficient
            return time(9, 45) <= current_time <= time(15, 31)

        entry_times = getattr(self, "_entry_times", None) or [getattr(self, "_entry_time", time(9, 45))]
        window_minutes = int(
            self.parameters.get(
                "entry_window_minutes",
                4 if len(entry_times) > 1 else 30,
            )
        )

        for target_time in entry_times:
            total_end = target_time.hour * 60 + target_time.minute + window_minutes
            window_end = time((total_end // 60) % 24, total_end % 60, 59)
            effective_end = min(window_end, time(15, 31, 0)) if window_end > time(15, 31, 0) and target_time <= time(15, 31, 0) else window_end
            if target_time <= current_time <= effective_end:
                return True

        return False

    def _check_allocation(self) -> bool:
        """Ensure we don't exceed max allocation, the hard position cap, or single-trade limits."""
        portfolio_value = self._get_portfolio_value()
        wing = self.parameters["wing_width"]
        qty = self.parameters["quantity"]
        max_loss_per_contract = wing * 100  # Options have $100 multiplier
        new_trade_risk = max_loss_per_contract * qty

        max_risk_cap = float(self.parameters.get("max_position_risk", 5000.0))

        # 1. Single-trade risk limit (sliding scale: 30% down to 5% as max risk increases)
        override_pct = (os.getenv("IC_MAX_SINGLE_TRADE_PCT") or "").strip()
        try:
            parsed_override = float(override_pct) if override_pct else None
        except ValueError:
            parsed_override = None
        single_trade_pct = (
            parsed_override
            if parsed_override is not None and 0 < parsed_override <= 1
            else get_max_single_trade_pct(max_risk_cap)
        )
        max_single_trade_risk = max_risk_cap * single_trade_pct

        if new_trade_risk > max_single_trade_risk:
            # Attempt to auto-scale quantity down to allowed limit
            allowed_qty = int(max_single_trade_risk // max_loss_per_contract)
            if allowed_qty < 1:
                logger.warning(
                    "Single contract risk ($%.2f) exceeds single-trade limit ($%.2f = %.0f%% of $%.0f cap). Cannot trade.",
                    max_loss_per_contract, max_single_trade_risk, single_trade_pct * 100, max_risk_cap,
                )
                return False
            logger.info(
                "Scaling trade quantity from %d to %d to respect single-trade limit ($%.2f = %.0f%% of $%.0f cap).",
                qty, allowed_qty, max_single_trade_risk, single_trade_pct * 100, max_risk_cap,
            )
            self.parameters["quantity"] = allowed_qty
            qty = allowed_qty
            new_trade_risk = max_loss_per_contract * qty

        # 2. Hard cap across any open positions (e.g. $5k limit)
        try:
            if not self._reconcile_broker_state() or self._recovery_lock:
                return False
            existing_exposure = self._position_ledger.portfolio_defined_risk(self._strategy_id())
            if existing_exposure is None:
                logger.warning("Ledger risk is unknown; blocking new exposure.")
                return False
        except Exception as e:
            logger.warning("Could not calculate existing positions exposure: %s", e)
            return False

        total_exposure = existing_exposure + new_trade_risk

        if total_exposure > max_risk_cap:
            logger.warning(
                "Strategy %s open exposure ($%.2f) exceeds per-strategy $%.2f cap (new trade risk: $%.2f, existing: $%.2f). Skipping trade.",
                self._strategy_id(), total_exposure, max_risk_cap, new_trade_risk, existing_exposure,
            )
            return False

        # Optional / configurable global portfolio risk cap across all strategies combined
        global_cap_raw = self.parameters.get("max_portfolio_risk") or os.getenv("IC_MAX_PORTFOLIO_RISK")
        if global_cap_raw is not None and str(global_cap_raw).strip():
            try:
                global_cap = float(global_cap_raw)
                total_portfolio_exposure = self._position_ledger.portfolio_defined_risk()
                if total_portfolio_exposure is not None and (total_portfolio_exposure + new_trade_risk) > global_cap:
                    logger.warning(
                        "Total account portfolio exposure ($%.2f) exceeds $%.2f global cap (new trade risk: $%.2f, existing: $%.2f). Skipping trade.",
                        total_portfolio_exposure + new_trade_risk, global_cap, new_trade_risk, total_portfolio_exposure,
                    )
                    return False
            except ValueError:
                pass

        # 3. Portfolio percentage allocation check
        if portfolio_value is None or portfolio_value <= 0:
            logger.warning("Portfolio value unavailable; blocking new trade.")
            return False
        max_allowed_pct = portfolio_value * self.parameters["max_allocation"]
        if new_trade_risk > max_allowed_pct:
            logger.warning(
                "Risk $%.0f exceeds %.0f%% allocation ($%.0f). Skipping.",
                new_trade_risk, self.parameters["max_allocation"] * 100, max_allowed_pct,
            )
            return False

        return True

    def _find_expiration(self):
        """Find the best expiration date for the target DTE."""
        try:
            chains = self._fetch_chains(self._underlying)
            if not chains:
                return None

            expirations = self._fetch_expirations(chains)
            if not expirations:
                return None

            today = self.get_datetime().date()
            target_date = today + timedelta(days=self.parameters["dte"])

            def to_date(exp):
                if isinstance(exp, str):
                    return datetime.strptime(exp[:10], "%Y-%m-%d").date()
                if isinstance(exp, datetime):
                    return exp.date()
                return exp

            sorted_exps = sorted(expirations, key=to_date)

            # Find the nearest expiration on or after target_date
            for exp in sorted_exps:
                if to_date(exp) >= target_date:
                    return exp

            # Fallback: use the latest available expiration closest to target_date
            return sorted_exps[-1] if sorted_exps else None

        except Exception as e:
            logger.error("Error finding expiration: %s", e)
            return None

    def _find_short_strikes(self, underlying_price: float, expiry):
        """
        Find put and call strikes near the target delta.

        Approach: Get all available strikes, then pick the OTM strikes
        closest to the target delta distance from current price.

        For a rough delta approximation without Greeks:
          - delta ~0.15 corresponds to roughly 1 standard deviation
          - For SPX with ~15-20 IV, 0DTE: ~0.7% to 1.0% move per delta-0.15
          - Fallback: offset = underlying_price × short_delta × sqrt(DTE/252)

        When the broker provides Greeks via get_greeks(), we use actual delta.
        """
        try:
            chains = self._fetch_chains(self._underlying)
            if not chains:
                return None, None

            strikes = self._fetch_strikes(chains, expiry=expiry)
            if not strikes:
                return None, None

            otm_pct = self.parameters.get("otm_pct")
            otm_put = self.parameters.get("otm_put_pct", otm_pct)
            otm_call = self.parameters.get("otm_call_pct", otm_pct)
            wing = float(self.parameters["wing_width"])

            active_put_delta = getattr(self, "_active_put_delta", None)
            active_call_delta = getattr(self, "_active_call_delta", None)

            if otm_put is not None and otm_call is not None:
                put_strike, call_strike = select_short_strikes_otm_pct(
                    strikes,
                    underlying_price,
                    float(otm_put),
                    float(otm_call),
                    wing,
                )
            elif active_put_delta is not None and active_call_delta is not None:
                put_strike, call_strike = select_short_strikes_skew(
                    strikes,
                    underlying_price,
                    float(active_put_delta),
                    float(active_call_delta),
                    self.parameters.get("dte", 0),
                    wing,
                )
            else:
                put_strike, call_strike = select_short_strikes(
                    strikes,
                    underlying_price,
                    self.parameters.get("short_delta", 0.15),
                    self.parameters.get("dte", 0),
                    wing,
                )

            available = {float(strike) for strike in strikes}
            if (
                put_strike is None
                or call_strike is None
                or float(put_strike) - wing not in available
                or float(call_strike) + wing not in available
            ):
                logger.warning("Selected short strikes do not have both configured long wings.")
                return None, None

            if put_strike and call_strike:
                logger.info(
                    "Strike selection: underlying=%.2f | "
                    "put_strike=%.0f (%.1f%% OTM) | call_strike=%.0f (%.1f%% OTM)",
                    underlying_price,
                    put_strike, (underlying_price - put_strike) / underlying_price * 100,
                    call_strike, (call_strike - underlying_price) / underlying_price * 100,
                )

            return put_strike, call_strike

        except Exception as e:
            logger.error("Error finding short strikes: %s", e)
            return None, None

    def _make_option(self, strike: float, expiry, right: str) -> Asset:
        """Create an option Asset entity."""
        return Asset(
            symbol=self.parameters["symbol"],
            asset_type=Asset.AssetType.OPTION,
            expiration=expiry,
            strike=strike,
            right=right,
        )

    # ------------------------------------------------------------------ #
    # Lifecycle hooks
    # ------------------------------------------------------------------ #

    def before_market_closes(self):
        """
        Safety net: close any remaining positions before market close.
        Especially important for 0DTE to avoid settlement issues.
        Multi-day swing positions are carried overnight unless expiring today.
        """
        states = tuple(self._position_states.values()) if getattr(self, "_position_states", {}) else (self._runtime(),)
        for position in states:
            dte = int(position.settings.get("dte", self.parameters.get("dte", 0)))
            should_close = dte == 0 or self._is_expiring_today(position_id=position.position_id)
            if position.position_open and should_close:
                exit_reason = (
                    "EOD_CLOSE"
                    if position.pending_exit_reason in PNL_EXIT_REASONS
                    else (position.pending_exit_reason or "EOD_CLOSE")
                )
                self._close_all_options(
                    exit_reason=exit_reason, position_id=position.position_id)
        for follower in getattr(self, "_attached_strategies", ()):
            try:
                follower.before_market_closes()
            except Exception as err:
                logger.error("Error in attached strategy %s before_market_closes: %s", getattr(follower, "name", follower), err)

    def after_market_closes(self) -> None:
        """Cancel remaining DAY orders, reconcile and record the session."""
        self._reconcile_broker_state()
        self._record_trajectory("HOLD", session_end=True)
        for follower in getattr(self, "_attached_strategies", ()):
            try:
                follower.after_market_closes()
            except Exception as err:
                logger.error("Error in attached strategy %s after_market_closes: %s", getattr(follower, "name", follower), err)

    def on_abrupt_closing(self):
        """Handle unexpected shutdowns gracefully."""
        logger.warning("Abrupt shutdown detected. Retaining positions and existing exit intents for recovery.")
        for position in getattr(self, "_position_states", {}).values():
            self._persist_position(position)
        for follower in getattr(self, "_attached_strategies", ()):
            try:
                follower.on_abrupt_closing()
            except Exception as err:
                logger.error("Error in attached strategy %s on_abrupt_closing: %s", getattr(follower, "name", follower), err)
