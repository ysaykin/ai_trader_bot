"""Install temporary record defaults before pytest imports application modules."""
from __future__ import annotations

import hashlib
import os
import sys
import tempfile
from pathlib import Path
from typing import Generator

import pytest

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))
if str(_ROOT / "tests") not in sys.path:
    sys.path.insert(0, str(_ROOT / "tests"))

from strategies.runtime_records import (
    install_test_context,
    is_protected_runtime_path,
    test_record_root,
)

if test_record_root() is None:
    _test_directory = tempfile.TemporaryDirectory(prefix="lumibot-pytest-")
    _root = Path(_test_directory.name)
    install_test_context(_root)
else:
    _root = test_record_root()


def _compute_protected_digests() -> dict[Path, str]:
    digests: dict[Path, str] = {}
    logs_dir = _ROOT / "logs"
    if logs_dir.exists():
        for name in ("trades.jsonl", "trajectories.jsonl", "position_ledger.sqlite3"):
            item = logs_dir / name
            if item.is_file():
                try:
                    digests[item] = hashlib.sha256(item.read_bytes()).hexdigest()
                except Exception:
                    pass
    return digests


@pytest.fixture(scope="session", autouse=True)
def verify_runtime_records_unchanged() -> Generator[None, None, None]:
    """Verify runtime journals and databases are never mutated during test runs."""
    pre_digests = _compute_protected_digests()
    yield
    post_digests = _compute_protected_digests()
    for path, digest in pre_digests.items():
        assert path in post_digests, f"Protected file {path} was deleted during test run!"
        assert (
            post_digests[path] == digest
        ), f"Protected file {path} was mutated during test run! (Pre: {digest}, Post: {post_digests[path]})"

# An inherited test root still requires guards in this interpreter.
install_test_context(_root)
