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
    value = str(value or '')
    # tmux session names are targets in command strings here; keep this strict.
    return value if value.replace('-', '').replace('_', '').isalnum() else ''


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
        await _run_tmux_size_safe('new-session', '-d', '-s', name)
        await tmux.send_command(f'switch-client -t {name}')
        tmux.session = name
        await _send_current_view(websocket)

    elif t == 'select_session':
        name = _session_name(msg.get('session'))
        if not name:
            return
        await tmux.send_command(f'switch-client -t {name}')
        tmux.session = name
        await _send_current_view(websocket)

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
        if pane:
            await tmux.send_command(f'select-pane -t {pane}')

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
            await tmux.send_command(f'select-window -t {tmux.session}:{win}')
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
