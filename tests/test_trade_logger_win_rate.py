"""Tests for dynamic leg-count fee accounting, exit fee calculations, and win-rate accounting in trade_logger."""

from __future__ import annotations

import json
import os
import tempfile
import unittest
import pytest
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
        from trade_history_fixtures import evidence_bundle, SCOPE, AS_OF
        bundle=evidence_bundle(self.log_path.parent / "retained")
        events=bundle["events"]
        for event in events:
            event["strategy"]="STRAT_V2"
        events[0].update(opening_credit_cash=100.0,entry_fees=0.80,credit=1.0)
        events[1].update(gross_pnl=1.0,fees=0.80,net_pnl=0.20)
        bundle["journal"].write_text("".join(json.dumps(e)+"\n" for e in events))
        trades=load_all_trades(bundle["journal"],ledger_path=bundle["ledger"],scope=SCOPE,as_of_date=AS_OF)
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


def test_explicit_unknown_amounts_remain_unknown(tmp_path):
    import json
    from trade_history_fixtures import SCOPE
    path=tmp_path/'unknown.jsonl'
    record_trade_entry(strategy_name='retained',symbol='XSP',expiry='2026-11-05',
                       strikes={'long_put':690,'short_put':695},quantity=1,credit=1.,max_risk=500.,
                       entry_fees=None,trade_id='unknown',log_path=path,scope=SCOPE)
    record_trade_exit(trade_id='unknown',strategy_name='retained',exit_reason='CASH_SETTLED',pnl=10.,
                      fees=None,net_pnl=None,log_path=path,scope=SCOPE)
    entry,exit_event=[json.loads(line) for line in path.read_text().splitlines()]
    assert entry['entry_fees'] is None
    assert exit_event['fees'] is None and exit_event['net_pnl'] is None
    assert exit_event['fees_complete'] is False


@pytest.mark.parametrize('field', ['fees', 'entry_fees', 'net_pnl'])
def test_complete_provenance_with_unknown_amount_excluded_from_metrics(tmp_path: Path, field: str) -> None:
    from trade_history_fixtures import evidence_bundle, SCOPE, AS_OF

    bundle = evidence_bundle(tmp_path)
    event = bundle['events'][0 if field == 'entry_fees' else 1]
    event[field] = None
    for record in bundle['events']:
        record['fees_complete'] = True
    bundle['journal'].write_text(''.join(json.dumps(record) + '\n' for record in bundle['events']))
    [trade] = load_all_trades(bundle['journal'], ledger_path=bundle['ledger'], scope=SCOPE, as_of_date=AS_OF)
    assert trade['reconciliation_status'] == 'VERIFIED'
    assert trade['is_eligible'] is False
    assert trade['fee_source'] == 'retained_actual'
    assert 'JOURNAL_AMOUNTS_INCOMPLETE' in trade['reason_codes']
    assert trade['net_pnl'] is None
    if field != 'net_pnl':
        assert trade['fees'] is None
    metrics = compute_strategy_metrics([trade], 'IronCondor_V31')
    assert metrics['total_trades'] == 0 and metrics['raw_pnl'] == 0.
