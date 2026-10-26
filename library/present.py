"""书城和书目层共用的显示规则：书名、期号、链接、简介的格式化，套装书名，奖项中文名，简介和体裁的来源顺序。
不依赖网页框架，数据模块（搜索、相似书、中文信息）也可以用（原先在 library/store.py 里）。
"""
from __future__ import annotations

import datetime as dt
import html
import re
from urllib.parse import urlencode

SETTY = re.compile(r"全集|合集|丛书|作品集|套装|\d+册|collection|译丛|三部曲|series", re.I)
BUNDLE = re.compile(SETTY.pattern + r"|bundle|box(?:ed)? set|\d+[- ]book", re.I)


def fmt_issue(issue: str, label: str = "") -> str:
    s = issue or ""
    try:
        if len(s) == 10:
            d = dt.date.fromisoformat(s)
            s = f"{d.year}年{d.month}月{d.day}日"
        elif len(s) == 7:
            s = f"{s[:4]}年{int(s[5:])}月"
        elif len(s) == 4:
            s = f"{s}年"
    except ValueError:
        pass
    label = re.sub(r"\b(?:the\s+)?magazine\b", "", label or "", flags=re.I).strip(" -·")
    return f"{s} {label}".strip()


def link(base: str, **kw) -> str:
    q = urlencode({k: v for k, v in kw.items() if v not in ("", None, 0)})
    return base + ("?" + q if q else "")


def short(t: str) -> str:
    """卡片上的书名：去掉电子书店塞进书名的推广语（括号里一长串）。详情页仍显示完整书名。"""
    s = re.sub(r"\s*[（(【][^（）()【】]{11,}[）)】]", "", t or "").strip()
    return s or (t or "")


DESC_SRC = {"douban": "豆瓣", "openlibrary": "Open Library", "googlebooks": "Google Books", "applebooks": "Apple Books"}   # 简介按这个顺序取，署名用这里的名字
GENRE_SRC = ("wikidata", "googlebooks")

# 常见奖项的中文名（Wikidata 的中文标签有错：雨果奖最佳长篇被标成「雨果最佳复古小说」，日本星云赏被标成美国的「星云奖」）
AWARD_ZH = {
    "Hugo Award for Best Novel": "雨果奖最佳长篇小说", "Nebula Award for Best Novel": "星云奖最佳长篇小说",
    "Locus Award for Best Science Fiction Novel": "轨迹奖最佳科幻小说", "Locus Award for Best Fantasy Novel": "轨迹奖最佳奇幻小说",
    "Locus Award for Best First Novel": "轨迹奖最佳处女作", "Locus Award for Best Collection": "轨迹奖最佳短篇集",
    "Seiun Award for Best Translated Long Work": "日本星云赏最佳翻译长篇", "Arthur C. Clarke Award": "亚瑟·克拉克奖",
    "World Fantasy Award for Best Novel": "世界奇幻奖最佳长篇小说", "BSFA Award for Best Novel": "英国科幻协会奖最佳长篇小说",
    "National Book Award for Fiction": "美国国家图书奖（小说）", "Pulitzer Prize for Fiction": "普利策小说奖",
    "Booker Prize": "布克奖", "International Booker Prize": "国际布克奖", "Prometheus Award": "普罗米修斯奖",
    "Bram Stoker Award for Best Novel": "布拉姆·斯托克奖最佳长篇小说", "Bram Stoker Award for Best First Novel": "布拉姆·斯托克奖最佳处女作",
    "Nobel Prize in Literature": "诺贝尔文学奖", "Mao Dun Literature Prize": "茅盾文学奖", "Lu Xun Literary Prize": "鲁迅文学奖",
    "Chinese Literature Media Award": "华语文学传媒大奖", "Lao She Literature Award": "老舍文学奖",
    # Wikidata 没有中文名的（书城上原先直接显示英文）
    "ALA Notable Books for Adults": "美国图书馆协会成人好书", "Alex Award": "亚历克斯奖",
    "Athenaeum Literary Award": "雅典娜文学奖", "Audie Award for Audiobook of the Year": "奥迪奖年度有声书",
    "Audie Awards": "奥迪有声书奖", "August Derleth Award": "奥古斯特·德莱斯奖",
    "August Derleth Award for Best Horror Novel": "奥古斯特·德莱斯奖最佳恐怖小说",
    "BSFA Award for Best Short Fiction": "英国科幻协会奖最佳短篇小说", "Bancarella Literary Prize": "班卡瑞拉文学奖",
    "Barry Award for Best Novel": "巴里奖最佳长篇小说", "Believer Book Award": "《信徒》图书奖",
    "Bob Morane award for best foreign novel": "鲍勃·莫拉内奖最佳外国长篇小说",
    "Bram Stoker Award for Best Fiction Collection": "布拉姆·斯托克奖最佳小说集",
    "Bram Stoker Award for Best Long Fiction": "布拉姆·斯托克奖最佳中篇小说",
    "Bram Stoker Award for Best Work for Young Readers": "布拉姆·斯托克奖最佳青少年作品",
    "Bram Stoker Award for Novel": "布拉姆·斯托克奖最佳长篇小说", "British Fantasy Award for Best Anthology": "英国奇幻奖最佳选集",
    "British Fantasy Award for Best Collection": "英国奇幻奖最佳短篇集", "Chicago Tribune Heartland Prize": "《芝加哥论坛报》心脏地带奖",
    "Compton Crook Award": "康普顿·克鲁克奖", "David Gemmell Legend Awards": "大卫·盖梅尔传奇奖",
    "Dragon Award for Best Fantasy Novel": "龙奖最佳奇幻小说", "Dragon Award for Best Science Fiction Novel": "龙奖最佳科幻小说",
    "Edgar Allan Poe Award for Best Novel": "爱伦·坡奖最佳长篇小说",
    "Goodreads Choice Award for Best Chick Lit": "Goodreads 读者选择奖最佳都市女性小说",
    "Goodreads Choice Award for Best Fantasy": "Goodreads 读者选择奖最佳奇幻",
    "Goodreads Choice Award for Best Fiction": "Goodreads 读者选择奖最佳小说",
    "Goodreads Choice Award for Best Young Adult Series": "Goodreads 读者选择奖最佳青少年系列",
    "Grand Prix de Littérature Policière": "法国侦探文学大奖", "Grand Prix de l'Imaginaire": "法国幻想文学大奖",
    "Grand Prix de l'Imaginaire for Best Foreign Novel": "法国幻想文学大奖最佳外国长篇小说",
    "Grand Prix de l'Imaginaire for Best Foreign-Language Short Story": "法国幻想文学大奖最佳外语短篇小说",
    "Hammett Prize": "哈米特奖", "Huxiang Excellent Publication Award": "湖湘优秀出版物奖",
    "Imaginales award for best foreign-language novel": "幻想节奖最佳外语长篇小说",
    "International Fantasy Award for Best Fiction": "国际幻想奖最佳小说", "LiveLib Readers' Choice": "LiveLib 读者选择奖",
    "Locus Award for Best Anthology": "轨迹奖最佳选集", "Locus Award for Best Young Adult Book": "轨迹奖最佳青少年图书",
    "Michigan Notable Books": "密歇根年度好书", "Mythopoeic Fantasy Award": "神话创造奇幻奖",
    "Mythopoeic Fantasy Award for Adult Literature": "神话创造奇幻奖（成人文学）",
    "Mythopoeic Fantasy Award for Children's Literature": "神话创造奇幻奖（儿童文学）",
    "National Book Critics Circle Award in Biography": "美国国家书评人协会奖（传记）",
    "National Book Critics Circle Award in Nonfiction": "美国国家书评人协会奖（非虚构）", "Oregon Book Awards": "俄勒冈图书奖",
    "Pacific Northwest Booksellers Association Award": "太平洋西北书商协会奖",
    "Premio Arcebispo Xoán de San Clemente de novela estranxeira": "圣克莱门特大主教奖（外国小说）",
    "Premio Gigamesh": "吉加梅什奖", "Prix Médicis essai": "美第奇奖（随笔）", "Prometheus Award - Hall of Fame": "普罗米修斯奖名人堂",
    "Pulitzer Prize for the Novel": "普利策小说奖", "Reese's Book Club": "瑞茜读书会推荐", "Retro Hugo Award for Best Novel": "追授雨果奖最佳长篇小说",
    "Robert Holdstock Award for Best Fantasy Novel": "罗伯特·霍尔德斯托克奖最佳奇幻小说",
    "Society of American Historians Prize for Historical Fiction": "美国历史学家协会历史小说奖",
    "The Best of the Booker": "布克奖中的布克奖", "The Hillman Prize for Book Journalism": "希尔曼新闻图书奖",
    "Tähtivaeltaja Award": "芬兰星际漫游者奖", "Vance Palmer Prize for Fiction": "万斯·帕尔默小说奖", "Whitney Awards": "惠特尼奖",
    "World Fantasy Award for Best Anthology": "世界奇幻奖最佳选集", "World Fantasy Award for Best Collection": "世界奇幻奖最佳短篇集",
}


def desc_paras(text: str) -> list[str]:
    """来源给的简介原文 → 段落：去掉 HTML 标签（Google Books）、markdown 链接和脚注（Open Library）；不改写内容。"""
    t = re.sub(r"(?i)<br\s*/?>|</p\s*>|<p(?:\s[^>]*)?>", "\n", text or "")
    t = html.unescape(re.sub(r"<[^>]+>", "", t))
    t = re.split(r"\n-{3,}|\n\*\*\*|\n\[\d+\]:", t)[0]
    t = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", t)
    t = re.sub(r"\(\[source\]\[\d+\]\)|\[\d+\]", "", t)
    return [x.strip() for x in t.split("\n") if x.strip()]
