"""离线英汉词典（ECDICT 精简版 + CMUdict 美式音标）。"""
from __future__ import annotations

import sqlite3
import threading
from pathlib import Path

from . import threadconn

TAGS = {"zk": "中考", "gk": "高考", "cet4": "四级", "cet6": "六级", "ky": "考研", "toefl": "托福",
        "ielts": "雅思", "gre": "GRE"}
KIND = {"p": "过去式", "d": "过去分词", "i": "现在分词", "3": "第三人称单数", "s": "复数", "r": "比较级", "t": "最高级"}


class Dictionary:
    def __init__(self, path: Path):
        self.path = path
        self._local = threading.local()
        self.ok = path.exists()

    @property
    def c(self) -> sqlite3.Connection:
        c = getattr(self._local, "c", None)
        if c is None:
            c = sqlite3.connect(f"file:{self.path}?mode=ro", uri=True, check_same_thread=False)
            c.row_factory = sqlite3.Row
            threadconn.keep(self._local, c)
        return c

    def words(self) -> set[str]:
        if not self.ok:
            return set()
        return {r[0].lower() for r in self.c.execute("SELECT word FROM words")}

    def _get(self, w: str):
        return self.c.execute("SELECT * FROM words WHERE word=?", (w,)).fetchone()

    def lookup(self, raw: str) -> dict | None:
        if not self.ok:
            return None
        w = raw.strip().strip("’'\"“”‘.,;:!?()[]—–-").replace("’", "'")
        if not w:
            return None
        cands = [w, w.lower()]
        if w.lower().endswith("'s"):
            cands.append(w[:-2].lower())
        row, via = None, None
        for c in cands:
            row = self._get(c)
            if row:
                break
        if row is None or (row and not row["zh"]):
            f = self.c.execute("SELECT lemma, kind FROM forms WHERE form=? LIMIT 1", (w.lower(),)).fetchone()
            if f:
                row, via = self._get(f["lemma"]), (f["kind"], w.lower())
        if row is None:
            # 简单词形还原兜底
            lw = w.lower()
            for suf, rep in (("ies", "y"), ("ied", "y"), ("ing", ""), ("ing", "e"), ("ed", ""), ("ed", "e"),
                             ("es", ""), ("s", ""), ("ly", ""), ("er", ""), ("est", "")):
                if lw.endswith(suf) and len(lw) - len(suf) >= 3:
                    row = self._get(lw[: -len(suf)] + rep)
                    if row:
                        via = ("", lw)
                        break
        if row is None:
            return None
        lemma = row["word"]
        out = {
            "word": lemma, "query": raw, "us": row["us"] or "", "uk": row["uk"] or "",
            "zh": [l for l in (row["zh"] or "").split("\n") if l.strip()][:6],
            "en": [l for l in (row["en"] or "").split("\n") if l.strip()][:3],
            "tags": [TAGS[t] for t in (row["tag"] or "").split() if t in TAGS],
            "collins": row["collins"] or 0,
        }
        if via and via[1] != lemma.lower():
            out["form"] = {"of": lemma, "kind": KIND.get(via[0], "词形变化"), "form": via[1]}
            # 变形本身在 CMU 里有读音时优先展示变形的读音
            r2 = self._get(via[1])
            if r2 and r2["us"]:
                out["form"]["us"] = r2["us"]
        return out

