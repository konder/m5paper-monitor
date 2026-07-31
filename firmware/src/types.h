#pragma once
#include <Arduino.h>

// 与 gateway snapshot.py 的 JSON schema 对应

struct Quota {
    bool  valid = false;
    bool  real  = false;   // true=官方真值(codex);false=日志估算(claude)
    float h5    = -1;      // 已用百分比,-1=无(未校准)
    float week  = -1;
    long  h5_tokens   = -1; // 估算模式下的窗口内 token 数
    long  week_tokens = -1;
    long  h5_reset   = 0;   // unix 秒
    long  week_reset = 0;
    String plan;
};

#define MAX_IMAGES_UI 4

struct ImgRef {
    String id;
    String name;
};

struct Session {
    String src;      // "codex" | "claude"
    String project;
    String state;    // running | idle | done | needs_input
    String task;
    String last_msg; // 最新输出(结果)
    String model;
    long   elapsed_s = -1;
    long   tokens    = -1;
    long   last_activity = 0;
    ImgRef images[MAX_IMAGES_UI];
    int    nImages = 0;
};

#define MAX_SESSIONS_UI 8

struct Snapshot {
    long    ts = 0;
    String  hhmm;          // 生成时刻 HH:MM(设备显示"更新 …")
    Quota   codex;
    Quota   claude;
    Session sessions[MAX_SESSIONS_UI];
    int     nSessions = 0;
};

// ---- v39 消耗看板 ----
// 与 gateway 的 event_hub/usage.py 对应。设备**不做任何 per-provider 逻辑** ——
// 哪个窗口存在、未校准怎么显示、stale 怎么标,全在 gateway 决定好了,
// 这里只负责「画一行油量表」。
#define USAGE_ROWS_MAX 6

struct UsageRow {
    String l;         // 左侧标签,如 "Codex 周"
    String n;         // 右侧小字备注,如 "prolite" / "831/2000G" / "1.4B tok" / "旧"
    String rin;       // 重置倒计时,gateway 已格式化好(设备无 RTC,自己算不了)
    int    rem = -1;  // 剩余百分比;-1 = 未知 → 空条 + 显示 "--"
};

struct Usage {
    bool   valid = false;
    long   ts = 0;
    int    rev = -1;  // gateway 只在实质变化时 +1;相同 rev = 心跳,不必重绘
    String hhmm;      // 顶栏时钟(gateway 按配置时区格式化,不是设备本地时间)
    String foot;      // 底部单行,当前是 LiteLLM 用量
    UsageRow rows[USAGE_ROWS_MAX];
    int    n = 0;
};

// v23 纯通知端:待命屏的历史事件列表项(设备 RAM 环形缓冲)
struct EventItem {
    String kind;      // done | needs_input | quota
    String src;       // codex | claude
    String project;
    String summary;   // 一行摘要(列表用,取正文首行/截断)
    long   ts = 0;
};
