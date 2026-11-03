"""Report entry capacity, reserved orders and optional fresh paper broker evidence.

Run inside the deployed bot with:
docker exec -i lumibot-bot python - --fresh --expect-capacity 2 < tools/check_trading_runtime.py
This command reads broker state and never submits or cancels an order.
"""
from __future__ import annotations

import argparse
from collections import Counter
import json
import os
from pathlib import Path
import re
import sys

ROOT = Path(__file__).resolve().parents[1] if Path(__file__).is_file() else Path.cwd()
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv
from strategies.broker_reads import BrokerReadUnavailable, get_broker_reads, fresh_broker_reads
from strategies.position_ledger import PositionLedger, resolve_ledger_path


def rows(payload, root, child):
    value = (payload.get(root) or {}).get(child) or []
    return [value] if isinstance(value, dict) else value


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fresh", action="store_true", help="Read fresh paper broker orders, inventory and held option quotes")
    parser.add_argument("--expect-capacity", type=int)
    args = parser.parse_args()
    load_dotenv(ROOT / ".env")
    capacity = int(os.getenv("IC_MAX_CONCURRENT_POSITIONS") or 1)
    report = {"max_positions": capacity,
              "spacing_minutes": float(os.getenv("IC_ENTRY_SPACING_MINUTES") or 60),
              "strategy_risk_cap": float(os.getenv("IC_MAX_POSITION_RISK") or 5000),
              "portfolio_risk_cap": os.getenv("IC_MAX_PORTFOLIO_RISK"),
              "strategies": [], "unresolved_orders": []}
    ledger = PositionLedger(resolve_ledger_path(), read_only=True)
    try:
        counts = Counter(p.strategy_id for p in ledger.open_positions())
        for owner, count in sorted(counts.items()):
            report["strategies"].append({"strategy": owner, "slots_used": count,
                                         "slots_free": max(0, capacity - count),
                                         "reserved_risk": ledger.portfolio_defined_risk(owner)})
        report["portfolio_reserved_risk"] = ledger.portfolio_defined_risk()
        inflight = [b for b in ledger.open_baskets() if ledger.is_inflight(b)]
        for basket in inflight:
            report["unresolved_orders"].append({"strategy": basket.strategy_id,
                "package_id": basket.package_id, "parent_id": basket.broker_parent_id,
                "state": basket.state, "created_at": basket.created_at})
        if args.fresh:
            paper = os.getenv("TRADIER_PAPER", "true").lower() in {"true", "1", "yes", "on"}
            if not paper:
                parser.error("Fresh diagnostics require TRADIER_PAPER=true")
            token = os.getenv("TRADIER_ACCESS_TOKEN") or os.environ["TRADIER_API_KEY"]
            account = os.getenv("TRADIER_ACCOUNT_NUMBER") or os.environ["TRADIER_ACCOUNT"]
            reads = get_broker_reads(token, account, True)
            with fresh_broker_reads():
                positions = rows(reads.get(f"/accounts/{account}/positions"), "positions", "position")
                report["broker_contracts"] = len(positions)
                owned = {}
                for key, quantity in ledger.all_owned_quantities().items():
                    asset = json.loads(key)
                    symbol = (asset["symbol"] + asset["expiration"][:10].replace("-", "")[2:]
                              + asset["right"][0].upper() + f"{round(float(asset['strike']) * 1000):08d}")
                    owned[symbol] = quantity
                actual = {p["symbol"]: p["quantity"] for p in positions
                          if re.fullmatch(r"[A-Z.]+\d{6}[CP]\d{8}", p.get("symbol", ""))}
                report["inventory_mismatches"] = {
                    symbol: {"ledger": owned.get(symbol, 0), "broker": actual.get(symbol, 0)}
                    for symbol in sorted(owned.keys() | actual.keys())
                    if owned.get(symbol, 0) != actual.get(symbol, 0)}
                symbols = sorted({p["symbol"] for p in positions if p.get("symbol")})
                try:
                    quotes = rows(reads.quotes(symbols), "quotes", "quote")
                    report["fresh_held_quotes"] = len(quotes)
                except BrokerReadUnavailable as error:
                    report["quote_error"] = str(error)
                for record in report["unresolved_orders"]:
                    if not record["parent_id"]:
                        continue
                    try:
                        order = reads.get(f"/accounts/{account}/orders/{record['parent_id']}").get("order") or {}
                        record["broker_status"] = order.get("status")
                        legs = order.get("leg") or []
                        legs = [legs] if isinstance(legs, dict) else legs
                        record["executions"] = [{"id": leg.get("id"), "status": leg.get("status"),
                                                  "quantity": leg.get("exec_quantity")} for leg in legs]
                    except BrokerReadUnavailable as error:
                        record["read_error"] = str(error)
    finally:
        ledger.close()
    print(json.dumps(report, indent=2))
    return int((args.expect_capacity is not None and capacity != args.expect_capacity)
               or "quote_error" in report or bool(report.get("inventory_mismatches")))


if __name__ == "__main__":
    raise SystemExit(main())
