#!/bin/sh
# 将 launchd 任务安装到当前用户：每天本地 11:00 运行日报并发邮件。
set -e
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
PLIST_SRC="$ROOT/launchd/com.deepseek-news-digest.plist"
DEST="$HOME/Library/LaunchAgents/com.deepseek-news-digest.plist"
if [ ! -f "$PLIST_SRC" ]; then
  echo "找不到 $PLIST_SRC" >&2
  exit 1
fi
chmod +x "$ROOT/scripts/run_daily_digest_scheduled.sh"
mkdir -p "$ROOT/logs"
cp "$PLIST_SRC" "$DEST"
# 若已加载则先卸载再加载（避免重复）
launchctl bootout "gui/$(id -u)/com.deepseek-news-digest" 2>/dev/null || true
launchctl unload "$DEST" 2>/dev/null || true
launchctl load "$DEST"
echo "已安装: $DEST"
echo "下次将在每天 11:00（本机时区）运行。立即试跑: launchctl start com.deepseek-news-digest"
echo "日志: $ROOT/logs/scheduled-digest.log"
