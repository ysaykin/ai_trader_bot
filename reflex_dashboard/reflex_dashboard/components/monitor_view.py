"""
Tab: 20-Variant Incubator Scanner & Schedule Monitor (EXP-05).
Houses all background-monitored variants, entry schedules, and scanning criteria,
keeping the active trading performance view clean and high-signal.
"""

import reflex as rx
from ..styles.numeric import numeric_color, numeric_text
from ..state import DashboardState
from ..styles.theme import (
    SURFACE_DARK,
    BORDER_COLOR,
    CYAN,
    GREEN,
    AMBER,
    TEXT_MUTED,
    TEXT_PRIMARY,
    KPI_CARD_STYLE,
    TABLE_HEADER_STYLE,
    TABLE_ROW_STYLE,
)


def monitor_view() -> rx.Component:
    return rx.vstack(
        # Header Callout
        rx.box(
            rx.vstack(
                rx.flex(
                    rx.hstack(
                        rx.icon("scan", size=20, color=CYAN),
                        rx.heading("20-Variant Incubator Scanner & Schedule Monitor", size="3", color=TEXT_PRIMARY, weight="bold"),
                        align="center",
                        spacing="2",
                        wrap="wrap",
                    ),
                    rx.badge("EXP-05 Multi-Variant Scanner", color_scheme="cyan", variant="surface", size="1"),
                    direction={"initial": "column", "sm": "row"},
                    justify="between",
                    align={"initial": "start", "sm": "center"},
                    width="100%",
                    gap="2",
                ),
                rx.text(
                    "Continuous background market scanner actively monitoring SPX/XSP option chains across all 20 parameter "
                    "combinations (Delta 0.08–0.25, Wing $2–$15, 0DTE/1DTE). Evaluates market marks, IV spreads, and entry "
                    "time windows every 60 seconds without cross-strategy interference.",
                    size="2",
                    color=TEXT_MUTED,
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

        # Status Summary Metrics Grid
        rx.grid(
            rx.box(
                rx.vstack(
                    rx.text("MONITORED GRID", size="1", color=TEXT_MUTED, weight="bold"),
                    rx.heading("20 Variants", size={"initial": "5", "sm": "6"}, color=CYAN, class_name="tabular-nums"),
                    rx.text("Configured in engine", size="1", color=TEXT_MUTED),
                    spacing="1",
                ),
                style=KPI_CARD_STYLE,
            ),
            rx.box(
                rx.vstack(
                    rx.text("SCANNING PIPELINE", size="1", color=TEXT_MUTED, weight="bold"),
                    rx.heading(DashboardState.monitored_count_str, size={"initial": "5", "sm": "6"}, color=AMBER, class_name="tabular-nums"),
                    rx.text("Awaiting window / trigger", size="1", color=TEXT_MUTED),
                    spacing="1",
                ),
                style=KPI_CARD_STYLE,
            ),
            rx.box(
                rx.vstack(
                    rx.text("TRADED VARIANTS", size="1", color=TEXT_MUTED, weight="bold"),
                    rx.heading(DashboardState.active_grid_str, size={"initial": "5", "sm": "6"}, color=GREEN, class_name="tabular-nums"),
                    rx.text("Completed profit target", size="1", color=TEXT_MUTED),
                    spacing="1",
                ),
                style=KPI_CARD_STYLE,
            ),
            rx.box(
                rx.vstack(
                    rx.text("MAX RISK PER STRATEGY", size="1", color=TEXT_MUTED, weight="bold"),
                    rx.heading("$5,000", size={"initial": "5", "sm": "6"}, color=TEXT_PRIMARY, class_name="tabular-nums"),
                    rx.text("RSK-01 isolated cap", size="1", color=TEXT_MUTED),
                    spacing="1",
                ),
                style=KPI_CARD_STYLE,
            ),
            columns={"initial": "2", "sm": "4"},
            spacing="3",
            width="100%",
        ),

        # Monitored Variants Schedule & Scanning Table
        rx.box(
            rx.vstack(
                rx.flex(
                    rx.hstack(
                        rx.icon("calendar-clock", size=18, color=CYAN),
                        rx.heading("Variant Entry Windows & Parameter Profiles", size="3", color=TEXT_PRIMARY, weight="bold"),
                        align="center",
                        spacing="2",
                    ),
                    rx.badge("1-Min Tick Evaluation", color_scheme="gray", variant="surface", size="1"),
                    direction={"initial": "column", "sm": "row"},
                    justify="between",
                    align={"initial": "start", "sm": "center"},
                    width="100%",
                    gap="2",
                ),
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
                                rx.table.column_header_cell("Entry Window", style=TABLE_HEADER_STYLE, class_name="nowrap"),
                                rx.table.column_header_cell("Risk Cap", style=TABLE_HEADER_STYLE, class_name="nowrap"),
                                rx.table.column_header_cell("Scanner Status", style=TABLE_HEADER_STYLE, class_name="nowrap"),
                            )
                        ),
                        rx.table.body(
                            rx.foreach(
                                DashboardState.incubator_monitored_rows,
                                lambda row: rx.table.row(
                                    rx.table.cell(rx.text(row["variant"], weight="bold", color=CYAN, class_name="font-mono nowrap")),
                                    rx.table.cell(rx.text(numeric_text(row["delta"]), class_name="tabular-nums nowrap")),
                                    rx.table.cell(rx.text(row["wing"], class_name="tabular-nums nowrap")),
                                    rx.table.cell(rx.badge(row["dte"], color_scheme="gray", size="1")),
                                    rx.table.cell(rx.text(row["profit_target"], class_name="tabular-nums nowrap")),
                                    rx.table.cell(rx.text(row["stop_loss"], class_name="tabular-nums nowrap")),
                                    rx.table.cell(rx.text(row["entry_time"], weight="bold", color=TEXT_PRIMARY, class_name="tabular-nums nowrap")),
                                    rx.table.cell(rx.text(row["risk_cap"], color=TEXT_MUTED, class_name="tabular-nums nowrap")),
                                    rx.table.cell(rx.badge(row["scan_status"], color_scheme="amber", size="1")),
                                    style=TABLE_ROW_STYLE,
                                )
                            )
                        ),
                        width="100%",
                    ),
                    class_name="table-responsive-container",
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
