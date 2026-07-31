"""多渠道扇出:collector 只面对一个对象,底下可以同时挂 BLE 和 MQTT。

存在的理由是设备固件是**双模**的(BLE 优先 + WiFi/MQTT 兜底,见 firmware/src/main.cpp):
- 只开 ble  → 完全不需要 mosquitto,但设备落到 WiFi 兜底时就收不到东西了
- 两个都开 → BLE 断了设备切 WiFi 仍能收(代价是要留着 broker)
- 只开 mqtt → 退回 v38 的行为

任一渠道抛异常只记日志、不影响其它渠道,也不会掀翻主循环 ——
一条链路坏掉不该让整个看板停摆。
"""
from __future__ import annotations

import sys


class MultiChannel:
    def __init__(self, channels: list):
        self.channels = [c for c in channels if c is not None]

    def _fan(self, method: str, *args):
        for c in self.channels:
            fn = getattr(c, method, None)
            if fn is None:
                continue
            try:
                fn(*args)
            except Exception as e:
                print(f"[warn] {type(c).__name__}.{method} 失败: {e}", file=sys.stderr)

    def publish_state(self, snap: dict):
        self._fan("publish_state", snap)

    def publish_event(self, ev: dict):
        self._fan("publish_event", ev)

    def publish_usage(self, payload: dict):
        self._fan("publish_usage", payload)

    def close(self):
        self._fan("close")

    def describe(self) -> str:
        if not self.channels:
            return "(无渠道!)"
        parts = []
        for c in self.channels:
            name = type(c).__name__
            conn = getattr(c, "connected", None)
            parts.append(f"{name}{'(已连)' if conn else ''}" if conn is not None else name)
        return " + ".join(parts)
