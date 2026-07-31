#!/usr/bin/env python3
"""墨水屏布局模拟器(横版 960x540,整体 ~1.5x)。渲染 PNG 做设计迭代。
用法: python3 sim_render.py [snapshot.json]  → sim_list.png (+ sim_detail.png)
定稿后同步到 firmware/src/render.cpp。
"""
import json
import sys
import time
from PIL import Image, ImageDraw, ImageFont

W, H = 960, 540          # 横版
BLACK, WHITE, GRAY, LGRAY, MGRAY = 0, 255, 80, 170, 140   # GRAY 加深,空闲文字更清晰

PF = "/System/Library/Fonts/PingFang.ttc"
HEITI_M = "/System/Library/Fonts/STHeiti Medium.ttc"
def font(p, s): return ImageFont.truetype(p, s)

# ~1.5x 字号
F_TITLE = font(HEITI_M, 40)
F_PROV  = font(HEITI_M, 27)
F_CARDT = font(HEITI_M, 33)
F_CHIP  = font(HEITI_M, 24)
F_PCT   = font(HEITI_M, 30)
F_BODY  = font(PF, 28)
F_RESULT= font(PF, 27)
F_SMALL = font(PF, 22)
F_TINY  = font(PF, 18)


def ftok(t):
    if t is None or t < 0: return ""
    if t < 1000: return str(t)
    if t < 1_000_000: return f"{t/1000:.0f}k"
    if t < 1_000_000_000: return f"{t/1e6:.1f}M"
    return f"{t/1e9:.1f}B"

def fdur(s):
    if s is None or s < 0: return ""
    if s < 60: return f"{s}s"
    if s < 3600: return f"{s//60}m{s%60}s"
    return f"{s//3600}h{(s%3600)//60}m"

def freset(r, now):
    if not r: return ""
    d = r - now
    if d <= 0: return "重置中"
    if d < 3600: return f"{d//60}分"
    if d < 86400: return f"{d//3600}时"
    return f"{d//86400}天"

def fago(ts, now): return fdur(max(0, now - ts)) + "前"

def tr(dr, x, y, s, f, fill): dr.text((x, y), s, font=f, fill=fill)
def trr(dr, xr, y, s, f, fill): dr.text((xr - dr.textlength(s, font=f), y), s, font=f, fill=fill)

def trunc(dr, s, f, maxw):
    s = s.replace("\n", " ").replace("\r", " ")
    if dr.textlength(s, font=f) <= maxw: return s
    while s and dr.textlength(s + "…", font=f) > maxw: s = s[:-1]
    return s + "…"

def wrap(dr, s, f, maxw, maxlines):
    lines, cur = [], ""
    for ch in s.replace("\n", " "):
        if dr.textlength(cur + ch, font=f) <= maxw: cur += ch
        else:
            lines.append(cur); cur = ch
            if len(lines) >= maxlines:
                lines[-1] = trunc(dr, lines[-1] + cur, f, maxw); return lines
    if cur: lines.append(cur)
    return lines

STATE_CN = {"running": "运行中", "done": "完成", "needs_input": "待输入", "idle": "空闲"}

def minibar(dr, x, y, w, h, pct):
    dr.rectangle([x, y, x + w, y + h], outline=BLACK, width=2)
    if pct is not None and pct > 0:
        fw = int((w - 2) * min(100, pct) / 100)
        col = BLACK if pct >= 85 else MGRAY
        if fw > 0: dr.rectangle([x + 2, y + 2, x + fw, y + h - 2], fill=col)

def draw_quota_inline(dr, x, y, label, q, now):
    """在一行内画一个 provider(横版顶部用)。"""
    tr(dr, x, y + 2, label, F_PROV, BLACK); x += 150  # 固定列,两行对齐
    if not q or not q.get("valid", True):
        tr(dr, x, y + 4, "(暂无)", F_SMALL, GRAY); return
    if q.get("h5") is None and not q.get("real"):
        tr(dr, x, y + 2, f"≈5h {ftok(q.get('h5_tokens'))} 周 {ftok(q.get('week_tokens'))}", F_BODY, BLACK)
        return
    for name, pct, rst in [("5h", q.get("h5"), q.get("h5_reset")), ("周", q.get("week"), q.get("week_reset"))]:
        tr(dr, x, y + 4, name, F_SMALL, GRAY); x += 34
        minibar(dr, x, y + 6, 84, 20, pct); x += 92
        tr(dr, x, y + 1, f"{pct:.0f}%" if pct is not None else "--", F_PCT, BLACK); x += 66
        rr = freset(rst, now)
        if rr: tr(dr, x, y + 5, rr, F_TINY, GRAY); x += dr.textlength(rr, font=F_TINY) + 18


def draw_card(dr, x, y, w, h, s, now):
    emph = s["state"] in ("running", "needs_input")
    # 边框:强调=粗+左侧实心条;普通=细
    dr.rectangle([x, y, x + w, y + h], outline=BLACK, width=3 if emph else 1)
    if emph:
        dr.rectangle([x, y, x + 12, y + h], fill=BLACK)
    pad = 26 if emph else 18
    inner = x + pad

    # 不画状态标签;活跃靠粗边框+左黑条区分
    proj = ("C·" if s.get("src") == "codex" else "A·") + s.get("project", "?")
    tr(dr, inner, y + 10, trunc(dr, proj, F_CARDT, w - pad - 20), F_CARDT, BLACK)

    # 结果摘要(全黑)
    msg = s.get("last_msg") or s.get("task") or ""
    tr(dr, inner, y + 48, trunc(dr, msg, F_RESULT, w - pad - 18), F_RESULT, BLACK)

    # 元信息(全黑,小字)
    parts = []
    if s["state"] == "running" and (s.get("elapsed_s") or -1) >= 0: parts.append("⏱" + fdur(s["elapsed_s"]))
    elif s.get("last_activity"): parts.append(fago(s["last_activity"], now))
    if (s.get("tokens") or -1) >= 0: parts.append(ftok(s["tokens"]) + " tok")
    if s.get("model"): parts.append(s["model"])
    if s.get("nImages"): parts.append(f"图{s['nImages']}")
    tr(dr, inner, y + h - 30, trunc(dr, "   ".join(parts), F_SMALL, w - pad - 18), F_SMALL, BLACK)


def draw_quota_col(dr, x, y, q, now):
    """列内额度油量表:黑填充=剩余,全黑文字。返回底部 y。"""
    if not q or not q.get("valid", True):
        tr(dr, x, y, "额度 暂无", F_BODY, BLACK); return y + 40
    if q.get("h5") is None and not q.get("real"):
        tr(dr, x, y, f"≈5h {ftok(q.get('h5_tokens'))}   周 {ftok(q.get('week_tokens'))}", F_BODY, BLACK)
        tr(dr, x, y + 36, "(未校准,显示消耗量)", F_SMALL, BLACK)
        return y + 70
    for i, (nm, used, rst) in enumerate([("5h", q.get("h5"), q.get("h5_reset")),
                                         ("周", q.get("week"), q.get("week_reset"))]):
        yy = y + i * 34
        rem = None if used is None else max(0, min(100, round(100 - used)))
        tr(dr, x, yy + 3, nm, F_BODY, BLACK)
        bx, bw, bh = x + 46, 168, 24
        dr.rectangle([bx, yy + 2, bx + bw, yy + 2 + bh], outline=BLACK, width=3)
        if rem is not None:
            fw = int((bw - 6) * rem / 100)
            if fw > 0:
                dr.rectangle([bx + 3, yy + 5, bx + 3 + fw, yy - 1 + bh], fill=BLACK)
        tr(dr, bx + bw + 14, yy, (f"剩{rem}%" if rem is not None else "--"), F_PCT, BLACK)
        rr = freset(rst, now)
        if rr: tr(dr, bx + bw + 150, yy + 3, rr + "后", F_SMALL, BLACK)
    return y + 70


def render_list(snap, out):
    now = snap.get("ts") or int(time.time())
    img = Image.new("L", (W, H), WHITE); dr = ImageDraw.Draw(img)

    # 顶部标题条
    tr(dr, 18, 8, "AI Agent 监控", F_TITLE, BLACK)
    trr(dr, W - 18, 20, f"{time.strftime('%H:%M', time.localtime(now))}  ·  80%  ·  v10", F_SMALL, BLACK)
    dr.rectangle([14, 52, W - 14, 55], fill=BLACK)

    # 竖分隔
    dr.rectangle([479, 60, 481, H - 12], fill=BLACK)
    q = snap.get("quota", {})
    colw, card_h = 452, 108
    for label, src, cx in (("CLAUDE", "claude", 14), ("CODEX", "codex", 494)):
        n = sum(1 for s in snap["sessions"] if s.get("src") == src)
        # 列头:黑底白字色块
        dr.rounded_rectangle([cx, 60, cx + colw, 100], radius=10, fill=BLACK)
        tr(dr, cx + 16, 64, label, F_CARDT, WHITE)
        trr(dr, cx + colw - 16, 70, f"{n} 会话", F_SMALL, WHITE)
        qy = draw_quota_col(dr, cx + 10, 112, q.get(src), now)
        yy = qy + 6
        for s in [s for s in snap["sessions"] if s.get("src") == src]:
            if yy > H - card_h - 4:
                break
            draw_card(dr, cx, yy, colw, card_h, s, now)
            yy += card_h + 8

    img = img.point(lambda p: round(p / 17) * 17); img.save(out); print("saved", out)


def render_detail(s, out, now=None):
    now = now or int(time.time())
    img = Image.new("L", (W, H), WHITE); dr = ImageDraw.Draw(img)
    tr(dr, 20, 16, "‹ 返回", F_BODY, GRAY)
    label = STATE_CN.get(s["state"], s["state"]); cw = dr.textlength(label, font=F_CHIP) + 24
    if s["state"] in ("running", "needs_input"):
        dr.rounded_rectangle([W - 20 - cw, 16, W - 20, 52], radius=8, fill=BLACK); tr(dr, W - 20 - cw + 12, 19, label, F_CHIP, WHITE)
    else:
        dr.rounded_rectangle([W - 20 - cw, 16, W - 20, 52], radius=8, outline=BLACK, width=2); tr(dr, W - 20 - cw + 12, 19, label, F_CHIP, BLACK)
    tr(dr, 130, 14, trunc(dr, ("[C] " if s.get("src") == "codex" else "[A] ") + s.get("project", "?"), F_TITLE, 600), F_TITLE, BLACK)
    dr.line([16, 64, W - 16, 64], fill=BLACK, width=2)

    has_img = s.get("nImages", 0) > 0
    text_w = 560 if has_img else W - 40   # 有图则左文右图

    # 元信息 2x2 网格(限制在左侧文字区)
    y = 78
    meta = [("模型", s.get("model", "-")), ("Token", ftok(s.get("tokens")) or "-"),
            ("耗时", fdur(s.get("elapsed_s")) or "-"), ("最近", fago(s.get("last_activity", now), now))]
    for i, (k, v) in enumerate(meta):
        cx = 20 + (i % 2) * 290; cy = y + (i // 2) * 40
        tr(dr, cx, cy + 3, k, F_SMALL, GRAY); tr(dr, cx + 64, cy, str(v), F_BODY, BLACK)
    y += 84
    dr.line([16, y, text_w, y], fill=LGRAY, width=1); y += 12

    tr(dr, 20, y, "任务", F_SMALL, GRAY); y += 30
    for ln in wrap(dr, s.get("task", "-"), F_BODY, text_w - 24, 2):
        tr(dr, 20, y, ln, F_BODY, BLACK); y += 34
    y += 6
    tr(dr, 20, y, "最新输出", F_SMALL, GRAY); y += 30
    for para in s.get("last_msg", "-").split("\n"):
        for ln in wrap(dr, para or " ", F_RESULT, text_w - 24, 8):
            if y > H - 40: break
            tr(dr, 20, y, ln, F_RESULT, BLACK); y += 31
        if y > H - 40: break

    if has_img:
        # 右侧图片区示意
        ix, iy, iw = 600, 78, W - 600 - 20
        dr.rectangle([ix, iy, ix + iw, H - 20], outline=LGRAY, width=1)
        tr(dr, ix + 10, iy + 8, f"图片 1/{s['nImages']}(轻触换)", F_SMALL, GRAY)
        dr.line([ix + 40, iy + 120, ix + iw - 40, H - 80], fill=GRAY, width=2)
        dr.line([ix + 40, H - 80, ix + iw - 40, iy + 120], fill=GRAY, width=2)

    img = img.point(lambda p: round(p / 17) * 17); img.save(out); print("saved", out)


SAMPLE = {
    "ts": int(time.time()),
    "quota": {
        "codex": {"valid": True, "real": True, "h5": 35, "week": 62, "plan": "prolite",
                   "h5_reset": int(time.time()) + 7200, "week_reset": int(time.time()) + 4 * 86400},
        "claude": {"valid": True, "real": False, "h5": None, "week": None,
                    "h5_tokens": 58_000_000, "week_tokens": 1_333_000_000},
    },
    "sessions": [
        {"src": "claude", "project": "payment-platform", "state": "running", "task": "重构账户资金模型",
         "last_msg": "30/88 批次完成,正在分析 cashier DTO 包,已写出 batch-30.json,继续处理剩余批次。",
         "elapsed_s": 266, "tokens": 918820, "model": "opus-4-8", "last_activity": int(time.time())},
        {"src": "codex", "project": "tt-logistics", "state": "needs_input", "task": "清理废弃接口",
         "last_msg": "是否要我继续删除这 3 个废弃接口?它们仍被 2 个测试引用。", "nImages": 0,
         "elapsed_s": 40, "tokens": 150000, "model": "gpt-5.5", "last_activity": int(time.time())},
        {"src": "claude", "project": "windowsill", "state": "done", "task": "生成战场分镜图",
         "last_msg": "已生成 4 张分镜,输出到 outputs/battle_*.png,平均 2分31秒。", "nImages": 4,
         "elapsed_s": 1210, "tokens": 4000000, "model": "opus-4-7", "last_activity": int(time.time()) - 300},
        {"src": "codex", "project": "airouter", "state": "idle", "task": "say hi",
         "last_msg": "Hi! 有什么可以帮你的?", "elapsed_s": -1, "tokens": 2000, "model": "gpt-5.5",
         "last_activity": int(time.time()) - 3600},
        {"src": "claude", "project": "探索", "state": "idle", "task": "M5PaperS3 面板",
         "last_msg": "固件编译通过,已生成 firmware.bin。", "elapsed_s": -1, "tokens": 19985390,
         "model": "opus-4-6", "last_activity": int(time.time()) - 5400},
        {"src": "codex", "project": "wade son md", "state": "idle", "task": "分镜重构",
         "last_msg": "已按新方案重构 5-2,旧内容标为历史参考。", "elapsed_s": -1, "tokens": 88000,
         "model": "gpt-5.5", "last_activity": int(time.time()) - 800},
    ],
}
DETAIL_SAMPLE = dict(SAMPLE["sessions"][2],
    task="用 ComfyUI 的 Wan2.2 生成 4 张战场分镜图,1280x720,写实风格",
    last_msg="已完成 4 张战场分镜图:\n- battle_01.png 黎明冲锋\n- battle_02.png 近身格斗\n- battle_03.png 战壕炮火\n- battle_04.png 黄昏撤退\n全部输出到 outputs/。5B 模型平均每张 2分31秒。")

if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1].endswith(".json"):
        render_list(json.load(open(sys.argv[1])), "sim_list.png")
    else:
        render_list(SAMPLE, "sim_list.png")
        render_detail(DETAIL_SAMPLE, "sim_detail.png")


def render_notify(ev, out):
    """事件通知全屏卡(横版)。ev: {kind, project, src, msg, meta, ts}"""
    now = ev.get("ts") or int(time.time())
    img = Image.new("L", (W, H), WHITE); dr = ImageDraw.Draw(img)
    kind = ev["kind"]
    TITLES = {"done": "任务完成", "needs_input": "待输入 / 待确认", "quota": "额度告警"}
    # 外框
    dr.rectangle([10, 10, W - 10, H - 10], outline=BLACK, width=4)
    # 顶部类型带(压扁,把空间让给正文)
    band_h = 76
    fill = BLACK if kind in ("needs_input", "quota") else WHITE
    dr.rectangle([10, 10, W - 10, 10 + band_h], fill=fill)
    tcol = WHITE if fill == BLACK else BLACK
    mark = {"done": "✓", "needs_input": "!", "quota": "⚠"}[kind]
    # 左侧标记圆
    dr.ellipse([32, 22, 32 + 52, 22 + 52], outline=tcol, width=4)
    dr.text((32 + 17, 22 + 6), mark, font=font(HEITI_M, 36), fill=tcol)
    dr.text((116, 24), TITLES[kind], font=font(HEITI_M, 44), fill=tcol)
    trr(dr, W - 34, 34, time.strftime("%H:%M", time.localtime(now)), font(PF, 28), tcol)

    y = 10 + band_h + 16
    # 项目(与 meta 同处理:项目名单行)
    tag = "[C] " if ev.get("src") == "codex" else "[A] "
    dr.text((36, y), tag + ev.get("project", "?"), font=font(HEITI_M, 36), fill=BLACK); y += 48
    dr.line([36, y, W - 36, y], fill=LGRAY, width=2); y += 14
    # 正文:尽量多显示。长文自动缩小字号 + 收紧行距,正文区下探到接近底部。
    meta = ev.get("meta", "")
    body_bottom = (H - 52) if meta else (H - 20)
    msg = ev.get("msg", "")
    bsize, lineH = (30, 39) if len(msg) <= 220 else (26, 33)   # 短文大字,长文塞更多
    bfont = font(PF, bsize)
    max_lines = max(1, (body_bottom - y) // lineH)
    lines = []
    for para in msg.split("\n"):
        lines += wrap(dr, para or " ", bfont, W - 80, max_lines - len(lines))
        if len(lines) >= max_lines:
            break
    truncated = len(lines) > max_lines
    for ln in lines[:max_lines]:
        dr.text((36, y), ln, font=bfont, fill=BLACK); y += lineH
    if truncated:                                     # 放不下:提示还有更多(设备上同样处理)
        trr(dr, W - 40, body_bottom - lineH, "…（更多见终端）", F_TINY, GRAY)
    # 底部 meta
    if meta:
        dr.text((36, H - 46), meta, font=font(PF, 26), fill=GRAY)
    img = img.point(lambda p: round(p / 17) * 17); img.save(out); print("saved", out)


if __name__ == "__main__" and "--notify" in sys.argv:
    # 较长正文,演示「尽量多显示正文」；图片/链接在真实链路由 collectors.clean_text 过滤,此处样例已是干净文本
    render_notify({"kind": "done", "src": "claude", "project": "payment-platform",
                   "msg": ("88/88 批次全部完成。账户资金模型分析已写出到 batch-*.json,"
                           "共 3 类异常账户被标记:超额透支 12 户、循环转账 5 户、休眠激活 2 户。"
                           "已生成汇总报告,无报错。建议下一步人工复核循环转账那 5 户的关联图谱。"),
                   "meta": "用时 20m10s · 918k tok · opus-4-8", "ts": int(time.time())}, "notify_done.png")
    render_notify({"kind": "needs_input", "src": "codex", "project": "tt-logistics",
                   "msg": ("是否要我继续删除这 3 个废弃接口?它们仍被 2 个测试引用,"
                           "删除后需同步改测试。涉及:/api/v1/legacy-track、/api/v1/old-eta、"
                           "/api/v1/mock-push。确认后我会一并更新对应单测并跑一遍回归。"),
                   "meta": "gpt-5.5 · 已等待 2m", "ts": int(time.time())}, "notify_input.png")
