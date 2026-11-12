"""Delayed provider bars remain usable with explicit source timestamps."""
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import Mock

from dashboard.monitoring import feature_health
from ml.features import MARKET_TZ, market_features
from trade_logger import TrajectoryLogger


OPENING = datetime(2026, 11, 10, 9, 30, tzinfo=MARKET_TZ)
NOW = OPENING + timedelta(minutes=50)


def minute_bars(count=35, *, volume=10, scale=1):
    return [{"timestamp": OPENING + timedelta(minutes=i), "high": (101 + i) * scale,
             "low": (99 + i) * scale, "close": (100 + i) * scale, "volume": volume}
            for i in range(count)]


def test_delayed_history_supplies_momentum_and_vwap():
    features = market_features(minute_bars(), NOW)
    assert features["price_velocity_15m"] == 134 / 119 - 1
    assert features["vwap_distance"] == (134 - 117) / 117


def test_momentum_requires_the_whole_fifteen_minute_window():
    bars = minute_bars(50)
    del bars[40]
    assert market_features(bars, NOW)["price_velocity_15m"] is None


def test_vwap_rejects_zero_volume_and_incomplete_session_history():
    assert market_features(minute_bars(volume=0), NOW)["vwap_distance"] is None
    bars = minute_bars()
    del bars[5]
    assert market_features(bars, NOW)["vwap_distance"] is None
    bars = minute_bars()
    bars[5]["volume"] = None
    assert market_features(bars, NOW)["vwap_distance"] is None


def test_features_ignore_previous_sessions_and_incomplete_or_future_bars():
    bars = minute_bars()
    expected = market_features(bars, NOW)
    for stamp in (OPENING - timedelta(days=1), NOW.replace(second=30), NOW + timedelta(minutes=1)):
        bars.append({"timestamp": stamp, "high": 1e6, "low": 1, "close": 1e6, "volume": 1e6})
    assert market_features(bars, NOW) == expected


def test_collector_records_source_age_and_a_consistent_spy_vwap_proxy(tmp_path):
    strategy = SimpleNamespace(
        name="test", parameters={"quantity": 1, "wing_width": 5, "max_position_risk": 5000},
        _underlying="XSP", _owned_legs=[], _entry_orders=[], _entry_credit=None,
        _position_open=False, _exit_requested=False, get_datetime=Mock(return_value=NOW),
        get_historical_prices=Mock(side_effect=lambda asset, *args, **kwargs:
                                   minute_bars(volume=0) if asset == "XSP" else minute_bars(scale=5)),
        _fetch_last_price=Mock(side_effect=lambda asset: {"VIX": 20, "VIX9D": 19}[asset]),
    )
    row = TrajectoryLogger(["test"], tmp_path / "trajectory.jsonl").record(strategy, "HOLD")
    assert row["market"]["price_velocity_15m"] == 134 / 119 - 1
    assert row["market"]["vwap_distance"] == (670 - 585) / 585
    source = row["market_observations"]["vwap_distance"]
    assert source["symbol"] == "SPY"
    assert source["proxy_for"] == "XSP"
    assert datetime.fromisoformat(source["as_of"]) == OPENING + timedelta(minutes=35)
    rows = {item["name"]: item for item in feature_health({"latest_snapshot": row}, now=NOW)}
    assert rows["15-minute momentum"]["status"] == "Delayed"
    assert rows["15-minute momentum"]["age"] == "15m 0s"
    assert rows["VWAP distance (SPY proxy)"]["status"] == "Delayed"
    assert strategy.get_historical_prices.call_count == 2


def test_proxy_unavailable_never_reuses_index_prices_or_volume(tmp_path):
    strategy = SimpleNamespace(
        _underlying="XSP", get_historical_prices=Mock(side_effect=[minute_bars(), None]),
        _fetch_last_price=Mock(return_value=None),
    )
    collector = TrajectoryLogger(["test"], tmp_path / "trajectory.jsonl")
    features = collector._market_state(strategy, NOW)
    assert features["vwap_distance"] is None
    assert features["price_velocity_15m"] is not None
    assert collector._market_observations["vwap_distance"]["as_of"] is None


def test_non_index_uses_its_own_vwap_without_an_extra_history_read(tmp_path):
    strategy = SimpleNamespace(
        _underlying=SimpleNamespace(symbol="SPY"),
        get_historical_prices=Mock(return_value=minute_bars()),
        _fetch_last_price=Mock(return_value=None),
    )
    collector = TrajectoryLogger(["test"], tmp_path / "trajectory.jsonl")
    features = collector._market_state(strategy, NOW)
    assert features["vwap_distance"] == (134 - 117) / 117
    assert collector._market_observations["vwap_distance"]["symbol"] == "SPY"
    assert "proxy_for" not in collector._market_observations["vwap_distance"]
    strategy.get_historical_prices.assert_called_once()


def test_vix_ratio_uses_the_older_provider_trade_time(tmp_path):
    stamps = {"VIX": NOW - timedelta(minutes=15), "VIX9D": NOW - timedelta(minutes=16)}
    strategy = SimpleNamespace(
        _underlying="SPY", get_historical_prices=Mock(return_value=minute_bars()),
        _fetch_quote=Mock(side_effect=lambda symbol: SimpleNamespace(
            price=20, raw_data={"trade_date": stamps[symbol].timestamp() * 1000})),
        _quote_midpoint=lambda quote: quote.price,
    )
    collector = TrajectoryLogger(["test"], tmp_path / "trajectory.jsonl")
    features = collector._market_state(strategy, NOW)
    rows = feature_health({"latest_snapshot": {"market": features, "market_as_of": NOW.isoformat(),
                           "market_observations": collector._market_observations}}, now=NOW)
    assert rows[0]["age"] == "15m 0s"
    assert rows[1]["age"] == "16m 0s"
    assert rows[0]["status"] == rows[1]["status"] == "Delayed"


def test_feature_health_does_not_use_receipt_time_for_unknown_provider_time():
    snapshot = {"market_as_of": NOW.isoformat(), "market": {"rsi_14": 60},
                "market_observations": {"rsi_14": {"as_of": None, "symbol": "XSP"}}}
    row = next(row for row in feature_health({"latest_snapshot": snapshot}, now=NOW)
               if row["name"] == "RSI")
    assert row["status"] == "Timestamp unknown"
    assert row["age"] == "Unknown"
