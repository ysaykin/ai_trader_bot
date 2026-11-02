"""
Tab 4: A/B Strategy Multi-Execution Lab (EXP-01 through EXP-04).
Simultaneously compares all strategies with actual trade executions,
featuring dynamic head-to-head comparison and Realized Net P&L %.
Fully responsive across mobile, tablet, and desktop screens.
"""

import reflex as rx
from ..styles.numeric import numeric_color, numeric_text
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
    TABLE_HEADER_STYLE,
    TABLE_ROW_STYLE,
)


def ab_lab_view() -> rx.Component:
    return rx.vstack(
        # Header Callout
        rx.box(
            rx.vstack(
                rx.flex(
                    rx.hstack(
                        rx.icon("scale", size=20, color=CYAN),
                        rx.heading("A/B Multi-Strategy Testing Lab", size="3", color=TEXT_PRIMARY, weight="bold"),
                        align="center",
                        spacing="2",
                        wrap="wrap",
                    ),
                    rx.badge("EXP-01 – EXP-04 Isolated Ledgers", color_scheme="cyan", variant="surface", size="1"),
                    direction={"initial": "column", "sm": "row"},
                    justify="between",
                    align={"initial": "start", "sm": "center"},
                    width="100%",
                    gap="2",
                ),
                rx.text(
                    "Simultaneously executes multiple distinct Iron Condor variations on the same underlying asset "
                    "without cross-talk, using deterministic order tagging and separate ledger databases. "
                    "All performance figures reflect real executed fills and broker contract fees ($0.40/leg).",
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

        # Interactive Selector Bar for Head-to-Head Comparison
        rx.box(
            rx.vstack(
                rx.flex(
                    rx.box(
                        rx.text("SELECT STRATEGY A", size="1", color=TEXT_MUTED, weight="bold", margin_bottom="4px"),
                        rx.select(
                            DashboardState.ab_strategy_options,
                            value=DashboardState.ab_strategy_a,
                            on_change=DashboardState.set_ab_strategy_a,
                            size="2",
                            variant="surface",
                            color_scheme="cyan",
                            width="100%",
                        ),
                        flex="1",
                        min_width={"initial": "100%", "sm": "240px"},
                    ),
                    rx.center(
                        rx.badge("VS", color_scheme="gray", variant="solid", size="2"),
                        padding_x="12px",
                        display={"initial": "none", "sm": "flex"},
                    ),
                    rx.box(
                        rx.text("SELECT STRATEGY B", size="1", color=TEXT_MUTED, weight="bold", margin_bottom="4px"),
                        rx.select(
                            DashboardState.ab_strategy_options,
                            value=DashboardState.ab_strategy_b,
                            on_change=DashboardState.set_ab_strategy_b,
                            size="2",
                            variant="surface",
                            color_scheme="amber",
                            width="100%",
                        ),
                        flex="1",
                        min_width={"initial": "100%", "sm": "240px"},
                    ),
                    direction={"initial": "column", "sm": "row"},
                    align="center",
                    gap="3",
                    width="100%",
                ),
                # Delta Lead Banner
                rx.box(
                    rx.hstack(
                        rx.icon("trophy", size=16, color=CYAN),
                        rx.text(
                            DashboardState.ab_delta_winner,
                            size="2",
                            weight="bold",
                            color=TEXT_PRIMARY,
                        ),
                        align="center",
                        spacing="2",
                    ),
                    padding="8px 12px",
                    background_color="#0C101A",
                    border=f"1px solid {BORDER_COLOR}",
                    border_radius="6px",
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

        # Head-to-Head Cards Grid - Responsive: 1 column on mobile, 2 on desktop
        rx.grid(
            # Strategy A Card
            rx.box(
                rx.vstack(
                    rx.flex(
                        rx.badge(
                            f"STRATEGY A: {DashboardState.ab_card_a['variant']}",
                            color_scheme="cyan",
                            variant="solid",
                            size="2",
                        ),
                        rx.badge(
                            DashboardState.ab_card_a["tag"],
                            color_scheme="cyan",
                            variant="surface",
                            size="1",
                        ),
                        justify="between",
                        align="center",
                        width="100%",
                    ),
                    rx.text(
                        DashboardState.ab_card_a["config_desc"],
                        size="2",
                        color=TEXT_MUTED,
                    ),
                    rx.divider(color_scheme="gray"),
                    rx.grid(
                        rx.box(
                            rx.text("Win Rate (Closed)", size="1", color=TEXT_MUTED),
                            rx.text(numeric_text(DashboardState.ab_card_a["win_rate"]), size="3", weight="bold", color=numeric_color(DashboardState.ab_card_a["win_rate"]), class_name="tabular-nums"),
                        ),
                        rx.box(
                            rx.text("Closed Trades", size="1", color=TEXT_MUTED),
                            rx.text(DashboardState.ab_card_a["closed_trades"], size="3", weight="bold", color=TEXT_PRIMARY, class_name="tabular-nums"),
                        ),
                        rx.box(
                            rx.text("Realized Net P&L", size="1", color=TEXT_MUTED),
                            rx.text(numeric_text(DashboardState.ab_card_a["net_pnl_str"]), size="4", weight="bold", color=numeric_color(DashboardState.ab_card_a["net_pnl_str"]), class_name="tabular-nums"),
                        ),
                        rx.box(
                            rx.text("Realized Net P&L % (RoR)", size="1", color=TEXT_MUTED),
                            rx.text(numeric_text(DashboardState.ab_card_a["net_ror_pct"]), size="4", weight="bold", color=numeric_color(DashboardState.ab_card_a["net_ror_pct"]), class_name="tabular-nums"),
                        ),
                        rx.box(
                            rx.text("Return on Credit %", size="1", color=TEXT_MUTED),
                            rx.text(numeric_text(DashboardState.ab_card_a["net_roc_pct"]), size="3", weight="bold", color=numeric_color(DashboardState.ab_card_a["net_roc_pct"]), class_name="tabular-nums"),
                        ),
                        rx.box(
                            rx.text("Broker Fees Paid", size="1", color=TEXT_MUTED),
                            rx.text(numeric_text(DashboardState.ab_card_a["fees_str"]), size="3", weight="bold", color=numeric_color(DashboardState.ab_card_a["fees_str"]), class_name="tabular-nums"),
                        ),
                        rx.box(
                            rx.text("Profit Factor", size="1", color=TEXT_MUTED),
                            rx.text(DashboardState.ab_card_a["profit_factor"], size="3", weight="bold", color=CYAN, class_name="tabular-nums"),
                        ),
                        rx.box(
                            rx.text("Avg Net / Trade", size="1", color=TEXT_MUTED),
                            rx.text(numeric_text(DashboardState.ab_card_a["avg_trade_str"]), size="3", weight="bold", color=numeric_color(DashboardState.ab_card_a["avg_trade_str"]), class_name="tabular-nums"),
                        ),
                        columns="2",
                        spacing="3",
                        width="100%",
                    ),
                    spacing="3",
                    width="100%",
                ),
                padding={"initial": "14px", "sm": "20px"},
                background_color=SURFACE_DARK,
                border=f"1px solid {BORDER_COLOR}",
                border_radius="8px",
            ),

            # Strategy B Card
            rx.box(
                rx.vstack(
                    rx.flex(
                        rx.badge(
                            f"STRATEGY B: {DashboardState.ab_card_b['variant']}",
                            color_scheme="amber",
                            variant="solid",
                            size="2",
                        ),
                        rx.badge(
                            DashboardState.ab_card_b["tag"],
                            color_scheme="amber",
                            variant="surface",
                            size="1",
                        ),
                        justify="between",
                        align="center",
                        width="100%",
                    ),
                    rx.text(
                        DashboardState.ab_card_b["config_desc"],
                        size="2",
                        color=TEXT_MUTED,
                    ),
                    rx.divider(color_scheme="gray"),
                    rx.grid(
                        rx.box(
                            rx.text("Win Rate (Closed)", size="1", color=TEXT_MUTED),
                            rx.text(numeric_text(DashboardState.ab_card_b["win_rate"]), size="3", weight="bold", color=numeric_color(DashboardState.ab_card_b["win_rate"]), class_name="tabular-nums"),
                        ),
                        rx.box(
                            rx.text("Closed Trades", size="1", color=TEXT_MUTED),
                            rx.text(DashboardState.ab_card_b["closed_trades"], size="3", weight="bold", color=TEXT_PRIMARY, class_name="tabular-nums"),
                        ),
                        rx.box(
                            rx.text("Realized Net P&L", size="1", color=TEXT_MUTED),
                            rx.text(numeric_text(DashboardState.ab_card_b["net_pnl_str"]), size="4", weight="bold", color=numeric_color(DashboardState.ab_card_b["net_pnl_str"]), class_name="tabular-nums"),
                        ),
                        rx.box(
                            rx.text("Realized Net P&L % (RoR)", size="1", color=TEXT_MUTED),
                            rx.text(numeric_text(DashboardState.ab_card_b["net_ror_pct"]), size="4", weight="bold", color=numeric_color(DashboardState.ab_card_b["net_ror_pct"]), class_name="tabular-nums"),
                        ),
                        rx.box(
                            rx.text("Return on Credit %", size="1", color=TEXT_MUTED),
                            rx.text(numeric_text(DashboardState.ab_card_b["net_roc_pct"]), size="3", weight="bold", color=numeric_color(DashboardState.ab_card_b["net_roc_pct"]), class_name="tabular-nums"),
                        ),
                        rx.box(
                            rx.text("Broker Fees Paid", size="1", color=TEXT_MUTED),
                            rx.text(numeric_text(DashboardState.ab_card_b["fees_str"]), size="3", weight="bold", color=numeric_color(DashboardState.ab_card_b["fees_str"]), class_name="tabular-nums"),
                        ),
                        rx.box(
                            rx.text("Profit Factor", size="1", color=TEXT_MUTED),
                            rx.text(DashboardState.ab_card_b["profit_factor"], size="3", weight="bold", color=CYAN, class_name="tabular-nums"),
                        ),
                        rx.box(
                            rx.text("Avg Net / Trade", size="1", color=TEXT_MUTED),
                            rx.text(numeric_text(DashboardState.ab_card_b["avg_trade_str"]), size="3", weight="bold", color=numeric_color(DashboardState.ab_card_b["avg_trade_str"]), class_name="tabular-nums"),
                        ),
                        columns="2",
                        spacing="3",
                        width="100%",
                    ),
                    spacing="3",
                    width="100%",
                ),
                padding={"initial": "14px", "sm": "20px"},
                background_color=SURFACE_DARK,
                border=f"1px solid {BORDER_COLOR}",
                border_radius="8px",
            ),
            columns={"initial": "1", "md": "2"},
            spacing="4",
            width="100%",
        ),

        # All Strategies with Actual Trades Leaderboard Table
        rx.box(
            rx.vstack(
                rx.flex(
                    rx.hstack(
                        rx.icon("table-properties", size=18, color=CYAN),
                        rx.heading("All Strategies With Executed Trades", size="3", color=TEXT_PRIMARY, weight="bold"),
                        align="center",
                        spacing="2",
                    ),
                    rx.hstack(
                        rx.badge("Ranked by Net Realized P&L", color_scheme="jade", variant="surface", size="1"),
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
                    "Side-by-side comparison across all variants that have completed trades in the isolated ledger. "
                    "Demonstrates performance dispersion across short delta, wing width, and profit target parameters.",
                    size="2",
                    color=TEXT_MUTED,
                ),
                rx.box(
                    rx.table.root(
                        rx.table.header(
                            rx.table.row(
                                rx.table.column_header_cell("Strategy Variant", style=TABLE_HEADER_STYLE, class_name="nowrap"),
                                rx.table.column_header_cell("Parameters", style=TABLE_HEADER_STYLE, class_name="nowrap"),
                                rx.table.column_header_cell("Closed", style=TABLE_HEADER_STYLE, class_name="nowrap"),
                                rx.table.column_header_cell("Win Rate", style=TABLE_HEADER_STYLE, class_name="nowrap"),
                                rx.table.column_header_cell("Gross P&L", style=TABLE_HEADER_STYLE, class_name="nowrap"),
                                rx.table.column_header_cell("Fees ($0.40/leg)", style=TABLE_HEADER_STYLE, class_name="nowrap"),
                                rx.table.column_header_cell("Realized Net P&L", style=TABLE_HEADER_STYLE, class_name="nowrap"),
                                rx.table.column_header_cell("Realized Net P&L % (RoR)", style=TABLE_HEADER_STYLE, class_name="nowrap"),
                                rx.table.column_header_cell("Return on Credit %", style=TABLE_HEADER_STYLE, class_name="nowrap"),
                                rx.table.column_header_cell("Profit Factor", style=TABLE_HEADER_STYLE, class_name="nowrap"),
                                rx.table.column_header_cell("Avg / Trade", style=TABLE_HEADER_STYLE, class_name="nowrap"),
                                rx.table.column_header_cell("Status", style=TABLE_HEADER_STYLE, class_name="nowrap"),
                            )
                        ),
                        rx.table.body(
                            rx.foreach(
                                DashboardState.ab_all_strategies,
                                lambda strat: rx.table.row(
                                    rx.table.cell(rx.text(strat["variant"], weight="bold", color=CYAN, class_name="font-mono nowrap")),
                                    rx.table.cell(rx.text(strat["config_desc"], size="1", color=TEXT_MUTED, class_name="nowrap")),
                                    rx.table.cell(rx.text(strat["closed_trades"], class_name="tabular-nums nowrap")),
                                    rx.table.cell(rx.text(numeric_text(strat["win_rate"]), weight="bold", color=numeric_color(strat["win_rate"]), class_name="tabular-nums nowrap")),
                                    rx.table.cell(rx.text(numeric_text(strat["gross_pnl_str"]), class_name="tabular-nums nowrap")),
                                    rx.table.cell(rx.text(numeric_text(strat["fees_str"]), color=numeric_color(strat["fees_str"]), class_name="tabular-nums nowrap")),
                                    rx.table.cell(rx.text(numeric_text(strat["net_pnl_str"]), weight="bold", color=numeric_color(strat["net_pnl_str"]), class_name="tabular-nums nowrap")),
                                    rx.table.cell(rx.text(numeric_text(strat["net_ror_pct"]), weight="bold", color=numeric_color(strat["net_ror_pct"]), class_name="tabular-nums nowrap")),
                                    rx.table.cell(rx.text(numeric_text(strat["net_roc_pct"]), class_name="tabular-nums nowrap")),
                                    rx.table.cell(rx.text(strat["profit_factor"], class_name="tabular-nums nowrap")),
                                    rx.table.cell(rx.text(numeric_text(strat["avg_trade_str"]), class_name="tabular-nums nowrap")),
                                    rx.table.cell(rx.badge(strat["status"], color_scheme=strat["status_color"], size="1")),
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
