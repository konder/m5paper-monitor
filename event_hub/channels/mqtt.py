"""MQTT 广播渠道:把状态快照 / 事件 / 指令发到 broker。

这是事件中心的一个「渠道」。渠道 = channels/ 下的模块,当前有 mqtt(设备走 WiFi+MQTT),
后续可加 ble(蓝牙,见 channels/ble.py)、webhook 等,collector 只依赖渠道暴露的
publish_state / publish_event 接口。

- 状态(state_topic):QoS0 + retained —— 离线不排队,靠 retained 给最新一帧,
  避免设备睡醒后堆一堆过期状态。
- 事件(event_topic):QoS1 —— 至少送达一次,设备离线时 broker 排队、重连补发。
- 消耗看板(usage_topic):QoS0 + retained —— 同 state 的理由,看板只关心「最新一帧」,
  设备重连时靠 retained 立刻拿到当前值,不需要补历史。
"""
from __future__ import annotations

import json


class MqttPublisher:
    def __init__(self, cfg: dict):
        import paho.mqtt.client as mqtt
        self.cfg = cfg["mqtt"]
        self.client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
        if self.cfg.get("username"):
            self.client.username_pw_set(self.cfg["username"], self.cfg.get("password") or None)
        self.client.connect(self.cfg["host"], int(self.cfg["port"]), keepalive=60)
        self.client.loop_start()

    def publish_state(self, snap: dict):
        # 状态用 QoS0 + retained:离线不排队(靠 retained 给最新),避免睡醒堆一堆旧状态
        self.client.publish(self.cfg["state_topic"], json.dumps(snap, ensure_ascii=False),
                            qos=0, retain=True)

    def publish_event(self, ev: dict):
        self.client.publish(self.cfg["event_topic"], json.dumps(ev, ensure_ascii=False), qos=1)

    def publish_usage(self, payload: dict):
        # 看板同 state:QoS0 + retained。节流由 collector 决定发不发,这里只管发出去
        self.client.publish(self.cfg["usage_topic"], json.dumps(payload, ensure_ascii=False),
                            qos=0, retain=True)

    def close(self):
        self.client.loop_stop()
        self.client.disconnect()
