#!/usr/bin/env python3
"""BLE 渠道 —— Mac Mini 当中枢,经 esp-ble-link 的 hubd 直连设备。**不需要 MQTT broker。**

    collector ──▶ BleChannel ──管道(NDJSON)──▶ espble hubd ──┬─▶ worker ──NUS──▶ 设备 A
                                                              └─▶ worker ──NUS──▶ 设备 B

v48 起这里和框架之间是**进程边界,不是 import**。本文件零 `import espble`。

============================================================================
为什么从 import 换成子进程
============================================================================
`from espble import BleHub` 要求 collector 和框架跑在同一个解释器里。而 collector
跑的是 `/usr/bin/python3`(Xcode 3.9)—— 它的 pip 老到装不了 PEP 660 editable,
于是生产上只能靠 LaunchAgent 里的一句 `PYTHONPATH=…/esp-ble-link/host` 续命。
那个补丁不进版本控制、只活在 Mac Mini 的一个 plist 里,换机器就是一次盲踩。

换成子进程之后,m5work 只需要知道**一个命令路径**(`hub_cmd`),
而且框架崩了不会把看板一起带走。

    旧归属                     新归属
    ------------------------  ------------------------------------------
    espble.BleHub(import)     espble hubd(子进程,NDJSON over stdin/stdout)
    MAX_LINE_BYTES / NUS UUID 框架内部,这里连知道都不需要知道
    retained / 历史环 / 重连   框架内部(所以 hubd 重启会丢 retained,见 _spawn)

本文件**刻意不出现任何协议细节**(UUID、分片长度、单行字节上限、分隔符)——
那些一旦在两处各写一份,就迟早会不一致。这里只留 m5work 自己的东西:
事件信封、遥测落盘、子进程监护。

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

import json
import os
import shlex
import subprocess
import sys
import threading
import time

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

DEFAULT_APP = os.path.join(_REPO_ROOT, ".build", "native", "M5PaperBLEHelper.app")
# 框架现在只是"一个命令"。指向 git 工作副本里的 shim:它自带 sys.path 处理,
# 不需要 pip —— 这正是这次解耦要解决的问题。
DEFAULT_HUB_CMD = "~/esp-ble-link/host/bin/espble hubd"
# 多设备:注册表记有哪些设备,会话根目录下每台一个子目录(<root>/<device_id>)。
# 每设备一个 session_dir 是多进程互不干扰的关键 —— helper 是按 session-dir 匹配
# 自己的进程的,共用一个目录会互相误杀(见 esp-ble-link 的 HelperSession._pids)。
DEFAULT_REGISTRY = os.path.expanduser("~/.config/espble/devices.json")
DEFAULT_SESSION_ROOT = os.path.expanduser("~/.config/espble/sessions")
TELEMETRY_LOG = os.environ.get("M5_TELEMETRY_LOG", "/tmp/m5_telemetry.log")

RESTART_BACKOFF = (2.0, 5.0, 10.0, 30.0)      # hubd 反复起不来时别把日志刷爆


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


def build_argv(c: dict) -> list:
    """从 [ble] 配置拼出 hubd 的命令行。

    设备清单走命令行参数而不是启动后再发指令:这样 **hubd 每次重启都自带登记**,
    监护逻辑不需要记住"我登记过什么"再补一遍。
    """
    argv = shlex.split(os.path.expanduser(c.get("hub_cmd") or DEFAULT_HUB_CMD))
    argv += [
        "--app", os.path.expanduser(c.get("helper_app") or DEFAULT_APP),
        "--device-type", str(c.get("device_type") or "m5paper"),
        "--registry", os.path.expanduser(c.get("registry") or DEFAULT_REGISTRY),
        "--session-root", os.path.expanduser(c.get("session_root") or DEFAULT_SESSION_ROOT),
        "--history-n", str(int(c.get("history_n", 8))),
        "--keepalive-sec", str(float(c.get("keepalive_sec", 30.0))),
        "--reconnect-sec", str(float(c.get("reconnect_sec", 5.0))),
        "--backoff-max-sec", str(float(c.get("backoff_max_sec", 45.0))),
        "--scan-timeout", str(float(c.get("scan_timeout", 20.0))),
    ]
    for dev_id, alias in parse_devices(c.get("devices") or ""):
        argv += ["--device", f"{dev_id}:{alias}" if alias else dev_id]
    return argv


class BleChannel:
    """与 MqttPublisher 同接口,collector 无需知道底层是 BLE 还是 MQTT。

    v48 起底座是 **`espble hubd` 子进程**:这里只做进程监护 + 协议编解码,
    路由、retained、历史环、重连全在框架那一侧。
    """

    def __init__(self, cfg: dict):
        c = (cfg or {}).get("ble") or {}
        self._argv = build_argv(c)
        if not os.path.exists(self._argv[0]):
            # 早失败、把修法写清楚。否则表现成"渠道起来了但什么都不动",
            # 而那个现象和管道没 flush、设备没广播长得一模一样,很难分辨。
            raise RuntimeError(
                f"找不到 hubd 命令:{self._argv[0]}\n"
                f"  在 config.toml 的 [ble] 里把 hub_cmd 指到框架仓库的 shim,例如\n"
                f'    hub_cmd = "/Users/你/esp-ble-link/host/bin/espble hubd"')

        self._devices: dict = {}            # device_id -> 状态快照(hubd 主动推)
        self._proc = None
        self._write_lock = threading.Lock()
        self._stopping = threading.Event()
        self._ready = threading.Event()

        self._worker = threading.Thread(target=self._supervise, name="hubd-supervisor",
                                        daemon=True)
        self._worker.start()
        # 等一下首帧 ready,好让 collector 启动日志里的"已连"是有意义的。
        # 等不到也照常往下走 —— 监护线程会一直重试,没必要卡住整个 collector。
        if not self._ready.wait(10.0):
            _log("⚠️ hubd 10 秒内没报 ready,先继续跑,监护线程会重试")

    # ---- 渠道接口(collector 调用)----

    @property
    def connected(self) -> bool:
        """至少有一台设备在线就算通。

        ⚠️ **必须读本地缓存,不能往管道里问一句再等回答。**
        MultiChannel.describe() 会读这个属性,而 describe 在 collector 主循环里 ——
        一个卡住的 hubd 就能把整个看板挂死。所以状态由 hubd 主动推(status 事件),
        这边永远只读内存。
        """
        return any(d.get("connected") for d in self._devices.values())

    def publish_state(self, snap: dict):
        """设备不消费全量快照(v23 起就废弃了),BLE 上不发 —— 白占带宽。"""
        return

    def publish_usage(self, payload: dict):
        # retained:每台设备(重)连上都补推最新看板。
        # 固件按 rev 去重,所以 keepalive 反复重发同一帧不会刷爆墨水屏。
        self._send({"op": "set_retained_all", "key": "usage",
                    "obj": {"t": "usage", **payload}})

    def publish_event(self, ev: dict):
        # publish_all 而不是 broadcast:事件要进每台设备的历史环,
        # 这样设备离线期间错过的几条会在重连后以 live=false 补上。
        self._send({"op": "publish_all", "obj": ev_envelope(ev, live=True)})

    def send_cmd(self, cmd: str, target: str = ""):
        """如 ota:设备收到会临时切 WiFi 走 HTTP OTA(thumbserver),不依赖 broker。

        target 给了就只发那一台(id 或别名),不给就广播给所有设备。
        指令"值得等",所以断连时也排队。
        """
        obj = {"t": "cmd", "cmd": cmd}
        if target:
            self._send({"op": "send", "target": target, "obj": obj, "queue_offline": True})
        else:
            self._send({"op": "broadcast", "obj": obj, "queue_offline": True})

    def close(self):
        self._stopping.set()
        proc = self._proc
        if proc is not None:
            # 关 stdin = 给 hubd 一个 EOF,它会自己把 helper 进程带走再退出。
            # 直接 kill 的话那些 helper 会变孤儿(虽然下次 start 能按 session-dir 收掉)。
            try:
                with self._write_lock:
                    if proc.stdin and not proc.stdin.closed:
                        proc.stdin.close()
            except OSError:
                pass
            try:
                proc.wait(timeout=8)
            except subprocess.TimeoutExpired:
                _log("hubd 没在 8 秒内退出,强制结束")
                proc.kill()
        if self._worker.is_alive():
            self._worker.join(timeout=3)

    # ---- 子进程监护 ----

    def _supervise(self):
        """起 hubd → 读它的事件直到 EOF → 退避重启。一个线程干完这三件事。

        照搬框架里 BleLink 管 Swift helper 的路子:**进程级恢复**。
        把"读"和"监护"放同一个线程是为了避免"读线程还在、进程已经没了"这种半死状态。
        """
        attempt = 0
        while not self._stopping.is_set():
            proc = self._spawn()
            if proc is not None:
                self._pump(proc)                    # 阻塞到 hubd 的 stdout EOF
                if self._stopping.is_set():
                    return
                rc = proc.poll()
                _log(f"hubd 退出(rc={rc})")
            attempt += 1
            self._devices = {}                      # 进程没了,"谁在线"这个答案也就作废了
            delay = RESTART_BACKOFF[min(attempt - 1, len(RESTART_BACKOFF) - 1)]
            _log(f"{delay:.0f}s 后重启 hubd(第 {attempt} 次)")
            if self._stopping.wait(delay):
                return

    def _spawn(self):
        """拉起 hubd。

        stderr **刻意不捕获** —— 让它继承下去和 collector 的日志混在同一个文件里,
        这样 `tail /tmp/m5monitor.err.log` 能看到一条连着的时间线(排查手感不变)。

        ⚠️ retained 状态活在 hubd 进程里,所以重启会丢。不补救是有意的:
        collector 每轮都会 publish_usage,下一轮就自然补上了,
        而在这里缓一份就等于把"最新看板是什么"这件事在两个进程里各存一遍。
        """
        try:
            proc = subprocess.Popen(
                self._argv,
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=None,
                text=True, encoding="utf-8", bufsize=1,      # 行缓冲(写侧)
            )
        except OSError as exc:
            _log(f"起不来 hubd: {exc}")
            return None
        self._proc = proc
        _log("hubd 已启动 pid=%d" % proc.pid)
        return proc

    def _pump(self, proc):
        """把 hubd 的 stdout 一行行读成事件,直到它退出。"""
        for raw in proc.stdout:
            if self._stopping.is_set():
                return
            raw = raw.strip()
            if not raw:
                continue
            try:
                ev = json.loads(raw)
            except ValueError:
                # 不是我们的协议 —— 多半是框架往 stdout 漏了一行日志。
                # 丢掉但要看得见,否则协议对不上时会静默失联。
                _log("hubd 吐了一行非 JSON:", raw[:200])
                continue
            try:
                self._on_event(ev)
            except Exception as exc:            # noqa: BLE001 —— 一条坏事件不该断掉整条流
                _log(f"处理事件出错 {type(exc).__name__}: {exc}")

    def _on_event(self, ev: dict):
        kind = ev.get("event")
        if kind in ("ready", "status"):
            self._devices = ev.get("devices") or {}
            if kind == "ready":
                self._ready.set()
                _log("hubd ready,设备:", ", ".join(self._devices) or "(一台都没登记)")
        elif kind == "message":
            self._on_telemetry(ev.get("label") or ev.get("device") or "?",
                               ev.get("device") or "?", ev.get("line") or "")
        elif kind == "device":
            _log(f"{ev.get('label')} {ev.get('what')}"
                 f"({'在线' if ev.get('connected') else '离线'})")
        elif kind == "error":
            _log("hubd 报错:", ev.get("message"))

    def _send(self, cmd: dict):
        proc = self._proc
        if proc is None or proc.stdin is None:
            return
        line = json.dumps(cmd, ensure_ascii=False)
        with self._write_lock:
            try:
                proc.stdin.write(line + "\n")
                proc.stdin.flush()
            except (OSError, ValueError):
                # 管道断了。丢掉这条,交给监护线程重启 —— 在这里排队没有意义:
                # 真正该补的是 retained,而它由 collector 下一轮自然重发。
                pass

    def _on_telemetry(self, label: str, device_id: str, line: str):
        """设备 notify 上来的电量遥测。在读线程里被调用,别做重活。

        多设备下必须把是谁发的记进日志,否则两台设备的遥测混在一起没法读。
        """
        _log(f"telemetry[{label}]", line)
        try:
            with open(TELEMETRY_LOG, "a") as fh:
                fh.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {device_id} {line}\n")
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
    # 多设备下"连上了"没有单一答案,所以轮询在线状态而不是等某条链路。
    deadline = time.time() + 40
    while time.time() < deadline and not ch.connected:
        time.sleep(0.5)
    if not ch.connected:
        _log("没连上。当前登记:", ch._devices or "(一台都没登记)")
        ch.close()
        return 1
    _log("在线:", [k for k, v in ch._devices.items() if v.get("connected")])

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
