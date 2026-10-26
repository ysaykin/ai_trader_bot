"""Lumibot trading entrypoint — supports Tradier and Alpaca."""
from __future__ import annotations

import os
import logging
from typing import TYPE_CHECKING
from pathlib import Path

from dotenv import load_dotenv
from lumibot.traders import Trader

if TYPE_CHECKING:
    from lumibot.brokers import Broker
    from lumibot.strategies.strategy import Strategy

load_dotenv()

os.makedirs("logs", exist_ok=True)
log_formatter = logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s")

root_logger = logging.getLogger()
root_logger.setLevel(logging.INFO)

console_handler = logging.StreamHandler()
console_handler.setFormatter(log_formatter)
root_logger.addHandler(console_handler)

try:
    from logging.handlers import RotatingFileHandler
    file_handler = RotatingFileHandler(
        "logs/trading.log", maxBytes=5 * 1024 * 1024, backupCount=3
    )
    file_handler.setFormatter(log_formatter)
    root_logger.addHandler(file_handler)
except Exception as e:
    print(f"Warning: Could not create file handler: {e}")

logger = logging.getLogger("main")


def get_broker() -> Broker:
    """Instantiate broker from env vars (Tradier sandbox or Alpaca)."""
    broker_type = os.getenv("TRADING_BROKER", "").strip().lower()
    if broker_type not in ("", "tradier", "alpaca"):
        raise RuntimeError(f"Unsupported TRADING_BROKER: {broker_type}")
    access_token = os.getenv("TRADIER_ACCESS_TOKEN") or os.getenv("TRADIER_API_KEY")

    # Prioritize an explicit broker selection; otherwise infer from credentials.
    if broker_type == "tradier" or (not broker_type and access_token):
        account_number = os.getenv("TRADIER_ACCOUNT_NUMBER") or os.getenv("TRADIER_ACCOUNT")
        if not access_token or access_token.startswith("your_") or not account_number:
            raise RuntimeError(
                "Tradier requires TRADIER_ACCESS_TOKEN and TRADIER_ACCOUNT_NUMBER "
                "(or TRADIER_API_KEY and TRADIER_ACCOUNT)."
            )
        paper_setting = os.getenv("TRADIER_IS_PAPER", os.getenv("TRADIER_SANDBOX", "true"))
        paper_setting = paper_setting.strip().lower()
        if paper_setting not in ("true", "false", "1", "0"):
            raise RuntimeError("TRADIER_IS_PAPER/TRADIER_SANDBOX must be true, false, 1, or 0.")
        from lumibot.brokers import Tradier

        tradier_config = {
            "ACCESS_TOKEN": access_token,
            "ACCOUNT_NUMBER": account_number,
            "PAPER": paper_setting in ("true", "1"),
        }
        logger.info(
            "Connecting to Tradier (account=%s, paper=%s)...",
            tradier_config["ACCOUNT_NUMBER"], tradier_config["PAPER"],
        )
        return Tradier(tradier_config)

    # Alpaca fallback
    api_key = os.getenv("ALPACA_API_KEY")
    api_secret = os.getenv("ALPACA_API_SECRET")
    if api_key and api_secret and api_key != "your_paper_api_key_here":
        from lumibot.brokers import Alpaca
        paper = os.getenv("ALPACA_PAPER", "true").lower() == "true"
        logger.info("Connecting to Alpaca (paper=%s)...", paper)
        return Alpaca({
            "API_KEY": api_key,
            "API_SECRET": api_secret,
            "PAPER": paper,
        })

    raise RuntimeError(
        "No valid broker credentials found in .env. Please set "
        "TRADIER_ACCESS_TOKEN/TRADIER_ACCOUNT_NUMBER or ALPACA_API_KEY/ALPACA_API_SECRET."
    )


def resolve_trading_mode() -> str:
    """Resolve active execution mode (`single`, `ab`, or `incubator`)."""
    explicit_mode = os.getenv("TRADING_MODE", "").strip().lower()
    if explicit_mode in ("single", "ab", "incubator"):
        return explicit_mode
    if os.getenv("INCUBATOR_ENABLED", "false").strip().lower() in ("true", "1", "yes"):
        return "incubator"
    if os.getenv("AB_TEST_ENABLED", "false").strip().lower() in ("true", "1", "yes"):
        return "ab"
    return "single"


def build_strategies_for_mode(
    broker: Broker,
    mode: str | None = None,
    budget: float = 100_000.0,
) -> list[Strategy]:
    """Instantiate strategies for the resolved execution mode."""
    from strategies.iron_condor import SPXIronCondor
    from strategies.incubator import build_ab_strategies, build_incubator_strategies
    from strategies.market_data_hub import get_shared_market_data_hub

    resolved = mode or resolve_trading_mode()
    hub = get_shared_market_data_hub()

    if resolved == "incubator":
        limit_env = os.getenv("INCUBATOR_STRATEGY_COUNT", "").strip()
        limit = int(limit_env) if limit_env.isdigit() else None
        strategies = build_incubator_strategies(broker=broker, market_data_hub=hub, budget=budget, limit=limit)
    elif resolved == "ab":
        strategies = build_ab_strategies(broker=broker, market_data_hub=hub, budget=budget)
    else:
        try:
            strategy = SPXIronCondor(broker=broker, budget=budget)
        except TypeError:
            strategy = SPXIronCondor()
        if not getattr(strategy, "name", None):
            try:
                strategy.name = "SPXIronCondor"
            except AttributeError:
                strategy._name = "SPXIronCondor"
        strategy._market_data_hub = hub
        strategies = [strategy]

    if os.getenv("TRAJECTORY_LOGGING_ENABLED", "true").strip().lower() in ("true", "1", "yes"):
        from trade_logger import TrajectoryLogger

        try:
            collector = TrajectoryLogger(
                [strategy.name for strategy in strategies],
                Path(os.getenv("TRAJECTORY_LOG_PATH", "logs/trajectories.jsonl")),
            )
        except OSError as error:
            logger.warning("Could not initialize trajectory logging: %s", error)
            return strategies
        for strategy in strategies:
            strategy._trajectory_logger = collector
    return strategies


if __name__ == "__main__":
    from strategies.incubator import attach_strategies_to_host

    broker = get_broker()
    mode = resolve_trading_mode()
    strategies = build_strategies_for_mode(broker=broker, mode=mode, budget=100_000)

    trader = Trader()
    host_strategy = attach_strategies_to_host(strategies)
    trader.add_strategy(host_strategy)

    logger.info(
        "Starting SPX Iron Condor bot in '%s' mode (%d concurrent %s)...",
        mode,
        len(strategies),
        "strategy" if len(strategies) == 1 else "strategies",
    )
    trader.run_all()
