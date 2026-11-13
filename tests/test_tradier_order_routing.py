"""Offline contracts for the installed LumiBot broker, with SDK I/O replaced.

These are required gates: missing upstream routing behavior must fail, not skip.
The Tradier constructor is bypassed to avoid credentials and authentication;
the real base initialization, guards, serialization, parsing and polling run.
"""

from __future__ import annotations

import os
import unittest
from datetime import date, datetime, timezone
from types import SimpleNamespace
from typing import Any
from unittest.mock import Mock, patch

import pandas as pd
from lumibot.brokers.broker import Broker
from lumibot.brokers.tradier import Tradier
from lumibot.entities import Asset, Order

from test_regressions import iron_condor, incubator


class TradierOrderRoutingTests(unittest.TestCase):
    def test_external_recovery_tag_is_not_ingested_by_strategy_polling(self) -> None:
        self.broker._pull_broker_all_orders = Mock(return_value=[
            {"id": 987654, "tag": "RC0123456789abcdef0123", "status": "filled"},
        ])
        self.broker._first_iteration = False
        with patch.object(self.broker, "_parse_broker_order_dict", side_effect=AssertionError("Recovery belongs to its worker")) as parse:
            self.broker.do_polling()
        parse.assert_not_called()
        self.assertEqual(self.events, [])

    def setUp(self) -> None:
        environment = patch.dict(os.environ, {"LUMIBOT_TELEMETRY": "0"}, clear=True)
        environment.start()
        self.addCleanup(environment.stop)
        self.broker = Tradier.__new__(Tradier)
        Broker.__init__(
            self.broker, name="Tradier", data_source=Mock(),
            connect_stream=False, start_orders_thread=False,
        )
        self.broker.tradier = SimpleNamespace(orders=Mock())
        self.broker.tradier.orders.multileg_order.return_value = {"id": "broker-parent"}
        self.broker.stream = Mock()
        self.broker.sync_positions = Mock()
        self.ledger = iron_condor.PositionLedger(":memory:")
        self.addCleanup(self.ledger._connection.close)
        self.host = self.make_strategy("IC_Host / café-1")
        self.events: list[tuple[str, dict[str, Any]]] = []
        self.subscriber = SimpleNamespace(
            name=self.host.name, FILLED_ORDER="fill", PARTIALLY_FILLED_ORDER="partial",
            add_event=Mock(side_effect=lambda event, payload: self.events.append((event, payload))),
        )
        self.broker._add_subscriber(self.subscriber)

    def make_strategy(self, name: str) -> Any:
        strategy = iron_condor.SPXIronCondor()
        strategy.name = name
        strategy.parameters = {**iron_condor.SPXIronCondor.parameters, "position_ledger": self.ledger}
        strategy.broker = self.broker
        strategy.initialize()
        strategy.get_datetime = Mock(return_value=datetime.now(timezone.utc))
        strategy.get_positions = Mock(return_value=[])
        strategy.create_order = Mock(side_effect=lambda asset, quantity, side: Order(
            strategy=strategy.name, asset=asset, quantity=quantity, side=side,
        ))
        strategy.submit_order = self.broker._submit_orders
        return strategy

    @staticmethod
    def option_legs(quantity: int = 1) -> list[tuple[Asset, int]]:
        return [
            (Asset("XSP", asset_type="option", expiration=date(2026, 11, 20), strike=strike, right=right), sign * quantity)
            for strike, right, sign in ((95, "put", 1), (100, "put", -1), (110, "call", -1), (115, "call", 1))
        ]

    def test_real_full_and_partial_fill_guards_accept_normalized_host_tag(self) -> None:
        tag = self.host._broker_routing_tag()
        self.assertEqual(tag, Broker.normalize_broker_strategy_tag(self.host.name))
        order = Order(strategy=self.host.name, asset=Asset("XSP"), quantity=2, side="buy", tag=tag)
        self.assertTrue(self.broker.order_belongs_to_local_strategy(order))
        self.broker._on_partially_filled_order(None, order, 1.0, 1, 100)
        self.broker._on_filled_order(None, order, 1.0, 2, 100)
        self.assertEqual([event for event, _ in self.events], ["partial", "fill"])
        self.assertTrue(all(payload["order"] is order for _, payload in self.events))

    def test_real_guards_reject_foreign_tags_even_when_strategy_names_match(self) -> None:
        for tag in ("Other-Strategy", "IC_0123456789abcdef0123", "IC0123456789abcdef0123"):
            with self.subTest(tag=tag):
                order = Order(strategy=self.host.name, asset=Asset("XSP"), quantity=2, side="buy", tag=tag)
                self.assertFalse(self.broker.order_belongs_to_local_strategy(order))
                self.broker._on_partially_filled_order(None, order, 1.0, 1, 100)
                self.broker._on_filled_order(None, order, 1.0, 2, 100)
        self.subscriber.add_event.assert_not_called()

    def test_atomic_serialization_preserves_host_tag_through_sdk_boundary(self) -> None:
        [parent] = self.host._submit_package(
            self.option_legs(), intent="open", order_type="credit", price=2.0,
        )
        api = self.broker.tradier.orders.multileg_order
        api.assert_called_once()
        self.assertEqual(api.call_args.kwargs["tag"], self.host._broker_routing_tag())
        self.assertEqual(api.call_args.kwargs["order_type"], "credit")
        self.assertEqual(len(api.call_args.kwargs["legs"]), 4)
        self.assertEqual(parent.tag, self.host._broker_routing_tag())
        self.assertTrue(all(child.tag == parent.tag for child in parent.child_orders))
        [basket] = self.ledger.open_baskets(self.host.name)
        self.assertEqual(basket.broker_tag, parent.tag)
        self.assertEqual(basket.broker_parent_id, parent.identifier)
        self.assertEqual(self.ledger.all_owned_quantities(), {})

    def polling_row(self, identifier: str, tag: str) -> dict[str, Any]:
        row = {
            "id": identifier, "class": "multileg", "symbol": "XSP",
            "side": "buy_to_open", "quantity": 1, "status": "open",
            "type": "market", "duration": "day", "tag": tag,
            "create_date": "2026-10-31T12:00:00.000Z", "avg_fill_price": None,
        }
        row["leg"] = [
            {
                **{key: value for key, value in row.items() if key != "leg"},
                "id": f"{identifier}-{index}", "class": "option",
                "option_symbol": f"XSP261120{right}{strike * 1000:08d}",
                "side": side,
            }
            for index, (right, strike, side) in enumerate((
                ("P", 95, "buy_to_open"), ("P", 100, "sell_to_open"),
                ("C", 110, "sell_to_open"), ("C", 115, "buy_to_open"),
            ))
        ]
        return row

    def test_fresh_polling_discovers_host_order_and_filters_foreign_order(self) -> None:
        self.assertEqual(self.broker.get_all_orders(), [])
        rows = [
            self.polling_row("host-parent", self.host._broker_routing_tag()),
            self.polling_row("foreign-parent", "Other-Strategy"),
        ]
        self.broker.tradier.orders.get_orders.return_value = pd.DataFrame(rows)
        self.broker.do_polling()
        self.broker.sync_positions.assert_called_once_with(None)
        orders = self.broker.get_all_orders()
        self.assertEqual({order.identifier for order in orders}, {
            "host-parent", "host-parent-0", "host-parent-1", "host-parent-2", "host-parent-3",
        })
        self.assertTrue(all(order.strategy == self.host.name for order in orders))
        self.assertTrue(all(order.tag == self.host._broker_routing_tag() for order in orders))
        parent = next(order for order in orders if order.identifier == "host-parent")
        self.assertEqual({child.parent_identifier for child in parent.child_orders}, {"host-parent"})
        self.assertFalse(self.broker._first_iteration)
        self.broker.do_polling()
        self.assertEqual(len(self.broker.get_all_orders()), 5)

    def test_host_callbacks_resolve_follower_and_duplicate_observations_do_not_add_fills(self) -> None:
        follower = self.make_strategy("Follower_A")
        peer = self.make_strategy("Follower_B")
        incubator.attach_strategies_to_host([self.host, follower, peer])
        [parent] = follower._submit_package(
            self.option_legs(quantity=2), intent="open", order_type="credit", price=2.0,
        )
        [basket] = self.ledger.open_baskets(follower.name)
        # Tradier returns broker-assigned child IDs on subsequent polling reads.
        broker_children = []
        for index, local_child in enumerate(parent.child_orders):
            right_char = "P" if str(local_child.asset.right).lower() == "put" else "C"
            opt_symbol = f"XSP261120{right_char}{int(local_child.asset.strike) * 1000:08d}"
            leg_dict = {
                "id": f"broker-child-{index}",
                "class": "option",
                "symbol": "XSP",
                "option_symbol": opt_symbol,
                "side": local_child.side,
                "quantity": 2,
                "status": "submitted",
                "type": "market",
                "duration": "day",
                "create_date": "2026-10-31T12:00:00.000Z",
                "exec_quantity": 0,
                "avg_fill_price": None,
                "tag": self.host._broker_routing_tag(),
            }
            child = self.broker._parse_broker_order(
                leg_dict, self.host.name, order_class_override=Order.OrderClass.SIMPLE
            )
            child.parent_identifier = parent.identifier
            broker_children.append(child)
        parent.child_orders = broker_children
        self.ledger.bind_broker_children(basket.package_id, parent.child_orders)

        def deliver(event: str, payload: dict[str, Any]) -> None:
            self.events.append((event, payload))
            if event == "fill":
                self.host.on_filled_order(**payload)
            elif event == "partial":
                self.host.on_partially_filled_order(**payload)

        self.subscriber.add_event.side_effect = deliver
        first = parent.child_orders[0]
        self.assertFalse(hasattr(first, "_ledger_order_id"))
        first._raw["exec_quantity"] = 1
        first._raw["avg_fill_price"] = 0.25
        first.avg_fill_price = 0.25
        first.status = "partially_filled"
        self.broker._on_partially_filled_order(None, first, 0.25, 1, 100)
        quantities = self.ledger.owned_quantities(follower.name)
        self.assertEqual(list(quantities.values()), [1])
        self.assertTrue(follower._position_open)
        self.assertFalse(peer._position_open)
        self.assertEqual(self.ledger.owned_quantities(self.host.name), {})
        self.assertEqual(self.ledger.owned_quantities(peer.name), {})
        self.broker._on_partially_filled_order(None, first, 0.25, 1, 100)
        follower._reconcile_inflight_orders([parent])
        self.assertEqual(self.ledger.owned_quantities(follower.name), quantities)
        self.assertEqual(self.ledger.get_basket(basket.package_id).opening_cash_flow, -25.0)

        with patch("trade_logger.record_trade_entry", return_value="confirmed-trade"):
            for child, price in zip(parent.child_orders, (0.25, 1.25, 1.25, 0.25)):
                child._raw["exec_quantity"] = 2
                child._raw["avg_fill_price"] = price
                child.avg_fill_price = price
                child.status = "filled"
                self.broker._on_filled_order(None, child, price, 2, 100)
        filled = self.ledger.get_basket(basket.package_id)
        self.assertEqual(filled.state, "open")
        self.assertEqual(filled.opening_cash_flow, 400.0)
        quantities = self.ledger.owned_quantities(follower.name)
        self.assertEqual(sorted(quantities.values()), [-2, -2, 2, 2])
        self.broker._on_filled_order(None, parent.child_orders[-1], 0.25, 2, 100)
        follower._reconcile_inflight_orders([parent])
        self.assertEqual(self.ledger.owned_quantities(follower.name), quantities)
        self.assertEqual(self.ledger.get_basket(basket.package_id).opening_cash_flow, 400.0)
        self.assertEqual(self.ledger.owned_quantities(peer.name), {})

    def test_fresh_parent_with_complete_execution_evidence_avoids_four_child_requests(self) -> None:
        [parent] = self.host._submit_package(self.option_legs(), intent="open", order_type="credit", price=2.0)
        [basket] = self.ledger.open_baskets(self.host.name)
        row = self.polling_row(str(parent.identifier), self.host._broker_routing_tag())
        row["status"] = "filled"
        for leg, price in zip(row["leg"], (.25, 1.25, 1.25, .25)):
            leg.update(status="filled", exec_quantity=1, avg_fill_price=price)
        with patch.object(self.broker, "_pull_broker_order", return_value=row) as pull, \
             patch("trade_logger.record_trade_entry", return_value="confirmed"):
            self.assertFalse(self.host._reconcile_inflight_orders([parent]))
        pull.assert_called_once_with(str(parent.identifier))
        self.assertEqual(self.ledger.get_basket(basket.package_id).state, "open")
        self.assertEqual(self.ledger.get_basket(basket.package_id).opening_cash_flow, 200.0)

    def test_provider_parent_child_evidence_survives_cumulative_replay(self) -> None:
        from trade_history_fixtures import SCOPE
        from strategies.position_ledger import PositionLedger
        self.ledger=PositionLedger(":memory:",scope=SCOPE)
        self.addCleanup(self.ledger.close)
        self.broker._tradier_account_number=SCOPE.account_id
        self.broker._tradier_paper=True
        host=self.make_strategy("EvidenceHost")
        host.parameters["symbol"]="XSP"
        [parent]=host._submit_package(self.option_legs(quantity=2),intent="open",order_type="credit",price=2.0)
        [basket]=self.ledger.open_baskets(host.name)
        row=self.polling_row(str(parent.identifier),host._broker_routing_tag())
        row.update(quantity=2,status="partially_filled")
        for leg,price in zip(row["leg"],(.25,1.25,1.25,.25)):
            leg.update(quantity=2,status="partially_filled",exec_quantity=1,avg_fill_price=price)
        with patch.object(self.broker,"_pull_broker_order",return_value=row):
            host._reconcile_inflight_orders([parent])
            quantities=self.ledger.all_owned_quantities()
            host._reconcile_inflight_orders([parent])
            self.assertEqual(self.ledger.all_owned_quantities(),quantities)
        row["status"]="filled"
        for leg in row["leg"]:
            leg.update(status="filled",exec_quantity=2)
        with patch.object(self.broker,"_pull_broker_order",return_value=row), patch("trade_logger.record_trade_entry",return_value=basket.position_id) as journal:
            host._reconcile_inflight_orders([parent])
        links=self.ledger.realization_evidence(f"{basket.package_id}:ENTRY")
        self.assertEqual(len(links),4)
        from strategies.runtime_records import classify_trade
        from tools.reconcile_trade_history import load_ledger_execution_evidence_from_connection
        data=journal.call_args.kwargs
        event=dict(event="ENTRY",event_id=data["event_id"],trade_id=data["trade_id"],
                   position_id=data["position_id"],package_id=data["package_id"],scope=SCOPE.to_dict(),
                   symbol=data["symbol"],expiry=data["expiry"],quantity=data["quantity"],
                   strikes=data["strikes"],legs=data["realization_legs"],evidence_refs=data["evidence_refs"])
        attributed=classify_trade([event],load_ledger_execution_evidence_from_connection(self.ledger),expected_scope=SCOPE,as_of_date="2026-11-06")
        self.assertEqual(attributed["partition"],"accepted",attributed)
        self.assertEqual(attributed["reconciliation_status"],"FEE_INCOMPLETE")
        self.assertEqual({abs(link["allocated_signed_quantity"]) for link in links},{2})
        for link in links:
            payload=self.ledger.get_broker_evidence(link["evidence_id"])["payload"]
            self.assertEqual(payload["parent_response"]["id"],str(parent.identifier))
            self.assertEqual(payload["raw_response"]["exec_quantity"],2)
        host._record_filled_order(self.broker._parse_broker_order_dict(row,host.name).child_orders[0],.25,2)
        self.assertEqual(len(self.ledger.realization_evidence(f"{basket.package_id}:ENTRY")),4)
        self.assertEqual(sorted(self.ledger.all_owned_quantities().values()),[-2,-2,2,2])

    def test_callback_first_retains_observed_parent_without_polling(self) -> None:
        from trade_history_fixtures import SCOPE
        from strategies.position_ledger import PositionLedger
        from strategies.runtime_records import classify_trade
        from tools.reconcile_trade_history import load_ledger_execution_evidence_from_connection

        for source in ('tracked', 'snapshot'):
            with self.subTest(source=source):
                self.ledger = PositionLedger(':memory:', scope=SCOPE)
                self.addCleanup(self.ledger.close)
                self.broker._tradier_account_number = SCOPE.account_id
                self.broker._tradier_paper = True
                host = self.make_strategy('CallbackEvidence')
                host.parameters['symbol'] = 'XSP'
                [submitted] = host._submit_package(self.option_legs(quantity=2), intent='open', order_type='credit', price=2.)
                [basket] = self.ledger.open_baskets(host.name)
                row = self.polling_row(str(submitted.identifier), host._broker_routing_tag())
                row.update(quantity=2, status='filled')
                for leg, price in zip(row['leg'], (.25, 1.25, 1.25, .25)):
                    leg.update(quantity=2, status='filled', exec_quantity=2, avg_fill_price=price)
                observed = self.broker._parse_broker_order_dict(row, host.name)
                self.ledger.bind_broker_children(basket.package_id, observed.child_orders)
                reads = SimpleNamespace(account=SCOPE.account_id, sandbox=True,
                                        snapshot=Mock(return_value={'data': {'orders': {'order': row}}}))
                self.broker._shared_broker_reads = reads
                cached = observed if source == 'tracked' else submitted
                with patch.object(self.broker, 'get_order', return_value=cached), \
                     patch.object(self.broker, '_pull_broker_order', side_effect=AssertionError('unexpected polling')), \
                     patch('trade_logger.record_trade_entry', return_value=basket.position_id) as journal:
                    first = observed.child_orders[0]
                    first._raw['exec_quantity'] = 1
                    host.on_partially_filled_order(None, first, .25, 1, 100)
                    partial_quantities = self.ledger.all_owned_quantities()
                    host.on_partially_filled_order(None, first, .25, 1, 100)
                    self.assertEqual(self.ledger.all_owned_quantities(), partial_quantities)
                    first._raw['exec_quantity'] = 2
                    for child in observed.child_orders:
                        self.assertNotIn('parent_order_id', child._raw)
                        host.on_filled_order(None, child, child.avg_fill_price, 2, 100)
                    journal.assert_called_once()
                    data = journal.call_args.kwargs
                    event = dict(event='ENTRY', event_id=data['event_id'], trade_id=data['trade_id'],
                                 position_id=data['position_id'], package_id=data['package_id'], scope=SCOPE.to_dict(),
                                 symbol=data['symbol'], expiry=data['expiry'], quantity=data['quantity'],
                                 strikes=data['strikes'], legs=data['realization_legs'], evidence_refs=data['evidence_refs'],
                                 fees_complete=False)
                    result = classify_trade([event], load_ledger_execution_evidence_from_connection(self.ledger),
                                            expected_scope=SCOPE, as_of_date='2026-11-06')
                    self.assertEqual(result['partition'], 'accepted', result)
                    self.assertEqual(result['reconciliation_status'], 'FEE_INCOMPLETE')
                    quantities = self.ledger.all_owned_quantities()
                    host.on_filled_order(None, first, .25, 2, 100)
                    host._reconcile_inflight_orders([observed])
                    self.assertEqual(self.ledger.all_owned_quantities(), quantities)
                    self.assertEqual(sorted(quantities.values()), [-2, -2, 2, 2])
                    if source == 'snapshot':
                        reads.snapshot.assert_called_with('orders')

    def test_aggregate_parent_fill_is_not_treated_as_an_unmatched_leg(self) -> None:
        [parent] = self.host._submit_package(self.option_legs(), intent="open", order_type="credit", price=2.0)
        [basket] = self.ledger.open_baskets(self.host.name)
        with self.assertNoLogs(iron_condor.__name__, level="WARNING"):
            self.host.on_filled_order(None, parent, -2.0, 4, 100)
        self.assertEqual(self.ledger.all_owned_quantities(), {})
        with patch("trade_logger.record_trade_entry", return_value="confirmed"):
            for child, price in zip(parent.child_orders, (.25, 1.25, 1.25, .25)):
                self.host.on_filled_order(None, child, price, 1, 100)
        owned = self.ledger.all_owned_quantities()
        self.assertEqual(sorted(owned.values()), [-1, -1, 1, 1])
        with self.assertNoLogs(iron_condor.__name__, level="WARNING"):
            self.host.on_filled_order(None, parent, -2.0, 4, 100)
        self.assertEqual(self.ledger.all_owned_quantities(), owned)
        self.assertEqual(self.ledger.get_basket(basket.package_id).opening_cash_flow, 200.0)

    def test_cancel_backoff_observes_raw_tradier_status_changes(self) -> None:
        row = self.polling_row("close-parent", self.host._broker_routing_tag())
        row["status"] = "pending"
        pending = self.broker._parse_broker_order_dict(row, self.host.name)
        self.host.cancel_order = Mock(side_effect=RuntimeError("cancel rejected"))
        self.host._cancel_managed_order(pending)
        self.host._cancel_managed_order(pending)
        self.host.cancel_order.assert_called_once()
        row["status"] = "open"
        opened = self.broker._parse_broker_order_dict(row, self.host.name)
        self.assertEqual(pending.status, opened.status)
        self.host._cancel_managed_order(opened)
        self.assertEqual(self.host.cancel_order.call_count, 2)

    def test_incomplete_parent_fetches_only_the_child_missing_execution_evidence(self) -> None:
        [parent] = self.host._submit_package(self.option_legs(), intent="open", order_type="credit", price=2.0)
        row = self.polling_row(str(parent.identifier), self.host._broker_routing_tag())
        row["status"] = "filled"
        for leg, price in zip(row["leg"], (.25, 1.25, 1.25, .25)):
            leg.update(status="filled", exec_quantity=1, avg_fill_price=price)
        child = dict(row["leg"][2])
        del row["leg"][2]["exec_quantity"]
        def pull(identifier):
            self.assertIn(identifier, {str(parent.identifier), child["id"]})
            return row if identifier == str(parent.identifier) else child
        with patch.object(self.broker, "_pull_broker_order", side_effect=pull) as fetch, \
             patch("trade_logger.record_trade_entry", return_value="confirmed"):
            self.assertFalse(self.host._reconcile_inflight_orders([parent]))
        self.assertEqual(fetch.call_count, 2)
        [basket] = self.ledger.open_baskets(self.host.name)
        self.assertEqual(basket.state, "open")
        self.assertEqual(basket.opening_cash_flow, 200.0)

    def test_fresh_prior_day_pending_entry_reserves_one_slot_without_locking_second(self) -> None:
        self.host.parameters["max_concurrent_positions"] = 2
        self.host.get_datetime.return_value = datetime(2026, 11, 2, 15, tzinfo=timezone.utc)
        [parent] = self.host._submit_package(self.option_legs(), intent="open", order_type="credit", price=2.0)
        row = self.polling_row(str(parent.identifier), self.host._broker_routing_tag())
        row["status"] = "pending"
        for leg in row["leg"]:
            leg.update(status="pending", exec_quantity=0, avg_fill_price=0)
        self.host.get_datetime.return_value = datetime(2026, 11, 3, 17, tzinfo=timezone.utc)
        self.host.get_orders = Mock(return_value=[parent])
        with patch.object(self.broker, "_pull_broker_order", return_value=row):
            self.assertTrue(self.host._reconcile_broker_state())
        self.assertFalse(self.host._recovery_lock)
        self.assertEqual(len(self.ledger.open_positions(self.host.name)), 1)
        self.assertEqual(self.ledger.portfolio_defined_risk(self.host.name), 500)
        self.broker.tradier.orders.multileg_order.return_value = {"id": "second-parent"}
        self.host._submit_package(self.option_legs(), intent="open", order_type="credit", price=2.0)
        self.assertEqual(len(self.ledger.open_positions(self.host.name)), 2)
        self.assertEqual(self.ledger.portfolio_defined_risk(self.host.name), 1000)
        with self.assertRaisesRegex(RuntimeError, "concurrency"):
            self.host._submit_package(self.option_legs(), intent="open", order_type="credit", price=2.0)

    def test_prior_day_pending_entry_with_missing_execution_evidence_keeps_lock(self) -> None:
        self.host.get_datetime.return_value = datetime(2026, 11, 2, 15, tzinfo=timezone.utc)
        [parent] = self.host._submit_package(self.option_legs(), intent="open", order_type="credit", price=2.0)
        row = self.polling_row(str(parent.identifier), self.host._broker_routing_tag())
        row["status"] = "pending"
        for leg in row["leg"]:
            leg.update(status="pending", exec_quantity=0, avg_fill_price=0)
        del row["leg"][2]["exec_quantity"]
        self.host.get_datetime.return_value = datetime(2026, 11, 3, 17, tzinfo=timezone.utc)
        self.host.get_orders = Mock(return_value=[parent])
        def pull(identifier):
            return row if str(identifier) == str(parent.identifier) else None
        with patch.object(self.broker, "_pull_broker_order", side_effect=pull):
            self.assertTrue(self.host._reconcile_broker_state())
        self.assertTrue(self.host._recovery_lock)
        self.assertEqual(len(self.ledger.open_positions(self.host.name)), 1)
        self.assertEqual(self.ledger.portfolio_defined_risk(self.host.name), 500)


if __name__ == "__main__":
    unittest.main()
