"""Ensure unittest test runs have isolated records and sticky guards installed."""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))
if str(_ROOT / "tests") not in sys.path:
    sys.path.insert(0, str(_ROOT / "tests"))

from strategies.runtime_records import install_test_context, test_record_root

if test_record_root() is None:
    _test_dir = tempfile.TemporaryDirectory(prefix="lumibot-unittest-")
    _root = Path(_test_dir.name)
    install_test_context(_root)
else:
    _root = test_record_root()

# An inherited test root still requires guards in this interpreter.
install_test_context(_root)
