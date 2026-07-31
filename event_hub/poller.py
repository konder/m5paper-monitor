"""远程数据源后台轮询 + TTL 缓存。

为什么必须是后台线程而不是直接调:
  collectors/snapshot.py 的 build_snapshot() 有两个调用方 —— 20s 主循环,**以及
  event_hub/eventserver.py 的 _find_session(),后者在每次 hook POST 时同步调用**。
  往那条路径里加 ssh/HTTP 会让每个 Claude Stop hook 都付上网络延迟。
  所以远程源只在这里拉,主循环只读缓存,build_snapshot 完全不碰网络。

只包**网络**源(litellm / gwtraffic)。故意不包的:
  - codex 额度:collectors/codex_quota 读本地文件,实测 68ms,直接内联即可
  - claude 额度:主循环的 build_snapshot 本来就会算(_scan 一趟同时出 sessions 和 quota),
    再单独缓存就要扫两遍;而且它不能加 TTL —— sessions 驱动 done/needs_input 事件检测,
    缓存会把通知延迟拖到 TTL 那么久

stale 的定义分两层,别混:
  - 本模块的 stale = 「我们这份缓存旧了」(now - last_success > 3*ttl)
  - collectors/gateway 的 fresh 字段 = 「网关那边的 daemon 还活着吗」
"""
from __future__ import annotations

import threading
import time

# 连续失败多久算 stale = ttl 的倍数
STALE_TTL_MULT = 3
# 轮询线程的 tick;各源按自己的 ttl 到期才真的拉
TICK_SEC = 1.0


class Source:
    """一个数据源:名字 + 无参取数函数 + TTL。fn 返回 None 视为失败。"""

    def __init__(self, name: str, fn, ttl: float):
        self.name = name
        self.fn = fn
        self.ttl = float(ttl)
        self.value = None            # 最后一次成功的返回值
        self.last_success = 0.0
        self.last_attempt = 0.0
        self.last_error = ""
        self.fails = 0

    def stale(self, now: float | None = None) -> bool:
        now = now or time.time()
        if self.value is None:
            return True
        return (now - self.last_success) > self.ttl * STALE_TTL_MULT

    def age(self, now: float | None = None) -> float:
        if not self.last_success:
            return -1.0
        return (now or time.time()) - self.last_success


class SourcePoller:
    def __init__(self, sources):
        self._sources = {s.name: s for s in sources}
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread = None

    # ---- 读侧(主循环用)----

    def get(self, name: str):
        """返回最后一次成功的值(可能是旧的);从未成功过则 None。"""
        with self._lock:
            s = self._sources.get(name)
            return s.value if s else None

    def is_stale(self, name: str, now: float | None = None) -> bool:
        with self._lock:
            s = self._sources.get(name)
            return True if s is None else s.stale(now)

    def status(self) -> dict:
        """给日志用的一行摘要。"""
        now = time.time()
        with self._lock:
            return {
                n: {"ok": s.value is not None, "age": round(s.age(now), 1),
                    "stale": s.stale(now), "fails": s.fails, "err": s.last_error[:60]}
                for n, s in self._sources.items()
            }

    # ---- 轮询侧 ----

    def _poll_one(self, s: Source, now: float):
        s.last_attempt = now
        try:
            v = s.fn()
        except Exception as e:          # 采集器自己该吞异常,这里是兜底
            v = None
            err = f"{type(e).__name__}: {e}"
        else:
            err = "" if v is not None else "returned None"
        with self._lock:
            if v is not None:
                s.value = v
                s.last_success = now
                s.fails = 0
                s.last_error = ""
            else:
                s.fails += 1
                s.last_error = err

    def _run(self):
        while not self._stop.is_set():
            now = time.time()
            for s in list(self._sources.values()):
                if now - s.last_attempt >= s.ttl:
                    self._poll_one(s, now)
                    now = time.time()      # 拉一次可能耗几百毫秒,刷新 now
            self._stop.wait(TICK_SEC)

    def start(self):
        self._thread = threading.Thread(target=self._run, name="SourcePoller", daemon=True)
        self._thread.start()
        return self

    def prime(self, timeout: float = 10.0):
        """阻塞式先拉一轮,让第一帧就有数据(否则首次发布会是一屏 stale)。

        每个源都只试一次,总耗时上限 timeout;超时就带着缺的那些继续走。
        """
        deadline = time.time() + timeout
        for s in list(self._sources.values()):
            if time.time() >= deadline:
                break
            self._poll_one(s, time.time())

    def stop(self):
        self._stop.set()
