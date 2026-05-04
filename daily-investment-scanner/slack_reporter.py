"""
slack_reporter.py — Build and send the daily investment report to Slack.

Uses Slack's Block Kit for structured, readable formatting.
Slack incoming webhook docs: https://api.slack.com/messaging/webhooks
Block Kit reference:         https://api.slack.com/reference/block-kit
"""

import logging
from datetime import datetime
from typing import Any

import pytz
import requests

from config import config
from market_scanner import Category, Opportunity
from questrade_client import Account, AccountBalance, Position

logger = logging.getLogger("scanner.slack")

# Timezone for the report timestamp
_ET = pytz.timezone("America/Toronto")

# Risk level → Slack emoji
_RISK_EMOJI = {"LOW": ":large_green_circle:", "MEDIUM": ":large_yellow_circle:", "HIGH": ":red_circle:"}

# Category → display label
_CATEGORY_LABELS: dict[str, str] = {
    "MOMENTUM":  ":rocket: Momentum Plays",
    "DIP_BUY":   ":chart_with_downwards_trend: Dip Buys",
    "ETF":       ":bar_chart: ETF Opportunities",
    "CRYPTO":    ":coin: Crypto Movers",
    "WATCHLIST": ":eyes: Your Watchlist",
}


# ---------------------------------------------------------------------------
# Block Kit helpers
# ---------------------------------------------------------------------------

def _divider() -> dict:
    return {"type": "divider"}


def _header(text: str) -> dict:
    return {
        "type": "header",
        "text": {"type": "plain_text", "text": text, "emoji": True},
    }


def _section(text: str) -> dict:
    return {
        "type": "section",
        "text": {"type": "mrkdwn", "text": text},
    }


def _fields_section(fields: list[str]) -> dict:
    """Two-column layout using Slack's 'fields' key (max 10 items)."""
    return {
        "type": "section",
        "fields": [{"type": "mrkdwn", "text": f} for f in fields[:10]],
    }


def _context(text: str) -> dict:
    return {
        "type": "context",
        "elements": [{"type": "mrkdwn", "text": text}],
    }


# ---------------------------------------------------------------------------
# Section builders
# ---------------------------------------------------------------------------

def _build_portfolio_section(
    balances: list[AccountBalance],
    positions_by_account: dict[str, list[Position]],
) -> list[dict]:
    """
    📊 Portfolio Snapshot
    - Total equity, cash, day change
    - Top 3 movers (gainers + losers)
    """
    blocks: list[dict] = [
        _header("📊 Portfolio Snapshot"),
    ]

    if not balances:
        blocks.append(_section("_No portfolio data available._"))
        return blocks

    # Aggregate across all accounts
    total_equity = sum(b.total_equity for b in balances)
    total_cash   = sum(b.cash for b in balances)
    total_day_chg = sum(b.day_change for b in balances)
    day_chg_pct   = (total_day_chg / (total_equity - total_day_chg)) if (total_equity - total_day_chg) else 0.0
    currency      = balances[0].currency

    day_arrow = ":arrow_up_small:" if total_day_chg >= 0 else ":arrow_down_small:"

    blocks.append(_fields_section([
        f"*Total Equity*\n{currency} {total_equity:,.2f}",
        f"*Cash Available*\n{currency} {total_cash:,.2f}",
        f"*Today's Change*\n{day_arrow} {currency} {total_day_chg:+,.2f} ({day_chg_pct*100:+.2f}%)",
        f"*Accounts*\n{len(balances)} account(s)",
    ]))

    # Gather all positions and sort by day P&L %
    all_positions: list[Position] = []
    for pos_list in positions_by_account.values():
        all_positions.extend(pos_list)

    if all_positions:
        sorted_pos = sorted(all_positions, key=lambda p: p.day_pnl_percent, reverse=True)
        top_gainers  = sorted_pos[:3]
        top_losers   = sorted_pos[-3:][::-1]
        movers = {p.symbol: p for p in (top_gainers + top_losers)}.values()  # dedup

        lines = ["*Top Movers Today*"]
        for p in movers:
            arrow = ":arrow_up_small:" if p.day_pnl >= 0 else ":arrow_down_small:"
            lines.append(
                f"{arrow} *{p.symbol}* — {p.day_pnl_percent*100:+.2f}% "
                f"(${p.day_pnl:+,.2f})"
            )
        blocks.append(_section("\n".join(lines)))

    return blocks


def _build_opportunities_section(
    opportunities: dict[Category, list[Opportunity]],
) -> list[dict]:
    """
    🟢 Today's Opportunities — grouped by category.
    Each pick: Ticker | Price | 1W% | Risk | Why
    """
    blocks: list[dict] = [
        _divider(),
        _header("🟢 Today's Opportunities"),
    ]

    # Only show categories with at least one pick (excluding watchlist — has its own section)
    main_categories: list[Category] = ["MOMENTUM", "DIP_BUY", "ETF", "CRYPTO"]
    any_found = False

    for cat in main_categories:
        picks = opportunities.get(cat, [])
        if not picks:
            continue
        any_found = True

        label = _CATEGORY_LABELS.get(cat, cat)
        blocks.append(_section(f"*{label}*"))

        for opp in picks:
            risk_emoji = _RISK_EMOJI.get(opp.risk, ":white_circle:")
            vol_ratio_str = f"{opp.volume_ratio:.1f}×" if opp.avg_volume else "—"
            line = (
                f"*{opp.ticker}* — ${opp.price:.2f}  "
                f"|  1d: {opp.change_1d*100:+.1f}%  "
                f"1w: {opp.change_1w*100:+.1f}%  "
                f"1mo: {opp.change_1mo*100:+.1f}%  "
                f"|  Vol: {vol_ratio_str}  "
                f"|  Risk: {risk_emoji} {opp.risk}\n"
                f"> _{opp.reason}_"
            )
            blocks.append(_section(line))

    if not any_found:
        blocks.append(_section("_No opportunities flagged today — market conditions may be quiet._"))

    # Watchlist section
    watchlist_picks = opportunities.get("WATCHLIST", [])
    if watchlist_picks:
        blocks.append(_section(f"*{_CATEGORY_LABELS['WATCHLIST']}*"))
        for opp in watchlist_picks:
            risk_emoji = _RISK_EMOJI.get(opp.risk, ":white_circle:")
            line = (
                f"*{opp.ticker}* — ${opp.price:.2f}  "
                f"|  1d: {opp.change_1d*100:+.1f}%  "
                f"1w: {opp.change_1w*100:+.1f}%  "
                f"|  Risk: {risk_emoji} {opp.risk}"
            )
            blocks.append(_section(line))

    return blocks


def _build_alerts_section(
    positions_by_account: dict[str, list[Position]],
) -> list[dict]:
    """
    ⚠️ Alerts on Holdings
    - Down >5% today
    - 52-week high/low (approximated from the Position data we have)
    - Note: earnings alerts require a paid data source; omitted here.
    """
    blocks: list[dict] = [
        _divider(),
        _header("⚠️ Alerts on My Holdings"),
    ]

    alerts: list[str] = []

    for pos_list in positions_by_account.values():
        for p in pos_list:
            # Alert: significant day loss
            if p.day_pnl_percent <= -config.PORTFOLIO_ALERT_DOWN_PCT:
                alerts.append(
                    f":rotating_light: *{p.symbol}* is down "
                    f"{p.day_pnl_percent*100:.1f}% today "
                    f"(${p.day_pnl:,.2f})"
                )

            # Alert: unrealised P&L > +50% — might be time to review
            if p.pnl_percent >= 0.50:
                alerts.append(
                    f":moneybag: *{p.symbol}* is up {p.pnl_percent*100:.0f}% "
                    "overall — consider reviewing your position."
                )

            # Alert: significant unrealised loss (>20%)
            if p.pnl_percent <= -0.20:
                alerts.append(
                    f":warning: *{p.symbol}* is down {p.pnl_percent*100:.0f}% "
                    "overall — may want to reassess."
                )

    if alerts:
        blocks.append(_section("\n".join(alerts)))
    else:
        blocks.append(_section(":white_check_mark: No alerts on current holdings."))

    blocks.append(_context(
        "_Earnings alerts require a premium data source and are not included in this free version._"
    ))

    return blocks


def _build_footer() -> list[dict]:
    now_et = datetime.now(_ET)
    timestamp = now_et.strftime("%A, %B %d %Y at %I:%M %p ET")
    return [
        _divider(),
        _context(
            f":clock8: Report generated {timestamp} · "
            ":warning: *Not financial advice. Do your own research.*"
        ),
    ]


# ---------------------------------------------------------------------------
# Main public functions
# ---------------------------------------------------------------------------

def build_report(
    balances: list[AccountBalance],
    positions_by_account: dict[str, list[Position]],
    opportunities: dict[Category, list[Opportunity]],
) -> list[dict]:
    """
    Assemble the full Block Kit payload (list of blocks).

    Call this to get the blocks, then pass them to `send_report` or print
    them in --dry-run mode.
    """
    blocks: list[dict] = []
    blocks += _build_portfolio_section(balances, positions_by_account)
    blocks += _build_opportunities_section(opportunities)
    blocks += _build_alerts_section(positions_by_account)
    blocks += _build_footer()
    return blocks


def send_report(blocks: list[dict]) -> None:
    """
    POST the Block Kit blocks to the Slack incoming webhook.

    Raises RuntimeError if the webhook URL is not configured or the POST fails.
    """
    webhook_url = config.SLACK_WEBHOOK_URL
    if not webhook_url or webhook_url.startswith("https://hooks.slack.com/services/YOUR"):
        raise ValueError(
            "SLACK_WEBHOOK_URL is not configured in .env. "
            "Create one at https://api.slack.com/apps → Incoming Webhooks."
        )

    payload: dict[str, Any] = {"blocks": blocks}

    try:
        resp = requests.post(webhook_url, json=payload, timeout=10)
        resp.raise_for_status()
    except requests.HTTPError as exc:
        raise RuntimeError(
            f"Slack webhook returned HTTP {exc.response.status_code}: {exc.response.text}"
        ) from exc
    except requests.RequestException as exc:
        raise RuntimeError(f"Network error sending Slack report: {exc}") from exc

    logger.info("Slack report sent successfully.")


def format_report_text(blocks: list[dict]) -> str:
    """
    Convert Block Kit blocks to plain text for --dry-run console output.
    Extracts only text nodes so the output is human-readable without Slack.
    """
    lines: list[str] = []
    for block in blocks:
        btype = block.get("type")
        if btype == "header":
            lines.append("\n" + "=" * 60)
            lines.append(block["text"]["text"])
            lines.append("=" * 60)
        elif btype == "section":
            text_obj = block.get("text")
            if text_obj:
                lines.append(text_obj["text"])
            for field in block.get("fields", []):
                lines.append(field["text"])
        elif btype == "context":
            for el in block.get("elements", []):
                lines.append(f"  [{el.get('text', '')}]")
        elif btype == "divider":
            lines.append("-" * 60)
    return "\n".join(lines)
