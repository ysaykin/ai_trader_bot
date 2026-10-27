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
import uuid
from datetime import date, datetime, time, timedelta

from lumibot.strategies.strategy import Strategy
from lumibot.entities import Asset

from strategies.iron_condor_math import (
    credit_strategy_pnl,
    select_short_strikes,
    signed_basket_value,
)
from strategies.position_ledger import LedgerLeg, PositionLedger, asset_key

logger = logging.getLogger(__name__)


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
        "stop_loss_mult": 2.0,
        "entry_time": "10:00",
        "max_allocation": 0.03,
        "max_position_risk": 5000.0,
    }

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

        # Parse entry time
        h, m = map(int, p["entry_time"].split(":"))
        self._entry_time = time(h, m)

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

    @property
    def on_trading_iteration_count(self) -> int:
        """Expose iteration count for lifecycle telemetry and backward compatibility."""
        return int(getattr(self, "_iteration_count", 0))

    @on_trading_iteration_count.setter
    def on_trading_iteration_count(self, value: int) -> None:
        self._iteration_count = int(value)

    def _record_trajectory(self, action: str, *, session_end: bool = False) -> None:
        """Keep telemetry failures independent of position management."""
        collector = getattr(self, "_trajectory_logger", None)
        if collector is not None:
            try:
                collector.record(self, action, session_end=session_end)
            except Exception as error:
                logger.warning("Could not persist trajectory: %s", error)

    def _run_trading_iteration(self) -> None:
        """Execute the trading tick before publishing its trajectory observation."""
        now = self.get_datetime()

        if not self._reconcile_broker_state():
            return
        queued = getattr(self, "_queued_policy_action", None)
        if queued is not None:
            self._queued_policy_action = None
            self.apply_policy_action(queued[0], dynamic_profit_target=queued[1])

        # --- MANAGEMENT: Check existing position ---
        if self._position_open:
            self._manage_position()
            return

        if self._recovery_lock:
            logger.error("Unmanaged broker option positions detected; new entries are locked.")
            return

        # --- COOLDOWN CHECK: Prevent duplicate entry on transient network blips ---
        # If we just checked positions and got an empty result, wait before re-entering
        if hasattr(self, '_position_open_cooldown') and self._position_open_cooldown:
            import time
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
            legs = [
                (long_put, int(qty)),
                (short_put, -int(qty)),
                (short_call, -int(qty)),
                (long_call, int(qty)),
            ]
            package_price = self._package_price(legs, order_type="credit")
            if package_price is None or package_price <= 0:
                logger.warning("Complete bid/ask quotes are required before entry submission.")
                return
            orders = self._submit_package(
                legs, intent="open", order_type="credit", price=package_price
            )
            self._exit_requested = False
            self._mfe_pnl = 0.0
            self._mae_pnl = 0.0
            self._last_pnl = 0.0
            self._entry_orders.extend(orders)
            self._position_open = True
            logger.info("Iron Condor package submitted at a %.2f credit limit.", package_price)
            self._publish_snapshot(pnl=0.0, status="SUBMITTED")

        except Exception as e:
            logger.error("Failed to submit iron condor: %s", e)
            self._position_open = bool(self._position_ledger.open_baskets(self._strategy_id()))
            # Set cooldown to prevent duplicate entry on transient network blip
            self._position_open_cooldown = True
            self._position_open_cooldown_until = self.get_datetime().timestamp() + 5.0  # 5 second cooldown

    # ------------------------------------------------------------------ #
    # Position management
    # ------------------------------------------------------------------ #

    def _manage_position(self) -> None:
        """Check profit target and stop loss on the open iron condor."""
        try:
            orders = self.get_orders()
            positions = self.get_positions()
            if positions is None or orders is None:
                logger.warning("Position or order data unavailable; retaining position management.")
                return
            pending_orders = any(
                order.is_active()
                for order in [*orders, *self._entry_orders, *self._closing_orders]
            )
        except Exception as e:
            logger.warning("Could not reconcile open position; retaining management: %s", e)
            return

        option_positions = self._filter_owned_option_positions(positions)
        if not option_positions:
            if pending_orders:
                if self._exit_requested:
                    self._close_all_options()
                return
            if self._owned_legs and self._all_owned_legs_expired():
                self._position_ledger.expire_strategy_positions(
                    self._strategy_id(), self.get_datetime().date()
                )
                self._hydrate_from_ledger()
                if not self._position_ledger.owned_quantities(self._strategy_id()):
                    self._record_exit_log("EXPIRED", float(getattr(self, "_last_pnl", 0.0)))
                    self._reset_flat_state()
                    return
            if self._position_ledger.owned_quantities(self._strategy_id()):
                logger.warning("Owned ledger contracts are not visible at the broker; retaining state.")
                return
            if any(
                basket.state in {"submitted", "partial", "closing"}
                for basket in self._position_ledger.open_baskets(self._strategy_id())
            ):
                logger.warning("Package state remains active in ledger; retaining management.")
                return
            self._reset_flat_state()
            return

        if self._exit_requested:
            self._close_all_options()
            return

        if not self._entry_credit_cash or self._entry_credit_cash <= 0:
            # Fallback: recover credit baseline from submitted package limit price
            latest_open = self._position_ledger.latest_basket(self._strategy_id(), {"open", "add"})
            if latest_open is not None and latest_open.limit_price and latest_open.limit_price > 0:
                contract_qty = max((abs(int(q)) for _, q in self._owned_legs), default=1)
                self._entry_credit_cash = float(latest_open.limit_price) * 100.0 * contract_qty
                self._entry_credit = float(latest_open.limit_price)
                logger.info(
                    "Restored entry credit baseline from ledger limit price: $%.2f ($%.2f cash)",
                    self._entry_credit, self._entry_credit_cash,
                )
            else:
                logger.warning(
                    "Filled opening credit is unavailable; retaining contracts without P&L exits."
                )
                self._publish_snapshot(pnl=0.0, status="VALUATION_UNAVAILABLE")
                # CRITICAL: Do NOT set self._position_open = False or clear _owned_legs.
                # Open positions remain active and will be caught by EOD close.
                return

        marked_legs = []
        for asset, signed_quantity in self._owned_legs:
            try:
                midpoint = self._quote_midpoint(self._fetch_quote(asset))
            except Exception as error:
                logger.warning("Could not quote owned option %s: %s", asset, error)
                self._publish_snapshot(pnl=0.0, status="VALUATION_UNAVAILABLE")
                return
            if midpoint is None:
                logger.warning("Incomplete option quotes; retaining contracts without P&L exits.")
                self._publish_snapshot(pnl=0.0, status="VALUATION_UNAVAILABLE")
                return
            marked_legs.append((signed_quantity, midpoint))
        current_value = signed_basket_value(marked_legs)
        pnl = credit_strategy_pnl(self._entry_credit_cash, current_value)
        if pnl is None:
            logger.warning("Incomplete option quotes; retaining contracts without P&L exits.")
            self._publish_snapshot(pnl=0.0, status="VALUATION_UNAVAILABLE")
            return
        fee_rate = float(os.getenv("OPTION_FEE_PER_CONTRACT", "0.40"))
        qty = max((abs(int(quantity)) for _, quantity in self._owned_legs), default=1)
        roundtrip_fees = round(8 * qty * fee_rate, 2)
        gross_pnl = float(pnl)
        net_pnl = round(gross_pnl - roundtrip_fees, 2)

        self._last_pnl = net_pnl
        self._mfe_pnl = max(float(getattr(self, "_mfe_pnl", 0.0)), net_pnl)
        self._mae_pnl = min(float(getattr(self, "_mae_pnl", 0.0)), net_pnl)
        self._publish_snapshot(pnl=net_pnl, status="OPEN")

        pt_threshold = self._entry_credit_cash * self.parameters["profit_target"]
        sl_threshold = -self._entry_credit_cash * self.parameters["stop_loss_mult"]

        logger.info(
            "IC P&L: Gross $%.2f | Fees -$%.2f | Net $%.2f | PT threshold: $%.2f | SL threshold: $%.2f",
            gross_pnl, roundtrip_fees, net_pnl, pt_threshold, sl_threshold,
        )

        if net_pnl >= pt_threshold and net_pnl > 0:
            logger.info("🎯 NET PROFIT TARGET HIT — closing iron condor (Gross $%.2f, Fees -$%.2f, Net $%.2f)", gross_pnl, roundtrip_fees, net_pnl)
            self._close_all_options(exit_reason="PROFIT_TARGET", gross_pnl=gross_pnl, fees=roundtrip_fees, net_pnl=net_pnl)
        elif net_pnl <= sl_threshold:
            logger.warning("🛑 STOP LOSS HIT — closing iron condor (Gross $%.2f, Fees -$%.2f, Net $%.2f)", gross_pnl, roundtrip_fees, net_pnl)
            self._close_all_options(exit_reason="STOP_LOSS", gross_pnl=gross_pnl, fees=roundtrip_fees, net_pnl=net_pnl)

    def _record_exit_log(
        self,
        reason: str,
        pnl: float,
        fees: float | None = None,
        gross_pnl: float | None = None,
        net_pnl: float | None = None,
    ) -> None:
        """Persist trade exit event to logs/trades.jsonl."""
        try:
            trade_id = getattr(self, "_current_trade_id", None)
            if trade_id:
                from trade_logger import record_trade_exit
                fee_rate = float(os.getenv("OPTION_FEE_PER_CONTRACT", "0.40"))
                qty = self.parameters.get("quantity", 1)
                calc_fees = float(fees if fees is not None else round(8 * qty * fee_rate, 2))
                calc_gross = float(gross_pnl if gross_pnl is not None else pnl)
                calc_net = float(net_pnl if net_pnl is not None else (calc_gross - calc_fees))
                mfe = max(float(getattr(self, "_mfe_pnl", 0.0)), calc_net)
                mae = min(float(getattr(self, "_mae_pnl", 0.0)), calc_net)
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
                self._current_trade_id = None
        except Exception as e:
            logger.warning("Could not persist trade exit: %s", e)

    def _can_add_contracts(self, add_qty: int = 1) -> bool:
        """Verify adding `add_qty` contracts respects RSK-01 ($5k cap), RSK-02 (single-trade tier), and allocation."""
        if add_qty < 1:
            return False
        wing = float(self.parameters.get("wing_width", 15.0))
        current_qty = max(
            (abs(int(q)) for _, q in getattr(self, "_owned_legs", ())),
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
            existing_exposure = self._position_ledger.portfolio_defined_risk()
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
        if dynamic_profit_target is not None:
            clamped_pt = max(0.15, min(0.80, float(dynamic_profit_target)))
            self.parameters["profit_target"] = clamped_pt
            if normalized in ("HOLD", "ADJUST_PROFIT_TARGET"):
                self._last_action = "ADJUST_PROFIT_TARGET"
                return "ADJUST_PROFIT_TARGET"

        if not self._position_open or self._exit_requested:
            self._last_action = "HOLD"
            return "HOLD"

        if normalized == "FLATTEN_NOW":
            self._close_all_options(exit_reason="NEURAL_EARLY_EXIT")
            self._last_action = "FLATTEN_NOW"
            return "FLATTEN_NOW"

        owned_legs = list(getattr(self, "_owned_legs", []))
        if len(owned_legs) != 4:
            self._last_action = "HOLD"
            return "HOLD"
        if any(
            basket.state in {"submitted", "partial", "closing"}
            for basket in self._position_ledger.open_baskets(self._strategy_id())
        ):
            self._last_action = "HOLD"
            return "HOLD"

        if normalized == "ADD_POSITION":
            add_qty = 1
            if not self._can_add_contracts(add_qty):
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
                    legs, intent="add", order_type="credit", price=price
                )
                self._entry_orders.extend(orders)
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
                    legs, intent="trim", order_type="debit", price=price
                )
                self._closing_orders.extend(orders)
                self._last_action = "TRIM_POSITION"
                return "TRIM_POSITION"
            except Exception as e:
                logger.error("Failed to execute TRIM_POSITION: %s", e)
                self._last_action = "HOLD"
                return "HOLD"

        self._last_action = "HOLD"
        return "HOLD"

    def _close_all_options(
        self,
        exit_reason: str = "MANUAL",
        gross_pnl: float | None = None,
        fees: float | None = None,
        net_pnl: float | None = None,
    ) -> None:
        """Close all open option positions owned by this strategy variant."""
        self._exit_requested = True
        self._position_open = True
        self._pending_exit_reason = exit_reason
        self._pending_gross_pnl = gross_pnl
        self._pending_fees = fees
        self._pending_net_pnl = net_pnl
        try:
            # Refresh order state and stop entry fills before unwinding their legs.
            broker_orders = self.get_orders() or []
            pending_entries = [order for order in self._entry_orders if order.is_active()]
            if pending_entries:
                for order in pending_entries:
                    self.cancel_order(order)
                return
            active_close = next(
                (
                    basket
                    for basket in self._position_ledger.open_baskets(self._strategy_id())
                    if basket.intent == "close" and basket.state in {"submitted", "partial", "closing"}
                ),
                None,
            )
            if active_close is not None:
                parent = next(
                    (
                        order
                        for order in [*broker_orders, *self._closing_orders]
                        if str(getattr(order, "identifier", None) or getattr(order, "id", ""))
                        == str(active_close.broker_parent_id or "")
                    ),
                    None,
                )
                if parent is not None and parent.is_active():
                    created = datetime.fromisoformat(str(active_close.created_at))
                    now = self.get_datetime()
                    if now.tzinfo is None:
                        now = now.replace(tzinfo=created.tzinfo)
                    if (now - created).total_seconds() >= self._close_reprice_seconds:
                        self.cancel_order(parent)
                    return
                # Cancellation/rejection must be observed by reconciliation before replacement.
                return
            self._hydrate_from_ledger()
            if not self._owned_legs:
                return
            previous = self._position_ledger.latest_basket(
                self._strategy_id(), {"close"}
            )
            reprice_count = 0
            replacement_of = None
            if previous is not None:
                if previous.state not in {"canceled", "expired", "failed", "rejected"}:
                    return
                reprice_count = previous.reprice_count + 1
                replacement_of = previous.package_id
            legs = [(asset, -signed_quantity) for asset, signed_quantity in self._owned_legs]
            price = self._package_price(legs, order_type="debit")
            if price is None or price <= 0:
                logger.warning("Complete quotes are required before closing the package.")
                return
            order_type = "debit"
            if reprice_count > self._close_max_reprices:
                if not self._close_allow_market:
                    logger.warning("Close reprice limit reached; retaining contracts for manual review.")
                    return
                order_type = "market"
            else:
                price += reprice_count * self._close_price_step
            orders = self._submit_package(
                legs,
                intent="close",
                order_type=order_type,
                price=price,
                replacement_of=replacement_of,
                reprice_count=reprice_count,
            )
            self._closing_orders.extend(orders)
            logger.info("Closing iron-condor package at a %.2f debit limit.", price)
        except Exception as e:
            logger.error("Could not close all option positions; retaining management: %s", e)

    # ------------------------------------------------------------------ #
    # Helpers
    # ------------------------------------------------------------------ #

    def _strategy_id(self) -> str:
        return str(
            getattr(self, "name", None)
            or self.parameters.get("strategy_name")
            or type(self).__name__
        )

    def _hydrate_from_ledger(self) -> None:
        quantities = self._position_ledger.owned_quantities(self._strategy_id())
        owned = []
        for key, quantity in quantities.items():
            asset = self._asset_registry.get(key)
            if asset is None:
                asset = self._asset_from_key(key)
                self._asset_registry[key] = asset
            owned.append((asset, quantity))
        self._owned_legs = sorted(
            owned,
            key=lambda item: (
                str(getattr(item[0], "right", "")),
                float(getattr(item[0], "strike", 0.0)),
            ),
        )
        baskets = self._position_ledger.open_baskets(self._strategy_id())
        opening_cash = self._position_ledger.active_opening_cash(self._strategy_id())
        self._entry_credit_cash = opening_cash if opening_cash > 0 else 0.0
        if self._owned_legs:
            self.parameters["quantity"] = max(abs(quantity) for _, quantity in self._owned_legs)
        quantity = max((abs(quantity) for _, quantity in self._owned_legs), default=0)
        self._entry_credit = (
            self._entry_credit_cash / (100.0 * quantity)
            if self._entry_credit_cash > 0 and quantity > 0
            else 0.0
        )
        self._current_trade_id = self._position_ledger.active_trade_id(self._strategy_id())
        latest_close = self._position_ledger.latest_basket(self._strategy_id(), {"close"})
        latest_open = self._position_ledger.latest_basket(
            self._strategy_id(), {"open", "add"}
        )
        close_is_current = bool(
            latest_close is not None
            and latest_open is not None
            and str(latest_close.created_at) >= str(latest_open.created_at)
        )
        if self._owned_legs and close_is_current:
            self._exit_requested = True
            self._pending_exit_reason = self._pending_exit_reason or "RECOVERY"
        self._position_open = bool(self._owned_legs) or any(
            basket.state in {"submitted", "partial", "closing"} for basket in baskets
        )

    def _all_owned_legs_expired(self) -> bool:
        if not self._owned_legs:
            return False
        today = self.get_datetime().date()
        for asset, _ in self._owned_legs:
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

    def _is_expiring_today(self) -> bool:
        """Check if any owned leg expires today or earlier."""
        if not self._owned_legs:
            return int(self.parameters.get("dte", 0)) == 0
        today = self.get_datetime().date()
        for asset, _ in self._owned_legs:
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

    def _asset_from_key(self, key: str) -> Asset:
        decoded = json.loads(key)
        return Asset(
            symbol=decoded["symbol"],
            asset_type=Asset.AssetType.OPTION,
            expiration=decoded["expiration"],
            strike=float(decoded["strike"]),
            right=decoded["right"],
        )

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
        self._hydrate_from_ledger()
        broker_options = [
            position
            for position in positions
            if getattr(getattr(position, "asset", None), "asset_type", None)
            == Asset.AssetType.OPTION
            and int(getattr(position, "quantity", 0)) != 0
        ]
        if (
            self._owned_legs
            and not any(
                order.is_active()
                for order in [*orders, *self._entry_orders, *self._closing_orders]
            )
            and not self._filter_owned_option_positions(positions)
            and self._all_owned_legs_expired()
        ):
            self._position_ledger.expire_strategy_positions(
                self._strategy_id(), self.get_datetime().date()
            )
            self._hydrate_from_ledger()
            if not self._position_open:
                self._record_exit_log("EXPIRED", float(getattr(self, "_last_pnl", 0.0)))
                self._reset_flat_state()
        ledger_totals = self._position_ledger.all_owned_quantities()
        inflight = any(
            basket.state in {"submitted", "partial", "closing"}
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

    def _reconcile_inflight_orders(self, orders: list, positions: list | None = None) -> bool:
        """Reconcile active packages by broker IDs/tags and flag ambiguous rollover."""
        broker_orders: list[object] = []

        def collect(order) -> None:
            broker_orders.append(order)
            for child in getattr(order, "child_orders", None) or ():
                collect(child)

        for order in orders:
            collect(order)
        order_lookup: dict[str, object] = {}
        tag_lookup: dict[str, object] = {}
        for order in broker_orders:
            identifier = getattr(order, "identifier", None) or getattr(order, "id", None)
            if identifier:
                order_lookup[str(identifier)] = order
            custom = getattr(order, "custom_params", None)
            tag = getattr(order, "tag", None)
            if not tag and isinstance(custom, dict):
                tag = custom.get("tag")
            if tag:
                tag_lookup[str(tag)] = order
        fetch_order = getattr(self, "get_order", None)
        terminal_map = {
            "canceled": "canceled",
            "cancelled": "canceled",
            "rejected": "rejected",
            "expired": "expired",
            "error": "failed",
            "failed": "failed",
        }
        ambiguous = False
        today = self.get_datetime().date()
        stale_ids = {
            basket.package_id
            for basket in self._position_ledger.stale_day_baskets(self._strategy_id(), today)
        }
        for basket in self._position_ledger.open_baskets(self._strategy_id()):
            if basket.state not in {"submitted", "partial", "closing"}:
                continue
            parent = order_lookup.get(str(basket.broker_parent_id or ""))
            if (
                parent is None
                and basket.broker_parent_id
                and callable(fetch_order)
            ):
                try:
                    parent = fetch_order(str(basket.broker_parent_id))
                except Exception:
                    parent = None
            if parent is None and basket.broker_tag:
                parent = tag_lookup.get(basket.broker_tag)
                if parent is not None:
                    try:
                        self._position_ledger.bind_broker_submission(basket.package_id, parent)
                        basket = self._position_ledger.get_basket(basket.package_id)
                    except (KeyError, ValueError):
                        ambiguous = True
            if parent is not None and getattr(parent, "child_orders", None):
                self._position_ledger.bind_broker_children(
                    basket.package_id, parent.child_orders
                )
                basket = self._position_ledger.get_basket(basket.package_id)
            leg_statuses: list[str] = []
            for leg in basket.legs:
                broker_id = leg.get("broker_order_id")
                broker_order = order_lookup.get(str(broker_id or ""))
                if broker_order is None and broker_id and callable(fetch_order):
                    try:
                        broker_order = fetch_order(str(broker_id))
                    except Exception:
                        broker_order = None
                if broker_order is None:
                    continue
                status = str(getattr(broker_order, "status", "") or "").strip().lower()
                if status:
                    leg_statuses.append(status)
                raw_filled = getattr(broker_order, "filled_quantity", None)
                raw_price = getattr(broker_order, "avg_fill_price", None)
                if raw_filled is not None and raw_price is not None:
                    try:
                        broker_filled = abs(int(raw_filled))
                        fill_price = float(raw_price)
                    except (TypeError, ValueError):
                        broker_filled = 0
                        fill_price = -1.0
                    already_filled = abs(int(leg["filled_signed_quantity"]))
                    if broker_filled > already_filled and math.isfinite(fill_price) and fill_price >= 0:
                        sign = 1 if int(leg["requested_signed_quantity"]) > 0 else -1
                        opening_cash_before = self._position_ledger.active_opening_cash(
                            self._strategy_id()
                        )
                        owned_before = self._position_ledger.owned_quantities(
                            self._strategy_id()
                        )
                        quantity_before = max(
                            (abs(value) for value in owned_before.values()), default=0
                        )
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
                            self._record_filled_exit(
                                filled_basket, opening_cash_before, quantity_before
                            )
            refreshed = self._position_ledger.get_basket(basket.package_id)
            if (
                refreshed.state == "submitted"
                and len(leg_statuses) == len(refreshed.legs)
                and all(status in terminal_map for status in leg_statuses)
                and all(int(leg["filled_signed_quantity"]) == 0 for leg in refreshed.legs)
            ):
                self._position_ledger.record_terminal_status(
                    refreshed.package_id, terminal_map[leg_statuses[0]]
                )
                continue
            parent_status = str(getattr(parent, "status", "") or "").lower()
            if (
                refreshed.state == "submitted"
                and parent_status in terminal_map
                and all(int(leg["filled_signed_quantity"]) == 0 for leg in refreshed.legs)
            ):
                self._position_ledger.record_terminal_status(
                    refreshed.package_id, terminal_map[parent_status]
                )
                continue
            if refreshed.package_id not in stale_ids:
                continue
            any_fill = any(int(leg["filled_signed_quantity"]) for leg in refreshed.legs)
            if any_fill:
                ambiguous = True
                continue
            basket_assets = {str(leg["asset_key"]) for leg in refreshed.legs}
            broker_has_assets = any(
                asset_key(position.asset) in basket_assets
                and int(getattr(position, "quantity", 0)) != 0
                for position in (positions or [])
            )
            if refreshed.intent in {"open", "add"} and broker_has_assets:
                ambiguous = True
            elif refreshed.intent in {"open", "add", "close", "trim"}:
                self._position_ledger.record_terminal_status(
                    refreshed.package_id, "expired"
                )
        return ambiguous

    def _supports_atomic_multileg(self) -> bool:
        broker = getattr(self, "broker", None)
        name = str(getattr(broker, "name", type(broker).__name__)).lower()
        return "tradier" in name

    def _submit_package(
        self,
        legs: list[tuple[Asset, int]],
        *,
        intent: str,
        order_type: str,
        price: float,
        replacement_of: str | None = None,
        reprice_count: int = 0,
    ) -> list:
        if not self._supports_atomic_multileg():
            raise RuntimeError("broker does not have a verified atomic multi-leg adapter")
        if len(legs) != 4 or len({abs(quantity) for _, quantity in legs}) != 1:
            raise ValueError("an atomic iron-condor package requires four balanced legs")
        package_id = uuid.uuid4().hex
        broker_tag = f"IC_{package_id[:20]}"
        owner = self._strategy_id()
        opening = intent in {"open", "add"}
        orders = []
        ledger_legs = []
        for index, (asset, signed_quantity) in enumerate(legs, start=1):
            if opening:
                side = "buy_to_open" if signed_quantity > 0 else "sell_to_open"
            else:
                side = "buy_to_close" if signed_quantity > 0 else "sell_to_close"
            order = self.create_order(asset, abs(signed_quantity), side)
            order_id = str(
                getattr(order, "identifier", None)
                or getattr(order, "id", None)
                or f"{package_id}:{index}"
            )
            for attribute, value in (
                ("_ledger_order_id", order_id),
                ("_package_id", package_id),
                ("_strategy_id", owner),
                ("tag", broker_tag),
            ):
                setattr(order, attribute, value)
            custom_params = getattr(order, "custom_params", None)
            if isinstance(custom_params, dict):
                custom_params["tag"] = broker_tag
            self._asset_registry[asset_key(asset)] = asset
            orders.append(order)
            ledger_legs.append(LedgerLeg(order_id, asset_key(asset), signed_quantity))
        self._position_ledger.record_submission(
            package_id,
            owner,
            intent,
            ledger_legs,
            duration="day",
            limit_price=round(float(price), 2),
            broker_tag=broker_tag,
            replacement_of=replacement_of,
            reprice_count=reprice_count,
        )
        submit_kwargs = {
            "is_multileg": True,
            "order_type": order_type,
            "duration": "day",
        }
        if order_type != "market":
            submit_kwargs["price"] = round(float(price), 2)
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
                ("tag", broker_tag),
            ):
                setattr(parent, attribute, value)
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

    def _record_filled_order(self, order, price: float, quantity: int) -> None:
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
        package_id = str(resolved["package_id"])
        opening_cash_before = self._position_ledger.active_opening_cash(self._strategy_id())
        owned_before = self._position_ledger.owned_quantities(self._strategy_id())
        quantity_before = max((abs(value) for value in owned_before.values()), default=0)
        sign = 1 if "buy" in side else -1
        basket = self._position_ledger.record_fill(
            str(resolved["order_id"]),
            sign * int(quantity),
            float(price),
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
            self._record_filled_exit(basket, opening_cash_before, quantity_before)
        self._hydrate_from_ledger()

    def _record_filled_exit(
        self, basket, opening_cash_before: float, quantity_before: int
    ) -> None:
        trade_id = getattr(self, "_current_trade_id", None)
        if not trade_id:
            return
        try:
            from trade_logger import record_trade_adjustment, record_trade_exit
            closing_cash = float(basket.closing_cash_flow or 0.0)
            fee_rate = float(os.getenv("OPTION_FEE_PER_CONTRACT", "0.40"))
            closed_qty = max(
                (abs(int(leg["filled_signed_quantity"])) for leg in basket.legs),
                default=0,
            )
            close_fees = round(4 * closed_qty * fee_rate, 2)
            realized_opening = (
                opening_cash_before * min(closed_qty / quantity_before, 1.0)
                if quantity_before > 0
                else 0.0
            )
            actual_gross = realized_opening + closing_cash
            actual_net = round(actual_gross - close_fees, 2)
            mfe = max(float(getattr(self, "_mfe_pnl", 0.0)), actual_net)
            mae = min(float(getattr(self, "_mae_pnl", 0.0)), actual_net)
            reason = getattr(self, "_pending_exit_reason", "PROFIT_TARGET")

            remaining = self._position_ledger.owned_quantities(self._strategy_id())
            remaining_qty = max((abs(value) for value in remaining.values()), default=0)
            if basket.intent == "trim" and remaining_qty > 0:
                record_trade_adjustment(
                    trade_id=trade_id,
                    strategy_name=self._strategy_id(),
                    opening_credit_cash=realized_opening,
                    closing_cash=closing_cash,
                    gross_pnl=actual_gross,
                    fees=close_fees,
                    net_pnl=actual_net,
                    remaining_quantity=remaining_qty,
                )
                logger.info(
                    "Recorded confirmed trim for %s: Net $%.2f, %d contracts remain.",
                    self._strategy_id(),
                    actual_net,
                    remaining_qty,
                )
                return

            record_trade_exit(
                trade_id=trade_id,
                strategy_name=self._strategy_id(),
                exit_reason=reason,
                pnl=actual_net,
                fees=close_fees,
                gross_pnl=actual_gross,
                net_pnl=actual_net,
                opening_credit_cash=realized_opening,
                closing_cash=closing_cash,
                mfe_pnl=mfe,
                mae_pnl=mae,
            )
            self._current_trade_id = None
            self._reset_flat_state()
            logger.info(
                "Recorded confirmed fill exit for %s: Gross $%.2f, Fees -$%.2f, Net Realized $%.2f",
                self._strategy_id(), actual_gross, close_fees, actual_net,
            )
        except Exception as error:
            logger.warning("Could not persist filled trade exit: %s", error)

    def _record_filled_entry(self, basket) -> None:
        if getattr(self, "_current_trade_id", None):
            return
        try:
            from trade_logger import record_trade_entry

            strikes = {
                f"{json.loads(leg['asset_key'])['right']}_{index}": json.loads(leg["asset_key"])["strike"]
                for index, leg in enumerate(basket.legs, start=1)
            }
            opening_cash = float(basket.opening_cash_flow or 0.0)
            quantity = max(abs(int(leg["filled_signed_quantity"])) for leg in basket.legs)
            self._current_trade_id = record_trade_entry(
                strategy_name=self._strategy_id(),
                symbol=self.parameters["symbol"],
                expiry=json.loads(basket.legs[0]["asset_key"])["expiration"],
                strikes=strikes,
                quantity=quantity,
                credit=opening_cash / (100.0 * quantity),
                opening_credit_cash=opening_cash,
                entry_fees=round(
                    4 * quantity * float(os.getenv("OPTION_FEE_PER_CONTRACT", "0.40")),
                    2,
                ),
                max_risk=self.parameters["wing_width"] * 100 * self.parameters["quantity"],
            )
            self._position_ledger.set_trade_id(
                basket.package_id, self._current_trade_id
            )
        except Exception as error:
            logger.warning("Could not persist filled trade entry: %s", error)

    def _record_filled_addition(self, basket) -> None:
        trade_id = getattr(self, "_current_trade_id", None)
        if not trade_id:
            trade_id = self._position_ledger.active_trade_id(self._strategy_id())
        if not trade_id:
            logger.warning("Could not journal filled addition without an active trade ID.")
            return
        try:
            from trade_logger import record_trade_addition

            quantity = max(
                abs(int(leg["filled_signed_quantity"])) for leg in basket.legs
            )
            record_trade_addition(
                trade_id=trade_id,
                strategy_name=self._strategy_id(),
                opening_credit_cash=float(basket.opening_cash_flow or 0.0),
                fees=round(
                    4 * quantity * float(os.getenv("OPTION_FEE_PER_CONTRACT", "0.40")),
                    2,
                ),
                added_quantity=quantity,
            )
            self._position_ledger.set_trade_id(basket.package_id, trade_id)
        except Exception as error:
            logger.warning("Could not persist filled trade addition: %s", error)

    def _package_price(
        self,
        legs: list[tuple[Asset, int]],
        *,
        order_type: str = "credit",
    ) -> float | None:
        quantities = {abs(quantity) for _, quantity in legs}
        if len(quantities) != 1:
            return None
        quotes = [self._fetch_quote(asset) for asset, _ in legs]
        marks = [
            (signed_quantity, self._quote_midpoint(quote))
            for (_, signed_quantity), quote in zip(legs, quotes)
        ]
        value = signed_basket_value(marks)
        if value is None:
            return None
        quantity = quantities.pop()
        if quantity <= 0:
            return None
        unit_value = value / (100.0 * quantity)
        if order_type == "credit":
            return -unit_value if unit_value < 0 else None
        if order_type == "debit":
            if unit_value > 0:
                return max(unit_value, 0.01)
            if unit_value == 0:
                natural = 0.0
                for (_, signed_quantity), quote in zip(legs, quotes):
                    bounds = self._quote_bounds(quote)
                    if bounds is None:
                        return None
                    bid_value, ask_value = bounds
                    natural += (ask_value if signed_quantity > 0 else -bid_value) * abs(signed_quantity)
                natural_unit = natural / quantity
                return max(natural_unit / 2.0, 0.01) if natural_unit > 0 else None
            return None
        return abs(unit_value)

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
        if bid_value < 0 or ask_value < bid_value:
            return None
        return bid_value, ask_value

    @classmethod
    def _quote_midpoint(cls, quote) -> float | None:
        bounds = cls._quote_bounds(quote)
        if bounds is None:
            return None
        return (bounds[0] + bounds[1]) / 2.0

    def _reset_flat_state(self) -> None:
        logger.info("Position closed or reconciled flat. Resetting strategy state.")
        self._position_open = False
        self._entry_credit = 0.0
        self._entry_credit_cash = 0.0
        self._entry_orders = []
        self._closing_orders = []
        self._owned_legs = []
        self._exit_requested = False
        self._pending_exit_reason = None
        self._pending_gross_pnl = None
        self._pending_fees = None
        self._pending_net_pnl = None
        self._mfe_pnl = 0.0
        self._mae_pnl = 0.0
        self._last_pnl = 0.0
        self._publish_snapshot(pnl=0.0, status="FLAT")

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

    def _filter_owned_option_positions(self, positions) -> list:
        """Return only option positions belonging to this strategy variant (or all if single-strategy fallback)."""
        option_positions = [
            p for p in positions
            if hasattr(p.asset, 'asset_type')
            and p.asset.asset_type == Asset.AssetType.OPTION
            and p.quantity != 0
        ]
        owned_legs = getattr(self, "_owned_legs", None)
        if not owned_legs:
            return []
        return [
            p for p in option_positions
            if any(self._asset_matches(p.asset, leg_asset) for leg_asset, _ in owned_legs)
        ]

    def _owned_quantity_for_position(self, pos) -> int:
        """Return signed contract quantity owned by this strategy for `pos.asset`."""
        owned_legs = getattr(self, "_owned_legs", None)
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

    def _fetch_strikes(self, chains):
        """Fetch option strikes via shared `MarketDataHub` when attached, else directly."""
        hub = getattr(self, "_market_data_hub", None)
        if hub is not None:
            return hub.get_strikes(self, self._underlying, chains=chains)
        return self.get_strikes(self._underlying, chains=chains)

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
                "stop_loss_mult": float(self.parameters.get("stop_loss_mult", 2.0)),
                "entry_time": str(self.parameters.get("entry_time", "10:00")),
                "quantity": int(self.parameters.get("quantity", 1)),
                "mfe_pnl": float(getattr(self, "_mfe_pnl", 0.0)),
                "mae_pnl": float(getattr(self, "_mae_pnl", 0.0)),
            },
        )

    def _is_entry_window(self, now: datetime) -> bool:
        """Only allow entry within a 30-minute window starting at entry_time."""
        current_time = now.time()
        window_end = time(
            self._entry_time.hour,
            self._entry_time.minute + 30 if self._entry_time.minute + 30 < 60
            else self._entry_time.minute + 30 - 60,
        )
        # Handle hour rollover
        if self._entry_time.minute + 30 >= 60:
            window_end = time(self._entry_time.hour + 1, self._entry_time.minute + 30 - 60)

        return self._entry_time <= current_time <= window_end

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
            existing_exposure = self._position_ledger.portfolio_defined_risk()
            if existing_exposure is None:
                logger.warning("Ledger risk is unknown; blocking new exposure.")
                return False
        except Exception as e:
            logger.warning("Could not calculate existing positions exposure: %s", e)
            return False

        total_exposure = existing_exposure + new_trade_risk

        if total_exposure > max_risk_cap:
            logger.warning(
                "Total open exposure ($%.2f) exceeds $%.2f cap (new trade risk: $%.2f, existing: $%.2f). Skipping trade.",
                total_exposure, max_risk_cap, new_trade_risk, existing_exposure,
            )
            return False

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

            strikes = self._fetch_strikes(chains)
            if not strikes:
                return None, None

            put_strike, call_strike = select_short_strikes(
                strikes,
                underlying_price,
                self.parameters["short_delta"],
                self.parameters["dte"],
                self.parameters["wing_width"],
            )

            available = {float(strike) for strike in strikes}
            wing = float(self.parameters["wing_width"])
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
        dte = int(self.parameters.get("dte", 0))
        should_close = (dte == 0) or self._is_expiring_today()
        if self._position_open and should_close:
            logger.info("Market closing — closing option positions expiring today (dte=%s)", dte)
            self._close_all_options()
        elif self._position_open:
            logger.info("Market closing — holding swing position overnight (dte=%s)", dte)
        for follower in getattr(self, "_attached_strategies", ()):
            try:
                follower.before_market_closes()
            except Exception as err:
                logger.error("Error in attached strategy %s before_market_closes: %s", getattr(follower, "name", follower), err)

    def after_market_closes(self) -> None:
        """Record terminal basket marks and relabel the completed session."""
        self._record_trajectory("HOLD", session_end=True)
        for follower in getattr(self, "_attached_strategies", ()):
            try:
                follower.after_market_closes()
            except Exception as err:
                logger.error("Error in attached strategy %s after_market_closes: %s", getattr(follower, "name", follower), err)

    def on_abrupt_closing(self):
        """Handle unexpected shutdowns gracefully."""
        logger.warning("Abrupt shutdown detected. Attempting to close positions.")
        if self._position_open:
            self._close_all_options()
        for follower in getattr(self, "_attached_strategies", ()):
            try:
                follower.on_abrupt_closing()
            except Exception as err:
                logger.error("Error in attached strategy %s on_abrupt_closing: %s", getattr(follower, "name", follower), err)
