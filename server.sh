#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
umask 077

HTTP_PORT="8766"
WS_PORT="8765"
PID_FILE="server.pid"
PYTHON=".venv/bin/python"
CONFIG_FILE=".web-tmux.env"

# Settings come from this checkout's .web-tmux.env only. A WEB_TMUX_*
# variable in the calling shell may belong to another checkout — a dev
# worktree's WEB_TMUX_STATE_DIR, left behind in a tmux pane's environment,
# say — and would point this server at that checkout's layouts.
for var in $(compgen -e); do
  if [[ "$var" == WEB_TMUX_* ]]; then unset "$var"; fi
done

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
  if [ -d /proc ]; then
    [ "$(readlink -f "/proc/$pid/cwd" 2>/dev/null)" = "$(pwd -P)" ] || return 1
  fi
  local command
  command="$(ps -p "$pid" -o command= 2>/dev/null || true)"
  [[ "$command" =~ ^([^[:space:]]*/)?python([0-9]+([.][0-9]+)*)?[[:space:]]+server[.]py([[:space:]]|$) ]]
}

find_checkout_server_pids() {
  # Recover a lost or stale PID file without killing whatever owns the port.
  # /proc supplies both the actual argv and cwd: another checkout, a shell
  # mentioning server.py, or another user's process must never be stopped.
  [ -d /proc ] && [ -x "$PYTHON" ] || return 0
  "$PYTHON" - "$(pwd -P)" <<'PY'
import os
import re
import sys

directory = sys.argv[1]
for entry in os.scandir('/proc'):
    if not entry.name.isdigit():
        continue
    try:
        if entry.stat().st_uid != os.getuid():
            continue
        with open(entry.path + '/cmdline', 'rb') as stream:
            args = stream.read().split(b'\0')
        if (len(args) < 2 or args[1] != b'server.py'
                or not re.fullmatch(rb'python(?:[0-9]+(?:\.[0-9]+)*)?', os.path.basename(args[0]))):
            continue
        if os.path.realpath(entry.path + '/cwd') == directory:
            print(entry.name)
    except (OSError, ValueError):
        continue  # process exited, or is not inspectable
PY
}

pid_is_running() {
  kill -0 "$1" 2>/dev/null || return 1
  local state
  state="$(ps -p "$1" -o stat= 2>/dev/null || true)"
  [[ -n "$state" && "$state" != Z* ]]
}

stop_recorded_server() {
  local pid recorded_pid=""
  local pids=()
  if [ -f "$PID_FILE" ]; then
    recorded_pid="$(sed -n '1p' "$PID_FILE" 2>/dev/null || true)"
    if is_web_tmux_pid "$recorded_pid"; then
      pids+=("$recorded_pid")
    elif [[ "$recorded_pid" =~ ^[0-9]+$ ]] && pid_is_running "$recorded_pid"; then
      echo "WARNING: stale PID file points to unrelated pid $recorded_pid; it was not killed." >&2
    fi
  fi
  while IFS= read -r pid; do
    if [ "$pid" != "$recorded_pid" ] && is_web_tmux_pid "$pid"; then
      echo "recovering server without a matching PID file: pid=$pid"
      pids+=("$pid")
    fi
  done < <(find_checkout_server_pids)
  for pid in "${pids[@]}"; do
    # Recheck immediately before signalling in case it exited during discovery.
    is_web_tmux_pid "$pid" || continue
    if ! kill "$pid" 2>/dev/null && pid_is_running "$pid"; then
      echo "ERROR: could not stop server pid $pid." >&2
      return 1
    fi
    # The server closes its ssh connections before it exits.
    for _ in {1..100}; do
      pid_is_running "$pid" || break
      sleep 0.1
    done
    if pid_is_running "$pid"; then
      echo "ERROR: server pid $pid did not stop; refusing to kill it forcibly." >&2
      return 1
    fi
  done
  rm -f "$PID_FILE"
}

port_is_free() {
  "$PYTHON" -c 'import socket,sys; s=socket.socket(); s.setsockopt(socket.SOL_SOCKET,socket.SO_REUSEADDR,1); s.bind(("127.0.0.1", int(sys.argv[1]))); s.close()' "$1"
}

do_stop() {
  stop_recorded_server
  echo "server stopped"
}

do_start() {
  need_cmd tmux
  if [ -n "${WEB_TMUX_SSH_HOSTS:-}" ]; then
    need_cmd ssh
  fi
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
  start|restart|"") do_start ;;
  *)
    echo "usage: $0 [start|stop|restart]" >&2
    exit 1
    ;;
esac
