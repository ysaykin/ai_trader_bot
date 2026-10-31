"""Tests for dynamic leg-count fee accounting, exit fee calculations, and win-rate accounting in trade_logger."""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from trade_logger import (
    compute_strategy_metrics,
    load_all_trades,
    record_trade_adjustment,
    record_trade_entry,
    record_trade_exit,
)
from strategies.iron_condor_math import option_fee_per_contract


class TestTradeLoggerFeeAccounting(unittest.TestCase):
    def setUp(self) -> None:
        self.tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmpdir.cleanup)
        self.log_path = Path(self.tmpdir.name) / "trades.jsonl"

    def test_record_trade_entry_scales_fees_by_strikes_length(self) -> None:
        # 2-leg vertical spread
        strikes_2 = {"put_short": 5800.0, "put_long": 5795.0}
        t1 = record_trade_entry(
            strategy_name="STRAT_V2",
            symbol="SPX",
            expiry="2026-11-01",
            strikes=strikes_2,
            quantity=1,
            credit=1.50,
            max_risk=500.0,
            log_path=self.log_path,
        )
        # 4-leg iron condor
        strikes_4 = {"put_short": 5800.0, "put_long": 5795.0, "call_short": 5900.0, "call_long": 5905.0}
        t2 = record_trade_entry(
            strategy_name="STRAT_IC",
            symbol="SPX",
            expiry="2026-11-01",
            strikes=strikes_4,
            quantity=1,
            credit=2.50,
            max_risk=500.0,
            log_path=self.log_path,
        )

        with open(self.log_path, "r", encoding="utf-8") as f:
            lines = [json.loads(line) for line in f]

        self.assertEqual(len(lines), 2)
        # 2 legs * 1 * 0.40 = 0.80
        self.assertEqual(lines[0]["entry_fees"], 0.80)
        # 4 legs * 1 * 0.40 = 1.60
        self.assertEqual(lines[1]["entry_fees"], 1.60)

    def test_record_trade_exit_calculates_fees_without_hardcoding_8_legs(self) -> None:
        strikes_2 = {"put_short": 5800.0, "put_long": 5795.0}
        t_id = record_trade_entry(
            strategy_name="STRAT_V2",
            symbol="SPX",
            expiry="2026-11-01",
            strikes=strikes_2,
            quantity=2,
            credit=1.50,
            max_risk=1000.0,
            log_path=self.log_path,
        )

        # Record exit without passing explicit fees
        record_trade_exit(
            trade_id=t_id,
            strategy_name="STRAT_V2",
            exit_reason="PROFIT_TARGET",
            pnl=100.0,
            log_path=self.log_path,
        )

        with open(self.log_path, "r", encoding="utf-8") as f:
            records = [json.loads(line) for line in f]

        exit_rec = next(r for r in records if r["event"] == "EXIT")
        # For 2 legs * 2 qty * $0.40 = $1.60 (not 8 * 0.40 = 3.20)
        self.assertEqual(exit_rec["fees"], 1.60)

    def test_load_all_trades_and_metrics_with_dynamic_fees(self) -> None:
        strikes_2 = {"put_short": 5800.0, "put_long": 5795.0}
        t_id = record_trade_entry(
            strategy_name="STRAT_V2",
            symbol="SPX",
            expiry="2026-11-01",
            strikes=strikes_2,
            quantity=1,
            credit=1.00,  # $100 opening cash
            max_risk=500.0,
            log_path=self.log_path,
        )
        # Close at a small gross gain of $1.00 (gross_pnl = $1.00)
        # Total fees: entry $0.80 + exit $0.80 = $1.60
        # Net P&L = $1.00 - $1.60 = -$0.60 (net loss despite positive gross)
        record_trade_exit(
            trade_id=t_id,
            strategy_name="STRAT_V2",
            exit_reason="PROFIT_TARGET",
            pnl=1.00,
            gross_pnl=1.00,
            fees=0.80,
            log_path=self.log_path,
        )

        trades = load_all_trades(self.log_path)
        self.assertEqual(len(trades), 1)
        trade = trades[0]
        self.assertEqual(trade["fees"], 1.60)
        self.assertEqual(trade["net_pnl"], -0.60)
        self.assertEqual(trade["status"], "CLOSED")

        # Win rate must account this negative net P&L trade as a LOSS (win rate 0%)
        metrics = compute_strategy_metrics(trades, "STRAT_V2")
        self.assertEqual(metrics["total_trades"], 1)
        self.assertEqual(metrics["wins"], 0)
        self.assertEqual(metrics["losses"], 1)
        self.assertIn("0.0%", metrics["win_rate"])

    def test_fee_env_var_resilience(self) -> None:
        for invalid_val in ("", " ", "-0.40", "abc"):
            with patch.dict(os.environ, {"OPTION_FEE_PER_CONTRACT": invalid_val}):
                # Should not raise ValueError
                fee = option_fee_per_contract()
                self.assertEqual(fee, 0.40)


if __name__ == "__main__":
    unittest.main()
