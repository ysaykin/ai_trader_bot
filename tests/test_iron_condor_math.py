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
