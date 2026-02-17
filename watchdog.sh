#!/bin/bash
# Bot Watchdog - Auto-restarts the trading bot if it dies or freezes
# Checks every minute via cron
# Uses caffeinate to prevent Mac sleep while bot is running
# Detects frozen processes by checking heartbeat file timestamps

BOT_DIR="/Users/sheenax/Documents/crypto ai bot"
BOT_SCRIPT="scheduler.py"
LOG_FILE="$BOT_DIR/bot.log"
WATCHDOG_LOG="$BOT_DIR/watchdog.log"
BOT_HEARTBEAT="$BOT_DIR/bot_heartbeat.txt"

cd "$BOT_DIR" || exit 1

# Ensure caffeinate is running to prevent Mac sleep
# -s = prevent system sleep, -w = exit when PID dies
if ! pgrep -x "caffeinate" > /dev/null; then
    # Start caffeinate in background — keeps Mac awake indefinitely
    nohup caffeinate -s > /dev/null 2>&1 &
    echo "[$(date)] Started caffeinate to prevent Mac sleep" >> "$WATCHDOG_LOG"
fi

# Function to check if heartbeat file is stale
check_heartbeat_stale() {
    local heartbeat_file=$1
    local max_age=600  # 10 minutes

    if [ ! -f "$heartbeat_file" ]; then
        return 0  # File doesn't exist, consider it stale
    fi

    if [[ "$OSTYPE" == "darwin"* ]]; then
        file_time=$(stat -f %m "$heartbeat_file" 2>/dev/null || echo 0)
    else
        file_time=$(stat -c %Y "$heartbeat_file" 2>/dev/null || echo 0)
    fi

    current_time=$(date +%s)
    age=$((current_time - file_time))

    if [ $age -gt $max_age ]; then
        return 0  # Stale
    else
        return 1  # Fresh
    fi
}

# Use exact match: "python3 scheduler.py" (not health_check_scheduler.py)
BOT_PGREP="python3 scheduler.py"
BOT_NEEDS_RESTART=0

if ! pgrep -f "python3 $BOT_SCRIPT" > /dev/null; then
    echo "[$(date)] Bot PROCESS is DOWN" >> "$WATCHDOG_LOG"
    BOT_NEEDS_RESTART=1
elif check_heartbeat_stale "$BOT_HEARTBEAT"; then
    BOT_PID=$(pgrep -f "python3 $BOT_SCRIPT")
    echo "[$(date)] Bot FROZEN (PID: $BOT_PID, heartbeat stale >10 min)" >> "$WATCHDOG_LOG"
    BOT_NEEDS_RESTART=1
fi

if [ $BOT_NEEDS_RESTART -eq 1 ]; then
    echo "[$(date)] Bot is DOWN/FROZEN - restarting..." >> "$WATCHDOG_LOG"

    # Kill any zombie or frozen bot processes (exact match only)
    pkill -9 -f "python3 $BOT_SCRIPT" 2>/dev/null

    sleep 2

    # Restart bot — use >> (append) to avoid corrupting log with null bytes
    nohup python3 "$BOT_SCRIPT" >> "$LOG_FILE" 2>&1 &

    sleep 5

    if pgrep -f "python3 $BOT_SCRIPT" > /dev/null; then
        echo "[$(date)] Bot RESTARTED successfully (PID: $(pgrep -f "python3 $BOT_SCRIPT"))" >> "$WATCHDOG_LOG"

        python3 -c "
from agents.notifier import send_telegram
send_telegram('🔄 <b>Bot Auto-Restarted</b>\n\nWatchdog detected bot was down/frozen and restarted it automatically.')
" 2>/dev/null
    else
        echo "[$(date)] Bot FAILED to restart!" >> "$WATCHDOG_LOG"

        python3 -c "
from agents.notifier import send_telegram
send_telegram('🚨 <b>Bot Restart FAILED</b>\n\nWatchdog could not restart the bot. Manual intervention needed!')
" 2>/dev/null
    fi
else
    # Bot is running and responsive - log every hour
    if [ "$(date +%M)" = "00" ]; then
        BOT_PID=$(pgrep -f "python3 $BOT_SCRIPT")
        echo "[$(date)] Bot alive and responsive (PID: $BOT_PID)" >> "$WATCHDOG_LOG"
    fi
fi
