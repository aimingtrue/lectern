#!/bin/bash
# 启动一个常驻服务，输出追加到 logs/<名字>.log；启动前日志超过 20 MB 就轮转（保留 .1 .2 .3 三份）。
#   bash tools/ops/run-service.sh server .venv/bin/python -m library serve
cd "$(dirname "$0")/../.."
export PATH="/opt/homebrew/bin:$PATH"   # launchd 默认 PATH 没有 Homebrew，对齐（mlx_qwen3_asr）要 ffmpeg
LOG="logs/$1.log"; shift
mkdir -p logs
if [ -f "$LOG" ] && [ "$(stat -f%z "$LOG")" -gt 20971520 ]; then
  [ -f "$LOG.2" ] && mv -f "$LOG.2" "$LOG.3"
  [ -f "$LOG.1" ] && mv -f "$LOG.1" "$LOG.2"
  mv -f "$LOG" "$LOG.1"
fi
echo "==== $(date '+%F %T') 启动：$*" >> "$LOG"
exec "$@" >> "$LOG" 2>&1
