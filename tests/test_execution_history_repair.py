"""Historical repairs require exact retained orders and preserve financial rows."""
import json
import os
import sqlite3
from unittest.mock import patch

import pytest

from strategies.position_ledger import PositionLedger
from strategies.scoped_recovery import contract_symbol
from tools.repair_execution_history import activate, build, digest, financial_fingerprint, verify
from trade_history_fixtures import ACQUIRED, SCOPE, evidence_bundle


def inputs(tmp_path):
    bundle = evidence_bundle(tmp_path / "original", fees=False)
    captures = tmp_path / "captures"
    captures.mkdir()
    ledger = PositionLedger(bundle["ledger"], scope=SCOPE)
    for event in bundle["events"]:
        basket = ledger.get_basket(event["package_id"])
        children = [dict(id=leg["broker_order_id"], symbol="XSP", option_symbol=contract_symbol(leg["asset_key"]),
                         side=leg["side"], quantity=abs(leg["requested_signed_quantity"]),
                         exec_quantity=abs(leg["filled_signed_quantity"]), avg_fill_price=leg["fill_price"])
                    for leg in basket.legs]
        capture = dict(scope=SCOPE.to_dict(), acquired_at=ACQUIRED, source="https://sandbox.tradier.com/v1",
                       payload={"order": dict(id=basket.broker_parent_id, status="filled", leg=children)})
        (captures / f"order-{basket.broker_parent_id}.json").write_text(json.dumps(capture))
        for field in ("position_id", "package_id", "legs", "evidence_refs"):
            event.pop(field)
    ledger.close()
    bundle["journal"].write_text("".join(json.dumps(event) + "\n" for event in bundle["events"]))
    return bundle, captures


def test_repair_restores_provenance_without_certifying_estimated_fees(tmp_path):
    bundle, captures = inputs(tmp_path)
    before = financial_fingerprint(bundle["ledger"])
    manifest = build(bundle["ledger"], [bundle["journal"]], captures, tmp_path / "repair", "2026-11-06")
    assert manifest["enriched_events"] == 2
    assert manifest["summary"] == {"FEE_INCOMPLETE": 1}
    assert financial_fingerprint(tmp_path / "repair/ledger.sqlite3") == before
    assert financial_fingerprint(bundle["ledger"]) == before
    verify(tmp_path / "repair")


def test_wrong_scope_or_fill_cannot_be_activated(tmp_path):
    bundle, captures = inputs(tmp_path)
    path = next(captures.glob("*.json"))
    record = json.loads(path.read_text())
    record["payload"]["order"]["leg"][0]["exec_quantity"] = 2
    path.write_text(json.dumps(record))
    with pytest.raises(ValueError, match="executions differ"):
        build(bundle["ledger"], [bundle["journal"]], captures, tmp_path / "repair", "2026-11-06")


def test_legacy_event_requires_a_unique_filled_package_and_provider_match(tmp_path):
    bundle, captures = inputs(tmp_path)
    for event in bundle["events"]:
        event.pop("event_id")
    bundle["journal"].write_text("".join(json.dumps(event) + "\n" for event in bundle["events"]))
    manifest = build(bundle["ledger"], [bundle["journal"]], captures, tmp_path / "repair", "2026-11-06")
    assert manifest["summary"] == {"FEE_INCOMPLETE": 1}


def test_enriching_entry_does_not_reassign_legacy_sibling_packages(tmp_path):
    bundle, captures = inputs(tmp_path)
    with sqlite3.connect(bundle["ledger"]) as db:
        db.execute("UPDATE packages SET trade_id=NULL WHERE intent='close'")
    before = financial_fingerprint(bundle["ledger"])
    build(bundle["ledger"], [bundle["journal"]], captures, tmp_path / "repair", "2026-11-06")
    assert financial_fingerprint(tmp_path / "repair/ledger.sqlite3") == before


def test_activation_rejects_changed_input_and_running_writers(tmp_path):
    bundle, captures = inputs(tmp_path)
    output = tmp_path / "repair"
    build(bundle["ledger"], [bundle["journal"]], captures, output, "2026-11-06")
    with pytest.raises(ValueError, match="Stop bot"):
        activate(output, bundle["journal"], writers_stopped=False)
    original = digest(bundle["ledger"])
    bundle["journal"].write_text(bundle["journal"].read_text() + "\n")
    with pytest.raises(ValueError, match="Runtime changed"):
        activate(output, bundle["journal"], writers_stopped=True)
    assert digest(bundle["ledger"]) == original


def test_activation_resumes_after_a_crash_between_ledger_and_journal(tmp_path):
    bundle, captures = inputs(tmp_path)
    output = tmp_path / "repair"
    build(bundle["ledger"], [bundle["journal"]], captures, output, "2026-11-06")
    replace = os.replace
    calls = []

    def crash_on_journal(source, target):
        calls.append(target)
        if len(calls) == 2:
            raise OSError("simulated interruption")
        return replace(source, target)

    with patch("tools.repair_execution_history.os.replace", side_effect=crash_on_journal), \
         pytest.raises(OSError, match="interruption"):
        activate(output, bundle["journal"], writers_stopped=True)
    activate(output, bundle["journal"], writers_stopped=True)
    assert digest(bundle["journal"]) == digest(output / "trades.jsonl")
    verify(output)
    activate(output, bundle["journal"], writers_stopped=True)
