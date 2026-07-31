#!/usr/bin/env python3
"""BLE 渠道 —— Mac Mini 当中枢,经原生 Swift helper 直连设备。**不需要 MQTT broker。**

    collector ──直接调用──▶ BleChannel ──espble──▶ <名字>BLEHelper.app ──NUS──▶ M5PaperS3

v40 起链路层整个搬到了 **esp-ble-link**(`pip install espble`):
进程邮箱、四条 open/kill 的坑、两级退避、ATT 写 keepalive、retained + 历史重放,
全都在那个包里,这里只剩 m5work 自己的东西 —— 事件信封、遥测落盘、CLI。

    旧文件/旧常量        新归属
    ------------------  ------------------------------------------
    helper_session.py   espble.HelperSession           (已删)
    native_ble/*        espble/native/*                (已删)
    ble.py 的下半部分    espble.BleLink + RetainedChannel
    MAX_LINE_BYTES      espble.framing.DEFAULT_LINE_LIMIT
    NUS UUID / 帧上限    espble::LinkConfig 的默认值

本文件**刻意不再出现任何协议细节**(UUID、分片长度、单行字节上限、分隔符)——
那些一旦在两处各写一份,就迟早会不一致。这里只留 m5work 自己的东西。

装依赖(框架还没开源,先指本地路径):
    pip install -e ~/esp-ble-link/host
编 helper(bundle id 必须唯一,沿用原来那个):
    espble build-helper --name M5PaperBLEHelper \\
                        --bundle-id com.nanzhang.m5paper.blehelper \\
                        --usage-desc "M5Paper 消耗看板通过蓝牙低功耗与 M5PaperS3 通信。"

单独跑(联调用,不启 collector):
    python3 -m event_hub.channels.ble --test "手动测试通知"
    python3 -m event_hub.channels.ble --ota
    python3 -m event_hub.channels.ble --watch          # 只连着看设备遥测
"""
from __future__ import annotations

import os
import sys
import time

from espble import BleLink, DeviceConfig, RetainedChannel

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

DEFAULT_APP = os.path.join(_REPO_ROOT, ".build", "native", "M5PaperBLEHelper.app")
DEFAULT_SESSION_DIR = os.path.expanduser("~/.config/m5paper-monitor/ble-session")
TELEMETRY_LOG = os.environ.get("M5_TELEMETRY_LOG", "/tmp/m5_telemetry.log")


def _log(*a):
    print("[ble]", *a, file=sys.stderr, flush=True)


def ev_envelope(ev: dict, live: bool) -> dict:
    """事件信封。字段必须与固件 handleBleMessage 对应(main.cpp)。"""
    return {
        "t": "ev", "live": live,
        "kind": ev.get("kind", "done"), "src": ev.get("src", ""),
        "project": ev.get("project", "?"), "msg": ev.get("msg", ""),
        "meta": ev.get("meta", ""), "ts": ev.get("ts", 0),
    }


class BleChannel:
    """与 MqttPublisher 同接口,collector 无需知道底层是 BLE 还是 MQTT。"""

    def __init__(self, cfg: dict):
        c = (cfg or {}).get("ble") or {}
        device = DeviceConfig(
            app_path=os.path.expanduser(c.get("helper_app") or DEFAULT_APP),
            session_dir=os.path.expanduser(c.get("session_dir") or DEFAULT_SESSION_DIR),
            device_name=c.get("device_name") or "",
            name_prefix=c.get("name_prefix") or "m5paper-",  # v41:身份由固件从 efuse MAC 派生
            scan_timeout=float(c.get("scan_timeout", 20.0)),
        )
        # autostart=False:等 RetainedChannel 把 on_connect / keepalive 接上再启动,
        # 否则首次连接可能赶在补推钩子挂上之前完成。
        self._link = BleLink(
            device,
            reconnect_sec=float(c.get("reconnect_sec", 5.0)),
            backoff_max_sec=float(c.get("backoff_max_sec", 45.0)),
            keepalive_sec=float(c.get("keepalive_sec", 30.0)),
            on_notification=self._on_telemetry,
            autostart=False,
        )
        # retained=最新看板(设备一连上就补推,也被拿来当 keepalive 帧);
        # history=最近 N 条事件(重连后以 live=false 重放,只进列表不蜂鸣)。
        self._channel = RetainedChannel(
            self._link,
            history_n=int(c.get("history_n", 8)),
            keepalive_key="usage",
        )

    # ---- 渠道接口(collector 调用)----

    @property
    def connected(self) -> bool:
        return self._channel.connected

    def publish_state(self, snap: dict):
        """设备不消费全量快照(v23 起就废弃了),BLE 上不发 —— 白占带宽。"""
        return

    def publish_usage(self, payload: dict):
        # retained:设备每次(重)连上补推最新看板。
        # 固件按 rev 去重,所以 keepalive 反复重发同一帧不会刷爆墨水屏。
        self._channel.set_retained("usage", {"t": "usage", **payload})

    def publish_event(self, ev: dict):
        # 存的就是信封本身,重放时 RetainedChannel 默认把 live 改成 False。
        self._channel.publish(ev_envelope(ev, live=True))

    def send_cmd(self, cmd: str):
        """如 ota:设备收到会临时切 WiFi 走 HTTP OTA(thumbserver),不依赖 broker。"""
        # 指令值得等,断连时也排队。
        self._channel.send_now({"t": "cmd", "cmd": cmd}, queue_while_offline=True)

    def close(self):
        self._channel.close()

    # ---- 内部 ----

    def _on_telemetry(self, line: str):
        """设备 notify 上来的电量遥测。在监护线程里被调用,别做重活。"""
        _log("telemetry", line)
        try:
            with open(TELEMETRY_LOG, "a") as fh:
                fh.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {line}\n")
        except OSError:
            pass


# ---------- 命令行联调 ----------
def _cli():
    import argparse
    ap = argparse.ArgumentParser(description="BLE 渠道联调工具(不启 collector)")
    ap.add_argument("--test", metavar="MSG", help="发一条测试通知")
    ap.add_argument("--ota", action="store_true", help="发 ota 指令")
    ap.add_argument("--watch", action="store_true", help="连着看设备遥测")
    ap.add_argument("--seconds", type=float, default=60.0, help="--watch 持续秒数")
    a = ap.parse_args()

    ch = BleChannel({})
    _log("等待连接…")
    if not ch._link.wait_connected(40):
        _log("没连上:", ch._link.fatal_error or "超时")
        ch.close()
        return 1

    if a.test:
        ch.publish_event({"kind": "done", "src": "codex", "project": "manual-test",
                          "msg": a.test, "meta": "手动测试", "ts": int(time.time())})
        _log("已发送:", a.test)
    if a.ota:
        ch.send_cmd("ota")
        _log("已发 ota")
    time.sleep(a.seconds if a.watch else 3)
    ch.close()
    return 0


if __name__ == "__main__":
    sys.exit(_cli())
