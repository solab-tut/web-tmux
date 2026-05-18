#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
SERVER_SOCKET="webtmux-ctl"
SERVER_SESSION="server"
HTTP_PORT="8766"
WS_PORT="8765"

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
tmux -L "$SERVER_SOCKET" kill-server 2>/dev/null || true
sleep 0.5

# サーバー起動。server.py 側で操作対象の web セッションを作成/初期化する。
tmux -L "$SERVER_SOCKET" new-session -d -s "$SERVER_SESSION" \
  "cd '$PWD' && sleep 1 && exec env -u TMUX -u TMUX_PANE python3 server.py > server.log 2>&1"
sleep 1.5
if ! tmux -L "$SERVER_SOCKET" has-session -t "$SERVER_SESSION" 2>/dev/null; then
  echo "server exited unexpectedly" >&2
  tail -n 40 server.log >&2 || true
  exit 1
fi

echo "server started: tmux socket=$SERVER_SOCKET session=$SERVER_SESSION"
echo "HTTP  http://127.0.0.1:${HTTP_PORT}/"
echo "WS    ws://127.0.0.1:${WS_PORT}/"
