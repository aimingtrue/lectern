#!/bin/bash
# 把书库网站（:8791）和朗读音色服务（:8792）交给 launchd 守护：登录后自动启动、崩溃后自动拉起。只装给当前用户。
#   bash tools/ops/install-service-agents.sh            安装 / 更新
#   bash tools/ops/install-service-agents.sh --remove   卸载（回到双击 .command 手动启动）
# 装好后「重启英文书库.command」「打开英文书库.command」会改用 launchctl，不会再起第二份。
set -e
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
DIR="$HOME/Library/LaunchAgents"
UIDN="$(id -u)"
plist() {   # 标签 日志名 命令…
  local label=$1 log=$2; shift 2
  local args=""
  for a in "$@"; do args="$args<string>$a</string>"; done
  cat > "$DIR/$label.plist" <<PL
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>$label</string>
  <key>ProgramArguments</key><array><string>/bin/bash</string><string>$ROOT/tools/ops/run-service.sh</string><string>$log</string>$args</array>
  <key>WorkingDirectory</key><string>$ROOT</string>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><dict><key>SuccessfulExit</key><false/></dict>
  <key>ThrottleInterval</key><integer>10</integer>
</dict></plist>
PL
}
if [ "$1" = "--remove" ]; then
  for l in com.lectern.server com.lectern.tts; do launchctl bootout "gui/$UIDN/$l" 2>/dev/null || true; rm -f "$DIR/$l.plist"; done
  echo "已卸载；之后用「打开英文书库.command」手动启动"; exit 0
fi
mkdir -p "$DIR"
pkill -f 'm library serv[e]' 2>/dev/null || true          # 先停掉手动起的那份，端口留给 launchd
plist com.lectern.server server "$ROOT/.venv/bin/python" -m library serve
labels="com.lectern.server"
if [ -x "$ROOT/.venv-tts/bin/python" ]; then
  pkill -f 'tts/server[.]py$' 2>/dev/null || true
  plist com.lectern.tts tts "$ROOT/.venv-tts/bin/python" "$ROOT/tts/server.py"
  labels="$labels com.lectern.tts"
fi
sleep 1
for l in $labels; do
  launchctl bootout "gui/$UIDN/$l" 2>/dev/null || true
  launchctl bootstrap "gui/$UIDN" "$DIR/$l.plist"
done
for i in $(seq 1 60); do sleep 1; curl -s --noproxy '*' -o /dev/null http://127.0.0.1:8791/api/health && break; done
echo "已安装：$labels（日志 logs/server.log、logs/tts.log，超过 20 MB 启动时轮转）"
