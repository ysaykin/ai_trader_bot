"""Canonical re-export of root trade_logger for dashboard callers.

Maintains 100% accounting and trajectory parity with root trade_logger.py.
"""
from __future__ import annotations

import sys
from pathlib import Path

_ROOT_DIR = Path(__file__).resolve().parent.parent
if str(_ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(_ROOT_DIR))

import trade_logger as _tl
from trade_logger import *

for _name in dir(_tl):
    if not _name.startswith("__"):
        globals()[_name] = getattr(_tl, _name)
