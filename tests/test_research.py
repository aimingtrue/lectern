"""资料（library/research.py）：浏览、链接改写、只给资料目录里的文件、送进阅读器；Markdown 解析成段落（library/md.py）。"""
from __future__ import annotations

import json

import pytest

DOC = """# 一辆车赚多少钱

**一句话答案**：只有四家赚钱 ⚠。

| 文件 | 内容 |
|---|---|
| [01-evidence.md](01-evidence.md) | 一手数字 |

## 每辆车的账

| 公司 | 卖价 | 落下 |
|---|---|---|
| 特斯拉 | 30.00 | +1.05 |
| 蔚来 | 26.99 | −0.32 |

![单车经营利润](charts/01.svg)

- 第一条
  - 子条目
- 第二条

```mermaid
graph LR
  A --> B
```

```python
print("不读代码")
```

### 细节

见 [规范](../CONVENTIONS.md#来源分级) 和 [外链](https://example.com)。
"""


@pytest.fixture()
def research(tmp_path, monkeypatch):
    data = tmp_path / "data"
    data.mkdir()
    root = tmp_path / "research"
    (root / "topic" / "charts").mkdir(parents=True)
    (root / "topic" / "README.md").write_text(DOC, encoding="utf-8")
    (root / "topic" / "01-evidence.md").write_text("# 证据\n\n原文。\n", encoding="utf-8")
    (root / "topic" / "charts" / "01.svg").write_text('<svg xmlns="http://www.w3.org/2000/svg"/>', encoding="utf-8")
    (root / "topic" / "make.py").write_text("x = '<b>'\n", encoding="utf-8")
    (root / "topic" / "page.html").write_text("<script>alert(1)</script>", encoding="utf-8")
    (root / "CONVENTIONS.md").write_text("# 规范\n\n## 来源分级\n\n一手。\n", encoding="utf-8")
    (root / "README.md").write_text("# 资料\n\n总览。\n", encoding="utf-8")
    (root / ".git").mkdir()
    (root / ".git" / "config").write_text("secret", encoding="utf-8")
    (tmp_path / "outside.md").write_text("# 外面\n\n不给看。\n", encoding="utf-8")
    (data / "config.json").write_text(json.dumps({"research": str(root)}), encoding="utf-8")
    monkeypatch.setenv("LIBRARY_DATA", str(data))
    return tmp_path


@pytest.fixture()
def client(research):
    from fastapi.testclient import TestClient
    from library.config import load
    from library.web import create_app
    return TestClient(create_app(load()))


def test_parse_md(research):
    from library.md import parse_md
    p = parse_md(research / "research" / "topic" / "README.md")
    texts = [x["text"] for x in p.paras]
    assert p.title == "一辆车赚多少钱" and p.lang == "other" and p.parser_ver == 1
    assert "一辆车赚多少钱" not in texts, "唯一的一级标题是书名，不当正文"
    assert texts[0] == "一句话答案：只有四家赚钱 ⚠。"
    assert "01-evidence.md — 一手数字" in texts, "两列的表：「名字 — 说明」"
    assert "特斯拉 — 卖价 30.00，落下 +1.05" in texts
    assert "图：单车经营利润" in texts
    assert "第一条" in texts and "子条目" in texts and "第二条" in texts
    assert not any("graph LR" in t or "print(" in t for t in texts), "代码块、mermaid 不读"
    heads = [(x["kind"], x["text"]) for x in p.paras if x["kind"] == "h"]
    assert heads == [("h", "每辆车的账"), ("h", "细节")]
    assert [(lv, t, n) for lv, t, _, n in p.toc] == [(1, "每辆车的账", texts.index("每辆车的账")),
                                                     (2, "细节", texts.index("细节"))]
    assert texts[-1] == "见 规范 和 外链。"


def test_long_table_is_cut(tmp_path):
    from library import md
    rows = "\n".join(f"| 第{i}行 | {i} |" for i in range(md.TABLE_ROWS + 5))
    f = tmp_path / "t.md"
    f.write_text(f"# 表\n\n说明。\n\n| 名字 | 值 |\n|---|---|\n{rows}\n", encoding="utf-8")
    texts = [x["text"] for x in md.parse_md(f).paras]
    assert f"第{md.TABLE_ROWS - 1}行 — {md.TABLE_ROWS - 1}" in texts and f"第{md.TABLE_ROWS}行 — {md.TABLE_ROWS}" not in texts
    assert texts[-1] == "（表格还有 5 行，见资料页。）"


def test_parse_md_without_body_fails(tmp_path):
    from library.md import parse_md
    f = tmp_path / "empty.md"
    f.write_text("# 只有标题\n\n```\ncode\n```\n", encoding="utf-8")
    with pytest.raises(RuntimeError):
        parse_md(f)


def test_pages_and_links(client):
    r = client.get("/research")
    assert r.status_code == 200 and "Other notes" in r.text and "一辆车赚多少钱" in r.text and 'class="on"' in r.text
    assert "只有四家赚钱" in r.text and "About these notes" in r.text                  # 卡片摘要是一句话答案；根目录 README 收在最后
    r = client.get("/research/topic/README.md")
    assert r.status_code == 200
    h = r.text
    assert 'href="/research/topic/01-evidence.md"' in h
    assert 'src="/research/file/topic/charts/01.svg"' in h
    assert 'href="/research/CONVENTIONS.md#来源分级"' in h
    assert 'href="https://example.com"' in h
    assert '<pre class="mermaid">' in h and "vendor/mermaid.min.js" in h
    assert 'id="read"' in h and "Send to reader" in h and "On this page" in h
    d = client.get("/research/topic")
    assert d.status_code == 200 and "In this folder" in d.text and "make.py" in d.text
    c = client.get("/research/topic/make.py")
    assert c.status_code == 200 and "x = &#x27;&lt;b&gt;&#x27;" in c.text
    img = client.get("/research/topic/charts/01.svg", follow_redirects=False)
    assert img.status_code == 307 and img.headers["location"] == "/research/file/topic/charts/01.svg"
    f = client.get("/research/file/topic/charts/01.svg")
    assert f.status_code == 200 and f.headers["content-type"].startswith("image/svg+xml")
    assert client.get("/research/file/topic/page.html").headers["content-type"].startswith("text/plain")


def test_only_research_files(client):
    for u in ("/research/.git/config", "/research/file/.git/config", "/research/topic/../../outside.md",
              "/research/file/%2E%2E/outside.md", "/research/nope.md"):
        assert client.get(u).status_code == 404, u


def test_send_to_reader(client):
    r = client.post("/api/research/read", json={"path": "topic/README.md"})
    assert r.status_code == 200, r.text
    bid = r.json()["book"]
    assert client.get(f"/read/{bid}").status_code == 200
    book = client.app.state.lib.db.book(bid)
    assert book["title"] == "一辆车赚多少钱" and book["status"] == "ok" and book["n_paras"] > 5
    j = client.get(f"/api/books/{bid}/text?from=0&count=3").json()
    assert "只有四家赚钱" in json.dumps(j, ensure_ascii=False)
    again = client.post("/api/research/read", json={"path": "topic/README.md"})
    assert again.json()["book"] == bid, "没改过的文档不重新入库"


def test_send_to_reader_rejects(client):
    assert client.post("/api/research/read", json={"path": "topic/make.py"}).status_code == 400
    assert client.post("/api/research/read", json={"path": "../outside.md"}).status_code == 404
    r = client.post("/api/research/read", json={"path": "topic/README.md"}, headers={"origin": "http://evil.example"})
    assert r.status_code == 403


def test_toc_not_double_escaped(research, client):
    (research / "research" / "topic" / "amp.md").write_text("# 篇\n\n## A & B\n\n正文。\n\n## C\n\n正文。\n", encoding="utf-8")
    h = client.get("/research/topic/amp.md").text
    assert "On this page" in h and ">A &amp; B</a>" in h and "&amp;amp;" not in h


def test_titles_and_labels():
    from library.research import clean, label, summary
    assert clean("earnings — 财报拆解") == "财报拆解" and clean("research-notes") == "research-notes"
    assert clean("Acme 2026 Q3 财报 — 导读") == "Acme 2026 Q3 财报"
    assert clean("SpaceX 2026 Q3 — 一手数据与原文") == "SpaceX 2026 Q3 — 一手数据与原文", "不是目录键的前缀不去"
    assert label("证据：中国房价现状（截至 2026-10-23）") == "证据"
    assert label("Acme 2026 Q3 — 解读") == "解读" and label("x", readme=True) == "Overview"
    assert label("高质量资料仓库清单") == "高质量资料仓库清单"
    s = summary("# 题\n\n**问题**：问什么。\n\n**一句话答案**：[美股](x.md)又稳又高。\n\n**把握度**：中。证据……\n\n*核验日期：2026-10-24。*\n")
    assert s == {"text": "美股又稳又高。", "sure": "中", "checked": "2026-10-24"}


def test_home_groups_by_section(tmp_path, monkeypatch):
    """资料集 → 板块 → 专题卡片；不露目录名、文件名；没有 README 的中间目录摊平；面包屑用中文标题。"""
    data, root = tmp_path / "data", tmp_path / "research"
    data.mkdir()
    def w(rel, text):
        f = root / rel
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(text, encoding="utf-8")
    w("README.md", "# 资料\n\n总览。\n")
    w("notes/README.md", "# 研究笔记\n\n一手资料驱动的研究库。\n")
    w("notes/CONVENTIONS.md", "# 写作与引用规范\n\n规则。\n")
    w("notes/topics/README.md", "# topics — 问题驱动的调研\n\n一个目录一个问题。\n")
    for slug, q, day in (("old-one", "旧问题？", "2026-10-01"), ("new-one", "新问题？", "2026-10-20")):
        w(f"notes/topics/{slug}/README.md", f"# {q}\n\n**一句话答案**：{q}的答案。\n\n**把握度**：高。\n\n核验日期：{day}\n")
        w(f"notes/topics/{slug}/01-evidence.md", "# 证据：一手数字\n\n数字。\n")
        w(f"notes/topics/{slug}/02-analysis.md", "# 分析：怎么想\n\n推理。\n")
    w("notes/earnings/README.md", "# earnings — 财报拆解\n\n一家公司一个目录。\n")
    w("notes/earnings/ACME/README.md", "# Acme (ACME) 财报跟踪\n\n跟踪。\n")
    w("notes/earnings/ACME/2026-Q3/README.md", "# Acme 2026 Q3 财报 — 导读\n\n导读。\n")
    w("notes/earnings/ACME/2026-Q3/03-analysis.md", "# Acme 2026 Q3 — 解读\n\n解读。\n")
    w("notes/earnings/WIDGET/trends-3y/README.md", "# Widget 公司三年趋势\n\n趋势。\n")
    w("notes/earnings/WIDGET/trends-3y/01-data.md", "# 三年数据底稿：2023 — 2025\n\n数据。\n")
    (data / "config.json").write_text(json.dumps({"research": str(root)}), encoding="utf-8")
    monkeypatch.setenv("LIBRARY_DATA", str(data))
    from fastapi.testclient import TestClient
    from library.config import load
    from library.web import create_app
    c = TestClient(create_app(load()))
    h = c.get("/research").text
    assert "研究笔记" in h and "问题驱动的调研" in h and "财报拆解" in h and "Acme (ACME) 财报跟踪" in h
    assert "topics —" not in h and "earnings —" not in h and ">notes/topics<" not in h
    assert h.index("新问题？") < h.index("旧问题？"), "新核验的专题在前"
    assert "新问题？的答案。" in h and "confidence 高" in h and "checked 2026-10-20" in h
    assert ">证据<" in h and ">分析<" in h and ">Overview<" in h and ">01-evidence<" not in h      # 地址里有文件名，显示的不是
    assert "Widget 公司三年趋势" in h and ">WIDGET<" not in h, "没有 README 的 WIDGET/ 摊平到财报拆解下"
    assert h.index("写作与引用规范") > h.index("Acme 2026 Q3 财报"), "零散文档收进最后的「更多」"
    t = c.get("/research/notes/topics/new-one/").text
    assert "研究笔记</a><i>/</i><a" in t and "问题驱动的调研</a>" in t and ">notes<" not in t and ">topics<" not in t
    assert "In this folder" in t and "<b>证据</b>" in t
    g = c.get("/research/notes/earnings/ACME/").text
    assert "Acme 2026 Q3 财报" in g and "rcard" in g
