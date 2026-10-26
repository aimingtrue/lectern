#!/bin/bash
# 新书入库（本机长任务一次只跑一个）：收件箱移进书库（逐字节相同的才进废纸篓）→ 入库 → 排除名单拦下的移走 → 质量体检
# （这次的新书：坏文件删掉、能修的修好，直接执行）→ 入库门禁（扫描版、质量，直接排除，见 docs/architecture/data-model.md）
# → 去重（同一本书的各版本先比质量再比格式，留最好的一份；排在体检之后，体检判过的坏文件不会被留下）→ 留下的版本换了的
# 重新入库并体检 → 难度 → 排除候选（个人偏好的规则，只出报告，复核后再执行）→ 后台补元数据（tools/ops/metadata.sh）
cd "$(dirname "$0")/../.."
PY=.venv/bin/python
START=$(date +%s)
idle() { [ -z "$($PY -c 'from library import busy; print(busy.other_jobs("io", ttl=0) or "")')" ]; }
until idle; do echo "$(date '+%F %T') 有别的长任务在跑，等 2 分钟"; sleep 120; done
echo "$(date '+%F %T') ⓪ 收件箱：清洗文件名、去掉重复，移进书库"
$PY -m library.ingest --apply 2>&1 | grep -av "^MuPDF"
until idle; do echo "$(date '+%F %T') 字幕/总结队列在跑，等 2 分钟"; sleep 120; done
echo "$(date '+%F %T') ② 入库"
caffeinate -i $PY -m library scan 2>&1 | LC_ALL=C tr '\r' '\n' | tee "logs/scan-$(date +%Y%m%d-%H%M).log" | grep -a "完成\|⚠️" | tail -20
echo "$(date '+%F %T') ②' 排除名单拦下的文件移到废纸篓"
$PY -m library.exclude sweep --apply 2>&1 | grep -a "要移到\|完成\|清单\|没有要移走\|长任务"
echo "$(date '+%F %T') ③ 质量体检（这次的新书：坏文件删掉、能修的修好）"
$PY tools/quality_gate.py --since "$START" --apply 2>&1 | grep -av "^[0-9,]*/\|^MuPDF"
echo "$(date '+%F %T') ③' 入库门禁（扫描版、读不好的书直接排除，宁缺毋滥）"
$PY -m library.exclude gate --since "$START" --apply 2>&1 | grep -a "门禁\|：\|完成\|清单"
echo "$(date '+%F %T') ④ 去重（同一本书各版本比质量：读不出、乱码、明显不完整的不留，再按格式挑）"
DEDUPED=$(date +%s)
$PY -m library.dedupe --apply 2>&1 | grep -a "有多个文件\|完成\|废纸篓\|清单\|迁到"
echo "$(date '+%F %T') ④' 留下的版本换了的书重新入库、体检、门禁"
caffeinate -i $PY -m library scan 2>&1 | LC_ALL=C tr '\r' '\n' | tee "logs/scan-$(date +%Y%m%d-%H%M).log" | grep -a "完成\|⚠️" | tail -5
$PY tools/quality_gate.py --since "$DEDUPED" --apply 2>&1 | grep -av "^[0-9,]*/\|^MuPDF"
$PY -m library.exclude gate --since "$DEDUPED" --apply 2>&1 | grep -a "门禁\|：\|完成\|清单"
echo "$(date '+%F %T') ⑤ 难度分级（新的英文书）"
$PY -m library.enrich level 2>&1 | tail -2
echo "$(date '+%F %T') ⑥ 排除候选（个人偏好的规则，报告；复核后 python -m library.exclude review 报告 --apply）"
$PY -m library.exclude plan 2>&1 | grep -a "命中\|报告\|没有"
echo "$(date '+%F %T') 全部完成；后台补元数据（书目层、评分、简介、封面；已经在跑就不再起第二份）"
nohup caffeinate -i bash tools/ops/metadata.sh >> "logs/metadata-$(date +%Y%m%d).log" 2>&1 &
