#pragma once
#include <Arduino.h>
#include <ArduinoJson.h>

#include "config.h"
#include "types.h"

// 事件 / 消耗看板的解析与状态机。
//
// 这里的东西**不碰屏幕、不碰射频、不碰睡眠** —— 从 main.cpp 抽出来就是为了能在
// native 单测里原样跑(见 firmware/test/test_logic)。渲染、模式切换、OTA 留在 main.cpp。
//
// 注意是「移动」不是「复制」:main.cpp 通过 extern 引用同一份状态。
// 复制一份到测试里会造出第二个会漂移的实现 —— tools/sim_render.py 已经吃过这个亏。

// ---- 状态(定义在 logic.cpp)----
extern EventItem g_hist[HISTORY_MAX];
extern int       g_histN;
extern bool      g_idleDirty;

extern Usage g_usage;
extern bool  g_usageDirty;

extern volatile bool g_haveEvent;
extern bool          g_evLive;
extern String        g_evKind, g_evSrc, g_evProject, g_evMsg, g_evMeta;
extern long          g_evTs;

extern volatile bool g_doOta;
extern volatile bool g_doDump;

// ---- 纯逻辑 ----

// 取正文首行、trim、截到 160 字符(历史列表一行放得下)
String summarize(const String& msg);

// 历史环形缓冲:新的插到 [0],其余后移,满 HISTORY_MAX 后丢最旧的
void addHistory(const String& kind, const String& src, const String& project,
                const String& msg, long ts);

// 事件置位(BLE / MQTT 两条路径统一走这里),只置标志,渲染交给 loop()
void setEvent(const char* kind, const char* src, const char* project,
              const char* msg, const char* meta, long ts, bool live);

// 看板解析(BLE / MQTT 共用一份;两条路径必须行为一致)。
// 第一道节流在这:rev 与上次相同 = gateway 的保底心跳,内容没变 → 直接返回不置 dirty。
void applyUsage(JsonDocument& doc);

// BLE 桥消息:{"t":"ev"|"cmd"|"usage", ...}
void handleBleMessage(const String& s);

// MQTT 回调(签名必须匹配 PubSubClient::setCallback)
void onMessage(char* topic, byte* payload, unsigned int len);

// 看板重绘的**第二道**节流(第一道是上面的 rev)。
// 原本是 loop() 里的两个函数级 static,抽成结构体纯粹为了可测 —— 语义不变:
// painted 保证第一帧立刻画(不用干等 USAGE_REPAINT_MIN_MS),之后才压最短间隔。
struct UsageRepaintGate {
    uint32_t lastPaint = 0;
    bool     painted   = false;

    // 返回 true 表示「该重绘了」,并已记账。now 用 millis()。
    bool shouldPaint(uint32_t now, bool usb, bool idleView, bool dirty, uint32_t minMs);
};

#if defined(NATIVE_TEST)
// 仅 native 单测:把上面所有状态清回初值,保证用例之间互不污染。
// 用 #if 圈住,设备固件里不会有这段。
void resetLogicStateForTest();
#endif
