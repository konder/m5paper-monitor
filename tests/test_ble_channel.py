"""BLE 渠道:配置怎么变成命令行、方法调用怎么变成管道上的 JSON、事件回来怎么落地。

v48 起 m5work 和 esp-ble-link 之间是**进程边界**,所以这份测试的划线也跟着变:

    在这里测        配置 → hubd 命令行、方法 → 指令 JSON、事件 JSON → 内部状态、
                   子进程监护(死了会不会重启、close 会不会留孤儿)、事件信封
    **不**在这里测   重连补推顺序、历史环上限、离线丢弃、长中文按字节截断、
                   按精确名匹配、每设备独立 session_dir
                   —— 那些是**框架行为**,进程边界之后这边既测不到也不该测。
                      对应覆盖(删之前逐条核对过):
                        test_channel.py::test_reconnect_replays_retained_then_history
                        test_channel.py::test_history_is_bounded
                        test_channel.py::test_immediate_messages_dropped_while_offline
                        test_framing.py::test_truncation_lands_on_char_boundary
                        test_hub.py::test_session_dirs_must_differ
                        test_hub.py::test_adopt_registry_rehydrates_after_restart

跑:  python3 -m pytest tests -q          # ⚠️ 不再需要 PYTHONPATH 指向框架
"""
import json
import os
import shlex
import subprocess
import sys
import time

import pytest

import event_hub.channels.ble as blemod
from event_hub.channels.ble import (BleChannel, build_argv, ev_envelope,
                                    parse_devices)

STUB = os.path.join(os.path.dirname(os.path.abspath(__file__)), "stub_hubd.py")
REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def wait_for(pred, timeout=6.0):
    end = time.time() + timeout
    while time.time() < end:
        if pred():
            return True
        time.sleep(0.02)
    return False


def cmds(path) -> list:
    """假 hubd 录下来的指令。"""
    if not os.path.exists(path):
        return []
    with open(path, encoding="utf-8") as fh:
        return [json.loads(l) for l in fh if l.strip()]


@pytest.fixture
def chan(tmp_path, monkeypatch):
    """接着假 hubd 的 BleChannel 工厂。返回 (渠道, 指令录音文件路径)。"""
    record = tmp_path / "cmds.jsonl"
    monkeypatch.setenv("STUB_HUBD_RECORD", str(record))
    # TELEMETRY_LOG 是 import 时求值的,所以要改模块属性而不是环境变量
    monkeypatch.setattr(blemod, "TELEMETRY_LOG", str(tmp_path / "telemetry.log"))
    made = []

    def make(devices="c119cc:看板", **extra):
        ch = BleChannel({"ble": dict(
            hub_cmd=f"{shlex.quote(sys.executable)} {shlex.quote(STUB)}",
            devices=devices, helper_app=str(tmp_path / "X.app"),
            registry=str(tmp_path / "devices.json"),
            session_root=str(tmp_path / "sessions"), **extra)})
        made.append(ch)
        return ch, str(record)

    yield make
    for ch in made:
        ch.close()


# ---- 解耦本身 ----

def test_the_framework_is_never_imported_into_this_process():
    """解耦的机械证据。

    ⚠️ 不能用「不带 PYTHONPATH 能不能跑」当判据 —— 开发机上 espble 是 pip 装了的
    (生产机 Mac Mini 才是没装的那台),那样测什么都能过。所以直接查 sys.modules,
    而且起一个干净解释器,免得被同一次 pytest 里别的 import 污染。
    """
    env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
    out = subprocess.run(
        [sys.executable, "-c",
         "import event_hub.channels.ble, sys;"
         "print(any(m == 'espble' or m.startswith('espble.') for m in sys.modules))"],
        cwd=REPO_ROOT, env=env, capture_output=True, text=True)
    assert out.returncode == 0, out.stderr
    assert out.stdout.strip() == "False", "espble 又被 import 进来了 —— 解耦破了"


def test_config_becomes_a_hubd_command_line():
    argv = build_argv({"hub_cmd": "/x/espble hubd", "devices": "aaa:甲,bbb",
                       "device_type": "m5paper", "helper_app": "/x/H.app",
                       "history_n": 3, "scan_timeout": 9})
    assert argv[:3] == ["/x/espble", "hubd", "--app"]
    # 设备清单走命令行而不是启动后补发指令 —— 这样 hubd 每次重启都自带登记,
    # 监护逻辑不需要记住"我登记过什么"
    assert argv[argv.index("--device") + 1] == "aaa:甲"
    assert "bbb" in argv                          # 别名可省,照样登记
    assert argv[argv.index("--history-n") + 1] == "3"
    assert argv[argv.index("--scan-timeout") + 1] == "9.0"


def test_a_missing_hub_command_fails_loudly_at_construction():
    # 静默失败会表现成"渠道起来了但什么都不动",而那和管道没 flush、设备没广播
    # 长得一模一样。宁可当场炸,并且把修法写进异常里。
    with pytest.raises(RuntimeError, match="hub_cmd"):
        BleChannel({"ble": {"hub_cmd": "/绝对不存在的路径/espble hubd"}})


# ---- 方法 → 指令 ----

def test_usage_goes_out_as_a_retained_state(chan):
    ch, record = chan()
    ch.publish_usage({"rev": 7, "pct": 60})
    assert wait_for(lambda: cmds(record))
    cmd = cmds(record)[0]
    # retained 而不是普通发送:设备每次(重)连上都要被补推最新看板
    assert cmd["op"] == "set_retained_all" and cmd["key"] == "usage"
    assert cmd["obj"] == {"t": "usage", "rev": 7, "pct": 60}


def test_events_go_out_as_publish_all_so_they_enter_each_history_ring(chan):
    ch, record = chan()
    ch.publish_event({"kind": "done", "src": "codex", "project": "p", "msg": "完事",
                      "meta": "", "ts": 3})
    assert wait_for(lambda: cmds(record))
    cmd = cmds(record)[0]
    # 刻意不是 broadcast:broadcast 只"现在发一次",publish_all 才进历史环 ——
    # 设备离线期间错过的那几条,要靠历史环在重连后补回来
    assert cmd["op"] == "publish_all"
    assert cmd["obj"]["t"] == "ev" and cmd["obj"]["live"] is True
    assert cmd["obj"]["msg"] == "完事"


def test_cmd_with_a_target_is_unicast_and_without_one_is_broadcast(chan):
    ch, record = chan(devices="aaa:甲屏,bbb:乙屏")
    ch.send_cmd("ota", target="甲屏")
    ch.send_cmd("ota")
    assert wait_for(lambda: len(cmds(record)) == 2)
    one, both = cmds(record)
    assert one["op"] == "send" and one["target"] == "甲屏"
    assert both["op"] == "broadcast"
    # 指令"值得等",所以断连时也排队(状态帧过期即无用,不排)
    assert one["queue_offline"] is True and both["queue_offline"] is True


def test_publish_state_puts_nothing_on_the_wire(chan):
    ch, record = chan()
    ch.publish_state({"anything": 1})
    time.sleep(0.3)
    assert cmds(record) == []      # 设备 v23 起就不消费全量快照了,发了纯属白占带宽


# ---- 事件 → 内部状态 ----

def test_connected_reads_the_pushed_snapshot_not_the_pipe(chan):
    ch, _ = chan()
    # hubd 启动就推 ready,所以构造完就知道谁在线。这个属性会被
    # MultiChannel.describe() 在 collector 主循环里读 —— 必须是纯内存的,
    # 否则一个卡住的 hubd 就能把看板挂死。
    assert wait_for(lambda: ch.connected)
    assert list(ch._devices) == ["c119cc"]


def test_connected_is_false_when_no_device_is_up(chan, tmp_path, monkeypatch):
    emit = tmp_path / "emit.jsonl"
    emit.write_text(json.dumps({"event": "status", "devices": {
        "c119cc": {"alias": "看板", "connected": False}}}) + "\n", encoding="utf-8")
    monkeypatch.setenv("STUB_HUBD_EMIT", str(emit))
    ch, _ = chan()
    assert wait_for(lambda: ch._devices and not ch.connected)


def test_telemetry_lands_with_the_device_id_so_two_screens_dont_mix(chan, tmp_path,
                                                                    monkeypatch):
    emit = tmp_path / "emit.jsonl"
    emit.write_text(json.dumps({"event": "message", "device": "c119cc",
                                "label": "看板", "line": '{"pct":100}'}) + "\n",
                    encoding="utf-8")
    monkeypatch.setenv("STUB_HUBD_EMIT", str(emit))
    chan()
    log = tmp_path / "telemetry.log"
    assert wait_for(lambda: log.exists() and log.read_text(encoding="utf-8").strip())
    assert "c119cc" in log.read_text(encoding="utf-8")


def test_an_unknown_event_kind_does_not_break_the_stream(chan, tmp_path, monkeypatch):
    emit = tmp_path / "emit.jsonl"
    emit.write_text("\n".join([
        json.dumps({"event": "以后新增的事件类型"}),
        json.dumps({"event": "status", "devices": {"c119cc": {"connected": True}}}),
    ]) + "\n", encoding="utf-8")
    monkeypatch.setenv("STUB_HUBD_EMIT", str(emit))
    ch, _ = chan()
    # 框架加了新事件类型,不该让老消费方就此失联
    assert wait_for(lambda: ch.connected)


# ---- 子进程监护 ----

def test_a_dead_hubd_is_restarted(chan, monkeypatch):
    monkeypatch.setattr(blemod, "RESTART_BACKOFF", (0.2,))
    monkeypatch.setenv("STUB_HUBD_DIE_AFTER", "0.3")
    ch, _ = chan()
    first = ch._proc.pid
    # 框架侧崩了不能让看板永久失联 —— 这正是进程边界换来的好处,但得真验一次
    assert wait_for(lambda: ch._proc is not None and ch._proc.pid != first, timeout=8)


def test_close_shuts_the_child_down_instead_of_orphaning_it(chan):
    ch, _ = chan()
    proc = ch._proc
    ch.close()
    # 关 stdin 给 EOF,让 hubd 自己把 helper 进程带走再退出;直接 kill 会留一批孤儿
    assert proc.poll() is not None


# ---- 纯 m5work 的东西 ----

@pytest.mark.parametrize("spec, want", [
    ("c119cc:看板", [("c119cc", "看板")]),
    ("c119cc", [("c119cc", "")]),                       # 别名可省
    ("a:甲, b:乙 ,c", [("a", "甲"), ("b", "乙"), ("c", "")]),
    ("", []),
    (None, []),
    (" , ,", []),                                        # 空项全丢掉
])
def test_parse_devices(spec, want):
    """扁平字符串格式 —— 因为 collector 的 _tiny_toml 不支持数组。"""
    assert parse_devices(spec) == want


def test_ev_envelope_matches_the_firmware_fields():
    # 字段名必须和固件 handleBleMessage 对得上,少一个设备那格就是空白
    assert ev_envelope({"kind": "done", "src": "codex", "project": "p",
                        "msg": "m", "meta": "x", "ts": 9}, live=False) == {
        "t": "ev", "live": False, "kind": "done", "src": "codex",
        "project": "p", "msg": "m", "meta": "x", "ts": 9}
