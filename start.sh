#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
SERVER_SOCKET="webtmux-ctl"
SERVER_SESSION="server"

# 既存プロセスを停止
pkill -f "[Pp]ython.*server.py" 2>/dev/null || true
lsof -tiTCP:8765 -sTCP:LISTEN 2>/dev/null | xargs kill 2>/dev/null || true
lsof -tiTCP:8766 -sTCP:LISTEN 2>/dev/null | xargs kill 2>/dev/null || true
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
echo "HTTP  http://127.0.0.1:8766/"
echo "WS    ws://127.0.0.1:8765/"
