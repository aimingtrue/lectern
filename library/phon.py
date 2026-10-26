"""CMUdict ARPAbet → 美式 IPA（带重音符号）。"""
from __future__ import annotations

VOWELS = {
    "AA": "ɑ", "AE": "æ", "AH": "ʌ", "AO": "ɔ", "AW": "aʊ", "AY": "aɪ", "EH": "ɛ", "ER": "ɝ",
    "EY": "eɪ", "IH": "ɪ", "IY": "i", "OW": "oʊ", "OY": "ɔɪ", "UH": "ʊ", "UW": "u",
}
CONS = {
    "B": "b", "CH": "tʃ", "D": "d", "DH": "ð", "F": "f", "G": "ɡ", "HH": "h", "JH": "dʒ", "K": "k",
    "L": "l", "M": "m", "N": "n", "NG": "ŋ", "P": "p", "R": "r", "S": "s", "SH": "ʃ", "T": "t",
    "TH": "θ", "V": "v", "W": "w", "Y": "j", "Z": "z", "ZH": "ʒ",
}
# 英语里合法的词首辅音丛（用于决定重音符号放在哪个辅音之前）
ONSETS = {
    ("P", "L"), ("P", "R"), ("T", "R"), ("T", "W"), ("K", "L"), ("K", "R"), ("K", "W"), ("B", "L"),
    ("B", "R"), ("D", "R"), ("D", "W"), ("G", "L"), ("G", "R"), ("F", "L"), ("F", "R"), ("TH", "R"),
    ("SH", "R"), ("S", "P"), ("S", "T"), ("S", "K"), ("S", "M"), ("S", "N"), ("S", "L"), ("S", "W"),
    ("S", "F"), ("P", "Y"), ("B", "Y"), ("K", "Y"), ("M", "Y"), ("F", "Y"), ("V", "Y"), ("HH", "Y"),
    ("S", "P", "L"), ("S", "P", "R"), ("S", "T", "R"), ("S", "K", "R"), ("S", "K", "W"), ("S", "P", "Y"),
    ("S", "K", "Y"),
}


def _sym(p: str) -> tuple[str, int | None]:
    base = p.rstrip("012")
    stress = int(p[-1]) if p[-1].isdigit() else None
    if base in VOWELS:
        v = VOWELS[base]
        if base == "AH" and stress == 0:
            v = "ə"
        if base == "ER" and stress == 0:
            v = "ɚ"
        return v, stress
    return CONS.get(base, base.lower()), None


def arpabet_to_ipa(pron: str) -> str:
    phones = pron.split()
    syms = [_sym(p) for p in phones]
    marks: dict[int, str] = {}
    last_vowel = -1
    for i, (p, (_, st)) in enumerate(zip(phones, syms)):
        if st is None:
            continue
        if st in (1, 2):
            # 向前找辅音，取最长合法词首
            j = i
            cons = []
            while j - 1 > last_vowel and syms[j - 1][1] is None:
                j -= 1
                cons.insert(0, phones[j].rstrip("012"))
            start = i
            if cons:
                start = i - 1
                for k in range(len(cons), 0, -1):
                    if k == 1 or tuple(cons[-k:]) in ONSETS:
                        start = i - k
                        break
                if last_vowel == -1:  # 词首：所有辅音都属于这个音节
                    start = 0
            marks[start] = "ˈ" if st == 1 else "ˌ"
        last_vowel = i
    out = []
    for i, (s, _) in enumerate(syms):
        out.append(marks.get(i, "") + s)
    txt = "".join(out)
    if txt.count("ˈ") == 1 and len([1 for _, st in syms if st is not None]) == 1:
        txt = txt.replace("ˈ", "")  # 单音节词不标重音
    return f"/{txt}/"
