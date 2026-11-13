"""Reconcile ended DAY reservations using confirmed broker inventory."""
from collections import defaultdict
import math
from strategies.order_sessions import day_session_ended
from strategies.scoped_recovery import ACTIVE, contract_symbol


def reconcile(ledger, *, orders, positions, now, apply=False):
    """Resolve only ended DAY orders with no active order or unexplained fills."""
    by_id = {str(order["id"]): order for order in orders}
    actual = defaultdict(int)
    for position in positions:
        quantity = float(position["quantity"])
        if not quantity.is_integer():
            raise ValueError("Broker contract quantity must be integral")
        actual[position["symbol"]] += int(quantity)
    report = []
    active = [order for order in orders if str(order.get("status") or "").lower() in ACTIVE]
    for basket in ledger.open_baskets():
        if (not ledger.is_inflight(basket) or (basket.duration or "").lower() != "day"
                or not day_session_ended(basket.created_at, now)):
            continue
        order = by_id.get(str(basket.broker_parent_id))
        row = {"package_id": basket.package_id, "strategy": basket.strategy_id, "result": "retained"}
        report.append(row)
        # An unidentified working broker order could own a missing local ID.
        if (order is not None and order in active) or (order is None and active):
            row["reason"] = "Broker still reports active orders"
            continue
        if order:
            if str(order.get("duration") or "").lower() != "day":
                row["reason"] = "Broker duration is not DAY"
                continue
            legs = order.get("leg") or [order]
            legs = legs if isinstance(legs, list) else [legs]
            recorded = {contract_symbol(leg["asset_key"]): abs(leg["filled_signed_quantity"])
                        for leg in basket.legs}
            def conflicting_execution(leg):
                raw = leg.get('exec_quantity')
                try:
                    executed = float(raw)
                except (TypeError, ValueError):
                    return True
                return (isinstance(raw, bool) or not math.isfinite(executed)
                        or not executed.is_integer() or executed < 0
                        or executed > recorded.get(leg.get('option_symbol') or leg.get('symbol'), 0))
            if any(conflicting_execution(leg) for leg in legs):
                row["reason"] = "Broker executions need fill reconciliation"
                continue
        quantities = {leg["asset_key"]: actual[contract_symbol(leg["asset_key"])]
                      for leg in basket.legs}
        owned = ledger.all_owned_quantities()
        if any(quantities[key] != owned.get(key, 0) for key in quantities):
            row["reason"] = "Broker and ledger inventory differ on requested contracts"
            continue
        row["result"] = "eligible"
        if apply:
            resolved = ledger.expire_day_order(basket.package_id, now=now, broker_quantities=quantities)
            row["result"] = "canceled" if resolved else "retained"
    return report

