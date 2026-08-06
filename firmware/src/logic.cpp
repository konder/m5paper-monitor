#include "logic.h"

// ---- 状态 ----
EventItem g_hist[HISTORY_MAX];
int       g_histN = 0;
bool      g_idleDirty = true;

// v39 消耗看板。注意全刷计数器 g_usageRenders 留在 main.cpp —— 那是渲染节奏,不是解析状态。
Usage g_usage;
bool  g_usageDirty = false;

volatile bool g_haveEvent = false;
bool          g_evLive = true;
String        g_evKind, g_evSrc, g_evProject, g_evMsg, g_evMeta;
long          g_evTs = 0;

volatile bool g_doOta = false;
volatile bool g_doDump = false;   // 调试:回传当前屏幕给网关(见 ota.cpp postScreenDump)

// ---- 纯逻辑 ----

String summarize(const String& msg) {
    String s = msg; int nl = s.indexOf('\n');
    if (nl >= 0) s = s.substring(0, nl);
    s.trim();
    if (s.length() > 160) s = s.substring(0, 160);
    return s;
}

void addHistory(const String& kind, const String& src, const String& project,
                const String& msg, long ts) {
    for (int i = HISTORY_MAX - 1; i > 0; i--) g_hist[i] = g_hist[i - 1];
    g_hist[0].kind = kind; g_hist[0].src = src; g_hist[0].project = project;
    g_hist[0].summary = summarize(msg); g_hist[0].ts = ts;
    if (g_histN < HISTORY_MAX) g_histN++;
    g_idleDirty = true;
}

void setEvent(const char* kind, const char* src, const char* project,
              const char* msg, const char* meta, long ts, bool live) {
    g_evKind = kind; g_evSrc = src; g_evProject = project;
    g_evMsg = msg; g_evMeta = meta; g_evTs = ts; g_evLive = live;
    g_haveEvent = true;
}

// 只置 dirty 位,不在这里渲染 —— 这个函数分别跑在 NimBLE 主机任务和 mqtt.loop() 里,
// 塞一次 1-2s 的墨水屏全刷进去会直接饿死协议栈。
void applyUsage(JsonDocument& doc) {
    int rev = doc["rev"] | -1;
    if (rev >= 0 && rev == g_usage.rev) {
        Serial.printf("[usage] rev=%d 心跳,内容未变,不重绘\n", rev);
        return;                       // gateway 的保底心跳,内容没变 → 不刷墨水屏
    }
    g_usage.rev  = rev;
    g_usage.ts   = doc["ts"] | 0;
    g_usage.hhmm = (const char*)(doc["hhmm"] | "");
    g_usage.foot = (const char*)(doc["foot"] | "");
    int n = 0;
    for (JsonObject r : doc["rows"].as<JsonArray>()) {
        if (n >= USAGE_ROWS_MAX) break;
        g_usage.rows[n].l   = (const char*)(r["l"]   | "");
        g_usage.rows[n].n   = (const char*)(r["n"]   | "");
        g_usage.rows[n].rin = (const char*)(r["rin"] | "");
        g_usage.rows[n].rem = r["rem"] | -1;
        n++;
    }
    g_usage.n = n;
    g_usage.valid = (n > 0);
    g_usageDirty = true;
    Serial.printf("[usage] rx rev=%d rows=%d hhmm=%s\n", rev, n, g_usage.hhmm.c_str());
}

void handleBleMessage(const String& s) {
    JsonDocument doc;
    if (deserializeJson(doc, s)) return;
    const char* t = doc["t"] | "";
    if (!strcmp(t, "cmd")) {
        String cmd((const char*)(doc["cmd"] | ""));
        if (cmd.indexOf("ota") >= 0) g_doOta = true;
        if (cmd.indexOf("dump") >= 0) g_doDump = true;
        return;
    }
    if (!strcmp(t, "usage")) { applyUsage(doc); return; }
    if (strcmp(t, "ev")) return;
    setEvent(doc["kind"] | "done", doc["src"] | "", doc["project"] | "?",
             doc["msg"] | "", doc["meta"] | "", doc["ts"] | 0, doc["live"] | true);
    Serial.printf("[ble-ev] kind=%s proj=%s live=%d\n", g_evKind.c_str(), g_evProject.c_str(), (int)g_evLive);
}

void onMessage(char* topic, byte* payload, unsigned int len) {
    if (!strcmp(topic, TOPIC_CMD)) {
        String c((const char*)payload, len);
        if (c.indexOf("ota") >= 0) g_doOta = true;
        if (c.indexOf("dump") >= 0) g_doDump = true;
        return;
    }
    if (!strcmp(topic, TOPIC_USAGE)) {
        JsonDocument ud;
        if (deserializeJson(ud, payload, len)) return;
        applyUsage(ud);
        return;
    }
    if (strcmp(topic, TOPIC_EVENT)) return;
    JsonDocument doc;
    if (deserializeJson(doc, payload, len)) return;
    setEvent(doc["kind"] | "done", doc["src"] | "", doc["project"] | "?",
             doc["msg"] | "", doc["meta"] | "", doc["ts"] | 0, true);
    Serial.printf("[ev] rx kind=%s proj=%s\n", g_evKind.c_str(), g_evProject.c_str());
}

bool UsageRepaintGate::shouldPaint(uint32_t now, bool usb, bool idleView,
                                   bool dirty, uint32_t minMs) {
    if (!(usb && idleView && dirty)) return false;
    if (painted && (uint32_t)(now - lastPaint) < minMs) return false;
    lastPaint = now;
    painted   = true;
    return true;
}

#if defined(NATIVE_TEST)
void resetLogicStateForTest() {
    for (int i = 0; i < HISTORY_MAX; i++) g_hist[i] = EventItem();
    g_histN = 0;
    g_idleDirty = true;
    g_usage = Usage();
    g_usageDirty = false;
    g_haveEvent = false;
    g_evLive = true;
    g_evKind = ""; g_evSrc = ""; g_evProject = ""; g_evMsg = ""; g_evMeta = "";
    g_evTs = 0;
    g_doOta = false;
    g_doDump = false;
}
#endif
