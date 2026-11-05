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

_VALID_ENVIRONMENTS = {"test", "paper", "live", "backtest"}

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
            mapping.get("TRADIER_ACCOUNT_ID")
            or mapping.get("TRADIER_ACCOUNT_NUMBER")
            or mapping.get("ALPACA_ACCOUNT_ID")
            or "default"
        ).strip()
        is_paper = str(
            mapping.get("TRADIER_PAPER")
            or mapping.get("TRADIER_IS_PAPER")
            or mapping.get("TRADIER_SANDBOX")
            or "true"
        ).strip().lower() in {"true", "1", "yes", "on"}
        environment = "paper" if is_paper else "live"
        return cls(environment=environment, broker=broker, account_id=account)


def is_protected_runtime_path(path: str | Path) -> bool:
    """Return True if path resolves to or inside the production logs directory."""
    try:
        resolved = Path(path).expanduser().resolve()
    except Exception:
        return False
    if resolved in _PROTECTED_FILES:
        return True
    if resolved == _PROTECTED_DIR:
        return True
    return _PROTECTED_DIR in resolved.parents


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


def install_test_context(root: str | Path | None = None, install_guards: bool = True) -> Path:
    """Install sticky test context, directory redirect, and safety guards."""
    global _test_root
    with _lock:
        if _test_root is not None:
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
                    is_ro = uri and "mode=ro" in db_str
                    if not is_ro:
                        raise PermissionError(
                            f"Test write guard blocked non-read-only connection to protected database: {database}"
                        )
            return _orig_sqlite_connect(database, *args, **kwargs)

        guarded_sqlite_connect._is_guard = True  # type: ignore[attr-defined]
        sqlite3.connect = guarded_sqlite_connect  # type: ignore[assignment]
    except ImportError:
        pass

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


def resolve_record_path(variable: str, default: str, scope: RecordScope | None = None) -> Path:
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
        return (effective_root / default).resolve()

    if scope is not None and not scope.is_test:
        return scope.default_record_path(default)

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
