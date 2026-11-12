#!/usr/bin/env python3
"""Replay dashboard regressions in a source copy with no credentials or records.

Run with a Python environment containing the dashboard dependencies. Additional
arguments are passed to pytest. Live services can keep writing their own records.
"""
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile


def main() -> int:
    root = Path(__file__).resolve().parents[1]
    with tempfile.TemporaryDirectory(prefix="lumibot-monitoring-check-") as temporary:
        target = Path(temporary)
        for name in ("dashboard", "reflex_dashboard", "strategies", "ml", "tests", "tools"):
            shutil.copytree(root / name, target / name,
                            ignore=shutil.ignore_patterns("__pycache__", ".web", "node_modules", "*.log", ".pytest_cache"))
        for name in ("trade_logger.py", "main.py", "pyproject.toml"):
            shutil.copy2(root / name, target / name)
        return subprocess.call([sys.executable, "-m", "pytest", "-q",
                                *(sys.argv[1:] or ["tests/test_dashboard_monitoring.py"])], cwd=target)


if __name__ == "__main__":
    raise SystemExit(main())
