"""Codex 会话采集:解析 ~/.codex/sessions/**/*.jsonl 的 rollout 文件。

产出:
  - 每个近期会话的状态/项目/当前任务/耗时/token
  - 订阅额度:全局最新一条含 rate_limits 的 payload(primary=5h / secondary=周)

rollout 行结构(每行一个 JSON,顶层 {timestamp,type,payload}):
  session_meta   payload: {session_id, cwd, cli_version, model_provider, git, ...}
  turn_context   payload: {model, cwd, effort, ...}
  response_item  payload: {role: user|assistant, content: [{type,text}...]}
  event_msg      payload: {type, last_agent_message, duration_ms, completed_at, turn_id, ...}
                 订阅模式下另有 token_count 类事件,payload 含 rate_limits + token 计数
纯 stdlib,无第三方依赖。
"""
from __future__ import annotations

import glob
import json
import os
import time
from datetime import datetime, timezone

try:
    from collectors.common import extract_images, clean_text
except ImportError:
    from common import extract_images, clean_text

import re as _re

LAST_MSG_MAX = 800


def _project_name(cwd: str | None) -> str:
    """项目名。Codex Desktop 用 ~/Documents/Codex/<date>/users-x-desktop-<slug> 作工作目录,美化之。"""
    if not cwd:
        return "?"
    base = os.path.basename(cwd.rstrip("/"))
    if "/Documents/Codex/" in cwd:
        m = _re.match(r"users-[^-]+-desktop-(.+)", base)
        if m:
            base = m.group(1)
        base = base.replace("-", " ").strip()
    return base[:28] or "?"

SESSIONS_DIR = os.path.expanduser("~/.codex/sessions")

# 会话在多久内有写入才认为"近期、值得展示"(秒)
ACTIVE_WINDOW = 6 * 3600
# 多久内有写入认为"正在运行"(秒);超过则视为空闲/结束
RUNNING_THRESH = 90


def _iso_to_epoch(s: str | None) -> float | None:
    if not s:
        return None
    try:
        # 兼容结尾 Z
        return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()
    except Exception:
        return None


def _to_epoch(v) -> float | None:
    """completed_at 可能是数字(秒或毫秒)或 ISO 字符串。"""
    if v is None:
        return None
    if isinstance(v, (int, float)):
        return v / 1000.0 if v > 1e12 else float(v)
    return _iso_to_epoch(str(v))


def _text_from_content(content) -> str:
    """response_item.payload.content 是 [{type,text}...],拼出纯文本。"""
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    parts = []
    for c in content:
        if isinstance(c, dict):
            t = c.get("text") or c.get("content") or ""
            if isinstance(t, str):
                parts.append(t)
    return "\n".join(p for p in parts if p).strip()


def _recent_rollouts(now: float):
    files = glob.glob(os.path.join(SESSIONS_DIR, "**", "*.jsonl"), recursive=True)
    out = []
    for f in files:
        try:
            mt = os.path.getmtime(f)
        except OSError:
            continue
        if now - mt <= ACTIVE_WINDOW:
            out.append((mt, f))
    out.sort()  # 按 mtime 升序,最新在末尾
    return out


def _parse_one(path: str, mtime: float, now: float) -> dict | None:
    """解析单个 rollout,返回会话摘要 + 该文件内出现过的 rate_limits(最后一条)。"""
    cwd = None
    model = None
    session_id = None
    started = None
    last_user_ts = None
    last_user = None
    last_agent = None
    last_completed = None
    rate_limits = None
    rl_ts = None
    rl_nz = None          # 该文件内最后一条"非零"rate_limits
    rl_nz_ts = None
    total_tokens = 0

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
                ts = _iso_to_epoch(o.get("timestamp"))
                if started is None:
                    started = ts
                p = o.get("payload")
                if not isinstance(p, dict):
                    continue
                t = o.get("type")
                if t == "session_meta":
                    cwd = p.get("cwd") or cwd
                    session_id = p.get("session_id") or p.get("id") or session_id
                elif t == "turn_context":
                    model = p.get("model") or model
                    cwd = p.get("cwd") or cwd
                elif t == "response_item":
                    role = p.get("role")
                    if role == "user":
                        txt = _text_from_content(p.get("content"))
                        if txt:
                            last_user = txt
                            if ts and (last_user_ts is None or ts > last_user_ts):
                                last_user_ts = ts
                    elif role == "assistant":
                        txt = _text_from_content(p.get("content"))
                        if txt:
                            last_agent = txt
                elif t == "event_msg":
                    if p.get("last_agent_message"):
                        last_agent = p.get("last_agent_message")
                    elif p.get("type") == "agent_message" and p.get("message"):
                        last_agent = p.get("message")
                    # task_complete(桌面版)/ 其它带 completed_at 的都算完成
                    if p.get("completed_at") is not None:
                        c = _to_epoch(p.get("completed_at"))
                        if c:
                            last_completed = c
                # rate_limits 可能挂在任意行的 payload 上(通常是 token_count 类 event)
                if isinstance(p.get("rate_limits"), dict):
                    rate_limits = p["rate_limits"]
                    rl_ts = _iso_to_epoch(o.get("timestamp")) or mtime
                    _pr = (rate_limits.get("primary") or {}).get("used_percent") or 0
                    _se = (rate_limits.get("secondary") or {}).get("used_percent") or 0
                    if _pr > 0 or _se > 0:
                        rl_nz = rate_limits
                        rl_nz_ts = rl_ts
                # token 计数:info.total_token_usage.total_tokens(桌面版) 或 info.total_tokens
                info = p.get("info") if isinstance(p.get("info"), dict) else None
                if info:
                    tu = info.get("total_token_usage")
                    if isinstance(tu, dict) and isinstance(tu.get("total_tokens"), (int, float)):
                        total_tokens = int(tu["total_tokens"])
                    elif isinstance(info.get("total_tokens"), (int, float)):
                        total_tokens = int(info["total_tokens"])
    except OSError:
        return None

    if session_id is None and cwd is None and last_user is None:
        return None

    state = "running" if (now - mtime) < RUNNING_THRESH else "idle"
    # 若最后一次 turn 完成时间接近 mtime,更可能是"已完成/等待输入"
    if state == "running" and last_completed and (mtime - last_completed) < 2:
        state = "done"

    task = (last_user or last_agent or "").splitlines()[0][:120] if (last_user or last_agent) else ""
    elapsed = int(mtime - last_user_ts) if last_user_ts else None

    return {
        "summary": {
            "src": "codex",
            "session_id": session_id,
            "project": _project_name(cwd),
            "cwd": cwd,
            "state": state,
            "task": task,
            "last_msg": clean_text(last_agent or "")[:LAST_MSG_MAX],
            "images": extract_images(last_agent or "", cwd),
            "done_ts": int(last_completed) if last_completed else 0,  # 最近一轮完成时刻(供无 hook 的完成检测)
            "elapsed_s": elapsed,
            "tokens": total_tokens or None,
            "model": model,
            "last_activity": int(mtime),
        },
        "rate_limits": rate_limits,
        "rate_limits_ts": rl_ts,
        "rl_nz": rl_nz,
        "rl_nz_ts": rl_nz_ts,
    }


def _norm_quota(rl: dict | None) -> dict | None:
    """把 codex rate_limits 归一化成 {h5, week, h5_reset, week_reset, plan, real}。

    ⚠️ 必须按 window_minutes 分桶,**不能假设 primary=5h / secondary=周**。
    实测 prolite 套餐:primary.window_minutes = 10080(周),secondary = null ——
    旧代码把 primary 直接当 h5,会把「周额度 50%」显示成「5h 50%」。
    """
    if not rl:
        return None
    out = {"h5": None, "week": None, "h5_reset": None, "week_reset": None,
           "plan": rl.get("plan_type"), "real": True}
    for slot in ("primary", "secondary"):
        w = rl.get(slot)
        if not isinstance(w, dict) or w.get("used_percent") is None:
            continue
        wm = w.get("window_minutes")
        key = "h5" if (isinstance(wm, (int, float)) and wm <= 360) else "week"
        out[key] = w.get("used_percent")
        out[key + "_reset"] = w.get("resets_at")
    return out


def collect(now: float | None = None) -> dict:
    """返回 {'sessions': [...], 'quota': {...}|None}。"""
    now = now or time.time()
    if not os.path.isdir(SESSIONS_DIR):
        return {"sessions": [], "quota": None}

    sessions = []
    best_any = None; best_any_ts = -1.0      # 最新的任意 rate_limits
    best_nz = None; best_nz_ts = -1.0        # 最新的"非零"rate_limits(真实交互会话)

    for mt, path in _recent_rollouts(now):
        parsed = _parse_one(path, mt, now)
        if not parsed:
            continue
        sessions.append(parsed["summary"])
        rl = parsed["rate_limits"]; ts = parsed["rate_limits_ts"] or 0
        if rl and ts > best_any_ts:
            best_any = rl; best_any_ts = ts
        # 全局最新的"非零"读数(跨文件、跨行);Codex Desktop cua 会话记 0/0,靠这个跳过
        nz = parsed["rl_nz"]; nzts = parsed["rl_nz_ts"] or 0
        if nz and nzts > best_nz_ts:
            best_nz = nz; best_nz_ts = nzts
    latest_rl = best_nz or best_any

    # 每个 cwd 只保留最新的一个会话(去重,避免同项目多条 rollout 刷屏)
    by_project = {}
    for s in sessions:
        key = s["session_id"] or s["cwd"]
        prev = by_project.get(key)
        if prev is None or (s["last_activity"] or 0) > (prev["last_activity"] or 0):
            by_project[key] = s

    result = sorted(by_project.values(), key=lambda s: s["last_activity"] or 0, reverse=True)
    return {"sessions": result, "quota": _norm_quota(latest_rl)}


if __name__ == "__main__":
    print(json.dumps(collect(), ensure_ascii=False, indent=2))
