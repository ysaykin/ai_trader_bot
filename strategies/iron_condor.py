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
from datetime import datetime, time, timedelta

from lumibot.strategies.strategy import Strategy
from lumibot.entities import Asset

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

        # Parse entry time
        h, m = map(int, p["entry_time"].split(":"))
        self._entry_time = time(h, m)

        # Check frequently so we don't miss the entry window
        try:
            self.sleeptime = "5M"  # every 5 minutes
        except AttributeError:
            self._sleeptime = "5M"

        # Trade state
        self._position_open = False
        self._entry_credit = 0.0
        self._entry_orders = []
        self._closing_orders = []
        self._owned_legs = []
        self._exit_requested = False
        self._retry_count = 0
        self._iteration_count = 0
        self._mfe_pnl = 0.0
        self._mae_pnl = 0.0
        self._last_pnl = 0.0
        self._last_action = None

        # Define the underlying asset
        # Note: Tradier quotes index tickers (SPX, XSP) without the 'I:' index prefix.
        broker_name = getattr(getattr(self, "broker", None), "name", "").lower()
        asset_type = Asset.AssetType.STOCK if "tradier" in broker_name else Asset.AssetType.INDEX
        self._underlying = Asset(
            symbol=p["symbol"],
            asset_type=asset_type,
        )

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
        self._iteration_count += 1
        self._last_action = None
        was_open = self._position_open
        try:
            self._run_trading_iteration()
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

        # --- MANAGEMENT: Check existing position ---
        if self._position_open:
            self._manage_position()
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
            orders = [
                self.create_order(long_put, qty, "buy"),
                self.create_order(short_put, qty, "sell"),
                self.create_order(short_call, qty, "sell"),
                self.create_order(long_call, qty, "buy"),
            ]

            self._entry_orders = []
            self._closing_orders = []
            self._owned_legs = [
                (long_put, int(qty)),
                (short_put, -int(qty)),
                (short_call, -int(qty)),
                (long_call, int(qty)),
            ]
            self._retry_count = 0
            self._exit_requested = False
            self._mfe_pnl = 0.0
            self._mae_pnl = 0.0
            self._last_pnl = 0.0
            for order in orders:
                # Submission can raise after the broker has accepted the leg.
                self._position_open = True
                self._entry_orders.append(order)
                self.submit_order(order)

            # Estimate entry credit from mid-prices
            self._entry_credit = self._estimate_credit(
                long_put, short_put, short_call, long_call, quantity=qty
            )
            logger.info("Iron Condor submitted. Estimated credit: $%.2f", self._entry_credit)
            self._publish_snapshot(pnl=0.0, status="OPEN")

            # Record trade entry event to logs/trades.jsonl
            try:
                from trade_logger import record_trade_entry
                self._current_trade_id = record_trade_entry(
                    strategy_name=getattr(self, "name", "SPXIronCondor"),
                    symbol=self.parameters["symbol"],
                    expiry=str(expiry),
                    strikes={
                        "long_put": long_put_strike,
                        "short_put": short_put_strike,
                        "short_call": short_call_strike,
                        "long_call": long_call_strike,
                    },
                    quantity=qty,
                    credit=self._entry_credit,
                    max_risk=self.parameters["wing_width"] * 100 * qty,
                )
            except Exception as log_err:
                logger.warning("Could not persist trade entry: %s", log_err)

        except Exception as e:
            logger.error("Failed to submit iron condor: %s", e)
            if self._position_open:
                self._exit_requested = True
            # Set cooldown to prevent duplicate entry on transient network blip
            self._position_open_cooldown = True
            self._position_open_cooldown_until = self.get_datetime().timestamp() + 5.0  # 5 second cooldown

    # ------------------------------------------------------------------ #
    # Position management
    # ------------------------------------------------------------------ #

    def _manage_position(self) -> None:
        """Check profit target and stop loss on the open iron condor."""
        # Confirm both positions and pending orders before releasing trade state.
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
            # Position may have expired worthless — full profit
            if self._position_open and self._entry_credit > 0:
                self._record_exit_log("EXPIRED_WORTHLESS", self._entry_credit * 100)
            logger.info("Position expired or closed. Resetting.")
            self._position_open = False
            self._entry_credit = 0.0
            self._entry_orders = []
            self._closing_orders = []
            self._owned_legs = []
            self._exit_requested = False
            self._retry_count = 0
            self._mfe_pnl = 0.0
            self._mae_pnl = 0.0
            self._last_pnl = 0.0
            self._publish_snapshot(pnl=0.0, status="FLAT")
            return

        if self._exit_requested:
            self._close_all_options()
            return

        if not self._entry_credit or self._entry_credit <= 0:
            # Refresh missing credit on subsequent ticks without blocking the engine.
            if len(self._entry_orders) == 4:
                self._entry_credit = self._estimate_credit(
                    *(order.asset for order in self._entry_orders),
                    quantity=int(self._entry_orders[0].quantity),
                )

        if not self._entry_credit or self._entry_credit <= 0:
            self._retry_count += 1
            if self._retry_count <= 5:
                logger.warning(
                    "Zero credit on tick %d (attempt %d/%d). "
                    "Retrying on the next iteration...",
                    self._iteration_count,
                    self._retry_count,
                    5,
                )
                return  # Retry on next iteration
            else:
                logger.error("Max retries (5) exceeded for credit estimation. Closing open contracts.")
                self._close_all_options()
                return
        self._retry_count = 0

        # Calculate current P&L from option positions
        current_value = 0.0
        for pos in option_positions:
            price = self._fetch_last_price(pos.asset)
            if price is None:
                logger.warning("Missing option quote for %s; deferring P&L management.", pos.asset)
                return
            # Short positions have negative quantity
            leg_qty = self._owned_quantity_for_position(pos)
            current_value += float(price) * leg_qty * 100  # options multiplier

        # P&L: for a credit spread, profit = entry_credit - cost_to_close
        # current_value is negative if we owe money (short positions)
        pnl = self._entry_credit * 100 - abs(current_value)
        self._last_pnl = float(pnl)
        self._mfe_pnl = max(float(getattr(self, "_mfe_pnl", 0.0)), float(pnl))
        self._mae_pnl = min(float(getattr(self, "_mae_pnl", 0.0)), float(pnl))
        self._publish_snapshot(pnl=pnl, status="OPEN")

        pt_threshold = self._entry_credit * self.parameters["profit_target"] * 100
        sl_threshold = -self._entry_credit * self.parameters["stop_loss_mult"] * 100

        logger.info(
            "IC P&L: $%.2f | PT threshold: $%.2f | SL threshold: $%.2f",
            pnl, pt_threshold, sl_threshold,
        )

        if pnl >= pt_threshold:
            logger.info("🎯 PROFIT TARGET HIT — closing iron condor (P&L: $%.2f)", pnl)
            self._record_exit_log("PROFIT_TARGET", pnl)
            self._close_all_options()
        elif pnl <= sl_threshold:
            logger.warning("🛑 STOP LOSS HIT — closing iron condor (P&L: $%.2f)", pnl)
            self._record_exit_log("STOP_LOSS", pnl)
            self._close_all_options()

    def _record_exit_log(self, reason: str, pnl: float) -> None:
        """Persist trade exit event to logs/trades.jsonl."""
        try:
            trade_id = getattr(self, "_current_trade_id", None)
            if trade_id:
                from trade_logger import record_trade_exit
                mfe = max(float(getattr(self, "_mfe_pnl", 0.0)), float(pnl))
                mae = min(float(getattr(self, "_mae_pnl", 0.0)), float(pnl))
                record_trade_exit(
                    trade_id=trade_id,
                    strategy_name=getattr(self, "name", "SPXIronCondor"),
                    exit_reason=reason,
                    pnl=pnl,
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
        current_qty = int(self.parameters.get("quantity", 1))
        max_loss_per_contract = wing * 100.0
        added_risk = max_loss_per_contract * add_qty
        combined_trade_risk = max_loss_per_contract * (current_qty + add_qty)

        max_risk_cap = float(self.parameters.get("max_position_risk", 5000.0))
        override_pct = (os.getenv("IC_MAX_SINGLE_TRADE_PCT") or "").strip()
        single_trade_pct = float(override_pct) if override_pct else get_max_single_trade_pct(max_risk_cap)
        max_single_trade_risk = max_risk_cap * single_trade_pct

        if combined_trade_risk > max_single_trade_risk:
            logger.warning(
                "ADD_POSITION blocked: combined trade risk ($%.2f) exceeds single-trade limit ($%.2f).",
                combined_trade_risk, max_single_trade_risk,
            )
            return False

        existing_exposure = 0.0
        try:
            positions = self.get_positions()
            if positions is None:
                return False
            for pos in positions:
                price = self._fetch_last_price(pos.asset) or 0.0
                existing_exposure += abs(pos.quantity) * float(price) * 100.0
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
        if portfolio_value is not None:
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
            pnl = float(getattr(self, "_last_pnl", 0.0))
            self._record_exit_log("NEURAL_EARLY_EXIT", pnl)
            self._close_all_options()
            self._last_action = "FLATTEN_NOW"
            return "FLATTEN_NOW"

        owned_legs = list(getattr(self, "_owned_legs", []))
        if len(owned_legs) != 4:
            self._last_action = "HOLD"
            return "HOLD"

        if normalized == "ADD_POSITION":
            add_qty = 1
            if not self._can_add_contracts(add_qty):
                self._last_action = "HOLD"
                return "HOLD"
            try:
                add_orders = []
                updated_legs = []
                for leg_asset, signed_qty in owned_legs:
                    side = "buy" if signed_qty > 0 else "sell"
                    order = self.create_order(leg_asset, add_qty, side)
                    add_orders.append(order)
                    new_signed = signed_qty + (add_qty if signed_qty > 0 else -add_qty)
                    updated_legs.append((leg_asset, new_signed))
                for order in add_orders:
                    self._entry_orders.append(order)
                    self.submit_order(order)
                self._owned_legs = updated_legs
                self.parameters["quantity"] = int(self.parameters.get("quantity", 1)) + add_qty
                added_credit = self._estimate_credit(
                    *(asset for asset, _ in owned_legs),
                    quantity=add_qty,
                )
                self._entry_credit = float(getattr(self, "_entry_credit", 0.0)) + added_credit
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
            remaining_qty = current_qty - trim_qty
            try:
                trim_orders = []
                updated_legs = []
                for leg_asset, signed_qty in owned_legs:
                    side = "sell" if signed_qty > 0 else "buy"
                    order = self.create_order(leg_asset, trim_qty, side)
                    trim_orders.append(order)
                    new_signed = remaining_qty if signed_qty > 0 else -remaining_qty
                    updated_legs.append((leg_asset, new_signed))
                for order in trim_orders:
                    self._closing_orders.append(order)
                    self.submit_order(order)
                self._owned_legs = updated_legs
                self.parameters["quantity"] = remaining_qty
                self._entry_credit = float(getattr(self, "_entry_credit", 0.0)) * (remaining_qty / current_qty)
                self._last_action = "TRIM_POSITION"
                return "TRIM_POSITION"
            except Exception as e:
                logger.error("Failed to execute TRIM_POSITION: %s", e)
                self._last_action = "HOLD"
                return "HOLD"

        self._last_action = "HOLD"
        return "HOLD"

    def _close_all_options(self) -> None:
        """Close all open option positions owned by this strategy variant."""
        self._exit_requested = True
        self._position_open = True
        try:
            # Refresh order state and stop entry fills before unwinding their legs.
            self.get_orders()
            pending_entries = [order for order in self._entry_orders if order.is_active()]
            if pending_entries:
                for order in pending_entries:
                    self.cancel_order(order)
                return

            positions = self.get_positions()
            owned_positions = self._filter_owned_option_positions(positions)
            for pos in owned_positions:
                if any(
                    order.asset == pos.asset and order.is_active()
                    for order in self._closing_orders
                ):
                    continue
                leg_qty = self._owned_quantity_for_position(pos)
                if leg_qty == 0:
                    continue
                side = "sell" if leg_qty > 0 else "buy"
                order = self.create_order(pos.asset, abs(leg_qty), side)
                self._closing_orders.append(order)
                self.submit_order(order)
                logger.info("Closing: %s %d × %s", side, abs(leg_qty), pos.asset)
        except Exception as e:
            logger.error("Could not close all option positions; retaining management: %s", e)

    # ------------------------------------------------------------------ #
    # Helpers
    # ------------------------------------------------------------------ #

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
            return option_positions
        return [
            p for p in option_positions
            if any(self._asset_matches(p.asset, leg_asset) for leg_asset, _ in owned_legs)
        ]

    def _owned_quantity_for_position(self, pos) -> int:
        """Return signed contract quantity owned by this strategy for `pos.asset`."""
        owned_legs = getattr(self, "_owned_legs", None)
        if not owned_legs:
            return int(pos.quantity)
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
        override_pct = os.getenv("IC_MAX_SINGLE_TRADE_PCT")
        single_trade_pct = float(override_pct) if override_pct else get_max_single_trade_pct(max_risk_cap)
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
        existing_exposure = 0.0
        try:
            positions = self.get_positions()
            for pos in positions:
                price = self._fetch_last_price(pos.asset) or 0.0
                existing_exposure += abs(pos.quantity) * float(price) * 100.0
        except Exception as e:
            logger.warning("Could not calculate existing positions exposure: %s", e)

        total_exposure = existing_exposure + new_trade_risk

        if total_exposure > max_risk_cap:
            logger.warning(
                "Total open exposure ($%.2f) exceeds $%.2f cap (new trade risk: $%.2f, existing: $%.2f). Skipping trade.",
                total_exposure, max_risk_cap, new_trade_risk, existing_exposure,
            )
            return False

        # 3. Portfolio percentage allocation check
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

            # Fallback: use the nearest available
            return sorted_exps[0] if sorted_exps else None

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

            strikes = sorted(strikes)

            # Estimate strike distance from price based on delta target
            # Quick heuristic: for 0DTE, 15-delta is roughly 0.8-1.2% OTM
            # For longer DTE, scale with sqrt(time)
            dte = max(self.parameters["dte"], 0.25)  # avoid zero in sqrt
            delta_target = self.parameters["short_delta"]

            # Rough offset: price × delta × volatility_factor × sqrt(DTE)
            # Using a ~16% annualized vol assumption for SPX
            annual_vol = 0.16
            daily_vol = annual_vol / (252 ** 0.5)
            offset = underlying_price * daily_vol * (dte ** 0.5) * (1.0 / delta_target) * 0.15

            # Clamp offset to reasonable bounds
            offset = max(offset, self.parameters["wing_width"])
            offset = min(offset, underlying_price * 0.05)  # max 5% away

            target_put_strike = underlying_price - offset
            target_call_strike = underlying_price + offset

            # Find nearest available strikes
            put_strike = min(
                (s for s in strikes if s <= underlying_price),
                key=lambda s: abs(s - target_put_strike),
                default=None,
            )
            call_strike = min(
                (s for s in strikes if s >= underlying_price),
                key=lambda s: abs(s - target_call_strike),
                default=None,
            )

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

    def _estimate_credit(self, long_put, short_put, short_call, long_call, quantity: int = 1) -> float:
        """
        Estimate the net credit received from the iron condor.
        Credit = (short_put_price - long_put_price) + (short_call_price - long_call_price)
        
        Args:
            long_put: The long put option asset.
            short_put: The short put option asset.
            short_call: The short call option asset.
            long_call: The long call option asset.
            quantity: Number of contracts (default 1).
        
        Returns:
            Net credit per contract, scaled by quantity.
        """
        try:
            lp = self._fetch_last_price(long_put) or 0
            sp = self._fetch_last_price(short_put) or 0
            sc = self._fetch_last_price(short_call) or 0
            lc = self._fetch_last_price(long_call) or 0

            credit = (float(sp) - float(lp)) + (float(sc) - float(lc))
            return max(credit, 0) * quantity  # Scale by quantity for P&L consistency
        except Exception:
            return 0.0

    # ------------------------------------------------------------------ #
    # Lifecycle hooks
    # ------------------------------------------------------------------ #

    def before_market_closes(self):
        """
        Safety net: close any remaining positions before market close.
        Especially important for 0DTE to avoid settlement issues.
        """
        if self._position_open:
            logger.info("Market closing — closing all option positions")
            self._close_all_options()
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
