"""
🦅 Lumibot Cockpit — Streamlit Web UI
Real-time monitoring, visualization, and controls for XSP 0DTE Iron Condor.
"""
import os
import re
from pathlib import Path
from datetime import datetime
import pandas as pd
import streamlit as st
from dotenv import load_dotenv

from alpaca_client import (
    get_client,
    fetch_account_info,
    fetch_market_clock,
    fetch_positions,
    fetch_recent_orders,
    emergency_cancel_orders,
    emergency_flatten_positions,
)
from payoff_chart import build_iron_condor_chart

# Load environment configuration
load_dotenv()

# Streamlit Page Configuration
st.set_page_config(
    page_title="Lumibot Cockpit | XSP Iron Condor",
    page_icon="🦅",
    layout="wide",
    initial_sidebar_state="expanded",
)

# Custom Styling
st.markdown("""
<style>
    .metric-card {
        background-color: #1a1d24;
        border: 1px solid #2d3139;
        border-radius: 8px;
        padding: 14px 18px;
        margin-bottom: 10px;
    }
    .badge-open {
        background-color: #1b4332;
        color: #52b788;
        padding: 3px 8px;
        border-radius: 4px;
        font-weight: 600;
        font-size: 0.85rem;
    }
    .badge-closed {
        background-color: #49111c;
        color: #ff4d6d;
        padding: 3px 8px;
        border-radius: 4px;
        font-weight: 600;
        font-size: 0.85rem;
    }
    .log-box {
        background-color: #0b0d11;
        color: #e6edf3;
        font-family: 'SF Mono', Monaco, Consolas, monospace;
        font-size: 0.82rem;
        padding: 12px;
        border-radius: 6px;
        border: 1px solid #21262d;
        height: 480px;
        overflow-y: scroll;
        white-space: pre-wrap;
        line-height: 1.45;
    }
</style>
""", unsafe_allow_html=True)


def parse_option_symbol(symbol: str):
    """
    Parse OCC option symbols, e.g. XSP241017C00585000 or SPXW...
    Returns dict(underlying, expiry, right, strike) or None.
    """
    match = re.match(r"^([A-Z]+)(\d{6})([CP])(\d{8})$", symbol)
    if not match:
        return None
    und, exp_str, right_code, strike_str = match.groups()
    try:
        exp = datetime.strptime(exp_str, "%y%m%d").strftime("%Y-%m-%d")
        strike = int(strike_str) / 1000.0
        right = "Call" if right_code == "C" else "Put"
        return {"underlying": und, "expiry": exp, "right": right, "strike": strike}
    except Exception:
        return None


def read_log_file(file_path: str, max_lines: int = 200):
    """Read tail of trading log file."""
    p = Path(file_path)
    if not p.exists():
        return []
    try:
        with open(p, "r", encoding="utf-8", errors="replace") as f:
            lines = f.readlines()
            return lines[-max_lines:]
    except Exception as e:
        return [f"Error reading log file: {e}"]


# -------------------------------------------------------------
# Data Loading
# -------------------------------------------------------------
client = get_client()
account = fetch_account_info(client)
clock = fetch_market_clock(client)
positions = fetch_positions(client)
orders = fetch_recent_orders(client, limit=30)

# Environment Parameters
env_symbol = os.getenv("IC_SYMBOL", "XSP")
env_dte = int(os.getenv("IC_DTE", "0"))
env_delta = float(os.getenv("IC_SHORT_DELTA", "0.15"))
env_wing = float(os.getenv("IC_WING_WIDTH", "1.5"))
env_qty = int(os.getenv("IC_QUANTITY", "1"))
env_pt = float(os.getenv("IC_PROFIT_TARGET", "0.30"))
env_sl = float(os.getenv("IC_STOP_LOSS_MULT", "2.0"))
env_entry = os.getenv("IC_ENTRY_TIME", "10:00")
env_alloc = float(os.getenv("IC_MAX_ALLOCATION", "0.03"))

# -------------------------------------------------------------
# Sidebar
# -------------------------------------------------------------
with st.sidebar:
    st.title("🦅 Lumibot Control")

    # Paper/Live Badge
    is_paper = os.getenv("ALPACA_PAPER", "true").lower() == "true"
    badge_label = "🟡 Alpaca Paper Trading" if is_paper else "🔴 Alpaca LIVE Trading"
    st.info(badge_label)

    # Market Status
    if clock:
        if clock["is_open"]:
            st.markdown('Market Status: <span class="badge-open">🟢 OPEN</span>', unsafe_allow_html=True)
            if clock["next_close"]:
                st.caption(f"Closes at: {clock['next_close'].strftime('%H:%M:%S EST')}")
        else:
            st.markdown('Market Status: <span class="badge-closed">🔴 CLOSED</span>', unsafe_allow_html=True)
            if clock["next_open"]:
                st.caption(f"Next Open: {clock['next_open'].strftime('%Y-%m-%d %H:%M EST')}")
    else:
        st.caption("Market clock unavailable (check API keys)")

    st.divider()

    # Manual refresh
    if st.button("🔄 Refresh Data", use_container_width=True):
        st.rerun()

    st.subheader("⚙️ Live Settings (.env)")
    st.write(f"**Underlying**: `{env_symbol}`")
    st.write(f"**Target DTE**: `{env_dte}DTE`")
    st.write(f"**Short Delta**: `Δ {env_delta:.2f}`")
    st.write(f"**Wing Width**: `${env_wing:.2f}`")
    st.write(f"**Contracts**: `{env_qty}`")
    st.write(f"**Profit Target**: `{env_pt*100:.0f}%`")
    st.write(f"**Stop Loss**: `{env_sl:.1f}x`")
    st.write(f"**Entry Time**: `{env_entry} EST`")
    st.write(f"**Max Allocation**: `{env_alloc*100:.1f}%`")

    st.divider()

    # Emergency Actions
    st.subheader("🚨 Emergency Safety")
    if st.button("❌ Cancel All Open Orders", use_container_width=True):
        if emergency_cancel_orders(client):
            st.success("All open orders cancelled!")
            st.rerun()
        else:
            st.error("Failed to cancel orders.")

    confirm_flatten = st.checkbox("Confirm Emergency Flatten", value=False)
    if st.button("💥 Liquidate All Positions", disabled=not confirm_flatten, use_container_width=True):
        if emergency_flatten_positions(client):
            st.warning("All positions liquidated and orders cancelled.")
            st.rerun()
        else:
            st.error("Failed to liquidate positions.")


# -------------------------------------------------------------
# Main Header & Alert Banner
# -------------------------------------------------------------
st.title("🦅 Lumibot Cockpit")
st.caption("Real-Time Execution & Analytics for XSP 0DTE Iron Condor")

if not client:
    st.warning(
        "⚠️ **Alpaca API credentials missing or invalid.**\n\n"
        "Please edit `.env` and provide `ALPACA_API_KEY` and `ALPACA_API_SECRET`, "
        "then restart or click **Refresh Data**."
    )

# -------------------------------------------------------------
# Top Metric Row
# -------------------------------------------------------------
m1, m2, m3, m4, m5 = st.columns(5)

if account:
    equity = account["equity"]
    cash = account["cash"]
    bp = account["buying_power"]
    last_equity = account["last_equity"]
    day_pl = equity - last_equity if last_equity > 0 else 0.0
    day_pl_pct = (day_pl / last_equity * 100) if last_equity > 0 else 0.0

    m1.metric("Portfolio Value", f"${equity:,.2f}", delta=f"{day_pl:+,.2f} ({day_pl_pct:+.2f}%)")
    m2.metric("Available Cash", f"${cash:,.2f}")
    m3.metric("Buying Power", f"${bp:,.2f}")
    m4.metric("Open Positions", len(positions))
    m5.metric("Account Status", account["status"].upper())
else:
    m1.metric("Portfolio Value", "--")
    m2.metric("Available Cash", "--")
    m3.metric("Buying Power", "--")
    m4.metric("Open Positions", "0")
    m5.metric("Account Status", "DISCONNECTED")

st.divider()

# -------------------------------------------------------------
# Tabs
# -------------------------------------------------------------
tab_cockpit, tab_orders, tab_logs, tab_guide = st.tabs([
    "📊 Strategy Cockpit",
    "📋 Orders & Activity",
    "📜 Bot Logs",
    "📘 Strategy Reference",
])

# =============================================================
# TAB 1: STRATEGY COCKPIT
# =============================================================
with tab_cockpit:
    st.subheader(f"🦅 {env_symbol} 0DTE Iron Condor Architecture")

    # Detect if open positions belong to our Iron Condor
    parsed_legs = []
    underlying_price_est = 585.0  # default reference if no price fetched
    for p in positions:
        parsed = parse_option_symbol(p["symbol"])
        if parsed and parsed["underlying"] in [env_symbol, "SPX", "SPXW"]:
            parsed.update(p)
            parsed_legs.append(parsed)
            if p.get("current_price") and p["current_price"] > 0:
                pass

    if len(parsed_legs) >= 4:
        st.success(f"Active {env_symbol} Iron Condor detected ({len(parsed_legs)} legs).")
    elif len(parsed_legs) > 0:
        st.info(f"Detected {len(parsed_legs)} partial option leg(s).")
    else:
        st.info(
            f"ℹ️ **No active position right now.** Showing the strategy model for **{env_symbol}** "
            f"configured for **{env_entry} EST** entry with **{env_wing:.2f}pt** wings and **Δ {env_delta:.2f}** target."
        )

    # Payoff parameters (use live detected strikes or simulated baseline)
    ref_spot = 585.0
    sp_strike = ref_spot * (1.0 - 0.01)       # ~579.0
    lp_strike = sp_strike - env_wing          # ~577.5
    sc_strike = ref_spot * (1.0 + 0.01)       # ~591.0
    lc_strike = sc_strike + env_wing          # ~592.5
    est_credit = 0.45                         # estimated 30% of wing

    # If active legs are present, override with real strikes
    if len(parsed_legs) >= 4:
        puts = sorted([l for l in parsed_legs if l["right"] == "Put"], key=lambda x: x["strike"])
        calls = sorted([l for l in parsed_legs if l["right"] == "Call"], key=lambda x: x["strike"])
        if len(puts) >= 2 and len(calls) >= 2:
            lp_strike = puts[0]["strike"]
            sp_strike = puts[1]["strike"]
            sc_strike = calls[0]["strike"]
            lc_strike = calls[1]["strike"]
            est_credit = sum(l["cost_basis"] for l in parsed_legs) / 100.0

    # Risk Metrics Summary
    max_profit = est_credit * 100 * env_qty
    max_loss = (env_wing - est_credit) * 100 * env_qty
    rr_ratio = (max_profit / max_loss) if max_loss > 0 else 0.0
    pt_exit = max_profit * env_pt
    sl_exit = -max_profit * env_sl

    col_a, col_b, col_c, col_d, col_e = st.columns(5)
    col_a.metric("Est. Net Credit", f"${est_credit:.2f}/sh", f"${max_profit:.2f} max")
    col_b.metric("Max Loss", f"${max_loss:.2f}", f"Wing ${env_wing:.2f}")
    col_c.metric("Profit Target (30%)", f"+${pt_exit:.2f}")
    col_d.metric("Stop Loss (-200%)", f"-${abs(sl_exit):.2f}")
    col_e.metric("Max Risk / Trade", f"${max_loss:.2f}", f"Alloc ≤ {env_alloc*100:.1f}%")

    # Render Plotly Payoff Chart
    fig = build_iron_condor_chart(
        underlying_price=ref_spot,
        long_put=lp_strike,
        short_put=sp_strike,
        short_call=sc_strike,
        long_call=lc_strike,
        net_credit=est_credit,
        symbol=env_symbol,
        qty=env_qty,
    )
    st.plotly_chart(fig, use_container_width=True)

    # Open Positions Table
    st.markdown("### 📋 Current Portfolio Positions")
    if positions:
        df_pos = pd.DataFrame(positions)
        display_cols = [c for c in ["symbol", "qty", "side", "current_price", "market_value", "cost_basis", "unrealized_pl", "unrealized_plpc"] if c in df_pos.columns]
        st.dataframe(
            df_pos[display_cols].style.format({
                "current_price": "${:.2f}",
                "market_value": "${:,.2f}",
                "cost_basis": "${:,.2f}",
                "unrealized_pl": "${:+,.2f}",
                "unrealized_plpc": "{:+.2%}",
            }),
            use_container_width=True,
        )
    else:
        st.caption("No open positions in Alpaca account.")


# =============================================================
# TAB 2: ORDERS & ACTIVITY
# =============================================================
with tab_orders:
    st.subheader("📋 Order Execution Log")
    if orders:
        df_ord = pd.DataFrame(orders)
        order_cols = ["submitted_at", "symbol", "side", "qty", "filled_qty", "type", "status", "filled_avg_price"]
        st.dataframe(
            df_ord[order_cols].style.format({
                "filled_avg_price": "${:.2f}",
            }),
            use_container_width=True,
        )
    else:
        st.caption("No recent orders found.")


# =============================================================
# TAB 3: BOT LOGS
# =============================================================
with tab_logs:
    st.subheader("📜 Live Bot Container Logs")

    l_col1, l_col2, l_col3 = st.columns([2, 1, 1])
    search_query = l_col1.text_input("🔍 Filter logs by text", placeholder="e.g. Iron Condor, PROFIT, error")
    level_filter = l_col2.selectbox("Severity", ["ALL", "INFO", "WARNING", "ERROR"])
    lines_count = l_col3.selectbox("Lines to show", [100, 250, 500], index=0)

    # Read log file
    log_path = os.getenv("TRADING_LOG_PATH", "logs/trading.log")
    raw_lines = read_log_file(log_path, max_lines=lines_count)

    filtered_lines = []
    for line in raw_lines:
        if level_filter != "ALL" and f"[{level_filter}]" not in line:
            continue
        if search_query and search_query.lower() not in line.lower():
            continue
        filtered_lines.append(line)

    if filtered_lines:
        log_content = "".join(filtered_lines)
        st.markdown(f'<div class="log-box">{log_content}</div>', unsafe_allow_html=True)
    else:
        st.info("No log entries match the filter, or `logs/trading.log` is still waiting for first bot run.")


# =============================================================
# TAB 4: STRATEGY REFERENCE
# =============================================================
with tab_guide:
    st.subheader("📘 Strategy Mechanics & Risk Rules")
    st.markdown("""
    ### Why XSP for 0DTE Iron Condors?
    1. **European-Style Settlement**: Unlike SPY, XSP options cannot be assigned early. No dividend or early exercise risk.
    2. **Cash Settled**: At 4:00 PM EST expiration, positions settle in cash. No shares of stock are ever transferred.
    3. **Section 1256 Tax Advantages**: 60% long-term / 40% short-term capital gains tax treatment regardless of holding time.
    4. **Right-Sized Risk**: 1/10th the size of SPX (similar notional size to SPY), allowing tight risk control for retail portfolios.

    ### Standard Operating Procedure
    - **10:00 AM EST**: Market opening volatility settles. The bot queries the options chain and targets ~15 delta wings.
    - **Profit Target**: Order triggers profit taker once +30% of credit is captured (e.g., closing at $0.70 profit on a $1.00 credit).
    - **Stop Loss**: 2.0x credit multiplier. If spread widens against us, risk is strictly capped.
    - **3:45 PM EST Safety Net**: `before_market_closes` triggers auto-close on any remaining open legs.

    ### Quick Commands
    ```bash
    # Start bot & dashboard
    lumibot-start

    # View live bot logs
    lumibot-logs

    # Open local dashboard
    open http://localhost:8501

    # Stop container
    lumibot-stop
    ```
    """)
