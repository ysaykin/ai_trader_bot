"""Tests for iron-condor math and durable fill ownership."""

from __future__ import annotations

import tempfile
import unittest
from datetime import date
from pathlib import Path
from types import SimpleNamespace

from strategies.iron_condor_math import (
    credit_strategy_pnl,
    max_defined_loss,
    option_midpoint,
    position_unrealized_pnl,
    select_short_strikes,
    signed_basket_value,
)
from strategies.position_ledger import LedgerLeg, PositionLedger, asset_key


class IronCondorMathTests(unittest.TestCase):
    def test_credit_pnl_uses_signed_basket_value_for_gains_and_losses(self) -> None:
        gain_mark = signed_basket_value(((1, 0.25), (-1, 1.25), (-1, 1.25), (1, 0.25)))
        self.assertEqual(gain_mark, -200.0)
        self.assertEqual(credit_strategy_pnl(250.0, gain_mark), 50.0)

        loss_mark = signed_basket_value(((1, 0.20), (-1, 3.20), (-1, 1.50), (1, 0.50)))
        self.assertEqual(loss_mark, -400.0)
        self.assertEqual(credit_strategy_pnl(250.0, loss_mark), -150.0)

    def test_incomplete_mark_is_unknown_and_never_zero(self) -> None:
        self.assertIsNone(signed_basket_value(((1, 0.25), (-1, None))))
        self.assertIsNone(signed_basket_value(((1, True), (-1, 1.0))))
        self.assertIsNone(credit_strategy_pnl(250.0, None))

    def test_strike_selection_and_defined_loss(self) -> None:
        put, call = select_short_strikes(
            [570, 575, 580, 585, 590, 595, 600], 585, 0.15, 0, 5
        )
        self.assertLessEqual(put, 585)
        self.assertGreaterEqual(call, 585)
        self.assertEqual(max_defined_loss(5, 2, 200), 800)

    def test_option_midpoint_pricing(self) -> None:
        self.assertEqual(option_midpoint(0.50, 0.60), 0.55)
        # Inverted bid/ask falls back to last trade
        self.assertEqual(option_midpoint(0.60, 0.50, last=0.58), 0.58)
        # Missing bid falls back to last trade
        self.assertEqual(option_midpoint(None, 0.50, last=0.45), 0.45)
        # Missing all quotes returns None
        self.assertIsNone(option_midpoint(None, None, last=None))

    def test_tradier_positions_accounting_and_basket_reconciliation(self) -> None:
        """Verify leg-level and spread-level mark-to-market against Tradier accounting."""
        # Long position: bought at $3.40 ($340 cost), mark is $5.04 -> MV $504, Gain +$164
        mv_long, gl_long = position_unrealized_pnl(1, 5.04, 340.0)
        self.assertEqual(mv_long, 504.0)
        self.assertEqual(gl_long, 164.0)

        # Short position: sold at $1.72 (-$172 cost basis), mark is $0.59 -> MV -$59, Gain +$113
        mv_short_win, gl_short_win = position_unrealized_pnl(-1, 0.59, -172.0)
        self.assertEqual(mv_short_win, -59.0)
        self.assertEqual(gl_short_win, 113.0)

        # Short position loss: sold at $3.93 (-$393 basis), mark is $5.81 -> MV -$581, Loss -$188
        mv_short_loss, gl_short_loss = position_unrealized_pnl(-1, 5.81, -393.0)
        self.assertEqual(mv_short_loss, -581.0)
        self.assertEqual(gl_short_loss, -188.0)

        # Multi-contract short: qty -2, sold at $1.265 (-$253 basis), mark $2.14 -> MV -$428, Loss -$175
        mv_2qty, gl_2qty = position_unrealized_pnl(-2, 2.14, -253.0)
        self.assertEqual(mv_2qty, -428.0)
        self.assertEqual(gl_2qty, -175.0)

        # Reconcile complete 22-leg portfolio from user's Tradier CSV
        csv_records = [
            (1, 5.04, 340.0), (1, 3.31, 191.0), (-1, 0.59, -172.0), (2, 1.39, 179.0),
            (1, 1.97, 113.0), (-1, 0.08, -82.0), (1, 1.24, 51.0), (-1, 0.39, -76.0),
            (-1, 0.03, -20.0), (-1, 0.04, -20.0), (-1, 0.03, -5.0), (1, 0.03, 4.0),
            (1, 0.03, 8.0), (1, 0.03, 9.0), (1, 0.17, 39.0), (1, 0.04, 31.0),
            (1, 0.36, 117.0), (-1, 1.56, -64.0), (-1, 3.01, -166.0), (-2, 2.14, -253.0),
            (-1, 4.16, -237.0), (-1, 5.81, -393.0),
        ]
        total_mv = sum(position_unrealized_pnl(q, p, cb)[0] for q, p, cb in csv_records)
        total_gl = sum(position_unrealized_pnl(q, p, cb)[1] for q, p, cb in csv_records)
        self.assertAlmostEqual(total_mv, -498.0, places=2)
        self.assertAlmostEqual(total_gl, -92.0, places=2)

        # Basket-level P&L verification for Nov 27 Iron Condor:
        # Opening credit = $108.00. Current marks: 736P @ 5.04, 741P @ 5.81, 805C @ 0.59, 810C @ 0.36
        ic_legs = ((1, 5.04), (-1, 5.81), (-1, 0.59), (1, 0.36))
        ic_basket_mv = signed_basket_value(ic_legs)
        self.assertAlmostEqual(ic_basket_mv, -100.0, places=2)
        ic_pnl = credit_strategy_pnl(108.0, ic_basket_mv)
        self.assertAlmostEqual(ic_pnl, 8.0, places=2)


class PositionLedgerTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.path = Path(temporary.name) / "positions.sqlite3"
        self.ledger = PositionLedger(self.path)
        self.assets = [
            SimpleNamespace(symbol="XSP", expiration="2026-10-26", strike=strike, right=right)
            for strike, right in ((95, "put"), (100, "put"), (110, "call"), (115, "call"))
        ]
        self.legs = tuple(
            LedgerLeg(f"order-{index}", asset_key(asset), quantity)
            for index, (asset, quantity) in enumerate(
                zip(self.assets, (1, -1, -1, 1)), start=1
            )
        )

    def test_submission_is_not_ownership_and_fills_are_strategy_scoped(self) -> None:
        self.ledger.record_submission("package-a", "strategy-a", "open", self.legs)
        self.assertEqual(self.ledger.owned_quantities("strategy-a"), {})
        # Pending entry reserves conservative full wing width before fills arrive.
        self.assertEqual(self.ledger.portfolio_defined_risk("strategy-a"), 500.0)
        for leg, price in zip(self.legs, (0.25, 1.25, 1.25, 0.25)):
            self.ledger.record_fill(leg.order_id, leg.requested_signed_quantity, price)
        self.assertEqual(
            self.ledger.owned_quantities("strategy-a"),
            {leg.asset_key: leg.requested_signed_quantity for leg in self.legs},
        )
        basket = self.ledger.open_baskets("strategy-a")[0]
        self.assertEqual(basket.state, "open")
        self.assertEqual(basket.opening_cash_flow, 200.0)
        self.assertEqual(self.ledger.portfolio_defined_risk("strategy-a"), 300.0)
        self.assertEqual(self.ledger.owned_quantities("strategy-b"), {})

    def test_add_trim_and_close_update_active_credit_and_close_cycle(self) -> None:
        self.ledger.record_submission("open-1", "strategy-a", "open", self.legs)
        for leg, price in zip(self.legs, (0.10, 0.50, 0.50, 0.10)):
            self.ledger.record_fill(leg.order_id, leg.requested_signed_quantity, price)
        self.assertAlmostEqual(self.ledger.active_opening_cash("strategy-a"), 80.0)

        add_legs = tuple(
            LedgerLeg(f"add-{idx}", leg.asset_key, leg.requested_signed_quantity)
            for idx, leg in enumerate(self.legs, start=1)
        )
        self.ledger.record_submission("add-1", "strategy-a", "add", add_legs)
        for leg, price in zip(add_legs, (0.10, 0.50, 0.50, 0.10)):
            self.ledger.record_fill(leg.order_id, leg.requested_signed_quantity, price)
        self.assertAlmostEqual(self.ledger.active_opening_cash("strategy-a"), 160.0)

        trim_legs = tuple(
            LedgerLeg(f"trim-{idx}", leg.asset_key, -leg.requested_signed_quantity)
            for idx, leg in enumerate(self.legs, start=1)
        )
        self.ledger.record_submission("trim-1", "strategy-a", "trim", trim_legs)
        for leg, price in zip(trim_legs, (0.05, 0.25, 0.25, 0.05)):
            self.ledger.record_fill(leg.order_id, leg.requested_signed_quantity, price)
        self.assertAlmostEqual(self.ledger.active_opening_cash("strategy-a"), 80.0)

        close_legs = tuple(
            LedgerLeg(f"close-{idx}", leg.asset_key, -leg.requested_signed_quantity)
            for idx, leg in enumerate(self.legs, start=1)
        )
        self.ledger.record_submission("close-1", "strategy-a", "close", close_legs)
        for leg, price in zip(close_legs, (0.05, 0.20, 0.20, 0.05)):
            self.ledger.record_fill(leg.order_id, leg.requested_signed_quantity, price)
        self.assertEqual(self.ledger.owned_quantities("strategy-a"), {})
        self.assertEqual(self.ledger.open_baskets("strategy-a"), ())
        self.assertEqual(self.ledger.active_opening_cash("strategy-a"), 0.0)

    def test_imbalanced_partial_fill_has_unknown_defined_risk(self) -> None:
        self.ledger.record_submission("package-a", "strategy-a", "open", self.legs)
        self.ledger.record_fill("order-1", 1, 0.25)
        self.assertIsNone(self.ledger.portfolio_defined_risk("strategy-a"))
        self.assertEqual(self.ledger.open_baskets("strategy-a")[0].state, "partial")

    def test_post_expiration_settlement_clears_expired_contracts_only(self) -> None:
        self.ledger.record_submission("package-a", "strategy-a", "open", self.legs)
        for leg in self.legs:
            self.ledger.record_fill(leg.order_id, leg.requested_signed_quantity, 1.0)
        self.assertEqual(self.ledger.expire_strategy_positions("strategy-a", date(2026, 10, 26)), 0)
        self.assertTrue(self.ledger.owned_quantities("strategy-a"))
        self.assertEqual(self.ledger.expire_strategy_positions("strategy-a", date(2026, 10, 27)), 1)
        self.assertEqual(self.ledger.owned_quantities("strategy-a"), {})
        self.assertEqual(self.ledger.open_baskets("strategy-a"), ())

    def test_ledger_survives_reopen(self) -> None:
        self.ledger.record_submission("package-a", "strategy-a", "open", self.legs)
        self.ledger.record_fill("order-1", 1, 0.25)
        reopened = PositionLedger(self.path)
        self.assertEqual(reopened.owned_quantities("strategy-a"), {self.legs[0].asset_key: 1})

    def test_broker_aliases_resolve_terminal_fill_without_local_id(self) -> None:
        self.ledger.record_submission(
            "package-a",
            "strategy-a",
            "open",
            self.legs,
            broker_tag="IC_TEST",
            duration="day",
            limit_price=2.0,
        )
        children = [
            SimpleNamespace(
                identifier=f"broker-{index}",
                asset=asset,
                side=side,
            )
            for index, (asset, side) in enumerate(
                zip(
                    self.assets,
                    ("buy_to_open", "sell_to_open", "sell_to_open", "buy_to_open"),
                ),
                start=1,
            )
        ]
        parent = SimpleNamespace(identifier="broker-parent", child_orders=children)
        self.ledger.bind_broker_submission("package-a", parent)
        basket = None
        for child, quantity, price in zip(children, (1, -1, -1, 1), (0.25, 1.25, 1.25, 0.25)):
            basket = self.ledger.record_fill(
                None,
                quantity,
                price,
                broker_id=child.identifier,
                cumulative=True,
            )
        self.assertIsNotNone(basket)
        self.assertEqual(basket.state, "open")
        self.assertEqual(basket.broker_parent_id, "broker-parent")

        # Replaying cumulative broker state is idempotent.
        replayed = self.ledger.record_fill(
            None, 1, 0.25, broker_id="broker-1", cumulative=True
        )
        self.assertEqual(replayed.opening_cash_flow, 200.0)

    def test_prior_day_day_order_is_discoverable_after_reopen(self) -> None:
        self.ledger.record_submission(
            "package-a", "strategy-a", "open", self.legs, duration="day"
        )
        self.ledger._connection.execute(
            "UPDATE packages SET created_at = '2026-10-25T14:00:00+00:00'"
        )
        self.ledger._connection.commit()
        reopened = PositionLedger(self.path)
        stale = reopened.stale_day_baskets("strategy-a", date(2026, 10, 26))
        self.assertEqual([basket.package_id for basket in stale], ["package-a"])


if __name__ == "__main__":
    unittest.main()
