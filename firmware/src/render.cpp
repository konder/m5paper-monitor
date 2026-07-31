#include "render.h"
#include "config.h"
#include <M5Unified.h>

// 横版 960(宽) x 540(高),整体 ~1.5x
static const int SCR_W = 960;
static const int SCR_H = 540;
static const int MARGIN = 16;

static const int COL_X[2] = {16, 492};
static const int COL_W = 452;
static const int CARD_H = 126;
static const int CARD_TOP = 130;
static const int CARD_GAP = 8;
static const int MAX_ROWS = 3;   // 双列 x 3 行 = 6 张

static uint32_t C_BLACK, C_WHITE, C_GRAY, C_LGRAY, C_MGRAY;

// 卡片命中记录
static int s_cardX[MAX_SESSIONS_UI], s_cardY[MAX_SESSIONS_UI];
static int s_cardCount = 0;

// 字体:1.5x → 正文 efontCN_24,次要 16,细 14
#define F_TITLE  &fonts::efontCN_24
#define F_PROV   &fonts::efontCN_24
#define F_CARDT  &fonts::efontCN_24
#define F_BODY   &fonts::efontCN_24
#define F_RESULT &fonts::efontCN_24
#define F_PCT    &fonts::efontCN_24
#define F_CHIP   &fonts::efontCN_16
#define F_SMALL  &fonts::efontCN_16
#define F_TINY   &fonts::efontCN_16

void renderInit(bool clearScreen) {
    auto& d = M5.Display;
    d.setRotation(1);          // 横版
    C_BLACK = d.color888(0, 0, 0);
    C_WHITE = d.color888(255, 255, 255);
    C_GRAY  = d.color888(80, 80, 80);    // 加深,空闲文字更清晰
    C_LGRAY = d.color888(170, 170, 170);
    C_MGRAY = d.color888(140, 140, 140);
    d.setEpdMode(epd_mode_t::epd_quality);
    if (clearScreen) d.fillScreen(C_WHITE);   // 深睡唤醒时不清,保留休眠页直到状态板就绪
    d.setTextColor(C_BLACK, C_WHITE);
    d.setTextWrap(false);
}

static String fTok(long t) {
    if (t < 0) return String();
    if (t < 1000) return String(t);
    if (t < 1000000) return String(t / 1000) + "k";
    if (t < 1000000000) { char b[16]; snprintf(b, sizeof(b), "%.1fM", t / 1e6); return String(b); }
    char b[16]; snprintf(b, sizeof(b), "%.1fB", t / 1e9); return String(b);
}
static String fDur(long s) {
    if (s < 0) return String();
    if (s < 60) return String(s) + "s";
    if (s < 3600) return String(s / 60) + "m" + String(s % 60) + "s";
    return String(s / 3600) + "h" + String((s % 3600) / 60) + "m";
}
static String fReset(long r, long now) {
    if (!r) return String();
    long d = r - now; if (d <= 0) return "重置中";
    if (d < 3600) return String(d / 60) + "分";
    if (d < 86400) return String(d / 3600) + "时";
    return String(d / 86400) + "天";
}
static String fAgo(long ts, long now) { long d = now - ts; if (d < 0) d = 0; return fDur(d) + "前"; }

static void setFont(const lgfx::IFont* f) { M5.Display.setFont(f); }
static int tw(const String& s) { return M5.Display.textWidth(s); }
// 加粗:点阵字横向多描一遍,墨水屏上更清晰(透明底,背景已铺好)
static void txt(int x, int y, const String& s, uint32_t c) {
    auto& d = M5.Display;
    d.setTextColor(c);
    d.setCursor(x, y); d.print(s);
    d.setCursor(x + 1, y); d.print(s);
}
static void txtR(int xr, int y, const String& s, uint32_t c) { txt(xr - tw(s), y, s, c); }
static String trunc(String s, int maxw) {
    s.replace("\n", " "); s.replace("\r", " ");
    if (tw(s) <= maxw) return s;
    while (s.length() && tw(s + "…") > maxw) s.remove(s.length() - 1);
    return s + "…";
}
static const char* stateCN(const String& st) {
    if (st == "running") return "运行中";
    if (st == "done") return "完成";
    if (st == "needs_input") return "待输入";
    return "空闲";
}
static void miniBar(int x, int y, int w, int h, float pct) {
    M5.Display.drawRect(x, y, w, h, C_BLACK);
    if (pct >= 0) {
        int fw = (int)((w - 2) * (pct > 100 ? 100 : pct) / 100.0);
        uint32_t col = pct >= 85 ? C_BLACK : C_MGRAY;
        if (fw > 0) M5.Display.fillRect(x + 1, y + 1, fw, h - 2, col);
    }
}

// 列内额度:5h / 周 两行(或 token 估算),返回底部 y
static int drawQuotaCol(int x, int y, const Quota& q, long now) {
    if (!q.valid) { setFont(F_BODY); txt(x, y, "额度 暂无", C_BLACK); return y + 40; }
    if (q.h5 < 0 && !q.real) {
        setFont(F_BODY);
        txt(x, y, "≈5h " + fTok(q.h5_tokens) + "  周 " + fTok(q.week_tokens), C_BLACK);
        setFont(F_SMALL); txt(x, y + 34, "(未校准,显示消耗量)", C_BLACK);
        return y + 70;
    }
    auto& d = M5.Display;
    float used[2] = {q.h5, q.week}; long rst[2] = {q.h5_reset, q.week_reset};
    const char* nm[2] = {"5h", "周"};
    for (int i = 0; i < 2; i++) {
        int yy = y + i * 34;
        int rem = used[i] < 0 ? -1 : (int)(100 - used[i] + 0.5);
        if (rem > 100) rem = 100;
        setFont(F_BODY); txt(x, yy + 3, nm[i], C_BLACK);
        // 油量表:黑填充=剩余
        int bx = x + 46, bw = 168, bh = 24;
        d.drawRect(bx, yy + 2, bw, bh, C_BLACK);
        d.drawRect(bx + 1, yy + 3, bw - 2, bh - 2, C_BLACK);
        if (rem >= 0) {
            int fw = (bw - 6) * rem / 100;
            if (fw > 0) d.fillRect(bx + 3, yy + 5, fw, bh - 6, C_BLACK);
        }
        setFont(F_PCT);
        txt(bx + bw + 14, yy, rem >= 0 ? ("剩" + String(rem) + "%") : String("--"), C_BLACK);
        String rr = fReset(rst[i], now);
        if (rr.length()) { setFont(F_TINY); txt(bx + bw + 150, yy + 3, rr + "后", C_BLACK); }
    }
    return y + 70;
}

static void drawCard(int x, int y, int w, int h, const Session& s, long now) {
    auto& d = M5.Display;
    bool emph = (s.state == "running" || s.state == "needs_input");
    d.drawRect(x, y, w, h, C_BLACK);
    if (emph) { d.drawRect(x + 1, y + 1, w - 2, h - 2, C_BLACK); d.fillRect(x, y, 12, h, C_BLACK); }
    int pad = emph ? 26 : 18;
    int inner = x + pad;

    // 不画状态标签;活跃(运行中/待输入)靠粗边框 + 左侧黑条区分,更干净
    // 项目名(C·/A· 前缀)
    setFont(F_CARDT);
    txt(inner, y + 10, trunc((s.src == "codex" ? "C·" : "A·") + s.project, w - pad - 20), C_BLACK);

    // 结果(全黑)
    setFont(F_RESULT);
    String msg = s.last_msg.length() ? s.last_msg : s.task;
    txt(inner, y + 48, trunc(msg, w - pad - 18), C_BLACK);

    // 元信息(全黑)
    setFont(F_SMALL);
    String foot;
    if (s.state == "running" && s.elapsed_s >= 0) foot = "⏱" + fDur(s.elapsed_s);
    else if (s.last_activity) foot = fAgo(s.last_activity, now);
    if (s.tokens >= 0) foot += "   " + fTok(s.tokens) + " tok";
    if (s.model.length()) foot += "   " + s.model;
    if (s.nImages > 0) foot += "   图" + String(s.nImages);
    txt(inner, y + h - 30, trunc(foot, w - pad - 18), C_BLACK);
}

void renderList(const Snapshot& snap, int batt, bool usb, bool full) {
    auto& d = M5.Display;
    d.setEpdMode(full ? epd_mode_t::epd_quality : epd_mode_t::epd_fast);
    d.startWrite();
    d.fillScreen(C_WHITE);
    long now = snap.ts ? snap.ts : 0;

    // 顶部细条
    setFont(F_TITLE); txt(18, 8, "AI Agent 监控", C_BLACK);
    setFont(F_BODY);
    String st = (snap.hhmm.length() ? ("更新 " + snap.hhmm + "  ·  ") : String(""));
    st += (usb ? "USB" : "电池");
    if (batt >= 0) st += " " + String(batt) + "%";
    st += "  ·  v" + String(FW_VERSION);
    txtR(SCR_W - 18, 10, st, C_BLACK);
    d.drawFastHLine(14, 48, SCR_W - 28, C_BLACK);
    d.drawFastHLine(14, 49, SCR_W - 28, C_BLACK);

    // 左右分栏:CLAUDE | CODEX
    d.drawFastVLine(480, 54, SCR_H - 66, C_LGRAY);
    const char* labels[2] = {"CLAUDE", "CODEX"};
    const char* srcs[2] = {"claude", "codex"};
    const Quota* quotas[2] = {&snap.claude, &snap.codex};
    int colX[2] = {14, 494};
    int colW = 452, cardH = 104;

    for (int col = 0; col < 2; col++) {
        int cx = colX[col];
        int n = 0;
        for (int i = 0; i < snap.nSessions; i++) if (snap.sessions[i].src == srcs[col]) n++;
        setFont(F_CARDT); txt(cx + 4, 56, labels[col], C_BLACK);
        setFont(F_SMALL); txtR(cx + colW, 62, String(n) + " 会话", C_GRAY);
        int y = drawQuotaCol(cx + 4, 96, *quotas[col], now) + 8;
        for (int i = 0; i < snap.nSessions; i++) {
            if (snap.sessions[i].src != srcs[col]) continue;
            if (y > SCR_H - cardH - 6) break;
            drawCard(cx, y, colW, cardH, snap.sessions[i], now);
            y += cardH + 8;
        }
    }
    d.endWrite();
    d.display();
}

int cardIndexAtTouch(int x, int y) {
    for (int i = 0; i < s_cardCount; i++)
        if (x >= s_cardX[i] && x <= s_cardX[i] + COL_W && y >= s_cardY[i] && y <= s_cardY[i] + CARD_H)
            return i;
    return -1;
}

bool versionAtTouch(int x, int y) {
    // 顶部右侧状态区(含 vN)
    return (y <= 48 && x >= SCR_W - 240);
}

// ---------- 详情页(左文右图)----------
static int wrapText(int x, int y, const String& s, int maxw, int lineH, int yLimit, uint32_t col) {
    int i = 0, n = s.length();
    String line;
    while (i < n && y < yLimit) {
        char c = s[i];
        if (c == '\n') { if (line.length()) { txt(x, y, line, col); } y += lineH; line = ""; i++; continue; }
        int clen = 1; unsigned char uc = (unsigned char)c;
        if (uc >= 0xF0) clen = 4; else if (uc >= 0xE0) clen = 3; else if (uc >= 0xC0) clen = 2;
        String ch = s.substring(i, i + clen);
        if (tw(line + ch) > maxw) { txt(x, y, line, col); y += lineH; line = ch; }
        else line += ch;
        i += clen;
    }
    if (line.length() && y < yLimit) { txt(x, y, line, col); y += lineH; }
    return y;
}

void renderDetail(const Session& s, long now, const unsigned char* jpg, unsigned int jpgLen,
                  int imgIdx, int imgCount) {
    auto& d = M5.Display;
    // 交互页用快刷,跟手(有图时那一刻用 quality 保画质)
    d.setEpdMode(jpg && jpgLen ? epd_mode_t::epd_quality : epd_mode_t::epd_fast);
    d.startWrite();
    d.fillScreen(C_WHITE);

    setFont(F_BODY); txt(20, 16, "‹ 返回", C_GRAY);
    String lb = stateCN(s.state); setFont(F_CHIP); int cw = tw(lb) + 24;
    if (s.state == "running" || s.state == "needs_input") { d.fillRoundRect(SCR_W - 20 - cw, 14, cw, 36, 8, C_BLACK); txt(SCR_W - 20 - cw + 12, 18, lb, C_WHITE); }
    else { d.drawRoundRect(SCR_W - 20 - cw, 14, cw, 36, 8, C_BLACK); txt(SCR_W - 20 - cw + 12, 18, lb, C_BLACK); }
    setFont(F_TITLE);
    txt(130, 14, trunc((s.src == "codex" ? "[C] " : "[A] ") + s.project, 600), C_BLACK);
    d.drawFastHLine(16, 62, SCR_W - 32, C_BLACK); d.drawFastHLine(16, 63, SCR_W - 32, C_BLACK);

    bool hasImg = s.nImages > 0;
    int textW = hasImg ? 560 : SCR_W - 40;

    // 元信息 2x2
    int y = 76;
    struct { const char* k; String v; } meta[4] = {
        {"模型", s.model.length() ? s.model : String("-")},
        {"Token", s.tokens >= 0 ? fTok(s.tokens) : String("-")},
        {"耗时", s.elapsed_s >= 0 ? fDur(s.elapsed_s) : String("-")},
        {"最近", fAgo(s.last_activity, now)},
    };
    for (int i = 0; i < 4; i++) {
        int cx = 20 + (i % 2) * 290, cy = y + (i / 2) * 38;
        setFont(F_SMALL); txt(cx, cy + 3, meta[i].k, C_GRAY);
        setFont(F_BODY); txt(cx + 64, cy, meta[i].v, C_BLACK);
    }
    y += 84;
    d.drawFastHLine(16, y, textW, C_LGRAY); y += 10;

    setFont(F_SMALL); txt(20, y, "任务", C_GRAY); y += 30;
    setFont(F_BODY); y = wrapText(20, y, s.task, textW - 24, 32, y + 70, C_BLACK) + 6;
    setFont(F_SMALL); txt(20, y, "最新输出", C_GRAY); y += 30;
    setFont(F_RESULT); wrapText(20, y, s.last_msg.length() ? s.last_msg : "(无)", textW - 24, 30, SCR_H - 20, C_BLACK);

    if (hasImg) {
        int ix = 600, iy = 76, iw = SCR_W - 600 - 20, ih = SCR_H - 76 - 20;
        d.drawRect(ix, iy, iw, ih, C_LGRAY);
        setFont(F_SMALL);
        String cap = "图片 " + String(imgIdx + 1) + "/" + String(imgCount);
        if (imgCount > 1) cap += " (轻触换)";
        txt(ix + 10, iy + 8, cap, C_GRAY);
        if (jpg && jpgLen) d.drawJpg(jpg, jpgLen, ix + 6, iy + 40, iw - 12, ih - 48);
        else txt(ix + 10, iy + 44, "加载中/失败", C_GRAY);
    }
    d.endWrite();
    d.display();
}

static const char* notifyTitle(const String& k) {
    if (k == "done") return "任务完成";
    if (k == "needs_input") return "待输入 / 待确认";
    if (k == "quota") return "额度告警";
    return "通知";
}

void renderNotify(const String& kind, const String& src, const String& project,
                  const String& msg, const String& meta, long ts) {
    auto& d = M5.Display;
    d.wakeup();                              // epd_poweron
    d.setEpdMode(epd_mode_t::epd_quality);   // 通知要清晰,值得一次全刷
    d.startWrite();
    d.fillScreen(C_WHITE);

    // 外框
    for (int i = 0; i < 4; i++) d.drawRect(10 + i, 10 + i, SCR_W - 20 - 2 * i, SCR_H - 20 - 2 * i, C_BLACK);

    // 顶部类型带(压扁,把空间让给正文):紧急(needs_input/quota)反白
    bool urgent = (kind != "done");
    int bx = 14, by = 14, bw = SCR_W - 28, bh = 76;
    uint32_t bandbg = urgent ? C_BLACK : C_WHITE;
    uint32_t bandfg = urgent ? C_WHITE : C_BLACK;
    if (urgent) d.fillRect(bx, by, bw, bh, C_BLACK);
    d.drawFastHLine(bx, by + bh, bw, C_BLACK);

    // 左侧标记圆 + 记号
    int cxp = 34, cyp = by + 14, cs = 48;
    d.drawCircle(cxp + cs / 2, cyp + cs / 2, cs / 2, bandfg);
    d.drawCircle(cxp + cs / 2, cyp + cs / 2, cs / 2 - 1, bandfg);
    if (kind == "done") {                        // 画对勾
        int mx = cxp + 12, my = cyp + 26;
        for (int t = 0; t < 3; t++) {
            d.drawLine(mx + t, my, mx + 9 + t, my + 9, bandfg);
            d.drawLine(mx + 9 + t, my + 9, mx + 26 + t, my - 10, bandfg);
        }
    } else {                                     // 惊叹号
        setFont(&fonts::efontCN_24); d.setTextSize(1.5);
        d.setTextColor(bandfg, bandbg);
        d.setCursor(cxp + cs / 2 - 5, cyp + 8); d.print("!");
        d.setTextSize(1);
    }

    // 标题
    setFont(&fonts::efontCN_24); d.setTextSize(1.7);
    d.setTextColor(bandfg, bandbg);
    d.setCursor(100, by + 18); d.print(notifyTitle(kind));
    d.setTextSize(1);
    // 时间(右)
    setFont(F_SMALL);
    char tb[8];
    long hh = (ts % 86400) / 3600, mm = (ts % 3600) / 60;  // 粗略时(UTC),仅示意
    snprintf(tb, sizeof(tb), "%02ld:%02ld", hh, mm);
    d.setTextColor(bandfg, bandbg);
    d.setCursor(SCR_W - 92, by + 26); d.print(tb);
    d.setTextColor(C_BLACK, C_WHITE);

    // 项目(单行)
    int y = by + bh + 14;
    setFont(F_TITLE);
    txt(36, y, trunc((src == "codex" ? "[C] " : "[A] ") + project, SCR_W - 72), C_BLACK);
    y += 44;
    d.drawFastHLine(36, y, SCR_W - 72, C_LGRAY); y += 14;

    // 正文:短文大字(24),长文小字(16)+收紧行距,正文区下探到接近底部,尽量多显示
    bool longMsg = msg.length() > 220;
    setFont(longMsg ? F_SMALL : F_BODY);
    int lineH = longMsg ? 26 : 38;
    int bodyBottom = meta.length() ? (SCR_H - 52) : (SCR_H - 22);
    wrapText(36, y, msg, SCR_W - 72, lineH, bodyBottom, C_BLACK);

    // 底部 meta
    if (meta.length()) { setFont(F_SMALL); txt(36, SCR_H - 46, trunc(meta, SCR_W - 72), C_GRAY); }

    d.endWrite();
    d.display();
    d.waitDisplay();
    d.sleep();                               // epd_poweroff:弹卡后断升压(画面保留)
}

// ---------- 待命屏:最近事件历史列表 ----------
static const char* kindTag(const String& k) {
    if (k == "done") return "完成";
    if (k == "needs_input") return "待输入";
    if (k == "quota") return "额度";
    return "事件";
}

void renderIdle(const EventItem* items, int n, int batteryPct, const char* link,
                int fwVersion, bool full) {
    auto& d = M5.Display;
    d.setEpdMode(full ? epd_mode_t::epd_quality : epd_mode_t::epd_fast);
    d.startWrite();
    d.fillScreen(C_WHITE);

    // 顶栏:标题 + 右侧 BLE/电量/版本
    setFont(F_TITLE);
    txt(18, 10, "待命中", C_BLACK);
    setFont(F_SMALL);
    String rt = String(link);
    if (batteryPct >= 0) rt += "  " + String(batteryPct) + "%";
    rt += "  v" + String(fwVersion);
    txtR(SCR_W - 18, 18, rt, C_BLACK);
    d.drawFastHLine(14, 50, SCR_W - 28, C_BLACK);
    d.drawFastHLine(14, 51, SCR_W - 28, C_BLACK);

    if (n <= 0) {
        setFont(F_TITLE);
        txt(SCR_W / 2 - 120, SCR_H / 2 - 20, "等待事件…", C_MGRAY);
        d.endWrite(); d.display();
        return;
    }

    // 列表:最新在上,每行一条(标签 chip + 项目 + 摘要 ... 时间)
    long now = 0;   // 无 RTC,用最新一条 ts 作相对基准
    if (n > 0) now = items[0].ts;
    int rowH = 56, y = 62;
    int rows = n < HISTORY_MAX ? n : HISTORY_MAX;
    for (int i = 0; i < rows && y + rowH <= SCR_H - 8; i++) {
        const EventItem& e = items[i];
        bool urgent = (e.kind != "done");
        // 标签 chip
        setFont(F_CHIP);
        String tag = kindTag(e.kind);
        int cw = tw(tag) + 20;
        if (urgent) { d.fillRoundRect(18, y + 4, cw, 34, 8, C_BLACK); txt(18 + 10, y + 8, tag, C_WHITE); }
        else        { d.drawRoundRect(18, y + 4, cw, 34, 8, C_BLACK); txt(18 + 10, y + 8, tag, C_BLACK); }
        int tx0 = 18 + cw + 12;
        // 时间(右)
        String ago = e.ts ? fAgo(e.ts, now) : String();
        int rightPad = 0;
        if (ago.length()) { setFont(F_SMALL); rightPad = tw(ago) + 14; txtR(SCR_W - 18, y + 4, ago, C_GRAY); }
        // 项目(粗)
        setFont(F_BODY);
        String proj = (e.src == "codex" ? "[C] " : "[A] ") + e.project;
        int projW = tw(proj + " ");
        txt(tx0, y + 2, trunc(proj, SCR_W - 18 - rightPad - tx0), C_BLACK);
        // 摘要(次行,小字)
        setFont(F_SMALL);
        txt(tx0, y + 30, trunc(e.summary, SCR_W - 18 - tx0), C_GRAY);
        (void)projW;
        if (i < rows - 1) d.drawFastHLine(18, y + rowH - 2, SCR_W - 36, C_LGRAY);
        y += rowH;
    }

    d.endWrite();
    d.display();
}

// ---------- v39 消耗看板 ----------
// 通用一行油量表。几何沿用 drawQuotaCol 的约定(双描边外框、**黑填充 = 剩余**),
// 只是整屏单列所以条子拉宽。所有语义判断都在 gateway 做完了,这里纯画。
static void drawGaugeRow(int y, int h, const UsageRow& r) {
    auto& d = M5.Display;
    const int LAB_X = 24;      // 标签
    const int BAR_X = 196;     // 油量表
    const int BAR_W = 380;
    const int BAR_H = 38;
    const int PCT_X = BAR_X + BAR_W + 20;   // 剩N%
    const int NOTE_X = PCT_X + 108;         // 备注小字
    const int RIGHT = SCR_W - 24;           // rin 右对齐到此

    int by = y + (h - BAR_H) / 2;           // 条在行内垂直居中
    int ty24 = by + (BAR_H - 24) / 2;       // 24px 字基线
    int ty16 = by + (BAR_H - 16) / 2;       // 16px 字基线

    setFont(F_TITLE);
    txt(LAB_X, ty24, trunc(r.l, BAR_X - LAB_X - 12), C_BLACK);

    if (r.rem >= 0) {
        // 已知:黑色双描边 + 黑填充=剩余
        d.drawRect(BAR_X, by, BAR_W, BAR_H, C_BLACK);
        d.drawRect(BAR_X + 1, by + 1, BAR_W - 2, BAR_H - 2, C_BLACK);
        int p = r.rem > 100 ? 100 : r.rem;
        int fw = (BAR_W - 6) * p / 100;
        if (fw > 0) d.fillRect(BAR_X + 3, by + 3, fw, BAR_H - 6, C_BLACK);
    } else {
        // 未知(未校准/数据旧):浅灰单描边空条。
        // ⚠️ 不能画成黑色空条 —— 油量表里"黑框空槽"读起来是「余量耗尽」,
        // 和真实含义(不知道)正好相反,会误报警。浅灰 = 此表无读数。
        d.drawRect(BAR_X, by, BAR_W, BAR_H, C_LGRAY);
    }

    setFont(F_PCT);
    txt(PCT_X, ty24, r.rem >= 0 ? ("剩" + String(r.rem) + "%") : String("--"), C_BLACK);

    // 右侧:rin 右对齐,备注挤在它左边(先量 rin 宽度才知道备注能占多少)
    setFont(F_SMALL);
    int rinW = 0;
    if (r.rin.length()) {
        rinW = tw(r.rin) + 14;
        txtR(RIGHT, ty16, r.rin, C_GRAY);
    }
    if (r.n.length()) {
        int avail = RIGHT - rinW - NOTE_X;
        if (avail > 20) txt(NOTE_X, ty16, trunc(r.n, avail), C_GRAY);
    }
}

void renderUsage(const Usage& u, int batteryPct, const char* link, int fwVersion, bool full) {
    auto& d = M5.Display;
    // ⚠️ 必须 wakeup:电池页/通知卡结尾都 sleep() 断了 EPD 升压,不开电什么都画不出来。
    // (renderIdle 历史上漏了这句,只是靠"上一次渲染恰好还供着电"侥幸能用。)
    d.wakeup();
    d.setEpdMode(full ? epd_mode_t::epd_quality : epd_mode_t::epd_fast);
    d.startWrite();
    d.fillScreen(C_WHITE);

    // 顶栏:标题 + 右侧 时钟/链路/电量/版本
    setFont(F_TITLE);
    txt(18, 10, "AI 消耗看板", C_BLACK);
    setFont(F_SMALL);
    String rt;
    if (u.hhmm.length()) rt = u.hhmm + "   ";
    rt += String(link);
    if (batteryPct >= 0) rt += "  " + String(batteryPct) + "%";
    rt += "  v" + String(fwVersion);
    txtR(SCR_W - 18, 18, rt, C_BLACK);
    d.drawFastHLine(14, 50, SCR_W - 28, C_BLACK);
    d.drawFastHLine(14, 51, SCR_W - 28, C_BLACK);

    const int ROW_TOP = 60;
    const int FOOT_Y = 470;

    if (u.n <= 0) {
        setFont(F_TITLE);
        txt(SCR_W / 2 - 140, SCR_H / 2 - 20, "等待用量数据…", C_MGRAY);
        d.endWrite(); d.display(); d.waitDisplay(); d.sleep();
        return;
    }

    // 行高按行数自适应(4 行 → 102,5-6 行 → 更紧),避免多一个窗口就排版溢出
    int rowH = (FOOT_Y - ROW_TOP) / u.n;
    if (rowH > 110) rowH = 110;
    if (rowH < 54) rowH = 54;

    for (int i = 0; i < u.n; i++) {
        int y = ROW_TOP + i * rowH;
        drawGaugeRow(y, rowH, u.rows[i]);
        if (i < u.n - 1) d.drawFastHLine(24, y + rowH - 1, SCR_W - 48, C_LGRAY);
    }

    // 底部:LiteLLM 单行(只有 token/请求数 —— 该 proxy 的 spend 恒为 0,故意不显示金额)
    d.drawFastHLine(14, FOOT_Y + 4, SCR_W - 28, C_BLACK);
    setFont(F_BODY);
    txt(24, FOOT_Y + 18, trunc(u.foot, SCR_W - 48), C_BLACK);

    d.endWrite();
    d.display();
    d.waitDisplay();
    d.sleep();       // epd_poweroff:画完断升压(画面双稳态保留),FW29 实测的省电关键
}

// 电池模式待机页:大字电量 + 电池条 + 充电状态/电压。渲染后断 EPD 电源省电(画面双稳态保留)。
void renderBatteryPage(int batteryPct, int mv, bool charging, const char* link, int fwVersion) {
    auto& d = M5.Display;
    d.wakeup();                              // epd_poweron:开墨水屏升压再刷
    d.setEpdMode(epd_mode_t::epd_quality);
    d.startWrite();
    d.fillScreen(C_WHITE);
    int cx = SCR_W / 2;

    // 顶栏:待命中 + 右上 NET/版本
    setFont(F_TITLE);
    txt(18, 10, "待命中", C_BLACK);
    setFont(F_SMALL);
    txtR(SCR_W - 18, 18, String(link) + "  v" + String(fwVersion), C_BLACK);
    d.drawFastHLine(14, 50, SCR_W - 28, C_BLACK);
    d.drawFastHLine(14, 51, SCR_W - 28, C_BLACK);

    // 超大号电量
    String pct = (batteryPct >= 0 ? String(batteryPct) : String("--")) + "%";
    setFont(F_TITLE); d.setTextSize(4);      // efontCN_24 × 4 ≈ 96px
    txt(cx - tw(pct) / 2, SCR_H / 2 - 150, pct, C_BLACK);
    d.setTextSize(1);

    // 电池条(带正极头)
    int bw = 460, bh = 50, bx = cx - bw / 2, by = SCR_H / 2 - 26;
    d.drawRoundRect(bx, by, bw, bh, 8, C_BLACK);
    d.drawRoundRect(bx + 1, by + 1, bw - 2, bh - 2, 8, C_BLACK);
    d.fillRect(bx + bw + 5, by + 15, 12, bh - 30, C_BLACK);   // 电池头
    if (batteryPct >= 0) {
        int p = batteryPct > 100 ? 100 : batteryPct;
        int fw = (bw - 12) * p / 100;
        if (fw > 0) d.fillRect(bx + 6, by + 6, fw, bh - 12, C_BLACK);
    }

    // 状态行:充电中 / 电池供电 + 电压
    setFont(F_CARDT);
    String s1 = charging ? "充电中" : "电池供电";
    txt(cx - tw(s1) / 2, SCR_H / 2 + 56, s1, C_BLACK);
    if (mv > 0) {
        setFont(F_SMALL);
        String s2 = String(mv / 1000.0, 2) + " V";
        txt(cx - tw(s2) / 2, SCR_H / 2 + 104, s2, C_GRAY);
    }

    d.endWrite();
    d.display();
    d.waitDisplay();
    d.sleep();                               // epd_poweroff:待机切断升压省电
}

void renderSleep(int batteryPct, const String& wakeAt) {
    auto& d = M5.Display;
    d.setEpdMode(epd_mode_t::epd_quality);   // 睡前一次全刷,画面干净保留数分钟
    d.startWrite();
    d.fillScreen(C_WHITE);
    int cx = SCR_W / 2;

    // 超大号电量
    String pct = (batteryPct >= 0 ? String(batteryPct) : String("--")) + "%";
    setFont(F_TITLE); d.setTextSize(4);      // efontCN_24 × 4 ≈ 96px
    txt(cx - tw(pct) / 2, SCR_H / 2 - 150, pct, C_BLACK);
    d.setTextSize(1);

    // 电池条(带正极头)
    int bw = 460, bh = 50, bx = cx - bw / 2, by = SCR_H / 2 - 26;
    d.drawRoundRect(bx, by, bw, bh, 8, C_BLACK);
    d.drawRoundRect(bx + 1, by + 1, bw - 2, bh - 2, 8, C_BLACK);
    d.fillRect(bx + bw + 5, by + 15, 12, bh - 30, C_BLACK);   // 电池头
    if (batteryPct >= 0) {
        int p = batteryPct > 100 ? 100 : batteryPct;
        int fw = (bw - 12) * p / 100;
        if (fw > 0) d.fillRect(bx + 6, by + 6, fw, bh - 12, C_BLACK);
    }

    // 休眠 + 下次唤醒
    setFont(F_CARDT);
    String s1 = "休眠中";
    txt(cx - tw(s1) / 2, SCR_H / 2 + 56, s1, C_BLACK);
    setFont(F_SMALL);
    String s2 = wakeAt.length() ? ("下次唤醒 约 " + wakeAt + "  ·  触摸可提前")
                                : "触摸屏幕或稍后自动唤醒";
    txt(cx - tw(s2) / 2, SCR_H / 2 + 100, s2, C_BLACK);

    d.endWrite();
    d.display();
    d.waitDisplay();
}

void renderStatus(const char* msg) {
    auto& d = M5.Display;
    d.wakeup();                              // epd_poweron
    d.setEpdMode(epd_mode_t::epd_fast);
    d.fillRect(0, 0, SCR_W, 48, C_WHITE);
    setFont(F_BODY); txt(MARGIN, 12, msg, C_BLACK);
    d.display();
    d.waitDisplay();
    d.sleep();                               // epd_poweroff
}
