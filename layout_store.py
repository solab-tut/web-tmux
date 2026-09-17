#!/usr/bin/env python3
"""
Named snapshots of your tmux workspace: for every session that was open when
you saved, its window names, split layouts, and each pane's working
directory. Running commands and screen contents are not captured — restoring
gives you the same sessions and frames back, with fresh shells in them.

A snapshot covers *all* sessions that were open at save time, each restored
under its own original name. This is meant for "bring my sessions back after
a reboot", not for stashing one particular layout.

Everything here is pure except load_store/save_store, so the interesting parts
(parsing tmux's -F output, deciding pane order, planning the restore) can be
tested without tmux. server.py owns the subprocess calls.
"""
import json
import os
import re
from collections import namedtuple

from layout_parser import parse_layout

SCHEMA_VERSION = 2

MAX_SLOTS                 = 20
MAX_SLOT_NAME             = 64
MAX_SESSIONS_PER_SNAPSHOT = 16
MAX_WINDOWS               = 32     # per session
MAX_PANES_PER_WINDOW      = 16
MAX_TOTAL_PANES           = 128    # per session
MAX_PANES_PER_SNAPSHOT    = 256    # summed across every session in the snapshot
MAX_CWD                   = 4096
MAX_LAYOUT                = 8192
MAX_NAME                  = 128

MIN_COLS, MAX_COLS = 20, 500
MIN_ROWS, MAX_ROWS = 5, 200

# A tmux layout string: 4 hex checksum digits, then only geometry punctuation.
# Nothing here ever reaches a shell (we exec argv lists), but a store that has
# been hand-edited shouldn't be able to feed tmux arbitrary option-looking text.
LAYOUT_RE = re.compile(r'^[0-9a-f]{4},[0-9,x{}\[\]]+$')

STORE_FILENAME = 'layouts.json'

# Placeholder for "the pane id captured by an earlier step", resolved at run
# time by server.py. Lets build_restore_steps() stay pure and comparable.
Ref = namedtuple('Ref', 'key')


# ──────────────────────────────────────────────────────────── store location / IO

def store_dir() -> str:
    override = os.environ.get('WEB_TMUX_STATE_DIR')
    if override:
        return override
    base = os.environ.get('XDG_DATA_HOME') or os.path.join(os.path.expanduser('~'), '.local', 'share')
    return os.path.join(base, 'web-tmux')


def store_path() -> str:
    return os.path.join(store_dir(), STORE_FILENAME)


def empty_store() -> dict:
    return {'version': SCHEMA_VERSION, 'slots': {}}


def load_store() -> dict:
    """Never raises. A missing, corrupt, or foreign-version store reads as empty.

    A version-1 (single-session) store is foreign here — it is not migrated,
    just read back as empty. This is a pre-release format change, not an
    upgrade path users depend on.
    """
    try:
        with open(store_path(), encoding='utf-8') as fh:
            raw = json.load(fh)
    except (OSError, ValueError):
        return empty_store()
    if not isinstance(raw, dict) or raw.get('version') != SCHEMA_VERSION:
        return empty_store()
    slots = raw.get('slots')
    if not isinstance(slots, dict):
        return empty_store()
    kept = {}
    for name, snap in slots.items():
        if not isinstance(name, str) or not _clean_slot_name(name):
            continue
        if validate_snapshot(snap) and snap.get('name') == name:
            kept[name] = snap
    return {'version': SCHEMA_VERSION, 'slots': kept}


def save_store(store: dict) -> None:
    """Atomic replace, 0600 file inside a 0700 directory (cwd paths are private)."""
    directory = store_dir()
    os.makedirs(directory, mode=0o700, exist_ok=True)
    try:
        os.chmod(directory, 0o700)
    except OSError:
        pass
    path = os.path.join(directory, STORE_FILENAME)
    tmp = path + '.tmp'
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as fh:
            json.dump(store, fh, ensure_ascii=False, separators=(',', ':'))
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def put_slot(store: dict, snap: dict) -> bool:
    """Add or replace a slot. False when adding a new one would exceed MAX_SLOTS."""
    name = snap['name']
    if name not in store['slots'] and len(store['slots']) >= MAX_SLOTS:
        return False
    store['slots'][name] = snap
    return True


def slot_summaries(store: dict) -> list[dict]:
    """The shape the browser lists, newest first."""
    out = []
    for snap in store['slots'].values():
        out.append({
            'name':     snap['name'],
            'sessions': len(snap['sessions']),
            'windows':  sum(len(s['windows']) for s in snap['sessions']),
            'panes':    sum(len(w['panes']) for s in snap['sessions'] for w in s['windows']),
            'saved_at': snap.get('saved_at', ''),
        })
    out.sort(key=lambda s: s['saved_at'], reverse=True)
    return out


def conflicting_sessions(snap: dict, live) -> list[str]:
    """Names in `snap` that are already running, in the snapshot's own order."""
    return [s['session'] for s in snap['sessions'] if s['session'] in live]


# ────────────────────────────────────────────────────────────────────── capture

WIN_FMT  = '#{window_index}\t#{window_layout}\t#{window_active}\t#{window_width}\t#{window_height}\t#{window_name}'
PANE_FMT = '#{window_index}\t#{pane_id}\t#{pane_index}\t#{pane_active}\t#{pane_current_path}'


def _clean_slot_name(value) -> str:
    if not isinstance(value, str):
        return ''
    value = value.strip()
    if not value or len(value) > MAX_SLOT_NAME:
        return ''
    if any(ord(ch) < 0x20 or ord(ch) == 0x7f for ch in value):
        return ''
    return value


def _clean_session_name(value) -> str:
    # Mirrors server._session_name: tmux targets are "session:window.pane".
    if not isinstance(value, str):
        return ''
    value = value.strip()
    if not value or len(value) > MAX_NAME or any(ch in value for ch in '\r\n\0:'):
        return ''
    return value


def _usable_cwd(cwd: str, isdir, home: str) -> str:
    """Fall back to $HOME for a path that is gone, too long, or not JSON-safe."""
    if not cwd or len(cwd) > MAX_CWD:
        return home
    try:
        cwd.encode('utf-8')          # surrogateescape leftovers can't be stored
    except UnicodeEncodeError:
        return home
    try:
        if not isdir(cwd):
            return home
    except (OSError, ValueError):
        return home
    return cwd


def _parse_window_lines(raw: str) -> list[dict]:
    out = []
    for line in raw.splitlines():
        if not line:
            continue
        parts = line.split('\t', 5)          # name last, may contain anything
        if len(parts) != 6:
            continue
        index, layout, active, width, height, name = parts
        if not (index.isdigit() and width.isdigit() and height.isdigit()):
            continue
        out.append({
            'index':  int(index),
            'layout': layout,
            'active': active == '1',
            'width':  int(width),
            'height': int(height),
            'name':   name,
        })
    return out


def _parse_pane_lines(raw: str) -> list[dict]:
    out = []
    for line in raw.splitlines():
        if not line:
            continue
        parts = line.split('\t', 4)          # cwd last, may contain anything
        if len(parts) != 5:
            continue
        window, pane_id, pane_index, active, cwd = parts
        if not (window.isdigit() and pane_index.isdigit()):
            continue
        if not (pane_id.startswith('%') and pane_id[1:].isdigit()):
            continue
        out.append({
            'window':     int(window),
            'num':        int(pane_id[1:]),
            'pane_index': int(pane_index),
            'active':     active == '1',
            'cwd':        cwd,
        })
    return out


def order_panes(layout_str: str, panes_by_num: dict) -> tuple[list[dict], str]:
    """Order panes the way select-layout will consume them.

    tmux assigns panes to layout cells by walking the window's pane list in
    order; the pane numbers embedded in the layout string are discarded when it
    is parsed. So a snapshot is only faithful if we record cwds in *layout tree*
    order — pane_index order puts them in the wrong cells after a swap-pane.
    """
    leaves = parse_layout(layout_str)
    nums = [leaf['id'] for leaf in leaves]
    if len(nums) == len(panes_by_num) and all(n in panes_by_num for n in nums):
        return [panes_by_num[n] for n in nums], 'layout'
    return [panes_by_num[n] for n in sorted(panes_by_num)], 'index'


def parse_session_snapshot(session: str, win_raw: str, pane_raw: str, *,
                           attached: bool, isdir=os.path.isdir,
                           home: str | None = None) -> dict | None:
    """Build one session's shape from its `list-windows`/`list-panes` output.

    Returns None when the session is unreadable or too big to store. The
    result has no `name`/`saved_at` — those belong to the snapshot that wraps
    it (see build_snapshot).
    """
    session = _clean_session_name(session)
    if not session:
        return None
    if home is None:
        home = os.path.expanduser('~')

    win_rows = _parse_window_lines(win_raw)
    if not win_rows or len(win_rows) > MAX_WINDOWS:
        return None

    panes_by_window: dict[int, list[dict]] = {}
    for row in _parse_pane_lines(pane_raw):
        panes_by_window.setdefault(row['window'], []).append(row)

    windows: list[dict] = []
    total = 0
    pane_order = 'layout'
    active_window = win_rows[0]['index']
    width, height = win_rows[0]['width'], win_rows[0]['height']

    for win in win_rows:
        rows = panes_by_window.get(win['index'], [])
        if not rows or len(rows) > MAX_PANES_PER_WINDOW:
            return None
        total += len(rows)
        if total > MAX_TOTAL_PANES:
            return None
        if len(win['layout']) > MAX_LAYOUT or not LAYOUT_RE.match(win['layout']):
            return None

        rows.sort(key=lambda r: r['pane_index'])
        ordered, mode = order_panes(win['layout'], {r['num']: r for r in rows})
        if mode != 'layout':
            pane_order = 'index'

        windows.append({
            'index':  win['index'],
            'name':   win['name'],
            'active': win['active'],
            'layout': win['layout'],
            'panes':  [{'cwd': _usable_cwd(r['cwd'], isdir, home), 'active': r['active']}
                       for r in ordered],
        })
        if win['active']:
            active_window = win['index']
            width, height = win['width'], win['height']

    return {
        'session':       session,
        'attached':      bool(attached),
        'width':         _clamp(width, MIN_COLS, MAX_COLS),
        'height':        _clamp(height, MIN_ROWS, MAX_ROWS),
        'pane_order':    pane_order,
        'active_window': active_window,
        'windows':       windows,
    }


def build_snapshot(name: str, sessions: list[dict], *, now: str) -> dict | None:
    """Wrap already-captured per-session shapes into one named snapshot.

    None when the name is invalid, there are no sessions (or too many), two
    sessions share a name, or the combined pane count is too big to restore
    in reasonable time.
    """
    name = _clean_slot_name(name)
    if not name:
        return None
    if not sessions or len(sessions) > MAX_SESSIONS_PER_SNAPSHOT:
        return None
    names = [s['session'] for s in sessions]
    if len(names) != len(set(names)):
        return None
    total_panes = sum(len(w['panes']) for s in sessions for w in s['windows'])
    if total_panes > MAX_PANES_PER_SNAPSHOT:
        return None
    return {'name': name, 'saved_at': now, 'sessions': sessions}


def _clamp(value: int, low: int, high: int) -> int:
    return max(low, min(high, value))


# ─────────────────────────────────────────────────────────────────── validation

def validate_snapshot(snap) -> bool:
    """Gate for anything read back off disk, before it becomes tmux arguments."""
    if not isinstance(snap, dict):
        return False
    if not _clean_slot_name(snap.get('name')):
        return False
    sessions = snap.get('sessions')
    if not isinstance(sessions, list) or not sessions or len(sessions) > MAX_SESSIONS_PER_SNAPSHOT:
        return False

    seen_names = set()
    total_panes = 0
    for session_snap in sessions:
        if not _validate_session(session_snap):
            return False
        session_name = session_snap['session']
        if session_name in seen_names:
            return False
        seen_names.add(session_name)
        total_panes += sum(len(w['panes']) for w in session_snap['windows'])

    if total_panes > MAX_PANES_PER_SNAPSHOT:
        return False
    return True


def _validate_session(snap) -> bool:
    """One entry of a snapshot's `sessions` list."""
    if not isinstance(snap, dict):
        return False
    if not _clean_session_name(snap.get('session')):
        return False
    if not isinstance(snap.get('attached'), bool):
        return False
    windows = snap.get('windows')
    if not isinstance(windows, list) or not windows or len(windows) > MAX_WINDOWS:
        return False
    for key in ('width', 'height', 'active_window'):
        value = snap.get(key)
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            return False

    total = 0
    for win in windows:
        if not isinstance(win, dict):
            return False
        index = win.get('index')
        if isinstance(index, bool) or not isinstance(index, int) or index < 0:
            return False
        layout = win.get('layout')
        if not isinstance(layout, str) or len(layout) > MAX_LAYOUT or not LAYOUT_RE.match(layout):
            return False
        wname = win.get('name')
        if not isinstance(wname, str) or len(wname) > MAX_NAME:
            return False
        if any(ch in wname for ch in '\r\n\0'):
            return False
        panes = win.get('panes')
        if not isinstance(panes, list) or not panes or len(panes) > MAX_PANES_PER_WINDOW:
            return False
        total += len(panes)
        if total > MAX_TOTAL_PANES:
            return False
        for pane in panes:
            if not isinstance(pane, dict):
                return False
            cwd = pane.get('cwd')
            if not isinstance(cwd, str) or not cwd or len(cwd) > MAX_CWD:
                return False
            if not cwd.startswith('/') or any(ch in cwd for ch in '\r\n\0'):
                return False
    return True


# ────────────────────────────────────────────────────────────────────── restore
#
# Everything below plans the rebuild of ONE session (an entry from a
# snapshot's `sessions` list). server.py calls these once per session and
# handles cross-session bookkeeping (name reservations, partial failure,
# which session gets focus) itself — see _restore_snapshot there.

def unique_session_name(base: str, taken) -> str:
    """base, else base-2, base-3, … — the name used when restoring alongside."""
    base = base[:MAX_NAME - 6] or 'session'
    if base not in taken:
        return base
    for n in range(2, 1000):
        candidate = f'{base}-{n}'
        if candidate not in taken:
            return candidate
    raise ValueError('no free session name')


def restore_size(session_snap: dict) -> tuple[int, int]:
    """Big enough that every split fits; splitting a too-small window fails."""
    widest = max(len(w['panes']) for w in session_snap['windows'])
    floor = 2 * widest + 2
    width = _clamp(max(session_snap.get('width', 0), floor), MIN_COLS, MAX_COLS)
    height = _clamp(max(session_snap.get('height', 0), floor), MIN_ROWS, MAX_ROWS)
    return width, height


def build_restore_steps(session_snap: dict, temp: str) -> list[dict]:
    """Plan the tmux commands that rebuild one session's shape as `temp`.

    Each step is {'op', 'argv', 'capture'?}. A Ref in argv stands for a pane id
    captured by an earlier step. Panes are always split off the previously
    created pane, which keeps the window's pane list in the same order as the
    cwds we stored (see order_panes), so select-layout lands them correctly.
    """
    width, height = restore_size(session_snap)
    steps: list[dict] = []
    first = session_snap['windows'][0]

    for wi, win in enumerate(session_snap['windows']):
        panes = win['panes']
        target = f'{temp}:{win["index"]}'
        head = f'w{wi}.p0'

        if wi == 0:
            steps.append({
                'op': 'new_session',
                'argv': ['new-session', '-d', '-P', '-F', '#{window_index}\t#{pane_id}',
                         '-s', temp, '-n', win['name'], '-c', panes[0]['cwd'],
                         '-x', str(width), '-y', str(height)],
                'capture': head,
            })
            # new-session lands on base-index, which may not be the saved index.
            steps.append({'op': 'move_first_window', 'session': temp, 'window': first['index']})
        else:
            steps.append({
                'op': 'new_window',
                'argv': ['new-window', '-d', '-P', '-F', '#{pane_id}',
                         '-t', target, '-n', win['name'], '-c', panes[0]['cwd']],
                'capture': head,
            })

        for pi in range(1, len(panes)):
            steps.append({
                'op': 'split',
                'argv': ['split-window', '-d', '-P', '-F', '#{pane_id}',
                         '-t', Ref(f'w{wi}.p{pi - 1}'), '-c', panes[pi]['cwd']],
                'capture': f'w{wi}.p{pi}',
            })

        steps.append({'op': 'layout', 'argv': ['select-layout', '-t', target, win['layout']]})

    active_index = session_snap.get('active_window', first['index'])
    steps.append({'op': 'select_window',
                  'argv': ['select-window', '-t', f'{temp}:{active_index}']})

    active_ref = _active_pane_ref(session_snap, active_index)
    if active_ref:
        steps.append({'op': 'select_pane', 'argv': ['select-pane', '-t', active_ref]})
    return steps


def _active_pane_ref(session_snap: dict, active_index: int):
    for wi, win in enumerate(session_snap['windows']):
        if win['index'] != active_index:
            continue
        for pi, pane in enumerate(win['panes']):
            if pane.get('active'):
                return Ref(f'w{wi}.p{pi}')
    return None
