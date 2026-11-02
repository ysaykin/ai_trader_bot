"""
Global Header and Telemetry Bar component.
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
    TEXT_MUTED,
    TEXT_PRIMARY,
    KPI_CARD_STYLE,
)


def header() -> rx.Component:
    return rx.vstack(
        # Top Global Navigation Bar - Responsive Stack
        rx.flex(
            # Brand & Title
            rx.hstack(
                rx.box(
                    rx.icon("activity", size=24, color=CYAN),
                    padding="6px",
                    background_color="rgba(0, 212, 178, 0.1)",
                    border=f"1px solid rgba(0, 212, 178, 0.3)",
                    border_radius="6px",
                    flex_shrink=0,
                ),
                rx.vstack(
                    rx.flex(
                        rx.heading("LUMIBOT", size="4", weight="bold", color=TEXT_PRIMARY, letter_spacing="0.05em"),
                        rx.badge("QUANT OS v2.0", color_scheme="cyan", variant="surface", size="1"),
                        rx.badge(DashboardState.broker_name, color_scheme="indigo", variant="surface", size="1"),
                        rx.cond(
                            DashboardState.is_paper,
                            rx.badge("PAPER", color_scheme="amber", variant="soft", size="1"),
                            rx.badge("LIVE", color_scheme="crimson", variant="solid", size="1"),
                        ),
                        align="center",
                        gap="2",
                        wrap="wrap",
                    ),
                    rx.text("Options positions & strategy performance", size="1", color=TEXT_MUTED),
                    spacing="0",
                    align="start",
                ),
                align="center",
                spacing="3",
            ),

            # Status Indicators: Heartbeat, Market Clock, Refresh Button
            rx.flex(
                # Engine Heartbeat
                rx.hstack(
                    rx.box(class_name="pulse-green"),
                    rx.vstack(
                        rx.hstack(
                            rx.text("BOT ENGINE", size="1", color=TEXT_MUTED, weight="bold"),
                            rx.badge(DashboardState.bot_state, color_scheme="jade", size="1"),
                            spacing="1",
                        ),
                        rx.text(DashboardState.bot_timestamp, size="1", color=TEXT_MUTED, class_name="tabular-nums"),
                        spacing="0",
                        align="start",
                    ),
                    padding="6px 10px",
                    background_color="rgba(16, 185, 129, 0.08)",
                    border=f"1px solid rgba(16, 185, 129, 0.2)",
                    border_radius="6px",
                    align="center",
                    spacing="2",
                ),
                # Market Clock
                rx.hstack(
                    rx.icon("clock", size=16, color=CYAN),
                    rx.vstack(
                        rx.hstack(
                            rx.text("SESSION", size="1", color=TEXT_MUTED, weight="bold"),
                            rx.badge(DashboardState.market_status, color_scheme="cyan", size="1"),
                            spacing="1",
                        ),
                        rx.text(DashboardState.market_time, size="1", color=TEXT_PRIMARY, weight="bold", class_name="tabular-nums"),
                        spacing="0",
                        align="start",
                    ),
                    padding="6px 10px",
                    background_color=SURFACE_DARK,
                    border=f"1px solid {BORDER_COLOR}",
                    border_radius="6px",
                    align="center",
                    spacing="2",
                ),
                # Live Polling Indicator & Toggle
                rx.tooltip(
                    rx.badge(
                        rx.cond(
                            DashboardState.auto_refresh,
                            rx.hstack(rx.box(class_name="pulse-green"), rx.text("LIVE SYNC", size="1"), spacing="1", align="center"),
                            rx.hstack(rx.text("PAUSED", size="1"), spacing="1", align="center"),
                        ),
                        color_scheme=rx.cond(DashboardState.auto_refresh, "jade", "gray"),
                        variant="surface",
                        size="2",
                        cursor="pointer",
                        on_click=DashboardState.toggle_auto_refresh,
                    ),
                    content="Toggle 3s live WebSocket telemetry stream",
                ),
                # Manual Refresh Button
                rx.tooltip(
                    rx.icon_button(
                        rx.icon("refresh-cw", size=18),
                        on_click=DashboardState.load_all,
                        variant="surface",
                        color_scheme="cyan",
                        size="2",
                        cursor="pointer",
                    ),
                    content="Refresh Broker & Log Telemetry",
                ),
                align="center",
                gap="2",
                wrap="wrap",
            ),
            direction={"initial": "column", "md": "row"},
            justify="between",
            align={"initial": "start", "md": "center"},
            width="100%",
            gap="3",
            padding={"initial": "12px 16px", "sm": "14px 24px"},
            border_bottom=f"1px solid {BORDER_COLOR}",
            background_color=SURFACE_DARK,
        ),

        # 4 Primary Telemetry Stat Cards - Responsive Grid
        rx.grid(
            # Card 1: Account Equity
            rx.box(
                rx.vstack(
                    rx.hstack(
                        rx.text("ACCOUNT EQUITY", size="1", color=TEXT_MUTED, weight="bold", letter_spacing="0.05em"),
                        rx.spacer(),
                        rx.icon("dollar-sign", size=16, color=CYAN),
                        width="100%",
                        align="center",
                    ),
                    rx.heading(numeric_text(DashboardState.equity_str), size={"initial": "5", "sm": "6", "md": "7"}, weight="bold", color=numeric_color(DashboardState.equity_str), class_name="tabular-nums"),
                    rx.hstack(
                        rx.badge("Tradier Sandbox", color_scheme="gray", variant="soft", size="1"),
                        rx.text("Base: $100k", size="1", color=TEXT_MUTED, class_name="tabular-nums"),
                        align="center",
                        spacing="2",
                        wrap="wrap",
                    ),
                    spacing="1",
                    align="start",
                ),
                style=KPI_CARD_STYLE,
            ),
            # Card 2: Available Cash
            rx.box(
                rx.vstack(
                    rx.hstack(
                        rx.text("AVAILABLE CASH", size="1", color=TEXT_MUTED, weight="bold", letter_spacing="0.05em"),
                        rx.spacer(),
                        rx.icon("wallet", size=16, color=GREEN),
                        width="100%",
                        align="center",
                    ),
                    rx.heading(numeric_text(DashboardState.cash_str), size={"initial": "5", "sm": "6", "md": "7"}, weight="bold", color=numeric_color(DashboardState.cash_str), class_name="tabular-nums"),
                    rx.text("Settled funds ready", size="1", color=TEXT_MUTED),
                    spacing="1",
                    align="start",
                ),
                style=KPI_CARD_STYLE,
            ),
            # Card 3: Option Buying Power
            rx.box(
                rx.vstack(
                    rx.hstack(
                        rx.text("OPTION BUYING POWER", size="1", color=TEXT_MUTED, weight="bold", letter_spacing="0.05em"),
                        rx.spacer(),
                        rx.icon("zap", size=16, color=CYAN),
                        width="100%",
                        align="center",
                    ),
                    rx.heading(numeric_text(DashboardState.buying_power_str), size={"initial": "5", "sm": "6", "md": "7"}, weight="bold", color=numeric_color(DashboardState.buying_power_str), class_name="tabular-nums"),
                    rx.hstack(
                        rx.badge("Margin", color_scheme="cyan", variant="soft", size="1"),
                        rx.text(f"Req: {DashboardState.option_requirement_str}", size="1", color=TEXT_MUTED, class_name="tabular-nums"),
                        align="center",
                        spacing="2",
                        wrap="wrap",
                    ),
                    spacing="1",
                    align="start",
                ),
                style=KPI_CARD_STYLE,
            ),
            # Card 4: Active Incubator Grid
            rx.box(
                rx.vstack(
                    rx.hstack(
                        rx.text("ACTIVE INCUBATOR GRID", size="1", color=TEXT_MUTED, weight="bold", letter_spacing="0.05em"),
                        rx.spacer(),
                        rx.icon("cpu", size=16, color=CYAN),
                        width="100%",
                        align="center",
                    ),
                    rx.heading(DashboardState.active_grid_str, size={"initial": "5", "sm": "6", "md": "7"}, weight="bold", color=CYAN, class_name="tabular-nums"),
                    rx.hstack(
                        rx.badge("EXP-05", color_scheme="indigo", variant="surface", size="1"),
                        rx.text(f"{DashboardState.configured_variant_count} configured", size="1", color=TEXT_MUTED),
                        align="center",
                        spacing="2",
                        wrap="wrap",
                    ),
                    spacing="1",
                    align="start",
                ),
                style=KPI_CARD_STYLE,
            ),
            columns={"initial": "1", "xs": "2", "md": "4"},
            spacing="3",
            width="100%",
            padding={"initial": "12px 16px 6px 16px", "sm": "16px 24px 8px 24px"},
        ),
        width="100%",
        spacing="0",
    )
