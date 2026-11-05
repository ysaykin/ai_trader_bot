#!/usr/bin/env python3
"""Offline test runner with sticky write/network guards and protected-record verification.

Ensures:
- Unit tests run in strictly isolated temporary environments.
- Zero mutations to production records (trades.jsonl, position_ledger.sqlite3, trajectories.jsonl).
- External network requests to brokers fail closed.
- Works across both pytest and unittest discovery.
"""
from __future__ import annotations

import argparse
import hashlib
import os
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from strategies.runtime_records import install_test_context, is_protected_runtime_path

PROTECTED_RECORDS = [
    ROOT / "logs/trades.jsonl",
    ROOT / "logs/trajectories.jsonl",
    ROOT / "logs/position_ledger.sqlite3",
]


def compute_digests() -> dict[Path, str]:
    digests: dict[Path, str] = {}
    for p in PROTECTED_RECORDS:
        if p.exists() and p.is_file():
            digests[p] = hashlib.sha256(p.read_bytes()).hexdigest()
    return digests


def main() -> None:
    parser = argparse.ArgumentParser(description="Run offline tests with protected-record verification.")
    parser.add_argument("tests", nargs="*", default=["tests"], help="Test paths to run")
    parser.add_argument("--framework", choices=["pytest", "unittest"], default="pytest")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args()

    # Pre-run digests
    pre_digests = compute_digests()

    # Install isolated test context
    test_dir = tempfile.TemporaryDirectory(prefix="lumibot-offline-runner-")
    root_path = Path(test_dir.name)
    install_test_context(root_path)

    # Determine Python executable
    python_bin = sys.executable
    venv_py = ROOT / ".venv/bin/python3"
    if venv_py.exists():
        python_bin = str(venv_py)

    env = dict(os.environ)
    env["LUMIBOT_TEST_RECORD_ROOT"] = str(root_path)
    env["LUMIBOT_OFFLINE_GUARD"] = "1"
    env["RUN_TRADIER_SANDBOX_WRITE_TEST"] = "0"
    env["TRADIER_ACCESS_TOKEN"] = "sanitized_test_token"
    env["TRADIER_TOKEN"] = "sanitized_test_token"
    env["TRADIER_ACCOUNT_ID"] = "VA_TEST_ACCOUNT"
    env["TRADIER_PAPER"] = "true"
    py_path = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = f"{ROOT}:{py_path}" if py_path else str(ROOT)

    if args.framework == "pytest":
        cmd = [python_bin, "-m", "pytest"]
        if args.verbose:
            cmd.append("-v")
        cmd.extend(args.tests)
    else:
        cmd = [python_bin, "-m", "unittest", "discover", "-s", "tests", "-t", "."]
        if args.verbose:
            cmd.append("-v")

    print("=" * 70)
    print("STARTING OFFLINE TEST RUNNER")
    print(f"Isolated Test Root: {root_path}")
    print(f"Protected Files:     {len(pre_digests)} verified")
    print("=" * 70)

    try:
        res = subprocess.run(cmd, env=env)
        retcode = res.returncode
    finally:
        # Post-run verification
        post_digests = compute_digests()
        for path, digest in pre_digests.items():
            if path not in post_digests:
                print(f"CRITICAL ERROR: Protected record {path} was deleted!", file=sys.stderr)
                sys.exit(1)
            if post_digests[path] != digest:
                print(
                    f"CRITICAL ERROR: Protected record {path} was mutated during test run!\n"
                    f"Pre:  {digest}\nPost: {post_digests[path]}",
                    file=sys.stderr,
                )
                sys.exit(1)
        print("=" * 70)
        print("PROTECTED RECORD INTEGRITY: PASSED (Zero mutations to production records)")
        print("=" * 70)

    sys.exit(retcode)


if __name__ == "__main__":
    main()
