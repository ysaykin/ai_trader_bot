"""
Tab 9: Interactive User Features Matrix.
Fully responsive across mobile, tablet, and desktop screens with horizontal table scrolling.
"""

import reflex as rx
from ..styles.theme import (
    SURFACE_DARK,
    BORDER_COLOR,
    CYAN,
    TEXT_MUTED,
    TEXT_PRIMARY,
    TABLE_HEADER_STYLE,
    TABLE_ROW_STYLE,
)

FEATURES_DATA = [
    {"code": "GUI-01", "name": "Global Telemetry Bar", "domain": "Dashboard Core", "status": "VERIFIED LIVE", "desc": "Persistent account equity, cash, option BP, and market clock."},
    {"code": "GUI-02", "name": "Capital Protection Gauges", "domain": "Risk Oversight", "status": "VERIFIED LIVE", "desc": "RSK-01 to RSK-04 visual risk ceiling and sizing progress meters."},
    {"code": "EXP-05", "name": "20-Strategy Incubator Grid", "domain": "Incubator Lab", "status": "VERIFIED LIVE", "desc": "Concurrent forward-testing of 20 distinct parameter variants with fee accounting."},
    {"code": "RSK-01", "name": "$5,000 Per-Strategy Open Risk Cap", "domain": "Capital Safety", "status": "VERIFIED LIVE", "desc": "Enforces $5,000 maximum defined risk ceiling on any individual strategy variant."},
    {"code": "RSK-02", "name": "Tiered Single-Trade Sizing", "domain": "Capital Safety", "status": "VERIFIED LIVE", "desc": "Allocates max 30% risk on small accounts scaling down to 5% on >$50k."},
    {"code": "RSK-03", "name": "Intelligent Contract Sizing", "domain": "Order Execution", "status": "VERIFIED LIVE", "desc": "Automatically clips contract quantities to fit capital protection rules."},
    {"code": "RSK-04", "name": "Buying Power Headroom", "domain": "Margin Safety", "status": "VERIFIED LIVE", "desc": "Ensures broker margin requirements never breach available capital."},
    {"code": "ML-01", "name": "State Feature Processing", "domain": "Neural Engine", "status": "VERIFIED LIVE", "desc": "Normalizes market features and volatility surface for policy net."},
    {"code": "ML-02", "name": "Multi-Head Policy Network", "domain": "Neural Engine", "status": "VERIFIED LIVE", "desc": "Infers discrete action probabilities (HOLD, ADD, TRIM, FLATTEN)."},
    {"code": "ML-04", "name": "Dynamic Profit Target Head", "domain": "Neural Engine", "status": "VERIFIED LIVE", "desc": "Predicts expected trade value to optimize exit thresholds dynamically."},
    {"code": "ML-05", "name": "Contextual Variant Selector", "domain": "Neural Engine", "status": "VERIFIED LIVE", "desc": "Routes entry capital into optimal grid variant for prevailing regime."},
    {"code": "ML-07", "name": "Counterfactual Regret Evaluator", "domain": "Neural Engine", "status": "VERIFIED LIVE", "desc": "Evaluates policy decisions against the hindsight-optimal incubator variant."},
    {"code": "EXP-01", "name": "A/B Strategy Multi-Execution", "domain": "Incubator Lab", "status": "VERIFIED LIVE", "desc": "Simultaneous execution of multiple strategies on same symbol."},
    {"code": "EXP-03", "name": "Deterministic Order Tagging", "domain": "Ledger Isolation", "status": "VERIFIED LIVE", "desc": "Prevents cross-talk between strategies using isolated order tags."},
]


def user_features_view() -> rx.Component:
    return rx.box(
        rx.vstack(
            rx.flex(
                rx.hstack(
                    rx.icon("check-check", size=20, color=CYAN),
                    rx.heading("Interactive User Features Matrix", size="3", color=TEXT_PRIMARY, weight="bold"),
                    align="center",
                    spacing="2",
                ),
                rx.badge("14/14 Capabilities Live", color_scheme="jade", variant="surface", size="1"),
                direction={"initial": "column", "sm": "row"},
                justify="between",
                align={"initial": "start", "sm": "center"},
                width="100%",
                gap="2",
            ),
            rx.text(
                "Complete index of architectural requirements and user features implemented across the Lumibot platform.",
                size="2",
                color=TEXT_MUTED,
            ),
            rx.box(
                rx.table.root(
                    rx.table.header(
                        rx.table.row(
                            rx.table.column_header_cell("Code", style=TABLE_HEADER_STYLE, class_name="nowrap"),
                            rx.table.column_header_cell("Feature Name", style=TABLE_HEADER_STYLE, class_name="nowrap"),
                            rx.table.column_header_cell("Domain", style=TABLE_HEADER_STYLE, class_name="nowrap"),
                            rx.table.column_header_cell("Description", style=TABLE_HEADER_STYLE, class_name="nowrap"),
                            rx.table.column_header_cell("Status", style=TABLE_HEADER_STYLE, class_name="nowrap"),
                        )
                    ),
                    rx.table.body(
                        *[
                            rx.table.row(
                                rx.table.cell(rx.text(f["code"], weight="bold", color=CYAN, class_name="font-mono nowrap")),
                                rx.table.cell(rx.text(f["name"], weight="bold", class_name="nowrap")),
                                rx.table.cell(rx.badge(f["domain"], color_scheme="indigo", size="1")),
                                rx.table.cell(rx.text(f["desc"], size="2", color=TEXT_MUTED)),
                                rx.table.cell(rx.badge(f["status"], color_scheme="jade", size="1")),
                                style=TABLE_ROW_STYLE,
                            )
                            for f in FEATURES_DATA
                        ]
                    ),
                    width="100%",
                ),
                class_name="table-responsive-container",
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
