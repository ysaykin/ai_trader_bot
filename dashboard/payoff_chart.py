"""Iron Condor Payoff and Risk Gauge Visualizer using Plotly."""
import numpy as np
import plotly.graph_objects as go
from typing import Optional, Dict, Any


def calculate_condor_curve(
    underlying_price: float,
    long_put: float,
    short_put: float,
    short_call: float,
    long_call: float,
    net_credit: float,
    qty: int = 1,
    price_span_min: float = 0.0,
    price_span_max: float = 0.0,
    num_points: int = 300,
):
    """Compute expiration P&L for a single Iron Condor configuration."""
    wing = short_put - long_put
    prices = np.linspace(price_span_min, price_span_max, num_points)
    pnl = []
    for s in prices:
        lp_val = max(long_put - s, 0.0)
        sp_val = -max(short_put - s, 0.0)
        sc_val = -max(s - short_call, 0.0)
        lc_val = max(s - long_call, 0.0)
        leg_val = lp_val + sp_val + sc_val + lc_val
        pnl.append((net_credit + leg_val) * 100 * qty)
    return prices, np.array(pnl)


def build_ab_payoff_chart(
    underlying_price: float,
    strat_a: Dict[str, Any],
    strat_b: Dict[str, Any],
    symbol: str = "XSP",
) -> go.Figure:
    """Generate an interactive Plotly chart comparing Strategy A vs Strategy B payoff curves."""
    # Determine overall price span
    all_strikes = [
        strat_a["long_put"], strat_a["short_put"], strat_a["short_call"], strat_a["long_call"],
        strat_b["long_put"], strat_b["short_put"], strat_b["short_call"], strat_b["long_call"],
    ]
    min_strike = min(all_strikes)
    max_strike = max(all_strikes)
    span = max(max_strike - min_strike, 12.0)
    p_min = max(min_strike - span * 0.35, 1.0)
    p_max = max_strike + span * 0.35

    prices_a, pnl_a = calculate_condor_curve(
        underlying_price, strat_a["long_put"], strat_a["short_put"],
        strat_a["short_call"], strat_a["long_call"], strat_a["net_credit"],
        strat_a.get("qty", 1), p_min, p_max
    )
    prices_b, pnl_b = calculate_condor_curve(
        underlying_price, strat_b["long_put"], strat_b["short_put"],
        strat_b["short_call"], strat_b["long_call"], strat_b["net_credit"],
        strat_b.get("qty", 1), p_min, p_max
    )

    fig = go.Figure()

    # Zero P&L horizontal reference
    fig.add_hline(y=0, line_dash="solid", line_color="rgba(255, 255, 255, 0.25)", line_width=1.5)

    # Strategy A Curve (Cyan)
    fig.add_trace(
        go.Scatter(
            x=prices_a,
            y=pnl_a,
            mode="lines",
            name=f"Strategy A ({strat_a.get('name', 'Baseline 15Δ')})",
            line=dict(color="#00d4b2", width=3.5),
            hovertemplate="Underlying: $%{x:.2f}<br>P&L: $%{y:.2f}<extra>Strat A</extra>",
        )
    )

    # Strategy B Curve (Orange)
    fig.add_trace(
        go.Scatter(
            x=prices_b,
            y=pnl_b,
            mode="lines",
            name=f"Strategy B ({strat_b.get('name', 'Challenger 20Δ')})",
            line=dict(color="#ff9f43", width=3, dash="dot"),
            hovertemplate="Underlying: $%{x:.2f}<br>P&L: $%{y:.2f}<extra>Strat B</extra>",
        )
    )

    # Current Underlying Price Vertical Line
    fig.add_vline(
        x=underlying_price,
        line_dash="dash",
        line_color="#ffd32a",
        line_width=2,
        annotation_text=f"Current {symbol} (${underlying_price:.2f})",
        annotation_position="top right",
        annotation_font_color="#ffd32a",
    )

    fig.update_layout(
        title=f"<b>A/B Strategy Payoff Overlay at Expiration ({symbol})</b>",
        xaxis_title="Underlying Price ($)",
        yaxis_title="Expiration Profit / Loss ($)",
        template="plotly_dark",
        paper_bgcolor="#11141a",
        plot_bgcolor="#161b22",
        hovermode="x unified",
        margin=dict(l=40, r=40, t=60, b=40),
        legend=dict(
            orientation="h",
            yanchor="bottom",
            y=1.02,
            xanchor="right",
            x=1,
            bgcolor="rgba(22, 27, 34, 0.8)",
        ),
        xaxis=dict(gridcolor="#21262d", zeroline=False),
        yaxis=dict(gridcolor="#21262d", zeroline=False),
    )
    return fig


def build_risk_gauge_chart(
    current_val: float,
    max_val: float,
    title: str,
    prefix: str = "$",
    color: str = "#00d4b2",
) -> go.Figure:
    """Generate a clean dark-theme circular gauge meter."""
    pct = (current_val / max_val * 100) if max_val > 0 else 0.0
    fig = go.Figure(
        go.Indicator(
            mode="gauge+number+delta",
            value=current_val,
            number={"prefix": prefix, "valueformat": ",.0f", "font": {"color": "#fafafa", "size": 26}},
            gauge={
                "axis": {"range": [0, max_val], "tickwidth": 1, "tickcolor": "#8b949e"},
                "bar": {"color": color, "thickness": 0.45},
                "bgcolor": "#21262d",
                "borderwidth": 0,
                "steps": [
                    {"range": [0, max_val * 0.6], "color": "rgba(0, 212, 178, 0.08)"},
                    {"range": [max_val * 0.6, max_val * 0.85], "color": "rgba(255, 159, 67, 0.15)"},
                    {"range": [max_val * 0.85, max_val], "color": "rgba(255, 75, 75, 0.25)"},
                ],
                "threshold": {
                    "line": {"color": "#ff4d4d", "width": 4},
                    "thickness": 0.75,
                    "value": max_val,
                },
            },
        )
    )
    fig.update_layout(
        title={"text": f"<b>{title}</b><br><span style='font-size:12px;color:#8b949e'>{pct:.1f}% of {prefix}{max_val:,.0f} limit</span>", "x": 0.5},
        template="plotly_dark",
        paper_bgcolor="#11141a",
        height=210,
        margin=dict(l=25, r=25, t=55, b=20),
    )
    return fig


def build_iron_condor_chart(
    underlying_price: float,
    long_put: float,
    short_put: float,
    short_call: float,
    long_call: float,
    net_credit: float,
    symbol: str = "XSP",
    qty: int = 1,
) -> go.Figure:
    """Standard single-strategy Iron Condor chart."""
    strat = {
        "name": f"{symbol} Iron Condor",
        "long_put": long_put,
        "short_put": short_put,
        "short_call": short_call,
        "long_call": long_call,
        "net_credit": net_credit,
        "qty": qty,
    }
    # Use ab chart with same config
    return build_ab_payoff_chart(underlying_price, strat, strat, symbol=symbol)
