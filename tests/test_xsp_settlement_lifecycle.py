"""Comprehensive accounting and lifecycle tests for pure XSP cash settlement, durable ledger lifecycle, outbox, and fee provenance."""

from __future__ import annotations

import json
import sqlite3
import tempfile
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
import unittest

from trade_history_fixtures import retain_settlement
from strategies.position_ledger import LedgerLeg, PositionLedger, asset_key
from strategies.xsp_settlement import (
    BrokerCashEvidence,
    FeeEvidence,
    LifecycleResult,
    SettlementLeg,
    SettlementValue,
    calculate_settlement_cash,
    create_broker_cash_evidence,
    create_settlement_value,
    parse_asset_key_to_leg,
    reconcile_lifecycle,
)
from trade_logger import compute_strategy_metrics, load_all_trades, record_trade_entry, record_trade_exit
from tools.reconcile_trade_history import get_verified_ledger_trades, partition_trade_journal


class TestXSPSettlementMath(unittest.TestCase):
    """Test pure intrinsic math, signs, multiplier, and spread boundaries."""

    def setUp(self) -> None:
        self.exp = date(2026, 10, 26)
        self.legs = (
            SettlementLeg(self.exp, "put", Decimal("95.0"), 1),    # Long 95 put
            SettlementLeg(self.exp, "put", Decimal("100.0"), -1),  # Short 100 put
            SettlementLeg(self.exp, "call", Decimal("110.0"), -1), # Short 110 call
            SettlementLeg(self.exp, "call", Decimal("115.0"), 1),  # Long 115 call
        )

    def test_all_otm_expires_worthless(self) -> None:
        settlement = create_settlement_value(self.exp, "105.0")
        cash_cents, worthless = calculate_settlement_cash(self.legs, settlement)
        self.assertEqual(cash_cents, 0)
        self.assertTrue(worthless)

    def test_itm_short_put_wing(self) -> None:
        # Settlement at 98.0: short 100 put is ITM by 2.0 ($200 cash debit)
        settlement = create_settlement_value(self.exp, "98.0")
        cash_cents, worthless = calculate_settlement_cash(self.legs, settlement)
        self.assertEqual(cash_cents, -20000)  # -$200.00
        self.assertFalse(worthless)

    def test_itm_long_put_wing_full_width(self) -> None:
        # Settlement at 90.0: short 100 put (10.0 ITM -> -$1000), long 95 put (5.0 ITM -> +$500)
        # Net: -$500.00 (-50000 cents)
        settlement = create_settlement_value(self.exp, "90.0")
        cash_cents, worthless = calculate_settlement_cash(self.legs, settlement)
        self.assertEqual(cash_cents, -50000)
        self.assertFalse(worthless)

    def test_itm_short_call_wing(self) -> None:
        # Settlement at 112.0: short 110 call is ITM by 2.0 ($200 cash debit)
        settlement = create_settlement_value(self.exp, "112.0")
        cash_cents, worthless = calculate_settlement_cash(self.legs, settlement)
        self.assertEqual(cash_cents, -20000)
        self.assertFalse(worthless)

    def test_itm_long_call_wing_full_width(self) -> None:
        # Settlement at 120.0: short 110 call (10.0 ITM -> -$1000), long 115 call (5.0 ITM -> +$500)
        # Net: -$500.00 (-50000 cents)
        settlement = create_settlement_value(self.exp, "120.0")
        cash_cents, worthless = calculate_settlement_cash(self.legs, settlement)
        self.assertEqual(cash_cents, -50000)
        self.assertFalse(worthless)

    def test_spread_boundary_at_exact_strike(self) -> None:
        # Settlement exactly at 100.0: 100 put intrinsic is 0, all calls 0 -> worthless
        settlement = create_settlement_value(self.exp, "100.0")
        cash_cents, worthless = calculate_settlement_cash(self.legs, settlement)
        self.assertEqual(cash_cents, 0)
        self.assertTrue(worthless)

    def test_negative_settlement_value_rejected(self) -> None:
        with self.assertRaises(ValueError):
            calculate_settlement_cash(
                self.legs,
                SettlementValue(self.exp, Decimal("-5.0"), "cboe.com", datetime.now(timezone.utc), "h", "o"),
            )

    def test_mismatched_expiration_rejected(self) -> None:
        other_exp = date(2026, 10, 27)
        with self.assertRaises(ValueError):
            calculate_settlement_cash(self.legs, create_settlement_value(other_exp, "105.0"))


class TestXSPFeeAndLifecycleReconciliation(unittest.TestCase):
    """Test fee completeness, provenance, known charges exposure, and fail-closed reconciliation."""

    def setUp(self) -> None:
        self.exp = date(2026, 10, 26)
        self.legs = (
            SettlementLeg(self.exp, "put", Decimal("95.0"), 1),
            SettlementLeg(self.exp, "put", Decimal("100.0"), -1),
            SettlementLeg(self.exp, "call", Decimal("110.0"), -1),
            SettlementLeg(self.exp, "call", Decimal("115.0"), 1),
        )

    def test_complete_fees_calculates_exact_net(self) -> None:
        settlement = create_settlement_value(self.exp, "105.0")
        broker_cash = create_broker_cash_evidence("acc-1", "act-1", self.exp, 0, True)
        fees = (
            FeeEvidence("open:order-1", 160, True, "opening", "fill-1"),
            FeeEvidence("settle:xsp-1", 0, True, "settlement", "cboe-1"),
        )
        res = reconcile_lifecycle(
            opening_cash_cents=20000,
            closing_cash_cents=0,
            remaining_legs=self.legs,
            settlement=settlement,
            broker_cash=broker_cash,
            fees=fees,
            fee_coverage_complete=True,
            expiration_passed=True,
        )
        self.assertEqual(res.outcome, "EXPIRED_WORTHLESS")
        self.assertTrue(res.reconciliation_complete)
        self.assertTrue(res.fees_complete)
        self.assertEqual(res.settlement_cash_cents, 0)
        self.assertEqual(res.gross_cents, 20000)
        self.assertEqual(res.fees_cents, 160)
        self.assertEqual(res.net_cents, 19840)
        self.assertEqual(res.known_fees_cents, 160)
        self.assertEqual(len(res.fee_evidence), 2)

    def test_incomplete_fees_exposes_known_charges_and_keeps_net_pnl_unavailable(self) -> None:
        settlement = create_settlement_value(self.exp, "105.0")
        broker_cash = create_broker_cash_evidence("acc-1", "act-1", self.exp, 0, True)
        # Confirmed 160 cents opening fee, but settlement fee is pending (amount_cents=None, complete=False)
        fees = (
            FeeEvidence("open:order-1", 160, True, "opening", "fill-1"),
            FeeEvidence("settle:xsp-1", None, False, "settlement", "pending"),
        )
        res = reconcile_lifecycle(
            opening_cash_cents=20000,
            closing_cash_cents=0,
            remaining_legs=self.legs,
            settlement=settlement,
            broker_cash=broker_cash,
            fees=fees,
            fee_coverage_complete=False,
            expiration_passed=True,
        )
        self.assertEqual(res.outcome, "EXPIRED_WORTHLESS")
        self.assertTrue(res.reconciliation_complete)
        self.assertFalse(res.fees_complete)
        self.assertEqual(res.gross_cents, 20000)
        # Final fee total and net P&L must be None
        self.assertIsNone(res.fees_cents)
        self.assertIsNone(res.net_cents)
        # Confirmed opening charges must be exposed and provenance preserved
        self.assertEqual(res.known_fees_cents, 160)
        self.assertEqual(len(res.fee_evidence), 2)
        self.assertEqual(res.fee_evidence[0].provenance, "fill-1")

    def test_broker_cash_mismatch_fails_closed(self) -> None:
        # Settlement says 98.0 (expected cash -20000 cents), but broker evidence reports -15000 cents
        settlement = create_settlement_value(self.exp, "98.0")
        broker_cash = create_broker_cash_evidence("acc-1", "act-1", self.exp, -15000, True)
        res = reconcile_lifecycle(
            opening_cash_cents=20000,
            closing_cash_cents=0,
            remaining_legs=self.legs,
            settlement=settlement,
            broker_cash=broker_cash,
            fees=(),
            fee_coverage_complete=True,
            expiration_passed=True,
        )
        self.assertFalse(res.reconciliation_complete)
        self.assertEqual(res.outcome, "EXPIRED_AWAITING_SETTLEMENT")
        self.assertIsNone(res.gross_cents)
        self.assertIsNone(res.net_cents)

    def test_unconfirmed_broker_disposition_fails_closed(self) -> None:
        settlement = create_settlement_value(self.exp, "105.0")
        broker_cash = create_broker_cash_evidence("acc-1", "act-1", self.exp, 0, disposition_confirmed=False)
        res = reconcile_lifecycle(
            opening_cash_cents=20000,
            closing_cash_cents=0,
            remaining_legs=self.legs,
            settlement=settlement,
            broker_cash=broker_cash,
            fees=(),
            fee_coverage_complete=True,
            expiration_passed=True,
        )
        self.assertFalse(res.reconciliation_complete)
        self.assertEqual(res.outcome, "EXPIRED_AWAITING_SETTLEMENT")

    def test_closed_by_trade_before_expiration(self) -> None:
        res = reconcile_lifecycle(
            opening_cash_cents=20000,
            closing_cash_cents=-5000,
            remaining_legs=(),
            settlement=None,
            broker_cash=None,
            fees=(FeeEvidence("f-1", 320, True),),
            fee_coverage_complete=True,
            expiration_passed=False,
        )
        self.assertEqual(res.outcome, "CLOSED_BY_TRADE")
        self.assertTrue(res.reconciliation_complete)
        self.assertEqual(res.gross_cents, 15000)
        self.assertEqual(res.fees_cents, 320)
        self.assertEqual(res.net_cents, 14680)


class TestPositionLedgerDurableLifecycle(unittest.TestCase):
    """Test durable SQLite schema, inventory retention, outbox queue, and CLI reconciliation."""

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.ledger_path = Path(self.temp_dir.name) / "position_ledger.sqlite3"
        self.journal_path = Path(self.temp_dir.name) / "trades.jsonl"
        self.ledger = PositionLedger(self.ledger_path)
        self.addCleanup(self.ledger.close)

        self.exp = "2026-10-26"
        self.assets = [
            SimpleNamespace(symbol="XSP", expiration=self.exp, strike=s, right=r)
            for s, r in ((95, "put"), (100, "put"), (110, "call"), (115, "call"))
        ]
        self.legs = tuple(
            LedgerLeg(f"leg-{i}", asset_key(a), qty)
            for i, (a, qty) in enumerate(zip(self.assets, (1, -1, -1, 1)), start=1)
        )

    def test_ledger_schema_includes_settlement_tables(self) -> None:
        with self.ledger._lock:
            tables = {
                r[0]
                for r in self.ledger._connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                ).fetchall()
            }
        expected = {
            "packages",
            "legs",
            "positions",
            "settlement_values",
            "broker_cash_evidence",
            "fee_evidence",
            "settlement_reconciliations",
            "outbox",
        }
        self.assertTrue(expected.issubset(tables))

    def test_past_expiration_without_evidence_retains_inventory_and_reservations(self) -> None:
        self.ledger.record_submission("pkg-1", "IC_STRAT", "open", self.legs, trade_id="T-001")
        for leg in self.legs:
            self.ledger.record_fill(leg.order_id, leg.requested_signed_quantity, 1.0)

        # Expiration passed without evidence
        retired = self.ledger.expire_strategy_positions("IC_STRAT", date(2026, 10, 27))
        self.assertEqual(retired, 0)

        # Inventory is retained
        owned = self.ledger.owned_quantities("IC_STRAT")
        self.assertTrue(owned)
        self.assertEqual(len(owned), 4)

        # Open baskets still lists the package under awaiting_settlement
        baskets = self.ledger.open_baskets("IC_STRAT")
        self.assertEqual(len(baskets), 1)
        self.assertEqual(baskets[0].state, "awaiting_settlement")

        # Risk reservations are retained
        risk = self.ledger.portfolio_defined_risk("IC_STRAT")
        self.assertIsNotNone(risk)
        self.assertGreater(risk, 0.0)

        # Retains state across ledger close and reopen
        reopened = PositionLedger(self.ledger_path)
        try:
            self.assertTrue(reopened.owned_quantities("IC_STRAT"))
            self.assertEqual(len(reopened.open_baskets("IC_STRAT")), 1)
            self.assertEqual(reopened.open_baskets("IC_STRAT")[0].state, "awaiting_settlement")
        finally:
            reopened.close()

    def test_explicit_settlement_evidence_reconciles_retires_and_queues_outbox(self) -> None:
        self.ledger.record_submission("pkg-1", "IC_STRAT", "open", self.legs, trade_id="T-001")
        for leg, px in zip(self.legs, (0.25, 1.25, 1.25, 0.25)):
            self.ledger.record_fill(leg.order_id, leg.requested_signed_quantity, px)

        settlement = create_settlement_value(date(2026, 10, 26), "105.0")
        broker_cash = create_broker_cash_evidence(self.ledger.record_scope.account_id, "act-xsp-1", date(2026, 10, 26), 0, True)
        fee = FeeEvidence("fee:open", 160, True, "opening")

        retain_settlement(self.ledger, "pkg-1", settlement, broker_cash, fee=fee)
        retired = self.ledger.expire_strategy_positions(
            "IC_STRAT",
            date(2026, 10, 27),
            settlement=settlement,
            broker_cash=broker_cash,
            fees=(fee,),
            fee_coverage_complete=True,
        )
        self.assertEqual(retired, 1)

        # Inventory is now cleared
        self.assertEqual(self.ledger.owned_quantities("IC_STRAT"), {})
        self.assertEqual(self.ledger.open_baskets("IC_STRAT"), ())

        # Reconciled record stored
        reconciliation = self.ledger.get_settlement_reconciliation("pkg-1")
        self.assertIsNotNone(reconciliation)
        self.assertEqual(reconciliation["outcome"], "EXPIRED_WORTHLESS")
        self.assertEqual(reconciliation["fees_complete"], 1)

        # Outbox event was created
        with self.ledger._lock:
            outbox_rows = self.ledger._connection.execute(
                "SELECT * FROM outbox WHERE status = 'pending'"
            ).fetchall()
        self.assertEqual(len(outbox_rows), 1)
        payload = json.loads(outbox_rows[0]["payload"])
        self.assertEqual(payload["trade_id"], "T-001")
        self.assertEqual(payload["exit_reason"], "EXPIRED_WORTHLESS")
        self.assertEqual(payload["status"], "CLOSED")

        # Flush outbox to journal
        dispatched = self.ledger.flush_outbox(self.journal_path)
        self.assertEqual(dispatched, 1)
        self.assertTrue(self.journal_path.exists())

        event = json.loads(self.journal_path.read_text().splitlines()[0])
        self.assertEqual(event["event_id"], "pkg-1:SETTLEMENT")
        self.assertEqual(event["scope"], self.ledger.record_scope.to_dict())
        self.assertEqual(len(event["evidence_refs"]), 2)
        self.assertIn("known_fees", event)
        self.assertIn("fees_complete", event)
        # Simulate crash after append but before marking the outbox dispatched.
        self.ledger._connection.execute("UPDATE outbox SET status='pending'")
        self.ledger._connection.commit()
        self.assertEqual(self.ledger.flush_outbox(self.journal_path), 1)
        self.assertEqual(len(self.journal_path.read_text().splitlines()), 1)
        # Second flush is idempotent
        self.assertEqual(self.ledger.flush_outbox(self.journal_path), 0)

    def test_unretained_settlement_assertions_do_not_release_inventory(self) -> None:
        self.ledger.record_submission("missing", "IC_STRAT", "open", self.legs, trade_id="legacy")
        for leg in self.legs:
            self.ledger.record_fill(leg.order_id, leg.requested_signed_quantity, 1.0)
        settlement=create_settlement_value(date(2026,10,26), "105")
        cash=create_broker_cash_evidence(self.ledger.record_scope.account_id,"aggregate",date(2026,10,26),0,True)
        self.assertEqual(self.ledger.expire_strategy_positions("IC_STRAT",date(2026,10,27),settlement=settlement,broker_cash=cash),0)
        self.assertTrue(self.ledger.owned_quantities("IC_STRAT"))
        self.assertGreater(self.ledger.portfolio_defined_risk("IC_STRAT"),0)

    def test_settlement_with_prior_trims_does_not_double_count(self) -> None:
        # Open 2 contracts
        open_legs = tuple(
            LedgerLeg(f"open-leg-{i}", leg.asset_key, leg.requested_signed_quantity * 2)
            for i, leg in enumerate(self.legs, start=1)
        )
        self.ledger.record_submission("open-1", "IC_STRAT", "open", open_legs, trade_id="T-TRIM")
        for leg in open_legs:
            self.ledger.record_fill(leg.order_id, leg.requested_signed_quantity, 1.0)

        # Trim 1 contract before expiration
        trim_legs = tuple(
            LedgerLeg(f"trim-{i}", leg.asset_key, -int(leg.requested_signed_quantity / 2))
            for i, leg in enumerate(open_legs, start=1)
        )
        self.ledger.record_submission("trim-1", "IC_STRAT", "trim", trim_legs, trade_id="T-TRIM", position_id="open-1")
        for leg in trim_legs:
            self.ledger.record_fill(leg.order_id, leg.requested_signed_quantity, 0.50)

        # Remaining inventory is exactly 1 contract per leg
        owned_before = self.ledger.owned_quantities("IC_STRAT")
        self.assertEqual(set(owned_before.values()), {1, -1})

        # Settle remaining 1 contract ITM on put side (Settlement at 98.0 -> -$200 for 1 contract)
        settlement = create_settlement_value(date(2026, 10, 26), "98.0")
        broker_cash = create_broker_cash_evidence(self.ledger.record_scope.account_id, "act-trim-1", date(2026, 10, 26), -20000, True)

        retain_settlement(self.ledger, "open-1", settlement, broker_cash)
        retired = self.ledger.expire_strategy_positions(
            "IC_STRAT",
            date(2026, 10, 27),
            settlement=settlement,
            broker_cash=broker_cash,
        )
        self.assertGreaterEqual(retired, 1)
        self.assertEqual(self.ledger.owned_quantities("IC_STRAT"), {})

        rec = self.ledger.get_settlement_reconciliation("open-1")
        self.assertEqual(rec["outcome"], "CASH_SETTLED")
        self.assertEqual(rec["settlement_cash_cents"], -20000)

    def test_fee_incomplete_settlement_excluded_from_certified_win_rate(self) -> None:
        from trade_history_fixtures import evidence_bundle, SCOPE, AS_OF
        bundle=evidence_bundle(Path(self.temp_dir.name)/"attributed",fees=False,events=("ENTRY",))
        ledger=PositionLedger(bundle["ledger"],scope=SCOPE)
        self.addCleanup(ledger.close)
        settlement=create_settlement_value("2026-11-05","700")
        cash=create_broker_cash_evidence(SCOPE.account_id,"settlement-fee-incomplete","2026-11-05",0,True)
        retain_settlement(ledger,"position-retained-1",settlement,cash)
        self.assertEqual(ledger.expire_strategy_positions("IronCondor_V31",date(2026,11,6)),1)
        self.assertEqual(ledger.flush_outbox(bundle["journal"]),1)
        trades=load_all_trades(bundle["journal"],ledger_path=bundle["ledger"],scope=SCOPE,as_of_date=AS_OF)
        self.assertEqual(len(trades), 1)
        trade = trades[0]
        self.assertFalse(trade["is_eligible"])
        self.assertEqual(trade["reconciliation_status"], "FEE_INCOMPLETE")

        metrics = compute_strategy_metrics(trades, "IronCondor_V31")
        self.assertEqual(metrics["total_trades"], 0)
        self.assertEqual(metrics["wins"], 0)
        self.assertIn("0 trades", metrics["win_rate"])


if __name__ == "__main__":
    unittest.main()


def test_settlement_cash_account_and_activity_cannot_be_reused(tmp_path):
    import pytest
    from trade_history_fixtures import evidence_bundle,SCOPE,retain_settlement
    bundle=evidence_bundle(tmp_path,events=('ENTRY',))
    ledger=PositionLedger(bundle['ledger'],scope=SCOPE)
    try:
        settlement=create_settlement_value('2026-11-05','700')
        wrong=create_broker_cash_evidence('OTHER','shared-activity','2026-11-05',0,True)
        with pytest.raises(ValueError,match='scope'):
            retain_settlement(ledger,'position-retained-1',settlement,wrong)
        # Failed attribution has not retired any known inventory.
        assert ledger.owned_quantities('IronCondor_V31')
        assert ledger.expire_strategy_positions('IronCondor_V31',date(2026,11,6))==0
        other=create_broker_cash_evidence(SCOPE.account_id,'different-activity','2026-11-05',0,True)
        retain_settlement(ledger,'position-retained-1',settlement,other)
        second_legs=[LedgerLeg('other-'+str(i),leg['asset_key'],leg['requested_signed_quantity'])
                     for i,leg in enumerate(ledger.get_basket('retained-1-0').legs)]
        ledger.record_submission('other','IronCondor_V31','open',second_legs,trade_id='other')
        for leg in second_legs:
            ledger.record_fill(leg.order_id,leg.requested_signed_quantity,1.)
        with pytest.raises(ValueError,match='Conflicting'):
            retain_settlement(ledger,'other',settlement,other)
    finally:
        ledger.close()
