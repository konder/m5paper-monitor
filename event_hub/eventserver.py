"""事件接收:hook 脚本 POST 到这里,补全结果摘要后经回调发到 MQTT。
POST /event?kind=done|needs_input&source=claude|codex   body=hook 的 stdin JSON
"""
from __future__ import annotations

import json
import os
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))  # 仓库根
from collectors import snapshot
from collectors.common import clean_text

_publish = None  # 由 start() 注入的发布回调


def _find_session(source: str, cwd: str, session_id: str):
    """在最新快照里找匹配会话,拿 project/last_msg/meta。"""
    snap = snapshot.build_snapshot(with_quota=False)
    best = None
    for s in snap["sessions"]:
        if s.get("src") != source:
            continue
        if cwd and s.get("project") and cwd.rstrip("/").endswith(s["project"]):
            return s
        if best is None:
            best = s
    return best


def _meta(s: dict) -> str:
    parts = []
    if s.get("elapsed_s") and s["elapsed_s"] >= 0:
        e = s["elapsed_s"]
        parts.append(f"用时 {e//60}m{e%60}s" if e >= 60 else f"用时 {e}s")
    if s.get("tokens") and s["tokens"] >= 0:
        t = s["tokens"]
        parts.append(f"{t/1e6:.1f}M tok" if t >= 1e6 else f"{t//1000}k tok")
    if s.get("model"):
        parts.append(s["model"])
    return " · ".join(parts)


def build_event(kind: str, source: str, hook: dict, now: int) -> dict:
    cwd = hook.get("cwd") or hook.get("workspace") or ""
    sid = hook.get("session_id") or hook.get("sessionId") or ""
    s = _find_session(source, cwd, sid)
    project = (s.get("project") if s else None) or (cwd.rstrip("/").split("/")[-1] if cwd else "?")
    if kind == "needs_input":
        # 通知类 hook 常带 message/问题文本
        msg = hook.get("message") or hook.get("prompt") or (s.get("last_msg") if s else "") or "需要你确认/输入"
    else:
        msg = (s.get("last_msg") if s else "") or "任务完成"
    return {
        # 通知端不显示图片:过滤图片/链接/URL(needs_input 的 hook 文本没走 collectors,这里补过滤);
        # 上限提到 1200(BLE 分帧可送长文),设备通知窗口尽量多显示正文。
        "kind": kind, "src": source, "project": project,
        "msg": clean_text(msg)[:1200], "meta": _meta(s) if s else "", "ts": now,
    }


class _H(BaseHTTPRequestHandler):
    def log_message(self, *a): pass

    def do_POST(self):
        u = urlparse(self.path)
        if u.path != "/event":
            self.send_response(404); self.end_headers(); return
        q = parse_qs(u.query)
        kind = (q.get("kind") or ["done"])[0]
        source = (q.get("source") or ["claude"])[0]
        ln = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(ln) if ln else b"{}"
        try:
            hook = json.loads(raw.decode("utf-8", "replace"))
        except Exception:
            hook = {}
        import time as _t
        ev = build_event(kind, source, hook, int(_t.time()))
        if _publish:
            _publish(ev)
        self.send_response(200); self.send_header("Content-Length", "2"); self.end_headers()
        self.wfile.write(b"ok")


def start(port: int, publish_fn):
    global _publish
    _publish = publish_fn
    srv = ThreadingHTTPServer(("127.0.0.1", port), _H)  # 只收本机 hook
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv
