"""
Shared Market Data Hub for Lumibot Multi-Strategy Execution
============================================================

Provides a thread-safe, tick-scoped in-memory cache so that 20+ concurrent
strategies running on a single broker (e.g., Tradier or Alpaca) share a single
option chain lookup, underlying price quote, and option contract quote per
5-minute iteration cycle rather than triggering 20x redundant REST API calls.

Key Features:
  - Thread-safe via `threading.RLock`
  - Automatic 5-minute time-bucket invalidation (configurable TTL)
  - Transparent fallback to direct `strategy.get_chains(...)` and
    `strategy.get_last_price(...)` if no hub is attached
"""

from __future__ import annotations

import logging
import threading
import time
from datetime import datetime
from typing import Any

logger = logging.getLogger(__name__)


def _asset_key(asset: Any) -> str:
    """Build a canonical cache key for a Lumibot Asset or symbol string."""
    if isinstance(asset, str):
        return f"STR:{asset.upper()}"
    symbol = getattr(asset, "symbol", str(asset))
    asset_type = getattr(asset, "asset_type", "stock")
    expiration = getattr(asset, "expiration", None)
    strike = getattr(asset, "strike", None)
    right = getattr(asset, "right", None)
    if expiration is not None or strike is not None or right is not None:
        return f"OPT:{symbol}:{expiration}:{strike}:{right}"
    return f"ASSET:{asset_type}:{symbol}"


class MarketDataHub:
    """
    Thread-safe shared market data cache across concurrent strategy variants.

    Caches:
      1. Option chains (`get_chains`) per underlying symbol & time bucket
      2. Expirations (`get_expiration`) per underlying symbol & time bucket
      3. Strikes (`get_strikes`) per underlying symbol & time bucket
      4. Last prices (`get_last_price`) per asset (underlying or option leg) & time bucket
      5. Cross-strategy telemetry snapshots (latest P&L, strikes, and status of each variant)
    """

    def __init__(self, ttl_seconds: int = 240) -> None:
        """
        Args:
            ttl_seconds: Cache validity duration in seconds. Defaults to 240s
                         (4 minutes) so every 5-minute strategy cycle refreshes
                         once and shares across all 20 strategies in that cycle.
        """
        self.ttl_seconds = max(int(ttl_seconds), 1)
        self._lock = threading.RLock()
        self._chains_cache: dict[tuple[str, int], Any] = {}
        self._expirations_cache: dict[tuple[str, int], list[Any]] = {}
        self._strikes_cache: dict[tuple[str, int], list[float]] = {}
        self._price_cache: dict[tuple[str, int], float] = {}
        self._strategy_snapshots: dict[str, dict[str, Any]] = {}
        self._stats = {
            "chain_hits": 0,
            "chain_misses": 0,
            "price_hits": 0,
            "price_misses": 0,
        }

    def _time_bucket(self, strategy: Any = None) -> int:
        """Compute current integer time bucket from strategy clock or wall clock."""
        if strategy is not None and hasattr(strategy, "get_datetime"):
            try:
                dt = strategy.get_datetime()
                if isinstance(dt, datetime):
                    return int(dt.timestamp()) // self.ttl_seconds
            except Exception:
                pass
        return int(time.time()) // self.ttl_seconds

    def clear(self) -> None:
        """Clear all cached market data."""
        with self._lock:
            self._chains_cache.clear()
            self._expirations_cache.clear()
            self._strikes_cache.clear()
            self._price_cache.clear()

    def get_stats(self) -> dict[str, int]:
        """Return cache hit/miss statistics."""
        with self._lock:
            return dict(self._stats)

    def get_last_price(self, strategy: Any, asset: Any) -> float | None:
        """Fetch last price for `asset`, deduplicated across strategies in the current tick bucket."""
        bucket = self._time_bucket(strategy)
        key = (_asset_key(asset), bucket)

        with self._lock:
            if key in self._price_cache:
                self._stats["price_hits"] += 1
                return self._price_cache[key]

        price = strategy.get_last_price(asset)
        if price is None:
            return None

        price_val = float(price)
        with self._lock:
            self._stats["price_misses"] += 1
            self._price_cache[key] = price_val
        return price_val

    def get_chains(self, strategy: Any, underlying: Any) -> Any:
        """Fetch option chains for `underlying`, deduplicated across strategies in the current tick bucket."""
        bucket = self._time_bucket(strategy)
        key = (_asset_key(underlying), bucket)

        with self._lock:
            if key in self._chains_cache:
                self._stats["chain_hits"] += 1
                return self._chains_cache[key]

        chains = strategy.get_chains(underlying)
        if not chains:
            return chains

        with self._lock:
            self._stats["chain_misses"] += 1
            self._chains_cache[key] = chains
        return chains

    def get_expiration(self, strategy: Any, underlying: Any, chains: Any = None) -> list[Any]:
        """Fetch option expirations for `underlying`, using cached chains when available."""
        bucket = self._time_bucket(strategy)
        key = (_asset_key(underlying), bucket)

        with self._lock:
            if key in self._expirations_cache:
                return self._expirations_cache[key]

        if chains is None:
            chains = self.get_chains(strategy, underlying)
        if not chains:
            return []

        expirations = strategy.get_expiration(chains)
        if not expirations:
            return []

        with self._lock:
            self._expirations_cache[key] = list(expirations)
        return list(expirations)

    def get_strikes(self, strategy: Any, underlying: Any, chains: Any = None) -> list[float]:
        """Fetch option strikes for `underlying`, using cached chains when available."""
        bucket = self._time_bucket(strategy)
        key = (_asset_key(underlying), bucket)

        with self._lock:
            if key in self._strikes_cache:
                return self._strikes_cache[key]

        if chains is None:
            chains = self.get_chains(strategy, underlying)
        if not chains:
            return []

        strikes = strategy.get_strikes(underlying, chains=chains)
        if not strikes:
            return []

        sorted_strikes = sorted(float(s) for s in strikes)
        with self._lock:
            self._strikes_cache[key] = sorted_strikes
        return sorted_strikes

    def update_strategy_snapshot(self, strategy_name: str, snapshot: dict[str, Any]) -> None:
        """Record latest intraday telemetry for a strategy variant in the 20-strategy grid."""
        with self._lock:
            self._strategy_snapshots[strategy_name] = dict(snapshot)

    def get_all_strategy_snapshots(self) -> dict[str, dict[str, Any]]:
        """Return a copy of all registered strategy snapshots for cross-strategy signal extraction."""
        with self._lock:
            return {k: dict(v) for k, v in self._strategy_snapshots.items()}


_DEFAULT_HUB: MarketDataHub | None = None
_HUB_LOCK = threading.Lock()


def get_shared_market_data_hub(ttl_seconds: int = 240) -> MarketDataHub:
    """Return the process-wide singleton `MarketDataHub` instance."""
    global _DEFAULT_HUB
    with _HUB_LOCK:
        if _DEFAULT_HUB is None:
            _DEFAULT_HUB = MarketDataHub(ttl_seconds=ttl_seconds)
        return _DEFAULT_HUB
