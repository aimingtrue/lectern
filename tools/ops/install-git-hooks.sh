#!/bin/bash
# 推送前先跑测试（本机的持续集成；GitHub Actions 起不来时用它兜底）。装在本仓库的 .git/hooks，所有在这里推送的人都会经过它。
#   bash tools/ops/install-git-hooks.sh            安装
#   bash tools/ops/install-git-hooks.sh --remove   卸载
#   临时跳过一次：git push --no-verify
set -e
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
HOOK="$ROOT/.git/hooks/pre-push"
if [ "$1" = "--remove" ]; then rm -f "$HOOK"; echo "已卸载 pre-push"; exit 0; fi
cat > "$HOOK" <<'H'
#!/bin/bash
cd "$(git rev-parse --show-toplevel)"
echo "pre-push：跑测试（git push --no-verify 可跳过）…"
.venv/bin/python -m pytest -q -x -p no:cacheprovider || { echo "测试没通过，已取消推送"; exit 1; }
H
chmod +x "$HOOK"
echo "已安装：$HOOK"
