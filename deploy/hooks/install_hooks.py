#!/usr/bin/env python3
"""幂等地把通知 hook 合并进 claude / codex 配置。
  claude: ~/.claude/settings.json   hooks.{Stop,Notification}
  codex : ~/.codex/hooks.json        hooks.{Stop,Notification}
只追加自己的组(命令含 on_event.sh),不动已有条目。写前自动备份。
用法: python3 install_hooks.py [--uninstall]
"""
import json
import os
import sys
import time

HOOK = os.path.expanduser("~/m5paper-monitor/gateway/hooks/on_event.sh")
CLAUDE = os.path.expanduser("~/.claude/settings.json")
CODEX = os.path.expanduser("~/.codex/hooks.json")

# (事件名, kind) —— source 由文件决定
EVENTS = [("Stop", "done"), ("Notification", "needs_input")]


def _cmd(kind, source):
    return f'bash "{HOOK}" {kind} {source}'


def _load(path):
    try:
        with open(path) as f:
            return json.load(f)
    except FileNotFoundError:
        return {}
    except Exception as e:
        print(f"  ! 读取 {path} 失败: {e}"); return None


def _save(path, data):
    if os.path.exists(path):
        bak = f"{path}.bak.{int(time.time())}"
        os.replace(path, bak)
        with open(bak) as f:
            pass  # 备份已生成
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)


def _has_our_group(arr):
    for g in arr:
        for h in g.get("hooks", []):
            if "on_event.sh" in (h.get("command") or ""):
                return True
    return False


def _clean(arr):
    out = []
    for g in arr:
        hooks = [h for h in g.get("hooks", []) if "on_event.sh" not in (h.get("command") or "")]
        if hooks:
            g = dict(g); g["hooks"] = hooks; out.append(g)
        elif not g.get("hooks"):
            out.append(g)  # 保留本就空的(不太可能)
    return out


def install(path, source, uninstall):
    data = _load(path)
    if data is None:
        return
    data.setdefault("hooks", {})
    changed = False
    for ev, kind in EVENTS:
        arr = data["hooks"].setdefault(ev, [])
        # 先移除旧的我们自己的组(便于更新命令)
        newarr = _clean(arr)
        if uninstall:
            if newarr != arr:
                data["hooks"][ev] = newarr; changed = True
            continue
        newarr.append({"hooks": [{"type": "command", "command": _cmd(kind, source)}]})
        data["hooks"][ev] = newarr
        changed = True
    if changed:
        _save(path, data)
        print(f"  ✓ {'卸载' if uninstall else '安装'} {source} hooks → {path}")
    else:
        print(f"  - {source} 无变化")


def main():
    uninstall = "--uninstall" in sys.argv
    if not uninstall and not os.access(HOOK, os.X_OK):
        try:
            os.chmod(HOOK, 0o755)
        except OSError:
            pass
    print(("卸载" if uninstall else "安装") + " 通知 hooks:")
    install(CLAUDE, "claude", uninstall)
    install(CODEX, "codex", uninstall)
    print("完成。重启对应 CLI 会话后生效。")


if __name__ == "__main__":
    main()
