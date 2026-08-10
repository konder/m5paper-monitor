"""迁移到 esp-ble-link 之后,BleChannel 对外的语义有没有变。

v40 把链路层换成了 espble 包,这份测试盯的是**没搬走的那部分**:
事件信封、retained/history 各自装什么、断连时哪些丢哪些留。
链路本身(邮箱、退避、keepalive)由 espble 自己的单测覆盖,这里不重复。

跑:  PYTHONPATH=~/esp-ble-link/host:. python3 -m pytest tests -q
"""
import json

import pytest

import espble.hub as hubmod

from event_hub.channels.ble import BleChannel, ev_envelope, parse_devices


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
def ch(monkeypatch, tmp_path):
    """一台设备的 BleChannel。

    ⚠️ registry / session_root **必须指到 tmp_path**。BleChannel 现在的底座是
    BleHub,它会把注册表落盘 —— 用默认路径就会往真实的
    ~/.config/espble/devices.json 里写测试设备,污染生产环境。
    """
    monkeypatch.setattr(hubmod, "BleLink", FakeLink)   # BleHub 内部实例化的就是这个
    return BleChannel({"ble": {
        "devices": "aaa111:测试屏",
        "registry": str(tmp_path / "devices.json"),
        "session_root": str(tmp_path / "sessions"),
    }})


def dl(ch):
    """唯一那台设备的 DeviceLink(多设备下每台自带 link + channel)。"""
    links = list(ch._hub._links.values())
    assert len(links) == 1, f"这个夹具只登记一台设备,实际 {len(links)} 台"
    return links[0]


def lk(ch):
    return dl(ch).link


def sent(ch):
    return [json.loads(x) for x in lk(ch).queued]


# ---- 装配 ----

def test_link_is_started_after_callbacks_are_wired(ch):
    # 顺序要紧:先接 on_connect 再 start,否则首次连接会漏掉补推
    assert lk(ch).on_connect is not None
    assert lk(ch).keepalive_provider is not None
    assert lk(ch).started


def test_each_device_is_matched_by_exact_name_not_prefix(ch):
    """v47:多设备下每个 worker 必须按**精确广播名**匹配,不能按前缀。

    v40 单设备时用的是 `name_prefix="m5paper-"`。多设备下那样做是错的:
    N 个 worker 全都拿同一个前缀,谁先扫到哪台就抢哪台 —— 别名和 id 的绑定就废了,
    发给"甲屏"的消息可能进了乙屏。所以 BleHub 用 `<type>-<id>` 精确匹配。
    """
    d = lk(ch).device
    assert d.device_name == "m5paper-aaa111"
    assert d.name_prefix is None


def test_no_protocol_details_are_restated_here(ch):
    """m5work 不该自己复述协议细节 —— UUID、帧上限、分隔符兼容一律用框架默认值。

    在两处各写一份的下场:改了一边忘了另一边。而且 config.h 里那个
    `#define NUS_RX` 曾经和框架的同名声明撞车(宏不认 namespace)。
    """
    d = lk(ch).device
    assert d.service_uuid is None and d.rx_uuid is None and d.tx_uuid is None
    # FW40 的 notify 会自己补分隔符,不需要「猜边界」的兼容模式
    assert d.accept_unterminated is False


# ---- 渠道语义 ----

def test_publish_state_is_a_noop(ch):
    ch.publish_state({"anything": 1})
    assert lk(ch).queued == []      # 设备 v23 起就不消费全量快照了


def test_publish_usage_goes_out_and_is_retained(ch):
    ch.publish_usage({"rev": 7, "rows": []})
    assert sent(ch)[0] == {"t": "usage", "rev": 7, "rows": []}
    # 同时进 retained:重连补推 + 当 keepalive 帧
    assert json.loads(dl(ch).channel._keepalive_line())["rev"] == 7


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

    lk(ch).blocking.clear()
    lk(ch).on_connect(lk(ch))          # 模拟(重)连上
    replayed = [json.loads(x) for x in lk(ch).blocking]

    assert replayed[0]["t"] == "usage"     # 先状态后列表,否则会闪一下空看板
    assert [x["msg"] for x in replayed[1:]] == ["一", "二"]
    # live=false → 设备只把它放进历史列表,不再蜂鸣、不再弹全屏卡
    assert all(x["live"] is False for x in replayed[1:])


def test_history_is_capped_at_history_n(ch):
    for i in range(12):
        ch.publish_event({"kind": "done", "project": "p", "msg": str(i), "ts": i})
    lk(ch).blocking.clear()
    lk(ch).on_connect(lk(ch))
    msgs = [json.loads(x)["msg"] for x in lk(ch).blocking]
    assert msgs == [str(i) for i in range(4, 12)]      # 默认 history_n=8


def test_events_are_dropped_while_offline_but_still_replayable(ch):
    lk(ch).connected = False
    ch.publish_event({"kind": "done", "project": "p", "msg": "离线时来的", "ts": 9})
    assert lk(ch).queued == []           # 不排队:攒着会在重连时一次灌爆设备
    lk(ch).connected = True
    lk(ch).on_connect(lk(ch))
    assert json.loads(lk(ch).blocking[0])["msg"] == "离线时来的"   # 但历史补得回来


def test_ota_command_queues_even_while_offline(ch):
    lk(ch).connected = False
    ch.send_cmd("ota")
    assert sent(ch) == [{"t": "cmd", "cmd": "ota"}]     # 指令值得等


def test_long_chinese_message_is_trimmed_to_device_ring_budget(ch):
    from espble import DEFAULT_LINE_LIMIT
    ch.publish_event({"kind": "done", "project": "p", "msg": "中" * 1200, "ts": 1})
    line = lk(ch).queued[0]
    # 上限由框架按设备环形缓冲算出(limit_for_ring),这里不重复那个数字
    assert len(line.encode("utf-8")) <= DEFAULT_LINE_LIMIT
    # 必须切在字符边界上,否则设备侧 UTF-8 解码得到乱码
    assert json.loads(line)["msg"].endswith("…")


def test_close_shuts_the_link_down(ch):
    link = lk(ch)          # 先拿到 —— close() 会把 hub 的 _links 清空
    ch.close()
    assert link.closed


# ---- 多设备(v47:底座换成 BleHub)----

@pytest.mark.parametrize("spec,want", [
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


@pytest.fixture
def two(monkeypatch, tmp_path):
    monkeypatch.setattr(hubmod, "BleLink", FakeLink)
    return BleChannel({"ble": {
        "devices": "aaa111:甲屏,bbb222:乙屏",
        "registry": str(tmp_path / "devices.json"),
        "session_root": str(tmp_path / "sessions"),
    }})


def test_two_devices_get_separate_links_and_session_dirs(two):
    links = list(two._hub._links.values())
    assert len(links) == 2
    dirs = {l.link.device.session_dir for l in links}
    assert len(dirs) == 2, "session_dir 必须按设备分,否则 helper 会互相误杀"


def test_usage_and_events_fan_out_to_every_device(two):
    two.publish_usage({"rev": 7})
    two.publish_event({"kind": "done", "project": "p", "msg": "好了", "ts": 1})
    for d in two._hub._links.values():
        lines = [json.loads(x) for x in d.link.queued]
        assert {"t": "usage", "rev": 7} in lines
        assert any(x.get("msg") == "好了" for x in lines)


def test_cmd_can_target_one_device_by_alias(two):
    two.send_cmd("ota", target="乙屏")
    got = {i: [json.loads(x) for x in d.link.queued]
           for i, d in two._hub._links.items()}
    assert got["bbb222"] == [{"t": "cmd", "cmd": "ota"}]
    assert got["aaa111"] == [], "单点不该发给别人"


def test_cmd_without_target_broadcasts(two):
    two.send_cmd("ota")
    for d in two._hub._links.values():
        assert [json.loads(x) for x in d.link.queued] == [{"t": "cmd", "cmd": "ota"}]


def test_connected_is_true_when_any_device_is_up(two):
    a, b = two._hub._links.values()
    a.link.connected = b.link.connected = False
    assert two.connected is False
    b.link.connected = True
    assert two.connected is True          # 有一台在线就算通


def test_registry_survives_restart_and_is_adopted(monkeypatch, tmp_path):
    """collector 重启后必须把设备接管回来,而不是"记得有设备但没人连"。"""
    monkeypatch.setattr(hubmod, "BleLink", FakeLink)
    cfg = {"ble": {"devices": "aaa111:甲屏",
                   "registry": str(tmp_path / "devices.json"),
                   "session_root": str(tmp_path / "sessions")}}
    first = BleChannel(cfg)
    first.close()

    # 第二次**不给** devices,全靠注册表 —— adopt_registry 该把它捡回来
    cfg2 = dict(cfg)
    cfg2["ble"] = dict(cfg["ble"], devices="")
    second = BleChannel(cfg2)
    assert list(second._hub.status()) == ["aaa111"]
    assert second._hub.registry.get("甲屏").device_id == "aaa111"
    second.close()
