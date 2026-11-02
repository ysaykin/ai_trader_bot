"""Verify the comparison uses the entry roster without needing a Reflex server."""

import importlib.util
import os
from pathlib import Path
from unittest.mock import patch

from strategies.incubator import get_incubator_specs


def load_adapters():
    path = Path(__file__).resolve().parents[1] / "reflex_dashboard/reflex_dashboard/data/adapters.py"
    spec = importlib.util.spec_from_file_location("comparison_adapters", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_focused_comparison_shows_each_selected_strategy_once():
    adapters = load_adapters()
    with patch.dict(os.environ, {}, clear=True), \
         patch.object(adapters, "get_reconciled_trades", return_value=({}, [], [])):
        data = adapters.get_incubator_data()
        expected = [s["strategy_name"] for s in get_incubator_specs()]
    assert [r["strategy_name"] for r in data["rows"]] == expected
    assert len(data["rows"]) == data["total_variants"] == 7
    assert data["rows"][0]["rank_badge"] == "STRATEGYBRO"
    assert data["rows"][-1]["rank_badge"] == "IC CONTROL"


def test_comparison_respects_entry_limit_and_excludes_retired_trades_from_win_rate():
    adapters = load_adapters()
    closed = [
        {"strategy": "IC_V25_90DTE_STRATEGYBRO_DYNAMIC", "net_pnl": 50.0},
        {"strategy": "IC_V01_0DTE_D10_W2_PT30_T0945", "net_pnl": -100.0},
    ]
    with patch.dict(os.environ, {"INCUBATOR_STRATEGY_COUNT": "1"}, clear=True), \
         patch.object(adapters, "get_reconciled_trades", return_value=({}, closed, [])):
        data = adapters.get_incubator_data()
    assert data["total_variants"] == len(data["rows"]) == 1
    assert data["overall_win_rate"] == "100.0%"
    assert data["total_net_pnl"] == 50.0
    assert data["all_closed_trades"] == closed


def test_full_grid_comparison_remains_available():
    adapters = load_adapters()
    with patch.dict(os.environ, {"INCUBATOR_PROFILE": "full_grid"}, clear=True), \
         patch.object(adapters, "get_reconciled_trades", return_value=({}, [], [])):
        data = adapters.get_incubator_data()
    assert data["total_variants"] == 29
    assert len(data["rows"]) == 9
