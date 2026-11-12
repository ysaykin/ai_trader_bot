#!/usr/bin/env python3
"""Read-only loss audit against journal exits and confirmed ledger leg fills.

Gross cash is reconstructed independently of journal P&L. Fees are estimates,
never certified net results. Missing fills or residual inventory are reported.
Use frozen copies of both inputs for a deterministic historical replay.
"""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from contextlib import closing
from datetime import datetime
from decimal import Decimal
import json
from pathlib import Path
import sqlite3


def number(value):
    return round(float(value), 2)


def audit(ledger, journal, since, through):
    events = {}
    for line in journal.read_text().splitlines():
        row = json.loads(line)
        if row.get("event") != "EXIT":
            continue
        stamp = datetime.fromisoformat(row["timestamp"])
        if since <= stamp <= through:
            events[row.get("event_id") or (row["trade_id"], row["timestamp"])] = row
    with closing(sqlite3.connect(ledger.resolve().as_uri() + "?mode=ro", uri=True)) as db:
        db.row_factory = sqlite3.Row
        db.execute("BEGIN")
        positions = {row["position_id"]: dict(row) for row in db.execute("SELECT * FROM positions")}
        packages = defaultdict(list)
        for row in db.execute("SELECT * FROM packages"):
            packages[row["position_id"]].append(dict(row))
        legs = defaultdict(list)
        for row in db.execute("SELECT * FROM legs"):
            legs[row["package_id"]].append(dict(row))
    results = []
    for event in sorted(events.values(), key=lambda row: row["timestamp"]):
        pid = event.get("position_id")
        if pid not in positions:
            matches = [p for p in positions.values() if p["trade_id"] == event["trade_id"]
                       and p["strategy_id"] == event["strategy"]]
            pid = matches[0]["position_id"] if len(matches) == 1 else None
        cash = Decimal(0)
        opening = Decimal(0)
        contracts = 0
        inventory = Counter()
        missing = False
        for package in packages.get(pid, []):
            for leg in legs[package["package_id"]]:
                quantity = leg["filled_signed_quantity"]
                if not quantity:
                    continue
                if leg["fill_price"] is None:
                    missing = True
                    continue
                contract = json.loads(leg["asset_key"])
                multiplier = Decimal(str(contract.get("multiplier", 100)))
                flow = -Decimal(quantity) * Decimal(str(leg["fill_price"])) * multiplier
                cash += flow
                if package["intent"] in ("open", "add"):
                    opening += flow
                contracts += abs(quantity)
                inventory[leg["asset_key"]] += quantity
        flat = bool(contracts) and not missing and all(q == 0 for q in inventory.values())
        decision = event.get("decision_evidence") or {}
        decision_legs = decision.get("legs") or []
        widths = {}
        for leg in decision_legs:
            asset = json.loads(leg["asset_key"])
            widths.setdefault((asset.get("expiration"), asset.get("right")), []).append(float(asset["strike"]))
        width = max((max(values) - min(values) for values in widths.values()), default=0)
        quantity = max((abs(leg["signed_quantity"]) for leg in decision_legs), default=1)
        max_debit = width * 100 * quantity
        decision_gross = event.get("decision_gross_pnl")
        decision_debit = float(opening) - decision_gross if decision_gross is not None else None
        impossible_mark = bool(width and decision_debit is not None and decision_debit > max_debit + 0.01)
        results.append({
            "at": event["timestamp"], "strategy": event["strategy"], "position_id": pid,
            "reason": event.get("exit_trigger", event.get("exit_reason")),
            "ledger_flat": flat, "ledger_gross": number(cash) if flat else None,
            "opening_cash": number(opening), "journal_gross": event.get("gross_pnl"),
            "journal_gross_matches": flat and abs(float(cash) - event.get("gross_pnl", 0)) < 0.011,
            "estimated_round_trip_fees": number(Decimal(contracts) * Decimal("0.40")),
            "journal_fees_complete_claim": event.get("fees_complete", False),
            "journal_source": event.get("source", "legacy"),
            "decision_gross": decision_gross, "decision_debit": number(decision_debit) if decision_debit is not None else None,
            "maximum_package_debit": max_debit or None, "impossible_decision_mark": impossible_mark,
            "decision_leg_spreads": [number(float(leg["ask"]) - float(leg["bid"])) for leg in decision_legs],
        })
    grouped = defaultdict(list)
    for row in results:
        if row["ledger_flat"]:
            grouped[row["strategy"]].append(row)
    summary = []
    for strategy, rows in sorted(grouped.items()):
        gross = sum(Decimal(str(row["ledger_gross"])) for row in rows)
        fees = sum(Decimal(str(row["estimated_round_trip_fees"])) for row in rows)
        summary.append({"strategy": strategy, "closes": len(rows),
                        "gross": number(gross), "estimated_round_trip_fees": number(fees),
                        "estimated_net": number(gross - fees),
                        "gross_winners": sum(row["ledger_gross"] > 0 for row in rows)})
    return {"since": since.isoformat(), "through": through.isoformat(),
            "note": "Gross reconstructed from ledger fills, without certifying broker provenance. Fee estimates are not certified net P&L. This is not a complete account return.",
            "gross_total": number(sum(Decimal(str(row["gross"])) for row in summary)),
            "unreconciled_closes": sum(not row["ledger_flat"] for row in results),
            "impossible_decision_marks": sum(row["impossible_decision_mark"] for row in results),
            "strategies": summary, "closes": results}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ledger", type=Path, required=True)
    parser.add_argument("--journal", type=Path, required=True)
    parser.add_argument("--since", type=datetime.fromisoformat, required=True)
    parser.add_argument("--through", type=datetime.fromisoformat, required=True)
    parser.add_argument("--check", action="store_true", help="Fail on negative gross cash or an impossible exit mark.")
    args = parser.parse_args()
    report = audit(args.ledger, args.journal, args.since, args.through)
    print(json.dumps(report, indent=2))
    return int(args.check and (report["gross_total"] < 0 or report["impossible_decision_marks"] > 0))


if __name__ == "__main__":
    raise SystemExit(main())
