#!/usr/bin/env python3
from __future__ import annotations

"""
tmux control mode (-CC) subprocess wrapper.

tmux -CC requires a real TTY; we create a PTY pair and run tmux with the
slave end as its controlling terminal.  The master end is used for I/O.

Input to panes:    via `send-keys -H <hex>...` (proper input injection)
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
    br'\x1b\[1m\x1b\[7m[%#]\x1b\[27m\x1b\[1m\x1b\[0m *\r ?\r'
)
_ZSH_EOL_MARK_PREFIX = b'\x1b[1m\x1b[7m'
_ZSH_EOL_MARK_SUFFIX = b'\x1b[27m\x1b[1m\x1b[0m'


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


def _strip_zsh_eol_marks(data: bytes) -> bytes:
    return _ZSH_EOL_MARK_RE.sub(b'', data)


def _split_clean_output(data: bytes) -> tuple[bytes, bytes]:
    """Strip zsh's prompt EOL mark while tolerating chunked tmux output."""
    out = bytearray()
    i = 0
    prefix_len = len(_ZSH_EOL_MARK_PREFIX)
    suffix_len = len(_ZSH_EOL_MARK_SUFFIX)

    while i < len(data):
        if not data.startswith(_ZSH_EOL_MARK_PREFIX, i):
            out.append(data[i])
            i += 1
            continue

        symbol_idx = i + prefix_len
        if symbol_idx >= len(data):
            break
        if data[symbol_idx] not in (ord('%'), ord('#')):
            out.append(data[i])
            i += 1
            continue

        suffix_idx = symbol_idx + 1
        if suffix_idx + suffix_len > len(data):
            break
        if not data.startswith(_ZSH_EOL_MARK_SUFFIX, suffix_idx):
            out.append(data[i])
            i += 1
            continue

        j = suffix_idx + suffix_len
        while j < len(data) and data[j] == 0x20:
            j += 1
        if j >= len(data):
            break
        if data[j] != 0x0d:
            out.append(data[i])
            i += 1
            continue

        j += 1
        if j >= len(data):
            break
        if data[j] == 0x20:
            j += 1
            if j >= len(data):
                break
        if data[j] != 0x0d:
            out.append(data[i])
            i += 1
            continue

        i = j + 1

    return bytes(out), data[i:]


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
        self._output_remainder: dict[str, bytes] = {}

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
        self._output_remainder = {}
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
                os.write(self._master_fd, (cmd + '\n').encode())
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

    async def write_input(self, pane_id: str, data: bytes) -> None:
        """Inject input into a pane via tmux send-keys -H.

        Writing to the slave TTY only produces output; to deliver real input
        to the shell running in the pane we must go through tmux's master PTY,
        which send-keys does internally.
        """
        if not data:
            return
        # send-keys -H accepts space-separated 2-digit hex values.
        # Each hex value is one byte delivered as keyboard input.
        # Chunk to avoid overly long command lines (max ~200 bytes at once).
        chunk_size = 100
        for i in range(0, len(data), chunk_size):
            chunk = data[i:i + chunk_size]
            hex_args = ' '.join(f'{b:02x}' for b in chunk)
            await self.send_command(f'send-keys -t {pane_id} -H {hex_args}')

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
            name = p[0]
            windows = int(p[1]) if p[1].isdigit() else 0
            attached = p[2] not in ('', '0')
            sessions.append({
                'name': name,
                'windows': windows,
                'attached': attached,
                'active': name == self.session,
            })

        win_raw = await self.send_command(
            f'list-windows -t {self.session} -F'
            ' "#{window_index}|#{window_name}|#{window_active}|#{window_layout}"'
        )
        windows, active_layout = [], ''
        active_window_idx: int | None = None
        for line in win_raw.splitlines():
            p = line.split('|', 3)
            if len(p) < 4:
                continue
            idx    = int(p[0]) if p[0].isdigit() else 0
            name   = p[1]
            active = p[2] == '1'
            layout = p[3]
            if active:
                active_window_idx = idx
                active_layout = layout
            windows.append({'index': idx, 'name': name, 'active': active, 'layout': layout})

        pane_target = self.session
        if active_window_idx is not None:
            pane_target = f'{self.session}:{active_window_idx}'
        pane_raw = await self.send_command(
            f'list-panes -t {pane_target} -F'
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
        if self._in_resp:
            self._cur_resp.append(line)
            return

        # ── notifications ──
        if line.startswith('%output '):
            parts = line.split(' ', 2)
            if len(parts) >= 3:
                pane_id = parts[1]
                raw_data = self._output_remainder.get(pane_id, b'') + _decode_output(parts[2])
                data, remainder = _split_clean_output(raw_data)
                if remainder:
                    self._output_remainder[pane_id] = remainder
                elif pane_id in self._output_remainder:
                    del self._output_remainder[pane_id]
                if not data:
                    return
                self._broadcast({
                    'type': 'output',
                    'pane': pane_id,
                    'data': base64.b64encode(data).decode('ascii'),
                })
        elif line.startswith('%layout-change '):
            parts = line.split(' ', 2)
            if len(parts) >= 3:
                self._broadcast({
                    'type':   'layout_change',
                    'target': parts[1],
                    'layout': parts[2],
                })
        elif line.startswith('%window-add '):
            self._broadcast({'type': 'window_add',    'target': line[12:]})
        elif line.startswith('%window-close '):
            self._broadcast({'type': 'window_close',  'target': line[14:]})
        elif line.startswith('%window-renamed '):
            p = line.split(' ', 2)
            self._broadcast({
                'type':   'window_renamed',
                'target': p[1] if len(p) > 1 else '',
                'name':   p[2] if len(p) > 2 else '',
            })
        elif line.startswith('%pane-focus-in '):
            self._broadcast({'type': 'focus', 'pane': line[15:].strip()})
        elif line.startswith('%sessions-changed'):
            self._broadcast({'type': 'sessions_changed'})
        elif line.startswith('%exit'):
            log.info('tmux session exited')
            self._schedule_restart()

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
