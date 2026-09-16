#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
umask 077

HTTP_PORT="8766"
WS_PORT="8765"
PID_FILE="server.pid"
PYTHON=".venv/bin/python"
CONFIG_FILE=".web-tmux.env"

if [ -f "$CONFIG_FILE" ]; then
  if stat -c '%a' "$CONFIG_FILE" >/dev/null 2>&1; then
    CONFIG_MODE="$(stat -c '%a' "$CONFIG_FILE")"
  else
    CONFIG_MODE="$(stat -f '%Lp' "$CONFIG_FILE")"
  fi
  if [ "$CONFIG_MODE" != "600" ]; then
    echo "ERROR: $CONFIG_FILE must have mode 600 (current: $CONFIG_MODE)." >&2
    exit 1
  fi
  set -a
  # shellcheck source=/dev/null
  . "./$CONFIG_FILE"
  set +a
fi

need_cmd() {
  if ! command -v "$1" >/dev/null 2>&1; then
    echo "required command not found: $1" >&2
    exit 1
  fi
}

is_web_tmux_pid() {
  local pid="$1"
  [[ "$pid" =~ ^[0-9]+$ ]] || return 1
  kill -0 "$pid" 2>/dev/null || return 1
  if [ -e "/proc/$pid/cwd" ]; then
    [ "$(readlink -f "/proc/$pid/cwd" 2>/dev/null)" = "$(pwd -P)" ] || return 1
  fi
  local command
  command="$(ps -p "$pid" -o command= 2>/dev/null || true)"
  [[ "$command" == *".venv/bin/python server.py"* ]]
}

stop_recorded_server() {
  if [ ! -f "$PID_FILE" ]; then return 0; fi
  local pid
  pid="$(sed -n '1p' "$PID_FILE" 2>/dev/null || true)"
  if is_web_tmux_pid "$pid"; then
    kill "$pid"
    for _ in {1..30}; do
      kill -0 "$pid" 2>/dev/null || break
      sleep 0.1
    done
    if kill -0 "$pid" 2>/dev/null; then
      echo "ERROR: server pid $pid did not stop; refusing to kill it forcibly." >&2
      return 1
    fi
  elif [[ "$pid" =~ ^[0-9]+$ ]] && kill -0 "$pid" 2>/dev/null; then
    echo "WARNING: stale PID file points to unrelated pid $pid; it was not killed." >&2
  fi
  rm -f "$PID_FILE"
}

port_is_free() {
  "$PYTHON" -c 'import socket,sys; s=socket.socket(); s.setsockopt(socket.SOL_SOCKET,socket.SO_REUSEADDR,1); s.bind(("127.0.0.1", int(sys.argv[1]))); s.close()' "$1"
}

do_stop() {
  if [ ! -f "$PID_FILE" ]; then
    echo "server is not running (no PID file)"
    return
  fi
  stop_recorded_server
  echo "server stopped"
}

do_start() {
  need_cmd tmux
  if [ ! -x "$PYTHON" ]; then
    echo "ERROR: .venv is missing; run ./setup.sh first." >&2
    exit 1
  fi
  stop_recorded_server
  for port in "$WS_PORT" "$HTTP_PORT"; do
    if ! port_is_free "$port"; then
      echo "ERROR: port $port is already in use; no process was killed." >&2
      exit 1
    fi
  done
  : > server.log
  chmod 600 server.log
  nohup env -u TMUX -u TMUX_PANE "$PYTHON" server.py >> server.log 2>&1 &
  echo $! > "$PID_FILE"
  chmod 600 "$PID_FILE"
  sleep 1.5
  if ! is_web_tmux_pid "$(sed -n '1p' "$PID_FILE")"; then
    echo "server exited unexpectedly" >&2
    tail -n 40 server.log >&2 || true
    exit 1
  fi
  echo "server started: pid=$(sed -n '1p' "$PID_FILE")"
  echo "HTTP  http://127.0.0.1:${HTTP_PORT}/"
  echo "WS    ws://127.0.0.1:${WS_PORT}/"
}

case "${1:-}" in
  stop)    do_stop ;;
  start|"") do_start ;;
  *)
    echo "usage: $0 [start|stop]" >&2
    exit 1
    ;;
esac
