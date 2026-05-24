#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
HTTP_PORT="8766"
WS_PORT="8765"
PID_FILE="server.pid"

need_cmd() {
  if ! command -v "$1" >/dev/null 2>&1; then
    echo "required command not found: $1" >&2
    exit 1
  fi
}

kill_by_pattern() {
  local pattern="$1"
  if command -v pgrep >/dev/null 2>&1; then
    local pids
    pids="$(pgrep -f "$pattern" 2>/dev/null || true)"
    if [[ -n "$pids" ]]; then
      # shellcheck disable=SC2086
      kill $pids 2>/dev/null || true
    fi
    return
  fi

  if command -v ps >/dev/null 2>&1; then
    local pids
    pids="$(
      ps -ax -o pid= -o command= 2>/dev/null \
        | awk -v pat="$pattern" '$0 ~ pat {print $1}'
    )"
    if [[ -n "$pids" ]]; then
      # shellcheck disable=SC2086
      kill $pids 2>/dev/null || true
    fi
  fi
}

kill_by_port() {
  local port="$1"

  if command -v lsof >/dev/null 2>&1; then
    local pids
    pids="$(lsof -tiTCP:"$port" -sTCP:LISTEN 2>/dev/null || true)"
    if [[ -n "$pids" ]]; then
      # shellcheck disable=SC2086
      kill $pids 2>/dev/null || true
    fi
    return
  fi

  if command -v fuser >/dev/null 2>&1; then
    fuser -k "${port}/tcp" >/dev/null 2>&1 || true
  fi
}

need_cmd tmux
need_cmd python3

# 既存プロセスを停止
kill_by_pattern "[Pp]ython.*server.py"
kill_by_port "$WS_PORT"
kill_by_port "$HTTP_PORT"
sleep 0.5

# server.py をバックグラウンドプロセスとして直接起動
# env -u TMUX -u TMUX_PANE: tmux内から起動した場合でもデフォルトsocketを使うよう保証
nohup env -u TMUX -u TMUX_PANE python3 server.py > server.log 2>&1 &
echo $! > "$PID_FILE"

sleep 1.5
if ! kill -0 "$(cat "$PID_FILE" 2>/dev/null)" 2>/dev/null; then
  echo "server exited unexpectedly" >&2
  tail -n 40 server.log >&2 || true
  exit 1
fi

echo "server started: pid=$(cat "$PID_FILE")"
echo "HTTP  http://127.0.0.1:${HTTP_PORT}/"
echo "WS    ws://127.0.0.1:${WS_PORT}/"
