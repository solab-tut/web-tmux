#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"

VENV_DIR=".venv"
REQUIREMENTS="requirements.txt"
MIN_PYTHON_MINOR=10

# ── OS detection ──────────────────────────────────────────────────────────────
detect_os() {
  case "$(uname -s)" in
    Darwin) echo "macos" ;;
    Linux)
      if [ -f /etc/os-release ]; then
        # shellcheck source=/dev/null
        . /etc/os-release
        echo "linux_${ID}_${VERSION_ID}"
      else
        echo "linux_unknown"
      fi
      ;;
    *) echo "unknown" ;;
  esac
}
OS="$(detect_os)"

# ── uv detection ──────────────────────────────────────────────────────────────
find_uv() {
  if command -v uv >/dev/null 2>&1; then
    command -v uv
    return 0
  fi
  local candidates=(
    "${HOME}/.local/bin/uv"
    "${XDG_BIN_HOME:-}/uv"
    "${CARGO_HOME:-${HOME}/.cargo}/bin/uv"
  )
  for c in "${candidates[@]}"; do
    if [ -x "$c" ]; then
      echo "$c"
      return 0
    fi
  done
  return 1
}

UV="$(find_uv 2>/dev/null || true)"

if [ -z "$UV" ]; then
  echo ""
  echo "uv is not installed."
  echo "uv manages Python versions automatically and is the recommended option."
  echo "See: https://astral.sh/uv/install.sh"
  echo ""
  printf "Install uv now? [Y/n]: "
  read -r REPLY
  REPLY="${REPLY:-Y}"
  if [[ "$REPLY" =~ ^[Yy]$ ]]; then
    echo "Installing uv..."
    curl -fsSL https://astral.sh/uv/install.sh | sh
    UV="$(find_uv 2>/dev/null || true)"
    if [ -z "$UV" ]; then
      echo "WARNING: uv installation completed but binary not found in PATH."
      echo "Falling back to venv+pip."
    else
      echo "uv installed: $UV"
    fi
  else
    echo "Skipping uv. Will use venv+pip."
  fi
fi

# ── Python 3.10+ detection (venv fallback) ────────────────────────────────────
find_python310() {
  for candidate in python3.13 python3.12 python3.11 python3.10 python3; do
    if command -v "$candidate" >/dev/null 2>&1; then
      local minor
      minor="$("$candidate" -c 'import sys; print(sys.version_info.minor)' 2>/dev/null || echo 0)"
      local major
      major="$("$candidate" -c 'import sys; print(sys.version_info.major)' 2>/dev/null || echo 0)"
      if [ "$major" -ge 3 ] && [ "$minor" -ge "$MIN_PYTHON_MINOR" ]; then
        echo "$candidate"
        return 0
      fi
    fi
  done
  return 1
}

ubuntu_python310_guidance() {
  echo ""
  echo "Ubuntu 20.04 ships Python 3.8 by default. Python 3.10+ is required."
  echo ""
  echo "Option 1 — deadsnakes PPA (recommended):"
  echo "  sudo add-apt-repository ppa:deadsnakes/ppa"
  echo "  sudo apt update"
  echo "  sudo apt install python3.10 python3.10-venv"
  echo ""
  echo "After installing Python 3.10, re-run: ./setup.sh"
}

# ── venv validation helper ────────────────────────────────────────────────────
venv_is_valid() {
  [ -d "$VENV_DIR" ] && [ -x "$VENV_DIR/bin/python" ]
}

# ── uv path ───────────────────────────────────────────────────────────────────
setup_with_uv() {
  local uv_bin="$1"
  echo "Using uv: $uv_bin ($("$uv_bin" --version))"

  if venv_is_valid; then
    echo "Virtual environment already exists at $VENV_DIR — skipping creation."
  else
    if [ -d "$VENV_DIR" ]; then
      echo "WARNING: $VENV_DIR exists but is incomplete. Recreating..."
      rm -rf "$VENV_DIR"
    fi
    echo "Creating virtual environment..."
    "$uv_bin" venv "$VENV_DIR" --python 3.10
  fi

  echo "Installing dependencies..."
  "$uv_bin" pip install --python "$VENV_DIR/bin/python" -r "$REQUIREMENTS"
}

# ── venv+pip fallback ─────────────────────────────────────────────────────────
setup_with_venv() {
  local python_bin="$1"
  echo "Using venv+pip with: $python_bin ($("$python_bin" --version))"

  if venv_is_valid; then
    echo "Virtual environment already exists at $VENV_DIR — skipping creation."
  else
    if [ -d "$VENV_DIR" ]; then
      echo "WARNING: $VENV_DIR exists but is incomplete. Recreating..."
      rm -rf "$VENV_DIR"
    fi
    echo "Creating virtual environment..."
    "$python_bin" -m venv "$VENV_DIR"
  fi

  echo "Installing dependencies..."
  "$VENV_DIR/bin/pip" install --upgrade pip --quiet
  "$VENV_DIR/bin/pip" install -r "$REQUIREMENTS"
}

# ── dispatch ──────────────────────────────────────────────────────────────────
if [ -n "$UV" ]; then
  setup_with_uv "$UV"
else
  PYTHON="$(find_python310 2>/dev/null || true)"
  if [ -z "$PYTHON" ]; then
    echo "ERROR: Python 3.10+ not found." >&2
    if [[ "$OS" == linux_ubuntu_20.04 ]]; then
      ubuntu_python310_guidance
    else
      echo "Please install Python 3.10 or later and re-run: ./setup.sh"
    fi
    exit 1
  fi
  setup_with_venv "$PYTHON"
fi

echo ""
echo "Setup complete. Run ./server.sh to start the server."
