"""灰度缩略图 HTTP 服务:设备详情页按 id 拉图。
GET  /thumb/<id>[?w=500]  → 16 级灰度 baseline JPEG(设备 drawJpg 用)
GET  /health              → ok
GET  /fw/version          → OTA 版本号;GET /fw/current.bin → 固件
POST /screen?w=&h=&fmt=   → 设备回传的整屏像素,落成灰度 PNG 到 <repo>/shots/
GET  /shots/latest.png    → 取回最近一张(agent 直接 curl,不用 ssh 捞文件)
id→本地路径 来自 snapshot.IMAGE_REGISTRY。需要 Pillow。
"""
from __future__ import annotations

import io
import os
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))  # 仓库根
try:
    from collectors.snapshot import IMAGE_REGISTRY
except ImportError:
    IMAGE_REGISTRY = {}

DEFAULT_W = 500      # 设备屏宽 540,留边
MAX_H = 620          # 详情页图片区高度上限
_cache: dict = {}    # (id,w,mtime) -> jpeg bytes

# OTA 固件目录:version.txt(整数)+ current.bin。相对仓库根,与 deploy/push_fw.sh 一致。
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FW_DIR = os.environ.get("M5_FW_DIR") or os.path.join(_REPO_ROOT, "fw")

# 设备回传的屏幕截图落这儿(git 忽略)。latest.png 是稳定入口,tools/dump_screen.py 盯它的 mtime。
SHOTS_DIR = os.environ.get("M5_SHOTS_DIR") or os.path.join(_REPO_ROOT, "shots")
MAX_SCREEN_BYTES = 4 * 1024 * 1024   # 960*540*3 ≈ 1.5MB,留足余量兼防炸内存


def _make_thumb(path: str, w: int) -> bytes | None:
    from PIL import Image
    try:
        mt = os.path.getmtime(path)
    except OSError:
        return None
    key = (path, w, int(mt))
    if key in _cache:
        return _cache[key]
    try:
        im = Image.open(path).convert("L")          # 转灰度
        im.thumbnail((w, MAX_H))                     # 等比缩放
        im = im.point(lambda p: round(p / 17) * 17)  # 量化到 16 级,贴近 EPD
        buf = io.BytesIO()
        im.save(buf, format="JPEG", quality=85)
        data = buf.getvalue()
    except Exception:
        return None
    _cache.clear() if len(_cache) > 64 else None
    _cache[key] = data
    return data


def _save_screen(data: bytes, w: int, h: int, fmt: str) -> str:
    """设备回传的整屏像素 → 灰度 PNG,返回落盘路径。

    fmt=bgr888:设备用 M5.Display.readRect 的 void* 重载读出来的,3 字节/像素。
    面板是 grayscale_8bit,三通道必然相等 —— 所以取第 0 通道就是精确的 8bit 灰度,
    也正因如此 BGR/RGB 的字节序在这里无所谓(真要渲染彩色时才需要区分)。
    """
    from PIL import Image
    if fmt != "bgr888":
        raise ValueError(f"不支持的 fmt: {fmt}")
    if len(data) != w * h * 3:
        raise ValueError(f"长度不符:收到 {len(data)},按 {w}x{h}x3 应为 {w * h * 3}")
    im = Image.frombytes("RGB", (w, h), data).getchannel(0)
    os.makedirs(SHOTS_DIR, exist_ok=True)
    path = os.path.join(SHOTS_DIR, time.strftime("screen-%Y%m%d-%H%M%S.png", time.localtime()))
    im.save(path, format="PNG", optimize=True)
    # latest.png 是给 agent / dump_screen.py 的稳定入口。用 replace 保证原子,避免读到半张图。
    latest = os.path.join(SHOTS_DIR, "latest.png")
    tmp = latest + ".tmp"
    im.save(tmp, format="PNG", optimize=True)
    os.replace(tmp, latest)
    return path


class _H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass  # 静音

    def do_GET(self):
        u = urlparse(self.path)
        if u.path == "/health":
            self._send(200, b"ok", "text/plain")
            return
        if u.path == "/fw/version":
            v = "0"
            try:
                with open(os.path.join(FW_DIR, "version.txt")) as fh:
                    v = fh.read().strip() or "0"
            except OSError:
                pass
            self._send(200, v.encode(), "text/plain")
            return
        if u.path == "/fw/current.bin":
            binp = os.path.join(FW_DIR, "current.bin")
            if not os.path.isfile(binp):
                self._send(404, b"no firmware", "text/plain")
                return
            with open(binp, "rb") as fh:
                data = fh.read()
            self._send(200, data, "application/octet-stream")
            return
        if u.path.startswith("/shots/"):
            # 回看设备截图。有了这条,agent 不必 ssh 进来捞文件,curl 就能拿。
            name = os.path.basename(u.path[len("/shots/"):]) or "latest.png"
            p = os.path.join(SHOTS_DIR, name)
            if not name.endswith(".png") or not os.path.isfile(p):
                self._send(404, b"no such shot", "text/plain")
                return
            with open(p, "rb") as fh:
                self._send(200, fh.read(), "image/png")
            return
        if u.path.startswith("/thumb/"):
            img_id = u.path[len("/thumb/"):]
            path = IMAGE_REGISTRY.get(img_id)
            if not path or not os.path.isfile(path):
                self._send(404, b"not found", "text/plain")
                return
            w = int((parse_qs(u.query).get("w") or [DEFAULT_W])[0])
            data = _make_thumb(path, max(64, min(540, w)))
            if not data:
                self._send(500, b"thumb error", "text/plain")
                return
            self._send(200, data, "image/jpeg")
            return
        self._send(404, b"not found", "text/plain")

    def do_POST(self):
        # 设备回传屏幕(cmd=dump 触发)。响应体回文件名,设备串口日志里就能看到落在哪。
        u = urlparse(self.path)
        if u.path != "/screen":
            self._send(404, b"not found", "text/plain")
            return
        q = parse_qs(u.query)
        try:
            n = int(self.headers.get("Content-Length") or 0)
            if not 0 < n <= MAX_SCREEN_BYTES:
                self._send(413, f"bad length {n}".encode(), "text/plain")
                return
            data = self.rfile.read(n)
            w = int((q.get("w") or [0])[0])
            h = int((q.get("h") or [0])[0])
            fmt = (q.get("fmt") or ["bgr888"])[0]
            path = _save_screen(data, w, h, fmt)
        except Exception as e:
            # log_message 是静音的,这里显式打,才会进 /tmp/m5monitor.err.log
            print(f"[screen] 失败: {e}", file=sys.stderr, flush=True)
            self._send(400, str(e).encode(), "text/plain")
            return
        fw = (q.get("fw") or ["?"])[0]
        print(f"[screen] {w}x{h} {n}B fw=v{fw} → {path}", file=sys.stderr, flush=True)
        self._send(200, os.path.basename(path).encode(), "text/plain")

    def _send(self, code, body, ctype):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def start(port: int = 8080):
    srv = ThreadingHTTPServer(("0.0.0.0", port), _H)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    return srv


if __name__ == "__main__":
    import sys
    p = int(sys.argv[1]) if len(sys.argv) > 1 else 8080
    print(f"thumb server on :{p}")
    start(p)
    import time
    while True:
        time.sleep(3600)
