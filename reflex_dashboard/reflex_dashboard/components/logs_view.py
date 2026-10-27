"""
Tab 8: Real-Time Terminal Log Console.
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


def logs_view() -> rx.Component:
    return rx.box(
        rx.vstack(
            rx.flex(
                rx.hstack(
                    rx.icon("terminal", size=20, color=CYAN),
                    rx.heading("Real-Time Engine Log Stream", size="3", color=TEXT_PRIMARY, weight="bold"),
                    align="center",
                    spacing="2",
                ),
                # Log Search / Filter Input
                rx.hstack(
                    rx.icon("search", size=16, color=TEXT_MUTED),
                    rx.input(
                        placeholder="Filter logs (e.g. FILLED, ERROR, IC_V01)...",
                        value=DashboardState.log_filter,
                        on_change=DashboardState.set_filter,
                        size="1",
                        width={"initial": "100%", "sm": "280px"},
                    ),
                    align="center",
                    spacing="2",
                    width={"initial": "100%", "sm": "auto"},
                ),
                direction={"initial": "column", "sm": "row"},
                justify="between",
                align={"initial": "start", "sm": "center"},
                width="100%",
                gap="3",
            ),

            # Terminal Monospace Box
            rx.box(
                rx.vstack(
                    rx.foreach(
                        DashboardState.logs,
                        lambda line: rx.text(
                            line,
                            size="1",
                            class_name="font-mono",
                            style={"white_space": "pre-wrap", "word_break": "break-all", "color": "#E2E8F0"},
                        ),
                    ),
                    spacing="1",
                    align="start",
                    width="100%",
                ),
                class_name="terminal-console",
                height={"initial": "400px", "sm": "550px"},
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
    )
