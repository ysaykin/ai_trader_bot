"""🦅 Lumibot Trading Cockpit — Professional Real-Time Dashboard & A/B Strategy Lab.

Implements all user-facing features defined in USER_FEATURES.md:
- Real-time engine heartbeat & market session clock
- $5,000 global open position risk gauge
- Tiered single-trade sliding scale (30% -> 5%) risk meter
- A/B Multi-Strategy comparative scorecard & payoff overlays
- Active positions & Greeks matrix
- Live telemetry log streaming
- S&P 500 benchmark comparison and guarded emergency controls
"""
from __future__ import annotations

import math
import os
import re
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from html import escape
from pathlib import Path
from typing import Any, Mapping
from zoneinfo import ZoneInfo


@dataclass(frozen=True)
class BrokerSettings:
    """Dashboard connection settings; credentials are excluded from repr."""

    name: str
    token: str = field(repr=False)
    account_number: str = ""
    paper: bool = True
    api_secret: str = field(default="", repr=False)

    @property
    def base_url(self) -> str:
        return "https://sandbox.tradier.com/v1" if self.paper else "https://api.tradier.com/v1"


def broker_settings(environment: Mapping[str, str]) -> BrokerSettings:
    """Use the same explicit selection and Tradier aliases as the trading entrypoint."""
    token = environment.get("TRADIER_ACCESS_TOKEN") or environment.get("TRADIER_API_KEY", "")
    name = environment.get("TRADING_BROKER", "").strip().lower() or ("tradier" if token else "alpaca")
    if name not in {"tradier", "alpaca"}:
        raise ValueError(f"Unsupported TRADING_BROKER: {name}")
    if name == "tradier":
        account = environment.get("TRADIER_ACCOUNT_NUMBER") or environment.get("TRADIER_ACCOUNT", "")
        paper = environment.get("TRADIER_IS_PAPER", environment.get("TRADIER_SANDBOX", "true"))
        if not token.strip() or token.startswith("your_") or not account.strip():
            raise ValueError("Set TRADIER_ACCESS_TOKEN and TRADIER_ACCOUNT_NUMBER to connect telemetry.")
        secret = ""
    else:
        token = environment.get("ALPACA_API_KEY", "")
        secret = environment.get("ALPACA_API_SECRET", "")
        account = ""
        paper = environment.get("ALPACA_PAPER", "true")
        if not token.strip() or token.startswith("your_") or not secret.strip():
            raise ValueError("Set broker credentials to connect telemetry.")
    paper = paper.strip().lower()
    if paper not in {"true", "false", "1", "0"}:
        raise ValueError("Broker paper/sandbox setting must be true, false, 1, or 0.")
    return BrokerSettings(name, token.strip(), account.strip(), paper in {"true", "1"}, secret)


def number(value: Any) -> float | None:
    """Keep missing and non-finite financial values distinct from zero."""
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if math.isfinite(parsed) else None


def money(value: Any) -> str:
    parsed = number(value)
    return f"${parsed:,.2f}" if parsed is not None else "Unavailable"


def single_trade_percentage(cap: float, override: str = "") -> float:
    """Mirror the engine's sliding scale, including a blank environment override."""
    if override.strip():
        parsed = number(override)
        if parsed is None or not 0 < parsed <= 1:
            raise ValueError("IC_MAX_SINGLE_TRADE_PCT must be greater than 0 and at most 1.")
        return parsed
    for threshold, percentage in ((5000, .30), (10000, .20), (25000, .15), (50000, .10)):
        if cap <= threshold:
            return percentage
    return .05


def fetch_json(settings: BrokerSettings, path: str, params: dict[str, str] | None = None) -> dict[str, Any]:
    """Read a Tradier endpoint without logging tokens or response bodies."""
    import requests

    response = requests.get(
        f"{settings.base_url}/{path}",
        headers={"Authorization": f"Bearer {settings.token}", "Accept": "application/json"},
        params=params, timeout=6,
    )
    response.raise_for_status()
    payload = response.json()
    if not isinstance(payload, dict) or payload.get("errors"):
        raise ValueError("Invalid broker response")
    return payload


def records(payload: dict[str, Any], container: str, key: str) -> list[dict[str, Any]]:
    """Normalize Tradier's null, single-object, and array response shapes."""
    value = payload[container]
    if value is None or value == "null":
        return []
    if not isinstance(value, dict) or key not in value:
        raise ValueError(f"Invalid {container} response")
    value = value[key]
    if value is None or value == "null":
        return []
    if isinstance(value, dict):
        return [value]
    if isinstance(value, list) and all(isinstance(item, dict) for item in value):
        return value
    raise ValueError(f"Invalid {key} response")


def account_balances(data: dict[str, Any], settings: BrokerSettings) -> dict[str, Any]:
    """Read margin, PDT, and cash accounts without synthetic balance defaults."""
    account_type = data.get("account_type", "")
    detail = data.get(account_type) or data.get("margin") or data.get("pdt") or data.get("cash") or {}
    buying_power = number(detail.get("option_buying_power", data.get("option_buying_power")))
    if buying_power is None and account_type == "cash":
        buying_power = number(detail.get("cash_available"))
    return {
        "broker": f"Tradier {'Sandbox' if settings.paper else 'Live'}",
        "account_number": settings.account_number,
        "account_type": account_type or "Unavailable",
        "equity": number(data.get("total_equity")),
        "cash": number(data.get("total_cash")),
        "buying_power": buying_power,
        "open_pl": number(data.get("open_pl")),
        "option_requirement": number(data.get("option_requirement")),
    }


def fetch_telemetry(settings: BrokerSettings, start: str, end: str) -> dict[str, Any]:
    """Fetch independent account and market snapshots; failures remain unavailable."""
    result: dict[str, Any] = {
        "account": None, "clock": None, "positions": None, "orders": None,
        "quotes": {}, "history": [], "errors": [],
        "fetched_at": datetime.now(timezone.utc).isoformat(),
    }
    if settings.name == "alpaca":
        import alpaca_client
        client = alpaca_client.get_client()
        if client is None:
            result["errors"].append("Alpaca connection unavailable.")
            return result
        for name, method in (("account", "fetch_account_info"), ("clock", "fetch_market_clock"),
                             ("positions", "fetch_positions"), ("orders", "fetch_recent_orders")):
            result[name] = getattr(alpaca_client, method)(client)
        if result["account"]:
            result["account"]["broker"] = f"Alpaca {'Paper' if settings.paper else 'Live'}"
        result["errors"].append("SPX benchmark history requires Tradier market data.")
        return result

    account_path = f"accounts/{settings.account_number}"
    endpoints = (
        ("account", f"{account_path}/balances", None),
        ("clock", "markets/clock", None),
        ("positions", f"{account_path}/positions", None),
        ("orders", f"{account_path}/orders", None),
        ("history", "markets/history", {"symbol": "SPX", "interval": "daily", "start": start, "end": end}),
    )
    for name, path, params in endpoints:
        try:
            payload = fetch_json(settings, path, params)
            if name == "account":
                result[name] = account_balances(payload["balances"], settings)
            elif name == "clock":
                clock = payload["clock"]
                if not isinstance(clock, dict) or not clock.get("state"):
                    raise ValueError("Missing market state")
                result[name] = {**clock, "is_open": clock["state"] == "open"}
            else:
                container, key = {"positions": ("positions", "position"), "orders": ("orders", "order"),
                                  "history": ("history", "day")}[name]
                result[name] = records(payload, container, key)
        except Exception:
            result["errors"].append(f"{name.title()} telemetry unavailable; refresh to retry.")
    symbols = {"SPX", "XSP", "SPY", os.getenv("IC_SYMBOL", "SPX")}
    symbols.update(str(p["symbol"]) for p in result["positions"] or [] if p.get("symbol"))
    try:
        payload = fetch_json(settings, "markets/quotes", {"symbols": ",".join(sorted(symbols)), "greeks": "true"})
        result["quotes"] = {q["symbol"]: q for q in records(payload, "quotes", "quote") if q.get("symbol")}
    except Exception:
        result["errors"].append("Quote telemetry unavailable; refresh to retry.")
    return result


def open_exposure_value(account: dict[str, Any] | None, positions: list[dict[str, Any]] | None) -> float | None:
    """Use broker option requirements, preserving unknown exposure after read errors."""
    if positions is None:
        return None
    requirement = number((account or {}).get("option_requirement"))
    if requirement is not None and requirement >= 0:
        return requirement
    return 0.0 if not positions else None


def strategy_tag(value: Any) -> str | None:
    """Recognize explicit strategy tags; never infer ownership from ticker letters."""
    match = re.fullmatch(r"strategy\s+([ab])(?:\s*\([^)]*\))?", str(value).strip(), re.IGNORECASE)
    return f"Strategy {match[1].upper()}" if match else None


def trade_exit_date(trade: dict[str, Any]) -> date | None:
    """Align journal exits to the benchmark's New York trading dates."""
    if trade.get("status") != "CLOSED":
        return None
    try:
        timestamp = datetime.fromisoformat(str(trade["exit_time"]).replace("Z", "+00:00"))
        if timestamp.tzinfo is None:
            timestamp = timestamp.replace(tzinfo=timezone.utc)
        return timestamp.astimezone(ZoneInfo("America/New_York")).date()
    except (KeyError, TypeError, ValueError):
        return None


def benchmark_comparison(history: list[dict[str, Any]], trades: list[dict[str, Any]], capital: float) -> list[dict[str, Any]]:
    """Compare realized journal returns to SPX closes on one date grid and capital base."""
    if not math.isfinite(capital) or capital <= 0:
        raise ValueError("Comparison capital must be positive.")
    closes: dict[date, float] = {}
    for bar in history:
        try:
            day = date.fromisoformat(str(bar["date"]))
        except (KeyError, TypeError, ValueError):
            continue
        close = number(bar.get("close"))
        if close is not None and close > 0:
            closes[day] = close
    if len(closes) < 2:
        return []
    days = sorted(closes)
    first, last = days[0], days[-1]
    exits = sorted(
        (exit_date, index, trade) for index, trade in enumerate(trades)
        if (exit_date := trade_exit_date(trade)) is not None and first < exit_date <= last
        and number(trade.get("pnl")) is not None
    )
    totals = {"Journal": 0.0, "Strategy A": 0.0, "Strategy B": 0.0}
    observed = {strategy_tag(trade.get("strategy")) for _, _, trade in exits}
    rows = []
    cursor = 0
    for day in days:
        while cursor < len(exits) and exits[cursor][0] <= day:
            trade = exits[cursor][2]
            pnl = float(trade["pnl"])
            totals["Journal"] += pnl
            tag = strategy_tag(trade.get("strategy"))
            if tag:
                totals[tag] += pnl
            cursor += 1
        row: dict[str, Any] = {"Date": day, "S&P 500 (SPX)": (closes[day] / closes[first] - 1) * 100}
        if exits:
            row["Journal"] = totals["Journal"] / capital * 100
        for tag in ("Strategy A", "Strategy B"):
            if tag in observed:
                row[tag] = totals[tag] / capital * 100
        rows.append(row)
    return rows


def position_records(positions: list[dict[str, Any]], quotes: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    """Join open legs to live quotes and Greeks by the complete contract symbol."""
    rows = []
    for position in positions:
        symbol = str(position.get("symbol", ""))
        quote = quotes.get(symbol, {})
        greeks = quote.get("greeks") or {}
        bid, ask = number(quote.get("bid")), number(quote.get("ask"))
        mid = (bid + ask) / 2 if bid is not None and ask is not None and 0 <= bid <= ask and ask > 0 else None
        rows.append({
            "Strategy": strategy_tag(position.get("strategy") or position.get("tag")) or "Unattributed",
            "Symbol": symbol, "Contracts": position.get("quantity", position.get("qty")),
            "Strike": number(quote.get("strike")), "Expiration": quote.get("expiration_date"),
            "Cost Basis": number(position.get("cost_basis")), "Market Mid": mid,
            "Delta (Δ)": number(greeks.get("delta")), "Theta (Θ)": number(greeks.get("theta")),
            "IV": number(greeks.get("mid_iv")),
            "Unrealized P&L": number(position.get("unrealized_pl")),
        })
    return rows


# -------------------------------------------------------------
# Telemetry & Log Parser
# -------------------------------------------------------------
def get_bot_status_from_log(log_path: str = "logs/trading.log") -> dict[str, str]:
    """Parse the most recent engine lifecycle state from the trading log."""
    p = Path(log_path)
    if not p.exists():
        return {
            "status": "OFFLINE / STARTING",
            "last_seen": "Never",
            "strategy": "SPXIronCondor",
            "last_message": "Log file not yet initialized.",
        }
    try:
        with open(p, "r", encoding="utf-8", errors="replace") as f:
            lines = f.readlines()
            if not lines:
                return {"status": "INITIALIZING", "last_seen": "Just now", "strategy": "SPXIronCondor", "last_message": "Awaiting initial log entry."}

            tail = lines[-50:]
            status = "SLEEPING UNTIL MARKET OPEN"
            last_msg = lines[-1].strip()
            last_ts = "Recent"

            for line in reversed(tail):
                ts_match = re.match(r"^(\d{4}-\d{2}-\d{2}\s\d{2}:\d{2}:\d{2})", line)
                if ts_match and last_ts == "Recent":
                    last_ts = ts_match.group(1)
                if "Executing the on_trading_iteration" in line:
                    status = "ACTIVE / EXECUTING"
                    break
                elif "Sleeping until the market opens" in line:
                    status = "SLEEPING (MARKET CLOSED)"
                    break
                elif "Strategy will check in again at" in line:
                    status = "ONLINE (HEARTBEAT OK)"
                    break

            return {
                "status": status,
                "last_seen": last_ts,
                "strategy": "SPXIronCondor (A/B Ready)",
                "last_message": last_msg,
            }
    except Exception as e:
        return {"status": "ERROR READING LOG", "last_seen": "Unknown", "strategy": "Unknown", "last_message": str(e)}


def main() -> None:
    """Render the dashboard, keeping data transforms independently testable."""
    import pandas as pd
    import plotly.graph_objects as go
    import streamlit as st
    from dotenv import load_dotenv

    from payoff_chart import build_ab_payoff_chart, build_risk_gauge_chart
    from trade_logger import load_all_trades, compute_strategy_metrics
    from config_manager import load_env_values, save_env_values, test_tradier_credentials, test_alpaca_credentials

    # Load environment configuration
    load_dotenv()

    # Streamlit Page Configuration
    st.set_page_config(
        page_title="Lumibot Cockpit | A/B Strategy Lab",
        page_icon="🦅",
        layout="wide",
        initial_sidebar_state="expanded",
    )

    # Custom High-Performance Dark Styling
    st.markdown("""
    <style>
        .metric-card {
            background-color: #161b22;
            border: 1px solid #30363d;
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
        .badge-live {
            background-color: #1f2937;
            color: #38bdf8;
            border: 1px solid #0284c7;
            padding: 3px 8px;
            border-radius: 4px;
            font-weight: 600;
            font-size: 0.82rem;
        }
        .log-box {
            background-color: #0d1117;
            color: #e6edf3;
            font-family: 'SF Mono', Monaco, Consolas, monospace;
            font-size: 0.82rem;
            padding: 12px;
            border-radius: 6px;
            border: 1px solid #30363d;
            height: 480px;
            overflow-y: scroll;
            white-space: pre-wrap;
            line-height: 1.45;
        }
    </style>
    """, unsafe_allow_html=True)


    configuration_error = ""
    try:
        settings = broker_settings(os.environ)
    except ValueError as error:
        settings = None
        configuration_error = str(error)
    with st.sidebar:
        lookback = st.selectbox("Benchmark period (days)", (30, 90, 365), index=1)
        comparison_capital = st.number_input("Comparison starting capital ($)", min_value=1.0, value=100000.0, step=1000.0)
    sidebar_placeholder = st.sidebar.empty()
    cached_telemetry = st.cache_data(ttl=30, show_spinner=False)(fetch_telemetry)

    def render_dashboard() -> None:
        # -------------------------------------------------------------
        # Data Ingestion
        # -------------------------------------------------------------
        today = datetime.now(ZoneInfo("America/New_York")).date()
        end = (today - timedelta(days=1)).isoformat()
        start = (today - timedelta(days=lookback)).isoformat()
        snapshot = cached_telemetry(settings, start, end) if settings else {
            "account": None, "clock": None, "positions": None, "orders": None,
            "quotes": {}, "history": [], "errors": [], "fetched_at": "Unavailable",
        }
        account, clock = snapshot["account"], snapshot["clock"]
        positions, orders, quotes = snapshot["positions"], snapshot["orders"], snapshot["quotes"]
        bot_state = get_bot_status_from_log()
        all_trades = load_all_trades()

        # Parameters from .env with fallbacks
        env_symbol = os.getenv("IC_SYMBOL", "SPX")
        env_dte = int(os.getenv("IC_DTE", "0"))
        env_delta = float(os.getenv("IC_SHORT_DELTA", "0.15"))
        env_wing = float(os.getenv("IC_WING_WIDTH", "15.0"))
        env_qty = int(os.getenv("IC_QUANTITY", "1"))
        env_pt = float(os.getenv("IC_PROFIT_TARGET", "0.30"))
        env_sl = float(os.getenv("IC_STOP_LOSS_MULT", "2.0"))
        env_entry = os.getenv("IC_ENTRY_TIME", "10:00")
        env_max_pos_risk = float(os.getenv("IC_MAX_POSITION_RISK", "5000.0"))
        env_single_trade_pct = single_trade_percentage(env_max_pos_risk, os.getenv("IC_MAX_SINGLE_TRADE_PCT", ""))

        # Derived Calculations
        single_trade_limit = env_max_pos_risk * env_single_trade_pct
        trade_risk = env_wing * 100 * env_qty

        # Current open exposure calculation
        open_exposure = open_exposure_value(account, positions)
        safe_qty = min(env_qty, int(single_trade_limit // (env_wing * 100))) if env_wing > 0 else 0


        # -------------------------------------------------------------
        # Sidebar: Control, Config & Emergency Killswitch
        # -------------------------------------------------------------
        with sidebar_placeholder.container():
            st.title("🦅 Cockpit Controls")

            # Engine Status Badge
            st.markdown(f'<span class="badge-live">🤖 Bot: {bot_state["status"]}</span>', unsafe_allow_html=True)
            st.caption(f"Last heartbeat: {bot_state['last_seen']}")

            # Broker Status Badge
            broker_name = account.get("broker", "Unavailable") if account else "Disconnected"
            st.info(f"Broker: **{broker_name}**")
            if account:
                st.caption(f"Account: `{account.get('account_number', 'N/A')}` · {account.get('account_type', '')}")

            # Market Clock
            if clock:
                if clock.get("is_open"):
                    st.markdown('Market State: <span class="badge-open">🟢 OPEN</span>', unsafe_allow_html=True)
                else:
                    st.markdown('Market State: <span class="badge-closed">🔴 CLOSED</span>', unsafe_allow_html=True)
                st.caption(f"Next change: {clock.get('next_change', 'Unavailable')} ET")

            st.divider()

            # Manual refresh
            if st.button("🔄 Refresh Telemetry", use_container_width=True):
                cached_telemetry.clear()
                st.rerun()
            st.caption(f"Snapshot (UTC): {snapshot['fetched_at']}")
            st.caption("Telemetry refreshes every 30 seconds when supported by Streamlit.")

            st.subheader("🛡️ Risk Parameters (.env)")
            st.write(f"• **Max Open Risk Cap**: `${env_max_pos_risk:,.0f}`")
            st.write(f"• **Single-Trade Limit**: `${single_trade_limit:,.0f}` ({env_single_trade_pct*100:.0f}%)")
            st.write(f"• **Underlying**: `{env_symbol}`")
            st.write(f"• **Target DTE**: `{env_dte}DTE`")
            st.write(f"• **Short Delta**: `Δ {env_delta:.2f}`")
            st.write(f"• **Wing Width**: `${env_wing:.2f}`")
            st.write(f"• **Entry Window**: `{env_entry} ET`")

            st.divider()

            # Emergency Actions Section
            st.subheader("🚨 Emergency Action Center")
            working_orders = [o for o in orders or [] if str(o.get("status", "")).lower() in {"open", "pending", "partially_filled"}]
            if st.button("❌ Cancel All Open Orders", disabled=not (settings and working_orders), use_container_width=True):
                if settings and settings.name == "tradier":
                    from broker_client import TradierClient
                    client = TradierClient(settings.token, settings.account_number, settings.paper)
                    cancelled = client.emergency_cancel_orders()
                else:
                    import alpaca_client
                    cancelled = alpaca_client.emergency_cancel_orders(alpaca_client.get_client())
                if cancelled:
                    st.success("Cancellation requests sent; refresh to confirm broker status.")
                    cached_telemetry.clear()
                else:
                    st.error("Failed to cancel orders.")

            st.button("💥 Liquidate All Open Positions", disabled=True, use_container_width=True)
            st.caption("Liquidation is unavailable here until broker position-close execution is implemented.")


        # -------------------------------------------------------------
        # Main Header
        # -------------------------------------------------------------
        st.title("🦅 Lumibot Trading Cockpit")
        st.caption("Account telemetry · S&P 500 benchmark · Capital protection · Strategy comparison")
        if configuration_error:
            st.warning(configuration_error)
        for telemetry_error in snapshot["errors"]:
            st.warning(telemetry_error)
        st.caption(f"New York: {datetime.now(ZoneInfo('America/New_York')):%Y-%m-%d %H:%M:%S %Z}")

        # -------------------------------------------------------------
        # Top Metric Row
        # -------------------------------------------------------------
        m1, m2, m3, m4, m5, m6 = st.columns(6)

        m1.metric("Account Equity", money((account or {}).get("equity")))
        m2.metric("Available Cash", money((account or {}).get("cash")))
        m3.metric("Option Buying Power", money((account or {}).get("buying_power")))
        m4.metric("Open Option Requirement", money(open_exposure), f"of ${env_max_pos_risk:,.0f} cap")
        observed_tags = {strategy_tag(t.get("strategy")) for t in all_trades} - {None}
        m5.metric("Observed A/B Variants", f"{len(observed_tags)}/2", "Tagged journal records")
        m6.metric("Unrealized Account P&L", money((account or {}).get("open_pl")))

        st.divider()

        # -------------------------------------------------------------
        # Real-Time Visual Risk Dials
        # -------------------------------------------------------------
        st.subheader("🛡️ Capital Protection & Risk Gauges (RSK-01, RSK-02)")
        g1, g2, g3 = st.columns(3)

        with g1:
            if open_exposure is None or env_max_pos_risk <= 0:
                st.info("Open option exposure unavailable.")
            else:
                st.plotly_chart(build_risk_gauge_chart(
                    open_exposure, env_max_pos_risk, title="Global Open Option Exposure",
                    prefix="$", color="#00d4b2",
                ), use_container_width=True)
            st.caption("Broker-reported option margin requirement; maximum loss can differ.")
            if open_exposure is not None and open_exposure > env_max_pos_risk:
                st.error("Reported option exposure exceeds the configured cap.")

        with g2:
            if single_trade_limit > 0:
                st.plotly_chart(build_risk_gauge_chart(
                    trade_risk, single_trade_limit, title="Single-Trade Sizing Meter",
                    prefix="$", color="#38bdf8",
                ), use_container_width=True)
            st.caption(f"Requested: {env_qty} contracts · Scaled for single-trade limit: {safe_qty} · Limit: {env_single_trade_pct:.0%}")

        with g3:
            equity = number((account or {}).get("equity"))
            buying_power = number((account or {}).get("buying_power"))
            if equity is not None and equity > 0 and buying_power is not None:
                st.plotly_chart(build_risk_gauge_chart(
                    max(buying_power, 0.0), equity, title="Option Buying Power Headroom",
                    prefix="$", color="#a855f7",
                ), use_container_width=True)
            else:
                st.info("Option buying power unavailable.")
            st.caption("Available option buying power relative to account equity.")

        st.divider()


        # -------------------------------------------------------------
        # Main Navigation Tabs
        # -------------------------------------------------------------
        tab_benchmark, tab_ab, tab_positions, tab_orders, tab_config, tab_logs, tab_features = st.tabs([
            "📈 S&P 500 Benchmark",
            "⚖️ A/B Strategy Lab",
            "📊 Active Positions & Greeks",
            "📋 Orders & Activity",
            "⚙️ Broker & Strategy Config",
            "📜 Real-Time Log Console",
            "📘 User Features Matrix",
        ])


        with tab_benchmark:
            st.subheader("Journal Return vs S&P 500")
            st.caption("SPX price return and realized journal P&L use the same closing dates and comparison capital. Open P&L, cash flows, fees and dividends are excluded unless recorded in journal P&L.")
            comparison = benchmark_comparison(snapshot["history"], all_trades, comparison_capital)
            if comparison:
                frame = pd.DataFrame(comparison).set_index("Date")
                figure = go.Figure()
                for column, color in (("S&P 500 (SPX)", "#a5b4fc"), ("Journal", "#f8fafc"),
                                      ("Strategy A", "#00d4b2"), ("Strategy B", "#ff9f43")):
                    if column in frame:
                        figure.add_trace(go.Scatter(x=frame.index, y=frame[column], mode="lines", name=column, line={"color": color}))
                figure.update_layout(template="plotly_dark", yaxis_title="Return (%)", hovermode="x unified", legend={"orientation": "h"})
                st.plotly_chart(figure, use_container_width=True)
                b1, b2, b3 = st.columns(3)
                last_row = comparison[-1]
                benchmark_return = last_row["S&P 500 (SPX)"]
                journal_return = last_row.get("Journal")
                b1.metric("S&P 500 Return", f"{benchmark_return:+.2f}%")
                b2.metric("Realized Journal Return", f"{journal_return:+.2f}%" if journal_return is not None else "Unavailable")
                b3.metric("Excess Return", f"{journal_return - benchmark_return:+.2f} pp" if journal_return is not None else "Unavailable")
                st.caption(f"{comparison[0]['Date']} through {last_row['Date']} · Starting capital: {money(comparison_capital)} for each curve · Completed daily bars through the previous day.")
                if journal_return is None:
                    st.info("No closed journal trades in this comparison interval. The benchmark remains visible.")
            else:
                st.info("At least two valid SPX daily closes are needed for a benchmark comparison.")


        # =============================================================
        # TAB 1: A/B STRATEGY LAB
        # =============================================================
        with tab_ab:
            st.subheader("⚖️ A/B Multi-Strategy Experimentation Lab (EXP-01, EXP-04)")
            st.caption("Baseline configuration and proposed challenger. Only explicitly tagged journal trades contribute to A/B results; these cards do not start a strategy.")

            # Underlying reference price
            cur_price = number(quotes.get(env_symbol, {}).get("last"))
            st.write(f"**Underlying Reference ({env_symbol})**: {money(cur_price)}")

            # Strategy Parameters Card Grid
            col_a, col_b = st.columns(2)

            with col_a:
                st.markdown("""
                <div class="metric-card">
                    <h4 style="color:#00d4b2;margin-top:0">🔵 Strategy A: Baseline (Configured)</h4>
                    <p>Designed for maximum consistency with high probability of profit.</p>
                </div>
                """, unsafe_allow_html=True)
                st.write(f"• **Target Delta**: `{env_delta:.2f}`")
                st.write(f"• **DTE**: `{env_dte}DTE`")
                st.write(f"• **Wing Width**: `${env_wing:.2f}` points (${env_wing*100:.0f} max risk/contract)")
                st.write(f"• **Profit Target**: `{env_pt:.0%}` credit received")
                st.write(f"• **Stop Loss**: `{env_sl:.1f}x` credit received")
                st.caption("Performance attribution requires explicit strategy tags.")

            with col_b:
                st.markdown("""
                <div class="metric-card">
                    <h4 style="color:#ff9f43;margin-top:0">🟠 Strategy B: Challenger (Yield 20Δ)</h4>
                    <p>Targets higher credit collection with tighter profit take rules.</p>
                </div>
                """, unsafe_allow_html=True)
                st.write("• **Target Delta**: `0.20` (20Δ OTM)")
                st.write("• **DTE**: `0DTE` (Same Day)")
                st.write(f"• **Wing Width**: `${env_wing:.2f}` points (${env_wing*100:.0f} max risk/contract)")
                st.write("• **Profit Target**: `50%` credit received")
                st.write("• **Stop Loss**: `1.5x` credit received")
                st.caption("Performance attribution requires explicit strategy tags.")

            st.subheader("🏆 Side-by-Side Comparative Scorecard (EXP-04)")
            st.caption("All recorded journal history. The benchmark period selector applies to the benchmark tab.")

            # Real Dynamic Scorecard from logs/trades.jsonl
            tagged_trades = {
                tag: sorted((t for t in all_trades if strategy_tag(t.get("strategy")) == tag), key=lambda t: str(t.get("exit_time", "")))
                for tag in ("Strategy A", "Strategy B")
            }
            m_a = compute_strategy_metrics(tagged_trades["Strategy A"], "Strategy A")
            m_b = compute_strategy_metrics(tagged_trades["Strategy B"], "Strategy B")
            for tag, metrics in (("Strategy A", m_a), ("Strategy B", m_b)):
                open_count = sum(t.get("status") == "OPEN" for t in tagged_trades[tag])
                metrics["status"] = f"{open_count} open journal trades" if tagged_trades[tag] else "No tagged observations"

            scorecard_data = {
                "Metric": [
                    "Journal Observations",
                    "Win Rate %",
                    "Completed Trades",
                    "Realized P&L",
                    "Avg Credit Collected",
                    "Avg Trade Duration",
                    "Max Drawdown",
                    "Profit Factor",
                ],
                "Strategy A (Baseline)": [
                    m_a["status"],
                    m_a["win_rate"],
                    str(m_a["total_trades"]),
                    m_a["realized_pnl"],
                    m_a["avg_credit"],
                    m_a["avg_duration"],
                    m_a["max_drawdown"],
                    m_a["profit_factor"],
                ],
                "Strategy B (Challenger)": [
                    m_b["status"],
                    m_b["win_rate"],
                    str(m_b["total_trades"]),
                    m_b["realized_pnl"],
                    m_b["avg_credit"],
                    m_b["avg_duration"],
                    m_b["max_drawdown"],
                    m_b["profit_factor"],
                ],
            }
            df_scorecard = pd.DataFrame(scorecard_data)
            st.table(df_scorecard)

            if m_a["total_trades"] == 0 and m_b["total_trades"] == 0:
                st.info("No closed A/B trades observed. Untagged SPXIronCondor trades remain in the journal and benchmark total.")
            st.subheader("Cumulative Realized P&L by Strategy")
            pnl_points = []
            for tag, trades in tagged_trades.items():
                cumulative = 0.0
                for trade in trades:
                    if trade_exit_date(trade) is not None and number(trade.get("pnl")) is not None:
                        cumulative += float(trade["pnl"])
                        pnl_points.append({"Exit": trade["exit_time"], "Strategy": tag, "Cumulative P&L": cumulative})
            if pnl_points:
                pnl_frame = pd.DataFrame(pnl_points)
                pnl_figure = go.Figure()
                for tag, color in (("Strategy A", "#00d4b2"), ("Strategy B", "#ff9f43")):
                    series = pnl_frame[pnl_frame["Strategy"] == tag]
                    if not series.empty:
                        pnl_figure.add_trace(go.Scatter(x=series["Exit"], y=series["Cumulative P&L"], name=tag, mode="lines+markers", line={"color": color}))
                pnl_figure.update_layout(template="plotly_dark", yaxis_title="Realized P&L ($)", hovermode="x unified")
                st.plotly_chart(pnl_figure, use_container_width=True)

            st.subheader("📈 Multi-Curve Expiration Payoff Model (GUI-04)")
            st.caption("Illustrative payoff scenarios with assumed strikes and credits, independent of actual open positions.")

            if cur_price is not None and cur_price > 0:
                # Synthetic strikes around current price for A/B comparison visualization
                strat_a_cfg = {
                    "name": "Strategy A (15Δ)",
                    "short_put": round(cur_price - 3.0),
                    "long_put": round(cur_price - 3.0 - env_wing),
                    "short_call": round(cur_price + 3.0),
                    "long_call": round(cur_price + 3.0 + env_wing),
                    "net_credit": 0.45,
                    "qty": env_qty,
                }
                strat_b_cfg = {
                    "name": "Strategy B (20Δ)",
                    "short_put": round(cur_price - 2.0),
                    "long_put": round(cur_price - 2.0 - env_wing),
                    "short_call": round(cur_price + 2.0),
                    "long_call": round(cur_price + 2.0 + env_wing),
                    "net_credit": 0.75,
                    "qty": env_qty,
                }

                fig_ab = build_ab_payoff_chart(cur_price, strat_a_cfg, strat_b_cfg, symbol=env_symbol)
                st.plotly_chart(fig_ab, use_container_width=True)
            else:
                st.info("A current underlying quote is needed for the payoff model.")


        # =============================================================
        # TAB 2: ACTIVE POSITIONS & GREEKS MATRIX
        # =============================================================
        with tab_positions:
            st.subheader("📊 Active Options Positions & Greeks Matrix (GUI-03)")

            if positions is None:
                st.warning("Position telemetry unavailable; open contracts may still exist.")
            elif positions:
                st.dataframe(pd.DataFrame(position_records(positions, quotes)), use_container_width=True)
                st.caption("Missing quotes, Greeks and unrealized P&L remain blank. Ownership requires an explicit tag.")
            else:
                st.info("The broker reports no open positions.")


        # =============================================================
        # TAB 3: ORDERS & ACTIVITY
        # =============================================================
        with tab_orders:
            st.subheader("📋 Recent Broker Orders & Executions")
            if orders:
                order_records = []
                for o in orders:
                    order_records.append({
                        "Order ID": o.get("id"),
                        "Symbol": o.get("symbol"),
                        "Type": o.get("type"),
                        "Side": o.get("side"),
                        "Status": o.get("status", "").upper(),
                        "Quantity": o.get("quantity"),
                        "Price": o.get("price", "Market"),
                        "Created At": o.get("create_date", "Recent"),
                    })
                st.dataframe(pd.DataFrame(order_records), use_container_width=True)
            elif orders is None:
                st.warning("Order telemetry unavailable.")
            else:
                st.info("No recent broker orders recorded in this trading session.")

            st.divider()
            st.subheader("📖 Trade Journal (logs/trades.jsonl)")
            if all_trades:
                df_journal = pd.DataFrame(all_trades)
                st.dataframe(df_journal, use_container_width=True)
            else:
                st.info("No historical trade records in `logs/trades.jsonl`. Trades will be automatically logged and journaled here upon execution.")


        # =============================================================
        # TAB 5: BROKER CREDENTIALS & STRATEGY CONFIG
        # =============================================================
        with tab_config:
            st.subheader("⚙️ Broker Credentials & Strategy Configuration")
            st.caption("Manage API keys, active broker, iron condor strike targeting, and risk ceilings. Configuration is saved directly to `.env`.")

            env_vals = load_env_values()

            # Broker Selection
            curr_broker = env_vals.get("TRADING_BROKER", "tradier").lower()
            broker_choice = st.radio(
                "Select Active Trading Broker",
                ["Tradier (Sandbox / Live)", "Alpaca (Paper / Live)"],
                index=0 if "tradier" in curr_broker else 1,
                horizontal=True,
            )
            is_tradier = "Tradier" in broker_choice

            st.divider()

            # Broker Credentials Section
            if is_tradier:
                st.markdown("#### 🔑 Tradier Broker Credentials")
                t_col1, t_col2 = st.columns(2)
                with t_col1:
                    t_api_key = st.text_input(
                        "Tradier API Key / Access Token",
                        value=env_vals.get("TRADIER_API_KEY", ""),
                        type="password",
                        help="Your sandbox or live Tradier API access token.",
                    )
                    t_account = st.text_input(
                        "Tradier Account Number",
                        value=env_vals.get("TRADIER_ACCOUNT_NUMBER") or env_vals.get("TRADIER_ACCOUNT", "VA21359019"),
                        help="e.g. VA21359019 for sandbox account.",
                    )
                with t_col2:
                    t_is_paper = st.checkbox(
                        "Sandbox / Paper Mode (`TRADIER_IS_PAPER=true`)",
                        value=env_vals.get("TRADIER_IS_PAPER", "true").lower() in ("true", "1") or env_vals.get("TRADIER_SANDBOX", "1") == "1",
                        help="Leave checked for Tradier Sandbox. Uncheck ONLY for real funded live trading.",
                    )
                    st.caption("💡 Tradier sandbox provides free delayed index option chains (SPX, XSP).")

                # Test Connection Button
                if st.button("🔌 Test Tradier Connection", key="test_tradier_btn"):
                    with st.spinner("Testing connection to Tradier API..."):
                        ok, msg = test_tradier_credentials(t_api_key, t_account, sandbox=t_is_paper)
                        if ok:
                            st.success(f"✅ {msg}")
                        else:
                            st.error(f"❌ {msg}")
            else:
                st.markdown("#### 🔑 Alpaca Broker Credentials")
                a_col1, a_col2 = st.columns(2)
                with a_col1:
                    a_api_key = st.text_input(
                        "Alpaca API Key ID",
                        value=env_vals.get("ALPACA_API_KEY", ""),
                        help="Alpaca Paper or Live API key.",
                    )
                    a_api_secret = st.text_input(
                        "Alpaca API Secret Key",
                        value=env_vals.get("ALPACA_API_SECRET", ""),
                        type="password",
                        help="Alpaca API secret key.",
                    )
                with a_col2:
                    a_paper = st.checkbox(
                        "Paper Trading Mode (`ALPACA_PAPER=true`)",
                        value=env_vals.get("ALPACA_PAPER", "true").lower() == "true",
                        help="Check for paper trading, uncheck for live trading.",
                    )
                # Test Connection Button
                if st.button("🔌 Test Alpaca Connection", key="test_alpaca_btn"):
                    with st.spinner("Testing connection to Alpaca API..."):
                        ok, msg = test_alpaca_credentials(a_api_key, a_api_secret, paper=a_paper)
                        if ok:
                            st.success(f"✅ {msg}")
                        else:
                            st.error(f"❌ {msg}")

            # Optional Cloud Analytics & Telemetry (Lumiwealth / Botspot)
            with st.expander("🌐 Cloud Telemetry & Botspot Tracking (Optional)", expanded=bool(env_vals.get("LUMIWEALTH_API_KEY"))):
                st.caption("Track your live bot execution and metrics online at [botspot.trade](https://www.botspot.trade).")
                lw_api_key = st.text_input(
                    "Lumiwealth API Key (`LUMIWEALTH_API_KEY`)",
                    value=env_vals.get("LUMIWEALTH_API_KEY", ""),
                    type="password",
                    help="Optional. Leave empty to run fully local without cloud sync.",
                )

            st.divider()

            # Strategy Parameters Section
            st.markdown("#### 📐 Iron Condor Strategy Parameters")
            p_col1, p_col2, p_col3 = st.columns(3)

            with p_col1:
                symbol_options = ["XSP", "SPX", "SPY"]
                curr_sym = env_vals.get("IC_SYMBOL", "XSP").upper()
                sym_idx = symbol_options.index(curr_sym) if curr_sym in symbol_options else 0
                cfg_symbol = st.selectbox("Underlying Symbol", symbol_options, index=sym_idx, help="XSP (Mini-SPX, cash-settled), SPX (Full Index), SPY (ETF).")
                cfg_dte = st.number_input("Target DTE (Days to Expiration)", min_value=0, max_value=45, value=int(env_vals.get("IC_DTE", "0")), help="0 for 0DTE intraday, 1 for 1DTE overnight.")
                cfg_delta = st.slider("Short Strike Delta (Δ)", min_value=0.05, max_value=0.35, value=float(env_vals.get("IC_SHORT_DELTA", "0.15")), step=0.01, help="0.15 = 15-delta OTM short strikes.")

            with p_col2:
                cfg_wing = st.number_input("Wing Width in Points", min_value=0.5, max_value=50.0, value=float(env_vals.get("IC_WING_WIDTH", "2.0")), step=0.5, help="Spread width between short and long strikes (e.g. 2.0 for XSP = $200 risk/contract).")
                cfg_qty = st.number_input("Contracts Quantity", min_value=1, max_value=50, value=int(env_vals.get("IC_QUANTITY", "1")), step=1, help="Number of Iron Condor contracts per entry.")
                cfg_entry = st.text_input("Entry Time (ET, HH:MM)", value=env_vals.get("IC_ENTRY_TIME", "10:00"), help="Start of entry window in Eastern Time.")

            with p_col3:
                cfg_pt = st.slider("Profit Target (% of Credit)", min_value=0.10, max_value=0.80, value=float(env_vals.get("IC_PROFIT_TARGET", "0.30")), step=0.05, help="0.30 = take profit at 30% of credit received.")
                cfg_sl = st.slider("Stop Loss Multiplier (x Credit)", min_value=1.0, max_value=4.0, value=float(env_vals.get("IC_STOP_LOSS_MULT", "2.0")), step=0.25, help="2.0 = exit if loss reaches 2.0x of credit received.")

            st.divider()

            # Risk Limits Section
            st.markdown("#### 🛡️ Capital Protection & Risk Boundaries")
            r_col1, r_col2 = st.columns(2)
            with r_col1:
                cfg_max_pos = st.number_input(
                    "Global Max Open Position Risk ($)",
                    min_value=500.0,
                    max_value=100000.0,
                    value=float(env_vals.get("IC_MAX_POSITION_RISK", "5000.0")),
                    step=500.0,
                    help="Strict dollar ceiling on the combined exposure of all open trades.",
                )
            with r_col2:
                cfg_single_pct = st.slider(
                    "Max Single-Trade Risk (% of Cap)",
                    min_value=0.05,
                    max_value=0.50,
                    value=float(env_vals.get("IC_MAX_SINGLE_TRADE_PCT", "0.30")),
                    step=0.05,
                    help="30% at $5,000 cap = $1,500 maximum loss per single trade.",
                )
                calc_single_max = cfg_max_pos * cfg_single_pct
                st.caption(f"Calculated Single-Trade Ceiling: **${calc_single_max:,.2f}**")

            st.divider()

            # Save Button
            save_btn = st.button("💾 Save All Configuration to .env", type="primary", use_container_width=True)
            if save_btn:
                new_env = {
                    "TRADING_BROKER": "tradier" if is_tradier else "alpaca",
                    "IC_SYMBOL": cfg_symbol,
                    "IC_DTE": str(cfg_dte),
                    "IC_SHORT_DELTA": f"{cfg_delta:.2f}",
                    "IC_WING_WIDTH": f"{cfg_wing:.1f}",
                    "IC_QUANTITY": str(cfg_qty),
                    "IC_PROFIT_TARGET": f"{cfg_pt:.2f}",
                    "IC_STOP_LOSS_MULT": f"{cfg_sl:.2f}",
                    "IC_ENTRY_TIME": cfg_entry,
                    "IC_MAX_POSITION_RISK": f"{cfg_max_pos:.1f}",
                    "IC_MAX_SINGLE_TRADE_PCT": f"{cfg_single_pct:.2f}",
                }
                if is_tradier:
                    new_env["TRADIER_API_KEY"] = t_api_key.strip()
                    new_env["TRADIER_ACCOUNT"] = t_account.strip()
                    new_env["TRADIER_ACCOUNT_NUMBER"] = t_account.strip()
                    new_env["TRADIER_IS_PAPER"] = "true" if t_is_paper else "false"
                    new_env["TRADIER_SANDBOX"] = "1" if t_is_paper else "0"
                else:
                    new_env["ALPACA_API_KEY"] = a_api_key.strip()
                    new_env["ALPACA_API_SECRET"] = a_api_secret.strip()
                    new_env["ALPACA_PAPER"] = "true" if a_paper else "false"

                if "lw_api_key" in locals() and lw_api_key.strip():
                    new_env["LUMIWEALTH_API_KEY"] = lw_api_key.strip()

                ok, msg = save_env_values(new_env)
                if ok:
                    st.success(f"✅ {msg}")
                    st.info("💡 To apply updated credentials to the trading bot engine, restart the bot container: `docker compose restart lumibot-bot`")
                    st.rerun()
                else:
                    st.error(f"❌ {msg}")


        # =============================================================
        # TAB 6: REAL-TIME LOG CONSOLE
        # =============================================================
        with tab_logs:
            st.subheader("📜 Real-Time Engine Log Console (GUI-05)")
            st.caption("Live streaming from `logs/trading.log` inside OrbStack container.")

            log_filter = st.radio("Log Filter", ["ALL", "HEARTBEAT", "ORDERS", "ERRORS & WARNINGS"], horizontal=True)

            log_file = Path("logs/trading.log")
            if log_file.exists():
                with open(log_file, "r", encoding="utf-8", errors="replace") as f:
                    all_lines = f.readlines()[-200:]

                if log_filter == "HEARTBEAT":
                    display_lines = [l for l in all_lines if "lifecycle method" in l or "check in" in l]
                elif log_filter == "ORDERS":
                    display_lines = [l for l in all_lines if "order" in l.lower() or "condor" in l.lower()]
                elif log_filter == "ERRORS & WARNINGS":
                    display_lines = [l for l in all_lines if "WARNING" in l or "ERROR" in l]
                else:
                    display_lines = all_lines

                log_content = "".join(display_lines) if display_lines else "No matching log entries found."
                st.markdown(f'<div class="log-box">{escape(log_content)}</div>', unsafe_allow_html=True)
            else:
                st.info("Log file `logs/trading.log` not found.")


        # =============================================================
        # TAB 7: USER FEATURES REFERENCE
        # =============================================================
        with tab_features:
            st.subheader("📘 Supported User Features Catalog (USER_FEATURES.md)")
            candidates = [
                Path("USER_FEATURES.md"),
                Path("dashboard/USER_FEATURES.md"),
                Path("/app/USER_FEATURES.md"),
                Path("/app/dashboard/USER_FEATURES.md"),
            ]
            uf_path = next((p for p in candidates if p.exists()), None)
            if uf_path:
                with open(uf_path, "r", encoding="utf-8") as f:
                    st.markdown(f.read())
            else:
                st.info("USER_FEATURES.md not found in project root.")


    if hasattr(st, "fragment"):
        st.fragment(run_every=30)(render_dashboard)()
    else:
        render_dashboard()


if __name__ == "__main__":
    main()
