#pragma once
// 只给 native 单测用的最小 Arduino 垫片(设备构建永远看不到这个文件 —— 它只在
// platformio.ini 的 [env:native] 里进 -I 路径)。
//
// 范围严格限定在 logic.cpp / types.h 真正用到的那点东西:String、Serial、byte、millis。
// 不要往这里堆 GPIO/WiFi 之类 —— 需要那些就说明该函数不属于「纯逻辑」,不该进 logic.cpp。
#include <cstdarg>
#include <cstdint>
#include <cstdio>
#include <cstring>
#include <string>

typedef uint8_t byte;

// Arduino String 的子集。内部用 std::string,只暴露固件里实际调用到的方法。
class String {
public:
    String() {}
    String(const char* s) : v(s ? s : "") {}
    String(const char* s, size_t n) : v(s ? std::string(s, n) : std::string()) {}
    String(const std::string& s) : v(s) {}
    String(int n) { char b[24]; snprintf(b, sizeof b, "%d", n); v = b; }
    String(long n) { char b[24]; snprintf(b, sizeof b, "%ld", n); v = b; }

    const char* c_str() const { return v.c_str(); }
    unsigned length() const { return (unsigned)v.size(); }
    bool isEmpty() const { return v.empty(); }

    // 找不到返回 -1,与 Arduino 一致
    int indexOf(char c) const {
        auto p = v.find(c);
        return p == std::string::npos ? -1 : (int)p;
    }
    int indexOf(const char* s) const {
        auto p = v.find(s);
        return p == std::string::npos ? -1 : (int)p;
    }

    String substring(unsigned from) const {
        return from >= v.size() ? String() : String(v.substr(from));
    }
    String substring(unsigned from, unsigned to) const {
        if (from >= v.size() || to <= from) return String();
        if (to > v.size()) to = (unsigned)v.size();
        return String(v.substr(from, to - from));
    }

    // Arduino 的 trim() 是原地改,不是返回新串 —— 这个差别会影响 summarize 的行为
    void trim() {
        const char* ws = " \t\n\r\f\v";
        auto b = v.find_first_not_of(ws);
        if (b == std::string::npos) { v.clear(); return; }
        v = v.substr(b, v.find_last_not_of(ws) - b + 1);
    }

    String& operator+=(const String& o) { v += o.v; return *this; }
    String& operator+=(const char* s) { v += (s ? s : ""); return *this; }
    friend String operator+(String a, const String& b) { a.v += b.v; return a; }
    friend String operator+(String a, const char* b) { a.v += (b ? b : ""); return a; }
    friend String operator+(String a, int b) { return a + String(b); }

    bool operator==(const String& o) const { return v == o.v; }
    bool operator==(const char* s) const { return v == (s ? s : ""); }
    bool operator!=(const String& o) const { return !(*this == o); }

    // ArduinoJson 的 ArduinoStringAdapter 需要这两个
    void concat(const char* s, size_t n) { v.append(s, n); }
    char operator[](unsigned i) const { return i < v.size() ? v[i] : '\0'; }

    std::string v;
};

// Serial:单测里就是往 stdout 打,方便看 applyUsage 的心跳/更新日志
struct SerialShim {
    int printf(const char* fmt, ...) {
        va_list ap; va_start(ap, fmt);
        int n = vprintf(fmt, ap);
        va_end(ap);
        return n;
    }
    void print(const char* s) { fputs(s ? s : "", stdout); }
    void println(const char* s) { puts(s ? s : ""); }
    void println() { putchar('\n'); }
};
extern SerialShim Serial;

// 单测里由用例自己推进"时间",默认停在 0
extern unsigned long g_fakeMillis;
inline unsigned long millis() { return g_fakeMillis; }
