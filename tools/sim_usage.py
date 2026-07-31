#!/usr/bin/env python3
"""消耗看板(v39 renderUsage)版式校验 + 预览。

为什么不复用 sim_render.py:那个模拟器的字号是"看起来像"调出来的
(F_TITLE=HEITI 40 对应固件的 efontCN_24),字宽和设备不一致,**查不出溢出**。

efontCN 是点阵等宽字库,宽度完全可算:
  efontCN_24 → 半宽(ASCII)12px,全宽(CJK)24px
  efontCN_16 → 半宽 8px,全宽 16px
所以这里直接按设备真实字宽算,能精确判断截断/碰撞;PNG 也按同样的步进逐字画,
预览与设备一致。

用法:
  python3 tools/sim_usage.py usage.json      # usage.json = collector --print 里的 usage 段
  python3 tools/sim_usage.py                 # 用内置样例(含长字符串压力测试)
产出 sim_usage.png,并在 stdout 报告每行的溢出情况。
"""
import json
import sys

from PIL import Image, ImageDraw, ImageFont

W, H = 960, 540
BLACK, WHITE, GRAY, LGRAY, MGRAY = 0, 255, 80, 170, 140

# ---- 与 firmware/src/render.cpp 的 renderUsage / drawGaugeRow 严格一致 ----
LAB_X = 24
BAR_X = 196
BAR_W = 380
BAR_H = 38
PCT_X = BAR_X + BAR_W + 20      # 596
NOTE_X = PCT_X + 108            # 704
RIGHT = W - 24                  # 936
ROW_TOP = 60
FOOT_Y = 470

CJK_FONT = "/System/Library/Fonts/PingFang.ttc"


def is_wide(ch: str) -> bool:
    """efontCN 的全宽判定:CJK / 全角标点 / 假名 等占满格,ASCII 占半格。"""
    o = ord(ch)
    return o >= 0x1100 and not (0xFF61 <= o <= 0xFFDC)


def ew(s: str, size: int) -> int:
    """按 efontCN 点阵网格算字符串像素宽(size=24 或 16)。"""
    return sum(size if is_wide(c) else size // 2 for c in s)


def etrunc(s: str, size: int, maxw: int) -> str:
    """复刻固件 trunc():按宽度逐字砍,末尾加省略号。"""
    s = s.replace("\n", " ").replace("\r", " ")
    if ew(s, size) <= maxw:
        return s
    while s and ew(s + "…", size) > maxw:
        s = s[:-1]
    return s + "…"


def draw_grid_text(dr, x, y, s, size, fill):
    """按 efont 网格逐字画,步进与设备一致(不用 PIL 的 kerning/advance)。"""
    f = ImageFont.truetype(CJK_FONT, size)
    for ch in s:
        dr.text((x, y), ch, font=f, fill=fill)
        x += size if is_wide(ch) else size // 2
    return x


def draw_grid_text_r(dr, xr, y, s, size, fill):
    return draw_grid_text(dr, xr - ew(s, size), y, s, size, fill)


def draw_gauge_row(dr, y, h, r, problems):
    by = y + (h - BAR_H) // 2
    ty24 = by + (BAR_H - 24) // 2
    ty16 = by + (BAR_H - 16) // 2

    lab_budget = BAR_X - LAB_X - 12          # 160
    lab = etrunc(r["l"], 24, lab_budget)
    if lab != r["l"]:
        problems.append(f"  ⚠ 标签被截断: {r['l']!r} → {lab!r} (预算 {lab_budget}px, 实需 {ew(r['l'],24)}px)")
    draw_grid_text(dr, LAB_X, ty24, lab, 24, BLACK)

    # 油量表:黑填充 = 剩余;rem<0 用浅灰单描边(黑空条会被误读成"余量耗尽")
    rem = r.get("rem", -1)
    if rem >= 0:
        dr.rectangle([BAR_X, by, BAR_X + BAR_W, by + BAR_H], outline=BLACK, width=2)
        fw = (BAR_W - 6) * min(100, rem) // 100
        if fw > 0:
            dr.rectangle([BAR_X + 3, by + 3, BAR_X + 3 + fw, by + BAR_H - 3], fill=BLACK)
    else:
        dr.rectangle([BAR_X, by, BAR_X + BAR_W, by + BAR_H], outline=LGRAY, width=1)

    pct = f"剩{rem}%" if rem >= 0 else "--"
    pct_end = PCT_X + ew(pct, 24)
    if pct_end > NOTE_X:
        problems.append(f"  ⚠ 剩N% 撞到备注区: {pct!r} 到 {pct_end}px > NOTE_X {NOTE_X}")
    draw_grid_text(dr, PCT_X, ty24, pct, 24, BLACK)

    rin = r.get("rin") or ""
    rin_w = ew(rin, 16) + 14 if rin else 0
    if rin:
        draw_grid_text_r(dr, RIGHT, ty16, rin, 16, GRAY)

    note = r.get("n") or ""
    if note:
        avail = RIGHT - rin_w - NOTE_X
        if avail > 20:
            nt = etrunc(note, 16, avail)
            if nt != note:
                problems.append(f"  ⚠ 备注被截断: {note!r} → {nt!r} (可用 {avail}px, 实需 {ew(note,16)}px)")
            draw_grid_text(dr, NOTE_X, ty16, nt, 16, GRAY)
        else:
            problems.append(f"  ⚠ 备注完全没地方放: {note!r} (可用 {avail}px, rin={rin!r})")


def render(u, out="sim_usage.png"):
    img = Image.new("L", (W, H), WHITE)
    dr = ImageDraw.Draw(img)
    problems = []

    # 顶栏
    draw_grid_text(dr, 18, 10, "AI 消耗看板", 24, BLACK)
    rt = (u.get("hhmm", "") + "   ") if u.get("hhmm") else ""
    rt += "WiFi  87%  v39"
    draw_grid_text_r(dr, W - 18, 18, rt, 16, BLACK)
    dr.rectangle([14, 50, W - 14, 51], fill=BLACK)

    rows = u.get("rows", [])
    n = len(rows)
    if n == 0:
        problems.append("  ⚠ rows 为空")
    else:
        row_h = max(54, min(110, (FOOT_Y - ROW_TOP) // n))
        print(f"rows={n}  row_h={row_h}  末行底部={ROW_TOP + n*row_h}px (FOOT_Y={FOOT_Y})")
        if ROW_TOP + n * row_h > FOOT_Y + 2:
            problems.append(f"  ⚠ 行区溢出到 foot: {ROW_TOP + n*row_h} > {FOOT_Y}")
        for i, r in enumerate(rows):
            y = ROW_TOP + i * row_h
            print(f"  [{i}] {r['l']!r} rem={r.get('rem')} n={r.get('n')!r} rin={r.get('rin')!r}")
            draw_gauge_row(dr, y, row_h, r, problems)
            if i < n - 1:
                dr.line([24, y + row_h - 1, W - 24, y + row_h - 1], fill=LGRAY, width=1)

    # 底部 LiteLLM 单行
    dr.rectangle([14, FOOT_Y + 4, W - 14, FOOT_Y + 5], fill=BLACK)
    foot = u.get("foot", "")
    ft = etrunc(foot, 24, W - 48)
    if ft != foot:
        problems.append(f"  ⚠ foot 被截断: {foot!r} → {ft!r} (实需 {ew(foot,24)}px, 可用 {W-48}px)")
    draw_grid_text(dr, 24, FOOT_Y + 18, ft, 24, BLACK)

    img = img.point(lambda p: round(p / 17) * 17)
    img.save(out)
    print("saved", out)
    if problems:
        print("\n版式问题:")
        for p in problems:
            print(p)
    else:
        print("\n✅ 无溢出/碰撞")
    return problems


# 压力样例:最长可能的标签/备注/foot,以及 rem=-1、rem=100 边界
STRESS = {
    "hhmm": "17:45",
    "rows": [
        {"l": "Codex 5h", "rem": 100, "rin": "4时后", "n": "prolite"},
        {"l": "Codex 周", "rem": 52, "rin": "5天后", "n": "prolite"},
        {"l": "Claude 5h", "rem": -1, "rin": "", "n": "无活动"},
        {"l": "Claude 周", "rem": -1, "rin": "1天后", "n": "1.4B tok"},
        {"l": "网关", "rem": 58, "rin": "8-17重置", "n": "旧 831/2000G"},
    ],
    "foot": "LiteLLM 7日 107.0M tok · 7617 req · 今日 706k",
}

if __name__ == "__main__":
    if len(sys.argv) > 1:
        doc = json.load(open(sys.argv[1]))
        u = doc.get("usage", doc)
        render(u)
    else:
        render(STRESS)
