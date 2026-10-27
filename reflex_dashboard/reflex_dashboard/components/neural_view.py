"""
Tab 2: Neural Policy & Counterfactual Evaluation (ML-01 through ML-07).
Fully responsive across mobile, tablet, and desktop screens.
"""

import reflex as rx
from ..state import DashboardState
from ..styles.theme import (
    SURFACE_DARK,
    BORDER_COLOR,
    CYAN,
    INDIGO,
    GREEN,
    AMBER,
    RED,
    TEXT_MUTED,
    TEXT_PRIMARY,
    KPI_CARD_STYLE,
)


def neural_view() -> rx.Component:
    return rx.vstack(
        # 4 Telemetry Cards - Responsive Grid
        rx.grid(
            # Card 1: Latent Market Regime
            rx.box(
                rx.vstack(
                    rx.text("LATENT MARKET REGIME", size="1", color=TEXT_MUTED, weight="bold"),
                    rx.heading(DashboardState.market_regime, size={"initial": "5", "sm": "6"}, color=INDIGO),
                    rx.text("Classifier: ML-02 Hidden State", size="1", color=TEXT_MUTED),
                    spacing="1",
                ),
                style=KPI_CARD_STYLE,
            ),
            # Card 2: Dynamic Profit Target
            rx.box(
                rx.vstack(
                    rx.text("DYNAMIC PROFIT TARGET", size="1", color=TEXT_MUTED, weight="bold"),
                    rx.heading(DashboardState.dynamic_pt, size={"initial": "5", "sm": "6"}, color=CYAN, class_name="tabular-nums"),
                    rx.text("Overrides static thresholds", size="1", color=TEXT_MUTED),
                    spacing="1",
                ),
                style=KPI_CARD_STYLE,
            ),
            # Card 3: Contextual Grid Selector
            rx.box(
                rx.vstack(
                    rx.text("CONTEXTUAL SELECTOR", size="1", color=TEXT_MUTED, weight="bold"),
                    rx.heading(DashboardState.contextual_selector, size={"initial": "5", "sm": "6"}, color=TEXT_PRIMARY),
                    rx.text("Optimal variant for current volatility", size="1", color=TEXT_MUTED),
                    spacing="1",
                ),
                style=KPI_CARD_STYLE,
            ),
            # Card 4: Counterfactual Regret
            rx.box(
                rx.vstack(
                    rx.text("COUNTERFACTUAL REGRET", size="1", color=TEXT_MUTED, weight="bold"),
                    rx.heading(DashboardState.regret_score, size={"initial": "5", "sm": "6"}, color=GREEN, class_name="tabular-nums"),
                    rx.text(DashboardState.capture_efficiency, size="1", color=TEXT_MUTED),
                    spacing="1",
                ),
                style=KPI_CARD_STYLE,
            ),
            columns={"initial": "1", "xs": "2", "md": "4"},
            spacing="3",
            width="100%",
        ),

        # Neural Policy Recommendation & Action Distribution Panel
        rx.box(
            rx.vstack(
                rx.flex(
                    rx.hstack(
                        rx.icon("brain-circuit", size=20, color=INDIGO),
                        rx.heading("🎯 Real-Time Neural Policy Recommendation", size="3", color=TEXT_PRIMARY, weight="bold"),
                        align="center",
                        spacing="2",
                        wrap="wrap",
                    ),
                    rx.badge(f"ACTION: {DashboardState.recommendation_action}", color_scheme="indigo", variant="solid", size="2"),
                    direction={"initial": "column", "sm": "row"},
                    justify="between",
                    align={"initial": "start", "sm": "center"},
                    width="100%",
                    gap="2",
                ),
                rx.text(
                    "Action probability distribution inferred from multi-head policy net across open contracts:",
                    size="2",
                    color=TEXT_MUTED,
                ),

                # Action 1: HOLD
                rx.vstack(
                    rx.hstack(
                        rx.text("HOLD (Maintain Open Contracts)", size="2", weight="bold"),
                        rx.spacer(),
                        rx.text(f"{DashboardState.action_prob_hold}%", size="2", weight="bold", color=CYAN, class_name="tabular-nums"),
                        width="100%",
                    ),
                    rx.progress(value=DashboardState.action_prob_hold, color_scheme="cyan", size="3", radius="small", width="100%"),
                    spacing="1",
                    width="100%",
                ),

                # Action 2: ADD_POSITION
                rx.vstack(
                    rx.hstack(
                        rx.text("ADD_POSITION (Scale In Mean-Reversion)", size="2", weight="bold"),
                        rx.spacer(),
                        rx.text(f"{DashboardState.action_prob_add}%", size="2", weight="bold", color=GREEN, class_name="tabular-nums"),
                        width="100%",
                    ),
                    rx.progress(value=DashboardState.action_prob_add, color_scheme="jade", size="3", radius="small", width="100%"),
                    spacing="1",
                    width="100%",
                ),

                # Action 3: TRIM_POSITION
                rx.vstack(
                    rx.hstack(
                        rx.text("TRIM_POSITION (Cut Size by 50% on Wing Stress)", size="2", weight="bold"),
                        rx.spacer(),
                        rx.text(f"{DashboardState.action_prob_trim}%", size="2", weight="bold", color=AMBER, class_name="tabular-nums"),
                        width="100%",
                    ),
                    rx.progress(value=DashboardState.action_prob_trim, color_scheme="amber", size="3", radius="small", width="100%"),
                    spacing="1",
                    width="100%",
                ),

                # Action 4: FLATTEN / TAKE PROFIT
                rx.vstack(
                    rx.hstack(
                        rx.text("FLATTEN / TAKE PROFIT (Lock Gains Ahead of Adverse Shift)", size="2", weight="bold"),
                        rx.spacer(),
                        rx.text(f"{DashboardState.action_prob_flatten}%", size="2", weight="bold", color=RED, class_name="tabular-nums"),
                        width="100%",
                    ),
                    rx.progress(value=DashboardState.action_prob_flatten, color_scheme="crimson", size="3", radius="small", width="100%"),
                    spacing="1",
                    width="100%",
                ),

                spacing="4",
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
