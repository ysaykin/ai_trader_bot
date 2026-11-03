"""Execution safety and lifecycle regression tests for SPXIronCondor and PositionLedger.

Covers dynamic fee scaling (bug-02), partial fill terminal transitions (bug-03),
durable panic halt enforcement, cumulative fill pricing, and idempotent journaling.
"""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from datetime import date, datetime, time, timezone
from types import SimpleNamespace
from unittest.mock import Mock, patch

from lumibot.entities import Asset, Order

from strategies.iron_condor import SPXIronCondor
from strategies.iron_condor_math import (
    option_execution_fees,
    option_fee_per_contract,
)
from strategies.position_ledger import (
    LedgerLeg,
    PositionLedger,
    asset_key,
)


class TestOptionFeePerContract(unittest.TestCase):
    def test_fee_env_parsing(self) -> None:
        test_cases = [
            ("0.50", 0.50),
            ("0.40", 0.40),
            ("0.0", 0.0),
            ("", 0.40),
            ("   ", 0.40),
            ("-0.50", 0.40),
            ("not_a_number", 0.40),
            (None, 0.40),
        ]
        for env_val, expected in test_cases:
            with self.subTest(env_val=env_val, expected=expected):
                if env_val is None:
                    env = {}
                else:
                    env = {"OPTION_FEE_PER_CONTRACT": env_val}
                with patch.dict(os.environ, env, clear=True):
                    self.assertEqual(option_fee_per_contract(), expected)


class TestDynamicLegCountFeeAccounting(unittest.TestCase):
    def setUp(self) -> None:
        self.tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmpdir.cleanup)
        self.ledger = PositionLedger(os.path.join(self.tmpdir.name, "test_ledger.sqlite3"))
        self.addCleanup(self.ledger._connection.close)

    def _create_strategy(self, structure: str = "iron_condor", quantity: int = 1) -> SPXIronCondor:
        strategy = SPXIronCondor.__new__(SPXIronCondor)
        strategy._name = "TestStrategy"
        strategy.parameters = {
            "symbol": "SPX",
            "quantity": quantity,
            "structure": structure,
            "wing_width": 5.0,
            "short_delta": 0.15,
            "dte": 0,
            "profit_target": 0.50,
            "stop_loss_mult": 2.0,
        }
        strategy.structure = structure
        strategy._position_ledger = self.ledger
        strategy._position_open = False
        strategy._owned_legs = []
        strategy._entry_credit_cash = 0.0
        strategy._current_trade_id = None
        strategy._recovery_lock = False
        strategy._closing_orders = []
        strategy._pending_exit_reason = None
        strategy._close_reprice_seconds = 30.0
        strategy._strategy_lookup = {"TestStrategy": strategy}
        strategy.get_datetime = Mock(return_value=datetime.now(timezone.utc))
        return strategy

    def test_2_leg_vertical_spread_fees_scale_by_2_legs_not_4(self) -> None:
        # Vertical spread has 2 legs
        signed_quantities_2_leg = [-1, 1]
        fees_2_leg = option_execution_fees(signed_quantities_2_leg, 0.40)
        self.assertEqual(fees_2_leg, 0.80)

        # 4-leg Iron Condor
        signed_quantities_4_leg = [-1, 1, -1, 1]
        fees_4_leg = option_execution_fees(signed_quantities_4_leg, 0.40)
        self.assertEqual(fees_4_leg, 1.60)

        # 2-leg vertical is half the cost of 4-leg condor
        self.assertEqual(fees_4_leg, 2 * fees_2_leg)

    def test_record_filled_entry_uses_filled_quantity_for_max_risk(self) -> None:
        strategy = self._create_strategy(structure="iron_condor", quantity=5)
        # Configured quantity is 5, but filled quantity is 2
        legs = [
            LedgerLeg(order_id="leg1", asset_key=asset_key(Asset("SPXW", asset_type="option", expiration=date(2026, 11, 1), strike=5800, right="call")), requested_signed_quantity=5),
            LedgerLeg(order_id="leg2", asset_key=asset_key(Asset("SPXW", asset_type="option", expiration=date(2026, 11, 1), strike=5805, right="call")), requested_signed_quantity=-5),
        ]
        self.ledger.record_submission("pkg-1", strategy.name, "open", legs)
        self.ledger.record_fill("leg1", 2, 1.0, cumulative=True)
        filled_basket = self.ledger.record_fill("leg2", -2, 0.5, cumulative=True)

        with patch("trade_logger.record_trade_entry") as mock_entry:
            mock_entry.return_value = "trade-123"
            strategy._record_filled_entry(filled_basket)
            mock_entry.assert_called_once()
            call_kwargs = mock_entry.call_args.kwargs
            self.assertEqual(call_kwargs["quantity"], 2)
            # max_risk must be wing_width (5.0) * 100 * filled quantity (2) = 1000.0, NOT 5 * 500 = 2500.0
            self.assertEqual(call_kwargs["max_risk"], 1000.0)


class TestPartialFillTerminalReconciliation(unittest.TestCase):
    def setUp(self) -> None:
        self.tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmpdir.cleanup)
        self.ledger = PositionLedger(os.path.join(self.tmpdir.name, "test_ledger.sqlite3"))
        self.addCleanup(self.ledger._connection.close)

    def test_partial_fill_entry_transitions_to_open_on_cancellation(self) -> None:
        legs = [
            LedgerLeg(order_id="leg1", asset_key=asset_key(Asset("SPXW", asset_type="option", expiration=date(2026, 11, 1), strike=5800, right="put")), requested_signed_quantity=2),
            LedgerLeg(order_id="leg2", asset_key=asset_key(Asset("SPXW", asset_type="option", expiration=date(2026, 11, 1), strike=5795, right="put")), requested_signed_quantity=-2),
        ]
        self.ledger.record_submission("pkg-entry", "strat1", "open", legs)
        # Partially fill 1 contract out of 2
        self.ledger.record_fill("leg1", 1, 1.5, cumulative=True)
        basket = self.ledger.record_fill("leg2", -1, 1.0, cumulative=True)
        self.assertEqual(basket.state, "partial")

        # Now the residual 1 contract is canceled at broker
        resolved = self.ledger.resolve_partial_package("pkg-entry", "canceled")
        # Balanced fills transition to 'open' state representing active ownership
        self.assertEqual(resolved.state, "open")
        self.assertEqual(self.ledger.owned_quantities("strat1"), {
            legs[0].asset_key: 1,
            legs[1].asset_key: -1,
        })

    def test_partial_fill_close_transitions_to_canceled_unblocking_new_close(self) -> None:
        # First own 2 contracts
        open_legs = [
            LedgerLeg(order_id="o1", asset_key=asset_key(Asset("SPXW", asset_type="option", expiration=date(2026, 11, 1), strike=5800, right="put")), requested_signed_quantity=2),
            LedgerLeg(order_id="o2", asset_key=asset_key(Asset("SPXW", asset_type="option", expiration=date(2026, 11, 1), strike=5795, right="put")), requested_signed_quantity=-2),
        ]
        self.ledger.record_submission("pkg-open", "strat1", "open", open_legs)
        self.ledger.record_fill("o1", 2, 1.5, cumulative=True)
        self.ledger.record_fill("o2", -2, 1.0, cumulative=True)

        # Submit close order for 2 contracts
        close_legs = [
            LedgerLeg(order_id="c1", asset_key=open_legs[0].asset_key, requested_signed_quantity=-2),
            LedgerLeg(order_id="c2", asset_key=open_legs[1].asset_key, requested_signed_quantity=2),
        ]
        self.ledger.record_submission("pkg-close", "strat1", "close", close_legs)
        # Partial fill of 1 contract
        self.ledger.record_fill("c1", -1, 0.8, cumulative=True)
        basket = self.ledger.record_fill("c2", 1, 0.4, cumulative=True)
        self.assertEqual(basket.state, "partial")

        # Broker cancels residual close order
        resolved = self.ledger.resolve_partial_package("pkg-close", "canceled")
        self.assertEqual(resolved.state, "canceled")

        # Confirm package is no longer in active closing/partial states
        active_close = next(
            (b for b in self.ledger.open_baskets("strat1") if b.intent == "close" and b.state in {"submitted", "partial", "closing"}),
            None,
        )
        self.assertIsNone(active_close)

        # 1 contract remains owned to be closed by subsequent order
        owned = self.ledger.owned_quantities("strat1")
        self.assertEqual(owned[open_legs[0].asset_key], 1)
        self.assertEqual(owned[open_legs[1].asset_key], -1)

    def test_cumulative_fill_price_is_not_corrupted_by_slice_weighting(self) -> None:
        legs = [
            LedgerLeg(order_id="leg1", asset_key="asset1", requested_signed_quantity=5),
            LedgerLeg(order_id="leg2", asset_key="asset2", requested_signed_quantity=-5),
        ]
        self.ledger.record_submission("pkg-1", "strat1", "open", legs)
        # Broker reports 2 contracts filled at avg price 1.50
        b1 = self.ledger.record_fill("leg1", 2, 1.50, cumulative=True)
        self.assertEqual(b1.legs[0]["fill_price"], 1.50)

        # Broker reports 5 contracts filled at cumulative avg price 1.60
        b2 = self.ledger.record_fill("leg1", 5, 1.60, cumulative=True)
        self.assertEqual(b2.legs[0]["fill_price"], 1.60)


class TestDurablePanicHalt(unittest.TestCase):
    def setUp(self) -> None:
        self.tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmpdir.cleanup)
        self.ledger = PositionLedger(os.path.join(self.tmpdir.name, "test_ledger.sqlite3"))
        self.addCleanup(self.ledger._connection.close)

    def test_panic_halt_persistence_and_check(self) -> None:
        self.assertFalse(self.ledger.is_panic_halted())
        self.ledger.set_panic_halt(True)
        self.assertTrue(self.ledger.is_panic_halted())

        # Verify new connection to same database sees the halt
        reopened = PositionLedger(self.ledger.path)
        self.assertTrue(reopened.is_panic_halted())
        reopened._connection.close()

        self.ledger.set_panic_halt(False)
        self.assertFalse(self.ledger.is_panic_halted())

    def test_strategy_blocks_trading_iteration_when_halted(self) -> None:
        strategy = SPXIronCondor.__new__(SPXIronCondor)
        strategy._name = "TestStrategy"
        strategy._position_ledger = self.ledger
        strategy.parameters = {"symbol": "SPX"}
        self.ledger.set_panic_halt(True)

        strategy._reconcile_broker_state = Mock(return_value=True)
        strategy._panic_recovery = Mock()
        strategy._run_trading_iteration()
        strategy._reconcile_broker_state.assert_called_once()
        strategy._panic_recovery.assert_called_once()

    def test_strategy_blocks_policy_action_when_halted(self) -> None:
        strategy = SPXIronCondor.__new__(SPXIronCondor)
        strategy._name = "TestStrategy"
        strategy._position_ledger = self.ledger
        strategy._position_open = True
        strategy._exit_requested = False
        strategy.parameters = {}
        self.ledger.set_panic_halt(True)

        strategy._panic_recovery = Mock()
        result = strategy.apply_policy_action("FLATTEN_NOW")
        self.assertEqual(result, "FLATTEN_NOW")
        strategy._panic_recovery.assert_called_once()


    def test_restarted_entry_canceled_before_close(self) -> None:
        strategy = SPXIronCondor()
        strategy.parameters = {**SPXIronCondor.parameters, "position_ledger": self.ledger, "strategy_name": "RestartStrat"}
        strategy.broker = Mock()
        strategy.broker.name = "Tradier"
        strategy.initialize()
        strategy.cancel_order = Mock()
        strategy.submit_order = Mock(return_value=[])

        legs = [
            LedgerLeg("e1", "leg1", 2),
            LedgerLeg("e2", "leg2", -2),
        ]
        self.ledger.record_submission("pkg-entry", "RestartStrat", "open", legs, broker_tag="test-tag")
        broker_parent = Order(identifier="broker-parent-1", strategy="RestartStrat", status="open", tag="test-tag")
        self.ledger.bind_broker_submission("pkg-entry", broker_parent)

        # entry_orders is empty simulating restart
        position = strategy._runtime()
        position.entry_orders = []
        strategy.get_orders = Mock(return_value=[broker_parent])

        # Trigger close
        strategy._close_all_options(exit_reason="STOP_LOSS")

        # Must cancel the durable active entry and return without submitting close
        strategy.cancel_order.assert_called_once_with(broker_parent)
        strategy.submit_order.assert_not_called()

    def test_missing_underlying_price_fallback_for_expiry_exits_no_attribute_error(self) -> None:
        strategy = SPXIronCondor()
        strategy.parameters = {**SPXIronCondor.parameters, "position_ledger": self.ledger, "strategy_name": "ExpiryStrat"}
        strategy.broker = Mock()
        strategy.broker.name = "Tradier"
        strategy.initialize()
        strategy._underlying = Asset("SPXW", asset_type="index")
        strategy._is_expiring_today = Mock(return_value=True)

        # Mock _fetch_last_price to return None (spot unavailable)
        strategy._fetch_last_price = Mock(return_value=None)
        strategy._quote_bounds = Mock(return_value=None)
        strategy._fetch_quote = Mock(return_value=None)

        # Call emergency pricing calculation
        buy_item = (Asset("SPXW", asset_type="option", expiration=date(2026, 11, 20), strike=5800, right="put"), 1)
        sell_item = (Asset("SPXW", asset_type="option", expiration=date(2026, 11, 20), strike=5795, right="put"), -1)
        # Should not raise AttributeError!
        price = strategy._emergency_close_debit_fallback([buy_item, sell_item])
        self.assertIsNotNone(price)
        self.assertTrue(price > 0)

    def test_orphan_pending_entry_without_broker_identity_does_not_block_close(self) -> None:
        strategy = SPXIronCondor()
        strategy.parameters = {**SPXIronCondor.parameters, "position_ledger": self.ledger, "strategy_name": "OrphanStrat"}
        strategy.broker = Mock()
        strategy.broker.name = "Tradier"
        strategy.initialize()
        strategy.cancel_order = Mock()
        strategy.submit_order = Mock(return_value=[SimpleNamespace(identifier="close-parent", status="open")])
        strategy._package_price = Mock(return_value=1.50)
        strategy._submit_package = Mock(return_value=[])

        # 1. Filled owned legs in the position
        asset1 = Asset("SPXW", asset_type="option", expiration=date(2026, 11, 20), strike=5800, right="put")
        asset2 = Asset("SPXW", asset_type="option", expiration=date(2026, 11, 20), strike=5795, right="put")
        position = strategy._runtime()
        position.owned_legs = [(asset1, 1), (asset2, -1)]
        position.position_open = True
        position.entry_credit_cash = 100.0

        # 2. Durable pending entry that was never submitted to broker (no broker parent, no tag match)
        legs = [LedgerLeg("orphan-leg-1", "leg1", 1), LedgerLeg("orphan-leg-2", "leg2", -1)]
        self.ledger.record_submission("pkg-orphan", "OrphanStrat", "open", legs, broker_tag="unsubmitted-tag", submission_started=0)

        # Durable marker proves submission never started.
        strategy.get_orders = Mock(return_value=[])

        # Trigger close
        strategy._close_all_options(exit_reason="STOP_LOSS")

        # The orphan pending basket must be terminated (reservation released)
        basket = self.ledger.get_basket("pkg-orphan")
        self.assertFalse(self.ledger.is_inflight(basket))
        self.assertEqual(basket.state, "failed")

        # Existing filled exposure must have close order submitted (not blocked!)
        strategy._submit_package.assert_called_once()

    def test_uncertain_orphan_remains_reserved(self) -> None:
        strategy = SPXIronCondor()
        strategy.parameters = {**SPXIronCondor.parameters, "position_ledger": self.ledger, "strategy_name": "OrphanStrat"}
        strategy.broker = Mock()
        strategy.broker.name = "Tradier"
        strategy.initialize()
        strategy.cancel_order = Mock()
        strategy.submit_order = Mock(return_value=[SimpleNamespace(identifier="close-parent", status="open")])
        strategy._package_price = Mock(return_value=1.50)
        strategy._submit_package = Mock(return_value=[])

        # 1. Filled owned legs in the position
        asset1 = Asset("SPXW", asset_type="option", expiration=date(2026, 11, 20), strike=5800, right="put")
        asset2 = Asset("SPXW", asset_type="option", expiration=date(2026, 11, 20), strike=5795, right="put")
        position = strategy._runtime()
        position.owned_legs = [(asset1, 1), (asset2, -1)]
        position.position_open = True
        position.entry_credit_cash = 100.0

        # 2. Durable pending entry that was never submitted to broker (no broker parent, no tag match)
        legs = [LedgerLeg("orphan-leg-1", "leg1", 1), LedgerLeg("orphan-leg-2", "leg2", -1)]
        self.ledger.record_submission("pkg-orphan", "OrphanStrat", "open", legs, broker_tag="unsubmitted-tag", submission_started=1)

        # Broker absence cannot prove an attempted submission failed.
        strategy.get_orders = Mock(return_value=[])

        # Trigger close
        strategy._close_all_options(exit_reason="STOP_LOSS")

        # An uncertain submission must remain reserved.
        basket = self.ledger.get_basket("pkg-orphan")
        self.assertTrue(self.ledger.is_inflight(basket))
        self.assertEqual(basket.state, "submitted")

        # Closing waits for confirmed terminal broker state.
        strategy.submit_order.assert_not_called()

    def test_zero_and_debit_positions_stop_loss(self) -> None:
        for entry_cash in (0.0, -50.0):
            with self.subTest(entry_cash=entry_cash):
                strategy = SPXIronCondor()
                strategy.parameters = {**SPXIronCondor.parameters, "position_ledger": self.ledger, "strategy_name": "ZeroDebitStrat", "wing_width": 5.0}
                strategy.broker = Mock()
                strategy.broker.name = "Tradier"
                strategy.broker._shared_broker_reads = None
                strategy.initialize()
                strategy._close_all_options = Mock()
                strategy.get_orders = Mock(return_value=[])
                strategy.get_positions = Mock(return_value=[object()])
                strategy._filter_owned_option_positions = Mock(return_value=[object()])
                strategy._days_to_expiration = Mock(return_value=10)
                strategy._persist_position = Mock()
                strategy._publish_position_snapshot = Mock()

                asset1 = Asset("SPXW", asset_type="option", expiration=date(2026, 11, 20), strike=5800, right="put")
                asset2 = Asset("SPXW", asset_type="option", expiration=date(2026, 11, 20), strike=5795, right="put")
                position = strategy._runtime()
                position.owned_legs = [(asset1, 1), (asset2, -1)]
                position.position_open = True
                position.entry_credit_cash = entry_cash

                # Market quotes where position is losing:
                # asset1 (long put 5800): mid = 0.50 (+50)
                # asset2 (short put 5795): mid = 6.00 (-600)
                # current_value = +50 - 600 = -550
                # signed pnl = entry_cash + (-550) <= -500 (defined loss = 500)
                def mock_quote(asset):
                    if asset.strike == 5800:
                        return {"bid": 0.40, "ask": 0.60, "last": 0.50}
                    return {"bid": 5.90, "ask": 6.10, "last": 6.00}

                strategy._fetch_quote = mock_quote
                strategy._manage_position()

                # Must not exit early with VALUATION_UNAVAILABLE; must trigger STOP_LOSS!
                strategy._close_all_options.assert_called_once()
                self.assertEqual(strategy._close_all_options.call_args.kwargs.get("exit_reason"), "STOP_LOSS")



if __name__ == "__main__":
    unittest.main()
