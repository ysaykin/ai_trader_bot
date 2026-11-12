#!/usr/bin/env python3
"""Run execution regressions in an isolated image without broker access or records."""
from __future__ import annotations

import argparse
from pathlib import Path
import shutil
import subprocess
import tempfile


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("tests", nargs="*", default=[
        "tests/test_exit_safeguards.py", "tests/test_session_runtime.py",
        "tests/test_broker_snapshots.py", "tests/test_concurrent_positions.py",
        "tests/test_iron_condor_execution_safety.py", "tests/test_strategybro.py",
        "tests/test_regressions.py", "tests/test_tradier_order_routing.py",
        "tests/test_recovery_fail_closed.py", "tests/test_container_recovery.py",
        "tests/test_order_session_close.py", "tests/test_day_order_expiry.py",
        "tests/test_policy_pipeline.py", "tests/test_neural_strategy_switch.py",
        "tests/test_position_ledger.py", "tests/test_iron_condor_math.py::IronCondorMathTests",
        "tests/test_strike_selection_and_pnl.py",
    ])
    parser.add_argument("--image", default="lumibot-lumibot-bot:latest")
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    with tempfile.TemporaryDirectory(prefix="lumibot-exit-check-") as directory:
        target = Path(directory)
        for folder in ("strategies", "tests", "tools", "ml", "dashboard", "reflex_dashboard"):
            shutil.copytree(root / folder, target / folder,
                            ignore=shutil.ignore_patterns("__pycache__", "node_modules", ".env*", ".web"))
        for name in ("main.py", "trade_logger.py", "sitecustomize.py", "pyproject.toml", "Dockerfile"):
            shutil.copy2(root / name, target / name)
        (target / "logs").mkdir()
        return subprocess.run([
            "docker", "run", "--rm", "--network", "none",
            "-e", "PYTEST_ADDOPTS=-p no:cacheprovider --tb=short -q",
            "-e", "PYTHONPATH=/app/reflex_dashboard:/app",
            "-v", f"{target}:/app:ro", "-w", "/app", "--entrypoint", "python",
            args.image, "tools/run_offline_tests.py", *args.tests,
        ], check=False).returncode


if __name__ == "__main__":
    raise SystemExit(main())
