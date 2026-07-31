#include "power.h"
#include "config.h"
#include <M5Unified.h>
#include <esp_sleep.h>

bool isUsbPowered() {
    // G5 分压检测:>200mV 认为 USB 在供电
    int mv = analogReadMilliVolts(PIN_USB_DET);
    return mv > 200;
}

int batteryPercent() {
    int lvl = M5.Power.getBatteryLevel();  // M5Unified 内部读 BAT_ADC
    if (lvl < 0) return -1;
    if (lvl > 100) lvl = 100;
    return lvl;
}

void deepSleepFor(uint32_t seconds) {
    // 定时唤醒(始终启用)
    esp_sleep_enable_timer_wakeup((uint64_t)seconds * 1000000ULL);
    // 触摸唤醒:仅当 GT911 INT(G48)当前为高(真空闲)时才用"低电平唤醒",
    // 否则会一睡下就被低电平立刻唤醒(秒醒),导致休眠页一闪而过 + 费电。
    pinMode(PIN_TOUCH_INT, INPUT);
    if (digitalRead(PIN_TOUCH_INT) == HIGH) {
        esp_sleep_enable_ext0_wakeup((gpio_num_t)PIN_TOUCH_INT, 0);
    }
    M5.Display.waitDisplay();
    esp_deep_sleep_start();
}

bool wokenByTouch() {
    return esp_sleep_get_wakeup_cause() == ESP_SLEEP_WAKEUP_EXT0;
}

bool wokeFromDeepSleep() {
    auto c = esp_sleep_get_wakeup_cause();
    return c == ESP_SLEEP_WAKEUP_TIMER || c == ESP_SLEEP_WAKEUP_EXT0;
}
