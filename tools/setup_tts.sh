#!/bin/bash
# 安装 TTS：独立环境 .venv-tts（MLX + mlx-audio）→ 下载 Qwen3-TTS 1.7B（bf16）→ 生成试听
set -uo pipefail
cd "$(dirname "$0")/.."
PY=""
for c in /opt/homebrew/Caskroom/miniforge/base/envs/mfa/bin/python3.12 python3.12 /opt/homebrew/bin/python3.12 python3.13 python3.11; do
  command -v "$c" >/dev/null 2>&1 && { PY=$c; break; }; done
[ -z "$PY" ] && { echo "需要 Python 3.11+（brew install python@3.12）"; exit 1; }
[ "$(uname -m)" = "arm64" ] || { echo "需要 Apple Silicon Mac"; exit 1; }
echo "① 建环境 .venv-tts（${PY}）"
[ -x .venv-tts/bin/python ] || "$PY" -m venv .venv-tts || exit 1
PIP=".venv-tts/bin/pip"
$PIP install -q --upgrade pip
echo "② 安装 mlx-audio 与逐词对齐 mlx-qwen3-asr（版本见 tts/requirements.txt，几分钟）"
if ! $PIP install -q -r tts/requirements.txt; then
  echo "   官方源失败，改用清华镜像重试"
  $PIP install -q -i https://pypi.tuna.tsinghua.edu.cn/simple -r tts/requirements.txt || exit 1
fi
.venv-tts/bin/python -c "import mlx_audio, mlx_qwen3_asr, mlx.core as mx; print('   mlx-audio', getattr(mlx_audio,'__version__','?'), '· mlx', mx.__version__, '· 对齐模型包已装')"
echo "③ 下载模型 Qwen3-TTS 1.7B Base（bf16，约 4 GB）并生成试听"
if ! curl -s -m 8 -o /dev/null https://huggingface.co; then
  echo "   huggingface.co 连不上，改用 hf-mirror.com"; export HF_ENDPOINT=https://hf-mirror.com; fi
.venv-tts/bin/python tts/server.py --prefetch || exit 1
echo "④ 启动朗读音色服务（其他音色用 tools/add_voice.py 装）"
pkill -f 'tts/server[.]py$' ; sleep 1
mkdir -p logs; nohup .venv-tts/bin/python tts/server.py >> logs/tts.log 2>&1 &
for i in $(seq 1 120); do sleep 1; curl -s --noproxy '*' -m 2 http://127.0.0.1:8792/health | grep -q '"ok":true' && break; done
curl -s --noproxy '*' -m 2 http://127.0.0.1:8792/health; echo
echo "完成。"
