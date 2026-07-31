"""迁移到 esp-ble-link 之后,BleChannel 对外的语义有没有变。

v40 把链路层换成了 espble 包,这份测试盯的是**没搬走的那部分**:
事件信封、retained/history 各自装什么、断连时哪些丢哪些留。
链路本身(邮箱、退避、keepalive)由 espble 自己的单测覆盖,这里不重复。

跑:  PYTHONPATH=~/esp-ble-link/host:. python3 -m pytest tests -q
"""
import json

import pytest

import event_hub.channels.ble as blemod
from event_hub.channels.ble import BleChannel, ev_envelope


class FakeLink:
    """替掉 espble.BleLink,只记录发了什么。"""

    def __init__(self, device, **kw):
        self.device = device
        self.kw = kw
        self.connected = True
        self.queued = []       # send_soon
        self.blocking = []     # send_blocking(补推走这条)
        self.started = False
        self.closed = False
        self.on_connect = None
        self.keepalive_provider = None
        self.fatal_error = ""

    def start(self):
        self.started = True

    def send_soon(self, line, *, queue_while_offline=False):
        if not queue_while_offline and not self.connected:
            return False
        self.queued.append(line)
        return True

    def send_blocking(self, line):
        self.blocking.append(line)
        return self.connected

    def clear_outbox(self):
        self.queued.clear()

    def wait_connected(self, timeout=40.0):
        return self.connected

    def close(self):
        self.closed = True


@pytest.fixture
def ch(monkeypatch):
    monkeypatch.setattr(blemod, "BleLink", FakeLink)
    return BleChannel({})


def sent(ch):
    return [json.loads(x) for x in ch._link.queued]


# ---- 装配 ----

def test_link_is_started_after_callbacks_are_wired(ch):
    # 顺序要紧:先接 on_connect 再 start,否则首次连接会漏掉补推
    assert ch._link.on_connect is not None
    assert ch._link.keepalive_provider is not None
    assert ch._link.started


def test_device_matched_by_type_prefix(ch):
    # v41:身份由固件从 efuse MAC 派生,广播名 m5paper-<id>,所以按类型前缀匹配。
    # 桌上还有 Codex-*/Claude-* 两个 NUS 设备,前缀足以把它们排除掉。
    assert ch._link.device.name_prefix == "m5paper-"


def test_no_protocol_details_are_restated_here(ch):
    """m5work 不该自己复述协议细节 —— UUID、帧上限、分隔符兼容一律用框架默认值。

    在两处各写一份的下场:改了一边忘了另一边。而且 config.h 里那个
    `#define NUS_RX` 曾经和框架的同名声明撞车(宏不认 namespace)。
    """
    d = ch._link.device
    assert d.service_uuid is None and d.rx_uuid is None and d.tx_uuid is None
    # FW40 的 notify 会自己补分隔符,不需要「猜边界」的兼容模式
    assert d.accept_unterminated is False


# ---- 渠道语义 ----

def test_publish_state_is_a_noop(ch):
    ch.publish_state({"anything": 1})
    assert ch._link.queued == []      # 设备 v23 起就不消费全量快照了


def test_publish_usage_goes_out_and_is_retained(ch):
    ch.publish_usage({"rev": 7, "rows": []})
    assert sent(ch)[0] == {"t": "usage", "rev": 7, "rows": []}
    # 同时进 retained:重连补推 + 当 keepalive 帧
    assert json.loads(ch._channel._keepalive_line())["rev"] == 7


def test_publish_event_wraps_in_envelope_with_live_true(ch):
    ch.publish_event({"kind": "done", "src": "codex", "project": "p",
                      "msg": "构建完成", "meta": "3m", "ts": 111})
    ev = sent(ch)[0]
    assert ev["t"] == "ev" and ev["live"] is True
    assert ev["msg"] == "构建完成" and ev["project"] == "p" and ev["ts"] == 111


def test_reconnect_replays_usage_then_history_with_live_false(ch):
    ch.publish_usage({"rev": 3})
    ch.publish_event({"kind": "done", "project": "a", "msg": "一", "ts": 1})
    ch.publish_event({"kind": "done", "project": "b", "msg": "二", "ts": 2})

    ch._link.blocking.clear()
    ch._link.on_connect(ch._link)          # 模拟(重)连上
    replayed = [json.loads(x) for x in ch._link.blocking]

    assert replayed[0]["t"] == "usage"     # 先状态后列表,否则会闪一下空看板
    assert [x["msg"] for x in replayed[1:]] == ["一", "二"]
    # live=false → 设备只把它放进历史列表,不再蜂鸣、不再弹全屏卡
    assert all(x["live"] is False for x in replayed[1:])


def test_history_is_capped_at_history_n(ch):
    for i in range(12):
        ch.publish_event({"kind": "done", "project": "p", "msg": str(i), "ts": i})
    ch._link.blocking.clear()
    ch._link.on_connect(ch._link)
    msgs = [json.loads(x)["msg"] for x in ch._link.blocking]
    assert msgs == [str(i) for i in range(4, 12)]      # 默认 history_n=8


def test_events_are_dropped_while_offline_but_still_replayable(ch):
    ch._link.connected = False
    ch.publish_event({"kind": "done", "project": "p", "msg": "离线时来的", "ts": 9})
    assert ch._link.queued == []           # 不排队:攒着会在重连时一次灌爆设备
    ch._link.connected = True
    ch._link.on_connect(ch._link)
    assert json.loads(ch._link.blocking[0])["msg"] == "离线时来的"   # 但历史补得回来


def test_ota_command_queues_even_while_offline(ch):
    ch._link.connected = False
    ch.send_cmd("ota")
    assert sent(ch) == [{"t": "cmd", "cmd": "ota"}]     # 指令值得等


def test_long_chinese_message_is_trimmed_to_device_ring_budget(ch):
    from espble import DEFAULT_LINE_LIMIT
    ch.publish_event({"kind": "done", "project": "p", "msg": "中" * 1200, "ts": 1})
    line = ch._link.queued[0]
    # 上限由框架按设备环形缓冲算出(limit_for_ring),这里不重复那个数字
    assert len(line.encode("utf-8")) <= DEFAULT_LINE_LIMIT
    # 必须切在字符边界上,否则设备侧 UTF-8 解码得到乱码
    assert json.loads(line)["msg"].endswith("…")


def test_close_shuts_the_link_down(ch):
    ch.close()
    assert ch._link.closed
