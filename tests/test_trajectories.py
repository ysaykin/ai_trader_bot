"""Offline checks for causal features, grid collection, and EOD labels."""

from __future__ import annotations

import json
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from ml.evaluator import (
    classify_market_regime,
    compute_mfe_mae_efficiency,
    compute_sortino_and_tail_metrics,
    evaluate_counterfactual_regret,
)
from ml.features import GREEKS, MARKET_TZ, grid_features, market_features
from trade_logger import TrajectoryLogger, relabel_session


def session_time(hour: int, minute: int = 0) -> datetime:
    return datetime(2026, 10, 26, hour, minute, tzinfo=MARKET_TZ)


class FeatureTests(unittest.TestCase):
    def test_completed_bars_only_and_opening_range_warmup(self) -> None:
        opening = session_time(9, 30)
        bars = [
            {"timestamp": opening + timedelta(minutes=i), "high": 101 + i,
             "low": 99 + i, "close": 100 + i, "volume": 10}
            for i in range(31)
        ]
        baseline = market_features(bars[:30], session_time(10), 20, 22)
        bars[-1].update(high=1e6, close=1e6)
        self.assertEqual(baseline, market_features(bars, session_time(10), 20, 22))
        self.assertAlmostEqual(baseline["vix_term_ratio"], 1.1)
        self.assertEqual(baseline["rsi_14"], 100)
        self.assertAlmostEqual(baseline["atr_pct"], 2 / 129)
        self.assertAlmostEqual(baseline["orb_30m_range"], 31 / 129)
        self.assertAlmostEqual(baseline["price_velocity_15m"], 129 / 114 - 1)
        self.assertAlmostEqual(baseline["vwap_distance"], 129 / 114.5 - 1)
        self.assertIsNone(market_features(bars, session_time(9, 59))["orb_30m_range"])
        self.assertEqual(baseline["minutes_to_close"], 360)

    def test_missing_or_nonfinite_data_is_not_fabricated(self) -> None:
        result = market_features([], session_time(10), float("nan"), 20)
        self.assertIsNone(result["vix"])
        self.assertIsNone(result["vix_term_ratio"])
        self.assertIsNone(result["rsi_14"])
        yesterday = {"timestamp": session_time(9, 30) - timedelta(days=1),
                     "high": 100, "low": 100, "close": 100, "volume": 10}
        self.assertEqual(result, market_features([yesterday], session_time(10)))
        with self.assertRaises(ValueError):
            market_features([], datetime(2026, 10, 26, 10))

    def test_grid_reports_coverage_and_does_not_sum_missing_greeks(self) -> None:
        states = {"a": {"unrealized_pnl": -10, **dict.fromkeys(GREEKS, 1)},
                  "b": {"unrealized_pnl": 10, **dict.fromkeys(GREEKS, 3)}}
        result = grid_features(states, {"delta_net": 3})
        self.assertEqual(result["pnl_dispersion"], 10)
        self.assertEqual(result["delta_drift"], 1)
        states["b"] = None
        result = grid_features(states)
        self.assertEqual(result["observed_count"], 1)
        self.assertIsNone(result["delta_net"])


class TrajectoryTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.path = Path(temporary.name) / "trajectories.jsonl"
        self.environment = patch.dict("os.environ", {}, clear=True)
        self.environment.start()
        self.addCleanup(self.environment.stop)

    def strategy(self, name: str, *, quantity: int = 1) -> SimpleNamespace:
        legs = [
            (SimpleNamespace(symbol="XSP", right=right, strike=strike, expiration="2026-10-26"), signed * quantity)
            for right, strike, signed in (("put", 95, 1), ("put", 100, -1), ("call", 110, -1), ("call", 115, 1))
        ]
        source = SimpleNamespace(
            name=name, parameters={"quantity": quantity, "wing_width": 5, "max_position_risk": 5000},
            _underlying="XSP", _owned_legs=legs, _entry_orders=[object() for _ in range(4)],
            _entry_credit=2.0 * quantity, _position_open=True, _exit_requested=False,
            get_datetime=Mock(return_value=session_time(15, 45)),
            get_historical_prices=Mock(return_value=None),
            get_greeks=Mock(return_value=dict.fromkeys(GREEKS, 0.1)),
        )
        self.mark(source, 0)
        return source

    def mark(self, strategy: SimpleNamespace, pnl: float) -> None:
        def price(asset: object) -> float:
            if isinstance(asset, str):
                return {"VIX": 20, "VIX9D": 22, "XSP": 105}[asset]
            return 1.25 - pnl / (200 * strategy.parameters["quantity"]) if asset.strike in (100, 110) else 0.25
        strategy._fetch_last_price = Mock(side_effect=price)

    def rows(self, event: str | None = None) -> list[dict]:
        if not self.path.exists():
            return []
        rows = [json.loads(line) for line in self.path.read_text().splitlines()]
        return [row for row in rows if event is None or row["event"] == event]

    def test_concurrent_grid_writes_one_row_and_shares_market_reads(self) -> None:
        strategies = [self.strategy(f"IC_V{i:02}") for i in range(1, 21)]
        collector = TrajectoryLogger([strategy.name for strategy in strategies], self.path)
        with ThreadPoolExecutor(max_workers=4) as pool:
            list(pool.map(lambda strategy: collector.record(strategy, "HOLD"), strategies))
        rows = self.rows()
        self.assertEqual(len(rows), 1)
        self.assertTrue(rows[0]["complete"])
        self.assertEqual(rows[0]["grid"]["observed_count"], 20)
        self.assertEqual(sum(strategy.get_historical_prices.call_count for strategy in strategies), 1)
        self.assertEqual(sum(strategy.get_greeks.call_count for strategy in strategies), 4)
        self.assertEqual(rows[0]["market"]["vix_term_ratio"], 1.1)
        for strategy in strategies:
            collector.record(strategy, "HOLD")
        self.assertEqual(len(self.rows()), 1)
        # A restart must not append the same completed grid again.
        TrajectoryLogger([strategy.name for strategy in strategies], self.path).record(strategies[0], "HOLD")
        self.assertEqual(len(self.rows()), 1)

    def test_partial_grid_never_uses_stale_strategy_state(self) -> None:
        a, b = self.strategy("a"), self.strategy("b")
        collector = TrajectoryLogger(["a", "b"], self.path)
        collector.record(a, "HOLD")
        a.get_datetime.return_value = session_time(15, 50)
        collector.record(a, "HOLD")
        partial = self.rows()[0]
        self.assertFalse(partial["complete"])
        self.assertIsNone(partial["strategies"]["b"])
        self.assertFalse(partial["strategies"]["a"]["can_add"])
        b.get_datetime.return_value = session_time(15, 50)
        collector.record(b, "HOLD")
        self.assertEqual(len(self.rows()), 2)

    def test_missing_quote_is_null_and_off_hours_are_skipped(self) -> None:
        strategy = self.strategy("a")
        collector = TrajectoryLogger(["a"], self.path)
        strategy.get_datetime.return_value = session_time(9)
        collector.record(strategy, "HOLD")
        self.assertFalse(self.path.exists())
        strategy.get_datetime.return_value = session_time(15, 55)
        strategy._fetch_last_price.return_value = None
        strategy._fetch_last_price.side_effect = None
        collector.record(strategy, "HOLD")
        state = self.rows()[0]["strategies"]["a"]
        self.assertIsNone(state["pnl"])
        self.assertIsNone(state["unrealized_pnl"])

    def test_observation_timestamp_is_after_provider_reads(self) -> None:
        strategy = self.strategy("a")
        strategy.get_datetime.side_effect = [session_time(15, 55), session_time(15, 56)]
        collector = TrajectoryLogger(["a"], self.path)
        collector.record(strategy, "HOLD")
        row = self.rows()[0]
        self.assertGreater(row["timestamp"], row["market_as_of"])
        self.assertEqual(row["timestamp"], row["strategies"]["a"]["observed_at"])

    def test_shadow_basket_survives_exit_and_labels_are_separate_idempotent(self) -> None:
        strategy = self.strategy("a")
        collector = TrajectoryLogger(["a"], self.path)
        for minute, pnl in ((45, 0), (50, 25), (55, 50)):
            strategy.get_datetime.return_value = session_time(15, minute)
            self.mark(strategy, pnl)
            collector.record(strategy, "HOLD")
        raw = self.path.read_text()
        self.assertEqual(relabel_session("2026-10-26", self.path), 0)
        strategy._position_open = False
        strategy._owned_legs = []
        strategy._entry_orders = []
        strategy.get_datetime.return_value = session_time(16)
        self.mark(strategy, 100)
        collector.record(strategy, "HOLD", session_end=True)
        self.assertTrue(self.path.read_text().startswith(raw))
        terminal = self.rows("SESSION_END")[0]["strategies"]["a"]
        self.assertEqual(terminal["pnl"], 100)
        self.assertEqual(terminal["unrealized_pnl"], 0)
        labels = self.rows("HINDSIGHT_LABEL")
        self.assertEqual(len(labels), 3)
        self.assertEqual(labels[0]["strategies"]["a"]["action"], "ADD_POSITION")
        self.assertEqual(labels[0]["strategies"]["a"]["future_pnl"], 100)
        self.assertEqual(relabel_session("2026-10-26", self.path), 0)
        self.assertEqual(len(self.rows("HINDSIGHT_LABEL")), 3)
        self.assertTrue(all("scores" not in row for row in self.rows("SNAPSHOT")))

    def test_restart_retries_relabeling_after_terminal_record_was_written(self) -> None:
        strategy = self.strategy("a")
        collector = TrajectoryLogger(["a"], self.path)
        strategy.get_datetime.return_value = session_time(15, 55)
        collector.record(strategy, "HOLD")
        strategy.get_datetime.return_value = session_time(16)
        with patch("trade_logger.relabel_session", side_effect=OSError("interrupted")):
            with self.assertRaises(OSError):
                collector.record(strategy, "HOLD", session_end=True)
        self.assertEqual(len(self.rows("SESSION_END")), 1)
        self.assertEqual(len(self.rows("HINDSIGHT_LABEL")), 0)
        TrajectoryLogger(["a"], self.path).record(strategy, "HOLD", session_end=True)
        self.assertEqual(len(self.rows("HINDSIGHT_LABEL")), 1)

    def test_missing_terminal_quote_or_missing_bucket_censors_labels(self) -> None:
        for missing_quote in (True, False):
            with self.subTest(missing_quote=missing_quote):
                self.path = self.path.with_name(f"{missing_quote}.jsonl")
                strategy = self.strategy("a")
                collector = TrajectoryLogger(["a"], self.path)
                collector.record(strategy, "HOLD")
                strategy.get_datetime.return_value = session_time(16)
                if missing_quote:
                    strategy._fetch_last_price.side_effect = None
                    strategy._fetch_last_price.return_value = None
                collector.record(strategy, "HOLD", session_end=True)
                self.assertEqual(self.rows("HINDSIGHT_LABEL")[0]["strategies"], {})

    def test_risk_limits_remove_add_from_counterfactual_actions(self) -> None:
        strategies = [self.strategy(name, quantity=3) for name in ("a", "b", "c")]
        collector = TrajectoryLogger([strategy.name for strategy in strategies], self.path)
        for hour, minute, pnl in ((15, 55, 0), (16, 0, 100)):
            for strategy in strategies:
                strategy.get_datetime.return_value = session_time(hour, minute)
                self.mark(strategy, pnl)
                collector.record(strategy, "HOLD", session_end=hour == 16)
        for label in self.rows("HINDSIGHT_LABEL")[0]["strategies"].values():
            self.assertNotIn("ADD_POSITION", label["scores"])
            self.assertIn("TRIM_POSITION", label["scores"])

    def test_risk_adjusted_labels_can_choose_trim_or_flatten(self) -> None:
        for pnls, expected in (((0, -350, 100, 100), "TRIM_POSITION"),
                               ((0, -50, -100, -150), "FLATTEN_NOW")):
            with self.subTest(expected=expected):
                self.path = self.path.with_name(f"{expected}.jsonl")
                strategy = self.strategy("a", quantity=2)
                collector = TrajectoryLogger(["a"], self.path)
                for now, pnl in zip((session_time(15, 45), session_time(15, 50),
                                     session_time(15, 55), session_time(16)), pnls):
                    strategy.get_datetime.return_value = now
                    self.mark(strategy, pnl)
                    collector.record(strategy, "HOLD", session_end=now.hour == 16)
                label = self.rows("HINDSIGHT_LABEL")[0]["strategies"]["a"]
                self.assertEqual(label["action"], expected)

    def test_post_market_hook_after_1605_anchors_closing_bucket_and_preserves_labels(self) -> None:
        a, b = self.strategy("a"), self.strategy("b")
        b._owned_legs = [
            (SimpleNamespace(symbol="XSP", right=right, strike=strike, expiration="2026-10-26"), signed)
            for right, strike, signed in (("put", 94, 1), ("put", 99, -1), ("call", 111, -1), ("call", 116, 1))
        ]
        b._fetch_last_price = Mock(side_effect=lambda asset: 1.25 if getattr(asset, "strike", 0) in (99, 111) else 0.25)
        collector = TrajectoryLogger(["a", "b"], self.path)
        for strategy in (a, b):
            strategy.get_datetime.return_value = session_time(15, 55)
            collector.record(strategy, "HOLD")
        a.get_datetime.return_value = session_time(16, 4)
        b.get_datetime.return_value = session_time(16, 15)
        self.mark(a, 50)
        b._fetch_last_price = Mock(
            side_effect=lambda asset: 1.25 - 75 / 200 if getattr(asset, "strike", 0) in (99, 111) else 0.25
        )
        collector.record(a, "HOLD", session_end=True)
        collector.record(b, "HOLD", session_end=True)
        terminals = self.rows("SESSION_END")
        self.assertEqual(len(terminals), 1)
        self.assertTrue(terminals[0]["complete"])
        labels = self.rows("HINDSIGHT_LABEL")
        self.assertEqual(len(labels), 1)
        self.assertEqual(labels[0]["best_strategy"], "b")
        self.assertEqual(labels[0]["strategies"]["a"]["future_pnl"], 50)
        self.assertEqual(labels[0]["strategies"]["b"]["future_pnl"], 75)
        self.assertEqual(labels[0]["horizon_timestamp"], terminals[0]["timestamp"])

    def test_whitespace_or_invalid_single_trade_pct_env_falls_back_safely(self) -> None:
        for raw_pct in ("   ", "nan", "-0.2"):
            with self.subTest(raw_pct=raw_pct), patch.dict("os.environ", {"IC_MAX_SINGLE_TRADE_PCT": raw_pct}):
                self.path = self.path.with_name(f"env_{abs(hash(raw_pct))}.jsonl")
                strategy = self.strategy("a")
                del strategy.name
                strategy.parameters["strategy_name"] = "a"
                collector = TrajectoryLogger(["a"], self.path)
                strategy.get_datetime.return_value = session_time(15, 55)
                collector.record(strategy, "HOLD")
                self.assertEqual(self.rows()[0]["strategies"]["a"]["single_trade_pct"], 0.30)


class EvaluatorTests(unittest.TestCase):
    def test_classify_market_regime_covers_all_four_regimes(self) -> None:
        self.assertEqual(classify_market_regime(None), "Unknown")
        self.assertEqual(classify_market_regime({"vix": 24.5, "vix_term_ratio": 1.08}), "High-Vol Expansion")
        self.assertEqual(classify_market_regime({"vix": 17.5, "vwap_distance": 0.006}), "Directional Trend")
        self.assertEqual(classify_market_regime({"vix": 13.8, "vwap_distance": 0.0005}), "Low-Vol Pin")
        self.assertEqual(classify_market_regime({"vix": 18.0, "vwap_distance": 0.001}), "Mean-Reverting Chop")

    def test_sortino_and_mfe_mae_efficiency_calculations(self) -> None:
        metrics = compute_sortino_and_tail_metrics([50.0, 75.0, -25.0, 100.0])
        self.assertEqual(metrics["count"], 4)
        self.assertEqual(metrics["win_rate"], 0.75)
        self.assertAlmostEqual(metrics["profit_factor"], 225.0 / 25.0)
        self.assertIsNotNone(metrics["sortino_ratio"])

        trades = [
            {"status": "CLOSED", "pnl": 80.0, "mfe_pnl": 100.0, "mae_pnl": -20.0, "max_risk": 200.0},
            {"status": "CLOSED", "pnl": -50.0, "mfe_pnl": 20.0, "mae_pnl": -120.0, "max_risk": 200.0},
        ]
        eff = compute_mfe_mae_efficiency(trades)
        self.assertEqual(eff["evaluated_trades"], 2)
        self.assertAlmostEqual(eff["mfe_capture_ratio"], (0.8 + (-1.0)) / 2)
        self.assertAlmostEqual(eff["mae_avoidance_ratio"], (1.0 + 0.75) / 2)

    def test_evaluate_counterfactual_regret_aggregates_labels_and_regimes(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            log_path = Path(tmpdir) / "trajectories.jsonl"
            strat = SimpleNamespace(
                name="a",
                parameters={"quantity": 1, "wing_width": 5, "max_position_risk": 5000},
                _underlying="XSP",
                _owned_legs=[
                    (SimpleNamespace(symbol="XSP", right=right, strike=strike, expiration="2026-10-26"), signed)
                    for right, strike, signed in (("put", 95, 1), ("put", 100, -1), ("call", 110, -1), ("call", 115, 1))
                ],
                _entry_orders=[object() for _ in range(4)],
                _entry_credit=2.0,
                _position_open=True,
                _exit_requested=False,
                get_datetime=Mock(return_value=session_time(15, 55)),
                get_historical_prices=Mock(return_value=None),
                get_greeks=Mock(return_value=dict.fromkeys(GREEKS, 0.1)),
            )
            strat._fetch_last_price = Mock(
                side_effect=lambda asset: 14.0 if asset == "VIX" else (13.5 if asset == "VIX9D" else (105.0 if asset == "XSP" else (1.25 if asset.strike in (100, 110) else 0.25)))
            )
            collector = TrajectoryLogger(["a"], log_path)
            collector.record(strat, "HOLD")

            strat.get_datetime.return_value = session_time(16, 0)
            strat._fetch_last_price = Mock(
                side_effect=lambda asset: 14.0 if asset == "VIX" else (13.5 if asset == "VIX9D" else (105.0 if asset == "XSP" else (0.75 if asset.strike in (100, 110) else 0.25)))
            )
            collector.record(strat, "HOLD", session_end=True)

            report = evaluate_counterfactual_regret(log_path, session="2026-10-26")
            self.assertEqual(report["labeled_ticks"], 1)
            self.assertEqual(report["evaluated_decisions"], 1)
            self.assertGreater(report["mean_action_regret"], 0.0)
            self.assertEqual(report["optimal_action_counts"]["ADD_POSITION"], 1)
            self.assertIn("Low-Vol Pin", report["by_regime"])
            self.assertIn("a", report["by_strategy"])


if __name__ == "__main__":
    unittest.main()
