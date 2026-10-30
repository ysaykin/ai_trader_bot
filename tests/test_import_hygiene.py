"""Guard against a local package shadowing the upstream Lumibot dependency."""

import importlib.util
from importlib.metadata import distribution
from pathlib import Path
import unittest

try:
    import pytest
except ImportError:
    pytest = None

ROOT = Path(__file__).resolve().parents[1]


class TestImportHygiene(unittest.TestCase):
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
