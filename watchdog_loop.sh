#!/bin/bash
# Watchdog wrapper - runs in infinite loop for launchd KeepAlive

while true; do
    bash "/Users/sheenax/Documents/crypto ai bot/watchdog.sh"
    sleep 60
done
