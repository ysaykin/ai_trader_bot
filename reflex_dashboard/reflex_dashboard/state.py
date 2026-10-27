"""
Reactive application state for the Lumibot Reflex Dashboard.
Manages live telemetry polling, user interactions, tabs, and broker controls.
"""

from __future__ import annotations

import reflex as rx
from typing import Any
from .data.adapters import (
    get_market_clock,
    get_bot_status_from_log,
    get_broker_telemetry,
    get_incubator_data,
    get_ab_lab_data,
    get_neural_policy_data,
    get_recent_logs,
    get_config_settings,
    save_config_settings,
    money,
)


class DashboardState(rx.State):
    """Global reactive state for the Lumibot Quantitative Trading Dashboard."""

    # Navigation
    current_tab: str = "incubator"

    # Market Clock & Lifecycle
    market_time: str = "09:30:00 ET"
    market_date: str = "Oct 27, 2026"
    market_status: str = "REGULAR TRADING HOURS"
    market_badge_color: str = "jade"

    # Bot Heartbeat
    bot_state: str = "RUNNING"
    bot_last_action: str = "Monitoring active variants"
    bot_timestamp: str = "Recent"
    bot_badge_color: str = "jade"

    # Broker Telemetry
    broker_name: str = "TRADIER"
    account_id: str = "VA21359019"
    is_paper: bool = True
    equity: float = 99762.40
    equity_str: str = "$99,762.40"
    cash: float = 99762.40
    cash_str: str = "$99,762.40"
    buying_power: float = 99795.65
    buying_power_str: str = "$99,795.65"
    option_requirement: float = 0.0
    option_requirement_str: str = "$0.00"
    open_risk: float = 0.0
    open_risk_str: str = "$0.00"
    open_risk_cap: float = 5000.0
    open_risk_pct: int = 0
    buying_power_headroom_pct: int = 100

    # Incubator Grid
    incubator_rows: list[dict[str, Any]] = []
    incubator_monitored_rows: list[dict[str, Any]] = []
    active_grid_str: str = "11/20 Active"
    monitored_count_str: str = "9 Scanning"
    total_completed_trades: int = 14
    overall_win_rate: str = "100.0%"
    total_gross_pnl_str: str = "$150.00"
    total_fees_str: str = "$0.00"
    total_net_pnl_str: str = "$150.00"
    selected_variant: str = "IC_V01"

    # A/B Multi-Strategy Testing Lab (EXP-01 through EXP-04)
    ab_all_strategies: list[dict[str, Any]] = []
    ab_strategy_options: list[str] = []
    ab_strategy_a: str = "IC_V20_D15_W5_PT50_1DTE"
    ab_strategy_b: str = "IC_V16_D20_W3_PT50_T1030"
    ab_card_a: dict[str, Any] = {
        "variant": "IC_V20",
        "tag": "[Strategy A: IC_V20]",
        "config_desc": "Delta 0.15, Wing $5.0, PT 50%, SL 2.0x, 1DTE",
        "closed_trades": "1 Trade",
        "win_rate": "100.0%",
        "gross_pnl_str": "+$28.00",
        "fees_str": "-$3.20",
        "net_pnl_str": "+$24.80",
        "net_ror_pct": "+4.96%",
        "net_roc_pct": "+45.9%",
        "profit_factor": "∞ (0 losses)",
        "avg_trade_str": "+$24.80",
    }
    ab_card_b: dict[str, Any] = {
        "variant": "IC_V16",
        "tag": "[Strategy B: IC_V16]",
        "config_desc": "Delta 0.20, Wing $3.0, PT 50%, SL 2.0x, 0DTE",
        "closed_trades": "1 Trade",
        "win_rate": "100.0%",
        "gross_pnl_str": "+$26.00",
        "fees_str": "-$3.20",
        "net_pnl_str": "+$22.80",
        "net_ror_pct": "+7.60%",
        "net_roc_pct": "+45.6%",
        "profit_factor": "∞ (0 losses)",
        "avg_trade_str": "+$22.80",
    }
    ab_delta_net_pnl: str = "+$2.00"
    ab_delta_ror_pct: str = "-2.64%"
    ab_delta_winner: str = "Strategy A Leads in Net P&L (+$2.00)"

    # Neural Policy
    market_regime: str = "Low-Vol Pinning"
    dynamic_pt: str = "40.0%"
    contextual_selector: str = "IC_V05 (15Δ, $5 Wing)"
    recommendation_action: str = "HOLD"
    action_prob_hold: int = 68
    action_prob_add: int = 12
    action_prob_trim: int = 15
    action_prob_flatten: int = 5
    regret_score: str = "+$18.40 vs Hindsight Best"
    capture_efficiency: str = "84.2% MFE Capture"

    # Positions & Orders
    positions: list[dict[str, Any]] = []
    orders: list[dict[str, Any]] = []

    # Real-Time Logs
    log_filter: str = ""
    logs: list[str] = []

    # Config form
    cfg_broker: str = "tradier"
    cfg_account_id: str = "VA21359019"
    cfg_access_token: str = ""
    cfg_paper: str = "true"
    cfg_symbol: str = "XSP"
    cfg_fee_per_contract: str = "0.40"
    cfg_risk_cap: str = "5000"

    # Testing Strategy Configuration (25 DTE Experimental Setup)
    cfg_testing_dte: str = "25"
    cfg_testing_delta: str = "0.15"
    cfg_testing_wing: str = "5.0"
    cfg_testing_pt: str = "0.50"
    cfg_testing_sl: str = "2.0"
    cfg_testing_entry: str = "10:30"

    # Pure Neural Network Strategy Configuration
    cfg_neural_enabled: str = "true"
    cfg_neural_dte: str = "7"
    cfg_neural_delta: str = "0.15"
    cfg_neural_wing: str = "5.0"
    cfg_neural_entry: str = "10:00"

    # Panic Liquidation Modal
    panic_modal_open: bool = False
    panic_in_progress: bool = False

    # Auto refresh toggle
    auto_refresh: bool = True
    is_polling: bool = False

    @rx.event(background=True)
    async def live_update_loop(self):
        """Background coroutine updating dashboard telemetry every 3 seconds."""
        import asyncio

        async with self:
            if self.is_polling:
                return
            self.is_polling = True

        while True:
            await asyncio.sleep(3)
            async with self:
                if not self.auto_refresh:
                    self.is_polling = False
                    break
                self.load_all()

    def toggle_auto_refresh(self):
        self.auto_refresh = not self.auto_refresh
        if self.auto_refresh and not self.is_polling:
            return DashboardState.live_update_loop

    def on_load(self):
        """Called when dashboard first loads."""
        self.load_all()
        return DashboardState.live_update_loop

    def load_all(self):
        """Fetch all telemetry and records."""
        try:
            # 1. Market clock
            clk = get_market_clock()
            self.market_time = clk["time_str"]
            self.market_date = clk["date_str"]
            self.market_status = clk["status"]
            self.market_badge_color = clk["badge_color"]

            # 2. Bot status
            bot = get_bot_status_from_log()
            self.bot_state = bot["state"]
            self.bot_last_action = bot["last_action"]
            self.bot_timestamp = bot["timestamp"]
            self.bot_badge_color = bot["badge_color"]

            # 3. Broker balances & positions
            tel = get_broker_telemetry()
            self.broker_name = tel["broker"]
            self.account_id = tel["account_id"]
            self.is_paper = tel["is_paper"]
            self.equity = tel["equity"]
            self.equity_str = money(tel["equity"])
            self.cash = tel["cash"]
            self.cash_str = money(tel["cash"])
            self.buying_power = tel["buying_power"]
            self.buying_power_str = money(tel["buying_power"])
            self.option_requirement = tel["option_requirement"]
            self.option_requirement_str = money(tel["option_requirement"])
            self.open_risk = tel["open_exposure"]
            self.open_risk_str = money(tel["open_exposure"])
            self.open_risk_cap = tel["risk_cap"]
            self.open_risk_pct = int(min(100.0, (self.open_risk / max(self.open_risk_cap, 1.0)) * 100.0))
            self.buying_power_headroom_pct = int(tel["buying_power_headroom_pct"])
            self.positions = tel["positions"]
            self.orders = tel["orders"]

            # 4. Incubator Grid
            inc = get_incubator_data()
            self.incubator_rows = inc["rows"]
            self.incubator_monitored_rows = inc["monitored_rows"]
            self.active_grid_str = inc["active_grid_str"]
            self.monitored_count_str = f"{len(inc['monitored_rows'])} Scanning"
            self.total_completed_trades = inc["total_trades"]
            self.overall_win_rate = inc["overall_win_rate"]
            self.total_gross_pnl_str = inc["total_gross_pnl_str"]
            self.total_fees_str = inc["total_fees_str"]
            self.total_net_pnl_str = inc["total_net_pnl_str"]

            # 5. A/B Testing Lab
            ab = get_ab_lab_data()
            self.ab_all_strategies = ab["strategies"]
            self.ab_strategy_options = ab["strategy_options"]
            if not self.ab_strategy_a and ab["default_a"]:
                self.ab_strategy_a = ab["default_a"]
            if not self.ab_strategy_b and ab["default_b"]:
                self.ab_strategy_b = ab["default_b"]
            self._recompute_ab_comparison()

            # 6. Neural Policy
            ml = get_neural_policy_data()
            self.market_regime = ml["market_regime"]
            self.dynamic_pt = ml["dynamic_pt"]
            self.contextual_selector = ml["contextual_selector"]
            self.recommendation_action = ml["recommendation_action"]
            self.action_prob_hold = int(ml["action_probs"]["HOLD"])
            self.action_prob_add = int(ml["action_probs"]["ADD"])
            self.action_prob_trim = int(ml["action_probs"]["TRIM"])
            self.action_prob_flatten = int(ml["action_probs"]["FLATTEN"])
            self.regret_score = ml["regret_score"]
            self.capture_efficiency = ml["capture_efficiency"]

            # 7. Logs
            self.logs = get_recent_logs(max_lines=120, filter_query=self.log_filter)

            # 8. Config
            cfg = get_config_settings()
            self.cfg_broker = cfg.get("broker", "tradier")
            self.cfg_account_id = cfg.get("tradier_account_id", "")
            self.cfg_symbol = cfg.get("symbol", "XSP")
            self.cfg_fee_per_contract = cfg.get("fee_per_contract", "0.40")
            self.cfg_risk_cap = cfg.get("risk_cap", "5000")
            self.cfg_testing_dte = cfg.get("testing_dte", "25")
            self.cfg_testing_delta = cfg.get("testing_delta", "0.15")
            self.cfg_testing_wing = cfg.get("testing_wing", "5.0")
            self.cfg_testing_pt = cfg.get("testing_pt", "0.50")
            self.cfg_testing_sl = cfg.get("testing_sl", "2.0")
            self.cfg_testing_entry = cfg.get("testing_entry", "10:30")
            self.cfg_neural_enabled = cfg.get("neural_enabled", "true")
            self.cfg_neural_dte = cfg.get("neural_dte", "7")
            self.cfg_neural_delta = cfg.get("neural_delta", "0.15")
            self.cfg_neural_wing = cfg.get("neural_wing", "5.0")
            self.cfg_neural_entry = cfg.get("neural_entry", "10:00")
        except Exception as exc:
            import logging
            logging.getLogger("reflex_dashboard").warning(f"Error in load_all: {exc}")

    def set_cfg_broker(self, val: str):
        self.cfg_broker = val

    def set_cfg_account_id(self, val: str):
        self.cfg_account_id = val

    def set_cfg_access_token(self, val: str):
        self.cfg_access_token = val

    def _recompute_ab_comparison(self):
        """Update side-by-side comparison metrics for selected Strategy A & B."""
        match_a = next((s for s in self.ab_all_strategies if s["strategy_name"] == self.ab_strategy_a), None)
        match_b = next((s for s in self.ab_all_strategies if s["strategy_name"] == self.ab_strategy_b), None)

        if match_a:
            self.ab_card_a = {
                "variant": match_a["variant"],
                "tag": f"[Strategy A: {match_a['variant']}]",
                "config_desc": match_a["config_desc"],
                "closed_trades": match_a["closed_trades"],
                "win_rate": match_a["win_rate"],
                "gross_pnl_str": match_a["gross_pnl_str"],
                "fees_str": match_a["fees_str"],
                "net_pnl_str": match_a["net_pnl_str"],
                "net_ror_pct": match_a["net_ror_pct"],
                "net_roc_pct": match_a["net_roc_pct"],
                "profit_factor": match_a["profit_factor"],
                "avg_trade_str": match_a["avg_trade_str"],
                "status": match_a["status"],
                "status_color": match_a["status_color"],
            }
        if match_b:
            self.ab_card_b = {
                "variant": match_b["variant"],
                "tag": f"[Strategy B: {match_b['variant']}]",
                "config_desc": match_b["config_desc"],
                "closed_trades": match_b["closed_trades"],
                "win_rate": match_b["win_rate"],
                "gross_pnl_str": match_b["gross_pnl_str"],
                "fees_str": match_b["fees_str"],
                "net_pnl_str": match_b["net_pnl_str"],
                "net_ror_pct": match_b["net_ror_pct"],
                "net_roc_pct": match_b["net_roc_pct"],
                "profit_factor": match_b["profit_factor"],
                "avg_trade_str": match_b["avg_trade_str"],
                "status": match_b["status"],
                "status_color": match_b["status_color"],
            }

        pnl_a = match_a["net_pnl_num"] if match_a else 0.0
        pnl_b = match_b["net_pnl_num"] if match_b else 0.0
        diff_pnl = pnl_a - pnl_b
        self.ab_delta_net_pnl = f"{diff_pnl:+.2f}"

        if pnl_a > pnl_b:
            self.ab_delta_winner = f"Strategy A ({match_a['variant'] if match_a else 'A'}) leads by {money(abs(diff_pnl))} Net P&L"
        elif pnl_b > pnl_a:
            self.ab_delta_winner = f"Strategy B ({match_b['variant'] if match_b else 'B'}) leads by {money(abs(diff_pnl))} Net P&L"
        else:
            self.ab_delta_winner = "Strategy A and Strategy B have equal Net P&L"

    def set_ab_strategy_a(self, val: str):
        self.ab_strategy_a = val
        self._recompute_ab_comparison()

    def set_ab_strategy_b(self, val: str):
        self.ab_strategy_b = val
        self._recompute_ab_comparison()

    def set_cfg_symbol(self, val: str):
        self.cfg_symbol = val

    def set_cfg_fee_per_contract(self, val: str):
        self.cfg_fee_per_contract = val

    def set_cfg_risk_cap(self, val: str):
        self.cfg_risk_cap = val

    def set_cfg_testing_dte(self, val: str):
        self.cfg_testing_dte = val

    def set_cfg_testing_delta(self, val: str):
        self.cfg_testing_delta = val

    def set_cfg_testing_wing(self, val: str):
        self.cfg_testing_wing = val

    def set_cfg_testing_pt(self, val: str):
        self.cfg_testing_pt = val

    def set_cfg_testing_sl(self, val: str):
        self.cfg_testing_sl = val

    def set_cfg_testing_entry(self, val: str):
        self.cfg_testing_entry = val

    def set_cfg_neural_enabled(self, val: str):
        self.cfg_neural_enabled = val

    def set_cfg_neural_dte(self, val: str):
        self.cfg_neural_dte = val

    def set_cfg_neural_delta(self, val: str):
        self.cfg_neural_delta = val

    def set_cfg_neural_wing(self, val: str):
        self.cfg_neural_wing = val

    def set_cfg_neural_entry(self, val: str):
        self.cfg_neural_entry = val

    def set_tab(self, tab: str):
        self.current_tab = tab

    def set_filter(self, query: str):
        self.log_filter = query
        self.logs = get_recent_logs(max_lines=120, filter_query=self.log_filter)

    def select_variant(self, variant: str):
        self.selected_variant = variant

    def toggle_panic_modal(self):
        self.panic_modal_open = not self.panic_modal_open

    def execute_panic_liquidation(self):
        """Execute emergency position flattening."""
        self.panic_in_progress = True
        try:
            from dashboard.broker_client import BrokerClient
            client = BrokerClient()
            client.close_all_positions()
            self.load_all()
            self.panic_modal_open = False
            self.panic_in_progress = False
            return rx.toast.success("🚨 Emergency Panic Liquidation Completed! All positions flattened.")
        except Exception as exc:
            self.panic_in_progress = False
            self.panic_modal_open = False
            return rx.toast.error(f"Panic liquidation failed: {exc}")

    def save_configuration(self):
        """Save form values to .env file."""
        updates = {
            "BROKER": self.cfg_broker,
            "TRADIER_ACCOUNT_ID": self.cfg_account_id,
            "IC_SYMBOL": self.cfg_symbol,
            "OPTION_FEE_PER_CONTRACT": self.cfg_fee_per_contract,
            "GLOBAL_RISK_CAP": self.cfg_risk_cap,
            "TESTING_DTE": self.cfg_testing_dte,
            "TESTING_DELTA": self.cfg_testing_delta,
            "TESTING_WING": self.cfg_testing_wing,
            "TESTING_PT": self.cfg_testing_pt,
            "TESTING_SL": self.cfg_testing_sl,
            "TESTING_ENTRY_TIME": self.cfg_testing_entry,
            "NEURAL_STRATEGY_ENABLED": self.cfg_neural_enabled,
            "NEURAL_STRATEGY_DTE": self.cfg_neural_dte,
            "NEURAL_STRATEGY_DELTA": self.cfg_neural_delta,
            "NEURAL_STRATEGY_WING": self.cfg_neural_wing,
            "NEURAL_STRATEGY_ENTRY_TIME": self.cfg_neural_entry,
        }
        if self.cfg_access_token:
            updates["TRADIER_ACCESS_TOKEN"] = self.cfg_access_token

        success = save_config_settings(updates)
        if success:
            return rx.toast.success("Configuration successfully saved to .env!")
        return rx.toast.error("Failed to write .env file.")
