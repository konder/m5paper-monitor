#!/usr/bin/env python3
"""BLE 渠道 —— Mac Mini 当中枢,经原生 Swift helper 直连设备。**不需要 MQTT broker。**

    collector ──直接调用──▶ BleChannel ──espble.BleHub──┬─▶ worker 进程 ──NUS──▶ 设备 A
                                                        └─▶ worker 进程 ──NUS──▶ 设备 B

v40 起链路层整个搬到了 **esp-ble-link**:
进程邮箱、四条 open/kill 的坑、两级退避、ATT 写 keepalive、retained + 历史重放,
全都在那个包里,这里只剩 m5work 自己的东西 —— 事件信封、遥测落盘、CLI。

**v47 起底座换成 BleHub(多设备)**,不再是单设备的 BleLink:
一个 bundle、每台设备一个 worker 进程、按 id/别名单点或全体广播。
看板(retained)和事件(历史环)都 fan-out 给所有设备,指令可指定 target。

    旧文件/旧常量        新归属
    ------------------  ------------------------------------------
    helper_session.py   espble.HelperSession           (已删)
    native_ble/*        espble/native/*                (已删)
    ble.py 的下半部分    espble.BleHub(内含 BleLink + RetainedChannel)
    MAX_LINE_BYTES      espble.framing.DEFAULT_LINE_LIMIT
    NUS UUID / 帧上限    espble::LinkConfig 的默认值

本文件**刻意不再出现任何协议细节**(UUID、分片长度、单行字节上限、分隔符)——
那些一旦在两处各写一份,就迟早会不一致。这里只留 m5work 自己的东西。

依赖:collector 跑的是 /usr/bin/python3(Xcode 3.9),它的 pip 太老装不了
PEP 660 editable。所以走 LaunchAgent 的 EnvironmentVariables:
    PYTHONPATH = /Users/nanzhang/esp-ble-link/host
这样和固件 lib_deps 一样直接读 git 工作副本,不会拿到快照。

编 helper(单一 bundle,四个参数都要带,漏 --session-dir 会让菜单栏看不到链路):
    espble build-helper --name EspBleHub --bundle-id com.nanzhang.espble.hub \\
        --name-prefix m5paper- --session-dir ~/.config/m5paper-monitor/ble-session \\
        --session-root ~/.config/espble/sessions \\
        --registry ~/.config/espble/devices.json --install

单独跑(联调用,不启 collector):
    python3 -m event_hub.channels.ble --test "手动测试通知"
    python3 -m event_hub.channels.ble --ota
    python3 -m event_hub.channels.ble --watch          # 只连着看设备遥测
"""
from __future__ import annotations

import os
import sys
import time

from espble import BleHub

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

DEFAULT_APP = os.path.join(_REPO_ROOT, ".build", "native", "M5PaperBLEHelper.app")
# 多设备:注册表记有哪些设备,会话根目录下每台一个子目录(<root>/<device_id>)。
# 每设备一个 session_dir 是多进程互不干扰的关键 —— helper 是按 session-dir 匹配
# 自己的进程的,共用一个目录会互相误杀(见 esp-ble-link 的 HelperSession._pids)。
DEFAULT_REGISTRY = os.path.expanduser("~/.config/espble/devices.json")
DEFAULT_SESSION_ROOT = os.path.expanduser("~/.config/espble/sessions")
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


def parse_devices(spec: str) -> list:
    """把扁平的 `"id:别名,id2:别名2"` 解析成 [(id, alias), …]。

    为什么是这种土格式而不是 TOML 数组:collector 的配置解析器 `_tiny_toml`
    **只支持 `[section]` + 标量 `key = value`** —— 没有数组、没有 `[[array of tables]]`
    (而 collector 跑在 Python 3.9 上,没有 tomllib,所以它就是生产解析器)。
    与其为了一个设备清单去动配置解析器,不如用一行字符串。

    别名可以省略(`"c119cc"`),这时用 id 当显示名。
    """
    out = []
    for item in (spec or "").split(","):
        item = item.strip()
        if not item:
            continue
        dev_id, _, alias = item.partition(":")
        dev_id = dev_id.strip()
        if dev_id:
            out.append((dev_id, alias.strip()))
    return out


class BleChannel:
    """与 MqttPublisher 同接口,collector 无需知道底层是 BLE 还是 MQTT。

    v47 起底座是 **BleHub**(多设备)而不是单设备的 BleLink:一个 bundle、
    每台设备一个 worker 进程、按 id/别名单点或全体广播。
    看板/事件这类"给所有屏看的"内容走 *_all 的 fan-out。
    """

    def __init__(self, cfg: dict):
        c = (cfg or {}).get("ble") or {}
        app = os.path.expanduser(c.get("helper_app") or DEFAULT_APP)
        self._hub = BleHub(
            app_path=app,
            device_type=c.get("device_type") or "m5paper",
            registry_path=os.path.expanduser(c.get("registry") or DEFAULT_REGISTRY),
            session_root=os.path.expanduser(c.get("session_root") or DEFAULT_SESSION_ROOT),
            history_n=int(c.get("history_n", 8)),
            reconnect_sec=float(c.get("reconnect_sec", 5.0)),
            backoff_max_sec=float(c.get("backoff_max_sec", 45.0)),
            keepalive_sec=float(c.get("keepalive_sec", 30.0)),
            scan_timeout=float(c.get("scan_timeout", 20.0)),
            on_message=self._on_telemetry,
        )
        # 先把注册表里已知的接管回来(hub 进程重启后必须做这一步,否则"记得有设备
        # 但没人在连"),再按配置补登记新的。两步都是幂等的。
        self._hub.adopt_registry()
        for dev_id, alias in parse_devices(c.get("devices") or ""):
            self._hub.register(dev_id, alias)
        if not self._hub.status():
            _log("⚠️ 一台设备都没登记 —— 在 config.toml 的 [ble] 里写 "
                 'devices = "<id>:<别名>"(id 用 `espble scan --app %s` 看广播名的后缀)' % app)

    # ---- 渠道接口(collector 调用)----

    @property
    def connected(self) -> bool:
        """至少有一台设备在线就算通。"""
        return any(d["connected"] for d in self._hub.status().values())

    def publish_state(self, snap: dict):
        """设备不消费全量快照(v23 起就废弃了),BLE 上不发 —— 白占带宽。"""
        return

    def publish_usage(self, payload: dict):
        # retained:每台设备(重)连上都补推最新看板。
        # 固件按 rev 去重,所以 keepalive 反复重发同一帧不会刷爆墨水屏。
        self._hub.set_retained_all("usage", {"t": "usage", **payload})

    def publish_event(self, ev: dict):
        # publish_all 而不是 broadcast:事件要进每台设备的历史环,
        # 这样设备离线期间错过的几条会在重连后以 live=false 补上。
        self._hub.publish_all(ev_envelope(ev, live=True))

    def send_cmd(self, cmd: str, target: str = ""):
        """如 ota:设备收到会临时切 WiFi 走 HTTP OTA(thumbserver),不依赖 broker。

        target 给了就只发那一台(id 或别名),不给就广播给所有设备。
        指令"值得等",所以断连时也排队。
        """
        obj = {"t": "cmd", "cmd": cmd}
        if target:
            if not self._hub.send(target, obj, queue_offline=True):
                _log(f"send_cmd: 找不到设备 {target!r}")
        else:
            self._hub.broadcast(obj, queue_offline=True)

    def close(self):
        self._hub.close()

    # ---- 内部 ----

    def _on_telemetry(self, record, line: str):
        """设备 notify 上来的电量遥测。在监护线程里被调用,别做重活。

        record 是 espble 的 DeviceRecord —— 多设备下必须把是谁发的记进日志,
        否则两台设备的遥测混在一起没法读。
        """
        _log(f"telemetry[{record.label}]", line)
        try:
            with open(TELEMETRY_LOG, "a") as fh:
                fh.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} "
                         f"{record.device_id} {line}\n")
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
    # 多设备下"连上了"没有单一答案,所以轮询 hub 的在线状态而不是等某条链路。
    deadline = time.time() + 40
    while time.time() < deadline and not ch.connected:
        time.sleep(0.5)
    if not ch.connected:
        _log("没连上。当前登记:", ch._hub.status() or "(一台都没登记)")
        ch.close()
        return 1
    _log("在线:", [k for k, v in ch._hub.status().items() if v["connected"]])

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
