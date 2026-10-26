"""从 ECDICT（英汉）+ CMUdict（美式发音）生成精简词典 data/dict.db。

在有网络的机器上运行一次即可：
  python tools/build_dict.py stardict.db cmudict.dict data/dict.db
"""
from __future__ import annotations

import re
import sqlite3
import sys

sys.path.insert(0, __file__.rsplit("/tools/", 1)[0])
from library.phon import arpabet_to_ipa  # noqa: E402

WORD_RE = re.compile(r"^[A-Za-z][A-Za-z'\-]*$")


def load_cmu(path: str) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    for line in open(path, encoding="utf-8"):
        line = line.split("#", 1)[0].strip()
        if not line:
            continue
        head, *ph = line.split()
        w = re.sub(r"\(\d+\)$", "", head).lower()
        out.setdefault(w, []).append(" ".join(ph))
    return out


def main(src: str, cmu_path: str, dst: str) -> None:
    cmu = load_cmu(cmu_path)
    s = sqlite3.connect(src)
    d = sqlite3.connect(dst)
    d.executescript("""
    DROP TABLE IF EXISTS words; DROP TABLE IF EXISTS forms;
    CREATE TABLE words(word TEXT PRIMARY KEY COLLATE NOCASE, us TEXT, uk TEXT, zh TEXT, en TEXT,
                       exchange TEXT, tag TEXT, collins INT, frq INT);
    CREATE TABLE forms(form TEXT COLLATE NOCASE, lemma TEXT, kind TEXT);
    """)
    n = 0
    seen: set[str] = set()
    for word, ph, zh, en, ex, tag, col, ox, frq, bnc in s.execute(
            "SELECT word, phonetic, translation, definition, exchange, tag, collins, oxford, frq, bnc FROM stardict"):
        if not word or not zh or not WORD_RE.match(word):
            continue
        common = (frq or 0) > 0 or (bnc or 0) > 0 or (col or 0) > 0 or (ox or 0) > 0 or (tag or "")
        if not common and word.lower() not in cmu:
            continue
        us = " / ".join(arpabet_to_ipa(p) for p in cmu.get(word.lower(), [])[:2])
        d.execute("INSERT OR IGNORE INTO words VALUES(?,?,?,?,?,?,?,?,?)",
                  (word, us, ph or "", zh.strip(), (en or "").strip()[:400], ex or "", tag or "",
                   col or 0, frq or bnc or 0))
        seen.add(word.lower())
        for part in (ex or "").split("/"):
            if ":" in part:
                k, v = part.split(":", 1)
                if k in "pdi3sr t" and v and v.lower() != word.lower():
                    d.execute("INSERT INTO forms VALUES(?,?,?)", (v, word, k))
        n += 1
    d.execute("CREATE INDEX forms_form ON forms(form)")
    d.commit()
    d.execute("VACUUM")
    print("词条", n)


if __name__ == "__main__":
    main(*sys.argv[1:4])
