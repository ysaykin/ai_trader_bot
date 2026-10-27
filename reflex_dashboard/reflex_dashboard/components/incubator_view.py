"""
Tab 1: Curated Strategy Leaderboard & Trade Journal Execution View.
Exclusively showcases the AI-managed strategy, Top 5 performing strategies, and Bottom 3 underperformers,
along with strategy aggregates and a 50-trade highlight log with CSV export capabilities.
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
    INDIGO,
    TEXT_MUTED,
    TEXT_PRIMARY,
    KPI_CARD_STYLE,
    TABLE_HEADER_STYLE,
    TABLE_ROW_STYLE,
)


def incubator_view() -> rx.Component:
    return rx.vstack(
        # 1. Top KPI Scorecards (Responsive Grid)
        rx.grid(
            # Metric 1: Active Grid
            rx.box(
                rx.vstack(
                    rx.text("ACTIVE GRID", size="1", color=TEXT_MUTED, weight="bold"),
                    rx.heading(DashboardState.active_grid_str, size={"initial": "5", "sm": "6"}, color=CYAN, class_name="tabular-nums"),
                    rx.text("24 Configured Variants", size="1", color=TEXT_MUTED),
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

        # 2. Strategy Group Aggregates Panel (Responsive 3-Column Grid)
        rx.box(
            rx.vstack(
                rx.flex(
                    rx.hstack(
                        rx.icon("layers", size=18, color=CYAN),
                        rx.heading("Strategy Performance Aggregates", size="3", color=TEXT_PRIMARY, weight="bold"),
                        align="center",
                        spacing="2",
                        wrap="wrap",
                    ),
                    rx.badge("Curated Multi-Variant Rollup", color_scheme="cyan", variant="surface", size="1"),
                    direction={"initial": "column", "sm": "row"},
                    justify="between",
                    align={"initial": "start", "sm": "center"},
                    width="100%",
                    gap="2",
                ),
                rx.text(
                    "High-level group comparison isolating the Autonomous Neural Policy against the Top 5 momentum configurations and Bottom 3 underperformers:",
                    size="2",
                    color=TEXT_MUTED,
                ),
                rx.grid(
                    # Aggregate Card 1: AI Pure Neural Policy
                    rx.box(
                        rx.vstack(
                            rx.flex(
                                rx.hstack(
                                    rx.icon("brain-circuit", size=16, color=INDIGO),
                                    rx.text("AI Pure Neural Strategy", size="2", weight="bold", color=TEXT_PRIMARY),
                                    align="center",
                                    spacing="1",
                                ),
                                rx.badge("LIVE META-POLICY", color_scheme="indigo", variant="surface", size="1"),
                                justify="between",
                                width="100%",
                            ),
                            rx.hstack(
                                rx.heading(DashboardState.ai_aggregate["net_pnl_str"], size="5", color=CYAN, class_name="tabular-nums font-mono"),
                                rx.badge(DashboardState.ai_aggregate["win_rate"], color_scheme="jade", size="1"),
                                align="baseline",
                                spacing="2",
                            ),
                            rx.grid(
                                rx.box(rx.text("Trades", size="1", color=TEXT_MUTED), rx.text(DashboardState.ai_aggregate["trades"], size="1", weight="bold")),
                                rx.box(rx.text("Fees", size="1", color=TEXT_MUTED), rx.text(DashboardState.ai_aggregate["fees_str"], size="1", color=AMBER)),
                                rx.box(rx.text("Regime", size="1", color=TEXT_MUTED), rx.text("Adaptive", size="1", color=CYAN)),
                                columns="3",
                                width="100%",
                                spacing="1",
                            ),
                            spacing="2",
                        ),
                        padding="14px",
                        background_color="#0C101A",
                        border=f"1px solid {BORDER_COLOR}",
                        border_radius="6px",
                    ),
                    # Aggregate Card 2: Top 5 Performers
                    rx.box(
                        rx.vstack(
                            rx.flex(
                                rx.hstack(
                                    rx.icon("trophy", size=16, color=GREEN),
                                    rx.text("Top 5 Performing Strategies", size="2", weight="bold", color=TEXT_PRIMARY),
                                    align="center",
                                    spacing="1",
                                ),
                                rx.badge("ALPHA LEADERS", color_scheme="jade", variant="surface", size="1"),
                                justify="between",
                                width="100%",
                            ),
                            rx.hstack(
                                rx.heading(DashboardState.top5_aggregate["net_pnl_str"], size="5", color=GREEN, class_name="tabular-nums font-mono"),
                                rx.badge(f"{DashboardState.top5_aggregate['avg_win_rate']} Win Rate", color_scheme="jade", size="1"),
                                align="baseline",
                                spacing="2",
                            ),
                            rx.grid(
                                rx.box(rx.text("Combined Volume", size="1", color=TEXT_MUTED), rx.text(f"{DashboardState.top5_aggregate['trades']} Trades", size="1", weight="bold")),
                                rx.box(rx.text("Total Fees", size="1", color=TEXT_MUTED), rx.text(DashboardState.top5_aggregate["fees_str"], size="1", color=AMBER)),
                                rx.box(rx.text("Top Variant", size="1", color=TEXT_MUTED), rx.text(DashboardState.top5_aggregate["best_variant"], size="1", color=GREEN)),
                                columns="3",
                                width="100%",
                                spacing="1",
                            ),
                            spacing="2",
                        ),
                        padding="14px",
                        background_color="#0C101A",
                        border=f"1px solid {BORDER_COLOR}",
                        border_radius="6px",
                    ),
                    # Aggregate Card 3: Bottom 3 Underperformers
                    rx.box(
                        rx.vstack(
                            rx.flex(
                                rx.hstack(
                                    rx.icon("triangle-alert", size=16, color=RED),
                                    rx.text("Bottom 3 Underperformers", size="2", weight="bold", color=TEXT_PRIMARY),
                                    align="center",
                                    spacing="1",
                                ),
                                rx.badge("UNDERPERFORMERS", color_scheme="crimson", variant="surface", size="1"),
                                justify="between",
                                width="100%",
                            ),
                            rx.hstack(
                                rx.heading(DashboardState.bottom3_aggregate["net_pnl_str"], size="5", color=RED, class_name="tabular-nums font-mono"),
                                rx.badge(f"{DashboardState.bottom3_aggregate['avg_win_rate']} Win Rate", color_scheme="crimson", size="1"),
                                align="baseline",
                                spacing="2",
                            ),
                            rx.grid(
                                rx.box(rx.text("Combined Volume", size="1", color=TEXT_MUTED), rx.text(f"{DashboardState.bottom3_aggregate['trades']} Trades", size="1", weight="bold")),
                                rx.box(rx.text("Total Fees", size="1", color=TEXT_MUTED), rx.text(DashboardState.bottom3_aggregate["fees_str"], size="1", color=AMBER)),
                                rx.box(rx.text("Lagging Variant", size="1", color=TEXT_MUTED), rx.text(DashboardState.bottom3_aggregate["worst_variant"], size="1", color=RED)),
                                columns="3",
                                width="100%",
                                spacing="1",
                            ),
                            spacing="2",
                        ),
                        padding="14px",
                        background_color="#0C101A",
                        border=f"1px solid {BORDER_COLOR}",
                        border_radius="6px",
                    ),
                    columns={"initial": "1", "md": "3"},
                    spacing="3",
                    width="100%",
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

        # 3. Curated Strategy Leaderboard Table (AI + Top 5 + Bottom 3)
        rx.box(
            rx.vstack(
                rx.flex(
                    rx.hstack(
                        rx.icon("list-ordered", size=18, color=CYAN),
                        rx.heading("Curated Strategy Leaderboard (AI, Top 5 & Bottom 3 Performers)", size="3", color=TEXT_PRIMARY, weight="bold"),
                        align="center",
                        spacing="2",
                    ),
                    rx.hstack(
                        rx.badge("9 Curated Variants", color_scheme="jade", variant="surface", size="1"),
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
                    "Displaying only the high-signal spectrum: the Pure Neural AI strategy alongside the Top 5 leaders and Bottom 3 underperformers. "
                    "Uninformative middle-ranking variants are excluded from the primary table.",
                    size="2",
                    color=TEXT_MUTED,
                ),
                # Responsive Table Container
                rx.box(
                    rx.table.root(
                        rx.table.header(
                            rx.table.row(
                                rx.table.column_header_cell("Rank / Group", style=TABLE_HEADER_STYLE, class_name="nowrap"),
                                rx.table.column_header_cell("Variant", style=TABLE_HEADER_STYLE, class_name="nowrap"),
                                rx.table.column_header_cell("Structure", style=TABLE_HEADER_STYLE, class_name="nowrap"),
                                rx.table.column_header_cell("Delta (Δ)", style=TABLE_HEADER_STYLE, class_name="nowrap"),
                                rx.table.column_header_cell("Wing", style=TABLE_HEADER_STYLE, class_name="nowrap"),
                                rx.table.column_header_cell("DTE", style=TABLE_HEADER_STYLE, class_name="nowrap"),
                                rx.table.column_header_cell("Profit Target", style=TABLE_HEADER_STYLE, class_name="nowrap"),
                                rx.table.column_header_cell("Stop Loss", style=TABLE_HEADER_STYLE, class_name="nowrap"),
                                rx.table.column_header_cell("Closed", style=TABLE_HEADER_STYLE, class_name="nowrap"),
                                rx.table.column_header_cell("Win Rate", style=TABLE_HEADER_STYLE, class_name="nowrap"),
                                rx.table.column_header_cell("Gross P&L", style=TABLE_HEADER_STYLE, class_name="nowrap"),
                                rx.table.column_header_cell("Fees ($0.40/leg)", style=TABLE_HEADER_STYLE, class_name="nowrap"),
                                rx.table.column_header_cell("Realized Net P&L", style=TABLE_HEADER_STYLE, class_name="nowrap"),
                                rx.table.column_header_cell("Net RoR %", style=TABLE_HEADER_STYLE, class_name="nowrap"),
                                rx.table.column_header_cell("Status", style=TABLE_HEADER_STYLE, class_name="nowrap"),
                            )
                        ),
                        rx.table.body(
                            rx.foreach(
                                DashboardState.incubator_rows,
                                lambda row: rx.table.row(
                                    rx.table.cell(rx.badge(row["rank_badge"], color_scheme=row["badge_color"], size="1", variant="surface")),
                                    rx.table.cell(rx.text(row["variant"], weight="bold", color=CYAN, class_name="font-mono nowrap")),
                                    rx.table.cell(rx.badge(row["structure_label"], color_scheme="gray", size="1")),
                                    rx.table.cell(rx.text(row["delta"], class_name="tabular-nums nowrap")),
                                    rx.table.cell(rx.text(row["wing"], class_name="tabular-nums nowrap")),
                                    rx.table.cell(rx.badge(row["dte"], color_scheme="gray", size="1")),
                                    rx.table.cell(rx.text(row["profit_target"], class_name="tabular-nums nowrap")),
                                    rx.table.cell(rx.text(row["stop_loss"], class_name="tabular-nums nowrap")),
                                    rx.table.cell(rx.text(row["closed_trades"], class_name="tabular-nums nowrap")),
                                    rx.table.cell(rx.text(row["win_rate"], weight="bold", color=GREEN, class_name="tabular-nums nowrap")),
                                    rx.table.cell(rx.text(row["gross_pnl_str"], class_name="tabular-nums nowrap")),
                                    rx.table.cell(rx.text(row["fees_str"], color=AMBER, class_name="tabular-nums nowrap")),
                                    rx.table.cell(rx.text(row["net_pnl_str"], weight="bold", color=row["status_color"], class_name="tabular-nums nowrap")),
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
                spacing="3",
                width="100%",
            ),
            padding={"initial": "12px", "sm": "16px"},
            background_color=SURFACE_DARK,
            border=f"1px solid {BORDER_COLOR}",
            border_radius="8px",
            width="100%",
        ),

        # 4. Per-Trade Execution Highlights & CSV Export Section (Past 50 Trades + Load More)
        rx.box(
            rx.vstack(
                rx.flex(
                    rx.hstack(
                        rx.icon("receipt", size=18, color=CYAN),
                        rx.heading("Per-Trade Execution Highlights (Past 50 Trades)", size="3", color=TEXT_PRIMARY, weight="bold"),
                        align="center",
                        spacing="2",
                        wrap="wrap",
                    ),
                    rx.hstack(
                        rx.button(
                            rx.hstack(rx.icon("download", size=14), rx.text("Export Visible (50) CSV"), align="center", spacing="1"),
                            on_click=DashboardState.download_visible_trades_csv,
                            size="1",
                            color_scheme="cyan",
                            variant="surface",
                            cursor="pointer",
                        ),
                        rx.button(
                            rx.hstack(rx.icon("file-spreadsheet", size=14), rx.text(f"Export Tradier Journal ({DashboardState.total_closed_trades_count}) CSV"), align="center", spacing="1"),
                            on_click=DashboardState.download_all_trades_csv,
                            size="1",
                            color_scheme="gray",
                            variant="surface",
                            cursor="pointer",
                        ),
                        spacing="2",
                        wrap="wrap",
                    ),
                    direction={"initial": "column", "sm": "row"},
                    justify="between",
                    align={"initial": "start", "sm": "center"},
                    width="100%",
                    gap="2",
                ),
                rx.text(
                    "Individual closed roundtrip executions with exact broker fills, vertical & condor fee accounting ($0.40/leg), and net returns:",
                    size="2",
                    color=TEXT_MUTED,
                ),
                # Table of 50 visible trades
                rx.box(
                    rx.table.root(
                        rx.table.header(
                            rx.table.row(
                                rx.table.column_header_cell("Time (ET)", style=TABLE_HEADER_STYLE, class_name="nowrap"),
                                rx.table.column_header_cell("Trade ID", style=TABLE_HEADER_STYLE, class_name="nowrap"),
                                rx.table.column_header_cell("Strategy Variant", style=TABLE_HEADER_STYLE, class_name="nowrap"),
                                rx.table.column_header_cell("Structure", style=TABLE_HEADER_STYLE, class_name="nowrap"),
                                rx.table.column_header_cell("Strikes", style=TABLE_HEADER_STYLE, class_name="nowrap"),
                                rx.table.column_header_cell("Qty", style=TABLE_HEADER_STYLE, class_name="nowrap"),
                                rx.table.column_header_cell("Credit", style=TABLE_HEADER_STYLE, class_name="nowrap"),
                                rx.table.column_header_cell("Exit Reason", style=TABLE_HEADER_STYLE, class_name="nowrap"),
                                rx.table.column_header_cell("Gross P&L", style=TABLE_HEADER_STYLE, class_name="nowrap"),
                                rx.table.column_header_cell("Fees", style=TABLE_HEADER_STYLE, class_name="nowrap"),
                                rx.table.column_header_cell("Net Realized P&L", style=TABLE_HEADER_STYLE, class_name="nowrap"),
                                rx.table.column_header_cell("RoR %", style=TABLE_HEADER_STYLE, class_name="nowrap"),
                            )
                        ),
                        rx.table.body(
                            rx.foreach(
                                DashboardState.visible_trades_50,
                                lambda t: rx.table.row(
                                    rx.table.cell(rx.text(t["time_str"], class_name="font-mono text-xs nowrap")),
                                    rx.table.cell(rx.text(t["trade_id"], class_name="font-mono text-xs text-slate-400 nowrap")),
                                    rx.table.cell(rx.text(t["strategy_label"], weight="bold", color=CYAN, class_name="nowrap")),
                                    rx.table.cell(rx.badge(t["structure_label"], color_scheme="gray", size="1")),
                                    rx.table.cell(rx.text(t["strikes_display"], class_name="font-mono text-xs nowrap")),
                                    rx.table.cell(rx.text(t["quantity"], class_name="tabular-nums nowrap")),
                                    rx.table.cell(rx.text(t["credit_str"], class_name="tabular-nums nowrap")),
                                    rx.table.cell(rx.badge(t["exit_reason"], color_scheme=t["net_pnl_color"], size="1")),
                                    rx.table.cell(rx.text(t["gross_pnl_str"], class_name="tabular-nums nowrap")),
                                    rx.table.cell(rx.text(t["fees_str"], color=AMBER, class_name="tabular-nums nowrap")),
                                    rx.table.cell(rx.text(t["net_pnl_str"], weight="bold", color=t["net_pnl_color"], class_name="tabular-nums nowrap")),
                                    rx.table.cell(rx.text(t["net_ror_str"], weight="bold", color=CYAN, class_name="tabular-nums nowrap")),
                                    style=TABLE_ROW_STYLE,
                                )
                            )
                        ),
                        width="100%",
                    ),
                    class_name="table-responsive-container",
                ),

                # Bottom Load More Callout (Forces user to download complete CSV archive)
                rx.box(
                    rx.flex(
                        rx.hstack(
                            rx.icon("archive", size=20, color=INDIGO),
                            rx.vstack(
                                rx.text(
                                    f"Showing all {DashboardState.total_closed_trades_count} verified broker trades executed via Tradier.",
                                    size="2",
                                    weight="bold",
                                    color=TEXT_PRIMARY,
                                ),
                                rx.text(
                                    "Historical trades are sourced exclusively from actual Tradier fills and fee reconciliations. "
                                    "Click below to download the complete Tradier trade execution journal in CSV format.",
                                    size="1",
                                    color=TEXT_MUTED,
                                ),
                                spacing="0",
                            ),
                            align="center",
                            spacing="3",
                        ),
                        # Load More Alert Dialog Trigger
                        rx.alert_dialog.root(
                            rx.alert_dialog.trigger(
                                rx.button(
                                    rx.hstack(
                                        rx.icon("download", size=16),
                                        rx.text("Load More (Download Full CSV Archive)"),
                                        align="center",
                                        spacing="1",
                                    ),
                                    color_scheme="indigo",
                                    variant="solid",
                                    size="2",
                                    cursor="pointer",
                                    width={"initial": "100%", "sm": "auto"},
                                ),
                            ),
                            rx.alert_dialog.content(
                                rx.alert_dialog.title("📥 Complete Trade Ledger Archive Export"),
                                rx.alert_dialog.description(
                                    f"The interactive table is calibrated for the past 50 trades to maintain ultra-fast sub-millisecond dashboard rendering. "
                                    f"To view and audit all {DashboardState.total_closed_trades_count} historical trades, download the full RFC-4180 compliant CSV export.",
                                    size="2",
                                ),
                                rx.hstack(
                                    rx.alert_dialog.cancel(
                                        rx.button("Cancel", variant="soft", color_scheme="gray"),
                                    ),
                                    rx.alert_dialog.action(
                                        rx.button(
                                            "Download Complete Trade Archive",
                                            color_scheme="indigo",
                                            on_click=DashboardState.load_more_and_export_csv,
                                        ),
                                    ),
                                    spacing="3",
                                    margin_top="16px",
                                    justify="end",
                                ),
                                style={
                                    "background_color": SURFACE_DARK,
                                    "border": f"1px solid {BORDER_COLOR}",
                                },
                            ),
                        ),
                        direction={"initial": "column", "sm": "row"},
                        justify="between",
                        align={"initial": "start", "sm": "center"},
                        width="100%",
                        gap="3",
                    ),
                    padding="14px 18px",
                    background_color="#0C101A",
                    border=f"1px solid {BORDER_COLOR}",
                    border_radius="6px",
                    width="100%",
                    margin_top="10px",
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
