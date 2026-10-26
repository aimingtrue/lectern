#!/bin/bash
# 第一次使用：建虚拟环境、装依赖。之后用 start.sh 启动。
set -euo pipefail
cd "$(dirname "$0")"
PY=""
for c in python3.12 python3.13 python3.11 /opt/homebrew/bin/python3.12 python3; do command -v $c >/dev/null 2>&1 && { PY=$c; break; }; done
[ -z "$PY" ] && { echo "需要 Python 3.11+（brew install python@3.12）"; exit 1; }
if [ ! -x .venv/bin/python ]; then "$PY" -m venv .venv; fi
.venv/bin/pip install -q --upgrade pip
.venv/bin/pip install -q -r requirements.txt
[ -f data/dict.db ] || echo "提示：data/dict.db（离线词典）不存在，查词会不可用。见 README。"
echo "环境就绪"
