"""书库网站：FastAPI。页面由服务端直接渲染（上万本书也只查一页），阅读器是前端脚本。"""
from __future__ import annotations

import hashlib
import html
import json
import re
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles

from . import busy
from .config import Config
from .db import DB
from .dictionary import Dictionary
from .extract import parser_ver
from .llm import Ollama
from .scan import Scanner

STATIC = Path(__file__).parent / "static"
e = html.escape
_sv: dict[str, str] = {}


def sv(name: str) -> str:
    """静态文件的地址，带按内容算的版本号：改了文件地址就变，浏览器可以放心缓存一年（见 VersionedStatic）。"""
    if name not in _sv:
        p = STATIC / name
        _sv[name] = hashlib.sha1(p.read_bytes()).hexdigest()[:10] if p.exists() else "0"
    return f"/static/{name}?v={_sv[name]}"


class VersionedStatic(StaticFiles):
    """带 ?v= 的静态文件缓存一年（地址随内容变），不带的每次校验。"""

    async def get_response(self, path, scope):
        r = await super().get_response(path, scope)
        r.headers["cache-control"] = ("public, max-age=31536000, immutable" if b"v=" in scope.get("query_string", b"")
                                      else "no-cache")
        return r


NO_GZIP = ("/api/tts", "/cover/", "/api/voice/")


def gzip_except_media(app):
    """gzip 压缩页面、脚本、JSON（书城首页 126 KB → 23 KB）；音频、图片本来就压不动，直接过。"""
    from starlette.middleware.gzip import GZipMiddleware
    gz = GZipMiddleware(app, minimum_size=1024)

    async def mw(scope, receive, send):
        if scope["type"] == "http" and scope["path"].startswith(NO_GZIP):
            await app(scope, receive, send)
        else:
            await gz(scope, receive, send)
    return mw


def hue(s: str) -> int:
    return int(hashlib.md5(s.encode()).hexdigest()[:4], 16) % 360


# ---------------------------------------------------------------- 页面框架
def page(title: str, body: str, nav: str = "", extra_head: str = "", full: bool = False) -> HTMLResponse:
    def a(href, label, key):
        return f'<a href="{href}" class="{"on" if nav == key else ""}">{label}</a>'
    header = "" if full else f"""
<header class="top"><div class="wrap">
  <a class="brand" href="/shelf">Lectern<em>local · read in your own voice</em></a>
  <nav class="main">{a("/shelf", "Shelf", "shelf")}{a("/research", "Notes", "research")}</nav>
  <span class="spacer"></span>
</div></header>"""
    return HTMLResponse(f"""<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{e(title)} · Lectern</title>
<link rel="icon" href="/static/icon.svg"><link rel="stylesheet" href="{sv("app.css")}">{extra_head}
<script>try{{const t=localStorage.getItem('lib.theme');if(t)document.documentElement.dataset.theme=t}}catch(e){{}}</script>
</head><body>{header}{body}</body></html>""")


def read_pct(b) -> int:
    """读到哪了（%）：按段落；还没按段落解析过的书按页。"""
    if b["n_paras"]:
        return min(100, round(100 * (b["progress_para"] + 1) / b["n_paras"]))
    return min(100, round(100 * (b["progress_page"] + 1) / b["pages"])) if b["pages"] else 0


def toc4(b) -> list:
    """目录 [[级别, 标题, 页(0 起), 段落序号]]；旧版本解析的书没有段落序号时记 0。"""
    return [[e[0], e[1], e[2], e[3] if len(e) > 3 else 0] for e in json.loads(b["toc"] or "[]")]


def cover_html(b, progress: bool = True) -> str:
    bar = ""
    if progress and b["last_read"]:
        bar = f'<div class="bar"><i style="width:{read_pct(b)}%"></i></div>'
    # 文字封面只给第一页是纯文字的 PDF；epub / mobi 的「第一页」是 HTML，早先也被标成了文字封面（2 万多本），
    # 它们用自带的封面图（/cover 取不到图时自己会回文字封面）
    if (b["typo_cover"] and str(b["path"] or "").lower().endswith(".pdf")) or b["status"] != "ok":
        h = hue(b["id"])
        return (f'<div class="cover" style="background:linear-gradient(160deg,hsl({h} 32% 30%),hsl({(h + 30) % 360} 30% 18%))">'
                f'<div class="typo"><b>{e(b["title"] or "")}</b><span>{e(b["author"] or "")}</span></div>{bar}</div>')
    return f'<div class="cover"><img loading="lazy" src="/cover/{b["id"]}.jpg" alt="">{bar}</div>'


def json_script(obj) -> str:
    """嵌进 &lt;script&gt; 的 JSON：&lt; &gt; &amp; 和 U+2028/2029 写成 \\u 转义，数据（书名等）闭合不了脚本、也开不了注释。"""
    return (json.dumps(obj, ensure_ascii=False).replace("<", "\\u003c").replace(">", "\\u003e")
            .replace("&", "\\u0026").replace("\u2028", "\\u2028").replace("\u2029", "\\u2029"))


# ---------------------------------------------------------------- 只服务本机
LOCAL_HOSTS = {"127.0.0.1", "localhost", "[::1]", "testserver"}     # testserver：TestClient 的默认主机名
WRITE_METHODS = {"POST", "PUT", "PATCH", "DELETE"}


def host_name(host: str) -> str:
    """Host 头去掉端口（[::1]:8791 → [::1]）。"""
    host = host.strip().lower()
    if host.startswith("["):                     # IPv6：[::1] 或 [::1]:8791
        return host[:host.find("]") + 1]
    return host.split(":", 1)[0]


def local_only(cfg: Config):
    """中间件：
    · Host 必须是本机名字（防 DNS 重绑定：攻击者域名解析到 127.0.0.1 时浏览器当作同源）；
    · 写请求只接受本站页面：带 Origin 的必须同源，Sec-Fetch-Site 是跨站的拒绝（别的网页能向 127.0.0.1
      发不触发预检的「简单请求」）。没有 Origin 的（命令行、脚本）照常。"""
    from starlette.responses import JSONResponse
    hosts = LOCAL_HOSTS | ({cfg.host} if cfg.host not in ("0.0.0.0", "::", "") else set())

    async def mw(request, call_next):
        host = request.headers.get("host", "")
        if host_name(host) not in hosts:
            return JSONResponse({"detail": "local addresses only"}, status_code=403)
        if request.method in WRITE_METHODS:
            origin = request.headers.get("origin")
            if origin is not None and origin.split("://", 1)[-1].lower() != host.lower():
                return JSONResponse({"detail": "same-origin requests only"}, status_code=403)
            if request.headers.get("sec-fetch-site") in ("cross-site", "same-site"):
                return JSONResponse({"detail": "same-origin requests only"}, status_code=403)
        return await call_next(request)
    return mw


def fts_query(q: str) -> str:
    toks = re.findall(r"[\w’']+", q.lower())
    return " ".join('"' + t.replace('"', "") + '"' + ("*" if i == len(toks) - 1 else "") for i, t in enumerate(toks))


# ---------------------------------------------------------------- 应用
class App:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.db = DB(cfg.db_path)
        self.dict = Dictionary(cfg.dict_path)
        self._words: set | None = None
        self.llm = Ollama(cfg.ollama)
        self.scanner = Scanner(self.db, cfg, None)
        self.guide_job: dict | None = None
        self.prep_job: dict | None = None

    @property
    def words(self) -> set:
        if self._words is None:
            self._words = self.dict.words()
        return self._words

    def fresh(self, bid: str):
        """打开一本书时按需解析（单本、同步、加锁）：解析器升级过，或者扫描时只存了摘要。返回最新的 books 行。

        文件已不在：有旧段落就照旧给，没存全文则 410。"""
        b = self.db.book(bid)
        if not b:
            raise HTTPException(404, "No such book")
        if (b["parser_ver"] or 0) < parser_ver(b["path"]) or not b["stored"]:
            if self.scanner.words is None:
                self.scanner.words = self.words
            r = self.scanner.refresh(bid)
            b = self.db.book(bid)
            if not b:
                raise HTTPException(404, "This file has changed — reopen it from the library")
            if r == "missing" and (not b["n_paras"] or not b["stored"]):
                raise HTTPException(410, "The file has moved — rescan from the admin page")
            if r == "failed" and not b["stored"]:
                raise HTTPException(422, f"Could not extract text: {(b["error"] or "parse failed")[:160]}")
        return b

    def need(self, bid: str):
        b = self.db.book(bid)
        if not b:
            raise HTTPException(404, "No such book")
        return b

    def fresh_or_old(self, bid: str):
        """页面用：按需重新解析；文件不在了、读不出正文也照样显示书的信息。"""
        try:
            return self.fresh(bid)
        except HTTPException as ex:
            if ex.status_code in (410, 422):
                return self.need(bid)
            raise

    def busy_reason(self, kind: str = "gpu") -> str | None:
        """这一类长任务现在能不能起：gpu（预生成、导读、人物抽取）只看用模型的任务，io（扫描）只看读写文件的任务。"""
        if kind == "io":
            if self.scanner.state.running:
                return "a library scan is running"
            return busy.other_jobs("io")
        if self.guide_job and self.guide_job.get("running"):
            return "another chapter guide is being generated"
        if self.prep_job and self.prep_job.get("running"):
            if self.prep_job.get("sweep"):           # 后台的全书角色配音准备：给用户要起的任务让路（当前这次模型调用做完就停）
                self.prep_job["stop"] = True
                return "stopping background character-voice prep (may take a few minutes)"
            return "pre-generating audio and translations"
        return busy.other_jobs()


def create_app(cfg: Config) -> FastAPI:
    st = App(cfg)
    st.scanner.words = None   # 扫描时再加载词表（启动更快）
    app = FastAPI(title="Lectern")
    app.middleware("http")(local_only(cfg))
    app.state.lib = st
    app.mount("/static", VersionedStatic(directory=STATIC), name="static")
    from .shelf import register as register_shelf   # 书架：阅读进度
    register_shelf(app, st)
    from .readtime import register as register_readtime   # 阅读时长：上报、统计页
    register_readtime(app, st)
    from .research import register as register_research   # 资料：研究笔记与调研清单（Markdown），可送进阅读器
    register_research(app, st)
    from .reader import register as register_reader   # 书页、阅读页、正文与进度
    register_reader(app, st)
    from .narrate import register as register_narrate   # 译文、朗读、预生成
    register_narrate(app, st)
    from .guide import register as register_guide   # 章节导读
    register_guide(app, st)
    from .cast import register as register_cast   # 人物关系图
    register_cast(app, st)
    from .concepts import register as register_map   # 右栏导图：人物关系或概念结构（按书的读法）
    register_map(app, st)
    from .mini import register as register_mini   # 投到 Mac mini（反向隧道 + mini 的 Chrome）
    register_mini(app, st)
    from .overlay import register as register_overlay   # 视频图层（讲堂视频叠在成片上的朗读页）
    register_overlay(app, st)
    app.add_middleware(gzip_except_media)
    return app


async def run_in_thread(fn):
    import anyio
    return await anyio.to_thread.run_sync(fn)
