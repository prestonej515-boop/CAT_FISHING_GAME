# Daily Investment Scanner

A Python-based daily scanner that pulls your Questrade portfolio, scans for
investment opportunities using free market data (yfinance), and sends a
formatted report to a Slack channel every weekday morning before market open.

---

## Project Structure

```
daily-investment-scanner/
├── .env.example          # Template for secrets (copy to .env)
├── .gitignore
├── README.md             # This file
├── requirements.txt
├── config.py             # Env vars, constants, thresholds
├── questrade_client.py   # Questrade REST API v1 wrapper
├── market_scanner.py     # Opportunity finder (yfinance)
├── slack_reporter.py     # Slack Block Kit formatter + sender
├── main.py               # Orchestrator — runs the full pipeline
├── cron_setup.sh         # One-shot cron installer script
└── data/
    └── watchlist.json    # Tickers to always track
```

---

## Setup

### 1. Prerequisites

- Python 3.10 or newer
- A [Questrade account](https://www.questrade.com) with API access enabled
- A Slack workspace where you can create an incoming webhook

### 2. Clone and create a virtualenv

```bash
cd daily-investment-scanner
python3 -m venv venv
source venv/bin/activate          # macOS/Linux
# venv\Scripts\activate           # Windows
pip install -r requirements.txt
```

### 3. Configure secrets

```bash
cp .env.example .env
```

Open `.env` and fill in:

| Variable | Where to get it |
|---|---|
| `QUESTRADE_REFRESH_TOKEN` | [Questrade API Access page](https://login.questrade.com/APIAccess) → "Generate new token" |
| `SLACK_WEBHOOK_URL` | [Slack API](https://api.slack.com/apps) → Create app → Incoming Webhooks → Add webhook |

Leave `QUESTRADE_API_SERVER` blank — the app fills it in automatically on first run.

### 4. Test without sending to Slack

```bash
python main.py --dry-run
```

This runs the full pipeline and prints the report to your terminal.
Nothing is sent to Slack.

### 5. Test the real Slack send

```bash
python main.py
```

Check your Slack channel. If it works, you're ready to automate.

### 6. Install the cron job

```bash
chmod +x cron_setup.sh
./cron_setup.sh
```

This adds a cron entry that runs the scanner at **8:30 AM ET, Monday–Friday**.

Verify: `crontab -l`

---

## Customising the Scanner

### Watchlist

Edit `data/watchlist.json` to add tickers you always want in the report:

```json
{
  "tickers": ["AAPL", "SHOP.TO", "BTC-USD"]
}
```

### Thresholds

All scanner thresholds live in `config.py`. For example:

```python
MOMENTUM_RETURN_MIN = 0.05   # flag stocks up at least 5% in a week
DIP_BUY_THRESHOLD   = 0.10   # flag stocks 10%+ below 30-day high
MAX_ENTRY_PRICE     = 500.0  # ignore anything over $500/share
```

Change these values and re-run — no other code needs to change.

### Stock/ETF/Crypto universe

Also in `config.py`:

```python
DEFAULT_STOCK_UNIVERSE = ["AAPL", "MSFT", ...]
DEFAULT_ETF_UNIVERSE   = ["SPY", "QQQ", ...]
DEFAULT_CRYPTO_UNIVERSE = ["BTC-USD", "ETH-USD", ...]
```

---

## Running manually

```bash
# Full run
python main.py

# Dry run (console output, no Slack)
python main.py --dry-run

# Skip Questrade (useful if API is down)
python main.py --skip-questrade

# Skip market scan (portfolio snapshot only)
python main.py --skip-scanner

# Combine flags
python main.py --skip-questrade --dry-run
```

---

## Slack Report Layout

```
📊 Portfolio Snapshot
  Total equity | Cash available | Today's change
  Top movers in your portfolio

🟢 Today's Opportunities
  🚀 Momentum Plays   (up 5-15% with volume spike)
  📉 Dip Buys         (quality stocks 10%+ off high)
  📊 ETF Opportunities
  🪙 Crypto Movers
  👀 Your Watchlist

⚠️ Alerts on My Holdings
  Positions down >5% today
  Large unrealised gains/losses

Footer: timestamp + disclaimer
```

---

## Limitations

- **Questrade tokens expire every 30 minutes.** The app refreshes them
  automatically and writes the new token back to `.env`. If you run the
  script from a read-only filesystem, token persistence will fail — you'll
  need to refresh manually.

- **yfinance data can lag** by 15–20 minutes for US equities and longer for
  Canadian stocks. Data at 8:30 AM ET may reflect the previous day's close.

- **No earnings alerts** — free data sources don't reliably provide upcoming
  earnings dates. A future version could use an earnings calendar API.

- **No database** — each run fetches fresh data. If Questrade or Yahoo Finance
  is down, that part of the report is blank.

- **Crypto prices** from Yahoo Finance are USD-denominated. The portfolio
  section shows CAD totals from Questrade; there is no automatic FX conversion
  between the two sections.

---

## v2 Ideas

- **Earnings calendar** — integrate a free earnings API (e.g. Finnhub free tier)
  to flag holdings with earnings in the next 7 days.
- **Historical runs** — save each scan to a SQLite file so you can see which
  picks panned out over time.
- **P&L tracking** — record entry prices when a pick is flagged and report
  performance the following week.
- **Telegram/email fallback** — send the report to Telegram or email if Slack
  is unavailable.
- **Sector rotation heatmap** — show which sectors are gaining/losing over
  rolling periods.
- **Position sizing suggestions** — given cash available, recommend a position
  size based on risk level.
- **Technical indicators** — add RSI, MACD, Bollinger Bands as additional
  scoring signals in market_scanner.py.
