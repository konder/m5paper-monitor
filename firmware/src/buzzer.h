#pragma once

void buzzerInit();
void beep(int freq, int ms);
// kind: "done" | "needs_input" | "quota" | "boot"
void buzzPattern(const char* kind);
