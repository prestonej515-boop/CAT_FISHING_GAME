"""
questrade_client.py — Questrade REST API v1 wrapper.

Handles OAuth2 token refresh automatically (tokens expire every 30 minutes).
After each refresh the new tokens are written back to .env so the next run
picks them up without any manual intervention.

Questrade API docs: https://www.questrade.com/api/documentation/getting-started
"""

import logging
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import requests
from dotenv import set_key

from config import config

logger = logging.getLogger("scanner.questrade")

# Path to .env file so we can persist the refreshed token
_ENV_FILE = Path(__file__).parent / ".env"


# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------

@dataclass
class Position:
    """A single holding in a Questrade account."""
    symbol: str
    symbol_id: int
    open_quantity: float
    current_market_value: float
    current_price: float
    average_entry_price: float
    open_pnl: float          # unrealised P&L
    day_pnl: float           # today's P&L (may be 0 if not available)
    is_real_time: bool

    @property
    def pnl_percent(self) -> float:
        """Unrealised P&L as a fraction of cost basis."""
        cost = self.average_entry_price * self.open_quantity
        return (self.open_pnl / cost) if cost else 0.0

    @property
    def day_pnl_percent(self) -> float:
        """Today's P&L as a fraction of yesterday's close value."""
        yesterday_value = self.current_market_value - self.day_pnl
        return (self.day_pnl / yesterday_value) if yesterday_value else 0.0


@dataclass
class AccountBalance:
    """Cash and equity summary for one account."""
    account_id: str
    currency: str
    cash: float
    market_value: float
    total_equity: float
    buying_power: float
    day_change: float       # total portfolio $ change today


@dataclass
class Account:
    """Basic account metadata."""
    account_id: str
    account_type: str
    status: str
    is_primary: bool


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------

class QuestradeClient:
    """
    Thin wrapper around the Questrade REST API.

    Usage:
        client = QuestradeClient()
        client.refresh_token()          # or called automatically on 401
        accounts = client.get_accounts()
    """

    def __init__(self) -> None:
        self._refresh_token: str = config.QUESTRADE_REFRESH_TOKEN
        self._access_token: str = ""
        self._api_server: str = config.QUESTRADE_API_SERVER
        self._token_expiry: float = 0.0   # Unix timestamp when access token expires

    # ------------------------------------------------------------------
    # Authentication
    # ------------------------------------------------------------------

    def refresh_token(self) -> None:
        """
        Exchange the refresh token for a new access token + refresh token.

        Questrade uses a rotating refresh token model: each call to the token
        endpoint invalidates the old refresh token and returns a new one.
        We write the new refresh token back to .env immediately.
        """
        if not self._refresh_token:
            raise ValueError(
                "QUESTRADE_REFRESH_TOKEN is not set in .env. "
                "Generate one at https://login.questrade.com/APIAccess"
            )

        logger.info("Refreshing Questrade access token…")
        url = f"{config.QUESTRADE_TOKEN_URL}?grant_type=refresh_token&refresh_token={self._refresh_token}"

        try:
            resp = requests.get(url, timeout=10)
            resp.raise_for_status()
        except requests.HTTPError as exc:
            status = exc.response.status_code if exc.response else "unknown"
            raise RuntimeError(
                f"Token refresh failed (HTTP {status}). "
                "Your refresh token may be expired or invalid. "
                "Generate a new one at https://login.questrade.com/APIAccess"
            ) from exc
        except requests.RequestException as exc:
            raise RuntimeError(f"Network error during token refresh: {exc}") from exc

        data = resp.json()

        self._access_token = data["access_token"]
        self._refresh_token = data["refresh_token"]
        self._api_server = data["api_server"].rstrip("/")
        expires_in: int = data.get("expires_in", 1800)
        self._token_expiry = time.time() + expires_in - 60  # 60-second safety buffer

        logger.info("Token refreshed. API server: %s. Expires in %ds.", self._api_server, expires_in)

        # Persist updated tokens to .env so the next scheduled run works
        self._persist_tokens()

    def _persist_tokens(self) -> None:
        """Write the latest refresh token and API server URL back to .env."""
        if not _ENV_FILE.exists():
            logger.warning(".env file not found at %s — tokens not persisted.", _ENV_FILE)
            return
        set_key(str(_ENV_FILE), "QUESTRADE_REFRESH_TOKEN", self._refresh_token)
        set_key(str(_ENV_FILE), "QUESTRADE_API_SERVER", self._api_server)
        logger.debug("Tokens persisted to .env.")

    def _ensure_authenticated(self) -> None:
        """Refresh the access token if it has expired or was never fetched."""
        if not self._access_token or time.time() >= self._token_expiry:
            self.refresh_token()

    # ------------------------------------------------------------------
    # HTTP helpers
    # ------------------------------------------------------------------

    def _get(self, path: str, params: dict[str, Any] | None = None, retry: bool = True) -> Any:
        """
        Authenticated GET request to the Questrade API.

        Automatically retries once on 401 (token expired mid-session).
        Raises RuntimeError on any non-2xx response.
        """
        self._ensure_authenticated()

        url = f"{self._api_server}/v1/{path.lstrip('/')}"
        headers = {"Authorization": f"Bearer {self._access_token}"}

        try:
            resp = requests.get(url, headers=headers, params=params, timeout=15)
        except requests.RequestException as exc:
            raise RuntimeError(f"Network error calling Questrade API ({path}): {exc}") from exc

        if resp.status_code == 401 and retry:
            # Token expired unexpectedly mid-session — refresh and try once more
            logger.warning("Received 401; refreshing token and retrying…")
            self.refresh_token()
            return self._get(path, params=params, retry=False)

        if resp.status_code == 429:
            # Rate limited — Questrade allows ~20 req/s; wait and retry once
            logger.warning("Rate limited by Questrade API. Waiting 2 seconds…")
            time.sleep(2)
            return self._get(path, params=params, retry=False)

        if not resp.ok:
            try:
                error_detail = resp.json()
            except Exception:
                error_detail = resp.text
            raise RuntimeError(
                f"Questrade API error on {path}: HTTP {resp.status_code} — {error_detail}"
            )

        return resp.json()

    # ------------------------------------------------------------------
    # Public API methods
    # ------------------------------------------------------------------

    def get_accounts(self) -> list[Account]:
        """
        Return all accounts linked to this Questrade login.

        Filters to only the IDs listed in QUESTRADE_ACCOUNT_IDS if that
        env var is set; otherwise returns all active accounts.
        """
        data = self._get("accounts")
        accounts: list[Account] = []

        for raw in data.get("accounts", []):
            acct = Account(
                account_id=str(raw["number"]),
                account_type=raw.get("type", ""),
                status=raw.get("status", ""),
                is_primary=raw.get("isPrimary", False),
            )
            # Skip closed/inactive accounts
            if acct.status.lower() not in ("active",):
                logger.debug("Skipping non-active account %s (status=%s)", acct.account_id, acct.status)
                continue
            # Apply optional account filter
            if config.QUESTRADE_ACCOUNT_IDS and acct.account_id not in config.QUESTRADE_ACCOUNT_IDS:
                continue
            accounts.append(acct)

        logger.info("Found %d active account(s).", len(accounts))
        return accounts

    def get_positions(self, account_id: str) -> list[Position]:
        """
        Return all open positions for a given account.

        Each Position includes current market value, unrealised P&L,
        and today's P&L (if available — Questrade may return 0 outside market hours).
        """
        data = self._get(f"accounts/{account_id}/positions")
        positions: list[Position] = []

        for raw in data.get("positions", []):
            qty = float(raw.get("openQuantity", 0))
            if qty == 0:
                continue  # Skip fully closed positions

            positions.append(Position(
                symbol=raw.get("symbol", ""),
                symbol_id=int(raw.get("symbolId", 0)),
                open_quantity=qty,
                current_market_value=float(raw.get("currentMarketValue", 0)),
                current_price=float(raw.get("currentPrice", 0)),
                average_entry_price=float(raw.get("averageEntryPrice", 0)),
                open_pnl=float(raw.get("openPnl", 0)),
                day_pnl=float(raw.get("dayPnl", 0)),
                is_real_time=bool(raw.get("isRealTime", False)),
            ))

        logger.info("Account %s: %d open position(s).", account_id, len(positions))
        return positions

    def get_account_balance(self, account_id: str) -> AccountBalance:
        """
        Return cash, market value, total equity, buying power, and day change
        for a given account (in the account's native currency).

        Questrade returns balances per currency; we sum CAD + USD converted
        balances from the 'combinedBalances' section for a total-equity view.
        """
        data = self._get(f"accounts/{account_id}/balances")

        # 'combinedBalances' rolls up multi-currency into CAD equivalent
        combined = data.get("combinedBalances", [])
        if not combined:
            # Fall back to perCurrencyBalances if combined is missing
            combined = data.get("perCurrencyBalances", [])

        # Sum across all currency rows
        total_cash = 0.0
        total_market_value = 0.0
        total_equity = 0.0
        buying_power = 0.0
        day_change = 0.0

        for row in combined:
            total_cash += float(row.get("cash", 0))
            total_market_value += float(row.get("marketValue", 0))
            total_equity += float(row.get("totalEquity", 0))
            buying_power += float(row.get("buyingPower", 0))
            day_change += float(row.get("dayChange", 0))

        currency = combined[0].get("currency", "CAD") if combined else "CAD"

        logger.info(
            "Account %s balance — equity: %.2f %s, cash: %.2f, day change: %.2f",
            account_id, total_equity, currency, total_cash, day_change,
        )

        return AccountBalance(
            account_id=account_id,
            currency=currency,
            cash=total_cash,
            market_value=total_market_value,
            total_equity=total_equity,
            buying_power=buying_power,
            day_change=day_change,
        )
