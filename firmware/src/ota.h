#pragma once

// 从 NVS 读 WiFi 并连接;NVS 为空时用 secrets.h 默认值播种进 NVS(以后 OTA 不再需要重烧 WiFi)。
bool wifiConnectNVS();

// 覆盖写入 NVS 里的 WiFi(供以后无线改 WiFi 用)
void wifiSaveNVS(const char* ssid, const char* pass);

// 开机版本检查:远端 /fw/version > FW_VERSION 则拉 .bin 自更新并重启。
// 返回 true 表示正在更新(通常不会返回,因为会重启)。
void checkOTA();

// 调试:把当前屏幕像素 POST 给网关的 /screen(落成 PNG,供 AI agent 查看渲染结果)。
// 需 WiFi 已连;只读 PSRAM 里的帧缓冲,不动面板、不触发重绘。由 cmd=dump 触发。
void postScreenDump();
