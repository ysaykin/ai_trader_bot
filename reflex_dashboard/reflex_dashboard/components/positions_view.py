"""Grouped positions, current strategy comparison and actual-leg payoff analysis."""
import reflex as rx
from ..styles.numeric import numeric_color, numeric_text
from typing import Any
from ..state import DashboardState
from ..styles.theme import SURFACE_DARK, BORDER_COLOR, CYAN, TEXT_MUTED, TEXT_PRIMARY, TABLE_HEADER_STYLE


def numeric(value: rx.Var[dict[str, str]]) -> rx.Component:
    return rx.text(value["text"], color=value["color"], class_name="tabular-nums nowrap", weight="medium")


def metric(label: str, value: rx.Var[dict[str, str]]) -> rx.Component:
    return rx.vstack(rx.text(label, size="1", color=TEXT_MUTED), numeric(value), spacing="1", align="start")


def selector(options, value, on_change, label: str) -> rx.Component:
    return rx.select.root(
        rx.select.trigger(aria_label=label, placeholder=label),
        rx.select.content(rx.foreach(options, lambda option: rx.select.item(option, value=option))),
        value=value, on_change=on_change,
    )


def legs_table(rows: rx.Var[list[dict[str, str]]]) -> rx.Component:
    def leg(row: rx.Var[dict[str, str]]) -> rx.Component:
        return rx.table.row(
            rx.table.cell(rx.text(row["symbol"], class_name="font-mono nowrap")),
            rx.table.cell(rx.text(numeric_text(row["quantity"]), color=numeric_color(row["quantity"]))),
            *[rx.table.cell(rx.text(row[f"{key}_text"], color=row[f"{key}_color"], class_name="tabular-nums nowrap"))
              for key in ("cost", "mark", "pnl", "delta", "theta")],
            rx.table.cell(rx.text(row["quote_time"], size="1", color=TEXT_MUTED, class_name="nowrap")),
        )
    return rx.box(
        rx.table.root(
            rx.table.header(rx.table.row(*[rx.table.column_header_cell(label, style=TABLE_HEADER_STYLE)
                for label in ("Contract", "Signed qty", "Remaining basis", "Mark", "Open P&L", "Delta (shares)", "Theta ($/day)", "Quote as of")])),
            rx.table.body(rx.foreach(rows, leg)), width="100%",
        ), class_name="table-responsive-container", width="100%", min_width="0", max_width="100%",
    )


def position_card(row: rx.Var[dict[str, Any]]) -> rx.Component:
    return rx.box(
        rx.flex(
            rx.vstack(
                rx.button(row["underlying"], " · ", row["structure_label"],
                          on_click=DashboardState.select_position(row["id"].to(str)),
                          variant="ghost", color_scheme="cyan", cursor="pointer", justify="start"),
                rx.text(row["expiry"], " · ", row["dte_label"], " · Qty ", row["quantity"].to(str), size="1", color=TEXT_MUTED),
                spacing="1", align="start", min_width="0", flex="1",
            ),
            rx.hstack(rx.badge(row["status"], color_scheme="gray"),
                      rx.badge(row["quality"], color_scheme=rx.cond(row["reconciled"].to(bool), "gray", "amber")), spacing="2"),
            justify="between", align="start", gap="8px", wrap="wrap", width="100%",
        ),
        rx.grid(
            metric("Open P&L", row["open_pnl_display"].to(dict[str, str])),
            metric("Realized portions", row["realized_pnl_display"].to(dict[str, str])),
            metric(row["fee_label"].to(str), row["fees_display"].to(dict[str, str])),
            metric("Net since opening", row["net_pnl_display"].to(dict[str, str])),
            columns={"initial": "2", "md": "4"}, gap="12px", margin_top="12px", width="100%",
        ),
        rx.button(rx.cond(DashboardState.position_expanded.contains(row["id"]), "Hide legs", "Show legs & analyze"),
                  on_click=DashboardState.select_position(row["id"].to(str)),
                  aria_expanded=DashboardState.position_expanded.contains(row["id"]), variant="ghost", size="1", margin_top="8px"),
        rx.cond(DashboardState.position_expanded.contains(row["id"]),
            rx.vstack(
                rx.text("Trade ", row["trade_id"], size="1", color=TEXT_MUTED, overflow_wrap="anywhere"),
                rx.hstack(metric("Opening defined risk, before fees", row["opening_risk_display"].to(dict[str, str])),
                          metric("Net return on opening risk", row["return_pct_display"].to(dict[str, str])), gap="20px", wrap="wrap"),
                rx.flex(
                    metric("Remaining entry cash (+ credit / - debit)", row["remaining_entry_cash_display"].to(dict[str, str])),
                    metric("Signed liquidation value", row["liquidation_value_display"].to(dict[str, str])),
                    metric("Position delta (shares)", row["delta_display"].to(dict[str, str])),
                    metric("Position theta ($/day)", row["theta_display"].to(dict[str, str])),
                    gap="20px", wrap="wrap", width="100%",
                ),
                legs_table(row["leg_rows"].to(list[dict[str, str]])),
                spacing="3", width="100%", padding_top="12px",
            )),
        padding="14px 16px", background_color=SURFACE_DARK,
        border=f"1px solid {BORDER_COLOR}", border_radius="8px", width="100%", min_width="0", max_width="100%",
    )


def strategy_group(group: rx.Var[dict[str, Any]]) -> rx.Component:
    return rx.vstack(
        rx.flex(rx.tooltip(rx.heading(group["label"], size="3"), content=group["strategy"].to(str)),
                numeric(group["net"].to(dict[str, str])), justify="between", gap="12px", align="center", width="100%"),
        rx.foreach(group["rows"].to(list[dict[str, Any]]), position_card),
        spacing="3", width="100%", min_width="0", max_width="100%", margin_top="12px",
    )


def comparison_table() -> rx.Component:
    def row(item: rx.Var[dict[str, Any]]) -> rx.Component:
        return rx.table.row(
            rx.table.cell(rx.text(item["strategy"], size="1", overflow_wrap="anywhere")),
            rx.table.cell(item["count"]),
            *[rx.table.cell(numeric(item[key].to(dict[str, str]))) for key in
              ("open_pnl", "realized_pnl", "fees", "net_pnl", "opening_risk", "return_pct")],
            rx.table.cell(rx.text(item["fee_label"], size="1", color=TEXT_MUTED)),
        )
    return rx.box(rx.table.root(
        rx.table.header(rx.table.row(*[rx.table.column_header_cell(label, style=TABLE_HEADER_STYLE) for label in
            ("Strategy", "Positions", "Open P&L", "Realized portions", "Fees", "Net since opening", "Opening risk", "Return on risk", "Fee source")])),
        rx.table.body(rx.foreach(DashboardState.position_comparison_rows, row)), width="100%",
    ), class_name="table-responsive-container", width="100%", min_width="0", max_width="100%")


def positions_view() -> rx.Component:
    return rx.vstack(
        rx.flex(
            rx.vstack(rx.heading("Positions", size="5"),
                      rx.text("Whole trades, combined P&L and strategy comparison", size="2", color=TEXT_MUTED), align="start", spacing="1"),
            rx.vstack(rx.badge(rx.cond(DashboardState.position_data_status == "ready", "Broker snapshot",
                                      rx.cond(DashboardState.position_data_status == "loading", "Loading", "Broker unavailable")), color_scheme="gray"),
                      rx.text(DashboardState.position_synced_at, size="1", color=TEXT_MUTED), align="end", spacing="1"),
            justify="between", gap="12px", wrap="wrap", width="100%",
        ),
        rx.foreach(DashboardState.position_issues, lambda issue: rx.callout(issue, color_scheme="amber", size="1", width="100%")),
        rx.flex(
            selector(["Positions", "Compare strategies"], DashboardState.position_workspace_mode,
                     DashboardState.set_position_workspace_mode, "Position view"),
            selector(["All structures", "Iron condor", "Bull put vertical", "Bear call vertical", "Adjusted / incomplete", "Unattributed contract"],
                     DashboardState.position_structure_filter, DashboardState.set_position_structure_filter, "Structure filter"),
            selector(DashboardState.position_underlying_options, DashboardState.position_underlying_filter,
                     DashboardState.set_position_underlying_filter, "Underlying filter"),
            selector(DashboardState.position_expiry_options, DashboardState.position_expiry_filter,
                     DashboardState.set_position_expiry_filter, "Expiration filter"),
            gap="8px", wrap="wrap", width="100%",
        ),
        rx.flex(
            rx.text("Strategies", size="2", color=TEXT_MUTED),
            rx.foreach(DashboardState.position_strategy_options, lambda option: rx.tooltip(
                rx.checkbox(option["label"].to(str), checked=option["checked"].to(bool),
                            on_change=lambda _: DashboardState.toggle_position_strategy(option["id"].to(str))),
                content=option["id"].to(str))),
            rx.button("All", on_click=DashboardState.reset_position_filters, variant="ghost", size="1"),
            rx.button("Clear", on_click=DashboardState.clear_position_strategies, variant="ghost", size="1"),
            gap="12px", wrap="wrap", align="center", width="100%",
        ),
        rx.flex(
            rx.vstack(rx.text(DashboardState.position_summary["count"], size="3", weight="medium"),
                      rx.text(DashboardState.position_summary["coverage"], size="1", color=TEXT_MUTED), align="start", spacing="1"),
            metric("Filtered open P&L", DashboardState.position_summary["open"].to(dict[str, str])),
            metric("Filtered net since opening", DashboardState.position_summary["net"].to(dict[str, str])),
            rx.text(DashboardState.position_summary["fee_note"], size="1", color=TEXT_MUTED),
            gap="20px", wrap="wrap", align="center", padding="14px 0", width="100%",
            border_bottom=f"1px solid {BORDER_COLOR}",
        ),
        rx.cond(DashboardState.filtered_position_records.length() > 0,
            rx.cond(DashboardState.position_workspace_mode == "Positions",
                    rx.foreach(DashboardState.position_groups, strategy_group),
                    rx.vstack(comparison_table(),
                              rx.text("Return on risk divides net P&L by defined risk committed at entry, including additions and linked rolls. Fees reduce P&L.", size="1", color=TEXT_MUTED),
                              spacing="2", width="100%")),
            rx.callout(rx.cond(DashboardState.position_data_status == "ready",
                              "No positions match the selected filters.",
                              "Broker positions are unavailable. Refresh to retry; this does not confirm an empty account."), color_scheme="gray", width="100%")),
        rx.divider(),
        rx.flex(rx.heading("Expiration payoff", size="4"),
                selector(DashboardState.position_scope_options, DashboardState.effective_position_scope,
                         DashboardState.set_position_payoff_scope, "Payoff underlying and expiry"),
                selector(["Dollars", "% of opening risk"], DashboardState.position_chart_units,
                         DashboardState.set_position_chart_units, "Payoff chart units"),
                gap="12px", wrap="wrap", align="center", width="100%"),
        rx.text("Projected total P&L at the selected expiry, including realized portions and fees as shown. Current marked results appear above. Mixed expiries and unmatched inventory are excluded.", size="1", color=TEXT_MUTED),
        rx.text(DashboardState.position_chart_info["label"], size="2", color=TEXT_PRIMARY),
        rx.cond(DashboardState.position_workspace_mode == "Positions",
                rx.flex(metric("Maximum profit at expiry", DashboardState.position_chart_info["max_profit"].to(dict[str, str])),
                        metric("Maximum loss at expiry", DashboardState.position_chart_info["max_loss"].to(dict[str, str])),
                        rx.vstack(rx.text("Breakevens after fees", size="1", color=TEXT_MUTED),
                                  rx.text(DashboardState.position_chart_info["breakevens"], class_name="tabular-nums"), align="start", spacing="1"),
                        gap="20px", wrap="wrap", width="100%")),
        rx.cond(DashboardState.effective_position_scope != "",
                rx.box(rx.plotly(data=DashboardState.position_payoff_figure,
                                 use_resize_handler=True, config={"responsive": True, "displaylogo": False},
                                 width="100%", height="350px"),
                       width="100%", min_width="0", max_width="100%", overflow="hidden"),
                rx.callout("Select a reconciled, defined-risk position to view its expiration payoff.", color_scheme="gray", width="100%")),
        spacing="4", width="100%", min_width="0",
    )
