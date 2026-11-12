"""Daily operations, evidence-backed performance and input health."""
import reflex as rx

from ..state import DashboardState
from ..styles.numeric import numeric_color
from ..styles.theme import CARD_STYLE, TEXT_MUTED, TABLE_HEADER_STYLE, TABLE_ROW_STYLE
from .logs_view import logs_view
from .monitor_view import monitor_view
from .neural_view import neural_view


def panel(title: str, *children) -> rx.Component:
    return rx.vstack(rx.heading(title, size="4"), *children, spacing="3", width="100%", align="stretch", style=CARD_STYLE)


def table(headers: list[str], rows, render) -> rx.Component:
    return rx.box(rx.table.root(
        rx.table.header(rx.table.row(*[rx.table.column_header_cell(label, style=TABLE_HEADER_STYLE) for label in headers])),
        rx.table.body(rx.foreach(rows, render)), width="100%", min_width="900px"),
        class_name="table-responsive-container", width="100%", min_width="0", max_width="100%")


def strategy_table() -> rx.Component:
    return table(["Strategy", "Entry constraints", "Open", "Pending", "Closed", "Win rate", "Closed net P&L", "Open P&L", "Results"],
        DashboardState.strategy_result_rows, lambda row: rx.table.row(
            rx.table.cell(rx.vstack(rx.text(row["name"], weight="bold"), rx.text(row["entry"], size="1", color=TEXT_MUTED), spacing="1", align="start")),
            rx.table.cell(rx.badge(row["status"], color_scheme=row["color"])),
            rx.table.cell(row["positions"]), rx.table.cell(row["orders"]), rx.table.cell(row["closed"]), rx.table.cell(row["win_rate"]),
            rx.table.cell(rx.text(row["net"], color=numeric_color(row["net"]), class_name="tabular-nums")),
            rx.table.cell(rx.text(row["open"], color=numeric_color(row["open"]), class_name="tabular-nums")),
            rx.table.cell(rx.text(row["outcome"], size="1", color=TEXT_MUTED)), style=TABLE_ROW_STYLE))


def health_table() -> rx.Component:
    return table(["Input", "Provider", "Value", "Status", "Received ago", "Provider age", "Last evidence / error"],
        DashboardState.health_rows, lambda row: rx.table.row(
            rx.table.cell(rx.text(row["name"], weight="bold")), rx.table.cell(row["source"]), rx.table.cell(row["value"]),
            rx.table.cell(rx.badge(row["status"], color_scheme=row["color"])),
            rx.table.cell(row["received"]), rx.table.cell(row["provider_age"]),
            rx.table.cell(rx.text(row["detail"], size="1", color=TEXT_MUTED)), style=TABLE_ROW_STYLE))


def overview_view() -> rx.Component:
    return rx.vstack(
        rx.flex(rx.vstack(rx.heading("Trading overview", size="6"),
                          rx.text("Positions, execution and verified results in one place.", color=TEXT_MUTED, size="2"), align="start", spacing="1"),
                rx.button("Refresh", on_click=DashboardState.load_all, variant="soft"),
                justify="between", align="center", width="100%", wrap="wrap", gap="3"),
        rx.grid(rx.foreach(DashboardState.overview_metrics, lambda metric: rx.vstack(
            rx.text(metric["label"], size="2", color=TEXT_MUTED),
            rx.heading(metric["value"], size="7", color=metric["color"], class_name="tabular-nums"),
            rx.text(metric["note"], size="1", color=TEXT_MUTED), align="start", spacing="2", style=CARD_STYLE)),
            columns={"initial": "1", "sm": "2", "lg": "4"}, spacing="3", width="100%"),
        panel("Needs attention", rx.cond(DashboardState.attention_items.length() > 0,
            rx.vstack(rx.foreach(DashboardState.attention_items[:4], lambda item: rx.callout(item, color_scheme="amber", width="100%")),
                rx.cond(DashboardState.attention_items.length() > 4, rx.accordion.root(
                    rx.accordion.item(header="More observations", content=rx.vstack(
                        rx.foreach(DashboardState.attention_items[4:], lambda item: rx.text(item, size="2", color=TEXT_MUTED)),
                        spacing="2", width="100%"), value="issues"), type="single", collapsible=True, width="100%")),
                spacing="2", width="100%"),
            rx.text(rx.cond(DashboardState.overview_metrics.length() > 0,
                            "No issues found in the latest dashboard observations.",
                            "Waiting for the first monitoring snapshot."), color=TEXT_MUTED)),
            rx.hstack(rx.button("Review orders", on_click=[DashboardState.set_order_view_mode("Needs reconciliation"), DashboardState.set_tab("orders")], variant="soft"),
                      rx.button("Inspect data health", on_click=DashboardState.set_tab("system"), variant="soft"), wrap="wrap", spacing="2")),
        panel("Latest engine activity", rx.hstack(rx.badge(DashboardState.bot_state, color_scheme=DashboardState.bot_badge_color),
              rx.text(DashboardState.bot_timestamp, size="1", color=TEXT_MUTED), wrap="wrap"),
              rx.text(DashboardState.bot_last_action, size="2"),
              rx.text("Log activity confirms an observation, not process liveness. Strategy observation: ", DashboardState.snapshot_time, size="1", color=TEXT_MUTED)),
        panel("Strategy results", rx.text(DashboardState.performance_scope, size="1", color=TEXT_MUTED), strategy_table(),
              rx.text("Closed P&L includes only verified completed trades and recorded fees. Open P&L uses available marks before fees. These totals are kept separate.", size="1", color=TEXT_MUTED)),
        spacing="4", width="100%", align="stretch")


def strategies_view() -> rx.Component:
    return rx.vstack(
        panel("Strategy performance", rx.text("Configured entry strategies. Entry constraints come from the ledger and configuration; observed actions appear below.", size="2", color=TEXT_MUTED),
              strategy_table()),
        panel("Recent closed trade evidence", rx.text(DashboardState.performance_scope, size="1", color=TEXT_MUTED),
              table(["Strategy", "Trade", "Evidence", "Entered ET", "Exited ET", "Gross", "Fees", "Net"],
                    DashboardState.visible_trades_50, lambda row: rx.table.row(
                        rx.table.cell(row["strategy_label"]), rx.table.cell(rx.text(row["trade_id"], size="1")),
                        rx.table.cell(rx.badge(row["reconciliation_status"],
                                              color_scheme=rx.cond(row["is_eligible"].to(bool), "jade", "amber"))),
                        rx.table.cell(row["time_str"]), rx.table.cell(row["exit_time_str"]),
                        rx.table.cell(row["gross_pnl_str"]), rx.table.cell(row["fees_str"]),
                        rx.table.cell(rx.text(row["net_pnl_str"], color=numeric_color(row["net_pnl_str"]))), style=TABLE_ROW_STYLE)),
              rx.cond(DashboardState.visible_trades_50.length() == 0,
                      rx.text("No closed trade evidence to display.", size="2", color=TEXT_MUTED)),
              rx.button("Export trade evidence CSV", on_click=DashboardState.download_all_trades_csv, variant="soft")),
        panel("Latest observed strategy actions", rx.text(DashboardState.snapshot_time, size="1", color=TEXT_MUTED),
              table(["Strategy", "Recorded action", "Observed at"], DashboardState.strategy_decisions,
                    lambda row: rx.table.row(rx.table.cell(row["name"]), rx.table.cell(row["action"]), rx.table.cell(row["observed"]), style=TABLE_ROW_STYLE)),
              rx.text("Recorded actions may be historical. An action does not confirm an order or fill; use Orders and Positions for execution evidence.", size="1", color=TEXT_MUTED)),
        spacing="4", width="100%", align="stretch")


def system_view() -> rx.Component:
    return rx.vstack(
        panel("Broker and market data health", health_table(),
              rx.text("Received age measures our cache. Provider age measures the last trade supplied by the provider. A recent response can still contain delayed data.", size="2", color=TEXT_MUTED),
              rx.text("VIX and VIX9D currently use Tradier. Alpaca does not supply spot index data. Missing inputs remain unavailable.", size="2", color=TEXT_MUTED)),
        panel("Inputs observed by the strategy", rx.text("Latest observation: ", DashboardState.snapshot_time, size="1", color=TEXT_MUTED),
              table(["Feature", "Observed value", "Status", "Observation age"], DashboardState.feature_rows,
                    lambda row: rx.table.row(rx.table.cell(row["name"]), rx.table.cell(row["value"]),
                                            rx.table.cell(rx.badge(row["status"], color_scheme=row["color"])), rx.table.cell(row["age"]), style=TABLE_ROW_STYLE)),
              rx.text(DashboardState.data_readiness_str, size="2"),
              rx.text("Warmup counts unique, complete snapshots. Missing feature values are reported separately.", size="1", color=TEXT_MUTED)),
        rx.accordion.root(
            rx.accordion.item(header="Entry schedules and capacity", content=monitor_view(), value="schedule"),
            rx.accordion.item(header="Neural policy diagnostics", content=rx.vstack(
                rx.text(DashboardState.dynamic_switching_status, size="2", color=TEXT_MUTED),
                rx.cond(DashboardState.policy_available, neural_view(),
                        rx.callout("No model recommendation is available. Model probabilities and performance are unavailable.", color_scheme="gray")), width="100%"), value="policy"),
            rx.accordion.item(header="Engine logs", content=logs_view(), value="logs"),
            type="multiple", width="100%"),
        spacing="4", width="100%", align="stretch")
