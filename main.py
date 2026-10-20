"""Lumibot trading entrypoint — Alpaca broker."""
import os
import logging
from dotenv import load_dotenv
from lumibot.brokers import Alpaca
from lumibot.traders import Trader

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



def get_broker():
    """Instantiate the Alpaca broker from env vars."""
    api_key = os.getenv("ALPACA_API_KEY")
    api_secret = os.getenv("ALPACA_API_SECRET")
    if not api_key or not api_secret:
        raise RuntimeError(
            "ALPACA_API_KEY and ALPACA_API_SECRET must be set in .env"
        )
    return Alpaca({
        "API_KEY": api_key,
        "API_SECRET": api_secret,
        "PAPER": os.getenv("ALPACA_PAPER", "true").lower() == "true",
    })


if __name__ == "__main__":
    from strategies.iron_condor import SPXIronCondor

    broker = get_broker()

    # Budget is informational — actual buying power comes from your Alpaca account
    strategy = SPXIronCondor(broker=broker, budget=100_000)

    trader = Trader()
    trader.add_strategy(strategy)

    logger.info("Starting SPX Iron Condor strategy...")
    trader.run_all()
