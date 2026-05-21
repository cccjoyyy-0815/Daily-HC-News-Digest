#!/bin/sh
# 供 launchd / cron 调用：在项目根目录执行 run_daily_digest.py，并把日志追加到 logs/。
set -e
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
mkdir -p "$ROOT/logs"
exec >>"$ROOT/logs/scheduled-digest.log" 2>&1
echo "======== $(date '+%Y-%m-%d %H:%M:%S %z') ========"
cd "$ROOT"
if [ -x "$ROOT/.venv/bin/python" ]; then
  exec "$ROOT/.venv/bin/python" "$ROOT/run_daily_digest.py"
else
  exec /usr/bin/env python3 "$ROOT/run_daily_digest.py"
fi
