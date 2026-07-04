#!/bin/bash
# trend-sol - Monitor principal
# Execute em uma janela tmux dedicada na VPS.

cd "$(dirname "$0")/.."

mkdir -p logs

LOGFILE="logs/monitor_$(date +%Y-%m-%d).txt"

source venv/bin/activate

echo "Iniciando trend-sol em $(date)" | tee -a "$LOGFILE"
python -u main.py >> "$LOGFILE" 2>&1
