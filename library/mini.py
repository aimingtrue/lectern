"""投到 Mac mini：书库只听 127.0.0.1，另一个房间的 mini 经反向 SSH 隧道访问。

阅读页点「投到 mini」先存进度，这里确保隧道在，再让 mini 的 Chrome 打开同一本书——进度存在服务端，
打开就停在刚才那一句。mini 上已有书库的标签页就原地换成这本书（不越开越多，也不会两边同时出声），没有才新开。

隧道由书库进程自己起（启动时就接上，`ssh -N -R`，断了 10 s 后重连）；mini 上 8791 已经通（别处起的隧道）就不再起第二条。
mini_ssh 是 ~/.ssh/config 里的主机名（data/config.json 设置），空着就不显示按钮。"""
from __future__ import annotations

import atexit
import logging
import subprocess
import threading
import time

from fastapi import FastAPI, HTTPException, Request

log = logging.getLogger(__name__)

SSH_OPTS = ["-o", "BatchMode=yes", "-o", "ConnectTimeout=8"]
TUNNEL_OPTS = ["-N", "-o", "ExitOnForwardFailure=yes", "-o", "ServerAliveInterval=30", "-o", "ServerAliveCountMax=3"]

# 在 mini 上跑（osascript - 地址 前缀）：找已开着书库的标签页换地址，没有就新开；先点亮屏幕
OPEN_SCRIPT = """on run argv
  set u to item 1 of argv
  set p to item 2 of argv
  tell application "Google Chrome"
    activate
    repeat with w in windows
      set i to 0
      repeat with t in tabs of w
        set i to i + 1
        if (URL of t) starts with p then
          set URL of t to u
          set active tab index of w to i
          set index of w to 1
          return "reused"
        end if
      end repeat
    end repeat
    if (count of windows) = 0 then
      make new window
      set URL of active tab of front window to u
    else
      tell front window to make new tab with properties {URL:u}
    end if
    return "new"
  end tell
end run
"""

# ssh 报错 → 该去哪儿查（两台路由器内网同为 192.168.0.x，只有 WAN 口 192.168.1.69:2222 这一条路）
HINTS = [
    ("Connection refused", "mini 房间路由器的 2222 转发没落到 mini 的 sshd：mini 可能换了内网 IP，或「共享 → 远程登录」关了"),
    ("timed out", "连不上 mini：mini 睡眠、关机、停在登录界面，或 mini 房间路由器断网"),
    ("No route to host", "连不上 mini 房间的路由器：它断电、断网，或光猫换了它的 WAN IP"),
    ("Could not resolve hostname", "~/.ssh/config 里没有这个主机名"),
    ("Permission denied", "mini 不认本机的 SSH 密钥"),
]


def hint(err: str) -> str:
    for k, h in HINTS:
        if k.lower() in err.lower():
            return h
    return (err.strip().splitlines() or ["ssh 失败"])[-1][:200]


class Mini:
    def __init__(self, ssh_host: str, port: int):
        self.host, self.port = ssh_host, port
        self.base = f"http://127.0.0.1:{port}/"
        self.proc: subprocess.Popen | None = None
        self.lock = threading.Lock()
        self.keeper: threading.Thread | None = None
        atexit.register(self.close)

    def ssh(self, cmd: str, timeout: float = 20, stdin: str | None = None) -> subprocess.CompletedProcess:
        return subprocess.run(["ssh", *SSH_OPTS, self.host, cmd], input=stdin, capture_output=True, text=True,
                              timeout=timeout)

    def remote_ok(self) -> tuple[bool, str]:
        """mini 上能不能访问到书库；连不上 mini 时第二项是 ssh 的报错。"""
        try:
            r = self.ssh(f"curl -s -m 4 --noproxy '*' {self.base}api/health", timeout=15)
        except subprocess.TimeoutExpired:
            return False, "timed out"
        if r.returncode == 255:                         # ssh 自己失败（不是 curl）
            return False, r.stderr
        return '"ok":true' in r.stdout.replace(" ", ""), ""

    def alive(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def spawn(self) -> None:
        fwd = f"127.0.0.1:{self.port}:127.0.0.1:{self.port}"
        self.proc = subprocess.Popen(["ssh", *SSH_OPTS, *TUNNEL_OPTS, "-R", fwd, self.host],
                                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                                     text=True)
        log.info("mini 隧道已起 pid=%s", self.proc.pid)

    def start(self) -> None:
        """书库启动时就接上隧道并一直看着（serve 调；不必等第一次投送——书库重启后 mini 上的页面立刻能续上）。"""
        if self.keeper is None:
            self.keeper = threading.Thread(target=self.keep, name="mini-tunnel", daemon=True)
            self.keeper.start()

    def keep(self) -> None:
        """隧道断了（睡眠醒来、换网络）10 s 后重连；mini 上已经通（别处的隧道）就不起；mini 连不上一分钟后再看。"""
        wait = 0
        while True:
            time.sleep(wait)
            wait = 10
            with self.lock:
                if self.alive():
                    continue
                if self.proc is not None:
                    err = (self.proc.stderr.read() if self.proc.stderr else "") or ""
                    self.proc = None
                    if err.strip():
                        log.info("mini 隧道断了：%s", err.strip().splitlines()[-1][:200])
                ok, err = self.remote_ok()
                if ok:
                    continue
                if err:
                    wait = 60
                    continue
                self.spawn()

    def ensure(self) -> str:
        """mini 能访问到书库就返回：已通 / 新建；不通抛 502 并说该去哪儿查。"""
        with self.lock:
            ok, err = self.remote_ok()
            if ok:
                return "已通"
            if err:
                raise HTTPException(502, hint(err))
            if not self.alive():
                self.spawn()
            self.start()
            for _ in range(10):
                time.sleep(1)
                if not self.alive():
                    err = self.proc.stderr.read() if self.proc and self.proc.stderr else ""
                    raise HTTPException(502, "could not open the tunnel: " + hint(err or "ssh 退出"))
                if self.remote_ok()[0]:
                    return "新建"
            raise HTTPException(502, "tunnel is up but the library is still unreachable on the mini")

    def open(self, path: str) -> str:
        url = self.base + path.lstrip("/")
        try:
            r = self.ssh(f"caffeinate -u -t 2 >/dev/null 2>&1 & osascript - '{url}' '{self.base}'", stdin=OPEN_SCRIPT)
        except subprocess.TimeoutExpired:
            raise HTTPException(504, "Chrome on the mini is not responding")
        if r.returncode != 0:
            raise HTTPException(502, "could not open Chrome on the mini: " + hint(r.stderr))
        return r.stdout.strip()

    def close(self) -> None:
        if self.alive():
            self.proc.terminate()


def register(app: FastAPI, st) -> None:
    mini = Mini(st.cfg.mini_ssh, st.cfg.port) if st.cfg.mini_ssh else None
    st.mini = mini

    @app.post("/api/mini/open")
    async def open_on_mini(req: Request):
        if mini is None:
            raise HTTPException(404, "no Mac mini configured (mini_ssh in data/config.json)")
        d = await req.json()
        bid = str(d.get("book", ""))
        st.need(bid)                                     # 只接受书库里的书 id，拼不进别的东西
        from .web import run_in_thread

        def go():
            t = time.time()
            tunnel = mini.ensure()
            how = mini.open(f"/read/{bid}")
            return {"ok": True, "tunnel": tunnel, "tab": how, "sec": round(time.time() - t, 1)}
        return await run_in_thread(go)
