"""LiteLLM 代理用量采集:读 DGX 上 litellm proxy 的 /global/activity。

为什么是这个端点(1.86.2 实测):
  - 响应只 ~190 字节,是所有 spend/activity 端点里最省的
  - /spend/logs 不带参数会吐 123MB(逐行 dump 全表),绝对不能碰
  - /global/spend/report 是 Enterprise-only,返回 400
  - /metrics 是 404 —— 这个部署没开 prometheus callback

⚠️ 只有 token / 请求数,没有金额。该 proxy 的热门模型是自建 vLLM,
model_info 里没配 input_cost_per_token,所以 spend 字段结构性恒为 0
(实测 22694 行 SpendLogs 里 0 行 spend>0)。看板故意不显示金额。

响应形状:
  {"daily_data":[{"date":"Jul 30","api_requests":107,"total_tokens":706480}, ...],
   "sum_api_requests":7617,"sum_total_tokens":106756482}

daily_data 升序,但**会跳过没有流量的日子**(实测 Jul 26 缺失),
所以「今日」必须按日期字符串匹配,不能取末元素。

纯 stdlib(仓库无 HTTP 依赖,不引入新的)。
"""
from __future__ import annotations

import json
import time
import urllib.error
import urllib.parse
import urllib.request

WINDOW_DAYS = 7

# LiteLLM 的 date 是 "%b %d" 形式。strftime("%b") 跟 locale 走,
# 非英文 locale 下会变成 "7月" 之类而匹配不上 → 写死英文缩写。
_MON = ("Jan", "Feb", "Mar", "Apr", "May", "Jun",
        "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")


def _day_label(t: time.struct_time) -> str:
    """把时间转成 LiteLLM 用的 "Jul 30"(日为零填充两位,实测 "Jul 25" 而非 "Jul 5")。"""
    return f"{_MON[t.tm_mon - 1]} {t.tm_mday:02d}"


def collect(base_url: str, api_key: str, timeout: float = 3.0,
            now: float | None = None) -> dict | None:
    """拉近 WINDOW_DAYS 天用量。失败返回 None(调用方按 stale 处理)。"""
    now = now or time.time()
    end = time.localtime(now)
    start = time.localtime(now - (WINDOW_DAYS - 1) * 86400)
    q = urllib.parse.urlencode({
        "start_date": time.strftime("%Y-%m-%d", start),
        "end_date": time.strftime("%Y-%m-%d", end),
    })
    url = f"{base_url.rstrip('/')}/global/activity?{q}"
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {api_key}"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            if resp.status != 200:
                return None
            doc = json.loads(resp.read().decode("utf-8", "replace"))
    except (urllib.error.URLError, OSError, ValueError, json.JSONDecodeError):
        return None
    if not isinstance(doc, dict):
        return None

    days = doc.get("daily_data") or []
    today = _day_label(end)
    tok_today = req_today = 0
    for d in days:
        if isinstance(d, dict) and d.get("date") == today:
            tok_today = int(d.get("total_tokens") or 0)
            req_today = int(d.get("api_requests") or 0)
            break   # 今天没流量就停在 0,而不是错拿末元素(可能是几天前)

    return {
        "tok_7d": int(doc.get("sum_total_tokens") or 0),
        "req_7d": int(doc.get("sum_api_requests") or 0),
        "tok_today": tok_today,
        "req_today": req_today,
        "days": len(days),
        "ok": True,
    }


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--base-url", default="http://192.168.101.128:4000")
    ap.add_argument("--api-key", required=True)
    a = ap.parse_args()
    print(json.dumps(collect(a.base_url, a.api_key), ensure_ascii=False, indent=2))
