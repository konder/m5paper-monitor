#pragma once
#include <stdint.h>

// 是否插着 USB 供电(读 G5 USB_DET)
bool isUsbPowered();

// 电池百分比(0-100),读不到返回 -1
int batteryPercent();

// 进入深睡,seconds 秒后定时唤醒 + 触摸唤醒(电池模式用)
void deepSleepFor(uint32_t seconds);

// 本次是否由触摸唤醒(用于唤醒后直接进入交互)
bool wokenByTouch();

// 本次是否由深睡唤醒(定时/触摸)——唤醒时不覆盖休眠页、跳过"连接中"提示
bool wokeFromDeepSleep();
