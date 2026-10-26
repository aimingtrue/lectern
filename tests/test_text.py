"""断句（全站唯一的规则）与行尾断字拼接。"""
from __future__ import annotations

import pytest

from library.text import ends_sentence, join_lines, sentences, split, utf16_spans

CASES = [
    # 普通句子
    ("It was late. We went home.", ["It was late.", "We went home."]),
    ("Stop! Who goes there? Nobody.", ["Stop!", "Who goes there?", "Nobody."]),
    ("No terminal punctuation here", ["No terminal punctuation here"]),
    ("CHAPTER I", ["CHAPTER I"]),
    ("", []),
    ("   ", []),
    ("  Leading and trailing spaces.  ", ["Leading and trailing spaces."]),
    # 对话与说话人
    ("“Do they miss me?” she cried ecstatically. “The whole town is desolate.”",
     ["“Do they miss me?” she cried ecstatically.", "“The whole town is desolate.”"]),
    ("“What village?” he asked helplessly.", ["“What village?” he asked helplessly."]),
    ("“Whenever you feel like criticizing anyone,” he told me, “just remember that.”",
     ["“Whenever you feel like criticizing anyone,” he told me, “just remember that.”"]),
    ("“You are!” He took down his drink.", ["“You are!”", "He took down his drink."]),
    ("“Why?” “Because.”", ["“Why?”", "“Because.”"]),
    ("“It was … simply amazing,” she said.", ["“It was … simply amazing,” she said."]),
    ("“Stop!” — he shouted it twice.", ["“Stop!” — he shouted it twice."]),
    ("He said, “Go!” and ran.", ["He said, “Go!” and ran."]),
    ("“Well, you ought to see her. She’s—”", ["“Well, you ought to see her.", "She’s—”"]),
    ('"Yes," he said. "Of course."', ['"Yes," he said.', '"Of course."']),
    # 嵌套引号（中间隔空格）
    ("“He said ‘go away.’ ” Then he left.", ["“He said ‘go away.’ ”", "Then he left."]),
    ("“ ‘All right,’ says Rosy.” He got up.", ["“ ‘All right,’ says Rosy.”", "He got up."]),
    # 括号
    ("(He was gone.) Nobody noticed.", ["(He was gone.)", "Nobody noticed."]),
    ("He left (quietly!) and shut the door.", ["He left (quietly!) and shut the door."]),
    # 省略号
    ("Well… I don’t know.", ["Well…", "I don’t know."]),
    ("I thought … maybe not.", ["I thought … maybe not."]),
    ("Wait . . . what? No!", ["Wait . . . what?", "No!"]),
    ("Really?! Yes.", ["Really?!", "Yes."]),
    # 称谓缩写
    ("Mr. and Mrs. Buchanan came. They sat.", ["Mr. and Mrs. Buchanan came.", "They sat."]),
    ("I had expected that Mr. Gatsby would be old.", ["I had expected that Mr. Gatsby would be old."]),
    ("Dr. Watson and St. Paul met Prof. Higgins.", ["Dr. Watson and St. Paul met Prof. Higgins."]),
    ("See e.g. the appendix. Or i.e. the notes.", ["See e.g. the appendix.", "Or i.e. the notes."]),
    # 名字缩写
    ("The eyes of Doctor T. J. Eckleburg are blue. They watch.", ["The eyes of Doctor T. J. Eckleburg are blue.", "They watch."]),
    ("John D. Rockefeller was rich. F. Scott Fitzgerald wrote.", ["John D. Rockefeller was rich.", "F. Scott Fitzgerald wrote."]),
    ("So am I. Then she left.", ["So am I.", "Then she left."]),
    # 时间、金额、小数、编号
    ("We met at 5 p.m. The sun was low.", ["We met at 5 p.m.", "The sun was low."]),
    ("It starts at 9 a.m. sharp. Be there.", ["It starts at 9 a.m. sharp.", "Be there."]),
    ("It cost $5.00 and weighed 3.14 pounds. Cheap.", ["It cost $5.00 and weighed 3.14 pounds.", "Cheap."]),
    ("He came in 1915. I stayed.", ["He came in 1915.", "I stayed."]),
    ("See No. 5 for details. No. That is wrong.", ["See No. 5 for details.", "No.", "That is wrong."]),
    ("apples, pears, etc. The list goes on.", ["apples, pears, etc.", "The list goes on."]),
    ("apples, pears, etc. and more.", ["apples, pears, etc. and more."]),
    ("He lived in the U.S. The next day he left.", ["He lived in the U.S.", "The next day he left."]),
    # 网址、邮箱、域名
    ("Visit standardebooks.org/ebooks for more. Thanks!", ["Visit standardebooks.org/ebooks for more.", "Thanks!"]),
    ("Write to a.b@example.com. Then wait.", ["Write to a.b@example.com.", "Then wait."]),
    ("Go to https://www.gutenberg.org/ebooks/64317. It is free.", ["Go to https://www.gutenberg.org/ebooks/64317.", "It is free."]),
]


@pytest.mark.parametrize("text,want", CASES)
def test_split(text, want):
    assert split(text) == want


@pytest.mark.parametrize("text,_", CASES)
def test_spans_are_trimmed_and_ordered(text, _):
    spans = sentences(text)
    last = 0
    for a, b in spans:
        s = text[a:b]
        assert s == s.strip() and s, (text, s)
        assert a >= last
        last = b
        assert any(c.isalnum() for c in s), "不能产生只有引号/标点的碎片"


def test_never_a_quote_only_span():
    for t in ["He said “Yes.” ”", "“Go!” ’", "Fine. ”", "End. …"]:
        assert all(any(c.isalnum() for c in t[a:b]) for a, b in sentences(t)), t


def test_ends_sentence():
    assert ends_sentence("obvious suppressions.")
    assert ends_sentence("“Never.”") and ends_sentence("She’s—”") and ends_sentence("he said:")
    assert not ends_sentence("and Mr.") and not ends_sentence("Doctor T.") and not ends_sentence("the end of")
    assert not ends_sentence("") and ends_sentence("Really?!")


def test_utf16_spans():
    t = "Go 😀 now. Then stop."
    spans = sentences(t)
    assert [t[a:b] for a, b in spans] == ["Go 😀 now.", "Then stop."]
    js = lambda x: len(x.encode("utf-16-le")) // 2   # noqa: E731  JS 字符串长度
    u = utf16_spans(t, spans)
    assert u == [[js(t[:a]), js(t[:b])] for a, b in spans] and u[0][1] == spans[0][1] + 1   # emoji 在 JS 里占 2 个单位
    assert utf16_spans("Plain. Text.", sentences("Plain. Text.")) == [[0, 6], [7, 12]]


def test_join_lines():
    words = {"vulnerable", "livingroom"}
    # 这本书用 ‐ 断字（soft=True）：‐ 去掉；行尾 ASCII '-' 是真连字符
    assert join_lines("win‐", "dows", True) == "windows"
    assert join_lines("living-", "room", True, words) == "living-room"
    assert join_lines("Fifty-", "Ninth", True) == "Fifty-Ninth"
    # 没有 ‐ 的书：词典里有合成词才去掉连字符
    assert join_lines("vulner-", "able", False, words) == "vulnerable"
    assert join_lines("well-", "known", False, words) == "well-known"
    assert join_lines("Fitz-", "Peters", False, words) == "Fitz-Peters"
    assert join_lines("soft­", "ware", False) == "software"
    assert join_lines("unsought—", "frequently", False) == "unsought—frequently"
    assert join_lines("a dash -", "here", False) == "a dash - here"
    assert join_lines("and Mr.", "Albrucksburger", True) == "and Mr. Albrucksburger"


# ---- 中文：。！？… 后面没有空格也断；引号里的短句读到收尾引号；后面紧跟逗号不断；超长句切到 150 字以内
@pytest.mark.parametrize("text, want", [
    ("他走了。她说：“拜呵！拜呵！”许多人失望？是的……好。",
     ["他走了。", "她说：“拜呵！拜呵！”", "许多人失望？", "是的……", "好。"]),
    ("“你来了。”他说，“坐吧。”", ["“你来了。”", "他说，“坐吧。”"]),
    ("“真的吗？”，她问。", ["“真的吗？”，她问。"]),
    ("使用 Python 写程序很方便。Mr. Smith 来了。", ["使用 Python 写程序很方便。", "Mr. Smith 来了。"]),
    ("He said 道 means way. Then he left.", ["He said 道 means way.", "Then he left."]),   # 英文为主：照英文规则
])
def test_split_zh(text, want):
    assert split(text) == want


def test_split_zh_long():
    toc = "第一卷影子部队第一章要案" * 60            # 连标点都没有的长串：按字数硬切
    assert all(len(s) <= 150 for s in split(toc)) and "".join(split(toc)) == toc
    long = "，".join(["这是一段很长很长的没有句号的文字"] * 30) + "。"
    parts = split(long)
    assert len(parts) > 1 and all(len(s) <= 200 for s in parts) and "".join(parts) == long
