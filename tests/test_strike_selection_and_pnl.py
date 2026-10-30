"""Unit test suite for iron condor strike selection and P&L math."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

from strategies.iron_condor_math import (
    credit_strategy_pnl,
    max_defined_loss,
    select_short_strikes,
    signed_basket_value,
)
from strategies.position_ledger import LedgerLeg, PositionLedger, asset_key


class TestStrikeSelection(unittest.TestCase):
    @property
    def available_strikes(self) -> list[float]:
        return [float(x) for x in range(560, 615, 5)]

    def test_short_strikes_bracket_underlying(self) -> None:
        underlying = 585.0
        short_put, short_call = select_short_strikes(
            strikes=self.available_strikes,
            underlying_price=underlying,
            short_delta=0.15,
            dte=0,
            wing_width=5.0,
        )
        self.assertIsNotNone(short_put)
        self.assertIsNotNone(short_call)
        self.assertLessEqual(short_put, underlying)
        self.assertGreaterEqual(short_call, underlying)
        self.assertIn(short_put, self.available_strikes)
        self.assertIn(short_call, self.available_strikes)

    def test_asymmetric_strikes_return_none_when_unavailable(self) -> None:
        put, call = select_short_strikes(
            strikes=[],
            underlying_price=585.0,
            short_delta=0.15,
            dte=0,
            wing_width=5.0,
        )
        self.assertIsNone(put)
        self.assertIsNone(call)

    def test_single_sided_strikes(self) -> None:
        # Only strikes above underlying -> put cannot be formed
        put, call = select_short_strikes(
            strikes=[590.0, 595.0, 600.0],
            underlying_price=585.0,
            short_delta=0.15,
            dte=0,
            wing_width=5.0,
        )
        self.assertIsNone(put)
        self.assertIsNotNone(call)
        self.assertGreaterEqual(call, 585.0)

    def test_unsorted_duplicate_strikes_match_sorted_unique_chain(self) -> None:
        strikes = self.available_strikes
        expected = select_short_strikes(strikes, 585.0, 0.15, 0, 5.0)
        self.assertEqual(
            select_short_strikes(list(reversed(strikes)) + strikes[::2], 585.0, 0.15, 0, 5.0),
            expected,
        )

    def test_put_only_chain_preserves_missing_call(self) -> None:
        put, call = select_short_strikes([570.0, 575.0, 580.0], 585.0, 0.15, 0, 5.0)
        self.assertIsNotNone(put)
        self.assertIsNone(call)


class TestPnLMath(unittest.TestCase):
    def test_winning_trade_positive_pnl(self) -> None:
        # Opening credit = $250. Current mark cost = $100 -> PnL = +$150
        opening_credit = 250.0
        basket_mark = -100.0  # debit to close
        pnl = credit_strategy_pnl(opening_credit, basket_mark)
        self.assertEqual(pnl, 150.0)

    def test_losing_trade_negative_pnl(self) -> None:
        # Opening credit = $200. Current mark cost = $450 -> PnL = -$250
        opening_credit = 200.0
        basket_mark = -450.0
        pnl = credit_strategy_pnl(opening_credit, basket_mark)
        self.assertEqual(pnl, -250.0)

    def test_incomplete_legs_return_none(self) -> None:
        for mark in (None, -0.01, True, False, float("nan"), float("inf"), -float("inf")):
            with self.subTest(mark=mark):
                self.assertIsNone(signed_basket_value(((1, 0.25), (-1, mark))))
        self.assertIsNone(credit_strategy_pnl(250.0, None))

    def test_complete_zero_marks_are_valid(self) -> None:
        marked_value = signed_basket_value(((1, 0.0), (-1, 0.0), (-1, 0.0), (1, 0.0)))
        self.assertEqual(marked_value, 0.0)
        self.assertEqual(credit_strategy_pnl(250.0, marked_value), 250.0)

    def test_max_defined_loss_calculation(self) -> None:
        # wing_width=5.0, quantity=2, credit=$200 (=$1.00/contract)
        # max loss = 5.0 * 100 * 2 - 200 = 1000 - 200 = 800
        loss = max_defined_loss(5.0, 2, 200.0)
        self.assertEqual(loss, 800.0)

    def test_multi_contract_gross_valuation(self) -> None:
        marked_value = signed_basket_value(((3, 0.25), (-3, 0.75), (-3, 0.75), (3, 0.25)))
        self.assertEqual(marked_value, -300.0)
        self.assertEqual(credit_strategy_pnl(600.0, marked_value), 300.0)


class TestPositionLedgerPartialFillCash(unittest.TestCase):
    def test_partial_fill_included_in_active_opening_cash(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            ledger_path = Path(tmp_dir) / "test_ledger.sqlite3"
            ledger = PositionLedger(ledger_path)

            assets = [
                SimpleNamespace(symbol="XSP", expiration="2026-10-27", strike=strike, right=right)
                for strike, right in ((575, "put"), (580, "put"), (590, "call"), (595, "call"))
            ]
            legs = tuple(
                LedgerLeg(f"order-{i}", asset_key(a), qty)
                for i, (a, qty) in enumerate(zip(assets, (1, -1, -1, 1)), start=1)
            )

            # Record submission
            ledger.record_submission("pkg-partial-1", "strategy-test", "open", legs, limit_price=1.50)

            # Fill only two legs: state is now 'partial'
            ledger.record_fill("order-1", 1, 0.20)
            ledger.record_fill("order-2", -1, 1.20)

            basket = ledger.open_baskets("strategy-test")[0]
            self.assertEqual(basket.state, "partial")
            self.assertIsNotNone(basket.opening_cash_flow)

            # active_opening_cash should now include the partial basket cash flow
            active_cash = ledger.active_opening_cash("strategy-test")
            self.assertGreater(active_cash, 0.0)


class TestClosingPackagePrices(unittest.TestCase):
    CASES = [
        ({"bid": 0.0, "ask": 0.0}, {"bid": 0.0, "ask": 0.0}, 0.01),
        ({"bid": 0.1, "ask": 0.3}, {"bid": 0.1, "ask": 0.3}, 0.2),
        ({"bid": 0.0, "ask": 0.1}, {"bid": 0.2, "ask": 0.3}, 0.01),
        ({"bid": 0.0, "ask": 0.2}, {"bid": 0.05, "ask": 0.25}, 0.15),
        ({"bid": 1.0, "ask": 1.2}, {"bid": 0.1, "ask": 0.3}, 1.8),
        (None, {"bid": 0.1, "ask": 0.3}, None),
        ({"bid": 0.3, "ask": 0.1}, {"bid": 0.1, "ask": 0.3}, None),
    ]

    def test_debit_price_with_complete_and_incomplete_quotes(self) -> None:
        from test_regressions import iron_condor

        for quantity in (1, 3):
            for short_quote, long_quote, expected in self.CASES:
                strategy = iron_condor.SPXIronCondor()
                strategy._fetch_quote = Mock(side_effect=[short_quote, long_quote, short_quote, long_quote])
                legs = [(Mock(), signed * quantity) for signed in (1, -1, 1, -1)]
                price = strategy._package_price(legs, order_type="debit")
                if expected is None:
                    self.assertIsNone(price)
                else:
                    self.assertAlmostEqual(price, expected, places=5)

    def test_entry_still_requires_a_positive_credit(self) -> None:
        from test_regressions import iron_condor

        strategy = iron_condor.SPXIronCondor()
        strategy._fetch_quote = Mock(return_value={"bid": 0.0, "ask": 0.0})
        self.assertIsNone(strategy._package_price([(Mock(), 1), (Mock(), -1)], order_type="credit"))


if __name__ == "__main__":
    unittest.main()
