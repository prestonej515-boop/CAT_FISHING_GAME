"""
config.py — Load environment variables and define application constants.

All secrets come from the .env file. This module provides a single
`Config` object imported everywhere else so settings are centralised.
"""

import os
import logging
from pathlib import Path
from dotenv import load_dotenv

# Load .env file from the project root (the directory containing this file)
_PROJECT_ROOT = Path(__file__).parent
load_dotenv(_PROJECT_ROOT / ".env")


# ---------------------------------------------------------------------------
# Logging setup — called once from main.py; defined here so every module
# that does `from config import logger` gets the same configured logger.
# ---------------------------------------------------------------------------

def setup_logging(log_level: str = "INFO", log_file: str | None = None) -> logging.Logger:
    """Configure root logger with console + optional file handler."""
    level = getattr(logging, log_level.upper(), logging.INFO)
    fmt = "%(asctime)s [%(levelname)s] %(name)s — %(message)s"
    datefmt = "%Y-%m-%d %H:%M:%S"

    handlers: list[logging.Handler] = [logging.StreamHandler()]

    if log_file:
        log_path = _PROJECT_ROOT / log_file
        log_path.parent.mkdir(parents=True, exist_ok=True)
        handlers.append(logging.FileHandler(log_path))

    logging.basicConfig(level=level, format=fmt, datefmt=datefmt, handlers=handlers)
    return logging.getLogger("scanner")


# ---------------------------------------------------------------------------
# Config dataclass — a simple namespace; no heavy frameworks needed.
# ---------------------------------------------------------------------------

class Config:
    """Central configuration loaded from environment variables."""

    # Questrade
    QUESTRADE_REFRESH_TOKEN: str = os.getenv("QUESTRADE_REFRESH_TOKEN", "")
    QUESTRADE_API_SERVER: str = os.getenv("QUESTRADE_API_SERVER", "")
    # Questrade OAuth token endpoint (always this URL regardless of API server)
    QUESTRADE_TOKEN_URL: str = "https://login.questrade.com/oauth2/token"

    # Slack
    SLACK_WEBHOOK_URL: str = os.getenv("SLACK_WEBHOOK_URL", "")

    # Optional: restrict to specific account IDs (comma-separated in .env)
    _raw_account_ids: str = os.getenv("QUESTRADE_ACCOUNT_IDS", "")
    QUESTRADE_ACCOUNT_IDS: list[str] = (
        [a.strip() for a in _raw_account_ids.split(",") if a.strip()]
        if _raw_account_ids else []
    )

    # Logging
    LOG_LEVEL: str = os.getenv("LOG_LEVEL", "INFO")
    LOG_FILE: str | None = os.getenv("LOG_FILE", "logs/scanner.log") or None

    # ---------------------------------------------------------------------------
    # Scanner thresholds — tweak these to adjust what the scanner flags.
    # ---------------------------------------------------------------------------

    # Maximum price per share/unit to consider (USD or CAD)
    MAX_ENTRY_PRICE: float = 500.0

    # Momentum: flag if 5-day return is in this range AND volume spiked
    MOMENTUM_RETURN_MIN: float = 0.05   # +5 %
    MOMENTUM_RETURN_MAX: float = 0.15   # +15 %
    MOMENTUM_VOLUME_MULTIPLIER: float = 2.0  # volume must be ≥ 2× 20-day avg

    # Dip buy: flag if price fell this much from its 30-day high
    DIP_BUY_THRESHOLD: float = 0.10     # down 10 %+ from 30-day high
    DIP_BUY_MIN_MARKET_CAP: float = 1e9  # quality filter: market cap ≥ $1B

    # Portfolio alerts
    PORTFOLIO_ALERT_DOWN_PCT: float = 0.05   # alert if position down >5 % today
    EARNINGS_LOOKAHEAD_DAYS: int = 7          # warn about earnings within 7 days

    # Max picks per category in the Slack report
    MAX_PICKS_PER_CATEGORY: int = 3

    # Tickers to always include in scanning (loaded from watchlist.json too)
    WATCHLIST_PATH: Path = _PROJECT_ROOT / "data" / "watchlist.json"

    # Default stock universe to scan for momentum + dip opportunities
    # These are well-known liquid names — extend as needed.
    DEFAULT_STOCK_UNIVERSE: list[str] = [
        # Large-cap US tech / growth
        "AAPL", "MSFT", "GOOGL", "AMZN", "META", "NVDA", "TSLA", "AMD", "INTC",
        # Financials
        "JPM", "BAC", "WFC", "GS",
        # Healthcare
        "JNJ", "PFE", "MRNA", "UNH",
        # Energy
        "XOM", "CVX", "ENB",
        # Consumer / Retail
        "WMT", "TGT", "COST", "NKE",
        # Canadian stocks (TSX)
        "SHOP.TO", "RY.TO", "TD.TO", "BNS.TO", "CNR.TO",
    ]

    # ETFs to track
    DEFAULT_ETF_UNIVERSE: list[str] = [
        "SPY", "QQQ", "IWM", "DIA",          # Broad market
        "XLK", "XLF", "XLE", "XLV", "XLI",  # US sector ETFs
        "GLD", "SLV", "USO",                  # Commodities
        "VFV.TO", "XIU.TO", "XIC.TO",         # Canadian ETFs
        "ARKK", "ARKG",                        # Thematic / Growth
    ]

    # Crypto tickers (Yahoo Finance format)
    DEFAULT_CRYPTO_UNIVERSE: list[str] = [
        "BTC-USD", "ETH-USD", "BNB-USD", "XRP-USD", "ADA-USD",
        "DOGE-USD", "SOL-USD", "DOT-USD", "MATIC-USD", "AVAX-USD",
        "LINK-USD", "UNI-USD", "LTC-USD", "BCH-USD", "XLM-USD",
    ]

    # Risk scoring thresholds (annualised volatility %)
    RISK_LOW_VOLATILITY_THRESHOLD: float = 0.20    # < 20 % vol → LOW
    RISK_HIGH_VOLATILITY_THRESHOLD: float = 0.40   # > 40 % vol → HIGH
    RISK_LARGE_CAP_THRESHOLD: float = 10e9          # > $10B → helps lower risk score


# Singleton instance used across the app
config = Config()
