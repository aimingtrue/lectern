"""书城编目：只读元数据、不解析正文，几万个文件几分钟编完。

  python -m library.catalog [文件夹 ...]   编目（不写参数则用「管理」里保存的文件夹），结束后打印统计

数据在 data/catalog.db（与 library.db 分开）：
  files    每个文件一行：格式、指纹（与 library 的 book_id 相同）、书名作者、杂志刊名/期号、来源目录标签
  works    「作品」：同一本书的 epub/mobi/pdf、不同目录里的同一个文件、两个杂志目录里的同一期，归成一个；
           共用 ISBN 或书名作者相同、但书名对不上的（卷册数字不同、一个词都不沾、一中一英）不归，见 same_book
  shelves  货架（榜单/书单/系列/套装）→ 作品，带榜单序号

同一作品多个格式时按功能挑来源，只把一份送去全文解析：
  文字来源（朗读、译文、查词）epub > 有文字层的 pdf > mobi/azw > txt；azw3（KF8）PyMuPDF 只读得出封面，不能当文字来源
  版式来源（看原版页面）pdf
其余格式只作「其他版本」列出，统计成可回收空间，不删任何文件。
"""
from __future__ import annotations

import functools
import html
import json
import logging
import os
import re
import posixpath
import sqlite3
import struct
import sys
import threading
import time
import unicodedata
import zipfile
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import unquote
from xml.etree import ElementTree as ET

from .extract import book_id
from .schemas import catalog as catalog_schema
from . import threadconn

log = logging.getLogger(__name__)
CATALOG_VER = 10         # 编目解析版本：2 起语言按正文判断；4 全书均匀取样、汉字按 2.5 个字母计；6 txt 取中部、mobi 解压取正文样本；7 epub 按全书、mobi 取 30 条并跳过坏记录；8 中英对照按语言标记和目录、txt 均匀取 4 段；9 日韩俄与拉丁字母各语种按正文分、中文译本的依据（trans）；10 拆开 Anna's Archive 文件名（书名 -- 作者 -- … -- ISBN）

EXTS = {".pdf", ".epub", ".mobi", ".azw", ".azw3", ".txt", ".fb2", ".djvu", ".chm", ".doc", ".docx"}
TEXT_RANK = {"epub": 0, "pdf": 1, "mobi": 2, "azw": 2, "fb2": 2, "txt": 3}     # 越小越适合当文字来源
FMT_ORDER = ["epub", "pdf", "mobi", "azw3", "azw", "fb2", "djvu", "txt", "chm", "doc", "docx"]

# 顶层目录 → 类别；其余都是书。「日报」目录下的归为报纸
KIND_DIRS = {"杂志": "magazine", "杂志-部分": "magazine", "儿童": "kids", "Lady bug 2024": "kids"}


# ---------------------------------------------------------------- 文件名清洗
CJK = re.compile(r"[㐀-鿿豈-﫿]")
AD = re.compile(r"威信|微信|客服|淘宝|店铺|vip|全网|更多|QQ|公众号|加v|免费分享", re.I)
NOISE = [
    re.compile(r"【[^】]*】"),                                            # 【淘宝店铺：…】【多多店铺：…】
    re.compile(r"\[[^\]]*\.(?:com|net|cn|org)\]", re.I),                 # [ibooks521.com]
    re.compile(r"\s*\((?:z-lib(?:rary|\.org)?|libgen(?:\.\w+)?|ebook|epub|pdf|mobi)\)", re.I),
    re.compile(r"\s*\(\d\)$"),                                           # 副本 (1)
    re.compile(r"[（(][^（()）]*(?:公众号|淘宝|店铺|微信|QQ)[^（()）]*[）)]", re.I),   # （公众号：书单严选）
    re.compile(r"获取(?:免费|更多)?电子书.*$"),
    re.compile(r"\s*\([^()]*(?:z-lib|1lib|libgen)[^()]*\)", re.I),        # (z-library.sk, 1lib.sk, z-lib.sk)
    re.compile(r"^[^【】]*(?:公众号|淘宝|店铺|微信)[^【】]*】"),              # 公众号：我爱读书鸭】圣母（缺了前半个括号）
    re.compile(r"\s+(?:公众号|微信|淘宝店铺|店铺|QQ群?)[:：]\S*$", re.I),    # 动物农场 公众号：精品书籍推荐分享
]
YEAR_ONLY = re.compile(r"^(?:19|20)\d{2}(?:年)?$")
LEAD_NUM = re.compile(r"^(?:\d{1,3}\s*[.\-、_]\s*|\d{1,3}\s+(?=\D)|\d{3}(?=[㐀-鿿]))")
SKIP_SEG = re.compile(r"^(?:ebook|epub|mobi|pdf|azw3|txt|分文件夹\d*|新建文件夹.*|\d+)$", re.I)
SHELF_RE = re.compile(r"榜|top|畅销|最佳|best|goodreads|亚马逊|amazon|豆瓣|纽约时报|new york times|modern library|"
                      r"推荐|经典|高分|书单|系列|套装|合集|全集|丛书|作品集|名著|获奖|奖", re.I)


DECOR = re.compile(r"[❥❤♥★☆✿❀]+")
REDO = re.compile(r"(?:\.(?:fixed|flip))+$")                            # 质量关卡重转留下的 .fixed.fixed
AD_TAG = re.compile(r"【[^】]*(?:" + AD.pattern + r"|\.(?:com|net|cn|org))[^】]*】", re.I)   # 只有带广告的【…】


def strip_ads(s: str, tags: bool = True) -> str:
    """去掉广告、来源站、副本号这类不属于书名的部分；不改下划线。
    tags=False（文件改名，library/ingest.py）：【儿子与情人】【日】这类书名标注留着，只去带广告的【…】。"""
    for r in NOISE if tags else [AD_TAG, *NOISE[1:]]:
        s = r.sub(" ", s)
    s = DECOR.sub("", REDO.sub("", s.strip()))
    if " " not in s.strip() and not CJK.search(s) and s.count("+") >= 2:       # More+Than+You+Know（网址编码的空格）
        s = re.sub(r"(?<!\+)\+(?!\+)", " ", s)
    # 「…-得道混顿大学…加威信xx」：带广告的分段整段去掉
    parts = re.split(r"(?<=\S)-(?=\S)|\s+-\s+", s)
    if len(parts) > 1 and AD.search(parts[-1]):
        s = s[: s.rfind(parts[-1])].rstrip(" -")
    return re.sub(r"\s+", " ", s).strip(" -.·")


ENTITY = re.compile(r"&(?:#\d+|#[xX][0-9a-fA-F]+|[A-Za-z][A-Za-z0-9]*);")


def unent(s: str) -> str:
    """HTML 实体解码：文件名里的「&amp; his Air Glider」「Don&#39;t」、epub/mobi 元数据里的「马克&#183;唐纳利」「Halley&#8217;s」。
    只认带分号的实体（AT&T、R&D 不动）；「&amp;#183;」这种转了两次的也解开。"""
    for _ in range(3):
        if not s or "&" not in s:
            break
        t = ENTITY.sub(lambda m: html.unescape(m.group()), s)
        if t == s:
            break
        s = t
    return s


def clean(s: str) -> str:
    s = strip_ads(s).replace("_ ", ": ").replace("_", " ")
    return re.sub(r"\s+", " ", s).strip(" -.·")


def strip_num(s: str) -> tuple[str, int | None]:
    """'01.The Boys' → ('The Boys', 1)；'1984'、'4th of July' 不动。"""
    m = LEAD_NUM.match(s)
    if not m or not s[m.end():].strip():
        return s, None
    n = re.match(r"\d+", s)
    return s[m.end():].strip(), int(n.group()) if n else None


def namey(s: str) -> bool:
    """像人名：1–6 个词、首字母大写或带点，没有数字；或带 [国] / · 的中文译名。"""
    s = s.strip()
    if not s or len(s) > 60 or re.search(r"\d", s):
        return False
    if CJK.search(s):
        return bool(re.search(r"[\[［（(【][^\]］）)】]{1,4}[\]］）)】]|[·•]|著$|译$", s)) or len(s) <= 6
    words = re.split(r"[\s,;&]+", s)
    return 1 <= len(words) <= 6 and all(w[:1].isupper() or w.lower() in ("de", "van", "von", "le", "la", "and", "jr.", "du", "da", "di", "del", "della",
                                                                         "der", "den", "dos", "das", "ter", "ten", "bin", "al")
                                        or w.endswith(".") for w in words if w)


# 文档软件、扫描软件写进元数据的占位作者（Administrator、Windows User、微软用户、CamScanner），打包人的账号、网站、邮箱；
# Anonymous 不算（Beowulf、The Arabian Nights 本来就署这个）
JUNK_AUTHOR = re.compile(r"^(?:administrators?|admin|(?:ms |lenovo |windows |microsoft )?users?|owner|windows\s*用户|微软用户|"
                         r"未知(?:作者)?|unknown|camscanner|link|golden|kira|cyj|xb21cn|山川的书屋|"
                         r"\S+@\S+|(?:www\.)?[\w\-]+(?:\.[\w\-]+)*\.(?:com|net|cn|org|ir))$", re.I)
WEBSITE_PREFIX = re.compile(r"^(?:www\.)?[\w\-]+\.(?:com|net|cn|org)\s+(?=\S)", re.I)    # 「ePUBw.COM 马良」


def junk_author(a: str) -> bool:
    """占位作者：「Windows 用户 [Windows 用户]」这种后面带括号的也算；「JPG转PDF转换器」按 GBK 读错的乱码（半角片假名、韩文区）也算。"""
    a = re.sub(r"\s*[\[［(（][^\]］)）]*[\]］)）]\s*$", "", (a or "").strip()) or (a or "").strip()
    return bool(JUNK_AUTHOR.match(a) or re.search(r"[ﾠ-￯]", a))


def clean_author(a: str) -> str:
    """去掉占位作者：整个是占位的返回空；作者列表里的「ePUBw.COM」这一项、开头的网站名去掉，其余照旧。"""
    a = WEBSITE_PREFIX.sub("", (a or "").strip())
    if junk_author(a):
        return ""
    parts = [p for p in re.split(r"\s*[,;，；]\s*", a) if p]
    keep = [p for p in parts if not junk_author(p)]
    if len(keep) == len(parts):
        return a
    sep = re.search(r"[,;，；]\s*", a).group()
    return sep.join(keep)


def fix_author(a: str) -> str:
    a = re.sub(r"\s+", " ", a).strip(" ,;")
    m = re.match(r"^([A-Z][\w'’-]+)[_,:]\s*([A-Z][\w .'’-]+)$", a)     # 'King_ Stephen' / 'King, Stephen'
    return f"{m.group(2).strip()} {m.group(1)}" if m else a


# ---------------------------------------------------------------- 繁转简
_T2S = None
# 简体里也照用、OpenCC 会转错的字：乾隆/乾纲、文徵明/魏徵、朱川湊人、城塚、朱西甯、赵孟頫、全汉昇、王汎森、凌濛初、雫井脩介、内田百閒、於（姓）
KEEP_TRAD = set("乾徵湊塚甯頫詝穀昇汎濛脩閒於樑璿崑")
ZH_EDITION = re.compile(r"\s*[（(]?(?:简体|繁体|繁體|簡體)(?:中文)?版?[）)]?$")


@functools.lru_cache(maxsize=65536)
def t2s(s: str) -> str:
    """整串繁转简（比较用的键：两边都转，转错的字两边一样错）。"""
    global _T2S
    if not s or not CJK.search(s):
        return s or ""
    if _T2S is None:
        from opencc import OpenCC
        _T2S = OpenCC("t2s")
    return _T2S.convert(s)


def simp_title(t: str) -> str:
    """中文书名转成简体（显示用）：KEEP_TRAD 里的字和转出来不在基本区的字（頫→𫖯）不转；去掉末尾的「简体」「(繁體)」
    版本说明（前面得是汉字或书名号：「Pro Git - 简体中文版」不动）。外文书名里夹的中文（Shui Hu Zhuan 水滸傳）不动。"""
    if not t or not CJK.search(t) or len(CJK.findall(t)) < len(re.findall(r"[A-Za-z]", t)):
        return t
    s = t2s(t)
    if len(s) == len(t):
        s = "".join(a if a in KEEP_TRAD or ord(b) > 0xFFFF else b for a, b in zip(t, s))
    elif any(c in KEEP_TRAD for c in t):
        s = t
    m = ZH_EDITION.search(s)
    if m and m.start() > 0 and re.match(r"[㐀-鿿豈-﫿》」）)]", s[m.start() - 1]):
        s = s[: m.start()]
    return s


def traditional(f: dict) -> bool:
    """这份文件是繁体版（看元数据书名，没有就看文件名）：同一作品同一格式的版本里，简体版优先当文字来源、去重时留下。"""
    s = f.get("meta_title") or Path(f.get("path") or "").stem
    if not CJK.search(s):
        return False
    s2 = t2s(s)
    return len(s2) == len(s) and any(a != b and a not in KEEP_TRAD for a, b in zip(s, s2))


# Anna's Archive 的文件名：「书名 -- 作者 -- 版次、丛书, 年份 -- 出版社 -- ISBN -- md5 -- Anna’s Archive」，
# 作者常写成「姓, 名」、多人用「;」分开，名字里的点在文件名里成了「_」（clean 之后是「:」）
AA_SEP = " -- "
AA_TAIL = re.compile(r"^(?:[0-9a-f]{32}|anna['’]?s archive|z-library|libgen(?:\.\w+)?|10[ .]\d{4,}\s+\S+|"
                     r"(?:97[89]\d{10}|\d{9}[\dXx])e?)$", re.I)
AA_ISBN = re.compile(r"^(97[89]\d{10}|\d{9}[\dXx])e?$")
AA_ROLE = re.compile(r"\s*\((?:auth|eds?|ed|editor|editors|author|trans|translator)\s*\.?\s*\)|,?\s*\b(?:author|editor|translator)\b\.?$"
                     r"|\(Author\)", re.I)


def _isbn_ok(s: str) -> bool:
    s = s.upper()
    if len(s) == 13:
        return sum(int(c) * (1 if i % 2 == 0 else 3) for i, c in enumerate(s)) % 10 == 0
    return len(s) == 10 and sum((10 if c == "X" else int(c)) * (10 - i) for i, c in enumerate(s)) % 11 == 0


def _aa_person(p: str) -> str:
    """「Atwood, Margaret, 1939-」→ Margaret Atwood；「Barbara G: Tabachnick」→ Barbara G. Tabachnick。"""
    p = AA_ROLE.sub("", p).strip(" ,")
    p = re.sub(r",?\s*\(?\d{4}\s*-\s*(?:\d{4})?\)?\s*$", "", p).strip(" ,")       # 生卒年
    p = re.sub(r"\b(Dr|Mr|Mrs|Ms|Prof|Jr|Sr|St|[A-Z]):(?=\s|$)", r"\1.", p)            # 文件名里点成了「_」
    bits = [b.strip() for b in p.split(",") if b.strip()]
    if len(bits) >= 2 and len(bits[0].split()) == 1 and not re.search(r"\d", bits[1]):
        p = f"{bits[1]} {bits[0]}"                                                  # 姓, 名（后面的别的说明丢掉）
    return re.sub(r"\s+", " ", p).strip(" ,:")


def aa_parts(stem: str) -> list[str] | None:
    """Anna's Archive 式文件名切成段（去掉末尾的 md5、站名、DOI 和 ISBN）；不是这种文件名返回 None。"""
    s = clean(stem)
    if AA_SEP not in s:
        return None
    parts = [x.strip() for x in s.split(AA_SEP)]
    while len(parts) > 1 and (AA_TAIL.match(parts[-1]) or not parts[-1]):
        parts.pop()
    return parts if parts[0] else None


def aa_isbn(stem: str) -> str:
    """Anna's Archive 式文件名末尾的 ISBN（校验位要对）；没有返回空串。"""
    s = clean(stem)
    if AA_SEP not in s:
        return ""
    for x in reversed([x.strip() for x in s.split(AA_SEP)][1:]):
        m = AA_ISBN.match(x)
        if m and _isbn_ok(m.group(1)):
            return m.group(1).upper()
    return ""


def aa_book(parts: list[str]) -> tuple[str, str]:
    """(书名, 作者)：第一段是书名，第二段像人名才算作者（多人用「; 」连接，最多 4 位）。"""
    title = parts[0].strip(" :,-")
    if len(parts) < 2 or not re.search(r"[^\W\d_]", parts[1]) or re.fullmatch(r"[\d\s,.;:()]+(?:st|nd|rd|th)?", parts[1]):
        return title, ""
    raw = re.sub(r"^(?:edited|translated|compiled)\s+by\s+", "", parts[1], flags=re.I)
    chunks = [c for c in re.split(r"\s*;\s*|\s+(?:and|&)\s+(?=\S+\s+\S)", raw) if c.strip()]
    people = []
    for c in chunks:
        bits = [b.strip() for b in AA_ROLE.sub("", c).split(",") if b.strip()]
        if len(bits) >= 2 and all(len(b.split()) >= 2 for b in bits):          # Bhabesh Deka, Sumit Datta
            people += [_aa_person(b) for b in bits]
        else:
            people.append(_aa_person(c))
    people = [x for x in dict.fromkeys(people)                  # 「Container of」「Laozi: Dao de jing」是馆藏著录里的别的字段
              if x and not re.search(r"\d|:", x) and not re.match(r"container of\b", x, re.I)]
    return title, "; ".join(people[:4])


# 「杨定一【好睡】」「001杨定一【真 原 醫】」「杨定一【疗愈的饮食与断食】简体版」：作者在前、书名在【】里，后面只有版本说明
NAME_TAG = re.compile(r"^(?:\d{1,3}\s*[.\-、_]?\s*)?([㐀-鿿·•]{2,6})【([^】]+)】\s*"
                      r"((?:简体|繁体|繁體|简|繁|横版|竖版|彩色|黑白|扫描|高清|精校|版|\s)*)$")


def guess_book(stem: str) -> tuple[str, str]:
    """文件名 → (书名, 作者)。"""
    if parts := aa_parts(stem):
        return aa_book(parts)
    m = NAME_TAG.match(stem.strip())
    if m and m.group(2).strip() not in FOREIGN and not AD_TAG.search(f"【{m.group(2)}】"):
        t = clean(m.group(2))
        if re.fullmatch(r"[㐀-鿿](?: [㐀-鿿])+", t):           # 「真 原 醫」：字和字之间的空格
            t = t.replace(" ", "")
        if t:
            return t, m.group(1)
    s = clean(stem)
    s, _ = strip_num(s)
    if CJK.search(s):
        if " - " in s:                                       # 看不见的城市 - [意大利] 伊塔洛·卡尔维诺
            t, a = s.rsplit(" - ", 1)
            if namey(a):
                return t.strip(), a.strip()
        m = re.match(r"^(.*?)\s*[(（]([^()（）]*)[)）]\s*$", s)   # 资治通鉴全本（…） (司马光)
        if m and m.group(2) and namey(m.group(2)) and m.group(1):
            return m.group(1).strip(), m.group(2).strip()
        if ":" in s:                                         # 书名_作者（清洗时 _ 已变成空格或冒号）
            t, a = s.rsplit(":", 1)
            if namey(a):
                return t.strip(" :"), a.strip()
        m = re.match(r"^(.*\S)\s+(\[[^\]]{1,4}\]\s*\S.*)$", s)  # …神经网络 [美] 杰弗瑞•希顿
        if m:
            return m.group(1), m.group(2)
        return s, ""
    m = re.match(r"^(.*\S)\s+by\s+(.+?)(\s*[(（][^()（）]*[)）])?$", s, re.I)    # Title by Author (Series book 3)
    if m and namey(m.group(2)):
        return (m.group(1) + (m.group(3) or "")).strip(), fix_author(m.group(2))
    m = re.match(r"^(.*\S)\s*\(([^()]+)\)$", s)                # Title (Author)：至少两个词，(DISNEY) 不算
    if m and namey(m.group(2)) and len(m.group(2).split()) >= 2:
        return m.group(1), fix_author(m.group(2))
    if " - " in s:
        t, a = s.rsplit(" - ", 1)
        a = fix_author(a)
        if namey(a):
            return t.strip(), a
        t2, a2 = s.split(" - ", 1)                           # Author - Title
        if namey(t2) and not namey(a2):
            return a2.strip(), fix_author(t2)
    return s, ""


# ---------------------------------------------------------------- 杂志期号
MONTHS = {m: i + 1 for i, m in enumerate(
    ["january", "february", "march", "april", "may", "june", "july", "august", "september", "october", "november", "december"])}
MONTHS.update({k[:3]: v for k, v in list(MONTHS.items())})
MONTHS["sept"] = 9
MON = r"(jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|june?|july?|aug(?:ust)?|sept?(?:ember)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)"


@dataclass
class Issue:
    y: int
    m: int = 0
    d: int = 0
    m2: int = 0                 # 双月刊的第二个月
    amb: tuple | None = None    # a.b.YYYY 两个数都 ≤12：(a, b)，按同刊其他期的写法再定日/月顺序
    rest: str = ""              # 去掉日期后剩下的文字（刊名、特刊名）

    def key(self) -> str:
        if self.d:
            return f"{self.y:04d}-{self.m:02d}-{self.d:02d}"
        if self.m:
            return f"{self.y:04d}-{self.m:02d}"
        return f"{self.y:04d}"


def _mon(s: str) -> int:
    s = s.lower()
    return MONTHS[s[:4]] if s[:4] == "sept" else MONTHS[s[:3]]


Y = r"(?P<y>(?:19|20)\d\d)"
ISSUE_PATS = [   # 按从具体到宽松的顺序试；分隔符可有可无（BarronsNovember62023、DailyExpress22November2023）
    rf"(?<!\d){Y}年(?P<m>\d{{1,2}})月(?:(?P<d>\d{{1,2}})日)?",
    rf"(?<!\d){Y}[-._ ]?(?P<m>0[1-9]|1[0-2])[-._ ]?(?P<d>[0-2]\d|3[01])(?!\d)",
    rf"(?<!\d){Y}[-._ ](?P<m>\d{{1,2}})[-._ ](?P<d>\d{{1,2}})(?!\d)",
    rf"(?<!\d)(?P<d>\d{{1,2}})(?:st|nd|rd|th)?[\s-]*(?P<M>{MON})\.?,?[\s-]*{Y}(?!\d)",
    rf"(?P<M>{MON})\.?[\s-]*(?P<d>\d{{1,2}})(?:st|nd|rd|th)?,?[\s-]*{Y}(?!\d)",
    rf"(?P<M>{MON})\.?(?:[\s/&-]*(?:and\s*)?(?P<M2>{MON})\.?)?,?[\s-]*{Y}(?!\d)",
    r"(?<!\d)(?P<a>\d{1,2})[./-](?P<b>\d{1,2})[./-]" + Y + r"(?!\d)",
    r"(?<!\d)(?P<m>\d{1,2})[./-](?P<m2>\d{1,2})\s+" + Y + r"(?!\d)",          # 01.02 2023 双月刊
    r"(?<![\d.])(?P<m>\d{1,2})[./-]" + Y + r"(?!\d)",
    rf"(?<!\d){Y}[-._ ]?(?P<m>0[1-9]|1[0-2])(?:\s*[&/]\s*(?P<m2>\d{{1,2}}))?(?![\d.])",   # 2021-01&02 合刊
    rf"(?<!\d){Y}(?!\d)",
]
ISSUE_RES = [re.compile(p, re.I) for p in ISSUE_PATS]


def parse_issue(stem: str, year_hint: int = 0) -> Issue | None:
    """文件名里的期号。year_hint：文件名只有「0727」这种月日时，用上层目录里的年份。"""
    s = clean(stem)
    for r in ISSUE_RES:
        m = r.search(s)
        if not m:
            continue
        g = {k: v for k, v in m.groupdict().items() if v}
        try:
            y = int(g["y"])
            mo = _mon(g["M"]) if "M" in g else int(g.get("m", 0))
            d = int(g.get("d", 0))
            m2 = _mon(g["M2"]) if "M2" in g else int(g.get("m2", 0))
            amb = None
            if "a" in g:
                a, b = int(g["a"]), int(g["b"])
                if a > 12:
                    mo, d = b, a
                elif b > 12:
                    mo, d = a, b
                else:
                    mo, d, amb = b, a, (a, b)            # 先按日.月，回头按同刊其他期的写法投票修正
            if m2 and not (mo < m2 <= 12):
                if mo > 12 >= m2:                        # 13.02 2023：其实是日.月
                    mo, d = m2, mo
                m2 = 0
        except (KeyError, ValueError):
            continue
        if not 0 <= mo <= 12 or not 0 <= d <= 31 or (d and not mo):
            continue
        if r is ISSUE_RES[-1]:                       # 只有年份：取最后一个（Time 1970s A Decade of Change 2024）
            m = list(r.finditer(s))[-1]
            y = int(m.group("y"))
            if year_hint and y != year_hint:             # People Celebrating The 80s 1984（目录是 2024）
                return Issue(year_hint, rest=s)
        iss = Issue(y, mo, d, m2, amb)
        iss.rest = re.sub(r"\s+", " ", s[: m.start()] + " " + s[m.end():]).strip(" -.,_()·")
        return iss
    if not year_hint:
        return None
    m = re.search(r"(?<!\d)(\d\d)(\d\d)(?:-\d\d)?$", s)                     # 0727、0706-13（合刊）
    if m and 1 <= int(m.group(1)) <= 12 and 1 <= int(m.group(2)) <= 31:
        return Issue(year_hint, int(m.group(1)), int(m.group(2)), rest=s[: m.start()].strip(" -"))
    m = re.search(r"(?<!\d)(\d\d)[-._](\d\d)$", s)                          # National Geographic-17-06
    if m and int(m.group(1)) == year_hint % 100 and 1 <= int(m.group(2)) <= 12:
        return Issue(year_hint, int(m.group(2)), rest=s[: m.start()].strip(" -"))
    if re.search(MON, s, re.I):                                               # The Independent - 24 March
        iss = parse_issue(f"{s} {year_hint}")
        if iss and len(iss.key()) > 4:
            return iss
    return None


GROUP_DIR = re.compile(r"^(?:日报|周报|月报|季刊|双月)更新\d*$|往期$|^\d{1,2}(?:\s*[~～-]\s*\d{1,2})?月|"
                       r"\d+\s*-\s*\d+$|^he-?\d+|^\d+$|^(?:19|20)\d\d(?:年|\s*-\s*(?:19|20)\d\d)?$", re.I)


def mag_name(seg: str) -> str:
    """目录名 → 刊名：去年份、序号；「英文丨中文」「26 – TIME – 时代周刊」「《独立报》The independent」取英文。"""
    s = re.sub(r"^\d+\s*[–-]\s+", "", seg)
    parts = [p.strip() for p in re.split(r"丨|\s[–—]\s|[《》]", s) if p.strip()]
    s = next((p for p in parts if not CJK.search(p)), parts[0] if parts else "")
    s = re.sub(r"(?:\s+|(?<=[a-z]))(?:19|20)\d\d(?:\s*-\s*(?:19|20)\d\d)?(?:年)?$", "", s, flags=re.I)
    s = re.sub(r"往期$", "", s)
    return re.sub(r"\s+", " ", s).strip(" -")


def mag_key(name: str) -> str:
    return re.sub(r"[^a-z0-9㐀-鿿]", "", name.lower())


# ---------------------------------------------------------------- 元数据
ISBN = re.compile(r"(97[89]\d{10}|\d{9}[\dX])")


def _isbn(s: str) -> str:
    m = ISBN.search(re.sub(r"[\s-]", "", s or "").upper())
    return m.group(1) if m else ""


def _xml(b: bytes):
    if b"<!ENTITY" in b:                 # 不处理带实体定义的 XML（防实体膨胀）
        raise ValueError("XML 含实体定义")
    return ET.fromstring(b)


def _epub_sample(z: zipfile.ZipFile, opf: str, o, limit: int = 3_000_000) -> str:
    """全书正文（去掉标签），判断语言用。不再抽样：抽中译著的英文注释、参考文献、版权页时会把中文书判成英文；
    按全书统计和书库核对的口径一致。limit 只防超大文件。"""
    items = {el.get("id"): el.get("href") for el in o.iter() if el.tag.rsplit("}", 1)[-1] == "item"}
    spine = [items.get(el.get("idref")) for el in o.iter() if el.tag.rsplit("}", 1)[-1] == "itemref"]
    spine = [h for h in spine if h] or sorted(n for n in z.namelist() if n.lower().endswith((".xhtml", ".html", ".htm")))
    base = posixpath.dirname(opf)
    names = set(z.namelist())
    out, n = [], 0
    for href in spine:
        name = href if href in names else posixpath.normpath(posixpath.join(base, unquote(href)))
        try:
            raw = z.read(name).decode("utf-8", "ignore")
        except KeyError:
            continue
        text = re.sub(r"<[^>]+>", " ", re.sub(r"(?is)<(script|style|head)\b.*?</\1>", " ", raw))
        text = re.sub(r"&(?:amp;)?[a-z#0-9]+;|\s+", " ", text)     # 转义两次的 &amp;nbsp; 也去掉，不算字数
        out.append(text)
        n += len(text)
        if n >= limit:
            break
    return " ".join(out)


def epub_meta(path: Path) -> dict:
    with zipfile.ZipFile(path) as z:
        root = _xml(z.read("META-INF/container.xml"))
        opf = next(el.get("full-path") for el in root.iter() if el.tag.endswith("rootfile"))
        o = _xml(z.read(opf))
        sample = _epub_sample(z, opf, o)
    out = {"title": "", "author": "", "isbn": "", "lang": "", "sample": sample}
    for el in o.iter():
        tag = el.tag.rsplit("}", 1)[-1]
        txt = (el.text or "").strip()
        if tag == "title" and not out["title"]:
            out["title"] = txt
        elif tag == "creator" and not out["author"]:
            out["author"] = txt
        elif tag == "language" and not out["lang"]:
            out["lang"] = txt[:2].lower()
        elif tag == "identifier" and not out["isbn"]:
            scheme = " ".join(v for k, v in el.attrib.items() if k.endswith("scheme")).lower()
            if "isbn" in scheme or "isbn" in txt.lower() or re.fullmatch(r"[\d\- X]{10,17}", txt):
                out["isbn"] = _isbn(txt)
    return out


def mobi_meta(path: Path) -> dict:
    """PalmDB/MOBI 头 + EXTH：100 作者、503 书名、104 ISBN、524 语言。"""
    with open(path, "rb") as f:
        head = f.read(78)
        n = struct.unpack(">H", head[76:78])[0]
        off0 = struct.unpack(">I", f.read(8)[:4])[0]
        f.seek(off0)
        r0 = f.read(1 << 16)
    out = {"title": "", "author": "", "isbn": "", "lang": ""}
    if n < 1 or r0[16:20] != b"MOBI":
        return out
    hlen, enc = struct.unpack(">I", r0[20:24])[0], struct.unpack(">I", r0[28:32])[0]
    codec = "utf-8" if enc == 65001 else "cp1252"
    fo, fl = struct.unpack(">II", r0[84:92])
    out["title"] = r0[fo:fo + fl].decode(codec, "replace").strip()
    flags = struct.unpack(">I", r0[128:132])[0] if len(r0) >= 132 else 0
    if flags & 0x40:
        p = 16 + hlen
        if r0[p:p + 4] == b"EXTH":
            cnt = struct.unpack(">I", r0[p + 8:p + 12])[0]
            p += 12
            for _ in range(cnt):
                if p + 8 > len(r0):
                    break
                t, ln = struct.unpack(">II", r0[p:p + 8])
                v = r0[p + 8:p + ln].decode(codec, "replace").strip()
                if t == 100 and not out["author"]:
                    out["author"] = v
                elif t == 503 and v:
                    out["title"] = v
                elif t == 104 and not out["isbn"]:
                    out["isbn"] = _isbn(v)
                elif t == 524 and not out["lang"]:
                    out["lang"] = v[:2].lower()
                p += max(ln, 8)
    return out


def pdf_meta(path: Path) -> dict:
    import pymupdf
    with pymupdf.open(path) as d:
        md = d.metadata or {}
        n = d.page_count
        sample = sorted({int(n * f) for f in (0.2, 0.5, 0.8)} & set(range(n))) or ([0] if n else [])
        texts = [d[i].get_text().strip() for i in sample]
        chars = sum(len(t) for t in texts)
        for i in sorted({int(n * f) for f in (0.1, 0.35, 0.65, 0.9)} & set(range(n)) - set(sample)):
            texts.append(d[i].get_text().strip())                 # 多取几页给判断语言用，不影响「有没有文字层」
        return {"title": (md.get("title") or "").strip(), "author": (md.get("author") or "").strip(),
                "isbn": "", "lang": "", "pages": n, "has_text": int(chars >= 150 * max(1, len(sample)) // 3),
                "sample": " ".join(texts)[:4000]}


JUNK_META = re.compile(r"^(?:untitled|unknown|microsoft word|title|document|book|cover|封面|作者|anonymous|"
                       r"adobe|calibre|amazon kindle|.*\.(?:docx?|pdf|indd|qxd)$)", re.I)


def sane(s: str) -> bool:
    s = (s or "").strip()
    return 2 <= len(s) <= 200 and not JUNK_META.match(s) and not re.fullmatch(r"[\W\d_]+", s) and "\\" not in s


# ---------------------------------------------------------------- 归一化（作品归并用）
def fold(s: str) -> str:
    s = unicodedata.normalize("NFKD", s or "")
    s = "".join(c for c in s if not unicodedata.combining(c)).lower()
    s = re.sub(r"[\(（\[［【][^\)）\]］】]*[\)）\]］】]", " ", s)
    s = s.replace("&", " and ").replace("’", "'")
    return re.sub(r"[^\w㐀-鿿]+", " ", s).strip()


def norm_title(t: str) -> str:
    """整个书名比较（不切副标题：同一作者的「系列名: 分册名」不能合并）。"""
    return re.sub(r"^(the|a|an) ", "", fold(t2s(t or ""))).replace(" ", "")      # 繁简同一个键


def norm_author(a: str) -> str:
    a = re.sub(r"[\[［（(【][^\]］）)】]*[\]］）)】]", " ", a or "")
    a = re.sub(r"\s*(?:著|译|编|等)\s*$", "", a)
    a = re.split(r"\s*(?:;|&|，|、|\band\b)\s*", a.strip())[0]
    if CJK.search(a):
        return fold(t2s(a)).replace(" ", "")                     # 楊定一 = 杨定一
    if "," in a:
        a = a.split(",")[0]                                  # 'King, Stephen'
    else:
        a = (a.split() or [""])[-1]
    return fold(a).replace(" ", "")


LATIN = re.compile(r"[A-Za-z]")
EN_DIR = re.compile(r"英文|english", re.I)


def lang_tag(tag: str) -> str:
    """书里自带的语言标记（已截成前两个字符）归一：und 之类当作没有，cn、「简体中文」「中文」都是 zh。"""
    tag = (tag or "").strip().lower()
    if tag in ("un", "mu", "zx", "xx"):
        return ""
    return "zh" if tag in ("cn", "ch", "简体", "繁体", "中文", "汉语") else tag


KANA = re.compile(r"[\u3040-\u30ff]")
HANGUL = re.compile(r"[\uac00-\ud7af]")
CYRILLIC = re.compile(r"[\u0400-\u04ff]")
# 拉丁字母写的几种语言各取最常见的虚词：正文里哪一组出现得最多就是哪种语言（书里的语言标记常是默认值，不可靠）
STOPWORDS = {
    "en": "the and of to in is was that it he for with as his on be at by had not but",
    "fr": "le la les des est une dans que qui pour pas sur au avec il elle ce sont du",
    "de": "der die das und ist nicht ein eine zu den mit sich auf dem des ich sie es",
    "es": "el la los las que de en y es por con una para del se lo como pero",
    "it": "il di che la per non una sono del della con gli le si è ma",
    "pt": "o a os as que de em um uma para com não do da por se mais",
    "nl": "de het een van en is dat niet op te zijn met voor die er ik",
}
STOP_SETS = {k: set(v.split()) for k, v in STOPWORDS.items()}
WORD = re.compile(r"[a-zà-ÿ]+")


def latin_lang(sample: str) -> str:
    """拉丁字母正文是哪种语言：虚词够多（≥ 100 个）且某种外语的虚词是英语的 3 倍以上才算外语；看不出来返回空。"""
    n = dict.fromkeys(STOP_SETS, 0)
    for w in WORD.findall(sample[:400_000].lower()):
        for k, s in STOP_SETS.items():
            if w in s:
                n[k] += 1
    if sum(n.values()) < 100:
        return ""
    best = max((k for k in n if k != "en"), key=n.get)
    return best if n[best] >= 3 * max(n["en"], 1) else "en"


def lang_of(title: str, meta_lang: str, sample: str = "", dir_lang: str = "") -> str:
    """语言按正文判断：书名里的汉字（「Hackers & Painters 黑客与画家」）、拼音（「Tan Lan De Duo Ba E」）都会误导。
    正文样本够长时看汉字和拉丁字母谁多；没有样本才用书里自带的语言标记和所在目录，最后才看书名。
    中英对照的书两种文字差不多一样多，谁多谁少只看取样落在哪段：先看书里标没标中文（国内出版的对照本都标 zh，
    「母亲集」），再看是不是放在英文书目录里（「英文原著专区/廊桥遗梦-中英版」），都没有才比多少。
    dir_lang：文件放在名字带「英文」「English」的目录里时为 en。
    日文（假名占汉字加假名的四分之一以上）、韩文、俄文按各自的文字判；拉丁字母的正文再按虚词分英、法、德、西、意、葡、荷。"""
    sample = sample or ""
    han, lat = len(CJK.findall(sample)), len(LATIN.findall(sample))
    kana, hangul, cyr = len(KANA.findall(sample)), len(HANGUL.findall(sample)), len(CYRILLIC.findall(sample))
    if kana >= 200 and kana * 4 >= han + kana:
        return "ja"
    if hangul >= 200 and hangul * 2.5 >= max(han * 2.5, lat):
        return "ko"
    if cyr >= 200 and cyr >= max(han * 2.5, lat):
        return "ru"
    if han + lat >= 200:            # 一个汉字的信息量约合 2.5 个字母
        if han <= lat <= 7.5 * han:     # 中英对照：折算后两种文字各占四分之一以上（英文书的注释、中文书里的代码都不到）
            if meta_lang == "zh":
                return "zh"
            if dir_lang:
                return dir_lang
        if han * 2.5 >= lat:
            return "zh"
        return latin_lang(sample) or (meta_lang if meta_lang and meta_lang != "zh" else "en")
    # 没有正文样本（mobi、扫描版 pdf 等）：书里标着英文、书名里又有两个以上英文单词的（「VANITY FAIR(名利场)」）算英文
    if meta_lang == "en" and len(re.findall(r"[A-Za-z]{2,}", title or "")) >= 2:
        return "en"
    if dir_lang:                    # 英文书目录里只有中文书名的扫描版分级读物（「分难易等级阅读/劝导.pdf」）
        return dir_lang
    if CJK.search(title or ""):
        return "zh"
    return meta_lang or "en"


# 中文正文的书是不是外国作品的译本（书城单列「译文」）。作者前的国籍标记只认外国：「（清）」「[宋]」「[中]」是中国的朝代和国别
FOREIGN = set("美 英 日 法 德 俄 苏 意 西 加 韩 澳 奥 印 以 荷 比 瑞 挪 丹 芬 波 捷 匈 葡 希 土 墨 智 巴 越 泰 朝 黎".split()) | set(
    "美国 英国 日本 法国 德国 俄国 俄罗斯 苏联 意大利 西班牙 加拿大 韩国 澳大利亚 奥地利 爱尔兰 以色列 印度 瑞典 挪威 丹麦 芬兰 "
    "荷兰 比利时 瑞士 波兰 捷克 匈牙利 希腊 古希腊 古罗马 罗马 葡萄牙 巴西 阿根廷 智利 哥伦比亚 墨西哥 秘鲁 土耳其 伊朗 埃及 "
    "南非 新西兰 黎巴嫩 阿富汗 危地马拉 乌拉圭 古巴 尼日利亚 肯尼亚 冰岛 罗马尼亚 保加利亚 塞尔维亚 克罗地亚 阿尔巴尼亚 乌克兰 "
    "白俄罗斯 格鲁吉亚 叙利亚 巴勒斯坦 越南 泰国 朝鲜 蒙古国 巴基斯坦 孟加拉国 斯里兰卡 菲律宾 印尼 印度尼西亚 斯洛文尼亚 "
    "斯洛伐克 立陶宛 拉脱维亚 爱沙尼亚 奥匈 波斯 古波斯 阿拉伯 沙特 阿尔及利亚 摩洛哥 突尼斯 苏格兰 威尔士 北爱尔兰 委内瑞拉 "
    "捷克斯洛伐克 南斯拉夫 厄瓜多尔 玻利维亚 多米尼加 海地 牙买加 津巴布韦 坦桑尼亚 乌干达 埃塞俄比亚 索马里 加纳 塞内加尔 "
    "喀麦隆 刚果 安哥拉 莫桑比克 马里 苏丹 古印度 古埃及 英格兰".split())
NAT = re.compile(r"[\[【（(［〔]\s*([^\]】）)］〕\s]{1,7}?)\s*[\]】）)］〕]")
# 版权页上的说法：只看正文开头 3000 字（书里引用的「××译，商务印书馆」不算）；「图字」引进版权的港台中文原创也有，不算
TR_FRONT = re.compile(r"译\s*者\s*[:：]|翻\s*译\s*[:：]|Translated (?:by|from)|Simplified Chinese (?:translation|edition|language)|"
                      r"Chinese (?:translation|language edition)|Original (?:English )?(?:language )?(?:edition|title)|"
                      r"原(?:版|著)?书名\s*[:：]|英文书名\s*[:：]|原\s*著\s*[:：]", re.I)
TR_AUTHOR = re.compile(r"译\s*$|[\u4e00-\u9fff]\s*译\s*[;；，,）)\]、]|[;；]\s*\S+\s*译")
TR_SERIES = re.compile(r"译丛|名著名译|汉译|全译本|译文集|中译本")
TRANSLIT = re.compile(r"[\u4e00-\u9fff][·•．・.]\s?[\u4e00-\u9fffA-Z]")    # 外国人名的音译：赫尔曼·梅尔维尔、乔治·R·R·马丁、艾琳.亨特


def foreign_mark(s: str) -> str:
    """「[美]赫尔曼·梅尔维尔」「【日】东野圭吾」里的外国国籍；没有返回空。"""
    return next((m.group(1) for m in NAT.finditer(s or "") if m.group(1) in FOREIGN), "")


def trans_cue(title: str, author: str, stem: str, sample: str) -> str:
    """看得出是外国作品的中文译本时返回依据（国籍、音译人名、译者、版权页、译丛、外文作者名），否则返回空。
    只用编目读得到的东西；书目库的 CIP、原版配对、模型给的原著语言在书城视图里再并进来（library/store.py）。"""
    if foreign_mark(author) or foreign_mark(stem) or foreign_mark(title):
        return "国籍"
    if TRANSLIT.search(author or ""):
        return "音译人名"
    if TR_AUTHOR.search(author or ""):
        return "译者"
    if TR_FRONT.search((sample or "")[:3000]):
        return "版权页"
    if TR_SERIES.search(f"{title} {stem}"):
        return "译丛"
    if len(re.findall(r"[A-Za-z]{2,}", author or "")) >= 2 and not CJK.search(author or ""):
        return "外文作者名"
    return ""


def spread_trans(works: list[dict]) -> None:
    """同一作者的中文书多数看得出是译本（至少两本）时，其余看不出的也算：「村上春树」「毛姆」有的文件带国籍、有的不带。"""
    by: dict = defaultdict(list)
    for w in works:
        if w["kind"] == "book" and w["lang"] == "zh" and len(k := norm_author(w["author"])) >= 2:
            by[k].append(w)
    for ws in by.values():
        n = sum(1 for w in ws if w["trans"])
        if n >= 2 and n * 2 >= len(ws):
            for w in ws:
                w["trans"] = w["trans"] or "同作者"


# ---------------------------------------------------------------- 一个文件
@dataclass
class FileInfo:
    path: str
    size: int
    mtime: float
    fid: str
    fmt: str
    kind: str
    title: str = ""
    author: str = ""
    meta_title: str = ""
    meta_author: str = ""
    isbn: str = ""
    lang: str = "en"
    pages: int = 0
    has_text: int = 1
    mag: str = ""
    issue: str = ""
    issue_label: str = ""
    tags: list = field(default_factory=list)       # [[货架名, 序号或 None], …]
    error: str = ""
    amb: tuple | None = None
    trans: str = ""                                 # 中文译本的依据（trans_cue），不是译本为空


def segments(path: Path, root: Path) -> list[str]:
    try:
        return list(path.relative_to(root).parts[:-1])
    except ValueError:
        return list(path.parts[:-1])


def _txt_sample(path: Path) -> str:
    """txt 的正文样本：全文均匀取 4 段、每段 16KB（开头常是大段空白和目录；中英对照的章节前一半英文、后一半中文，
    只取一处会只看到一种），小文件整个读。截取处可能切断多字节字符，UTF-8 两头各允许少几个字节；
    不是 UTF-8 的按 GB18030 宽松解码（英文字母在哪种解码里都一样）。"""
    size = path.stat().st_size
    with open(path, "rb") as f:
        head = f.read(4)
        if size <= 65536:
            f.seek(0)
            raws = [f.read()]
        else:
            raws = []
            for i in range(4):
                f.seek(size * (2 * i + 1) // 8 - 8192 & ~1)     # 偶数字节处：UTF-16 不错位
                raws.append(f.read(16384))
    out = []
    for raw in raws:
        if head.startswith((b"\xff\xfe", b"\xfe\xff")):
            text = raw.decode("utf-16-le" if head.startswith(b"\xff\xfe") else "utf-16-be", "ignore")
        else:
            text = None
            for a in range(4):
                for b in range(4):
                    try:
                        text = raw[a:len(raw) - b].decode("utf-8")
                        break
                    except UnicodeDecodeError:
                        continue
                if text is not None:
                    break
            if text is None:                                 # 双字节编码从半个字切进来会整段错位：错一个字节试试，取乱码少的
                text = min((raw[k:].decode("gb18030", "replace") for k in (0, 1)),
                           key=lambda t: len(re.findall("[\ufffd\ue000-\uf8ff]", t))).replace("\ufffd", "")
        text = re.sub(r"\s+", " ", text)
        n = len(text)
        if len(raws) == 1 and n > 16000:                     # 整个读进来的小文件也均匀取 4 段
            out += [text[n * i // 4:n * i // 4 + 4000] for i in range(4)]
        else:
            out.append(text[:16000 // len(raws)])
    return " ".join(out)


def _palmdoc(data: bytes) -> bytes:
    """MOBI 正文常用的 PalmDOC（LZ77）解压。"""
    out = bytearray()
    i = 0
    while i < len(data):
        c = data[i]
        i += 1
        if c == 0 or 9 <= c <= 0x7F:
            out.append(c)
        elif c <= 8:
            out += data[i:i + c]
            i += c
        elif c >= 0xC0:
            out += b" " + bytes([c ^ 0x80])
        else:
            if i >= len(data):
                break
            m = ((c << 8) | data[i]) & 0x3FFF
            i += 1
            dist, n = m >> 3, (m & 7) + 3
            for _ in range(n):
                out.append(out[-dist] if 0 < dist <= len(out) else 32)
    return bytes(out)


def _strip_trailing(d: bytes, flags: int) -> bytes:
    """去掉正文记录末尾的附加数据（MOBI 头的 extra data flags）；不去掉的话解压出来会多出乱码。"""
    f = flags >> 1
    while f:
        if f & 1:
            n = 0
            for b in reversed(d[-4:]):            # 从末尾倒着读的变长整数，高位为 1 的字节是它的开头
                n += 1
                if b & 0x80:
                    break
            size = 0
            for b in d[len(d) - n:]:
                size = (size << 7) | (b & 0x7F)
            d = d[:len(d) - size] if 0 < size <= len(d) else d
        f >>= 1
    if flags & 1 and d:
        d = d[:len(d) - ((d[-1] & 3) + 1)]
    return d


def mobi_text(path: Path, picks: int = 0) -> tuple[str, int, int]:
    """mobi/azw 的正文（HTML）：(文字, 坏记录数, 正文记录数)。picks>0 时只在全书均匀取这么多条记录。
    只解无压缩和 PalmDOC 压缩；HUFF/CDIC 压缩的返回空。解出来比一条记录还长的是坏记录（文件中段损坏），不要。"""
    with open(path, "rb") as f:
        head = f.read(78)
        n = struct.unpack(">H", head[76:78])[0]
        offs = [struct.unpack(">I", f.read(8)[:4])[0] for _ in range(n)] + [path.stat().st_size]
        f.seek(offs[0])
        r0 = f.read(256)
        comp, nrec = struct.unpack(">H", r0[0:2])[0], struct.unpack(">H", r0[8:10])[0]
        size = struct.unpack(">H", r0[10:12])[0] or 4096
        mobi = r0[16:20] == b"MOBI"
        enc = struct.unpack(">I", r0[28:32])[0] if mobi else 1252
        flags = struct.unpack(">H", r0[0xF2:0xF4])[0] if mobi and struct.unpack(">I", r0[20:24])[0] >= 0xE4 else 0
        nrec = min(nrec, n - 1)
        if comp not in (1, 2) or nrec < 1:
            return "", 0, 0
        ks = (sorted({max(1, min(nrec, int(nrec * (0.05 + 0.9 * i / (picks - 1))))) for i in range(picks)})
              if picks > 1 else range(1, nrec + 1))
        out, bad = [], 0
        for k in ks:
            f.seek(offs[k])
            data = _strip_trailing(f.read(offs[k + 1] - offs[k]), flags)
            text = _palmdoc(data) if comp == 2 else data
            if len(text) > size + 8:
                bad += 1
            else:
                out.append(text)
    return b"".join(out).decode("utf-8" if enc == 65001 else "cp1252", "ignore"), bad, nrec


def _mobi_sample(path: Path, limit: int = 200_000) -> str:
    """mobi/azw 的正文样本：全书均匀取 30 条正文记录，去掉标签，判断语言用。"""
    text = mobi_text(path, picks=30)[0]
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", text))[:limit]


def describe(path: Path, root: Path, read_meta: bool = True) -> FileInfo:
    st = path.stat()
    fmt = path.suffix.lower().lstrip(".")
    segs = [unent(x) for x in segments(path, root)]
    dir_lang = "en" if any(EN_DIR.search(s) for s in segs) else ""     # 放在「英文原著专区」这类目录里（书库目录本身的名字不算）
    top = (segs[:1] or [""])[0] if segs[:1] and segs[0] in KIND_DIRS else root.name   # 也可以直接拿「杂志」目录当书库目录
    kind = KIND_DIRS.get(top, "book")
    if top == root.name and kind != "book":
        segs = [root.name] + segs
    if kind == "magazine" and any("日报" in s for s in segs):
        kind = "newspaper"
    fi = FileInfo(str(path), st.st_size, st.st_mtime, book_id(path), fmt, kind)
    stem = unent(path.stem)
    meta = {}
    if read_meta:
        try:
            if fmt == "epub":
                meta = epub_meta(path)
            elif fmt in ("mobi", "azw", "azw3"):
                meta = mobi_meta(path)
            elif fmt == "pdf":
                meta = pdf_meta(path)
        except Exception as ex:                       # 坏文件也照样编目（按文件名）
            fi.error = f"{type(ex).__name__}: {ex}"[:200]
    fi.meta_title, fi.meta_author = unent(meta.get("title", "")), unent(meta.get("author", ""))
    fi.isbn = meta.get("isbn", "") or (aa_isbn(stem) if kind == "book" else "")
    fi.pages = meta.get("pages", 0)
    fi.has_text = meta.get("has_text", 0 if fmt in ("azw3", "djvu", "chm") else 1)

    if kind == "book":
        t, a = guess_book(stem)
        # 文件名就是「书名.ext」、上一级是作者目录（1600 本那种：作者/书名 (id)/文件）
        if not a and len(segs) >= 2 and re.search(r"\(\d+\)$", segs[-1]) and namey(segs[-2]):
            a = segs[-2]
        ma = clean_author(fi.meta_author)            # Administrator、Windows User 这类占位不算作者，退回文件名里的
        if fmt in ("epub", "mobi", "azw", "azw3") and sane(fi.meta_title):
            t = fi.meta_title
            a = ma if sane(ma) else a
        elif fmt == "pdf" and not a and sane(ma) and namey(ma):
            a = ma
        fi.title, fi.author = simp_title(t.strip()), fix_author(clean_author(a))
        fi.tags = shelf_tags(segs, stem)
    else:
        hint = next((int(y) for seg in reversed(segs) for y in re.findall(r"(?<!\d)((?:19|20)\d\d)(?!\d)", seg)), 0)
        iss = parse_issue(stem, hint)
        # 最浅一层「不是分组/年份/月份目录」的目录名；中文目录名（《泰晤士报》）时用文件名里的英文刊名
        folders = [mag_name(s) for s in segs[1:] if not GROUP_DIR.search(s.strip()) and mag_name(s)]
        name = folders[0] if folders else ""
        if (not name or CJK.search(name)) and iss and iss.rest and not CJK.search(iss.rest):
            name = re.sub(r"(?:\s+(?:uk|us|usa|international|edition)|\s*-\s*\d+)$", "", iss.rest, flags=re.I)
        if not name and segs:
            name = mag_name(segs[0])
        fi.mag = name
        if iss:
            fi.issue, fi.amb = iss.key(), iss.amb
            label = iss.rest
            if label and mag_key(label).startswith(mag_key(name)[:6]):
                label = label[len(name):].strip(" -") if label.lower().startswith(name.lower()) else ""
            label = re.sub(r"\b(?:UK|USA?|AU|NZ|SG|Int|International|Edition)\b", " ", label or "")
            label = re.sub(r"^[\W_]+|[\W_]+$", "", re.sub(r"\s+", " ", label))
            fi.issue_label = label if label and not re.fullmatch(r"(?:i\d+|\(\d\))", label, re.I) else ""
            if iss.m2:
                fi.issue_label = (fi.issue_label + f" {iss.m}–{iss.m2}月").strip()
        else:
            m = re.search(r"(\d{2,4})$", clean(stem))           # 只有期数：HotEnglish151
            fi.issue_label = f"No. {int(m.group(1))}" if m else clean(stem)
        fi.title = f"{name} · {fi.issue_label or fi.issue}".strip(" ·")
        fi.tags = [[name, None]]
    try:
        if not meta.get("sample") and fmt == "txt":
            meta["sample"] = _txt_sample(path)
        elif not meta.get("sample") and fmt in ("mobi", "azw", "azw3"):
            meta["sample"] = _mobi_sample(path)
    except Exception:  # noqa: BLE001  取不到样本就按书里的语言标记和书名
        pass
    fi.lang = lang_of(fi.title, lang_tag(meta.get("lang", "")), meta.get("sample", ""), dir_lang)
    if fi.lang == "zh" and fi.kind == "book":
        fi.trans = trans_cue(fi.title, fi.author, stem, meta.get("sample", ""))
    return fi


def shelf_tags(segs: list[str], stem: str) -> list[list]:
    """来源目录 → 货架标签 [[名字, 本书在这个货架里的序号]]。书自己的目录（和文件名同名）不算货架。"""
    out = []
    base = fold(strip_num(clean(stem))[0])
    for i, s in enumerate(segs):
        name, _ = strip_num(clean(s))
        if "丨" in name:
            name = name.split("丨")[-1].strip()
        name = re.sub(r"\s+\d+\s*本$|[（(][^)）]*(?:epub|mobi|pdf|格式)[^)）]*[)）]|！?mobi格式$", "", name, flags=re.I).strip(" ！!")
        if not name or SKIP_SEG.match(name) or YEAR_ONLY.match(name) or re.search(r"\(\d+\)$", s) \
                or re.fullmatch(r"(?:19|20)\d\d[-.]\d\d(?:[-.]\d\d)?", name):
            continue
        if fold(name) and (fold(name) == base or base.startswith(fold(name)) and i == len(segs) - 1):
            continue                                       # 书自己的目录
        rank = None
        if i + 1 < len(segs):
            _, rank = strip_num(clean(segs[i + 1]))
        elif i == len(segs) - 1:
            _, rank = strip_num(clean(stem))
        out.append([name, rank])
    return out


# ---------------------------------------------------------------- 作品归并
NUMS = re.compile(r"\d+")
COPY_MARK = re.compile(r"\s*[(（]\d{1,2}[)）]\s*$")      # 重名加的「 (2)」「(1)」：不是卷册号


def volume_numbers(title: str) -> frozenset:
    """书名里的卷册、级别、版次数字（年份不算）：Creative Couplings (Book 2) → {2}，第3级全 → {3}。"""
    t = COPY_MARK.sub("", title or "")
    return frozenset(int(n) for n in NUMS.findall(t) if not (len(n) == 4 and 1400 <= int(n) <= 2099))


STOP = {"the", "and", "for", "with", "from", "book", "books", "novel", "edition", "classics", "series", "volume", "part"}


def title_words(title: str) -> set:
    """书名里有意义的词：西文 3 个字母以上的词（去掉 the、edition…），中文按字。括号里的丛书名（Lonely Planet、
    Barnes & Noble Classics）不算，除非去掉括号就什么都不剩了。"""
    t = re.sub(r"(?<=[a-z])(?=[A-Z])", " ", title or "").lower()      # OfMiceAndMen → of mice and men

    def words(t: str) -> set:
        return {w for w in re.findall(r"[a-z]{3,}", t) if w not in STOP} | set(re.findall(r"[\u4e00-\u9fff]", t))
    return words(re.sub(r"[(（\[［【][^)）\]］】]*[)）\]］】]", " ", t)) or words(t)


def same_book(a: dict, b: dict) -> bool:
    """共用一个 ISBN 的两份书是不是真是同一本书的两个版本：语言相同、书名里的卷册数字相同、书名沾边。
    套装、丛书、译本、乱填的元数据会让不同的书共用 ISBN；归并作品和去重都用这条判断。只用于书（kind=book）。"""
    if {a.get("lang"), b.get("lang")} == {"zh", "en"}:          # 中英对照的译本；别的语言判断在短样本上不准，不拦
        return False
    va, vb = volume_numbers(a.get("title", "")), volume_numbers(b.get("title", ""))
    if va and vb and va != vb:                                   # 一边没写卷册号（Twilight / Twilight, Volume 1）不算不同
        return False
    wa, wb = title_words(a.get("title", "")), title_words(b.get("title", ""))
    return not (wa and wb and not wa & wb)                       # 书名一个词都不沾（India / Russia）：ISBN 撞到一起的


class UF:
    def __init__(self):
        self.p: dict = {}

    def find(self, x):
        self.p.setdefault(x, x)
        while self.p[x] != x:
            self.p[x] = self.p[self.p[x]]
            x = self.p[x]
        return x

    def union(self, a, b):
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.p[max(ra, rb)] = min(ra, rb)


def resolve_ambiguous(files: list[dict]) -> None:
    """a.b.YYYY 两个数都 ≤12 的期号：看同一本刊其他期是「日.月」还是「月.日」。"""
    votes: dict = defaultdict(Counter)
    for f in files:
        if f["kind"] != "book" and f["issue"] and not f.get("amb") and f["issue"].count("-") == 2:
            stem = Path(f["path"]).stem
            m = re.search(r"(?<!\d)(\d{1,2})[./-](\d{1,2})[./-](?:19|20)\d\d", stem)
            if m:
                a, b = int(m.group(1)), int(m.group(2))
                votes[mag_key(f["mag"])]["dm" if a > 12 else "md" if b > 12 else ""] += 1
    for f in files:
        amb = f.get("amb")
        if amb and votes[mag_key(f["mag"])]["md"] > votes[mag_key(f["mag"])]["dm"]:
            y = f["issue"][:4]
            f["issue"] = f"{y}-{amb[0]:02d}-{amb[1]:02d}"
            if f["issue_label"] == "":
                f["title"] = f"{f['mag']} · {f['issue']}"
            else:
                f["title"] = f"{f['mag']} · {f['issue_label']}"


def group(files: list[dict]) -> list[list[dict]]:
    uf = UF()
    by: dict = defaultdict(list)
    isbn: dict = defaultdict(list)
    ta: dict = defaultdict(list)
    stem: dict = defaultdict(list)
    for i, f in enumerate(files):
        uf.find(i)
        by[("fid", f["fid"])].append(i)
        if f["kind"] == "book":
            d = str(Path(f["path"]).parent)
            stem[(d, fold(clean(Path(f["path"]).stem)))].append(i)
            if f["isbn"]:
                isbn[f["isbn"]].append(i)
            nt, na = norm_title(f["title"]), norm_author(f["author"])
            if nt and na:
                ta[(nt, na)].append(i)
        elif f["issue"] and len(f["issue"]) >= 7:           # 有具体月份/日期的期号；只有年份的特刊靠指纹合并
            by[("issue", f["kind"], mag_key(f["mag"]), f["issue"], fold(f["issue_label"]))].append(i)
    # 同一个 ISBN 的文件只在书名、语言对得上时合并（Book 1 / Book 2、Lonely Planet India / Russia、原著 / 中译本不并）；
    # 书名作者相同、同一目录里文件名相同的（fold 去掉了括号）只看卷册数字：Henry IV (Part 1) / (Part 2)、书虫 第5级 / 第6级
    # 不并，语言不看（同一套中英对照读物各格式的语言判断不一样）
    def vol_ok(name):
        def ok(a: dict, b: dict) -> bool:
            va, vb = volume_numbers(name(a)), volume_numbers(name(b))
            return not (va and vb and va != vb)
        return ok
    for buckets, ok in ((isbn, same_book), (ta, vol_ok(lambda f: f["title"])),
                        (stem, vol_ok(lambda f: clean(Path(f["path"]).stem)))):
        for idx in buckets.values():
            parts: list[list[int]] = []                  # 每个文件进第一个和其中每一份都对得上的小组，都不合就自成一组
            for i in idx:
                part = next((p for p in parts if all(ok(files[i], files[j]) for j in p)), None)
                if part is None:
                    parts.append([i])
                else:
                    part.append(i)
            for p in parts:
                for j in p[1:]:
                    uf.union(p[0], j)
    for idx in by.values():
        for j in idx[1:]:
            uf.union(idx[0], j)
    groups: dict = defaultdict(list)
    for i, f in enumerate(files):
        groups[uf.find(i)].append(f)
    return list(groups.values())


FILE_COLS = ("size", "mtime", "fid", "fmt", "kind", "title", "author", "meta_title", "meta_author", "isbn", "lang",
             "pages", "has_text", "mag", "issue", "issue_label", "tags", "error", "ver", "amb", "trans")


def text_rank(f: dict) -> tuple:
    r = TEXT_RANK.get(f["fmt"], 9)
    if f["fmt"] == "pdf" and not f["has_text"]:
        r = 9
    if f["error"] or f.get("gate"):                 # 质量关卡判了只能翻阅 / 入库读不出正文：不当文字来源
        r = 9
    return (r, -int(bool(sane(f["meta_title"]))), -f["size"])


def pick(g: list[dict], ghost_tags: list | None = None) -> dict:
    """一个作品：文字来源、版式来源、书名作者、格式、货架。"""
    text = min(g, key=lambda f: (text_rank(f)[0], traditional(f), text_rank(f)))      # 同格式简体版优先
    if text_rank(text)[0] >= 9:
        text = None
    pdfs = sorted((f for f in g if f["fmt"] == "pdf" and not f["error"] and f.get("gate") != "unreadable"),
                  key=lambda f: (-(f["has_text"] and not f.get("gate")), -f["size"]))
    layout = pdfs[0] if pdfs else None
    # 书名作者：有元数据的电子书 > 文字来源 > 任意一个
    named = sorted(g, key=lambda f: (f["fmt"] not in ("epub", "mobi", "azw", "azw3") or not sane(f["meta_title"]),
                                     not f["author"], text_rank(f)[0], traditional(f), text_rank(f)))
    best = named[0]
    author = best["author"] or next((f["author"] for f in named if f["author"]), "")
    fids = sorted({f["fid"] for f in g})
    tags: dict = {}
    tag_lists = [json.loads(f["tags"]) if isinstance(f["tags"], str) else f["tags"] for f in g] + (ghost_tags or [])
    for tl in tag_lists:
        for name, rank in tl:
            if name not in tags or (rank is not None and (tags[name] is None or rank < tags[name])):
                tags[name] = rank
    if author:                                       # 作者目录（1600 本那种按作者分的文件夹）不算货架
        tags = {k: v for k, v in tags.items() if fold(k) != fold(author)}
    formats = sorted({f["fmt"] for f in g}, key=lambda x: FMT_ORDER.index(x) if x in FMT_ORDER else 99)
    first = g[0]
    title = best["title"] or Path(best["path"]).stem
    if first["kind"] != "book":
        tags = {first["mag"]: None}
        title = " ".join(x for x in (first["mag"], first["issue"], first["issue_label"]) if x)
    return {
        "id": fids[0], "kind": first["kind"], "title": title, "author": author,
        "lang": (text or best)["lang"],            # 语言跟着实际阅读的那份文件（按它的正文判断）走
        "trans": next((f.get("trans") for f in [text or best] + g if f.get("trans")), None),   # 哪个格式看得出是译本都算
        "isbn": next((f["isbn"] for f in g if f["isbn"]), ""),
        "mag": first["mag"], "issue": max((f["issue"] for f in g), key=len, default=""),
        "issue_label": first["issue_label"], "formats": formats,
        "text_src": text["path"] if text else None, "layout_src": layout["path"] if layout else None,
        "n_files": len(g), "bytes": sum(f["size"] for f in g),
        "pages": (layout or text or first)["pages"] or 0,
        "has_text": int(text is not None), "tags": tags,
        "sort_title": re.sub(r"^(the|a|an)\s+", "", fold(title)),
    }


# ---------------------------------------------------------------- 编目库
@dataclass
class CatState:
    running: bool = False
    total: int = 0
    done: int = 0
    added: int = 0
    skipped: int = 0
    failed: int = 0
    excluded: int = 0            # 命中排除名单（library.exclude）的新文件：不进编目，等 exclude sweep 移走
    current: str = ""
    started: float = 0
    finished: float = 0
    stop: bool = False
    errors: list = field(default_factory=list)

    def as_dict(self) -> dict:
        d = {k: v for k, v in self.__dict__.items() if k != "errors"}
        d["errors"] = self.errors[-20:]
        return d


JUNK_FILE = re.compile(r"解压密码|获取音频|获取方式|客服|必看|说明|使用方法|readme|下载|更多资源|关注|二维码", re.I)


def find_files(roots: list[Path]) -> list[tuple[Path, Path]]:
    out = []
    for root in roots:
        for dp, dn, fn in os.walk(root):
            dn[:] = [x for x in dn if not x.startswith(".") and x != "$RECYCLE.BIN"]
            for f in fn:
                ext = Path(f).suffix.lower()
                if f.startswith(".") or ext not in EXTS:
                    continue
                if ext in (".txt", ".doc", ".docx") and JUNK_FILE.search(f):   # 资源包里的广告/说明文件
                    continue
                out.append((Path(dp) / f, root))
    return sorted(out)


def _entities(row) -> bool:
    return any(ENTITY.search(row[c] or "") for c in ("title", "author", "meta_title", "meta_author", "mag"))


class Catalog:
    def __init__(self, path: Path, on_connect=None, readonly: bool = False):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self.on_connect = on_connect          # 每个新连接建好后调用一次（书城用它建临时视图）
        self.on_use = None                    # 每次用连接前调用 (连接, 线程本地)：书城用它换上新一版的物化视图
        self.readonly = readonly              # 书城：只读连接（升级结构由写的一方在打开时做）
        self._local = threading.local()
        self.lock = threading.RLock()
        self.state = CatState()
        if not readonly:
            catalog_schema.ensure(self.conn)            # 结构见 library/schemas/catalog.py

    @property
    def conn(self) -> sqlite3.Connection:
        c = getattr(self._local, "c", None)
        if c is None:
            if self.readonly:
                c = sqlite3.connect(f"file:{self.path}?mode=ro", uri=True, timeout=30, check_same_thread=False)
            else:
                c = sqlite3.connect(self.path, timeout=30, check_same_thread=False)
                c.execute("PRAGMA synchronous=NORMAL")
            c.row_factory = sqlite3.Row
            threadconn.keep(self._local, c)
            if self.on_connect:
                self.on_connect(c)
        if self.on_use:
            self.on_use(c, self._local)
        return c

    def q(self, sql: str, args=()) -> list[sqlite3.Row]:
        return self.conn.execute(sql, args).fetchall()

    def one(self, sql: str, args=()):
        return self.conn.execute(sql, args).fetchone()

    # ---- 扫描文件
    def scan(self, dirs: list[str], progress=None) -> CatState:
        s = self.state = CatState(running=True, started=time.time())
        try:
            roots = [Path(os.path.expanduser(d)) for d in dirs]
            live = [r for r in roots if r.exists()]
            for r in roots:
                if r not in live:
                    s.errors.append({"path": str(r), "error": "目录不存在（外接盘没插？），已跳过，原有编目保留"})
            files = find_files(live)
            s.total = len(files)
            known = {r["path"]: r for r in self.q("SELECT path, size, mtime, ver, title, author, meta_title, meta_author, mag FROM files")}
            from .exclude import Blocklist, hold
            blk = Blocklist.load(self.path.parent)
            batch, held = [], []
            for p, root in files:
                if s.stop:
                    break
                s.current = str(p)
                try:
                    st = p.stat()
                    k = known.get(str(p))
                    if k and k["size"] == st.st_size and abs(k["mtime"] - st.st_mtime) < 1 and k["ver"] == CATALOG_VER \
                            and not _entities(k):         # 解码实体之前编目的行（「&amp;」「&#183;」）重新读一遍
                        s.skipped += 1
                    elif str(p) in blk.held:
                        s.excluded += 1
                    else:
                        fi = describe(p, root)
                        hit = blk.hit(vars(fi)) if blk else None
                        if hit:
                            held.append((vars(fi), hit))
                            s.excluded += 1
                        else:
                            batch.append(fi)
                            s.added += 1
                except Exception as ex:
                    s.failed += 1
                    s.errors.append({"path": str(p), "error": f"{type(ex).__name__}: {ex}"[:200]})
                s.done += 1
                if len(batch) >= 200:
                    self._save(batch)
                    batch = []
                if progress:
                    progress(s)
            self._save(batch)
            if held:
                hold(self.path.parent, held)
            if not s.stop:
                alive = {str(p) for p, _ in files}
                for root in live:
                    under = [r["path"] for r in known.values() if r["path"].startswith(str(root).rstrip("/") + "/")]
                    if under and not any(a.startswith(str(root).rstrip("/") + "/") for a in alive):
                        s.errors.append({"path": str(root), "error": f"目录里一个文件都没找到，但编目里有 {len(under)} 个：没有移除"})
                        continue
                    gone = [p for p in under if p not in alive and not Path(p).exists()]
                    with self.lock:
                        self.conn.executemany("DELETE FROM files WHERE path=?", [(p,) for p in gone])
                        self.conn.commit()
            s.current = "归并作品…"
            self.rebuild()
        except Exception:
            import traceback
            s.errors.append({"path": "", "error": traceback.format_exc()[-300:]})
        finally:
            s.running, s.current, s.finished = False, "", time.time()
        return s

    def _save(self, batch: list[FileInfo]) -> None:
        if not batch:
            return
        rows = [(f.path, f.size, f.mtime, f.fid, f.fmt, f.kind, f.title, f.author, f.meta_title, f.meta_author, f.isbn,
                 f.lang, f.pages, f.has_text, f.mag, f.issue, f.issue_label,
                 json.dumps(f.tags, ensure_ascii=False), f.error or None, CATALOG_VER,
                 json.dumps(f.amb) if f.amb else None, f.trans or None) for f in batch]
        with self.lock:
            # 重新编目只更新读出来的信息：作品归属（work/role，rebuild 会重算）原样保留，
            # 质量关卡的结论（gate）在文件内容没变时保留——不能一重新编目就把扫描版又标回「有文字」
            self.conn.executemany(
                "INSERT INTO files(path,size,mtime,fid,fmt,kind,title,author,meta_title,meta_author,isbn,lang,"
                "pages,has_text,mag,issue,issue_label,tags,error,ver,amb,trans) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT(path) DO UPDATE SET " + ",".join(f"{c}=excluded.{c}" for c in FILE_COLS) +
                ", gate=CASE WHEN files.fid=excluded.fid THEN files.gate END",
                rows)
            self.conn.commit()

    def set_gate(self, path: str, gate: str | None) -> None:
        """记下质量关卡/入库对一个文件的结论（flip / unreadable / None 清除）。files.gate 只从这里写。"""
        assert gate in (None, "flip", "unreadable"), gate
        with self.lock:
            self.conn.execute("UPDATE files SET gate=? WHERE path=?", (gate, path))
            self.conn.commit()

    # ---- 归并作品（每次全量重建：几万行在内存里几秒）
    def rebuild(self) -> None:
        files = [dict(r) for r in self.q("SELECT * FROM files")]
        for f in files:
            f["amb"] = tuple(json.loads(f["amb"])) if f["amb"] else None
        resolve_ambiguous(files)
        # 同一刊名的不同写法（TIME / Time）统一成最常见的那个
        names: dict = defaultdict(Counter)
        for f in files:
            if f["kind"] != "book":
                names[mag_key(f["mag"])][f["mag"]] += 1
        by_dir: dict = defaultdict(Counter)
        for f in files:
            if f["kind"] != "book" and not CJK.search(f["mag"]):
                by_dir[str(Path(f["path"]).parent)][f["mag"]] += 1
        for f in files:
            if f["kind"] != "book":
                if CJK.search(f["mag"]) and by_dir[str(Path(f["path"]).parent)]:
                    f["mag"] = by_dir[str(Path(f["path"]).parent)].most_common(1)[0][0]
                f["mag"] = names[mag_key(f["mag"])].most_common(1)[0][0] if names[mag_key(f["mag"])] else f["mag"]
        added = {r["id"]: r["added"] for r in self.q("SELECT id, added FROM works")}
        now = time.time()
        works, roles, shelves = [], [], []
        ghosts: dict = defaultdict(list)
        for r in self.q("SELECT keep_fid, tags FROM ghosts"):
            ghosts[r["keep_fid"]].append(json.loads(r["tags"] or "[]"))
        for g in group(files):
            w = pick(g, [t for fid in {f["fid"] for f in g} for t in ghosts.get(fid, [])])
            works.append(w)
            for f in g:
                role = "text" if f["path"] == w["text_src"] else "layout" if f["path"] == w["layout_src"] else \
                    "dup" if any(o["fid"] == f["fid"] and o is not f and o["path"] in (w["text_src"], w["layout_src"])
                                 for o in g) else "alt"
                roles.append((w["id"], role, f["issue"], f["title"], f["path"]))
            for name, rank in w["tags"].items():
                shelves.append((name, w["id"], rank))
        spread_trans(works)
        with self.lock:
            c = self.conn
            c.execute("BEGIN IMMEDIATE")
            try:
                c.execute("DELETE FROM works")
                c.execute("DELETE FROM shelves")
                c.execute("DELETE FROM works_fts")
                c.executemany(
                    "INSERT INTO works(id,kind,title,author,lang,isbn,mag,issue,issue_label,formats,text_src,layout_src,"
                    "n_files,bytes,pages,has_text,tags,sort_title,added,trans) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    [(w["id"], w["kind"], w["title"], w["author"], w["lang"], w["isbn"], w["mag"], w["issue"],
                      w["issue_label"], json.dumps(w["formats"]), w["text_src"], w["layout_src"], w["n_files"],
                      w["bytes"], w["pages"], w["has_text"], json.dumps(w["tags"], ensure_ascii=False),
                      w["sort_title"], added.get(w["id"], now), w["trans"] if w["lang"] == "zh" else None) for w in works])
                c.executemany("INSERT OR REPLACE INTO shelves(name,work,rank) VALUES(?,?,?)", shelves)
                c.executemany("INSERT INTO works_fts(id,title,author,mag,tags) VALUES(?,?,?,?,?)",
                              [(w["id"], w["title"], w["author"], w["mag"], " ".join(w["tags"])) for w in works])
                c.executemany("UPDATE files SET work=?, role=?, issue=?, title=? WHERE path=?", roles)
                c.execute("COMMIT")
            except Exception:
                c.execute("ROLLBACK")
                raise

    # ---- 统计
    def stats(self) -> dict:
        kinds = {r["kind"]: (r["n"], r["b"]) for r in self.q("SELECT kind, COUNT(*) n, SUM(bytes) b FROM works GROUP BY kind")}
        return {"kinds": kinds,
                "shelves": self.one("SELECT COUNT(DISTINCT name) n FROM shelves")["n"],
                "mags": self.one("SELECT COUNT(DISTINCT mag) n FROM works WHERE kind!='book'")["n"]}


def main(argv: list[str]) -> int:
    from .config import load
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname).1s %(message)s", datefmt="%H:%M:%S")
    cfg = load()
    dirs = [str(Path(a).expanduser()) for a in argv] or cfg.sources
    if not dirs:
        print("没有要编目的文件夹：先在「管理」里设置，或在命令行写上文件夹")
        return 1
    cat = Catalog(cfg.data / "catalog.db")
    t0 = time.time()

    def prog(s):
        if s.done % 500 == 0 or s.done == s.total:
            print(f"\r{s.done}/{s.total}  新编目 {s.added}  跳过 {s.skipped}  失败 {s.failed}", end="", flush=True)
    s = cat.scan(dirs, prog)
    print(f"\n完成（{time.time() - t0:.0f} 秒）")
    if s.excluded:
        print(f"排除名单拦下 {s.excluded:,} 个文件，没有编目：python -m library.exclude sweep --apply 移到废纸篓")
    for e in s.errors[-20:]:
        print(f"⚠️ {e['path']}：{e['error']}")
    st = cat.stats()
    for k, (n, b) in st["kinds"].items():
        print(f"  {dict(book='书', magazine='杂志', newspaper='报纸', kids='儿童刊').get(k, k)}：{n:,}")
    print(f"刊名 {st['mags']} 种，书单与榜单 {st['shelves']} 个")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
