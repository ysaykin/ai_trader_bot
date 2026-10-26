"""Trade persistence and performance analytics engine for Lumibot.

Stores trade lifecycle events in logs/trades.jsonl and calculates
100% real historical performance metrics (Win Rate, P&L, Drawdown, Profit Factor).
"""
from __future__ import annotations

import os
import sys
import json
import logging
import threading
from collections.abc import Mapping
from pathlib import Path
from datetime import datetime, time, timezone
from typing import Dict, Any, List, Optional

_ROOT_DIR = Path(__file__).resolve().parent
if not (_ROOT_DIR / "ml").exists() and (_ROOT_DIR.parent / "ml").exists():
    _ROOT_DIR = _ROOT_DIR.parent
if str(_ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(_ROOT_DIR))

from ml.features import GREEKS, MARKET_TZ, finite_number, grid_features, market_features

DEFAULT_LOG_PATH = Path("logs/trades.jsonl")
DEFAULT_TRAJECTORY_PATH = Path("logs/trajectories.jsonl")
logger = logging.getLogger(__name__)


def _strategy_name(strategy: Any) -> str:
    name = getattr(strategy, "name", None)
    if name:
        return str(name)
    params = getattr(strategy, "parameters", None)
    if isinstance(params, Mapping) and params.get("strategy_name"):
        return str(params["strategy_name"])
    return type(strategy).__name__


def _resolve_single_trade_pct(max_position_risk: float) -> float:
    override = finite_number((os.getenv("IC_MAX_SINGLE_TRADE_PCT") or "").strip() or None)
    if override is not None and 0.0 < override <= 1.0:
        return override
    if max_position_risk <= 5000:
        return 0.30
    if max_position_risk <= 10000:
        return 0.20
    if max_position_risk <= 25000:
        return 0.15
    if max_position_risk <= 50000:
        return 0.10
    return 0.05


def _read_trajectories(log_path: Path) -> list[dict[str, Any]]:
    if not log_path.exists():
        return []
    rows = []
    with log_path.open(encoding="utf-8") as stream:
        for line in stream:
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                logger.warning("Ignoring malformed trajectory record in %s", log_path)
                continue
            if isinstance(row, dict) and row.get("schema_version") == 1:
                rows.append(row)
    return rows


def _append_trajectory(log_path: Path, row: dict[str, Any]) -> None:
    payload = json.dumps(row, allow_nan=False, sort_keys=True)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("a", encoding="utf-8") as stream:
        stream.write(payload + "\n")


def relabel_session(session: str, log_path: Path = DEFAULT_TRAJECTORY_PATH) -> int:
    """Append reproducible EOD labels without changing causal observations.

    Compare immediate HOLD, doubling, a whole-contract 50% trim, and flattening,
    followed by holding the remaining fixed basket to the recorded session end.
    Score = incremental P&L / original defined risk - squared adverse excursion
    / original risk squared. Marks assume no fees/slippage and are estimates,
    not fills. Only ADDs satisfying recorded single/global risk caps qualify.
    Incomplete paths and baskets without a terminal mark remain unlabelled.
    """
    records = [row for row in _read_trajectories(log_path) if row.get("session") == session]
    terminal = next((row for row in reversed(records) if row.get("event") == "SESSION_END"), None)
    if terminal is None:
        return 0
    ticks = sorted((row for row in records if row.get("event") == "SNAPSHOT"), key=lambda row: row["timestamp"])
    labelled = {row["source_id"] for row in records if row.get("event") == "HINDSIGHT_LABEL"}
    count = 0
    for index, tick in enumerate(ticks):
        if tick["record_id"] in labelled:
            continue
        labels = {}
        for name, state in tick["strategies"].items():
            if not state or not state.get("trajectory_id"):
                continue
            end = terminal["strategies"].get(name)
            if not end or end.get("trajectory_id") != state["trajectory_id"]:
                continue
            path = [row["strategies"].get(name) for row in [*ticks[index:], terminal]]
            if any(
                not item
                or item.get("trajectory_id") != state["trajectory_id"]
                or finite_number(item.get("pnl")) is None
                for item in path
            ):
                continue
            # Missing five-minute buckets would understate adverse excursions.
            stamps = [datetime.fromisoformat(row["bucket_start"]) for row in [*ticks[index:], terminal]]
            if any((right - left).total_seconds() > 300 for left, right in zip(stamps, stamps[1:])):
                continue
            risk = finite_number(state.get("max_risk"))
            if risk is None or risk <= 0:
                continue
            pnl = state["pnl"]
            gain = end["pnl"] - pnl
            adverse = max(pnl - min(item["pnl"] for item in path), 0.0)
            quantity = state["quantity"]
            multiples = {"HOLD": 1.0 if state["position_open"] else 0.0}
            if state["position_open"]:
                multiples["FLATTEN_NOW"] = 0.0
                if quantity >= 2:
                    multiples["TRIM_POSITION"] = (quantity - quantity // 2) / quantity
                if state.get("can_add"):
                    multiples["ADD_POSITION"] = 2.0
            scores = {
                action: scale * gain / risk - (scale * adverse / risk) ** 2
                for action, scale in multiples.items()
            }
            best = max(scores, key=scores.get)
            opening_credit = finite_number(state.get("entry_credit"))
            best_future_pnl = max(item["pnl"] for item in path)
            profit_target = None
            if opening_credit is not None and opening_credit > 0:
                profit_target = min(max(best_future_pnl / (opening_credit * 100.0), 0.15), 0.80)
            labels[name] = {
                "action": best,
                "scores": scores,
                "future_pnl": gain,
                "max_adverse_excursion": adverse,
                "profit_target": profit_target,
            }
        best_strategy = max(labels, key=lambda name: max(labels[name]["scores"].values())) if labels else None
        _append_trajectory(log_path, {
            "schema_version": 1, "event": "HINDSIGHT_LABEL", "session": session,
            "source_id": tick["record_id"], "timestamp": tick["timestamp"],
            "horizon_timestamp": terminal["timestamp"], "label_version": "eod_mark_risk_v1",
            "strategies": labels, "best_strategy": best_strategy,
        })
        count += 1
    return count


class TrajectoryLogger:
    """One process-local, thread-safe collector for an entire strategy grid.

    Each strategy reports once after its iteration, including flat/early-return
    ticks. A row is emitted when the grid is complete, or as explicitly partial
    when the next bucket arrives. Market history and contract Greeks are fetched
    once per bucket across the grid. Shadow baskets continue after actual exits
    so EOD labels do not confuse going flat with a zero-valued contract basket.
    """

    def __init__(
        self,
        strategy_names: list[str],
        log_path: Path = DEFAULT_TRAJECTORY_PATH,
        completed_snapshot_callback: Any | None = None,
    ) -> None:
        if not strategy_names or len(set(strategy_names)) != len(strategy_names):
            raise ValueError("Trajectory strategy names must be nonempty and unique")
        self.names = tuple(strategy_names)
        self.log_path = Path(log_path)
        self._lock = threading.RLock()
        self._pending: dict[str, Any] | None = None
        self._bucket: int | None = None
        self._event = ""
        self._market: dict[str, Any] = {}
        self._cache: dict[tuple[Any, ...], Any] = {}
        self._books: dict[str, dict[str, Any]] = {}
        self._previous_grid: dict[str, Any] = {}
        self._previous_states: dict[str, dict[str, Any]] = {}
        self._session = ""
        self._written = {row["record_id"] for row in _read_trajectories(self.log_path) if "record_id" in row}
        self._completed_snapshot_callback = completed_snapshot_callback

    def _optional(self, callback: Any, *args: Any, **kwargs: Any) -> Any:
        try:
            return callback(*args, **kwargs)
        except Exception as error:
            logger.debug("Trajectory market data unavailable: %s", error)
            return None

    def _cached(self, kind: str, strategy: Any, asset: Any) -> Any:
        key = (
            kind, str(getattr(asset, "symbol", asset)),
            str(getattr(asset, "expiration", "")),
            str(getattr(asset, "strike", "")), str(getattr(asset, "right", "")),
        )
        if key not in self._cache:
            callback = getattr(strategy, "_fetch_last_price" if kind == "price" else "get_greeks", None)
            self._cache[key] = self._optional(callback, asset) if callback else None
        return self._cache[key]

    def _market_state(self, strategy: Any, now: datetime) -> dict[str, Any]:
        fetch = getattr(strategy, "get_historical_prices", None)
        bars = self._optional(fetch, strategy._underlying, 390, timestep="minute") if fetch else None
        rows = []
        if bars is not None:
            try:
                frame = getattr(bars, "pandas_df", None)
                if frame is None:
                    frame = getattr(bars, "df", None)
                if frame is not None:
                    rows = [dict(row, timestamp=stamp) for stamp, row in zip(frame.index, frame.to_dict("records"))]
                elif isinstance(bars, list):
                    rows = bars
            except (AttributeError, TypeError, ValueError) as error:
                logger.debug("Trajectory history unavailable: %s", error)
        return market_features(
            rows, now, self._cached("price", strategy, "VIX"),
            self._cached("price", strategy, "VIX9D"),
        )

    def _strategy_state(self, strategy: Any, now: datetime, action: str) -> dict[str, Any]:
        name = _strategy_name(strategy)
        parameters = strategy.parameters
        legs = getattr(strategy, "_owned_legs", [])
        credit = finite_number(getattr(strategy, "_entry_credit", None))
        entries = getattr(strategy, "_entry_orders", [])
        book = self._books.get(name)
        if len(legs) == 4 and credit is not None and credit > 0:
            entry_token = (
                entries[0]
                if entries
                else tuple(
                    (
                        str(getattr(asset, "symbol", "")),
                        str(getattr(asset, "expiration", "")),
                        float(getattr(asset, "strike", 0.0)),
                        str(getattr(asset, "right", "")),
                    )
                    for asset, _ in legs
                )
            )
            if book is None or book["entry"] != entry_token:
                book = {
                    "entry": entry_token, "legs": list(legs), "credit": credit,
                    "trade_id": getattr(strategy, "_current_trade_id", None),
                    "trajectory_id": f"{name}:{now.isoformat()}",
                    "quantity": int(parameters["quantity"]),
                }
                self._books[name] = book
        quantity = book["quantity"] if book else int(parameters["quantity"])
        risk = float(parameters["wing_width"]) * 100 * quantity
        capped_position_risk = min(float(parameters["max_position_risk"]), 5000.0)
        state = {
            "observed_at": now.astimezone(timezone.utc).isoformat(),
            "action": action, "position_open": bool(strategy._position_open),
            "trajectory_id": book["trajectory_id"] if book else None,
            "trade_id": book["trade_id"] if book else None,
            "entry_credit": book["credit"] if book else None,
            "parameters": {
                key: parameters[key]
                for key in ("symbol", "dte", "short_delta", "wing_width", "profit_target", "stop_loss_mult", "entry_time")
                if key in parameters
            },
            "quantity": quantity, "max_risk": risk, "pnl": None,
            "unrealized_pnl": None if strategy._position_open else 0.0,
            "put_pressure": None, "call_pressure": None,
            "max_position_risk": capped_position_risk,
            "single_trade_pct": _resolve_single_trade_pct(capped_position_risk),
            "exit_requested": bool(strategy._exit_requested),
            **{greek: None if strategy._position_open else 0.0 for greek in GREEKS},
        }
        if book is None:
            return state
        prices = [finite_number(self._cached("price", strategy, asset)) for asset, _ in book["legs"]]
        if all(price is not None and price >= 0 for price in prices):
            state["pnl"] = book["credit"] * 100 + sum(
                price * signed_qty * 100
                for price, (_, signed_qty) in zip(prices, book["legs"])
            )
            if strategy._position_open:
                state["unrealized_pnl"] = state["pnl"]
        if strategy._position_open:
            greek_rows = [self._cached("greeks", strategy, asset) for asset, _ in book["legs"]]
            for greek in GREEKS:
                values = [
                    finite_number(row.get(greek) if row.get(greek) is not None else row.get(greek.capitalize()))
                    if isinstance(row, Mapping) else None
                    for row in greek_rows
                ]
                if all(value is not None for value in values):
                    state[greek] = sum(
                        value * signed_qty * 100
                        for value, (_, signed_qty) in zip(values, book["legs"])
                    )
            underlying = finite_number(self._cached("price", strategy, strategy._underlying))
            if underlying is not None:
                for asset, signed_qty in book["legs"]:
                    if signed_qty < 0:
                        right = str(asset.right).lower()
                        distance = (
                            underlying - float(asset.strike) if "put" in right
                            else float(asset.strike) - underlying
                        )
                        pressure = max(1 - distance / float(parameters["wing_width"]), 0.0)
                        state["put_pressure" if "put" in right else "call_pressure"] = pressure
        return state

    def record(
        self, strategy: Any, action: str, *, session_end: bool = False
    ) -> dict[str, Any] | None:
        """Collect a tick and return the last row finalized by this call."""
        now = strategy.get_datetime()
        if now.tzinfo is None:
            raise ValueError("Strategy clock must be timezone-aware")
        local = now.astimezone(MARKET_TZ)
        if not session_end and (local.weekday() >= 5 or not time(9, 30) <= local.time() < time(16)):
            return
        stamp = now.astimezone(timezone.utc)
        closing = datetime.combine(local.date(), time(16), MARKET_TZ).astimezone(timezone.utc)
        bucket_stamp = min(stamp, closing) if session_end else stamp
        bucket = int(bucket_stamp.timestamp()) // 300
        session = local.date().isoformat()
        event = "SESSION_END" if session_end else "SNAPSHOT"
        record_id = f"{session}:{bucket}:{event}"
        name = _strategy_name(strategy)
        finalized: list[dict[str, Any]] = []
        with self._lock:
            if name not in self.names:
                raise ValueError(f"Unregistered trajectory strategy: {name}")
            if record_id in self._written:
                if session_end:
                    relabel_session(session, self.log_path)
                return None
            if self._bucket is not None and bucket < self._bucket:
                return
            if self._pending and self._pending["record_id"] != record_id:
                row = self._flush()
                if row is not None:
                    finalized.append(row)
            if session != self._session:
                self._books.clear()
                self._previous_states.clear()
                self._previous_grid.clear()
                self._session = session
            if bucket != self._bucket or event != self._event:
                self._cache.clear()
                self._bucket = bucket
                self._event = event
                self._market = self._market_state(strategy, now)
                if session_end:
                    self._market["minutes_to_close"] = 0.0
            if self._pending is None:
                self._pending = {
                    "schema_version": 1, "event": event, "record_id": record_id,
                    "timestamp": stamp.isoformat(),
                    "bucket_start": datetime.fromtimestamp(bucket * 300, timezone.utc).isoformat(),
                    "market_as_of": stamp.isoformat(),
                    "valuation": "estimated_fixed_basket_no_costs",
                    "session": session, "market": dict(self._market),
                    "strategies": dict.fromkeys(self.names),
                }
            self._pending["strategies"][name] = self._strategy_state(strategy, now, action)
            # The row becomes available after all its provider reads, not at the
            # beginning of the bucket (which could introduce temporal leakage).
            self._pending["strategies"][name]["observed_at"] = (
                strategy.get_datetime().astimezone(timezone.utc).isoformat()
            )
            if all(state is not None for state in self._pending["strategies"].values()):
                row = self._flush()
                if row is not None:
                    finalized.append(row)
        callback = self._completed_snapshot_callback
        if callback is not None:
            for row in finalized:
                try:
                    callback(row)
                except Exception as error:
                    logger.warning("Completed snapshot callback failed: %s", error)
        return finalized[-1] if finalized else None

    def _flush(self) -> dict[str, Any] | None:
        row = self._pending
        if row is None:
            return None
        states = row["strategies"]
        row["timestamp"] = max(state["observed_at"] for state in states.values() if state)
        total_risk = sum(state["max_risk"] for state in states.values() if state and state["position_open"])
        complete = all(state is not None for state in states.values())
        for name, state in states.items():
            if state is None:
                continue
            risk = state["max_risk"]
            state["can_add"] = bool(
                complete and state["position_open"] and not state["exit_requested"]
                and risk * 2 <= state["max_position_risk"] * state["single_trade_pct"]
                and total_risk + risk <= state["max_position_risk"]
            )
            previous = self._previous_states.get(name)
            state["reward"] = None
            if (
                previous and previous["trajectory_id"] == state["trajectory_id"]
                and state["pnl"] is not None and previous["pnl"] is not None
            ):
                state["reward"] = state["pnl"] - previous["pnl"]
        row["grid"] = grid_features(states, self._previous_grid)
        row["complete"] = complete
        _append_trajectory(self.log_path, row)
        self._written.add(row["record_id"])
        self._previous_grid = row["grid"]
        self._previous_states = {name: dict(state) for name, state in states.items() if state}
        self._pending = None
        if row["event"] == "SESSION_END":
            relabel_session(row["session"], self.log_path)
        return row


def record_trade_entry(
    strategy_name: str,
    symbol: str,
    expiry: str,
    strikes: Dict[str, float],
    quantity: int,
    credit: float,
    max_risk: float,
    trade_id: Optional[str] = None,
    log_path: Path = DEFAULT_LOG_PATH,
) -> str:
    """Record a trade entry event to logs/trades.jsonl."""
    log_path.parent.mkdir(parents=True, exist_ok=True)
    if not trade_id:
        trade_id = f"trade_{datetime.now().strftime('%Y%m%d_%H%M%S')}_{strategy_name.lower()[:7]}"

    entry_data = {
        "event": "ENTRY",
        "trade_id": trade_id,
        "timestamp": datetime.now().isoformat(),
        "strategy": strategy_name,
        "symbol": symbol,
        "expiry": str(expiry),
        "strikes": strikes,
        "quantity": quantity,
        "credit": float(credit),
        "max_risk": float(max_risk),
        "status": "OPEN",
    }

    with open(log_path, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry_data) + "\n")

    return trade_id


def record_trade_exit(
    trade_id: str,
    strategy_name: str,
    exit_reason: str,
    pnl: float,
    exit_price: float = 0.0,
    mfe_pnl: Optional[float] = None,
    mae_pnl: Optional[float] = None,
    log_path: Path = DEFAULT_LOG_PATH,
) -> None:
    """Record a trade exit event to logs/trades.jsonl."""
    log_path.parent.mkdir(parents=True, exist_ok=True)
    exit_data: Dict[str, Any] = {
        "event": "EXIT",
        "trade_id": trade_id,
        "timestamp": datetime.now().isoformat(),
        "strategy": strategy_name,
        "exit_reason": exit_reason,
        "pnl": float(pnl),
        "exit_price": float(exit_price),
        "status": "CLOSED",
    }
    if mfe_pnl is not None:
        exit_data["mfe_pnl"] = float(mfe_pnl)
    if mae_pnl is not None:
        exit_data["mae_pnl"] = float(mae_pnl)

    with open(log_path, "a", encoding="utf-8") as f:
        f.write(json.dumps(exit_data) + "\n")


def load_all_trades(log_path: Path = DEFAULT_LOG_PATH) -> List[Dict[str, Any]]:
    """Parse logs/trades.jsonl and reconcile entries and exits into completed trade records."""
    if not log_path.exists():
        return []

    entries: Dict[str, Dict[str, Any]] = {}
    completed_trades: List[Dict[str, Any]] = []

    try:
        with open(log_path, "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    record = json.loads(line)
                    t_id = record.get("trade_id")
                    ev = record.get("event")

                    if ev == "ENTRY":
                        entries[t_id] = record
                    elif ev == "EXIT" and t_id in entries:
                        entry = entries.pop(t_id)
                        entry_dt = datetime.fromisoformat(entry["timestamp"])
                        exit_dt = datetime.fromisoformat(record["timestamp"])
                        duration_mins = max(round((exit_dt - entry_dt).total_seconds() / 60.0, 1), 0.0)

                        completed_trades.append({
                            "trade_id": t_id,
                            "strategy": entry.get("strategy", "SPXIronCondor"),
                            "symbol": entry.get("symbol", "XSP"),
                            "entry_time": entry["timestamp"],
                            "exit_time": record["timestamp"],
                            "duration_minutes": duration_mins,
                            "credit_received": entry.get("credit", 0.0),
                            "max_risk": entry.get("max_risk", 0.0),
                            "quantity": entry.get("quantity", 1),
                            "pnl": record.get("pnl", 0.0),
                            "mfe_pnl": record.get("mfe_pnl"),
                            "mae_pnl": record.get("mae_pnl"),
                            "exit_reason": record.get("exit_reason", "MANUAL"),
                            "status": "CLOSED",
                        })
                except json.JSONDecodeError:
                    continue

        # Add remaining open entries
        for t_id, entry in entries.items():
            completed_trades.append({
                "trade_id": t_id,
                "strategy": entry.get("strategy", "SPXIronCondor"),
                "symbol": entry.get("symbol", "XSP"),
                "entry_time": entry["timestamp"],
                "exit_time": "OPEN",
                "duration_minutes": 0.0,
                "credit_received": entry.get("credit", 0.0),
                "max_risk": entry.get("max_risk", 0.0),
                "quantity": entry.get("quantity", 1),
                "pnl": 0.0,
                "mfe_pnl": None,
                "mae_pnl": None,
                "exit_reason": "IN_PROGRESS",
                "status": "OPEN",
            })

    except Exception:
        return []

    return completed_trades


def compute_strategy_metrics(trades: List[Dict[str, Any]], strategy_name: str) -> Dict[str, Any]:
    """Compute real performance metrics for a specific strategy from trade history."""
    strat_trades = [t for t in trades if strategy_name.lower() in t["strategy"].lower() and t["status"] == "CLOSED"]
    
    total_trades = len(strat_trades)
    if total_trades == 0:
        return {
            "status": "🟢 Ready / Monitoring",
            "total_trades": 0,
            "win_rate": "N/A (0 trades)",
            "realized_pnl": "$0.00",
            "avg_credit": "$0.00",
            "avg_duration": "0 mins",
            "max_drawdown": "$0.00",
            "profit_factor": "N/A",
            "wins": 0,
            "losses": 0,
            "raw_pnl": 0.0,
        }

    pnls = [t["pnl"] for t in strat_trades]
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p < 0]
    total_pnl = sum(pnls)

    win_rate_pct = (len(wins) / total_trades) * 100.0
    avg_credit = sum(t["credit_received"] for t in strat_trades) / total_trades
    avg_duration = sum(t["duration_minutes"] for t in strat_trades) / total_trades

    gross_profit = sum(wins)
    gross_loss = abs(sum(losses))
    profit_factor = f"{(gross_profit / gross_loss):.2f}" if gross_loss > 0 else ("Inf" if gross_profit > 0 else "N/A")

    # Max Drawdown calculation
    cum_pnl = 0.0
    peak = 0.0
    max_dd = 0.0
    for p in pnls:
        cum_pnl += p
        if cum_pnl > peak:
            peak = cum_pnl
        dd = peak - cum_pnl
        if dd > max_dd:
            max_dd = dd

    dur_hours = int(avg_duration // 60)
    dur_mins = int(avg_duration % 60)
    duration_str = f"{dur_hours}h {dur_mins}m" if dur_hours > 0 else f"{dur_mins} mins"

    return {
        "status": "🟢 Active Trading",
        "total_trades": total_trades,
        "win_rate": f"{win_rate_pct:.1f}%",
        "realized_pnl": f"{'+' if total_pnl >= 0 else ''}${total_pnl:,.2f}",
        "avg_credit": f"${avg_credit:.2f} / contract",
        "avg_duration": duration_str,
        "max_drawdown": f"-${max_dd:,.2f}",
        "profit_factor": profit_factor,
        "wins": len(wins),
        "losses": len(losses),
        "raw_pnl": total_pnl,
    }
