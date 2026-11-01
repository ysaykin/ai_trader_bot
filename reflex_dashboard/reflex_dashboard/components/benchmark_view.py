"""
Tab 3: S&P 500 Benchmark Comparison Lab.
Fully responsive across mobile, tablet, and desktop screens.
"""

import reflex as rx
from ..styles.theme import (
    SURFACE_DARK,
    BORDER_COLOR,
    CYAN,
    INDIGO,
    GREEN,
    RED,
    CRIMSON,
    TEXT_MUTED,
    TEXT_PRIMARY,
    KPI_CARD_STYLE,
)


def benchmark_view() -> rx.Component:
    return rx.vstack(
        # Comparison Scorecards - Responsive Grid
        rx.grid(
            rx.box(
                rx.vstack(
                    rx.text("LUMIBOT RETURN", size="1", color=TEXT_MUTED, weight="bold"),
                    rx.heading("+0.15%", size={"initial": "5", "sm": "6"}, color=GREEN, class_name="tabular-nums"),
                    rx.text("14 Closed Trades", size="1", color=TEXT_MUTED),
                    spacing="1",
                ),
                style=KPI_CARD_STYLE,
            ),
            rx.box(
                rx.vstack(
                    rx.text("S&P 500 (SPY)", size="1", color=TEXT_MUTED, weight="bold"),
                    rx.heading("+0.08%", size={"initial": "5", "sm": "6"}, color=CYAN, class_name="tabular-nums"),
                    rx.text("Buy & Hold", size="1", color=TEXT_MUTED),
                    spacing="1",
                ),
                style=KPI_CARD_STYLE,
            ),
            rx.box(
                rx.vstack(
                    rx.text("ALPHA (α)", size="1", color=TEXT_MUTED, weight="bold"),
                    rx.heading("+0.07%", size={"initial": "5", "sm": "6"}, color=GREEN, class_name="tabular-nums"),
                    rx.text("Excess Return", size="1", color=TEXT_MUTED),
                    spacing="1",
                ),
                style=KPI_CARD_STYLE,
            ),
            rx.box(
                rx.vstack(
                    rx.text("BETA (β)", size="1", color=TEXT_MUTED, weight="bold"),
                    rx.heading("0.12", size={"initial": "5", "sm": "6"}, color=INDIGO, class_name="tabular-nums"),
                    rx.text("Delta Neutral", size="1", color=TEXT_MUTED),
                    spacing="1",
                ),
                style=KPI_CARD_STYLE,
            ),
            rx.box(
                rx.vstack(
                    rx.text("SHARPE RATIO", size="1", color=TEXT_MUTED, weight="bold"),
                    rx.heading("2.45", size={"initial": "5", "sm": "6"}, color=GREEN, class_name="tabular-nums"),
                    rx.text("Annualized", size="1", color=TEXT_MUTED),
                    spacing="1",
                ),
                style=KPI_CARD_STYLE,
            ),
            columns={"initial": "2", "sm": "3", "md": "5"},
            spacing="3",
            width="100%",
        ),

        # Benchmark Overview Panel - Responsive Grid
        rx.box(
            rx.vstack(
                rx.flex(
                    rx.hstack(
                        rx.icon("trending-up", size=18, color=CYAN),
                        rx.heading("S&P 500 Benchmark Comparison", size="3", color=TEXT_PRIMARY, weight="bold"),
                        align="center",
                        spacing="2",
                        wrap="wrap",
                    ),
                    rx.badge("Delta-Neutral Alpha", color_scheme="cyan", variant="surface", size="1"),
                    direction={"initial": "column", "sm": "row"},
                    justify="between",
                    align={"initial": "start", "sm": "center"},
                    width="100%",
                    gap="2",
                ),
                rx.text(
                    "The 20-Variant Iron Condor grid achieves an uncorrelated return profile by harvesting implied volatility risk premium on SPX/XSP without directional bias.",
                    size="2",
                    color=TEXT_MUTED,
                ),
                rx.grid(
                    rx.box(
                        rx.text("Sortino Ratio", size="1", color=TEXT_MUTED),
                        rx.text("3.82", size="3", weight="bold", color=GREEN, class_name="tabular-nums"),
                        padding="10px 12px",
                        background_color="#0C101A",
                        border_radius="6px",
                    ),
                    rx.box(
                        rx.text("Max Strategy Drawdown", size="1", color=TEXT_MUTED),
                        rx.text("-0.19%", size="3", weight="bold", color=CRIMSON, class_name="tabular-nums"),
                        padding="10px 12px",
                        background_color="#0C101A",
                        border_radius="6px",
                    ),
                    rx.box(
                        rx.text("Max S&P 500 Drawdown", size="1", color=TEXT_MUTED),
                        rx.text("-1.42%", size="3", weight="bold", color=CRIMSON, class_name="tabular-nums"),
                        padding="10px 12px",
                        background_color="#0C101A",
                        border_radius="6px",
                    ),
                    rx.box(
                        rx.text("Correlation with SPY", size="1", color=TEXT_MUTED),
                        rx.text("0.08", size="3", weight="bold", color=CYAN, class_name="tabular-nums"),
                        padding="10px 12px",
                        background_color="#0C101A",
                        border_radius="6px",
                    ),
                    columns={"initial": "1", "xs": "2", "md": "4"},
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
            width="100%",
        ),
        spacing="4",
        width="100%",
    )
