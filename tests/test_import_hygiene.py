"""Guard against a local package shadowing the upstream Lumibot dependency."""

import importlib.util
from importlib.metadata import distribution
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

try:
    import pytest
except ImportError:
    pytest = None

ROOT = Path(__file__).resolve().parents[1]


class TestImportHygiene(unittest.TestCase):
    def test_entrypoint_import_does_not_construct_an_implicit_broker(self) -> None:
        environment = dict(os.environ)
        environment.pop("LUMIBOT_LAZY_CREDENTIALS", None)
        environment.update(PYTHONPATH=str(ROOT), TRADING_BROKER="tradier",
                           TRADIER_ACCESS_TOKEN="test-token", TRADIER_ACCOUNT_NUMBER="test-account",
                           TRADIER_PAPER="true", DATA_SOURCE="")
        code = """
from unittest.mock import patch
with patch('lumibot.brokers.Tradier') as constructor:
    import main
    import strategies.iron_condor
    constructor.assert_not_called()
"""
        with tempfile.TemporaryDirectory() as temporary:
            result = subprocess.run([sys.executable, "-c", code], cwd=temporary,
                                    env=environment, capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_no_local_lumibot_directory_shadows_upstream(self) -> None:
        self.assertFalse((ROOT / "lumibot").exists(), "./lumibot shadows the upstream package")

    def test_upstream_lumibot_resolves_from_installed_distribution(self) -> None:
        spec = importlib.util.find_spec("lumibot")
        if spec is None:
            self.skipTest("upstream lumibot not installed in this interpreter")
        origin = Path(spec.origin or next(iter(spec.submodule_search_locations)))
        installed = distribution("lumibot").locate_file("lumibot/__init__.py")
        self.assertEqual(origin.resolve(), Path(installed).resolve())
        self.assertFalse(origin.resolve().is_relative_to(ROOT / "lumibot"))
