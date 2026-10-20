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

import os
import logging
from datetime import datetime, time, timedelta

from lumibot.strategies.strategy import Strategy
from lumibot.entities import Asset

logger = logging.getLogger(__name__)


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
    }

    def initialize(self):
        """Load parameters from env vars and set up state."""
        p = self.parameters

        # Override defaults from environment
        p["symbol"] = os.getenv("IC_SYMBOL", p["symbol"])
        p["dte"] = int(os.getenv("IC_DTE", p["dte"]))
        p["short_delta"] = float(os.getenv("IC_SHORT_DELTA", p["short_delta"]))
        p["wing_width"] = float(os.getenv("IC_WING_WIDTH", p["wing_width"]))
        p["quantity"] = int(os.getenv("IC_QUANTITY", p["quantity"]))
        p["profit_target"] = float(os.getenv("IC_PROFIT_TARGET", p["profit_target"]))
        p["stop_loss_mult"] = float(os.getenv("IC_STOP_LOSS_MULT", p["stop_loss_mult"]))
        p["entry_time"] = os.getenv("IC_ENTRY_TIME", p["entry_time"])
        p["max_allocation"] = float(os.getenv("IC_MAX_ALLOCATION", p["max_allocation"]))

        # Parse entry time
        h, m = map(int, p["entry_time"].split(":"))
        self._entry_time = time(h, m)

        # Check frequently so we don't miss the entry window
        self.sleeptime = "5M"  # every 5 minutes

        # Trade state
        self._position_open = False
        self._entry_credit = 0.0
        self._entry_orders = []

        # Define the underlying asset
        # For SPX index options, the symbol is typically "SPX" or "SPXW"
        self._underlying = Asset(
            symbol=p["symbol"],
            asset_type=Asset.AssetType.INDEX,
        )

        logger.info(
            "Iron Condor initialized: %s | DTE=%d | Δ=%.2f | width=%.0f | "
            "qty=%d | PT=%.0f%% | SL=%.0fx",
            p["symbol"], p["dte"], p["short_delta"], p["wing_width"],
            p["quantity"], p["profit_target"] * 100, p["stop_loss_mult"],
        )

    # ------------------------------------------------------------------ #
    # Main trading loop
    # ------------------------------------------------------------------ #

    def on_trading_iteration(self):
        """Called every 5 minutes during market hours."""
        now = self.get_datetime()

        # --- MANAGEMENT: Check existing position ---
        if self._position_open:
            self._manage_position()
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
        underlying_price = self.get_last_price(self._underlying)
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

            for order in orders:
                self.submit_order(order)

            self._position_open = True
            self._entry_orders = orders

            # Estimate entry credit from mid-prices
            self._entry_credit = self._estimate_credit(
                long_put, short_put, short_call, long_call
            )
            logger.info("Iron Condor submitted. Estimated credit: $%.2f", self._entry_credit)

        except Exception as e:
            logger.error("Failed to submit iron condor: %s", e)

    # ------------------------------------------------------------------ #
    # Position management
    # ------------------------------------------------------------------ #

    def _manage_position(self):
        """Check profit target and stop loss on the open iron condor."""
        if not self._entry_credit or self._entry_credit <= 0:
            return

        # Get current value of the position
        positions = self.get_positions()
        if not positions:
            # Position may have expired worthless — full profit
            logger.info("Position expired or closed. Resetting.")
            self._position_open = False
            self._entry_credit = 0.0
            return

        # Calculate current P&L from option positions
        current_value = 0.0
        option_positions = [
            p for p in positions
            if hasattr(p.asset, 'asset_type')
            and p.asset.asset_type == Asset.AssetType.OPTION
        ]

        if not option_positions:
            self._position_open = False
            self._entry_credit = 0.0
            return

        for pos in option_positions:
            price = self.get_last_price(pos.asset)
            if price is not None:
                # Short positions have negative quantity
                current_value += float(price) * pos.quantity * 100  # options multiplier

        # P&L: for a credit spread, profit = entry_credit - cost_to_close
        # current_value is negative if we owe money (short positions)
        pnl = self._entry_credit * 100 - abs(current_value)

        pt_threshold = self._entry_credit * self.parameters["profit_target"] * 100
        sl_threshold = -self._entry_credit * self.parameters["stop_loss_mult"] * 100

        logger.info(
            "IC P&L: $%.2f | PT threshold: $%.2f | SL threshold: $%.2f",
            pnl, pt_threshold, sl_threshold,
        )

        if pnl >= pt_threshold:
            logger.info("🎯 PROFIT TARGET HIT — closing iron condor (P&L: $%.2f)", pnl)
            self._close_all_options()
        elif pnl <= sl_threshold:
            logger.warning("🛑 STOP LOSS HIT — closing iron condor (P&L: $%.2f)", pnl)
            self._close_all_options()

    def _close_all_options(self):
        """Close all open option positions."""
        positions = self.get_positions()
        for pos in positions:
            if (hasattr(pos.asset, 'asset_type')
                    and pos.asset.asset_type == Asset.AssetType.OPTION):
                side = "sell" if pos.quantity > 0 else "buy"
                order = self.create_order(pos.asset, abs(pos.quantity), side)
                self.submit_order(order)
                logger.info("Closing: %s %d × %s", side, abs(pos.quantity), pos.asset)

        self._position_open = False
        self._entry_credit = 0.0

    # ------------------------------------------------------------------ #
    # Helpers
    # ------------------------------------------------------------------ #

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
        """Ensure we don't exceed max allocation."""
        portfolio_value = self.portfolio_value
        wing = self.parameters["wing_width"]
        qty = self.parameters["quantity"]
        max_loss_per_contract = wing * 100  # SPX options have $100 multiplier
        total_risk = max_loss_per_contract * qty

        max_allowed = portfolio_value * self.parameters["max_allocation"]

        if total_risk > max_allowed:
            logger.warning(
                "Risk $%.0f exceeds %.0f%% allocation ($%.0f). Skipping.",
                total_risk, self.parameters["max_allocation"] * 100, max_allowed,
            )
            return False
        return True

    def _find_expiration(self):
        """Find the best expiration date for the target DTE."""
        try:
            chains = self.get_chains(self._underlying)
            if not chains:
                return None

            expirations = self.get_expiration(chains)
            if not expirations:
                return None

            today = self.get_datetime().date()
            target_date = today + timedelta(days=self.parameters["dte"])

            # Find the nearest expiration on or after target_date
            for exp in sorted(expirations):
                if exp >= target_date:
                    return exp

            # Fallback: use the nearest available
            return expirations[0] if expirations else None

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
            chains = self.get_chains(self._underlying)
            if not chains:
                return None, None

            strikes = self.get_strikes(self._underlying, chains=chains)
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

    def _estimate_credit(self, long_put, short_put, short_call, long_call) -> float:
        """
        Estimate the net credit received from the iron condor.
        Credit = (short_put_price - long_put_price) + (short_call_price - long_call_price)
        """
        try:
            lp = self.get_last_price(long_put) or 0
            sp = self.get_last_price(short_put) or 0
            sc = self.get_last_price(short_call) or 0
            lc = self.get_last_price(long_call) or 0

            credit = (float(sp) - float(lp)) + (float(sc) - float(lc))
            return max(credit, 0)
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

    def on_abrupt_closing(self):
        """Handle unexpected shutdowns gracefully."""
        logger.warning("Abrupt shutdown detected. Attempting to close positions.")
        if self._position_open:
            self._close_all_options()
