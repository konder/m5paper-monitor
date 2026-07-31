#pragma once

// 从 NVS 读 WiFi 并连接;NVS 为空时用 secrets.h 默认值播种进 NVS(以后 OTA 不再需要重烧 WiFi)。
bool wifiConnectNVS();

// 覆盖写入 NVS 里的 WiFi(供以后无线改 WiFi 用)
void wifiSaveNVS(const char* ssid, const char* pass);

// 开机版本检查:远端 /fw/version > FW_VERSION 则拉 .bin 自更新并重启。
// 返回 true 表示正在更新(通常不会返回,因为会重启)。
void checkOTA();
