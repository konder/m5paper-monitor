"""AI 消耗看板 payload:把四路数据源拼成设备直接可渲染的通用 rows。

设计原则:**固件零 per-provider 逻辑**。设备只会画「一行油量表」,
所有取舍(哪个窗口存在、未校准怎么显示、stale 怎么标)都在这里做完。

payload(发到 m5paper/usage,retained QoS0,约 450 字节):
  {"ts":1785404230, "hhmm":"21:34", "rev":17,
   "rows":[{"l":"Codex 周","rem":54,"rin":"6天后","n":"prolite"}, ...],
   "foot":"LiteLLM 7日 107M tok · 7617 req · 今日 706k"}

  l   = 左侧标签
  rem = **剩余**百分比(对齐 render.cpp 里 drawQuotaCol 的「黑填充=剩余」约定);
        -1 = 未知 → 设备画空条 + 显示「--」
  rin = 重置倒计时,**已在这里格式化成字符串**。设备无 RTC(render.cpp 现在拿
        items[0].ts 当 now),而且四行的格式不同(相对天数 vs 日历日期),
        预格式化最省事
  n   = 右侧小字备注
  rev = 只在**实质变化**时才 +1。5 分钟心跳重发时 rev 不变,设备就不会白刷一次墨水屏

「旧」标记不用 ⚠ 之类符号 —— efontCN 是点阵中日韩字库,U+26A0 大概率缺字形。
"""
from __future__ import annotations

import time

# ---- 实质变化阈值 ----
TOK_STEP = 1_000_000        # LiteLLM 7日 token 变化 ≥1M 才算有变化
GB_STEP = 1.0               # 网关用量变化 ≥1GB 才算有变化

STALE_MARK = "旧"


def _fmt_tok(n) -> str:
    """token 数缩写,与固件 render.cpp 的 fTok 同风格。"""
    try:
        n = int(n)
    except (TypeError, ValueError):
        return "?"
    if n >= 1_000_000_000:
        return f"{n / 1e9:.1f}B"
    if n >= 1_000_000:
        return f"{n / 1e6:.1f}M"
    if n >= 1000:
        return f"{n // 1000}k"
    return str(n)


def _to_epoch(v):
    """resets_at 可能是 unix 秒(rollout 里就是),也可能是 ISO 串(防御性)。"""
    if v is None:
        return None
    if isinstance(v, (int, float)):
        return float(v)
    try:
        from datetime import datetime
        return datetime.fromisoformat(str(v).replace("Z", "+00:00")).timestamp()
    except Exception:
        return None


def _fmt_rin(reset, now: float) -> str:
    """相对倒计时:重置中 / 42分后 / 5时后 / 3天后。"""
    r = _to_epoch(reset)
    if not r:
        return ""
    d = r - now
    if d <= 0:
        return "重置中"
    if d < 3600:
        return f"{int(d // 60)}分后"
    if d < 86400:
        return f"{int(d // 3600)}时后"
    return f"{int(d // 86400)}天后"


def _fmt_date_rin(datestr: str) -> str:
    """"2026-08-17" → "8-17重置"(网关是日历日重置,不是滚动窗口)。"""
    parts = (datestr or "").split("-")
    if len(parts) != 3:
        return ""
    try:
        return f"{int(parts[1])}-{int(parts[2])}重置"
    except ValueError:
        return ""


def _rem_from_used(used) -> int:
    """已用百分比 → 剩余整数百分比;None → -1。"""
    if used is None:
        return -1
    try:
        return max(0, min(100, int(round(100 - float(used)))))
    except (TypeError, ValueError):
        return -1


def _row(label: str, rem: int, rin: str, note: str) -> dict:
    return {"l": label, "rem": rem, "rin": rin, "n": note}


def _hhmm(now: float, tz_offset_hours) -> str:
    """看板顶栏的时钟。

    ⚠️ Mac Mini 的系统时区是 US/Pacific(-0700),而用户在 CST(+0800),差 15 小时 ——
    直接 localtime() 会让墨水屏显示美西时间。时钟本身是准的(sntp 偏差 <7s),
    所以这里只做时区偏移,不动系统设置。rin 倒计时是 epoch 差值,不受时区影响。
    tz_offset_hours 为 None → 用本机 localtime。
    """
    if tz_offset_hours is None or tz_offset_hours == "":
        return time.strftime("%H:%M", time.localtime(now))
    try:
        off = float(tz_offset_hours)
    except (TypeError, ValueError):
        return time.strftime("%H:%M", time.localtime(now))
    return time.strftime("%H:%M", time.gmtime(now + off * 3600))


def build(snap: dict, codex_q: dict | None, poller, now: float | None = None,
          tz_offset_hours=None):
    """拼 rows + foot。返回 (payload, alerts)。

    snap    = build_snapshot() 的结果,只用它的 quota.claude(主循环本来就算了,白拿)
    codex_q = collectors.codex_quota.collect() 的结果(本地文件,68ms,内联调)
    poller  = SourcePoller,提供 litellm / gwtraffic 两个网络源

    alerts 单独返回而不塞进 payload:告警去重要用**原始 reset 值**做键
    (payload 里的 rin 是格式化后的倒计时,每分钟都在变,当不了键),
    而设备不需要这个字段 —— 没必要为它加宽上行报文。
    """
    now = now or time.time()
    rows: list[dict] = []
    alerts: list[dict] = []

    def add(label, rem, rin, note, reset=None, real=False):
        rows.append(_row(label, rem, rin, note))
        if rem >= 0:
            alerts.append({"label": label, "rem": rem, "reset": reset, "real": real})

    # ---- Codex(官方真值)----
    # prolite 套餐只报 10080 分钟窗口 → 只会出「周」这一行;有 5h 窗口的套餐会出两行
    if codex_q:
        plan = codex_q.get("plan") or ""
        for key, label in (("h5", "Codex 5h"), ("week", "Codex 周")):
            if codex_q.get(key) is None:
                continue
            reset = codex_q.get(key + "_reset")
            add(label, _rem_from_used(codex_q[key]), _fmt_rin(reset, now), plan,
                reset=reset, real=True)
    else:
        rows.append(_row("Codex", -1, "", STALE_MARK))

    # ---- Claude(本地日志估算)----
    cq = (snap.get("quota") or {}).get("claude") or {}
    if cq.get("valid"):
        calibrated = bool(cq.get("calibrated"))
        for key, label in (("h5", "Claude 5h"), ("week", "Claude 周")):
            rem = _rem_from_used(cq.get(key))
            if calibrated and rem >= 0:
                note = "估"
            else:
                # 未校准:不编造百分比,直接把窗口内 token 消耗量摆出来
                tk = cq.get(key + "_tokens") or 0
                note = "无活动" if tk == 0 else (_fmt_tok(tk) + " tok")
                rem = -1
            reset = cq.get(key + "_reset")
            add(label, rem, _fmt_rin(reset, now), note, reset=reset, real=False)
    else:
        rows.append(_row("Claude", -1, "", STALE_MARK))

    # ---- 代理网关流量配额 ----
    gw = poller.get("gwtraffic")
    if gw and not poller.is_stale("gwtraffic", now):
        note = f"{gw['used_gb']:.0f}/{gw['quota_gb']:.0f}G"
        if not gw.get("fresh"):
            note = STALE_MARK + " " + note      # ssh 通了但网关 daemon 可能死了
        add("网关", gw.get("rem_pct", -1), _fmt_date_rin(gw.get("next_reset", "")), note,
            reset=gw.get("next_reset"), real=True)
    else:
        rows.append(_row("网关", -1, "", STALE_MARK))

    # ---- LiteLLM(底部单行;只有 token/请求数,该 proxy 的 spend 恒为 0)----
    ll = poller.get("litellm")
    if ll and not poller.is_stale("litellm", now):
        foot = (f"LiteLLM 7日 {_fmt_tok(ll['tok_7d'])} tok · {ll['req_7d']} req"
                f" · 今日 {_fmt_tok(ll['tok_today'])}")
    else:
        foot = "LiteLLM 数据" + STALE_MARK

    payload = {
        "ts": int(now),
        "hhmm": _hhmm(now, tz_offset_hours),
        "rows": rows,
        "foot": foot,
    }
    return payload, alerts


def signature(payload: dict, poller) -> tuple:
    """实质变化的比较键。刻意**不含 rin** —— 倒计时每分钟都在变,
    否则「42分后→41分后」会每分钟触发一次重刷。"""
    rows = tuple((r["l"], r["rem"], r["n"]) for r in payload.get("rows", []))
    ll = poller.get("litellm") or {}
    gw = poller.get("gwtraffic") or {}
    return (
        rows,
        int((ll.get("tok_7d") or 0) // TOK_STEP),        # 按 1M 分桶
        int((gw.get("used_gb") or 0) // GB_STEP),        # 按 1GB 分桶
        poller.is_stale("litellm"), poller.is_stale("gwtraffic"),
    )
