#!/usr/bin/env python3
"""假 hubd —— 只说协议,不碰蓝牙。给 test_ble_channel 当真子进程用。

**为什么是真子进程而不是 mock。** BleChannel 现在的全部职责就是"把方法调用翻译成
管道上的 JSON 行、把管道上的 JSON 行翻译回内部状态"。mock 掉 Popen 的话,恰好把
最容易错的那层(换行分帧、编码、flush、块缓冲)跳过去了 —— 而那正是这次重构
唯一的新风险。所以这里起一个真进程,让每个字节都真的过一遍管道。

行为:
  - 按 `--device id[:别名]` 组出设备表,启动即 `ready`
  - 收到的每条指令原样追加到 `$STUB_HUBD_RECORD`(一行一条 JSON)
  - `{"op":"status"}` 回 status
  - `$STUB_HUBD_EMIT` 指向一个文件,里面每行一个事件 → 启动后原样吐出去
    (用来模拟设备遥测、掉线之类的推送)
  - `$STUB_HUBD_DIE_AFTER=N` → 吐完 ready 等 N 秒就自杀,用来验监护重启
"""
import argparse
import json
import os
import sys
import time


def emit(obj):
    sys.stdout.write(json.dumps(obj, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--app")
    ap.add_argument("--device", action="append", default=[])
    ap.add_argument("--device-type", default="")
    ap.add_argument("--registry")
    ap.add_argument("--session-root")
    ap.add_argument("--history-n", type=int, default=8)
    ap.add_argument("--keepalive-sec", type=float, default=30.0)
    ap.add_argument("--reconnect-sec", type=float, default=5.0)
    ap.add_argument("--backoff-max-sec", type=float, default=45.0)
    ap.add_argument("--scan-timeout", type=float, default=20.0)
    args = ap.parse_args()

    devices = {}
    for item in args.device:
        dev_id, _, alias = item.partition(":")
        devices[dev_id] = {"alias": alias or dev_id, "type": args.device_type,
                           "name": f"{args.device_type}-{dev_id}", "fw": 0,
                           "caps": [], "connected": True, "fatal": ""}

    record = os.environ.get("STUB_HUBD_RECORD", "")
    emit({"event": "ready", "devices": devices})

    for path in ([os.environ["STUB_HUBD_EMIT"]] if os.environ.get("STUB_HUBD_EMIT") else []):
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                if line.strip():
                    emit(json.loads(line))

    die_after = float(os.environ.get("STUB_HUBD_DIE_AFTER", "0") or 0)
    if die_after:
        time.sleep(die_after)
        return 7                      # 非 0 退出码,好在日志里认出是"死了"而不是"收摊了"

    while True:
        raw = sys.stdin.readline()
        if not raw:                   # EOF:调用方走了
            return 0
        raw = raw.strip()
        if not raw:
            continue
        if record:
            with open(record, "a", encoding="utf-8") as fh:
                fh.write(raw + "\n")
        try:
            cmd = json.loads(raw)
        except ValueError:
            emit({"event": "error", "message": "坏 JSON"})
            continue
        if cmd.get("op") == "status":
            emit({"event": "status", "devices": devices})


if __name__ == "__main__":
    sys.exit(main())
