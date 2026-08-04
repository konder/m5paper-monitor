#!/usr/bin/env python3
"""让设备把当前屏幕回传成 PNG —— 给 AI agent 看渲染结果用的「截图键」。

链路:本脚本发 MQTT cmd=dump → 设备 readRect 读 PSRAM 里的帧缓冲 → HTTP POST 给
thumbserver 的 /screen → 落成灰度 PNG 到 <repo>/shots/。设备侧见 ota.cpp:postScreenDump。

在 **网关机(Mac Mini)** 上跑:那里 broker、shots/、paho 都是本地的。
  python3 tools/dump_screen.py                  # 发命令,等新图,打印路径
  python3 tools/dump_screen.py --timeout 320    # 设备在电池模式深睡,等一个唤醒周期

拿回图片不用 ssh 捞文件,thumbserver 直接给:
  curl -s http://<网关>:8899/shots/latest.png -o /tmp/screen.png

⚠️ 设备在 BLE 模式时 WiFi 是关的(射频互斥),dump 会被跳过并在串口打日志 ——
调试期把 config.toml 的 [channels] ble 设为 false,设备开机 30s 后自动落 WiFi 兜底。
"""
from __future__ import annotations

import argparse
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))  # 仓库根

from event_hub.collector import load_config          # 自带 py3.9 的 _tiny_toml 兜底
from event_hub.thumbserver import SHOTS_DIR

LATEST = os.path.join(SHOTS_DIR, "latest.png")


def _mtime(path: str) -> float:
    try:
        return os.path.getmtime(path)
    except OSError:
        return 0.0


def main() -> int:
    ap = argparse.ArgumentParser(description="触发设备回传屏幕并等待 PNG 落盘")
    ap.add_argument("--timeout", type=float, default=60.0,
                    help="等新图的秒数(默认 60;电池模式深睡最长要等 SLEEP_INTERVAL_SEC=300)")
    ap.add_argument("--broker", help="覆盖 broker 地址(默认取 config.toml 的 [mqtt] host)")
    args = ap.parse_args()

    cfg = load_config()["mqtt"]
    host = args.broker or cfg["host"]
    # 设备订阅的是 config.h 里写死的 TOPIC_CMD;允许配置覆盖,默认与固件保持一致
    topic = cfg.get("cmd_topic", "m5paper/cmd")

    before = _mtime(LATEST)

    import paho.mqtt.client as mqtt
    cli = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
    if cfg.get("username"):
        cli.username_pw_set(cfg["username"], cfg.get("password") or None)
    cli.connect(host, int(cfg["port"]), keepalive=30)
    cli.loop_start()
    # QoS1:设备用持久会话订阅(main.cpp connectMqtt),深睡期间 broker 会替它排队,醒来补发
    info = cli.publish(topic, "dump", qos=1)
    info.wait_for_publish(timeout=10)
    print(f"[dump] 已发 {topic}=dump → {host}:{cfg['port']}", file=sys.stderr)
    cli.loop_stop()
    cli.disconnect()

    deadline = time.time() + args.timeout
    while time.time() < deadline:
        if _mtime(LATEST) > before:
            time.sleep(0.3)          # 让 os.replace 落稳(其实是原子的,纯保险)
            print(LATEST)
            return 0
        time.sleep(0.5)

    print(f"[dump] 等了 {args.timeout:.0f}s 没有新图。排查顺序:"
          f"设备是否在线(看 m5paper/device 遥测)→ 是否 BLE 模式(dump 会跳过)→ "
          f"thumbserver 是否在跑(curl /health)", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
