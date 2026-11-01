#!/usr/bin/env bash
# Install or update web-tmux, then run its ./setup.sh.
#
#   curl -fsSL https://raw.githubusercontent.com/solab-tut/web-tmux/main/install.sh | bash
#
# It needs no root and installs no packages: anything missing is listed with
# the command to install it. It clones the latest release (or WEB_TMUX_REF)
# into WEB_TMUX_DIR (default ~/web-tmux); run again, it updates that checkout.
#
#   WEB_TMUX_DIR   where web-tmux lives (default: ~/web-tmux)
#   WEB_TMUX_REF   a tag or branch to install instead of the latest release
#   WEB_TMUX_REPO  the repository to clone (default: the GitHub one)
set -euo pipefail

# Everything is in functions and runs from the last line, so a download cut
# short runs nothing at all.

REPO_DEFAULT="https://github.com/solab-tut/web-tmux.git"
MIN_PYTHON_MINOR=10

say() { printf '%s\n' "$*"; }
fail() { printf 'web-tmux install: %s\n' "$*" >&2; exit 1; }

have_python() {
  local candidate minor
  for candidate in python3.13 python3.12 python3.11 python3.10 python3; do
    command -v "$candidate" >/dev/null 2>&1 || continue
    minor="$("$candidate" -c 'import sys; print(sys.version_info.minor if sys.version_info.major == 3 else 0)' 2>/dev/null || echo 0)"
    [ "$minor" -ge "$MIN_PYTHON_MINOR" ] && return 0
  done
  return 1
}

check_prerequisites() {
  local missing=()
  command -v git >/dev/null 2>&1 || missing+=(git)
  command -v tmux >/dev/null 2>&1 || missing+=(tmux)
  if ! command -v uv >/dev/null 2>&1 && ! have_python; then
    missing+=("python3 (3.10 or later)")
  fi
  if [ "${#missing[@]}" -gt 0 ]; then
    say "web-tmux needs: ${missing[*]}"
    case "$(uname -s)" in
      Darwin) say "  brew install git tmux python" ;;
      Linux)
        if command -v apt-get >/dev/null 2>&1; then
          say "  sudo apt install git tmux python3 python3-venv"
        elif command -v dnf >/dev/null 2>&1; then
          say "  sudo dnf install git tmux python3"
        fi
        ;;
    esac
    fail "install those, then run this again."
  fi
  if ! command -v tailscale >/dev/null 2>&1 && [ ! -x /Applications/Tailscale.app/Contents/MacOS/Tailscale ]; then
    say "Note: Tailscale is not installed yet. web-tmux is reached through it;"
    say "      get it from https://tailscale.com/download (setup will remind you)."
  fi
}

# github.com/solab-tut/web-tmux, whichever way the URL spells it, so a checkout
# cloned over ssh counts as the same repository.
repo_key() {
  local url="$1"
  url="${url%/}"
  url="${url%.git}"
  url="${url#https://}"
  url="${url#http://}"
  url="${url#ssh://}"
  url="${url#git@}"
  printf '%s\n' "${url/://}"
}

latest_release() {
  # The highest vX.Y[.Z] tag, or nothing when there is none.
  git ls-remote --tags --refs "$1" 'v*' 2>/dev/null \
    | sed -n 's#.*refs/tags/\(v[0-9][0-9.]*\)$#\1#p' \
    | awk -F'[v.]' '{ printf "%05d%05d%05d %s\n", $2, $3, $4, $0 }' \
    | sort | tail -n 1 | cut -d' ' -f2 || true
}

clone() {
  local repo="$1" dir="$2" ref="$3"
  say "Cloning web-tmux ${ref:-(default branch)} into $dir"
  if [ -n "$ref" ]; then
    git -c advice.detachedHead=false clone -q --branch "$ref" "$repo" "$dir"
  else
    git clone -q "$repo" "$dir"
  fi
}

update() {
  local repo="$1" dir="$2" ref="$3"
  [ -d "$dir/.git" ] || fail "$dir exists and is not a web-tmux checkout; set WEB_TMUX_DIR to install elsewhere."
  local origin
  origin="$(git -C "$dir" remote get-url origin 2>/dev/null || true)"
  [ "$(repo_key "$origin")" = "$(repo_key "$repo")" ] \
    || fail "$dir is a checkout of ${origin:-something else}, not web-tmux; set WEB_TMUX_DIR to install elsewhere."
  [ -z "$(git -C "$dir" status --porcelain --untracked-files=no)" ] \
    || fail "$dir has local changes; commit or stash them, then run this again."
  say "Updating web-tmux in $dir to ${ref:-the latest commit}"
  git -C "$dir" fetch -q --tags origin
  if [ -z "$ref" ]; then
    git -C "$dir" pull -q --ff-only
  elif git -C "$dir" show-ref -q --verify "refs/tags/$ref"; then
    git -C "$dir" -c advice.detachedHead=false checkout -q "$ref"
  else
    git -C "$dir" checkout -q "$ref"
    git -C "$dir" pull -q --ff-only origin "$ref"
  fi
}

run_setup() {
  # Piped into bash, this script's stdin is the script itself: setup's
  # questions have to come from the terminal instead.
  if (exec </dev/tty) 2>/dev/null; then
    ./setup.sh </dev/tty
  else
    ./setup.sh
  fi
}

main() {
  local repo="${WEB_TMUX_REPO:-$REPO_DEFAULT}"
  local dir="${WEB_TMUX_DIR:-$HOME/web-tmux}"
  local ref="${WEB_TMUX_REF:-}"

  check_prerequisites
  if [ -z "$ref" ]; then
    ref="$(latest_release "$repo")"
  fi
  local updating=""
  if [ -e "$dir" ]; then
    update "$repo" "$dir" "$ref"
    updating=1
  else
    clone "$repo" "$dir" "$ref"
  fi

  cd "$dir"
  if ! run_setup; then
    say ""
    say "When that is done, finish with: cd $dir && ./setup.sh"
    exit 1
  fi
  if [ -n "$updating" ] && [ -f server.pid ]; then
    say ""
    say "If web-tmux is running, restart it to use the update: cd $dir && ./server.sh restart"
  fi
}

main "$@"
