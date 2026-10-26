#!/bin/bash
# 安装每天自动备份（launchd，每天 03:30；电脑睡着错过的话，醒来后补跑一次）。只装给当前用户，不需要管理员权限。
#   bash tools/ops/install-backup-agent.sh            安装 / 更新
#   bash tools/ops/install-backup-agent.sh --remove   卸载
# 备份到哪里：环境变量 LECTERN_BACKUP_DIR（默认 ~/Backups/english-library，最好指到外接盘）。
# 同时推到另一台机器：LECTERN_BACKUP_PUSH=主机:目录（主机是 ~/.ssh/config 里的别名，密钥不能带口令，launchd 下没有 ssh-agent）。
set -e
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
LABEL=com.lectern.backup
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
DEST="${LECTERN_BACKUP_DIR:-$HOME/Backups/english-library}"
PUSH="${LECTERN_BACKUP_PUSH:-}"
PUSHARGS=""; [ -n "$PUSH" ] && PUSHARGS="<string>--push</string><string>$PUSH</string>"
if [ "$1" = "--remove" ]; then
  launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
  rm -f "$PLIST"; echo "已卸载 $LABEL"; exit 0
fi
mkdir -p "$HOME/Library/LaunchAgents" "$ROOT/logs"
cat > "$PLIST" <<PL
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>$LABEL</string>
  <key>ProgramArguments</key><array>
    <string>$ROOT/.venv/bin/python</string><string>$ROOT/tools/backup.py</string><string>--dest</string><string>$DEST</string>$PUSHARGS
  </array>
  <key>WorkingDirectory</key><string>$ROOT</string>
  <key>StartCalendarInterval</key><dict><key>Hour</key><integer>3</integer><key>Minute</key><integer>30</integer></dict>
  <key>StandardOutPath</key><string>$ROOT/logs/backup.log</string>
  <key>StandardErrorPath</key><string>$ROOT/logs/backup.log</string>
</dict></plist>
PL
launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" "$PLIST"
echo "已安装：每天 03:30 备份到 $DEST${PUSH:+，并推到 $PUSH}（日志 logs/backup.log）。立即跑一次：launchctl kickstart gui/$(id -u)/$LABEL"
