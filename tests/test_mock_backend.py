"""Checks for the mock dashboard's cash and defined-risk accounting."""
from unittest.mock import patch

import pytest
from fastapi import HTTPException

from backend import app as backend


def test_credit_and_close_accounting():
    with patch.object(backend, "_state", {"positions": [], "account_balance": 100000.0, "account_equity": 100000.0}), patch.object(backend.random, "uniform", return_value=3.0):
        trade = {"short_delta": .15, "wing_width": 10, "quantity": 2, "max_allocation": .03, "symbol": "XSP", "entry_time": "10:00"}
        result = backend.place_trade(trade)
        assert result["position"]["credit_received"] == 600
        assert result["position"]["max_risk"] == 1400
        assert backend.get_status()["account"]["cash"] == 100600
        backend._state["positions"][0]["pnl"] = 125
        backend.close_all_positions()
        assert backend._state["account_balance"] == 100125
        assert backend._state["account_equity"] == 100125


def test_rejects_excess_defined_risk():
    with patch.object(backend, "_state", {"positions": [], "account_balance": 100000.0, "account_equity": 100000.0}), patch.object(backend.random, "uniform", return_value=3.0):
        with pytest.raises(HTTPException):
            backend.place_trade({"short_delta": .15, "wing_width": 20, "quantity": 4, "max_allocation": .03})
