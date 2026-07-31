"""Claude 会话采集(纯本地日志,不碰 OAuth/token,零封禁风险)。

数据来源只有 ~/.claude/projects/*/*.jsonl:
  - 会话:状态/项目/当前任务/耗时/token/model
  - 额度(估算):把最近 5h / 7天窗口内每条 assistant 消息的 token 加权累加,
    再除以"满额 token 数"得到已用百分比。满额数无法从官方拿到 → 用一次性校准反推:
      你看一眼 `claude /usage` 的百分比,告诉本工具,即可算出你套餐的满额 token。
    校准前只显示已用 token 绝对值(百分比为 None)。real=False 表示"估算"。

纯 stdlib。
"""
from __future__ import annotations

import glob
import json
import os
import time
from datetime import datetime

try:
    from collectors.common import extract_images, clean_text
except ImportError:
    from common import extract_images, clean_text

LAST_MSG_MAX = 800  # 详情页最多传这么多字符

PROJECTS_DIR = os.path.expanduser("~/.claude/projects")
CALIB_FILE = os.path.expanduser("~/.config/m5paper-monitor/claude_calib.json")

ACTIVE_WINDOW = 6 * 3600      # 会话列表:只收这么久内活跃的
RUNNING_THRESH = 90           # 判"运行中"
WINDOW_5H = 5 * 3600
WINDOW_7D = 7 * 86400


def _iso_to_epoch(s):
    if not s:
        return None
    try:
        return datetime.fromisoformat(str(s).replace("Z", "+00:00")).timestamp()
    except Exception:
        return None


def _wtokens(u: dict) -> int:
    """限额吞吐量代理:输入+输出+缓存创建+缓存读取(全算)。"""
    if not isinstance(u, dict):
        return 0
    return (int(u.get("input_tokens") or 0)
            + int(u.get("output_tokens") or 0)
            + int(u.get("cache_creation_input_tokens") or 0)
            + int(u.get("cache_read_input_tokens") or 0))


def _parse_file(path: str, mtime: float, now: float):
    """返回 (session|None, events)。
    session 仅当文件在 ACTIVE_WINDOW 内才生成;
    events = 最近 7 天内每条 assistant 消息的 (ts, wtokens),用于额度估算。
    """
    cwd = None
    model = None
    session_id = os.path.splitext(os.path.basename(path))[0]
    last_user_ts = None
    title = None
    last_prompt = None
    last_asst = None           # 最后一条 assistant 文本(结果)
    tokens = 0                 # 展示用(不含 cache_read)
    events = []

    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    o = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if o.get("cwd"):
                    cwd = o["cwd"]
                ts = _iso_to_epoch(o.get("timestamp"))
                t = o.get("type")
                if t == "user" and not o.get("isMeta"):
                    if ts and (last_user_ts is None or ts > last_user_ts):
                        last_user_ts = ts
                elif t == "ai-title" and o.get("aiTitle"):
                    title = o["aiTitle"]
                elif t == "last-prompt" and o.get("lastPrompt"):
                    last_prompt = o["lastPrompt"]
                elif t == "assistant":
                    m = o.get("message")
                    if isinstance(m, dict):
                        if m.get("model"):
                            model = m["model"]
                        # 提取 assistant 文本块作为"结果"
                        c = m.get("content")
                        if isinstance(c, list):
                            txt = "\n".join(
                                b.get("text", "") for b in c
                                if isinstance(b, dict) and b.get("type") == "text")
                            if txt.strip():
                                last_asst = txt.strip()
                        elif isinstance(c, str) and c.strip():
                            last_asst = c.strip()
                        u = m.get("usage")
                        if isinstance(u, dict):
                            tokens += (int(u.get("input_tokens") or 0)
                                       + int(u.get("output_tokens") or 0)
                                       + int(u.get("cache_creation_input_tokens") or 0))
                            if ts and (now - ts) <= WINDOW_7D:
                                events.append((ts, _wtokens(u)))
    except OSError:
        return None, events

    session = None
    if (now - mtime) <= ACTIVE_WINDOW:
        state = "running" if (now - mtime) < RUNNING_THRESH else "idle"
        task = (title or last_prompt or "").splitlines()[0][:120] if (title or last_prompt) else ""
        elapsed = int(mtime - last_user_ts) if last_user_ts else None
        session = {
            "src": "claude",
            "session_id": session_id,
            "project": os.path.basename(cwd) if cwd else "?",
            "cwd": cwd,
            "state": state,
            "task": task,
            "last_msg": clean_text(last_asst or "")[:LAST_MSG_MAX],
            "images": extract_images(last_asst or "", cwd),
            "elapsed_s": elapsed,
            "tokens": tokens or None,
            "model": model,
            "last_activity": int(mtime),
        }
    return session, events


def _scan(now: float):
    """单趟扫描:7 天内有写入的文件 → (sessions, all_events)。"""
    sessions = []
    events = []
    if not os.path.isdir(PROJECTS_DIR):
        return sessions, events
    for f in glob.glob(os.path.join(PROJECTS_DIR, "*", "*.jsonl")):
        try:
            mt = os.path.getmtime(f)
        except OSError:
            continue
        if now - mt > WINDOW_7D:
            continue
        s, ev = _parse_file(f, mt, now)
        if s:
            sessions.append(s)
        events.extend(ev)
    sessions.sort(key=lambda s: s["last_activity"] or 0, reverse=True)
    return sessions, events


# ---------- 额度:纯日志估算 + 一次性校准 ----------

def _load_calib() -> dict:
    try:
        with open(CALIB_FILE) as fh:
            return json.load(fh)
    except Exception:
        return {}


def _window_sum(events, now, span):
    total = 0
    earliest = None
    for ts, w in events:
        if now - ts <= span:
            total += w
            if earliest is None or ts < earliest:
                earliest = ts
    return total, earliest


def quota_from_logs(events, now: float | None = None) -> dict:
    now = now or time.time()
    w5, e5 = _window_sum(events, now, WINDOW_5H)
    w7, e7 = _window_sum(events, now, WINDOW_7D)
    calib = _load_calib()
    c5 = calib.get("ceil_5h")
    c7 = calib.get("ceil_7d")

    def pct(used, ceil):
        if not ceil:
            return None
        return round(min(100.0, used / ceil * 100.0), 1)

    return {
        "valid": True,
        "real": False,                       # 估算,非官方真值
        "h5": pct(w5, c5),
        "week": pct(w7, c7),
        "h5_tokens": w5,
        "week_tokens": w7,
        # 滚动窗口"重置":当前窗口最早那笔 token 滚出的时刻
        "h5_reset": int(e5 + WINDOW_5H) if e5 else None,
        "week_reset": int(e7 + WINDOW_7D) if e7 else None,
        "calibrated": bool(c5 or c7),
    }


def calibrate(now: float, usage_5h_pct: float | None, usage_7d_pct: float | None) -> dict:
    """用 `claude /usage` 当前显示的百分比反推满额 token 数并保存。"""
    _, events = _scan(now)
    w5, _ = _window_sum(events, now, WINDOW_5H)
    w7, _ = _window_sum(events, now, WINDOW_7D)
    calib = _load_calib()
    if usage_5h_pct and usage_5h_pct > 0:
        calib["ceil_5h"] = int(w5 / (usage_5h_pct / 100.0))
    if usage_7d_pct and usage_7d_pct > 0:
        calib["ceil_7d"] = int(w7 / (usage_7d_pct / 100.0))
    calib["calibrated_at"] = int(now)
    os.makedirs(os.path.dirname(CALIB_FILE), exist_ok=True)
    with open(CALIB_FILE, "w") as fh:
        json.dump(calib, fh)
    return {"ceil_5h": calib.get("ceil_5h"), "ceil_7d": calib.get("ceil_7d"),
            "now_5h_tokens": w5, "now_7d_tokens": w7}


def collect(now: float | None = None, with_quota: bool = True) -> dict:
    now = now or time.time()
    sessions, events = _scan(now)
    return {
        "sessions": sessions,
        "quota": quota_from_logs(events, now) if with_quota else None,
    }


if __name__ == "__main__":
    import sys
    if "--calibrate" in sys.argv:
        i = sys.argv.index("--calibrate")
        p5 = float(sys.argv[i + 1]) if len(sys.argv) > i + 1 else None
        p7 = float(sys.argv[i + 2]) if len(sys.argv) > i + 2 else None
        print(json.dumps(calibrate(time.time(), p5, p7), ensure_ascii=False, indent=2))
    else:
        wq = "--no-quota" not in sys.argv
        print(json.dumps(collect(with_quota=wq), ensure_ascii=False, indent=2))
