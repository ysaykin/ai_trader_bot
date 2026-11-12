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
    def test_explicit_broker_construction_reuses_its_data_source_in_every_mode(self) -> None:
        environment = dict(os.environ)
        environment.pop("LUMIBOT_LAZY_CREDENTIALS", None)
        environment.update(
            PYTHONPATH=str(ROOT), PYTHON_DOTENV_DISABLED="1", TRADING_BROKER="tradier",
            TRADIER_ACCESS_TOKEN="test-token", TRADIER_ACCOUNT_NUMBER="test-account",
            TRADIER_PAPER="true", DATA_SOURCE="tradier", LUMIBOT_TELEMETRY="0",
            ML_POLICY_ENABLED="false",
        )
        code = """
from unittest.mock import Mock, patch
import main
from lumibot.brokers.broker import Broker
from lumibot.brokers.tradier import Tradier

source = Mock(SOURCE='TRADIER')
broker = Tradier.__new__(Tradier)
Broker.__init__(broker, name='Tradier', data_source=source,
                connect_stream=False, start_orders_thread=False)
broker._get_balances_at_broker = Mock(return_value=(100000, 0, 100000))
with patch('lumibot.strategies._strategy.get_default_data_source',
           side_effect=AssertionError('Explicit broker resolved default credentials')) as default_source:
    for mode in ('single', 'ab', 'incubator'):
        strategies = main.build_strategies_for_mode(broker, mode=mode)
        assert strategies[0]._data_source is source
        assert all(strategy.broker is broker for strategy in strategies)
        assert broker.data_source is source
        strategies[0]._position_ledger.close()
    default_source.assert_not_called()
"""
        with tempfile.TemporaryDirectory() as temporary:
            result = subprocess.run(
                [sys.executable, "-c", code], cwd=temporary, env=environment,
                capture_output=True, text=True, timeout=30, check=False,
            )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

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
