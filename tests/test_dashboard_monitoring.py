"""Monitoring must distinguish evidence, missing inputs and old observations."""
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import sys
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "reflex_dashboard"))

from dashboard.presentation import build_dashboard_presentation_bundle
from dashboard.monitoring import data_health, feature_health, order_activity, strategy_results
from reflex_dashboard.data import adapters


def test_expired_quote_cannot_value_current_inventory():
    bundle = build_dashboard_presentation_bundle({
        "positions": {"status": "ready", "data": {"positions": {"position": [
            {"symbol": "XSP261120P00500000", "quantity": 1, "cost_basis": 100},
        ]}}},
        "quote:XSP261120P00500000": {
            "status": "expired", "data": {"bid": 2, "ask": 3, "last": 2.5},
        },
    })
    assert bundle["unrealized_pnl"] is None
    assert bundle["valuation_coverage"]["valued"] == 0


def test_reflex_workspace_cannot_use_expired_quote():
    telemetry = {}
    adapters._apply_broker_snapshot(telemetry, {
        "quote:XSP261120P00500000": {
            "status": "expired", "data": {"bid": 2, "ask": 3, "last": 2.5},
        },
    })
    assert telemetry["quotes"]["XSP261120P00500000"]["mark"] is None


def test_account_health_does_not_include_unused_historical_quotes():
    telemetry = {}
    snapshots = {
        "balances": {"status": "ready", "data": {"balances": {"total_equity": 100}}},
        "positions": {"status": "ready", "data": {"positions": "null"}},
        "orders": {"status": "ready", "data": {"orders": "null"}},
        "quote:OLD_CONTRACT": {"status": "expired", "data": {"last": 1}},
    }
    adapters._apply_broker_snapshot(telemetry, snapshots)
    assert telemetry["sync_status"] == "ready"
    assert telemetry["quotes"]["OLD_CONTRACT"]["mark"] is None


def test_missing_model_has_no_invented_policy_or_results(tmp_path):
    with patch.object(adapters, "PROJECT_ROOT", tmp_path):
        data = adapters.get_neural_policy_data()
    assert data["recommendation_action"] == "Unavailable"
    assert data["dynamic_pt"] == "N/A"
    assert data["contextual_selector"] == "Unavailable"
    assert data["regret_score"] == "N/A"
    assert data["capture_efficiency"] == "N/A"
    assert data["has_sufficient_data"] is False


def test_incomplete_duplicate_ticks_do_not_satisfy_readiness(tmp_path):
    records = tmp_path / "logs"
    records.mkdir()
    today = datetime.now(adapters.EASTERN_TZ).date().isoformat()
    incomplete = {"event": "SNAPSHOT", "session": today, "record_id": "same-tick",
                  "complete": False, "market": {"vix": None}}
    (records / "trajectories.jsonl").write_text((json.dumps(incomplete) + "\n") * 12)
    with patch.object(adapters, "PROJECT_ROOT", tmp_path), \
         patch("strategies.runtime_records.resolve_record_path", return_value=records / "trajectories.jsonl"):
        data = adapters.get_neural_policy_data()
    assert data["current_ticks"] == 0
    assert data["has_sufficient_data"] is False


def test_old_log_does_not_claim_the_bot_is_running(tmp_path):
    log = tmp_path / "trading.log"
    old = datetime.now(adapters.EASTERN_TZ) - timedelta(days=1)
    log.write_text(old.strftime("%Y-%m-%d %H:%M:%S") + " INFO monitoring cycle completed\n")
    with patch("strategies.runtime_records.resolve_record_path", return_value=log):
        data = adapters.get_bot_status_from_log()
    assert data["state"] == "STALE / UNCONFIRMED"
    assert data["badge_color"] != "jade"


def test_structured_engine_telemetry_has_a_readable_activity_message(tmp_path):
    log = tmp_path / "trading.log"
    stamp = datetime.now(adapters.EASTERN_TZ).strftime("%Y-%m-%d %H:%M:%S")
    log.write_text(stamp + " INFO " + json.dumps({"strategy_name": "IC_V25_90DTE_STRATEGYBRO_DYNAMIC",
                                                "broker_snapshot": {}, "process_rss_bytes": 100, "ts": 1}) + "\n")
    with patch("strategies.runtime_records.resolve_record_path", return_value=log):
        data = adapters.get_bot_status_from_log()
    assert "Broker telemetry received" in data["last_action"]
    assert "process_rss_bytes" not in data["last_action"]


def test_recent_receipt_does_not_hide_delayed_vix():
    now = datetime(2026, 11, 9, 16, tzinfo=timezone.utc)
    telemetry = {"broker": "TRADIER", "quote_snapshots": {"VIX": {
        "status": "ready", "age": 2, "data": {"last": 20.1,
        "trade_date": (now - timedelta(minutes=15)).timestamp() * 1000}}}}
    vix = data_health(telemetry, symbols=["VIX"], session_open=True, now=now)[-1]
    assert vix["status"] == "Delayed"
    assert vix["received"] == "2s"
    assert vix["provider_age"] == "15m 0s"


def test_empty_broker_orders_does_not_erase_unconfirmed_reservations():
    package = {"package_id": "local-1", "strategy_id": "StrategyBro", "intent": "open",
               "state": "submitted", "legs": [{"requested_signed_quantity": -2, "filled_signed_quantity": -1}]}
    rows = order_activity({"ledger_packages": [package], "raw_orders": [], "orders_status": "ready"})
    assert len(rows) == 1
    assert rows[0]["status"] == "Unconfirmed"
    assert rows[0]["pending"] and rows[0]["needs_review"]
    assert rows[0]["progress"] == "1/2 leg contracts filled"


def test_order_ownership_uses_parent_identity_and_keeps_reconciliation_visible():
    packages = [{"package_id": "local-1", "broker_parent_id": "123", "position_id": "position-1",
                 "strategy_id": "StrategyBro", "intent": "close", "state": "closing", "legs": []}]
    rows = order_activity({"ledger_packages": packages, "orders_status": "ready", "raw_orders": [
        {"id": 123, "status": "filled", "price": 1, "avg_fill_price": .99},
        {"id": 456, "tag": "StrategyBro", "status": "pending"},
    ]})
    match = next(row for row in rows if row["id"] == "123")
    assert match["strategy"] == "StrategyBro"
    assert match["position_id"] == "position-1"
    assert match["evidence"] == "Ledger reconciliation pending"
    assert not match["pending"]
    assert match["needs_review"]
    assert next(row for row in rows if row["id"] == "456")["strategy"] == "Unattributed"


def test_no_closed_trades_has_no_success_claim_or_win_rate():
    rows = strategy_results({"all_rows": [{"strategy_name": "StrategyBro", "variant": "Bro", "closed_trades": 0,
        "net_pnl": 0, "win_rate": "0%", "scan_status": "ORDER REVIEW REQUIRED", "scan_status_color": "amber",
        "entry_time": "10:30 ET"}]}, [], [])
    assert rows[0]["net"] == "N/A"
    assert rows[0]["win_rate"] == "N/A"
    assert rows[0]["outcome"] == "No verified closed trades"


def test_model_warmup_does_not_hide_missing_market_features():
    rows = feature_health({"current_ticks": 12, "latest_snapshot": {"market": {"vix": None}}})
    assert rows[0]["status"] == "Missing"


def test_rejected_order_is_not_green_or_market_priced():
    rows = order_activity({"orders_status": "ready", "raw_orders": [{"id": 1, "status": "rejected", "type": "limit"}]})
    assert rows[0]["needs_review"]
    assert rows[0]["color"] == "amber"
    assert rows[0]["price"] == "N/A"


def test_unfilled_order_has_no_average_fill_price_and_current_orders_sort_first():
    package = {"package_id": "old", "strategy_id": "Bro", "state": "submitted", "intent": "open", "legs": []}
    current = {**package, "package_id": "current", "broker_parent_id": "5"}
    rows = order_activity({"orders_status": "ready", "ledger_packages": [package, current],
                          "raw_orders": [{"id": 5, "status": "pending", "exec_quantity": 0, "avg_fill_price": 0}]})
    assert rows[0]["id"] == "5"
    assert rows[0]["average"] == "N/A"


def test_order_view_keeps_unconfirmed_rows_and_can_show_filled_orders():
    from reflex_dashboard.state import DashboardState
    state = DashboardState(_reflex_internal_init=True)
    state.execution_orders = [
        {"id": "pending", "pending": True, "needs_review": True},
        {"id": "filled", "pending": False, "needs_review": False},
    ]
    assert [row["id"] for row in state.visible_execution_orders] == ["pending"]
    state.set_order_view_mode("All recent orders")
    assert len(state.visible_execution_orders) == 2
    state.inspect_order_position("position-1", "StrategyBro")
    assert state.current_tab == "positions"
    assert state.selected_position_id == "position-1"
    assert state.position_strategy_filters == ["StrategyBro"]


def test_complete_snapshot_duplicates_count_once_and_use_scoped_record_path(tmp_path):
    trajectory = tmp_path / "scoped-trajectories.jsonl"
    session = datetime.now(adapters.EASTERN_TZ).date().isoformat()
    tick = {"event": "SNAPSHOT", "session": session, "record_id": "tick-1", "complete": True,
            "market": {"vix": None}}
    trajectory.write_text((json.dumps(tick) + "\n") * 12)
    with patch.object(adapters, "PROJECT_ROOT", tmp_path), \
         patch("strategies.runtime_records.resolve_record_path", return_value=trajectory) as resolver:
        data = adapters.get_neural_policy_data()
    resolver.assert_called_once_with("TRAJECTORY_LOG_PATH", "logs/trajectories.jsonl")
    assert data["current_ticks"] == 1
    assert data["has_sufficient_data"] is False
    assert data["market_regime"] == "Unavailable"


def test_invalid_model_file_does_not_satisfy_readiness(tmp_path):
    model = tmp_path / "logs/models/policy.json"
    model.parent.mkdir(parents=True)
    model.write_text('{"sequence_length": 1}')
    with patch.object(adapters, "PROJECT_ROOT", tmp_path):
        data = adapters.get_neural_policy_data()
    assert data["model_loaded"] is False
    assert data["has_sufficient_data"] is False
    assert data["recommendation_action"] == "Unavailable"


def test_overview_separates_attributed_inventory_and_verified_closed_results():
    from reflex_dashboard.state import DashboardState
    owned = {"strategy": "Bro", "structure": "bull_put", "open_pnl": 5, "net_pnl": 4,
             "opening_risk": 100, "realized_pnl": 0, "fees": 1, "estimated_fees": True}
    unmatched = {**owned, "strategy": "Unattributed", "structure": "unattributed", "open_pnl": None, "net_pnl": None}
    expired = {**owned, "awaiting_settlement": True, "open_pnl": None, "net_pnl": None}
    state = DashboardState(_reflex_internal_init=True)
    state._load_monitoring({"broker": "TRADIER", "positions_status": "ready", "orders_status": "ready"},
        {"rows": [owned, unmatched, expired], "issues": []},
        {"all_rows": [], "eligible_closed_trades": [{"net_pnl": 10}]}, {}, {"is_open": True})
    metrics = {row["label"]: row for row in state.overview_metrics}
    assert metrics["Attributed open positions"]["value"] == "1"
    assert metrics["Attributed open positions"]["note"] == "1 unattributed contract rows · 1 awaiting settlement"
    assert metrics["Marked open P&L"]["value"] == "N/A"
    assert metrics["Verified closed P&L"]["value"] == "$10.00"
