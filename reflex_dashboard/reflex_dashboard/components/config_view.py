"""
Tab 7: Broker & Strategy Configuration View.
Fully responsive across mobile, tablet, and desktop screens.
"""

import reflex as rx
from ..state import DashboardState
from ..styles.theme import (
    SURFACE_DARK,
    BORDER_COLOR,
    CYAN,
    INDIGO,
    TEXT_MUTED,
    TEXT_PRIMARY,
)


def config_view() -> rx.Component:
    return rx.box(
        rx.vstack(
            rx.flex(
                rx.hstack(
                    rx.icon("settings", size=20, color=CYAN),
                    rx.heading("Broker & Strategy Configuration", size="3", color=TEXT_PRIMARY, weight="bold"),
                    align="center",
                    spacing="2",
                    wrap="wrap",
                ),
                rx.badge("Zero-Terminal Administration", color_scheme="cyan", variant="surface", size="1"),
                direction={"initial": "column", "sm": "row"},
                justify="between",
                align={"initial": "start", "sm": "center"},
                width="100%",
                gap="2",
            ),
            rx.text(
                "Modify broker credentials, trading parameters, fee assumptions, and portfolio risk ceilings. "
                "Changes are safely written to the local .env configuration file.",
                size="2",
                color=TEXT_MUTED,
            ),
            rx.divider(color_scheme="gray"),

            # Form Grid - Responsive: 1 column on mobile, 2 on desktop
            rx.grid(
                # Broker Selection
                rx.vstack(
                    rx.text("Broker Platform", size="2", weight="bold"),
                    rx.select(
                        ["tradier", "alpaca"],
                        value=DashboardState.cfg_broker,
                        on_change=DashboardState.set_cfg_broker,
                        color_scheme="cyan",
                        size="2",
                        width="100%",
                    ),
                    rx.text("Selected routing engine", size="1", color=TEXT_MUTED),
                    spacing="1",
                    align="start",
                    width="100%",
                ),

                # Account ID
                rx.vstack(
                    rx.text("Account ID", size="2", weight="bold"),
                    rx.input(
                        value=DashboardState.cfg_account_id,
                        on_change=DashboardState.set_cfg_account_id,
                        placeholder="Broker account identifier",
                        size="2",
                        width="100%",
                    ),
                    rx.text("Broker designated account identifier", size="1", color=TEXT_MUTED),
                    spacing="1",
                    align="start",
                    width="100%",
                ),

                # Access Token
                rx.vstack(
                    rx.text("Access Token / API Key", size="2", weight="bold"),
                    rx.input(
                        type="password",
                        value=DashboardState.cfg_access_token,
                        on_change=DashboardState.set_cfg_access_token,
                        placeholder="Leave blank to keep current token",
                        size="2",
                        width="100%",
                    ),
                    rx.text("Bearer token with trading permissions", size="1", color=TEXT_MUTED),
                    spacing="1",
                    align="start",
                    width="100%",
                ),

                # Target Symbol
                rx.vstack(
                    rx.text("Target Index Symbol", size="2", weight="bold"),
                    rx.select(
                        ["XSP", "SPY"],
                        value=DashboardState.cfg_symbol,
                        on_change=DashboardState.set_cfg_symbol,
                        color_scheme="cyan",
                        size="2",
                        width="100%",
                    ),
                    rx.text("Mini-SPX index options or SPY ETF", size="1", color=TEXT_MUTED),
                    spacing="1",
                    align="start",
                    width="100%",
                ),

                # Option Fee Per Leg
                rx.vstack(
                    rx.text("Option Fee / Contract Leg ($)", size="2", weight="bold"),
                    rx.input(
                        value=DashboardState.cfg_fee_per_contract,
                        on_change=DashboardState.set_cfg_fee_per_contract,
                        placeholder="0.40",
                        size="2",
                        width="100%",
                    ),
                    rx.text("$0.40/leg = $3.20 roundtrip per 4-leg condor", size="1", color=TEXT_MUTED),
                    spacing="1",
                    align="start",
                    width="100%",
                ),

                # Per-Strategy Risk Cap
                rx.vstack(
                    rx.text("Per-Strategy Risk Cap ($)", size="2", weight="bold"),
                    rx.input(
                        value=DashboardState.cfg_risk_cap,
                        on_change=DashboardState.set_cfg_risk_cap,
                        placeholder="15000",
                        size="2",
                        width="100%",
                    ),
                    rx.text("Hard ceiling per individual strategy variant (default: $15,000)", size="1", color=TEXT_MUTED),
                    spacing="1",
                    align="start",
                    width="100%",
                ),

                # Portfolio Risk Cap
                rx.vstack(
                    rx.text("Global Portfolio Risk Cap ($)", size="2", weight="bold"),
                    rx.input(
                        value=DashboardState.cfg_portfolio_risk_cap,
                        on_change=DashboardState.set_cfg_portfolio_risk_cap,
                        placeholder="100000",
                        size="2",
                        width="100%",
                    ),
                    rx.text("Account-wide risk ceiling across all active variants (default: $100,000)", size="1", color=TEXT_MUTED),
                    spacing="1",
                    align="start",
                    width="100%",
                ),

                # Paper / Sandbox Mode Toggle
                rx.vstack(
                    rx.text("Environment Mode", size="2", weight="bold"),
                    rx.select(
                        ["true", "false"],
                        value=DashboardState.cfg_paper,
                        on_change=DashboardState.set_cfg_paper,
                        color_scheme="cyan",
                        size="2",
                        width="100%",
                    ),
                    rx.text("true = Paper / Sandbox, false = Live Production", size="1", color=TEXT_MUTED),
                    spacing="1",
                    align="start",
                    width="100%",
                ),
                columns={"initial": "1", "md": "2"},
                spacing="4",
                width="100%",
            ),

            rx.divider(color_scheme="gray"),

            # Testing Strategy Section
            rx.flex(
                rx.hstack(
                    rx.icon("flask-conical", size=18, color=CYAN),
                    rx.heading("🧪 Testing Strategy Variation (Configurable DTE)", size="3", color=TEXT_PRIMARY, weight="bold"),
                    align="center",
                    spacing="2",
                ),
                rx.badge("IC_V21 Testing Variant", color_scheme="amber", variant="surface", size="1"),
                direction={"initial": "column", "sm": "row"},
                justify="between",
                align={"initial": "start", "sm": "center"},
                width="100%",
                gap="2",
            ),
            rx.text(
                "Customize parameters for the dynamic testing variant (default: 25 DTE). "
                "Adjust expiration horizon, delta, wing width, profit target, and entry time directly from the UI.",
                size="2",
                color=TEXT_MUTED,
            ),
            rx.grid(
                # Testing DTE
                rx.vstack(
                    rx.text("Testing Target DTE", size="2", weight="bold"),
                    rx.input(
                        value=DashboardState.cfg_testing_dte,
                        on_change=DashboardState.set_cfg_testing_dte,
                        placeholder="25",
                        size="2",
                        width="100%",
                    ),
                    rx.text("Expiration target in days (e.g. 25 DTE)", size="1", color=TEXT_MUTED),
                    spacing="1",
                    align="start",
                    width="100%",
                ),
                # Testing Delta
                rx.vstack(
                    rx.text("Short Delta (Δ)", size="2", weight="bold"),
                    rx.input(
                        value=DashboardState.cfg_testing_delta,
                        on_change=DashboardState.set_cfg_testing_delta,
                        placeholder="0.15",
                        size="2",
                        width="100%",
                    ),
                    rx.text("Option delta for short put/call strikes (e.g. 0.15)", size="1", color=TEXT_MUTED),
                    spacing="1",
                    align="start",
                    width="100%",
                ),
                # Testing Wing Width
                rx.vstack(
                    rx.text("Wing Width ($)", size="2", weight="bold"),
                    rx.input(
                        value=DashboardState.cfg_testing_wing,
                        on_change=DashboardState.set_cfg_testing_wing,
                        placeholder="5.0",
                        size="2",
                        width="100%",
                    ),
                    rx.text("Strike distance between short and long legs (e.g. $5.0)", size="1", color=TEXT_MUTED),
                    spacing="1",
                    align="start",
                    width="100%",
                ),
                # Testing Profit Target
                rx.vstack(
                    rx.text("Profit Target (%)", size="2", weight="bold"),
                    rx.input(
                        value=DashboardState.cfg_testing_pt,
                        on_change=DashboardState.set_cfg_testing_pt,
                        placeholder="0.50",
                        size="2",
                        width="100%",
                    ),
                    rx.text("Take-profit threshold as fraction of credit (e.g. 0.50 = 50%)", size="1", color=TEXT_MUTED),
                    spacing="1",
                    align="start",
                    width="100%",
                ),
                # Testing Stop Loss Multiplier
                rx.vstack(
                    rx.text("Stop Loss Multiplier", size="2", weight="bold"),
                    rx.input(
                        value=DashboardState.cfg_testing_sl,
                        on_change=DashboardState.set_cfg_testing_sl,
                        placeholder="2.0",
                        size="2",
                        width="100%",
                    ),
                    rx.text("Loss exit as multiple of credit (e.g. 2.0x)", size="1", color=TEXT_MUTED),
                    spacing="1",
                    align="start",
                    width="100%",
                ),
                # Testing Entry Time
                rx.vstack(
                    rx.text("Entry Time (ET)", size="2", weight="bold"),
                    rx.input(
                        value=DashboardState.cfg_testing_entry,
                        on_change=DashboardState.set_cfg_testing_entry,
                        placeholder="10:30",
                        size="2",
                        width="100%",
                    ),
                    rx.text("Daily market entry window start (e.g. 10:30)", size="1", color=TEXT_MUTED),
                    spacing="1",
                    align="start",
                    width="100%",
                ),
                # Testing Option Structure (Iron Condor vs Vertical Spreads)
                rx.vstack(
                    rx.text("Option Structure", size="2", weight="bold"),
                    rx.select(
                        ["iron_condor", "bull_put", "bear_call"],
                        value=DashboardState.cfg_testing_structure,
                        on_change=DashboardState.set_cfg_testing_structure,
                        color_scheme="amber",
                        size="2",
                        width="100%",
                    ),
                    rx.text("Trade 4-leg condor, 2-leg bull put, or 2-leg bear call", size="1", color=TEXT_MUTED),
                    spacing="1",
                    align="start",
                    width="100%",
                ),
                # Standalone Verticals Grid Toggle
                rx.vstack(
                    rx.text("Verticals Grid Inclusion", size="2", weight="bold"),
                    rx.select(
                        ["true", "false"],
                        value=DashboardState.cfg_verticals_enabled,
                        on_change=DashboardState.set_cfg_verticals_enabled,
                        color_scheme="cyan",
                        size="2",
                        width="100%",
                    ),
                    rx.text("Include IC_V23 & IC_V24 standalone verticals in incubator", size="1", color=TEXT_MUTED),
                    spacing="1",
                    align="start",
                    width="100%",
                ),
                columns={"initial": "1", "sm": "2", "md": "3"},
                spacing="4",
                width="100%",
            ),

            rx.divider(color_scheme="gray"),

            # Pure Neural Strategy Section
            rx.flex(
                rx.hstack(
                    rx.icon("brain-circuit", size=18, color=INDIGO),
                    rx.heading("🧠 Pure Neural Network Managed Strategy", size="3", color=TEXT_PRIMARY, weight="bold"),
                    align="center",
                    spacing="2",
                ),
                rx.badge("IC_V22 Pure AI Managed", color_scheme="indigo", variant="solid", size="1"),
                direction={"initial": "column", "sm": "row"},
                justify="between",
                align={"initial": "start", "sm": "center"},
                width="100%",
                gap="2",
            ),
            rx.text(
                "Dedicated benchmark strategy purely governed by the Multi-Head Policy Network. "
                "Exits, scaling, and dynamic profit targets are driven in real-time by neural predictions "
                "and evaluated directly against fixed-config variants in the A/B Testing Lab.",
                size="2",
                color=TEXT_MUTED,
            ),
            rx.grid(
                # Neural Strategy Toggle
                rx.vstack(
                    rx.text("Neural Strategy Mode", size="2", weight="bold"),
                    rx.select(
                        ["true", "false"],
                        value=DashboardState.cfg_neural_enabled,
                        on_change=DashboardState.set_cfg_neural_enabled,
                        color_scheme="indigo",
                        size="2",
                        width="100%",
                    ),
                    rx.text("Enable/disable dedicated neural-managed strategy", size="1", color=TEXT_MUTED),
                    spacing="1",
                    align="start",
                    width="100%",
                ),
                # Neural DTE
                rx.vstack(
                    rx.text("Neural Target DTE", size="2", weight="bold"),
                    rx.input(
                        value=DashboardState.cfg_neural_dte,
                        on_change=DashboardState.set_cfg_neural_dte,
                        placeholder="7",
                        size="2",
                        width="100%",
                    ),
                    rx.text("Expiration target for neural contracts (e.g. 7 DTE)", size="1", color=TEXT_MUTED),
                    spacing="1",
                    align="start",
                    width="100%",
                ),
                # Neural Delta
                rx.vstack(
                    rx.text("Baseline Delta (Δ)", size="2", weight="bold"),
                    rx.input(
                        value=DashboardState.cfg_neural_delta,
                        on_change=DashboardState.set_cfg_neural_delta,
                        placeholder="0.15",
                        size="2",
                        width="100%",
                    ),
                    rx.text("Initial delta selection before neural management", size="1", color=TEXT_MUTED),
                    spacing="1",
                    align="start",
                    width="100%",
                ),
                # Neural Wing
                rx.vstack(
                    rx.text("Baseline Wing ($)", size="2", weight="bold"),
                    rx.input(
                        value=DashboardState.cfg_neural_wing,
                        on_change=DashboardState.set_cfg_neural_wing,
                        placeholder="5.0",
                        size="2",
                        width="100%",
                    ),
                    rx.text("Wing width in index points (e.g. $5.0)", size="1", color=TEXT_MUTED),
                    spacing="1",
                    align="start",
                    width="100%",
                ),
                columns={"initial": "1", "sm": "2", "md": "4"},
                spacing="4",
                width="100%",
            ),

            rx.divider(color_scheme="gray"),

            # Action Buttons - Full width on mobile
            rx.flex(
                rx.button(
                    rx.icon("save", size=16),
                    "Save Configuration",
                    color_scheme="cyan",
                    variant="solid",
                    on_click=DashboardState.save_configuration,
                    size="3",
                    cursor="pointer",
                    width={"initial": "100%", "sm": "auto"},
                ),
                rx.button(
                    rx.icon("plug", size=16),
                    "Test Broker Connection",
                    color_scheme="indigo",
                    variant="soft",
                    on_click=DashboardState.load_all,
                    size="3",
                    cursor="pointer",
                    width={"initial": "100%", "sm": "auto"},
                ),
                direction={"initial": "column", "sm": "row"},
                spacing="3",
                width="100%",
            ),
            spacing="4",
            width="100%",
        ),
        padding={"initial": "14px", "sm": "24px"},
        background_color=SURFACE_DARK,
        border=f"1px solid {BORDER_COLOR}",
        border_radius="8px",
        width="100%",
    )
