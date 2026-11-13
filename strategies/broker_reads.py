"""Shared Tradier read snapshots. Display retention is separate from trading freshness.

The SQLite file belongs to telemetry, not the position ownership ledger. All
network I/O happens outside transactions. Leases fence late publishers after
takeover, and quota reservations count physical attempts across processes.
"""
from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
import hashlib
from importlib.metadata import PackageNotFoundError, version
import json
import logging
import math
import os
from pathlib import Path
import random
import re
import sqlite3
import threading
import time
import uuid
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import requests

log = logging.getLogger(__name__)
try:
    EASTERN = ZoneInfo("America/New_York")
except ZoneInfoNotFoundError:
    EASTERN = None
_fresh = ContextVar("fresh_broker_reads", default=False)
_priority = ContextVar("broker_read_priority", default="background")
_sdk_error = ContextVar("broker_sdk_read_error", default=None)
ACTIVE = {"pending", "open", "partially_filled", "pending_cancel", "accepted", "held"}


class BrokerReadUnavailable(requests.RequestException):
    """No successful broker response meets the requested freshness/deadline."""


@contextmanager
def fresh_broker_reads():
    """Execution boundary: require new evidence and use reserved quota."""
    fresh_token = _fresh.set(True)
    priority_token = _priority.set("execution")
    try:
        yield
    finally:
        _priority.reset(priority_token)
        _fresh.reset(fresh_token)


def _rows(payload, root, child):
    value = payload.get(root)
    if value in (None, "null"):
        return []
    if not isinstance(value, dict):
        return []
    child_val = value.get(child)
    if isinstance(child_val, list):
        return [item for item in child_val if isinstance(item, dict)]
    if isinstance(child_val, dict):
        return [child_val]
    return []


def _stamp(value):
    return datetime.fromtimestamp(value, timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC") if value else "No successful sync"


class BrokerReads:
    def __init__(self, token, account, sandbox=True, *, path=None, transport=None,
                 clock=time.time, sleep=time.sleep, jitter=random.random, market_open=None):
        from dotenv import dotenv_values
        settings = {**dotenv_values(Path(__file__).resolve().parents[1] / ".env"), **os.environ}
        token, account = token.strip(), account.strip()
        if not token or not account:
            raise ValueError("Broker token and account are required")
        self.token, self.account, self.sandbox = token, account, sandbox
        self.base_url = "https://sandbox.tradier.com/v1" if sandbox else "https://api.tradier.com/v1"
        self.headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
        market_token = str(settings.get("TRADIER_MARKET_DATA_TOKEN") or "").strip()
        market_file = str(settings.get("TRADIER_MARKET_DATA_TOKEN_FILE") or "").strip()
        if market_token and market_file:
            raise ValueError("Configure a market-data token or token file, not both")
        if market_file:
            market_token = Path(market_file).expanduser().read_text().strip()
            if not market_token:
                raise ValueError("Market-data credential file is empty")
        market_paper = str(settings.get("TRADIER_MARKET_DATA_PAPER") or "false").strip().lower()
        if market_paper not in {"true", "false", "1", "0", "yes", "no", "on", "off"}:
            raise ValueError("TRADIER_MARKET_DATA_PAPER must be a boolean")
        self.market_sandbox = market_paper in {"true", "1", "yes", "on"} if market_token else sandbox
        self.market_base_url = "https://sandbox.tradier.com/v1" if self.market_sandbox else "https://api.tradier.com/v1"
        self.market_headers = {"Authorization": f"Bearer {market_token or token}", "Accept": "application/json"}
        self.scope = hashlib.sha256(f"{sandbox}:{token}".encode()).hexdigest()
        if market_token:
            self.scope = hashlib.sha256(f"{self.scope}:market:{self.market_sandbox}:{market_token}".encode()).hexdigest()
        self.account_key = hashlib.sha256(f"{self.scope}:{account}".encode()).hexdigest()
        from strategies.runtime_records import (
            is_protected_runtime_path,
            is_test_mode_active,
            resolve_record_path,
        )
        if path is not None:
            candidate = Path(path).expanduser()
            if not candidate.is_absolute():
                candidate = Path(__file__).resolve().parents[1] / candidate
            resolved = candidate.resolve()
            if is_test_mode_active() and is_protected_runtime_path(resolved):
                raise PermissionError(
                    f"Explicit path to protected broker snapshots '{resolved}' is rejected in test mode."
                )
            self.path = resolved
        else:
            self.path = resolve_record_path("BROKER_SNAPSHOT_PATH", "logs/broker_snapshots.sqlite3")
        self.clock, self.sleep, self.jitter = clock, sleep, jitter
        self.market_open = market_open or self._weekday_session
        self.transport = transport or requests.Session().request
        self._owner_uuid = uuid.uuid4().hex
        self.intervals = {name: max(1, float(settings.get(env) or default)) for name, env, default in (
            ("balances", "BROKER_BALANCES_INTERVAL", 30), ("positions", "BROKER_POSITIONS_INTERVAL", 15),
            ("orders_idle", "BROKER_ORDERS_IDLE_INTERVAL", 30), ("orders_active", "BROKER_ORDERS_ACTIVE_INTERVAL", 5),
            ("quotes", "BROKER_QUOTES_INTERVAL", 5))}
        self.display_max_age = max(1, float(settings.get("BROKER_DISPLAY_MAX_AGE") or 300))
        if any(not math.isfinite(value) for value in (*self.intervals.values(), self.display_max_age)):
            raise ValueError("Broker intervals and display age must be finite")
        self._stop = threading.Event()
        self._thread = None
        self._role = "dashboard"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.db() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS snapshots (
                    scope TEXT, key TEXT, payload TEXT, success REAL DEFAULT 0,
                    attempt REAL DEFAULT 0, error TEXT DEFAULT '', retry_at REAL DEFAULT 0,
                    failures INTEGER DEFAULT 0, generation INTEGER DEFAULT 0,
                    owner TEXT DEFAULT '', lease_until REAL DEFAULT 0,
                    PRIMARY KEY(scope,key));
                CREATE TABLE IF NOT EXISTS quotas (
                    scope TEXT, category TEXT, expires REAL, used INTEGER, background INTEGER,
                    allowed INTEGER, cooldown REAL DEFAULT 0, PRIMARY KEY(scope,category));
                CREATE TABLE IF NOT EXISTS attempts (
                    scope TEXT, at REAL, category TEXT, endpoint TEXT, caller TEXT,
                    status INTEGER, latency REAL, retry INTEGER);
                CREATE INDEX IF NOT EXISTS attempts_by_time ON attempts(at);
                CREATE INDEX IF NOT EXISTS attempts_by_scope ON attempts(scope,at);
                CREATE TABLE IF NOT EXISTS counters (
                    scope TEXT, name TEXT, value INTEGER, PRIMARY KEY(scope,name));
                CREATE TABLE IF NOT EXISTS leases (
                    scope TEXT, name TEXT, owner TEXT, expires REAL, PRIMARY KEY(scope,name));
                CREATE TABLE IF NOT EXISTS demand (
                    scope TEXT, symbol TEXT, expires REAL, PRIMARY KEY(scope,symbol));
            """)

    @property
    def owner(self):
        # A prefork web server must not give every worker the master's identity.
        return f"{self._owner_uuid}:{os.getpid()}"

    @contextmanager
    def db(self, write=False):
        db = sqlite3.connect(self.path, timeout=5)
        db.row_factory = sqlite3.Row
        try:
            if write:
                db.execute("BEGIN IMMEDIATE")
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    def count(self, name):
        with self.db(True) as db:
            db.execute("INSERT INTO counters VALUES (?,?,1) ON CONFLICT(scope,name) DO UPDATE SET value=value+1",
                       (self.scope, name))

    def metrics(self):
        versions = {}
        for name in ("lumibot", "lumiwealth-tradier"):
            try:
                versions[name] = version(name)
            except PackageNotFoundError:
                versions[name] = None
        with self.db() as db:
            return {
                "versions": versions,
                "market_data_environment": "paper" if self.market_sandbox else "live",
                "execution_environment": "paper" if self.sandbox else "live",
                "counters": {r["name"]: r["value"] for r in db.execute("SELECT * FROM counters WHERE scope=?", (self.scope,))},
                "attempts": [dict(r) for r in db.execute(
                    "SELECT at,category,endpoint,caller,status,latency,retry FROM attempts WHERE scope=? ORDER BY at", (self.scope,))],
                "quotas": [dict(r) for r in db.execute(
                    "SELECT category,expires,used,background,allowed,cooldown FROM quotas WHERE scope=?", (self.scope,))],
                "resources": {key: {field: value[field] for field in ("status", "age", "synced_at", "error", "generation")}
                              for key, value in self.display().items()},
            }

    def snapshot(self, key, *, max_age=300, ttl=15):
        with self.db() as db:
            row = db.execute("SELECT * FROM snapshots WHERE scope=? AND key=?", (self.account_key, key)).fetchone()
        return self._snapshot(row, max_age, ttl)

    def _snapshot(self, row, max_age, ttl):
        if row is None or row["payload"] is None:
            return {"data": None, "status": "unavailable", "success_at": 0,
                    "synced_at": "No successful sync", "age": None,
                    "error": row["error"] if row else "", "generation": 0}
        age = max(0, self.clock() - row["success"])
        return {"data": json.loads(row["payload"]), "status": "expired" if age > max_age else
                "stale" if row["error"] or age >= ttl else "ready", "success_at": row["success"],
                "synced_at": _stamp(row["success"]), "age": age, "error": row["error"],
                "generation": row["generation"]}

    def display(self):
        """Read a consistent bundle without network I/O or starting a collector."""
        with self.db() as db:
            rows = list(db.execute("SELECT * FROM snapshots WHERE scope=? AND (key IN ('balances','positions','orders') OR key LIKE 'quote:%')", (self.account_key,)))
        orders = next((json.loads(row["payload"]) for row in rows if row["key"] == "orders" and row["payload"]), {})
        active = any(str(row.get("status", "")).lower() in ACTIVE for row in _rows(orders, "orders", "order"))
        return {row["key"]: self._snapshot(row, self.display_max_age, self._ttl(row["key"], active=active)) for row in rows}

    def invalidate(self, *, inventory_only=False):
        # Keep last successful values for display, but force the next fresh read.
        with self.db(True) as db:
            db.execute("UPDATE snapshots SET retry_at=0,error='Refresh required',owner='',lease_until=0 WHERE scope=?"
                       + (" AND (key IN ('balances','positions') OR key LIKE 'quote:%')" if inventory_only else ""), (self.account_key,))

    def demand(self, symbols):
        with self.db(True) as db:
            db.executemany("INSERT INTO demand VALUES (?,?,?) ON CONFLICT(scope,symbol) DO UPDATE SET expires=excluded.expires",
                           [(self.account_key, str(s), self.clock() + 900) for s in set(symbols) if s])

    def _reserve(self, category, priority):
        now = self.clock()
        with self.db(True) as db:
            row = db.execute("SELECT * FROM quotas WHERE scope=? AND category=?", (self.scope, category)).fetchone()
            source_is_paper = self.market_sandbox if category == "market" else self.sandbox
            allowed = 60 if source_is_paper or category == "trading" else 120
            if row is None or now >= row["expires"]:
                db.execute("INSERT OR REPLACE INTO quotas VALUES (?,?,?,0,0,?,?)",
                           (self.scope, category, now + 60, row["allowed"] if row else allowed,
                            row["cooldown"] if row else 0))
                row = db.execute("SELECT * FROM quotas WHERE scope=? AND category=?", (self.scope, category)).fetchone()
            background_cap = row["allowed"] // 2 if category != "trading" else row["allowed"]
            if category == "standard":
                background_cap = min(30, background_cap)
            if now < row["cooldown"] or row["used"] >= row["allowed"] or (
                    priority == "background" and row["background"] >= background_cap):
                raise BrokerReadUnavailable(f"{category} request budget unavailable")
            db.execute("UPDATE quotas SET used=used+1,background=background+? WHERE scope=? AND category=?",
                       (int(priority == "background"), self.scope, category))

    def _record(self, response, *, category, endpoint, caller, started, retry):
        now = self.clock()
        headers = response.headers if response is not None else {}
        with self.db(True) as db:
            db.execute("INSERT INTO attempts VALUES (?,?,?,?,?,?,?,?)", (self.scope, now, category, endpoint,
                       caller, response.status_code if response is not None else 0, max(0, now - started), retry))
            db.execute("DELETE FROM attempts WHERE at<?", (now - 86400,))
            try:
                allowed = int(headers["X-Ratelimit-Allowed"])
                used = int(headers["X-Ratelimit-Used"]) if "X-Ratelimit-Used" in headers else allowed - int(headers["X-Ratelimit-Available"])
                expiry = float(headers["X-Ratelimit-Expiry"]) / 1000
                if allowed > 0 and expiry > now:
                    db.execute("UPDATE quotas SET allowed=?,used=MAX(used,?),expires=? WHERE scope=? AND category=?",
                               (allowed, used, expiry, self.scope, category))
            except (KeyError, TypeError, ValueError):
                pass
            quota = response is not None and (response.status_code == 429 or
                    response.status_code == 400 and "Quota Violation" in response.text)
            if quota:
                match = re.search(r"Expires\s+(\d{12,})", response.text)
                if match and float(match[1]) / 1000 > now:
                    db.execute("UPDATE quotas SET expires=? WHERE scope=? AND category=?", (float(match[1])/1000, self.scope, category))
                # The provider's expiry header is preferable to a new local window.
                db.execute("UPDATE quotas SET cooldown=expires WHERE scope=? AND category=?", (self.scope, category))
            try:
                raw = headers.get("Retry-After", "0")
                try:
                    delay = float(raw)
                except ValueError:
                    delay = parsedate_to_datetime(raw).timestamp() - now
                if delay > 0:
                    db.execute("UPDATE quotas SET cooldown=MAX(cooldown,?) WHERE scope=? AND category=?",
                               (now + delay, self.scope, category))
            except (TypeError, ValueError):
                pass

    def _key(self, endpoint, params):
        tail = endpoint.split(f"/accounts/{self.account}/", 1)[-1]
        if tail in {"balances", "positions", "orders"} and (not params or
                tail == "orders" and set(params) <= {"includeTags", "page"} and str(params.get("page", 1)) == "1"):
            return tail
        digest = hashlib.sha256(json.dumps(params or {}, sort_keys=True).encode()).hexdigest()[:20]
        # Store no account ID or token in keys or request metrics.
        return endpoint.replace(self.account, "{account}") + ":" + digest

    def _ttl(self, key, *, active=None):
        if active is None:
            snap = self.snapshot("orders")
            active = bool(snap["data"] and any(str(r.get("status", "")).lower() in ACTIVE
                                              for r in _rows(snap["data"], "orders", "order")))
        factor = 1 if active or self.market_open() else 4
        if key == "balances":
            return self.intervals["balances"] * factor
        if key == "positions":
            return self.intervals["positions"] * factor
        if key == "orders":
            return self.intervals["orders_active"] if active else self.intervals["orders_idle"] * factor
        return self.intervals["quotes"] * factor if "/orders/" in key or "quote:" in key else 240

    def _weekday_session(self):
        # Scheduling heuristic only. Never authorizes trading or overrides a halt.
        if EASTERN is None:
            return True
        now = datetime.fromtimestamp(self.clock(), EASTERN)
        return now.weekday() < 5 and (9, 30) <= (now.hour, now.minute) < (16, 0)

    def _claim(self, key, deadline, fresh):
        initial_generation = None
        waited = False
        while self.clock() < deadline:
            now = self.clock()
            with self.db(True) as db:
                row = db.execute("SELECT * FROM snapshots WHERE scope=? AND key=?", (self.account_key, key)).fetchone()
                if initial_generation is None:
                    initial_generation = row["generation"] if row else 0
                if row and row["payload"] and not row["error"] and (
                        row["generation"] > initial_generation if fresh else now - row["success"] < self._ttl(key)):
                    return json.loads(row["payload"]), None
                if row and row["retry_at"] > now:
                    raise BrokerReadUnavailable(row["error"] or "Broker read cooling down")
                if row is None or row["lease_until"] <= now:
                    lease = uuid.uuid4().hex
                    db.execute("INSERT OR IGNORE INTO snapshots(scope,key) VALUES (?,?)", (self.account_key, key))
                    db.execute("UPDATE snapshots SET owner=?,lease_until=? WHERE scope=? AND key=?",
                               (lease, deadline + 2, self.account_key, key))
                    return None, lease
            if not waited:
                self.count("coalesced_waiters")
                waited = True
            self.sleep(.02)
        raise BrokerReadUnavailable("Broker read deadline exceeded")

    def _publish(self, key, lease, payload=None, error=""):
        now = self.clock()
        with self.db(True) as db:
            row = db.execute("SELECT * FROM snapshots WHERE scope=? AND key=? AND owner=? AND lease_until>?",
                             (self.account_key, key, lease, now)).fetchone()
            if row is None:
                raise BrokerReadUnavailable("Expired broker read publisher")
            if error:
                failures = row["failures"] + 1
                db.execute("UPDATE snapshots SET attempt=?,error=?,retry_at=?,failures=?,owner='',lease_until=0 WHERE scope=? AND key=?",
                           (now, error, now + min(60, 2 ** min(failures, 6)) + self.jitter(), failures, self.account_key, key))
            else:
                db.execute("UPDATE snapshots SET payload=?,success=?,attempt=?,error='',retry_at=0,failures=0,generation=generation+1,owner='',lease_until=0 WHERE scope=? AND key=?",
                           (json.dumps(payload, allow_nan=False), now, now, self.account_key, key))

    @staticmethod
    def _validate(payload, key):
        if not isinstance(payload, dict) or payload.get("errors"):
            raise BrokerReadUnavailable("Invalid broker response")
        if key == "quotes":
            if not isinstance(payload.get("quotes"), dict) or "quote" not in payload["quotes"]:
                raise BrokerReadUnavailable("Invalid quotes response")
        if key in {"balances", "positions", "orders"}:
            if key not in payload:
                raise BrokerReadUnavailable(f"Missing {key} response")
            value = payload[key]
            if key == "balances":
                if not isinstance(value, dict) or not any(k in value for k in ("total_equity", "equity", "total_cash")):
                    raise BrokerReadUnavailable("Invalid balances response")
                for field in ("total_equity", "equity", "total_cash"):
                    if field in value:
                        try:
                            valid = math.isfinite(float(value[field]))
                        except (TypeError, ValueError):
                            valid = False
                        if not valid:
                            raise BrokerReadUnavailable("Invalid balance value")
            elif value not in (None, "null"):
                child = "position" if key == "positions" else "order"
                if not isinstance(value, dict) or child not in value:
                    raise BrokerReadUnavailable(f"Invalid {key} response")
                raw_child = value[child]
                if not isinstance(raw_child, (dict, list)):
                    raise BrokerReadUnavailable(f"Malformed {key} structure")
                rows = [raw_child] if isinstance(raw_child, dict) else raw_child
                if any(not isinstance(row, dict) for row in rows):
                    raise BrokerReadUnavailable(f"Invalid {key} rows")
                if key == "positions" and any(not row.get("symbol") or "quantity" not in row or "cost_basis" not in row for row in rows):
                    raise BrokerReadUnavailable("Incomplete inventory response")
                if key == "positions":
                    try:
                        if any(not math.isfinite(float(row[field])) for row in rows for field in ("quantity", "cost_basis")):
                            raise ValueError
                    except (TypeError, ValueError):
                        raise BrokerReadUnavailable("Invalid inventory value") from None

    @staticmethod
    def _valid_quote(row):
        if not isinstance(row, dict) or not row.get("symbol"):
            return False
        for key in ("bid", "ask", "last"):
            try:
                if math.isfinite(float(row.get(key))):
                    return True
            except (ValueError, TypeError):
                pass
        return False

    def get(self, endpoint, params=None, *, fresh=None, caller="sdk", timeout=6):
        try:
            self.count("logical_reads")
            endpoint = "/" + endpoint.lstrip("/").removeprefix("v1/")
            if endpoint.startswith("/accounts/") and endpoint.split("/")[2] != self.account:
                raise BrokerReadUnavailable("Broker account scope mismatch")
            params = {str(k): v for k, v in (params or {}).items() if isinstance(k, str)}
            if endpoint.endswith("/orders"):
                # The SDK routes shared-account orders by tag. A collector response
                # without tags must never satisfy its includeTags request.
                params["includeTags"] = True
            if endpoint == "/markets/quotes":
                return self.quotes(str(params.get("symbols", "")).split(","), fresh=fresh, caller=caller)
            key = self._key(endpoint, params)
            deadline = self.clock() + timeout
            for publication_attempt in range(2):
                payload, lease = self._claim(key, deadline, _fresh.get() if fresh is None else fresh)
                if lease is None:
                    self.count("cache_hits")
                    return payload
                category = "market" if endpoint.startswith("/markets/") else "standard"
                try:
                    previous = self.snapshot(key)["data"]
                    payload = self._fetch(endpoint, params, category, caller, deadline)
                    self._validate(payload, key)
                    self._publish(key, lease, payload)
                    if (key == "orders" or "/orders/" in key) and previous is not None and payload != previous:
                        self.invalidate(inventory_only=True)
                    return payload
                except Exception as error:
                    message = str(error) if isinstance(error, BrokerReadUnavailable) else "Broker transport failure"
                    try:
                        self._publish(key, lease, error=message)
                    except BrokerReadUnavailable:
                        # A concurrent order change revokes in-flight inventory
                        # reads. Reacquire once within the original deadline;
                        # never return the response captured before invalidation.
                        if publication_attempt == 0 and self.clock() < deadline:
                            continue
                    raise BrokerReadUnavailable(message) from None
        except sqlite3.Error as error:
            raise BrokerReadUnavailable(f"Broker storage failure: {error}") from None

    def _fetch(self, endpoint, params, category, caller, deadline):
        last = "Broker read failed"
        for retry in range(2):
            if self.clock() >= deadline:
                break
            self._reserve(category, _priority.get())
            started = self.clock()
            response = None
            try:
                market = endpoint.startswith("/markets/")
                base_url = self.market_base_url if market else self.base_url
                headers = self.market_headers if market else self.headers
                response = self.transport("GET", base_url + endpoint, params=params,
                                          headers=headers, timeout=max(.1, deadline - started))
                status = response.status_code
                if status == 200:
                    try:
                        return response.json()
                    except ValueError:
                        raise BrokerReadUnavailable("Malformed broker JSON") from None
                last = f"Broker HTTP {status}"
                if status not in {500, 502, 503, 504, 520, 530}:
                    raise BrokerReadUnavailable(last)
            except (requests.Timeout, requests.ConnectionError):
                last = "Broker transport timeout or connection failure"
            finally:
                self._record(response, category=category, endpoint=endpoint.replace(self.account, "{account}"),
                             caller=caller, started=started, retry=retry)
            delay = .25 + self.jitter() * .25
            if retry or self.clock() + delay >= deadline:
                break
            self.sleep(delay)
        raise BrokerReadUnavailable(last)

    def quotes(self, symbols, *, fresh=None, caller="sdk", register=True):
        symbols = sorted(set(s for s in symbols if s))
        if not symbols:
            return {"quotes": {"quote": []}}
        if register:
            self.demand(symbols)
        # One batch lease handles overlapping symbol sets as well as identical requests.
        deadline = self.clock() + 6
        fresh = _fresh.get() if fresh is None else fresh
        initial = {symbol: self.snapshot("quote:" + symbol)["generation"] for symbol in symbols}
        result = {}
        while self.clock() < deadline:
            with self.db(True) as db:
                lease = db.execute("SELECT * FROM leases WHERE scope=? AND name='quote_batch'", (self.account_key,)).fetchone()
                if lease is None or lease["expires"] <= self.clock():
                    owner = uuid.uuid4().hex
                    db.execute("INSERT OR REPLACE INTO leases VALUES (?,'quote_batch',?,?)",
                               (self.account_key, owner, deadline + 2))
                    break
            self.count("coalesced_quote_waiters")
            self.sleep(.02)
        else:
            raise BrokerReadUnavailable("Quote batch deadline exceeded")
        try:
            needed = []
            for symbol in symbols:
                snap = self.snapshot("quote:" + symbol, ttl=self._ttl("quote:" + symbol))
                if snap["data"] and not snap["error"] and (
                        snap["generation"] > initial[symbol] if fresh else snap["age"] < self._ttl("quote:" + symbol)):
                    result[symbol] = snap["data"]
                else:
                    needed.append(symbol)
            for offset in range(0, len(needed), 100):
                chunk = needed[offset:offset + 100]
                # Share transport failures only. A missing or expired symbol has
                # its own cooldown and must not block quotes for other positions.
                # Use a new key so old persisted partial-batch failures do not
                # retain the account-wide cooldown after this change.
                batch_key = "quote_transport_status"
                _, batch_ticket = self._claim(batch_key, deadline, True)
                tickets = {}
                transport_error = ""
                try:
                    for symbol in chunk:
                        try:
                            value, ticket = self._claim("quote:" + symbol, deadline, True)
                        except BrokerReadUnavailable:
                            # Keep per-symbol cooldowns out of the shared fetch.
                            # The complete request still fails below, after the
                            # other symbols have had a chance to refresh.
                            continue
                        if ticket is None:
                            result[symbol] = value
                        else:
                            tickets[symbol] = ticket
                    if not tickets:
                        self._publish(batch_key, batch_ticket, {})
                        continue
                    try:
                        payload = self._fetch("/markets/quotes", {"symbols": ",".join(tickets), "greeks": "true"}, "market", caller, deadline)
                        self._validate(payload, "quotes")
                    except Exception as error:
                        transport_error = str(error) if isinstance(error, BrokerReadUnavailable) else "Quote transport failure"
                        raise
                    received = {str(row.get("symbol")): row for row in _rows(payload, "quotes", "quote") if self._valid_quote(row)}
                    for symbol, ticket in tickets.items():
                        value = received.get(symbol)
                        self._publish("quote:" + symbol, ticket, value, "Missing quote" if value is None else "")
                        if symbol in received:
                            result[symbol] = received[symbol]
                except Exception as error:
                    message = str(error) if isinstance(error, BrokerReadUnavailable) else "Quote transport failure"
                    for symbol, ticket in tickets.items():
                        if ticket and symbol not in result:
                            try:
                                self._publish("quote:" + symbol, ticket, error=message)
                            except BrokerReadUnavailable:
                                pass  # A mutation or takeover fenced this publisher.
                    self._publish(batch_key, batch_ticket, None if transport_error else {}, error=transport_error)
                    raise BrokerReadUnavailable(str(error) if isinstance(error, BrokerReadUnavailable) else "Invalid quote response") from None
                self._publish(batch_key, batch_ticket, {})
            if set(result) != set(symbols):
                raise BrokerReadUnavailable("Incomplete broker quote batch")
            return {"quotes": {"quote": [result[symbol] for symbol in symbols]}}
        finally:
            with self.db(True) as db:
                db.execute("DELETE FROM leases WHERE scope=? AND name='quote_batch' AND owner=?", (self.account_key, owner))

    def write(self, callback, *, caller="execution"):
        """One attempt only. Ambiguous writes require broker reconciliation."""
        self._reserve("trading", "execution")
        started = self.clock()
        self.invalidate()
        try:
            return callback()
        finally:
            self.invalidate()
            self._record(None, category="trading", endpoint="/accounts/{account}/orders", caller=caller, started=started, retry=0)

    def collect_once(self, role="dashboard"):
        now = self.clock()
        with self.db(True) as db:
            if role == "bot":
                db.execute("INSERT OR REPLACE INTO leases VALUES (?,'bot_present',?,?)", (self.account_key, self.owner, now + 15))
            elif db.execute("SELECT 1 FROM leases WHERE scope=? AND name='bot_present' AND expires>?", (self.account_key, now)).fetchone():
                db.execute("DELETE FROM leases WHERE scope=? AND name='collector' AND owner=?", (self.account_key, self.owner))
                return False
            lease = db.execute("SELECT * FROM leases WHERE scope=? AND name='collector'", (self.account_key,)).fetchone()
            if lease and lease["owner"] != self.owner and lease["expires"] > now:
                return False
            db.execute("INSERT OR REPLACE INTO leases VALUES (?,'collector',?,?)", (self.account_key, self.owner, now + 30))
        for resource in ("balances", "positions", "orders"):
            if self._stop.is_set():
                return False
            try:
                self.get(f"/accounts/{self.account}/{resource}", caller=role)
            except BrokerReadUnavailable:
                pass
        symbols = set()
        positions = self.snapshot("positions")["data"]
        if positions:
            symbols.update(row["symbol"] for row in _rows(positions, "positions", "position") if row.get("symbol"))
        orders = self.snapshot("orders")["data"]
        def order_symbols(rows):
            for row in rows:
                if str(row.get("status", "")).lower() in ACTIVE:
                    symbols.update(row[k] for k in ("option_symbol", "symbol") if row.get(k))
                    order_symbols(row.get("leg") if isinstance(row.get("leg"), list) else [])
        if orders:
            order_symbols(_rows(orders, "orders", "order"))
        with self.db(True) as db:
            db.execute("DELETE FROM demand WHERE expires<?", (self.clock(),))
            db.execute("DELETE FROM snapshots WHERE scope=? AND key NOT IN ('balances','positions','orders') AND attempt<? AND lease_until<=?",
                       (self.account_key, self.clock() - 86400, self.clock()))
            symbols.update(row[0] for row in db.execute("SELECT symbol FROM demand WHERE scope=?", (self.account_key,)))
        if symbols and not self._stop.is_set():
            try:
                self.quotes(symbols, caller=role, register=False)
            except BrokerReadUnavailable:
                pass
        return True

    def start(self, role="dashboard"):
        if role == "bot":
            self._role = role
        elif self._role == "dashboard":
            # This dashboard has one configured connection. A credential or
            # environment change must not leave its old fallback poller running.
            with _clients_lock:
                for other in _clients.values():
                    if other is not self and other._role == "dashboard":
                        other._stop.set()
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        def run():
            while not self._stop.is_set():
                try:
                    self.collect_once(self._role)
                except Exception:
                    log.exception("Broker snapshot collector failed")
                self._stop.wait(1)
        self._thread = threading.Thread(target=run, name="broker-snapshots", daemon=True)
        self._thread.start()

    def close(self):
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=8)
        with self.db(True) as db:
            db.execute("DELETE FROM leases WHERE scope=? AND owner=?", (self.account_key, self.owner))


_clients = {}
_clients_lock = threading.Lock()


def get_broker_reads(token, account, sandbox=True):
    token, account = token.strip(), account.strip()
    key = (token, account, sandbox, os.getenv("BROKER_SNAPSHOT_PATH", ""),
           os.getenv("TRADIER_MARKET_DATA_TOKEN", ""), os.getenv("TRADIER_MARKET_DATA_TOKEN_FILE", ""),
           os.getenv("TRADIER_MARKET_DATA_PAPER", ""))
    with _clients_lock:
        if key not in _clients:
            _clients[key] = BrokerReads(token, account, sandbox)
        return _clients[key]


def install_sdk_reads(broker, token, account, sandbox=True):
    """Adapt instance-level public Tradier request methods; never edit SDK source."""
    reads = get_broker_reads(token, account, sandbox)
    clients = [getattr(broker, "tradier", None)]
    for source_name in ("data_source", "option_source"):
        clients.append(getattr(getattr(broker, source_name, None), "tradier", None))
    installed = set()
    for client in clients:
        if client is None:
            continue
        for name in ("account", "orders", "market"):
            api = getattr(client, name, None)
            if api is None or id(api) in installed or not callable(getattr(api, "request", None)):
                continue
            if getattr(api, "_shared_broker_reads", None) is reads:
                continue
            installed.add(id(api))
            original = api.request
            def request(endpoint, params=None, headers=None, data=None, required_response_key=None, method="get", _original=original):
                if method.lower() != "get":
                    try:
                        return reads.write(lambda: _original(endpoint, params=params, headers=headers, data=data,
                                                            required_response_key=required_response_key, method=method))
                    except Exception as error:
                        normalized = error if isinstance(error, BrokerReadUnavailable) else BrokerReadUnavailable(f"Broker storage or adapter failure: {error}")
                        _sdk_error.set(normalized)
                        raise normalized from None
                try:
                    payload = reads.get(endpoint, params, caller="sdk")
                    if required_response_key and payload.get(required_response_key) is None:
                        raise BrokerReadUnavailable("Missing required broker response")
                    return payload
                except Exception as error:
                    normalized = error if isinstance(error, BrokerReadUnavailable) else BrokerReadUnavailable(f"Broker storage or adapter failure: {error}")
                    _sdk_error.set(normalized)
                    raise normalized from None
            api.request = request
            api._shared_broker_reads = reads
    broker._shared_broker_reads = reads
    # Some SDK versions turn unknown read exceptions into empty inventory/orders.
    # Fence that conversion before sync_positions/sync_orders can consume it.
    for name in ("_pull_positions", "_pull_broker_all_orders", "_get_balances_at_broker"):
        original = getattr(broker, name, None)
        if not callable(original) or getattr(original, "_broker_reads_guard", False):
            continue
        def guarded(*args, _original=original, **kwargs):
            context = _sdk_error.set(None)
            try:
                result = _original(*args, **kwargs)
                error = _sdk_error.get()
                if error is not None:
                    raise error
                return result
            finally:
                _sdk_error.reset(context)
        guarded._broker_reads_guard = True
        setattr(broker, name, guarded)
    polling = getattr(broker, "do_polling", None)
    if callable(polling) and not getattr(polling, "_broker_poll_guard", False):
        def poll_safely(*args, **kwargs):
            try:
                return polling(*args, **kwargs)
            except BrokerReadUnavailable as error:
                # Abort this cycle before SDK reconciliation can treat a failed
                # read as empty inventory. Execution reads still raise above.
                log.warning("Skipping broker polling cycle after unavailable read: %s", error)
        poll_safely._broker_poll_guard = True
        broker.do_polling = poll_safely
    return reads
