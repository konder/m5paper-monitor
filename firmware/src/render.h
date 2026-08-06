#pragma once
#include "types.h"

// 初始化屏幕(旋转/配色),开机调用一次。clearScreen=false 时保留当前画面(深睡唤醒用)
void renderInit(bool clearScreen = true);

// 列表页。full=true 全刷去残影,否则局刷。内部记录卡片位置供触摸命中。
void renderList(const Snapshot& snap, int batteryPct, bool usb, bool full);

// 触摸命中:返回被点中的会话下标(对应 snap.sessions),无则 -1
int cardIndexAtTouch(int x, int y);

// 触摸是否落在底栏版本号区域(用于"点版本主动查更新")
bool versionAtTouch(int x, int y);

// 详情页:文字结果 + 可选一张已拉取的 JPEG(jpg 可为 null)。
void renderDetail(const Session& s, long nowTs, const unsigned char* jpg, unsigned int jpgLen,
                  int imgIdx, int imgCount);

// 事件通知全屏卡。kind: done | needs_input | quota
void renderNotify(const String& kind, const String& src, const String& project,
                  const String& msg, const String& meta, long ts);

// 待命屏:最近事件历史列表(最新在上)。items 按新→旧传入,n 条;full=true 全刷去残影。
void renderIdle(const EventItem* items, int n, int batteryPct, const char* link,
                int fwVersion, bool full);

// v39 待命屏:AI 消耗看板(每行一条油量表 + 底部 LiteLLM 单行)。
// full=true 才全刷;**默认应传 false 走 epd_fast 局刷** —— 全刷 1-2s 会阻塞主循环,
// BLE 模式下有击穿中心监管超时的风险(见 esp-ble-link docs/pitfalls.md A3)。
void renderUsage(const Usage& u, int batteryPct, const char* link, int fwVersion, bool full);

// 休眠页:大字电量 + 电池条 + 下次唤醒时间(深睡前渲染,墨水屏保留)
void renderSleep(int batteryPct, const String& wakeAt);

// 电池模式待机页:大字电量 + 电池条 + 充电状态/电压(v29;渲染后自动断 EPD 电源省电)
void renderBatteryPage(int batteryPct, int mv, bool charging, const char* link, int fwVersion);

// 顶部状态提示(连接中/离线等)
void renderStatus(const char* msg);

#if defined(NATIVE_TEST)
// 仅 native 渲染测试:把内部的 trunc() 透出来直接测。它依赖 M5.Display.textWidth,
// 所以只能在装好 Panel_sdl 的环境里跑(见 test/test_render)。设备固件不含这段。
String renderTruncForTest(const String& s, int maxw);
#endif
