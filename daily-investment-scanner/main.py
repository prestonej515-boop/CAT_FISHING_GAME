"""
main.py — Orchestrator for the daily investment scanner.

Pipeline:
  1. Authenticate with Questrade (refresh OAuth2 token)
  2. Pull portfolio holdings and account balances
  3. Scan the market for new opportunities
  4. Build the Slack report
  5. Send to Slack (or print to console in --dry-run mode)

Usage:
    python main.py              # full run, sends to Slack
    python main.py --dry-run    # prints report to console, skips Slack
"""

import argparse
import logging
import sys

from config import config, setup_logging
from market_scanner import run_full_scan, Opportunity, Category
from questrade_client import QuestradeClient, Account, AccountBalance, Position
from slack_reporter import build_report, send_report, format_report_text


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Daily investment scanner — pulls portfolio + scans market opportunities."
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the report to console instead of sending to Slack.",
    )
    parser.add_argument(
        "--skip-questrade",
        action="store_true",
        help="Skip Questrade auth/portfolio pull (useful if API is down).",
    )
    parser.add_argument(
        "--skip-scanner",
        action="store_true",
        help="Skip market scan (portfolio snapshot only).",
    )
    return parser.parse_args()


def fetch_portfolio(
    client: QuestradeClient,
) -> tuple[list[AccountBalance], dict[str, list[Position]]]:
    """
    Pull balances and positions for all active accounts.

    Returns:
        balances: one AccountBalance per account
        positions_by_account: {account_id: [Position, ...]}
    """
    logger = logging.getLogger("scanner.main")
    accounts: list[Account] = client.get_accounts()

    if not accounts:
        logger.warning("No active Questrade accounts found.")
        return [], {}

    balances: list[AccountBalance] = []
    positions_by_account: dict[str, list[Position]] = {}

    for acct in accounts:
        try:
            balance = client.get_account_balance(acct.account_id)
            balances.append(balance)
        except RuntimeError as exc:
            logger.error("Could not fetch balance for account %s: %s", acct.account_id, exc)

        try:
            positions = client.get_positions(acct.account_id)
            positions_by_account[acct.account_id] = positions
        except RuntimeError as exc:
            logger.error("Could not fetch positions for account %s: %s", acct.account_id, exc)
            positions_by_account[acct.account_id] = []

    return balances, positions_by_account


def main() -> int:
    """
    Run the full pipeline.

    Return codes:
        0 — success
        1 — partial failure (some data missing but report still sent)
        2 — fatal failure (nothing sent)
    """
    args = parse_args()

    # Configure logging first so all subsequent log calls are formatted
    logger = setup_logging(log_level=config.LOG_LEVEL, log_file=config.LOG_FILE)
    logger.info("=== Daily Investment Scanner starting ===")
    if args.dry_run:
        logger.info("DRY RUN MODE — output goes to console only.")

    exit_code = 0

    # ------------------------------------------------------------------ #
    # Step 1: Questrade — portfolio data                                   #
    # ------------------------------------------------------------------ #
    balances: list[AccountBalance] = []
    positions_by_account: dict[str, list[Position]] = {}

    if not args.skip_questrade:
        logger.info("--- Step 1: Questrade portfolio pull ---")
        client = QuestradeClient()
        try:
            balances, positions_by_account = fetch_portfolio(client)
        except RuntimeError as exc:
            logger.error("Questrade auth/portfolio failed: %s", exc)
            logger.warning("Continuing without portfolio data (report will still be sent).")
            exit_code = 1
    else:
        logger.info("--- Step 1: Questrade SKIPPED (--skip-questrade) ---")

    # ------------------------------------------------------------------ #
    # Step 2: Market scan                                                  #
    # ------------------------------------------------------------------ #
    opportunities: dict[Category, list[Opportunity]] = {
        "MOMENTUM": [], "DIP_BUY": [], "ETF": [], "CRYPTO": [], "WATCHLIST": [],
    }

    if not args.skip_scanner:
        logger.info("--- Step 2: Market scan ---")
        try:
            opportunities = run_full_scan()
        except Exception as exc:
            logger.error("Market scan failed: %s", exc)
            logger.warning("Continuing with portfolio snapshot only.")
            exit_code = 1
    else:
        logger.info("--- Step 2: Market scan SKIPPED (--skip-scanner) ---")

    # ------------------------------------------------------------------ #
    # Step 3: Build report                                                 #
    # ------------------------------------------------------------------ #
    logger.info("--- Step 3: Building Slack report ---")
    try:
        blocks = build_report(balances, positions_by_account, opportunities)
    except Exception as exc:
        logger.error("Failed to build report: %s", exc)
        return 2

    # ------------------------------------------------------------------ #
    # Step 4: Send or print                                                #
    # ------------------------------------------------------------------ #
    if args.dry_run:
        logger.info("--- Step 4: Dry run — printing to console ---")
        print(format_report_text(blocks))
    else:
        logger.info("--- Step 4: Sending to Slack ---")
        try:
            send_report(blocks)
        except (RuntimeError, ValueError) as exc:
            logger.error("Failed to send Slack report: %s", exc)
            # Still print to console so the run isn't completely wasted
            print("\n--- FALLBACK: Printing report to console ---")
            print(format_report_text(blocks))
            return 2

    logger.info("=== Scanner complete (exit code %d) ===", exit_code)
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
