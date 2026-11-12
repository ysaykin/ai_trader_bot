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
    isolate_snapshot_database,
    protected_record_inventory,
    test_record_root,
)

# Defer the installed SDK's default broker construction during import.
os.environ["LUMIBOT_LAZY_CREDENTIALS"] = "1"

# Capture initial inventory of protected runtime records before application imports
_initial_inventory = protected_record_inventory()

if test_record_root() is None:
    _test_directory = tempfile.TemporaryDirectory(prefix="lumibot-pytest-")
    _root = Path(_test_directory.name)
    install_test_context(_root)
else:
    _root = test_record_root()

# Ensure empty isolated snapshot database is initialized in the test root
isolate_snapshot_database(_root)


@pytest.fixture(scope="session", autouse=True)
def verify_runtime_records_unchanged() -> Generator[None, None, None]:
    """Verify runtime journals, databases, snapshots, and sidecars are never mutated or added during test runs."""
    pre_inventory = _initial_inventory
    yield
    post_inventory = protected_record_inventory()

    deleted = set(pre_inventory.keys()) - set(post_inventory.keys())
    assert not deleted, f"Protected runtime records were deleted during test run: {sorted(deleted)}"

    mutated = [k for k in pre_inventory if k in post_inventory and pre_inventory[k] != post_inventory[k]]
    assert not mutated, f"Protected runtime records were mutated during test run: {mutated}"

    created = set(post_inventory.keys()) - set(pre_inventory.keys())
    assert not created, f"Protected runtime records directory was polluted with new files during test run: {sorted(created)}"

# An inherited test root still requires guards in this interpreter.
install_test_context(_root)
