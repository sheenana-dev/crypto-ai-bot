#!/bin/bash
# Bot Watchdog - Auto-restarts the trading bot AND telegram listener if they die
# Checks every minute via cron

BOT_DIR="/Users/sheenax/Documents/crypto ai bot"
BOT_SCRIPT="scheduler.py"
TELEGRAM_SCRIPT="telegram_standalone.py"
LOG_FILE="$BOT_DIR/bot.log"
TELEGRAM_LOG="$BOT_DIR/telegram_standalone.log"
WATCHDOG_LOG="$BOT_DIR/watchdog.log"

cd "$BOT_DIR" || exit 1

# Check if bot is running
if ! pgrep -f "$BOT_SCRIPT" > /dev/null; then
    echo "[$(date)] Bot is DOWN - restarting..." >> "$WATCHDOG_LOG"

    # Kill any zombie processes
    pkill -9 -f "$BOT_SCRIPT" 2>/dev/null

    # Wait a moment
    sleep 2

    # Restart bot
    nohup python3 "$BOT_SCRIPT" > "$LOG_FILE" 2>&1 &

    # Wait for startup
    sleep 5

    # Verify it started
    if pgrep -f "$BOT_SCRIPT" > /dev/null; then
        echo "[$(date)] Bot RESTARTED successfully (PID: $(pgrep -f "$BOT_SCRIPT"))" >> "$WATCHDOG_LOG"

        # Send Telegram alert (optional - only if bot has TELEGRAM configured)
        python3 -c "
from agents.notifier import send_telegram
send_telegram('🔄 <b>Bot Auto-Restarted</b>\n\nWatchdog detected bot was down and restarted it automatically.')
" 2>/dev/null
    else
        echo "[$(date)] Bot FAILED to restart!" >> "$WATCHDOG_LOG"

        # Send critical alert
        python3 -c "
from agents.notifier import send_telegram
send_telegram('🚨 <b>Bot Restart FAILED</b>\n\nWatchdog could not restart the bot. Manual intervention needed!')
" 2>/dev/null
    fi
else
    # Bot is running - log heartbeat every hour (only on minute 0)
    if [ "$(date +%M)" = "00" ]; then
        BOT_PID=$(pgrep -f "$BOT_SCRIPT")
        echo "[$(date)] Bot alive (PID: $BOT_PID)" >> "$WATCHDOG_LOG"
    fi
fi

# Check if Telegram standalone listener is running
if ! pgrep -f "$TELEGRAM_SCRIPT" > /dev/null; then
    echo "[$(date)] Telegram listener is DOWN - restarting..." >> "$WATCHDOG_LOG"

    # Kill any zombie processes
    pkill -9 -f "$TELEGRAM_SCRIPT" 2>/dev/null
    sleep 1

    # Restart listener
    nohup python3 "$TELEGRAM_SCRIPT" > "$TELEGRAM_LOG" 2>&1 &
    sleep 3

    # Verify it started
    if pgrep -f "$TELEGRAM_SCRIPT" > /dev/null; then
        echo "[$(date)] Telegram listener RESTARTED (PID: $(pgrep -f "$TELEGRAM_SCRIPT"))" >> "$WATCHDOG_LOG"
    else
        echo "[$(date)] Telegram listener restart FAILED!" >> "$WATCHDOG_LOG"
    fi
else
    # Listener is running - log heartbeat every hour
    if [ "$(date +%M)" = "00" ]; then
        TELEGRAM_PID=$(pgrep -f "$TELEGRAM_SCRIPT")
        echo "[$(date)] Telegram listener alive (PID: $TELEGRAM_PID)" >> "$WATCHDOG_LOG"
    fi
fi
