"""本地 Ollama 调用（流式读取，长输出不会因总时长超时）。"""
from __future__ import annotations

import json
import re

import httpx


class LLMError(RuntimeError):
    pass


class Ollama:
    def __init__(self, base: str, timeout: float = 300):
        self.client = httpx.Client(base_url=base, timeout=timeout, trust_env=False)

    def ok(self) -> bool:
        try:
            return self.client.get("/api/tags", timeout=2).status_code == 200
        except Exception:
            return False

    def chat(self, model: str, system: str, user: str, *, num_ctx: int = 8192, temperature: float = 0.3,
             json_mode: bool = False, timeout: float | None = None) -> str:
        """timeout：这一次调用的总时长上限（秒），不给用客户端默认的 300 s。人物抽取、对白归属用 120 s，超时就跳过那一块。"""
        body = {"model": model, "stream": True, "think": False, "keep_alive": "30m",
                "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
                "options": {"num_ctx": num_ctx, "temperature": temperature}}
        if json_mode:
            body["format"] = "json"
        out = []
        try:
            with self.client.stream("POST", "/api/chat", json=body, timeout=timeout) as r:
                if r.status_code >= 400:
                    r.read()
                    raise LLMError(f"HTTP {r.status_code}: {r.text[:200]}")
                for line in r.iter_lines():
                    if not line.strip():
                        continue
                    d = json.loads(line)
                    if d.get("error"):
                        raise LLMError(d["error"])
                    out.append(d.get("message", {}).get("content", ""))
                    if d.get("done"):
                        break
        except httpx.HTTPError as e:
            raise LLMError(f"本地模型不可用：{e}") from e
        return re.sub(r"<think>.*?</think>", "", "".join(out), flags=re.S).strip()

    def chat_json(self, model: str, system: str, user: str, **kw) -> dict:
        txt = self.chat(model, system, user, json_mode=True, **kw)
        txt = re.sub(r"^```(?:json)?\s*|\s*```$", "", txt.strip())
        try:
            return json.loads(txt)
        except json.JSONDecodeError:
            m = re.search(r"\{.*\}", txt, re.S)
            if m:
                return json.loads(m.group(0))
            raise LLMError("模型返回的不是 JSON")
