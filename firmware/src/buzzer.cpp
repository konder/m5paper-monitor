#include "buzzer.h"
#include "config.h"
#include <Arduino.h>

// 用 LEDC PWM 直驱 G21 无源蜂鸣器(不依赖 M5Unified Speaker 映射)
// arduino-esp32 2.x API:ledcSetup(channel,freq,res) + ledcAttachPin(pin,channel)
#define BUZ_CH 6
static bool s_ok = false;

void buzzerInit() {
    ledcSetup(BUZ_CH, 2000, 10);
    ledcAttachPin(PIN_BUZZER, BUZ_CH);
    s_ok = true;
}

void beep(int freq, int ms) {
    if (!s_ok) buzzerInit();
    ledcWriteTone(BUZ_CH, freq);
    delay(ms);
    ledcWriteTone(BUZ_CH, 0);
}

void buzzPattern(const char* kind) {
    String k(kind);
    if (k == "done") {                 // 悦耳上行双响
        beep(1568, 130); delay(50); beep(2093, 180);
    } else if (k == "needs_input") {   // 急促三响
        for (int i = 0; i < 3; i++) { beep(2600, 90); delay(80); }
    } else if (k == "quota") {         // 低沉长鸣
        beep(700, 450);
    } else {                            // boot / 测试
        beep(1200, 90);
    }
}
