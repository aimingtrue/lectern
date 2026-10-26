"""角色配音的全书准备：人物（cast 表）→ 对白归属（speech 表）→ 音色（speakers 表），按章推进，任何书都一样。

  顺带     先定这本书的读法（library/bookkind.py，中文书问一次模型）；论述类的书（map = concepts）先抽读到的这几节的概念导图
  何时跑   ① 按章预生成（library/narrate.py 的 _prep_run）做完当前章的音频、译文之后，接着把这本书其余的章都准备好
           （先往后、再往前），占着同一个长任务锁；② 打开阅读页时这本书还有章没准备，就在后台起一遍（最多 10 分钟一次）；
           ③ 命令行 python -m library voices 书id（给早先读过、没做过的书补）
  让路     后台准备随时可停：用户按播放起一章的预生成、点导读、单独抽人物时，busy_reason() 把它叫停（当前这一次模型调用
           做完就停，最多几分钟），新任务接着做；预生成做完当前章会再把扫全书的活接下去
  不重做   已抽过人物的章不再抽；归属只补没有行的段；只有每一块都成功的范围才记 speech_done。早先版本在整章归属都失败时
           也记了 speech_done（于是整章退化成旁白、再也不重试）：status() 发现「有引号段、一段归属都没有、却记了做完」就把那条
           记录删掉，下次照常补
"""
from __future__ import annotations

import logging
import threading
import time

from . import bookkind, busy, cast, concepts, speech

log = logging.getLogger(__name__)
RETRY_EVERY = 600                 # 打开阅读页触发的后台准备：同一本书多久以内不再起（模型不在时别每次打开都撞一下）


def status(db, b, repair: bool = True) -> list[dict]:
    """每一章的准备情况：人物抽了没、有引号的段数、做了归属的段数、是否记为做完。
    repair：把「有引号段却一段归属都没有、还记了做完」的假记录删掉（见模块说明）。"""
    bid = b["id"]
    have_cast = {r["chapter"] for r in db.q("SELECT chapter FROM cast WHERE book=?", (bid,))}
    attributed = {r["para"] for r in db.q("SELECT para FROM speech WHERE book=?", (bid,))}
    done = db.q("SELECT start, end FROM speech_done WHERE book=?", (bid,))
    out = []
    for i, title, s, e in cast.chapters(b):
        quoted = [p["n"] for p in db.paras_range(bid, s, e) if speech.quotes(p["text"])]
        got = sum(1 for n in quoted if n in attributed)
        d = [x for x in done if x["start"] <= s and x["end"] >= e]
        if repair and d and quoted and not got:
            for x in d:
                db.run("DELETE FROM speech_done WHERE book=? AND start=?", (bid, x["start"]))
            log.info("角色配音：第 %s 项记了做完却一段归属都没有，删掉重做 %s", i, bid)
            d = []
        out.append({"chapter": i, "title": title, "start": s, "end": e, "cast": i in have_cast,
                    "quoted": len(quoted), "attributed": got, "done": bool(d)})
    return out


def todo(row: dict) -> bool:
    """这一章还有活没干：没抽人物，或有引号段还没全部归属、也没记做完。"""
    return not row["cast"] or (row["quoted"] > row["attributed"] and not row["done"])


def order(rows: list[dict], para: int) -> list[dict]:
    """处理顺序：正在读的那一章先，然后往后各章（读者接下来要读的），最后往前补。"""
    k = max((j for j, r in enumerate(rows) if r["start"] <= para), default=0)
    return rows[k:] + rows[:k]


def sweep(st, b, para: int, stop=lambda: False, pool: list[dict] | None = None, exclude: set[str] = frozenset(), progress=None) -> dict:
    """把这本书还没准备的章都准备好（人物 → 归属 → 音色），从 para 所在章起。stop() 为真就在下一次模型调用前停下。
    返回 {"chapters": 做了几章, "stopped": 是否被叫停}。每章出错只记日志，接着做下一章。"""
    db = st.db
    rows = [r for r in order(status(db, b), para) if todo(r)]
    n = 0
    for r in rows:
        if stop():
            return {"chapters": n, "stopped": True}
        try:
            if not r["cast"]:
                cast.extract(st, b, r["chapter"], r["title"], r["start"], r["end"])
            if stop():
                return {"chapters": n, "stopped": True}
            speech.ensure(st, b, r["start"], r["end"], stop=stop, pool=pool, exclude=exclude)
        except Exception as ex:  # noqa: BLE001
            log.warning("角色配音准备失败 %s 第 %s 项：%s", b["id"], r["chapter"], ex)
        n += 1
        if progress:
            progress(r)
    return {"chapters": n, "stopped": stop()}


_tried: dict[str, float] = {}
START_DELAY = 20                  # 打开阅读页后等这么久再起：读者点开就按播放的话，由那一章的预生成接手（它做完会扫全书）


def start(st, b, para: int, delay: float = START_DELAY) -> bool:
    """打开阅读页：delay 秒后，这本书还有章没准备、没有别的模型任务在跑，就起一个后台准备任务（登记成 st.prep_job，
    sweep=True，预生成 / 导读要起时会把它叫停）。排上了返回 True（同一本书 RETRY_EVERY 秒内只排一次）。"""
    bid = b["id"]
    if time.time() - _tried.get(bid, 0) < RETRY_EVERY:
        return False
    _tried[bid] = time.time()

    def run():
        job = st.prep_job
        if (job and job["running"]) or busy.other_jobs():
            return
        kind = bookkind.stored(st.db, bid)
        maps = kind is None or (kind.get("map") == "concepts" and concepts.missing(st.db, b, para))
        if not maps and not any(todo(r) for r in status(st.db, b)):
            return
        voices = st.narrator_voices() if getattr(st, "narrator_voices", None) else ()
        job = st.prep_job = {"running": True, "book": bid, "start": para, "end": para, "voice": tuple(voices), "total": 0,
                             "audio": 0, "tr": 0, "stop": False, "error": "", "done": False, "speech": threading.Event(), "sweep": True}
        try:
            with busy.hold("character voice prep"):
                if bookkind.ensure(st, b).get("map") == "concepts":   # 读法（中文书问一次模型）；论述类的书先抽读到的这几节的概念导图
                    concepts.ensure(st, b, para, stop=lambda: job["stop"])
                r = sweep(st, b, para, stop=lambda: job["stop"], pool=st.tts_voices(), exclude=set(voices))
                log.info("角色配音准备 %s：%d 章%s", bid, r["chapters"], "，被叫停" if r["stopped"] else "")
        except busy.Busy as ex:
            log.info("角色配音准备没起：%s", ex)
        except Exception as ex:  # noqa: BLE001
            log.warning("角色配音准备出错 %s：%s", bid, ex)
        finally:
            job["running"] = False
            job["speech"].set()
    t = threading.Timer(delay, run)
    t.daemon = True
    t.start()
    return True
