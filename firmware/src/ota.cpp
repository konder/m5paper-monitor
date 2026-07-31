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
