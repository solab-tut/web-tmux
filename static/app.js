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

const XTERM_THEMES = {
  dark: {
    background: '#1e1e1e', foreground: '#d4d4d4', cursor: '#aeafad',
    black:   '#1e1e1e', brightBlack:   '#808080',
    red:     '#f44747', brightRed:     '#f44747',
    green:   '#608b4e', brightGreen:   '#608b4e',
    yellow:  '#dcdcaa', brightYellow:  '#dcdcaa',
    blue:    '#569cd6', brightBlue:    '#569cd6',
    magenta: '#c586c0', brightMagenta: '#c586c0',
    cyan:    '#4ec9b0', brightCyan:    '#4ec9b0',
    white:   '#d4d4d4', brightWhite:   '#d4d4d4',
  },
  light: {
    background: '#ffffff', foreground: '#333333', cursor: '#333333',
    black:   '#000000', brightBlack:   '#767676',
    red:     '#cd3131', brightRed:     '#cd3131',
    green:   '#00bc00', brightGreen:   '#14ce14',
    yellow:  '#949800', brightYellow:  '#b5ba00',
    blue:    '#0451a5', brightBlue:    '#0451a5',
    magenta: '#bc05bc', brightMagenta: '#bc05bc',
    cyan:    '#0598bc', brightCyan:    '#0598bc',
    white:   '#555555', brightWhite:   '#a5a5a5',
  },
  nord: {
    background: '#2e3440', foreground: '#d8dee9', cursor: '#d8dee9',
    black:   '#3b4252', brightBlack:   '#4c566a',
    red:     '#bf616a', brightRed:     '#bf616a',
    green:   '#a3be8c', brightGreen:   '#a3be8c',
    yellow:  '#ebcb8b', brightYellow:  '#ebcb8b',
    blue:    '#81a1c1', brightBlue:    '#81a1c1',
    magenta: '#b48ead', brightMagenta: '#b48ead',
    cyan:    '#88c0d0', brightCyan:    '#8fbcbb',
    white:   '#e5e9f0', brightWhite:   '#eceff4',
  },
};

const THEME_STORAGE_KEY = 'web-tmux-theme';
const VALID_THEMES = ['dark', 'light', 'nord'];

const FONT_SIZE_STORAGE_KEY = 'web-tmux-font-size';
const VALID_FONT_SIZES = [11, 12, 13, 14, 16, 18];
const DEFAULT_FONT_SIZE = 13;

function currentThemeName() {
  return document.documentElement.dataset.theme || 'dark';
}

function initTheme() {
  const saved = localStorage.getItem(THEME_STORAGE_KEY) || 'dark';
  applyTheme(VALID_THEMES.includes(saved) ? saved : 'dark', false);
}

function applyTheme(name, save = true) {
  document.documentElement.dataset.theme = name;
  if (save) localStorage.setItem(THEME_STORAGE_KEY, name);
  const xt = XTERM_THEMES[name] || XTERM_THEMES.dark;
  Object.values(panes).forEach(p => { p.term.options.theme = xt; });
  document.querySelectorAll('#theme-menu [data-theme-name]').forEach(btn => {
    btn.classList.toggle('active', btn.dataset.themeName === name);
  });
}

function currentFontSize() {
  const saved = parseInt(localStorage.getItem(FONT_SIZE_STORAGE_KEY), 10);
  return VALID_FONT_SIZES.includes(saved) ? saved : DEFAULT_FONT_SIZE;
}

function initFontSize() {
  applyFontSize(currentFontSize(), false);
}

function applyFontSize(size, save = true) {
  if (!VALID_FONT_SIZES.includes(size)) return;
  if (save) localStorage.setItem(FONT_SIZE_STORAGE_KEY, String(size));
  Object.values(panes).forEach(p => { p.term.options.fontSize = size; });
  document.querySelectorAll('#font-size-menu [data-font-size]').forEach(btn => {
    btn.classList.toggle('active', parseInt(btn.dataset.fontSize, 10) === size);
  });
  if (Object.keys(panes).length > 0) {
    resetResizeCache();
    refitCurrentLayout();
  }
}

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
let _confirmDeleteSession = '';
let _confirmDeleteWindow = null;
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
  if (msg.pane && panes[msg.pane]) {
    markSnapshotPending([msg.pane]);
    scheduleSnapshotRefresh([msg.pane]);
  }
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
  if (msg.pane && panes[msg.pane]) {
    markSnapshotPending([msg.pane]);
    scheduleSnapshotRefresh([msg.pane]);
  }
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

  // Create pane elements before positioning so fit() can measure them.
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

  // Mark the active pane before positioning so focus and outlines are current.
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
  // fitAddon computes the exact cols/rows that fill the pixel area.
  requestAnimationFrame(() => {
    // Fit all panes; capture term.cols/rows from the largest pane as reference.
    const refLp = layoutPanes.reduce((best, lp) =>
      lp.cols * lp.rows > best.cols * best.rows ? lp : best, layoutPanes[0]);

    let refTermCols = 0, refTermRows = 0;
    layoutPanes.forEach(({ id }) => {
      const pid = '%' + id;
      const p = panes[pid];
      if (!p) return;
      try { p.fitAddon.fit(); } catch (_) {}
      if (pid === '%' + refLp.id && p.term.cols > 0) {
        refTermCols = p.term.cols;
        refTermRows = p.term.rows;
      }
    });

    // Compute desired total cols/rows via the ratio of fitted vs layout cols.
    // Using ratios avoids the sub-pixel rounding error from clientWidth/term.cols,
    // which caused positionPanes→resize→layout-change oscillation after resize or
    // font-size changes. When term.cols == refLp.cols the ratio is 1 and no
    // resize is sent, breaking the feedback loop immediately.
    if (refTermCols > 0 && refLp.cols > 0 && refTermRows > 0 && refLp.rows > 0) {
      const sendCols = Math.round(totalCols * refTermCols / refLp.cols);
      const sendRows = Math.round(totalRows * refTermRows / refLp.rows);
      if (sendCols !== totalCols || sendRows !== totalRows) {
        maybeSendResize(sendCols, sendRows);
      }
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
    fontSize:    currentFontSize(),
    scrollback:  10000,
    cursorBlink: true,
    scrollOnUserInput: true,
    smoothScrollDuration: 80,
    theme:       XTERM_THEMES[currentThemeName()] || XTERM_THEMES.dark,
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
    if (options.forceZoom)  payload.force_zoom  = true;
    if (options.toggleZoom) payload.toggle_zoom = true;
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
  // With a single visible pane (including tmux zoom), selecting it can change
  // the usable viewport. In split layouts the active pane is only a fraction of
  // the tmux window, so do not send its pane cols/rows as the total size.
  if (isMobileWidth() && !_layoutApplying && _currentLayoutPanes.length <= 1) {
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
    } else if (_confirmDeleteWindow === w.index) {
      div.classList.add('confirming');
      div.appendChild(buildInlineConfirm({
        label: `Close window ${w.index}?`,
        onConfirm: () => {
          _confirmDeleteWindow = null;
          killWindow(w.index);
        },
        onCancel: () => {
          _confirmDeleteWindow = null;
          renderWindowList(windows);
        },
      }));
    } else {
      div.tabIndex = 0;
      div.innerHTML =
        `<span class="window-idx">${w.index}</span>` +
        `<span class="window-name">${escHtml(w.name)}</span>`;
      if (windows.length > 1) {
        div.appendChild(buildDeleteButton(`Close window ${w.index}`, () => {
          _editingWindowIndex = null;
          _confirmDeleteWindow = w.index;
          renderWindowList(windows);
        }));
      }
      div.appendChild(buildRenameButton(`Rename window ${w.index}`, () => {
        _confirmDeleteWindow = null;
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
    } else if (_confirmDeleteSession === s.name) {
      div.classList.add('confirming');
      div.appendChild(buildInlineConfirm({
        label: `Kill session "${s.name}"?`,
        onConfirm: () => {
          _confirmDeleteSession = '';
          killSession(s.name);
        },
        onCancel: () => {
          _confirmDeleteSession = '';
          renderSessionList(sessions);
        },
      }));
    } else {
      div.tabIndex = 0;
      div.innerHTML = `<span class="session-name">${escHtml(`(${s.windows}) ${s.name}`)}</span>`;
      div.title = s.attached ? 'attached' : 'detached';
      div.appendChild(buildDeleteButton(`Kill session ${s.name}`, () => {
        _editingSessionName = '';
        _confirmDeleteSession = s.name;
        renderSessionList(sessions);
      }));
      div.appendChild(buildRenameButton(`Rename session ${s.name}`, () => {
        _confirmDeleteSession = '';
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
      const isCurrentPane = p.id === activePaneId;
      selectPane(p.id, isCurrentPane ? { toggleZoom: true } : { forceZoom: true });
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
  button.className = 'item-icon-btn';
  button.setAttribute('aria-label', label);
  button.title = label;
  button.innerHTML =
    `<svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.4"` +
    ` stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">` +
    `<path d="M11 2.5l2.5 2.5L5.5 13H3v-2.5L11 2.5z"/>` +
    `<path d="M9 4.5l2.5 2.5"/>` +
    `</svg>`;
  button.addEventListener('click', (ev) => {
    ev.preventDefault();
    ev.stopPropagation();
    onClick();
  });
  return button;
}

function buildDeleteButton(label, onClick) {
  const button = document.createElement('button');
  button.type = 'button';
  button.className = 'item-icon-btn item-delete-btn';
  button.setAttribute('aria-label', label);
  button.title = label;
  button.innerHTML =
    `<svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.4"` +
    ` stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">` +
    `<path d="M2.5 4.5h11"/>` +
    `<path d="M5.5 4.5V3.5a1 1 0 011-1h3a1 1 0 011 1v1"/>` +
    `<path d="M12.5 4.5l-.8 8a1 1 0 01-1 .9H5.3a1 1 0 01-1-.9l-.8-8"/>` +
    `<path d="M6.5 7.5v3M9.5 7.5v3"/>` +
    `</svg>`;
  button.addEventListener('click', (ev) => {
    ev.preventDefault();
    ev.stopPropagation();
    onClick();
  });
  return button;
}

function killSession(name) {
  ws.send(JSON.stringify({ type: 'kill_session', session: name }));
}

function killWindow(idx) {
  ws.send(JSON.stringify({ type: 'kill_window', window: idx }));
}

function buildInlineConfirm({ label, onConfirm, onCancel }) {
  const div = document.createElement('div');
  div.className = 'item-inline-editor';

  const text = document.createElement('span');
  text.className = 'item-confirm-label';
  text.textContent = label;

  const yes = document.createElement('button');
  yes.type = 'button';
  yes.className = 'item-inline-btn danger';
  yes.textContent = 'delete';

  const cancel = document.createElement('button');
  cancel.type = 'button';
  cancel.className = 'item-inline-btn secondary';
  cancel.textContent = 'cancel';

  const stop = (ev) => { ev.preventDefault(); ev.stopPropagation(); };
  [div, yes, cancel].forEach((el) => {
    el.addEventListener('mousedown', stop);
    el.addEventListener('click', (ev) => ev.stopPropagation());
  });

  yes.addEventListener('click', onConfirm);
  cancel.addEventListener('click', onCancel);

  div.appendChild(text);
  div.appendChild(yes);
  div.appendChild(cancel);
  return div;
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
    const paneId = item.dataset.paneId || '';
    const isCurrentPane = paneId === activePaneId;
    selectPane(paneId, isCurrentPane ? { toggleZoom: true } : { forceZoom: true });
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

function _onPaneAreaResize() {
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
}

// ResizeObserver catches ALL size changes to the pane area (window resize,
// sidebar open/close, etc.) — not just window-level resize events.
(function setupPaneAreaResize() {
  const area = document.getElementById('pane-area');
  if (window.ResizeObserver) {
    new ResizeObserver(_onPaneAreaResize).observe(area);
  } else {
    window.addEventListener('resize', _onPaneAreaResize);
  }
})();

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

  // \x1b[?1049l — exit alternate screen (vim/htop etc.) and restore normal screen+scrollback
  // \x1b[!p    — soft reset (clears modes/colors without clearing scrollback)
  const parts = [asciiBytes('\x1b[?25l\x1b[?1049l\x1b[!p\x1b[H\x1b[2J'), snapshot];
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

// ─── Theme ────────────────────────────────────────────────────────────────────

document.getElementById('theme-toggle').addEventListener('click', (e) => {
  const menu = document.getElementById('theme-menu');
  menu.hidden = !menu.hidden;
  e.stopPropagation();
});

document.querySelectorAll('#theme-menu [data-theme-name]').forEach(btn => {
  btn.addEventListener('click', () => {
    applyTheme(btn.dataset.themeName);
    document.getElementById('theme-menu').hidden = true;
  });
});

document.addEventListener('click', () => {
  const menu = document.getElementById('theme-menu');
  if (menu) menu.hidden = true;
  const fsMenu = document.getElementById('font-size-menu');
  if (fsMenu) fsMenu.hidden = true;
});

document.getElementById('font-size-toggle').addEventListener('click', (e) => {
  const menu = document.getElementById('font-size-menu');
  menu.hidden = !menu.hidden;
  e.stopPropagation();
});

document.querySelectorAll('#font-size-menu [data-font-size]').forEach(btn => {
  btn.addEventListener('click', () => {
    applyFontSize(parseInt(btn.dataset.fontSize, 10));
    document.getElementById('font-size-menu').hidden = true;
  });
});

// ─── Boot ─────────────────────────────────────────────────────────────────────

initTheme();
initFontSize();
connect();
