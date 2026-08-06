// render.cpp 的宿主机渲染测试 —— 跑真的 M5GFX 绘制代码,不需要设备、不开窗口。
//   pio test -e native_render      → 产出 /tmp/render_native.pgm(960x540 8bit 灰度)
//
// 原理:M5GFX 的非 ESP 分支会给 M5PaperS3 装 lgfx::Panel_sdl(继承 Panel_HasBuffer,
// 帧缓冲在普通内存里)。我们**不调 Panel_sdl::main()** —— 那条路会
// SDL_CreateRenderer(SDL_RENDERER_ACCELERATED),无头环境下拿不到 renderer 直接失败。
// 自己写 main、画完用 readRect 把帧缓冲读出来,全程不碰 renderer。
#include <unity.h>

#include <M5Unified.h>

#include <cstdio>
#include <cstring>
#include <string>
#include <vector>

#include "config.h"
#include "render.h"
#include "types.h"

static const char* OUT_PGM = "/tmp/render_native.pgm";

void setUp(void) {}
void tearDown(void) {}

// 读回整屏。readRect 的 void* 重载是 bgr888;面板是 grayscale_8bit,三通道相等,
// 取第 0 通道就是精确灰度(和设备侧 postScreenDump 用的是同一条路径)。
static std::vector<uint8_t> grabGray(int& w, int& h) {
    auto& d = M5.Display;
    w = d.width();
    h = d.height();
    std::vector<uint8_t> rgb((size_t)w * h * 3);
    d.readRect(0, 0, w, h, (void*)rgb.data());
    std::vector<uint8_t> gray((size_t)w * h);
    for (size_t i = 0; i < gray.size(); i++) gray[i] = rgb[i * 3];
    return gray;
}

static void writePgm(const char* path, const std::vector<uint8_t>& g, int w, int h) {
    FILE* f = fopen(path, "wb");
    if (!f) return;
    fprintf(f, "P5\n%d %d\n255\n", w, h);
    fwrite(g.data(), 1, g.size(), f);
    fclose(f);
}

static Usage makeUsage() {
    Usage u;
    u.valid = true;
    u.rev = 1;
    u.hhmm = "12:52";
    u.foot = "LiteLLM 7日 107.0M tok · 7617 req";
    const char* l[] = {"Codex 周", "Claude 5h", "Claude 周", "网关"};
    const char* n[] = {"prolite", "177.8M tok", "1.1B tok", "旧 831/2000G"};
    const char* r[] = {"3天后", "1时后", "40分后", "8-17重置"};
    int rem[] = {14, -1, -1, 58};
    for (int i = 0; i < 4; i++) {
        u.rows[i].l = l[i]; u.rows[i].n = n[i]; u.rows[i].rin = r[i]; u.rows[i].rem = rem[i];
    }
    u.n = 4;
    return u;
}

// 最基本的一条:renderUsage 真的能在宿主机上跑完并且画出了东西。
// 这是 641 行 render.cpp 第一次被实际执行 —— 之前只有 Python 复刻版跑过。
static void test_render_usage_produces_image(void) {
    renderInit(true);
    Usage u = makeUsage();
    renderUsage(u, 87, "WiFi", FW_VERSION, true);

    int w = 0, h = 0;
    std::vector<uint8_t> g = grabGray(w, h);
    TEST_ASSERT_EQUAL_INT_MESSAGE(960, w, "宽不是 960 —— 板子选错或旋转不对");
    TEST_ASSERT_EQUAL_INT_MESSAGE(540, h, "高不是 540");

    size_t dark = 0, light = 0;
    for (uint8_t px : g) (px < 128 ? dark : light)++;
    writePgm(OUT_PGM, g, w, h);
    printf("[render] %dx%d 深色像素 %zu / 浅色 %zu → %s\n", w, h, dark, light, OUT_PGM);

    TEST_ASSERT_TRUE_MESSAGE(dark > 1000, "几乎没有深色像素 —— 大概率什么都没画上去");
    TEST_ASSERT_TRUE_MESSAGE(light > dark, "深色占比过高 —— 墨水屏应该是白底为主");
}

// trunc() 是 render.cpp 里按**显示宽度**回删字节的截断,怀疑会切碎 UTF-8:
//   while (s.length() && tw(s + "…") > maxw) s.remove(s.length() - 1);
// 一次删一个字节,中文一个字 3 字节 —— 停在字符中间就会画出半个汉字。
// 这里不直接调 trunc(它是 static),而是渲染一个超长中文标签,再看截断后的串。
static bool isValidUtf8(const char* s, size_t n) {
    size_t i = 0;
    while (i < n) {
        unsigned char c = (unsigned char)s[i];
        size_t need;
        if (c < 0x80)              need = 0;
        else if ((c >> 5) == 0x6)  need = 1;
        else if ((c >> 4) == 0xE)  need = 2;
        else if ((c >> 3) == 0x1E) need = 3;
        else return false;
        if (need > 0 && i + need >= n) return false;
        for (size_t k = 1; k <= need; k++)
            if (((unsigned char)s[i + k] & 0xC0) != 0x80) return false;
        i += need + 1;
    }
    return true;
}

static void test_trunc_keeps_utf8_intact(void) {
    // 标签预算是 BAR_X-LAB_X-12 = 160px,efontCN_24 一个汉字 24px → 6 个字就满了
    String label;
    for (int i = 0; i < 40; i++) label += "超长标签";
    String out = renderTruncForTest(label, 160);
    printf("[trunc] 输入 %u 字节 → 输出 %u 字节: %s\n",
           label.length(), out.length(), out.c_str());
    TEST_ASSERT_TRUE_MESSAGE(isValidUtf8(out.c_str(), out.length()),
                             "trunc 切碎了 UTF-8 —— 墨水屏会画出半个汉字");
}

int main(int, char**) {
    auto cfg = M5.config();
    cfg.clear_display = false;
    M5.begin(cfg);

    UNITY_BEGIN();
    RUN_TEST(test_render_usage_produces_image);
    RUN_TEST(test_trunc_keeps_utf8_intact);
    return UNITY_END();
}
