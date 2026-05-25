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

# CPR sequences (ESC [ row ; col R) are terminal-to-application responses.
# Some TUI CLIs (e.g. Hermes agent) accidentally emit them to stdout,
# which causes them to appear as literal text in the pane buffer.
_CPR_RE = re.compile(br'\x1b\[\d+;\d+R')

# OSC response sequences (ESC ] N ; value ST) are terminal-to-application
# replies (e.g. OSC 10/11/12 color reports). The PTY hosting tmux's control
# client can emit these into the data stream where they show up as literal
# text like ^[]11;rgb:2e2e/3434/4040^[\.
_OSC_RESPONSE_RE = re.compile(br'\x1b\]\d+;[^\x07\x1b]*(?:\x1b\\|\x07)')

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


def _strip_cpr_sequences(data: bytes) -> bytes:
    if b'\x1b' not in data:
        return data
    return _CPR_RE.sub(b'', data)


def _strip_osc_responses(data: bytes) -> bytes:
    if b'\x1b]' not in data:
        return data
    return _OSC_RESPONSE_RE.sub(b'', data)


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
        return _strip_osc_responses(_strip_cpr_sequences(_strip_zsh_eol_marks(_decode_output(raw))))

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
                data = _strip_osc_responses(_strip_cpr_sequences(data))
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
                data = _strip_osc_responses(_strip_cpr_sequences(data))
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
