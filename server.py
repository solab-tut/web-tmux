#!/usr/bin/env python3
from __future__ import annotations

"""
web-tmux control mode server

  HTTP  127.0.0.1:8766  — static files
  WS    127.0.0.1:8765  — terminal events
"""
import asyncio
import base64
import contextlib
import json
import logging
import os
import re
import secrets
import signal
import threading
import time
from datetime import datetime
from functools import partial
from http import HTTPStatus
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

from websockets.asyncio.server import ServerConnection, serve
from websockets.exceptions import ConnectionClosed

import askpass
import layout_store
import tmux_hosts
from layout_parser import parse_layout
from layout_store import Ref
from tmux_control import SNAPSHOT_SCROLLBACK_LINES, TmuxControl
from tmux_hosts import LOCAL, LOCAL_HOST, TmuxHost
from web_security import AccessController

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
AUTH_PATH     = '/auth/session'
VENDOR_PREFIX = '/vendor/'

MAX_CONNECTIONS = 8
MAX_INPUT_BYTES = 8 * 1024
MAX_CLIENT_LOG_CHARS = 200
MAX_OUTBOUND_QUEUE_BYTES = 1024 * 1024
WS_MAX_MESSAGE_BYTES = 64 * 1024
CONTROL_RATE = 30.0
INPUT_RATE_BYTES = 128 * 1024.0
INPUT_BURST_BYTES = 256 * 1024.0

ACCESS = AccessController.from_env()
# Where tmux may run: this machine first, then the ssh_config aliases in
# WEB_TMUX_SSH_HOSTS. The browser can only ever name one of these.
HOSTS: dict[str, TmuxHost] = {h.name: h for h in (LOCAL_HOST, *tmux_hosts.hosts_from_env())}
DIRECTORY = tmux_hosts.HostDirectory(HOSTS.values(), on_change=lambda: _on_hosts_changed())
tmux = None  # TmuxControl
_active_connections: set[ServerConnection] = set()
_clients: set = set()   # ClientConnection, subscribed to tmux output or not
_allowed_sessions: dict[str, set[str]] = {}   # per host
_allowed_windows: set[int] = set()            # the active host's only
_allowed_panes: set[str] = set()
_layout_lock = asyncio.Lock()   # save/restore/delete run one at a time
_host_lock = asyncio.Lock()     # one host switch at a time
_last_size: tuple[int, int] | None = None   # the active tab's last refresh-client -C
ASKPASS: askpass.AskpassBroker | None = None
# Sign-in prompts waiting on a browser: id -> (the client asked, its answer)
_auth_prompts: dict[str, tuple[object, asyncio.Future]] = {}
AUTH_PROMPT_TIMEOUT = askpass.HELPER_TIMEOUT - 10
_AUTH_ID_RE = re.compile(r'^[A-Za-z0-9_-]{1,32}$')
_restore_seq = 0


def _single_header(headers, name: str) -> str | None:
    values = headers.get_all(name) or []
    return values[0] if len(values) == 1 else None


class TokenBucket:
    def __init__(self, rate: float, capacity: float) -> None:
        self.rate = rate
        self.capacity = capacity
        self.tokens = capacity
        self.updated = time.monotonic()

    def consume(self, amount: float = 1.0) -> bool:
        now = time.monotonic()
        self.tokens = min(self.capacity, self.tokens + (now - self.updated) * self.rate)
        self.updated = now
        if amount > self.tokens:
            return False
        self.tokens -= amount
        return True


def _cache_headers(path: str) -> tuple[tuple[str, str], ...]:
    """Caching rules per kind of response.

    `no-store` on the document would be the safe default, but it also makes the
    page ineligible for the back/forward cache in every major browser: a tab
    that has been away for a while can then only come back by re-fetching the
    document, and on a phone that request lands exactly when the network is
    least likely to answer. Nothing served here is secret — the terminal itself
    only ever travels over the WebSocket — so the shell is merely revalidated,
    the versioned vendor files are cached outright, and only the response that
    carries the session cookie stays unstored.
    """
    if path == AUTH_PATH:
        return (
            ('Cache-Control', 'no-store, no-cache, must-revalidate, max-age=0'),
            ('Pragma', 'no-cache'),
            ('Expires', '0'),
        )
    if path.startswith(VENDOR_PREFIX):
        return (('Cache-Control', 'public, max-age=31536000, immutable'),)
    return (('Cache-Control', 'no-cache'),)


def _security_headers() -> tuple[tuple[str, str], ...]:
    connect_sources = ' '.join(("'self'", *ACCESS.csp_connect_sources))
    csp = '; '.join((
        "default-src 'self'",
        "base-uri 'none'",
        f'connect-src {connect_sources}',
        "font-src 'self'",
        "form-action 'none'",
        "frame-ancestors 'none'",
        "img-src 'self' data:",
        "object-src 'none'",
        "script-src 'self'",
        "style-src 'self'",
        "worker-src 'self'",   # the app shell's service worker, /sw.js
    ))
    return (
        ('Content-Security-Policy', csp),
        ('Cross-Origin-Opener-Policy', 'same-origin'),
        ('Cross-Origin-Resource-Policy', 'same-origin'),
        # The page writes what pane applications copy (tmux paste buffers) to
        # the OS clipboard; reading the clipboard stays off.
        ('Permissions-Policy', 'clipboard-read=(), clipboard-write=(self)'),
        ('Referrer-Policy', 'no-referrer'),
        ('X-Content-Type-Options', 'nosniff'),
        ('X-Frame-Options', 'DENY'),
    )


class SecureStaticHandler(SimpleHTTPRequestHandler):
    server_version = 'web-tmux'
    sys_version = ''

    def version_string(self) -> str:
        return self.server_version

    def end_headers(self) -> None:
        for name, value in _cache_headers(self._path()):
            self.send_header(name, value)
        for name, value in _security_headers():
            self.send_header(name, value)
        super().end_headers()

    def _path(self) -> str:
        try:
            return urlsplit(self.path).path
        except ValueError:
            return ''

    # The completion line below (log_request) only appears once a response has
    # been written. Logging the arrival too is what tells a request that never
    # reached us apart from one that reached us and stalled.
    def _log_arrival(self) -> None:
        log.info('http start %s %s', self.command, self._path()[:128])

    def _authorize(self):
        return ACCESS.authorize_http(
            _single_header(self.headers, 'Host') or '',
            _single_header(self.headers, 'Tailscale-User-Login'),
        )

    def _forbidden(self) -> None:
        log.warning('HTTP request rejected path=%s', urlsplit(self.path).path[:128])
        self.send_response(HTTPStatus.FORBIDDEN)
        self.send_header('Content-Length', '0')
        self.end_headers()

    def _auth_session(self, context) -> None:
        self.send_response(HTTPStatus.NO_CONTENT)
        self.send_header('Set-Cookie', ACCESS.session_cookie(context))
        self.send_header('Content-Length', '0')
        self.end_headers()

    def do_GET(self) -> None:
        self._log_arrival()
        context = self._authorize()
        if context is None:
            self._forbidden()
            return
        if self._path() == AUTH_PATH:
            self._auth_session(context)
            return
        super().do_GET()

    def do_HEAD(self) -> None:
        self._log_arrival()
        context = self._authorize()
        if context is None:
            self._forbidden()
            return
        if self._path() == AUTH_PATH:
            self._auth_session(context)
            return
        super().do_HEAD()

    def list_directory(self, path):
        self.send_error(HTTPStatus.NOT_FOUND)
        return None


def _pane_id(value) -> str:
    if not isinstance(value, str):
        return ''
    return value if value.startswith('%') and value[1:].isdigit() else ''


def _window_index(value) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value if value >= 0 else None


def _session_name(value) -> str:
    if not isinstance(value, str):
        return ''
    value = value.strip()
    if not value or len(value) > 128 or any(ch in value for ch in '\r\n\0:'):
        return ''
    return value


def _window_name(value) -> str:
    if not isinstance(value, str):
        return ''
    value = value.strip()
    if not value or len(value) > 128 or any(ch in value for ch in '\r\n\0'):
        return ''
    return value


def _layout_name(value) -> str:
    # Layout slot names are store keys, not tmux targets, so they are checked
    # for shape only — _allowed_sessions can't help here, since a slot names a
    # session that may not exist yet (that's the point of restoring it).
    if not isinstance(value, str):
        return ''
    value = value.strip()
    if not value or len(value) > layout_store.MAX_SLOT_NAME:
        return ''
    if any(ord(ch) < 0x20 or ord(ch) == 0x7f for ch in value):
        return ''
    return value


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


def _hosts_payload(state: dict | None = None) -> list[dict]:
    """Every host and its sessions, for the sidebar. The active host's list
    comes from `state` (its control client) when there is one."""
    active = tmux.host.name
    hosts = DIRECTORY.snapshot()
    for entry in hosts:
        entry['active'] = entry['host'] == active
        if entry['active'] and state is not None:
            entry.update(reachable=True, code='', sessions=state['sessions'])
        else:
            for session in entry['sessions']:
                session['active'] = entry['active'] and session['name'] == tmux.session
    return hosts


def _view_message(msg_type: str, state: dict, *, with_layout: bool = True) -> str:
    msg = {
        'type':        msg_type,
        # Which host's pane ids these are. The browser echoes it back on
        # everything aimed at a pane (see _stale_view).
        'gen':         tmux.target_gen,
        'host':        state.get('host', LOCAL),
        'session':     state['session'],
        'sessions':    state['sessions'],
        'hosts':       _hosts_payload(state),
        'windows':     state['windows'],
        'panes':       state['panes'],
        'active_pane': state['active_pane'],
    }
    if with_layout:
        msg['layout'] = state.get('layout', '')
        msg['layout_panes'] = parse_layout(state.get('layout', ''))
    return json.dumps(msg)


async def _send_current_view(websocket, msg_type: str = 'window_switched') -> None:
    state = await tmux.get_initial_state()
    _update_allowed_targets(state)
    await websocket.send(_view_message(msg_type, state))


async def _send_state(websocket) -> None:
    state = await tmux.get_initial_state()
    _update_allowed_targets(state)
    await websocket.send(_view_message('state', state, with_layout=False))


def _on_hosts_changed() -> None:
    if tmux is None:
        return
    for name in HOSTS:
        if name != tmux.host.name:
            _allowed_sessions[name] = DIRECTORY.sessions(name)
    data = json.dumps({'type': 'hosts', 'hosts': _hosts_payload()})
    for client in list(_clients):
        client.enqueue(data)


def _refresh_hosts_soon(force: bool = False) -> None:
    # Never awaited by a message handler: a host that doesn't answer must not
    # hold up the connection that happened to ask.
    asyncio.ensure_future(DIRECTORY.refresh_all(force))


async def _run_tmux(*args: str, host: TmuxHost | None = None) -> int:
    rc, _, _ = await tmux_hosts.run(host or tmux.host, *args)
    return rc


async def _run_tmux_capture(*args: str, host: TmuxHost | None = None) -> tuple[int, str]:
    """Like _run_tmux, but hands back stdout. Used where failure must be seen —
    TmuxControl.send_command reports %error as success, so it can't be trusted
    for save/restore."""
    rc, out, _ = await tmux_hosts.run(host or tmux.host, *args)
    return rc, out


async def _run_tmux_size_safe(*args: str, host: TmuxHost | None = None) -> int:
    # Keep window-size=latest so the active control client size is reflected in
    # the current window. Reassert it around mutating commands for tmux 3.6a.
    await _run_tmux('set-option', '-g', 'window-size', 'latest', host=host)
    try:
        return await _run_tmux(*args, host=host)
    finally:
        await _run_tmux('set-option', '-g', 'window-size', 'latest', host=host)


# ──────────────────────────────────────────── HTTP (static files, background thread)

def _start_http() -> None:
    handler = partial(SecureStaticHandler, directory=STATIC_DIR)
    server = ThreadingHTTPServer(('127.0.0.1', HTTP_PORT), handler)
    server.daemon_threads = True
    log.info('HTTP  http://127.0.0.1:%d/', HTTP_PORT)
    server.serve_forever()


# ──────────────────────────────────────────── WebSocket

class ClientConnection:
    """WebSocket wrapper with a bounded queue for unsolicited terminal output."""

    def __init__(self, websocket: ServerConnection) -> None:
        self.websocket = websocket
        self.remote_address = websocket.remote_address
        self.control_bucket = TokenBucket(CONTROL_RATE, CONTROL_RATE)
        self.input_bucket = TokenBucket(INPUT_RATE_BYTES, INPUT_BURST_BYTES)
        self._queue: asyncio.Queue[tuple[str, int]] = asyncio.Queue()
        self._queued_bytes = 0
        self._send_lock = asyncio.Lock()
        self._closed = False
        self._close_task = None
        self._writer_task = asyncio.create_task(self._writer())

    async def send(self, data: str) -> None:
        async with self._send_lock:
            await self.websocket.send(data)

    def enqueue(self, data: str) -> bool:
        if self._closed:
            return False
        size = len(data.encode('utf-8'))
        if self._queued_bytes + size > MAX_OUTBOUND_QUEUE_BYTES:
            if self._close_task is None:
                self._close_task = asyncio.create_task(
                    self.websocket.close(code=1013, reason='client is too slow')
                )
            return False
        self._queued_bytes += size
        self._queue.put_nowait((data, size))
        return True

    async def _writer(self) -> None:
        try:
            while True:
                data, size = await self._queue.get()
                self._queued_bytes -= size
                async with self._send_lock:
                    await self.websocket.send(data)
        except (ConnectionClosed, asyncio.CancelledError):
            pass
        except Exception as exc:
            log.warning('broadcast writer stopped: %s', type(exc).__name__)

    async def close(self, code: int, reason: str) -> None:
        await self.websocket.close(code=code, reason=reason)

    async def stop(self) -> None:
        self._closed = True
        self._writer_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await self._writer_task
        if self._close_task is not None:
            with contextlib.suppress(Exception):
                await self._close_task


class PolicyViolation(ValueError):
    pass


def _update_allowed_targets(state: dict) -> None:
    global _allowed_windows, _allowed_panes
    host = state.get('host', LOCAL)
    _allowed_sessions[host] = {item['name'] for item in state.get('sessions', [])}
    _allowed_windows = {item['index'] for item in state.get('windows', [])}
    _allowed_panes = {item['id'] for item in state.get('panes', [])}
    DIRECTORY.set_sessions(host, state.get('sessions', []))


def _active_host() -> str:
    return tmux.host.name if tmux is not None else LOCAL


def _msg_host(msg: dict) -> str:
    """The host a message is about: its `host` field, else the active one."""
    host = msg.get('host')
    if host is None:
        return _active_host()
    if not isinstance(host, str) or host not in HOSTS:
        raise PolicyViolation('unknown host')
    return host


def _stale_view(msg: dict) -> bool:
    """True for a pane message composed against an earlier host.

    Pane ids are per tmux server: '%3' after a host switch is a different
    pane on a different machine. A browser that hasn't processed the new
    `init` yet — another tab, a phone flushing keystrokes on wake — still
    sends ids from the old view, and they may well be valid on the new host.
    `gen` says which view it was looking at.
    """
    return 'gen' in msg and msg['gen'] != tmux.target_gen


_MESSAGE_FIELDS = {
    # `gen` is required on input: keystrokes are what must never reach the
    # wrong machine.
    'input': ({'type', 'pane', 'data', 'gen'}, {'pane', 'data', 'gen'}),
    'client_active': ({'type'}, set()),
    'client_log': ({'type', 'text'}, {'text'}),
    'resize': ({'type', 'cols', 'rows'}, {'cols', 'rows'}),
    # `host` on window messages names the host the browser was looking at;
    # one sent just before a host switch is dropped instead of hitting the
    # same window index on the new host.
    'new_window': ({'type', 'host'}, set()),
    'new_session': ({'type', 'name', 'host'}, set()),
    'select_session': ({'type', 'session', 'host'}, {'session'}),
    'rename_session': ({'type', 'session', 'name', 'host'}, {'session', 'name'}),
    'rename_window': ({'type', 'window', 'name', 'host'}, {'window', 'name'}),
    'kill_session': ({'type', 'session', 'host'}, {'session'}),
    'kill_window': ({'type', 'window', 'host'}, {'window'}),
    'split_window': ({'type', 'direction', 'pane', 'gen'}, {'direction'}),
    'get_snapshot': ({'type', 'pane', 'gen'}, {'pane'}),
    'get_history': ({'type', 'pane', 'lines', 'gen'}, {'pane'}),
    'get_state': ({'type'}, set()),
    'get_current_view': ({'type'}, set()),
    'select_pane': ({'type', 'pane', 'force_zoom', 'toggle_zoom', 'gen'}, {'pane'}),
    'select_window': ({'type', 'window', 'host'}, {'window'}),
    'refresh_hosts': ({'type'}, set()),
    'connect_host': ({'type', 'host'}, {'host'}),
    'host_auth_answer': ({'type', 'id', 'answer', 'cancel'}, {'id'}),
    'list_layouts': ({'type'}, set()),
    'save_layout': ({'type', 'name', 'overwrite'}, {'name'}),
    'restore_layout': ({'type', 'name', 'mode'}, {'name'}),
    'delete_layout': ({'type', 'name'}, {'name'}),
}

_RESTORE_MODES = {'auto', 'replace', 'duplicate', 'skip'}


def _validated_message(raw: str | bytes) -> dict:
    if not isinstance(raw, str):
        raise PolicyViolation('binary messages are not supported')
    try:
        msg = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise PolicyViolation('invalid JSON') from exc
    if not isinstance(msg, dict):
        raise PolicyViolation('message must be an object')
    msg_type = msg.get('type')
    if not isinstance(msg_type, str) or msg_type not in _MESSAGE_FIELDS:
        raise PolicyViolation('unknown message type')
    allowed, required = _MESSAGE_FIELDS[msg_type]
    if set(msg) - allowed or required - set(msg):
        raise PolicyViolation('invalid message fields')
    if 'gen' in msg and (isinstance(msg['gen'], bool) or not isinstance(msg['gen'], int)
                         or msg['gen'] < 0):
        raise PolicyViolation('invalid view generation')

    if msg_type == 'input':
        pane, data = _pane_id(msg['pane']), msg['data']
        if not pane or pane not in _allowed_panes or not isinstance(data, str) or not data:
            raise PolicyViolation('invalid input target or data')
        if len(data.encode('utf-8')) > MAX_INPUT_BYTES:
            raise PolicyViolation('input is too large')
    elif msg_type == 'client_log':
        text = msg['text']
        # Control characters would let a client forge whole log lines.
        if (not isinstance(text, str) or not text
                or len(text) > MAX_CLIENT_LOG_CHARS
                or any(ch < ' ' or ch == '\x7f' for ch in text)):
            raise PolicyViolation('invalid client log')
    elif msg_type == 'resize':
        cols, rows = msg['cols'], msg['rows']
        if (isinstance(cols, bool) or not isinstance(cols, int) or not 10 <= cols <= 500
                or isinstance(rows, bool) or not isinstance(rows, int) or not 5 <= rows <= 200):
            raise PolicyViolation('invalid terminal size')
    elif msg_type == 'new_session':
        _msg_host(msg)
        name = msg.get('name')
        if name not in ('', None) and (not isinstance(name, str) or not _session_name(name)):
            raise PolicyViolation('invalid session name')
    elif msg_type in {'select_session', 'kill_session'}:
        sessions = _allowed_sessions.get(_msg_host(msg), set())
        if not isinstance(msg['session'], str) or _session_name(msg['session']) not in sessions:
            raise PolicyViolation('unknown session')
    elif msg_type == 'rename_session':
        sessions = _allowed_sessions.get(_msg_host(msg), set())
        if (not isinstance(msg['session'], str) or _session_name(msg['session']) not in sessions
                or not isinstance(msg['name'], str) or not _session_name(msg['name'])):
            raise PolicyViolation('invalid session rename')
    elif msg_type == 'new_window':
        _msg_host(msg)
    elif msg_type == 'connect_host':
        if _msg_host(msg) == LOCAL:
            raise PolicyViolation('unknown host')
    elif msg_type == 'host_auth_answer':
        if not isinstance(msg['id'], str) or not _AUTH_ID_RE.match(msg['id']):
            raise PolicyViolation('invalid sign-in prompt')
        answer = msg.get('answer', '')
        # The helper hands ssh one line: a newline would end the answer early.
        if (not isinstance(answer, str) or len(answer) > askpass.MAX_ANSWER
                or any(ch in answer for ch in '\r\n\0')):
            raise PolicyViolation('invalid sign-in answer')
        if 'cancel' in msg and not isinstance(msg['cancel'], bool):
            raise PolicyViolation('invalid sign-in answer')
    elif msg_type in {'kill_window', 'select_window'}:
        # A window index from another host's view is ignored by the handler,
        # not a violation: the browser may simply not have seen the switch.
        if _msg_host(msg) == _active_host() and _window_index(msg['window']) not in _allowed_windows:
            raise PolicyViolation('unknown window')
    elif msg_type == 'rename_window':
        if ((_msg_host(msg) == _active_host() and _window_index(msg['window']) not in _allowed_windows)
                or not isinstance(msg['name'], str) or not _window_name(msg['name'])):
            raise PolicyViolation('invalid window rename')
    elif msg_type == 'split_window':
        if not isinstance(msg['direction'], str) or msg['direction'] not in {'h', 'v'}:
            raise PolicyViolation('invalid split direction')
        if 'pane' in msg and msg['pane'] not in ('', None):
            pane = _pane_id(msg['pane'])
            if not pane or pane not in _allowed_panes:
                raise PolicyViolation('unknown pane')
    elif msg_type in {'get_snapshot', 'get_history', 'select_pane'}:
        pane = _pane_id(msg['pane'])
        if not pane or pane not in _allowed_panes:
            raise PolicyViolation('unknown pane')
        if msg_type == 'get_history' and 'lines' in msg:
            lines = msg['lines']
            if isinstance(lines, bool) or not isinstance(lines, int) or not 1 <= lines <= SNAPSHOT_SCROLLBACK_LINES:
                raise PolicyViolation('invalid history size')
        if msg_type == 'select_pane':
            for key in ('force_zoom', 'toggle_zoom'):
                if key in msg and not isinstance(msg[key], bool):
                    raise PolicyViolation('invalid zoom flag')
    elif msg_type in {'save_layout', 'restore_layout', 'delete_layout'}:
        if not _layout_name(msg['name']):
            raise PolicyViolation('invalid layout name')
        if msg_type == 'save_layout' and 'overwrite' in msg and not isinstance(msg['overwrite'], bool):
            raise PolicyViolation('invalid overwrite flag')
        if msg_type == 'restore_layout' and 'mode' in msg and msg['mode'] not in _RESTORE_MODES:
            raise PolicyViolation('invalid restore mode')
    return msg


_resize_master = None   # only the active browser tab/device may send resize


async def ws_handler(websocket: ServerConnection) -> None:
    global _resize_master
    if len(_active_connections) >= MAX_CONNECTIONS:
        await websocket.close(code=1013, reason='too many connections')
        return

    client = ClientConnection(websocket)
    _active_connections.add(websocket)
    _clients.add(client)
    log.info('ws connect %s', websocket.remote_address)
    if _resize_master is None:
        _resize_master = client
    tmux.subscribers.append(client)
    try:
        state = await tmux.get_initial_state()
        _update_allowed_targets(state)
        await client.send(_view_message('init', state))
        _refresh_hosts_soon()

        async for raw in websocket:
            try:
                msg = _validated_message(raw)
            except PolicyViolation as exc:
                log.warning('ws policy violation: %s', exc)
                await client.close(1008, str(exc)[:120])
                break
            if msg['type'] == 'input':
                charge = max(64, len(msg['data'].encode('utf-8')))
                if not client.input_bucket.consume(charge):
                    await client.close(1008, 'input rate exceeded')
                    break
            elif not client.control_bucket.consume():
                await client.close(1008, 'control rate exceeded')
                break
            await _handle_msg(client, msg)

    except ConnectionClosed:
        pass
    except Exception as exc:
        log.error('ws error: %s', type(exc).__name__)
    finally:
        _clients.discard(client)
        for owner, answer in list(_auth_prompts.values()):
            if owner is client and not answer.done():
                answer.set_result(None)
        if client in tmux.subscribers:
            tmux.subscribers.remove(client)
        if client is _resize_master:
            _resize_master = tmux.subscribers[-1] if tmux.subscribers else None
        _active_connections.discard(websocket)
        await client.stop()
        log.info('ws disconnect %s', websocket.remote_address)


class _RestoreFailed(Exception):
    pass


async def _send_layouts(websocket, store: dict | None = None) -> None:
    if store is None:
        store = layout_store.load_store()
    await websocket.send(json.dumps({
        'type':    'layouts',
        'layouts': layout_store.slot_summaries(store),
    }))


async def _send_layout_error(websocket, name: str, code: str) -> None:
    # Fixed codes only — tmux's own error text never reaches the browser.
    await websocket.send(json.dumps({'type': 'layout_error', 'name': name, 'code': code}))


async def _live_sessions_info(host: TmuxHost | None = None) -> dict[str, bool]:
    """{session_name: attached} for every session currently running."""
    rc, raw = await _run_tmux_capture(
        'list-sessions', '-F', '#{session_name}\t#{session_attached}', host=host)
    if rc != 0:
        return {}
    info: dict[str, bool] = {}
    for line in raw.splitlines():
        name, _, attached = line.partition('\t')
        if name:
            info[name] = attached not in ('', '0')   # mirrors tmux_control.py's own check
    return info


async def _live_session_names(host: TmuxHost | None = None) -> set[str]:
    return set(await _live_sessions_info(host))


def _absolute_path(path: str) -> bool:
    return path.startswith('/')


async def _capture_one_session(session: str, attached: bool,
                               host: TmuxHost = LOCAL_HOST) -> dict | None:
    rc, win_raw = await _run_tmux_capture(
        'list-windows', '-t', session, '-F', layout_store.WIN_FMT, host=host)
    if rc != 0:
        return None
    rc, pane_raw = await _run_tmux_capture(
        'list-panes', '-s', '-t', session, '-F', layout_store.PANE_FMT, host=host)
    if rc != 0:
        return None
    if host.is_local:
        return layout_store.parse_session_snapshot(session, win_raw, pane_raw, attached=attached)
    # Whether a remote cwd still exists is checked on that host at restore.
    return layout_store.parse_session_snapshot(
        session, win_raw, pane_raw, attached=attached, host=host.name,
        isdir=_absolute_path, home='/')


async def _capture_host(host: TmuxHost) -> tuple[list[dict] | None, str]:
    """(sessions, '') for a host that was read; (None, code) for one that
    can't be reached, which the save skips; ([], code) when a reachable host
    failed part-way, which fails the save."""
    code = await DIRECTORY.preflight(host)
    if code:
        return None, code
    live = await _live_sessions_info(host)
    if len(live) > layout_store.MAX_SESSIONS_PER_HOST:
        return [], 'too_large'
    sessions = []
    for session, attached in live.items():
        one = await _capture_one_session(session, attached, host)
        if one is None:
            return [], 'capture_failed'
        sessions.append(one)
    return sessions, ''


async def _capture_all_sessions(name: str) -> tuple[dict | None, str, list[str]]:
    """Snapshot every currently open session, on every host, under one slot.

    All-or-nothing per reachable host: if any one session can't be read, the
    whole save is refused rather than silently saving a workspace with a
    session missing. A host that can't be reached at all is left out and
    named in the third value, so the browser can say so.
    """
    hosts = list(HOSTS.values())
    captured = await asyncio.gather(*(_capture_host(h) for h in hosts))
    sessions: list[dict] = []
    skipped: list[str] = []
    for host, (got, code) in zip(hosts, captured):
        if got is None:
            skipped.append(host.name)
            continue
        if code:
            return None, code, skipped
        sessions.extend(got)
    if not sessions:
        return None, 'capture_failed', skipped
    now = datetime.now().astimezone().isoformat(timespec='seconds')
    snap = layout_store.build_snapshot(name, sessions, now=now)
    return (snap, '', skipped) if snap else (None, 'too_large', skipped)


# Prints $HOME, then each directory named on stdin that exists. One round
# trip for a whole host's worth of cwds, and no path ever goes through a
# command line.
_DIR_CHECK = ('printf "%s\\n" "$HOME"; '
              'while IFS= read -r d; do if [ -d "$d" ]; then printf "%s\\n" "$d"; fi; done')


async def _existing_dirs(host: TmuxHost, cwds: list[str]) -> tuple[set[str], str] | None:
    """(the cwds that exist on `host`, its $HOME), or None if it didn't answer."""
    if host.is_local:
        return {d for d in cwds if os.path.isdir(d)}, os.path.expanduser('~')
    rc, out, _ = await tmux_hosts.run_shell(host, _DIR_CHECK, ''.join(f'{d}\n' for d in cwds).encode())
    lines = out.split('\n')
    if rc != 0 or not lines[0].startswith('/'):
        return None
    return set(lines[1:]) & set(cwds), lines[0]


async def _build_temp_session(session_snap: dict, temp: str, host: TmuxHost,
                              dirs: tuple[set[str], str]) -> None:
    """Run the restore plan. Raises _RestoreFailed leaving nothing but `temp`."""
    captured: dict[str, str] = {}
    first_window = ''
    existing, home = dirs

    for step in layout_store.build_restore_steps(session_snap, temp):
        op = step['op']

        if op == 'move_first_window':
            # new-session lands on base-index; nudge it to the saved index.
            if first_window and first_window != str(step['window']):
                rc = await _run_tmux('move-window',
                                     '-s', f'{step["session"]}:{first_window}',
                                     '-t', f'{step["session"]}:{step["window"]}',
                                     host=host)
                if rc != 0:
                    raise _RestoreFailed('move-window')
            continue

        argv = layout_store.cwd_fallback(
            [captured[a.key] if isinstance(a, Ref) else a for a in step['argv']], existing, home)
        rc, out = await _run_tmux_capture(*argv, host=host)
        if rc != 0:
            if op == 'layout':
                # Only the geometry is lost; the panes themselves are fine.
                log.warning('restore: select-layout failed for %s', argv[2])
                continue
            raise _RestoreFailed(op)

        if 'capture' in step:
            fields = out.strip().split('\t')
            if op == 'new_session':
                if len(fields) != 2:
                    raise _RestoreFailed(op)
                first_window, pane = fields
            else:
                pane = fields[0]
            if not pane:
                raise _RestoreFailed(op)
            captured[step['capture']] = pane


async def _restore_one_session(session_snap: dict, target: str, mode: str,
                                live: set[str], host: TmuxHost,
                                dirs: tuple[set[str], str]) -> tuple[bool, str]:
    """Rebuild one session's shape under a throwaway name, then rename it to
    `target`. Building elsewhere first means a failure costs nothing: the
    session already running under `target` (if any) is untouched until the
    very end. Returns (succeeded, the name it actually ended up under).

    Only when `target` is the session the control client is presently
    attached to (`tmux.session` on `tmux.host`) does killing/renaming it risk
    the control client itself — TmuxControl recreates tmux.session on %exit,
    so that one session needs the switch-client-before-kill dance. Every
    other session in the batch is unattached from the control client's point
    of view and can be killed/renamed directly.
    """
    global _restore_seq
    _restore_seq += 1
    temp = f'web-tmux-restore-{os.getpid()}-{_restore_seq}'
    if not _session_name(target) or temp in live:
        return False, target

    await _run_tmux('set-option', '-g', 'window-size', 'latest', host=host)
    try:
        await _build_temp_session(session_snap, temp, host, dirs)
    except _RestoreFailed as exc:
        log.warning('restore of session %r on %s failed at %s; discarding %s',
                    session_snap['session'], host.name, exc, temp)
        await _run_tmux('kill-session', '-t', temp, host=host)
        return False, target
    finally:
        await _run_tmux('set-option', '-g', 'window-size', 'latest', host=host)

    is_attached_here = host.name == tmux.host.name and target == tmux.session
    if is_attached_here:
        await tmux.send_command(f'switch-client -t {_tmux_quote(temp)}')
        tmux.session = temp
    if mode == 'replace' and target in live:
        await _run_tmux('kill-session', '-t', target, host=host)
    if await _run_tmux('rename-session', '-t', temp, target, host=host) == 0:
        if is_attached_here:
            tmux.session = target
        return True, target
    log.warning('restore: could not rename %s to %r; leaving it as-is', temp, target)
    if is_attached_here:
        tmux.session = temp
    return True, temp


async def _restore_host(host_name: str, sessions: list[dict], mode: str) -> list[dict]:
    """Restore one host's share of a snapshot; one result per session."""
    def failed(reason: str) -> list[dict]:
        return [{'host': host_name, 'session': s['session'], 'target': s['session'],
                 'status': 'failed', 'reason': reason} for s in sessions]

    host = HOSTS.get(host_name)
    if host is None:
        return failed('host_not_configured')
    code = await DIRECTORY.preflight(host)
    if code:
        return failed(code)
    dirs = await _existing_dirs(host, layout_store.snapshot_cwds(sessions))
    if dirs is None:
        return failed('unreachable')

    live = await _live_session_names(host)
    # Names already spoken for in this batch, kept up to date as targets are
    # picked, so a 'duplicate' rename can't land on another session this same
    # snapshot is about to (re)create.
    reserved = set(live) | {s['session'] for s in sessions}
    results: list[dict] = []
    for session_snap in sessions:
        target = session_snap['session']
        if target in live and mode == 'skip':
            results.append({
                'host':    host_name,
                'session': target,
                'target':  target,
                'status':  'skipped',
            })
            continue
        # Only a real name clash gets renamed — 'duplicate' leaves every
        # non-conflicting session under its original name, it just avoids
        # replacing the ones that collide.
        if target in live and mode in ('duplicate', 'auto'):
            target = layout_store.unique_session_name(target, reserved)
        reserved.add(target)

        ok, final_target = await _restore_one_session(session_snap, target, mode, live, host, dirs)
        results.append({
            'host':    host_name,
            'session': session_snap['session'],
            'target':  final_target,
            'status':  'restored' if ok else 'failed',
        })
    return results


async def _restore_snapshot(websocket, snap: dict, mode: str) -> list[dict]:
    """Restore every session in `snap` independently — one session's (or
    one host's) failure does not stop the others. Returns a per-session
    result list; the caller reports it to the browser as `restore_result`.
    """
    # Same reason as select_window: keep this connection off the broadcast
    # list for the WHOLE batch, not just one session's build, so no other
    # session's %layout-change can overtake the window_switched that ends it.
    was_subscribed = websocket in tmux.subscribers
    if was_subscribed:
        tmux.subscribers.remove(websocket)

    results: list[dict] = []
    attached: list[bool] = []
    try:
        for host_name, sessions in layout_store.group_by_host(snap):
            results += await _restore_host(host_name, sessions, mode)
            attached += [bool(s.get('attached')) for s in sessions]

        # Focus on whichever session was attached at save time, if it came
        # back; otherwise the first one that did. Only on the host the
        # browser is already looking at: moving every browser to another
        # host is the user's call. If nothing came back here, the current
        # view is left alone.
        here = [(a, r) for a, r in zip(attached, results)
                if r['host'] == tmux.host.name and r['status'] == 'restored']
        focus = next((r['target'] for a, r in here if a), None)
        if focus is None:
            focus = next((r['target'] for _, r in here), None)
        if focus is not None:
            if focus != tmux.session:
                await tmux.send_command(f'switch-client -t {_tmux_quote(focus)}')
                tmux.session = focus
            await _send_current_view(websocket)
    finally:
        if was_subscribed:
            tmux.subscribers.append(websocket)
    if any(r['host'] != tmux.host.name for r in results):
        _refresh_hosts_soon(force=True)

    return results


async def _live_by_host(snap: dict) -> dict[str, set[str]]:
    """What's running now on each reachable host the snapshot mentions."""
    async def one(host_name: str):
        host = HOSTS.get(host_name)
        if host is None or await DIRECTORY.preflight(host):
            return host_name, None
        return host_name, await _live_session_names(host)
    pairs = await asyncio.gather(*(one(h) for h, _ in layout_store.group_by_host(snap)))
    return {h: live for h, live in pairs if live is not None}


# ──────────────────────────────────────────── host switching

async def _send_host_error(websocket, host: str, code: str) -> None:
    # Fixed codes only — ssh's and tmux's own text never reaches the browser.
    await websocket.send(json.dumps({'type': 'host_error', 'host': host, 'code': code}))


async def _switch_host(websocket, host_name: str, session: str, *, create: bool = False) -> None:
    """Move the shared control client to `session` on another host.

    Every browser follows: they all watch the one control client. Pane ids
    are per tmux server, so the old host's %3 and the new host's %3 are
    different panes — no output may reach a browser between the switch and
    the `init` that makes it drop every pane it had.
    """
    if _host_lock.locked() or _layout_lock.locked():
        await _send_host_error(websocket, host_name, 'busy')
        return
    async with _host_lock:
        host = HOSTS[host_name]
        # Check before touching the live client, so a failure changes nothing.
        code = await DIRECTORY.preflight(host)
        if code == 'auth_required':
            # Needs a password first. The sign-in waits on answers that come
            # in over this same connection, so it can't hold up its loop.
            _start_sign_in(websocket, host_name, then=lambda: _switch_host(
                websocket, host_name, session, create=create))
            return
        if not code:
            if create:
                rc = await _run_tmux_size_safe('new-session', '-d', '-s', session, host=host)
                code = 'switch_failed' if rc != 0 else ''
            else:
                rc = await _run_tmux('has-session', '-t', f'={session}', host=host)
                code = 'session_gone' if rc != 0 else ''
        if code:
            await _send_host_error(websocket, host_name, code)
            _refresh_hosts_soon(force=True)
            return

        subscribers = tmux.subscribers[:]
        tmux.subscribers.clear()
        try:
            ok = await tmux.switch_target(host, session)
            await _resend_view(subscribers)
            if not ok:
                await _send_host_error(websocket, host_name, 'switch_failed')
        finally:
            _resubscribe(subscribers)
    log.info('control client now on host=%s session=%s', tmux.host.name, tmux.session)
    _refresh_hosts_soon()


async def _resend_view(clients: list) -> None:
    """Queue a fresh `init` to `clients`, ahead of any output they get next."""
    state = await tmux.get_initial_state()
    _update_allowed_targets(state)
    if _last_size is not None:
        await tmux.send_command(f'refresh-client -C {_last_size[0]}x{_last_size[1]}')
    data = _view_message('init', state)
    for client in clients:
        client.enqueue(data)


def _resubscribe(clients: list) -> None:
    for client in clients:
        if not client._closed and client not in tmux.subscribers:
            tmux.subscribers.append(client)


def _on_target_changed(reason: str, previous: str, subscribers: list) -> None:
    """TmuxControl gave up on a remote host and went back to the local
    session; it has kept `subscribers` off the stream for us."""
    async def resume() -> None:
        try:
            await _resend_view(subscribers)
            data = json.dumps({'type': 'host_error', 'host': previous, 'code': reason})
            for client in subscribers:
                client.enqueue(data)
        finally:
            _resubscribe(subscribers)
        _refresh_hosts_soon(force=True)
    asyncio.ensure_future(resume())


# ──────────────────────────────────────────── signing in to a host

async def _ask_browser(client, host_name: str, prompt: str) -> str | None:
    """Show ssh's `prompt` on `client`; its answer, or None (cancelled,
    timed out, disconnected)."""
    if getattr(client, '_closed', False):
        return None
    prompt_id = secrets.token_urlsafe(12)
    answer = asyncio.get_running_loop().create_future()
    _auth_prompts[prompt_id] = (client, answer)
    log.info('sign-in prompt for host %s', host_name)
    try:
        await client.send(json.dumps({
            'type':   'host_auth_prompt',
            'id':     prompt_id,
            'host':   host_name,
            'prompt': askpass.clean_prompt(prompt),
        }))
        return await asyncio.wait_for(answer, AUTH_PROMPT_TIMEOUT)
    except (asyncio.TimeoutError, ConnectionClosed):
        return None
    finally:
        _auth_prompts.pop(prompt_id, None)
        if not answer.done() or answer.cancelled():
            with contextlib.suppress(Exception):
                await client.send(json.dumps({'type': 'host_auth_closed', 'id': prompt_id}))


async def _sign_in(client, host_name: str) -> str:
    """Start `host_name`'s shared connection, asking `client` for whatever
    passphrase or password ssh needs. '' on success, else an error code."""
    cancelled = False

    async def ask(prompt: str) -> str | None:
        nonlocal cancelled
        if cancelled:
            return None   # ssh would ask again after a refusal; don't
        answer = await _ask_browser(client, host_name, prompt)
        if answer is None:
            cancelled = True
        return answer

    code = await tmux_hosts.ensure_master(HOSTS[host_name], ask=ask)
    if code == 'auth_required':
        code = 'auth_cancelled' if cancelled else 'auth_failed'
    await DIRECTORY.refresh(host_name, force=True)
    return code


def _start_sign_in(client, host_name: str, then=None) -> None:
    async def run() -> None:
        code = await _sign_in(client, host_name)
        if code:
            with contextlib.suppress(Exception):
                await _send_host_error(client, host_name, code)
        elif then is not None:
            await then()
    asyncio.ensure_future(run())


async def _run_on_host(host_name: str, *args: str) -> None:
    """A session command on a host the control client isn't on. The browser
    hears about the result through the `hosts` broadcast."""
    rc = await _run_tmux(*args, host=HOSTS[host_name])
    if rc != 0:
        log.warning('%s on host %s failed rc=%d', args[0], host_name, rc)
    await DIRECTORY.refresh(host_name, force=True)


_PANE_MESSAGES = {'input', 'split_window', 'get_snapshot', 'get_history', 'select_pane'}


async def _handle_msg(websocket, msg: dict) -> None:
    global _resize_master, _last_size
    t = msg.get('type')

    if t in _PANE_MESSAGES and _stale_view(msg):
        return   # aimed at a pane of the host we just left

    if t == 'input':
        _resize_master = websocket
        pane = _pane_id(msg.get('pane'))
        data = msg.get('data', '')
        if pane and data:
            # Also dropped if a switch lands while this is being written.
            await tmux.write_input(pane, data.encode('utf-8'), target_gen=msg['gen'])

    elif t == 'client_active':
        _resize_master = websocket

    elif t == 'client_log':
        # Phones have no usable dev tools, so the page reports what it saw on
        # resume here and it lands in server.log next to the HTTP requests.
        log.info('client %s', msg['text'])

    elif t == 'resize':
        if websocket is not _resize_master:
            return   # only the active tab/device may resize
        cols = int(msg.get('cols', 80))
        rows = int(msg.get('rows', 24))
        log.info('resize request cols=%d rows=%d', cols, rows)
        _last_size = (cols, rows)
        await tmux.send_command(f'refresh-client -C {cols}x{rows}')

    elif t == 'new_window':
        if msg.get('host', tmux.host.name) != tmux.host.name:
            return
        await _run_tmux_size_safe('new-window', '-t', tmux.session)
        await _send_current_view(websocket)

    elif t == 'new_session':
        import time
        name = _session_name(msg.get('name')) or f'sess-{int(time.time())}'
        host = msg.get('host', tmux.host.name)
        if host != tmux.host.name:
            await _switch_host(websocket, host, name, create=True)
            return
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
        host = msg.get('host', tmux.host.name)
        if host != tmux.host.name:
            await _switch_host(websocket, host, name)
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
        host = msg.get('host', tmux.host.name)
        if host != tmux.host.name:
            await _run_on_host(host, 'rename-session', '-t', f'={current_name}', next_name)
            return
        await _run_tmux('rename-session', '-t', current_name, next_name)
        if tmux.session == current_name:
            tmux.session = next_name
            await _send_current_view(websocket)
        else:
            await _send_state(websocket)

    elif t == 'rename_window':
        win = _window_index(msg.get('window'))
        name = _window_name(msg.get('name'))
        if win is None or not name or msg.get('host', tmux.host.name) != tmux.host.name:
            return
        await _run_tmux('rename-window', '-t', f'{tmux.session}:{win}', name)
        await _send_current_view(websocket, msg_type='state')

    elif t == 'kill_session':
        name = _session_name(msg.get('session'))
        if not name:
            return
        host = msg.get('host', tmux.host.name)
        if host != tmux.host.name:
            await _run_on_host(host, 'kill-session', '-t', f'={name}')
            return
        await _run_tmux('kill-session', '-t', name)
        await _send_state(websocket)

    elif t == 'kill_window':
        win = _window_index(msg.get('window'))
        if win is None or msg.get('host', tmux.host.name) != tmux.host.name:
            return
        await _run_tmux('kill-window', '-t', f'{tmux.session}:{win}')
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
            def deliver(snap: dict) -> None:
                log.info(
                    'snapshot pane=%s bytes=%d cursor=%d,%d size=%dx%d alt=%d region=%d-%d',
                    pane,
                    len(snap['data']),
                    snap['cursor_x'],
                    snap['cursor_y'],
                    snap['pane_cols'],
                    snap['pane_rows'],
                    snap['alternate_on'],
                    snap['scroll_region_upper'],
                    snap['scroll_region_lower'],
                )
                # Queued with the broadcasts, not sent directly: the client
                # treats every output frame for this pane that arrives before
                # the snapshot as already captured and drops it.
                websocket.enqueue(json.dumps({
                    **snap,
                    'type': 'snapshot',
                    'pane': pane,
                    'data': base64.b64encode(snap['data']).decode('ascii'),
                }))

            await tmux.snapshot_pane(pane, deliver)

    elif t == 'get_history':
        pane = _pane_id(msg.get('pane'))
        if pane:
            lines = int(msg.get('lines', 2000))
            content = await tmux.capture_history(pane, lines)
            log.info('history pane=%s lines=%d bytes=%d', pane, lines, len(content))
            await websocket.send(json.dumps({
                'type': 'history',
                'pane': pane,
                'data': base64.b64encode(content).decode('ascii'),
            }))

    elif t == 'get_state':
        # Used by the client to refresh sidebar lists after pane/window changes.
        await _send_state(websocket)

    elif t == 'get_current_view':
        await _send_current_view(websocket)

    elif t == 'refresh_hosts':
        _refresh_hosts_soon(force=True)

    elif t == 'connect_host':
        _start_sign_in(websocket, msg['host'])

    elif t == 'host_auth_answer':
        # Never logged: this is a password or a key passphrase.
        entry = _auth_prompts.get(msg['id'])
        if entry is not None and entry[0] is websocket and not entry[1].done():
            entry[1].set_result(None if msg.get('cancel') else msg.get('answer', ''))

    elif t == 'select_pane':
        _resize_master = websocket
        pane = _pane_id(msg.get('pane'))
        force_zoom  = _to_bool(msg.get('force_zoom'))
        toggle_zoom = _to_bool(msg.get('toggle_zoom'))
        if pane:
            await tmux.send_command(f'select-pane -t {pane}')
            if force_zoom:
                zoomed = (await tmux.send_command(
                    f'display-message -p -t {pane} "#{{window_zoomed_flag}}"'
                )).strip()
                if zoomed != '1':
                    await tmux.send_command(f'resize-pane -Z -t {pane}')
            elif toggle_zoom:
                await tmux.send_command(f'resize-pane -Z -t {pane}')

    elif t == 'select_window':
        _resize_master = websocket
        win = _window_index(msg.get('window'))
        if win is None or msg.get('host', tmux.host.name) != tmux.host.name:
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

    elif t == 'list_layouts':
        await _send_layouts(websocket)

    elif t == 'save_layout':
        name = _layout_name(msg.get('name'))
        if _layout_lock.locked():
            await _send_layout_error(websocket, name, 'busy')
            return
        async with _layout_lock:
            store = layout_store.load_store()
            if name in store['slots'] and not _to_bool(msg.get('overwrite')):
                await websocket.send(json.dumps(
                    {'type': 'layout_conflict', 'name': name, 'scope': 'slot'}))
                return
            snap, code, skipped = await _capture_all_sessions(name)
            if snap is None:
                await _send_layout_error(websocket, name, code)
                return
            if not layout_store.put_slot(store, snap):
                await _send_layout_error(websocket, name, 'store_full')
                return
            try:
                layout_store.save_store(store)
            except OSError:
                log.exception('could not write the layout store')
                await _send_layout_error(websocket, name, 'save_failed')
                return
        if skipped:
            await websocket.send(json.dumps(
                {'type': 'save_result', 'name': name, 'skipped_hosts': skipped}))
        await _send_layouts(websocket)

    elif t == 'restore_layout':
        _resize_master = websocket
        name = _layout_name(msg.get('name'))
        mode = msg.get('mode') or 'auto'
        if _layout_lock.locked():
            await _send_layout_error(websocket, name, 'busy')
            return
        async with _layout_lock:
            snap = layout_store.load_store()['slots'].get(name)
            if snap is None:
                await _send_layout_error(websocket, name, 'not_found')
                return
            if mode == 'auto':
                conflicts = layout_store.conflicting_sessions(snap, await _live_by_host(snap))
                if conflicts:
                    await websocket.send(json.dumps(
                        {'type': 'layout_conflict', 'name': name,
                         'scope': 'snapshot', 'sessions': conflicts}))
                    return
            results = await _restore_snapshot(websocket, snap, mode)
            await websocket.send(json.dumps(
                {'type': 'restore_result', 'name': name, 'sessions': results}))
        await _send_layouts(websocket)

    elif t == 'delete_layout':
        name = _layout_name(msg.get('name'))
        if _layout_lock.locked():
            await _send_layout_error(websocket, name, 'busy')
            return
        async with _layout_lock:
            store = layout_store.load_store()
            if store['slots'].pop(name, None) is None:
                await _send_layout_error(websocket, name, 'not_found')
                return
            try:
                layout_store.save_store(store)
            except OSError:
                log.exception('could not write the layout store')
                await _send_layout_error(websocket, name, 'save_failed')
                return
        await _send_layouts(websocket)


# ──────────────────────────────────────────── main

def _ws_process_request(connection: ServerConnection, request):
    if len(_active_connections) >= MAX_CONNECTIONS:
        return connection.respond(HTTPStatus.SERVICE_UNAVAILABLE, 'Too many connections\n')
    context, reason = ACCESS.authorize_websocket(
        origin_value=_single_header(request.headers, 'Origin'),
        host=_single_header(request.headers, 'Host') or '',
        tailscale_login=_single_header(request.headers, 'Tailscale-User-Login'),
        cookie_header=_single_header(request.headers, 'Cookie'),
    )
    if context is None:
        log.warning('WS handshake rejected: %s', reason)
        return connection.respond(HTTPStatus.FORBIDDEN, 'Forbidden\n')
    connection.web_tmux_auth = context
    return None


async def main() -> None:
    global tmux, ASKPASS

    # Static file server in a daemon thread
    t = threading.Thread(target=_start_http, daemon=True)
    t.start()

    # tmux control mode
    tmux_hosts.ensure_control_dir(HOSTS.values())
    remote = [h for h in HOSTS.values() if not h.is_local]
    # Connections left by a server that didn't get to close them (killed,
    # crashed): signed-in or not, they belong to no running server now.
    await tmux_hosts.stop_masters(remote)
    if remote:
        ASKPASS = askpass.AskpassBroker(remote[0].control_dir)
        await ASKPASS.start()
        tmux_hosts.configure_askpass(ASKPASS)
    if len(HOSTS) > 1:
        log.info('ssh hosts: %s', ', '.join(name for name in HOSTS if name != LOCAL))
    log.info('layouts: %s', layout_store.store_path())
    tmux = TmuxControl(session=SESSION, host=LOCAL_HOST,
                       fallback=(LOCAL_HOST, SESSION), on_target_changed=_on_target_changed)
    await tmux.start()
    refresher = asyncio.ensure_future(_refresh_hosts_loop())

    # server.sh stop sends SIGTERM. Shut down through the code below rather
    # than dying on the spot, so no ssh connection outlives the server.
    stopping = asyncio.get_running_loop().create_future()
    for sig in (signal.SIGTERM, signal.SIGINT):
        asyncio.get_running_loop().add_signal_handler(
            sig, lambda: stopping.done() or stopping.set_result(None))

    # WebSocket server
    log.info('WS    ws://127.0.0.1:%d/', WS_PORT)
    async with serve(
        ws_handler,
        '127.0.0.1',
        WS_PORT,
        origins=ACCESS.allowed_origin_values,
        process_request=_ws_process_request,
        compression=None,
        max_size=WS_MAX_MESSAGE_BYTES,
        max_queue=4,
        write_limit=32 * 1024,
        server_header=None,
        close_timeout=1,   # don't let a sleeping phone hold up a stop
    ):
        try:
            await stopping   # run until asked to stop
            log.info('stopping')
        finally:
            refresher.cancel()
            await _shutdown_hosts()


async def _shutdown_hosts() -> None:
    with contextlib.suppress(Exception):
        await tmux._cleanup_client()
    await tmux_hosts.stop_masters(HOSTS.values())
    if ASKPASS is not None:
        await ASKPASS.stop()


async def _refresh_hosts_loop() -> None:
    # Keeps the other hosts' session lists current while anyone is looking.
    # Unreachable hosts back off on their own (HostDirectory._due).
    while True:
        await asyncio.sleep(tmux_hosts.REACHABLE_TTL)
        if _clients and len(HOSTS) > 1:
            with contextlib.suppress(Exception):
                await DIRECTORY.refresh_all()


if __name__ == '__main__':
    asyncio.run(main())
