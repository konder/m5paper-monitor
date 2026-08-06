// Arduino.h 垫片里那两个全局的定义(只进 native 构建)
#include <Arduino.h>

SerialShim Serial;
unsigned long g_fakeMillis = 0;
