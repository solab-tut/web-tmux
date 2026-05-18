#!/usr/bin/env bash
# web-tmux installer
#
# Generates every source file this service needs into the current directory.
# Safe to re-run: existing files are overwritten with the bundled contents.
#
# After running, start the server with `./start.sh`.

set -euo pipefail
cd "$(dirname "$0")"

echo "=== web-tmux installer ==="
mkdir -p static

echo "  -> server.py"
cat > server.py <<'__WEBTMUX_EOF__'
#!/usr/bin/env python3
from __future__ import annotations

"""
web-tmux control mode server

  HTTP  127.0.0.1:8766  — static files
  WS    127.0.0.1:8765  — terminal events
"""
import asyncio
import base64
import json
import logging
import os
import threading
from functools import partial
from http.server import HTTPServer, SimpleHTTPRequestHandler

import websockets

from layout_parser import parse_layout
from tmux_control import TmuxControl

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s %(levelname)s %(message)s',
)
log = logging.getLogger(__name__)

HERE       = os.path.dirname(os.path.abspath(__file__))
STATIC_DIR = os.path.join(HERE, 'static')
WS_PORT    = 8765
HTTP_PORT  = 8766
SESSION    = os.environ.get('TMUX_SESSION', 'web')

tmux = None  # TmuxControl


class NoCacheStaticHandler(SimpleHTTPRequestHandler):
    def end_headers(self) -> None:
        self.send_header('Cache-Control', 'no-store, no-cache, must-revalidate, max-age=0')
        self.send_header('Pragma', 'no-cache')
        self.send_header('Expires', '0')
        super().end_headers()


def _pane_id(value) -> str:
    value = str(value or '')
    return value if value.startswith('%') and value[1:].isdigit() else ''


def _window_index(value) -> int | None:
    try:
        idx = int(value)
    except (TypeError, ValueError):
        return None
    return idx if idx >= 0 else None


def _session_name(value) -> str:
    value = str(value or '').strip()
    if not value or any(ch in value for ch in '\r\n\0:'):
        return ''
    return value[:128]


def _window_name(value) -> str:
    value = str(value or '').strip()
    if not value or any(ch in value for ch in '\r\n\0'):
        return ''
    return value[:128]


def _tmux_quote(value: str) -> str:
    return '"' + value.replace('\\', '\\\\').replace('"', '\\"') + '"'


def _to_bool(value) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    if isinstance(value, str):
        return value.strip().lower() in {'1', 'true', 'yes', 'on'}
    return False


async def _send_current_view(websocket, msg_type: str = 'window_switched') -> None:
    state = await tmux.get_initial_state()
    layout_panes = parse_layout(state.get('layout', ''))
    await websocket.send(json.dumps({
        'type':         msg_type,
        'session':      state['session'],
        'sessions':     state['sessions'],
        'windows':      state['windows'],
        'panes':        state['panes'],
        'active_pane':  state['active_pane'],
        'layout':       state.get('layout', ''),
        'layout_panes': layout_panes,
    }))


async def _run_tmux(*args: str) -> int:
    proc = await asyncio.create_subprocess_exec(
        'tmux', *args,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
    )
    return await proc.wait()


async def _run_tmux_size_safe(*args: str) -> int:
    # Keep window-size=latest so the active control client size is reflected in
    # the current window. Reassert it around mutating commands for tmux 3.6a.
    await _run_tmux('set-option', '-g', 'window-size', 'latest')
    try:
        return await _run_tmux(*args)
    finally:
        await _run_tmux('set-option', '-g', 'window-size', 'latest')


# ──────────────────────────────────────────── HTTP (static files, background thread)

def _start_http() -> None:
    handler = partial(NoCacheStaticHandler, directory=STATIC_DIR)
    server  = HTTPServer(('127.0.0.1', HTTP_PORT), handler)
    log.info('HTTP  http://127.0.0.1:%d/', HTTP_PORT)
    server.serve_forever()


# ──────────────────────────────────────────── WebSocket

_resize_master = None   # only the active browser tab/device may send resize


async def ws_handler(websocket, path=None) -> None:
    global _resize_master
    log.info('ws connect %s', websocket.remote_address)
    if _resize_master is None:
        _resize_master = websocket
    tmux.subscribers.append(websocket)
    try:
        # 1. Send initial state
        state = await tmux.get_initial_state()
        layout_panes = parse_layout(state.get('layout', ''))
        await websocket.send(json.dumps({
            'type':         'init',
            'session':      state['session'],
            'sessions':     state['sessions'],
            'windows':      state['windows'],
            'panes':        state['panes'],
            'active_pane':  state['active_pane'],
            'layout':       state.get('layout', ''),
            'layout_panes': layout_panes,
        }))

        # 2. Handle incoming messages
        async for raw in websocket:
            try:
                msg = json.loads(raw)
            except json.JSONDecodeError:
                continue
            await _handle_msg(websocket, msg)

    except websockets.exceptions.ConnectionClosed:
        pass
    except Exception as e:
        log.error('ws error: %s', e)
    finally:
        if websocket in tmux.subscribers:
            tmux.subscribers.remove(websocket)
        if websocket is _resize_master:
            _resize_master = tmux.subscribers[-1] if tmux.subscribers else None
        log.info('ws disconnect %s', websocket.remote_address)


async def _handle_msg(websocket, msg: dict) -> None:
    global _resize_master
    t = msg.get('type')

    if t == 'input':
        _resize_master = websocket
        pane = msg.get('pane', '')
        data = msg.get('data', '')
        if pane and data:
            await tmux.write_input(pane, data.encode('utf-8'))

    elif t == 'client_active':
        _resize_master = websocket

    elif t == 'resize':
        if websocket is not _resize_master:
            return   # only the active tab/device may resize
        cols = int(msg.get('cols', 80))
        rows = int(msg.get('rows', 24))
        log.info('resize request cols=%d rows=%d', cols, rows)
        await tmux.send_command(f'refresh-client -C {cols}x{rows}')

    elif t == 'new_window':
        await _run_tmux_size_safe('new-window', '-t', tmux.session)
        await _send_current_view(websocket)

    elif t == 'new_session':
        import time
        name = _session_name(msg.get('name')) or f'sess-{int(time.time())}'
        was_subscribed = websocket in tmux.subscribers
        if was_subscribed:
            tmux.subscribers.remove(websocket)
        try:
            await _run_tmux_size_safe('new-session', '-d', '-s', name)
            await tmux.send_command(f'switch-client -t {_tmux_quote(name)}')
            tmux.session = name
            await _send_current_view(websocket)
        finally:
            if was_subscribed:
                tmux.subscribers.append(websocket)

    elif t == 'select_session':
        name = _session_name(msg.get('session'))
        if not name:
            return
        was_subscribed = websocket in tmux.subscribers
        if was_subscribed:
            tmux.subscribers.remove(websocket)
        try:
            await tmux.send_command(f'switch-client -t {_tmux_quote(name)}')
            tmux.session = name
            await _send_current_view(websocket)
        finally:
            if was_subscribed:
                tmux.subscribers.append(websocket)

    elif t == 'rename_session':
        current_name = _session_name(msg.get('session'))
        next_name = _session_name(msg.get('name'))
        if not current_name or not next_name or current_name == next_name:
            return
        await _run_tmux('rename-session', '-t', current_name, next_name)
        if tmux.session == current_name:
            tmux.session = next_name
            await _send_current_view(websocket)
        else:
            state = await tmux.get_initial_state()
            await websocket.send(json.dumps({
                'type':        'state',
                'session':     state['session'],
                'sessions':    state['sessions'],
                'windows':     state['windows'],
                'panes':       state['panes'],
                'active_pane': state['active_pane'],
            }))

    elif t == 'rename_window':
        win = _window_index(msg.get('window'))
        name = _window_name(msg.get('name'))
        if win is None or not name:
            return
        await _run_tmux('rename-window', '-t', f'{tmux.session}:{win}', name)
        await _send_current_view(websocket, msg_type='state')

    elif t == 'split_window':
        direction = msg.get('direction', 'h')   # 'h' (side-by-side) or 'v' (top-bottom)
        flag = '-h' if direction == 'h' else '-v'
        pane = _pane_id(msg.get('pane'))
        target = f' -t {pane}' if pane else ''
        await tmux.send_command(f'split-window {flag}{target}')

    elif t == 'get_snapshot':
        pane = _pane_id(msg.get('pane'))
        if pane:
            content = await tmux.capture_pane(pane)
            cursor = await tmux.get_pane_cursor(pane)
            log.info(
                'snapshot pane=%s bytes=%d cursor=%d,%d size=%dx%d',
                pane,
                len(content),
                cursor['cursor_x'],
                cursor['cursor_y'],
                cursor['pane_cols'],
                cursor['pane_rows'],
            )
            await websocket.send(json.dumps({
                'type': 'snapshot',
                'pane': pane,
                'data': base64.b64encode(content).decode('ascii'),
                'cursor_x': cursor['cursor_x'],
                'cursor_y': cursor['cursor_y'],
                'pane_cols': cursor['pane_cols'],
                'pane_rows': cursor['pane_rows'],
            }))

    elif t == 'get_state':
        # Used by the client to refresh sidebar lists after pane/window changes.
        state = await tmux.get_initial_state()
        await websocket.send(json.dumps({
            'type':        'state',
            'session':     state['session'],
            'sessions':    state['sessions'],
            'windows':     state['windows'],
            'panes':       state['panes'],
            'active_pane': state['active_pane'],
        }))

    elif t == 'get_current_view':
        await _send_current_view(websocket)

    elif t == 'select_pane':
        _resize_master = websocket
        pane = _pane_id(msg.get('pane'))
        force_zoom = _to_bool(msg.get('force_zoom'))
        if pane:
            await tmux.send_command(f'select-pane -t {pane}')
            if force_zoom:
                zoomed = (await tmux.send_command(
                    f'display-message -p -t {pane} "#{{window_zoomed_flag}}"'
                )).strip()
                if zoomed != '1':
                    await tmux.send_command(f'resize-pane -Z -t {pane}')

    elif t == 'select_window':
        _resize_master = websocket
        win = _window_index(msg.get('window'))
        if win is None:
            return
        # Suspend broadcasts to this connection while the switch is in flight.
        # Otherwise a %layout-change for the NEW window may arrive before
        # window_switched, leaving the browser confused about which window
        # the layout belongs to.
        was_subscribed = websocket in tmux.subscribers
        if was_subscribed:
            tmux.subscribers.remove(websocket)
        try:
            target = f'{tmux.session}:{win}'
            await tmux.send_command(f'select-window -t {_tmux_quote(target)}')
            # When switching via WINDOW list, show multi-pane windows unzoomed.
            zoom_info = (await tmux.send_command(
                f'display-message -p -t {_tmux_quote(target)} '
                '"#{window_zoomed_flag}|#{window_panes}|#{pane_id}"'
            )).strip()
            zoom_parts = zoom_info.split('|', 2)
            zoomed = len(zoom_parts) > 0 and zoom_parts[0] == '1'
            pane_count = int(zoom_parts[1]) if len(zoom_parts) > 1 and zoom_parts[1].isdigit() else 0
            pane_id = _pane_id(zoom_parts[2] if len(zoom_parts) > 2 else '')
            if zoomed and pane_count > 1:
                target = f' -t {pane_id}' if pane_id else ''
                await tmux.send_command(f'resize-pane -Z{target}')
            await _send_current_view(websocket)
        finally:
            if was_subscribed:
                tmux.subscribers.append(websocket)


# ──────────────────────────────────────────── main

async def main() -> None:
    global tmux

    # Static file server in a daemon thread
    t = threading.Thread(target=_start_http, daemon=True)
    t.start()

    # tmux control mode
    tmux = TmuxControl(session=SESSION)
    await tmux.start()

    # WebSocket server
    log.info('WS    ws://127.0.0.1:%d/', WS_PORT)
    async with websockets.serve(ws_handler, '127.0.0.1', WS_PORT):
        await asyncio.Future()  # run forever


if __name__ == '__main__':
    asyncio.run(main())
__WEBTMUX_EOF__

echo "  -> tmux_control.py"
cat > tmux_control.py <<'__WEBTMUX_EOF__'
#!/usr/bin/env python3
from __future__ import annotations

"""
tmux control mode (-CC) subprocess wrapper.

tmux -CC requires a real TTY; we create a PTY pair and run tmux with the
slave end as its controlling terminal.  The master end is used for I/O.

Input to panes:    via `send-keys -H <hex>...` plus prefix-table routing
Output from panes: `%output` notifications → vis-decoded → base64 → broadcast
"""
import asyncio
import base64
import fcntl
import json
import logging
import os
import pty
import re
import struct
import termios

log = logging.getLogger(__name__)

_ZSH_EOL_MARK_RE = re.compile(
    br'\x1b\[1m\x1b\[7m[%#]\x1b\[27m\x1b\[1m\x1b\[0m *(\r ?\r)'
)

_TMUX_ESCAPED_KEYS: tuple[tuple[bytes, str], ...] = (
    (b'\x1b[A', 'Up'),
    (b'\x1b[B', 'Down'),
    (b'\x1b[C', 'Right'),
    (b'\x1b[D', 'Left'),
    (b'\x1b[H', 'Home'),
    (b'\x1b[F', 'End'),
    (b'\x1b[1~', 'Home'),
    (b'\x1b[4~', 'End'),
    (b'\x1b[5~', 'PageUp'),
    (b'\x1b[6~', 'PageDown'),
)

_TMUX_CONTROL_KEYS = {
    0x00: 'C-Space',
    0x09: 'Tab',
    0x0d: 'Enter',
    0x1b: 'Escape',
    0x1c: 'C-\\',
    0x1d: 'C-]',
    0x1e: 'C-^',
    0x1f: 'C-_',
    0x7f: 'BSpace',
}


def _decode_output(s: str) -> bytes:
    """Decode tmux vis(3)-encoded string.

    tmux encodes control characters using C/octal escapes:
      \\n  → 0x0a   \\r  → 0x0d   \\t  → 0x09
      \\\\  → 0x5c
      \\NNN → byte with octal value NNN  (e.g. \\033 = ESC = 0x1b)
    All other bytes pass through as latin-1.
    """
    out: list[bytes] = []
    i = 0
    while i < len(s):
        if s[i] == '\\' and i + 1 < len(s):
            nxt = s[i + 1]
            if nxt == 'n':
                out.append(b'\n');  i += 2
            elif nxt == 'r':
                out.append(b'\r');  i += 2
            elif nxt == 't':
                out.append(b'\t');  i += 2
            elif nxt == '\\':
                out.append(b'\\'); i += 2
            elif nxt in '01234567':
                # Octal escape \NNN (1–3 octal digits)
                j = i + 1
                oct_s = ''
                while j < len(s) and len(oct_s) < 3 and s[j] in '01234567':
                    oct_s += s[j]
                    j += 1
                out.append(bytes([int(oct_s, 8) & 0xff]))
                i = j
            else:
                out.append(s[i].encode('latin-1', errors='replace'))
                i += 1
        else:
            out.append(s[i].encode('latin-1', errors='replace'))
            i += 1
    return b''.join(out)


def _decode_output_stream(s: str, remainder: str = '') -> tuple[bytes, str]:
    """Decode vis-encoded tmux output with chunk-boundary safety.

    `%output` payload can be split at arbitrary boundaries, including inside
    escape sequences such as `\\`, `\\n`, `\\033`. Keep incomplete tail bytes as
    remainder and decode them with the next chunk.
    """
    src = remainder + s
    out = bytearray()
    i = 0
    n = len(src)
    while i < n:
        if src[i] != '\\':
            out.extend(src[i].encode('latin-1', errors='replace'))
            i += 1
            continue

        if i + 1 >= n:
            break

        nxt = src[i + 1]
        if nxt == 'n':
            out.append(0x0a)
            i += 2
            continue
        if nxt == 'r':
            out.append(0x0d)
            i += 2
            continue
        if nxt == 't':
            out.append(0x09)
            i += 2
            continue
        if nxt == '\\':
            out.append(0x5c)
            i += 2
            continue
        if nxt in '01234567':
            j = i + 1
            oct_s = ''
            while j < n and len(oct_s) < 3 and src[j] in '01234567':
                oct_s += src[j]
                j += 1
            # Octal escape is 1-3 digits. If chunk ended before 3 digits,
            # keep it for the next chunk to avoid splitting `\033`.
            if j == n and len(oct_s) < 3:
                break
            out.append(int(oct_s, 8) & 0xff)
            i = j
            continue

        # Unknown escape. Preserve the backslash literally.
        out.append(0x5c)
        i += 1

    return bytes(out), src[i:]


def _strip_zsh_eol_marks(data: bytes) -> bytes:
    # Remove the visible mark but keep cursor rewind (\r ?\r), otherwise
    # line-editor redraw on wrapped input can break.
    return _ZSH_EOL_MARK_RE.sub(b'\\1', data)


def _tmux_quote(value: str) -> str:
    return '"' + value.replace('\\', '\\\\').replace('"', '\\"') + '"'


def _tmux_text(value: str) -> str:
    try:
        return value.encode('latin-1').decode('utf-8')
    except UnicodeDecodeError:
        return value


def _tmux_key_from_bytes(data: bytes) -> tuple[str, int] | None:
    if not data:
        return None
    for seq, name in _TMUX_ESCAPED_KEYS:
        if data.startswith(seq):
            return name, len(seq)
    b = data[0]
    if 0x01 <= b <= 0x1a:
        return f'C-{chr(ord("a") + b - 1)}', 1
    if b in _TMUX_CONTROL_KEYS:
        return _TMUX_CONTROL_KEYS[b], 1
    if 0x20 <= b <= 0x7e:
        return chr(b), 1
    return None


class TmuxControl:
    def __init__(self, session: str = 'web'):
        self.session    = session
        self.proc       = None
        self.subscribers: list = []
        self._master_fd: int | None = None
        self._buf: str  = ''
        self._pending: list[asyncio.Future] = []
        self._cur_resp: list[str] = []
        self._in_resp: bool = False
        self._restart_lock = asyncio.Lock()
        self._restart_task: asyncio.Task | None = None
        self._decode_remainder: dict[str, str] = {}
        self._prefix_key_names: set[str] | None = None
        self._prefix_pending: bool = False

    # ──────────────────────────────────────── lifecycle

    async def start(self) -> None:
        await self._restart_client()

    async def _restart_client(self) -> None:
        async with self._restart_lock:
            await self._cleanup_client()
            await self._ensure_session_exists()
            await self._set_window_size_mode('latest')
            await self._attach_control_client()

    async def _ensure_session_exists(self) -> None:
        # Ensure session exists
        chk = await asyncio.create_subprocess_exec(
            'tmux', 'has-session', '-t', self.session,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        await chk.wait()
        if chk.returncode != 0:
            new = await asyncio.create_subprocess_exec(
                'tmux', 'new-session', '-d', '-s', self.session,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            await new.wait()

    async def _set_window_size_mode(self, mode: str) -> None:
        # Let the active browser client drive the window size via refresh-client -C.
        # Using "smallest" makes a narrow phone/control client shrink the shared
        # tmux window even when the desktop browser is the active viewer.
        ws = await asyncio.create_subprocess_exec(
            'tmux', 'set-option', '-g', 'window-size', mode,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        await ws.wait()

    async def _attach_control_client(self) -> None:
        # PTY pair: slave is tmux's controlling terminal
        master_fd, slave_fd = pty.openpty()
        _set_winsize(slave_fd, rows=50, cols=220)

        self.proc = await asyncio.create_subprocess_exec(
            'tmux', '-CC', 'attach-session', '-t', self.session,
            stdin=slave_fd, stdout=slave_fd, stderr=slave_fd,
            close_fds=True,
        )
        os.close(slave_fd)
        self._master_fd = master_fd

        # Register master_fd as an asyncio readable source
        loop = asyncio.get_event_loop()
        loop.add_reader(master_fd, self._on_readable)

        log.info('tmux -CC started, session=%s pid=%d', self.session, self.proc.pid)

    async def _cleanup_client(self) -> None:
        loop = asyncio.get_event_loop()
        if self._master_fd is not None:
            try:
                loop.remove_reader(self._master_fd)
            except Exception:
                pass
            try:
                os.close(self._master_fd)
            except OSError:
                pass
            self._master_fd = None

        if self.proc is not None and self.proc.returncode is None:
            self.proc.terminate()
            try:
                await asyncio.wait_for(self.proc.wait(), timeout=1.0)
            except (asyncio.TimeoutError, ProcessLookupError):
                self.proc.kill()
                try:
                    await asyncio.wait_for(self.proc.wait(), timeout=1.0)
                except Exception:
                    pass
        self.proc = None

        self._buf = ''
        self._cur_resp = []
        self._in_resp = False
        self._decode_remainder = {}
        self._prefix_key_names = None
        self._prefix_pending = False
        while self._pending:
            fut = self._pending.pop(0)
            if not fut.done():
                fut.set_result('')

    async def _ensure_connected(self) -> None:
        if self._master_fd is not None and self.proc is not None and self.proc.returncode is None:
            return
        await self._restart_client()

    def _schedule_restart(self) -> None:
        if self._restart_task and not self._restart_task.done():
            return
        self._restart_task = asyncio.create_task(self._restart_client())

    # ──────────────────────────────────────── async I/O

    def _on_readable(self) -> None:
        try:
            data = os.read(self._master_fd, 4096)
        except OSError:
            asyncio.get_event_loop().remove_reader(self._master_fd)
            log.warning('tmux PTY master closed')
            return
        if not data:
            return
        self._buf += data.decode('latin-1', errors='replace')
        while '\n' in self._buf:
            line, self._buf = self._buf.split('\n', 1)
            self._handle_line(line.rstrip('\r'))

    # ──────────────────────────────────────── commands

    async def send_command(self, cmd: str) -> str:
        """Send one tmux command, await %begin/%end response."""
        for attempt in range(2):
            await self._ensure_connected()
            if self._master_fd is None:
                return ''

            loop = asyncio.get_event_loop()
            fut: asyncio.Future = loop.create_future()
            self._pending.append(fut)
            try:
                os.write(self._master_fd, (cmd + '\n').encode('utf-8'))
            except OSError as e:
                if fut in self._pending:
                    self._pending.remove(fut)
                log.error('send_command write error: %s', e)
                if attempt == 0:
                    await self._restart_client()
                    continue
                return ''

            try:
                return await asyncio.wait_for(asyncio.shield(fut), timeout=5.0)
            except asyncio.TimeoutError:
                if fut in self._pending:
                    self._pending.remove(fut)
                if attempt == 0:
                    await self._restart_client()
                    continue
                return ''
        return ''

    async def _ensure_prefix_key_names(self) -> set[str]:
        if self._prefix_key_names is not None:
            return self._prefix_key_names

        names: set[str] = set()
        for option in ('prefix', 'prefix2'):
            value = (await self.send_command(f'show-options -gv {option}')).strip()
            if value and value != 'None':
                names.add(value)
        self._prefix_key_names = names or {'C-b'}
        return self._prefix_key_names

    async def _send_literal_input(self, pane_id: str, data: bytes) -> None:
        if not data:
            return
        chunk_size = 100
        target = f' -t {pane_id}' if pane_id else ''
        for i in range(0, len(data), chunk_size):
            chunk = data[i:i + chunk_size]
            hex_args = ' '.join(f'{b:02x}' for b in chunk)
            await self.send_command(f'send-keys{target} -H {hex_args}')

    async def _send_prefix_key(self, data: bytes) -> int:
        key = _tmux_key_from_bytes(data)
        if not key:
            self._prefix_pending = False
            return 0
        key_name, used = key
        await self.send_command(f'send-keys -K {_tmux_quote(key_name)}')
        self._prefix_pending = False
        return used

    async def write_input(self, pane_id: str, data: bytes) -> None:
        """Inject input into a pane, preserving tmux prefix key bindings.

        Normal input goes directly to the selected pane. When the configured
        prefix key is seen, switch the control client to the `prefix` key table
        and send the following key through tmux's key processing.
        """
        if not data:
            return

        prefix_keys = await self._ensure_prefix_key_names()
        literal_start = 0
        i = 0
        while i < len(data):
            if self._prefix_pending:
                await self._send_literal_input(pane_id, data[literal_start:i])
                used = await self._send_prefix_key(data[i:])
                if used:
                    i += used
                    literal_start = i
                    continue
                literal_start = i

            key = _tmux_key_from_bytes(data[i:])
            if key and key[0] in prefix_keys:
                await self._send_literal_input(pane_id, data[literal_start:i])
                await self.send_command('switch-client -T prefix')
                self._prefix_pending = True
                i += key[1]
                literal_start = i
                continue

            i += 1

        await self._send_literal_input(pane_id, data[literal_start:])

    async def capture_pane(self, pane_id: str) -> bytes:
        raw = await self.send_command(f'capture-pane -t {pane_id} -p -e -N')
        # Response content uses the same vis(3) encoding as %output data.
        return _strip_zsh_eol_marks(_decode_output(raw))

    async def get_pane_cursor(self, pane_id: str) -> dict:
        raw = await self.send_command(
            f'display-message -p -t {pane_id} '
            '"#{cursor_x}|#{cursor_y}|#{pane_width}|#{pane_height}"'
        )
        parts = raw.strip().split('|', 3)
        if len(parts) < 4:
            return {'cursor_x': 0, 'cursor_y': 0, 'pane_cols': 0, 'pane_rows': 0}
        return {
            'cursor_x': int(parts[0]) if parts[0].isdigit() else 0,
            'cursor_y': int(parts[1]) if parts[1].isdigit() else 0,
            'pane_cols': int(parts[2]) if parts[2].isdigit() else 0,
            'pane_rows': int(parts[3]) if parts[3].isdigit() else 0,
        }

    async def get_initial_state(self) -> dict:
        sess_raw = await self.send_command(
            'list-sessions -F'
            ' "#{session_name}|#{session_windows}|#{session_attached}"'
        )
        sessions = []
        for line in sess_raw.splitlines():
            p = line.split('|', 2)
            if len(p) < 3:
                continue
            name = _tmux_text(p[0])
            windows = int(p[1]) if p[1].isdigit() else 0
            attached = p[2] not in ('', '0')
            sessions.append({
                'name': name,
                'windows': windows,
                'attached': attached,
                'active': name == self.session,
            })

        win_raw = await self.send_command(
            f'list-windows -t {_tmux_quote(self.session)} -F'
            ' "#{window_index}|#{window_id}|#{window_name}|#{window_active}|#{window_visible_layout}"'
        )
        windows, active_layout = [], ''
        active_window_idx: int | None = None
        for line in win_raw.splitlines():
            p = line.split('|', 4)
            if len(p) < 5:
                continue
            idx    = int(p[0]) if p[0].isdigit() else 0
            wid    = p[1]
            name   = _tmux_text(p[2])
            active = p[3] == '1'
            layout = p[4]
            if active:
                active_window_idx = idx
                active_layout = layout
            windows.append({
                'index': idx,
                'id': wid,
                'name': name,
                'active': active,
                'layout': layout,
            })

        pane_target = self.session
        if active_window_idx is not None:
            pane_target = f'{self.session}:{active_window_idx}'
        pane_raw = await self.send_command(
            f'list-panes -t {_tmux_quote(pane_target)} -F'
            ' "#{pane_id}|#{pane_active}|#{pane_width}|#{pane_height}|#{pane_current_command}"'
        )
        panes, active_pane = [], ''
        for line in pane_raw.splitlines():
            p = line.split('|', 4)
            if len(p) < 5:
                continue
            pid    = p[0]
            active = p[1] == '1'
            cols   = int(p[2]) if p[2].isdigit() else 80
            rows   = int(p[3]) if p[3].isdigit() else 24
            cmd    = p[4]
            if active:
                active_pane = pid
            panes.append({'id': pid, 'active': active, 'cols': cols, 'rows': rows, 'command': cmd})

        return {
            'session':     self.session,
            'sessions':    sessions,
            'windows':     windows,
            'panes':       panes,
            'active_pane': active_pane,
            'layout':      active_layout,
        }

    # ──────────────────────────────────────── event parsing

    def _handle_notification_line(self, line: str) -> bool:
        if line.startswith('%extended-output '):
            # tmux may emit: %extended-output <pane> <age> <vis-encoded-bytes>
            # Treat it the same as %output.
            parts = line.split(' ', 3)
            if len(parts) >= 4:
                pane_id = parts[1]
                data, rem = _decode_output_stream(
                    parts[3],
                    self._decode_remainder.get(pane_id, ''),
                )
                if rem:
                    self._decode_remainder[pane_id] = rem
                elif pane_id in self._decode_remainder:
                    del self._decode_remainder[pane_id]
                if not data:
                    return True
                self._broadcast({
                    'type': 'output',
                    'pane': pane_id,
                    'data': base64.b64encode(data).decode('ascii'),
                })
            return True
        if line.startswith('%output '):
            parts = line.split(' ', 2)
            if len(parts) >= 3:
                pane_id = parts[1]
                data, rem = _decode_output_stream(
                    parts[2],
                    self._decode_remainder.get(pane_id, ''),
                )
                if rem:
                    self._decode_remainder[pane_id] = rem
                elif pane_id in self._decode_remainder:
                    del self._decode_remainder[pane_id]
                if not data:
                    return True
                self._broadcast({
                    'type': 'output',
                    'pane': pane_id,
                    'data': base64.b64encode(data).decode('ascii'),
                })
            return True
        if line.startswith('%layout-change '):
            parts = line.split(' ', 4)
            if len(parts) >= 3:
                self._broadcast({
                    'type':   'layout_change',
                    'target': parts[1],
                    'layout': parts[3] if len(parts) >= 4 else parts[2],
                })
            return True
        if line.startswith('%window-add '):
            self._broadcast({'type': 'window_add',    'target': line[12:]})
            return True
        if line.startswith('%window-close '):
            self._broadcast({'type': 'window_close',  'target': line[14:]})
            return True
        if line.startswith('%window-renamed '):
            p = line.split(' ', 2)
            self._broadcast({
                'type':   'window_renamed',
                'target': p[1] if len(p) > 1 else '',
                'name':   p[2] if len(p) > 2 else '',
            })
            return True
        if line.startswith('%session-window-changed '):
            p = line.split(' ', 2)
            self._broadcast({
                'type':    'session_window_changed',
                'session': p[1] if len(p) > 1 else '',
                'window':  p[2] if len(p) > 2 else '',
            })
            return True
        if line.startswith('%session-changed '):
            p = line.split(' ', 2)
            self._broadcast({
                'type':    'session_changed',
                'session': p[2] if len(p) > 2 else '',
                'target':  p[1] if len(p) > 1 else '',
            })
            return True
        if line.startswith('%pane-mode-changed '):
            self._broadcast({'type': 'pane_mode_changed', 'pane': line[19:].strip()})
            return True
        if line.startswith('%pane-focus-in '):
            self._broadcast({'type': 'focus', 'pane': line[15:].strip()})
            return True
        if line.startswith('%window-pane-changed '):
            p = line.split(' ', 2)
            self._broadcast({
                'type':   'window_pane_changed',
                'window': p[1] if len(p) > 1 else '',
                'pane':   p[2] if len(p) > 2 else '',
            })
            return True
        if line.startswith('%sessions-changed'):
            self._broadcast({'type': 'sessions_changed'})
            return True
        if line.startswith('%exit'):
            log.info('tmux session exited')
            self._schedule_restart()
            return True
        return False

    def _handle_line(self, line: str) -> None:
        # ── response demux ──
        if line.startswith('%begin '):
            self._in_resp, self._cur_resp = True, []
            return
        if line.startswith(('%end ', '%error ')):
            self._in_resp = False
            result = '\n'.join(self._cur_resp)
            if self._pending:
                fut = self._pending.pop(0)
                if not fut.done():
                    fut.set_result(result)
            self._cur_resp = []
            return
        if self._handle_notification_line(line):
            return
        if self._in_resp:
            self._cur_resp.append(line)
            return
        if line.startswith('%'):
            log.debug('unhandled control line: %s', line[:200])

    def _broadcast(self, msg: dict) -> None:
        if not self.subscribers:
            return
        data = json.dumps(msg)
        dead = []
        for ws in self.subscribers:
            try:
                asyncio.create_task(_safe_send(ws, data))
            except Exception:
                dead.append(ws)
        for ws in dead:
            self.subscribers.remove(ws)


# ──────────────────────────────────────────── helpers

def _set_winsize(fd: int, rows: int, cols: int) -> None:
    winsize = struct.pack('HHHH', rows, cols, 0, 0)
    fcntl.ioctl(fd, termios.TIOCSWINSZ, winsize)



async def _safe_send(ws, data: str) -> None:
    try:
        await ws.send(data)
    except Exception:
        pass
__WEBTMUX_EOF__

echo "  -> layout_parser.py"
cat > layout_parser.py <<'__WEBTMUX_EOF__'
#!/usr/bin/env python3
"""
Parse tmux layout strings into a flat list of pane geometries.

Layout string format:
  checksum,WxH,x,y,pane_id          (leaf pane)
  checksum,WxH,x,y{child,...}       (horizontal split, {})
  checksum,WxH,x,y[child,...]       (vertical split,   [])

All x/y coordinates are absolute within the terminal window.
"""
import re


def parse_layout(layout_str: str) -> list[dict]:
    """Return [{id: int, x, y, cols, rows}, ...] for each leaf pane."""
    idx = layout_str.find(',')
    if idx < 0:
        return []
    panes: list[dict] = []
    _parse_node(layout_str[idx + 1:], panes)
    return panes


def _parse_node(s: str, panes: list[dict]) -> None:
    m = re.match(r'(\d+)x(\d+),(\d+),(\d+)(.*)', s)
    if not m:
        return
    cols, rows = int(m.group(1)), int(m.group(2))
    x,    y    = int(m.group(3)), int(m.group(4))
    rest       = m.group(5)

    if not rest:
        return

    if rest[0] == ',':
        # Leaf: ,pane_id
        num = rest[1:].split(',')[0]
        if num.isdigit():
            panes.append({'id': int(num), 'x': x, 'y': y, 'cols': cols, 'rows': rows})
    elif rest[0] in ('{', '['):
        inner, _ = _extract_bracket(rest)
        for child in _split_children(inner):
            _parse_node(child, panes)


def _extract_bracket(s: str) -> tuple[str, str]:
    depth = 0
    for i, c in enumerate(s):
        if c in ('{', '['):
            depth += 1
        elif c in ('}', ']'):
            depth -= 1
            if depth == 0:
                return s[1:i], s[i + 1:]
    return s[1:], ''


def _split_children(s: str) -> list[str]:
    # Split only at commas that are followed by a new node header (\d+x\d+),
    # not at commas that are part of a leaf's (x, y, pane_id) sequence.
    parts, depth, start = [], 0, 0
    i = 0
    while i < len(s):
        c = s[i]
        if c in ('{', '['):
            depth += 1
        elif c in ('}', ']'):
            depth -= 1
        elif c == ',' and depth == 0 and re.match(r'\d+x\d+', s[i + 1:]):
            parts.append(s[start:i])
            start = i + 1
        i += 1
    if start < len(s):
        parts.append(s[start:])
    return parts


if __name__ == '__main__':
    # Quick smoke test
    cases = [
        '5963,80x24,0,0,14',
        '7b1a,80x24,0,0{40x24,0,0,14,39x24,41,0,15}',
        'abcd,80x24,0,0[80x12,0,0,0,80x11,0,13{40x11,0,13,1,39x11,41,13,2}]',
    ]
    for c in cases:
        print(c)
        for p in parse_layout(c):
            print(' ', p)
__WEBTMUX_EOF__

echo "  -> static/index.html"
cat > static/index.html <<'__WEBTMUX_EOF__'
<!DOCTYPE html>
<html lang="ja">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
  <title>web-tmux</title>
  <link rel="stylesheet"
        href="https://cdn.jsdelivr.net/npm/xterm@5.3.0/css/xterm.css">
  <link rel="stylesheet" href="style.css">
</head>
<body>
  <div id="app">

    <!-- ── Top Bar (always visible) ─────────────────────────── -->
    <div id="topbar">
      <button id="hamburger" type="button" aria-label="toggle sidebar">☰</button>
      <div id="session-name">—</div>
      <div id="topbar-spacer"></div>
      <div id="topbar-actions">
        <button id="scroll-up-half" class="topbar-btn" type="button" aria-label="Scroll up" title="Scroll up half page">
          <svg viewBox="0 0 24 24" aria-hidden="true">
            <path d="M12 4l6 8H6z" fill="currentColor"/>
            <rect x="6" y="17" width="12" height="2" rx="1" fill="currentColor"/>
          </svg>
        </button>
        <button id="scroll-down-half" class="topbar-btn" type="button" aria-label="Scroll down" title="Scroll down half page">
          <svg viewBox="0 0 24 24" aria-hidden="true">
            <rect x="6" y="5" width="12" height="2" rx="1" fill="currentColor"/>
            <path d="M12 20l-6-8h12z" fill="currentColor"/>
          </svg>
        </button>
        <button id="clipboard-toggle" class="topbar-btn" type="button" aria-label="Clipboard" title="Clipboard">
          <svg viewBox="0 0 24 24" aria-hidden="true">
            <rect x="7" y="4" width="10" height="15" rx="2" fill="none" stroke="currentColor" stroke-width="1.6"/>
            <rect x="9" y="2.5" width="6" height="3.5" rx="1.5" fill="none" stroke="currentColor" stroke-width="1.6"/>
          </svg>
        </button>
      </div>
      <div id="status" class="disconnected" title="disconnected">●</div>
    </div>

    <!-- ── Middle: Sidebar + Pane Area ──────────────────────── -->
    <div id="middle">
      <div id="sidebar-backdrop"></div>
      <div id="sidebar">
        <div class="side-section">
          <div class="side-head">
            <span>sessions</span>
            <button class="side-btn" id="btn-new-session" title="new session">+</button>
          </div>
          <div id="session-list"></div>
        </div>
        <div class="side-section">
          <div class="side-head">
            <span>windows</span>
            <button class="side-btn" id="btn-new-window" title="new window">+</button>
          </div>
          <div id="window-list"></div>
        </div>
        <div class="side-section">
          <div class="side-head">
            <span>panes</span>
            <button class="side-btn" id="btn-split-h" title="split horizontally">⇿</button>
            <button class="side-btn" id="btn-split-v" title="split vertically">⇕</button>
          </div>
          <div id="pane-list"></div>
        </div>
      </div>
      <div id="main">
        <div id="pane-area"></div>
      </div>
    </div>

    <!-- ── Bottom Bar (≤768px width only) ───────────────────── -->
    <div id="bottombar">
      <button class="vkey icon-btn key-wide" data-key="esc" type="button" aria-label="Escape" title="Escape">
        <svg viewBox="0 0 40 24" aria-hidden="true">
          <rect x="2.5" y="3.5" width="35" height="17" rx="4.5" fill="none" stroke="currentColor" stroke-width="1.5"/>
          <text x="20" y="15.2" text-anchor="middle">ESC</text>
        </svg>
      </button>
      <button id="ctrl-toggle" class="icon-btn key-wide" type="button" aria-label="Control" title="Control">
        <svg viewBox="0 0 40 24" aria-hidden="true">
          <rect x="2.5" y="3.5" width="35" height="17" rx="4.5" fill="none" stroke="currentColor" stroke-width="1.5"/>
          <text x="20" y="15.2" text-anchor="middle">CTRL</text>
        </svg>
      </button>
      <button class="vkey icon-btn key-wide" data-key="tab" type="button" aria-label="Tab" title="Tab">
        <svg viewBox="0 0 32 24" aria-hidden="true">
          <path d="M6 6v12" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round"/>
          <path d="M9 12h13" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round"/>
          <path d="M18 8l4 4-4 4" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"/>
        </svg>
      </button>
      <button class="vkey icon-btn key-wide" data-key="enter" type="button" aria-label="Enter" title="Enter">
        <svg viewBox="0 0 32 24" aria-hidden="true">
          <path d="M25 6v7H10" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"/>
          <path d="M14 9l-4 4 4 4" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"/>
        </svg>
      </button>
      <button class="vkey icon-btn" data-key="up" type="button" aria-label="Up" title="Up">
        <svg viewBox="0 0 24 24" aria-hidden="true">
          <path d="M12 6l6 9H6z" fill="currentColor"/>
        </svg>
      </button>
      <button class="vkey icon-btn" data-key="down" type="button" aria-label="Down" title="Down">
        <svg viewBox="0 0 24 24" aria-hidden="true">
          <path d="M12 18l-6-9h12z" fill="currentColor"/>
        </svg>
      </button>
      <button class="vkey icon-btn" data-key="left" type="button" aria-label="Left" title="Left">
        <svg viewBox="0 0 24 24" aria-hidden="true">
          <path d="M6 12l9-6v12z" fill="currentColor"/>
        </svg>
      </button>
      <button class="vkey icon-btn" data-key="right" type="button" aria-label="Right" title="Right">
        <svg viewBox="0 0 24 24" aria-hidden="true">
          <path d="M18 12l-9 6V6z" fill="currentColor"/>
        </svg>
      </button>
    </div>

    <div id="clipboard-sheet" class="sheet hidden" aria-hidden="true">
      <div class="sheet-backdrop"></div>
      <div class="sheet-card" role="dialog" aria-modal="true" aria-labelledby="clipboard-title">
        <div class="sheet-head">
          <div id="clipboard-title">Clipboard</div>
          <button id="clipboard-close" class="sheet-close" type="button" aria-label="Close clipboard">×</button>
        </div>
        <label class="sheet-label" for="clipboard-copy-text">Copy text</label>
        <textarea id="clipboard-copy-text" readonly spellcheck="false"></textarea>
        <label class="sheet-label" for="clipboard-paste-text">Paste text</label>
        <textarea id="clipboard-paste-text" spellcheck="false" autocapitalize="off" autocomplete="off" autocorrect="off" placeholder="Paste here, then send to terminal"></textarea>
        <div class="sheet-actions">
          <button id="clipboard-send" type="button">Send</button>
          <button id="clipboard-copy-all" type="button">Copy all</button>
        </div>
      </div>
    </div>

  </div>

  <script src="https://cdn.jsdelivr.net/npm/xterm@5.3.0/lib/xterm.js"></script>
  <script src="https://cdn.jsdelivr.net/npm/xterm-addon-fit@0.8.0/lib/xterm-addon-fit.js"></script>
  <script src="https://cdn.jsdelivr.net/npm/xterm-addon-unicode11@0.4.0/lib/xterm-addon-unicode11.js"></script>
  <script src="app.js"></script>
</body>
</html>
__WEBTMUX_EOF__

echo "  -> static/style.css"
cat > static/style.css <<'__WEBTMUX_EOF__'
*, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }

html, body {
  height: 100%;
  background: #1e1e1e;
  color: #ccc;
  font-family: "SF Mono", Menlo, "Cascadia Code", "Fira Code", monospace;
  font-size: 13px;
  overflow: hidden;
  -webkit-tap-highlight-color: transparent;
}

#app {
  display: flex;
  flex-direction: column;
  /* 100vh on iOS returns the LARGEST viewport (URL bar hidden), which is
     bigger than the actual visible area on initial load. 100dvh tracks the
     dynamic viewport, so the layout always fits. JS (applyViewportFix) pins
     the height in px on browsers that don't support dvh. */
  height: 100vh;
  height: 100dvh;
}

/* ── Top Bar ─────────────────────────────────────── */
#topbar {
  flex: 0 0 auto;
  display: flex;
  align-items: center;
  gap: 12px;
  padding: 4px 8px;
  background: #252525;
  border-bottom: 1px solid #333;
  min-height: 40px;
}

#hamburger {
  width: 36px;
  height: 32px;
  font-size: 20px;
  line-height: 1;
  color: #ccc;
  background: transparent;
  border: 1px solid #444;
  border-radius: 4px;
  cursor: pointer;
  touch-action: manipulation;
  font-family: inherit;
}
#hamburger:hover  { background: #2e2e2e; color: #fff; }
#hamburger:active { background: #383838; }

#session-name {
  color: #4af;
  font-weight: bold;
  white-space: nowrap;
}

#topbar-spacer { flex: 1; }

#topbar-actions {
  display: none;
  align-items: center;
  gap: 6px;
}

.topbar-btn {
  width: 30px;
  height: 30px;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  color: #cfcfcf;
  background: #2f2f2f;
  border: 1px solid #454545;
  border-radius: 7px;
  cursor: pointer;
  touch-action: manipulation;
}

.topbar-btn svg {
  width: 16px;
  height: 16px;
  display: block;
}

.topbar-btn:hover  { background: #383838; color: #fff; }
.topbar-btn:active { background: #414141; }

/* ── Middle (sidebar + main) ─────────────────────── */
#middle {
  flex: 1 1 auto;
  display: flex;
  min-height: 0;
  position: relative;
}

/* ── Sidebar ─────────────────────────────────────── */
#sidebar {
  width: 240px;
  min-width: 240px;
  background: #252525;
  border-right: 1px solid #333;
  overflow-x: hidden;
  overflow-y: auto;
  display: flex;
  flex-direction: column;
  transition: width .15s ease, transform .2s ease;
}

#sidebar.closed {
  width: 0;
  min-width: 0;
  border-right: 0;
}

.side-section {
  border-bottom: 1px solid #2a2a2a;
  flex: 0 0 auto;
}
.side-section.side-footer {
  margin-top: auto;
  border-bottom: 0;
  padding: 8px;
}

.side-head {
  display: flex;
  align-items: center;
  gap: 4px;
  padding: 8px 10px 4px;
  font-size: 10px;
  color: #666;
  text-transform: uppercase;
  letter-spacing: .05em;
}
.side-head > span { flex: 1; }

.side-btn {
  min-width: 22px;
  height: 22px;
  padding: 0 6px;
  background: #21443a;
  color: #d7f5ea;
  border: 1px solid #2d6a58;
  border-radius: 3px;
  cursor: pointer;
  font-family: inherit;
  font-size: 12px;
  line-height: 1;
}
.side-btn:hover  { background: #295545; color: #fff; }
.side-btn:active { background: #316654; }
.side-btn.wide   { width: 100%; height: 28px; }

#session-list, #window-list, #pane-list {
  max-height: 40vh;
  overflow-y: auto;
}

.session-item {
  padding: 8px 10px;
  cursor: pointer;
  display: flex;
  align-items: center;
  gap: 6px;
  border-left: 2px solid transparent;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}
.session-item:hover  { background: #2e2e2e; }
.session-item.active { border-left-color: #4af; color: #fff; }
.session-item.editing { cursor: default; }
.session-item:focus {
  outline: 1px solid #4af;
  outline-offset: -1px;
  background: #303030;
}

.pane-item {
  padding: 8px 10px;
  cursor: pointer;
  display: flex;
  align-items: center;
  gap: 6px;
  border-left: 2px solid transparent;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
  font-size: 12px;
}
.pane-item:hover  { background: #2e2e2e; }
.pane-item.active { border-left-color: #4af; color: #fff; }
.pane-item:focus {
  outline: 1px solid #4af;
  outline-offset: -1px;
  background: #303030;
}
.pane-id  { color: #666; min-width: 32px; font-size: 11px; }
.pane-cmd { color: #ccc; overflow: hidden; text-overflow: ellipsis; }

.window-item {
  padding: 8px 10px;
  cursor: pointer;
  display: flex;
  align-items: center;
  gap: 6px;
  border-left: 2px solid transparent;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}
.window-item:hover  { background: #2e2e2e; }
.window-item.active { border-left-color: #4af; color: #fff; }
.window-item.editing { cursor: default; }
.window-item:focus {
  outline: 1px solid #4af;
  outline-offset: -1px;
  background: #303030;
}

.window-idx {
  color: #666;
  font-size: 11px;
  min-width: 14px;
}

.window-name {
  color: #ccc;
  overflow: hidden;
  text-overflow: ellipsis;
}

.session-name {
  color: #ccc;
  overflow: hidden;
  text-overflow: ellipsis;
}

.item-edit-btn {
  margin-left: auto;
  width: 22px;
  min-width: 22px;
  height: 22px;
  padding: 0;
  background: #303030;
  color: #a8a8a8;
  border: 1px solid #3f3f3f;
  border-radius: 3px;
  font-family: inherit;
  font-size: 13px;
  line-height: 1;
  cursor: pointer;
  flex: 0 0 auto;
}

.item-edit-btn:hover {
  background: #3a3a3a;
  color: #fff;
}

.item-inline-editor {
  width: 100%;
  display: flex;
  align-items: center;
  gap: 6px;
}

.item-inline-input {
  min-width: 0;
  flex: 1 1 auto;
  height: 24px;
  padding: 0 7px;
  color: #e6e6e6;
  background: #1f1f1f;
  border: 1px solid #4a4a4a;
  border-radius: 4px;
  font-family: inherit;
  font-size: 12px;
}

.item-inline-input:focus {
  outline: 1px solid #4af;
  border-color: #4af;
}

.item-inline-btn {
  height: 22px;
  padding: 0 6px;
  background: #2f4b2f;
  color: #d7f5d7;
  border: 1px solid #426742;
  border-radius: 3px;
  font-family: inherit;
  font-size: 10px;
  line-height: 1;
  cursor: pointer;
  flex: 0 0 auto;
}

.item-inline-btn.secondary {
  background: #343434;
  color: #cfcfcf;
  border-color: #474747;
}

/* ── Backdrop (mobile drawer overlay) ────────────── */
#sidebar-backdrop {
  display: none;
  position: absolute;
  inset: 0;
  background: rgba(0,0,0,.4);
  z-index: 19;
}

/* ── Main terminal area ──────────────────────────── */
#main {
  flex: 1;
  position: relative;
  overflow: hidden;
  min-width: 0;
}

#pane-area {
  position: absolute;
  inset: 0;
}

.pane-wrap {
  position: absolute;
  overflow: hidden;
  outline: 1px solid #2a2a2a;
}
.pane-wrap.active { outline: 1px solid #4af4; }

.xterm           { height: 100%; }
.xterm-viewport  { overflow: hidden !important; overscroll-behavior: contain; }

/* ── Bottom Bar (virtual keys, mobile only) ──────── */
#bottombar {
  flex: 0 0 auto;
  display: none;          /* shown only on ≤768px (see media query) */
  gap: 4px;
  padding: 6px;
  background: #2a2a2a;
  border-top: 1px solid #333;
  overflow-x: auto;
  flex-wrap: nowrap;
}

#bottombar button {
  flex: 0 0 auto;
  min-width: 46px;
  height: 40px;
  padding: 0 8px;
  color: #d4d4d4;
  background: linear-gradient(180deg, #404040 0%, #343434 100%);
  border: 1px solid #4d4d4d;
  border-radius: 8px;
  font-family: inherit;
  font-size: 13px;
  cursor: pointer;
  touch-action: manipulation;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  box-shadow: inset 0 1px 0 rgba(255,255,255,.05);
}
#bottombar button:active        { background: linear-gradient(180deg, #4a4a4a 0%, #3f3f3f 100%); }
#bottombar button.active        { background: #4af; color: #000; border-color: #4af; box-shadow: none; }

#bottombar .icon-btn.key-wide { min-width: 48px; }

#bottombar .icon-btn svg {
  width: 20px;
  height: 20px;
  display: block;
  overflow: visible;
}

#bottombar .icon-btn.key-wide svg {
  width: 30px;
  height: 22px;
}

#bottombar .icon-btn text {
  fill: currentColor;
  font-family: inherit;
  font-size: 8px;
  font-weight: 700;
  letter-spacing: .08em;
  text-transform: uppercase;
}

.sheet {
  position: absolute;
  inset: 0;
  z-index: 40;
}

.sheet.hidden {
  display: none;
}

.sheet-backdrop {
  position: absolute;
  inset: 0;
  background: rgba(0,0,0,.5);
}

.sheet-card {
  position: absolute;
  left: 12px;
  right: 12px;
  bottom: 12px;
  max-height: min(76dvh, 560px);
  display: flex;
  flex-direction: column;
  gap: 10px;
  padding: 14px;
  background: #252525;
  border: 1px solid #3c3c3c;
  border-radius: 14px;
  box-shadow: 0 18px 48px rgba(0,0,0,.35);
}

.sheet-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 10px;
  color: #f2f2f2;
  font-size: 14px;
  font-weight: 700;
}

.sheet-close {
  width: 32px;
  height: 32px;
  border: 1px solid #4a4a4a;
  border-radius: 8px;
  background: #333;
  color: #ddd;
  font-size: 18px;
  line-height: 1;
  cursor: pointer;
}

.sheet-label {
  color: #9a9a9a;
  font-size: 11px;
  font-weight: 700;
  letter-spacing: .08em;
  text-transform: uppercase;
}

.sheet textarea {
  width: 100%;
  min-height: 108px;
  resize: none;
  padding: 10px 12px;
  border: 1px solid #404040;
  border-radius: 10px;
  background: #171717;
  color: #e5e5e5;
  font: inherit;
  line-height: 1.45;
  -webkit-user-select: text;
  user-select: text;
}

.sheet textarea[readonly] {
  background: #151515;
}

.sheet-actions {
  display: flex;
  gap: 8px;
}

.sheet-actions button {
  flex: 1 1 0;
  height: 38px;
  border: 1px solid #4a4a4a;
  border-radius: 10px;
  background: #343434;
  color: #ececec;
  font: inherit;
  cursor: pointer;
}

/* ── Status indicator ────────────────────────────── */
#status {
  font-size: 12px;
  color: #444;
  pointer-events: none;
}
#status.connected    { color: #4a4; }
#status.disconnected { color: #a44; }

/* ── Mobile breakpoint (≤768px) ──────────────────── */
@media (max-width: 768px) {
  #sidebar {
    position: absolute;
    top: 0; bottom: 0; left: 0;
    z-index: 20;
    width: 160px;
    min-width: 160px;
    transform: translateX(-100%);
    transition: transform .2s ease;
  }
  #sidebar.open {
    transform: translateX(0);
  }
  #sidebar.closed {
    width: 160px;
    min-width: 160px;
    border-right: 1px solid #333;
    transform: translateX(-100%);
  }
  #sidebar-backdrop.visible {
    display: block;
  }
  #bottombar {
    display: flex;
  }

  #topbar-actions {
    display: flex;
  }

  .side-head {
    padding: 7px 8px 4px;
    font-size: 9px;
  }

  .side-btn {
    min-width: 20px;
    height: 20px;
    padding: 0 5px;
    font-size: 11px;
  }

  .session-item,
  .window-item,
  .pane-item {
    padding: 7px 8px;
    font-size: 11px;
  }

  .pane-id,
  .window-idx {
    font-size: 10px;
  }

  .session-name,
  .window-name,
  .pane-cmd {
    font-size: 11px;
  }

  .item-edit-btn {
    width: 20px;
    min-width: 20px;
    height: 20px;
    font-size: 12px;
  }

  .item-inline-input {
    height: 22px;
    padding: 0 6px;
    font-size: 11px;
  }

  .item-inline-btn {
    height: 20px;
    padding: 0 5px;
    font-size: 9px;
  }

  .pane-wrap.active .xterm-viewport {
    overflow-y: auto !important;
    overflow-x: hidden !important;
    -webkit-overflow-scrolling: touch;
    touch-action: pan-y;
  }

  /* Pane zoom: show only the active pane, full-screen */
  .pane-wrap {
    display: none !important;
  }
  .pane-wrap.active {
    display: block !important;
    left:   0    !important;
    top:    0    !important;
    width:  100% !important;
    height: 100% !important;
    outline: none;
  }
}
__WEBTMUX_EOF__

echo "  -> static/app.js"
cat > static/app.js <<'__WEBTMUX_EOF__'
'use strict';

// WebSocket URL: when served over HTTPS (e.g. Tailscale serve), use wss://.
const WS_URL = `${location.protocol === 'https:' ? 'wss' : 'ws'}://${location.hostname}:8765`;
const FONT_FAMILY = '"SF Mono", Menlo, "Cascadia Code", "Fira Code", monospace';
const MOBILE_BP   = 768;
const CLIENT_PREFIX_KEY = '\x01'; // Ctrl+A, matching this app's tmux setup.
const NON_ASCII_DUPLICATE_SUPPRESS_MS = 120;

const VIRTUAL_KEYS = {
  esc:   '\x1b',
  tab:   '\t',
  enter: '\r',
  up:    '\x1b[A',
  down:  '\x1b[B',
  left:  '\x1b[D',
  right: '\x1b[C',
};

const XTERM_THEME = {
  background:  '#1e1e1e',
  foreground:  '#d4d4d4',
  cursor:      '#aeafad',
  black:       '#1e1e1e', brightBlack:   '#808080',
  red:         '#f44747', brightRed:     '#f44747',
  green:       '#608b4e', brightGreen:   '#608b4e',
  yellow:      '#dcdcaa', brightYellow:  '#dcdcaa',
  blue:        '#569cd6', brightBlue:    '#569cd6',
  magenta:     '#c586c0', brightMagenta: '#c586c0',
  cyan:        '#4ec9b0', brightCyan:    '#4ec9b0',
  white:       '#d4d4d4', brightWhite:   '#d4d4d4',
};

// ─── State ────────────────────────────────────────────────────────────────────

let ws             = null;
let panes          = {};     // pane_id → { term, fitAddon, el }
let activePaneId   = null;
let currentSession = '';     // tmux session currently displayed
let currentWinIdx  = 0;      // tmux window index currently displayed
let currentWinId   = '';     // tmux window id currently displayed, e.g. @42
let totalCols      = 80;
let totalRows      = 24;

// Remembered layout — used by the window-resize handler to re-flow panes
let _currentLayoutStr   = '';
let _currentLayoutPanes = [];

// Debounce: prevent positionPanes from being called too frequently
let _layoutRafId   = null;
let _pendingLayout = null;

function scheduleLayout(lp, layoutStr) {
  _pendingLayout = { lp, layoutStr };
  if (_layoutRafId) cancelAnimationFrame(_layoutRafId);
  _layoutRafId = requestAnimationFrame(() => {
    _layoutRafId = null;
    if (_pendingLayout) {
      positionPanes(_pendingLayout.lp, _pendingLayout.layoutStr);
      _pendingLayout = null;
    }
  });
}

// Only the active tab/device should drive tmux's real size. This matters when a
// phone and desktop are both open: tmux has one shared window size.
let _clientActive = false;
let _resizeSendTimer = null;
let _pendingResize = null;
let _snapshotRefreshTimer = null;
let _currentViewRefreshTimer = null;
let _layoutApplying = false;
let _heldClientPrefix = false;
let _heldClientPrefixPaneId = null;
let _heldClientPrefixTimer = null;
let _editingSessionName = '';
let _editingWindowIndex = null;
const _pendingSnapshotPanes = new Set();
const _bufferedPaneOutput = new Map();
let _lastViewportSize = { width: 0, height: 0 };
const TMUX_COL_SAFETY_MARGIN = 0;

function markClientActive() {
  _clientActive = true;
  if (ws && ws.readyState === WebSocket.OPEN) {
    ws.send(JSON.stringify({ type: 'client_active' }));
  }
}

function refitCurrentLayout() {
  resetResizeCache();
  if (_currentLayoutPanes.length === 1) {
    positionSinglePane('%' + _currentLayoutPanes[0].id);
  } else if (_currentLayoutPanes.length > 1) {
    scheduleLayout(_currentLayoutPanes, _currentLayoutStr);
  }
}

function activateClient() {
  markClientActive();
  refitCurrentLayout();
}

function validResize(cols, rows) {
  return Number.isFinite(cols) && Number.isFinite(rows) && cols >= 10 && rows >= 5;
}

// Only send resize when the size actually changed (avoids feedback loops)
let _lastResize = { cols: 0, rows: 0 };
function maybeSendResize(cols, rows) {
  cols = Math.floor(cols) - TMUX_COL_SAFETY_MARGIN;
  rows = Math.floor(rows);
  if (cols < 10) cols = 10;
  if (!_clientActive || document.visibilityState === 'hidden') return;
  if (!validResize(cols, rows)) return;
  if (cols === _lastResize.cols && rows === _lastResize.rows) {
    if (_pendingSnapshotPanes.size > 0) {
      scheduleSnapshotRefresh([..._pendingSnapshotPanes]);
    }
    return;
  }
  _pendingResize = { cols, rows };
  if (_resizeSendTimer) clearTimeout(_resizeSendTimer);
  _resizeSendTimer = setTimeout(() => {
    _resizeSendTimer = null;
    if (!_pendingResize) return;
    const next = _pendingResize;
    _pendingResize = null;
    if (next.cols === _lastResize.cols && next.rows === _lastResize.rows) {
      if (_pendingSnapshotPanes.size > 0) {
        scheduleSnapshotRefresh([..._pendingSnapshotPanes]);
      }
      return;
    }
    if (!_clientActive || document.visibilityState === 'hidden') return;
    _lastResize = next;
    if (ws && ws.readyState === WebSocket.OPEN) {
      ws.send(JSON.stringify({ type: 'resize', cols: next.cols, rows: next.rows }));
      if (_pendingSnapshotPanes.size > 0) {
        scheduleSnapshotRefresh([..._pendingSnapshotPanes]);
      }
    }
  }, 80);
}
function resetResizeCache() { _lastResize = { cols: 0, rows: 0 }; }

function getActivePane() {
  if (activePaneId && panes[activePaneId]) return panes[activePaneId];
  const firstPaneId = Object.keys(panes)[0];
  return firstPaneId ? panes[firstPaneId] : null;
}

function sendPaneInput(data, paneId) {
  if (!data || !ws || ws.readyState !== WebSocket.OPEN) return;
  const targetPaneId = paneId || activePaneId || Object.keys(panes)[0];
  if (!targetPaneId) return;
  markClientActive();
  ws.send(JSON.stringify({ type: 'input', pane: targetPaneId, data }));
}

function flushHeldClientPrefix() {
  if (!_heldClientPrefix) return;
  const paneId = _heldClientPrefixPaneId;
  _heldClientPrefix = false;
  _heldClientPrefixPaneId = null;
  if (_heldClientPrefixTimer) {
    clearTimeout(_heldClientPrefixTimer);
    _heldClientPrefixTimer = null;
  }
  sendPaneInput(CLIENT_PREFIX_KEY, paneId);
}

function focusSidebarChooser(listId, itemSelector) {
  setSidebarOpen(true);
  const list = document.getElementById(listId);
  const target = list.querySelector(`${itemSelector}.active`) || list.querySelector(itemSelector);
  if (target) {
    target.focus();
    target.scrollIntoView({ block: 'nearest' });
  }
}

function focusSessionChooser() {
  focusSidebarChooser('session-list', '.session-item');
}

function focusWindowChooser() {
  focusSidebarChooser('window-list', '.window-item');
}

function focusPaneChooser() {
  focusSidebarChooser('pane-list', '.pane-item');
}

function handleClientPrefixShortcut(key, paneId) {
  if (key === 's') {
    focusSessionChooser();
    return true;
  }
  if (key === 'w') {
    focusWindowChooser();
    return true;
  }
  if (key === 'q') {
    focusPaneChooser();
    return true;
  }
  sendPaneInput(CLIENT_PREFIX_KEY + key, paneId);
  return true;
}

function handleTerminalInput(data, paneId) {
  if (!data) return;

  if (_heldClientPrefix) {
    if (_heldClientPrefixTimer) {
      clearTimeout(_heldClientPrefixTimer);
      _heldClientPrefixTimer = null;
    }
    const prefixPaneId = _heldClientPrefixPaneId || paneId;
    _heldClientPrefix = false;
    _heldClientPrefixPaneId = null;
    if (data.length === 1 && handleClientPrefixShortcut(data, prefixPaneId)) return;
    sendPaneInput(CLIENT_PREFIX_KEY + data, prefixPaneId);
    return;
  }

  if (data.startsWith(CLIENT_PREFIX_KEY) && data.length > 1) {
    if (data[1] === 's') {
      focusSessionChooser();
      if (data.length > 2) sendPaneInput(data.slice(2), paneId);
      return;
    }
    if (data[1] === 'w') {
      focusWindowChooser();
      if (data.length > 2) sendPaneInput(data.slice(2), paneId);
      return;
    }
    if (data[1] === 'q') {
      focusPaneChooser();
      if (data.length > 2) sendPaneInput(data.slice(2), paneId);
      return;
    }
    sendPaneInput(data, paneId);
    return;
  }

  if (data === CLIENT_PREFIX_KEY) {
    _heldClientPrefix = true;
    _heldClientPrefixPaneId = paneId;
    _heldClientPrefixTimer = setTimeout(() => {
      _heldClientPrefixTimer = null;
      flushHeldClientPrefix();
    }, 700);
    return;
  }

  sendPaneInput(data, paneId);
}

function shouldPreventBrowserCtrlShortcut(ev, textarea) {
  if (!ev || ev.type !== 'keydown') return false;
  if (!textarea) return false;
  if (!ev.ctrlKey || ev.metaKey || ev.altKey) return false;
  return true;
}

function loadUnicode11Addon(term) {
  const addonCtor = window.Unicode11Addon && window.Unicode11Addon.Unicode11Addon;
  if (!addonCtor) return;

  try {
    term.loadAddon(new addonCtor());
    term.unicode.activeVersion = '11';
  } catch (e) {
    console.warn('unicode11 addon failed to load', e);
  }
}

function hasNonAsciiText(data) {
  return /[^\x00-\x7f]/.test(data);
}

function createInputDeduper() {
  return { data: '', at: 0 };
}

function shouldSuppressDuplicateTextInput(deduper, data) {
  if (!deduper || !data || !hasNonAsciiText(data)) return false;

  const now = Date.now();
  if (data === deduper.data && now - deduper.at <= NON_ASCII_DUPLICATE_SUPPRESS_MS) {
    deduper.data = '';
    deduper.at = 0;
    return true;
  }

  deduper.data = data;
  deduper.at = now;
  return false;
}

function scheduleSnapshotRefresh(paneIds) {
  const ids = paneIds && paneIds.length ? [...paneIds] : Object.keys(panes);
  if (_snapshotRefreshTimer) clearTimeout(_snapshotRefreshTimer);
  _snapshotRefreshTimer = setTimeout(() => {
    _snapshotRefreshTimer = null;
    if (!ws || ws.readyState !== WebSocket.OPEN) return;
    ids.forEach((paneId) => {
      if (!panes[paneId]) return;
      _pendingSnapshotPanes.add(paneId);
      ws.send(JSON.stringify({ type: 'get_snapshot', pane: paneId }));
    });
  }, 260);
}

function markSnapshotPending(paneIds) {
  (paneIds || []).forEach((paneId) => {
    if (paneId) _pendingSnapshotPanes.add(paneId);
  });
}

function scheduleCurrentViewRefresh() {
  if (_currentViewRefreshTimer) clearTimeout(_currentViewRefreshTimer);
  _currentViewRefreshTimer = setTimeout(() => {
    _currentViewRefreshTimer = null;
    if (ws && ws.readyState === WebSocket.OPEN) {
      ws.send(JSON.stringify({ type: 'get_current_view' }));
    }
  }, 80);
}

function queuePaneOutput(paneId, data) {
  if (!paneId || !data || data.length === 0) return;
  const queued = _bufferedPaneOutput.get(paneId);
  if (queued) {
    queued.push(data);
  } else {
    _bufferedPaneOutput.set(paneId, [data]);
  }
}

function drainBufferedOutput(paneId) {
  const p = panes[paneId];
  if (!p) {
    _bufferedPaneOutput.delete(paneId);
    _pendingSnapshotPanes.delete(paneId);
    return;
  }
  const queued = _bufferedPaneOutput.get(paneId);
  if (!queued || queued.length === 0) {
    _bufferedPaneOutput.delete(paneId);
    _pendingSnapshotPanes.delete(paneId);
    return;
  }
  _bufferedPaneOutput.delete(paneId);
  const data = queued.length === 1 ? queued[0] : concatBytes(queued);
  p.term.write(data, () => drainBufferedOutput(paneId));
}

function updateCurrentWindow(windows) {
  const activeWin = (windows || []).find(w => w.active);
  if (!activeWin) return;
  currentWinIdx = activeWin.index;
  currentWinId = activeWin.id || '';
}

// ─── WebSocket ────────────────────────────────────────────────────────────────

function connect() {
  ws = new WebSocket(WS_URL);

  ws.onopen = () => {
    setStatus('connected');
    if (document.visibilityState !== 'hidden') {
      markClientActive();
    }
  };

  ws.onclose = () => {
    setStatus('disconnected');
    setTimeout(connect, 2000);
  };

  ws.onerror = () => setStatus('disconnected');

  ws.onmessage = (ev) => {
    try { handleMsg(JSON.parse(ev.data)); }
    catch (e) { console.error('parse error', e); }
  };
}

// ─── Message handlers ─────────────────────────────────────────────────────────

function handleMsg(msg) {
  switch (msg.type) {
    case 'init':            onInit(msg);           break;
    case 'window_switched': onWindowSwitched(msg); break;
    case 'snapshot':        onSnapshot(msg);        break;
    case 'output':          onOutput(msg);          break;
    case 'layout_change':   onLayoutChange(msg);   break;
    case 'focus':           onFocus(msg);           break;
    case 'session_changed':
    case 'session_window_changed':
      onCurrentViewChanged(msg);
      break;
    case 'pane_mode_changed': onPaneModeChanged(msg); break;
    case 'window_pane_changed': onWindowPaneChanged(msg); break;
    case 'window_add':
    case 'window_close':
    case 'window_renamed':  onWindowsChanged(msg); break;
    case 'sessions_changed': onSessionsChanged(msg); break;
    case 'state':           onState(msg);          break;
  }
}

function onState(msg) {
  if (msg.session) {
    currentSession = msg.session;
    document.getElementById('session-name').textContent = msg.session;
  }
  if (msg.sessions) renderSessionList(msg.sessions);
  if (msg.windows) {
    renderWindowList(msg.windows);
    updateCurrentWindow(msg.windows);
  }
  if (msg.panes)   renderPaneList(msg.panes, msg.active_pane);
  if (msg.active_pane) {
    if (panes[msg.active_pane]) {
      setActivePaneVisual(msg.active_pane);
    } else {
      activePaneId = msg.active_pane;
    }
  }
}

function onCurrentViewChanged(msg) {
  scheduleCurrentViewRefresh();
}

function onPaneModeChanged(msg) {
  const paneId = msg.pane || activePaneId;
  if (paneId) {
    markSnapshotPending([paneId]);
    scheduleSnapshotRefresh([paneId]);
    if (paneId === activePaneId) {
      focusActivePane({ defer: true, retries: 2 });
    }
  }
}

function onWindowPaneChanged(msg) {
  if (msg.pane) setActivePaneVisual(msg.pane);
  focusActivePane({ defer: true, retries: 3 });
  scheduleStateRefresh();
}

function onInit(msg) {
  resetResizeCache();
  currentSession = msg.session || '';
  document.getElementById('session-name').textContent = msg.session;
  renderSessionList(msg.sessions);
  renderWindowList(msg.windows);
  renderPaneList(msg.panes, msg.active_pane);
  updateCurrentWindow(msg.windows);
  applyLayout(msg.panes, msg.layout_panes, msg.layout, msg.active_pane);
  scheduleSnapshotRefresh((msg.panes || []).map((p) => p.id));
}

function onWindowSwitched(msg) {
  resetResizeCache();   // new window → terminal size may differ
  if (msg.session) {
    currentSession = msg.session;
    document.getElementById('session-name').textContent = msg.session;
  }
  renderSessionList(msg.sessions);
  renderWindowList(msg.windows);
  renderPaneList(msg.panes, msg.active_pane);
  updateCurrentWindow(msg.windows);
  destroyAllPanes();
  markSnapshotPending((msg.panes || []).map(p => p.id));
  applyLayout(msg.panes, msg.layout_panes, msg.layout, msg.active_pane);
  scheduleSnapshotRefresh((msg.panes || []).map((p) => p.id));
}

function onSnapshot(msg) {
  const p = panes[msg.pane];
  if (!p) {
    _bufferedPaneOutput.delete(msg.pane);
    _pendingSnapshotPanes.delete(msg.pane);
    return;
  }
  const frame = buildSnapshotFrame(msg, p.term);
  try { p.term.reset(); } catch (_) {}
  p.term.write(frame, () => drainBufferedOutput(msg.pane));
}

function onOutput(msg) {
  const p = panes[msg.pane];
  const data = b64ToUint8(msg.data);
  if (_pendingSnapshotPanes.has(msg.pane)) {
    queuePaneOutput(msg.pane, data);
    return;
  }
  if (p) p.term.write(data);
}

function onLayoutChange(msg) {
  if (!msg.layout) return;

  // tmux may send either "session:window_idx" or a window id such as "@42".
  if (msg.target) {
    if (msg.target.startsWith('@')) {
      if (currentWinId && msg.target !== currentWinId) return;
    } else {
      const sep = msg.target.lastIndexOf(':');
      const sessionName = sep >= 0 ? msg.target.slice(0, sep) : '';
      const winIdx = sep >= 0 ? parseInt(msg.target.slice(sep + 1), 10) : NaN;
      if (currentSession && sessionName && sessionName !== currentSession) return;
      if (!isNaN(winIdx) && winIdx !== currentWinIdx) return;
    }
  }

  const lp = parseLayout(msg.layout);
  if (lp.length === 0) return;
  _currentLayoutStr   = msg.layout;
  _currentLayoutPanes = lp;

  // Pane IDs now in layout
  const layoutIds = new Set(lp.map(p => '%' + p.id));

  // Destroy panes that disappeared
  Object.keys(panes).forEach(id => {
    if (!layoutIds.has(id)) destroyPane(id);
  });

  // Create panes that are new
  const newIds = [];
  lp.forEach(({ id, cols, rows }) => {
    const pid = '%' + id;
    if (!panes[pid]) {
      ensurePane(pid, cols, rows);
      newIds.push(pid);
    }
  });

  // Reposition — debounced to avoid rapid flickering
  if (lp.length === 1) {
    _layoutApplying = true;
    positionSinglePane('%' + lp[0].id);
  } else {
    _layoutApplying = true;
    scheduleLayout(lp, msg.layout);
  }

  const refreshIds = [...layoutIds];
  if (refreshIds.length > 0) {
    markSnapshotPending(refreshIds);
    scheduleSnapshotRefresh(refreshIds);
  }

  focusActivePane({ defer: true, retries: 3 });

  // Refresh sidebar pane list (debounced) so commands etc. show up
  scheduleStateRefresh();
}

let _stateRefreshTimer = null;
function scheduleStateRefresh() {
  if (_stateRefreshTimer) clearTimeout(_stateRefreshTimer);
  _stateRefreshTimer = setTimeout(() => {
    _stateRefreshTimer = null;
    if (ws && ws.readyState === WebSocket.OPEN) {
      ws.send(JSON.stringify({ type: 'get_state' }));
    }
  }, 250);
}

function onFocus(msg) {
  setActivePaneVisual(msg.pane);
  focusActivePane({ defer: _layoutApplying, retries: 3 });
}

function onWindowsChanged(msg) {
  // tmux control notifications only include the target/name, not the full
  // sidebar model, so ask the server for a fresh windows/panes snapshot.
  scheduleStateRefresh();
  if (msg.sessions) renderSessionList(msg.sessions);
  if (msg.windows) renderWindowList(msg.windows);
}

function onSessionsChanged(msg) {
  scheduleStateRefresh();
  if (msg.sessions) renderSessionList(msg.sessions);
}

// ─── Layout ───────────────────────────────────────────────────────────────────

function applyLayout(panesInfo, layoutPanes, layoutStr, activePane) {
  _layoutApplying = true;
  markSnapshotPending((panesInfo || []).map(p => p.id));
  const lp = layoutPanes && layoutPanes.length > 0 ? layoutPanes : null;
  _currentLayoutStr   = layoutStr || '';
  _currentLayoutPanes = lp || [];

  // Create pane elements first (they're hidden via mobile CSS until .active is set)
  if (!lp) {
    if (panesInfo && panesInfo.length > 0) {
      ensurePane(panesInfo[0].id, panesInfo[0].cols, panesInfo[0].rows);
    }
  } else {
    lp.forEach(({ id, cols, rows }) => ensurePane('%' + id, cols, rows));
  }

  const fallbackActivePane =
    (activePane && panes[activePane] && activePane) ||
    (lp && lp.length > 0 ? '%' + lp[0].id : null) ||
    (panesInfo && panesInfo.length > 0 ? panesInfo[0].id : null);

  // IMPORTANT: mark the active pane BEFORE positioning, so the `.active`
  // class makes the element visible (mobile CSS hides non-active panes).
  // Otherwise fit() would measure a `display: none` element as 0×0.
  if (fallbackActivePane) {
    setActivePaneVisual(fallbackActivePane);
    activePaneId = fallbackActivePane;
    panes[fallbackActivePane]?.term.focus();
  }

  // Now position; fit() will see the active pane at its real container size.
  if (!lp) {
    if (panesInfo && panesInfo.length > 0) {
      positionSinglePane(panesInfo[0].id);
    } else {
      _layoutApplying = false;
    }
  } else if (lp.length === 1) {
    positionSinglePane('%' + lp[0].id);
  } else {
    scheduleLayout(lp, layoutStr);
  }
}

function positionPanes(layoutPanes, layoutStr) {
  applyViewportFix();   // pin #app to real viewport height before measuring
  const area = document.getElementById('pane-area');
  const W = area.clientWidth;
  const H = area.clientHeight;
  if (!W || !H) {
    _layoutApplying = false;
    return;
  }

  const m = layoutStr && layoutStr.match(/,(\d+)x(\d+),/);
  if (m) { totalCols = +m[1]; totalRows = +m[2]; }

  const cellW = totalCols > 0 ? W / totalCols : 10;
  const cellH = totalRows > 0 ? H / totalRows : 20;

  // Set container pixel sizes proportional to tmux layout
  layoutPanes.forEach(({ id, x, y, cols, rows }) => {
    const pid = '%' + id;
    const p = panes[pid];
    if (!p) return;
    p.el.style.left   = `${x * cellW}px`;
    p.el.style.top    = `${y * cellH}px`;
    p.el.style.width  = `${cols * cellW}px`;
    p.el.style.height = `${rows * cellH}px`;
  });

  // After CSS is painted: fit every pane to its container using fitAddon.
  // fitAddon computes the exact cols/rows that fill the pixel area without
  // overflow — no manual ratio needed, no term.resize() override needed.
  // We send maybeSendResize so tmux's layout converges to the browser's size.
  requestAnimationFrame(() => {
    let charW = 0, charH = 0;

    // Fit all panes; measure char dimensions from the largest one
    const refLp = layoutPanes.reduce((best, lp) =>
      lp.cols * lp.rows > best.cols * best.rows ? lp : best, layoutPanes[0]);

    layoutPanes.forEach(({ id }) => {
      const pid = '%' + id;
      const p = panes[pid];
      if (!p) return;
      try { p.fitAddon.fit(); } catch (_) {}
      if (pid === '%' + refLp.id && p.term.cols > 0) {
        charW = p.el.clientWidth  / p.term.cols;
        charH = p.el.clientHeight / p.term.rows;
      }
    });

    // Tell tmux the total terminal dimensions derived from actual char size.
    // The server's resize-master guard ensures only one tab does this.
    if (charW > 0 && charH > 0) {
      maybeSendResize(Math.floor(W / charW), Math.floor(H / charH));
    }
    _layoutApplying = false;
    focusActivePane({ defer: true, retries: 2 });
  });
}

function positionSinglePane(paneId) {
  applyViewportFix();   // pin #app to real viewport height before measuring
  const p = panes[paneId];
  if (!p) {
    _layoutApplying = false;
    return;
  }
  p.el.style.left   = '0';
  p.el.style.top    = '0';
  p.el.style.width  = '100%';
  p.el.style.height = '100%';
  // Two rAFs: the first lets the CSS settle, the second runs after the next
  // browser layout pass so el.clientWidth/Height is non-zero.
  requestAnimationFrame(() => requestAnimationFrame(() => {
    applyViewportFix();   // re-pin after layout, in case URL bar moved
    try { p.fitAddon.fit(); } catch (e) { console.error('fit error:', e); }
    maybeSendResize(p.term.cols, p.term.rows);
    _layoutApplying = false;
    focusActivePane({ defer: true, retries: 2 });
  }));
}

// ─── Pane management ──────────────────────────────────────────────────────────

function ensurePane(paneId, cols, rows) {
  if (panes[paneId]) return;

  const area = document.getElementById('pane-area');
  const el = document.createElement('div');
  el.className = 'pane-wrap';
  el.dataset.paneId = paneId;
  // Pre-size with a rough placeholder so term.open() doesn't open against a
  // 0×0 element (which causes a one-frame flicker until positionPanes runs).
  // ~8px char width, ~16px line height is a reasonable rough size.
  el.style.width  = `${cols * 8}px`;
  el.style.height = `${rows * 16}px`;
  area.appendChild(el);

  const term = new Terminal({
    cols, rows,
    fontFamily:  FONT_FAMILY,
    fontSize:    13,
    scrollback:  10000,
    cursorBlink: true,
    scrollOnUserInput: true,
    smoothScrollDuration: 80,
    theme:       XTERM_THEME,
  });

  loadUnicode11Addon(term);

  const fitAddon = new FitAddon.FitAddon();
  term.loadAddon(fitAddon);
  term.open(el);
  const inputDeduper = createInputDeduper();
  if (term.textarea) {
    term.textarea.setAttribute('autocapitalize', 'none');
    term.textarea.setAttribute('autocomplete', 'off');
    term.textarea.setAttribute('autocorrect', 'off');
    term.textarea.setAttribute('spellcheck', 'false');
    term.textarea.setAttribute('enterkeyhint', 'enter');
    term.textarea.style.fontFamily = FONT_FAMILY;
    term.textarea.style.fontSize = '16px';
  }

  term.attachCustomKeyEventHandler((ev) => {
    if (shouldPreventBrowserCtrlShortcut(ev, term.textarea)) {
      // xterm still handles the key and emits onData, we only suppress browser defaults.
      ev.preventDefault();
      markClientActive();
    }
    return true;
  });

  // Send keyboard input to the active pane only.
  // If the Ctrl-toggle is on, apply a Ctrl modifier to the next single character.
  term.onData((data) => {
    if (shouldSuppressDuplicateTextInput(inputDeduper, data)) return;
    const sendData = applyCtrlModifier(data);
    handleTerminalInput(sendData, activePaneId || paneId);
  });

  // Click to focus this pane
  el.addEventListener('mousedown', () => selectPane(paneId));
  panes[paneId] = { term, fitAddon, el };
}

function destroyPane(paneId) {
  const p = panes[paneId];
  if (!p) return;
  p.term.dispose();
  p.el.remove();
  delete panes[paneId];
  _bufferedPaneOutput.delete(paneId);
  _pendingSnapshotPanes.delete(paneId);
  if (activePaneId === paneId) activePaneId = null;
}

function destroyAllPanes() {
  Object.keys(panes).forEach(destroyPane);
}

function selectPane(paneId, opts) {
  const options = opts || {};
  markClientActive();
  activePaneId = paneId;
  setActivePaneVisual(paneId);
  focusActivePane({ retries: 2 });
  if (ws && ws.readyState === WebSocket.OPEN) {
    const payload = { type: 'select_pane', pane: paneId };
    if (options.forceZoom) payload.force_zoom = true;
    ws.send(JSON.stringify(payload));
  }
}

function focusActivePane(opts) {
  const options = opts || {};
  const retries = Number.isInteger(options.retries) ? options.retries : 0;
  const defer = !!options.defer;

  const focusOnce = (remaining) => {
    if (document.visibilityState === 'hidden') return;

    let paneId = activePaneId;
    if (!paneId || !panes[paneId]) {
      paneId = Object.keys(panes)[0];
      if (!paneId) return;
      setActivePaneVisual(paneId);
    }

    const p = panes[paneId];
    if (!p) return;

    try { p.term.focus(); } catch (_) {}

    if (remaining <= 0) return;
    const textarea = p.term && p.term.textarea;
    if (!textarea || document.activeElement !== textarea) {
      requestAnimationFrame(() => focusOnce(remaining - 1));
    }
  };

  if (defer) {
    requestAnimationFrame(() => focusOnce(retries));
  } else {
    focusOnce(retries);
  }
}

function setActivePaneVisual(paneId) {
  if (!paneId) return;
  activePaneId = paneId;
  for (const [id, p] of Object.entries(panes)) {
    p.el.classList.toggle('active', id === paneId);
  }
  // On mobile, the active pane fills the screen — refit it to the new size
  if (isMobileWidth() && !_layoutApplying) {
    const p = panes[paneId];
    if (p) {
      requestAnimationFrame(() => {
        try { p.fitAddon.fit(); } catch (_) {}
        maybeSendResize(p.term.cols, p.term.rows);
      });
    }
  }
}

// ─── Window list ──────────────────────────────────────────────────────────────

function renderWindowList(windows) {
  const list = document.getElementById('window-list');
  list.innerHTML = '';
  [...(windows || [])]
    .sort((a, b) => String(a.name || '').localeCompare(String(b.name || ''), 'ja'))
    .forEach((w) => {
    const div = document.createElement('div');
    div.className = 'window-item' + (w.active ? ' active' : '');
    div.dataset.windowIndex = String(w.index);
    if (_editingWindowIndex === w.index) {
      div.classList.add('editing');
      div.appendChild(buildInlineEditor({
        kind: 'window',
        value: w.name,
        label: `window ${w.index}`,
        onSave: (name) => renameWindow(w.index, name),
        onCancel: () => {
          _editingWindowIndex = null;
          renderWindowList(windows);
        },
      }));
    } else {
      div.tabIndex = 0;
      div.innerHTML =
        `<span class="window-idx">${w.index}</span>` +
        `<span class="window-name">${escHtml(w.name)}</span>`;
      div.appendChild(buildRenameButton(`Rename window ${w.index}`, () => {
        _editingSessionName = '';
        _editingWindowIndex = w.index;
        renderWindowList(windows);
      }));
      div.addEventListener('click', () => {
        switchWindow(w.index);
        if (isMobileWidth()) setSidebarOpen(false);
        focusActivePane();
      });
    }
    list.appendChild(div);
    });
}

function renderSessionList(sessions) {
  const list = document.getElementById('session-list');
  list.innerHTML = '';
  [...(sessions || [])]
    .sort((a, b) => String(a.name || '').localeCompare(String(b.name || ''), 'ja'))
    .forEach((s) => {
    const div = document.createElement('div');
    div.className = 'session-item' + (s.active ? ' active' : '');
    div.dataset.sessionName = s.name;
    if (_editingSessionName === s.name) {
      div.classList.add('editing');
      div.appendChild(buildInlineEditor({
        kind: 'session',
        value: s.name,
        label: s.name,
        onSave: (name) => renameSession(s.name, name),
        onCancel: () => {
          _editingSessionName = '';
          renderSessionList(sessions);
        },
      }));
    } else {
      div.tabIndex = 0;
      div.innerHTML = `<span class="session-name">${escHtml(`(${s.windows}) ${s.name}`)}</span>`;
      div.title = s.attached ? 'attached' : 'detached';
      div.appendChild(buildRenameButton(`Rename session ${s.name}`, () => {
        _editingWindowIndex = null;
        _editingSessionName = s.name;
        renderSessionList(sessions);
      }));
      div.addEventListener('click', () => {
        selectSession(s.name);
        if (isMobileWidth()) setSidebarOpen(false);
        focusActivePane();
      });
    }
    list.appendChild(div);
    });
}

function renderPaneList(panesInfo, activePane) {
  const list = document.getElementById('pane-list');
  list.innerHTML = '';
  (panesInfo || []).forEach((p) => {
    const div = document.createElement('div');
    const isActive = p.active || p.id === activePane;
    div.className = 'pane-item' + (isActive ? ' active' : '');
    div.tabIndex = 0;
    div.dataset.paneId = p.id;
    div.innerHTML =
      `<span class="pane-id">${escHtml(p.id)}</span>` +
      `<span class="pane-cmd">${escHtml(p.command || '')}</span>`;
    div.addEventListener('click', () => {
      selectPane(p.id, { forceZoom: true });
      if (isMobileWidth()) setSidebarOpen(false);
      focusActivePane();
    });
    list.appendChild(div);
  });
}

function switchWindow(idx) {
  markClientActive();
  if (ws && ws.readyState === WebSocket.OPEN) {
    ws.send(JSON.stringify({ type: 'select_window', window: idx }));
  }
}

function renameWindow(idx, name) {
  const nextName = (name || '').trim();
  if (!nextName || !ws || ws.readyState !== WebSocket.OPEN) return;
  _editingWindowIndex = null;
  markClientActive();
  ws.send(JSON.stringify({ type: 'rename_window', window: idx, name: nextName }));
}

function selectSession(name) {
  markClientActive();
  if (ws && ws.readyState === WebSocket.OPEN) {
    ws.send(JSON.stringify({ type: 'select_session', session: name }));
  }
}

function renameSession(currentName, nextName) {
  const trimmed = (nextName || '').trim();
  if (!trimmed || !ws || ws.readyState !== WebSocket.OPEN) return;
  _editingSessionName = '';
  markClientActive();
  ws.send(JSON.stringify({ type: 'rename_session', session: currentName, name: trimmed }));
}

function buildRenameButton(label, onClick) {
  const button = document.createElement('button');
  button.type = 'button';
  button.className = 'item-edit-btn';
  button.setAttribute('aria-label', label);
  button.title = label;
  button.textContent = '✎';
  button.addEventListener('click', (ev) => {
    ev.preventDefault();
    ev.stopPropagation();
    onClick();
  });
  return button;
}

function buildInlineEditor({ kind, value, label, onSave, onCancel }) {
  const editor = document.createElement('div');
  editor.className = 'item-inline-editor';

  const input = document.createElement('input');
  input.type = 'text';
  input.className = 'item-inline-input';
  input.value = value || '';
  input.setAttribute('aria-label', `Rename ${kind} ${label}`);

  const save = document.createElement('button');
  save.type = 'button';
  save.className = 'item-inline-btn';
  save.textContent = 'save';

  const cancel = document.createElement('button');
  cancel.type = 'button';
  cancel.className = 'item-inline-btn secondary';
  cancel.textContent = 'cancel';

  const stop = (ev) => {
    ev.preventDefault();
    ev.stopPropagation();
  };
  [editor, input, save, cancel].forEach((el) => {
    el.addEventListener('mousedown', stop);
    el.addEventListener('click', (ev) => ev.stopPropagation());
  });

  input.addEventListener('keydown', (ev) => {
    if (ev.key === 'Enter') {
      ev.preventDefault();
      onSave(input.value);
    } else if (ev.key === 'Escape') {
      ev.preventDefault();
      onCancel();
    }
  });

  save.addEventListener('click', () => onSave(input.value));
  cancel.addEventListener('click', onCancel);

  editor.appendChild(input);
  editor.appendChild(save);
  editor.appendChild(cancel);

  requestAnimationFrame(() => {
    input.focus();
    input.select();
  });

  return editor;
}

function moveSidebarFocus(list, delta) {
  const items = [...list.querySelectorAll('.session-item, .window-item, .pane-item')];
  if (items.length === 0) return;
  const current = document.activeElement;
  const currentIdx = items.indexOf(current);
  const activeIdx = items.findIndex((item) => item.classList.contains('active'));
  const start = currentIdx >= 0 ? currentIdx : Math.max(activeIdx, 0);
  const next = items[(start + delta + items.length) % items.length];
  next.focus();
  next.scrollIntoView({ block: 'nearest' });
}

function activateSidebarItem(item) {
  if (!item || item.classList.contains('editing')) return;
  if (item.classList.contains('session-item')) {
    selectSession(item.dataset.sessionName || '');
  } else if (item.classList.contains('window-item')) {
    switchWindow(Number(item.dataset.windowIndex));
  } else if (item.classList.contains('pane-item')) {
    selectPane(item.dataset.paneId || '', { forceZoom: true });
  }
  if (isMobileWidth()) setSidebarOpen(false);
}

function handleSidebarListKeydown(ev) {
  const list = ev.currentTarget;
  if (ev.target instanceof HTMLInputElement) return;
  if (ev.key === 'ArrowDown') {
    ev.preventDefault();
    moveSidebarFocus(list, 1);
  } else if (ev.key === 'ArrowUp') {
    ev.preventDefault();
    moveSidebarFocus(list, -1);
  } else if (ev.key === 'Enter') {
    ev.preventDefault();
    activateSidebarItem(document.activeElement);
  } else if (ev.key === 'Escape') {
    ev.preventDefault();
    focusActivePane();
  }
}

document.getElementById('session-list').addEventListener('keydown', handleSidebarListKeydown);
document.getElementById('window-list').addEventListener('keydown', handleSidebarListKeydown);
document.getElementById('pane-list').addEventListener('keydown', handleSidebarListKeydown);

// ─── Sidebar action buttons ───────────────────────────────────────────────────

function wsSendType(type, extra) {
  if (!ws || ws.readyState !== WebSocket.OPEN) return;
  markClientActive();
  ws.send(JSON.stringify({ type, ...(extra || {}) }));
}

document.getElementById('btn-new-window').addEventListener('click', () => {
  wsSendType('new_window');
  focusActivePane();
});
document.getElementById('btn-new-session').addEventListener('click', () => {
  wsSendType('new_session');
  focusActivePane();
});
document.getElementById('btn-split-h').addEventListener('click', () => {
  wsSendType('split_window', { direction: 'h', pane: activePaneId || '' });
  focusActivePane();
});
document.getElementById('btn-split-v').addEventListener('click', () => {
  wsSendType('split_window', { direction: 'v', pane: activePaneId || '' });
  focusActivePane();
});

// ─── Browser window resize ────────────────────────────────────────────────────

let _resizeDebounce = null;

window.addEventListener('resize', () => {
  // Debounce: many resize events fire during a drag — collapse to one.
  if (_resizeDebounce) clearTimeout(_resizeDebounce);
  _resizeDebounce = setTimeout(() => {
    _resizeDebounce = null;
    if (_currentLayoutPanes.length === 0) return;
    if (_currentLayoutPanes.length === 1) {
      positionSinglePane('%' + _currentLayoutPanes[0].id);
    } else {
      scheduleLayout(_currentLayoutPanes, _currentLayoutStr);
    }
  }, 100);
});

// ─── Layout string parser (client-side mirror of layout_parser.py) ────────────

function parseLayout(layoutStr) {
  const idx = layoutStr.indexOf(',');
  if (idx < 0) return [];
  const panes = [];
  parseNode(layoutStr.slice(idx + 1), panes);
  return panes;
}

function parseNode(s, out) {
  const m = s.match(/^(\d+)x(\d+),(\d+),(\d+)(.*)/);
  if (!m) return;
  const [, cols, rows, x, y, rest] = m;
  if (rest[0] === ',') {
    const numMatch = rest.slice(1).match(/^(\d+)/);
    if (numMatch) out.push({ id: +numMatch[1], x: +x, y: +y, cols: +cols, rows: +rows });
  } else if (rest[0] === '{' || rest[0] === '[') {
    const inner = extractBracket(rest);
    splitChildren(inner).forEach(child => parseNode(child, out));
  }
}

function extractBracket(s) {
  let depth = 0;
  for (let i = 0; i < s.length; i++) {
    if ('{['.includes(s[i])) depth++;
    else if ('}]'.includes(s[i])) { depth--; if (depth === 0) return s.slice(1, i); }
  }
  return s.slice(1);
}

function splitChildren(s) {
  const parts = [], re = /^\d+x\d+/;
  let depth = 0, start = 0;
  for (let i = 0; i < s.length; i++) {
    if ('{['.includes(s[i])) depth++;
    else if ('}]'.includes(s[i])) depth--;
    else if (s[i] === ',' && depth === 0 && re.test(s.slice(i + 1))) {
      parts.push(s.slice(start, i));
      start = i + 1;
    }
  }
  parts.push(s.slice(start));
  return parts;
}

// ─── Helpers ──────────────────────────────────────────────────────────────────

function b64ToUint8(b64) {
  const bin = atob(b64);
  const u8  = new Uint8Array(bin.length);
  for (let i = 0; i < bin.length; i++) u8[i] = bin.charCodeAt(i);
  return u8;
}

const _asciiEncoder = new TextEncoder();

function clamp(n, min, max) {
  return Math.min(Math.max(n, min), max);
}

function asciiBytes(s) {
  return _asciiEncoder.encode(s);
}

function concatBytes(parts) {
  const total = parts.reduce((sum, part) => sum + part.length, 0);
  const out = new Uint8Array(total);
  let offset = 0;
  parts.forEach((part) => {
    out.set(part, offset);
    offset += part.length;
  });
  return out;
}

// capture-pane uses LF only between rows, and rows shorter than the pane width
// are not padded. xterm.js (LNM reset, convertEol off) treats LF as "down only"
// without resetting the column, so the next row would start at the previous
// row's end column. Inserting CR before every LF makes each row start at col 1.
function lfToCrlf(bytes) {
  let count = 0;
  for (let i = 0; i < bytes.length; i++) if (bytes[i] === 0x0A) count++;
  if (count === 0) return bytes;
  const out = new Uint8Array(bytes.length + count);
  let j = 0;
  for (let i = 0; i < bytes.length; i++) {
    if (bytes[i] === 0x0A) out[j++] = 0x0D;
    out[j++] = bytes[i];
  }
  return out;
}

function buildSnapshotFrame(msg, term) {
  const snapshot = lfToCrlf(b64ToUint8(msg.data || ''));
  const paneRows = Math.max(1, msg.pane_rows || term.rows || 1);
  const paneCols = Math.max(1, msg.pane_cols || term.cols || 1);
  const cursorRow = clamp((msg.cursor_y || 0) + 1, 1, paneRows);
  const cursorCol = clamp((msg.cursor_x || 0) + 1, 1, paneCols);

  const parts = [asciiBytes('\x1b[?25l\x1b[H\x1b[2J'), snapshot];
  parts.push(asciiBytes(`\x1b[${cursorRow};${cursorCol}H\x1b[?25h`));
  return concatBytes(parts);
}

function escHtml(s) {
  return s.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
}

function setStatus(state) {
  const el = document.getElementById('status');
  el.className = state;
  el.title     = state;
}

// ─── Sidebar drawer (hamburger) ───────────────────────────────────────────────

function isMobileWidth() {
  return window.innerWidth <= MOBILE_BP;
}

function setSidebarOpen(open) {
  const sidebar  = document.getElementById('sidebar');
  const backdrop = document.getElementById('sidebar-backdrop');
  sidebar.classList.toggle('open',   open);
  sidebar.classList.toggle('closed', !open);
  backdrop.classList.toggle('visible', open && isMobileWidth());
}

function toggleSidebar() {
  const sidebar = document.getElementById('sidebar');
  setSidebarOpen(!sidebar.classList.contains('open'));
}

document.getElementById('hamburger').addEventListener('click', () => {
  toggleSidebar();
  focusActivePane();
});
document.getElementById('sidebar-backdrop').addEventListener('click', () => {
  setSidebarOpen(false);
  focusActivePane();
});

window.addEventListener('focus', activateClient);
document.addEventListener('pointerdown', markClientActive, { passive: true });
document.addEventListener('visibilitychange', () => {
  if (document.visibilityState === 'hidden') {
    _clientActive = false;
  } else if (document.hasFocus()) {
    activateClient();
  }
});

// Initial state: open on desktop, closed on mobile
setSidebarOpen(!isMobileWidth());

// Re-evaluate sidebar visibility and pane layout on width crossing the breakpoint
let _prevMobile = isMobileWidth();
window.addEventListener('resize', () => {
  const nowMobile = isMobileWidth();
  if (nowMobile !== _prevMobile) {
    setSidebarOpen(!nowMobile);
    _prevMobile = nowMobile;
    resetResizeCache();   // CSS mode swap — old _lastResize is stale

    // Re-apply layout: refresh inline styles for all panes and refit.
    if (_currentLayoutPanes.length === 1) {
      positionSinglePane('%' + _currentLayoutPanes[0].id);
    } else if (_currentLayoutPanes.length > 1) {
      scheduleLayout(_currentLayoutPanes, _currentLayoutStr);
    }
    focusActivePane();
  }
});

// ─── Bottom bar (virtual keys + Ctrl toggle) ──────────────────────────────────

let _ctrlActive = false;

function setCtrlActive(on) {
  _ctrlActive = on;
  document.getElementById('ctrl-toggle').classList.toggle('active', on);
}

function applyCtrlModifier(data) {
  if (!_ctrlActive || data.length !== 1) return data;
  const code = data.charCodeAt(0);
  // A-Z / a-z / @ [ \ ] ^ _ → Ctrl+<key>
  if (code >= 0x40 && code < 0x80) {
    setCtrlActive(false);
    return String.fromCharCode(code & 0x1f);
  }
  return data;
}

function sendVirtualKey(name) {
  const data = VIRTUAL_KEYS[name];
  if (!data) return;
  sendPaneInput(data);
}

function scrollActivePaneHalfPage(direction) {
  const pane = getActivePane();
  if (!pane) return;
  const delta = Math.max(1, Math.floor(pane.term.rows / 2)) * direction;
  pane.term.scrollLines(delta);
}

function hideSoftwareKeyboard() {
  const pane = getActivePane();
  if (pane) {
    try { pane.term.blur(); } catch (_) {}
  }
  const activeEl = document.activeElement;
  if (activeEl && typeof activeEl.blur === 'function') {
    try { activeEl.blur(); } catch (_) {}
  }
}

function getActivePaneViewportText() {
  const pane = getActivePane();
  if (!pane) return '';
  const buffer = pane.term.buffer.active;
  const start = Math.max(0, buffer.viewportY);
  const end = Math.min(buffer.length, start + pane.term.rows);
  const lines = [];
  for (let i = start; i < end; i++) {
    const line = buffer.getLine(i);
    if (!line) continue;
    lines.push(line.translateToString(true));
  }
  return lines.join('\n');
}

function setClipboardSheetOpen(open) {
  const sheet = document.getElementById('clipboard-sheet');
  if (!sheet) return;
  sheet.classList.toggle('hidden', !open);
  sheet.setAttribute('aria-hidden', open ? 'false' : 'true');
}

function openClipboardSheet() {
  const copyBox = document.getElementById('clipboard-copy-text');
  const pasteBox = document.getElementById('clipboard-paste-text');
  copyBox.value = getActivePaneViewportText();
  pasteBox.value = '';
  setClipboardSheetOpen(true);
}

function closeClipboardSheet() {
  setClipboardSheetOpen(false);
  focusActivePane();
}

async function copyClipboardSheetText() {
  const copyBox = document.getElementById('clipboard-copy-text');
  if (!copyBox.value) return;
  if (navigator.clipboard && navigator.clipboard.writeText) {
    try {
      await navigator.clipboard.writeText(copyBox.value);
      return;
    } catch (_) {}
  }
  copyBox.focus();
  copyBox.select();
}

function sendClipboardSheetPaste() {
  const pasteBox = document.getElementById('clipboard-paste-text');
  if (!pasteBox.value) {
    closeClipboardSheet();
    return;
  }
  sendPaneInput(pasteBox.value);
  closeClipboardSheet();
}

function preserveKeyboardState(ev) {
  // Keep virtual-key buttons from stealing focus away from xterm's textarea on iPhone.
  ev.preventDefault();
  markClientActive();
}

document.querySelectorAll('#bottombar button, #topbar-actions button').forEach((btn) => {
  btn.addEventListener('pointerdown', preserveKeyboardState);
});

document.querySelectorAll('#bottombar .vkey').forEach((btn) => {
  btn.addEventListener('click', () => {
    sendVirtualKey(btn.dataset.key);
  });
});

document.getElementById('scroll-up-half').addEventListener('click', () => {
  hideSoftwareKeyboard();
  scrollActivePaneHalfPage(-1);
});

document.getElementById('scroll-down-half').addEventListener('click', () => {
  hideSoftwareKeyboard();
  scrollActivePaneHalfPage(1);
});

document.getElementById('ctrl-toggle').addEventListener('click', () => {
  markClientActive();
  setCtrlActive(!_ctrlActive);
  // Ctrl should reopen the software keyboard if it was closed.
  focusActivePane();
});

document.getElementById('clipboard-toggle').addEventListener('click', () => {
  openClipboardSheet();
});

document.getElementById('clipboard-close').addEventListener('click', () => {
  closeClipboardSheet();
});

document.querySelector('#clipboard-sheet .sheet-backdrop').addEventListener('click', () => {
  closeClipboardSheet();
});

document.getElementById('clipboard-send').addEventListener('click', () => {
  sendClipboardSheetPaste();
});

document.getElementById('clipboard-copy-all').addEventListener('click', () => {
  copyClipboardSheetText();
});

// ─── Soft keyboard / viewport handling (mobile) ───────────────────────────────
// On iOS, `height: 100vh` returns the LARGEST possible viewport (URL bar hidden),
// which is bigger than the actually visible area when the URL bar is showing.
// We pin #app's height to visualViewport.height so the layout always fits the
// real visible area — and re-fit the active terminal whenever that changes.

function applyViewportFix() {
  const vv  = window.visualViewport;
  const app = document.getElementById('app');
  if (!vv || !app) return;
  const width = Math.round(vv.width);
  const height = Math.round(vv.height);
  const widthChanged = width !== _lastViewportSize.width;
  const heightChanged = height !== _lastViewportSize.height;
  _lastViewportSize = { width, height };
  if (isMobileWidth()) {
    app.style.height = `${vv.height}px`;
    // Re-fit the active pane so xterm.js can resize to the new viewport,
    // and inform tmux of the new dimensions so output formatting matches.
    const p = activePaneId && panes[activePaneId];
    if (p && !_layoutApplying && (widthChanged || heightChanged)) {
      const wasFocused = p.term.textarea && document.activeElement === p.term.textarea;
      try { p.fitAddon.fit(); } catch (_) {}
      maybeSendResize(p.term.cols, p.term.rows);
      if (wasFocused) {
        requestAnimationFrame(() => {
          try { p.term.focus(); } catch (_) {}
        });
      }
    }
  } else {
    app.style.height = '';
  }
}

(function setupViewportEvents() {
  const vv = window.visualViewport;
  if (!vv) return;
  vv.addEventListener('resize', applyViewportFix);
  window.addEventListener('resize', applyViewportFix);
  applyViewportFix();
})();

// ─── Boot ─────────────────────────────────────────────────────────────────────

connect();
__WEBTMUX_EOF__

echo "  -> start.sh"
cat > start.sh <<'__WEBTMUX_EOF__'
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
__WEBTMUX_EOF__
chmod +x start.sh

chmod +x start.sh

echo ""
echo "files generated:"
echo "  server.py"
echo "  tmux_control.py"
echo "  layout_parser.py"
echo "  static/index.html"
echo "  static/style.css"
echo "  static/app.js"
echo "  start.sh"
echo ""
echo "next steps:"
echo "  1. python3 -m pip install --user websockets   # if not already installed"
echo "  2. ./start.sh"
