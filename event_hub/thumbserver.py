"""灰度缩略图 HTTP 服务:设备详情页按 id 拉图。
GET /thumb/<id>[?w=500]  → 16 级灰度 baseline JPEG(设备 drawJpg 用)
GET /health             → ok
id→本地路径 来自 snapshot.IMAGE_REGISTRY。需要 Pillow。
"""
from __future__ import annotations

import io
import os
import sys
import threading
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
FW_DIR = os.environ.get("M5_FW_DIR") or os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "fw")


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
