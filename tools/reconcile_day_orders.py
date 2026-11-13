"""Preview or apply expired paper DAY reservations using fresh broker GETs.

Run inside the dashboard container with PYTHONPATH=/app. Default is a preview.
--apply updates local order lifecycle only. No broker orders are submitted or
canceled, and confirmed fills and settlement records are retained.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1] if Path(__file__).is_file() else Path.cwd()
sys.path.insert(0, str(ROOT))

from strategies.broker_reads import fresh_broker_reads, get_broker_reads
from strategies.day_orders import reconcile
from strategies.order_sessions import day_session_ended
from strategies.position_ledger import PositionLedger, resolve_ledger_path


def rows(payload, root, child):
    value = payload.get(root)
    if value in (None, "null"):
        return []
    if not isinstance(value, dict) or child not in value:
        raise ValueError(f"Incomplete broker {root} response")
    value = value[child]
    return value if isinstance(value, list) else [value]



def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    from dashboard.app import broker_settings
    from dotenv import load_dotenv
    load_dotenv(ROOT / ".env")
    settings = broker_settings(os.environ)
    if settings.name != "tradier" or not settings.paper:
        parser.error("Cleanup requires the configured Tradier paper account")
    ledger = PositionLedger(resolve_ledger_path(), read_only=not args.apply)
    try:
        # Hold the same gate as submissions while capturing evidence and applying
        # transitions. Fill callbacks also recheck inventory inside the ledger.
        with ledger.execution_gate():
            reads = get_broker_reads(settings.token, settings.account_number, True)
            with fresh_broker_reads():
                orders = rows(reads.get(f"/accounts/{settings.account_number}/orders"), "orders", "order")
                positions = rows(reads.get(f"/accounts/{settings.account_number}/positions"), "positions", "position")
                ids = {str(order["id"]) for order in orders}
                now = datetime.now(timezone.utc)
                for basket in ledger.open_baskets():
                    if (ledger.is_inflight(basket) and basket.broker_parent_id and basket.broker_parent_id not in ids
                            and (basket.duration or "").lower() == "day" and day_session_ended(basket.created_at, now)):
                        order = reads.get(f"/accounts/{settings.account_number}/orders/{basket.broker_parent_id}").get("order")
                        if not isinstance(order, dict):
                            raise ValueError("Historical order confirmation unavailable")
                        orders.append(order)
                        ids.add(basket.broker_parent_id)
            before = ledger.all_owned_quantities()
            report = reconcile(ledger, orders=orders, positions=positions, now=now, apply=args.apply)
            if ledger.all_owned_quantities() != before:
                raise RuntimeError("Cleanup changed confirmed inventory")
        print(json.dumps({"applied": args.apply, "orders": report}, indent=2))
    finally:
        ledger.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
