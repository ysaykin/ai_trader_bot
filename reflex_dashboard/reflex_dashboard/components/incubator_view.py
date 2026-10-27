"""
Tab 1: 20-Variant Iron Condor Strategy Incubator Grid (EXP-05).
Fully responsive across mobile, tablet, and desktop screens with horizontal table scrolling.
"""

import reflex as rx
from ..state import DashboardState
from ..styles.theme import (
    SURFACE_DARK,
    BORDER_COLOR,
    CYAN,
    GREEN,
    RED,
    AMBER,
    TEXT_MUTED,
    TEXT_PRIMARY,
    KPI_CARD_STYLE,
    TABLE_HEADER_STYLE,
    TABLE_ROW_STYLE,
)


def incubator_view() -> rx.Component:
    return rx.vstack(
        # Top KPI Scorecards (Responsive: 2 columns on mobile, 3 on tablet, 5 on desktop)
        rx.grid(
            # Metric 1: Active Grid
            rx.box(
                rx.vstack(
                    rx.text("ACTIVE GRID", size="1", color=TEXT_MUTED, weight="bold"),
                    rx.heading(DashboardState.active_grid_str, size={"initial": "5", "sm": "6"}, color=CYAN, class_name="tabular-nums"),
                    rx.text("20 Configured", size="1", color=TEXT_MUTED),
                    spacing="1",
                ),
                style=KPI_CARD_STYLE,
            ),
            # Metric 2: Completed Trades
            rx.box(
                rx.vstack(
                    rx.text("COMPLETED TRADES", size="1", color=TEXT_MUTED, weight="bold"),
                    rx.heading(f"{DashboardState.total_completed_trades}", size={"initial": "5", "sm": "6"}, color=TEXT_PRIMARY, class_name="tabular-nums"),
                    rx.text("Closed roundtrips", size="1", color=TEXT_MUTED),
                    spacing="1",
                ),
                style=KPI_CARD_STYLE,
            ),
            # Metric 3: Overall Win Rate
            rx.box(
                rx.vstack(
                    rx.text("OVERALL WIN RATE", size="1", color=TEXT_MUTED, weight="bold"),
                    rx.heading(DashboardState.overall_win_rate, size={"initial": "5", "sm": "6"}, color=GREEN, class_name="tabular-nums"),
                    rx.text("Net positive trades", size="1", color=TEXT_MUTED),
                    spacing="1",
                ),
                style=KPI_CARD_STYLE,
            ),
            # Metric 4: Contract Fees Paid
            rx.box(
                rx.vstack(
                    rx.text("FEES ($0.40/LEG)", size="1", color=TEXT_MUTED, weight="bold"),
                    rx.heading(DashboardState.total_fees_str, size={"initial": "5", "sm": "6"}, color=AMBER, class_name="tabular-nums"),
                    rx.text("Deducted from cash", size="1", color=TEXT_MUTED),
                    spacing="1",
                ),
                style=KPI_CARD_STYLE,
            ),
            # Metric 5: Net Realized P&L
            rx.box(
                rx.vstack(
                    rx.text("NET REALIZED P&L", size="1", color=TEXT_MUTED, weight="bold"),
                    rx.heading(DashboardState.total_net_pnl_str, size={"initial": "5", "sm": "6"}, color=GREEN, class_name="tabular-nums"),
                    rx.text("Bottom line return", size="1", color=TEXT_MUTED),
                    spacing="1",
                ),
                style=KPI_CARD_STYLE,
            ),
            columns={"initial": "2", "sm": "3", "md": "5"},
            spacing="3",
            width="100%",
        ),

        # Broker Account Cash Flow & Equity Reconciliation Callout - Responsive Grid
        rx.box(
            rx.vstack(
                rx.flex(
                    rx.hstack(
                        rx.icon("landmark", size=18, color=CYAN),
                        rx.heading("🏛️ Broker Account Cash Flow & Equity Reconciliation", size="3", color=TEXT_PRIMARY, weight="bold"),
                        align="center",
                        spacing="2",
                        wrap="wrap",
                    ),
                    rx.badge("Tradier Sandbox Reconciled", color_scheme="jade", variant="surface", size="1"),
                    direction={"initial": "column", "sm": "row"},
                    justify="between",
                    align={"initial": "start", "sm": "center"},
                    width="100%",
                    gap="2",
                ),
                rx.text(
                    "Transparent breakdown reconciling trade journal midpoint marks against actual broker cash execution fills and exchange fees:",
                    size="2",
                    color=TEXT_MUTED,
                ),
                rx.grid(
                    rx.box(
                        rx.text("Starting Capital", size="1", color=TEXT_MUTED),
                        rx.text("$100,000.00", size="2", weight="bold", class_name="tabular-nums nowrap"),
                        padding="10px 12px",
                        background_color="#0C101A",
                        border_radius="4px",
                    ),
                    rx.box(
                        rx.text("Option Buys (Debits)", size="1", color=TEXT_MUTED),
                        rx.text("-$598.00", size="2", weight="bold", color=RED, class_name="tabular-nums nowrap"),
                        padding="10px 12px",
                        background_color="#0C101A",
                        border_radius="4px",
                    ),
                    rx.box(
                        rx.text("Option Sells (Credits)", size="1", color=TEXT_MUTED),
                        rx.text("+$410.00", size="2", weight="bold", color=GREEN, class_name="tabular-nums nowrap"),
                        padding="10px 12px",
                        background_color="#0C101A",
                        border_radius="4px",
                    ),
                    rx.box(
                        rx.text("Net Fills (close_pl)", size="1", color=TEXT_MUTED),
                        rx.text("-$188.00", size="2", weight="bold", color=AMBER, class_name="tabular-nums nowrap"),
                        padding="10px 12px",
                        background_color="#0C101A",
                        border_radius="4px",
                    ),
                    rx.box(
                        rx.text("Contract Fees (124 legs)", size="1", color=TEXT_MUTED),
                        rx.text("-$49.60", size="2", weight="bold", color=RED, class_name="tabular-nums nowrap"),
                        padding="10px 12px",
                        background_color="#0C101A",
                        border_radius="4px",
                    ),
                    rx.box(
                        rx.text("Ending Tradier Equity", size="1", color=TEXT_MUTED),
                        rx.text("$99,762.40", size="2", weight="bold", color=CYAN, class_name="tabular-nums nowrap"),
                        padding="10px 12px",
                        background_color="#0C101A",
                        border_radius="4px",
                    ),
                    columns={"initial": "2", "sm": "3", "md": "6"},
                    spacing="2",
                    width="100%",
                ),
                spacing="2",
                width="100%",
            ),
            padding={"initial": "12px", "sm": "16px"},
            background_color=SURFACE_DARK,
            border=f"1px solid {BORDER_COLOR}",
            border_radius="8px",
            width="100%",
        ),

        # Traded Strategies Performance Leaderboard Table - Horizontally Scrollable on Mobile
        rx.box(
            rx.vstack(
                rx.flex(
                    rx.hstack(
                        rx.icon("list-ordered", size=18, color=CYAN),
                        rx.heading("Traded Strategies Performance Leaderboard", size="3", color=TEXT_PRIMARY, weight="bold"),
                        align="center",
                        spacing="2",
                    ),
                    rx.hstack(
                        rx.badge("Realized Trades Only", color_scheme="jade", variant="surface", size="1"),
                        rx.text("(Swipe table horizontally on mobile)", size="1", color=TEXT_MUTED, display={"initial": "block", "md": "none"}),
                        align="center",
                        spacing="2",
                    ),
                    direction={"initial": "column", "sm": "row"},
                    justify="between",
                    align={"initial": "start", "sm": "center"},
                    width="100%",
                    gap="2",
                ),
                rx.text(
                    "Displaying only variants with completed roundtrips or active positions. "
                    "Untriggered incubator variants are housed separately in the Scanner & Monitor tab.",
                    size="2",
                    color=TEXT_MUTED,
                ),
                # Responsive Table Container
                rx.box(
                    rx.table.root(
                        rx.table.header(
                            rx.table.row(
                                rx.table.column_header_cell("Variant", style=TABLE_HEADER_STYLE, class_name="nowrap"),
                                rx.table.column_header_cell("Delta (Δ)", style=TABLE_HEADER_STYLE, class_name="nowrap"),
                                rx.table.column_header_cell("Wing", style=TABLE_HEADER_STYLE, class_name="nowrap"),
                                rx.table.column_header_cell("DTE", style=TABLE_HEADER_STYLE, class_name="nowrap"),
                                rx.table.column_header_cell("Profit Target", style=TABLE_HEADER_STYLE, class_name="nowrap"),
                                rx.table.column_header_cell("Stop Loss", style=TABLE_HEADER_STYLE, class_name="nowrap"),
                                rx.table.column_header_cell("Entry Time", style=TABLE_HEADER_STYLE, class_name="nowrap"),
                                rx.table.column_header_cell("Closed", style=TABLE_HEADER_STYLE, class_name="nowrap"),
                                rx.table.column_header_cell("Win Rate (Closed)", style=TABLE_HEADER_STYLE, class_name="nowrap"),
                                rx.table.column_header_cell("Gross P&L", style=TABLE_HEADER_STYLE, class_name="nowrap"),
                                rx.table.column_header_cell("Fees ($0.40/leg)", style=TABLE_HEADER_STYLE, class_name="nowrap"),
                                rx.table.column_header_cell("Realized Net P&L", style=TABLE_HEADER_STYLE, class_name="nowrap"),
                                rx.table.column_header_cell("Realized Net P&L % (RoR)", style=TABLE_HEADER_STYLE, class_name="nowrap"),
                                rx.table.column_header_cell("Status", style=TABLE_HEADER_STYLE, class_name="nowrap"),
                            )
                        ),
                        rx.table.body(
                            rx.foreach(
                                DashboardState.incubator_rows,
                                lambda row: rx.table.row(
                                    rx.table.cell(rx.text(row["variant"], weight="bold", color=CYAN, class_name="font-mono nowrap")),
                                    rx.table.cell(rx.text(row["delta"], class_name="tabular-nums nowrap")),
                                    rx.table.cell(rx.text(row["wing"], class_name="tabular-nums nowrap")),
                                    rx.table.cell(rx.badge(row["dte"], color_scheme="gray", size="1")),
                                    rx.table.cell(rx.text(row["profit_target"], class_name="tabular-nums nowrap")),
                                    rx.table.cell(rx.text(row["stop_loss"], class_name="tabular-nums nowrap")),
                                    rx.table.cell(rx.text(row["entry_time"], class_name="tabular-nums nowrap")),
                                    rx.table.cell(rx.text(row["closed_trades"], class_name="tabular-nums nowrap")),
                                    rx.table.cell(rx.text(row["win_rate"], weight="bold", color=GREEN, class_name="tabular-nums nowrap")),
                                    rx.table.cell(rx.text(row["gross_pnl_str"], class_name="tabular-nums nowrap")),
                                    rx.table.cell(rx.text(row["fees_str"], color=AMBER, class_name="tabular-nums nowrap")),
                                    rx.table.cell(rx.text(row["net_pnl_str"], weight="bold", color=GREEN, class_name="tabular-nums nowrap")),
                                    rx.table.cell(rx.text(row["net_ror_pct"], weight="bold", color=CYAN, class_name="tabular-nums nowrap")),
                                    rx.table.cell(rx.badge(row["status"], color_scheme=row["status_color"], size="1")),
                                    style=TABLE_ROW_STYLE,
                                )
                            )
                        ),
                        width="100%",
                    ),
                    class_name="table-responsive-container",
                ),

                # Callout linking to the separate Monitor tab
                rx.box(
                    rx.flex(
                        rx.hstack(
                            rx.icon("scan", size=18, color=CYAN),
                            rx.text(
                                "9 untriggered variants are actively scanning the SPX/XSP chain in background monitoring. "
                                "Inspect entry windows, delta targets, and risk caps in the Scanner & Monitor tab.",
                                size="2",
                                color=TEXT_MUTED,
                            ),
                            align="center",
                            spacing="2",
                        ),
                        rx.button(
                            rx.hstack(rx.text("View Scanner & Monitor"), rx.icon("arrow-right", size=14), align="center", spacing="1"),
                            on_click=DashboardState.set_tab("monitor"),
                            size="1",
                            color_scheme="cyan",
                            variant="surface",
                            cursor="pointer",
                        ),
                        direction={"initial": "column", "sm": "row"},
                        justify="between",
                        align={"initial": "start", "sm": "center"},
                        width="100%",
                        gap="2",
                    ),
                    padding="10px 14px",
                    background_color="#0C101A",
                    border=f"1px solid {BORDER_COLOR}",
                    border_radius="6px",
                    width="100%",
                    margin_top="8px",
                ),
                spacing="3",
                width="100%",
            ),
            padding={"initial": "12px", "sm": "16px"},
            background_color=SURFACE_DARK,
            border=f"1px solid {BORDER_COLOR}",
            border_radius="8px",
            width="100%",
        ),
        spacing="4",
        width="100%",
    )
