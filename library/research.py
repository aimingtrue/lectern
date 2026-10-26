"""资料：本机的研究资料（Markdown 研究笔记、调研清单，默认在仓库的 research/，见 Config.research），在书库里浏览，
也能送进阅读器逐句朗读。

  /research                 总览：每个资料集一节，板块（问题调研、财报拆解…）下面是专题卡片，新的在前；卡片上是导读的
                            中文标题、一句话答案、把握度、核验日期和各篇的短名（导读 · 证据 · 分析 · 结论）。新加的文档自动出现，
                            标题都取导读的一级标题（「earnings — 财报拆解」这类目录键前缀去掉），不露目录名、文件名
  /research/{path}          目录：它的 README，板块下面是专题卡片，一组文档下面是各篇；一篇 .md：渲染（表格、图、mermaid，站内相对链接改成资料页地址）；
                            脚本、数据等文字文件原样显示；图片转到 /research/file/
  /research/file/{path}     资料里的原文件（图片等）
  /api/research/read        把一篇 .md 入库到阅读器（解析见 library/md.py），返回书号；文档改过会重新解析

只给资料目录里的文件：路径里不能有以点开头的段（.git、..），解析符号链接后也必须还在目录里。
"""
from __future__ import annotations

import datetime as dt
import html
import mimetypes
import posixpath
import re
from pathlib import Path
from urllib.parse import quote, unquote

from fastapi import Body, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse

from .extract import book_id
from .md import render, title_of
from .ui import same_origin
from .txt import decode
from .web import page, sv

e = html.escape
TEXTS = {".py", ".txt", ".json", ".csv", ".tsv", ".yml", ".yaml", ".toml", ".sh", ".js", ".css"}
TEXT_MAX = 400_000                  # 原样显示的文字文件最多这么大
SCHEME = re.compile(r"^[a-z][a-z0-9+.-]*:", re.I)

READ_JS = """<script>
const rd=document.getElementById('read');
if(rd)rd.onclick=async()=>{rd.disabled=true;const s=document.getElementById('readst');s.textContent='Preparing this document — usually a few seconds…';
 try{const r=await fetch('/api/research/read',{method:'POST',headers:{'content-type':'application/json'},body:JSON.stringify({path:rd.dataset.path})});
  const j=await r.json();if(!r.ok)throw new Error(j.detail||r.status);location.href='/read/'+j.book}
 catch(err){rd.disabled=false;s.textContent='Cannot open it right now: '+err.message}};
</script>"""


CJK = re.compile(r"[㐀-鿿]")
SLUG = re.compile(r"^[a-z0-9][a-z0-9-]*\s+[—–]\s+(?=.*[㐀-鿿])")       # 「earnings — 财报拆解」：目录键前缀
GUIDE = re.compile(r"\s+[—–]\s+导读$")                                  # 「Acme 2026 Q3 财报 — 导读」
DASH = re.compile(r"\s+[—–]\s+")
ANSWER = re.compile(r"^\*\*(?:一句话答案|一句话结论|结论)\*\*[：:]\s*(.+)$", re.M)
SURE = re.compile(r"\*\*把握度\*\*[：:]\s*([^。.，,\s（(]+)")
CHECKED = re.compile(r"核验日期[：:]\s*(\d{4}-\d{2}-\d{2})")
SUMMARY_MAX = 96


def clean(t: str) -> str:
    """资料页上显示的标题：去掉目录键前缀和「— 导读」后缀。"""
    return GUIDE.sub("", SLUG.sub("", t)).strip()


def label(t: str, readme: bool = False) -> str:
    """一篇文档在卡片、目录里的短名：README 是「导读」；「证据：……」取冒号前，「Acme 2026 Q3 — 解读」取破折号后。"""
    if readme:
        return "Overview"
    t = clean(t)
    head, sep, _ = t.partition("：")
    if sep:
        h = head if len(head) <= 8 else head.split()[-1]       # 「梅赛德斯-Widget 公司集团 三年数据底稿：…」→ 三年数据底稿
        if len(h) <= 8 and CJK.search(h):
            return h
    parts = DASH.split(t, maxsplit=1)
    if len(parts) == 2 and len(parts[1]) <= 10 and CJK.search(parts[1]):
        return parts[1]
    return t if len(t) <= 14 else t[:13] + "…"


def plain(s: str) -> str:
    """Markdown 一行 → 纯文字（卡片摘要用）。"""
    s = re.sub(r"!\[[^\]]*\]\([^)]*\)", "", s)
    s = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", s)
    return re.sub(r"[*_`]", "", s).strip()


def summary(text: str) -> dict:
    """导读里的一句话答案（没有就第一段正文）、把握度、核验日期。"""
    m = ANSWER.search(text)
    if m:
        s = m.group(1)
    else:
        s = next((x for x in text.split("\n\n") if x.strip() and not x.lstrip().startswith(("#", "|", ">", "!", "-", "```", "**Question**"))), "")
    s = plain(" ".join(s.split()))
    if len(s) > SUMMARY_MAX:
        s = s[:SUMMARY_MAX - 1].rstrip("，、；：,; ") + "…"
    sure, chk = SURE.search(text), CHECKED.search(text)
    return {"text": s, "sure": sure.group(1) if sure else "", "checked": chk.group(1) if chk else ""}


def url(rel: str, raw: bool = False) -> str:
    rel = rel.strip("/")
    return ("/research/file/" if raw else "/research/" if rel else "/research") + quote(rel)


def fixer(base: str):
    """一篇文档（所在目录 base，相对资料根）里的相对链接 → 资料页地址；图片 → 原文件地址。外链、锚点、资料目录外的原样留着。"""
    def fix(u: str, attr: str) -> str:
        if not u or SCHEME.match(u) or u.startswith(("#", "/")):
            return u
        target, _, frag = u.partition("#")
        rel = posixpath.normpath(posixpath.join(base, unquote(target)))
        if rel == ".":
            rel = ""
        if rel.startswith(".."):
            return u
        if attr == "src":
            return url(rel, raw=True)
        return url(rel) + ("#" + frag if frag else "")
    return fix


def register(app: FastAPI, st) -> None:
    cfg = st.cfg
    head = f'<link rel="stylesheet" href="{sv("research.css")}">'
    titles: dict[str, tuple[float, str]] = {}       # 路径 → (mtime, 标题)

    def root() -> Path:
        return cfg.research_root().resolve()

    def locate(rel: str) -> Path:
        base = root()
        parts = [x for x in rel.split("/") if x]
        if any(x.startswith(".") for x in parts):
            raise HTTPException(404, "No such document")
        p = base.joinpath(*parts).resolve() if parts else base
        if not p.is_relative_to(base) or not p.exists():
            raise HTTPException(404, "No such document")
        return p

    def rel_of(p: Path) -> str:
        return p.relative_to(root()).as_posix() if p != root() else ""

    def title(p: Path) -> str:
        """文档的标题（第一个一级标题，没有就用文件名）；目录用它的 README 的标题。"""
        if p.is_dir():
            readme = p / "README.md"
            return title(readme) if readme.exists() else p.name
        key, mt = str(p), p.stat().st_mtime
        hit = titles.get(key)
        if hit and hit[0] == mt:
            return hit[1]
        t = title_of(decode(p.read_bytes())) if p.suffix.lower() == ".md" else ""
        t = t or (p.parent.name if p.name == "README.md" and p != root() / "README.md" else p.stem)
        titles[key] = (mt, t)
        return t

    def children(d: Path) -> tuple[list[Path], list[Path]]:
        """目录里的子目录和文件（不含以点开头的、__pycache__）；文件 README 在前。"""
        items = [x for x in sorted(d.iterdir()) if not x.name.startswith(".") and x.name != "__pycache__"]
        dirs = [x for x in items if x.is_dir()]
        files = sorted((x for x in items if x.is_file()), key=lambda x: (x.name != "README.md", x.name))
        return dirs, files

    def crumbs(p: Path) -> str:
        """面包屑：每一级用它的中文标题（目录用 README 的标题），不露目录名。"""
        out = ['<a href="/research">Notes</a>']
        parts = rel_of(p).split("/") if rel_of(p) else []
        for i in range(len(parts)):
            rel = "/".join(parts[:i + 1])
            name = clean(title(root() / rel))
            out.append(f'<a href="{url(rel)}">{e(name if len(name) <= 20 else name[:19] + "…")}</a>')
        return '<nav class="crumbs">' + "<i>/</i>".join(out) + "</nav>"

    infos: dict[str, tuple[float, dict]] = {}       # README 路径 → (mtime, 摘要)

    def info(d: Path) -> dict:
        readme = d / "README.md"
        if not readme.exists():
            return {"text": "", "sure": "", "checked": ""}
        key, mt = str(readme), readme.stat().st_mtime
        hit = infos.get(key)
        if not hit or hit[0] != mt:
            hit = infos[key] = (mt, summary(decode(readme.read_bytes())))
        return hit[1]

    def mds(d: Path) -> list[Path]:
        """目录里直接放的 .md，README 在前。"""
        return sorted((x for x in d.iterdir() if x.is_file() and x.suffix.lower() == ".md" and not x.name.startswith(".")),
                      key=lambda x: (x.name != "README.md", x.name))

    def subdirs(d: Path) -> list[Path]:
        """下面还有 .md 的子目录（charts/、data/ 这类不算）。"""
        return [x for x in children(d)[0] if any(not any(s.startswith(".") for s in y.relative_to(x).parts)
                                                for y in x.rglob("*.md"))]

    def is_set(d: Path) -> bool:
        """一组文档（一个专题、一份财报）：下面没有文档目录；或者除了 README 还有别的 .md、下面的文档目录都不再分
        （专题里的 originals/ 原文存档）。notes/ 下面有 topics/ 这种还要再分的板块，是分组。"""
        subs = subdirs(d)
        return not subs or (any(x.name != "README.md" for x in mds(d)) and not any(subdirs(x) for x in subs))

    def newest(d: Path) -> str:
        """排序用：导读里的核验日期，没有就文件的修改日期。"""
        chk = info(d)["checked"]
        if chk:
            return chk
        ts = [x.stat().st_mtime for x in d.rglob("*.md")]
        return dt.date.fromtimestamp(max(ts)).isoformat() if ts else ""

    def card(d: Path) -> str:
        inf = info(d)
        meta = "".join(f"<span>{e(x)}</span>" for x in (f"confidence {inf["sure"]}" if inf["sure"] else "",
                                                         f"checked {inf["checked"]}" if inf["checked"] else "") if x)
        chips = [f'<a href="{url(rel_of(x))}">{e(label(title(x), x.name == "README.md"))}</a>' for x in mds(d)]
        chips += [f'<a href="{url(rel_of(x))}">{e(label(title(x)))}</a>' for x in subdirs(d)]
        return (f'<div class="rcard"><a class="rt" href="{url(rel_of(d))}">{e(clean(title(d)))}</a>'
                + (f'<p>{e(inf["text"])}</p>' if inf["text"] else "")
                + (f'<div class="rmeta">{meta}</div>' if meta else "")
                + (f'<div class="rchips">{"".join(chips)}</div>' if len(chips) > 1 else "") + "</div>")

    def parts_of(d: Path) -> tuple[list[Path], list[Path]]:
        """一个分组下面的 (文档组, 子分组)；没有 README 的中间目录（WIDGET/ 只套着 trends-3y/）摊平到上一级。"""
        sets, groups = [], []
        for x in subdirs(d):
            if is_set(x):
                sets.append(x)
            elif (x / "README.md").exists():
                groups.append(x)
            else:
                s, g = parts_of(x)
                sets += s
                groups += g
        return sets, groups

    def section(d: Path, level: int, top: bool = False) -> str:
        """一个分组：标题、导读摘要、文档组卡片（新的在前）、子分组。资料集这一级的零散文档和小目录收进「更多」。"""
        sets, groups = parts_of(d)
        sets.sort(key=newest, reverse=True)
        groups.sort(key=lambda g: max([newest(x) for x in parts_of(g)[0]] or [""]), reverse=True)
        more = [x for x in mds(d) if x.name != "README.md"]
        if top and groups:                        # 研究笔记下的 ideas/、scripts/：不占卡片
            more += sets
            sets = []
        h = f"h{level}"
        intro = info(d)["text"]
        out = [f'<section class="rsec lv{level}"><{h}><a href="{url(rel_of(d))}">{e(clean(title(d)))}</a></{h}>'
               + (f'<p class="rintro">{e(intro)}</p>' if intro else "")]
        if sets:
            out.append(f'<div class="rcards">{"".join(card(x) for x in sets)}</div>')
        out += [section(g, level + 1) for g in groups]
        if more:
            out.append('<div class="rmore"><b>More</b>' + "".join(
                f'<a href="{url(rel_of(x))}">{e(clean(title(x)))}</a>' for x in more) + "</div>")
        return "".join(out) + "</section>"

    def home() -> str:
        """资料总览：每个资料集一节；本身就是一组文档的资料集（github/）放进「其他资料」。"""
        base = root()
        cols = subdirs(base)
        big = [x for x in cols if not is_set(x)]
        small = [x for x in cols if is_set(x)]
        n_sets = sum(1 for x in base.rglob("README.md") if x.parent != base and is_set(x.parent))
        n_docs = sum(1 for x in base.rglob("*.md") if not any(s.startswith(".") for s in x.relative_to(base).parts))
        dates = [info(x.parent)["checked"] for x in base.rglob("README.md")]
        last = max((x for x in dates if x), default="")
        lead = f"{n_sets} topics · {n_docs} documents" + (f" · last checked {last}" if last else "")
        out = [f'<h1>Notes</h1><p class="rlead">{lead}. Any of them can be sent to the reader and read aloud sentence by sentence.</p>']
        out += [section(x, 2, top=True) for x in big]
        if small:
            out.append('<section class="rsec lv2"><h2>Other notes</h2>'
                       f'<div class="rcards">{"".join(card(x) for x in small)}</div></section>')
        return "".join(out)

    def relabel(body: str) -> str:
        """站内链接的文字就是目标的文件名、目录名（「[01-evidence.md](01-evidence.md)」「[china-housing-2029](…)」）：
        文档换成它的短名（证据、分析…），目录换成「进入」。写在正文里的代码（`README.md`）不动。"""
        def fix(m):
            href, text = m.group(1), m.group(2)
            rel = unquote(href[len("/research/"):]).partition("#")[0].strip("/")
            t = text.strip().rstrip("/")
            if not rel or not t or " " in t or not ("/" + rel).endswith("/" + t):     # 也认「ideas/inbox.md」「ACME/trends-3y」
                return m.group(0)
            q = root() / rel
            if q.is_dir():
                return f'<a href="{href}">Open</a>'
            if q.is_file() and q.suffix.lower() == ".md":
                return f'<a href="{href}">{e(label(title(q), q.name == "README.md"))}</a>'
            return m.group(0)
        return re.sub(r'<a href="(/research/[^"]+)">([^<]+)</a>', fix, body)

    def article(p: Path) -> tuple[str, list, bool]:
        base = posixpath.dirname(rel_of(p))
        body, toc = render(decode(p.read_bytes()), fixer(base))
        return f'<article class="doc">{relabel(body)}</article>', toc, 'class="mermaid"' in body

    def mermaid(on: bool) -> str:
        if not on:
            return ""
        return (f'<script src="{sv("vendor/mermaid.min.js")}"></script><script>'
                "const dk=document.documentElement.dataset.theme==='dark'||(!document.documentElement.dataset.theme"
                "&&matchMedia('(prefers-color-scheme: dark)').matches);"
                "mermaid.initialize({startOnLoad:true,securityLevel:'strict',theme:dk?'dark':'neutral'});</script>")

    def toc_html(toc: list) -> str:
        def items(ts):                              # toc_tokens 的 name 是转义过的纯文字，不再转义
            return "".join(f'<li><a href="#{e(t["id"])}">{t["name"]}</a>'
                           f'{"<ul>" + items(t["children"]) + "</ul>" if t["children"] and t["level"] < 3 else ""}</li>'
                           for t in ts)
        flat = toc[0]["children"] if len(toc) == 1 and toc[0]["level"] == 1 else toc    # 只有一个一级标题：它是篇名
        def count(ts):
            return sum(1 + count(t["children"]) for t in ts)
        if count(flat) < 2:
            return ""
        return f'<aside class="doc-toc"><b>On this page</b><ul>{items(flat)}</ul></aside>'

    def listing(d: Path) -> str:
        """一组文档的目录页：每篇的短名和标题；下面的文档目录用它的标题；脚本、数据这类文件照文件名列。"""
        dirs, files = children(d)
        docs = set(subdirs(d))
        rows = [f'<li><b>{e(label(title(x)))}</b><a href="{url(rel_of(x))}">{e(clean(title(x)))}</a></li>'
                for x in mds(d) if x.name != "README.md"]
        rows += [f'<li><b>{e(label(title(x)))}</b><a href="{url(rel_of(x))}">{e(clean(title(x)))}</a></li>'
                 for x in dirs if x in docs]
        rows += [f'<li class="raw"><b>Attachment</b><a href="{url(rel_of(x))}">{e(x.name)}/</a></li>' for x in dirs if x not in docs]
        rows += [f'<li class="raw"><b>Attachment</b><a href="{url(rel_of(x))}">{e(x.name)}</a></li>'
                 for x in files if x.suffix.lower() != ".md"]
        return f'<ul class="ls">{"".join(rows)}</ul>' if rows else ""

    # ============================================================ 页面
    @app.get("/research", response_class=HTMLResponse)
    def research_home():
        return show("")

    @app.get("/research/file/{rel:path}")
    def research_file(rel: str):
        p = locate(rel)
        if not p.is_file():
            raise HTTPException(404, "No such document")
        mt = mimetypes.guess_type(p.name)[0] or "application/octet-stream"
        if mt in ("text/html", "application/xhtml+xml"):     # 资料里的网页不在书库的地址下执行
            mt = "text/plain"
        return FileResponse(p, media_type=mt, headers={"Cache-Control": "no-cache"})

    @app.get("/research/{rel:path}", response_class=HTMLResponse)
    def research_page(rel: str):
        return show(rel)

    def show(rel: str):
        p = locate(rel)
        if p.is_dir():
            readme = p / "README.md"
            art, toc, mm = article(readme) if readme.exists() else ("", [], False)
            if p == root():                      # 总览：按资料集、板块排的卡片；根目录的 README 收在最后
                about = f'<details class="rabout"><summary>About these notes</summary>{art}</details>' if art else ""
                body = f'<div class="wrap research rhome">{home()}{about}<div style="height:60px"></div></div>'
                return page("Notes", body, "research", head + mermaid(mm))
            name = clean(title(p))
            if is_set(p):
                ls = listing(p)
                extra = f'<h2 class="rls">In this folder</h2>{ls}' if ls else ""
            else:                                # 板块（专题、财报）：导读下面是它的卡片
                sets, groups = parts_of(p)
                extra = (f'<div class="rhome">{"".join(section(g, 2) for g in groups)}</div>' if groups else "")
                if sets:
                    sets.sort(key=newest, reverse=True)
                    extra = f'<h2 class="rls">In this folder</h2><div class="rcards">{"".join(card(x) for x in sets)}</div>' + extra
            if not art and not extra:
                art = '<div class="empty">Nothing here yet</div>'
            body = f"""<div class="wrap research">{crumbs(p)}
{art or f"<h1>{e(name)}</h1>"}{extra}<div style="height:60px"></div></div>"""
            return page(name, body, "research", head + mermaid(mm))
        ext = p.suffix.lower()
        if ext == ".md":
            art, toc, mm = article(p)
            bid = st.db.one("SELECT last_read FROM books WHERE path=? AND status='ok'", (str(p),))
            label = "Continue reading aloud" if bid and bid["last_read"] else "Send to reader"
            acts = (f'<div class="doc-acts"><button class="btn primary" id="read" data-path="{e(rel_of(p))}">{label}</button>'
                    '<span class="muted" id="readst"></span></div>')
            body = f"""<div class="wrap research">{crumbs(p)}{acts}
<div class="doc-grid">{art}{toc_html(toc)}</div><div style="height:60px"></div></div>{READ_JS}"""
            return page(title(p), body, "research", head + mermaid(mm))
        if ext in TEXTS and p.stat().st_size <= TEXT_MAX:
            body = f"""<div class="wrap research">{crumbs(p)}
<h1 class="file">{e(p.name)}</h1><pre class="code">{e(decode(p.read_bytes()))}</pre><div style="height:60px"></div></div>"""
            return page(p.name, body, "research", head)
        return RedirectResponse(url(rel_of(p), raw=True))

    # ============================================================ 送进阅读器
    @app.post("/api/research/read")
    def research_read(req: Request, payload: dict = Body(...)):
        same_origin(req)
        p = locate(str(payload.get("path") or ""))
        if not p.is_file() or p.suffix.lower() != ".md":
            raise HTTPException(400, "Only Markdown documents can be sent to the reader")
        if st.scanner.words is None:
            st.scanner.words = st.words
        r = st.scanner.add_file(p, full=True)          # 文档没改过就跳过；改过了按新内容重新入库
        bid = book_id(p)
        b = st.db.book(bid)
        if r == "failed" or not b or b["status"] != "ok":
            raise HTTPException(500, (b["error"] if b and b["error"] else "No readable text in this document")[:200])
        return {"book": bid}
