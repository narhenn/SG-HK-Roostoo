#!/bin/bash
# Deploy script for EC2 — starts all bots
set -e

cd "$(dirname "$0")"
mkdir -p logs data

echo "Killing old processes..."
pkill -f adaptive_bot.py 2>/dev/null || true
pkill -f team_detector.py 2>/dev/null || true
pkill -f pump_detector.py 2>/dev/null || true
pkill -f dashboard_finals.py 2>/dev/null || true
sleep 2

echo "Starting V10 Adaptive Bot..."
nohup python3 -u adaptive_bot.py > logs/adaptive.log 2>&1 &
echo "  PID: $!"

echo "Starting Team Detector..."
nohup python3 -u team_detector.py > logs/team_detector.log 2>&1 &
echo "  PID: $!"

echo "Starting Pump Detector..."
nohup python3 -u pump_detector.py > logs/pump_detector.log 2>&1 &
echo "  PID: $!"

echo "Starting Dashboard..."
nohup python3 -u dashboard_finals.py > logs/dashboard.log 2>&1 &
echo "  PID: $!"

echo ""
echo "All bots started. Verify:"
ps aux | grep python3 | grep -v grep
echo ""
echo "Logs: tail -f logs/adaptive.log"
