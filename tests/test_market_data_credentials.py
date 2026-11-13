"""Real-time market credentials cannot change paper account routing."""
from unittest.mock import Mock

from strategies.broker_reads import BrokerReads
from test_broker_snapshots import response


def test_live_market_reads_keep_account_reads_in_sandbox(tmp_path, monkeypatch):
    monkeypatch.setenv("TRADIER_MARKET_DATA_TOKEN", "live-data-token")
    transport = Mock(side_effect=[response({"quotes": {"quote": {"symbol": "SPY", "last": 100}}}),
                                  response({"positions": None})])
    reads = BrokerReads("paper-token", "paper-account", sandbox=True,
                        path=tmp_path / "snapshots.sqlite3", transport=transport)
    reads.get("/markets/quotes", {"symbols": "SPY"}, fresh=True)
    reads.get("/accounts/paper-account/positions", fresh=True)
    calls = transport.call_args_list
    assert calls[0].args[1].startswith("https://api.tradier.com/v1/markets/")
    assert calls[0].kwargs["headers"]["Authorization"] == "Bearer live-data-token"
    assert calls[1].args[1].startswith("https://sandbox.tradier.com/v1/accounts/")
    assert calls[1].kwargs["headers"]["Authorization"] == "Bearer paper-token"
    assert reads.sandbox is True


def test_market_credential_file_rotates_snapshot_identity(tmp_path, monkeypatch):
    credential = tmp_path / "market-token"
    credential.write_text("live-data-token\n")
    original = BrokerReads("paper-token", "paper-account", path=tmp_path / "snapshots.sqlite3")
    monkeypatch.setenv("TRADIER_MARKET_DATA_TOKEN_FILE", str(credential))
    live = BrokerReads("paper-token", "paper-account", path=tmp_path / "snapshots.sqlite3")
    assert live.market_headers["Authorization"] == "Bearer live-data-token"
    assert live.account_key != original.account_key
    assert "live-data-token" not in repr(live.metrics())
