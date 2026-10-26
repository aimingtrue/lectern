"""长任务入口要拿本项目的长任务锁（busy.hold，排队等）：本地模型分类、相似书向量、同一本书判定（比对正文时）。"""
from collections import Counter
from contextlib import contextmanager

import pytest

from library import busy


@pytest.fixture()
def held(monkeypatch, tmp_path):
    monkeypatch.setenv("LIBRARY_DATA", str(tmp_path))
    names = []

    @contextmanager
    def fake_hold(name, wait=False, **kw):
        names.append((name, wait))
        yield
    monkeypatch.setattr(busy, "hold", fake_hold)
    return names


def test_match_build_holds_lock_only_with_content(held, monkeypatch):
    from library import match as M
    st = {"seconds": 0, "works": 0, "pairs": 0, "max_group": 0, "before": (Counter(), Counter()),
          "after": (Counter(), Counter()), "reasons": Counter(), "content": None,
          "decided": Counter(), "final_groups": 0, "final_removable": 0}
    monkeypatch.setattr(M, "build", lambda *a, **k: dict(st))
    M.main(["build", "--no-content"])
    assert held == []
    M.main(["build"])
    assert held == [("同一本书判定", True)]
