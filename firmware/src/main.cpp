// M5PaperS3 —— 双模事件通知端(v30):BLE 优先(低功耗即时)+ WiFi 兜底,射频互斥
// 常态:BLE 连 Mac Mini 中枢(~1-5mA,即时);BLE 连不上→WiFi+MQTT 兜底(~20mA);
// WiFi 兜底时定时回试 BLE,连上则切回。OTA 走 WiFi(BLE 模式收到 cmd=ota 临时切 WiFi)。
// 保留:EPD 关电省电 + 电池模式大电量待机页。
#include <M5Unified.h>
#include <WiFi.h>
#include <PubSubClient.h>
#include <ArduinoJson.h>
#include <esp_pm.h>
#include <esp_wifi.h>

#include "secrets.h"
#include "config.h"
#include "types.h"
#include "render.h"
#include "power.h"
#include "ota.h"
#include "buzzer.h"
#include <EspBleLink.h>

RTC_DATA_ATTR uint32_t g_bootCount = 0;
static bool g_usb = true;

enum Mode { MODE_BLE, MODE_WIFI };
static Mode g_mode = MODE_BLE;

static WiFiClient wifiClient;
static PubSubClient mqtt(wifiClient);

enum View { V_IDLE, V_NOTIFY };
static View g_view = V_IDLE;
static uint32_t g_notifyUntil = 0;

static EventItem g_hist[HISTORY_MAX];
static int g_histN = 0;
static bool g_idleDirty = true;
static uint32_t g_idleRenders = 0;

// v39 消耗看板。g_usageRenders 是**独立**的全刷计数器 —— 和 g_idleRenders 共用会让
// 两个页面的局刷/全刷节奏交错,残影攒不掉。
static Usage g_usage;
static bool g_usageDirty = false;
static uint32_t g_usageRenders = 0;

static volatile bool g_haveEvent = false;
static bool g_evLive = true;
static String g_evKind, g_evSrc, g_evProject, g_evMsg, g_evMeta;
static long g_evTs = 0;
static volatile bool g_doOta = false;
static volatile bool g_doDump = false;   // 调试:回传当前屏幕给网关(见 ota.cpp postScreenDump)

static uint32_t g_bleDropAt = 0;   // BLE 掉线起始时刻(0=在线)

// ---- 前向声明 ----
static void showIdle();
static void showNotify(const String&, const String&, const String&, const String&, const String&, long);
static void configurePowerSave();
static bool connectMqtt();
static void enterBleMode();
static void enterWifiMode();
static bool tryBle(uint32_t waitMs);
static void sendBattery();

static String summarize(const String& msg) {
    String s = msg; int nl = s.indexOf('\n');
    if (nl >= 0) s = s.substring(0, nl);
    s.trim();
    if (s.length() > 160) s = s.substring(0, 160);
    return s;
}

static void addHistory(const String& kind, const String& src, const String& project,
                       const String& msg, long ts) {
    for (int i = HISTORY_MAX - 1; i > 0; i--) g_hist[i] = g_hist[i - 1];
    g_hist[0].kind = kind; g_hist[0].src = src; g_hist[0].project = project;
    g_hist[0].summary = summarize(msg); g_hist[0].ts = ts;
    if (g_histN < HISTORY_MAX) g_histN++;
    g_idleDirty = true;
}

static void showIdle() {
    g_view = V_IDLE;
    // v38: 状态栏明确显示当前链路 BLE/WiFi(连接中带…)
    const char* link = (g_mode == MODE_BLE) ? (espble::connected() ? "BLE" : "BLE…")
                                            : (mqtt.connected() ? "WiFi" : "WiFi…");
    if (!g_usb) {
        // 电池模式:专门的大电量待机页(EPD 画完自动断电省电)。
        // 看板是"插电抬头看"的场景,电池模式仍以电量页为主。
        renderBatteryPage(batteryPercent(), M5.Power.getBatteryVoltage(),
                          M5.Power.isCharging(), link, FW_VERSION);
    } else if (g_usage.valid) {
        // v39: 有看板数据就以看板为待机屏(事件仍会弹卡,NOTIFY_MS 后回到这里)
        renderUsage(g_usage, batteryPercent(), link, FW_VERSION,
                    (g_usageRenders++ % FULL_REFRESH_EVERY) == 0);
        g_usageDirty = false;
    } else {
        // 还没收到 usage(retained 未到 / gateway 关了看板)→ 回落事件历史列表
        renderIdle(g_hist, g_histN, batteryPercent(), link,
                   FW_VERSION, (g_idleRenders++ % FULL_REFRESH_EVERY) == 0);
    }
    g_idleDirty = false;
}

static void showNotify(const String& kind, const String& src, const String& project,
                       const String& msg, const String& meta, long ts) {
    g_view = V_NOTIFY;
    g_notifyUntil = millis() + NOTIFY_MS;
    buzzPattern(kind.c_str());
    renderNotify(kind, src, project, msg, meta, ts);
}

// ---- 事件解析(BLE / MQTT 统一置位)----
static void setEvent(const char* kind, const char* src, const char* project,
                     const char* msg, const char* meta, long ts, bool live) {
    g_evKind = kind; g_evSrc = src; g_evProject = project;
    g_evMsg = msg; g_evMeta = meta; g_evTs = ts; g_evLive = live;
    g_haveEvent = true;
}

// ---- v39 消耗看板解析(BLE / MQTT 共用一份;两条路径必须行为一致)----
// 只置 dirty 位,不在这里渲染 —— 这两个函数分别跑在 NimBLE 主机任务和 mqtt.loop() 里,
// 塞一次 1-2s 的墨水屏全刷进去会直接饿死协议栈。
static void applyUsage(JsonDocument& doc) {
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

// BLE 桥消息:{"t":"ev"|"cmd"|"usage", ...}
static void handleBleMessage(const String& s) {
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

// 电池模式开自动轻睡眠;WiFi 模式再叠加 WiFi modem sleep。插电全速。
static void configurePowerSave() {
    if (g_mode == MODE_WIFI) WiFi.setSleep(g_usb ? WIFI_PS_NONE : WIFI_PS_MIN_MODEM);
#if ESP_IDF_VERSION_MAJOR >= 5
    esp_pm_config_t pm = {};
#else
    esp_pm_config_esp32s3_t pm = {};
#endif
    pm.max_freq_mhz = PM_MAX_FREQ_MHZ;
    pm.min_freq_mhz = g_usb ? PM_MAX_FREQ_MHZ : PM_MIN_FREQ_MHZ;
    pm.light_sleep_enable = g_usb ? false : true;
    esp_err_t e = esp_pm_configure(&pm);
    Serial.printf("[pm] ls=%d min=%d -> %s\n", pm.light_sleep_enable, pm.min_freq_mhz, esp_err_to_name(e));
}

static void sendBattery() {
    char buf[160];
    snprintf(buf, sizeof(buf),
        "{\"pct\":%d,\"mv\":%d,\"up\":%lu,\"usb\":%d,\"v\":%d,\"g5\":%d,\"chg\":%d,\"ls\":%d,\"link\":\"%s\"}",
        batteryPercent(), M5.Power.getBatteryVoltage(), (unsigned long)(millis() / 1000), g_usb ? 1 : 0,
        FW_VERSION, analogReadMilliVolts(PIN_USB_DET), (int)M5.Power.isCharging(), g_usb ? 0 : 1,
        g_mode == MODE_BLE ? "ble" : "wifi");
    if (g_mode == MODE_WIFI) { if (mqtt.connected()) mqtt.publish(TOPIC_DEVICE, buf, true); }
    else espble::notify(String(buf));
    Serial.printf("[bat] %s\n", buf);
}

static void checkLowBatt() {
    static bool alerted = false;
    int p = batteryPercent();
    if (!g_usb && p >= 0 && p < LOW_BATT_PCT) {
        if (!alerted) {
            alerted = true;
            showNotify("quota", "", "电量不足", String("电量 ") + p + "% ,请尽快充电。", "", millis() / 1000);
        }
    } else if (g_usb || p >= LOW_BATT_PCT + 5) {
        alerted = false;
    }
}

// ---- MQTT(WiFi 模式)----
static void onMessage(char* topic, byte* payload, unsigned int len) {
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

static bool connectMqtt() {
    mqtt.setServer(MQTT_HOST, MQTT_PORT);
    mqtt.setBufferSize(MQTT_BUFFER_SIZE);
    mqtt.setKeepAlive(MQTT_KEEPALIVE_SEC);
    mqtt.setCallback(onMessage);
    String cid = "m5papers3-" + String((uint32_t)ESP.getEfuseMac(), HEX);
    const char* user = strlen(MQTT_USER) ? MQTT_USER : nullptr;
    const char* pass = strlen(MQTT_PASS) ? MQTT_PASS : nullptr;
    // 持久会话:短暂掉线期间 broker 排队 QoS1 事件,重连补发
    bool ok = mqtt.connect(cid.c_str(), user, pass, nullptr, 0, false, nullptr, false);
    if (ok) {
        mqtt.subscribe(TOPIC_EVENT, 1);
        mqtt.subscribe(TOPIC_CMD, 1);
        // v39 看板:QoS0 就够(retained,重连即得最新一帧,不需要补历史)
        mqtt.subscribe(TOPIC_USAGE, 0);
        Serial.println("[mqtt] connected + subscribed (events/cmd/usage)");
    }
    return ok;
}

// ---- 模式切换(射频互斥)----

// v40 起链路层来自 esp-ble-link。本工程只提供**自己特有**的那一项:广播名。
// GATT UUID(NUS)、环形缓冲与帧上限、notify 分片、连接参数纪律全部用框架默认值 ——
// 复述一遍除了制造不一致没有别的作用(而且 config.h 里那个 #define NUS_RX
// 曾经和框架的同名声明撞车,宏不认 namespace)。
static espble::LinkConfig bleConfig() {
    espble::LinkConfig cfg;
    cfg.deviceType = BLE_TYPE;      // 广播名 = m5paper-<efuse MAC 后3字节>
    cfg.fwVersion  = FW_VERSION;
    cfg.caps       = BLE_CAPS;
    return cfg;
}

static bool tryBle(uint32_t waitMs) {
    espble::begin(bleConfig());          // 启动 BLE 广播
    uint32_t t0 = millis();
    while (!espble::connected() && millis() - t0 < waitMs) delay(100);
    return espble::connected();
}
static void enterBleMode() {
    g_mode = MODE_BLE;
    WiFi.disconnect(true); WiFi.mode(WIFI_OFF);   // 让出 2.4G
    g_bleDropAt = 0;
    configurePowerSave();
    Serial.println("[mode] -> BLE");
}
static void enterWifiMode() {
    g_mode = MODE_WIFI;
    // 释放 BT,把 2.4G 让给 WiFi。
    // ⚠️ espble::end() 会真的 deinit 协议栈。本工程跑在 Arduino core 2.0.17(IDF 4.4)
    //    上没问题;**如果哪天把 platform 升到带 core 3.x 的版本,这里会 panic**
    //    (NimBLE 1.4.x 的 HCI deinit 在 IDF 5.x 上是坏的,见 esp-ble-link
    //     docs/pitfalls.md A9),那时要改成 espble::quiesce()。
    espble::end();
    WiFi.mode(WIFI_STA);
    wifiConnectNVS();
    connectMqtt();
    configurePowerSave();
    Serial.printf("[mode] -> WiFi ip=%s\n", WiFi.localIP().toString().c_str());
}

void setup() {
    auto cfg = M5.config();
    cfg.clear_display = false;
    M5.begin(cfg);
    Serial.begin(115200);
    Serial.setTxTimeoutMs(0);
    g_bootCount++;
    analogReadResolution(12);
    buzzerInit();
    renderInit(true);

    g_usb = isUsbPowered();
    Serial.printf("[pwr] g5mv=%d chg=%d batV=%d lvl=%d usb=%d\n",
                  analogReadMilliVolts(PIN_USB_DET), (int)M5.Power.isCharging(),
                  M5.Power.getBatteryVoltage(), M5.Power.getBatteryLevel(), g_usb);
    buzzPattern("boot");

    // BLE 优先:开机先广播等中枢连;超时→WiFi 兜底(顺带查 OTA)
    renderStatus("蓝牙连接中…");
    if (tryBle(BLE_BOOT_WAIT_MS)) {
        enterBleMode();
        Serial.printf("[boot] v%d BLE usb=%d boot#%u\n", FW_VERSION, g_usb, g_bootCount);
    } else {
        renderStatus("蓝牙超时,连 WiFi…");
        enterWifiMode();
        checkOTA();
        Serial.printf("[boot] v%d WiFi usb=%d boot#%u\n", FW_VERSION, g_usb, g_bootCount);
    }
    showIdle();
}

void loop() {
    uint32_t now = millis();

    if (g_mode == MODE_BLE) {
        String bmsg;
        while (espble::popMessage(bmsg)) handleBleMessage(bmsg);
        if (!espble::connected()) {
            if (g_bleDropAt == 0) g_bleDropAt = now;
            else if (now - g_bleDropAt > BLE_DROP_TIMEOUT_MS) {
                Serial.println("[mode] BLE 掉线超时 → WiFi 兜底");
                enterWifiMode();
                showIdle();
            }
        } else {
            g_bleDropAt = 0;   // 连接参数由中心驱动,外设这里无事可做(见 EspBleLink.h)
        }
    } else {   // MODE_WIFI
        if (WiFi.status() != WL_CONNECTED) { renderStatus("重连 WiFi…"); wifiConnectNVS(); }
        if (!mqtt.connected()) {
            static uint32_t lastTry = 0;
            if (now - lastTry > 3000) { lastTry = now; connectMqtt(); }
        }
        mqtt.loop();
        // 定时回试 BLE(连上则切回低功耗)
        static uint32_t lastBleRetry = 0;
        if (lastBleRetry == 0) lastBleRetry = now;
        if (now - lastBleRetry > BLE_RETRY_INTERVAL_MS) {
            lastBleRetry = now;
            Serial.println("[mode] 回试 BLE…");
            mqtt.disconnect(); WiFi.disconnect(true); WiFi.mode(WIFI_OFF);
            if (tryBle(BLE_RETRY_WAIT_MS)) { enterBleMode(); showIdle(); }
            else { Serial.println("[mode] BLE 仍不可达,回 WiFi"); enterWifiMode(); }
        }
    }

    // OTA(需 WiFi)
    if (g_doOta) {
        g_doOta = false;
        if (g_mode == MODE_BLE) {
            Serial.println("[ota] BLE 模式收到 ota,临时切 WiFi");
            espble::end(); WiFi.mode(WIFI_STA); wifiConnectNVS();
            checkOTA();                          // 成功会自动重启
            WiFi.disconnect(true); WiFi.mode(WIFI_OFF);
            if (tryBle(BLE_RETRY_WAIT_MS)) enterBleMode(); else enterWifiMode();
            showIdle();
        } else {
            checkOTA();
        }
    }

    // 屏幕 dump(调试用,需 WiFi)。BLE 模式下射频互斥、WiFi 是关的,**不为它切模式** ——
    // 切一次要断 BLE + 重连,代价远大于一次截图;调试时让设备走 WiFi 兜底即可。
    if (g_doDump) {
        g_doDump = false;
        if (g_mode == MODE_BLE) Serial.println("[dump] BLE 模式下 WiFi 关闭,跳过(调试请走 WiFi 兜底)");
        else postScreenDump();
    }

    // 事件 → 通知卡(live)/ 历史(补发)
    if (g_haveEvent) {
        g_haveEvent = false;
        addHistory(g_evKind, g_evSrc, g_evProject, g_evMsg, g_evTs);
        if (g_evLive) showNotify(g_evKind, g_evSrc, g_evProject, g_evMsg, g_evMeta, g_evTs);
        else g_idleDirty = true;
    }

    now = millis();

    // v39 看板重绘节流:gateway 只在实质变化时 bump rev(applyUsage 已按 rev 去重),
    // 这里再压一道最短间隔。usagePainted 保证**第一帧立刻画**,不用等 2 分钟。
    static uint32_t lastUsagePaint = 0;
    static bool usagePainted = false;
    if (g_usb && g_view == V_IDLE && g_usageDirty &&
        (!usagePainted || (uint32_t)(now - lastUsagePaint) >= USAGE_REPAINT_MIN_MS)) {
        lastUsagePaint = now;
        usagePainted = true;
        g_idleDirty = true;      // 交给下面统一的 showIdle 路径,避免两处渲染
    }

    if (g_view == V_NOTIFY && (int32_t)(now - g_notifyUntil) >= 0) showIdle();
    else if (g_view == V_IDLE && g_idleDirty) showIdle();

    // 插电定期重刷(仅 USB):让看板顶栏时钟和 rin 倒计时不至于太旧
    static uint32_t lastBoard = 0;
    if (g_usb && g_view == V_IDLE && now - lastBoard > 120000) { lastBoard = now; showIdle(); }

    // 周期:电量遥测 + 低电告警 + 电池页按电量变化重绘
    static uint32_t lastBat = 0;
    if (now - lastBat > BAT_REPORT_MS || lastBat == 0) {
        lastBat = now; sendBattery(); checkLowBatt();
        // v37: 电量滞回——变化≥3% 才重绘,消掉 pct 噪声(65/66/67 抖)导致的无谓刷屏
        static int lastShownPct = -999;
        if (!g_usb && g_view == V_IDLE) {
            int p = batteryPercent();
            if (lastShownPct < 0 || abs(p - lastShownPct) >= 3) { lastShownPct = p; g_idleDirty = true; }
        }
    }

    delay(g_usb ? 20 : LOOP_IDLE_MS);
}
