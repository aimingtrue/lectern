"""全部测试共用的设置。"""
import pytest


@pytest.fixture(autouse=True)
def fresh_stamps(monkeypatch):
    """测试里改了数据马上就要看到：缓存版本戳不设最短间隔（线上 30 秒，见 library/stamp.py）。"""
    from library import stamp
    monkeypatch.setattr(stamp, "TTL", 0.0)


@pytest.fixture(autouse=True)
def fresh_busy_cache():
    """长任务锁里 pgrep 的结果缓存 5 秒（library/busy.py）：每个用例从空缓存开始，用例之间互不影响。"""
    from library import busy
    busy._cache.clear()
    yield
    busy._cache.clear()
