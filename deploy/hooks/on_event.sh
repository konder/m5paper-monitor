#!/bin/bash
# Agent hook → 事件通知端。用法(在 hook 配置里):on_event.sh <kind> <source>
#   kind: done | needs_input   source: claude | codex
# 读取 hook 的 stdin JSON 原样转给采集器,快速返回不阻塞 agent。
KIND="${1:-done}"
SOURCE="${2:-claude}"
PORT="${M5_EVENT_PORT:-8898}"
INPUT=$(cat)
[ -z "$INPUT" ] && INPUT='{}'
curl -s -m 3 -X POST "http://127.0.0.1:${PORT}/event?kind=${KIND}&source=${SOURCE}" \
  -H 'Content-Type: application/json' --data-binary "$INPUT" >/dev/null 2>&1 || true
exit 0
