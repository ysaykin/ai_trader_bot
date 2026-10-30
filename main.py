"""Lumibot trading entrypoint — supports Tradier and Alpaca."""
from __future__ import annotations

import os
import logging
import time
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


class ConfigurationError(RuntimeError):
    """Unrecoverable configuration problem; restarting cannot fix it."""


def _env_bool(*names: str, default: bool = False) -> bool:
    """Parse the first configured environment boolean in precedence order."""
    for name in names:
        raw = os.getenv(name)
        if raw is None or not raw.strip():
            continue
        normalized = raw.strip().lower()
        if normalized in ("true", "1", "yes", "on"):
            return True
        if normalized in ("false", "0", "no", "off"):
            return False
        raise ConfigurationError(f"{name} must be true, false, 1, 0, yes, no, on, or off.")
    return default


def _tradier_paper() -> bool:
    return _env_bool(
        "TRADIER_PAPER", "TRADIER_IS_PAPER", "TRADIER_SANDBOX", default=True
    )


def get_broker() -> Broker:
    """Instantiate broker from env vars (Tradier sandbox or Alpaca)."""
    broker_type = os.getenv("TRADING_BROKER", "").strip().lower()
    if broker_type not in ("", "tradier", "alpaca"):
        raise ConfigurationError(f"Unsupported TRADING_BROKER: {broker_type}")
    access_token = os.getenv("TRADIER_ACCESS_TOKEN") or os.getenv("TRADIER_API_KEY")

    # Prioritize an explicit broker selection; otherwise infer from credentials.
    if broker_type == "tradier" or (not broker_type and access_token):
        account_number = os.getenv("TRADIER_ACCOUNT_NUMBER") or os.getenv("TRADIER_ACCOUNT")
        token_str = (access_token or "").strip()
        account_str = (account_number or "").strip()
        if not token_str or token_str.startswith("your_") or not account_str or account_str.startswith("your_"):
            raise ConfigurationError(
                "Tradier requires TRADIER_ACCESS_TOKEN and TRADIER_ACCOUNT_NUMBER "
                "(or TRADIER_API_KEY and TRADIER_ACCOUNT)."
            )
        from lumibot.brokers import Tradier

        is_paper = _tradier_paper()
        tradier_config = {
            "ACCESS_TOKEN": token_str,
            "ACCOUNT_NUMBER": account_str,
            "PAPER": is_paper,
        }
        raw_poll = os.getenv("TRADIER_POLLING_INTERVAL", "15.0" if is_paper else "5.0").strip()
        try:
            polling_interval = max(float(raw_poll), 1.0)
        except ValueError:
            polling_interval = 15.0 if is_paper else 5.0
        logger.info(
            "Connecting to Tradier (paper=%s, polling_interval=%.1fs)...",
            tradier_config["PAPER"], polling_interval,
        )
        return Tradier(tradier_config, polling_interval=polling_interval)

    # Alpaca fallback
    api_key = os.getenv("ALPACA_API_KEY")
    api_secret = os.getenv("ALPACA_API_SECRET")
    if api_key and api_secret and api_key != "your_paper_api_key_here":
        from lumibot.brokers import Alpaca
        paper = _env_bool("ALPACA_PAPER", default=True)
        logger.info("Connecting to Alpaca (paper=%s)...", paper)
        return Alpaca({
            "API_KEY": api_key,
            "API_SECRET": api_secret,
            "PAPER": paper,
        })

    raise ConfigurationError(
        "No valid broker credentials found in .env. Please set "
        "TRADIER_ACCESS_TOKEN/TRADIER_ACCOUNT_NUMBER or ALPACA_API_KEY/ALPACA_API_SECRET."
    )


def _configured_sequence_length() -> int | None:
    raw = os.getenv("ML_SEQUENCE_LENGTH")
    if raw is None or not raw.strip():
        return None
    try:
        parsed = int(raw.strip())
    except ValueError as error:
        raise ConfigurationError("ML_SEQUENCE_LENGTH must be a positive integer.") from error
    if parsed < 1:
        raise ConfigurationError("ML_SEQUENCE_LENGTH must be a positive integer.")
    return parsed


def validate_runtime_configuration() -> None:
    """Fail before strategy construction when the requested runtime is unsafe."""
    broker = os.getenv("TRADING_BROKER", "").strip().lower()
    data_source = os.getenv("DATA_SOURCE", "").strip().lower()
    if data_source not in {"", "tradier"}:
        raise ConfigurationError(
            f"Unsupported DATA_SOURCE: {data_source}. This runner supports Tradier data only."
        )
    if broker == "tradier":
        access_token = (os.getenv("TRADIER_ACCESS_TOKEN") or os.getenv("TRADIER_API_KEY") or "").strip()
        account_number = (os.getenv("TRADIER_ACCOUNT_NUMBER") or os.getenv("TRADIER_ACCOUNT") or "").strip()
        if not access_token or access_token.startswith("your_") or not account_number or account_number.startswith("your_"):
            raise ConfigurationError(
                "TRADING_BROKER=tradier requires TRADIER_ACCESS_TOKEN and "
                "TRADIER_ACCOUNT_NUMBER."
            )
        _tradier_paper()
    if _env_bool("ML_POLICY_ENABLED", default=False):
        from ml.inference import LivePolicyController

        LivePolicyController(
            Path(os.getenv("ML_MODEL_PATH", "logs/models/policy.json")),
            expected_sequence_length=_configured_sequence_length(),
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
    from strategies.position_ledger import PositionLedger

    resolved = mode or resolve_trading_mode()
    hub = get_shared_market_data_hub()
    ledger = PositionLedger(os.getenv("POSITION_LEDGER_PATH", "logs/position_ledger.sqlite3"))

    if resolved == "incubator":
        limit_env = os.getenv("INCUBATOR_STRATEGY_COUNT", "").strip()
        limit = int(limit_env) if limit_env.isdigit() else None
        strategies = build_incubator_strategies(
            broker=broker, market_data_hub=hub, budget=budget, limit=limit,
            position_ledger=ledger,
        )
    elif resolved == "ab":
        strategies = build_ab_strategies(
            broker=broker, market_data_hub=hub, budget=budget, position_ledger=ledger
        )
    else:
        try:
            strategy = SPXIronCondor(
                broker=broker,
                budget=budget,
                parameters={**SPXIronCondor.parameters, "position_ledger": ledger},
            )
        except TypeError:
            strategy = SPXIronCondor()
        if not getattr(strategy, "name", None):
            try:
                strategy.name = "SPXIronCondor"
            except AttributeError:
                strategy._name = "SPXIronCondor"
        strategy._market_data_hub = hub
        strategy._position_ledger = ledger
        strategies = [strategy]

    controller = None
    has_neural_strategy = any(
        getattr(strategy, "pure_neural_mode", False)
        or getattr(strategy, "parameters", {}).get("pure_neural_mode", False)
        for strategy in strategies
    )
    model_path = Path(os.getenv("ML_MODEL_PATH", "logs/models/policy.json"))
    if _env_bool("ML_POLICY_ENABLED", default=False):
        from ml.inference import LivePolicyController

        controller = LivePolicyController(
            model_path,
            {strategy.name: strategy for strategy in strategies},
            expected_sequence_length=_configured_sequence_length(),
        )
    elif has_neural_strategy and model_path.is_file():
        try:
            from ml.inference import LivePolicyController

            controller = LivePolicyController(
                model_path,
                {strategy.name: strategy for strategy in strategies},
                expected_sequence_length=_configured_sequence_length(),
            )
            logger.info("Attached LivePolicyController to neural-managed strategy")
        except Exception as exc:
            logger.warning("Could not initialize LivePolicyController for neural strategy: %s", exc)
    if controller is not None:
        for strategy in strategies:
            strategy._policy_controller = controller
    if os.getenv("TRAJECTORY_LOGGING_ENABLED", "true").strip().lower() in ("true", "1", "yes"):
        from trade_logger import TrajectoryLogger

        try:
            collector = TrajectoryLogger(
                [strategy.name for strategy in strategies],
                Path(os.getenv("TRAJECTORY_LOG_PATH", "logs/trajectories.jsonl")),
                completed_snapshot_callback=(controller.on_snapshot if controller else None),
            )
        except OSError as error:
            logger.warning("Could not initialize trajectory logging: %s", error)
            return strategies
        for strategy in strategies:
            strategy._trajectory_logger = collector
    return strategies


def _run_once() -> None:
    """Construct a fresh broker/strategy graph and run it until it stops."""
    from strategies.incubator import attach_strategies_to_host

    validate_runtime_configuration()
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


def run() -> None:
    """Run the trader, rebuilding durable state after unexpected failures."""
    raw_backoff = os.getenv("PROCESS_RESTART_BACKOFF_SECONDS", "15").strip()
    try:
        try:
            backoff = max(float(raw_backoff), 0.0)
        except ValueError as error:
            raise ConfigurationError("PROCESS_RESTART_BACKOFF_SECONDS must be numeric.") from error
        validate_runtime_configuration()
    except (ConfigurationError, FileNotFoundError, ValueError) as error:
        logger.critical("Fatal configuration error; not restarting: %s", error)
        raise SystemExit(2) from error
    while True:
        try:
            _run_once()
            return
        except ConfigurationError as error:
            logger.critical("Fatal configuration error; not restarting: %s", error)
            raise SystemExit(2) from error
        except Exception:
            logger.exception(
                "Trader stopped unexpectedly; reconstructing from the durable ledger in %.1fs.",
                backoff,
            )
            time.sleep(backoff)


if __name__ == "__main__":
    run()
