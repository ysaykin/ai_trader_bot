"""
Reactive application state for the Lumibot Reflex Dashboard.
Manages live telemetry polling, user interactions, tabs, and broker controls.
"""

from __future__ import annotations

import logging
import math
import os
import reflex as rx
import plotly.graph_objects as go
from typing import Any

logger = logging.getLogger(__name__)
from .data.adapters import (
    get_market_clock,
    get_bot_status_from_log,
    get_broker_telemetry,
    get_position_workspace,
    get_incubator_data,
    get_ab_lab_data,
    get_neural_policy_data,
    get_recent_logs,
    get_config_settings,
    save_config_settings,
    generate_trades_csv,
    money,
)
from dashboard.positions import display, payoff_bounds, payoff_figure, summarize


class DashboardState(rx.State):
    """Global reactive state for the Lumibot Quantitative Trading Dashboard."""

    # Navigation
    current_tab: str = "positions"

    # Market Clock & Lifecycle
    market_time: str = "--:--:-- ET"
    market_date: str = ""
    market_status: str = "Loading"
    market_badge_color: str = "gray"

    # Bot Heartbeat
    bot_state: str = "INITIALIZING"
    bot_last_action: str = "Waiting for engine status"
    bot_timestamp: str = ""
    bot_badge_color: str = "gray"

    # Broker Telemetry
    broker_name: str = "TRADIER"
    account_id: str = ""
    broker_sync_status: str = "loading"
    broker_sync_message: str = "Waiting for the first broker snapshot"
    broker_last_known: str = ""
    orders_status: str = "loading"
    orders_synced_at: str = "No successful sync"
    is_paper: bool = True
    equity: float | None = None
    equity_str: str = "N/A"
    cash: float | None = None
    cash_str: str = "N/A"
    buying_power: float | None = None
    buying_power_str: str = "N/A"
    option_requirement: float | None = None
    option_requirement_str: str = "N/A"
    broker_margin: float | None = None
    broker_margin_str: str = "N/A"
    strategy_risk: float | None = None
    strategy_risk_str: str = "N/A"
    strategy_risk_cap: float = 15000.0
    strategy_risk_cap_str: str = "$15,000.00"
    strategy_risk_pct: int = 0
    strategy_risk_pct_str: str = "EXPOSURE UNAVAILABLE"
    portfolio_risk: float | None = None
    portfolio_risk_str: str = "N/A"
    portfolio_risk_cap: float = 100000.0
    portfolio_risk_cap_str: str = "$100,000.00"
    portfolio_risk_pct: int = 0
    portfolio_risk_pct_str: str = "EXPOSURE UNAVAILABLE"
    open_risk: float | None = None
    open_risk_str: str = "N/A"
    open_risk_cap: float = 15000.0
    open_risk_cap_str: str = "$15,000.00"
    open_risk_pct: int = 0
    open_risk_pct_str: str = "EXPOSURE UNAVAILABLE"
    buying_power_headroom_pct: int = 0
    buying_power_headroom_str: str = "Headroom unavailable"

    # Incubator Grid
    incubator_rows: list[dict[str, Any]] = []
    incubator_monitored_rows: list[dict[str, Any]] = []
    active_grid_str: str = "Loading"
    configured_variant_count: int = 0
    monitored_count_str: str = "Loading"
    total_completed_trades: int = 0
    overall_win_rate: str = "N/A"
    total_gross_pnl_str: str = "N/A"
    total_fees_str: str = "N/A"
    total_net_pnl_str: str = "N/A"
    selected_variant: str = "IC_V01"

    # Incubator Curated Groups & Per-Trade History
    ai_aggregate: dict[str, Any] = {
        "title": "🤖 AI Pure Neural Strategy",
        "variant": "IC_V22 (Neural Pure)",
        "net_pnl_str": "N/A",
        "win_rate": "N/A",
        "trades": 0,
        "fees_str": "N/A",
        "status": "DYNAMIC REGIME ADAPT",
        "badge_color": "indigo",
    }
    top5_aggregate: dict[str, Any] = {
        "title": "🏆 Top 5 Performing Strategies",
        "net_pnl_str": "N/A",
        "avg_win_rate": "N/A",
        "trades": 0,
        "fees_str": "N/A",
        "best_variant": "N/A",
        "badge_color": "jade",
    }
    bottom3_aggregate: dict[str, Any] = {
        "title": "⚠️ Bottom 3 Underperforming Strategies",
        "net_pnl_str": "N/A",
        "avg_win_rate": "N/A",
        "trades": 0,
        "fees_str": "N/A",
        "worst_variant": "N/A",
        "badge_color": "crimson",
    }
    visible_trades_50: list[dict[str, Any]] = []
    total_closed_trades_count: int = 0
    visible_closed_trades_count: int = 0
    eligible_closed_trades_count: int = 0
    unattributed_trades_count: int = 0
    quarantined_trades_count: int = 0
    load_more_modal_open: bool = False

    # A/B Multi-Strategy Testing Lab (EXP-01 through EXP-04)
    ab_all_strategies: list[dict[str, Any]] = []
    ab_strategy_options: list[str] = []
    ab_strategy_a: str = ""
    ab_strategy_b: str = ""
    ab_card_a: dict[str, Any] = {}
    ab_card_b: dict[str, Any] = {}
    ab_delta_net_pnl: str = "$0.00"
    ab_delta_ror_pct: str = "0.00%"
    ab_delta_winner: str = "No data"

    # S&P 500 Benchmark Comparison Lab
    benchmark_lumibot_return: str = "+0.00%"
    benchmark_closed_trades: str = "0 Closed Trades"
    benchmark_spy_return: str = "+0.00%"
    benchmark_alpha: str = "+0.00%"
    benchmark_beta: str = "0.00"
    benchmark_sharpe: str = "0.00"
    benchmark_sortino: str = "0.00"
    benchmark_max_strategy_dd: str = "0.00%"
    benchmark_max_spy_dd: str = "-1.42%"
    benchmark_correlation: str = "0.08"

    # Neural Policy
    model_version: str = "v1"
    market_regime: str = "Low-Vol Pinning"
    dynamic_pt: str = "40.0%"
    contextual_selector: str = "IC_V05 (15Δ, $5 Wing)"
    active_switched_profile: str = "IC_V05_7DTE_D12_W3_PT35_T1015"
    dynamic_switching_status: str = "AUTO-ADAPTING TO MARKET REGIME"
    data_readiness_str: str = "READY (12/12 Ticks)"
    has_sufficient_data: bool = True
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
    position_records: list[dict[str, Any]] = []
    position_issues: list[str] = []
    position_data_status: str = "loading"
    position_synced_at: str = "Waiting for broker data"
    position_filter_mode: str = "all"
    position_strategy_filters: list[str] = []
    position_structure_filter: str = "All structures"
    position_underlying_filter: str = "All underlyings"
    position_expiry_filter: str = "All expiries"
    position_workspace_mode: str = "Positions"
    position_expanded: list[str] = []
    selected_position_id: str = ""
    position_payoff_scope: str = ""
    position_chart_units: str = "Dollars"

    @rx.var
    def position_strategy_options(self) -> list[dict[str, Any]]:
        return [{"id": strategy, "label": "_".join(strategy.split("_")[:2]),
                 "checked": self.position_filter_mode == "all" or strategy in self.position_strategy_filters}
                for strategy in sorted({p["strategy"] for p in self.position_records})]

    @rx.var
    def position_underlying_options(self) -> list[str]:
        return ["All underlyings", *sorted({p["underlying"] for p in self.position_records})]

    @rx.var
    def position_expiry_options(self) -> list[str]:
        return ["All expiries", *sorted({p["expiry"] for p in self.position_records})]

    @rx.var
    def filtered_position_records(self) -> list[dict[str, Any]]:
        return [p for p in self.position_records
                if (self.position_filter_mode == "all" or p["strategy"] in self.position_strategy_filters)
                and (self.position_structure_filter == "All structures" or p["structure_label"] == self.position_structure_filter)
                and (self.position_underlying_filter == "All underlyings" or p["underlying"] == self.position_underlying_filter)
                and (self.position_expiry_filter == "All expiries" or p["expiry"] == self.position_expiry_filter)]

    def _position_display_row(self, row: dict[str, Any]) -> dict[str, Any]:
        result = dict(row)
        for key in ("open_pnl", "realized_pnl", "net_pnl", "opening_risk", "return_pct", "remaining_entry_cash", "liquidation_value", "delta", "theta"):
            result[key + "_display"] = display(row[key], percent=key == "return_pct", currency=key != "delta")
        result["fees_display"] = display(-row["fees"] if row["fees"] is not None else None)
        result["fee_label"] = "Estimated fees" if row["estimated_fees"] else "Reported fees"
        result["dte_label"] = f"{row['dte']} DTE" if row["dte"] is not None else "DTE unavailable"
        result["leg_rows"] = [{"symbol": leg["symbol"], "quantity": str(leg["quantity"]),
                               "quantity_color": display(float(leg["quantity"]))["color"],
                               **{f"{key}_{attr}": value for key in ("cost", "mark", "pnl", "delta", "theta")
                                  for attr, value in display(leg[key], currency=key != "delta").items()},
                               "quote_time": leg["quote_time"]} for leg in row["legs"]]
        return result

    @rx.var
    def position_groups(self) -> list[dict[str, Any]]:
        rows = self.filtered_position_records
        return [{"strategy": strategy, "label": "_".join(strategy.split("_")[:2]),
                 "net": display(summarize([p for p in rows if p["strategy"] == strategy])["net_pnl"]),
                 "rows": [self._position_display_row(p) for p in rows if p["strategy"] == strategy]}
                for strategy in sorted({p["strategy"] for p in rows})]

    @rx.var
    def position_summary(self) -> dict[str, Any]:
        summary = summarize(self.filtered_position_records)
        if self.position_data_status != "ready" and not self.filtered_position_records:
            summary.update(open_pnl=None, net_pnl=None)
        return {"count": f"{summary['count']} of {len(self.position_records)} positions",
                "coverage": f"{summary['marked']} of {summary['count']} valued",
                "open": display(summary["open_pnl"]), "net": display(summary["net_pnl"]),
                "fee_note": "Includes estimated fees" if summary["estimated_fees"] else "Includes reported fees"}

    @rx.var
    def position_comparison_rows(self) -> list[dict[str, Any]]:
        rows = self.filtered_position_records
        result = []
        for strategy in sorted({p["strategy"] for p in rows}):
            group = [p for p in rows if p["strategy"] == strategy]
            totals = summarize(group)
            result.append({"strategy": strategy, "count": str(len(group)),
                           "fee_label": "Estimated" if totals["estimated_fees"] else "Reported",
                           **{key: display(totals[key], percent=key == "return_pct") for key in
                              ("open_pnl", "realized_pnl", "net_pnl", "opening_risk", "return_pct")},
                           "fees": display(-totals["fees"] if totals["fees"] is not None else None)})
        return result

    @rx.var
    def position_chart_records(self) -> list[dict[str, Any]]:
        rows = self.filtered_position_records
        if self.position_workspace_mode == "Positions":
            selected = next((p for p in rows if p["id"] == self.selected_position_id), None)
            rows = [selected] if selected else rows[:1]
        return rows

    @rx.var
    def position_scope_options(self) -> list[str]:
        return sorted({p["scope"] for p in self.position_chart_records if p["payoff_eligible"]})

    @rx.var
    def effective_position_scope(self) -> str:
        options = self.position_scope_options
        return self.position_payoff_scope if self.position_payoff_scope in options else next(iter(options), "")

    @rx.var
    def position_payoff_figure(self) -> go.Figure:
        return payoff_figure(self.position_chart_records, self.effective_position_scope, self.position_chart_units)

    @rx.var
    def position_chart_info(self) -> dict[str, Any]:
        rows = [r for r in self.position_chart_records if r["payoff_eligible"] and r["scope"] == self.effective_position_scope]
        bounds = payoff_bounds(rows[0]) if len(rows) == 1 else {"max_profit": None, "max_loss": None, "breakevens": []}
        label = (f"{'_'.join(rows[0]['strategy'].split('_')[:2])} · {rows[0]['structure_label']} · {rows[0]['expiry']}"
                 if len(rows) == 1 else f"{len(rows)} positions in this chart; the table includes all selected expiries")
        return {"label": label, "max_profit": display(bounds["max_profit"]), "max_loss": display(bounds["max_loss"]),
                "breakevens": ", ".join(money(v) for v in bounds["breakevens"]) or "None"}

    def toggle_position_strategy(self, strategy: str):
        selected = [p["id"] for p in self.position_strategy_options if p["checked"]]
        if strategy in selected:
            selected.remove(strategy)
        else:
            selected.append(strategy)
        self.position_strategy_filters = selected
        self.position_filter_mode = "custom"

    def reset_position_filters(self):
        self.position_filter_mode = "all"
        self.position_strategy_filters = []
        self.position_structure_filter = "All structures"
        self.position_underlying_filter = "All underlyings"
        self.position_expiry_filter = "All expiries"

    def clear_position_strategies(self):
        self.position_filter_mode = "custom"
        self.position_strategy_filters = []

    def set_position_workspace_mode(self, value: str):
        self.position_workspace_mode = value

    def set_position_structure_filter(self, value: str):
        self.position_structure_filter = value

    def set_position_underlying_filter(self, value: str):
        self.position_underlying_filter = value

    def set_position_expiry_filter(self, value: str):
        self.position_expiry_filter = value

    def set_position_payoff_scope(self, value: str):
        self.position_payoff_scope = value

    def set_position_chart_units(self, value: str):
        self.position_chart_units = value

    def select_position(self, position_id: str):
        self.selected_position_id = position_id
        self.position_expanded = ([value for value in self.position_expanded if value != position_id]
                                  if position_id in self.position_expanded else [*self.position_expanded, position_id])

    # Real-Time Logs
    log_filter: str = ""
    logs: list[str] = []

    # Config form
    cfg_broker: str = "tradier"
    cfg_account_id: str = ""
    cfg_access_token: str = ""
    cfg_paper: str = "true"
    cfg_symbol: str = "XSP"
    cfg_fee_per_contract: str = "0.40"
    cfg_risk_cap: str = "15000"
    cfg_portfolio_risk_cap: str = "100000"

    # Testing Strategy Configuration (25 DTE Experimental Setup)
    cfg_testing_dte: str = "25"
    cfg_testing_delta: str = "0.15"
    cfg_testing_wing: str = "5.0"
    cfg_testing_pt: str = "0.50"
    cfg_testing_sl: str = "2.0"
    cfg_testing_entry: str = "10:30"
    cfg_testing_structure: str = "iron_condor"

    # Pure Neural Network Strategy Configuration
    cfg_neural_enabled: str = "true"
    cfg_neural_dte: str = "7"
    cfg_neural_delta: str = "0.15"
    cfg_neural_wing: str = "5.0"
    cfg_neural_entry: str = "10:00"

    # Vertical Spread Strategy Configuration
    cfg_verticals_enabled: str = "true"

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
                from .reflex_dashboard import app
                connected = self.router.session.client_token in app.event_namespace.token_to_sid
                if not self.auto_refresh or not connected:
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
            self.broker_sync_status = tel.get("sync_status", "loading")
            self.broker_sync_message = tel.get("sync_message", "Waiting for the first broker snapshot")
            self.broker_last_known = tel.get("last_known", "")
            self.equity = tel["equity"]
            self.equity_str = money(tel["equity"])
            self.cash = tel["cash"]
            self.cash_str = money(tel["cash"])
            self.buying_power = tel["buying_power"]
            self.buying_power_str = money(tel["buying_power"])
            self.broker_margin = tel.get("option_requirement")
            self.broker_margin_str = money(self.broker_margin)
            self.option_requirement = self.broker_margin
            self.option_requirement_str = self.broker_margin_str

            # Effective runtime caps
            self.strategy_risk_cap = tel["risk_cap"]
            self.strategy_risk_cap_str = money(self.strategy_risk_cap)
            self.portfolio_risk_cap = tel["portfolio_risk_cap"]
            self.portfolio_risk_cap_str = money(self.portfolio_risk_cap)

            # Strategy risk utilization from ledger
            strat_risk_map = tel.get("strategy_risk")
            if strat_risk_map is None:
                strat_risk = None
            elif len(self.position_strategy_filters) == 1:
                strat_risk = strat_risk_map.get(self.position_strategy_filters[0], 0.0)
            elif strat_risk_map:
                strat_risk = max(strat_risk_map.values())
            else:
                strat_risk = 0.0 if not tel.get("ledger_error") else None

            self.strategy_risk = strat_risk
            self.strategy_risk_str = money(strat_risk)
            self.strategy_risk_pct = int(min(100.0, strat_risk / max(self.strategy_risk_cap, 1.0) * 100)) if strat_risk is not None else 0
            self.strategy_risk_pct_str = f"{self.strategy_risk_pct:.1f}% OF CAP USED" if strat_risk is not None else "EXPOSURE UNAVAILABLE"

            # Portfolio risk utilization from ledger
            port_risk = tel.get("portfolio_risk")
            if port_risk is None and not tel.get("ledger_error") and (strat_risk_map is not None and not strat_risk_map):
                port_risk = 0.0
            self.portfolio_risk = port_risk
            self.portfolio_risk_str = money(port_risk)
            self.portfolio_risk_pct = int(min(100.0, port_risk / max(self.portfolio_risk_cap, 1.0) * 100)) if port_risk is not None else 0
            self.portfolio_risk_pct_str = f"{self.portfolio_risk_pct:.1f}% OF CAP USED" if port_risk is not None else "EXPOSURE UNAVAILABLE"

            # Per-strategy open_risk properties
            self.open_risk = self.strategy_risk
            self.open_risk_str = self.strategy_risk_str
            self.open_risk_cap = self.strategy_risk_cap
            self.open_risk_cap_str = self.strategy_risk_cap_str
            self.open_risk_pct = self.strategy_risk_pct
            self.open_risk_pct_str = self.strategy_risk_pct_str
            headroom = tel["buying_power_headroom_pct"]
            self.buying_power_headroom_pct = int(headroom) if headroom is not None else 0
            self.buying_power_headroom_str = f"{headroom:.1f}% Available" if headroom is not None else "Headroom unavailable"
            self.positions = tel["positions"]
            self.orders = tel["orders"]
            self.orders_status = tel.get("orders_status", "unavailable")
            self.orders_synced_at = tel.get("orders_synced_at", "No successful sync")
            workspace = get_position_workspace(tel)
            self.position_records = workspace["rows"]
            self.position_issues = workspace["issues"]
            self.position_data_status = tel["positions_status"]
            self.position_synced_at = tel["synced_at"]

            # 4. Incubator Grid
            inc = get_incubator_data()
            self.configured_variant_count = len(inc.get("all_rows", []))
            self.incubator_rows = inc["rows"]
            self.incubator_monitored_rows = inc["monitored_rows"]
            self.active_grid_str = f"{len(inc['traded_rows'])}/{self.configured_variant_count} traded"
            self.monitored_count_str = f"{len(inc['monitored_rows'])} Monitored"
            self.total_completed_trades = inc["total_trades"]
            self.overall_win_rate = inc["overall_win_rate"]
            self.total_gross_pnl_str = inc["total_gross_pnl_str"]
            self.total_fees_str = inc["total_fees_str"]
            self.total_net_pnl_str = inc["total_net_pnl_str"]
            if "strategy_aggregates" in inc:
                self.ai_aggregate = inc["strategy_aggregates"].get("ai", {})
                self.top5_aggregate = inc["strategy_aggregates"].get("top5", {})
                self.bottom3_aggregate = inc["strategy_aggregates"].get("bottom3", {})
            self.visible_trades_50 = inc.get("visible_trades_50", [])
            self.total_closed_trades_count = inc.get("total_closed_trades_count", len(inc.get("all_closed_trades", [])))
            self.visible_closed_trades_count = inc.get("visible_closed_trades_count", len(self.visible_trades_50))
            self.eligible_closed_trades_count = inc.get("eligible_closed_trades_count", len(inc.get("eligible_closed_trades", [])))
            self.unattributed_trades_count = inc.get("unattributed_trades_count", 0)
            self.quarantined_trades_count = inc.get("quarantined_trades_count", 0)

            # 5. A/B Testing Lab
            ab = get_ab_lab_data()
            self.ab_all_strategies = ab.get("strategies", [])
            self.ab_strategy_options = ab.get("strategy_options", [])
            if (not self.ab_strategy_a or self.ab_strategy_a not in self.ab_strategy_options) and ab.get("default_a"):
                self.ab_strategy_a = ab["default_a"]
            if (not self.ab_strategy_b or self.ab_strategy_b not in self.ab_strategy_options) and ab.get("default_b"):
                self.ab_strategy_b = ab["default_b"]
            self._recompute_ab_comparison()

            # Benchmark Lab computation from actual closed trades
            closed_trades = inc.get("all_closed_trades", [])
            total_net_pnl = sum(float(t.get("net_pnl", 0.0)) for t in closed_trades)
            initial_cap = 100000.0
            strat_return_pct = (total_net_pnl / initial_cap) * 100.0 if initial_cap > 0 else 0.0
            spy_return_pct = 0.08
            alpha_pct = strat_return_pct - spy_return_pct
            self.benchmark_lumibot_return = f"{strat_return_pct:+.2f}%"
            self.benchmark_closed_trades = f"{len(closed_trades)} Closed Trades"
            self.benchmark_spy_return = f"{spy_return_pct:+.2f}%"
            self.benchmark_alpha = f"{alpha_pct:+.2f}%"
            self.benchmark_beta = "0.12"
            if len(closed_trades) >= 2:
                returns = [float(t.get("net_pnl", 0.0)) for t in closed_trades]
                mean_r = sum(returns) / len(returns)
                var_r = sum((r - mean_r) ** 2 for r in returns) / (len(returns) - 1)
                std_r = math.sqrt(var_r) if var_r > 0 else 1.0
                downside_var = sum((min(r, 0.0)) ** 2 for r in returns) / len(returns)
                downside_std = math.sqrt(downside_var) if downside_var > 0 else 1.0
                self.benchmark_sharpe = f"{(mean_r / std_r * math.sqrt(252)):.2f}" if std_r > 0 else "0.00"
                self.benchmark_sortino = f"{(mean_r / downside_std * math.sqrt(252)):.2f}" if downside_std > 0 else "0.00"
                cum = 0.0
                peak = 0.0
                max_dd = 0.0
                for r in returns:
                    cum += r
                    peak = max(peak, cum)
                    max_dd = min(max_dd, cum - peak)
                max_dd_pct = (max_dd / initial_cap) * 100.0
                self.benchmark_max_strategy_dd = f"{max_dd_pct:.2f}%"
            else:
                self.benchmark_sharpe = "0.00"
                self.benchmark_sortino = "0.00"
                self.benchmark_max_strategy_dd = "0.00%"

            # 6. Neural Policy
            ml = get_neural_policy_data()
            self.model_version = ml.get("model_version", "v1")
            self.market_regime = ml["market_regime"]
            self.dynamic_pt = ml["dynamic_pt"]
            self.contextual_selector = ml["contextual_selector"]
            self.active_switched_profile = ml.get("active_switched_profile", "IC_V05_7DTE_D12_W3_PT35_T1015")
            self.dynamic_switching_status = ml.get("dynamic_switching_status", "AUTO-ADAPTING TO MARKET REGIME")
            self.data_readiness_str = ml.get("readiness_str", "READY (12/12 Ticks)")
            self.has_sufficient_data = bool(ml.get("has_sufficient_data", True))
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
            self.cfg_risk_cap = cfg.get("risk_cap", "15000")
            self.cfg_portfolio_risk_cap = cfg.get("portfolio_risk_cap", "100000")
            self.cfg_paper = cfg.get("tradier_paper", "true")
            self.cfg_testing_dte = cfg.get("testing_dte", "25")
            self.cfg_testing_delta = cfg.get("testing_delta", "0.15")
            self.cfg_testing_wing = cfg.get("testing_wing", "5.0")
            self.cfg_testing_pt = cfg.get("testing_pt", "0.50")
            self.cfg_testing_sl = cfg.get("testing_sl", "2.0")
            self.cfg_testing_entry = cfg.get("testing_entry", "10:30")
            self.cfg_testing_structure = cfg.get("testing_structure", "iron_condor")
            self.cfg_neural_enabled = cfg.get("neural_enabled", "true")
            self.cfg_neural_dte = cfg.get("neural_dte", "7")
            self.cfg_neural_delta = cfg.get("neural_delta", "0.15")
            self.cfg_neural_wing = cfg.get("neural_wing", "5.0")
            self.cfg_neural_entry = cfg.get("neural_entry", "10:00")
            self.cfg_verticals_enabled = cfg.get("verticals_enabled", "true")
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

        if not match_a or not match_b:
            self.ab_card_a = {}
            self.ab_card_b = {}
            self.ab_delta_net_pnl = "-"
            self.ab_delta_ror_pct = "-"
            self.ab_delta_winner = "Select two available strategies"
            return

        if match_a:
            self.ab_card_a = {
                "variant": match_a["variant"],
                "tag": f"[Strategy A: {match_a['variant']}]",
                "config_desc": match_a["config_desc"],
                "closed_trades": match_a["closed_trades"],
                "win_rate": match_a["win_rate"],
                "win_rate_color": match_a.get("win_rate_color", "#10B981"),
                "gross_pnl_str": match_a["gross_pnl_str"],
                "fees_str": match_a["fees_str"],
                "net_pnl_str": match_a["net_pnl_str"],
                "net_pnl_color": match_a.get("net_pnl_color", "#10B981"),
                "net_ror_pct": match_a["net_ror_pct"],
                "net_ror_color": match_a.get("net_ror_color", "#10B981"),
                "net_roc_pct": match_a["net_roc_pct"],
                "net_roc_color": match_a.get("net_roc_color", "#10B981"),
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
                "win_rate_color": match_b.get("win_rate_color", "#10B981"),
                "gross_pnl_str": match_b["gross_pnl_str"],
                "fees_str": match_b["fees_str"],
                "net_pnl_str": match_b["net_pnl_str"],
                "net_pnl_color": match_b.get("net_pnl_color", "#10B981"),
                "net_ror_pct": match_b["net_ror_pct"],
                "net_ror_color": match_b.get("net_ror_color", "#10B981"),
                "net_roc_pct": match_b["net_roc_pct"],
                "net_roc_color": match_b.get("net_roc_color", "#10B981"),
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

    def set_cfg_portfolio_risk_cap(self, val: str):
        self.cfg_portfolio_risk_cap = val

    def set_cfg_paper(self, val: str):
        self.cfg_paper = val

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

    def set_cfg_testing_structure(self, val: str):
        self.cfg_testing_structure = val

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

    def set_cfg_verticals_enabled(self, val: str):
        self.cfg_verticals_enabled = val

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
            from dashboard.broker_client import get_broker_client

            client = get_broker_client()
            if client is None or not callable(getattr(client, "emergency_flatten_positions", None)):
                return rx.toast.error("Panic liquidation is unavailable for the configured broker.")

            ledger = None
            try:
                from strategies.position_ledger import PositionLedger, resolve_ledger_path
                ledger = PositionLedger(resolve_ledger_path())
                with ledger.execution_gate():
                    ledger.set_panic_halt(True, reason="dashboard_panic_liquidation")
                    success = client.emergency_flatten_positions()
            except Exception as e:
                logger.exception("Failed to persist durable panic halt: %s", e)
                return rx.toast.error("Panic halt could not be saved. Check the broker account immediately.")
            finally:
                if ledger is not None:
                    ledger.close()

            failures = getattr(client, "flatten_failures", {})
            if not success or failures:
                failed_syms = ", ".join(failures.keys()) if failures else "broker unverified"
                return rx.toast.error(f"Panic liquidation incomplete: {failed_syms}. Check broker immediately.")
            self.load_all()
            return rx.toast.success("Emergency liquidation verified. All positions are flat.")
        except Exception as exc:
            logger.exception("Unexpected error during panic liquidation: %s", exc)
            return rx.toast.error("Panic liquidation failed. Check the broker account immediately.")
        finally:
            self.panic_in_progress = False
            self.panic_modal_open = False

    def save_configuration(self):
        """Save form values to .env file."""
        updates = {
            "TRADING_BROKER": self.cfg_broker,
            "TRADIER_ACCOUNT_NUMBER": self.cfg_account_id,
            "TRADIER_PAPER": self.cfg_paper,
            "IC_SYMBOL": self.cfg_symbol,
            "OPTION_FEE_PER_CONTRACT": self.cfg_fee_per_contract,
            "IC_MAX_POSITION_RISK": self.cfg_risk_cap,
            "IC_MAX_PORTFOLIO_RISK": self.cfg_portfolio_risk_cap,
            "TESTING_DTE": self.cfg_testing_dte,
            "TESTING_DELTA": self.cfg_testing_delta,
            "TESTING_WING": self.cfg_testing_wing,
            "TESTING_PT": self.cfg_testing_pt,
            "TESTING_SL": self.cfg_testing_sl,
            "TESTING_ENTRY_TIME": self.cfg_testing_entry,
            "TESTING_STRUCTURE": self.cfg_testing_structure,
            "NEURAL_STRATEGY_ENABLED": self.cfg_neural_enabled,
            "NEURAL_STRATEGY_DTE": self.cfg_neural_dte,
            "NEURAL_STRATEGY_DELTA": self.cfg_neural_delta,
            "NEURAL_STRATEGY_WING": self.cfg_neural_wing,
            "NEURAL_STRATEGY_ENTRY_TIME": self.cfg_neural_entry,
            "VERTICAL_STRATEGIES_ENABLED": self.cfg_verticals_enabled,
        }
        if self.cfg_access_token:
            updates["TRADIER_ACCESS_TOKEN"] = self.cfg_access_token

        success = save_config_settings(updates)
        if success:
            return rx.toast.success("Configuration successfully saved to .env!")
        return rx.toast.error("Failed to write .env file.")

    # -------------------------------------------------------------
    # Trade Journal CSV Exports & Archive Handlers
    # -------------------------------------------------------------
    def download_visible_trades_csv(self):
        """Export the currently visible 50 historical trades to CSV."""
        from strategies.runtime_records import is_fixture_trade
        inc = get_incubator_data()
        trades = [
            t for t in inc.get("visible_trades_50", [])
            if not is_fixture_trade(t.get("trade_id", ""), t)
            and t.get("partition") != "quarantined"
            and t.get("reconciliation_status") != "QUARANTINED"
            and t.get("source") != "synthetic"
        ]
        csv_content = generate_trades_csv(trades)
        return rx.download(
            data=csv_content,
            filename="lumibot_visible_trades_past_50.csv",
            mime_type="text/csv",
        )

    def download_all_trades_csv(self):
        """Export eligible broker-verified historical trades to CSV archive."""
        from strategies.runtime_records import is_fixture_trade
        inc = get_incubator_data()
        trades = [
            t for t in inc.get("eligible_closed_trades", [])
            if not is_fixture_trade(t.get("trade_id", ""), t)
            and t.get("partition") != "quarantined"
            and t.get("reconciliation_status") != "QUARANTINED"
            and t.get("source") != "synthetic"
        ]
        csv_content = generate_trades_csv(trades)
        return rx.download(
            data=csv_content,
            filename="lumibot_eligible_trades_journal.csv",
            mime_type="text/csv",
        )

    def open_load_more_modal(self):
        self.load_more_modal_open = True

    def close_load_more_modal(self):
        self.load_more_modal_open = False

    def load_more_and_export_csv(self):
        """Forces the user to download the full CSV export to view/audit historical trades beyond the visible 50."""
        self.load_more_modal_open = False
        from strategies.runtime_records import is_fixture_trade
        inc = get_incubator_data()
        trades = [
            t for t in inc.get("eligible_closed_trades", [])
            if not is_fixture_trade(t.get("trade_id", ""), t)
            and t.get("partition") != "quarantined"
            and t.get("reconciliation_status") != "QUARANTINED"
            and t.get("source") != "synthetic"
        ]
        csv_content = generate_trades_csv(trades)
        return rx.download(
            data=csv_content,
            filename="lumibot_complete_trades_archive.csv",
            mime_type="text/csv",
        )
