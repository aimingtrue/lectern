#!/bin/bash
# 元数据补全（联网、慢，可断点续跑，查过的不重查）：书目层（ISBN、CIP、自带封面）→ 评分（Goodreads 数据集、Hardcover 按 ISBN 和书名、
# Open Library）→ 简介 / 主题词 / 首版年（Open Library 按 ISBN）→ 外部封面（Open Library 按 ISBN，再按书名 + 作者）
# → 按书名补（Open Library）→ 综合口碑分；Wikidata、Google Books（有 key 时）另起一条线并行（不同站点）；豆瓣评分和条目页（简介）、Apple Books 简介等出版日期任务
# （library.pubdate，也查这两个站）结束再查，免得两边一起请求。各来源的许可与限速见 docs/plans/store-distribution-plan.md。
# 只有书目层要打开几万本书，拿 io 锁（有别的 io 长任务就等）；其余只联网，不拿锁。同一时间只跑一份本脚本。
cd "$(dirname "$0")/../.."
PY=.venv/bin/python
GR=${GOODREADS_DIR:-~/Downloads/goodreads}
step() { echo "== $(date '+%F %T') $*"; }
PIDF=data/.jobs/metadata.pid
if [ -f "$PIDF" ] && kill -0 "$(cat "$PIDF")" 2>/dev/null; then
  echo "$(date '+%F %T') 元数据补全已经在跑（PID $(cat "$PIDF")），不再起第二份"; exit 0
fi
mkdir -p data/.jobs; echo $$ > "$PIDF"; trap 'rm -f "$PIDF"' EXIT
step "书目层：ISBN、CIP、自带封面"
$PY -m library.biblio build --wait 2>&1 | LC_ALL=C tr '\r' '\n' | grep -av "^[0-9]*/[0-9]*$"

step "Wikidata（并行）：获奖、系列、体裁、首版年、中文名"
( $PY -m library.sources.wikidata run > "logs/wikidata-$(date +%Y%m%d).log" 2>&1
  $PY -m library.sources.wikidata zhcn >> "logs/wikidata-$(date +%Y%m%d).log" 2>&1
  echo "$(date '+%F %T') Wikidata 完成" ) &

step "豆瓣评分（并行；等出版日期任务结束再查）"
( while pgrep -f "library.pubdate run" > /dev/null; do sleep 300; done
  $PY -m library.ratings douban >> logs/ratings-douban.log 2>&1
  echo "$(date '+%F %T') 豆瓣评分完成"
  $PY -u -m library.sources.douban run >> logs/douban-subject.log 2>&1      # 已对上的条目页：内容简介、评分、原作名
  echo "$(date '+%F %T') 豆瓣条目页完成" ) &

if [ -f "$GR/goodreads_books.json.gz" ]; then
  step "Goodreads 数据集（本地，按 ISBN）"
  $PY -m library.ratings goodreads "$GR" 2>&1 | tail -1
fi
step "Hardcover（按 ISBN，补 Goodreads 没有的）"
$PY -m library.ratings hardcover >> logs/ratings-hardcover.log 2>&1; tail -2 logs/ratings-hardcover.log
step "Hardcover 书名搜索（各来源都没评分的外文书，书名 + 作者都对上才收；顺带首版年）"
$PY -m library.ratings hardcover-title >> logs/ratings-hardcover.log 2>&1; tail -2 logs/ratings-hardcover.log
step "Open Library 评分（按 ISBN）"
$PY -m library.ratings ol >> logs/ratings-ol.log 2>&1; tail -2 logs/ratings-ol.log
step "Open Library 简介、主题词、首版年（按 ISBN）"
$PY -u -m library.sources.openlibrary run >> logs/openlibrary-full.log 2>&1; tail -1 logs/openlibrary-full.log
step "外部封面（Open Library，按 ISBN，再按书名 + 作者）"
$PY -m library.biblio covers 2>&1 | grep -av "^[0-9,]*/" | tail -3

step "Google Books（并行；data/config.json 里有 google_books_key 时）：简介、分类、出版年、封面，中文书先查"
( $PY -u -m library.sources.google_books run --all --limit 900 >> logs/googlebooks.log 2>&1
  echo "$(date '+%F %T') Google Books 这一轮完成（每天额度有限，没查完的下次接着查）" ) &

step "Apple Books 简介（并行；Open Library 补过之后还没有简介的英文书，等出版日期任务结束再查）"
( while pgrep -f "library.pubdate run" > /dev/null; do sleep 300; done
  $PY -u -m library.sources.apple_books run >> logs/applebooks.log 2>&1
  echo "$(date '+%F %T') Apple Books 完成" ) &
step "Open Library 按书名补（没有 ISBN 的英文书：出版年、作者、简介、评分）"
$PY -u -m library.enrich online >> logs/enrich-online.log 2>&1; tail -1 logs/enrich-online.log
step "综合口碑分"
$PY -c 'from library.config import load; from library import ratings as r; print("并入按书名查到的评分：", r.from_enrich(load().data))'
$PY -m library.ratings score
wait
$PY -m library.ratings score
step "完成"; $PY -m library.coverage --missing
