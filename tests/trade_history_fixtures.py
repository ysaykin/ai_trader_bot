"""Temporary, explicitly scoped retained provider exports for provenance tests.

These redacted examples use the production verifier. They confer no authenticity
on arbitrary payloads and must never be appended to runtime records.
"""
from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from strategies.position_ledger import LedgerLeg, PositionLedger, asset_key
from strategies.runtime_records import RecordScope

SCOPE = RecordScope(environment="paper", broker="tradier", account_id="REDACTED")
AS_OF = "2026-11-06"
ACQUIRED = "2026-11-05T18:00:00+00:00"


def evidence_bundle(directory: Path, *, trade_id: str = "retained-1", fees: bool = True,
                    events: tuple[str, ...] = ("ENTRY", "EXIT")) -> dict[str, Any]:
    """Build actual local ownership, immutable responses, and realization links."""
    directory.mkdir(parents=True, exist_ok=True)
    ledger_path = directory / "evidence.sqlite3"
    journal = directory / "trades.jsonl"
    ledger = PositionLedger(ledger_path, scope=SCOPE)
    records: list[dict[str, Any]] = []
    position_id = f"position-{trade_id}"
    contracts = [dict(symbol="XSP", expiration="2026-11-05", right="put", strike=strike, multiplier=100)
                 for strike in (690.0, 695.0)]
    for index, event in enumerate(events):
        package_id = f"{trade_id}-{index}"
        event_id = f"{package_id}:{event}"
        opening = event in {"ENTRY", "ADDITION"}
        signs = (1, -1) if opening else (-1, 1)
        legs = [LedgerLeg(f"{package_id}-leg-{i}", asset_key(SimpleNamespace(**c)), q,
                          f"child-{package_id}-{i}", f"parent-{package_id}")
                for i, (c, q) in enumerate(zip(contracts, signs))]
        ledger.record_submission(package_id, "IronCondor_V31", {"ENTRY":"open", "ADDITION":"add", "ADJUSTMENT":"trim", "EXIT":"close"}[event],
                                 legs, position_id=position_id, trade_id=trade_id)
        ledger.bind_broker_submission(package_id, SimpleNamespace(identifier=f"parent-{package_id}", child_orders=[]))
        provider_legs = []
        for i, leg in enumerate(legs):
            side = ("buy" if signs[i] > 0 else "sell") + ("_to_open" if opening else "_to_close")
            ledger.record_fill(leg.order_id, signs[i], 1.0, broker_id=leg.broker_order_id)
            provider_legs.append(dict(leg_id=leg.order_id, order_id=leg.broker_order_id,
                                      parent_order_id=leg.broker_parent_id, contract=contracts[i], side=side,
                                      signed_quantity=signs[i]))
        payload = dict(scope=SCOPE.to_dict(), trade_id=trade_id, position_id=position_id,
                       package_id=package_id, parent_order_id=f"parent-{package_id}", legs=provider_legs,
                       raw_response={"id": f"parent-{package_id}", "status": "filled", "leg": provider_legs})
        evidence_id = f"execution-{package_id}"
        ledger.record_broker_evidence(evidence_id, scope=SCOPE, kind="execution", provider_identity=f"parent-{package_id}:1",
                                      acquisition_source="provider-export", acquired_at=ACQUIRED, payload=payload)
        for leg in legs:
            ledger.link_realization_evidence(event_id=event_id, trade_id=trade_id, position_id=position_id,
                                              package_id=package_id, leg_id=leg.order_id, evidence_id=evidence_id,
                                              allocated_signed_quantity=leg.requested_signed_quantity)
        record = dict(event=event, event_id=event_id, trade_id=trade_id, position_id=position_id, package_id=package_id,
                      scope=SCOPE.to_dict(), strategy="IronCondor_V31", timestamp=ACQUIRED, symbol="XSP", expiry="2026-11-05",
                      quantity=1, legs=provider_legs, evidence_refs=[evidence_id],
                      strikes={"long_put":690.0,"short_put":695.0}, gross_pnl=10.0, fees=0.8, net_pnl=9.2)
        if fees:
            fid = f"fee-{package_id}"
            fp = dict(scope=SCOPE.to_dict(), event_id=event_id, trade_id=trade_id, position_id=position_id,
                      package_id=package_id, amount_cents=80, complete=True, raw_response={"commission": "0.80", "order_id":f"parent-{package_id}"})
            ledger.record_broker_evidence(fid, scope=SCOPE, kind="fee", provider_identity=fid,
                                          acquisition_source="provider-export", acquired_at=ACQUIRED, payload=fp)
            ledger._connection.execute(
                "INSERT INTO fee_evidence (identity,trade_id,package_id,position_id,amount_cents,complete,category,provenance,created_at,raw_evidence_id,event_id) "
                "VALUES (?,?,?,?,?,1,'execution','provider-export',?,?,?)",
                (fid, trade_id, package_id, position_id, 80, ACQUIRED, fid, event_id))
            ledger._connection.commit()
        records.append(record)
    ledger.close()
    journal.write_text("".join(json.dumps(r, sort_keys=True)+"\n" for r in records))
    return dict(ledger=ledger_path, journal=journal, events=records, scope=SCOPE, as_of_date=AS_OF)


def retain_settlement(ledger: PositionLedger, position_id: str, settlement: Any, cash: Any,
                      *, fee: Any = None, link: bool = True) -> dict[str, str]:
    """Retain scoped settlement exports with exact remaining contract allocation."""
    scope=ledger.record_scope
    position=ledger.get_position(position_id)
    if position.trade_id is None:
        ledger.set_trade_id(ledger.position_baskets(position_id)[0].package_id, position_id)
        position=ledger.get_position(position_id)
    sid=f'settlement-{position_id}'
    cid=f'cash-{cash.activity_id}'
    event_id=f'{position_id}:SETTLEMENT'
    official=dict(scope=scope.to_dict(), symbol='XSP',expiration=str(settlement.expiration),value=str(settlement.value),
                  raw_response={'symbol':'XSP','expiration':str(settlement.expiration),'value':str(settlement.value)})
    legs=[dict(contract={**json.loads(key),'multiplier':100},signed_quantity=value)
          for key,value in sorted(ledger.position_quantities(position_id).items()) if value]
    activity=dict(scope=scope.to_dict(),symbol='XSP',expiration=str(cash.expiration),position_id=position_id,
                  trade_id=position.trade_id,activity_id=cash.activity_id,cash_cents=cash.cash_cents,disposition_confirmed=True,legs=legs,
                  raw_response={'activity_id':cash.activity_id,'cash_cents':cash.cash_cents,'legs':legs,
                                'disposition_confirmed':cash.disposition_confirmed})
    for eid,kind,payload in ((sid,'settlement',official),(cid,'cash',activity)):
        ledger.record_broker_evidence(eid,scope=scope,kind=kind,provider_identity=cash.activity_id if kind=='cash' else eid,
                                      acquisition_source='provider-export',acquired_at=ACQUIRED,payload=payload)
    ledger.record_settlement_value(settlement,raw_evidence_id=sid)
    ledger.record_broker_cash_evidence(cash,raw_evidence_id=cid)
    if link:
        ledger.link_settlement_evidence(position_id=position_id,event_id=event_id,trade_id=position.trade_id,
                                        settlement_evidence_id=sid,cash_evidence_id=cid,allocated_cash_cents=cash.cash_cents)
    if fee:
        fid=f'raw-{fee.identity}'
        fp=dict(scope=scope.to_dict(),event_id=event_id,position_id=position_id,trade_id=position.trade_id,
                amount_cents=fee.amount_cents,complete=fee.complete,raw_response={'commission_cents':fee.amount_cents,'activity_id':cash.activity_id})
        ledger.record_broker_evidence(fid,scope=scope,kind='fee',provider_identity=fid,
                                      acquisition_source='provider-export',acquired_at=ACQUIRED,payload=fp)
        ledger.record_fee_evidence(fee,position_id=position_id,trade_id=position.trade_id,raw_evidence_id=fid,event_id=event_id)
    return dict(settlement_evidence_id=sid,cash_evidence_id=cid,event_id=event_id)
