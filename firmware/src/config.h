#pragma once
// 全局常量:WiFi/MQTT、PM 轻睡眠、引脚、OTA、刷新策略
// v26 起:WiFi + MQTT 常连 + PM 自动轻睡眠(WiFi modem sleep),低功耗且通知即时;全屋覆盖不受 BLE 距离限制。

// 固件版本(每次要 OTA 推新时 +1;gateway 的 /fw/version 返回值 > 此值即触发更新)
#define FW_VERSION 50

// 硬件看门狗超时(秒)。主循环连续这么久没喂就复位。
// ⚠️ 别定小。墨水屏全刷 1~2 秒,一轮里可能连着刷屏 + 发帧;定太短会把正常操作
//    判成挂死,变成无限复位环 —— 那比不装看门狗更糟。
//    30 秒的取舍:任何正常单轮都远够,而「主循环挂死」从 18 小时失联变成 30 秒空档。
//    OTA / 屏幕 dump 那种几十秒的操作走 withWifi(),它会临时关掉看门狗。
#define WDT_TIMEOUT_S 30

// ---- MQTT 主题 ----
#define TOPIC_EVENT  "m5paper/events"  // 事件:done/needs_input/quota(QoS1 离线排队)
#define TOPIC_CMD    "m5paper/cmd"     // 指令:ota(查更新) / dump(回传屏幕,见 tools/dump_screen.py)
#define TOPIC_DEVICE "m5paper/device"  // 设备上报电量遥测(供实测续航)
#define TOPIC_USAGE  "m5paper/usage"   // v39 消耗看板(retained QoS0,四路额度/流量)
#define HISTORY_MAX  8                 // 待命屏历史事件列表最多显示/保留条数

// ---- v39 消耗看板 ----
// 收到 usage 后最短重绘间隔。gateway 侧已按「实质变化」节流(rev 不变就不重绘),
// 这里是第二道保险,防止上游异常抖动把墨水屏刷爆。
#define USAGE_REPAINT_MIN_MS 120000

// ---- 双模(v30):BLE 优先(低功耗即时)+ WiFi 兜底;射频互斥 ----
// v41:身份改由框架派生 —— 广播名自动是 `<BLE_TYPE>-<efuse MAC 后3字节>`,
// 例如 m5paper-c119cc。同一份固件烧多块板子自动不重名,BleHub 按 `m5paper-` 前缀发现。
// (v40 之前是写死的 "M5PaperNotify",那意味着每台设备一份固件。)
#define BLE_TYPE     "m5paper"
#define BLE_CAPS     "usage,ev,cmd"    // 随 hello 帧上报,中枢据此决定广播发不发给它
// GATT UUID、帧上限、连接参数纪律全部归 esp-ble-link,本工程不再复述。
// 需要非默认值时在 main.cpp 的 bleConfig() 里覆盖 LinkConfig 对应字段。
// v46:双模切换时序(BLE_BOOT_WAIT_MS / BLE_DROP_TIMEOUT_MS / BLE_RETRY_INTERVAL_MS /
// BLE_RETRY_WAIT_MS)全部删除 —— 状态机没了,没有"等多久再切"这件事了。
// 留个记录:那套参数(每 300s 广播 15s)正是主机连不上的病因,占空比只有 5%,
// 和中枢的重连退避互相错过。别再把它们加回来。

// ---- 省电:电池模式自动轻睡眠(BLE 常驻,CPU 在空闲/广播间隙睡)----
#define PM_MIN_FREQ_MHZ    40          // 轻睡眠时降频到此
#define PM_MAX_FREQ_MHZ    240
#define LOOP_IDLE_MS       250         // 主循环空闲步进(轻睡眠期);越大越省电,通知延迟越大(≤此值)

// ---- 电量遥测 + 低电告警 ----
#define BAT_REPORT_MS  20000           // 诊断期:20s 上报一次(稳定后改回 300000=5min)
#define LOW_BATT_PCT   15              // 低于此且非 USB → 弹低电告警 + 蜂鸣(每次下探只报一次)

#define PIN_BUZZER   21                // G21 BUZ_PWM 无源蜂鸣器
#define NOTIFY_MS    30000             // 通知全屏卡停留时长(毫秒),之后回待命屏(历史列表)

// OTA 固件服务(留在 Mac Mini,设备走家里 WiFi 可达;与 gateway config.toml [thumb].port 一致)
#define THUMB_PORT   8899

// PaperS3 引脚(见官方 PinMap)
#define PIN_USB_DET  5     // G5:USB 供电检测,>0.2V 视为插电
#define PIN_TOUCH_INT 48   // G48:GT911 触摸中断,做深睡唤醒源

// 触摸/视图
#define IDLE_SLEEP_MS 6000   // 电池模式:醒来处理完(收事件+画板)多久回睡;越短越省电

// OTA:插电常开模式下,每隔这么久再查一次新固件(毫秒);电池模式每次唤醒都会查
#define OTA_CHECK_MS 1800000  // 30 分钟

// 电池模式:深睡唤醒间隔(秒)。越大越省电,完成提醒延迟越大。
#define SLEEP_INTERVAL_SEC   300
// 插电模式:保持长连接,MQTT keepalive(秒)
#define MQTT_KEEPALIVE_SEC   30

// MQTT 快照较大,需要放大 PubSubClient 缓冲(默认仅 256B)
#define MQTT_BUFFER_SIZE     8192

// 全刷去残影:每累计这么多次局刷后做一次全刷
#define FULL_REFRESH_EVERY   10

// WiFi/MQTT 连接超时(毫秒)
#define WIFI_TIMEOUT_MS      15000
#define MQTT_WAIT_STATE_MS   6000   // 唤醒后等 retained 快照到达的最长时间
