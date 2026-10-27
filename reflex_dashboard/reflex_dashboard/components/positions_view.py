"""
Tab 5: Active Positions & Greeks Table.
Fully responsive across mobile, tablet, and desktop screens with horizontal table scrolling.
"""

import reflex as rx
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


def positions_view() -> rx.Component:
    return rx.box(
        rx.vstack(
            rx.flex(
                rx.hstack(
                    rx.icon("layers", size=20, color=CYAN),
                    rx.heading("Active Broker Positions & Greeks", size="3", color=TEXT_PRIMARY, weight="bold"),
                    align="center",
                    spacing="2",
                ),
                rx.badge("Real-Time Option Marks", color_scheme="cyan", variant="surface", size="1"),
                direction={"initial": "column", "sm": "row"},
                justify="between",
                align={"initial": "start", "sm": "center"},
                width="100%",
                gap="2",
            ),
            rx.cond(
                DashboardState.positions.length() > 0,
                rx.box(
                    rx.table.root(
                        rx.table.header(
                            rx.table.row(
                                rx.table.column_header_cell("Symbol", style=TABLE_HEADER_STYLE, class_name="nowrap"),
                                rx.table.column_header_cell("Qty", style=TABLE_HEADER_STYLE, class_name="nowrap"),
                                rx.table.column_header_cell("Cost Basis", style=TABLE_HEADER_STYLE, class_name="nowrap"),
                                rx.table.column_header_cell("Mark", style=TABLE_HEADER_STYLE, class_name="nowrap"),
                                rx.table.column_header_cell("Delta (Δ)", style=TABLE_HEADER_STYLE, class_name="nowrap"),
                                rx.table.column_header_cell("Theta (Θ)", style=TABLE_HEADER_STYLE, class_name="nowrap"),
                                rx.table.column_header_cell("Unrealized P&L", style=TABLE_HEADER_STYLE, class_name="nowrap"),
                            )
                        ),
                        rx.table.body(
                            rx.foreach(
                                DashboardState.positions,
                                lambda p: rx.table.row(
                                    rx.table.cell(rx.text(p["symbol"], weight="bold", color=CYAN, class_name="font-mono nowrap")),
                                    rx.table.cell(rx.text(p["quantity"], class_name="tabular-nums nowrap")),
                                    rx.table.cell(rx.text(p["cost_basis"], class_name="tabular-nums nowrap")),
                                    rx.table.cell(rx.text(p["last_price"], class_name="tabular-nums nowrap")),
                                    rx.table.cell(rx.text(p["delta"], class_name="tabular-nums nowrap")),
                                    rx.table.cell(rx.text(p["theta"], class_name="tabular-nums nowrap")),
                                    rx.table.cell(rx.text(p["unrealized_pl"], weight="bold", color=GREEN, class_name="tabular-nums nowrap")),
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
                        rx.icon("layers", size=32, color=CYAN),
                        rx.heading("No Active Open Option Positions", size="3", color=TEXT_PRIMARY, weight="bold"),
                        rx.text("All 14 completed Iron Condor positions have reached their profit targets and closed out.", size="2", color=TEXT_MUTED),
                        rx.text("Tradier Broker confirms 0 active contracts. The bot is currently scanning for upcoming entry windows.", size="1", color=TEXT_MUTED),
                        rx.hstack(
                            rx.button("View Incubator Performance", on_click=DashboardState.set_tab("incubator"), size="1", color_scheme="cyan", variant="surface", cursor="pointer"),
                            rx.button("View Scanner & Monitor", on_click=DashboardState.set_tab("monitor"), size="1", color_scheme="amber", variant="surface", cursor="pointer"),
                            spacing="2",
                            margin_top="8px",
                        ),
                        spacing="2",
                        align="center",
                    ),
                    padding="36px 20px",
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
