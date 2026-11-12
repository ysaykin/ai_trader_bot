"""
Global Header and Telemetry Bar component.
Fully responsive across mobile, tablet, and desktop screens.
"""

import reflex as rx
from ..state import DashboardState
from ..styles.theme import (
    SURFACE_DARK,
    BORDER_COLOR,
    CYAN,
    TEXT_MUTED,
    TEXT_PRIMARY,
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
                    border="1px solid rgba(0, 212, 178, 0.3)",
                    border_radius="6px",
                    flex_shrink=0,
                ),
                rx.vstack(
                    rx.flex(
                        rx.heading("LUMIBOT", size="4", weight="bold", color=TEXT_PRIMARY, letter_spacing="0.05em"),
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
                    rx.hstack(
                        rx.text("Account data", size="1", color=TEXT_MUTED),
                        rx.badge(DashboardState.broker_sync_status,
                                 color_scheme=rx.cond(DashboardState.broker_sync_status == "ready", "jade", "amber")),
                        rx.text(DashboardState.broker_sync_message, size="1", color=TEXT_MUTED),
                        wrap="wrap", align="center", spacing="2",
                    ),
                    rx.cond(DashboardState.broker_last_known != "",
                            rx.text(DashboardState.broker_last_known, size="1", color=TEXT_MUTED)),
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
                    rx.icon("activity", size=16, color=rx.cond(DashboardState.bot_badge_color == "amber", "#F59E0B", TEXT_MUTED)),
                    rx.vstack(
                        rx.hstack(
                            rx.text("BOT ENGINE", size="1", color=TEXT_MUTED, weight="bold"),
                            rx.badge(DashboardState.bot_state, color_scheme=DashboardState.bot_badge_color, size="1"),
                            spacing="1",
                        ),
                        rx.text(DashboardState.bot_timestamp, size="1", color=TEXT_MUTED, class_name="tabular-nums"),
                        spacing="0",
                        align="start",
                    ),
                    padding="6px 10px",
                    background_color="rgba(16, 185, 129, 0.08)",
                    border="1px solid rgba(16, 185, 129, 0.2)",
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
                    content="Toggle dashboard snapshot updates",
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
                        aria_label="Refresh dashboard snapshots",
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

        rx.flex(
            rx.hstack(rx.text("Equity", size="1", color=TEXT_MUTED),
                      rx.text(DashboardState.equity_str, size="2", class_name="tabular-nums"), spacing="2"),
            rx.hstack(rx.text("Cash", size="1", color=TEXT_MUTED),
                      rx.text(DashboardState.cash_str, size="2", class_name="tabular-nums"), spacing="2"),
            rx.hstack(rx.text("Option buying power", size="1", color=TEXT_MUTED),
                      rx.text(DashboardState.buying_power_str, size="2", class_name="tabular-nums"), spacing="2"),
            gap="24px", wrap="wrap", padding="10px 24px", width="100%", border_bottom=f"1px solid {BORDER_COLOR}",
        ),
        width="100%",
        spacing="0",
    )
