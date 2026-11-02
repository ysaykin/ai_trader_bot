"""
Lumibot Reflex Dashboard - Root Application Module.
Assembles global header, risk gauges, Radix tabs navigation, and all 9 views.
Fully responsive across mobile, tablet, and desktop screens.
"""

import reflex as rx
from .state import DashboardState
from .components import (
    header,
    risk_gauges_section,
    incubator_view,
    monitor_view,
    neural_view,
    benchmark_view,
    ab_lab_view,
    positions_view,
    orders_view,
    config_view,
    logs_view,
    user_features_view,
)
from .styles.theme import (
    BG_DARK,
    SURFACE_DARK,
    BORDER_COLOR,
    TEXT_MUTED,
)


def index() -> rx.Component:
    return rx.box(
        rx.vstack(
            # Persistent Header & Telemetry
            header(),

            # Capital Protection & Risk Gauges
            rx.box(
                rx.accordion.root(
                    rx.accordion.item(header="Account risk & emergency controls", content=risk_gauges_section(), value="risk"),
                    type="single", collapsible=True, width="100%",
                ),
                padding_x={"initial": "14px", "sm": "24px"}, width="100%",
            ),

            # 10-Tab Navigation System - Horizontally Scrollable on Mobile
            rx.box(
                rx.tabs.root(
                    rx.tabs.list(
                        rx.tabs.trigger(
                            rx.hstack(rx.icon("layers", size=16), rx.text("Positions", class_name="nowrap"), spacing="2"),
                            value="positions",
                        ),
                        rx.tabs.trigger(
                            rx.hstack(rx.icon("cpu", size=16), rx.text("Incubator Grid", class_name="nowrap"), spacing="2"),
                            value="incubator",
                        ),
                        rx.tabs.trigger(
                            rx.hstack(rx.icon("scan", size=16), rx.text("Scanner / Monitor", class_name="nowrap"), spacing="2"),
                            value="monitor",
                        ),
                        rx.tabs.trigger(
                            rx.hstack(rx.icon("brain-circuit", size=16), rx.text("Neural Policy", class_name="nowrap"), spacing="2"),
                            value="neural",
                        ),
                        rx.tabs.trigger(
                            rx.hstack(rx.icon("trending-up", size=16), rx.text("S&P 500 Benchmark", class_name="nowrap"), spacing="2"),
                            value="benchmark",
                        ),
                        rx.tabs.trigger(
                            rx.hstack(rx.icon("scale", size=16), rx.text("A/B Lab", class_name="nowrap"), spacing="2"),
                            value="ab_lab",
                        ),
                        rx.tabs.trigger(
                            rx.hstack(rx.icon("clipboard-list", size=16), rx.text("Orders", class_name="nowrap"), spacing="2"),
                            value="orders",
                        ),
                        rx.tabs.trigger(
                            rx.hstack(rx.icon("settings", size=16), rx.text("Broker Config", class_name="nowrap"), spacing="2"),
                            value="config",
                        ),
                        rx.tabs.trigger(
                            rx.hstack(rx.icon("terminal", size=16), rx.text("Log Console", class_name="nowrap"), spacing="2"),
                            value="logs",
                        ),
                        rx.tabs.trigger(
                            rx.hstack(rx.icon("check-check", size=16), rx.text("Features Matrix", class_name="nowrap"), spacing="2"),
                            value="user_features",
                        ),
                        size="2",
                        color_scheme="cyan",
                        class_name="tabs-scroll-list",
                    ),

                    # Tab 1: Incubator Grid
                    rx.tabs.content(
                        incubator_view(),
                        value="incubator",
                        padding_top="16px",
                    ),

                    # Tab 2: Scanner & Monitor
                    rx.tabs.content(
                        monitor_view(),
                        value="monitor",
                        padding_top="16px",
                    ),

                    # Tab 3: Neural Policy
                    rx.tabs.content(
                        neural_view(),
                        value="neural",
                        padding_top="16px",
                    ),

                    # Tab 3: Benchmark
                    rx.tabs.content(
                        benchmark_view(),
                        value="benchmark",
                        padding_top="16px",
                    ),

                    # Tab 4: A/B Lab
                    rx.tabs.content(
                        ab_lab_view(),
                        value="ab_lab",
                        padding_top="16px",
                    ),

                    # Tab 5: Positions
                    rx.tabs.content(
                        positions_view(),
                        value="positions",
                        padding_top="16px",
                    ),

                    # Tab 6: Orders
                    rx.tabs.content(
                        orders_view(),
                        value="orders",
                        padding_top="16px",
                    ),

                    # Tab 7: Config
                    rx.tabs.content(
                        config_view(),
                        value="config",
                        padding_top="16px",
                    ),

                    # Tab 8: Logs
                    rx.tabs.content(
                        logs_view(),
                        value="logs",
                        padding_top="16px",
                    ),

                    # Tab 9: User Features Matrix
                    rx.tabs.content(
                        user_features_view(),
                        value="user_features",
                        padding_top="16px",
                    ),

                    value=DashboardState.current_tab,
                    on_change=DashboardState.set_tab,
                    width="100%",
                ),
                padding={"initial": "0 14px 20px 14px", "sm": "0 24px 24px 24px"},
                width="100%",
            ),

            # Responsive Footer
            rx.flex(
                rx.text("Lumibot Quantitative Autonomous Trading Engine", size="1", color=TEXT_MUTED),
                rx.spacer(),
                rx.text("Powered by Reflex Python + Radix UI + Next.js", size="1", color=TEXT_MUTED),
                direction={"initial": "column", "sm": "row"},
                gap="2",
                width="100%",
                padding={"initial": "12px 16px", "sm": "16px 24px"},
                border_top=f"1px solid {BORDER_COLOR}",
                background_color=SURFACE_DARK,
                align={"initial": "start", "sm": "center"},
            ),
            spacing="3",
            width="100%",
        ),
        background_color=BG_DARK,
        min_height="100vh",
        width="100%",
        on_mount=DashboardState.on_load,
    )


# App initialization
app = rx.App(
    stylesheets=[
        "/terminal.css",
    ],
    html_lang="en",
)

app.add_page(
    index,
    route="/",
    title="Lumibot Mission Control - Quantitative Options Dashboard",
    description="Real-time multi-strategy incubator and deep reinforcement learning policy terminal.",
)
