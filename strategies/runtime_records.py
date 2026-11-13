"""Runtime record scoping, process-local path resolution, and security guards.

Enforces:
1. Validated RecordScope with token-independent account identity.
2. Sticky write and network guards protecting production journals, databases, and logs.
3. Subprocess propagation and credential sanitization surviving environment clearing.
4. Protected-record verification covering SQLite, symlinks, pytest, and unittest.
5. Rejection of cross-scope appends and conflicting duplicate events.
"""
from __future__ import annotations

import builtins
import hashlib
import json
import io
import os
import re
import socket
import subprocess
import sys
import tempfile
import threading
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Callable
from urllib.parse import parse_qs, unquote, urlsplit

_ROOT = Path(__file__).resolve().parent.parent
_PROTECTED_DIR = (_ROOT / "logs").resolve()
_PROTECTED_FILES = {
    (_PROTECTED_DIR / name).resolve()
    for name in (
        "trades.jsonl",
        "trajectories.jsonl",
        "position_ledger.sqlite3",
        "position_ledger.sqlite3.execution.lock",
        "broker_snapshots.sqlite3",
        "trading.log",
        "positions.db",
    )
}

_VALID_ENVIRONMENTS = {"test", "paper", "live", "demo", "backtest"}

_test_root: Path | None = None
_lock = threading.RLock()
_guard_installed = False
_orig_open: Callable[..., Any] | None = None
_orig_sqlite_connect: Callable[..., Any] | None = None
_orig_socket_connect: Callable[..., Any] | None = None
_orig_popen_init: Callable[..., Any] | None = None

_MARKER_PATH = Path(tempfile.gettempdir()) / f".lumibot_test_guard_{os.getuid() if hasattr(os, 'getuid') else 'global'}.json"


@dataclass(frozen=True)
class RecordScope:
    """Execution scope with token-independent account identity."""

    environment: str
    broker: str = "tradier"
    account_id: str = "default"

    def __post_init__(self) -> None:
        norm_env = str(self.environment).strip().lower()
        if norm_env not in _VALID_ENVIRONMENTS:
            raise ValueError(
                f"Invalid RecordScope environment '{self.environment}'. Must be one of {_VALID_ENVIRONMENTS}"
            )
        object.__setattr__(self, "environment", norm_env)
        norm_broker = str(self.broker).strip().lower() or "tradier"
        object.__setattr__(self, "broker", norm_broker)
        norm_acc = str(self.account_id).strip() or "default"
        if not re.fullmatch(r"[A-Za-z0-9_-]+", norm_broker) or not re.fullmatch(r"[A-Za-z0-9_-]+", norm_acc):
            raise ValueError("Invalid broker or account identity")
        object.__setattr__(self, "account_id", norm_acc)

    @property
    def scope_key(self) -> str:
        """Token-independent identity key."""
        return f"{self.broker}_{self.environment}_{self.account_id}"

    @property
    def is_test(self) -> bool:
        return self.environment == "test"

    @property
    def is_paper(self) -> bool:
        return self.environment == "paper"

    @property
    def is_live(self) -> bool:
        return self.environment == "live"

    def to_dict(self) -> dict[str, str]:
        return {
            "environment": self.environment,
            "broker": self.broker,
            "account_id": self.account_id,
            "scope_key": self.scope_key,
        }

    def default_record_path(self, relative_path: str | Path) -> Path:
        """Derive an isolated or scoped path for this scope."""
        rel = Path(relative_path)
        if self.is_test:
            root = test_record_root() or Path(tempfile.gettempdir()) / "lumibot-tests"
            return (root / rel).resolve()
        return (_ROOT / "logs" / "scopes" / self.scope_key / rel.name).resolve()

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> RecordScope:
        """Construct RecordScope from environment variables without exposing tokens."""
        mapping = env if env is not None else os.environ
        if is_test_mode_active():
            return cls(environment="test", broker="mock", account_id="test_account")

        broker = str(mapping.get("TRADING_BROKER") or mapping.get("BROKER") or "tradier").strip().lower()
        account = str(
            mapping.get("TRADIER_ACCOUNT_NUMBER")
            or mapping.get("TRADIER_ACCOUNT")
            or mapping.get("TRADIER_ACCOUNT_ID")
            or mapping.get("ALPACA_ACCOUNT_ID")
            or "default"
        ).strip()
        explicit = str(mapping.get("LUMIBOT_RECORD_ENV") or "").strip().lower()
        if explicit:
            environment = explicit
        else:
            raw = next((str(mapping[name]).strip().lower() for name in
                        ("TRADIER_PAPER", "TRADIER_IS_PAPER", "TRADIER_SANDBOX")
                        if mapping.get(name) and str(mapping[name]).strip()), "true")
            if raw not in {"true", "1", "yes", "on", "false", "0", "no", "off"}:
                raise ValueError("Invalid paper mode boolean")
            environment = "paper" if raw in {"true", "1", "yes", "on"} else "live"
        return cls(environment=environment, broker=broker, account_id=account)


def is_protected_runtime_path(path: str | Path) -> bool:
    """Return True if path resolves to or inside the production logs directory."""
    try:
        raw = os.fsdecode(path)
        if raw.startswith("file:"):
            parsed = urlsplit(raw)
            if parsed.netloc not in {"", "localhost"}:
                return True
            raw = unquote(parsed.path)
        resolved = Path(raw).expanduser().resolve()
    except Exception:
        return False
    if resolved in _PROTECTED_FILES:
        return True
    if resolved == _PROTECTED_DIR:
        return True
    return _PROTECTED_DIR in resolved.parents


def protected_record_inventory() -> dict[str, str]:
    """Hash runtime records, including nested files and SQLite sidecars."""
    if not _PROTECTED_DIR.exists():
        return {}
    inventory = {}
    for path in sorted(_PROTECTED_DIR.rglob("*")):
        if path.is_file():
            rel = str(path.relative_to(_PROTECTED_DIR))
            try:
                inventory[rel] = hashlib.sha256(path.read_bytes()).hexdigest()
            except Exception as error:
                raise OSError(f"Failed to read protected runtime record '{path}' for inventory: {error}") from error
    return inventory


def _read_marker_root() -> Path | None:
    # Isolation is inherited explicitly by subprocesses, never globally by user ID.
    return None


def is_test_mode_active() -> bool:
    """Check if test isolation is active via memory, environment, marker, or framework."""
    if _test_root is not None:
        return True
    if os.environ.get("LUMIBOT_OFFLINE_GUARD") == "1" or os.environ.get("LUMIBOT_TEST_RECORD_ROOT"):
        return True
    if _read_marker_root() is not None:
        return True
    # Detect pytest or unittest invocation
    if "pytest" in sys.modules or "unittest" in sys.modules:
        if any("pytest" in arg or "unittest" in arg or "test" in arg for arg in sys.argv):
            return True
    return False


def test_record_root() -> Path | None:
    """Get the active test temporary record root directory."""
    global _test_root
    if _test_root is not None:
        return _test_root
    env_root = os.environ.get("LUMIBOT_TEST_RECORD_ROOT")
    if env_root:
        _test_root = Path(env_root).resolve()
        return _test_root
    marker_root = _read_marker_root()
    if marker_root is not None:
        _test_root = marker_root
        return _test_root
    return None


def isolate_snapshot_database(test_root: str | Path) -> Path:
    """Initialize an empty snapshot schema without reading runtime broker data."""
    import sqlite3

    target_path = (Path(test_root) / "logs" / "broker_snapshots.sqlite3").resolve()
    if is_protected_runtime_path(target_path):
        raise PermissionError("Snapshot isolation cannot target protected runtime records")
    target_path.parent.mkdir(parents=True, exist_ok=True)
    connect_fn = _orig_sqlite_connect or sqlite3.connect
    with connect_fn(str(target_path)) as conn:
        conn.executescript("""
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
    return target_path


def install_test_context(root: str | Path | None = None, install_guards: bool = True) -> Path:
    """Install sticky test context, directory redirect, and safety guards."""
    global _test_root
    with _lock:
        if _test_root is not None:
            isolate_snapshot_database(_test_root)
            if install_guards:
                _install_guards()
            return _test_root

        if root is None:
            root = Path(tempfile.mkdtemp(prefix="lumibot-tests-"))
        resolved = Path(root).expanduser().resolve()
        resolved.mkdir(parents=True, exist_ok=True)
        (resolved / "logs").mkdir(parents=True, exist_ok=True)
        _test_root = resolved

        # Set environment variables
        os.environ["LUMIBOT_TEST_RECORD_ROOT"] = str(_test_root)
        os.environ["LUMIBOT_OFFLINE_GUARD"] = "1"
        os.environ["RUN_TRADIER_SANDBOX_WRITE_TEST"] = "0"
        # Sanitize credentials
        os.environ["TRADIER_ACCESS_TOKEN"] = "sanitized_test_token"
        os.environ["TRADIER_TOKEN"] = "sanitized_test_token"
        os.environ["TRADIER_ACCOUNT_ID"] = "VA_TEST_ACCOUNT"
        os.environ["TRADIER_PAPER"] = "true"

        # Explicit path redirections
        for name, rel in {
            "TRADE_LOG_PATH": "logs/trades.jsonl",
            "TRAJECTORY_LOG_PATH": "logs/trajectories.jsonl",
            "POSITION_LEDGER_PATH": "logs/position_ledger.sqlite3",
            "BROKER_SNAPSHOT_PATH": "logs/broker_snapshots.sqlite3",
            "TRADING_LOG_PATH": "logs/trading.log",
        }.items():
            os.environ[name] = str(_test_root / rel)

        isolate_snapshot_database(_test_root)

        if install_guards:
            _install_guards()
        return _test_root


def _install_guards() -> None:
    """Install sticky open, SQLite, network, and subprocess guards."""
    global _guard_installed, _orig_open, _orig_sqlite_connect, _orig_socket_connect, _orig_popen_init
    if _guard_installed:
        return
    _guard_installed = True

    # 1. builtins.open write guard
    _orig_open = builtins.open

    def guarded_open(file: Any, mode: str = "r", *args: Any, **kwargs: Any) -> Any:
        is_write = any(char in mode for char in ("w", "a", "+", "x"))
        if is_write and is_test_mode_active():
            if is_protected_runtime_path(file):
                raise PermissionError(
                    f"Test write guard blocked write access to protected runtime record: {file}"
                )
        return _orig_open(file, mode, *args, **kwargs)

    guarded_open._is_guard = True  # type: ignore[attr-defined]
    builtins.open = guarded_open  # type: ignore[assignment]
    original_io_open = io.open

    def guarded_io_open(file: Any, mode: str = "r", *args: Any, **kwargs: Any) -> Any:
        if any(char in mode for char in ("w", "a", "+", "x")) and is_protected_runtime_path(file):
            raise PermissionError(f"Test write guard blocked protected record: {file}")
        return original_io_open(file, mode, *args, **kwargs)

    io.open = guarded_io_open
    original_os_open = os.open

    def guarded_os_open(path: Any, flags: int, *args: Any, **kwargs: Any) -> int:
        if flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND) and is_protected_runtime_path(path):
            raise PermissionError(f"Test write guard blocked protected record: {path}")
        return original_os_open(path, flags, *args, **kwargs)

    os.open = guarded_os_open

    # 2. sqlite3.connect write guard
    try:
        import sqlite3

        _orig_sqlite_connect = sqlite3.connect

        def guarded_sqlite_connect(database: Any, *args: Any, **kwargs: Any) -> Any:
            if is_test_mode_active():
                if is_protected_runtime_path(database):
                    uri = kwargs.get("uri", False)
                    db_str = str(database)
                    is_ro = uri and parse_qs(urlsplit(db_str).query).get("mode") == ["ro"]
                    if not is_ro:
                        raise PermissionError(
                            f"Test write guard blocked non-read-only connection to protected database: {database}"
                        )
            return _orig_sqlite_connect(database, *args, **kwargs)

        guarded_sqlite_connect._is_guard = True  # type: ignore[attr-defined]
        sqlite3.connect = guarded_sqlite_connect  # type: ignore[assignment]
        sqlite3.dbapi2.connect = guarded_sqlite_connect  # type: ignore[assignment]
    except ImportError:
        pass

    for operation in ("rename", "replace", "unlink", "remove"):
        original = getattr(os, operation)

        def guarded_mutation(*args: Any, _original: Callable[..., Any] = original, **kwargs: Any) -> Any:
            if is_test_mode_active():
                targets: list[tuple[Any, Any]] = []
                if _original.__name__ in {"rename", "replace"}:
                    src = args[0] if len(args) > 0 else kwargs.get("src")
                    dst = args[1] if len(args) > 1 else kwargs.get("dst")
                    src_fd = kwargs.get("src_dir_fd")
                    dst_fd = kwargs.get("dst_dir_fd")
                    if src is not None:
                        targets.append((src, src_fd))
                    if dst is not None:
                        targets.append((dst, dst_fd))
                else:
                    path = args[0] if len(args) > 0 else kwargs.get("path")
                    dir_fd = kwargs.get("dir_fd")
                    if path is not None:
                        targets.append((path, dir_fd))

                for path, fd in targets:
                    try:
                        raw = os.fsdecode(path)
                    except Exception:
                        raw = str(path)
                    if fd is not None:
                        candidate = Path(f"/proc/self/fd/{fd}") / raw
                    else:
                        candidate = Path(raw)

                    # Removing or moving an external symlink does not mutate its target.
                    try:
                        if candidate.is_symlink():
                            parent = candidate.parent.resolve()
                            protected = (parent == _PROTECTED_DIR or _PROTECTED_DIR in parent.parents)
                        else:
                            protected = is_protected_runtime_path(candidate)
                    except Exception:
                        protected = is_protected_runtime_path(candidate)

                    if protected:
                        raise PermissionError(
                            f"Test write guard blocked protected record mutation via {_original.__name__}: {path}"
                        )
            return _original(*args, **kwargs)

        setattr(os, operation, guarded_mutation)

    # 3. socket network guard
    _orig_socket_connect = socket.socket.connect

    def guarded_socket_connect(self: socket.socket, address: Any) -> Any:
        if is_test_mode_active():
            if isinstance(address, tuple) and len(address) >= 2:
                host, _ = address[0], address[1]
                host_str = str(host).lower()
                allowed_loopbacks = {"127.0.0.1", "::1", "localhost", "0.0.0.0"}
                if host_str not in allowed_loopbacks and not host_str.startswith("127."):
                    raise RuntimeError(
                        f"Test network guard blocked external network connection to: {address}"
                    )
        return _orig_socket_connect(self, address)

    guarded_socket_connect._is_guard = True  # type: ignore[attr-defined]
    socket.socket.connect = guarded_socket_connect  # type: ignore[assignment]
    original_connect_ex = socket.socket.connect_ex

    def guarded_connect_ex(self: socket.socket, address: Any) -> int:
        if is_test_mode_active() and isinstance(address, tuple) and len(address) >= 2:
            host = str(address[0]).lower()
            if host not in {"127.0.0.1", "::1", "localhost", "0.0.0.0"} and not host.startswith("127."):
                raise RuntimeError(f"Test network guard blocked external network connection to: {address}")
        return original_connect_ex(self, address)

    socket.socket.connect_ex = guarded_connect_ex  # type: ignore[assignment]

    # 4. subprocess.Popen propagation guard
    _orig_popen_init = subprocess.Popen.__init__

    def guarded_popen_init(self: subprocess.Popen, args: Any, **kwargs: Any) -> None:
        if is_test_mode_active():
            env = kwargs.get("env")
            root = test_record_root() or (_ROOT / "tmp/tests")
            if env is None:
                env = os.environ.copy()
            else:
                env = dict(env)
            env["LUMIBOT_TEST_RECORD_ROOT"] = str(root)
            env["LUMIBOT_OFFLINE_GUARD"] = "1"
            env["RUN_TRADIER_SANDBOX_WRITE_TEST"] = "0"
            env["TRADIER_ACCESS_TOKEN"] = "sanitized_test_token"
            env["TRADIER_TOKEN"] = "sanitized_test_token"
            env["TRADIER_ACCOUNT_ID"] = "VA_TEST_ACCOUNT"
            env["TRADIER_PAPER"] = "true"
            py_path = env.get("PYTHONPATH", "")
            if str(_ROOT) not in py_path:
                env["PYTHONPATH"] = f"{_ROOT}:{py_path}" if py_path else str(_ROOT)
            kwargs["env"] = env
        _orig_popen_init(self, args, **kwargs)

    guarded_popen_init._is_guard = True  # type: ignore[attr-defined]
    subprocess.Popen.__init__ = guarded_popen_init  # type: ignore[assignment]


def resolve_effective_scope(scope: Any = None) -> RecordScope:
    """Coerce various scope representations to RecordScope or resolve from environment."""
    if isinstance(scope, RecordScope):
        return scope
    if isinstance(scope, dict):
        return RecordScope(**scope)
    if isinstance(scope, str) and scope.strip():
        return RecordScope(environment=scope.strip())
    return RecordScope.from_env()


def resolve_record_path(variable: str, default: str, scope: Any = None) -> Path:
    """Resolve an explicit path or an environment-independent default.

    In test mode:
    - Explicit paths pointing to protected production records are strictly rejected.
    - Default paths resolve to the isolated test directory.
    In runtime:
    - Resolves to explicit override or scoped/production default.
    """
    root = test_record_root()
    active_test = is_test_mode_active()
    value = os.environ.get(variable, "").strip()

    if value:
        candidate = Path(value).expanduser()
        if not candidate.is_absolute():
            candidate = _ROOT / candidate
        resolved = candidate.resolve()
        if active_test and is_protected_runtime_path(resolved):
            raise PermissionError(
                f"Explicit runtime path to protected production record '{resolved}' is rejected in test mode. "
                "Tests must use isolated test directories."
            )
        return resolved

    if active_test:
        effective_root = root or _read_marker_root() or (Path(tempfile.gettempdir()) / "lumibot-tests")
        eff_scope = resolve_effective_scope(scope)
        if not eff_scope.is_test:
            return (effective_root / "scopes" / eff_scope.scope_key / Path(default).name).resolve()
        return (effective_root / default).resolve()

    eff_scope = resolve_effective_scope(scope)
    if eff_scope is not None and not eff_scope.is_test:
        return eff_scope.default_record_path(default)

    return (_ROOT / default).resolve()


def validate_append_event(
    log_path: Path,
    payload: dict[str, Any],
    event_id: str | None = None,
    scope: RecordScope | None = None,
) -> bool:
    """Validate cross-scope appends and conflicting duplicate events.

    Returns True if valid to append, False if duplicate is idempotent and should be skipped.
    Raises ValueError on cross-scope mismatch or conflicting duplicates.
    """
    if scope is None:
        scope = RecordScope.from_env()

    if event_id:
        payload["event_id"] = event_id

    # Rejection of cross-scope appends and conflicting duplicate events
    if log_path.exists() and log_path.stat().st_size > 0:
        try:
            with open(log_path, "r", encoding="utf-8", errors="replace") as f:
                for line in f:
                    line_str = line.strip()
                    if not line_str:
                        continue
                    try:
                        record = json.loads(line_str)
                    except json.JSONDecodeError:
                        continue

                    # Cross-scope check
                    rec_scope = record.get("scope")
                    if isinstance(rec_scope, dict):
                        existing_key = rec_scope.get("scope_key")
                        if existing_key and existing_key != scope.scope_key:
                            raise ValueError(
                                f"Cross-scope append rejected: cannot append event with scope '{scope.scope_key}' "
                                f"to journal with existing scope '{existing_key}' at {log_path}"
                            )

                    # Conflicting duplicate check
                    rec_ev_id = record.get("event_id")
                    if event_id and rec_ev_id == event_id:
                        # Compare critical fields: event, trade_id, quantity, symbol, expiry
                        crit_fields = ("event", "trade_id", "quantity", "symbol", "expiry")
                        diffs = [
                            f for f in crit_fields
                            if record.get(f) is not None and payload.get(f) is not None and record.get(f) != payload.get(f)
                        ]
                        if diffs:
                            raise ValueError(
                                f"Conflicting duplicate event rejected for event_id '{event_id}': "
                                f"conflicting fields {diffs}"
                            )
                        # Identical duplicate -> skip
                        return False
        except (ValueError, PermissionError):
            raise
        except Exception:
            pass

    # Attach scope metadata to payload
    payload["scope"] = scope.to_dict()
    if "source" not in payload:
        payload["source"] = "unattributed" if scope.environment in {"paper", "live"} else "synthetic"
    return True


CLASSIFIER_VERSION = "2"
EVIDENCE_CONTRACT_VERSION = "2"
_SCOPE_FIELDS = ("broker", "account_id", "environment")
_REALIZATIONS = {"ENTRY": "open", "ADDITION": "add", "ADJUSTMENT": "trim", "EXIT": "close"}


def is_fixture_trade(trade_id: str, record: dict[str, Any] | None = None) -> bool:
    """Quarantine explicit synthetic markers and the documented regression fingerprint.

    Names such as test_, mock_, or fixture alone do not establish provenance.
    The historical filled-* fingerprint also requires the exact four fixture strikes.
    """
    record = record or {}
    scope = record.get("scope") or {}
    environment = scope.get("environment") if isinstance(scope, dict) else scope
    if (isinstance(environment, str) and environment in {"test", "demo", "backtest"}) or record.get("source") == "synthetic":
        return True
    strikes = record.get("strikes") or {}
    if not isinstance(strikes, dict):
        return False
    return str(trade_id).startswith("filled-") and all(
        strikes.get(name, strikes.get(alias)) == value
        for name, alias, value in (("long_put","put_long",95), ("short_put","put_short",100),
                                   ("short_call","call_short",110), ("long_call","call_long",115))
    )


def load_ledger_execution_evidence(
    ledger_path: Path | str | None = None, *, expected_scope: RecordScope | None = None,
    as_of_date: Any = None,
) -> dict[str, Any]:
    """Read retained evidence without migrations, with explicit schema/read diagnostics."""
    import sqlite3
    evidence: dict[str, Any] = dict(scope=None, trades={}, packages={}, retained={}, invalid_evidence={}, links=[],
                                   settlement_links=[], settlements={}, fees=[], diagnostics=[])
    if ledger_path is None:
        ledger_path = resolve_record_path("POSITION_LEDGER_PATH", "logs/position_ledger.sqlite3", expected_scope or RecordScope.from_env())
    path = Path(ledger_path)
    if not path.exists():
        evidence["diagnostics"].append("MISSING_EVIDENCE_DATABASE")
        return evidence
    tables = ("record_scope", "packages", "legs", "broker_evidence", "realization_evidence_links",
              "settlement_evidence_links", "settlement_reconciliations", "fee_evidence", "settlement_values", "broker_cash_evidence")
    try:
        with sqlite3.connect(path.resolve().as_uri()+"?mode=ro", uri=True) as db:
            db.row_factory=sqlite3.Row
            existing={r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            rows={}
            for table in tables:
                if table not in existing:
                    evidence["diagnostics"].append("MISSING_EVIDENCE_SCHEMA:"+table)
                    rows[table]=[]
                else:
                    rows[table]=[dict(row) for row in db.execute(f'SELECT * FROM {table}')]
            if rows["record_scope"]:
                evidence["scope"]={k:rows["record_scope"][0].get(k) for k in _SCOPE_FIELDS}
            for package in rows["packages"]:
                package["legs"]=[leg for leg in rows["legs"] if leg.get("package_id")==package.get("package_id")]
                evidence["packages"][package["package_id"]]=package
                evidence["trades"].setdefault(package.get("trade_id"),[]).append(package)
            for row in rows["broker_evidence"]:
                if hashlib.sha256(row["payload"].encode()).hexdigest()!=row["sha256"]:
                    evidence["diagnostics"].append("EVIDENCE_HASH_MISMATCH:"+row["evidence_id"])
                    continue
                row["payload"]=json.loads(row["payload"])
                if not isinstance(row["payload"],dict):
                    raise ValueError("retained payload must be an object")
                if row["kind"] == "execution" and not execution_payload_is_valid(row["payload"]):
                    evidence["invalid_evidence"][row["evidence_id"]] = "INVALID_EXECUTION_PAYLOAD"
                    continue
                evidence["retained"][row["evidence_id"]]=row
            evidence["links"]=rows["realization_evidence_links"]
            evidence["settlement_links"]=rows["settlement_evidence_links"]
            evidence["settlements"]={r.get("trade_id"):r for r in rows["settlement_reconciliations"]}
            evidence["fees"]=rows["fee_evidence"]
            evidence["settlement_values"]=rows["settlement_values"]
            evidence["cash_records"]=rows["broker_cash_evidence"]
    except (sqlite3.Error, OSError, ValueError, KeyError, TypeError) as error:
        evidence["diagnostics"].append("EVIDENCE_READ_ERROR:"+type(error).__name__)
        evidence["retained"]={}
    return evidence


def _contract_identity(contract: Any) -> tuple[Any, ...] | None:
    """Parse the complete contract identity at the evidence boundary."""
    import math
    if (not isinstance(contract, dict)
            or any(not isinstance(contract.get(key), str) for key in ("symbol", "expiration", "right"))
            or any(contract.get(key) is None for key in ("strike", "multiplier"))):
        return None
    try:
        from datetime import date
        expiry=date.fromisoformat(contract["expiration"]).isoformat()
        strike=float(contract["strike"])
        multiplier=float(contract["multiplier"])
        if (not contract["symbol"] or contract["right"] not in {"put","call"}
                or isinstance(contract["strike"],bool) or isinstance(contract["multiplier"],bool)
                or not math.isfinite(strike) or strike<=0 or not math.isfinite(multiplier) or multiplier<=0):
            return None
        return contract["symbol"],expiry,contract["right"],strike,multiplier
    except (ValueError,TypeError,OverflowError):
        return None


def execution_payload_is_valid(payload: Any) -> bool:
    """Validate execution container shapes before traversing provider content."""
    if not isinstance(payload, dict):
        return False
    legs = payload.get("legs")
    if not isinstance(legs, list) or not legs:
        return False
    for leg in legs:
        if (not isinstance(leg, dict)
                or any(not isinstance(leg.get(key), str) or not leg[key]
                       for key in ("leg_id", "order_id", "parent_order_id", "side"))
                or not isinstance(leg.get("signed_quantity"), int) or isinstance(leg["signed_quantity"], bool)
                or not isinstance(leg.get("contract"), dict)
                or not isinstance(leg["contract"].get("symbol"), str)):
            return False
    raw = payload.get("raw_response")
    parent = payload.get("parent_response")
    if not isinstance(raw, dict) or (parent is not None and not isinstance(parent, dict)):
        return False
    return all("leg" not in response or (isinstance(response["leg"], list)
               and all(isinstance(item, dict) for item in response["leg"]))
               for response in (raw, parent or {}))


def _raw_execution_matches(payload: dict[str, Any], leg: dict[str, Any]) -> bool:
    """Check normalized SDK/export fields against the retained provider response."""
    from decimal import Decimal, InvalidOperation

    raw=payload.get("raw_response")
    if not isinstance(raw,dict) or not raw:
        return False
    scope = payload.get("scope")
    parent = payload.get("parent_response")
    parent = {} if parent is None else parent
    if not isinstance(scope, dict) or not isinstance(parent, dict):
        return False
    parent_id = payload.get("parent_order_id")
    contract = _contract_identity(leg.get("contract"))
    signed_quantity = leg.get("signed_quantity")
    side = leg.get("side")
    if (not isinstance(parent_id, str) or not parent_id or leg.get("parent_order_id") != parent_id
            or contract is None or not isinstance(signed_quantity, int)
            or isinstance(signed_quantity, bool) or not signed_quantity or not isinstance(side, str)
            or side not in {"buy_to_open", "sell_to_open", "buy_to_close", "sell_to_close"}
            or (signed_quantity > 0) != side.startswith("buy")):
        return False

    def provider_id(item: dict[str, Any]) -> str | None:
        identities = [item[key] for key in ("id", "order_id") if key in item]
        if (not identities or any(not isinstance(value, (str, int)) or isinstance(value, bool)
                                  or not str(value) for value in identities)
                or len({str(value) for value in identities}) != 1):
            return None
        return str(identities[0])

    # SDK responses may omit scope, but explicit provider fields cannot disagree.
    for response in (raw, parent):
        children = response.get("leg", [])
        if not isinstance(children, list) or any(not isinstance(child, dict) for child in children):
            return False
        identities = [provider_id(child) for child in children]
        if None in identities or len(set(identities)) != len(identities):
            return False
        if (("leg" in response and any(key in response for key in ("id", "order_id"))
             and provider_id(response) != parent_id)
                or (response is parent and parent and provider_id(parent) != parent_id)):
            return False
        for item in (response, *children):
            explicit_scope = item.get("scope", {})
            if not isinstance(explicit_scope, dict):
                return False
            if "parent_order_id" in item and item["parent_order_id"] != parent_id:
                return False
            for field in ("broker", "account_id", "environment", "scope_key"):
                if ((field in item and item[field] != scope.get(field))
                        or (field in explicit_scope and explicit_scope[field] != scope.get(field))):
                    return False

    def fields_match(item: dict[str, Any], *, cumulative_parent: bool = False) -> bool:
        if (not cumulative_parent or "side" in item) and item.get("side") != side:
            return False
        signed = item.get("signed_quantity")
        if "signed_quantity" in item and (
                not isinstance(signed, int) or isinstance(signed, bool)
                or (signed != 0 and (signed > 0) != (signed_quantity > 0))
                or (abs(signed) > abs(signed_quantity) if cumulative_parent else signed != signed_quantity)):
            return False
        if "exec_quantity" in item:
            try:
                quantity = Decimal(str(item["exec_quantity"]))
                if (not quantity.is_finite() or quantity < 0 or quantity != quantity.to_integral_value()
                        or (quantity > abs(signed_quantity) if cumulative_parent else quantity != abs(signed_quantity))
                        or ("signed_quantity" in item and quantity != abs(signed))):
                    return False
            except InvalidOperation:
                return False
        elif "signed_quantity" not in item and not cumulative_parent:
            return False
        if "contract" in item and _contract_identity(item["contract"]) != contract:
            return False
        if "option_symbol" in item:
            match=re.fullmatch(r"([A-Z]+)\s*(\d{6})([CP])(\d{8})", str(item.get("option_symbol","")))
            if match is None:
                return False
            symbol,stamp,right,strike=match.groups()
            if contract[:4] != (symbol,f"20{stamp[:2]}-{stamp[2:4]}-{stamp[4:]}","put" if right=="P" else "call",int(strike)/1000):
                return False
        elif "contract" not in item and not cumulative_parent:
            return False
        return True

    candidates = [item for item in raw.get("leg", [raw]) if provider_id(item) == leg.get("order_id")]
    if len(candidates) != 1 or not fields_match(candidates[0]):
        return False
    parent_children = [item for item in parent.get("leg", []) if provider_id(item) == leg.get("order_id")]
    # Cached parents can precede a later cumulative child fill. Their supplied
    # fields must remain consistent, and their quantities cannot exceed it.
    if "leg" in parent and (len(parent_children) != 1 or not fields_match(parent_children[0], cumulative_parent=True)):
        return False
    return (candidates[0].get("parent_order_id") == parent_id
            or ("leg" in raw and provider_id(raw) == parent_id)
            or ("leg" not in raw and provider_id(raw) == parent_id == leg.get("order_id"))
            or (provider_id(parent) == parent_id and len(parent_children) == 1))


def settlement_cash_matches(
    payload: dict[str, Any], *, scope: RecordScope, provider_identity: str,
) -> bool:
    """Match an allocated cash activity to its retained provider fields."""
    raw = payload.get("raw_response")
    amount = payload.get("cash_cents")
    if (not isinstance(raw, dict) or payload.get("scope") != scope.to_dict()
            or raw.get("scope", scope.to_dict()) != scope.to_dict()
            or not provider_identity or payload.get("activity_id") != provider_identity
            or raw.get("activity_id") != provider_identity
            or not isinstance(amount, int) or isinstance(amount, bool)
            or not isinstance(raw.get("cash_cents"), int) or isinstance(raw.get("cash_cents"), bool)
            or raw["cash_cents"] != amount
            or payload.get("disposition_confirmed") is not True
            or raw.get("disposition_confirmed") is not True
            or payload.get("symbol") != "XSP"
            or raw.get("symbol", "XSP") != "XSP"
            or raw.get("expiration", payload.get("expiration")) != payload.get("expiration")):
        return False

    def quantities(legs: Any) -> dict[tuple[Any, ...], int] | None:
        if not isinstance(legs, list) or not legs:
            return None
        result: dict[tuple[Any, ...], int] = {}
        for leg in legs:
            if not isinstance(leg, dict):
                return None
            contract = _contract_identity(leg.get("contract"))
            quantity = leg.get("signed_quantity")
            if (contract is None or contract in result or contract[0] != "XSP"
                    or contract[1] != payload.get("expiration") or contract[4] != 100
                    or not isinstance(quantity, int) or isinstance(quantity, bool) or not quantity):
                return None
            result[contract] = quantity
        return result

    normalized = quantities(payload.get("legs"))
    return normalized is not None and normalized == quantities(raw.get("legs"))


def journal_event_conflicts(events: list[dict[str, Any]]) -> set[str]:
    """Find conflicting IDs across the whole journal, including different trades."""
    seen: dict[str, str] = {}
    conflicts: set[str] = set()
    for event in events:
        event_id = event.get("event_id")
        if not event_id:
            continue
        encoded = json.dumps(event, sort_keys=True, separators=(",", ":"))
        if event_id in seen and seen[event_id] != encoded:
            conflicts.add(event_id)
        seen[event_id] = encoded
    return conflicts


def fee_payload_matches(
    payload: Any, *, scope: RecordScope,
    order_id: str | None = None, activity_id: str | None = None,
) -> bool:
    """Match normalized actual fees to scoped provider amounts and ownership."""
    from decimal import Decimal, InvalidOperation

    if not isinstance(payload, dict):
        return False
    raw = payload.get("raw_response")
    amount = payload.get("amount_cents")
    if (not isinstance(raw, dict) or payload.get("scope") != scope.to_dict()
            or raw.get("scope", scope.to_dict()) != scope.to_dict()
            or not isinstance(amount, int) or isinstance(amount, bool) or amount < 0
            or not isinstance(payload.get("complete"), bool)
            or bool(order_id) == bool(activity_id)):
        return False
    raw_amount = raw.get("total_fees_cents", raw.get("commission_cents"))
    if raw_amount is None and "commission" in raw:
        try:
            cents = Decimal(str(raw["commission"])) * 100
            raw_amount = int(cents) if cents.is_finite() and cents == cents.to_integral_value() else None
        except (ValueError, TypeError, InvalidOperation):
            return False
    owner_matches = raw.get("order_id") == order_id if order_id else raw.get("activity_id") == activity_id
    return (isinstance(raw_amount, int) and not isinstance(raw_amount, bool)
            and raw_amount == amount and owner_matches)


def classify_trade(
    events_or_trade: list[dict[str, Any]] | dict[str, Any], ledger_evidence: dict[str, Any] | None = None,
    *, expected_scope: RecordScope | None = None, as_of_date: Any = None,
) -> dict[str, Any]:
    """Verify every realization against retained content, exact scope and ownership.

    Hashes establish retained-content integrity. Provider identity and acquisition
    context remain auditable evidence, not a cryptographic authenticity claim.
    """
    from collections import defaultdict
    from datetime import date, datetime, timezone
    events=events_or_trade if isinstance(events_or_trade,list) else [events_or_trade]
    amounts_complete = all(event[field] is not None for event in events
                           for field in ("entry_fees", "fees", "net_pnl") if field in event)
    evidence=ledger_evidence or {}
    reasons:set[str]=set()
    refs:set[str]=set()
    details:dict[str,list[str]]={}
    trade_id=str(events[0].get("trade_id", "")) if events else ""
    as_of=date.fromisoformat(str(as_of_date)) if as_of_date else datetime.now(timezone.utc).date()

    def result(partition: str, status: str) -> dict[str, Any]:
        return dict(partition=partition,reconciliation_status=status,
                    source={"VERIFIED":"tradier_verified","FEE_INCOMPLETE":"fee_incomplete","QUARANTINED":"synthetic"}.get(status,"unattributed"),
                    is_eligible=status=="VERIFIED" and amounts_complete, reason_codes=sorted(reasons),reason_details=details,
                    evidence_refs=sorted(refs),classifier_version=CLASSIFIER_VERSION,evidence_contract_version=EVIDENCE_CONTRACT_VERSION)

    if any(is_fixture_trade(trade_id,event) for event in events):
        reasons.add("SYNTHETIC_MARKER_OR_FIXTURE_FINGERPRINT")
        return result("quarantined","QUARANTINED")
    if expected_scope is None:
        reasons.add("EXPECTED_SCOPE_MISSING")
    expected=expected_scope.to_dict() if expected_scope else {}

    def check_scope(actual: Any, errors: set[str]) -> None:
        actual=actual if isinstance(actual,dict) else {}
        for field,label in (("broker","BROKER"),("account_id","ACCOUNT"),("environment","ENVIRONMENT")):
            if not actual.get(field) or (field=="account_id" and actual.get(field)=="default"):
                errors.add(label+"_MISSING")
            elif actual.get(field)!=expected.get(field):
                errors.add(label+"_MISMATCH")

    check_scope(evidence.get("scope"),reasons)
    if evidence.get("diagnostics"):
        reasons.add("EVIDENCE_STORE_INCOMPLETE")
    retained = {key: row for key, row in evidence.get("retained", {}).items()
                if row.get("kind") != "execution" or execution_payload_is_valid(row.get("payload"))}
    all_links=evidence.get("links",[])
    allocations:dict[tuple[Any,...],int]=defaultdict(int)
    capacities:dict[tuple[Any,...],int]=defaultdict(int)
    # Account for allocations across every trade and cumulative observation.
    for link in all_links:
        row=retained.get(link.get("evidence_id"),{})
        if row.get("kind") != "execution":
            continue
        payload=row.get("payload",{})
        for leg in payload.get("legs",[]):
            if leg.get("leg_id")==link.get("leg_id"):
                key=(row.get("broker"),row.get("account_id"),row.get("environment"),leg.get("order_id"),leg.get("parent_order_id"))
                allocations[key]+=abs(link.get("allocated_signed_quantity",0))
    for row in retained.values():
        if row.get("kind") != "execution":
            continue
        for leg in row.get("payload",{}).get("legs",[]):
            key=(row.get("broker"),row.get("account_id"),row.get("environment"),leg.get("order_id"),leg.get("parent_order_id"))
            quantity=leg.get("signed_quantity")
            if (row.get("kind")=="execution" and isinstance(quantity,int) and not isinstance(quantity,bool)
                    and _raw_execution_matches(row.get("payload",{}),leg)):
                capacities[key]=max(capacities[key],abs(quantity))
    unique:dict[str,dict[str,Any]]={}
    fees_complete=True
    saw_entry=False
    inventory:dict[tuple[Any,...],int]=defaultdict(int)
    for event in events:
        errors:set[str]=set()
        event_id=event.get("event_id")
        if event_id in evidence.get("conflicting_event_ids",set()):
            errors.add("CONFLICTING_EVENT_ID")
        if not event_id:
            errors.add("EVENT_ID_MISSING")
        elif event_id in unique:
            if event != unique[event_id]:
                reasons.add("CONFLICTING_EVENT_ID")
            continue
        else:
            unique[event_id]=event
        kind=event.get("event")
        if kind not in _REALIZATIONS:
            errors.add("UNSUPPORTED_REALIZATION")
        if event.get("trade_id")!=trade_id or not trade_id:
            errors.add("TRADE_ID_MISMATCH")
        event_scope = event.get("scope")
        if event_scope is not None and (not isinstance(event_scope, dict)
                or any(field in event_scope and not isinstance(event_scope[field], str)
                       for field in (*_SCOPE_FIELDS, "scope_key"))):
            errors.add("JOURNAL_SCOPE_INVALID")
        check_scope(event_scope,errors)
        if kind=="ENTRY":
            if saw_entry:
                errors.add("MULTIPLE_ENTRIES")
            saw_entry=True
        elif not saw_entry:
            errors.add("ENTRY_MISSING")
        settlement_links=[l for l in evidence.get("settlement_links",[]) if l.get("event_id")==event_id and l.get("trade_id")==trade_id]
        if kind=="EXIT" and settlement_links:
            link=settlement_links[0]
            settlement=retained.get(link["settlement_evidence_id"],{})
            cash=retained.get(link["cash_evidence_id"],{})
            sp,cp=settlement.get("payload",{}),cash.get("payload",{})
            rec=evidence.get("settlements",{}).get(trade_id,{})
            for row in (settlement,cash):
                check_scope(row,errors)
                check_scope(row.get("payload",{}).get("scope"),errors)
            refs.update((link["settlement_evidence_id"],link["cash_evidence_id"]))
            if (settlement.get("kind")!="settlement" or cash.get("kind")!="cash" or not sp.get("raw_response") or not cp.get("raw_response")
                    or sp.get("symbol")!="XSP" or cp.get("symbol")!="XSP"
                    or sp.get("expiration")!=events[0].get("expiry") or cp.get("expiration")!=sp.get("expiration")
                    or not sp.get("expiration") or sp["expiration"]>=str(as_of)
                    or cp.get("position_id")!=event.get("position_id") or cp.get("position_id")!=link.get("position_id")
                    or cp.get("trade_id")!=trade_id or rec.get("position_id")!=link.get("position_id")
                    or cp.get("cash_cents")!=link.get("allocated_cash_cents") or rec.get("settlement_cash_cents")!=cp.get("cash_cents")
                    or cp.get("disposition_confirmed") is not True
                    or cp.get("activity_id")!=cash.get("provider_identity")
                    or expected_scope is None
                    or not settlement_cash_matches(cp, scope=expected_scope, provider_identity=cash.get("provider_identity", ""))
                    or sp.get("raw_response",{}).get("expiration")!=sp.get("expiration")
                    or sp.get("raw_response",{}).get("symbol")!=sp.get("symbol")
                    or sp.get("raw_response",{}).get("value")!=sp.get("value")):
                errors.add("SETTLEMENT_PROVENANCE_MISMATCH")
            held={_contract_identity(l.get("contract")):l.get("signed_quantity") for l in cp.get("legs",[])}
            if not held or None in held or held!={k:v for k,v in inventory.items() if v}:
                errors.add("SETTLEMENT_CONTRACT_OR_QUANTITY_MISMATCH")
            try:
                from strategies.xsp_settlement import calculate_settlement_cash, create_settlement_value, SettlementLeg
                from decimal import Decimal
                settlement_legs=[SettlementLeg(date.fromisoformat(k[1]),k[2],Decimal(str(k[3])),q) for k,q in held.items()]
                calculated,_=calculate_settlement_cash(settlement_legs,create_settlement_value(sp["expiration"],sp["value"]))
                if calculated!=cp.get("cash_cents"):
                    errors.add("SETTLEMENT_CASH_MISMATCH")
            except (ValueError,KeyError,TypeError):
                errors.add("SETTLEMENT_PAYLOAD_INVALID")
            inventory.clear()
        else:
            links=[link for link in all_links if link.get("event_id")==event_id and event_id]
            event_legs=event.get("legs")
            package=evidence.get("packages",{}).get(event.get("package_id"),{})
            if not links:
                errors.add("MISSING_REALIZATION_LINKS")
            partial_close = kind == 'ADJUSTMENT' and package.get('intent') == 'close'
            if not package or package.get("trade_id")!=trade_id or package.get("position_id")!=event.get("position_id") or (package.get("intent")!=_REALIZATIONS.get(kind) and not partial_close):
                errors.add("PACKAGE_OWNERSHIP_MISMATCH")
            if not isinstance(event_legs,list) or not event_legs:
                errors.add("CONTRACTS_MISSING")
                event_legs=[]
            elif any(not isinstance(leg, dict)
                     or any(key in leg and not isinstance(leg[key], str)
                            for key in ("leg_id", "order_id", "parent_order_id", "side"))
                     or ("signed_quantity" in leg and (not isinstance(leg["signed_quantity"], int)
                                                       or isinstance(leg["signed_quantity"], bool)))
                     for leg in event_legs):
                errors.add("JOURNAL_LEGS_INVALID")
                event_legs=[]
            leg_ids=[leg.get("leg_id") for leg in event_legs]
            if len(set(leg_ids))!=len(leg_ids) or set(leg_ids)!={leg.get("order_id") for leg in package.get("legs",[])}:
                errors.add("CONTRACT_SET_MISMATCH")
            if len(links)!=len(event_legs):
                errors.add("ALLOCATION_COUNT_MISMATCH")
            for leg in event_legs:
                matching=[l for l in links if l.get("leg_id")==leg.get("leg_id")]
                if len(matching)!=1:
                    errors.add("MISSING_OR_AMBIGUOUS_ALLOCATION")
                    continue
                link=matching[0]
                row=retained.get(link.get("evidence_id"),{})
                payload=row.get("payload",{}) if row.get("kind") == "execution" else {}
                refs.add(link["evidence_id"])
                check_scope(row,errors)
                check_scope(payload.get("scope"),errors)
                provider_leg=next((l for l in payload.get("legs",[]) if l.get("leg_id")==leg.get("leg_id")),{})
                local_leg=next((l for l in package.get("legs",[]) if l.get("order_id")==leg.get("leg_id")),{})
                if not row or row.get("kind")!="execution" or not row.get("provider_identity") or not row.get("acquisition_source") or not row.get("acquired_at"):
                    errors.add("MISSING_RETAINED_EVIDENCE")
                if (link.get("trade_id")!=trade_id or link.get("position_id")!=event.get("position_id")
                        or link.get("package_id")!=event.get("package_id") or payload.get("trade_id")!=trade_id
                        or payload.get("position_id")!=link.get("position_id") or payload.get("package_id")!=link.get("package_id")
                        or not provider_leg.get("order_id") or provider_leg.get("order_id")!=local_leg.get("broker_order_id")
                        or leg.get("order_id")!=provider_leg.get("order_id")
                        or leg.get("parent_order_id")!=provider_leg.get("parent_order_id")
                        or not package.get("broker_parent_id") or provider_leg.get("parent_order_id")!=package.get("broker_parent_id")
                        or payload.get("parent_order_id")!=package.get("broker_parent_id")):
                    errors.add("PACKAGE_OWNERSHIP_MISMATCH")
                contract=_contract_identity(leg.get("contract"))
                try:
                    local_contract=_contract_identity({**json.loads(local_leg.get("asset_key","{}")),"multiplier":100})
                except (ValueError,TypeError):
                    local_contract=None
                if contract is None or contract!=_contract_identity(provider_leg.get("contract")) or contract!=local_contract:
                    errors.add("CONTRACT_MISMATCH")
                quantity=leg.get("signed_quantity")
                side=leg.get("side")
                if side not in {"buy_to_open","sell_to_open","buy_to_close","sell_to_close"} or side!=provider_leg.get("side") or side!=local_leg.get("side"):
                    errors.add("SIDE_MISMATCH")
                if isinstance(side,str) and not side.endswith("_to_open" if kind in {"ENTRY","ADDITION"} else "_to_close"):
                    errors.add("SIDE_MISMATCH")
                if contract and kind=="ENTRY" and (contract[0]!=event.get("symbol") or contract[1]!=event.get("expiry")):
                    errors.add("JOURNAL_CONTRACT_MISMATCH")
                if (not isinstance(quantity,int) or isinstance(quantity,bool) or quantity==0
                        or link.get("allocated_signed_quantity")!=quantity or provider_leg.get("signed_quantity")!=quantity
                        or local_leg.get("filled_signed_quantity")!=quantity
                        or (side and (quantity>0)!=side.startswith("buy"))):
                    errors.add("QUANTITY_MISMATCH")
                key=(row.get("broker"),row.get("account_id"),row.get("environment"),provider_leg.get("order_id"),provider_leg.get("parent_order_id"))
                if allocations[key]>capacities[key]:
                    errors.add("REUSED_EXECUTION_QUANTITY")
                if not _raw_execution_matches(payload,provider_leg):
                    errors.add("RAW_EXECUTION_MISMATCH")
                if contract and isinstance(quantity,int):
                    inventory[contract]+=quantity
            declared_quantity=event.get("quantity",event.get("added_quantity"))
            if partial_close and (not inventory or not any(inventory.values())
                    or isinstance(event.get('remaining_quantity'), bool)
                    or event.get('remaining_quantity') != max(map(abs, inventory.values()), default=0)):
                errors.add('PARTIAL_CLOSE_REMAINDER_MISMATCH')
            if declared_quantity is not None and event_legs and any(abs(l.get("signed_quantity",0))!=declared_quantity for l in event_legs):
                errors.add("QUANTITY_MISMATCH")
        event_refs=event.get("evidence_refs")
        linked_refs={l["evidence_id"] for l in all_links if l.get("event_id")==event_id}
        if settlement_links:
            linked_refs={settlement_links[0]["settlement_evidence_id"],settlement_links[0]["cash_evidence_id"]}
        if (not isinstance(event_refs,list)
                or any(not isinstance(ref, str) or not ref for ref in event_refs)
                or set(event_refs)!=linked_refs):
            errors.add("EVIDENCE_REFERENCE_MISMATCH")
        fee_rows=[f for f in evidence.get("fees",[]) if f.get("event_id")==event_id and f.get("trade_id")==trade_id]
        covered=False
        for fee in fee_rows:
            row=retained.get(fee.get("raw_evidence_id"),{})
            fp=row.get("payload",{})
            scope_errors:set[str]=set()
            check_scope(row,scope_errors)
            check_scope(fp.get("scope"),scope_errors)
            owner = {"activity_id": cash.get("provider_identity")} if settlement_links else {"order_id": package.get("broker_parent_id")}
            if (not scope_errors and expected_scope is not None
                    and fee_payload_matches(fp, scope=expected_scope, **owner)
                    and row.get("kind")=="fee" and fee.get("complete")==1
                    and fp.get("complete") is True and fee.get("amount_cents") is not None
                    and fp.get("amount_cents")==fee["amount_cents"] and fp.get("event_id")==event_id
                    and fp.get("trade_id")==trade_id and fp.get("position_id")==event.get("position_id")
                    and (settlement_links or fp.get("package_id")==event.get("package_id"))):
                covered=True
                refs.add(row["evidence_id"])
        if not covered:
            fees_complete=False
        if errors:
            details[str(event_id or "missing")]=sorted(errors)
            reasons.update(errors)
    if not saw_entry:
        reasons.add("ENTRY_MISSING")
    if not events:
        reasons.add("EMPTY_EVENT_SEQUENCE")
    if any(e.get("event")=="EXIT" for e in events) and any(inventory.values()):
        reasons.add("TERMINAL_QUANTITY_MISMATCH")
    if not any(e.get("event")=="EXIT" for e in events) and events and str(events[0].get("expiry", "9999"))<str(as_of):
        reasons.add("SETTLEMENT_PENDING")
    if reasons:
        expiry=events[0].get("expiry") if events else None
        unresolved=bool(expiry and str(expiry)<str(as_of) and not any(e.get("event")=="EXIT" for e in events))
        return result("unattributed","UNRESOLVED_EXPIRY" if unresolved else "UNATTRIBUTED")
    if not amounts_complete:
        reasons.add("JOURNAL_AMOUNTS_INCOMPLETE")
    if not fees_complete:
        reasons.add("FEE_EVIDENCE_INCOMPLETE")
        return result("accepted","FEE_INCOMPLETE")
    return result("accepted","VERIFIED")
