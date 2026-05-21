#!/bin/sh
# 移除本机 launchd 定时任务（改用 GitHub Actions 或其它云端调度时请执行，避免每天发两封）。
set -e
LABEL="com.deepseek-news-digest"
DEST="$HOME/Library/LaunchAgents/${LABEL}.plist"
launchctl bootout "gui/$(id -u)/${LABEL}" 2>/dev/null || true
launchctl unload "$DEST" 2>/dev/null || true
rm -f "$DEST"
echo "已尝试卸载: $DEST（若从未安装会静默跳过）。"
