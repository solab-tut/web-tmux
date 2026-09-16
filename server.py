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
import threading
import time
from functools import partial
from http import HTTPStatus
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

from websockets.asyncio.server import ServerConnection, serve
from websockets.exceptions import ConnectionClosed

from layout_parser import parse_layout
from tmux_control import SNAPSHOT_SCROLLBACK_LINES, TmuxControl
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

MAX_CONNECTIONS = 8
MAX_INPUT_BYTES = 8 * 1024
MAX_OUTBOUND_QUEUE_BYTES = 1024 * 1024
WS_MAX_MESSAGE_BYTES = 64 * 1024
CONTROL_RATE = 30.0
INPUT_RATE_BYTES = 128 * 1024.0
INPUT_BURST_BYTES = 256 * 1024.0

ACCESS = AccessController.from_env()
tmux = None  # TmuxControl
_active_connections: set[ServerConnection] = set()
_allowed_sessions: set[str] = set()
_allowed_windows: set[int] = set()
_allowed_panes: set[str] = set()


def _single_header(headers, name: str) -> str | None:
    values = headers.get_all(name) or []
    return values[0] if len(values) == 1 else None


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
        "worker-src 'none'",
    ))
    return (
        ('Cache-Control', 'no-store, no-cache, must-revalidate, max-age=0'),
        ('Pragma', 'no-cache'),
        ('Expires', '0'),
        ('Content-Security-Policy', csp),
        ('Cross-Origin-Opener-Policy', 'same-origin'),
        ('Cross-Origin-Resource-Policy', 'same-origin'),
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
        for name, value in _security_headers():
            self.send_header(name, value)
        super().end_headers()

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
        context = self._authorize()
        if context is None:
            self._forbidden()
            return
        if urlsplit(self.path).path == '/auth/session':
            self._auth_session(context)
            return
        super().do_GET()

    def do_HEAD(self) -> None:
        context = self._authorize()
        if context is None:
            self._forbidden()
            return
        if urlsplit(self.path).path == '/auth/session':
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
    _update_allowed_targets(state)
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
    handler = partial(SecureStaticHandler, directory=STATIC_DIR)
    server = ThreadingHTTPServer(('127.0.0.1', HTTP_PORT), handler)
    server.daemon_threads = True
    log.info('HTTP  http://127.0.0.1:%d/', HTTP_PORT)
    server.serve_forever()


# ──────────────────────────────────────────── WebSocket

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
    global _allowed_sessions, _allowed_windows, _allowed_panes
    _allowed_sessions = {item['name'] for item in state.get('sessions', [])}
    _allowed_windows = {item['index'] for item in state.get('windows', [])}
    _allowed_panes = {item['id'] for item in state.get('panes', [])}


_MESSAGE_FIELDS = {
    'input': ({'type', 'pane', 'data'}, {'pane', 'data'}),
    'client_active': ({'type'}, set()),
    'resize': ({'type', 'cols', 'rows'}, {'cols', 'rows'}),
    'new_window': ({'type'}, set()),
    'new_session': ({'type', 'name'}, set()),
    'select_session': ({'type', 'session'}, {'session'}),
    'rename_session': ({'type', 'session', 'name'}, {'session', 'name'}),
    'rename_window': ({'type', 'window', 'name'}, {'window', 'name'}),
    'kill_session': ({'type', 'session'}, {'session'}),
    'kill_window': ({'type', 'window'}, {'window'}),
    'split_window': ({'type', 'direction', 'pane'}, {'direction'}),
    'get_snapshot': ({'type', 'pane'}, {'pane'}),
    'get_history': ({'type', 'pane', 'lines'}, {'pane'}),
    'get_state': ({'type'}, set()),
    'get_current_view': ({'type'}, set()),
    'select_pane': ({'type', 'pane', 'force_zoom', 'toggle_zoom'}, {'pane'}),
    'select_window': ({'type', 'window'}, {'window'}),
}


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

    if msg_type == 'input':
        pane, data = _pane_id(msg['pane']), msg['data']
        if not pane or pane not in _allowed_panes or not isinstance(data, str) or not data:
            raise PolicyViolation('invalid input target or data')
        if len(data.encode('utf-8')) > MAX_INPUT_BYTES:
            raise PolicyViolation('input is too large')
    elif msg_type == 'resize':
        cols, rows = msg['cols'], msg['rows']
        if (isinstance(cols, bool) or not isinstance(cols, int) or not 10 <= cols <= 500
                or isinstance(rows, bool) or not isinstance(rows, int) or not 5 <= rows <= 200):
            raise PolicyViolation('invalid terminal size')
    elif msg_type == 'new_session':
        name = msg.get('name')
        if name not in ('', None) and (not isinstance(name, str) or not _session_name(name)):
            raise PolicyViolation('invalid session name')
    elif msg_type in {'select_session', 'kill_session'}:
        if not isinstance(msg['session'], str) or _session_name(msg['session']) not in _allowed_sessions:
            raise PolicyViolation('unknown session')
    elif msg_type == 'rename_session':
        if (not isinstance(msg['session'], str) or _session_name(msg['session']) not in _allowed_sessions
                or not isinstance(msg['name'], str) or not _session_name(msg['name'])):
            raise PolicyViolation('invalid session rename')
    elif msg_type in {'kill_window', 'select_window'}:
        if _window_index(msg['window']) not in _allowed_windows:
            raise PolicyViolation('unknown window')
    elif msg_type == 'rename_window':
        if (_window_index(msg['window']) not in _allowed_windows
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
    return msg


_resize_master = None   # only the active browser tab/device may send resize


async def ws_handler(websocket: ServerConnection) -> None:
    global _resize_master
    if len(_active_connections) >= MAX_CONNECTIONS:
        await websocket.close(code=1013, reason='too many connections')
        return

    client = ClientConnection(websocket)
    _active_connections.add(websocket)
    log.info('ws connect %s', websocket.remote_address)
    if _resize_master is None:
        _resize_master = client
    tmux.subscribers.append(client)
    try:
        state = await tmux.get_initial_state()
        _update_allowed_targets(state)
        layout_panes = parse_layout(state.get('layout', ''))
        await client.send(json.dumps({
            'type': 'init',
            'session': state['session'],
            'sessions': state['sessions'],
            'windows': state['windows'],
            'panes': state['panes'],
            'active_pane': state['active_pane'],
            'layout': state.get('layout', ''),
            'layout_panes': layout_panes,
        }))

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
        if client in tmux.subscribers:
            tmux.subscribers.remove(client)
        if client is _resize_master:
            _resize_master = tmux.subscribers[-1] if tmux.subscribers else None
        _active_connections.discard(websocket)
        await client.stop()
        log.info('ws disconnect %s', websocket.remote_address)


async def _handle_msg(websocket, msg: dict) -> None:
    global _resize_master
    t = msg.get('type')

    if t == 'input':
        _resize_master = websocket
        pane = _pane_id(msg.get('pane'))
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
            _update_allowed_targets(state)
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

    elif t == 'kill_session':
        name = _session_name(msg.get('session'))
        if not name:
            return
        await _run_tmux('kill-session', '-t', name)
        state = await tmux.get_initial_state()
        _update_allowed_targets(state)
        await websocket.send(json.dumps({
            'type':        'state',
            'session':     state['session'],
            'sessions':    state['sessions'],
            'windows':     state['windows'],
            'panes':       state['panes'],
            'active_pane': state['active_pane'],
        }))

    elif t == 'kill_window':
        win = _window_index(msg.get('window'))
        if win is None:
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
        state = await tmux.get_initial_state()
        _update_allowed_targets(state)
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
    global tmux

    # Static file server in a daemon thread
    t = threading.Thread(target=_start_http, daemon=True)
    t.start()

    # tmux control mode
    tmux = TmuxControl(session=SESSION)
    await tmux.start()

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
    ):
        await asyncio.Future()  # run forever


if __name__ == '__main__':
    asyncio.run(main())
