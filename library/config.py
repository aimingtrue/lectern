"""配置：默认值 + data/config.json 覆盖。"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


@dataclass
class Config:
    data: Path = ROOT / "data"
    host: str = "127.0.0.1"
    port: int = 8791
    sources: list[str] = field(default_factory=list)   # 书所在的文件夹
    inbox: str = "~/Downloads/下"                         # 新书收件箱：library.ingest 从这里清洗、去重后移进书库
    ollama: str = "http://127.0.0.1:11434"
    model: str = "qwen3.6:35b-a3b-q8_0"                 # 查词/解释：快
    guide_model: str = "qwen3.8:27b-mtp-q8_0"           # 章节导读：质量优先；MTP 版输出与 27b-q8_0 逐字相同，生成快约 70%
    gloss_lang: str = "English"                         # 译文与词义卡的目标语言（"English"、"简体中文"、"Español"…）；设成 English 即英英释义
    tts_url: str = "http://127.0.0.1:8792"              # 朗读音色服务（tts/server.py）
    mini_ssh: str = ""                                  # 「投到 mini」：~/.ssh/config 里 Mac mini 的主机名；空着不显示按钮
    research: str = ""                                  # 资料目录（library/research.py）；空着用仓库里的 research/

    @property
    def db_path(self) -> Path:
        return self.data / "library.db"

    @property
    def dict_path(self) -> Path:
        return self.data / "dict.db"

    def library_root(self) -> Path:
        """放书的主文件夹：「管理」里保存的第一个；没设置时用默认的 ~/Documents/Books。工具脚本都从这里取，不写死路径。"""
        return Path(self.sources[0]).expanduser() if self.sources else Path.home() / "Documents/Books"

    def research_root(self) -> Path:
        """资料（Markdown 研究笔记、调研清单）所在的文件夹。"""
        return Path(self.research).expanduser() if self.research else ROOT / "research"

    @property
    def covers(self) -> Path:
        return self.data / "covers"

    def save(self) -> None:
        p = self.data / "config.json"
        p.parent.mkdir(parents=True, exist_ok=True)
        try:                                             # 文件里别的字段（用户自己填的 nyt_key、google_books_key…）原样保留
            old = json.loads(p.read_text()) if p.exists() else {}
        except ValueError:
            old = {}
        old.update({"sources": self.sources, "port": self.port, "model": self.model, "guide_model": self.guide_model})
        p.write_text(json.dumps(old, ensure_ascii=False, indent=2))


def load() -> Config:
    cfg = Config()
    if os.environ.get("LIBRARY_DATA"):
        cfg.data = Path(os.environ["LIBRARY_DATA"]).expanduser()
    p = cfg.data / "config.json"
    if p.exists():
        for k, v in json.loads(p.read_text()).items():
            if hasattr(cfg, k):
                setattr(cfg, k, v)
    if os.environ.get("LIBRARY_PORT"):
        cfg.port = int(os.environ["LIBRARY_PORT"])
    return cfg
