"""Preview/import one explicitly confirmed manual opening in the paper account.

Inside the bot container:
  python - --order-id 41547656 --confirm-manual < tools/reconcile_manual_order.py

Add --apply --backup /app/logs/backups/manual-41547656.sqlite3 to import it.
Only broker GETs are used. The imported position stays under manual management.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sqlite3
import sys

ROOT = Path(__file__).resolve().parents[1] if Path(__file__).is_file() else Path.cwd()
sys.path.insert(0, str(ROOT))

from strategies.manual_positions import MANUAL_OWNER, import_manual_order
from strategies.position_ledger import PositionLedger, resolve_ledger_path
from strategies.runtime_records import RecordScope


def main() -> int:
    from dotenv import load_dotenv
    import requests

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--order-id", required=True)
    parser.add_argument("--confirm-manual", action="store_true")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--backup", type=Path)
    parser.add_argument("--ledger", type=Path)
    args = parser.parse_args()
    load_dotenv(ROOT / ".env")
    scope = RecordScope.from_env()
    if scope.broker != "tradier" or not scope.is_paper:
        parser.error("This repair requires the configured Tradier paper account")
    if not args.confirm_manual:
        parser.error("Confirm that the user identified this specific order as manual")
    if not args.order_id.isdecimal() or int(args.order_id) <= 0:
        parser.error("Expected a positive broker order ID")
    if args.apply and not args.backup:
        parser.error("--apply requires a new --backup path")
    token = os.getenv("TRADIER_ACCESS_TOKEN") or os.environ["TRADIER_API_KEY"]
    ledger = PositionLedger(resolve_ledger_path(args.ledger), read_only=not args.apply, scope=scope)
    session = requests.Session()
    session.headers.update({"Authorization": f"Bearer {token}", "Accept": "application/json"})

    def get(suffix: str) -> dict:
        response = session.get(f"https://sandbox.tradier.com/v1/accounts/{scope.account_id}/{suffix}", timeout=20)
        if response.status_code != 200:
            raise RuntimeError(f"Paper broker GET returned HTTP {response.status_code}")
        return response.json()

    try:
        with ledger.execution_gate():
            order = get(f"orders/{args.order_id}").get("order")
            if not isinstance(order, dict) or str(order.get("id")) != args.order_id:
                raise ValueError("Requested broker order was not returned")
            inventory_response = get("positions")
            if "positions" not in inventory_response:
                raise ValueError("Incomplete broker inventory response")
            container = inventory_response["positions"]
            if container in (None, "null"):
                positions = []
            elif isinstance(container, dict) and "position" in container:
                positions = container["position"]
                positions = [positions] if isinstance(positions, dict) else positions
                if not isinstance(positions, list):
                    raise ValueError("Incomplete broker inventory response")
            else:
                raise ValueError("Incomplete broker inventory response")
            kwargs = dict(order=order, broker_positions=positions, scope=scope, confirmed_manual=True)
            preview = import_manual_order(ledger, **kwargs)
            backup_path = None
            if args.apply and preview["result"] == "eligible":
                backup_path = args.backup.resolve()
                backup_path.parent.mkdir(parents=True, exist_ok=True)
                # Exclusive creation prevents an accidental overwrite of a prior backup.
                with backup_path.open("xb"):
                    pass
                with sqlite3.connect(backup_path) as backup:
                    ledger._connection.backup(backup)
            result = import_manual_order(ledger, **kwargs, apply=args.apply)
            result.update(applied=args.apply, backup=str(backup_path) if backup_path else None,
                          manual_defined_risk=ledger.portfolio_defined_risk(MANUAL_OWNER),
                          portfolio_defined_risk=ledger.portfolio_defined_risk())
            print(json.dumps(result, indent=2))
    finally:
        session.close()
        ledger.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
