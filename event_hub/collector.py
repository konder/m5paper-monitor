#!/usr/bin/env python3
"""事件中心 collector 主进程:采集 → 快照 → 广播渠道 + 事件/额度告警。

用法(从仓库根运行):
  python -m event_hub.collector --once --print   # 打印一次快照 JSON,不连渠道(本地调试)
  python -m event_hub.collector --once           # 采集一次并 publish,退出
  python -m event_hub.collector                  # 常驻:定时刷新 + retained 发布 + 事件推送

渠道(channels/,由 [channels] 选,可同时开):
  ble  —— Mac Mini 经原生 Swift helper 直连设备,**不需要 mosquitto**(channels/ble.py)
  mqtt —— 经 broker,设备走 WiFi 兜底时用(channels/mqtt.py)
collector 只依赖渠道的 publish_state / publish_event / publish_usage 接口。
依赖:paho-mqtt(仅 mqtt 渠道需要);采集/额度/BLE 全 stdlib。
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _REPO_ROOT)
from collectors.snapshot import build_snapshot  # noqa: E402
from event_hub.channels.mqtt import MqttPublisher  # noqa: E402
from event_hub.channels.fanout import MultiChannel  # noqa: E402
from event_hub import usage as usage_mod  # noqa: E402
from event_hub.poller import Source, SourcePoller  # noqa: E402

# 真实运行配置放 config/config.toml(git 忽略);仓库里只带 config.example.toml
CONFIG_PATH = os.path.join(_REPO_ROOT, "config", "config.toml")

DEFAULTS = {
    "mqtt": {
        "host": "127.0.0.1", "port": 1883, "username": "", "password": "",
        "state_topic": "m5paper/state", "event_topic": "m5paper/events",
        "usage_topic": "m5paper/usage",
    },
    "collector": {"refresh_sec": 20, "with_quota": True},
    "thumb": {"port": 8080},
    "event": {"port": 8898, "quota_alert_pct": 85},
    # 消耗看板的两个远程源。api_key 空 = 关掉该源(看板对应行显示「旧」)
    "litellm": {"base_url": "", "api_key": "", "ttl_sec": 60},
    "gwtraffic": {"host": "", "control_path": "~/.ssh/cm-gwtraffic.sock",
                  "identity": "~/.ssh/id_ed25519", "ttl_sec": 30},
    "usage": {"enabled": True, "min_republish_sec": 300, "tz_offset_hours": 8},
    # 渠道选择。ble=true 走原生 Swift helper 直连设备,**不需要 mosquitto**。
    # FW46 起设备是 BLE-only 的(WiFi 只在 OTA/dump 时临时借用),mqtt 渠道对设备
    # 已经没有用了,所以默认改成只开 ble。
    "channels": {"mqtt": False, "ble": True},
    # ⚠️ device_name 必须留空。它在 helper 的匹配里**优先级高于 name_prefix**,
    #    而这里以前写死 "M5PaperNotify" —— 那是 FW41 之前的名字。FW41 起身份由固件
    #    从 efuse MAC 派生成 `m5paper-<后3字节>`,于是这个陈旧默认值让 BLE 渠道一开
    #    就报 "timed out scanning for M5PaperNotify",而设备明明在广播、就在扫描结果里。
    "ble": {"device_name": "", "name_prefix": "m5paper-", "helper_app": "", "session_dir": "",
            "reconnect_sec": 5, "keepalive_sec": 30, "history_n": 8, "scan_timeout": 20},
}

# 值得蜂鸣提醒的目标状态
ALERT_STATES = {"done", "needs_input"}


def _tiny_toml(path: str) -> dict:
    """极简 toml 解析(仅支持 [section] + key = value),供无 tomllib 的 py<3.11 回退。"""
    out: dict = {}
    section = None
    with open(path, "r", encoding="utf-8") as fh:
        for raw in fh:
            line = raw.split("#", 1)[0].strip()
            if not line:
                continue
            if line.startswith("[") and line.endswith("]"):
                section = line[1:-1].strip()
                out[section] = {}
                continue
            if "=" not in line or section is None:
                continue
            k, v = (x.strip() for x in line.split("=", 1))
            if v and v[0] in "\"'" and v[-1] == v[0]:
                val = v[1:-1]
            elif v.lower() in ("true", "false"):
                val = v.lower() == "true"
            else:
                try:
                    val = int(v)
                except ValueError:
                    try:
                        val = float(v)
                    except ValueError:
                        val = v
            out[section][k] = val
    return out


def load_config() -> dict:
    cfg = {k: dict(v) for k, v in DEFAULTS.items()}
    try:
        try:
            import tomllib
            with open(CONFIG_PATH, "rb") as fh:
                user = tomllib.load(fh)
        except ModuleNotFoundError:
            user = _tiny_toml(CONFIG_PATH)
        for section, vals in user.items():
            cfg.setdefault(section, {}).update(vals)
    except FileNotFoundError:
        pass
    except Exception as e:
        print(f"[warn] 读取 config.toml 失败,用默认值: {e}", file=sys.stderr)
    return cfg


def make_channel(cfg: dict) -> MultiChannel:
    """按 [channels] 组装广播渠道。

    每个渠道单独 try —— 以前 MqttPublisher 的构造没有保护,broker 一挂整个 collector
    就被拖死(靠 launchd KeepAlive 反复重启)。现在 broker 挂了只是少一条链路,
    BLE 那条还能把看板推到设备上。
    """
    sel = cfg.get("channels") or {}
    chans = []
    if sel.get("ble"):
        try:
            from event_hub.channels.ble import BleChannel
            chans.append(BleChannel(cfg))
            print("[chan] BLE 渠道已启动(原生 helper,无需 broker)", file=sys.stderr)
        except Exception as e:
            print(f"[warn] BLE 渠道启动失败: {e}", file=sys.stderr)
    if sel.get("mqtt", True):
        try:
            chans.append(MqttPublisher(cfg))
            print(f"[chan] MQTT 渠道已连 {cfg['mqtt']['host']}:{cfg['mqtt']['port']}", file=sys.stderr)
        except Exception as e:
            print(f"[warn] MQTT 渠道启动失败(broker 没起?): {e}", file=sys.stderr)
    if not chans:
        print("[warn] 一个渠道都没起来 —— 只会本地计算,不会推送任何东西", file=sys.stderr)
    return MultiChannel(chans)


def make_poller(cfg: dict) -> SourcePoller:
    """按配置组装消耗看板的远程源。

    缺配置的源**不注册** —— 这样 poller.get() 返回 None、is_stale() 返回 True,
    看板对应行自然显示「旧」,不需要额外的 enabled 开关。
    """
    from collectors import gateway as gw_mod
    from collectors import litellm as ll_mod

    sources = []
    ll = cfg.get("litellm") or {}
    if ll.get("base_url") and ll.get("api_key"):
        sources.append(Source(
            "litellm",
            lambda: ll_mod.collect(ll["base_url"], ll["api_key"]),
            float(ll.get("ttl_sec", 60)),
        ))
    gw = cfg.get("gwtraffic") or {}
    if gw.get("host"):
        sources.append(Source(
            "gwtraffic",
            lambda: gw_mod.collect(gw["host"],
                                   gw.get("control_path", "~/.ssh/cm-gwtraffic.sock"),
                                   gw.get("identity", "~/.ssh/id_ed25519")),
            float(gw.get("ttl_sec", 30)),
        ))
    return SourcePoller(sources)


def _session_key(s: dict) -> str:
    return f"{s.get('src')}:{s.get('project')}:{s.get('task')}"


def diff_events(prev: dict, cur: dict) -> list[dict]:
    """比较前后两帧,找出新进入 done/needs_input 的会话 → 事件列表。"""
    prev_states = {_session_key(s): s.get("state") for s in (prev or {}).get("sessions", [])}
    events = []
    for s in cur.get("sessions", []):
        k = _session_key(s)
        new_state = s.get("state")
        if new_state in ALERT_STATES and prev_states.get(k) != new_state:
            events.append({
                "kind": new_state,
                "src": s.get("src"),
                "project": s.get("project"),
                "task": s.get("task"),
                "ts": cur.get("ts"),
            })
    return events


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--once", action="store_true", help="采集一次后退出")
    ap.add_argument("--print", dest="do_print", action="store_true",
                    help="打印快照到 stdout(不连 MQTT)")
    ap.add_argument("--no-quota", action="store_true", help="跳过额度查询(加速调试)")
    args = ap.parse_args()

    cfg = load_config()
    with_quota = cfg["collector"].get("with_quota", True) and not args.no_quota
    usage_on = bool(cfg["usage"].get("enabled", True))
    # Mac Mini 系统时区是 US/Pacific,用户在 CST → 顶栏时钟按此偏移(见 usage._hhmm)
    tz_off = cfg["usage"].get("tz_offset_hours", 8)

    from collectors import codex_quota

    poller = make_poller(cfg)

    # 纯打印模式:不碰 MQTT。看板也一起打,方便离线核对四路数据
    if args.do_print:
        snap = build_snapshot(with_quota=with_quota)
        out = {"state": snap}
        if usage_on:
            poller.prime()
            payload, alerts = usage_mod.build(snap, codex_quota.collect(), poller,
                                             tz_offset_hours=tz_off)
            payload["rev"] = 0
            out["usage"] = payload
            out["usage_alerts"] = alerts
            out["poller"] = poller.status()
        print(json.dumps(out, ensure_ascii=False, indent=2))
        return

    # 启动灰度缩略图 HTTP 服务(设备详情页拉图)
    try:
        from event_hub import thumbserver
        thumbserver.start(int(cfg["thumb"].get("port", 8080)))
        print(f"[thumb] serving on :{cfg['thumb'].get('port', 8080)}", file=sys.stderr)
    except Exception as e:
        print(f"[warn] thumb server 启动失败: {e}", file=sys.stderr)

    pub = make_channel(cfg)

    # 事件接收服务(hook POST 进来 → 补全 → 发 MQTT)
    try:
        from event_hub import eventserver
        eventserver.start(int(cfg["event"].get("port", 8898)), pub.publish_event)
        print(f"[event] 接收端口 :{cfg['event'].get('port', 8898)}", file=sys.stderr)
    except Exception as e:
        print(f"[warn] event server 启动失败: {e}", file=sys.stderr)

    # 远程源(litellm / 网关)只在后台线程里拉:build_snapshot() 会被 eventserver
    # 的每次 hook POST 同步调用,网络 I/O 绝不能进那条路径
    if usage_on:
        poller.prime()
        poller.start()
        print(f"[usage] poller: {poller.status()}", file=sys.stderr)

    quota_alert_pct = int(cfg["event"].get("quota_alert_pct", 85))
    min_republish = int(cfg["usage"].get("min_republish_sec", 300))
    alerted = {}  # 看板行标签 -> 该窗口的 reset 值,避免重复告警直到重置
    codex_done = {}   # key -> 最近已通知的完成时刻(codex 无 hook,靠日志检测)
    first_pass = True
    usage_rev = 0       # 只在实质变化时 +1;心跳重发不 +1,设备就不会白刷墨水屏
    usage_sig = None
    usage_pub_at = 0.0

    def _emit_done_from_session(s):
        e = s.get("elapsed_s"); t = s.get("tokens")
        meta = []
        if e and e >= 0: meta.append(f"用时 {e//60}m{e%60}s" if e >= 60 else f"用时 {e}s")
        if t and t >= 0: meta.append(f"{t/1e6:.1f}M tok" if t >= 1e6 else f"{t//1000}k tok")
        if s.get("model"): meta.append(s["model"])
        pub.publish_event({
            "kind": "done", "src": "codex", "project": s.get("project", "?"),
            "msg": (s.get("last_msg") or "任务完成")[:600], "meta": " · ".join(meta),
            "ts": s.get("done_ts") or int(time.time()),
        })

    prev = None
    try:
        while True:
            snap = build_snapshot(with_quota=with_quota)
            pub.publish_state(snap)
            for ev in diff_events(prev, snap):
                pub.publish_event(ev)
                print(f"[event] {ev['kind']} {ev['src']}/{ev['project']}: {ev['task']}", file=sys.stderr)
            # ---- 消耗看板:阈值 + 保底节流 ----
            # 发布条件 = 实质变化(signature 变) 或 距上次发布 ≥ min_republish(心跳,让倒计时不至于太旧)。
            # 心跳那次 rev 不变 → 设备收到但不重绘,墨水屏不白刷。
            if usage_on:
                now_t = time.time()
                payload, alerts = usage_mod.build(snap, codex_quota.collect(), poller, now_t,
                                                 tz_offset_hours=tz_off)
                sig = usage_mod.signature(payload, poller)
                changed = sig != usage_sig
                if changed:
                    usage_rev += 1
                if changed or (now_t - usage_pub_at) >= min_republish:
                    payload["rev"] = usage_rev
                    pub.publish_usage(payload)
                    usage_sig, usage_pub_at = sig, now_t
                    print(f"[usage] rev={usage_rev} {'变化' if changed else '心跳'} "
                          + " ".join(f"{r['l']}={r['rem']}%" for r in payload["rows"]),
                          file=sys.stderr)

                # 额度阈值告警,现在通用地跑在看板行上(网关流量配额也能告警了)
                for a in alerts:
                    rem = a["rem"]
                    if rem < 0 or rem > (100 - quota_alert_pct):
                        continue
                    label, reset = a["label"], a["reset"]
                    if alerted.get(label) == reset:   # 同一重置周期只报一次
                        continue
                    alerted[label] = reset
                    pub.publish_event({
                        "kind": "quota", "src": label.split()[0], "project": label,
                        "msg": f"{label}额度只剩 {rem}%,注意节流或等待重置。",
                        "meta": f"{'真实' if a['real'] else '估算'} · 剩 {rem}%",
                        "ts": int(now_t),
                    })
                    print(f"[event] quota {label} 剩{rem}%", file=sys.stderr)

            # codex 无 hook:靠 rollout 的 completed_at 检测每轮完成
            for s in snap.get("sessions", []):
                if s.get("src") != "codex":
                    continue
                dt = s.get("done_ts") or 0
                if dt <= 0:
                    continue
                key = "codex:" + (s.get("project") or "?")
                if not first_pass and dt > codex_done.get(key, 0):
                    _emit_done_from_session(s)
                    print(f"[event] codex done {s.get('project')}", file=sys.stderr)
                codex_done[key] = max(dt, codex_done.get(key, 0))
            first_pass = False
            prev = snap
            n = len(snap["sessions"])
            q = snap["quota"]
            print(f"[state] {time.strftime('%H:%M:%S')} sessions={n} "
                  f"codex={_fmt_q(q.get('codex'))} claude={_fmt_q(q.get('claude'))}", file=sys.stderr)
            if args.once:
                break
            time.sleep(int(cfg["collector"].get("refresh_sec", 20)))
    except KeyboardInterrupt:
        pass
    finally:
        poller.stop()
        pub.close()


def _fmt_q(q):
    """日志用的额度摘要。只有周窗口的套餐(如 codex prolite)也要能显示 ——
    旧版遇到 h5 为 None 就整行 "-",把有效的周额度也吞了。"""
    if not q:
        return "-"
    parts = []
    if q.get("h5") is not None:
        parts.append(f"5h {q['h5']:.0f}%")
    if q.get("week") is not None:
        parts.append(f"wk {q['week']:.0f}%")
    return "/".join(parts) if parts else "-"


if __name__ == "__main__":
    main()
