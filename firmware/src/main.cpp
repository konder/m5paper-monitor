// M5PaperS3 —— BLE-only 事件通知端(v46)。
//
// **只有一个常驻链路:BLE**(连 Mac Mini 中枢,~1-5mA,即时)。数据(usage/ev/cmd)全走 BLE。
// WiFi 不再是一个「模式」,而是一件**临时借用射频**去干的事 —— 只有 OTA 和屏幕 dump
// 需要它,干完立刻还给 BLE(见 withWifi)。
//
// v46 拿掉了 v30 那套「BLE 掉线 60s → 切 WiFi+MQTT 常驻 → 每 5 分钟回试 BLE」的双模状态机。
// 原因是它自相矛盾:射频互斥,常驻 WiFi 让 BLE 只能每 300s 挤出 15s 广播(5% 占空比),
// 而主机的重连退避跟这个窄窗口互相错过 —— 实测中枢连 240s 一次都没连上,
// 而连续扫描一扫就到(-48 dBm)。BLE 常驻广播后主机秒连,这类时序错配从根上消失。
// 顺带:`espble::end()` 从「每 5 分钟必走」变成「只有 OTA/dump 才走」,
// A13 那类拆栈 bug 的暴露面也一起缩小了。
//
// ⚠️ **代价(老板明确选择):没有不依赖 BLE 的自主更新通道了。** 以前开机走 WiFi 兜底时
// 会顺手 checkOTA(),那是一条 BLE 坏了也能远程救回来的路。现在 OTA 只能靠 BLE 下发
// cmd=ota —— 刷坏了只能接 USB(而这块板的 USB 有 download 模式锁死的历史,见 A11)。
// 改这个文件前想清楚:**你正在动的是唯一的远程入口。**
//
// 保留:EPD 关电省电 + 电池模式大电量待机页。
#include <M5Unified.h>
#include <WiFi.h>
#include <ArduinoJson.h>
#include <esp_pm.h>
#include <esp_wifi.h>
#include <esp_system.h>          // esp_reset_reason:分辨「看门狗救的」和「正常开机」
#include <esp_task_wdt.h>        // 硬件 TWDT —— 主循环挂死时唯一还能救的东西

#include "secrets.h"
#include "config.h"
#include "types.h"
#include "logic.h"
#include "render.h"
#include "power.h"
#include "ota.h"
#include "buzzer.h"
#include <EspBleLink.h>

RTC_DATA_ATTR uint32_t g_bootCount = 0;
static bool g_usb = true;

// v46:没有 Mode 了 —— BLE 是唯一常驻链路,WiFi 是临时借用(见 withWifi)。
// MQTT 客户端也一并去掉:usage/ev/cmd 全部经 BLE 到达(handleBleMessage 与旧的
// onMessage 处理的是同样三种消息,且多支持 live 标志用于历史重放)。
// 注:logic.cpp 里的 onMessage() 及其 7 个单测暂时留着(纯解析函数,不再被调用)——
// 那几个测试盯着真实的子串匹配 bug("du" 不该匹配 dump),不值得跟这次改动一起删。

enum View { V_IDLE, V_NOTIFY };
static View g_view = V_IDLE;
static uint32_t g_notifyUntil = 0;

// 事件/看板的状态与解析逻辑住在 logic.{h,cpp}(g_hist / g_usage / g_ev* / g_doOta …),
// 那部分不碰屏幕射频,能在 native 单测里跑。这里只留渲染节奏和模式切换相关的。
static uint32_t g_idleRenders = 0;

// g_usageRenders 是**独立**的全刷计数器 —— 和 g_idleRenders 共用会让
// 两个页面的局刷/全刷节奏交错,残影攒不掉。
static uint32_t g_usageRenders = 0;

// ---- 前向声明 ----
static void showIdle();
static void showNotify(const String&, const String&, const String&, const String&, const String&, long);
static void configurePowerSave();
static void bleStart();
static void withWifi(const char* what, void (*body)());
static void sendBattery();

static void showIdle() {
    g_view = V_IDLE;
    // v46: 只有 BLE 一条链路了。"BLE…" = 在广播但中枢还没连上。
    // 这个省略号是**唯一**能从屏幕上看出「中枢没连上」的地方(这块板不能读串口),
    // 所以别把它简化掉。
    const char* link = espble::connected() ? "BLE" : "BLE…";
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

// setEvent / applyUsage / handleBleMessage / onMessage 已移到 logic.{h,cpp}(可 native 单测)

// 电池模式开自动轻睡眠;WiFi 模式再叠加 WiFi modem sleep。插电全速。
static void configurePowerSave() {
    // v46:不再设 WiFi.setSleep —— BLE 常驻时 WiFi 是关着的,只有 withWifi 里那几十秒
    // 才开,那段时间要的是尽快传完(OTA 镜像 1.29MB / 截图上传),不是省电。
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
    // c/d/st 是 v45 为排查加的,v46 起**保留**,因为在 BLE-only 下它们的含义更重要了:
    //   c/d = 累计建连/断连次数 —— 唯一能看出链路 flapping 的地方(重启清零,用 up 关联)
    //   st  = BLE 协议栈还在不在。**BLE-only 下 st=0 就是「设备已失联」** ——
    //         没有 WiFi 兜底了,栈掉了就再也没人能连上它,这是最该报警的一个位。
    // 没有 link 字段了:只有一条链路,写死一个 "ble" 没有信息量(屏幕上的 "BLE…" 才有)。
    // sd/ar 是框架可见性看门狗的战绩,**非 0 就说明设备本来会失联**:
    //   sd = 「以为连着、其实早断了」被纠正(对端进程被杀、主机崩,断连回调丢了)
    //   ar = 「没连着又没广播」被重新拉起广播
    // BLE-only 没有 WiFi 兜底,失联就只能接 USB,所以这两个数值得一直盯着。
    // v48 加的三个是**接收侧**统计,专门用来判「连上约 1 秒就断」是不是被灌爆:
    //   rd = rxDroppedBytes,环形缓冲满而丢弃。**非 0 就是主循环排空太慢**(见 A2)
    //   rf = rxFrames,成功组出的完整帧 —— 给 rd 当分母,不然不知道 rd 严不严重
    //   ro = rxOversize,超过 maxFrameBytes 被整条丢弃的帧
    // 判据是阳性的:rd 一直 0 就能**排除**接收侧被打爆这条线,省得继续猜。
    // v49 加 rst = esp_reset_reason()。这是**硬件看门狗有没有救过场的唯一远程证据** ——
    // 6 = ESP_RST_TASK_WDT,即「主循环挂死被复位」。1=上电 3=软复位 4=panic。
    // 串口只有插着 USB 才看得到,而这个设备平时是纯 BLE 的,所以必须进遥测。
    const espble::LinkStats& st = espble::stats();
    char buf[300];
    snprintf(buf, sizeof(buf),
        "{\"pct\":%d,\"mv\":%d,\"up\":%lu,\"usb\":%d,\"v\":%d,\"g5\":%d,\"chg\":%d,\"ls\":%d"
        ",\"c\":%lu,\"d\":%lu,\"st\":%d,\"sd\":%lu,\"ar\":%lu"
        ",\"rd\":%lu,\"rf\":%lu,\"ro\":%lu,\"rst\":%d}",
        batteryPercent(), M5.Power.getBatteryVoltage(), (unsigned long)(millis() / 1000), g_usb ? 1 : 0,
        FW_VERSION, analogReadMilliVolts(PIN_USB_DET), (int)M5.Power.isCharging(), g_usb ? 0 : 1,
        (unsigned long)st.connects, (unsigned long)st.disconnects,
        espble::started() ? 1 : 0,
        (unsigned long)st.staleDrops, (unsigned long)st.advRestarts,
        (unsigned long)st.rxDroppedBytes, (unsigned long)st.rxFrames,
        (unsigned long)st.rxOversize, (int)esp_reset_reason());
    // 未连接时 notify() 静默丢弃 —— 没关系,中枢重连后会拿到下一个周期的。
    espble::notify(String(buf));
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

// ---- 射频:BLE 常驻,WiFi 按需借用(两者互斥)----

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

// 起 BLE 并开始广播。**不等连接** —— 以前那个「阻塞等 30s」是双模状态机的遗物
// (要在超时后决定切不切 WiFi)。现在没有要决定的事了:一直广播,中枢什么时候来都行。
// 不阻塞的额外好处是开机能立刻画屏,而不是先黑 30 秒。
static void bleStart() {
    WiFi.disconnect(true); WiFi.mode(WIFI_OFF);   // 把 2.4G 完整留给 BLE
    if (!espble::begin(bleConfig())) Serial.println("[ble] begin 失败(内存?)");
    configurePowerSave();
    Serial.println("[ble] advertising");
}

// 借用射频跑一件需要 WiFi 的事,干完还给 BLE。目前只有两个调用者:OTA 和屏幕 dump。
//
// ⚠️ 必须真的 end() 把 BLE 协议栈停掉 —— 这块板 BLE/WiFi 射频互斥,只 stopAdvertising
//    是不够的。这一行在框架 <0.1.1 时**每次都 panic**(A13),所以 lib_deps 必须 ≥0.1.1。
// ⚠️ body() 可能不返回:checkOTA() 拉到新镜像会直接重启。所以还原 BLE 的代码写在后面
//    是「没升级成功」才会走到的路径 —— 这是对的,不要试图在重启前"清理"。
static void withWifi(const char* what, void (*body)()) {
    Serial.printf("[wifi] 借用射频:%s\n", what);
    // ⚠️ 这两件事(拉 1.3MB 固件、回传整屏)本来就要几十秒到几分钟,远超 TWDT 超时。
    //    不关掉看门狗的话它们**必然**触发复位 —— OTA 永远升不上去。
    //    代价老实说:这段时间没有看门狗保护。可接受,因为它们都是人手动触发的
    //    一次性操作(BLE 下发 cmd),不是常驻路径;常驻路径才是需要兜底的那个。
    disableLoopWDT();
    renderStatus((String("连 WiFi:") + what + "…").c_str());
    espble::end();
    WiFi.mode(WIFI_STA);
    if (wifiConnectNVS()) {
        body();
    } else {
        Serial.println("[wifi] 连不上,放弃");
    }
    WiFi.disconnect(true); WiFi.mode(WIFI_OFF);
    enableLoopWDT();     // 长耗时段结束,把兜底装回去(要在 bleStart 之前 ——
                         // 恢复 BLE 这一步失败才是最该被看门狗接住的)
    bleStart();          // 还给 BLE —— 这一步失败设备就失联了,所以放在最后且无条件执行
    showIdle();
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

    // BLE-only:开始广播就完事,不等中枢(等不等它都一样要广播)。
    // 注意这里**没有** checkOTA() 了 —— 以前开机走 WiFi 兜底时会顺手查一次更新,
    // 那是唯一不依赖 BLE 的救援通道。老板明确选择去掉,现在只认 BLE 下发的 cmd=ota。
    bleStart();
    Serial.printf("[boot] v%d BLE-only usb=%d boot#%u rst=%d\n",
                  FW_VERSION, g_usb, g_bootCount, (int)esp_reset_reason());

    // ---- 硬件看门狗:主循环挂死的唯一救援通道 ----
    //
    // 为什么非要硬件的:框架的 checkVisibility() 是搭 popMessage() 的车跑的,
    // 而 popMessage() 由**主循环**调 —— 主循环一挂,那个看门狗跟着挂,一起死。
    // 实测代价:设备静默 18 小时(串口无输出、不广播、主机 786 次连接失败),
    // coredump 区全 0xff(没崩)、判活探针说它在跑应用代码 —— 就是主循环卡住了。
    // BLE-only 没有 WiFi 兜底,那次只有接 USB 拉 EN 脚才救回来。
    //
    // TWDT 不依赖任何软件路径:主循环超过 WDT_TIMEOUT_S 没喂它,芯片自己复位。
    // Arduino 的 loop() 外壳每轮自动喂(enableLoopWDT 之后),所以业务代码不用管。
    //
    // ⚠️ 超时要给够。墨水屏全刷 1~2 秒,加上一轮里可能连着刷屏 + 发帧,
    //    定太短会把正常操作判成挂死 —— 那比不装看门狗更糟(无限复位环)。
    //    30 秒:任何正常单轮都远够,而 18 小时失联变成 30 秒空档。
    // ⚠️ 这里用的是 **IDF 4.x** 的老 API(本工程 framework-arduinoespressif32 是
    //    core 2.x / IDF 4.4):esp_task_wdt_init(超时秒数, panic)。
    //    IDF 5.x 那套 esp_task_wdt_config_t + esp_task_wdt_reconfigure() 在这里
    //    **编译不过** —— 别照 5.x 的文档抄(我已经踩过一次)。
    //    再调一次 init 就是重新配超时,是允许的(Arduino 启动时已经初始化过 TWDT)。
    esp_err_t we = esp_task_wdt_init(WDT_TIMEOUT_S, true);   // panic=true → 复位
    enableLoopWDT();                                        // 把 loopTask 挂上去
    Serial.printf("[wdt] init=%s timeout=%ds\n", esp_err_to_name(we), WDT_TIMEOUT_S);

    showIdle();
}

void loop() {
#ifdef WDT_SELFTEST
    // ---- 看门狗阳性自测(默认不编进去)----
    // **故意**把主循环挂死,验证 TWDT 真的会复位芯片、且复位后 rst=6(ESP_RST_TASK_WDT)。
    //
    // 为什么非做不可:上一个看门狗(框架的 checkVisibility)装了却救不了自己 ——
    // 它住在主循环里,主循环挂了它一起挂。「装上了」和「会生效」是两件事,
    // 而这个东西平时不响,不主动测就永远不知道它是不是哑的。
    //
    // 用法:
    //   PLATFORMIO_BUILD_FLAGS=-DWDT_SELFTEST python3 -m platformio run -e PaperS3 -d firmware
    //   刷进去 → 开机 45 秒后主循环卡死 → 约 30 秒后应自动复位 →
    //   遥测里 rst=6 即通过 → 然后刷回干净固件。
    if (millis() > 45000) {
        Serial.println("[wdt] 自测:开始故意挂死主循环,30s 后应被复位");
        Serial.flush();
        for (;;) { __asm__ __volatile__("nop"); }   // 不喂狗、不 delay
    }
#endif
    uint32_t now = millis();

    String bmsg;
    while (espble::popMessage(bmsg)) handleBleMessage(bmsg);

    // 掉线不用做任何事:框架的 onDisconnect 会自动重新广播,中枢会自己回来。
    // (以前这里有个 60s 计时器用来切 WiFi 兜底,v46 连同那套状态机一起删了。)

    // 下面两件事需要 WiFi —— 借一下射频,干完还给 BLE。
    // v46 起 dump 也走这条路:以前它在 BLE 模式下直接跳过,理由是"切一次代价太大",
    // 但兜底模式没了之后"让设备走 WiFi 兜底"这个前提也没了,再跳过就等于永久不可用。
    if (g_doOta)  { g_doOta  = false; withWifi("查更新", checkOTA); }
    if (g_doDump) { g_doDump = false; withWifi("回传屏幕", postScreenDump); }

    // 事件 → 通知卡(live)/ 历史(补发)
    if (g_haveEvent) {
        g_haveEvent = false;
        addHistory(g_evKind, g_evSrc, g_evProject, g_evMsg, g_evTs);
        if (g_evLive) showNotify(g_evKind, g_evSrc, g_evProject, g_evMsg, g_evMeta, g_evTs);
        else g_idleDirty = true;
    }

    now = millis();

    // v39 看板重绘节流:gateway 只在实质变化时 bump rev(applyUsage 已按 rev 去重),
    // 这里再压一道最短间隔。判定逻辑在 logic.cpp 的 UsageRepaintGate(可 native 单测)。
    static UsageRepaintGate usageGate;
    if (usageGate.shouldPaint(now, g_usb, g_view == V_IDLE, g_usageDirty, USAGE_REPAINT_MIN_MS)) {
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
