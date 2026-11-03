"""Verify the comparison uses the entry roster without needing a Reflex server."""

import importlib.util
import os
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from strategies.position_ledger import LedgerLeg, PositionLedger, asset_key

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


def test_scanner_keeps_every_configured_strategy_after_open_and_closed_trades(tmp_path):
    adapters = load_adapters()
    ledger = PositionLedger(tmp_path / "ledger.sqlite3")
    ledger.close()
    with patch.dict(os.environ, {"POSITION_LEDGER_PATH": str(tmp_path / "ledger.sqlite3")}, clear=True):
        names = [spec["strategy_name"] for spec in get_incubator_specs()]
        closed = [{"strategy": name, "net_pnl": 10} for name in names]
        opened = [{"strategy": name} for name in names]
        with patch.object(adapters, "get_reconciled_trades", return_value=({}, closed, opened)):
            data = adapters.get_incubator_data()
    assert [row["strategy_name"] for row in data["monitored_rows"]] == names
    assert len(data["traded_rows"]) == len(names)


def test_scanner_counts_filled_and_pending_positions_in_capacity(tmp_path):
    adapters = load_adapters()
    owner = "IC_V26_10DTE_TRENDFLOW_DYNAMIC"
    path = tmp_path / "ledger.sqlite3"
    ledger = PositionLedger(path)
    try:
        for package in ("filled", "pending"):
            legs = [LedgerLeg(package + str(strike), asset_key(SimpleNamespace(
                symbol="XSP", asset_type="option", expiration="2026-11-20", right="put", strike=strike)), quantity)
                for strike, quantity in ((90, 1), (95, -1))]
            ledger.record_submission(package, owner, "open", legs)
            ledger.bind_broker_submission(package, SimpleNamespace(identifier=package + "-parent", child_orders=[]))
            if package == "filled":
                for leg in legs:
                    ledger.record_fill(leg.order_id, leg.requested_signed_quantity, .5)
        with patch.dict(os.environ, {"POSITION_LEDGER_PATH": str(path), "IC_MAX_CONCURRENT_POSITIONS": "2"}, clear=True), \
             patch.object(adapters, "get_reconciled_trades", return_value=({}, [], [])):
            data = adapters.get_incubator_data()
        row = next(row for row in data["monitored_rows"] if row["strategy_name"] == owner)
        assert row["position_slots"] == "2/2"
        assert row["scan_status"] == "CAPACITY FULL"
        assert len(data["monitored_rows"]) == 7
    finally:
        ledger.close()


def test_scanner_does_not_claim_disabled_neural_policy_is_active(tmp_path):
    adapters = load_adapters()
    path = tmp_path / "ledger.sqlite3"
    ledger = PositionLedger(path)
    ledger.close()
    with patch.dict(os.environ, {"POSITION_LEDGER_PATH": str(path), "ML_POLICY_ENABLED": "false"}, clear=True), \
         patch.object(adapters, "get_reconciled_trades", return_value=({}, [], [])):
        data = adapters.get_incubator_data()
    row = next(row for row in data["monitored_rows"] if row["is_neural"])
    assert row["scan_status"] == "POLICY DISABLED"
