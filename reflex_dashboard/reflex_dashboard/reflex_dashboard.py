"""Lumibot operations dashboard."""
import reflex as rx

from .state import DashboardState
from .components.header import header
from .components.risk_gauges import risk_gauges_section
from .components.positions_view import positions_view
from .components.orders_view import orders_view
from .components.config_view import config_view
from .components.monitoring_view import overview_view, strategies_view, system_view
from .styles.theme import BG_DARK, BORDER_COLOR, TEXT_MUTED


NAVIGATION = (
    ("overview", "Overview", "activity", overview_view),
    ("positions", "Positions", "layers", positions_view),
    ("orders", "Orders", "clipboard-list", orders_view),
    ("strategies", "Strategies", "chart-no-axes-combined", strategies_view),
    ("system", "System health", "heart-pulse", system_view),
    ("config", "Settings", "settings", config_view),
)


def index() -> rx.Component:
    return rx.box(
        rx.vstack(
            header(),
            rx.cond(DashboardState.refresh_error != "",
                    rx.callout(DashboardState.refresh_error, color_scheme="amber", width="100%")),
            rx.box(
                rx.accordion.root(
                    rx.accordion.item(header="Account risk & emergency controls", content=risk_gauges_section(), value="risk"),
                    type="single", collapsible=True, width="100%"),
                padding_x={"initial": "14px", "sm": "24px"}, width="100%"),
            rx.box(
                rx.tabs.root(
                    rx.tabs.list(*[
                        rx.tabs.trigger(rx.hstack(rx.icon(icon, size=16), rx.text(label, class_name="nowrap"), spacing="2"), value=key)
                        for key, label, icon, _ in NAVIGATION
                    ], size="2", color_scheme="cyan", class_name="tabs-scroll-list"),
                    *[rx.tabs.content(view(), value=key, padding_top="16px") for key, _, _, view in NAVIGATION],
                    value=DashboardState.current_tab, on_change=DashboardState.set_tab, width="100%"),
                padding={"initial": "0 14px 20px", "sm": "0 24px 24px"}, width="100%"),
            rx.text("Broker snapshots, confirmed position ownership and verified trade evidence.",
                    size="1", color=TEXT_MUTED, padding="16px 24px", border_top=f"1px solid {BORDER_COLOR}", width="100%"),
            spacing="3", width="100%"),
        background_color=BG_DARK, min_height="100vh", width="100%", on_mount=DashboardState.on_load)


app = rx.App(stylesheets=["/terminal.css"], html_lang="en")
app.add_page(index, route="/", title="Lumibot | Trading overview",
             description="Track positions, orders, verified strategy results and data health.")
