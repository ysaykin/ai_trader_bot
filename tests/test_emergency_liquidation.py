"""Offline checks for emergency liquidation failure handling."""

from unittest.mock import patch

from dashboard.broker_client import TradierClient


def test_cancel_timeout_is_failure() -> None:
    client = TradierClient("token", "account")
    working = [{"id": "1", "status": "open"}]
    with patch.object(client, "_fetch_all_orders", return_value=working), \
         patch("dashboard.broker_client.requests.delete") as delete, \
         patch("dashboard.broker_client.time.monotonic", side_effect=[0, 4]):
        delete.return_value.status_code = 200
        assert client.emergency_cancel_orders() is False


def test_flatten_stops_if_cancellation_unverified() -> None:
    client = TradierClient("token", "account")
    with patch.object(client, "emergency_cancel_orders", return_value=False), \
         patch.object(client, "_fetch_all_positions") as positions, \
         patch("dashboard.broker_client.requests.post") as post:
        assert client.emergency_flatten_positions() is False
        positions.assert_not_called()
        post.assert_not_called()


def test_flatten_timeout_cannot_report_success_from_positions_alone() -> None:
    client = TradierClient("token", "account")
    positions = [{"symbol": "ABC", "quantity": 1}]
    with patch.object(client, "emergency_cancel_orders", return_value=True), \
         patch.object(client, "_fetch_all_positions", side_effect=[positions, []]), \
         patch("dashboard.broker_client.requests.post") as post, \
         patch("dashboard.broker_client.time.monotonic", side_effect=[0, 11]):
        post.return_value.status_code = 200
        post.return_value.json.return_value = {}
        assert client.emergency_flatten_positions() is False
        assert "__verification__" in client.flatten_failures


def test_failed_cancel_followed_by_terminal_order_closes_filled_position() -> None:
    client = TradierClient("token", "account")
    working = [{"id": "1", "status": "open"}]
    filled = [{"id": "1", "status": "filled"}]
    position = [{"symbol": "ABC", "quantity": 1}]
    with patch.object(client, "_fetch_all_orders", side_effect=[working, filled, filled]), \
         patch.object(client, "_fetch_all_positions", side_effect=[position, []]), \
         patch("dashboard.broker_client.requests.delete") as delete, \
         patch("dashboard.broker_client.requests.post") as post, \
         patch("dashboard.broker_client.time.sleep"):
        delete.return_value.status_code = 400
        post.return_value.status_code = 200
        post.return_value.json.return_value = {}
        assert client.emergency_flatten_positions()
        post.assert_called_once()


def test_unconfirmed_short_close_preserves_protective_long() -> None:
    client = TradierClient("token", "account")
    positions = [{"symbol": "ABC261120C00100000", "quantity": -1},
                 {"symbol": "ABC261120C00110000", "quantity": 1}]
    with patch.object(client, "emergency_cancel_orders", return_value=True), \
         patch.object(client, "_fetch_all_positions", return_value=positions), \
         patch.object(client, "_fetch_all_orders", return_value=[]), \
         patch("dashboard.broker_client.requests.post") as post, \
         patch("dashboard.broker_client.time.monotonic", side_effect=[0, 2]):
        post.return_value.status_code = 200
        post.return_value.json.return_value = {}
        assert not client.emergency_flatten_positions(timeout_seconds=1)
        assert post.call_count == 1
        assert post.call_args.kwargs["data"]["side"] == "buy_to_close"
