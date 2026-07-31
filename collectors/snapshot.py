"""组装设备用紧凑快照:合并 codex + claude 两源。

schema:
{
  "ts": <unix秒>,
  "quota": {
    "codex":  {"h5","week","h5_reset","week_reset","real","plan"} | null,
    "claude": {"h5","week","h5_reset","week_reset","real",...}     | null
  },
  "sessions": [ {src,project,state,task,elapsed_s,tokens,model,last_activity} ... ]
}
"""
from __future__ import annotations

import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))  # 仓库根
try:
    from collectors import claude as claude_src
    from collectors import codex as codex_src
except ImportError:  # 直接在 collectors/ 目录内运行时的回退
    import claude as claude_src
    import codex as codex_src

# 设备屏幕有限,最多展示这么多会话(running 优先,再按最近活跃)
MAX_SESSIONS = 8
# 只保留这么久内活跃过的会话进快照(秒)
STALE_AFTER = 3 * 3600

_STATE_RANK = {"needs_input": 0, "running": 1, "done": 2, "idle": 3}

# 图片 id → 本地真实路径(缩略图 HTTP 服务用;不发给设备)
IMAGE_REGISTRY: dict[str, str] = {}


def _slim(s: dict) -> dict:
    """去掉 cwd 等设备不需要的字段,压缩体积。

    elapsed_s 仅对 running/done 有意义(当前/刚完成那一轮的时长);
    idle 会话置 None,设备改用 last_activity 显示"多久前活跃"。
    """
    elapsed = s.get("elapsed_s") if s.get("state") in ("running", "done") else None
    return {
        "src": s.get("src"),
        "project": s.get("project"),
        "state": s.get("state"),
        "task": s.get("task"),
        "last_msg": s.get("last_msg") or "",
        # 只给设备 id+name;路径留在 gateway 侧 registry
        "images": [{"id": im["id"], "name": im["name"]} for im in (s.get("images") or [])],
        "elapsed_s": elapsed,
        "tokens": s.get("tokens"),
        "model": s.get("model"),
        "last_activity": s.get("last_activity"),
        "done_ts": s.get("done_ts", 0),   # 供采集器做 codex 无-hook 完成检测(设备忽略)
    }


def build_snapshot(now: float | None = None, with_quota: bool = True) -> dict:
    now = now or time.time()

    cx = codex_src.collect(now)
    cl = claude_src.collect(now, with_quota=with_quota)

    sessions = list(cx["sessions"]) + list(cl["sessions"])
    sessions = [s for s in sessions if (now - (s.get("last_activity") or 0)) <= STALE_AFTER]
    sessions.sort(key=lambda s: (_STATE_RANK.get(s.get("state"), 9), -(s.get("last_activity") or 0)))
    sessions = sessions[:MAX_SESSIONS]
    # 更新图片注册表(id→路径),供缩略图服务
    for s in sessions:
        for im in (s.get("images") or []):
            IMAGE_REGISTRY[im["id"]] = im["path"]
    sessions = [_slim(s) for s in sessions]

    return {
        "ts": int(now),
        "hhmm": time.strftime("%H:%M", time.localtime(now)),   # 生成时刻(本地),设备显示"更新 HH:MM"
        "quota": {
            "codex": cx.get("quota"),
            "claude": cl.get("quota") if with_quota else None,
        },
        "sessions": sessions,
    }


if __name__ == "__main__":
    import sys
    wq = "--no-quota" not in sys.argv
    print(json.dumps(build_snapshot(with_quota=wq), ensure_ascii=False, indent=2))
