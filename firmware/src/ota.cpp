#include "ota.h"
#include "config.h"
#include "secrets.h"
#include "render.h"

#include <M5Unified.h>
#include <WiFi.h>
#include <Preferences.h>
#include <HTTPClient.h>
#include <HTTPUpdate.h>

static Preferences prefs;

// 跨深睡缓存上次 AP 的 BSSID+信道,唤醒定向快连(跳过扫描)
RTC_DATA_ATTR static uint8_t s_bssid[6];
RTC_DATA_ATTR static int s_channel = 0;
RTC_DATA_ATTR static bool s_wifiCache = false;

void wifiSaveNVS(const char* ssid, const char* pass) {
    prefs.begin("wifi", false);
    prefs.putString("ssid", ssid);
    prefs.putString("pass", pass);
    prefs.end();
}

bool wifiConnectNVS() {
    prefs.begin("wifi", true);
    String ssid = prefs.getString("ssid", "");
    String pass = prefs.getString("pass", "");
    prefs.end();
    // NVS 空 → 用编译进来的默认值播种(仅第一次)
    if (ssid.isEmpty()) {
        ssid = WIFI_SSID;
        pass = WIFI_PASSWORD;
        wifiSaveNVS(ssid.c_str(), pass.c_str());
    }
    WiFi.mode(WIFI_STA);
    uint32_t t0 = millis();
    // 有缓存则定向快连(指定信道+BSSID,跳过全信道扫描)
    if (s_wifiCache) {
        WiFi.begin(ssid.c_str(), pass.c_str(), s_channel, s_bssid);
        while (WiFi.status() != WL_CONNECTED && millis() - t0 < 4000) delay(100);
    }
    // 快连失败 → 常规连接(扫描)
    if (WiFi.status() != WL_CONNECTED) {
        WiFi.disconnect();
        WiFi.begin(ssid.c_str(), pass.c_str());
        while (WiFi.status() != WL_CONNECTED && millis() - t0 < WIFI_TIMEOUT_MS) delay(150);
    }
    if (WiFi.status() == WL_CONNECTED) {
        memcpy(s_bssid, WiFi.BSSID(), 6);
        s_channel = WiFi.channel();
        s_wifiCache = true;
        return true;
    }
    return false;
}

void checkOTA() {
    // OTA 固件服务器(thumbserver)在 collector 所在机(Mac Mini),与 MQTT broker(飞牛)可能不同机,
    // 故用独立 OTA_HOST;secrets.h 未定义时回退 MQTT_HOST。
    #ifndef OTA_HOST
    #define OTA_HOST MQTT_HOST
    #endif
    String base = "http://" + String(OTA_HOST) + ":" + String(THUMB_PORT);
    // 1) 取远端版本
    HTTPClient http;
    if (!http.begin(base + "/fw/version")) { Serial.println("[ota] begin 失败"); return; }
    int code = http.GET();
    if (code != 200) { Serial.printf("[ota] /fw/version HTTP %d\n", code); http.end(); return; }
    int remote = http.getString().toInt();
    http.end();
    Serial.printf("[ota] local v%d, remote v%d @ %s\n", FW_VERSION, remote, base.c_str());
    if (remote <= FW_VERSION) { Serial.println("[ota] 已最新,跳过"); return; }

    // 2) 拉 .bin 自更新
    Serial.printf("[ota] 开始更新到 v%d ...\n", remote);
    renderStatus(("发现新固件 v" + String(remote) + ",更新中…").c_str());
    WiFiClient client;
    httpUpdate.rebootOnUpdate(true);
    t_httpUpdate_return ret = httpUpdate.update(client, base + "/fw/current.bin");
    if (ret == HTTP_UPDATE_FAILED) {
        Serial.printf("[ota] 失败 %d: %s\n", httpUpdate.getLastError(), httpUpdate.getLastErrorString().c_str());
        renderStatus(("更新失败:" + String(httpUpdate.getLastError())).c_str());
        delay(1500);
    }
    // 成功会自动重启;NO_UPDATES/其它情况直接返回继续正常启动
}

// 调试:把当前屏幕内容原样回传给网关(落成 PNG,供 AI agent 直接查看渲染结果)。
//
// 读的是 lgfx::Panel_EPD 在 PSRAM 里的整屏像素缓冲(它继承 Panel_HasBuffer 并 override 了
// readRect),**不碰面板硬件** —— 所以 EPD 已经 sleep() 断电时照样能 dump,而且不会触发重绘。
// 尺寸走 d.width()/d.height() 运行时取:setRotation(1) 叠面板自带的 offset_rotation=3
// 之后逻辑尺寸才是 960x540,写死容易错。
// readRect 的 void* 重载是 bgr888;灰度源三通道相等,所以网关随便取一个通道就是精确的
// 8bit 灰度,设备侧一个位运算都不用做。(uint8_t* 重载是 rgb332,只剩 8 级灰,别用。)
void postScreenDump() {
    if (WiFi.status() != WL_CONNECTED) { Serial.println("[dump] 未连 WiFi,跳过"); return; }

    auto& d = M5.Display;
    const int w = d.width(), h = d.height();
    const size_t len = (size_t)w * h * 3;
    uint8_t* buf = (uint8_t*)ps_malloc(len);      // 1.5MB 只能放 PSRAM
    if (!buf) { Serial.printf("[dump] PSRAM 分配 %u 字节失败\n", (unsigned)len); return; }

    uint32_t t0 = millis();
    d.readRect(0, 0, w, h, (void*)buf);
    uint32_t readMs = millis() - t0;

    String url = "http://" + String(OTA_HOST) + ":" + String(THUMB_PORT)
               + "/screen?w=" + w + "&h=" + h + "&fmt=bgr888&fw=" + FW_VERSION;
    HTTPClient http;
    if (!http.begin(url)) { Serial.println("[dump] http.begin 失败"); free(buf); return; }
    http.addHeader("Content-Type", "application/octet-stream");
    int code = http.POST(buf, len);
    String body = code > 0 ? http.getString() : String();
    http.end();
    free(buf);

    Serial.printf("[dump] %dx%d %u字节 读取%ums → HTTP %d %s\n",
                  w, h, (unsigned)len, (unsigned)readMs, code, body.c_str());
}
