#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"

echo "=== web-tmux installer ==="

# 必須コマンドのチェック
for cmd in tmux python3; do
  if ! command -v "$cmd" >/dev/null 2>&1; then
    echo "required command not found: $cmd" >&2
    exit 1
  fi
done

# Python 3.10 以上チェック
py_ver="$(python3 -c 'import sys; print(sys.version_info[:2])' 2>/dev/null)"
if [[ "$py_ver" == "(3, "$((${py_ver##3,}")")" ]] && (( "${py_ver##3,}" < 10 )); then
  echo "python3 3.10+ is required (found $py_ver)" >&2
  exit 1
fi
echo "python3 $py_ver OK"

# websockets のインストールチェック
if ! python3 -c "import websockets" 2>/dev/null; then
  echo "installing websockets ..."
  python3 -m pip install --user websockets 2>&1
fi

# start.sh の実行権限付与
if [[ ! -x start.sh ]]; then
  chmod +x start.sh
  echo "start.sh executable"
fi

# 古いプロセスを片づけ
for port in 8765 8766; do
  if command -v lsof >/dev/null 2>&1; then
    lsof -tiTCP:"$port" -sTCP:LISTEN 2>/dev/null | xargs kill >/dev/null 2>&1 || true
  elif command -v fuser >/dev/null 2>&1; then
    fuser -k "${port}/tcp" >/dev/null 2>&1 || true
  fi
done
pgrep -f "[Pp]ython.*server.py" 2>/dev/null | xargs kill >/dev/null 2>&1 || true
sleep 0.5

echo ""
echo "installation complete. run:"
echo "  ./start.sh"
