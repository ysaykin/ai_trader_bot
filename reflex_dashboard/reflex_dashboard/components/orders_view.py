"""Broker execution and retained ledger reservations."""
import reflex as rx
from ..state import DashboardState
from ..styles.theme import CARD_STYLE, TEXT_MUTED, TABLE_HEADER_STYLE, TABLE_ROW_STYLE


def orders_view() -> rx.Component:
    return rx.vstack(
        rx.flex(
            rx.vstack(rx.heading("Orders and execution", size="5"),
                      rx.text("DAY orders leave pending at session close. Unresolved records remain available in reconciliation.", size="2", color=TEXT_MUTED),
                      align="start", spacing="1"),
            rx.select(["Pending orders", "Needs reconciliation", "All recent orders"], value=DashboardState.order_view_mode,
                      on_change=DashboardState.set_order_view_mode, aria_label="Order view"),
            justify="between", width="100%", wrap="wrap", gap="3"),
        rx.hstack(rx.badge(DashboardState.orders_status,
                           color_scheme=rx.cond(DashboardState.orders_status == "ready", "jade", "amber")),
                  rx.text("Broker snapshot: ", DashboardState.orders_synced_at, size="1", color=TEXT_MUTED), wrap="wrap"),
        rx.cond(DashboardState.orders_status != "ready",
                rx.callout("Broker orders are not current. Rows below retain the last known status and unresolved ledger reservations.", color_scheme="amber", width="100%")),
        rx.cond(DashboardState.ledger_warning != "",
                rx.callout(DashboardState.ledger_warning, color_scheme="amber", width="100%")),
        rx.cond(DashboardState.order_reconciliation_count > 0,
                rx.hstack(rx.text(DashboardState.order_reconciliation_count, " orders need reconciliation. Their reservations remain until broker confirmation.", size="2", color=TEXT_MUTED),
                          rx.button("View reconciliation", on_click=DashboardState.set_order_view_mode("Needs reconciliation"), variant="soft", size="1"),
                          wrap="wrap", align="center")),
        rx.box(rx.table.root(
            rx.table.header(rx.table.row(*[rx.table.column_header_cell(label, style=TABLE_HEADER_STYLE) for label in
                ["Order / position", "Strategy / intent", "Status", "Fill progress", "Limit", "Avg fill", "Evidence", "Updated ET"]])),
            rx.table.body(rx.foreach(DashboardState.visible_execution_orders, lambda order: rx.table.row(
                rx.table.cell(rx.vstack(rx.text(order["id"], size="2", class_name="font-mono"),
                    rx.cond(order["position_id"] != "Unknown",
                            rx.button("View position", on_click=DashboardState.inspect_order_position(order["position_id"], order["strategy"]), variant="ghost", size="1")),
                    align="start", spacing="1")),
                rx.table.cell(rx.vstack(rx.text(order["strategy_label"], size="1"), rx.text(order["intent"], size="1", color=TEXT_MUTED), align="start", spacing="1")),
                rx.table.cell(rx.badge(order["status"], color_scheme=order["color"])),
                rx.table.cell(rx.text(order["progress"], size="1")),
                rx.table.cell(order["price"]), rx.table.cell(order["average"]),
                rx.table.cell(rx.text(order["evidence"], size="1", color=TEXT_MUTED)),
                rx.table.cell(rx.text(order["updated"], size="1", color=TEXT_MUTED)), style=TABLE_ROW_STYLE))),
            width="100%", min_width="1000px"), class_name="table-responsive-container", width="100%", min_width="0", max_width="100%", style=CARD_STYLE),
        rx.cond(DashboardState.visible_execution_orders.length() == 0,
                rx.callout(rx.cond(DashboardState.orders_status == "ready",
                    rx.cond(DashboardState.order_view_mode == "Pending orders",
                            "No pending orders for the current session. Ended sessions remain in reconciliation.",
                            "No orders match this view in the available evidence."),
                    "Order evidence is unavailable. This does not confirm there are no pending orders."), color_scheme="gray", width="100%")),
        rx.text("Session close uses the exchange calendar, including holidays and early closes. A session ending does not establish a fill or cancellation. GTC orders stay pending until the broker resolves them.",
                size="1", color=TEXT_MUTED), spacing="4", width="100%", align="stretch")
