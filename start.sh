#!/bin/bash
# 启动英文书库网站：http://127.0.0.1:8791 （顺带拉起朗读服务 :8792）
cd "$(dirname "$0")"
[ -x .venv/bin/python ] || bash setup.sh
if [ -x .venv-tts/bin/python ] && ! curl -s --noproxy '*' -m 1 -o /dev/null http://127.0.0.1:8792/health; then
  nohup bash tools/ops/run-service.sh tts .venv-tts/bin/python tts/server.py >/dev/null 2>&1 &
fi
exec .venv/bin/python -m library serve
