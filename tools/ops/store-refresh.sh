#!/bin/bash
# 书城推荐信号重建：书库大改（新书入库、去重、排除、编目拆分作品）之后跑一遍；元数据补全（tools/ops/metadata.sh）
# 跑完后再跑一遍，新到的主题词、体裁、简介才进分类和搜索。问过模型的书不重问，第二遍只有规则部分花时间。
#   两级分类（规则 → 规则归不了的问本地模型 → 重建）→ 原著语言（中文正文的书分「中文书 / 译文」）→ 原版 ↔ 译本 →
#   相似书向量与邻居 → 外文书的中文书名（可靠来源，没有的本机模型译）、主题词译文 → 阅读口味（ego）→ 搜索索引 → 综合口碑分
# 模型部分各自拿 gpu 锁排队，字幕 / 总结队列在跑时等它。网站的预热线程（store.prewarm）看到编目库、书目库变了，
# 自己重建物化视图、货架索引、榜单补充表、口味和书架「为你」，不用重启。
cd "$(dirname "$0")/../.."
PY=.venv/bin/python
step() { echo "== $(date '+%F %T') $*"; }
step "两级分类：规则"
$PY -m library.classify build 2>&1 | tail -1
step "两级分类：规则归不了的书问本地模型（问完重建）"
$PY -u -m library.classify model --wait 2>&1 | grep -av "已问\|模型失败" | tail -3
step "原著语言（编目和书目信号判断不了的中文正文书）"
$PY -u -m library.origin --wait 2>&1 | tail -3
step "原版 ↔ 译本"
$PY -u -m library.translations build --wait 2>&1 | grep -a "关联\|统计\|完成\|停" | tail -5
step "相似书向量、邻居"
$PY -u -m library.similar embed --wait 2>&1 | tail -1
$PY -m library.similar neighbours 2>&1 | tail -1
step "外文书的中文书名（可靠来源；没有的本机模型译书名和作者，问过的不重问）、主题词译文"
$PY -u -m library.zh build --shelf --wait 2>&1 | grep -av "模型失败" | tail -4        # 书架上的书先补齐（含简介）
$PY -u -m library.zh build --only names --wait 2>&1 | grep -av "模型失败" | tail -3
$PY -u -m library.zh build --only subj --wait 2>&1 | tail -2
step "阅读口味（ego 导出）"
$PY -m library.taste sync 2>&1 | tail -2
step "搜索索引"
$PY -m library.search build 2>&1 | tail -1
step "综合口碑分"
$PY -m library.ratings score
step "完成"
