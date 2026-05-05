"""
market_scanner.py — Find investment opportunities using free market data.

Data source: yfinance (wraps Yahoo Finance — free, no API key required).

Scans four categories:
  - Momentum plays   : up 5-15% in past week with volume spike ≥ 2× average
  - Dip buys         : quality stocks (mktcap ≥ $1B) down 10%+ from 30-day high
  - ETF opportunities: sector ETFs with strong recent price momentum
  - Crypto movers    : notable price action in top-20 cryptos

All picks are capped at MAX_ENTRY_PRICE per share/unit.
"""

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

import yfinance as yf

from config import config

logger = logging.getLogger("scanner.market")

RiskLevel = Literal["LOW", "MEDIUM", "HIGH"]
Category = Literal["MOMENTUM", "DIP_BUY", "ETF", "CRYPTO", "WATCHLIST"]


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

@dataclass
class Opportunity:
    """A single investment idea identified by the scanner."""
    ticker: str
    name: str
    category: Category
    price: float
    change_1d: float       # fraction, e.g. 0.03 = +3 %
    change_1w: float
    change_1mo: float
    volume: int
    avg_volume: int
    risk: RiskLevel
    reason: str            # one-line human-readable explanation

    @property
    def volume_ratio(self) -> float:
        return (self.volume / self.avg_volume) if self.avg_volume else 1.0


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _load_watchlist() -> list[str]:
    """Load tickers from data/watchlist.json; return [] if file is missing or empty."""
    path: Path = config.WATCHLIST_PATH
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text())
        tickers = data.get("tickers", [])
        logger.info("Watchlist loaded: %d tickers.", len(tickers))
        return [t.upper().strip() for t in tickers if t.strip()]
    except (json.JSONDecodeError, KeyError) as exc:
        logger.warning("Could not parse watchlist.json: %s", exc)
        return []


def _score_risk(volatility: float, market_cap: float | None) -> RiskLevel:
    """
    Assign LOW / MEDIUM / HIGH risk based on annualised volatility and market cap.

    Large-cap + low volatility → LOW
    High volatility regardless of size → HIGH
    Everything else → MEDIUM
    """
    low_vol = config.RISK_LOW_VOLATILITY_THRESHOLD
    high_vol = config.RISK_HIGH_VOLATILITY_THRESHOLD
    large_cap = config.RISK_LARGE_CAP_THRESHOLD

    if volatility >= high_vol:
        return "HIGH"
    if volatility < low_vol and (market_cap or 0) >= large_cap:
        return "LOW"
    return "MEDIUM"


def _fetch_ticker_data(ticker: str) -> dict | None:
    """
    Download 3 months of daily OHLCV data for a single ticker via yfinance.

    Returns a dict with the fields needed by the scanner, or None if the
    ticker can't be fetched (delisted, wrong symbol, network error, etc.).
    """
    try:
        t = yf.Ticker(ticker)
        hist = t.history(period="3mo", auto_adjust=True)

        if hist.empty or len(hist) < 5:
            logger.debug("Not enough history for %s — skipping.", ticker)
            return None

        info = t.info or {}

        current_price = float(hist["Close"].iloc[-1])
        if current_price <= 0 or current_price > config.MAX_ENTRY_PRICE:
            return None

        # Calculate returns
        price_1d_ago = float(hist["Close"].iloc[-2]) if len(hist) >= 2 else current_price
        price_1w_ago = float(hist["Close"].iloc[-6]) if len(hist) >= 6 else current_price
        price_1mo_ago = float(hist["Close"].iloc[-22]) if len(hist) >= 22 else current_price

        change_1d = (current_price - price_1d_ago) / price_1d_ago if price_1d_ago else 0.0
        change_1w = (current_price - price_1w_ago) / price_1w_ago if price_1w_ago else 0.0
        change_1mo = (current_price - price_1mo_ago) / price_1mo_ago if price_1mo_ago else 0.0

        # Volume stats
        current_volume = int(hist["Volume"].iloc[-1])
        avg_volume = int(hist["Volume"].iloc[-21:].mean()) if len(hist) >= 21 else current_volume

        # 30-day high (used for dip-buy detection)
        high_30d = float(hist["High"].iloc[-30:].max()) if len(hist) >= 30 else current_price

        # Annualised volatility from daily returns
        daily_returns = hist["Close"].pct_change().dropna()
        volatility = float(daily_returns.std() * (252 ** 0.5)) if len(daily_returns) > 1 else 0.5

        market_cap = info.get("marketCap") or info.get("market_cap")
        if market_cap:
            market_cap = float(market_cap)

        return {
            "ticker": ticker,
            "name": info.get("shortName") or info.get("longName") or ticker,
            "price": current_price,
            "change_1d": change_1d,
            "change_1w": change_1w,
            "change_1mo": change_1mo,
            "volume": current_volume,
            "avg_volume": avg_volume,
            "high_30d": high_30d,
            "volatility": volatility,
            "market_cap": market_cap,
        }

    except Exception as exc:
        logger.debug("Error fetching %s: %s", ticker, exc)
        return None


# ---------------------------------------------------------------------------
# Scanner functions
# ---------------------------------------------------------------------------

def scan_momentum(tickers: list[str]) -> list[Opportunity]:
    """
    Flag stocks that are up 5-15% in the past week with a volume spike ≥ 2×.

    This pattern often indicates genuine buying interest vs. a quiet drift.
    """
    picks: list[Opportunity] = []

    for ticker in tickers:
        d = _fetch_ticker_data(ticker)
        if not d:
            continue

        w_ret = d["change_1w"]
        vol_ratio = d["volume"] / d["avg_volume"] if d["avg_volume"] else 1.0

        if (config.MOMENTUM_RETURN_MIN <= w_ret <= config.MOMENTUM_RETURN_MAX
                and vol_ratio >= config.MOMENTUM_VOLUME_MULTIPLIER):

            risk = _score_risk(d["volatility"], d.get("market_cap"))
            reason = (
                f"Up {w_ret*100:.1f}% this week on {vol_ratio:.1f}× avg volume — "
                "strong buying momentum"
            )
            picks.append(Opportunity(
                ticker=d["ticker"],
                name=d["name"],
                category="MOMENTUM",
                price=d["price"],
                change_1d=d["change_1d"],
                change_1w=d["change_1w"],
                change_1mo=d["change_1mo"],
                volume=d["volume"],
                avg_volume=d["avg_volume"],
                risk=risk,
                reason=reason,
            ))

    # Sort by weekly return descending; cap at MAX_PICKS_PER_CATEGORY
    picks.sort(key=lambda o: o.change_1w, reverse=True)
    return picks[: config.MAX_PICKS_PER_CATEGORY]


def scan_dip_buys(tickers: list[str]) -> list[Opportunity]:
    """
    Flag quality stocks (mktcap ≥ $1B) trading 10%+ below their 30-day high.

    These are potential mean-reversion opportunities in otherwise solid companies.
    """
    picks: list[Opportunity] = []

    for ticker in tickers:
        d = _fetch_ticker_data(ticker)
        if not d:
            continue

        mkt_cap = d.get("market_cap") or 0
        if mkt_cap < config.DIP_BUY_MIN_MARKET_CAP:
            continue

        high_30d = d["high_30d"]
        price = d["price"]
        dip_pct = (high_30d - price) / high_30d if high_30d else 0.0

        if dip_pct >= config.DIP_BUY_THRESHOLD:
            risk = _score_risk(d["volatility"], mkt_cap)
            reason = (
                f"Down {dip_pct*100:.1f}% from 30-day high — "
                f"potential mean-reversion in ${mkt_cap/1e9:.1f}B company"
            )
            picks.append(Opportunity(
                ticker=d["ticker"],
                name=d["name"],
                category="DIP_BUY",
                price=d["price"],
                change_1d=d["change_1d"],
                change_1w=d["change_1w"],
                change_1mo=d["change_1mo"],
                volume=d["volume"],
                avg_volume=d["avg_volume"],
                risk=risk,
                reason=reason,
            ))

    # Sort by dip depth descending (biggest dip first)
    picks.sort(key=lambda o: o.change_1mo)
    return picks[: config.MAX_PICKS_PER_CATEGORY]


def scan_etfs(tickers: list[str]) -> list[Opportunity]:
    """
    Flag ETFs with positive 1-week momentum and above-average volume inflows.

    ETFs provide diversified exposure; we look for sector rotation signals.
    """
    picks: list[Opportunity] = []

    for ticker in tickers:
        d = _fetch_ticker_data(ticker)
        if not d:
            continue

        w_ret = d["change_1w"]
        vol_ratio = d["volume"] / d["avg_volume"] if d["avg_volume"] else 1.0

        # ETF criteria: positive week + any volume uptick
        if w_ret > 0.01 and vol_ratio >= 1.2:
            risk = _score_risk(d["volatility"], None)
            reason = (
                f"ETF up {w_ret*100:.1f}% this week with {vol_ratio:.1f}× avg volume — "
                "sector showing inflows"
            )
            picks.append(Opportunity(
                ticker=d["ticker"],
                name=d["name"],
                category="ETF",
                price=d["price"],
                change_1d=d["change_1d"],
                change_1w=d["change_1w"],
                change_1mo=d["change_1mo"],
                volume=d["volume"],
                avg_volume=d["avg_volume"],
                risk=risk,
                reason=reason,
            ))

    picks.sort(key=lambda o: o.change_1w, reverse=True)
    return picks[: config.MAX_PICKS_PER_CATEGORY]


def scan_crypto(tickers: list[str]) -> list[Opportunity]:
    """
    Flag crypto assets with notable price action (large moves up or down).

    Crypto is inherently high-volatility; we focus on coins under $500/unit.
    """
    picks: list[Opportunity] = []

    for ticker in tickers:
        d = _fetch_ticker_data(ticker)
        if not d:
            continue

        abs_1w = abs(d["change_1w"])
        vol_ratio = d["volume"] / d["avg_volume"] if d["avg_volume"] else 1.0

        # Flag any ≥8% move in either direction with volume confirmation
        if abs_1w >= 0.08 and vol_ratio >= 1.3:
            direction = "surging" if d["change_1w"] > 0 else "selling off"
            risk: RiskLevel = "HIGH"  # Crypto is always HIGH by default
            reason = (
                f"Crypto {direction} {d['change_1w']*100:.1f}% this week "
                f"on {vol_ratio:.1f}× volume — notable price action"
            )
            picks.append(Opportunity(
                ticker=d["ticker"],
                name=d["name"],
                category="CRYPTO",
                price=d["price"],
                change_1d=d["change_1d"],
                change_1w=d["change_1w"],
                change_1mo=d["change_1mo"],
                volume=d["volume"],
                avg_volume=d["avg_volume"],
                risk=risk,
                reason=reason,
            ))

    # Sort by absolute weekly move, biggest first
    picks.sort(key=lambda o: abs(o.change_1w), reverse=True)
    return picks[: config.MAX_PICKS_PER_CATEGORY]


def scan_watchlist(tickers: list[str]) -> list[Opportunity]:
    """
    Always include tickers from the user's watchlist, regardless of scanner criteria.

    These are returned as-is with a basic summary; no filtering applied.
    """
    picks: list[Opportunity] = []

    for ticker in tickers:
        d = _fetch_ticker_data(ticker)
        if not d:
            logger.warning("Watchlist ticker %s could not be fetched.", ticker)
            continue

        risk = _score_risk(d["volatility"], d.get("market_cap"))
        reason = f"On your watchlist — {d['change_1w']*100:+.1f}% this week"
        picks.append(Opportunity(
            ticker=d["ticker"],
            name=d["name"],
            category="WATCHLIST",
            price=d["price"],
            change_1d=d["change_1d"],
            change_1w=d["change_1w"],
            change_1mo=d["change_1mo"],
            volume=d["volume"],
            avg_volume=d["avg_volume"],
            risk=risk,
            reason=reason,
        ))

    return picks


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

def run_full_scan() -> dict[Category, list[Opportunity]]:
    """
    Run all four scanners and return results grouped by category.

    Also appends watchlist items as a fifth group.
    Called by main.py to get all opportunities at once.
    """
    logger.info("Starting market scan…")

    watchlist_tickers = _load_watchlist()

    # Remove watchlist tickers from the universe scans to avoid duplication
    wl_set = set(t.upper() for t in watchlist_tickers)
    stocks = [t for t in config.DEFAULT_STOCK_UNIVERSE if t.upper() not in wl_set]
    etfs   = [t for t in config.DEFAULT_ETF_UNIVERSE   if t.upper() not in wl_set]
    crypto = [t for t in config.DEFAULT_CRYPTO_UNIVERSE if t.upper() not in wl_set]

    results: dict[Category, list[Opportunity]] = {
        "MOMENTUM":  scan_momentum(stocks),
        "DIP_BUY":   scan_dip_buys(stocks),
        "ETF":       scan_etfs(etfs),
        "CRYPTO":    scan_crypto(crypto),
        "WATCHLIST": scan_watchlist(watchlist_tickers),
    }

    total = sum(len(v) for v in results.values())
    logger.info("Scan complete — %d total opportunities found.", total)
    return results
