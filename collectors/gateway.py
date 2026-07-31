"""代理网关流量采集:ssh 到网关 VPS 读 /run/gwtraffic.json。

数据链路(网关侧,实测):
  gwtraffic.service → /usr/local/bin/gwtraffic-daemon (bash while-loop, 每 2s)
    读 /sys/class/net/eth0/statistics/{rx,tx}_bytes
    累计状态持久化到 /var/lib/gwtraffic/state ("CYCLE CYCBYTES LASTTOTAL",含重启回绕保护)
    写 /run/gwtraffic.json (tmpfs)
计费周期每月 17 日重置,由 daemon 每轮比对 cycle_id 惰性判定(无 timer)。

payload(9 个键全必需):
  {"iface","cycle_start","next_reset","quota_gb","cycle_bytes",
   "in_Bps","out_Bps","total_Bps","ts"}
  cycle_bytes = 本周期累计 rx+tx 合并标量(⚠️ 上下行已合并,不可拆分)
  *_Bps       = 瞬时字节/秒(近 2s 均值)
  quota_gb    = 十进制 GB(对齐服务商计费口径,不是 GiB)

⚠️ ts 新鲜度必须自己判:ssh 成功**不代表 daemon 还在跑**(JSON 在 tmpfs 里会留着),
菜单栏那个 NetTrafficBar 就缺这一层。这里 fresh=False 表示「网关 daemon 可能死了」,
与「我们这份缓存旧了」是两件事(后者由 poller 的 last_success 判)。

⚠️ ControlMaster socket 是 0600 owned by nanzhang → 本采集必须以 nanzhang 跑
(LaunchAgent 可以,root 的 LaunchDaemon 不行)。

仓库里这是第一处 subprocess;沿用 deploy/hooks/on_event.sh 的 house style:
硬超时 + 吞掉失败,绝不阻塞调用方。
"""
from __future__ import annotations

import json
import os
import subprocess
import time

REMOTE_PATH = "/run/gwtraffic.json"
# daemon 每 2s 刷一次,给 ~7x 余量;超了就认为 daemon 卡死/没在跑
FRESH_WINDOW = 15


def collect(host: str, control_path: str, identity: str, timeout: float = 8.0,
            now: float | None = None) -> dict | None:
    """ssh 读一次网关流量。失败返回 None(调用方按 stale 处理)。"""
    now = now or time.time()
    cmd = [
        "ssh",
        "-o", "BatchMode=yes",
        "-o", "ConnectTimeout=5",
        "-o", "StrictHostKeyChecking=accept-new",
        "-o", "ControlMaster=auto",
        "-o", f"ControlPath={os.path.expanduser(control_path)}",
        "-o", "ControlPersist=600",
        "-i", os.path.expanduser(identity),
        host, "cat", REMOTE_PATH,
    ]
    try:
        p = subprocess.run(cmd, capture_output=True, timeout=timeout, check=False)
    except (subprocess.TimeoutExpired, OSError):
        return None
    if p.returncode != 0 or not p.stdout:
        return None
    try:
        d = json.loads(p.stdout.decode("utf-8", "replace"))
    except (ValueError, json.JSONDecodeError):
        return None
    if not isinstance(d, dict) or "cycle_bytes" not in d:
        return None

    try:
        cycle_bytes = int(d["cycle_bytes"])
        quota_gb = float(d.get("quota_gb") or 0)
        ts = int(d.get("ts") or 0)
    except (TypeError, ValueError):
        return None

    used_gb = cycle_bytes / 1e9          # 十进制 GB,对齐服务商面板
    rem_pct = -1
    if quota_gb > 0:
        rem_pct = int(round(max(0.0, min(100.0, (1 - used_gb / quota_gb) * 100))))

    return {
        "iface": d.get("iface") or "?",
        "used_gb": used_gb,
        "quota_gb": quota_gb,
        "rem_pct": rem_pct,
        "cycle_start": d.get("cycle_start") or "",
        "next_reset": d.get("next_reset") or "",
        "in_Bps": int(d.get("in_Bps") or 0),
        "out_Bps": int(d.get("out_Bps") or 0),
        "ts": ts,
        "fresh": bool(ts and (now - ts) <= FRESH_WINDOW),
        "ok": True,
    }


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="root@173.242.121.204")
    ap.add_argument("--control-path", default="~/.ssh/cm-gwtraffic.sock")
    ap.add_argument("--identity", default="~/.ssh/id_ed25519")
    a = ap.parse_args()
    print(json.dumps(collect(a.host, a.control_path, a.identity),
                     ensure_ascii=False, indent=2))
