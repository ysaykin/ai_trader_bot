"""Lumibot Trading Dashboard Backend - FastAPI server."""

import random
import uuid
from datetime import datetime
from typing import Any

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

app = FastAPI(title="Lumibot Trading Dashboard", version="1.0.0")

_state = {"positions": [], "account_balance": 100000.0, "account_equity": 100000.0}


@app.get("/status")
def get_status() -> dict:
    return {
        "status": "running",
        "account": {
            "balance": _state["account_balance"],
            "equity": _state["account_equity"],
            "cash": _state["account_balance"],
        },
        "positions": _state["positions"],
        "timestamp": datetime.now().isoformat(),
    }


@app.get("/config")
def get_config() -> dict:
    return {
        "symbol": "SPX",
        "dte": 0,
        "short_delta": 0.15,
        "wing_width": 15,
        "quantity": 1,
        "profit_target": 0.30,
        "stop_loss_mult": 2.0,
        "entry_time": "10:00",
        "max_allocation": 0.03,
    }


@app.post("/trade")
def place_trade(trade: dict[str, Any]) -> dict:
    short_strike = 5000.0 + trade["short_delta"] * 100
    long_strike = short_strike - trade["wing_width"]
    quantity = int(trade["quantity"])
    width = float(trade["wing_width"])
    if quantity <= 0 or width <= 0:
        raise HTTPException(status_code=400, detail="Quantity and wing width must be positive")
    credit = random.uniform(2.5, 4.5)
    credit_received = credit * quantity * 100
    risk = max(width * quantity * 100 - credit_received, 0.0)
    current_risk = sum(p.get("max_risk", 0.0) for p in _state["positions"])
    max_position_value = min(_state["account_equity"] * trade["max_allocation"], 5000.0)
    if current_risk + risk > 5000.0 or risk > max_position_value:
        raise HTTPException(status_code=400, detail="Position exceeds defined-risk limit")

    position = {
        "id": str(uuid.uuid4())[:8],
        "symbol": trade["symbol"],
        "short_strike": short_strike,
        "long_strike": long_strike,
        "credit": credit,
        "credit_received": credit_received,
        "quantity": quantity,
        "max_risk": risk,
        "pnl": 0.0,
        "pnl_percent": 0.0,
        "status": "open",
        "entry_time": trade["entry_time"],
        "timestamp": datetime.now().isoformat(),
    }

    _state["positions"].append(position)
    _state["account_balance"] += credit_received

    return {
        "success": True,
        "message": "Trade placed successfully",
        "position": position,
        "account_equity": _state["account_equity"],
    }


@app.get("/positions")
def get_positions() -> list:
    return [p.copy() for p in _state["positions"]]


@app.post("/close")
def close_all_positions() -> dict:
    """Close all open positions and return P&L summary."""
    total_credit = sum(p["credit_received"] for p in _state["positions"])
    total_pnl = sum(p["pnl"] for p in _state["positions"])

    closed = _state["positions"].copy()
    _state["positions"] = []

    _state["account_balance"] += total_pnl - total_credit
    _state["account_equity"] += total_pnl

    result = {
        "success": True,
        "message": f"Closed {len(closed)} position(s)",
        "closed_positions": closed,
        "total_credit": total_credit,
        "total_pnl": total_pnl,
        "account_equity": _state["account_equity"],
    }
    return result


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)