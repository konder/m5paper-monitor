"""Codex 额度采集(轻量版):只取额度,不解析会话。

为什么不复用 collectors/codex.py 的额度部分:
  ~/.codex/sessions 实测 11GB / 268 个文件,最新单文件 58MB。codex.py 会把 6 小时窗口内
  **每个** rollout 整体逐行 json.loads 一遍,每 20s 一次。要额度不该付这个代价。
  这里只做两件廉价事:
    1. 读 ~/.code-buddy/state.json(code-buddy 的 launchd agent 维护,mtime 亚秒级实时)
       → snapshot.usage.*_remaining。⚠️ 它是**剩余**百分比,不是已用。
       但它**没有 reset 时间**。
    2. 反向 tail 最新 rollout 的尾部若干字节,倒着找最后一条 rate_limits
       → 拿到 resets_at + used_percent。一次 seek+read,与文件大小无关。

⚠️ 窗口必须按 window_minutes 分桶,不能假设 primary=5h / secondary=周。
   实测当前 prolite 套餐:primary.window_minutes = **10080(周)**,secondary = null。
   collectors/codex.py 的 _norm_quota 把 primary 直接当 h5,在这个套餐下会把周额度
   标成「5h」—— 那是个既存 bug,本模块不沿用那套映射。

⚠️ 老 rollout 会带不同的 limit_id(如 codex_bengalfox)和一串归零读数,
   所以倒着扫时优先取「非零」的那条。

纯 stdlib。
"""
from __future__ import annotations

import glob
import json
import os
import time

SESSIONS_DIR = os.path.expanduser("~/.codex/sessions")
BUDDY_STATE = os.path.expanduser("~/.code-buddy/state.json")

# 反向 tail 的字节数。rollout 每行可能很长(带 info/token 计数),
# 512KB 足够覆盖最后几十条 token_count 事件。
TAIL_BYTES = 512 * 1024
# 只信任近期文件,避免翻出几周前的陈旧额度
MAX_AGE = 24 * 3600
# window_minutes 分桶阈值:<=360 分钟算「5h 窗口」,其余算「周窗口」
H5_WINDOW_MAX = 360


def _newest_rollout(now: float) -> str | None:
    """按 mtime 找最新 rollout。只 stat 不读,268 个文件可忽略。"""
    newest, newest_mt = None, -1.0
    for f in glob.glob(os.path.join(SESSIONS_DIR, "**", "*.jsonl"), recursive=True):
        try:
            mt = os.path.getmtime(f)
        except OSError:
            continue
        if now - mt <= MAX_AGE and mt > newest_mt:
            newest, newest_mt = f, mt
    return newest


def _tail_lines(path: str, nbytes: int = TAIL_BYTES) -> list[bytes]:
    """读文件尾部 nbytes,返回完整行(丢掉被切开的首行)。"""
    try:
        with open(path, "rb") as fh:
            fh.seek(0, os.SEEK_END)
            size = fh.tell()
            start = max(0, size - nbytes)
            fh.seek(start)
            blob = fh.read()
    except OSError:
        return []
    lines = blob.split(b"\n")
    if start > 0 and lines:
        lines = lines[1:]          # 首行可能被切断
    return lines


def _bucket(rl: dict) -> dict:
    """把一条 rate_limits 按 window_minutes 分桶成 {h5,week,h5_reset,week_reset,plan}。"""
    out = {"h5": None, "week": None, "h5_reset": None, "week_reset": None,
           "plan": rl.get("plan_type")}
    for slot in ("primary", "secondary"):
        w = rl.get(slot)
        if not isinstance(w, dict):
            continue
        pct = w.get("used_percent")
        if pct is None:
            continue
        wm = w.get("window_minutes")
        key = "h5" if (isinstance(wm, (int, float)) and wm <= H5_WINDOW_MAX) else "week"
        out[key] = float(pct)
        out[key + "_reset"] = w.get("resets_at")
    return out


def _from_rollout(now: float) -> dict | None:
    """倒着扫最新 rollout 的尾部,取最后一条(优先非零)rate_limits。"""
    path = _newest_rollout(now)
    if not path:
        return None
    any_hit = None
    for line in reversed(_tail_lines(path)):
        if b'"rate_limits"' not in line:
            continue
        try:
            o = json.loads(line)
        except (ValueError, json.JSONDecodeError):
            continue
        p = o.get("payload")
        rl = p.get("rate_limits") if isinstance(p, dict) else None
        if not isinstance(rl, dict):
            continue
        b = _bucket(rl)
        if any_hit is None:
            any_hit = b
        # 非零 = 真实交互会话的读数,优先;归零的多来自 Desktop cua 会话
        if (b.get("h5") or 0) > 0 or (b.get("week") or 0) > 0:
            b["src"] = "rollout"
            return b
    if any_hit is not None:
        any_hit["src"] = "rollout"
    return any_hit


def _from_buddy_state() -> dict | None:
    """读 code-buddy 的 state.json。字段是**剩余**百分比,这里转成已用以统一口径。"""
    try:
        with open(BUDDY_STATE, "r", encoding="utf-8") as fh:
            d = json.load(fh)
    except (OSError, ValueError, json.JSONDecodeError):
        return None
    usage = ((d.get("snapshot") or {}).get("usage") or {})
    if not isinstance(usage, dict):
        return None
    out = {"h5": None, "week": None, "h5_reset": None, "week_reset": None,
           "plan": None, "src": "code-buddy"}
    for field, key in (("five_hour_remaining", "h5"), ("seven_day_remaining", "week")):
        rem = usage.get(field)
        if isinstance(rem, (int, float)):
            out[key] = float(100 - rem)      # remaining → used
    if out["h5"] is None and out["week"] is None:
        return None
    return out


def collect(now: float | None = None) -> dict | None:
    """返回 {h5, week, h5_reset, week_reset, plan, real, src} 或 None。

    h5 / week 是**已用**百分比(与 collectors/claude.py 的口径一致);
    *_reset 是 unix 秒。rollout 优先(它同时有 pct 和 reset),
    state.json 用来补 rollout 缺的窗口。
    """
    now = now or time.time()
    roll = _from_rollout(now)
    buddy = _from_buddy_state()
    if roll is None and buddy is None:
        return None

    out = dict(roll or buddy)
    # 用另一个源补空窗口(prolite 只报周窗口,5h 两边都可能是 None)
    other = buddy if roll is not None else None
    if other:
        for key in ("h5", "week"):
            if out.get(key) is None and other.get(key) is not None:
                out[key] = other[key]
    out["real"] = True          # 两个源都是官方真值(读自己机器上的本地文件)
    out.setdefault("src", "?")
    return out


if __name__ == "__main__":
    print(json.dumps(collect(), ensure_ascii=False, indent=2))
