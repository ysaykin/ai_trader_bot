"""Configuration and credentials manager for Lumibot Trading Platform.

Handles loading, validating, testing, and saving .env configuration
directly from the dashboard GUI.
"""
import os
from pathlib import Path
from typing import Dict, Any, Tuple
import requests

ENV_PATHS = [
    Path(".env"),
    Path("/app/.env"),
    Path("../.env"),
]


def get_env_path() -> Path:
    """Find the active .env path."""
    for p in ENV_PATHS:
        if p.exists():
            return p
    return Path(".env")


def load_env_values() -> Dict[str, str]:
    """Read all key-values from the .env file."""
    path = get_env_path()
    values = {}
    if not path.exists():
        return values

    try:
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                if "=" in line:
                    k, v = line.split("=", 1)
                    values[k.strip()] = v.strip().strip('"').strip("'")
    except Exception:
        pass
    return values


def save_env_values(updates: Dict[str, Any]) -> Tuple[bool, str]:
    """Save updated parameters into the .env file while preserving comments."""
    path = get_env_path()
    lines = []
    keys_written = set()

    if path.exists():
        try:
            with open(path, "r", encoding="utf-8") as f:
                for line in f:
                    stripped = line.strip()
                    if stripped and not stripped.startswith("#") and "=" in stripped:
                        key = stripped.split("=", 1)[0].strip()
                        if key in updates:
                            lines.append(f"{key}={updates[key]}\n")
                            keys_written.add(key)
                            continue
                    lines.append(line)
        except Exception as e:
            return False, f"Failed to read existing .env: {e}"

    # Append any new keys not already in the file
    for k, v in updates.items():
        if k not in keys_written:
            lines.append(f"{k}={v}\n")

    try:
        with open(path, "w", encoding="utf-8") as f:
            f.writelines(lines)
        return True, "Configuration successfully saved to .env."
    except Exception as e:
        return False, f"Failed to write .env file: {e}"


def test_tradier_credentials(api_key: str, account: str, sandbox: bool = True) -> Tuple[bool, str]:
    """Test Tradier API credentials against Tradier REST endpoint."""
    if not api_key:
        return False, "API key cannot be empty."

    base_url = "https://sandbox.tradier.com/v1" if sandbox else "https://api.tradier.com/v1"
    headers = {
        "Authorization": f"Bearer {api_key.strip()}",
        "Accept": "application/json",
    }

    try:
        url = f"{base_url}/accounts/{account.strip()}/balances"
        res = requests.get(url, headers=headers, timeout=7)
        if res.status_code == 200:
            data = res.json().get("balances", {})
            equity = float(data.get("total_equity", data.get("equity", 0.0)))
            cash = float(data.get("total_cash", 0.0))
            margin = data.get("margin", {})
            bp = float(margin.get("option_buying_power", data.get("option_buying_power", 0.0)))
            return True, f"Verified! Account {account} (Equity: ${equity:,.2f}, Option BP: ${bp:,.2f}, Cash: ${cash:,.2f})"
        elif res.status_code == 401:
            return False, "Invalid Access Token (HTTP 401). Please check your Tradier API Key."
        elif res.status_code == 404:
            return False, f"Account number '{account}' not found under this API key (HTTP 404)."
        else:
            return False, f"Tradier API returned HTTP {res.status_code}: {res.text}"
    except Exception as e:
        return False, f"Connection failed: {e}"


def test_alpaca_credentials(api_key: str, api_secret: str, paper: bool = True) -> Tuple[bool, str]:
    """Test Alpaca credentials against Alpaca API."""
    if not api_key or not api_secret:
        return False, "Alpaca API Key and Secret are required."

    base_url = "https://paper-api.alpaca.markets/v2" if paper else "https://api.alpaca.markets/v2"
    headers = {
        "APCA-API-KEY-ID": api_key.strip(),
        "APCA-API-SECRET-KEY": api_secret.strip(),
    }

    try:
        res = requests.get(f"{base_url}/account", headers=headers, timeout=7)
        if res.status_code == 200:
            data = res.json()
            equity = float(data.get("equity", 0.0))
            bp = float(data.get("buying_power", 0.0))
            status = data.get("status", "ACTIVE")
            return True, f"Verified! Alpaca account status: {status} (Equity: ${equity:,.2f}, Buying Power: ${bp:,.2f})"
        elif res.status_code in [401, 403]:
            return False, "Invalid Alpaca API Key or Secret Key."
        else:
            return False, f"Alpaca API returned HTTP {res.status_code}: {res.text}"
    except Exception as e:
        return False, f"Connection failed: {e}"
