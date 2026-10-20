"""Iron Condor Payoff Diagram Builder using Plotly."""
import numpy as np
import plotly.graph_objects as go
from typing import Optional


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
    """Generate an interactive Plotly Iron Condor expiration payoff chart."""
    wing_width = short_put - long_put
    max_profit = net_credit * 100 * qty
    max_loss = -(wing_width - net_credit) * 100 * qty
    lower_be = short_put - net_credit
    upper_be = short_call + net_credit

    # Price range for curve
    span = max(long_call - long_put, 10.0)
    p_min = max(long_put - span * 0.4, 1.0)
    p_max = long_call + span * 0.4
    prices = np.linspace(p_min, p_max, 400)

    # Expiration P&L calculation
    pnl = []
    for s in prices:
        lp_val = max(long_put - s, 0.0)
        sp_val = -max(short_put - s, 0.0)
        sc_val = -max(s - short_call, 0.0)
        lc_val = max(s - long_call, 0.0)
        leg_val = lp_val + sp_val + sc_val + lc_val
        pnl.append((net_credit + leg_val) * 100 * qty)

    pnl = np.array(pnl)

    fig = go.Figure()

    # Zero P&L reference line
    fig.add_hline(y=0, line_dash="solid", line_color="rgba(255, 255, 255, 0.2)", line_width=1)

    # Payoff curve
    fig.add_trace(
        go.Scatter(
            x=prices,
            y=pnl,
            mode="lines",
            name="Expiration P&L",
            line=dict(color="#00d4b2", width=3),
            hovertemplate="Underlying: $%{x:.2f}<br>P&L: $%{y:.2f}<extra></extra>",
        )
    )

    # Shaded Profit Zone
    profit_mask = pnl >= 0
    if np.any(profit_mask):
        fig.add_trace(
            go.Scatter(
                x=prices[profit_mask],
                y=pnl[profit_mask],
                fill="tozeroy",
                fillcolor="rgba(0, 212, 178, 0.15)",
                line=dict(color="rgba(0,0,0,0)"),
                hoverinfo="skip",
                showlegend=False,
            )
        )

    # Shaded Loss Zone
    loss_mask = pnl < 0
    if np.any(loss_mask):
        fig.add_trace(
            go.Scatter(
                x=prices[loss_mask],
                y=pnl[loss_mask],
                fill="tozeroy",
                fillcolor="rgba(255, 75, 75, 0.15)",
                line=dict(color="rgba(0,0,0,0)"),
                hoverinfo="skip",
                showlegend=False,
            )
        )

    # Vertical strike lines
    strikes = [
        (long_put, f"Long Put (${long_put:.1f})", "#ff79c6"),
        (short_put, f"Short Put (${short_put:.1f})", "#50fa7b"),
        (short_call, f"Short Call (${short_call:.1f})", "#50fa7b"),
        (long_call, f"Long Call (${long_call:.1f})", "#ff79c6"),
    ]

    for strike, label, color in strikes:
        fig.add_vline(
            x=strike,
            line_dash="dot",
            line_color=color,
            line_width=1.5,
            annotation_text=label,
            annotation_position="bottom right",
            annotation_font_size=10,
            annotation_font_color=color,
        )

    # Current underlying price line
    fig.add_vline(
        x=underlying_price,
        line_dash="dash",
        line_color="#f1fa8c",
        line_width=2.5,
        annotation_text=f"Current {symbol} (${underlying_price:.2f})",
        annotation_position="top left",
        annotation_font_size=11,
        annotation_font_color="#f1fa8c",
    )

    fig.update_layout(
        title=f"🦅 {symbol} Iron Condor Payoff at Expiration (Qty: {qty})",
        xaxis_title=f"{symbol} Index Price ($)",
        yaxis_title="Profit / Loss ($)",
        template="plotly_dark",
        paper_bgcolor="rgba(0,0,0,0)",
        plot_bgcolor="rgba(20, 24, 33, 0.7)",
        margin=dict(l=40, r=40, t=50, b=40),
        hovermode="x unified",
        legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="right", x=1),
    )

    return fig
