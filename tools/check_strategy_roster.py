"""Inspect configured entries and optional ledger recovery without starting a broker."""

import argparse
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from dotenv import load_dotenv
from strategies.incubator import _get_management_specs, get_incubator_specs
from strategies.position_ledger import PositionLedger


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ledger", type=Path, help="Inspect management-only owners in this ledger, opened read-only")
    args = parser.parse_args()
    load_dotenv(Path(__file__).resolve().parents[1] / ".env")
    configured_count = os.getenv("INCUBATOR_STRATEGY_COUNT", "").strip()
    entries = get_incubator_specs(limit=int(configured_count) if configured_count.isdigit() else None)
    managers = []
    if args.ledger is not None:
        ledger = PositionLedger(args.ledger, read_only=True)
        try:
            managers = _get_management_specs(entries, ledger)
        finally:
            ledger.close()
    def describe(spec):
        return {
            "strategy": spec["strategy_name"],
            "structure": "neural" if spec.get("pure_neural_mode") else spec.get("structure", "iron_condor"),
            "dte": spec["dte"],
        }
    print(json.dumps({
        "entry_enabled": [describe(s) for s in entries],
        "management_only": [describe(s) for s in managers],
    }, indent=2))


if __name__ == "__main__":
    main()
