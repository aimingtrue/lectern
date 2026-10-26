"""一本书的摘要：入库时从解析结果算好，存进 library.db 的 digest 表。全文（paras）只给打开阅读过的书存，
同一本书的正文比对、去重、模型分类、确认页、质量体检都只读摘要，不必为几万本书存下全部正文。

  head    正文开头（跳过书名、作者行、版权页、目录、全大写的标题行和乱码），最多 HEAD_CHARS + 1 字
  sketch  全书正文切片的 bottom-k 指纹（library.match.sketch，稳定哈希，跨进程可比），8 字节一个；正文太短为 NULL
  gate    质量体检指标（tools/quality_gate.py）：total w tiny repl zh sense spaced latin1 teaser

常用汉字表 common_han.txt（质量体检判断中文乱码用）由 build_common 从已入库的书里统计一次，随代码提交。
"""
from __future__ import annotations

import json
import random
import re
import sqlite3
import unicodedata
from collections import Counter
from pathlib import Path

HEAD_CHARS = 2000                # 模型分类读 2000 字（classify.TEXT_CHARS），确认页 120 字
HEAD_PARAS = 400                 # 开头只在前 400 段里找
COMMON_FILE = Path(__file__).with_name("common_han.txt")

# 质量体检用（规则见 tools/quality_gate.py）
TEASER = re.compile(r"点击此处查看更多内容|查看更多内容|试读结束|免费试读|ERROR: cannot find chapter|"
                    r"end of (?:this )?sample|buy the (?:full )?book", re.I)
CJK_GAP = re.compile(r"(?<=[㐀-鿿])[ 　]+(?=[㐀-鿿])")
WORDY = re.compile(r"[^\W\d_]{3,}")
HAN = re.compile(r"[㐀-鿿]")
LATIN = re.compile(r"[A-Za-z]")


# ---------------------------------------------------------------- 开头
def head_text(texts, n: int = HEAD_CHARS) -> str:
    """正文段落（kind='p'，按顺序）里跳过前置内容，拼到超过 n 字为止（不截断）。"""
    from .match import FRONT, GARBLED, TOC
    out = ""
    for t in texts:
        t = re.sub(r"\s+", " ", t or "").strip()
        k = unicodedata.normalize("NFKC", t)
        caps = sum(c.isupper() for c in k) > 0.6 * max(1, sum(c.isalpha() and c.isascii() for c in k)) > 20
        if len(t) < 30 or FRONT.search(t) or FRONT.search(k) or len(TOC.findall(k)) >= 2 or caps or \
                len(GARBLED.findall(t)) > len(t) // 4:
            continue
        out += t + " "
        if len(out) > n:
            break
    return out.strip()


# ---------------------------------------------------------------- 指纹
def body_text(texts):
    """正文（归一化后）去掉前 10%（版权页、序、目录）和后 5%（后记、广告）；太短返回 None。"""
    from .match import MIN_BODY, norm_text
    t = norm_text(" ".join(x or "" for x in texts))
    t = t[len(t) // 10: len(t) - len(t) // 20]
    return t if len(t) >= (MIN_BODY if isinstance(t, str) else MIN_BODY // 4) else None


def pack(sk: list[int]) -> bytes:
    return b"".join(h.to_bytes(8, "big") for h in sk)


def unpack(b: bytes) -> list[int]:
    return [int.from_bytes(b[i:i + 8], "big") for i in range(0, len(b), 8)]


# ---------------------------------------------------------------- 质量体检指标
def _len(t: str) -> int:
    """和 SQLite 的 length() 一样只数到第一个 NUL（体检和去重原先按 paras 表用 SQL 算，口径保持不变）。"""
    k = t.find("\x00")
    return len(t) if k < 0 else k


def gate(paras: list[tuple[str, str]], common: set | None) -> dict:
    """paras：全书 (kind, text)。与 tools/quality_gate.py 原先按 paras 表现算的结果相同。
    common 为 None（没有常用汉字表）时中文书的 sense 记 None（不判乱码）。"""
    n = len(paras)
    texts = [t or "" for _, t in paras]
    total = sum(_len(t) for t in texts)
    tiny = sum(_len(t) <= 3 for t in texts) / n if n else 0
    repl = sum(t.count("�") for t in texts)
    step = max(1, n // 300)
    raw = " ".join(texts[i] for i in range(0, n, step))
    han = len(HAN.findall(raw))
    spaced = len(CJK_GAP.findall(raw)) / max(1, han)            # 中文字之间带空格的比例
    sample = CJK_GAP.sub("", raw)
    nonspace = [ch for ch in sample if not ch.isspace()]
    latin1 = sum(0x80 <= ord(ch) <= 0xff for ch in nonspace) / max(1, len(nonspace))   # GBK 当 Latin-1 读的乱码
    letters = sum(ch.isalpha() for ch in sample)
    zh = han > 0.3 * max(1, letters)
    if zh:                                                       # 中文：生僻字比例（识别错的扫描文字满篇怪字）
        h = HAN.findall(sample)
        if len(h) < 1000:
            sense = 1.0
        else:
            sense = None if common is None else 1 - sum(1 for ch in h if ch not in common) / len(h)
    else:                                                        # 西文：像单词的词元比例
        toks = [t for t in sample.split() if any(ch.isalnum() or ch == "�" for ch in t)]
        sense = sum(1 for t in toks if WORDY.search(t)) / max(1, len(toks))
    head = " ".join(texts[:60])
    # w：有效字数（汉字按 2.5 个字母计），和质量体检算原文件完整度的口径一致；没存全文的书拿它比
    w = sum(len(HAN.findall(t)) * 2.5 + len(LATIN.findall(t)) for t in texts)
    return {"total": total, "w": w, "tiny": round(tiny, 3), "repl": round(repl / max(1, total), 4), "zh": zh,
            "sense": None if sense is None else round(sense, 3), "spaced": round(spaced, 3), "latin1": round(latin1, 3),
            "teaser": bool(TEASER.search(head)) and total < 20000}


_common: set | None = None


def common_han() -> set | None:
    global _common
    if _common is None and COMMON_FILE.exists():
        _common = set(COMMON_FILE.read_text().strip())
    return _common


def build_common(conn: sqlite3.Connection) -> set:
    """常用汉字表：书库 600 本中文书里至少出现于 3 本的字（繁体书、古籍的常用字也会进表，一本书里的怪字不会）。
    每本书取均匀分布的约 100 段。"""
    cnt: Counter = Counter()
    rows = conn.execute("SELECT id, n_paras FROM books WHERE status='ok' AND n_paras>100").fetchall()
    random.Random(3).shuffle(rows)
    used = 0
    for bid, n in rows:
        step = max(1, n // 100)
        raw = " ".join(r[0] for r in conn.execute("SELECT text FROM paras WHERE book=? AND n % ? = 0", (bid, step)))
        h = HAN.findall(raw)
        if len(h) > 2000:
            cnt.update(set(h))
            used += 1
            if used >= 600:
                break
    return {ch for ch, k in cnt.items() if k >= 3}


# ---------------------------------------------------------------- 合起来
def compute(paras: list[dict]) -> dict:
    """paras：解析结果 [{"kind","text",...}]。返回 digest 表的一行（不含 book）。"""
    from .match import sketch
    body = [p["text"] for p in paras if p["kind"] == "p"]
    b = body_text(body)
    return {"head": head_text(body[:HEAD_PARAS]),
            "sketch": pack(sketch(b)) if b else None,
            "gate": json.dumps(gate([(p["kind"], p["text"]) for p in paras], common_han()), separators=(",", ":"))}


DIGEST_SCHEMA = "CREATE TABLE IF NOT EXISTS digest(book TEXT PRIMARY KEY, head TEXT, sketch BLOB, gate TEXT) WITHOUT ROWID;"


def ensure_db(path: Path) -> None:
    """建好 data/digest.db（表、WAL）。library.db 打开时调用。"""
    c = sqlite3.connect(path, timeout=30)
    try:
        c.execute("PRAGMA journal_mode=WAL")
        c.execute(DIGEST_SCHEMA)
        c.commit()
    finally:
        c.close()


def attach(conn: sqlite3.Connection) -> bool:
    """把 library.db 旁边的 digest.db 挂到这个连接上（名字 dg），SQL 里不带前缀的 digest 就指它。
    已挂过、或 main 里还有旧的 digest 表（没升到版本 4 的库）都没关系；挂不上（只读连接且文件不存在）返回 False。"""
    dbs = {r[1]: r[2] for r in conn.execute("PRAGMA database_list")}
    if "dg" in dbs:
        return True
    main = dbs.get("main") or ""
    if not main:
        return False
    path = Path(main).with_name("digest.db")
    try:
        if not path.exists():
            ensure_db(path)
        conn.execute("ATTACH DATABASE ? AS dg", (str(path),))
        try:
            conn.execute("CREATE TABLE IF NOT EXISTS dg.digest(book TEXT PRIMARY KEY, head TEXT, sketch BLOB, gate TEXT) WITHOUT ROWID")
        except sqlite3.OperationalError:          # 只读连接：表已经有了
            pass
        return True
    except sqlite3.Error:
        return False


def read(conn: sqlite3.Connection, bid: str):
    """一本书的摘要 (head, sketch, gate)；没有（还没算、或库里还没有 digest 表）返回 None。"""
    attach(conn)
    try:
        r = conn.execute("SELECT head, sketch, gate FROM digest WHERE book=?", (bid,)).fetchone()
    except sqlite3.OperationalError:
        return None
    return None if r is None else {"head": r[0], "sketch": r[1], "gate": r[2]}


def lengths(conn: sqlite3.Connection) -> dict[str, int]:
    """每本书的正文字数：有摘要的读摘要，没有的按 paras 现算。"""
    attach(conn)
    try:
        rows = conn.execute("SELECT b.id, COALESCE(json_extract(d.gate, '$.total'), "
                            "(SELECT SUM(length(text)) FROM paras WHERE book=b.id)) FROM books b "
                            "LEFT JOIN digest d ON d.book=b.id").fetchall()
    except sqlite3.OperationalError:            # 旧库：还没有 digest 表
        rows = conn.execute("SELECT book, SUM(length(text)) FROM paras GROUP BY book").fetchall()
    return {r[0]: r[1] for r in rows if r[1] is not None}
