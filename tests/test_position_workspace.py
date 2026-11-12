"""Deterministic checks for trade ownership, cash accounting and display scope."""
import json
import sqlite3
from datetime import date
from pathlib import Path

import pytest

from dashboard.positions import build_positions, display, payoff_at, payoff_bounds, payoff_figure, quote_symbols, read_packages, summarize


@pytest.fixture(autouse=True)
def pin_position_valuation_date(monkeypatch):
    # These accounting fixtures expire Nov 6. Their valuation date must not
    # advance with the machine clock; expiry tests pass their own as_of date.
    class ValuationDate(date):
        @classmethod
        def today(cls):
            return cls(2026, 11, 5)

    monkeypatch.setattr("dashboard.positions.date", ValuationDate)


def package(pid="open", trade="trade-1", strategy="IC_V23", qty=2, intent="open", legs=None):
    legs = legs or [(100, qty, .50), (105, -qty, 2.50)]
    return {"package_id": pid, "trade_id": trade, "strategy_id": strategy,
            "state": "open" if intent in {"open", "add"} else "closed", "intent": intent,
            "legs": [{"asset_key": json.dumps({"symbol": "XSP", "expiration": "2026-11-06", "right": "put", "strike": k}),
                      "filled_signed_quantity": q, "requested_signed_quantity": q, "fill_price": p} for k, q, p in legs]}


def snapshot(packages, *, missing=()):
    quantities, quotes = {}, {}
    for p in packages:
        for leg in p["legs"]:
            key = json.loads(leg["asset_key"])
            symbol = f"XSP261106P{key['strike'] * 1000:08d}"
            quantities[symbol] = quantities.get(symbol, 0) + leg["filled_signed_quantity"]
            if symbol not in missing:
                quotes[symbol] = {"mark": .4 if key["strike"] == 100 else 1.4, "delta": -.1 if key["strike"] == 100 else -.2, "theta": -.03}
    broker = [{"symbol": symbol, "quantity": q} for symbol, q in quantities.items() if q]
    return broker, quotes


def rows_for(packages, **kwargs):
    broker, quotes = snapshot(packages)
    return build_positions(packages, broker, quotes, **kwargs)


def test_whole_vertical_values_and_signed_greeks():
    rows, issues = rows_for([package()])
    row = rows[0]
    assert not issues
    assert row["structure"] == "bull_put"
    assert row["open_pnl"] == pytest.approx(200)
    assert row["net_pnl"] == pytest.approx(198.4)
    assert row["opening_risk"] == 600
    assert sum(leg["delta"] for leg in row["legs"]) == pytest.approx(20)
    assert row["estimated_fees"]
    assert payoff_at(row, 110) == pytest.approx(398.4)
    assert payoff_at(row, 95) == pytest.approx(-601.6)
    assert payoff_bounds(row)["breakevens"] == [103.01]


def test_condor_and_bear_call_shapes_and_maximum_loss():
    condor = package(qty=1, legs=[(100, 1, .5), (105, -1, 1.5), (115, -1, 1), (120, 1, .4)])
    for leg in condor["legs"][2:]:
        key = json.loads(leg["asset_key"])
        key["right"] = "call"
        leg["asset_key"] = json.dumps(key)
    # Supply broker quantities using the same contract keys, independent of marks.
    from dashboard.positions import Contract
    broker = [{"symbol": Contract.from_key(l["asset_key"]).symbol, "quantity": l["filled_signed_quantity"]} for l in condor["legs"]]
    rows, issues = build_positions([condor], broker, {})
    assert not issues
    assert rows[0]["structure"] == "iron_condor"
    assert payoff_bounds(rows[0])["max_profit"] == pytest.approx(158.4)
    assert payoff_bounds(rows[0])["max_loss"] == pytest.approx(-341.6)
    assert payoff_bounds(rows[0])["breakevens"] == [103.42, 116.58]
    bear = package(qty=1, legs=[(115, -1, 1.5), (120, 1, .4)])
    for leg in bear["legs"]:
        key = json.loads(leg["asset_key"])
        key["right"] = "call"
        leg["asset_key"] = json.dumps(key)
    broker = [{"symbol": Contract.from_key(l["asset_key"]).symbol, "quantity": l["filled_signed_quantity"]} for l in bear["legs"]]
    rows, _ = build_positions([bear], broker, {})
    assert rows[0]["structure"] == "bear_call"
    assert payoff_bounds(rows[0])["max_profit"] == pytest.approx(109.2)
    assert payoff_bounds(rows[0])["max_loss"] == pytest.approx(-390.8)


def test_additions_stay_with_the_trade_and_pending_close_stays_visible():
    opening, addition = package(qty=1), package("addition", qty=1, intent="add")
    pending = package("closing", None, intent="close", legs=[(100, -1, .4), (105, 1, 1.4)])
    pending["state"] = "submitted"
    for leg in pending["legs"]:
        leg["filled_signed_quantity"] = 0
    rows, issues = rows_for([opening, addition, pending])
    assert not issues
    assert len(rows) == 1 and rows[0]["quantity"] == 2
    assert rows[0]["open_pnl"] == pytest.approx(200)
    assert rows[0]["status"] == "Closing"


def test_explicitly_linked_roll_preserves_realized_profit_and_trade_identity():
    opening = package(qty=1)
    close = package("close", intent="close", legs=[(100, -1, .4), (105, 1, 1.4)])
    rolled = package("rolled", qty=1)
    for leg in rolled["legs"]:
        contract = json.loads(leg["asset_key"])
        contract["expiration"] = "2026-11-13"
        leg["asset_key"] = json.dumps(contract)
    from dashboard.positions import Contract
    broker = [{"symbol": Contract.from_key(l["asset_key"]).symbol, "quantity": l["filled_signed_quantity"]} for l in rolled["legs"]]
    quotes = {p["symbol"]: {"mark": .4 if p["quantity"] > 0 else 1.4} for p in broker}
    rows, issues = build_positions([opening, close, rolled], broker, quotes)
    assert not issues and len(rows) == 1
    assert rows[0]["trade_id"] == "trade-1"
    assert rows[0]["expiry"] == "2026-11-13"
    assert rows[0]["realized_pnl"] == pytest.approx(100)
    assert rows[0]["open_pnl"] == pytest.approx(100)
    assert rows[0]["net_pnl"] == pytest.approx(197.6)


def test_partial_fill_retains_inventory_and_does_not_plot_a_complete_spread():
    partial = package()
    partial["state"] = "partial"
    partial["legs"][1]["filled_signed_quantity"] = 0
    rows, issues = rows_for([partial])
    assert not issues and len(rows) == 1
    assert rows[0]["structure"] == "adjusted"
    assert rows[0]["opening_risk"] is None
    assert not rows[0]["payoff_eligible"]


def test_trim_keeps_realized_and_open_profit_separate_without_double_counting():
    opening = package()
    trim = package("trim", intent="close", legs=[(100, -1, .4), (105, 1, 1.4)])
    rows, issues = rows_for([opening, trim], reported_fees={"trade-1": 2.4})
    row = rows[0]
    assert not issues
    assert row["quantity"] == 1
    assert row["open_pnl"] == pytest.approx(100)
    assert row["realized_pnl"] == pytest.approx(100)
    assert row["net_pnl"] == pytest.approx(197.6)
    assert not row["estimated_fees"]
    assert payoff_at(row, 95) == pytest.approx(-202.4)


def test_partial_journal_fee_coverage_does_not_claim_complete_reported_fees():
    packages = [package(), package("trim", intent="close", legs=[(100, -1, .4), (105, 1, 1.4)])]
    rows, _ = rows_for(packages, reported_fees={"trade-1": 1.6}, reported_fee_contracts={"trade-1": 4})
    assert rows[0]["estimated_fees"]
    assert rows[0]["fees"] == pytest.approx(2.4)


def test_shared_contracts_remain_separate_trades_and_sum_to_broker():
    first, second = package(qty=1), package("second", "trade-2", "IC_V24", qty=1)
    second["legs"][1]["fill_price"] = 3
    rows, issues = rows_for([first, second])
    assert not issues
    assert len(rows) == 2
    assert rows[0]["open_pnl"] == pytest.approx(100)
    assert rows[1]["open_pnl"] == pytest.approx(150)
    assert summarize(rows)["open_pnl"] == pytest.approx(250)


def test_unique_legacy_close_can_link_but_ambiguous_close_cannot():
    trim = package("trim", None, intent="close", legs=[(100, -1, .4), (105, 1, 1.4)])
    rows, issues = rows_for([package(), trim])
    assert not issues
    assert rows[0]["realized_pnl"] == pytest.approx(100)
    rows, issues = rows_for([package(qty=1), package("second", "trade-2", qty=1), trim])
    assert any("unique trade link" in issue for issue in issues)
    assert all(r["net_pnl"] is None for r in rows)


def test_missing_mark_or_broker_does_not_manufacture_zero_profit():
    packages = [package()]
    broker, quotes = snapshot(packages, missing=("XSP261106P00100000",))
    rows, _ = build_positions(packages, broker, quotes)
    assert rows[0]["net_pnl"] is None
    assert summarize(rows)["net_pnl"] is None
    rows, issues = build_positions(packages, None, {})
    assert issues
    assert rows[0]["net_pnl"] is None


@pytest.mark.parametrize("long_mark,short_mark", [(2, 1), (.5, 6), (8, 1)])
def test_inconsistent_spread_marks_do_not_distort_strategy_totals(long_mark, short_mark):
    packages = [package(qty=1)]
    broker, quotes = snapshot(packages)
    quotes["XSP261106P00100000"]["mark"] = long_mark
    quotes["XSP261106P00105000"]["mark"] = short_mark
    rows, issues = build_positions(packages, broker, quotes)
    assert any("outside defined payoff bounds" in issue for issue in issues)
    assert rows[0]["quality"] == "Inconsistent quotes"
    assert rows[0]["net_pnl"] is None
    assert all(leg["pnl"] is None for leg in rows[0]["legs"])
    assert summarize(rows)["net_pnl"] is None
    assert rows[0]["payoff_eligible"]
    assert payoff_bounds(rows[0])["max_loss"] == pytest.approx(-300.8)


def test_mismatch_and_unattributed_broker_inventory_stay_visible():
    broker, quotes = snapshot([package()])
    broker.append({"symbol": "SPY261106C00500000", "quantity": -1})
    rows, issues = build_positions([package()], broker, quotes)
    assert len(rows) == 2 and issues
    assert any(r["strategy"] == "Unattributed" for r in rows)
    assert summarize(rows)["net_pnl"] is None


def test_closed_and_unfilled_packages_are_not_open_positions():
    close = package("close", intent="close", legs=[(100, -2, .4), (105, 2, 1.4)])
    rows, _ = rows_for([package(), close])
    assert rows == []
    unfilled = package()
    for leg in unfilled["legs"]:
        leg["filled_signed_quantity"] = 0
    assert rows_for([unfilled])[0] == []


def test_expired_absent_inventory_awaits_settlement_without_poisoning_open_pnl():
    expired = package("expired", "expired-trade", qty=1)
    pending = package("close", "expired-trade", qty=1, intent="close")
    pending["state"] = "submitted"
    for leg in pending["legs"]:
        leg["filled_signed_quantity"] = 0
    live = package("live", "live-trade", "IC_V25", qty=1)
    for leg in live["legs"]:
        key = json.loads(leg["asset_key"])
        key["expiration"] = "2026-11-20"
        leg["asset_key"] = json.dumps(key)
    from dashboard.positions import Contract
    broker = [{"symbol": Contract.from_key(l["asset_key"]).symbol,
               "quantity": l["filled_signed_quantity"]} for l in live["legs"]]
    quotes = {p["symbol"]: {"mark": .4 if p["quantity"] > 0 else 1.4} for p in broker}
    # Even a retained old quote cannot value a settled or expired contract.
    quotes.update(snapshot([expired])[1])
    rows, issues = build_positions([expired, pending, live], broker, quotes, as_of=date(2026, 11, 7))
    past, current = rows
    assert past["status"] == "Awaiting settlement"
    assert past["quality"] == "Settlement unverified"
    assert past["open_pnl"] is None and past["net_pnl"] is None
    assert not past["reconciled"] and not past["payoff_eligible"]
    assert all(leg["mark"] is None and leg["delta"] is None for leg in past["legs"])
    assert current["open_pnl"] == pytest.approx(100)
    totals = summarize(rows)
    assert totals["open_pnl"] == pytest.approx(100)
    assert totals["net_pnl"] is None
    assert totals["awaiting_settlement"] == 1 and totals["open_count"] == 1
    assert any("awaiting settlement" in issue for issue in issues)
    assert not any("quantity mismatch" in issue for issue in issues)
    import sys
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "reflex_dashboard"))
    from reflex_dashboard.state import DashboardState
    state = DashboardState(_reflex_internal_init=True)
    state.position_records = rows
    state.position_data_status = "ready"
    assert state.position_summary["open"]["text"] == "$100.00"
    assert state.position_summary["net"]["text"] == "N/A"
    assert state.position_summary["coverage"] == "1 of 1 open positions valued · 1 awaiting settlement"
    assert state._position_display_row(past)["dte_label"] == "Expired 1d ago"


def test_missing_broker_and_unexpired_absence_remain_reconciliation_failures():
    rows, issues = build_positions([package()], None, {}, as_of=date(2026, 11, 7))
    assert rows[0]["open_pnl"] is None and summarize(rows)["open_pnl"] is None
    assert any("Broker positions unavailable" in issue for issue in issues)
    rows, issues = build_positions([package()], [], {}, as_of=date(2026, 11, 6))
    assert rows[0]["status"] == "Open"
    assert any("quantity mismatch" in issue for issue in issues)


def test_expired_contract_still_at_broker_remains_a_quantity_mismatch():
    p = package(qty=1)
    broker, quotes = snapshot([p])
    broker[0]["quantity"] = 2
    rows, issues = build_positions([p], broker, quotes, as_of=date(2026, 11, 7))
    assert any("quantity mismatch" in issue for issue in issues)
    assert rows[0]["net_pnl"] is None


def test_quote_demand_excludes_past_expiration_inventory():
    assert quote_symbols([package()], as_of=date(2026, 11, 7)) == set()
    assert "XSP" in quote_symbols([package()], as_of=date(2026, 11, 6))


def test_expiration_payoff_requires_one_scope_and_normalizes_by_summed_risk():
    rows, _ = rows_for([package()])
    assert not payoff_figure(rows, "wrong scope").data
    figure = payoff_figure(rows, rows[0]["scope"], "% of opening risk")
    assert len(figure.data) == 1
    assert max(figure.data[0].y) == pytest.approx(398.4 / 600 * 100)


def test_rounding_never_displays_negative_zero_and_negative_values_are_red():
    assert display(-12.5) == {"text": "-$12.50", "color": "#EF4444"}
    assert display(-.00001) == {"text": "$0.00", "color": "#94A3B8"}
    assert display(None)["text"] == "N/A"
    assert display(-3, currency=False)["text"] == "-3.00"


def test_ledger_reader_never_creates_or_migrates_database(tmp_path):
    missing = tmp_path / "missing.sqlite3"
    with pytest.raises(sqlite3.OperationalError):
        read_packages(missing)
    assert not missing.exists()
    path = tmp_path / "ledger.sqlite3"
    with sqlite3.connect(path) as c:
        c.execute("CREATE TABLE packages(package_id TEXT, created_at TEXT)")
        c.execute("CREATE TABLE legs(order_id TEXT, package_id TEXT)")
    before = path.read_bytes()
    assert read_packages(path) == []
    assert path.read_bytes() == before


def test_filter_selection_survives_refresh_and_clear_is_not_all():
    import sys
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "reflex_dashboard"))
    from reflex_dashboard.state import DashboardState
    state = DashboardState(_reflex_internal_init=True)
    state.position_records = rows_for([package(), package("second", "trade-2", "IC_V24")])[0]
    assert len(state.filtered_position_records) == 2
    state.toggle_position_strategy("IC_V23")
    assert [r["strategy"] for r in state.filtered_position_records] == ["IC_V24"]
    state.position_records = rows_for([package(), package("second", "trade-2", "IC_V24")])[0]
    assert [r["strategy"] for r in state.filtered_position_records] == ["IC_V24"]
    state.clear_position_strategies()
    assert state.filtered_position_records == []
    state.reset_position_filters()
    assert len(state.filtered_position_records) == 2


def test_broker_failure_has_no_example_balance_or_successful_empty_position(tmp_path):
    import sys
    from unittest.mock import Mock, patch
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "reflex_dashboard"))
    from reflex_dashboard.data.adapters import get_broker_telemetry
    from strategies.broker_reads import BrokerReads
    env = {"TRADING_BROKER": "tradier", "TRADIER_ACCOUNT_NUMBER": "test", "TRADIER_ACCESS_TOKEN": "test"}
    response = Mock(status_code=503)
    response.headers = {}
    reads = BrokerReads("test", "test", path=tmp_path / "snapshots.sqlite3", transport=lambda *a, **k: response, sleep=lambda _: None)
    reads.collect_once()
    with patch("reflex_dashboard.data.adapters.PROJECT_ROOT", tmp_path), \
         patch("reflex_dashboard.data.adapters.dotenv_values", return_value=env), \
         patch("strategies.broker_reads.get_broker_reads", return_value=reads), patch.object(reads, "start"):
        value = get_broker_telemetry()
    assert value["equity"] is None
    assert value["buying_power_headroom_pct"] is None
    assert value["raw_positions"] is None
    assert value["positions_status"] == "unavailable"


def test_zero_balance_is_preserved_and_successful_null_positions_are_empty(tmp_path):
    import sys
    from unittest.mock import Mock, patch
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "reflex_dashboard"))
    from reflex_dashboard.data.adapters import get_broker_telemetry
    from strategies.broker_reads import BrokerReads
    env = {"TRADING_BROKER": "tradier", "TRADIER_ACCOUNT_NUMBER": "test", "TRADIER_ACCESS_TOKEN": "test"}
    responses = [Mock(status_code=200, json=lambda: {"balances": {"total_equity": 0, "total_cash": 0, "margin": {"option_buying_power": 0}}}),
                 Mock(status_code=200, json=lambda: {"positions": "null"}),
                 Mock(status_code=200, json=lambda: {"orders": {"order": []}})]
    for response in responses:
        response.headers = {}
    reads = BrokerReads("test", "test", path=tmp_path / "snapshots.sqlite3", transport=Mock(side_effect=responses))
    reads.collect_once()
    with patch("reflex_dashboard.data.adapters.PROJECT_ROOT", tmp_path), \
         patch("reflex_dashboard.data.adapters.dotenv_values", return_value=env), \
         patch("strategies.broker_reads.get_broker_reads", return_value=reads), patch.object(reads, "start"):
        value = get_broker_telemetry()
    assert value["equity"] == 0
    assert value["cash"] == 0
    assert value["raw_positions"] == []
    assert value["positions_status"] == "ready"
