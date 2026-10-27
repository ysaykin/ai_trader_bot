"""Guard against a local package shadowing the upstream Lumibot dependency."""

import importlib.util
from importlib.metadata import distribution
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def test_no_local_lumibot_directory_shadows_upstream() -> None:
    assert not (ROOT / "lumibot").exists(), "./lumibot shadows the upstream package"


def test_upstream_lumibot_resolves_from_installed_distribution() -> None:
    spec = importlib.util.find_spec("lumibot")
    if spec is None:
        pytest.skip("upstream lumibot not installed in this interpreter")
    origin = Path(spec.origin or next(iter(spec.submodule_search_locations)))
    installed = distribution("lumibot").locate_file("lumibot/__init__.py")
    assert origin.resolve() == Path(installed).resolve()
    assert not origin.resolve().is_relative_to(ROOT / "lumibot")
