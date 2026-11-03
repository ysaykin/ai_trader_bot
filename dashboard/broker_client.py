"""Unified Broker Client for Lumibot Trading Dashboard.

Supports both Tradier (Sandbox/Live) and Alpaca (Paper/Live).
"""
import os
import re
import math
import time
import logging
from datetime import datetime
from typing import Dict, Any, List, Optional
import requests
from dotenv import load_dotenv
from dashboard.presentation import option_mark

load_dotenv()
logger = logging.getLogger(__name__)


def _env_bool(*names: str, default: bool = True) -> bool:
    """Parse environment boolean with canonical precedence, matching main.py."""
    for name in names:
        raw = os.getenv(name)
        if raw is None or not raw.strip():
            continue
        normalized = raw.strip().lower()
        if normalized in ("true", "1", "yes", "on"):
            return True
        if normalized in ("false", "0", "no", "off"):
            return False
        raise ValueError(f"{name} must be true, false, 1, 0, yes, no, on, or off.")
    return default


class TradierClient:
    """Direct Tradier REST client for dashboard telemetry."""

    def __init__(self, api_key: str, account: str, sandbox: bool = True):
        self.api_key = api_key.strip()
        self.account = account.strip()
        self.sandbox = sandbox
        self.base_url = "https://sandbox.tradier.com/v1" if sandbox else "https://api.tradier.com/v1"
        self.headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Accept": "application/json",
        }
        self.flatten_failures: Dict[str, str] = {}
        self.submitted_emergency_orders: Dict[str, Any] = {}

    def _reads(self):
        from strategies.broker_reads import get_broker_reads
        return get_broker_reads(self.api_key, self.account, self.sandbox)

    def _get(self, url: str, *, headers=None, params=None, timeout=6, fresh=False):
        from strategies.broker_reads import fresh_broker_reads
        endpoint = url.removeprefix(self.base_url)
        if fresh:
            with fresh_broker_reads():
                payload = self._reads().get(endpoint, params, fresh=True, caller="recovery", timeout=timeout)
        else:
            payload = self._reads().get(endpoint, params, caller="legacy-dashboard", timeout=timeout)
        response = requests.Response()
        response.status_code = 200
        import json
        response._content = json.dumps(payload).encode()
        return response

    def _submit_once(self, method: str, url: str, **kwargs) -> Any:
        """Submit a mutation once; ambiguous outcomes require reconciliation."""
        func = getattr(requests, method.lower(), None)
        def send():
            if callable(func):
                return func(url, **kwargs)
            return requests.request(method, url, **kwargs)
        return self._reads().write(send, caller="recovery")

    def fetch_account_info(self) -> Optional[Dict[str, Any]]:
        """Fetch balances, equity, cash, and option buying power."""
        try:
            url = f"{self.base_url}/accounts/{self.account}/balances"
            res = self._get(url, headers=self.headers, timeout=6)
            if res.status_code != 200:
                logger.error(f"Tradier balances error {res.status_code}: {res.text}")
                return None
            data = res.json().get("balances", {})
            equity = float(data.get("total_equity", data.get("equity", 100000.0)))
            cash = float(data.get("total_cash", 100000.0))
            margin = data.get("margin", {})
            buying_power = float(margin.get("option_buying_power", data.get("option_buying_power", 100000.0)))
            return {
                "broker": "Tradier Sandbox" if self.sandbox else "Tradier Live",
                "account_number": self.account,
                "status": "ACTIVE",
                "equity": equity,
                "cash": cash,
                "buying_power": buying_power,
                "last_equity": equity,
                "currency": "USD",
                "day_pl": 0.0,
            }
        except Exception as e:
            logger.error(f"Tradier fetch_account_info exception: {e}")
            return None

    def fetch_market_clock(self) -> Optional[Dict[str, Any]]:
        """Fetch live market clock and state from Tradier."""
        try:
            url = f"{self.base_url}/markets/clock"
            res = self._get(url, headers=self.headers, timeout=6)
            if res.status_code == 200:
                clk = res.json().get("clock", {})
                state = clk.get("state", "closed").lower()
                is_open = state == "open"
                desc = clk.get("description", "Market is open" if is_open else "Market is closed")
                return {
                    "is_open": is_open,
                    "state": state,
                    "description": desc,
                    "next_change": clk.get("next_change"),
                    "next_state": clk.get("next_state"),
                }
        except Exception as e:
            logger.warning(f"Tradier clock exception: {e}")

        # Fallback heuristic using local time
        now = datetime.now()
        is_open = now.weekday() < 5 and (9, 30) <= (now.hour, now.minute) <= (16, 0)
        return {
            "is_open": is_open,
            "state": "open" if is_open else "closed",
            "description": "Market is open" if is_open else "Market is closed",
            "next_change": "16:00" if is_open else "09:30",
            "next_state": "closed" if is_open else "open",
        }

    def fetch_quotes(self, symbols: List[str]) -> Dict[str, float]:
        """Fetch latest quotes for given symbols."""
        try:
            url = f"{self.base_url}/markets/quotes"
            res = self._get(url, headers=self.headers, params={"symbols": ",".join(symbols)}, timeout=6)
            if res.status_code == 200:
                data = res.json().get("quotes", {}).get("quote", [])
                if isinstance(data, dict):
                    data = [data]
                return {q["symbol"]: float(q.get("last", 0.0) or q.get("prevclose", 0.0)) for q in data if "symbol" in q}
        except Exception as e:
            logger.warning(f"Tradier quotes exception: {e}")
        return {}

    def fetch_option_greeks(self, symbol: str, expiration: str) -> Dict[str, Dict[str, float]]:
        """Fetch real options Greeks from Tradier API for active strikes."""
        try:
            url = f"{self.base_url}/markets/options/chains"
            res = self._get(url, headers=self.headers, params={"symbol": symbol, "expiration": expiration, "greeks": "true"}, timeout=6)
            if res.status_code == 200:
                opts = res.json().get("options", {}).get("option", [])
                if isinstance(opts, dict):
                    opts = [opts]
                greeks_by_symbol = {}
                for o in opts:
                    sym = o.get("symbol")
                    g = o.get("greeks", {})
                    if sym and g:
                        greeks_by_symbol[sym] = {
                            "delta": float(g.get("delta", 0.0) or 0.0),
                            "theta": float(g.get("theta", 0.0) or 0.0),
                            "mid_iv": float(g.get("mid_iv", 0.0) or 0.0),
                            "bid": float(o.get("bid", 0.0) or 0.0),
                            "ask": float(o.get("ask", 0.0) or 0.0),
                            "last": float(o.get("last", 0.0) or 0.0),
                        }
                return greeks_by_symbol
        except Exception as e:
            logger.warning(f"Tradier fetch_option_greeks exception: {e}")
        return {}

    def fetch_positions(self) -> List[Dict[str, Any]]:
        """Fetch open positions with real-time mark-to-market pricing and Greeks."""
        try:
            url = f"{self.base_url}/accounts/{self.account}/positions"
            res = self._get(url, headers=self.headers, timeout=6)
            if res.status_code == 200:
                pos_data = res.json().get("positions", {})
                if pos_data == "null" or not pos_data:
                    return []
                position_list = pos_data.get("position", [])
                if isinstance(position_list, dict):
                    position_list = [position_list]
                if not position_list:
                    return []

                # Fetch real-time quotes and Greeks for all open option symbols
                symbols = [str(p.get("symbol")) for p in position_list if p.get("symbol")]
                quotes_map: Dict[str, Dict[str, Any]] = {}
                if symbols:
                    try:
                        q_url = f"{self.base_url}/markets/quotes"
                        q_res = self._get(
                            q_url,
                            headers=self.headers,
                            params={"symbols": ",".join(symbols), "greeks": "true"},
                            timeout=6,
                        )
                        if q_res.status_code == 200:
                            q_data = q_res.json().get("quotes", {}).get("quote", [])
                            if isinstance(q_data, dict):
                                q_data = [q_data]
                            for q in q_data:
                                if "symbol" in q:
                                    quotes_map[q["symbol"]] = q
                    except Exception as q_err:
                        logger.warning(f"Tradier fetch_positions quotes error: {q_err}")

                formatted = []
                for p in position_list:
                    sym = p.get("symbol")
                    qty = float(p.get("quantity", 0))
                    cost_basis = float(p.get("cost_basis", 0))
                    q = quotes_map.get(sym, {})
                    bid = q.get("bid")
                    ask = q.get("ask")
                    last = q.get("last")

                    mark = option_mark(bid, ask, last)
                    market_value = qty * mark * 100.0 if mark is not None else None
                    # Gain/Loss = Market Value - Cost Basis (holds for both long and short per Tradier accounting)
                    unrealized_pl = market_value - cost_basis if market_value is not None else None

                    greeks = q.get("greeks") or {}
                    delta = float(greeks.get("delta")) if greeks.get("delta") is not None else None
                    theta = float(greeks.get("theta")) if greeks.get("theta") is not None else None

                    formatted.append({
                        "symbol": sym,
                        "qty": qty,
                        "quantity": qty,
                        "cost_basis": cost_basis,
                        "mark_available": mark is not None,
                        "current_price": mark,
                        "last_price": mark,
                        "market_value": market_value,
                        "unrealized_pl": unrealized_pl,
                        "delta": delta,
                        "theta": theta,
                        "side": "long" if qty > 0 else "short",
                    })
                return formatted
        except Exception as e:
            logger.warning(f"Tradier fetch_positions exception: {e}")
        return []

    def fetch_recent_orders(self, limit: int = 20) -> List[Dict[str, Any]]:
        """Fetch recent order history."""
        try:
            url = f"{self.base_url}/accounts/{self.account}/orders"
            res = self._get(url, headers=self.headers, timeout=6)
            if res.status_code == 200:
                orders_data = res.json().get("orders", {})
                if orders_data == "null" or not orders_data:
                    return []
                order_list = orders_data.get("order", [])
                if isinstance(order_list, dict):
                    order_list = [order_list]
                return order_list[:limit]
        except Exception as e:
            logger.warning(f"Tradier fetch_recent_orders exception: {e}")
        return []

    def _fetch_all_orders(self) -> List[Dict[str, Any]]:
        """Strict order reader with pagination that raises on non-200 HTTP status."""
        all_orders: List[Dict[str, Any]] = []
        page = 1
        seen_ids = set()
        while True:
            url = f"{self.base_url}/accounts/{self.account}/orders"
            res = self._get(url, headers=self.headers, params={"page": page}, timeout=6, fresh=True)
            if res.status_code != 200:
                raise RuntimeError(f"Failed to fetch orders from Tradier (status {res.status_code}): {res.text}")
            data = res.json()
            if not isinstance(data, dict) or "orders" not in data:
                raise ValueError("Missing orders response")
            orders_data = data["orders"]
            if orders_data is None or orders_data == "null":
                break
            if not isinstance(orders_data, dict) or "order" not in orders_data:
                raise ValueError("Incomplete orders response")
            order_list = orders_data["order"]
            if isinstance(order_list, dict):
                order_list = [order_list]
            if not isinstance(order_list, list):
                raise ValueError("Invalid orders list")
            for order in order_list:
                if not isinstance(order, dict) or not order.get("id") or str(order.get("status", "")).lower() not in {"open", "pending", "partially_filled", "submitted", "calculated", "accepted", "filled", "canceled", "cancelled", "rejected", "expired", "error"}:
                    raise ValueError("Invalid order or unknown status")
            if not order_list:
                break
            new_orders = [o for o in order_list if o.get("id") not in seen_ids]
            if not new_orders:
                break
            for o in new_orders:
                seen_ids.add(o.get("id"))
                all_orders.append(o)
            if len(order_list) < 25:
                break
            page += 1
        return all_orders

    def _fetch_all_positions(self) -> List[Dict[str, Any]]:
        """Strict position reader that raises on non-200 HTTP status."""
        url = f"{self.base_url}/accounts/{self.account}/positions"
        res = self._get(url, headers=self.headers, timeout=6, fresh=True)
        if res.status_code != 200:
            raise RuntimeError(f"Failed to fetch positions from Tradier (status {res.status_code}): {res.text}")
        data = res.json()
        if not isinstance(data, dict) or "positions" not in data:
            raise ValueError("Missing positions response")
        pos_data = data["positions"]
        if pos_data is None or pos_data == "null":
            return []
        if not isinstance(pos_data, dict) or "position" not in pos_data:
            raise ValueError("Incomplete positions response")
        pos_list = pos_data["position"]
        if isinstance(pos_list, dict):
            pos_list = [pos_list]
        if not isinstance(pos_list, list):
            raise ValueError("Invalid positions list")
        for position in pos_list:
            if not isinstance(position, dict) or not position.get("symbol") or "quantity" not in position:
                raise ValueError("Incomplete position")
            quantity = float(position["quantity"])
            if not math.isfinite(quantity) or not quantity.is_integer():
                raise ValueError("Invalid position quantity")
        return pos_list

    def emergency_cancel_orders(self) -> bool:
        """Cancel all working/open orders and confirm termination."""
        try:
            orders = self._fetch_all_orders()
        except Exception as e:
            logger.error(f"Tradier emergency_cancel_orders fetch failure: {e}")
            return False

        working_statuses = {"open", "pending", "partially_filled", "submitted", "calculated", "accepted"}
        working_orders = [o for o in orders if str(o.get("status", "")).lower() in working_statuses]
        if not working_orders:
            return True

        for o in working_orders:
            order_id = o.get("id")
            try:
                url = f"{self.base_url}/accounts/{self.account}/orders/{order_id}"
                r = self._submit_once("delete", url, headers=self.headers, timeout=5)
                if r.status_code not in [200, 204]:
                    logger.warning(f"Failed to cancel order {order_id} (status {r.status_code}): {r.text}")
            except Exception as e:
                logger.warning(f"Exception canceling order {order_id}: {e}")

        deadline = time.monotonic() + 3.0
        while time.monotonic() < deadline:
            try:
                current_orders = self._fetch_all_orders()
                still_working = [o for o in current_orders if str(o.get("status", "")).lower() in working_statuses]
                if not still_working:
                    return True
            except Exception:
                pass
            time.sleep(0.5)

        return False

    def emergency_flatten_positions(self, timeout_seconds: float = 10.0) -> bool:
        """Emergency liquidate all open positions with verified order acceptance and polling."""
        self.flatten_failures = {}

        # 1. Cancel working orders first
        if not self.emergency_cancel_orders():
            self.flatten_failures["__global__"] = "Unable to verify cancellation of working orders"
            return False

        # A prior dispatch without a broker ID cannot be safely matched after a
        # timeout. Keep it unresolved rather than risk placing a duplicate order.
        try:
            from strategies.position_ledger import PositionLedger
            with_ledger = PositionLedger()
            try:
                prior = with_ledger.get_unresolved_emergency_submissions()
                orders = {str(o.get("id")): o for o in self._fetch_all_orders()} if prior else {}
                for submission in prior:
                    order = orders.get(str(submission.get("broker_id"))) if submission.get("broker_id") else None
                    status = str(order.get("status", "")).lower() if order else ""
                    if status not in {"filled", "canceled", "cancelled", "expired", "rejected", "failed"}:
                        self.flatten_failures["__reconciliation__"] = "Prior emergency submission remains unresolved"
                        return False
                    fills = int(float(order.get("executed_quantity") or order.get("filled_quantity") or 0))
                    with_ledger.update_emergency_submission(submission["intent_id"], status=status,
                                                           cumulative_fills=fills)
            finally:
                with_ledger.close()
        except Exception as exc:
            self.flatten_failures["__reconciliation__"] = f"Cannot reconcile emergency submissions: {exc}"
            return False

        # 2. Re-read positions to capture fills during cancellation
        try:
            positions = self._fetch_all_positions()
        except Exception as e:
            self.flatten_failures["__global__"] = f"Failed to fetch positions: {e}"
            logger.error(f"Tradier emergency_flatten_positions error: {e}")
            return False

        occ_pattern = re.compile(r"^([A-Za-z]{1,6})\d{6}[CP]\d{8}$")
        equity_pattern = re.compile(r"^[A-Za-z]{1,6}$")

        short_options = []
        long_options = []
        short_equities = []
        long_equities = []

        for p in positions:
            sym = str(p.get("symbol", "")).strip()
            qty = float(p.get("quantity", 0))
            if qty == 0:
                continue

            occ_match = occ_pattern.match(sym)
            if occ_match:
                underlying = occ_match.group(1).upper()
                if qty < 0:
                    short_options.append((sym, underlying, abs(int(qty)), "buy_to_close"))
                else:
                    long_options.append((sym, underlying, abs(int(qty)), "sell_to_close"))
            elif equity_pattern.match(sym):
                if qty < 0:
                    short_equities.append((sym, sym, abs(int(qty)), "buy_to_cover"))
                else:
                    long_equities.append((sym, sym, abs(int(qty)), "sell"))
            else:
                self.flatten_failures[sym] = "Unsupported instrument format"
                logger.error(f"Unsupported instrument in emergency flatten: {sym}")

        if self.flatten_failures:
            return False

        # Close short legs first, then long protective legs
        close_batches = [
            ("option", short_options),
            ("equity", short_equities),
            ("option", long_options),
            ("equity", long_equities),
        ]

        for batch_index, (instrument_type, batch) in enumerate(close_batches):
            if batch_index == 2 and short_options:
                if self.flatten_failures:
                    return False
                deadline = time.monotonic() + max(timeout_seconds, 1.0)
                short_symbols = {item[0] for item in short_options}
                while time.monotonic() < deadline:
                    try:
                        remaining = self._fetch_all_positions()
                        orders = self._fetch_all_orders()
                        if not any(p["symbol"] in short_symbols and float(p["quantity"]) < 0 for p in remaining) and not any(str(o["status"]).lower() in {"open", "pending", "partially_filled", "submitted", "calculated", "accepted"} for o in orders):
                            break
                    except Exception as exc:
                        logger.warning("Short close verification failed: %s", exc)
                    time.sleep(0.5)
                else:
                    self.flatten_failures["__verification__"] = "Short option closes not confirmed; protection retained"
                    return False
            if self.flatten_failures:
                return False
            for item in batch:
                sym, underlying_or_sym, abs_qty, side = item
                if instrument_type == "option":
                    data = {
                        "class": "option",
                        "symbol": underlying_or_sym,
                        "option_symbol": sym,
                        "side": side,
                        "quantity": str(abs_qty),
                        "type": "market",
                        "duration": "day",
                    }
                else:
                    data = {
                        "class": "equity",
                        "symbol": underlying_or_sym,
                        "side": side,
                        "quantity": str(abs_qty),
                        "type": "market",
                        "duration": "day",
                    }
                if instrument_type == "option" and underlying_or_sym in {"SPX", "SPXW", "XSP"}:
                    try:
                        quote_response = self._get(
                            f"{self.base_url}/markets/quotes", headers=self.headers,
                            params={"symbols": sym}, timeout=6, fresh=True,
                        )
                        quote_response.raise_for_status()
                        raw_quotes = quote_response.json().get("quotes", {})
                        quote = raw_quotes.get("quote", {})
                        if isinstance(quote, list):
                            quote = quote[0] if quote else {}
                        elif not isinstance(quote, dict):
                            quote = {}
                        price = float(quote.get("ask" if side == "buy_to_close" else "bid") or 0)
                        if not math.isfinite(price) or price <= 0:
                            if side == "sell_to_close":
                                price = 0.05
                            else:
                                raise ValueError("No executable quote")
                        price = round(round(price / 0.05) * 0.05, 2)
                        if price <= 0:
                            price = 0.05
                        data.update(type="limit", price=f"{price:.2f}")
                    except Exception as exc:
                        if side == "sell_to_close":
                            data.update(type="limit", price="0.05")
                        else:
                            self.flatten_failures[sym] = f"Cannot price index option close: {exc}"
                            continue
                intent_id = f"emerg_{sym}_{int(time.time() * 1000)}"
                ledger = None
                try:
                    from strategies.position_ledger import PositionLedger
                    ledger = PositionLedger()
                    limit_p = float(data["price"]) if "price" in data else None
                    ledger.persist_emergency_intent(
                        intent_id, sym, side, abs_qty, price=limit_p, payload=data
                    )
                except Exception as exc:
                    self.flatten_failures[sym] = f"Cannot persist emergency intent: {exc}"
                    if ledger:
                        ledger.close()
                    continue

                broker_id = None
                try:
                    url = f"{self.base_url}/accounts/{self.account}/orders"
                    res = self._submit_once("post", url, headers=self.headers, data=data, timeout=6)
                    if res.status_code not in (200, 201):
                        self.flatten_failures[sym] = f"Order rejected (status {res.status_code})"
                        logger.error(f"Tradier reject for {sym}: {res.status_code} {res.text}")
                        if ledger:
                            ledger.update_emergency_submission(intent_id, status="rejected", last_error=f"HTTP {res.status_code}")
                    else:
                        res_json = res.json()
                        if not isinstance(res_json, dict):
                            res_json = {}
                        if "errors" in res_json:
                            self.flatten_failures[sym] = f"Broker errors: {res_json['errors']}"
                            if ledger:
                                ledger.update_emergency_submission(intent_id, status="rejected", last_error=str(res_json["errors"]))
                        elif "fault" in res_json or "error" in res_json:
                            err_msg = str(res_json.get("fault") or res_json.get("error"))
                            self.flatten_failures[sym] = f"Broker error: {err_msg}"
                            if ledger:
                                ledger.update_emergency_submission(intent_id, status="rejected", last_error=err_msg)
                        else:
                            order_info = res_json.get("order") if isinstance(res_json.get("order"), dict) else {}
                            broker_id = str(order_info.get("id")) if order_info.get("id") else None
                            order_stat = str(order_info.get("status", "ok")).lower()
                            if not broker_id or order_stat not in {"ok", "open", "pending", "submitted", "accepted", "partially_filled", "filled"}:
                                self.flatten_failures[sym] = "Broker acceptance could not be verified"
                                ledger.update_emergency_submission(intent_id, broker_id=broker_id, status="unresolved", last_error=str(order_info))
                            elif order_stat in ("rejected", "error"):
                                self.flatten_failures[sym] = f"Broker order rejected: {order_stat}"
                                if ledger:
                                    ledger.update_emergency_submission(intent_id, broker_id=broker_id, status="rejected", last_error=order_stat)
                            else:
                                self.submitted_emergency_orders[sym] = {
                                    "intent_id": intent_id,
                                    "broker_id": broker_id,
                                    "symbol": sym,
                                    "side": side,
                                    "quantity": abs_qty,
                                    "status": "submitted",
                                }
                                if ledger:
                                    ledger.update_emergency_submission(intent_id, broker_id=broker_id, status="submitted")
                except Exception as exc:
                    self.flatten_failures[sym] = f"Order exception/ambiguous: {exc}"
                    logger.error(f"Exception submitting close order for {sym}: {exc}")
                    if ledger:
                        ledger.update_emergency_submission(intent_id, status="unresolved", last_error=str(exc))
                finally:
                    if ledger:
                        try:
                            ledger.close()
                        except Exception:
                            pass

        if self.flatten_failures:
            return False

        # Poll until verified flat with zero working orders
        working_statuses = {"open", "pending", "partially_filled", "submitted", "calculated", "accepted"}
        deadline = time.monotonic() + max(timeout_seconds, 1.0)
        while time.monotonic() < deadline:
            time.sleep(0.5)
            try:
                remaining_positions = [
                    p for p in self._fetch_all_positions()
                    if float(p.get("quantity", 0)) != 0
                ]
                working_orders = [
                    o for o in self._fetch_all_orders()
                    if str(o.get("status", "")).lower() in working_statuses
                ]
                for o in working_orders:
                    o_id = str(o.get("id", ""))
                    fills = int(float(o.get("executed_quantity") or o.get("filled_quantity") or 0))
                    for tracked in self.submitted_emergency_orders.values():
                        if tracked.get("broker_id") == o_id:
                            tracked["cumulative_fills"] = fills
                if not remaining_positions and not working_orders:
                    return True
            except Exception as poll_err:
                logger.warning(f"Polling verification exception: {poll_err}")

        try:
            final_positions = [
                p for p in self._fetch_all_positions()
                if float(p.get("quantity", 0)) != 0
            ]
            for p in final_positions:
                sym = str(p.get("symbol", "unknown"))
                self.flatten_failures[sym] = f"Position still open: qty {p.get('quantity')}"
        except Exception as e:
            self.flatten_failures["__verification__"] = f"Verification failed: {e}"

        self.flatten_failures.setdefault("__verification__", "Unable to verify flat positions and no working orders")
        return False


def get_broker_client():
    """Instantiate and return the active broker client (Tradier or Alpaca)."""
    broker_type = os.getenv("TRADING_BROKER", "").strip().lower()
    if broker_type not in ("", "tradier", "alpaca"):
        return None

    # 1. Check Tradier
    tradier_key = (os.getenv("TRADIER_ACCESS_TOKEN") or os.getenv("TRADIER_API_KEY") or "").strip()
    account = (os.getenv("TRADIER_ACCOUNT_NUMBER") or os.getenv("TRADIER_ACCOUNT") or "").strip()

    if broker_type == "tradier" or (not broker_type and tradier_key):
        if not tradier_key or tradier_key.startswith("your_") or not account or account.startswith("your_"):
            return None
        sandbox = _env_bool("TRADIER_PAPER", "TRADIER_IS_PAPER", "TRADIER_SANDBOX", default=True)
        return TradierClient(api_key=tradier_key, account=account, sandbox=sandbox)

    # 2. Check Alpaca
    alpaca_key = (os.getenv("ALPACA_API_KEY") or "").strip()
    alpaca_secret = (os.getenv("ALPACA_API_SECRET") or "").strip()
    if (broker_type == "alpaca" or not broker_type) and alpaca_key and alpaca_secret and not alpaca_key.startswith("your_"):
        try:
            from alpaca_client import get_client
            client = get_client()
            if client:
                return client
        except ImportError:
            pass

    return None
