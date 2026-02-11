#!/bin/bash
BOT_DIR="/Users/sheenax/Documents/crypto ai bot"
cd "$BOT_DIR"
echo "=== Killing old crypto bot processes ==="
pkill -f "crypto ai bot/main.py" 2>/dev/null
pkill -f "crypto ai bot/scheduler.py" 2>/dev/null
pkill -f "crypto ai bot/telegram_standalone.py" 2>/dev/null
sleep 2
echo "=== Starting crypto ai bot ==="
nohup python3 main.py > main.log 2>&1 &
echo "main.py started (PID: $!)"
nohup python3 scheduler.py > scheduler.log 2>&1 &
echo "scheduler.py started (PID: $!)"
sleep 3
echo "=== Running processes ==="
ps aux | grep "crypto ai bot" | grep -v grep || echo "WARNING: No processes found"
echo "=== Last 15 lines of main.log ==="
tail -15 main.log
echo "=== DONE ==="
