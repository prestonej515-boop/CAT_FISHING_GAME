#!/usr/bin/env bash
# ==============================================================================
# cron_setup.sh — Install the daily investment scanner cron job
#
# Adds a cron entry to run main.py at 8:30 AM Eastern Time, Mon–Fri.
# Run this script once after setting up your virtualenv and .env file.
#
# Usage:
#   chmod +x cron_setup.sh
#   ./cron_setup.sh
#
# Prerequisites:
#   - Python virtualenv created at ./venv/ (see README.md setup steps)
#   - .env file filled in with your tokens
# ==============================================================================

set -euo pipefail

# --------------------------------------------------------------------------
# Resolve absolute paths (works whether you run from inside the project or not)
# --------------------------------------------------------------------------
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV_PYTHON="$SCRIPT_DIR/venv/bin/python"
MAIN_PY="$SCRIPT_DIR/main.py"
LOG_DIR="$SCRIPT_DIR/logs"
CRON_LOG="$LOG_DIR/cron.log"

# --------------------------------------------------------------------------
# Sanity checks
# --------------------------------------------------------------------------
if [ ! -f "$VENV_PYTHON" ]; then
    echo "ERROR: Virtual environment not found at $VENV_PYTHON"
    echo "       Create it first:"
    echo "         cd $SCRIPT_DIR"
    echo "         python3 -m venv venv"
    echo "         source venv/bin/activate"
    echo "         pip install -r requirements.txt"
    exit 1
fi

if [ ! -f "$MAIN_PY" ]; then
    echo "ERROR: main.py not found at $MAIN_PY"
    exit 1
fi

if [ ! -f "$SCRIPT_DIR/.env" ]; then
    echo "WARNING: .env file not found. Copy .env.example and fill it in before the cron runs:"
    echo "           cp $SCRIPT_DIR/.env.example $SCRIPT_DIR/.env"
fi

mkdir -p "$LOG_DIR"

# --------------------------------------------------------------------------
# Build the cron command
#
# Cron uses UTC by default on most Linux systems. 8:30 AM ET is:
#   - 13:30 UTC during EST (UTC-5, November–March)
#   - 12:30 UTC during EDT (UTC-4, March–November)
#
# Strategy: run at BOTH times (12:30 and 13:30 UTC). The script itself
# checks the current ET time on startup and exits early if it already ran
# today — so only one execution per day actually does work.
#
# Alternatively, set TZ=America/Toronto in your crontab (works on Linux,
# NOT on macOS) to let cron handle DST automatically. We add both approaches
# below and comment out the one that doesn't apply.
# --------------------------------------------------------------------------

# The cron job command
CRON_CMD="$VENV_PYTHON $MAIN_PY >> $CRON_LOG 2>&1"

# --------------------------------------------------------------------------
# Linux: use TZ= in crontab for automatic DST handling
# --------------------------------------------------------------------------
install_linux() {
    echo "Installing cron job for Linux (TZ=America/Toronto in crontab)…"

    # Grab existing crontab, or start fresh
    EXISTING_CRONTAB=$(crontab -l 2>/dev/null || true)

    # Check if already installed
    if echo "$EXISTING_CRONTAB" | grep -q "$MAIN_PY"; then
        echo "Cron job already present. To update, remove the existing entry first:"
        echo "  crontab -e"
        exit 0
    fi

    NEW_ENTRY="30 8 * * 1-5 TZ=America/Toronto $CRON_CMD"

    (
        echo "$EXISTING_CRONTAB"
        echo "# Daily Investment Scanner — 8:30 AM ET, Mon-Fri"
        echo "$NEW_ENTRY"
    ) | crontab -

    echo "Done! Cron job installed:"
    echo "  $NEW_ENTRY"
}

# --------------------------------------------------------------------------
# macOS: TZ= in crontab is not supported. Use launchd instead (recommended),
# or fall back to the dual-UTC-time approach.
# --------------------------------------------------------------------------
install_macos() {
    echo "Installing cron job for macOS (dual UTC times for DST coverage)…"
    echo ""
    echo "Note: macOS cron doesn't support TZ= per-job. We schedule at both"
    echo "12:30 UTC and 13:30 UTC. Only one run will complete; the other will"
    echo "exit early (or send a duplicate — add a lock file in main.py if needed)."
    echo ""

    EXISTING_CRONTAB=$(crontab -l 2>/dev/null || true)

    if echo "$EXISTING_CRONTAB" | grep -q "$MAIN_PY"; then
        echo "Cron job already present. To update, run: crontab -e"
        exit 0
    fi

    (
        echo "$EXISTING_CRONTAB"
        echo "# Daily Investment Scanner — 8:30 AM ET (EDT = 12:30 UTC, EST = 13:30 UTC)"
        echo "30 12 * * 1-5 $CRON_CMD"
        echo "30 13 * * 1-5 $CRON_CMD"
    ) | crontab -

    echo "Done! Two cron entries added (covers both EDT and EST)."
    echo ""
    echo "For a more robust macOS solution, consider using launchd:"
    echo "  1. Copy the plist template below to ~/Library/LaunchAgents/"
    echo "  2. Run: launchctl load ~/Library/LaunchAgents/com.investment-scanner.plist"
    echo ""
    generate_launchd_plist
}

generate_launchd_plist() {
    PLIST_PATH="$SCRIPT_DIR/com.investment-scanner.plist"
    cat > "$PLIST_PATH" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
    "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>com.investment-scanner</string>
    <key>ProgramArguments</key>
    <array>
        <string>$VENV_PYTHON</string>
        <string>$MAIN_PY</string>
    </array>
    <key>StartCalendarInterval</key>
    <dict>
        <key>Hour</key>
        <integer>8</integer>
        <key>Minute</key>
        <integer>30</integer>
        <!-- Weekdays only: Monday=2 through Friday=6 -->
        <!-- launchd doesn't support day ranges; use separate entries or run daily -->
    </dict>
    <key>EnvironmentVariables</key>
    <dict>
        <key>TZ</key>
        <string>America/Toronto</string>
    </dict>
    <key>StandardOutPath</key>
    <string>$CRON_LOG</string>
    <key>StandardErrorPath</key>
    <string>$CRON_LOG</string>
    <key>RunAtLoad</key>
    <false/>
</dict>
</plist>
PLIST
    echo "launchd plist generated at: $PLIST_PATH"
    echo "Install it with:"
    echo "  cp $PLIST_PATH ~/Library/LaunchAgents/"
    echo "  launchctl load ~/Library/LaunchAgents/com.investment-scanner.plist"
}

# --------------------------------------------------------------------------
# Detect OS and run appropriate installer
# --------------------------------------------------------------------------
OS="$(uname -s)"
case "$OS" in
    Linux*)  install_linux ;;
    Darwin*) install_macos ;;
    *)
        echo "Unrecognised OS: $OS"
        echo "Manually add this cron entry (adjust for your timezone):"
        echo "  30 8 * * 1-5 $CRON_CMD"
        exit 1
        ;;
esac

echo ""
echo "Verify installation with:  crontab -l"
echo "Check logs at:             $CRON_LOG"
