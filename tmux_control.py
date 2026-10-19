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
import contextlib
import fcntl
import json
import logging
import os
import pty
import re
import struct
import termios
import time
import tty
from typing import Callable

import tmux_hosts
from tmux_hosts import LOCAL_HOST, TmuxHost

log = logging.getLogger(__name__)

# Upper bound on the history a client may pull in one get_history request.
# This is paid once per pane at attach (snapshots carry the visible screen
# only), so it can sit much closer to the 50000-line history-limit than the
# old per-snapshot budget could.
SNAPSHOT_SCROLLBACK_LINES = 10000

# A paste buffer larger than this is not forwarded to the browser clipboard.
# The broadcast goes through every client's 1 MiB outbound queue, and JSON
# escaping can grow non-ASCII text several times over.
CLIPBOARD_MAX_BYTES = 128 * 1024

# How long a freshly started control client may take to answer its attach.
# A remote one has to get through ssh (and maybe a ProxyJump hop) first.
LOCAL_READY_TIMEOUT = 5.0
REMOTE_READY_TIMEOUT = 20.0
# Consecutive failed (re)connects to a remote host before falling back to
# the local session, so a dead host can't leave the browser on a dead view.
REMOTE_ATTEMPTS = 3
MAX_RETRY_DELAY = 30.0

# Longest control-protocol line we hold while waiting for its newline. tmux's
# own lines (a %output chunk, one captured row) are far shorter; a stream
# that never ends a line — a broken or hostile remote — gets the client
# reconnected instead of growing the server's memory.
MAX_CONTROL_LINE = 1024 * 1024
# And the most one command's response (everything between %begin and %end)
# may add up to. The biggest real one, a full scrollback capture with
# colours, is a few MB. Past this the rest of the response is dropped and
# the command reads as failed (an empty response) — the channel itself stays
# up, so a legitimate oversized capture doesn't cost the connection.
MAX_RESPONSE_BYTES = 16 * 1024 * 1024

# tmux -CC wraps its whole output in a DCS: the first line starts with it.
_DCS_START = '\x1bP1000p'

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
_TERMINAL_RESPONSE_CSI_RE = re.compile(br'\x1b\[[0-9;?]*[Rcn]')
# The longest unfinished sequence held back in case it turns out to be one of
# those replies, which run to a few dozen bytes.
_MAX_HELD_REPLY = 4096

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


def _response_bytes(s: str) -> bytes:
    """A command's output as the pane's own bytes.

    Unlike %output, the lines between %begin and %end are not vis(3)-encoded:
    a captured row reaches us with its ESC and TAB bytes as they are, and a
    backslash on screen is just a backslash. Decoding it as %output would turn
    a visible `\\n` (C source, printf formats, Windows paths) into a real line
    break and shift every row below it. _on_readable read the bytes as
    latin-1, so encoding back gives them unchanged.
    """
    return s.encode('latin-1', errors='replace')


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


def _strip_terminal_response_sequences_stream(data: bytes, remainder: bytes = b'') -> tuple[bytes, bytes]:
    """Remove terminal-to-application replies, preserving split escape tails.

    tmux `%output` chunks may split inside OSC/CSI replies. Regex replacement on
    each chunk misses those tails, leaving visible `^[...]` text in panes.
    """
    src = remainder + data
    out = bytearray()
    i = 0
    n = len(src)

    def hold(esc: int) -> tuple[bytes, bytes]:
        # A tail longer than any reply can't be one: pass it on, so a stream
        # that never finishes its sequence can't make us keep it all.
        if n - esc > _MAX_HELD_REPLY:
            return bytes(out) + src[esc:], b''
        return bytes(out), src[esc:]

    while i < n:
        esc = src.find(b'\x1b', i)
        if esc < 0:
            out.extend(src[i:])
            break

        out.extend(src[i:esc])
        if esc + 1 >= n:
            return hold(esc)

        kind = src[esc + 1]

        if kind == ord('['):
            j = esc + 2
            while j < n and 0x30 <= src[j] <= 0x3f:
                j += 1
            if j >= n:
                return hold(esc)

            final = src[j]
            if 0x40 <= final <= 0x7e:
                seq = src[esc:j + 1]
                if _TERMINAL_RESPONSE_CSI_RE.fullmatch(seq):
                    i = j + 1
                    continue
                out.extend(seq)
                i = j + 1
                continue

            out.append(src[esc])
            i = esc + 1
            continue

        if kind == ord(']'):
            # A terminal ends an OSC at BEL or at the next ESC, whatever
            # follows it (ESC \ is ST proper). Looking only for BEL and ST
            # held every later byte of the pane, colours and all, until one
            # turned up.
            bel = src.find(b'\x07', esc + 2)
            nxt = src.find(b'\x1b', esc + 2)
            if nxt >= 0 and (bel < 0 or nxt < bel):
                if nxt + 1 >= n:
                    return hold(esc)   # maybe the ESC of a split ST
                end = nxt + 2 if src[nxt + 1] == 0x5c else nxt
            elif bel >= 0:
                end = bel + 1
            else:
                return hold(esc)

            seq = src[esc:end]
            if _OSC_RESPONSE_RE.fullmatch(seq):
                i = end
                continue
            out.extend(seq)
            i = end
            continue

        out.append(src[esc])
        i = esc + 1

    return bytes(out), b''


def _strip_terminal_response_sequences(data: bytes) -> bytes:
    filtered, _ = _strip_terminal_response_sequences_stream(data)
    return filtered


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
    def __init__(self, session: str = 'web', host: TmuxHost = LOCAL_HOST, *,
                 fallback: tuple[TmuxHost, str] | None = None,
                 on_target_changed: Callable[[str, str, list], None] | None = None):
        self.session    = session
        self.host       = host
        self.proc       = None
        self.subscribers: list = []
        # Bumped whenever the control client moves to another host: pane ids
        # name different panes there, so work started for the old target
        # must not land on the new one.
        self.target_gen = 0
        self._fallback  = fallback
        self._on_target_changed = on_target_changed
        self._master_fd: int | None = None
        self._buf: str  = ''
        self._pending: list[asyncio.Future] = []
        # Callbacks run the moment a command list's last %end is parsed, before
        # any later line — the only point that is ordered with %output.
        self._on_end: dict[asyncio.Future, Callable[[], None]] = {}
        self._cur_resp: list[str] = []
        self._cur_resp_bytes = 0
        self._resp_overflow = False
        self._in_resp: bool = False
        self._resp_guard = ''   # "<time> <number> <flags>" of the open %begin
        self._resp_ours = True  # the open block answers a command we sent
        self._ready = asyncio.Event()
        self._failures = 0
        self._next_attempt = 0.0
        self._watch_task: asyncio.Task | None = None
        self._restart_lock = asyncio.Lock()
        self._restart_task: asyncio.Task | None = None
        self._decode_remainder: dict[str, str] = {}
        self._response_remainder: dict[str, bytes] = {}
        self._prefix_key_names: set[str] | None = None
        self._prefix_pending: bool = False
        self._clipboard_seq: int = 0
        self._clipboard_tasks: set[asyncio.Task] = set()

    # ──────────────────────────────────────── lifecycle

    async def start(self) -> None:
        if not await self._restart_client():
            self._schedule_restart()

    def _alive(self) -> bool:
        return (self._master_fd is not None and self.proc is not None
                and self.proc.returncode is None and self._ready.is_set())

    async def _restart_client(self) -> bool:
        """(Re)attach to the current target. On repeated failure of a remote
        host, fall back to the local session. True once attached."""
        async with self._restart_lock:
            if self._alive():
                return True
            if await self._connect():
                return True
            if (self.host.is_local or self._fallback is None
                    or self._failures < REMOTE_ATTEMPTS):
                return False
            previous = self.host.name
            self.host, self.session = self._fallback
            self.target_gen += 1
            self._failures, self._next_attempt = 0, 0.0
            log.warning('host %s unreachable; falling back to the local session', previous)
            # Pane ids mean other panes on the fallback host: keep everyone
            # off the stream until the callback has told them about the move.
            subscribers = self.subscribers[:]
            self.subscribers.clear()
            ok = await self._connect()
            if self._on_target_changed is not None:
                self._on_target_changed('fell_back', previous, subscribers)
            else:
                self.subscribers.extend(subscribers)
            return ok

    async def switch_target(self, host: TmuxHost, session: str) -> bool:
        """Move the control client to `session` on `host`. On failure it goes
        back to where it was and returns False."""
        if self._restart_task and not self._restart_task.done():
            self._restart_task.cancel()
        async with self._restart_lock:
            previous = (self.host, self.session)
            self.host, self.session = host, session
            self.target_gen += 1
            self._failures, self._next_attempt = 0, 0.0
            if await self._connect():
                return True
            self.host, self.session = previous
            self.target_gen += 1
            self._failures, self._next_attempt = 0, 0.0
            reattached = await self._connect()
        if not reattached:
            self._schedule_restart()
        return False

    async def _connect(self) -> bool:
        await self._cleanup_client()
        await self._ensure_session_exists()
        await self._set_server_options()
        await self._attach_control_client()
        if await self._wait_ready():
            self._failures, self._next_attempt = 0, 0.0
            return True
        self._failures += 1
        self._next_attempt = time.monotonic() + min(MAX_RETRY_DELAY, 2.0 ** self._failures)
        log.warning('tmux control client for host=%s did not attach (attempt %d)',
                    self.host.name, self._failures)
        await self._cleanup_client()
        return False

    async def _wait_ready(self) -> bool:
        proc = self.proc
        if proc is None:
            return False
        timeout = LOCAL_READY_TIMEOUT if self.host.is_local else REMOTE_READY_TIMEOUT
        ready = asyncio.ensure_future(self._ready.wait())
        exited = asyncio.ensure_future(proc.wait())
        try:
            await asyncio.wait({ready, exited}, timeout=timeout,
                               return_when=asyncio.FIRST_COMPLETED)
        finally:
            ready.cancel()
            exited.cancel()
        return self._ready.is_set() and proc.returncode is None

    async def _ensure_session_exists(self) -> None:
        # '=' makes the match exact; a bare name would also match a prefix.
        rc, _, _ = await tmux_hosts.run(self.host, 'has-session', '-t', f'={self.session}')
        if rc != 0 and rc != tmux_hosts.RC_UNREACHABLE:
            await tmux_hosts.run(self.host, 'new-session', '-d', '-s', self.session)

    async def _set_server_options(self) -> None:
        # Let the active browser client drive the window size via refresh-client -C.
        # Using "smallest" makes a narrow phone/control client shrink the shared
        # tmux window even when the desktop browser is the active viewer.
        # One invocation for both: on a remote host each is an ssh round trip.
        await tmux_hosts.run(self.host,
                             'set-option', '-g', 'window-size', 'latest', ';',
                             'set-option', '-g', 'history-limit', '50000')

    async def _attach_control_client(self) -> None:
        # PTY pair: slave is tmux's (or ssh's) controlling terminal
        master_fd, slave_fd = pty.openpty()
        _set_winsize(slave_fd, rows=50, cols=220)
        if not self.host.is_local:
            # Nothing may echo back before ssh switches the terminal to raw.
            tty.setraw(slave_fd)

        try:
            self.proc = await asyncio.create_subprocess_exec(
                *self.host.control_argv(self.session),
                stdin=slave_fd, stdout=slave_fd, stderr=slave_fd,
                close_fds=True,
                # ssh: askpass that refuses, and no controlling terminal to
                # prompt on instead (see tmux_hosts.ssh_env).
                env=tmux_hosts.process_env(self.host),
                start_new_session=not self.host.is_local,
            )
        except OSError as exc:
            os.close(slave_fd)
            os.close(master_fd)
            log.error('could not start the tmux control client: %s', type(exc).__name__)
            return
        os.close(slave_fd)
        # Writes must never block the event loop: a stalled network stops
        # ssh reading, and a full PTY would freeze every browser.
        os.set_blocking(master_fd, False)
        self._master_fd = master_fd

        # Register master_fd as an asyncio readable source
        loop = asyncio.get_event_loop()
        loop.add_reader(master_fd, self._on_readable)
        self._watch_task = asyncio.ensure_future(self._watch_proc(self.proc))

        log.info('tmux -CC started, host=%s session=%s pid=%d',
                 self.host.name, self.session, self.proc.pid)

    async def _watch_proc(self, proc) -> None:
        # %exit covers tmux leaving on its own; a dropped ssh connection only
        # shows up as the process going away.
        await proc.wait()
        if proc is self.proc and self._ready.is_set():
            log.info('tmux control client exited rc=%s host=%s', proc.returncode, self.host.name)
            self._schedule_restart()

    async def _cleanup_client(self) -> None:
        loop = asyncio.get_event_loop()
        self._ready.clear()
        if self._watch_task is not None and self._watch_task is not asyncio.current_task():
            self._watch_task.cancel()
        self._watch_task = None
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
        self._cur_resp_bytes = 0
        self._resp_overflow = False
        self._in_resp = False
        self._resp_ours = True
        # Pane ids are reused by the next server (or host): a partial
        # sequence from this one must not be glued onto that pane's output.
        self._decode_remainder = {}
        self._response_remainder = {}
        self._prefix_key_names = None
        self._prefix_pending = False
        self._on_end.clear()
        while self._pending:
            fut = self._pending.pop(0)
            if not fut.done():
                fut.set_result('')

    async def _ensure_connected(self) -> None:
        if self._alive():
            return
        if self._restart_lock.locked():
            # Someone is already (re)connecting; wait for that instead.
            async with self._restart_lock:
                return
        if time.monotonic() < self._next_attempt:
            # Backing off from a dead host: fail fast instead of making every
            # keystroke wait out a connect timeout.
            self._schedule_restart()
            return
        if not await self._restart_client():
            self._schedule_restart()

    def _schedule_restart(self) -> None:
        if self._restart_task and not self._restart_task.done():
            return
        self._restart_task = asyncio.ensure_future(self._restart_loop())

    async def _restart_loop(self) -> None:
        while True:
            delay = self._next_attempt - time.monotonic()
            if delay > 0:
                await asyncio.sleep(delay)
            if await self._restart_client():
                return

    # ──────────────────────────────────────── async I/O

    def _on_readable(self) -> None:
        try:
            data = os.read(self._master_fd, 4096)
        except BlockingIOError:
            return
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
        if len(self._buf) > MAX_CONTROL_LINE:
            log.warning('control client (host=%s) sent a line over %d bytes; reconnecting',
                        self.host.name, MAX_CONTROL_LINE)
            self._buf = ''
            asyncio.ensure_future(self._reconnect())

    async def _reconnect(self) -> None:
        await self._cleanup_client()
        self._schedule_restart()

    async def _write(self, data: bytes) -> None:
        """Write all of `data` without blocking the loop; OSError if the
        client stops reading for too long."""
        fd = self._master_fd
        loop = asyncio.get_event_loop()
        deadline = loop.time() + 5.0
        while data:
            if fd is None or fd != self._master_fd:
                raise OSError('control client went away')
            try:
                n = os.write(fd, data)
                data = data[n:]
                continue
            except BlockingIOError:
                pass
            writable = loop.create_future()
            loop.add_writer(fd, lambda: writable.done() or writable.set_result(None))
            try:
                await asyncio.wait_for(writable, timeout=max(0.0, deadline - loop.time()))
            except asyncio.TimeoutError:
                raise OSError('control client is not reading') from None
            finally:
                with contextlib.suppress(Exception):
                    loop.remove_writer(fd)

    # ──────────────────────────────────────── commands

    async def send_command(self, cmd: str, *, target_gen: int | None = None) -> str:
        """Send one tmux command, await %begin/%end response."""
        return (await self.send_command_list([cmd], target_gen=target_gen))[0]

    async def send_command_list(
        self,
        cmds: list[str],
        on_end: Callable[[list[str]], None] | None = None,
        *,
        target_gen: int | None = None,
    ) -> list[str]:
        """Send commands as one `a ; b` line and return each one's response.

        tmux runs a command line as a single queue pass, without reading pane
        output in between, and answers every command with its own %begin/%end
        block. So the responses describe one instant of the pane, and pane
        output before/after that instant is ordered around them in the stream.

        on_end, if given, gets the responses synchronously while the last %end
        is handled: anything it broadcasts is ordered exactly after the %output
        that preceded the commands and before the %output that follows them.

        target_gen, if given, drops the commands when the control client has
        moved to another host since the caller looked at it.
        """
        empty = [''] * len(cmds)
        for attempt in range(2):
            await self._ensure_connected()
            if not self._alive():
                return empty
            if target_gen is not None and target_gen != self.target_gen:
                return empty

            loop = asyncio.get_event_loop()
            futs: list[asyncio.Future] = [loop.create_future() for _ in cmds]
            self._pending.extend(futs)
            if on_end is not None:
                self._on_end[futs[-1]] = lambda futs=futs: on_end([f.result() for f in futs])
            try:
                await self._write((' ; '.join(cmds) + '\n').encode('utf-8'))
            except OSError as e:
                self._drop_pending(futs)
                log.error('send_command write error: %s', e)
                await self._cleanup_client()
                if attempt == 0 and await self._restart_client():
                    continue
                self._schedule_restart()
                return empty

            try:
                return list(await asyncio.wait_for(
                    asyncio.shield(asyncio.gather(*futs)), timeout=5.0
                ))
            except asyncio.TimeoutError:
                self._drop_pending(futs)
                await self._cleanup_client()
                if attempt == 0 and await self._restart_client():
                    continue
                self._schedule_restart()
                return empty
        return empty

    def _drop_pending(self, futs: list[asyncio.Future]) -> None:
        for fut in futs:
            if fut in self._pending:
                self._pending.remove(fut)
            self._on_end.pop(fut, None)
            fut.cancel()

    async def _ensure_prefix_key_names(self) -> set[str]:
        if self._prefix_key_names is not None:
            return self._prefix_key_names

        names: set[str] = set()
        version = tmux_hosts.parse_tmux_version(
            'tmux ' + (await self.send_command('display-message -p "#{version}"')).strip())
        if version is not None and version < tmux_hosts.PREFIX_ROUTING_TMUX:
            # No send-keys -K before 3.4 (an older remote host): the prefix
            # key then reaches the pane like any other key.
            self._prefix_key_names = names
            return names
        for option in ('prefix', 'prefix2'):
            value = (await self.send_command(f'show-options -gv {option}')).strip()
            if value and value != 'None':
                names.add(value)
        self._prefix_key_names = names or {'C-b'}
        return self._prefix_key_names

    async def _send_literal_input(self, pane_id: str, data: bytes, target_gen: int) -> None:
        if not data:
            return
        chunk_size = 100
        target = f' -t {pane_id}' if pane_id else ''
        for i in range(0, len(data), chunk_size):
            chunk = data[i:i + chunk_size]
            hex_args = ' '.join(f'{b:02x}' for b in chunk)
            await self.send_command(f'send-keys{target} -H {hex_args}', target_gen=target_gen)

    async def _send_prefix_key(self, data: bytes, target_gen: int) -> int:
        key = _tmux_key_from_bytes(data)
        if not key:
            self._prefix_pending = False
            return 0
        key_name, used = key
        await self.send_command(f'send-keys -K {_tmux_quote(key_name)}', target_gen=target_gen)
        self._prefix_pending = False
        return used

    async def write_input(self, pane_id: str, data: bytes, *,
                          target_gen: int | None = None) -> None:
        """Inject input into a pane, preserving tmux prefix key bindings.

        Normal input goes directly to the selected pane. When the configured
        prefix key is seen, switch the control client to the `prefix` key table
        and send the following key through tmux's key processing.
        """
        if not data:
            return

        # The pane id came from the browser's view of one host (target_gen);
        # if the client moves to another host, the same id is someone else's
        # pane, so every command below is dropped from then on.
        gen = self.target_gen if target_gen is None else target_gen
        prefix_keys = await self._ensure_prefix_key_names()
        literal_start = 0
        i = 0
        while i < len(data):
            if self._prefix_pending:
                await self._send_literal_input(pane_id, data[literal_start:i], gen)
                used = await self._send_prefix_key(data[i:], gen)
                if used:
                    i += used
                    literal_start = i
                    continue
                literal_start = i

            key = _tmux_key_from_bytes(data[i:])
            if key and key[0] in prefix_keys:
                await self._send_literal_input(pane_id, data[literal_start:i], gen)
                await self.send_command('switch-client -T prefix', target_gen=gen)
                self._prefix_pending = True
                i += key[1]
                literal_start = i
                continue

            i += 1

        await self._send_literal_input(pane_id, data[literal_start:], gen)

    async def snapshot_pane(self, pane_id: str, deliver: Callable[[dict], None]) -> None:
        """Visible screen, cursor and terminal modes of one pane, atomically.

        get_snapshot fires on pane focus, layout changes, alt-screen exit and
        resize, and the client redraws these rows in place without touching its
        scrollback (buildSnapshotFrame in app.js). History arrives separately,
        once per pane, via capture_history().

        Both commands go out on one line so no pane output lands between them:
        every %output the stream carried before this response is already in the
        capture. deliver() runs at that exact point in the stream, so a client
        that queues it with its broadcasts can drop what it held back for the
        pane instead of replaying it on top of the redraw. It is not called if
        tmux never answers.
        """
        def on_end(responses: list[str]) -> None:
            state_raw, screen_raw = responses
            screen = _strip_terminal_response_sequences(
                _strip_zsh_eol_marks(_response_bytes(screen_raw))
            )
            state = _parse_pane_state(state_raw)
            if state['pane_cols'] <= 0 or state['pane_rows'] <= 0:
                return   # %error (pane gone): the client's watchdog releases it
            deliver({'data': screen, **state})

        await self.send_command_list([
            f'display-message -p -t {pane_id} "{_PANE_STATE_FORMAT}"',
            f'capture-pane -t {pane_id} -p -e -N -S 0',
        ], on_end=on_end)

    async def capture_history(self, pane_id: str, lines: int) -> bytes:
        """The scrollback *above* the visible screen (-E -1 stops before it).

        Sent once per pane to seed the client's scrollback; the client's buffer
        then grows from live output, so this cost is paid at attach only.
        """
        lines = max(1, min(int(lines), SNAPSHOT_SCROLLBACK_LINES))
        raw = await self.send_command(
            f'capture-pane -t {pane_id} -p -e -N -S -{lines} -E -1'
        )
        return _strip_terminal_response_sequences(_strip_zsh_eol_marks(_response_bytes(raw)))

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
            'host':        self.host.name,
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
                data, response_rem = _strip_terminal_response_sequences_stream(
                    data,
                    self._response_remainder.get(pane_id, b''),
                )
                if response_rem:
                    self._response_remainder[pane_id] = response_rem
                elif pane_id in self._response_remainder:
                    del self._response_remainder[pane_id]
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
                data, response_rem = _strip_terminal_response_sequences_stream(
                    data,
                    self._response_remainder.get(pane_id, b''),
                )
                if response_rem:
                    self._response_remainder[pane_id] = response_rem
                elif pane_id in self._response_remainder:
                    del self._response_remainder[pane_id]
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
        if line.startswith('%paste-buffer-changed '):
            self._on_paste_buffer_changed(line[22:])
            return True
        if line.startswith('%exit'):
            log.info('tmux session exited')
            self._schedule_restart()
            return True
        return False

    def _handle_line(self, line: str) -> None:
        if line.startswith(_DCS_START):
            line = line[len(_DCS_START):]
        if not self._ready.is_set():
            self._handle_attach_line(line)
            return
        # ── response demux ──
        if self._in_resp:
            # Every line of a block is command output, whatever it starts with:
            # a captured row can read "%end ..." or "%output ...". Only the
            # guard line repeating the %begin's arguments closes it.
            if not (line.startswith(('%end ', '%error '))
                    and line.split(' ', 1)[1] == self._resp_guard):
                self._add_response_line(line)
                return
        elif line.startswith('%begin '):
            self._in_resp, self._cur_resp = True, []
            self._resp_guard = line.split(' ', 1)[1]
            # Flags 0: a command tmux ran for this client on its own — a key
            # binding fired by send-keys -K, a hook — not a line we wrote.
            # Nothing is waiting for it; pairing it with the next pending
            # command would hand every later response to the wrong caller.
            self._resp_ours = self._resp_guard.rsplit(' ', 1)[-1] != '0'
            self._cur_resp_bytes, self._resp_overflow = 0, False
            return
        if line.startswith(('%end ', '%error ')):
            self._in_resp = False
            ours, self._resp_ours = self._resp_ours, True
            result = '' if self._resp_overflow else '\n'.join(self._cur_resp)
            self._cur_resp_bytes, self._resp_overflow = 0, False
            if self._pending and ours:
                fut = self._pending.pop(0)
                if not fut.done():
                    fut.set_result(result)
                callback = self._on_end.pop(fut, None)
                if callback is not None:
                    try:
                        callback()
                    except Exception:
                        log.exception('command response callback failed')
            self._cur_resp = []
            return
        if self._handle_notification_line(line):
            return
        if line.startswith('%'):
            log.debug('unhandled control line: %s', line[:200])

    def _add_response_line(self, line: str) -> None:
        if self._resp_overflow or not self._resp_ours:
            return
        self._cur_resp_bytes += len(line) + 1
        if self._cur_resp_bytes > MAX_RESPONSE_BYTES:
            log.warning('control client (host=%s): response over %d bytes; dropping it',
                        self.host.name, MAX_RESPONSE_BYTES)
            self._resp_overflow = True
            self._cur_resp = []
            return
        self._cur_resp.append(line)

    def _handle_attach_line(self, line: str) -> None:
        """Lines before the attach is answered. The attach command's own
        %begin/%end comes first and belongs to no pending command; anything
        that isn't control protocol is ssh or the remote shell talking."""
        if line.startswith('%end '):
            self._ready.set()
        elif line.startswith('%exit'):
            pass
        elif line and not line.startswith('%') and line != '\x1b\\':
            # Not terminal content: nothing is attached yet.
            log.warning('control client (host=%s): %s',
                        self.host.name, tmux_hosts.sanitize(line))

    # ──────────────────────────────────────── clipboard

    def _on_paste_buffer_changed(self, name: str) -> None:
        # Applications in a pane copy into tmux paste buffers (Claude Code's
        # mouse selection, copy-mode, OSC 52 with set-clipboard on). A control
        # client has no terminal clipboard for tmux to set, so forward the new
        # buffer to the browsers, which put it on the OS clipboard.
        self._clipboard_seq += 1
        task = asyncio.get_running_loop().create_task(
            self._forward_paste_buffer(name, self._clipboard_seq)
        )
        self._clipboard_tasks.add(task)
        task.add_done_callback(self._clipboard_tasks.discard)

    async def _forward_paste_buffer(self, name: str, seq: int) -> None:
        # save-buffer gives the exact bytes; show-buffer through the control
        # channel would lose the trailing newline in the %begin/%end framing.
        gen = self.target_gen
        rc, text, _ = await tmux_hosts.run(self.host, 'save-buffer', '-b', name, '-')
        # A later copy finished first or is still on its way; it wins. And a
        # buffer read from a host we have since left is that host's, not ours.
        if seq != self._clipboard_seq or gen != self.target_gen:
            return
        if rc != 0 or not text:
            return
        data = text.encode('utf-8', errors='surrogateescape')
        if len(data) > CLIPBOARD_MAX_BYTES:
            log.info('paste buffer %s is %d bytes, not forwarded to the clipboard', name, len(data))
            return
        self._broadcast({'type': 'clipboard', 'text': data.decode('utf-8', errors='replace')})

    def _broadcast(self, msg: dict) -> None:
        if not self.subscribers:
            return
        data = json.dumps(msg)
        dead = [subscriber for subscriber in self.subscribers if not subscriber.enqueue(data)]
        for subscriber in dead:
            if subscriber in self.subscribers:
                self.subscribers.remove(subscriber)


# ──────────────────────────────────────────── helpers

# What a snapshot needs besides the screen text: where the cursor is and the
# modes the pane's application set, which the client's redraw has to put back
# (a lost scroll region makes every later scroll move the wrong rows).
_PANE_STATE_FIELDS: tuple[tuple[str, str, int], ...] = (
    ('cursor_x',            'cursor_x',            0),
    ('cursor_y',            'cursor_y',            0),
    ('pane_cols',           'pane_width',          0),
    ('pane_rows',           'pane_height',         0),
    ('alternate_on',        'alternate_on',        0),
    ('scroll_region_upper', 'scroll_region_upper', 0),
    ('scroll_region_lower', 'scroll_region_lower', -1),
    ('origin_flag',         'origin_flag',         0),
    ('wrap_flag',           'wrap_flag',           1),
    ('insert_flag',         'insert_flag',         0),
    ('keypad_cursor_flag',  'keypad_cursor_flag',  0),
    ('keypad_flag',         'keypad_flag',         0),
    ('cursor_flag',         'cursor_flag',         1),
    ('mouse_standard_flag', 'mouse_standard_flag', 0),
    ('mouse_button_flag',   'mouse_button_flag',   0),
    ('mouse_all_flag',      'mouse_all_flag',      0),
    ('mouse_sgr_flag',      'mouse_sgr_flag',      0),
)
_PANE_STATE_FORMAT = '|'.join(f'#{{{fmt}}}' for _, fmt, _ in _PANE_STATE_FIELDS)


def _parse_pane_state(raw: str) -> dict:
    """display-message output → ints; anything missing keeps its default."""
    parts = raw.strip().split('|')
    state = {}
    for i, (key, _, default) in enumerate(_PANE_STATE_FIELDS):
        value = parts[i] if i < len(parts) else ''
        state[key] = int(value) if value.isdigit() else default
    return state


def _set_winsize(fd: int, rows: int, cols: int) -> None:
    winsize = struct.pack('HHHH', rows, cols, 0, 0)
    fcntl.ioctl(fd, termios.TIOCSWINSZ, winsize)
