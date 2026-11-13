"""Reduce one position using durable, attributable single-leg close packages.

Each step either reconciles an existing order or submits one reduction. A lost
submission response remains reserved and is matched by its exact tag on retry.
"""
from __future__ import annotations

from datetime import date, datetime, timezone
import json
import math
from types import SimpleNamespace
import uuid

from strategies.position_ledger import LedgerLeg

ACTIVE = {"open", "pending", "submitted", "accepted", "calculated", "partially_filled", "pending_cancel", "held"}
TERMINAL = {"canceled", "cancelled", "expired", "rejected", "failed", "error"}


def contract_symbol(key: str) -> str:
    asset = json.loads(key)
    expiry = date.fromisoformat(asset["expiration"][:10])
    right = "P" if asset["right"].lower() == "put" else "C"
    return f"{asset['symbol']}{expiry:%y%m%d}{right}{round(float(asset['strike']) * 1000):08d}"


def reconcile_order(ledger, basket, raw: dict) -> None:
    """Apply only exact contract/side/quantity matches from a provider order."""
    if basket.broker_parent_id and str(raw.get("id")) != str(basket.broker_parent_id):
        raise ValueError("Recovery order identity mismatch")
    children = raw.get("leg") or [raw]
    children = [children] if isinstance(children, dict) else children
    expected = {contract_symbol(leg["asset_key"]): leg for leg in basket.legs}
    if len(children) != len(expected):
        raise ValueError("Incomplete recovery order legs")
    observations = []
    seen = set()
    for child in children:
        symbol = child.get("option_symbol") or child.get("symbol")
        leg = expected.get(symbol)
        if leg is None or symbol in seen:
            raise ValueError("Recovery order contracts differ from reservation")
        seen.add(symbol)
        sign = 1 if leg["requested_signed_quantity"] > 0 else -1
        suffix = "open" if basket.intent in {"open", "add"} else "close"
        side = ("buy_to_" if sign > 0 else "sell_to_") + suffix
        quantity = float(child.get("quantity", 0))
        executed = float(child.get("exec_quantity", 0))
        if (isinstance(child.get("quantity"), bool) or isinstance(child.get("exec_quantity"), bool)
                or child.get("side") != side or quantity != abs(leg["requested_signed_quantity"])
                or not math.isfinite(executed) or not executed.is_integer()
                or executed < 0 or executed > quantity):
            raise ValueError("Recovery order side or quantity differs from reservation")
        if str(raw.get("status")).lower() == "filled" and executed != quantity:
            raise ValueError("Filled recovery order lacks complete leg executions")
        asset = SimpleNamespace(**json.loads(leg["asset_key"]), asset_type="option", multiplier=100)
        if not child.get("id"):
            raise ValueError("Provider order is missing its leg identity")
        observation = SimpleNamespace(identifier=str(child["id"]), asset=asset,
                                      side=side, _raw=child)
        observations.append((leg, observation, int(executed) * sign))
    ledger.bind_broker_submission(basket.package_id, SimpleNamespace(identifier=str(raw["id"])))
    ledger.bind_broker_children(basket.package_id, [item[1] for item in observations])
    for leg, observation, executed in observations:
        if executed:
            ledger.record_fill(leg["order_id"], executed, float(observation._raw["avg_fill_price"]))
            ledger.capture_order_evidence(observation, leg, scope=ledger.record_scope,
                                          parent_response=raw if raw.get("leg") else None)
    status = str(raw.get("status", "")).lower()
    if status in TERMINAL:
        ledger.resolve_partial_package(basket.package_id, "canceled" if status == "cancelled" else
                                       "failed" if status == "error" else status)


def recover_position(ledger, client, job: dict) -> tuple[bool, str | None]:
    """Run under execution_gate; preserve other positions and unresolved orders."""
    position_id = job["target_id"]
    position = ledger.get_position(position_id)
    # Resume reporting after a crash between recording a fill and dispatching it.
    for basket in ledger.position_baskets(position_id):
        queue_recovery_realization(ledger, basket)
    if client.account != ledger.record_scope.account_id:
        raise ValueError("Recovery client account differs from ledger")
    if client.sandbox != (ledger.record_scope.environment != "live"):
        raise ValueError("Recovery client environment differs from ledger")
    orders = client._fetch_all_orders()
    for basket in ledger.position_baskets(position_id):
        if not ledger.is_inflight(basket):
            continue
        matches = [order for order in orders if
                   (basket.broker_parent_id and str(order.get("id")) == basket.broker_parent_id)
                   or (basket.broker_tag and order.get("tag") == basket.broker_tag)]
        if basket.broker_parent_id:
            response = client._get(f"{client.base_url}/accounts/{client.account}/orders/{basket.broker_parent_id}",
                                   headers=client.headers, fresh=True)
            response.raise_for_status()
            raw = response.json().get("order")
        else:
            raw = matches[0] if len(matches) == 1 else None
        if not isinstance(raw, dict):
            return False, "Prior position submission remains unidentified"
        reconcile_order(ledger, basket, raw)
        queue_recovery_realization(ledger, ledger.get_basket(basket.package_id))
        if ledger.is_inflight(ledger.get_basket(basket.package_id)):
            recovery = ledger._connection.execute(
                "SELECT recovery_job_id FROM packages WHERE package_id=?", (basket.package_id,)
            ).fetchone()[0]
            if not recovery and str(raw.get("status", "")).lower() in ACTIVE:
                client._submit_once("delete", f"{client.base_url}/accounts/{client.account}/orders/{raw['id']}",
                                    headers=client.headers, timeout=6)
            return False, "Waiting for position order termination and confirmed fills"
    inventory = ledger.position_quantities(position_id)
    if not inventory:
        return True, None
    today = datetime.now(timezone.utc).date()
    if any(date.fromisoformat(json.loads(key)["expiration"][:10]) < today for key in inventory):
        return False, "Expired position requires settlement evidence"
    actual = {}
    for row in client._fetch_all_positions():
        count = float(row["quantity"])
        if not math.isfinite(count) or not count.is_integer():
            raise ValueError("Broker option inventory must contain whole contracts")
        actual[row["symbol"]] = actual.get(row["symbol"], 0) + int(count)
    owned = ledger.all_owned_quantities()
    for key in inventory:
        if actual.get(contract_symbol(key), 0) != owned.get(key, 0):
            return False, "Broker inventory differs from attributable ledger inventory"
        net = actual.get(contract_symbol(key), 0)
        if net * inventory[key] <= 0 or abs(net) < abs(inventory[key]):
            return False, "Opposing ownership prevents an account reduce-only close"
    # Confirm short reductions before releasing any long protection.
    key, quantity = min(inventory.items(), key=lambda item: (item[1] > 0, item[0]))
    symbol = contract_symbol(key)
    asset = json.loads(key)
    response = client._get(f"{client.base_url}/markets/quotes", headers=client.headers,
                           params={"symbols": symbol}, fresh=True)
    response.raise_for_status()
    quote = response.json().get("quotes", {}).get("quote")
    if isinstance(quote, list):
        quote = quote[0] if len(quote) == 1 else None
    if not isinstance(quote, dict) or quote.get("symbol") != symbol:
        return False, "Exact recovery contract quote unavailable"
    stamps = [float(quote.get(field) or 0) / 1000 for field in ("bid_date", "ask_date")]
    ages = [datetime.now(timezone.utc).timestamp() - stamp for stamp in stamps]
    if any(not math.isfinite(age) or age < -5 or age > (1020 if client.sandbox else 120) for age in ages):
        return False, "Recovery quote observation time is missing or stale"
    bid, ask = float(quote.get("bid", -1)), float(quote.get("ask", -1))
    if (not all(math.isfinite(value) for value in (bid, ask)) or bid < 0 or ask <= 0
            or ask < bid or ask - bid > max(.50, (ask + bid) * .25)):
        return False, "Recovery quote is invalid or too wide"
    side = "buy_to_close" if quantity < 0 else "sell_to_close"
    price = max(.05, round(math.ceil(ask / .05) * .05, 2)) if quantity < 0 else max(.05, round(math.floor(bid / .05) * .05, 2))
    package_id = uuid.uuid4().hex
    tag = "RC" + package_id[:20]
    ledger.record_submission(package_id, position.strategy_id, "close",
                             [LedgerLeg(package_id + ":0", key, -quantity)],
                             position_id=position_id, trade_id=position.trade_id,
                             limit_price=price, broker_tag=tag, recovery_job_id=job["job_id"])
    ledger.mark_submission_started(package_id)
    response = client._submit_once("post", f"{client.base_url}/accounts/{client.account}/orders",
                                  headers=client.headers, timeout=6, data={
                                      "class": "option", "symbol": asset["symbol"], "option_symbol": symbol,
                                      "quantity": str(abs(quantity)), "side": side, "type": "limit",
                                      "duration": "day", "price": f"{price:.2f}", "tag": tag})
    if response.status_code in {400, 401, 403, 404, 422, 429}:
        ledger.record_terminal_status(package_id, "rejected")
        return False, f"Recovery close rejected with HTTP {response.status_code}"
    response.raise_for_status()
    accepted = response.json().get("order")
    if not isinstance(accepted, dict) or not accepted.get("id"):
        return False, "Recovery submission response is ambiguous; reservation retained"
    ledger.bind_broker_submission(package_id, SimpleNamespace(identifier=str(accepted["id"])))
    return False, "Recovery close submitted; awaiting confirmed fills"


def queue_recovery_realization(ledger, basket) -> None:
    """Durably journal each terminal recovery fill, retaining unknown actual fees."""
    recovery = ledger._connection.execute(
        'SELECT recovery_job_id FROM packages WHERE package_id=?', (basket.package_id,)
    ).fetchone()[0]
    if (not recovery or ledger.is_inflight(basket)
            or not any(leg['filled_signed_quantity'] for leg in basket.legs)
            or ledger.journaled(basket.package_id, 'REALIZATION')):
        return
    trade_id = basket.trade_id or basket.position_id
    event_id = basket.package_id + ':REALIZATION'
    metadata = ledger.attach_package_evidence(basket.package_id, event_id, trade_id)
    if len(metadata['legs']) != len(basket.legs):
        raise ValueError('Recovery realization lacks retained fill evidence')
    remaining = ledger.quantities_after(basket.package_id)
    opening = ledger.closing_opening_cash(basket.package_id)
    closing = float(basket.closing_cash_flow or 0)
    event = 'ADJUSTMENT' if remaining else 'EXIT'
    payload = dict(metadata, event=event, event_id=event_id, scope=ledger.record_scope.to_dict(),
                   trade_id=trade_id, strategy=basket.strategy_id, exit_reason='RECOVERY',
                   opening_credit_cash=opening, closing_cash=closing, gross_pnl=round(opening+closing, 2),
                   fees=None, net_pnl=None, remaining_quantity=max(map(abs, remaining.values()), default=0),
                   source='broker_verified')
    with ledger._lock, ledger._connection:
        if ledger.journaled(basket.package_id, 'REALIZATION'):
            return
        ledger._connection.execute(
            "INSERT INTO outbox(event_type,payload,status,created_at) VALUES (?,?,'pending',?)",
            (event, json.dumps(payload), datetime.now(timezone.utc).isoformat()))
        ledger._connection.execute(
            "INSERT INTO journal_events(package_id,event) VALUES (?,'REALIZATION')", (basket.package_id,))
