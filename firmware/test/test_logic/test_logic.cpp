// logic.cpp 的纯逻辑单测 —— 跑在宿主机,不需要设备、不需要 SDL。
//   pio test -e native
//
// 这里测的是**设备上跑的同一份源码**(logic.cpp 被直接编进来),不是另写一份复刻。
#include <unity.h>

#include <Arduino.h>
#include <ArduinoJson.h>

#include "config.h"
#include "logic.h"

void setUp(void) {
    resetLogicStateForTest();
    g_fakeMillis = 0;
}
void tearDown(void) {}

// 把 JSON 字面量喂给 applyUsage
static void feedUsage(const char* json) {
    JsonDocument d;
    TEST_ASSERT_FALSE_MESSAGE(deserializeJson(d, json), "测试用的 JSON 本身解析失败");
    applyUsage(d);
}

// ---------------- applyUsage:第一道节流(rev 去重)----------------

static void test_usage_first_frame_accepted(void) {
    feedUsage(R"({"rev":1,"ts":100,"hhmm":"09:30","foot":"F",
                  "rows":[{"l":"Codex 周","rem":40,"rin":"5天后","n":"prolite"}]})");
    TEST_ASSERT_TRUE(g_usage.valid);
    TEST_ASSERT_TRUE(g_usageDirty);
    TEST_ASSERT_EQUAL_INT(1, g_usage.rev);
    TEST_ASSERT_EQUAL_INT(1, g_usage.n);
    TEST_ASSERT_EQUAL_STRING("09:30", g_usage.hhmm.c_str());
    TEST_ASSERT_EQUAL_STRING("Codex 周", g_usage.rows[0].l.c_str());
    TEST_ASSERT_EQUAL_INT(40, g_usage.rows[0].rem);
}

// gateway 的保底心跳:rev 没变 → 不置 dirty,墨水屏不该被刷
static void test_usage_same_rev_is_heartbeat(void) {
    feedUsage(R"({"rev":7,"hhmm":"09:30","rows":[{"l":"A","rem":10}]})");
    g_usageDirty = false;                       // 模拟已经画过一帧
    feedUsage(R"({"rev":7,"hhmm":"10:45","rows":[{"l":"B","rem":99}]})");
    TEST_ASSERT_FALSE_MESSAGE(g_usageDirty, "rev 相同却置了 dirty —— 墨水屏会被心跳刷爆");
    TEST_ASSERT_EQUAL_STRING_MESSAGE("09:30", g_usage.hhmm.c_str(),
                                     "rev 相同不该覆盖已有内容");
}

static void test_usage_new_rev_updates(void) {
    feedUsage(R"({"rev":7,"hhmm":"09:30","rows":[{"l":"A","rem":10}]})");
    g_usageDirty = false;
    feedUsage(R"({"rev":8,"hhmm":"10:45","rows":[{"l":"B","rem":99}]})");
    TEST_ASSERT_TRUE(g_usageDirty);
    TEST_ASSERT_EQUAL_INT(8, g_usage.rev);
    TEST_ASSERT_EQUAL_STRING("10:45", g_usage.hhmm.c_str());
}

// rev 缺失 → -1。两帧都是 -1 时**不**该当成心跳吃掉(去重只对 rev>=0 生效)
static void test_usage_missing_rev_always_applies(void) {
    feedUsage(R"({"hhmm":"09:30","rows":[{"l":"A","rem":10}]})");
    TEST_ASSERT_EQUAL_INT(-1, g_usage.rev);
    g_usageDirty = false;
    feedUsage(R"({"hhmm":"11:11","rows":[{"l":"A","rem":10}]})");
    TEST_ASSERT_TRUE_MESSAGE(g_usageDirty, "rev=-1 不该走去重分支");
    TEST_ASSERT_EQUAL_STRING("11:11", g_usage.hhmm.c_str());
}

// 超过 USAGE_ROWS_MAX 的行必须被截断,否则写越界
static void test_usage_rows_capped(void) {
    String j = "{\"rev\":1,\"rows\":[";
    for (int i = 0; i < USAGE_ROWS_MAX + 3; i++) {
        if (i) j += ",";
        j += String("{\"l\":\"r") + i + "\",\"rem\":1}";
    }
    j += "]}";
    feedUsage(j.c_str());
    TEST_ASSERT_EQUAL_INT(USAGE_ROWS_MAX, g_usage.n);
}

// 空 rows → valid=false,设备会回落到事件历史列表而不是画一张空看板
static void test_usage_empty_rows_invalid(void) {
    feedUsage(R"({"rev":1,"rows":[]})");
    TEST_ASSERT_FALSE(g_usage.valid);
    TEST_ASSERT_EQUAL_INT(0, g_usage.n);
}

// ---------------- UsageRepaintGate:第二道节流 ----------------

static void test_gate_first_frame_paints_immediately(void) {
    UsageRepaintGate g;
    TEST_ASSERT_TRUE_MESSAGE(g.shouldPaint(0, true, true, true, USAGE_REPAINT_MIN_MS),
                             "第一帧必须立刻画,不能干等 USAGE_REPAINT_MIN_MS");
}

static void test_gate_throttles_within_window(void) {
    UsageRepaintGate g;
    TEST_ASSERT_TRUE(g.shouldPaint(1000, true, true, true, USAGE_REPAINT_MIN_MS));
    TEST_ASSERT_FALSE(g.shouldPaint(1000 + USAGE_REPAINT_MIN_MS - 1, true, true, true,
                                    USAGE_REPAINT_MIN_MS));
    TEST_ASSERT_TRUE(g.shouldPaint(1000 + USAGE_REPAINT_MIN_MS, true, true, true,
                                   USAGE_REPAINT_MIN_MS));
}

static void test_gate_requires_usb_idle_dirty(void) {
    UsageRepaintGate g;
    TEST_ASSERT_FALSE_MESSAGE(g.shouldPaint(0, false, true, true, 1000), "电池模式不画看板");
    TEST_ASSERT_FALSE_MESSAGE(g.shouldPaint(0, true, false, true, 1000), "通知卡期间不画");
    TEST_ASSERT_FALSE_MESSAGE(g.shouldPaint(0, true, true, false, 1000), "没脏不画");
    // 上面三次都不该记账,所以真正第一次仍然立刻画
    TEST_ASSERT_TRUE(g.shouldPaint(0, true, true, true, 1000));
}

// millis() 是 32 位,约 49.7 天回绕。差值用无符号算才不会卡死不刷
static void test_gate_survives_millis_wrap(void) {
    UsageRepaintGate g;
    uint32_t near = 0xFFFFFF00u;
    TEST_ASSERT_TRUE(g.shouldPaint(near, true, true, true, 1000));
    uint32_t after = near + 2000;          // 回绕到小数值
    TEST_ASSERT_TRUE_MESSAGE(g.shouldPaint(after, true, true, true, 1000),
                             "millis 回绕后不该永远不刷");
}

// ---------------- summarize ----------------

static void test_summarize_takes_first_line_and_trims(void) {
    TEST_ASSERT_EQUAL_STRING("第一行", summarize("  第一行  \n第二行\n第三行").c_str());
    TEST_ASSERT_EQUAL_STRING("无换行", summarize("无换行").c_str());
    TEST_ASSERT_EQUAL_STRING("", summarize("   ").c_str());
}

static void test_summarize_caps_at_160(void) {
    String long_(std::string(300, 'x'));
    TEST_ASSERT_EQUAL_UINT(160, summarize(long_).length());
}

// 是不是合法 UTF-8(只判结构,够用了)
static bool isValidUtf8(const char* s, size_t n) {
    size_t i = 0;
    while (i < n) {
        unsigned char c = (unsigned char)s[i];
        size_t need;
        if (c < 0x80)             need = 0;
        else if ((c >> 5) == 0x6) need = 1;
        else if ((c >> 4) == 0xE) need = 2;
        else if ((c >> 3) == 0x1E) need = 3;
        else return false;                          // 落单的续接字节 / 非法首字节
        if (need > 0 && i + need >= n) return false;  // 多字节序列被尾部切断
        for (size_t k = 1; k <= need; k++)
            if (((unsigned char)s[i + k] & 0xC0) != 0x80) return false;
        i += need + 1;
    }
    return true;
}

// 160 是**字节**上限,而中文一个字 3 字节 —— 160/3=53.3,所以第 54 个字正好被切两半。
// 网关侧 framing.py 明确处理了字符边界("直接切字节会切出半个汉字"),设备侧不能漏。
// 切碎的 UTF-8 进 EventItem::summary → renderIdle 画出来就是乱码/豆腐块。
static void test_summarize_truncates_on_char_boundary(void) {
    std::string cn;
    for (int i = 0; i < 100; i++) cn += "测";     // 300 字节,远超 160
    String out = summarize(String(cn));
    TEST_ASSERT_TRUE_MESSAGE(out.length() <= 160, "没截到 160 字节以内");
    TEST_ASSERT_TRUE_MESSAGE(isValidUtf8(out.c_str(), out.length()),
                             "截断切碎了 UTF-8 —— 墨水屏会渲染出半个汉字");
}

// 中英混排时边界落在哪不固定,扫一遍长度确保每种都不切碎
static void test_summarize_boundary_sweep(void) {
    for (int pad = 0; pad < 6; pad++) {
        std::string s(pad, 'a');
        for (int i = 0; i < 100; i++) s += "字";
        String out = summarize(String(s));
        char msg[64];
        snprintf(msg, sizeof msg, "pad=%d 时切碎了 UTF-8", pad);
        TEST_ASSERT_TRUE_MESSAGE(isValidUtf8(out.c_str(), out.length()), msg);
    }
}

// ---------------- addHistory ----------------

static void test_history_newest_first_and_capped(void) {
    for (int i = 0; i < HISTORY_MAX + 4; i++) {
        addHistory("done", "codex", String("proj") + i, "msg", 1000 + i);
    }
    TEST_ASSERT_EQUAL_INT(HISTORY_MAX, g_histN);
    // 最新的在 [0]
    String newest = String("proj") + (HISTORY_MAX + 3);
    TEST_ASSERT_EQUAL_STRING(newest.c_str(), g_hist[0].project.c_str());
    TEST_ASSERT_TRUE(g_idleDirty);
}

// ---------------- 命令分发 ----------------

static void test_mqtt_cmd_dispatch(void) {
    char topic[] = TOPIC_CMD;
    byte ota[] = "ota";
    onMessage(topic, ota, 3);
    TEST_ASSERT_TRUE(g_doOta);
    TEST_ASSERT_FALSE(g_doDump);

    resetLogicStateForTest();
    byte dump[] = "dump";
    onMessage(topic, dump, 4);
    TEST_ASSERT_TRUE_MESSAGE(g_doDump, "cmd=dump 没有置位 —— 屏幕回传会失效");
    TEST_ASSERT_FALSE(g_doOta);
}

// payload 不是 C 字符串(PubSubClient 给的是带长度的裸缓冲),不能越界读
static void test_mqtt_cmd_respects_length(void) {
    char topic[] = TOPIC_CMD;
    byte buf[] = "dumpXXXX";
    onMessage(topic, buf, 2);            // 只取 "du",不该匹配 dump
    TEST_ASSERT_FALSE_MESSAGE(g_doDump, "越过 len 读到了后面的字节");
}

static void test_mqtt_usage_topic_routes_to_applyUsage(void) {
    char topic[] = TOPIC_USAGE;
    char json[] = R"({"rev":3,"hhmm":"08:00","rows":[{"l":"A","rem":5}]})";
    onMessage(topic, (byte*)json, strlen(json));
    TEST_ASSERT_TRUE(g_usage.valid);
    TEST_ASSERT_EQUAL_INT(3, g_usage.rev);
}

static void test_mqtt_event_topic_sets_event(void) {
    char topic[] = TOPIC_EVENT;
    char json[] = R"({"kind":"needs_input","src":"codex","project":"p","msg":"m","ts":42})";
    onMessage(topic, (byte*)json, strlen(json));
    TEST_ASSERT_TRUE(g_haveEvent);
    TEST_ASSERT_EQUAL_STRING("needs_input", g_evKind.c_str());
    TEST_ASSERT_EQUAL_INT(42, (int)g_evTs);
    TEST_ASSERT_TRUE_MESSAGE(g_evLive, "MQTT 来的事件一律 live=true");
}

// 坏 JSON 不能改状态、更不能崩
static void test_mqtt_bad_json_is_ignored(void) {
    char topic[] = TOPIC_USAGE;
    char bad[] = "{not json";
    onMessage(topic, (byte*)bad, strlen(bad));
    TEST_ASSERT_FALSE(g_usage.valid);
    TEST_ASSERT_FALSE(g_usageDirty);
}

// BLE 和 MQTT 两条路径的行为必须一致 —— 这是 logic.cpp 注释里明确写的约束
static void test_ble_and_mqtt_usage_agree(void) {
    const char* json = R"({"rev":9,"hhmm":"07:07","rows":[{"l":"X","rem":66,"n":"note"}]})";

    char topic[] = TOPIC_USAGE;
    onMessage(topic, (byte*)json, strlen(json));
    Usage viaMqtt = g_usage;

    resetLogicStateForTest();
    handleBleMessage(String("{\"t\":\"usage\",\"rev\":9,\"hhmm\":\"07:07\","
                            "\"rows\":[{\"l\":\"X\",\"rem\":66,\"n\":\"note\"}]}"));

    TEST_ASSERT_EQUAL_INT(viaMqtt.rev, g_usage.rev);
    TEST_ASSERT_EQUAL_INT(viaMqtt.n, g_usage.n);
    TEST_ASSERT_EQUAL_STRING(viaMqtt.hhmm.c_str(), g_usage.hhmm.c_str());
    TEST_ASSERT_EQUAL_INT(viaMqtt.rows[0].rem, g_usage.rows[0].rem);
    TEST_ASSERT_EQUAL_STRING(viaMqtt.rows[0].n.c_str(), g_usage.rows[0].n.c_str());
}

static void test_ble_cmd_dispatch(void) {
    handleBleMessage(String(R"({"t":"cmd","cmd":"dump"})"));
    TEST_ASSERT_TRUE(g_doDump);
    TEST_ASSERT_FALSE(g_doOta);
}

// BLE 事件默认 live=true,补发历史时 gateway 会显式给 live:false
static void test_ble_event_live_flag(void) {
    handleBleMessage(String(R"({"t":"ev","kind":"done","project":"p","live":false})"));
    TEST_ASSERT_TRUE(g_haveEvent);
    TEST_ASSERT_FALSE_MESSAGE(g_evLive, "补发的历史事件被当成 live,会弹通知卡");
}

int main(int, char**) {
    UNITY_BEGIN();
    RUN_TEST(test_usage_first_frame_accepted);
    RUN_TEST(test_usage_same_rev_is_heartbeat);
    RUN_TEST(test_usage_new_rev_updates);
    RUN_TEST(test_usage_missing_rev_always_applies);
    RUN_TEST(test_usage_rows_capped);
    RUN_TEST(test_usage_empty_rows_invalid);

    RUN_TEST(test_gate_first_frame_paints_immediately);
    RUN_TEST(test_gate_throttles_within_window);
    RUN_TEST(test_gate_requires_usb_idle_dirty);
    RUN_TEST(test_gate_survives_millis_wrap);

    RUN_TEST(test_summarize_takes_first_line_and_trims);
    RUN_TEST(test_summarize_caps_at_160);
    RUN_TEST(test_summarize_truncates_on_char_boundary);
    RUN_TEST(test_summarize_boundary_sweep);
    RUN_TEST(test_history_newest_first_and_capped);

    RUN_TEST(test_mqtt_cmd_dispatch);
    RUN_TEST(test_mqtt_cmd_respects_length);
    RUN_TEST(test_mqtt_usage_topic_routes_to_applyUsage);
    RUN_TEST(test_mqtt_event_topic_sets_event);
    RUN_TEST(test_mqtt_bad_json_is_ignored);
    RUN_TEST(test_ble_and_mqtt_usage_agree);
    RUN_TEST(test_ble_cmd_dispatch);
    RUN_TEST(test_ble_event_live_flag);
    return UNITY_END();
}
