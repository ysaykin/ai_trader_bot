"""
Capital Protection and Risk Gauges Component (RSK-01 through RSK-04).
Fully responsive across mobile, tablet, and desktop screens.
"""

import reflex as rx
from ..state import DashboardState
from ..styles.theme import (
    SURFACE_DARK,
    BORDER_COLOR,
    CYAN,
    RED,
    TEXT_MUTED,
    TEXT_PRIMARY,
    GREEN,
)


def risk_gauges_section() -> rx.Component:
    return rx.box(
        rx.vstack(
            # Section Header - Responsive Flex
            rx.flex(
                rx.hstack(
                    rx.icon("shield-check", size=20, color=CYAN),
                    rx.heading("Capital Protection & Risk Oversight", size="3", color=TEXT_PRIMARY, weight="bold"),
                    rx.badge("RSK-01 – RSK-04", color_scheme="cyan", variant="surface", size="1"),
                    align="center",
                    spacing="2",
                    wrap="wrap",
                ),
                # Panic Liquidation Button
                rx.alert_dialog.root(
                    rx.alert_dialog.trigger(
                        rx.button(
                            rx.icon("flame", size=16),
                            "EMERGENCY PANIC LIQUIDATE",
                            color_scheme="crimson",
                            variant="solid",
                            size="2",
                            cursor="pointer",
                            width={"initial": "100%", "sm": "auto"},
                        ),
                    ),
                    rx.alert_dialog.content(
                        rx.alert_dialog.title("Confirm Immediate Position Liquidation"),
                        rx.alert_dialog.description(
                            "Are you sure you want to trigger emergency liquidation? "
                            "This will send exit orders for ALL currently open option contracts "
                            "across all 20 incubator strategies and halt entry cycles.",
                            size="2",
                        ),
                        rx.hstack(
                            rx.alert_dialog.cancel(
                                rx.button("Cancel", variant="soft", color_scheme="gray"),
                            ),
                            rx.alert_dialog.action(
                                rx.button(
                                    "YES, LIQUIDATE ALL POSITIONS",
                                    color_scheme="crimson",
                                    on_click=DashboardState.execute_panic_liquidation,
                                ),
                            ),
                            spacing="3",
                            margin_top="16px",
                            justify="end",
                        ),
                        style={"max_width": "500px", "background_color": SURFACE_DARK, "border": f"1px solid {RED}"},
                    ),
                ),
                direction={"initial": "column", "sm": "row"},
                justify="between",
                align={"initial": "stretch", "sm": "center"},
                width="100%",
                gap="3",
            ),

            # Risk Gauges Grid - 1 column on mobile, 3 on desktop
            rx.grid(
                # Gauge 1: Per-Strategy Open Risk Cap (RSK-01)
                rx.box(
                    rx.vstack(
                        rx.flex(
                            rx.text("PER-STRATEGY RISK CAP (RSK-01)", size="1", color=TEXT_MUTED, weight="bold"),
                            rx.spacer(),
                            rx.text(f"{DashboardState.strategy_risk_str} / {DashboardState.strategy_risk_cap_str} Max", size="1", color=TEXT_PRIMARY, class_name="tabular-nums"),
                            width="100%",
                            align="center",
                            wrap="wrap",
                        ),
                        rx.progress(
                            value=DashboardState.strategy_risk_pct,
                            color_scheme="cyan",
                            size="3",
                            radius="small",
                            width="100%",
                        ),
                        rx.hstack(
                            rx.badge(DashboardState.strategy_risk_pct_str, color_scheme="jade", variant="soft", size="1"),
                            rx.text("Ledger defined risk per individual strategy variant", size="1", color=TEXT_MUTED),
                            align="center",
                            spacing="2",
                            wrap="wrap",
                        ),
                        spacing="2",
                        width="100%",
                    ),
                    padding="12px 14px",
                    background_color="rgba(16, 20, 30, 0.6)",
                    border=f"1px solid {BORDER_COLOR}",
                    border_radius="6px",
                ),

                # Gauge 2: Portfolio Defined Risk Cap
                rx.box(
                    rx.vstack(
                        rx.flex(
                            rx.text("PORTFOLIO RISK CAP", size="1", color=TEXT_MUTED, weight="bold"),
                            rx.spacer(),
                            rx.text(f"{DashboardState.portfolio_risk_str} / {DashboardState.portfolio_risk_cap_str} Max", size="1", color=TEXT_PRIMARY, class_name="tabular-nums"),
                            width="100%",
                            align="center",
                            wrap="wrap",
                        ),
                        rx.progress(
                            value=DashboardState.portfolio_risk_pct,
                            color_scheme="indigo",
                            size="3",
                            radius="small",
                            width="100%",
                        ),
                        rx.hstack(
                            rx.badge(DashboardState.portfolio_risk_pct_str, color_scheme="indigo", variant="soft", size="1"),
                            rx.text("Combined ledger risk across all active variants", size="1", color=TEXT_MUTED),
                            align="center",
                            spacing="2",
                            wrap="wrap",
                        ),
                        spacing="2",
                        width="100%",
                    ),
                    padding="12px 14px",
                    background_color="rgba(16, 20, 30, 0.6)",
                    border=f"1px solid {BORDER_COLOR}",
                    border_radius="6px",
                ),

                # Gauge 3: Option Buying Power & Broker Margin (RSK-04)
                rx.box(
                    rx.vstack(
                        rx.flex(
                            rx.text("BUYING POWER & BROKER MARGIN (RSK-04)", size="1", color=TEXT_MUTED, weight="bold"),
                            rx.spacer(),
                            rx.text(DashboardState.buying_power_headroom_str, size="1", color=GREEN, weight="bold", class_name="tabular-nums"),
                            width="100%",
                            align="center",
                            wrap="wrap",
                        ),
                        rx.progress(
                            value=DashboardState.buying_power_headroom_pct,
                            color_scheme="jade",
                            size="3",
                            radius="small",
                            width="100%",
                        ),
                        rx.hstack(
                            rx.badge("MARGIN", color_scheme="jade", variant="soft", size="1"),
                            rx.text(f"Broker Margin: {DashboardState.broker_margin_str} | BP: {DashboardState.buying_power_str}", size="1", color=TEXT_MUTED, class_name="tabular-nums"),
                            align="center",
                            spacing="2",
                            wrap="wrap",
                        ),
                        spacing="2",
                        width="100%",
                    ),
                    padding="12px 14px",
                    background_color="rgba(16, 20, 30, 0.6)",
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
        padding={"initial": "12px 14px", "sm": "16px 20px"},
        margin={"initial": "0 16px 12px 16px", "sm": "0 24px 16px 24px"},
        background_color=SURFACE_DARK,
        border=f"1px solid {BORDER_COLOR}",
        border_radius="8px",
    )
