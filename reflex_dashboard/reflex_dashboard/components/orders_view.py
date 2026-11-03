"""
Tab 6: Broker Orders & Execution Activity Stream.
Fully responsive across mobile, tablet, and desktop screens with horizontal table scrolling.
"""

import reflex as rx
from ..styles.numeric import numeric_color, numeric_text
from ..state import DashboardState
from ..styles.theme import (
    SURFACE_DARK,
    BORDER_COLOR,
    CYAN,
    GREEN,
    TEXT_MUTED,
    TEXT_PRIMARY,
    TABLE_HEADER_STYLE,
    TABLE_ROW_STYLE,
)


def orders_view() -> rx.Component:
    return rx.box(
        rx.vstack(
            rx.flex(
                rx.hstack(
                    rx.icon("clipboard-list", size=20, color=CYAN),
                    rx.heading("Recent Broker Orders & Activity", size="3", color=TEXT_PRIMARY, weight="bold"),
                    align="center",
                    spacing="2",
                ),
                rx.vstack(
                    rx.badge(DashboardState.orders_status, color_scheme="gray", variant="surface", size="1"),
                    rx.text(DashboardState.orders_synced_at, size="1", color=TEXT_MUTED),
                    align="end", spacing="1",
                ),
                direction={"initial": "column", "sm": "row"},
                justify="between",
                align={"initial": "start", "sm": "center"},
                width="100%",
                gap="2",
            ),
            rx.cond(
                DashboardState.orders.length() > 0,
                rx.box(
                    rx.table.root(
                        rx.table.header(
                            rx.table.row(
                                rx.table.column_header_cell("Order ID", style=TABLE_HEADER_STYLE, class_name="nowrap"),
                                rx.table.column_header_cell("Symbol", style=TABLE_HEADER_STYLE, class_name="nowrap"),
                                rx.table.column_header_cell("Side", style=TABLE_HEADER_STYLE, class_name="nowrap"),
                                rx.table.column_header_cell("Type", style=TABLE_HEADER_STYLE, class_name="nowrap"),
                                rx.table.column_header_cell("Qty", style=TABLE_HEADER_STYLE, class_name="nowrap"),
                                rx.table.column_header_cell("Status", style=TABLE_HEADER_STYLE, class_name="nowrap"),
                                rx.table.column_header_cell("Price", style=TABLE_HEADER_STYLE, class_name="nowrap"),
                            )
                        ),
                        rx.table.body(
                            rx.foreach(
                                DashboardState.orders,
                                lambda o: rx.table.row(
                                    rx.table.cell(rx.text(o["id"], color=CYAN, class_name="font-mono nowrap")),
                                    rx.table.cell(rx.text(o["symbol"], class_name="font-mono nowrap")),
                                    rx.table.cell(rx.text(o["side"], weight="bold", class_name="nowrap")),
                                    rx.table.cell(rx.badge(o["type"], color_scheme="gray", size="1")),
                                    rx.table.cell(rx.text(numeric_text(o["quantity"]), class_name="tabular-nums nowrap")),
                                    rx.table.cell(rx.badge(o["status"], color_scheme="jade", size="1")),
                                    rx.table.cell(rx.text(o["price"], class_name="tabular-nums nowrap")),
                                    style=TABLE_ROW_STYLE,
                                )
                            )
                        ),
                        width="100%",
                    ),
                    class_name="table-responsive-container",
                ),
                rx.center(
                    rx.vstack(
                        rx.icon("clipboard-list", size=32, color=TEXT_MUTED),
                        rx.text(rx.cond(DashboardState.orders_status == "ready",
                                        "The broker reported no recent orders in this snapshot.",
                                        "Current order data is unavailable. This does not confirm that pending orders are cleared."),
                                size="2", color=TEXT_MUTED),
                        spacing="2",
                        align="center",
                    ),
                    padding="30px",
                    width="100%",
                ),
            ),
            spacing="3",
            width="100%",
        ),
        padding={"initial": "14px", "sm": "20px"},
        background_color=SURFACE_DARK,
        border=f"1px solid {BORDER_COLOR}",
        border_radius="8px",
        width="100%",
    )
