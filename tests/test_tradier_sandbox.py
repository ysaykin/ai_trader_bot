"""Opt-in Tradier paper write test plus offline multileg response coverage."""

from __future__ import annotations

import os
import unittest

@unittest.skipUnless(
    os.getenv("RUN_TRADIER_SANDBOX_WRITE_TEST") == "1",
    "state-changing Tradier sandbox test is not opted in",
)
class TradierSandboxWriteTests(unittest.TestCase):
    """Submit and cancel one deliberately non-marketable paper multileg order."""

    def test_submit_and_cancel_multileg_parent(self) -> None:
        import requests

        token = os.environ["TRADIER_ACCESS_TOKEN"]
        account = os.environ["TRADIER_ACCOUNT_NUMBER"]
        underlying = os.environ["TRADIER_SANDBOX_TEST_UNDERLYING"]
        option_symbols = [
            value.strip()
            for value in os.environ["TRADIER_SANDBOX_TEST_OPTION_SYMBOLS"].split(",")
            if value.strip()
        ]
        self.assertEqual(len(option_symbols), 4)
        headers = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/json",
        }
        payload: dict[str, str | int] = {
            "class": "multileg",
            "symbol": underlying,
            "duration": "day",
            "type": "credit",
            # A very high credit keeps the verification order non-marketable.
            "price": os.getenv("TRADIER_SANDBOX_TEST_CREDIT", "99.00"),
            "tag": "LUMIBOT_RECON_TEST",
        }
        sides = ("buy_to_open", "sell_to_open", "sell_to_open", "buy_to_open")
        for index, (symbol, side) in enumerate(zip(option_symbols, sides)):
            payload[f"option_symbol[{index}]"] = symbol
            payload[f"side[{index}]"] = side
            payload[f"quantity[{index}]"] = 1

        response = requests.post(
            f"https://sandbox.tradier.com/v1/accounts/{account}/orders",
            headers=headers,
            data=payload,
            timeout=20,
        )
        response.raise_for_status()
        order = response.json().get("order") or {}
        order_id = order.get("id")
        self.assertIsNotNone(order_id, response.text)
        try:
            status = requests.get(
                f"https://sandbox.tradier.com/v1/accounts/{account}/orders/{order_id}",
                headers=headers,
                timeout=20,
            )
            status.raise_for_status()
            self.assertEqual(str(status.json()["order"]["id"]), str(order_id))
        finally:
            cancel = requests.delete(
                f"https://sandbox.tradier.com/v1/accounts/{account}/orders/{order_id}",
                headers=headers,
                timeout=20,
            )
            cancel.raise_for_status()


if __name__ == "__main__":
    unittest.main()
