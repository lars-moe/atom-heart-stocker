#!/usr/bin/env bash
# Keeps the scheduler running. Start at login, e.g. crontab:  @reboot /path/to/atom_heart_stocker/run_scheduler.sh
cd "$(dirname "$0")"
mkdir -p logs
source .venv/bin/activate 2>/dev/null || true
while true; do
  python main.py scheduler >> logs/scheduler.log 2>&1
  sleep 30   # restart after a crash
done
