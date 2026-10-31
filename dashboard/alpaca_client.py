"""Alpaca Trading Client wrapper for Streamlit dashboard."""
import os
import logging
from typing import Optional, Dict, Any, List
from dotenv import load_dotenv

load_dotenv()
logger = logging.getLogger(__name__)

# Try to import alpaca-py
try:
    from alpaca.trading.client import TradingClient
    from alpaca.trading.requests import GetOrdersRequest
    from alpaca.trading.enums import QueryOrderStatus
    ALPACA_AVAILABLE = True
except ImportError:
    ALPACA_AVAILABLE = False


def get_client() -> Optional[Any]:
    """Instantiate Alpaca TradingClient if credentials exist."""
    if not ALPACA_AVAILABLE:
        return None
    api_key = os.getenv("ALPACA_API_KEY", "").strip()
    api_secret = os.getenv("ALPACA_API_SECRET", "").strip()
    paper = os.getenv("ALPACA_PAPER", "true").lower() == "true"

    if not api_key or not api_secret or api_key == "your_paper_api_key_here":
        return None

    try:
        return TradingClient(api_key=api_key, secret_key=api_secret, paper=paper)
    except Exception as e:
        logger.error(f"Error initializing TradingClient: {e}")
        return None


def fetch_account_info(client) -> Optional[Dict[str, Any]]:
    """Fetch basic account equity and buying power."""
    if not client:
        return None
    try:
        acc = client.get_account()
        return {
            "status": acc.status.value if hasattr(acc.status, "value") else str(acc.status),
            "currency": acc.currency,
            "equity": float(acc.equity) if acc.equity is not None else 0.0,
            "cash": float(acc.cash) if acc.cash is not None else 0.0,
            "buying_power": float(acc.buying_power) if acc.buying_power is not None else 0.0,
            "portfolio_value": float(acc.portfolio_value) if acc.portfolio_value is not None else 0.0,
            "last_equity": float(acc.last_equity) if acc.last_equity is not None else 0.0,
            "multiplier": acc.multiplier,
            "paper": client._paper if hasattr(client, "_paper") else True,
        }
    except Exception as e:
        logger.error(f"Error fetching account info: {e}")
        return None


def fetch_market_clock(client) -> Optional[Dict[str, Any]]:
    """Fetch current market open/close clock."""
    if not client:
        return None
    try:
        clock = client.get_clock()
        return {
            "is_open": clock.is_open,
            "timestamp": clock.timestamp,
            "next_open": clock.next_open,
            "next_close": clock.next_close,
        }
    except Exception as e:
        logger.error(f"Error fetching market clock: {e}")
        return None


def fetch_positions(client) -> List[Dict[str, Any]]:
    """Fetch current open positions."""
    if not client:
        return []
    try:
        positions = client.get_all_positions()
        res = []
        for p in positions:
            res.append({
                "symbol": p.symbol,
                "qty": float(p.qty),
                "side": p.side.value if hasattr(p.side, "value") else str(p.side),
                "market_value": float(p.market_value) if p.market_value is not None else None,
                "cost_basis": float(p.cost_basis) if p.cost_basis is not None else 0.0,
                "unrealized_pl": float(p.unrealized_pl) if p.unrealized_pl is not None else None,
                "unrealized_plpc": float(p.unrealized_plpc) if p.unrealized_plpc is not None else None,
                "current_price": float(p.current_price) if p.current_price is not None else None,
                "asset_class": p.asset_class.value if hasattr(p.asset_class, "value") else str(p.asset_class),
            })
        return res
    except Exception as e:
        logger.error(f"Error fetching positions: {e}")
        return []


def fetch_recent_orders(client, limit: int = 25) -> List[Dict[str, Any]]:
    """Fetch recent orders."""
    if not client:
        return []
    try:
        req = GetOrdersRequest(
            status=QueryOrderStatus.ALL,
            limit=limit,
            nested=True,
        )
        orders = client.get_orders(filter=req)
        res = []
        for o in orders:
            res.append({
                "id": str(o.id),
                "symbol": o.symbol,
                "qty": float(o.qty) if o.qty else 0.0,
                "filled_qty": float(o.filled_qty) if o.filled_qty else 0.0,
                "side": o.side.value if hasattr(o.side, "value") else str(o.side),
                "type": o.type.value if hasattr(o.type, "value") else str(o.type),
                "status": o.status.value if hasattr(o.status, "value") else str(o.status),
                "filled_avg_price": float(o.filled_avg_price) if o.filled_avg_price else 0.0,
                "submitted_at": o.submitted_at.strftime("%Y-%m-%d %H:%M:%S") if o.submitted_at else "",
                "filled_at": o.filled_at.strftime("%Y-%m-%d %H:%M:%S") if o.filled_at else "",
            })
        return res
    except Exception as e:
        logger.error(f"Error fetching orders: {e}")
        return []


def emergency_cancel_orders(client) -> bool:
    """Cancel all active open orders."""
    if not client:
        return False
    try:
        client.cancel_orders()
        return True
    except Exception as e:
        logger.error(f"Error canceling orders: {e}")
        return False


def emergency_flatten_positions(client) -> bool:
    """Liquidate all open positions."""
    if not client:
        return False
    try:
        client.close_all_positions(cancel_orders=True)
        return True
    except Exception as e:
        logger.error(f"Error flattening positions: {e}")
        return False
