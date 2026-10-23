'use strict';

// WebSocket URL: when served over HTTPS (e.g. Tailscale serve), use wss://.
const WS_URL = `${location.protocol === 'https:' ? 'wss' : 'ws'}://${location.hostname}:8765`;
const FONT_FAMILY = '"SF Mono", Menlo, "Cascadia Code", "Fira Code", monospace';
const MOBILE_BP   = 768;
const CLIENT_PREFIX_KEY = '\x01'; // Ctrl+A, matching this app's tmux setup.
const NON_ASCII_DUPLICATE_SUPPRESS_MS = 120;
const ALT_SCREEN_EXIT_PARAMS = new Set(['47', '1047', '1049']);
const OUTPUT_SCAN_TAIL_BYTES = 32;
const MOUSE_DISABLE_SEQS = '\x1b[?1000l\x1b[?1002l\x1b[?1003l\x1b[?1006l\x1b[?1015l';
const DESKTOP_SCROLLBACK = 50000;
const MOBILE_SCROLLBACK = 10000;
// Lines of tmux history pulled once per pane at attach to seed xterm's
// scrollback. After that the buffer grows from live output alone — snapshots
// no longer carry history (see buildSnapshotFrame).
const INITIAL_HISTORY_LINES = 10000;
const MAX_INPUT_CHUNK_BYTES = 8 * 1024;
const MOBILE_VIEWPORT_REFIT_DELAY_MS = 180;
const MOBILE_TOUCH_SCROLL_IDLE_MS = 240;
// Snapshot requests are coalesced, but never postponed past MAX_DELAY: every
// call to scheduleSnapshotRefresh restarts the debounce, and the callers that
// fire in bursts (layout changes, alt-screen exits, mobile viewport refits)
// could otherwise keep pushing the request out while output sits buffered.
const SNAPSHOT_DEBOUNCE_MS = 260;
const SNAPSHOT_MAX_DELAY_MS = 600;
// How long output may be held for a snapshot that has not come back. Anything
// still queued after this is written as-is: stale-but-live beats a frozen pane.
const SNAPSHOT_HOLD_TIMEOUT_MS = 2500;

// Every step of connecting has to end on its own. A phone waking from sleep
// keeps sockets that look alive but carry nothing, so a request made right
// after the resume can hang for minutes — long enough for the browser to sit
// on "loading" forever and for the reconnect chain to stall behind it.
const AUTH_FETCH_TIMEOUT_MS  = 8000;    // /auth/session must answer by then
const WS_OPEN_TIMEOUT_MS     = 10000;   // …and the socket must reach OPEN
const RECONNECT_BASE_MS      = 1000;
const RECONNECT_MAX_MS       = 15000;
const RECONNECT_JITTER_MS    = 250;
// A socket the network dropped without telling us still reads as OPEN. Probe
// it whenever the page comes back, and whenever it has been quiet for a while.
const LIVENESS_TIMEOUT_MS    = 5000;    // probe reply window
const LIVENESS_IDLE_MS       = 30000;   // silence that makes a probe worthwhile
const CONNECTION_SUPERVISOR_MS = 15000; // last-resort sweep
const AUTH_URL = '/auth/session';
// Handshakes that fail this many times in a row are treated as "the cookie is
// no longer accepted" rather than "the network is down", and earn one fetch.
const HANDSHAKE_FAILURES_BEFORE_REAUTH = 2;
const SESSION_REFRESH_AFTER_MS = 60 * 60 * 1000;  // cookie age worth renewing
const SESSION_REFRESH_DELAY_MS = 30000;           // …once the link looks stable
// How long to wait for the load event before connecting regardless.
const CONNECT_AFTER_LOAD_FALLBACK_MS = 3000;
// Turning a restored tab back into a normal navigation (see
// renavigateIfRestored). Set to false to keep restored tabs as they are.
const RENAVIGATE_RESTORED_TABS = true;
const RENAVIGATE_KEY = 'web-tmux-renavigated-at';
const RENAVIGATE_MIN_INTERVAL_MS = 30000;
// Resume diagnostics, reported over the socket because a phone has no console.
// Flip to false once the loading-indicator question is settled.
const CLIENT_DIAGNOSTICS = true;

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

// Text colours too close to the background are lightened or darkened until
// they reach this contrast (WCAG AA). Applications pick greys for a dark or a
// light terminal without knowing ours: Nord's bright black on its background
// is 1.6:1, and xterm's black on the dark theme is the background itself.
// history.js applies the same rule so its rows still match the screen.
const MIN_CONTRAST_RATIO = 4.5;

const THEME_STORAGE_KEY = 'web-tmux-theme';
const VALID_THEMES = ['dark', 'light', 'nord'];

const FONT_SIZE_STORAGE_KEY = 'web-tmux-font-size';
const VALID_FONT_SIZES = [11, 12, 13, 14, 16, 18];
const DEFAULT_FONT_SIZE = 13;

const VIRTUAL_KEYBOARD_MODE_STORAGE_KEY = 'web-tmux-virtual-keyboard-mode';
const VALID_VIRTUAL_KEYBOARD_MODES = ['auto', 'on', 'off'];
const DEFAULT_VIRTUAL_KEYBOARD_MODE = 'auto';

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
  // Overlay rows are coloured from the theme at build time, so drop them.
  if (window.HistoryOverlay) HistoryOverlay.close();
  document.querySelectorAll('#theme-menu [data-theme-name]').forEach(btn => {
    btn.classList.toggle('active', btn.dataset.themeName === name);
  });
}

function currentFontSize() {
  const saved = parseInt(localStorage.getItem(FONT_SIZE_STORAGE_KEY), 10);
  return VALID_FONT_SIZES.includes(saved) ? saved : DEFAULT_FONT_SIZE;
}

function terminalScrollback() {
  return isMobileWidth() ? MOBILE_SCROLLBACK : DESKTOP_SCROLLBACK;
}

function applyTerminalScrollOptions() {
  const scrollback = terminalScrollback();
  Object.values(panes).forEach((p) => {
    p.term.options.scrollback = scrollback;
  });
}

function initFontSize() {
  applyFontSize(currentFontSize(), false);
}

function applyFontSize(size, save = true) {
  if (!VALID_FONT_SIZES.includes(size)) return;
  if (save) localStorage.setItem(FONT_SIZE_STORAGE_KEY, String(size));
  Object.values(panes).forEach(p => { p.term.options.fontSize = size; });
  // Row height changes, so the overlay's metrics and rows are both stale.
  if (window.HistoryOverlay) HistoryOverlay.close();
  document.querySelectorAll('#font-size-menu [data-font-size]').forEach(btn => {
    btn.classList.toggle('active', parseInt(btn.dataset.fontSize, 10) === size);
  });
  if (Object.keys(panes).length > 0) {
    resetResizeCache();
    refitCurrentLayout();
  }
}

function hasTouchInput() {
  const coarsePointer = window.matchMedia && window.matchMedia('(any-pointer: coarse)').matches;
  return navigator.maxTouchPoints > 0 || !!coarsePointer;
}

function usesTouchInterface() {
  return isMobileWidth() || hasTouchInput();
}

function currentVirtualKeyboardMode() {
  const saved = localStorage.getItem(VIRTUAL_KEYBOARD_MODE_STORAGE_KEY);
  return VALID_VIRTUAL_KEYBOARD_MODES.includes(saved) ? saved : DEFAULT_VIRTUAL_KEYBOARD_MODE;
}

function shouldShowVirtualKeyboard(mode) {
  if (mode === 'on') return true;
  if (mode === 'off') return false;
  // Width keeps the previous phone behaviour, while touch capability catches
  // iPads in both orientations (including iPadOS's desktop-style user agent).
  return usesTouchInterface();
}

function applyVirtualKeyboardMode(mode, save = true) {
  if (!VALID_VIRTUAL_KEYBOARD_MODES.includes(mode)) return;
  if (save) localStorage.setItem(VIRTUAL_KEYBOARD_MODE_STORAGE_KEY, mode);

  const root = document.documentElement;
  const wasVisible = root.classList.contains('virtual-keyboard-visible');
  const visible = shouldShowVirtualKeyboard(mode);
  root.classList.toggle('virtual-keyboard-visible', visible);

  const toggle = document.getElementById('virtual-keyboard-toggle');
  const modeLabel = mode === 'auto' ? 'Auto' : mode === 'on' ? 'On' : 'Off';
  const stateLabel = visible ? 'shown' : 'hidden';
  toggle.classList.toggle('active', visible);
  toggle.setAttribute('aria-pressed', visible ? 'true' : 'false');
  toggle.title = `Virtual keys: ${modeLabel} (${stateLabel})`;
  toggle.setAttribute('aria-label', toggle.title);

  document.querySelectorAll('#virtual-keyboard-menu [data-virtual-keyboard-mode]').forEach((btn) => {
    btn.classList.toggle('active', btn.dataset.virtualKeyboardMode === mode);
  });

  if (visible !== wasVisible && Object.keys(panes).length > 0) {
    requestAnimationFrame(() => {
      resetResizeCache();
      refitCurrentLayout();
    });
  }
}

function initVirtualKeyboard() {
  applyVirtualKeyboardMode(currentVirtualKeyboardMode(), false);
}

// ─── State ────────────────────────────────────────────────────────────────────

let ws             = null;
let panes          = {};     // pane_id → { term, fitAddon, el, cached }
let activePaneId   = null;
let currentSession = '';     // tmux session currently displayed
let currentHost    = 'local'; // host the server's control client is on
// Which host's pane ids the panes on screen are. Echoed on everything aimed
// at a pane, so the server can drop what was meant for a host it has left.
let currentGen     = 0;
let hostsModel     = [];     // [{ host, active, reachable, code, sessions }]
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
let _viewportRefitTimer = null;
let _layoutApplying = false;
let _heldClientPrefix = false;
let _heldClientPrefixPaneId = null;
let _heldClientPrefixTimer = null;
let _editingSessionName = '';
let _editingWindowIndex = null;
let _confirmDeleteSession = '';
let _confirmDeleteWindow = null;
let _layouts = [];                // saved layout slots, newest first
let _savingLayout = false;        // the "name this slot" editor is open
let _confirmDeleteLayout = '';
let _overwriteLayout = '';        // slot awaiting an overwrite confirmation
let _conflictLayout = '';         // slot whose session name is already taken
const _pendingSnapshotPanes = new Set();
const _scheduledSnapshotPanes = new Set();
const _snapshotWatchdogs = new Map();   // pane → timer holding output hostage
const _snapshotsInFlight = new Map();   // pane → get_snapshot requests not yet answered
let _snapshotRefreshDeadline = 0;
const _bufferedPaneOutput = new Map();
const _paneOutputScanTail = new Map();
const _historyRequested = new Set();
let _lastViewportSize = { width: 0, height: 0 };
let _lastTouchScrollAt = 0;
const MOBILE_TMUX_COL_SAFETY_MARGIN = 1;

let _reclaimTriedFor = '';   // the foreign window size markClientActive last resent ours over

function markClientActive() {
  _clientActive = true;
  if (ws && ws.readyState === WebSocket.OPEN) {
    ws.send(JSON.stringify({ type: 'client_active' }));
  }
  // Another tab or device has resized tmux since our last resize, and this
  // one is being used again without a focus event (a second device that was
  // already showing): its size wins again. Once per foreign size — a plain
  // `tmux attach` client keeps the window under window-size latest, and
  // retrying on every keystroke would redraw the pane on every keystroke.
  const size = layoutWindowSize(_currentLayoutStr);
  if (size && _lastResize.cols && !holdsWindowSize(size) && size.key !== _reclaimTriedFor) {
    _reclaimTriedFor = size.key;
    refitCurrentLayout();
  }
}

function layoutWindowSize(layoutStr) {
  const m = (layoutStr || '').match(/^[^,]*,(\d+)x(\d+),/);
  return m ? { cols: +m[1], rows: +m[2], key: `${m[1]}x${m[2]}` } : null;
}

function holdsWindowSize(size) {
  return size.cols === _lastResize.cols && size.rows === _lastResize.rows;
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
  // Touch-oriented browsers can round xterm's sub-pixel cell width down just enough
  // for the final column to be painted outside the visible pane. TUIs that
  // position their cursor at the right edge then appear shifted or truncated.
  // Keep one spare xterm column there; pointer-only desktop keeps the exact fit.
  cols = Math.floor(cols) - (usesTouchInterface() ? MOBILE_TMUX_COL_SAFETY_MARGIN : 0);
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

// The live terminal is only ever a painter of the current screen: the
// scrollback is the overlay's (history.js), and .xterm-viewport is
// overflow:hidden. Its viewport can still drift off the base row — xterm's
// Buffer.resize keeps the view anchored to its old line when the row count
// changes, and its own wheel handler runs whenever the overlay declines a
// gesture — and a terminal parked above the base row stops showing new output
// altogether: the pane looks frozen until the page is reloaded.
function pinToBottom(p) {
  if (!p) return;
  try {
    const buffer = p.term.buffer.active;
    if (buffer.viewportY !== buffer.baseY) p.term.scrollToBottom();
  } catch (_) {}
}

function getActivePane() {
  if (activePaneId && panes[activePaneId]) return panes[activePaneId];
  const firstPaneId = Object.keys(panes)[0];
  return firstPaneId ? panes[firstPaneId] : null;
}

function splitUtf8Chunks(data, maxBytes = MAX_INPUT_CHUNK_BYTES) {
  const encoder = new TextEncoder();
  const chunks = [];
  let chunk = '';
  let chunkBytes = 0;
  for (const char of data) {
    const charBytes = encoder.encode(char).byteLength;
    if (chunk && chunkBytes + charBytes > maxBytes) {
      chunks.push(chunk);
      chunk = '';
      chunkBytes = 0;
    }
    chunk += char;
    chunkBytes += charBytes;
  }
  if (chunk) chunks.push(chunk);
  return chunks;
}

function sendPaneInput(data, paneId) {
  if (!data || !ws || ws.readyState !== WebSocket.OPEN) return;
  const targetPaneId = paneId || activePaneId || Object.keys(panes)[0];
  if (!targetPaneId) return;
  if (!isAutomaticInput(data)) markClientActive();
  splitUtf8Chunks(data).forEach((chunk) => {
    ws.send(JSON.stringify({ type: 'input', pane: targetPaneId, data: chunk, gen: currentGen }));
  });
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
  // Typing means "back to live" — but xterm's own DA/CPR replies come through
  // here too, and a snapshot's queries would otherwise yank the user out of the
  // scrollback they are reading.
  // Focus reports (\x1b[I / \x1b[O) are not typing either: pressing the
  // overlay's text to select it blurs the terminal.
  if (!isAutomaticInput(data)) HistoryOverlay.close();

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

// WebGL rendering moves per-frame drawing to the GPU. Must be loaded after
// term.open(); falls back silently to xterm 5.3's built-in DOM renderer when
// WebGL2 is unavailable or the context is later lost (low-memory devices, GPU
// driver resets, etc). Scrolling no longer depends on this either way — the
// scrollback is plain DOM in the overlay (history.js) and the live terminal
// only ever repaints on real output.
function loadWebglAddon(term) {
  const addonCtor = window.WebglAddon && window.WebglAddon.WebglAddon;
  if (!addonCtor) return null;

  let addon = null;
  try {
    addon = new addonCtor();
    addon.onContextLoss(() => { try { addon.dispose(); } catch (_) {} });
    term.loadAddon(addon);
    return addon;
  } catch (e) {
    console.warn('webgl addon failed to load, falling back to the DOM renderer', e);
    // An addon that threw half-way through activate() stays registered on the
    // terminal, so unregister it here rather than leaving term.dispose() to
    // trip over it later (see disposePaneTerminal).
    try { if (addon) addon.dispose(); } catch (_) {}
    return null;
  }
}

// xterm 5.3 tears its core down before the addon manager runs, so by the time
// WebglAddon.dispose() asks the terminal for a DOM renderer to fall back to,
// `_core._createRenderer()` returns undefined and RenderService.setRenderer
// throws. The exception escapes destroyPane into whatever WebSocket message
// handler is running (onLayoutChange, onInit, ...), which then never creates
// the new panes, never repositions the layout and never asks for a snapshot —
// the screen sits stale until the page is reloaded. Disposing the addon while
// the terminal is still alive takes the same fallback path successfully.
function disposePaneTerminal(p) {
  try { if (p.webglAddon) p.webglAddon.dispose(); } catch (e) { console.warn('webgl dispose failed', e); }
  try { p.term.dispose(); } catch (e) { console.warn('terminal dispose failed', e); }
}

function hasNonAsciiText(data) {
  return /[^\x00-\x7f]/.test(data);
}

function isMouseReportingSequence(data) {
  if (!data || data.length < 3) return false;
  if (data.charCodeAt(0) !== 0x1b || data.charCodeAt(1) !== 0x5b) return false;
  const c = data.charCodeAt(2);
  return c === 0x4d || c === 0x3c; // X10 mouse 'M' or SGR mouse '<'
}

// Every reply xterm 5.3 sends to an application's query: DA1/DA2, DSR/CPR,
// DECRQM ($y), window reports (t), colour reports (OSC) and DECRQSS (DCS $r).
// A modified F3 (\x1b[1;2R) reads the same as a CPR; nothing else typed does.
function isTerminalResponseSequence(data) {
  return /^(?:\x1b\[[0-9;?>]*\$?[Rcnty]|\x1b\]\d+;[^\x07\x1b]*(?:\x1b\\|\x07)|\x1bP[01]\$r[^\x1b]*\x1b\\)+$/
    .test(data || '');
}

// What xterm sends on its own rather than because someone is using this tab:
// query replies, and focus reports (\x1b[I / \x1b[O). Every browser showing
// the pane replies to the same query, so none of this may claim the window
// size for the device it came from.
function isAutomaticInput(data) {
  return isTerminalResponseSequence(data) || /^(?:\x1b\[[IO])+$/.test(data || '');
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

// The keys that turn the text `replaced` (just before the cursor) into `data`:
// a DEL for each character of the old text past what the two share, then
// the new tail.
function textReplacementInput(replaced, data) {
  const oldChars = [...replaced];
  const newChars = [...data];
  let same = 0;
  while (same < oldChars.length && same < newChars.length && oldChars[same] === newChars[same]) same++;
  return '\x7f'.repeat(oldChars.length - same) + newChars.slice(same).join('');
}

// iOS dictation inserts each guess as it hears more, putting the new guess in
// place of the last one, which is still sitting in xterm's textarea. xterm
// sends every insertText whole, so the shell got "音声音声入力音声入力テスト…".
// Catch those replacements before xterm does and send only the difference.
function guardTextReplacement(term, onData) {
  const textarea = term.textarea;
  if (!textarea || !term.element) return;
  let before = null;
  term.element.addEventListener('beforeinput', (ev) => {
    before = null;
    if (ev.target !== textarea || ev.inputType !== 'insertText' || ev.isComposing) return;
    const { value, selectionStart: start, selectionEnd: end } = textarea;
    // Only a selection running to the end of the text ends at the shell's cursor.
    if (start < end && end === value.length) before = { value, start };
  }, true);
  term.element.addEventListener('input', (ev) => {
    const old = before;
    before = null;
    if (!old || ev.target !== textarea || ev.inputType !== 'insertText'
        || !ev.data || ev.isComposing) return;
    if (textarea.value !== old.value.slice(0, old.start) + ev.data) return;
    ev.stopImmediatePropagation();
    const data = textReplacementInput(old.value.slice(old.start), ev.data);
    if (data) onData(data);
  }, true);
}

function scheduleSnapshotRefresh(paneIds) {
  const ids = paneIds && paneIds.length ? paneIds : Object.keys(panes);
  ids.forEach((paneId) => {
    if (paneId) _scheduledSnapshotPanes.add(paneId);
  });
  const now = Date.now();
  if (!_snapshotRefreshDeadline) _snapshotRefreshDeadline = now + SNAPSHOT_MAX_DELAY_MS;
  const delay = Math.max(0, Math.min(SNAPSHOT_DEBOUNCE_MS, _snapshotRefreshDeadline - now));
  if (_snapshotRefreshTimer) clearTimeout(_snapshotRefreshTimer);
  _snapshotRefreshTimer = setTimeout(() => {
    _snapshotRefreshTimer = null;
    _snapshotRefreshDeadline = 0;
    const ids = [..._scheduledSnapshotPanes];
    _scheduledSnapshotPanes.clear();
    const connected = ws && ws.readyState === WebSocket.OPEN;
    ids.forEach((paneId) => {
      // Nothing will answer for a pane that is gone or while the socket is
      // down, so release its buffered output instead of holding it forever.
      if (!connected || !panes[paneId]) {
        flushBufferedOutput(paneId);
        return;
      }
      _pendingSnapshotPanes.add(paneId);
      armSnapshotWatchdog(paneId);
      _snapshotsInFlight.set(paneId, (_snapshotsInFlight.get(paneId) || 0) + 1);
      ws.send(JSON.stringify({ type: 'get_snapshot', pane: paneId, gen: currentGen }));
    });
  }, delay);
}

function markSnapshotPending(paneIds) {
  (paneIds || []).forEach((paneId) => {
    if (!paneId) return;
    _pendingSnapshotPanes.add(paneId);
    armSnapshotWatchdog(paneId);
  });
}

// Output for a pane awaiting a snapshot is buffered (see onOutput) so the
// redraw cannot land on top of newer bytes. Every hold gets a deadline: a
// snapshot that is never requested or never answered must not freeze the pane.
function armSnapshotWatchdog(paneId) {
  clearSnapshotWatchdog(paneId);
  _snapshotWatchdogs.set(paneId, setTimeout(() => {
    _snapshotWatchdogs.delete(paneId);
    console.warn('snapshot timed out, releasing buffered output', paneId);
    flushBufferedOutput(paneId);
  }, SNAPSHOT_HOLD_TIMEOUT_MS));
}

function clearSnapshotWatchdog(paneId) {
  const timer = _snapshotWatchdogs.get(paneId);
  if (timer) clearTimeout(timer);
  _snapshotWatchdogs.delete(paneId);
}

// Stop holding output for paneId and write whatever piled up.
function flushBufferedOutput(paneId) {
  clearSnapshotWatchdog(paneId);
  _pendingSnapshotPanes.delete(paneId);
  _snapshotsInFlight.delete(paneId);
  const queued = _bufferedPaneOutput.get(paneId);
  _bufferedPaneOutput.delete(paneId);
  const p = panes[paneId];
  if (!p || !queued || queued.length === 0) return;
  p.term.write(queued.length === 1 ? queued[0] : concatBytes(queued));
}

function flushAllBufferedOutput() {
  new Set([..._pendingSnapshotPanes, ..._bufferedPaneOutput.keys()])
    .forEach(flushBufferedOutput);
}

function mergedScanBytes(paneId, data) {
  const tail = _paneOutputScanTail.get(paneId);
  if (!tail || tail.length === 0) return data;
  return concatBytes([tail, data]);
}

function rememberScanTail(paneId, data) {
  if (!paneId) return;
  const len = Math.min(data.length, OUTPUT_SCAN_TAIL_BYTES);
  if (len === 0) {
    _paneOutputScanTail.delete(paneId);
    return;
  }
  _paneOutputScanTail.set(paneId, data.slice(data.length - len));
}

function containsAltScreenExit(bytes) {
  for (let i = 0; i < bytes.length - 4; i++) {
    if (bytes[i] !== 0x1b || bytes[i + 1] !== 0x5b || bytes[i + 2] !== 0x3f) continue; // ESC [ ?
    let j = i + 3;
    while (j < bytes.length) {
      const ch = bytes[j];
      if (ch === 0x6c) { // l: DEC private mode reset
        const params = String.fromCharCode(...bytes.slice(i + 3, j)).split(';');
        if (params.some((param) => ALT_SCREEN_EXIT_PARAMS.has(param))) return true;
        break;
      }
      if ((ch >= 0x30 && ch <= 0x39) || ch === 0x3b) {
        j++;
        continue;
      }
      break;
    }
  }
  return false;
}

function maybeRefreshAfterAltScreenExit(paneId, data) {
  if (!paneId || !data || data.length === 0) return;
  if (panes[paneId] && panes[paneId].cached) return;
  const scanBytes = mergedScanBytes(paneId, data);
  if (containsAltScreenExit(scanBytes)) {
    scheduleSnapshotRefresh([paneId]);
  }
  rememberScanTail(paneId, scanBytes);
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

function updateCurrentWindow(windows) {
  const activeWin = (windows || []).find(w => w.active);
  if (!activeWin) return;
  currentWinIdx = activeWin.index;
  currentWinId = activeWin.id || '';
  updateCurrentBlock();
}

// ─── WebSocket ────────────────────────────────────────────────────────────────

let _connecting = false;
let _reconnectTimer = null;
let _reconnectAttempt = 0;
let _authAbort = null;          // aborts an /auth/session fetch that hangs
let _wsOpenTimer = null;        // gives up on a socket stuck in CONNECTING
let _livenessTimer = null;      // waits for the reply to a liveness probe
let _lastMessageAt = 0;         // last frame received from the server
let _connectStartedAt = 0;      // start of the attempt in flight
let _authNeeded = true;         // fetch a cookie before the next handshake
let _lastAuthAt = 0;            // when the cookie was last issued
let _lastOpenedAt = 0;          // when the current socket reached OPEN
let _handshakeFailures = 0;     // sockets closed before ever opening
let _lastOpenedSocket = null;   // the most recent socket that reached OPEN;
                                // a socket closing that is not this one never
                                // got past the handshake
// Counters reported to the server: a gap between attempts and completions is
// what an unfinished request looks like from inside the page.
let _authAttempts = 0;
let _authOk = 0;
let _authAborted = 0;
let _wsAttempts = 0;
let _lastResume = 'load';
let _diagnosticsSent = false;   // a diagnostic went out on this socket
let _diagnosticsOff = false;    // …and the server did not accept it
let _renavigated = false;       // this document is on its way out

function clearReconnectTimer() {
  if (_reconnectTimer) {
    clearTimeout(_reconnectTimer);
    _reconnectTimer = null;
  }
}

function clearWsOpenTimer() {
  if (_wsOpenTimer) {
    clearTimeout(_wsOpenTimer);
    _wsOpenTimer = null;
  }
}

function clearLivenessTimer() {
  if (_livenessTimer) {
    clearTimeout(_livenessTimer);
    _livenessTimer = null;
  }
}

function scheduleReconnect() {
  if (_reconnectTimer) return;
  const backoff = Math.min(
    RECONNECT_MAX_MS, RECONNECT_BASE_MS * 2 ** _reconnectAttempt,
  );
  _reconnectAttempt++;
  _reconnectTimer = setTimeout(() => {
    _reconnectTimer = null;
    connect();
  }, backoff + Math.random() * RECONNECT_JITTER_MS);
}

async function connect() {
  // Every in-flight path below ends in onclose or in the catch, both of which
  // schedule the next attempt — but re-arm here too, so a caller that finds an
  // attempt already running can never be the one that drops the chain.
  if (_connecting || (ws && (
    ws.readyState === WebSocket.OPEN || ws.readyState === WebSocket.CONNECTING
  ))) {
    if (!ws || ws.readyState !== WebSocket.OPEN) scheduleReconnect();
    return;
  }
  _connecting = true;
  _connectStartedAt = Date.now();
  _historyRequested.clear();
  _snapshotsInFlight.clear();   // the old socket's requests will never be answered
  setStatus('connecting');
  try {
    // The cookie outlives the page by a month, so a resume normally opens the
    // socket straight away. That matters beyond saving a round trip: WebKit
    // counts an unfinished fetch as page loading, and a fetch issued in the
    // first seconds after a phone wakes up can hang without ever finishing —
    // leaving the browser spinning on a page that otherwise works perfectly.
    // Ask for a cookie only when the handshake has told us we need one.
    if (_authNeeded) await fetchSession();

    _wsAttempts++;
    const socket = new WebSocket(WS_URL);
    ws = socket;
    clearWsOpenTimer();
    // A socket that never leaves CONNECTING would otherwise block every
    // retry: the guard above treats it as an attempt still in progress.
    _wsOpenTimer = setTimeout(() => {
      _wsOpenTimer = null;
      if (socket.readyState === WebSocket.CONNECTING) socket.close();
    }, WS_OPEN_TIMEOUT_MS);
    socket.onopen = () => {
      clearWsOpenTimer();
      _lastOpenedSocket = socket;
      _lastOpenedAt = Date.now();
      _lastMessageAt = _lastOpenedAt;
      _reconnectAttempt = 0;
      _handshakeFailures = 0;
      _authNeeded = false;
      setStatus('connected');
      if (document.visibilityState !== 'hidden') markClientActive();
      reportDiagnostics();
      renavigateIfRestored();
    };
    socket.onclose = (ev) => {
      // 1008 is the server rejecting something we sent. The only message it
      // could object to here is the diagnostic one — an older server does not
      // know the type — so stop sending it rather than loop: connect, get
      // closed, reconnect.
      if (ev && ev.code === 1008 && _diagnosticsSent) {
        console.warn('server rejected a message; disabling diagnostics');
        _diagnosticsOff = true;
      }
      if (ws === socket) {
        ws = null;
        clearWsOpenTimer();
        clearLivenessTimer();
        // Closed before it ever opened: either the handshake was rejected —
        // the cookie is gone, expired, or signed by a server that has since
        // restarted — or the network is down. Re-auth only once the second
        // attempt has failed too, so a dead network does not drag an HTTP
        // request along on every retry.
        if (socket !== _lastOpenedSocket) {
          _handshakeFailures++;
          if (_handshakeFailures >= HANDSHAKE_FAILURES_BEFORE_REAUTH) _authNeeded = true;
        }
        setStatus('disconnected');
        scheduleReconnect();
      }
    };
    socket.onerror = () => setStatus('disconnected');
    socket.onmessage = (ev) => {
      _lastMessageAt = Date.now();
      clearLivenessTimer();
      let msg;
      try { msg = JSON.parse(ev.data); }
      catch (e) { console.error('parse error', e); return; }
      try { handleMsg(msg); }
      catch (e) { onHandlerFailure(msg, e); }
    };
  } catch (error) {
    console.error('connection failed', error);
    setStatus('disconnected');
    scheduleReconnect();
  } finally {
    _connecting = false;
  }
}

async function fetchSession() {
  const abort = new AbortController();
  _authAbort = abort;
  _authAttempts++;
  const timer = setTimeout(() => abort.abort(), AUTH_FETCH_TIMEOUT_MS);
  try {
    const auth = await fetch(AUTH_URL, {
      method: 'GET',
      credentials: 'same-origin',
      cache: 'no-store',
      signal: abort.signal,
    });
    if (!auth.ok) throw new Error(`session authorization failed: ${auth.status}`);
    _authOk++;
    _lastAuthAt = Date.now();
    _authNeeded = false;
  } catch (error) {
    if (abort.signal.aborted) _authAborted++;
    throw error;
  } finally {
    clearTimeout(timer);
    if (_authAbort === abort) _authAbort = null;
  }
}

// Renew the cookie well before it expires, but only from a connection that has
// already been up for a while: by then the network has proved it works, which
// is exactly what the moment of waking up cannot promise.
function maybeRefreshSession() {
  if (_connecting || !ws || ws.readyState !== WebSocket.OPEN) return;
  if (document.visibilityState === 'hidden') return;
  const now = Date.now();
  if (now - _lastOpenedAt < SESSION_REFRESH_DELAY_MS) return;
  if (now - _lastAuthAt < SESSION_REFRESH_AFTER_MS) return;
  fetchSession().catch((e) => console.warn('session refresh failed', e));
}

// Reconnect now, whatever the socket currently claims. Used by the status
// dot and by any resume that finds the connection unusable.
function reconnectNow() {
  clearReconnectTimer();
  clearWsOpenTimer();
  clearLivenessTimer();
  _reconnectAttempt = 0;
  if (_authAbort) _authAbort.abort();
  const socket = ws;
  if (socket) {
    ws = null;
    socket.onclose = null;
    try { socket.close(); } catch (_) {}
  }
  connect();
}

// The server answers get_state with a 'state' frame, so any reply at all
// proves the socket still carries traffic. Silence means it is half-open:
// alive to readyState, dead on the wire.
function probeLiveness() {
  if (!ws || ws.readyState !== WebSocket.OPEN || _livenessTimer) return;
  const socket = ws;
  const sentAt = Date.now();
  clearLivenessTimer();
  _livenessTimer = setTimeout(() => {
    _livenessTimer = null;
    if (ws !== socket || socket.readyState !== WebSocket.OPEN) return;
    if (_lastMessageAt >= sentAt) return;
    console.warn('no reply to the liveness probe — reconnecting');
    reconnectNow();
  }, LIVENESS_TIMEOUT_MS);
  wsSend('get_state');
}

// What the page saw at the last resume, sent once per connection. The browser
// showing "loading" on a page that works has to be explained from inside the
// page: whether this document is still the one being loaded (readyState,
// navigation type), whether the browser navigated again instead of restoring
// from the back/forward cache, and whether any request we made never finished.
function reportDiagnostics() {
  if (!CLIENT_DIAGNOSTICS || _diagnosticsOff) return;
  let nav = '?';
  let loadEnd = -1;
  try {
    const entry = performance.getEntriesByType('navigation')[0];
    nav = (entry && entry.type) || '?';
    // 0 means the load event never fired: the document itself is still
    // loading, as opposed to the browser merely showing that it is.
    if (entry) loadEnd = Math.round(entry.loadEventEnd);
  } catch (_) {}
  const fields = [
    `ev=${_lastResume}`,
    `nav=${nav}`,
    `rs=${document.readyState}`,
    `le=${loadEnd}`,
    `online=${navigator.onLine}`,
    // Whether this document was served by the worker: on a restored tab that
    // is the difference between a load that touched the network and one that
    // did not.
    `sw=${navigator.serviceWorker && navigator.serviceWorker.controller ? 1 : 0}`,
    `auth=${_authAttempts}/${_authOk}/${_authAborted}`,   // tried/ok/aborted
    `ws=${_wsAttempts}`,
    `hsfail=${_handshakeFailures}`,
    `age=${Math.round(performance.now() / 1000)}s`,
  ];
  _diagnosticsSent = wsSend('client_log', { text: fields.join(' ').slice(0, 200) });
}

function noteResume(kind) {
  _lastResume = kind;
}

// A tab that the phone discarded comes back as a history navigation, and
// WebKit then leaves the browser showing "loading" for good: measurements at
// that moment show a completed load event, every request finished, and
// nothing pending — the stop button has nothing to cancel either. A normal
// navigation never ends up in that state, and with the service worker in
// place one costs no network at all, so replace this document with one.
//
// Waiting for the socket means the app is known to work before the page is
// thrown away, and the guard in sessionStorage means this can happen at most
// once per tab per window of time — if the guard cannot be written, the
// workaround is skipped rather than risk a reload loop.
function renavigateIfRestored() {
  if (!RENAVIGATE_RESTORED_TABS || _renavigated) return;
  let nav = '';
  try { nav = performance.getEntriesByType('navigation')[0]?.type || ''; }
  catch (_) { return; }
  if (nav !== 'back_forward') return;
  try {
    const last = parseInt(sessionStorage.getItem(RENAVIGATE_KEY), 10) || 0;
    if (Date.now() - last < RENAVIGATE_MIN_INTERVAL_MS) return;
    sessionStorage.setItem(RENAVIGATE_KEY, String(Date.now()));
    if (parseInt(sessionStorage.getItem(RENAVIGATE_KEY), 10) <= last) return;
  } catch (_) {
    return;   // no way to remember we did this — leave the tab alone
  }
  _renavigated = true;
  console.info('restored tab: reloading as a normal navigation');
  location.replace(location.href);
}

// Single entry point for "the page is back / time passed — is the connection
// still real?". Anything stuck past its deadline is torn down here so the
// normal reconnect path can take over.
function ensureConnected(resumed) {
  if (_connecting) {
    if (Date.now() - _connectStartedAt > AUTH_FETCH_TIMEOUT_MS && _authAbort) {
      _authAbort.abort();
    }
    return;
  }
  if (!ws || ws.readyState === WebSocket.CLOSED || ws.readyState === WebSocket.CLOSING) {
    clearReconnectTimer();
    if (resumed) _reconnectAttempt = 0;
    connect();
    return;
  }
  if (ws.readyState === WebSocket.CONNECTING) {
    if (Date.now() - _connectStartedAt > WS_OPEN_TIMEOUT_MS) ws.close();
    return;
  }
  // A resume that finds the socket still alive reports nothing otherwise, and
  // that is exactly the moment worth seeing in the log.
  if (resumed) reportDiagnostics();
  if (resumed || Date.now() - _lastMessageAt > LIVENESS_IDLE_MS) probeLiveness();
}

setInterval(() => {
  if (document.visibilityState === 'hidden') return;
  ensureConnected(false);
  maybeRefreshSession();
}, CONNECTION_SUPERVISOR_MS);

// A handler that dies half-way leaves the client inconsistent — panes not
// created, the layout not repositioned, output still held for a snapshot that
// was never requested — and nothing would ever ask again, so the screen stays
// stale until the page is reloaded. Release the held output and pull the
// current view again so the next frame is authoritative.
function onHandlerFailure(msg, err) {
  console.error('message handler failed:', msg && msg.type, err);
  flushAllBufferedOutput();
  scheduleCurrentViewRefresh();
  scheduleStateRefresh();
}

// ─── Message handlers ─────────────────────────────────────────────────────────

function handleMsg(msg) {
  switch (msg.type) {
    case 'init':            onInit(msg);           break;
    case 'window_switched': onWindowSwitched(msg); break;
    case 'snapshot':        onSnapshot(msg);        break;
    case 'history':         onHistory(msg);         break;
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
    case 'layouts':         onLayouts(msg);        break;
    case 'layout_conflict': onLayoutConflict(msg); break;
    case 'layout_error':    onLayoutError(msg);    break;
    case 'restore_result':  onRestoreResult(msg);  break;
    case 'clipboard':       onClipboard(msg);      break;
    case 'hosts':           onHosts(msg);          break;
    case 'host_error':      onHostError(msg);      break;
    case 'save_result':     onSaveResult(msg);     break;
    case 'host_auth_prompt': onHostAuthPrompt(msg); break;
    case 'host_auth_closed': onHostAuthClosed(msg); break;
    case 'dir_listing':     onDirListing(msg);     break;
    case 'file_content':    onFileContent(msg);    break;
    case 'file_error':      onFileError(msg);      break;
    case 'agent_status':    onAgentStatus(msg);    break;
  }
}

function onState(msg) {
  if (msg.session) {
    currentSession = msg.session;
    document.getElementById('session-name').textContent = msg.session;
  }
  if (msg.sessions) updateSessions(msg);
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
  destroyAllPanes();
  resetResizeCache();
  currentSession = msg.session || '';
  document.getElementById('session-name').textContent = msg.session;
  _sessionNote = '';
  dropHostAuthPrompts();   // a reconnect means the server gave up on them
  _editingWindowIndex = null;   // its window belonged to the old session
  updateSessions(msg);
  // Another host's files, or a reply the old socket took with it.
  if (_files.open) {
    if (_files.host !== currentHost) closeFileViewer();
    else if (_files.pending) sendFileRequest(_files.pending);
    else if (_mdImages.inFlight) {
      _mdImages.queue.unshift(_mdImages.inFlight.path);
      _mdImages.inFlight = null;
      pumpMdImages();
    }
  }
  renderWindowList(msg.windows);
  renderPaneList(msg.panes, msg.active_pane);
  updateCurrentWindow(msg.windows);
  applyLayout(msg.panes, msg.layout_panes, msg.layout, msg.active_pane);
  scheduleSnapshotRefresh((msg.panes || []).map((p) => p.id));
  // Also refreshes the list after a reconnect. A plain send: every open tab
  // gets this init (reconnects, host switches), hidden ones included, and
  // none of them is being used because of it.
  wsSend('list_layouts');
}

function onWindowSwitched(msg) {
  resetResizeCache();   // new window → terminal size may differ
  if (msg.session) {
    currentSession = msg.session;
    document.getElementById('session-name').textContent = msg.session;
  }
  updateSessions(msg);
  renderWindowList(msg.windows);
  renderPaneList(msg.panes, msg.active_pane);
  updateCurrentWindow(msg.windows);
  hideAllPanes();
  markSnapshotPending((msg.panes || []).map(p => p.id));
  applyLayout(msg.panes, msg.layout_panes, msg.layout, msg.active_pane);
  scheduleSnapshotRefresh((msg.panes || []).map((p) => p.id));
}

// The server queues a snapshot behind the output frames that preceded its
// capture, so every byte held for the pane is already part of the capture: it
// goes in *before* the redraw (lines it scrolled away still reach the
// scrollback) and the redraw then puts the screen exactly where tmux has it.
// Replaying it after the redraw, as this used to, applied it twice — a TUI
// that redraws relative to its cursor ("up N rows, rewrite") landed on the
// wrong rows and the screen stayed shifted until the next full redraw.
function onSnapshot(msg) {
  const paneId = msg.pane;
  const left = (_snapshotsInFlight.get(paneId) || 1) - 1;
  if (left > 0) _snapshotsInFlight.set(paneId, left);
  else _snapshotsInFlight.delete(paneId);
  const held = _bufferedPaneOutput.get(paneId) || [];
  _bufferedPaneOutput.delete(paneId);
  // Another request is still out: keep holding until its snapshot arrives.
  if (left === 0) {
    clearSnapshotWatchdog(paneId);
    _pendingSnapshotPanes.delete(paneId);
  }
  const p = panes[paneId];
  if (!p) return;
  // The capture is laid out for the pane's size at that instant; drawn into
  // any other size its rows wrap or clip.
  if (validResize(msg.pane_cols, msg.pane_rows)
      && (p.term.cols !== msg.pane_cols || p.term.rows !== msg.pane_rows)) {
    try { p.term.resize(msg.pane_cols, msg.pane_rows); } catch (_) {}
  }
  const frame = buildSnapshotFrame(msg, p.term);
  p.term.write(held.length ? concatBytes([...held, frame]) : frame, () => {
    pinToBottom(p);
    // The frame may have turned mouse tracking on or off, which decides
    // whether a swipe scrolls the scrollback or goes to the application.
    if (paneId === activePaneId) HistoryOverlay.sync();
  });
}

// One-time scrollback seed for a pane (see requestInitialHistory).
function onHistory(msg) {
  const p = panes[msg.pane];
  if (!p) return;
  const data = lfToCrlf(b64ToUint8(msg.data || ''));
  if (data.length === 0) return;
  // Move the whole capture into the scrollback: term.rows newlines at the
  // bottom of the screen scroll every displayed history row out of the screen
  // area, leaving it blank for the snapshot that follows to draw into.
  const flush = asciiBytes('\r\n'.repeat(p.term.rows));
  p.term.write(concatBytes([data, flush]));
}

function onOutput(msg) {
  const p = panes[msg.pane];
  const data = b64ToUint8(msg.data);
  if (_pendingSnapshotPanes.has(msg.pane)) {
    queuePaneOutput(msg.pane, data);
    return;
  }
  if (p) {
    p.term.write(data, () => pinToBottom(p));
    maybeRefreshAfterAltScreenExit(msg.pane, data);
    if (msg.pane === activePaneId) HistoryOverlay.noteOutput();
  }
}

// tmux has already resized these panes, and every byte it sends from here on
// is formatted for the new size, so xterm takes it now. Placing the pane
// elements waits for a frame, and a hidden tab runs no frames at all: a
// snapshot drawn into the old size and resized afterwards leaves the cursor on
// the wrong row, and every later line lands there too.
function syncTermSizes(layoutPanes) {
  (layoutPanes || []).forEach(({ id, cols, rows }) => {
    const p = panes['%' + id];
    if (p && validResize(cols, rows) && (p.term.cols !== cols || p.term.rows !== rows)) {
      try { p.term.resize(cols, rows); } catch (_) {}
    }
  });
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
  const size = layoutWindowSize(msg.layout);
  if (size && holdsWindowSize(size)) _reclaimTriedFor = '';   // ours again: retry next time it is lost
  _currentLayoutStr   = msg.layout;
  _currentLayoutPanes = lp;

  // Pane IDs now in layout
  const layoutIds = new Set(lp.map(p => '%' + p.id));

  // Destroy panes that disappeared from the current window's layout.
  // Skip cached (hidden) panes — they belong to other windows and must survive.
  Object.keys(panes).forEach(id => {
    if (!layoutIds.has(id) && !panes[id].cached) destroyPane(id);
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
  syncTermSizes(lp);

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
  if (msg.sessions) updateSessions(msg);
  if (msg.windows) renderWindowList(msg.windows);
}

function onSessionsChanged(msg) {
  scheduleStateRefresh();
  if (msg.sessions) updateSessions(msg);
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
    syncTermSizes(lp);
  }

  const fallbackActivePane =
    (activePane && panes[activePane] && activePane) ||
    (lp && lp.length > 0 ? '%' + lp[0].id : null) ||
    (panesInfo && panesInfo.length > 0 ? panesInfo[0].id : null);

  // Mark the active pane before positioning so focus and outlines are current.
  if (fallbackActivePane) {
    setActivePaneVisual(fallbackActivePane);
    activePaneId = fallbackActivePane;
    if (!fieldOwnsKeyboard()) panes[fallbackActivePane]?.term.focus();
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
    // Fit all panes; pick the largest as the source for character dimensions.
    const refLp = layoutPanes.reduce((best, lp) =>
      lp.cols * lp.rows > best.cols * best.rows ? lp : best, layoutPanes[0]);

    let charW = 0, charH = 0;
    layoutPanes.forEach(({ id }) => {
      const pid = '%' + id;
      const p = panes[pid];
      if (!p) return;
      try { p.fitAddon.fit(); } catch (_) {}
      pinToBottom(p);
      if (pid === '%' + refLp.id && p.term.cols > 0) {
        // Prefer the renderer's exact CSS cell size (same value fitAddon uses
        // internally) — avoids the clientWidth integer-rounding that made
        // sendCols oscillate ±1 between positionPanes calls.
        try {
          const cell = p.term._core._renderService.dimensions.css.cell;
          if (cell && cell.width > 0) { charW = cell.width; charH = cell.height; }
        } catch (_) {}
        // Fallback if the internal path is unavailable.
        if (!charW && p.el.clientWidth > 0) {
          charW = p.el.clientWidth  / p.term.cols;
          charH = p.el.clientHeight / p.term.rows;
        }
      }
    });

    // Force each pane to exactly the col/row count tmux reports.
    // fit() derives cols from pixel width which can be ±1 off due to sub-pixel
    // rounding — if term.cols ≠ tmux's pane width, zsh output formatted for the
    // tmux width wraps or leaves a gap, causing the reversed-% and line-shift
    // artifacts visible in split view (zoom view is unaffected because a single
    // pane always matches the maybeSendResize value exactly).
    layoutPanes.forEach(({ id, cols: lCols, rows: lRows }) => {
      const pid = '%' + id;
      const p = panes[pid];
      if (!p) return;
      if (p.term.cols !== lCols || p.term.rows !== lRows) {
        try { p.term.resize(lCols, lRows); } catch (_) {}
        // xterm reflows its screen its own way; take tmux's instead.
        markSnapshotPending([pid]);
        scheduleSnapshotRefresh([pid]);
      }
    });

    // sendCols is derived from the renderer's true charW so it is stable across
    // positionPanes calls for the same font/window size.
    if (charW > 0 && charH > 0) {
      const sendCols = Math.floor(W / charW);
      const sendRows = Math.floor(H / charH);
      if (sendCols !== totalCols || sendRows !== totalRows) {
        maybeSendResize(sendCols, sendRows);
      }
    }
    _layoutApplying = false;
    HistoryOverlay.sync();
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
    fitSinglePane(paneId);
    _layoutApplying = false;
    HistoryOverlay.sync();
    focusActivePane({ defer: true, retries: 2 });
  }));
}

// A lone pane (or a zoomed one) fills the pane area: ask tmux for the size
// that fits it, but keep xterm at the size tmux actually has. tmux formats the
// pane's output for its own width — autowrap, zsh's line editor, relative
// cursor moves — so an xterm even one column wider (the touch safety margin
// below, or another device holding resize mastership) shifts every wrapped
// line. When tmux takes the new size, %layout-change brings it back here.
function fitSinglePane(paneId) {
  const p = panes[paneId];
  if (!p) return;
  let dims = null;
  try { dims = p.fitAddon.proposeDimensions(); } catch (e) { console.error('fit error:', e); }
  if (dims && Number.isFinite(dims.cols) && Number.isFinite(dims.rows)) {
    maybeSendResize(dims.cols, dims.rows);
  }
  const tmuxSize = _currentLayoutPanes.find((lp) => '%' + lp.id === paneId) || dims;
  if (tmuxSize && validResize(tmuxSize.cols, tmuxSize.rows)
      && (p.term.cols !== tmuxSize.cols || p.term.rows !== tmuxSize.rows)) {
    try { p.term.resize(tmuxSize.cols, tmuxSize.rows); } catch (_) {}
    // xterm reflows its screen its own way; take tmux's instead.
    markSnapshotPending([paneId]);
    scheduleSnapshotRefresh([paneId]);
  }
  pinToBottom(p);
}

// ─── Pane management ──────────────────────────────────────────────────────────

function ensurePane(paneId, cols, rows) {
  if (panes[paneId]) {
    if (panes[paneId].cached) {
      panes[paneId].el.style.display = '';
      panes[paneId].cached = false;
    }
    return;
  }

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
    allowProposedApi: true, // required by Unicode11Addon's `term.unicode.activeVersion` setter
    fontFamily:  FONT_FAMILY,
    fontSize:    currentFontSize(),
    scrollback:  terminalScrollback(),
    cursorBlink: true,
    scrollOnUserInput: true,
    minimumContrastRatio: MIN_CONTRAST_RATIO,
    theme:       XTERM_THEMES[currentThemeName()] || XTERM_THEMES.dark,
  });

  loadUnicode11Addon(term);

  const fitAddon = new FitAddon.FitAddon();
  term.loadAddon(fitAddon);
  term.open(el);
  const webglAddon = loadWebglAddon(term);
  // The viewport is overflow:hidden, so Viewport's `offsetWidth - scrollArea
  // .offsetWidth || 15` fallback reports a 15px scrollbar that isn't there, and
  // FitAddon.proposeDimensions() subtracts it from every pane's usable width.
  try { term._core.viewport.scrollBarWidth = 0; } catch (_) {}
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
  const onData = (data) => {
    if (shouldSuppressDuplicateTextInput(inputDeduper, data)) return;
    // xterm generates terminal replies on behalf of the pane that requested
    // them. Do not route these through activePane/client-prefix handling, or
    // OSC/DA/CPR replies can appear as literal garbage in another input line.
    if (isTerminalResponseSequence(data)) {
      sendPaneInput(data, paneId);
      return;
    }
    const sendData = applyCtrlModifier(data);
    if (isTerminalResponseSequence(sendData)) {
      handleTerminalInput(sendData, paneId);
      return;
    }
    if (isMouseReportingSequence(sendData) && activePaneId && paneId !== activePaneId) return;
    handleTerminalInput(sendData, activePaneId || paneId);
  };
  term.onData(onData);
  guardTextReplacement(term, onData);

  // Click to focus this pane
  el.addEventListener('mousedown', () => selectPane(paneId));
  panes[paneId] = { term, fitAddon, el, webglAddon, cached: false };
  requestInitialHistory(paneId);
}

// Seed xterm's scrollback from tmux history, once per pane. Snapshots only
// carry the visible screen, so this is the only path that brings in history.
function requestInitialHistory(paneId) {
  if (_historyRequested.has(paneId)) return;
  if (!ws || ws.readyState !== WebSocket.OPEN) return;
  _historyRequested.add(paneId);
  // Sent before the pane's first get_snapshot, and the server handles a
  // connection's messages strictly in order, so the history reply always
  // arrives first and the snapshot lands on top of it.
  ws.send(JSON.stringify({ type: 'get_history', pane: paneId, lines: INITIAL_HISTORY_LINES,
                           gen: currentGen }));
}

function destroyPane(paneId) {
  const p = panes[paneId];
  if (!p) return;
  HistoryOverlay.detach(p);  // unmount the overlay before its host element goes
  disposePaneTerminal(p);
  p.el.remove();
  delete panes[paneId];
  clearSnapshotWatchdog(paneId);
  _bufferedPaneOutput.delete(paneId);
  _pendingSnapshotPanes.delete(paneId);
  _scheduledSnapshotPanes.delete(paneId);
  _snapshotsInFlight.delete(paneId);
  _paneOutputScanTail.delete(paneId);
  _historyRequested.delete(paneId);
  if (activePaneId === paneId) activePaneId = null;
  HistoryOverlay.sync();
}

function destroyAllPanes() {
  Object.keys(panes).forEach(destroyPane);
}

function hideAllPanes() {
  Object.keys(panes).forEach(id => {
    const p = panes[id];
    if (!p) return;
    p.el.style.display = 'none';
    p.cached = true;
  });
}

function selectPane(paneId, opts) {
  const options = opts || {};
  markClientActive();
  activePaneId = paneId;
  setActivePaneVisual(paneId);
  focusActivePane({ retries: 2 });
  if (ws && ws.readyState === WebSocket.OPEN) {
    const payload = { type: 'select_pane', pane: paneId, gen: currentGen };
    if (options.forceZoom)  payload.force_zoom  = true;
    if (options.toggleZoom) payload.toggle_zoom = true;
    ws.send(JSON.stringify(payload));
  }
}

// A sheet (sign-in, paste, layout conflict) or a rename field owns the
// keyboard while it is up. On iPhone, tapping its field raises the soft
// keyboard, which resizes the pane, and the layout events that follow would
// hand focus back to xterm.
function fieldOwnsKeyboard() {
  if (document.querySelector('.sheet:not(.hidden)')) return true;
  return !!document.activeElement?.classList.contains('item-inline-input');
}

function focusActivePane(opts) {
  const options = opts || {};
  const retries = Number.isInteger(options.retries) ? options.retries : 0;
  const defer = !!options.defer;

  const focusOnce = (remaining) => {
    if (document.visibilityState === 'hidden') return;
    if (fieldOwnsKeyboard()) return;
    if (usesTouchInterface() && Date.now() - _lastTouchScrollAt < MOBILE_TOUCH_SCROLL_IDLE_MS) return;

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
  HistoryOverlay.sync();   // the overlay lives inside the active pane's wrapper
  // With a single visible pane (including tmux zoom), selecting it can change
  // the usable viewport. In split layouts the active pane is only a fraction of
  // the tmux window, so do not send its pane cols/rows as the total size.
  if (isMobileWidth() && !_layoutApplying && _currentLayoutPanes.length <= 1) {
    if (panes[paneId]) requestAnimationFrame(() => fitSinglePane(paneId));
  }
}

// ─── Window list ──────────────────────────────────────────────────────────────

let _windowListLatest = [];

function renderWindowList(windows) {
  _windowListLatest = windows;
  const list = document.getElementById('window-list');
  if (keepInlineEditor(list, `window:${_editingWindowIndex}`)) return;
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
        key: `window:${w.index}`,
        kind: 'window',
        value: w.name,
        label: `window ${w.index}`,
        onSave: (name) => renameWindow(w.index, name),
        onCancel: () => {
          _editingWindowIndex = null;
          renderWindowList(_windowListLatest);
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
          renderWindowList(_windowListLatest);
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
          renderWindowList(_windowListLatest);
        }));
      }
      div.appendChild(buildRenameButton(`Rename window ${w.index}`, () => {
        _confirmDeleteWindow = null;
        _editingWindowIndex = w.index;
        renderWindowList(_windowListLatest);
      }));
      div.addEventListener('click', () => {
        switchWindow(w.index);
        if (isMobileWidth()) setSidebarOpen(false);
        focusActivePane();
      });
    }
    list.appendChild(div);
    });
  applyAgentStatus();
  focusInlineEditor(list);
}

// ─── Agent status ─────────────────────────────────────────────────────────────
// The server reads the screen of every pane that runs Claude Code, Codex or
// Hermes Agent, on the host being viewed and on the other reachable hosts,
// and says which of them are working or waiting for an answer, and which
// finished a turn no one has acknowledged yet (see Unseen there). Each pane,
// window and session row gets an icon for the most pressing state among its
// panes — an icon rather than a dot, so it can't be taken for a host's
// connection dot. Another host's sessions only have session rows here.

const AGENT_STATE_RANK = { idle: 1, thinking: 2, done: 3, approval: 4 };
const AGENT_STATE_LABELS = {
  idle: 'idle', thinking: 'thinking', done: 'finished', approval: 'waiting for you',
};
const AGENT_NAMES = { claude: 'Claude Code', codex: 'Codex', hermes: 'Hermes Agent' };
// A prompt, a turning arc, a check mark, a warning sign.
const AGENT_STATE_ICONS = {
  idle: '<path d="M2.5 4l4 4-4 4" stroke-width="1.9"/><path d="M8.5 12.5h5" stroke-width="1.9"/>',
  thinking: '<path d="M13 8a5 5 0 11-1.46-3.54"/><path d="M13 3v2.5h-2.5"/>',
  done: '<path d="M2.5 8.5l3.5 3.5 7.5-8" stroke-width="2.2"/>',
  approval: '<path d="M8 2.2L14.3 13.3H1.7z"/><path d="M8 6.5v3M8 11.6v.1"/>',
};

// Every host's agents, each entry naming its host. Kept whole across a
// switch of host: the server only sends it again when something changes.
let _agentPanes = [];

function onAgentStatus(msg) {
  if (!Array.isArray(msg.panes)) return;
  _agentPanes = msg.panes.filter((e) =>
    e && typeof e.host === 'string' && Object.hasOwn(AGENT_STATE_RANK, e.state));
  applyAgentStatus();
}

function mostPressing(entries) {
  return entries.reduce((best, e) =>
    (!best || AGENT_STATE_RANK[e.state] > AGENT_STATE_RANK[best.state] ? e : best), null);
}

// Only the icons change, so a rename field that is open keeps its draft.
function applyAgentStatus() {
  const here = _agentPanes.filter((e) => e.host === currentHost);
  applyAgentIcons('pane-list', '.pane-item', (item) =>
    here.filter((e) => e.pane === item.dataset.paneId));
  applyAgentIcons('window-list', '.window-item', (item) =>
    here.filter((e) => e.session === currentSession && String(e.window) === item.dataset.windowIndex));
  applyAgentIcons('session-list', '.session-item', (item) =>
    _agentPanes.filter((e) => e.host === item.dataset.sessionHost && e.session === item.dataset.sessionName));
  // Counted over every host: the reason to look may be on another one.
  applyAgentAlert(_agentPanes);
}

// Agents that want a look — waiting for you, or finished off screen — show
// outside the sidebar too: on the hamburger, which a phone's closed drawer
// leaves as the only thing in sight, and in the tab's title and icon, which
// a desktop sees from another tab.
const AGENT_ALERT_MARKS = { approval: '⚠', done: '✓' };
const AGENT_ALERT_COLORS = { approval: '%23c8952c', done: '%2344aa44' };
const _faviconHref = document.querySelector('link[rel="icon"]')?.getAttribute('href') || '';
let _agentAlertKey = '0/0';   // approval/done counts last shown

function applyAgentAlert(entries) {
  const counts = { approval: 0, done: 0, thinking: 0 };
  entries.forEach((e) => { if (e.state in counts) counts[e.state] += 1; });
  const top = counts.approval ? 'approval' : (counts.done ? 'done' : '');
  const button = document.getElementById('hamburger');
  button.classList.toggle('agent-alert-approval', top === 'approval');
  button.classList.toggle('agent-alert-done', top === 'done');
  // The sidebar's turning arc, in the corner while anything works. Kept, not
  // rebuilt, so the spin runs on smoothly from one update to the next.
  let busy = button.querySelector('.hamburger-busy');
  if (counts.thinking && !busy) {
    busy = document.createElement('span');
    busy.className = 'hamburger-busy agent-thinking';
    busy.setAttribute('aria-hidden', 'true');
    busy.innerHTML =
      `<svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="2"` +
      ` stroke-linecap="round" stroke-linejoin="round">${AGENT_STATE_ICONS.thinking}</svg>`;
    button.appendChild(busy);
  } else if (!counts.thinking) {
    busy?.remove();
  }
  const words = [];
  if (counts.approval) words.push(`${counts.approval} waiting for you`);
  if (counts.done) words.push(`${counts.done} finished`);
  if (counts.thinking) words.push(`${counts.thinking} working`);
  button.setAttribute('aria-label', 'toggle sidebar' + (words.length ? ` (agents: ${words.join(', ')})` : ''));
  button.title = words.length ? `Agents: ${words.join(', ')}` : '';

  const key = `${counts.approval}/${counts.done}`;
  if (key === _agentAlertKey) return;
  _agentAlertKey = key;
  updateTitle();
  const icon = document.querySelector('link[rel="icon"]');
  if (icon && _faviconHref) {
    // A dot in the top-right corner of the usual icon.
    icon.setAttribute('href', !top ? _faviconHref : _faviconHref.replace(/%3C\/svg%3E$/,
      `%3Ccircle cx='23' cy='9' r='8.5' fill='${AGENT_ALERT_COLORS[top]}'` +
      ` stroke='%231e1e1e' stroke-width='2'/%3E%3C/svg%3E`));
  }
}

function agentAlertPrefix() {
  const [approval, done] = _agentAlertKey.split('/').map(Number);
  const parts = [];
  if (approval) parts.push(`${AGENT_ALERT_MARKS.approval}${approval}`);
  if (done) parts.push(`${AGENT_ALERT_MARKS.done}${done}`);
  return parts.length ? parts.join(' ') + ' ' : '';
}

// Rows without an agent keep an empty slot while any row of the list has an
// icon, so the names stay in one column; a list with no agent has no slots.
function applyAgentIcons(listId, selector, entriesFor) {
  const list = document.getElementById(listId);
  let any = false;
  list.querySelectorAll(selector).forEach((item) => {
    const entry = mostPressing(entriesFor(item));
    if (entry) any = true;
    setAgentIcon(item, entry);
  });
  list.classList.toggle('has-agents', any);
}

function setAgentIcon(item, entry) {
  let icon = item.querySelector(':scope > .agent-icon');
  if (item.classList.contains('editing') || item.classList.contains('confirming')) {
    icon?.remove();
    return;
  }
  if (!icon) {
    icon = document.createElement('span');
    item.prepend(icon);
  }
  if (!entry) {
    icon.className = 'agent-icon agent-none';
    icon.dataset.state = '';
    icon.innerHTML = '';
    icon.removeAttribute('role');
    icon.removeAttribute('title');
    icon.removeAttribute('aria-label');
    icon.setAttribute('aria-hidden', 'true');
    return;
  }
  icon.setAttribute('role', 'img');
  icon.removeAttribute('aria-hidden');
  // Redrawn only on a change of state: a fresh SVG would restart the spin.
  if (icon.dataset.state !== entry.state) {
    icon.dataset.state = entry.state;
    icon.className = `agent-icon agent-${entry.state}`;
    icon.innerHTML =
      `<svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.6"` +
      ` stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">` +
      `${AGENT_STATE_ICONS[entry.state]}</svg>`;
  }
  const label = `${AGENT_NAMES[entry.agent] || entry.agent}: ${AGENT_STATE_LABELS[entry.state]}`;
  icon.title = label;
  icon.setAttribute('aria-label', label);
}

// ─── Hosts & sessions ─────────────────────────────────────────────────────────
// The server's one control client is on one host at a time (`currentHost`);
// the list shows every configured host's sessions, and picking a session on
// another host moves the control client — and every open browser — there.

let _sessionNote = '';
let _sessionNoteTimer = null;

// Hosts whose session list is folded away, remembered per browser.
const COLLAPSED_HOSTS_STORAGE_KEY = 'web-tmux-collapsed-hosts';
const _collapsedHosts = loadCollapsedHosts();

function loadCollapsedHosts() {
  try {
    const saved = JSON.parse(localStorage.getItem(COLLAPSED_HOSTS_STORAGE_KEY));
    return new Set(Array.isArray(saved) ? saved.filter((h) => typeof h === 'string') : []);
  } catch {
    return new Set();
  }
}

function toggleHostCollapsed(host) {
  if (!_collapsedHosts.delete(host)) _collapsedHosts.add(host);
  try {
    localStorage.setItem(COLLAPSED_HOSTS_STORAGE_KEY, JSON.stringify([..._collapsedHosts]));
  } catch { /* private mode: still folds, just not remembered */ }
  renderSessionList();
}

function sessionKey(host, name) {
  return host + '\0' + name;
}

// A view message carries the active host's sessions, plus `hosts` from a
// server that knows about hosts. An older one sends only `sessions`.
function updateSessions(msg) {
  if (msg.host) currentHost = msg.host;
  if (Number.isInteger(msg.gen)) currentGen = msg.gen;
  if (Array.isArray(msg.hosts) && msg.hosts.length) {
    hostsModel = msg.hosts;
  } else if (msg.sessions) {
    const others = hostsModel.filter((h) => h.host !== currentHost);
    hostsModel = [{ host: currentHost, active: true, reachable: true, code: '', sessions: msg.sessions },
                  ...others];
  }
  updateHostLabel();
  renderSessionList();
}

function onHosts(msg) {
  if (!Array.isArray(msg.hosts)) return;
  hostsModel = msg.hosts;
  renderSessionList();
}

const HOST_ERRORS = {
  unreachable:      'is unreachable',
  auth_required:    'needs a password or key passphrase',
  auth_failed:      'did not accept the password or passphrase',
  auth_cancelled:   'sign-in cancelled',
  unsupported_tmux: 'has no tmux, or one older than 3.2',
  session_gone:     'no longer has that session',
  switch_failed:    'could not be attached',
  fell_back:        'was lost — back on this machine',
  busy:             'is busy; try again in a moment',
  error:            'answered with an error',
  disconnected:     'is disconnected',
};

function onHostError(msg) {
  showSessionNote(`${msg.host} ${HOST_ERRORS[msg.code] || 'failed'}`);
}

function showSessionNote(text) {
  _sessionNote = text;
  clearTimeout(_sessionNoteTimer);
  _sessionNoteTimer = setTimeout(() => { _sessionNote = ''; renderSessionList(); }, 6000);
  renderSessionList();
}

// The windows and panes block belongs to one host's one session; say which,
// since the session list above it can be long.
function updateCurrentBlock() {
  const host = !currentHost || currentHost === 'local' ? _host : currentHost;
  document.getElementById('current-host').textContent = host;
  document.getElementById('current-session').textContent = currentSession;
}

function updateHostLabel() {
  const remote = currentHost && currentHost !== 'local';
  const label = document.getElementById('topbar-host');
  label.textContent = remote ? `${_host} → ${currentHost}` : _host;
  label.classList.toggle('remote', !!remote);
  updateCurrentBlock();
  updateTitle();
}

function updateTitle() {
  const remote = currentHost && currentHost !== 'local';
  document.title = agentAlertPrefix() + (remote ? currentHost : _host) + ' - web-tmux';
}

function renderSessionList() {
  const list = document.getElementById('session-list');
  if (keepInlineEditor(list, `session:${_editingSessionName}`)) return;
  list.innerHTML = '';
  if (_sessionNote) {
    const note = document.createElement('div');
    note.className = 'session-note';
    note.textContent = _sessionNote;
    list.appendChild(note);
  }
  // With no ssh hosts configured the list looks exactly as it always has.
  const grouped = hostsModel.length > 1 || hostsModel.some((h) => h.host !== 'local');
  // Each host header has its own "+", so the section-wide one would only
  // repeat the current host's.
  document.getElementById('btn-new-session').hidden = grouped;
  list.classList.toggle('grouped', grouped);
  hostsModel.forEach((h) => {
    if (grouped) list.appendChild(buildHostHeader(h));
    if (h.reachable === false || (grouped && _collapsedHosts.has(h.host))) return;
    [...(h.sessions || [])]
      .sort((a, b) => String(a.name || '').localeCompare(String(b.name || ''), 'ja'))
      .forEach((s) => list.appendChild(buildSessionItem(h.host, s)));
  });
  applyAgentStatus();
  focusInlineEditor(list);
}

function buildHostHeader(h) {
  const div = document.createElement('div');
  const state = h.code === 'disconnected' ? 'off'
    : h.reachable === false ? 'down' : (h.reachable ? 'up' : 'unknown');
  div.className = `host-head host-${state}` + (h.host === currentHost ? ' current' : '');
  div.dataset.host = h.host;
  const label = h.host === 'local' ? _host : h.host;
  const collapsed = _collapsedHosts.has(h.host);
  const count = (h.sessions || []).length;
  div.innerHTML =
    `<svg class="host-chevron" viewBox="0 0 8 8" aria-hidden="true">` +
    `<path d="M2.5 1.5L5.5 4L2.5 6.5" fill="none" stroke="currentColor" stroke-width="1.2"` +
    ` stroke-linecap="round" stroke-linejoin="round"/></svg>` +
    `<span class="host-name">${escHtml(label)}</span>` +
    `<span class="host-dot" aria-hidden="true"></span>` +
    (collapsed && count ? `<span class="host-count">${count}</span>` : '');
  div.title = h.reachable === false ? `${label} ${HOST_ERRORS[h.code] || 'is unavailable'}` : label;
  if (h.reachable !== false) {
    // The whole row folds the host's sessions; its own button keeps its job.
    div.classList.toggle('collapsed', collapsed);
    div.setAttribute('role', 'button');
    div.tabIndex = 0;
    div.setAttribute('aria-expanded', String(!collapsed));
    div.addEventListener('click', (ev) => {
      if (!ev.target.closest('.side-btn')) toggleHostCollapsed(h.host);
    });
    div.addEventListener('keydown', (ev) => {
      if (ev.target !== div || (ev.key !== 'Enter' && ev.key !== ' ')) return;
      ev.preventDefault();
      toggleHostCollapsed(h.host);
      // The list was rebuilt, so hand focus to the new copy of this header.
      document.querySelector(`#session-list .host-head[data-host="${CSS.escape(h.host)}"]`)?.focus();
    });
  }
  if (h.code === 'disconnected') {
    div.appendChild(buildLinkButton(`Connect to ${label}`, true,
      () => connectHost(h.host, `Connecting to ${label}…`)));
  } else if (h.reachable === false && h.code === 'auth_required') {
    const key = document.createElement('button');
    key.type = 'button';
    key.className = 'side-btn';
    key.textContent = '🔑';
    key.title = `Sign in to ${label}`;
    key.addEventListener('click', () => connectHost(h.host));
    div.appendChild(key);
  } else if (h.reachable === false) {
    const retry = document.createElement('button');
    retry.type = 'button';
    retry.className = 'side-btn';
    retry.textContent = '↻';
    retry.title = `Retry ${label}`;
    retry.addEventListener('click', () => wsSendType('refresh_hosts'));
    div.appendChild(retry);
  } else {
    const add = document.createElement('button');
    add.type = 'button';
    add.className = 'side-btn';
    add.textContent = '+';
    add.title = `New session on ${label}`;
    add.addEventListener('click', () => {
      newSession(h.host);
      focusActivePane();
    });
    if (h.host !== 'local') {
      div.appendChild(buildLinkButton(`Disconnect from ${label}`, false,
        () => disconnectHost(h.host)));
    }
    div.appendChild(add);
  }
  return div;
}

// The icon shows what pressing it does: a whole link connects, a broken
// one disconnects.
function buildLinkButton(label, connect, onClick) {
  const button = document.createElement('button');
  button.type = 'button';
  button.className = 'side-btn host-link';
  button.setAttribute('aria-label', label);
  button.title = label;
  button.innerHTML =
    `<svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.6"` +
    ` stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">` +
    `<path d="M8.5 4.5l1-1a2.83 2.83 0 014 4l-1 1"/>` +
    `<path d="M7.5 11.5l-1 1a2.83 2.83 0 01-4-4l1-1"/>` +
    (connect
      ? `<path d="M6 10l4-4"/>`
      : `<path d="M5.5 1.5v2M1.5 5.5h2M10.5 14.5v-2M14.5 10.5h-2"/>`) +
    `</svg>`;
  button.addEventListener('click', onClick);
  return button;
}

function buildSessionItem(host, s) {
  const key = sessionKey(host, s.name);
  const isActive = host === currentHost && s.name === currentSession;
  const div = document.createElement('div');
  div.className = 'session-item' + (isActive ? ' active' : '');
  div.dataset.sessionName = s.name;
  div.dataset.sessionHost = host;
  if (_editingSessionName === key) {
    div.classList.add('editing');
    div.appendChild(buildInlineEditor({
      key: `session:${key}`,
      kind: 'session',
      value: s.name,
      label: s.name,
      onSave: (name) => renameSession(host, s.name, name),
      onCancel: () => {
        _editingSessionName = '';
        renderSessionList();
      },
    }));
  } else if (_confirmDeleteSession === key) {
    div.classList.add('confirming');
    div.appendChild(buildInlineConfirm({
      label: `Kill session "${s.name}"?`,
      onConfirm: () => {
        _confirmDeleteSession = '';
        killSession(host, s.name);
      },
      onCancel: () => {
        _confirmDeleteSession = '';
        renderSessionList();
      },
    }));
  } else {
    div.tabIndex = 0;
    div.innerHTML = `<span class="session-name">${escHtml(`(${s.windows}) ${s.name}`)}</span>`;
    div.title = s.attached ? 'attached' : 'detached';
    div.appendChild(buildDeleteButton(`Kill session ${s.name}`, () => {
      _editingSessionName = '';
      _confirmDeleteSession = key;
      renderSessionList();
    }));
    div.appendChild(buildRenameButton(`Rename session ${s.name}`, () => {
      _confirmDeleteSession = '';
      _editingSessionName = key;
      renderSessionList();
    }));
    div.addEventListener('click', () => {
      selectSession(host, s.name);
      if (isMobileWidth()) setSidebarOpen(false);
      focusActivePane();
    });
  }
  return div;
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
    div.appendChild(buildFilesButton(p.id));
    div.addEventListener('click', () => {
      const isCurrentPane = p.id === activePaneId;
      selectPane(p.id, isCurrentPane ? { toggleZoom: true } : { forceZoom: true });
      if (isMobileWidth()) setSidebarOpen(false);
      focusActivePane();
    });
    list.appendChild(div);
  });
  applyAgentStatus();
}

function buildFilesButton(paneId) {
  const button = document.createElement('button');
  button.type = 'button';
  button.className = 'item-icon-btn';
  button.setAttribute('aria-label', `Files in ${paneId}'s folder`);
  button.title = 'Files in this pane\'s folder';
  button.innerHTML =
    `<svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.4"` +
    ` stroke-linejoin="round" aria-hidden="true">` +
    `<path d="M2 4.5a1 1 0 011-1h3l1.5 1.5H13a1 1 0 011 1v6a1 1 0 01-1 1H3a1 1 0 01-1-1z"/>` +
    `</svg>`;
  button.addEventListener('click', (ev) => {
    ev.preventDefault();
    ev.stopPropagation();
    if (isMobileWidth()) setSidebarOpen(false);
    openFileViewer(paneId);
  });
  return button;
}

function switchWindow(idx) {
  markClientActive();
  if (ws && ws.readyState === WebSocket.OPEN) {
    ws.send(JSON.stringify({ type: 'select_window', window: idx, host: currentHost }));
  }
}

function renameWindow(idx, name) {
  const nextName = (name || '').trim();
  if (!nextName || !ws || ws.readyState !== WebSocket.OPEN) return;
  _editingWindowIndex = null;
  markClientActive();
  ws.send(JSON.stringify({ type: 'rename_window', window: idx, name: nextName, host: currentHost }));
}

function selectSession(host, name) {
  markClientActive();
  if (ws && ws.readyState === WebSocket.OPEN) {
    if (host !== currentHost) showSessionNote(`Connecting to ${host}…`);
    ws.send(JSON.stringify({ type: 'select_session', session: name, host }));
  }
}

function connectHost(host, note = `Signing in to ${host}…`) {
  markClientActive();
  showSessionNote(note);
  wsSendType('connect_host', { host });
}

function disconnectHost(host) {
  markClientActive();
  showSessionNote(`Disconnecting from ${host}…`);
  wsSendType('disconnect_host', { host });
}

// ─── Host sign-in ─────────────────────────────────────────────────────────────
// ssh's own prompts ("Enter passphrase for key …", "me@box's password:"),
// one at a time, only on the browser that asked to connect. The answer goes
// straight back over the WebSocket and is not kept here either.

let _authPrompts = [];   // [{ id, host, prompt }], the first one is on screen

function onHostAuthPrompt(msg) {
  if (typeof msg.id !== 'string') return;
  _authPrompts.push({ id: msg.id, host: String(msg.host || ''), prompt: String(msg.prompt || '') });
  if (_authPrompts.length === 1) showHostAuthPrompt();
}

function onHostAuthClosed(msg) {
  const wasShown = _authPrompts.length && _authPrompts[0].id === msg.id;
  _authPrompts = _authPrompts.filter((p) => p.id !== msg.id);
  if (wasShown) showHostAuthPrompt();
}

function showHostAuthPrompt() {
  const sheet = document.getElementById('host-auth-sheet');
  const input = document.getElementById('host-auth-input');
  input.value = '';
  const current = _authPrompts[0];
  if (!current) {
    sheet.classList.add('hidden');
    sheet.setAttribute('aria-hidden', 'true');
    return;
  }
  document.getElementById('host-auth-title').textContent = `Sign in to ${current.host}`;
  document.getElementById('host-auth-text').textContent = current.prompt || 'Password:';
  sheet.classList.remove('hidden');
  sheet.setAttribute('aria-hidden', 'false');
  setTimeout(() => input.focus(), 0);
}

function answerHostAuth(cancel) {
  const current = _authPrompts.shift();
  const input = document.getElementById('host-auth-input');
  const answer = input.value;
  input.value = '';
  if (current) {
    wsSend('host_auth_answer', cancel ? { id: current.id, cancel: true }
                                      : { id: current.id, answer });
  }
  showHostAuthPrompt();
  if (!_authPrompts.length) focusActivePane();
}

function dropHostAuthPrompts() {
  _authPrompts = [];
  showHostAuthPrompt();
}

function newSession(host) {
  markClientActive();
  if (host !== currentHost) showSessionNote(`Connecting to ${host}…`);
  wsSendType('new_session', { host });
}

function renameSession(host, currentName, nextName) {
  const trimmed = (nextName || '').trim();
  if (!trimmed || !ws || ws.readyState !== WebSocket.OPEN) return;
  _editingSessionName = '';
  markClientActive();
  ws.send(JSON.stringify({ type: 'rename_session', session: currentName, name: trimmed, host }));
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

function killSession(host, name) {
  ws.send(JSON.stringify({ type: 'kill_session', session: name, host }));
}

function killWindow(idx) {
  ws.send(JSON.stringify({ type: 'kill_window', window: idx, host: currentHost }));
}

function buildInlineConfirm({ label, confirmText = 'delete', onConfirm, onCancel }) {
  const div = document.createElement('div');
  div.className = 'item-inline-editor';

  const text = document.createElement('span');
  text.className = 'item-confirm-label';
  text.textContent = label;

  const yes = document.createElement('button');
  yes.type = 'button';
  yes.className = 'item-inline-btn danger';
  yes.textContent = confirmText;

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

// While a rename is open, a list refresh would rebuild the field: the draft is
// lost, and on iPhone the new field cannot take focus without a tap, so the
// keyboard drops. Leave the list as it is until the rename ends.
function keepInlineEditor(list, key) {
  return list.querySelector('.item-inline-editor')?.dataset.key === key;
}

// Called while the rename button's tap is still being handled, so iOS raises
// the keyboard; a focus from a later frame would only place the caret.
function focusInlineEditor(list) {
  const input = list.querySelector('.item-inline-input');
  if (!input) return;
  input.focus();
  input.select();
}

function buildInlineEditor({ key, kind, value, label, onSave, onCancel }) {
  const editor = document.createElement('div');
  editor.className = 'item-inline-editor';
  editor.dataset.key = key;

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
  [editor, save, cancel].forEach((el) => {
    el.addEventListener('mousedown', stop);
    el.addEventListener('click', (ev) => ev.stopPropagation());
  });
  // Not cancelled here: mousedown is what moves focus (and the caret) into
  // the field, and a tap on iPhone gets no keyboard without it.
  input.addEventListener('mousedown', (ev) => ev.stopPropagation());
  input.addEventListener('click', (ev) => ev.stopPropagation());

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
  return editor;
}

// ─── Saved layouts ────────────────────────────────────────────────────────────
// A slot holds the shape of every session that was open when it was saved —
// window names, splits, each pane's cwd. Restoring rebuilds each of those
// sessions under its original name; nothing that was running in them comes
// back.

function layoutMeta(slot) {
  const when = slot.saved_at ? slot.saved_at.slice(5, 16).replace('T', ' ') : '';
  const sessions = `${slot.sessions} session${slot.sessions === 1 ? '' : 's'}`;
  const remotes = (slot.hosts || []).filter((h) => h !== 'local');
  const hosts = remotes.length ? ` · +${remotes.join(', ')}` : '';
  return `${sessions}${hosts} · ${slot.windows}w/${slot.panes}p${when ? ' · ' + when : ''}`;
}

function renderLayoutList() {
  const list = document.getElementById('layout-list');
  list.innerHTML = '';

  if (_savingLayout) {
    const div = document.createElement('div');
    div.className = 'layout-item editing';
    div.appendChild(buildInlineEditor({
      kind: 'layout',
      value: '',
      label: 'slot',
      onSave: (name) => {
        _savingLayout = false;
        saveLayout(name);
      },
      onCancel: () => {
        _savingLayout = false;
        renderLayoutList();
      },
    }));
    list.appendChild(div);
  }

  _layouts.forEach((slot) => {
    const div = document.createElement('div');
    div.className = 'layout-item';
    div.dataset.layoutName = slot.name;

    if (_confirmDeleteLayout === slot.name) {
      div.classList.add('confirming');
      div.appendChild(buildInlineConfirm({
        label: `Delete layout "${slot.name}"?`,
        onConfirm: () => {
          _confirmDeleteLayout = '';
          deleteLayout(slot.name);
        },
        onCancel: () => {
          _confirmDeleteLayout = '';
          renderLayoutList();
        },
      }));
    } else if (_overwriteLayout === slot.name) {
      div.classList.add('confirming');
      div.appendChild(buildInlineConfirm({
        label: `Overwrite "${slot.name}"?`,
        confirmText: 'overwrite',
        onConfirm: () => {
          _overwriteLayout = '';
          saveLayout(slot.name, true);
        },
        onCancel: () => {
          _overwriteLayout = '';
          renderLayoutList();
        },
      }));
    } else {
      div.tabIndex = 0;
      div.title = `Restore ${slot.sessions} session${slot.sessions === 1 ? '' : 's'}`;
      div.innerHTML =
        `<span class="layout-name">${escHtml(slot.name)}</span>` +
        `<span class="layout-meta">${escHtml(layoutMeta(slot))}</span>`;
      div.appendChild(buildDeleteButton(`Delete layout ${slot.name}`, () => {
        _overwriteLayout = '';
        _confirmDeleteLayout = slot.name;
        renderLayoutList();
      }));
      div.addEventListener('click', () => {
        restoreLayout(slot.name);
        if (isMobileWidth()) setSidebarOpen(false);
      });
    }
    list.appendChild(div);
  });

  if (!_layouts.length && !_savingLayout) {
    const hint = document.createElement('div');
    hint.id = 'layout-empty';
    hint.textContent = 'none saved yet';
    list.appendChild(hint);
  }
}

function saveLayout(name, overwrite) {
  name = (name || '').trim();
  if (!name) {
    renderLayoutList();
    return;
  }
  wsSendType('save_layout', overwrite ? { name, overwrite: true } : { name });
}

function restoreLayout(name, mode) {
  wsSendType('restore_layout', mode ? { name, mode } : { name });
}

function deleteLayout(name) {
  wsSendType('delete_layout', { name });
}

function onLayouts(msg) {
  _layouts = msg.layouts || [];
  renderLayoutList();
}

function onLayoutConflict(msg) {
  if (msg.scope === 'slot') {
    _confirmDeleteLayout = '';
    _overwriteLayout = msg.name;
    renderLayoutList();
    return;
  }
  _conflictLayout = msg.name;
  const names = (msg.sessions || []).map((s) => `"${s}"`).join(', ');
  const plural = (msg.sessions || []).length === 1 ? 'session is' : 'sessions are';
  document.getElementById('layout-conflict-text').textContent =
    `${names} ${plural} already running. Replace them, restore them under new names, or skip them and restore only the remaining sessions?`;
  setLayoutConflictOpen(true);
}

function restoredLabel(s) {
  return s.host && s.host !== 'local' ? `${s.host}:${s.session}` : s.session;
}

const RESTORE_REASONS = {
  unreachable:         'unreachable',
  auth_required:       'not signed in',
  unsupported_tmux:    'no usable tmux',
  host_not_configured: 'host not configured',
};

function onRestoreResult(msg) {
  const sessions = msg.sessions || [];
  const restored = sessions.filter((s) => s.status === 'restored');
  const skipped = sessions.filter((s) => s.status === 'skipped');
  const failed = sessions.filter((s) => s.status === 'failed');
  if (!skipped.length && !failed.length) return;   // full success stays quiet
  const details = [];
  if (skipped.length) details.push(`skipped: ${skipped.map(restoredLabel).join(', ')}`);
  if (failed.length) {
    details.push(`failed: ${failed.map((s) =>
      restoredLabel(s) + (RESTORE_REASONS[s.reason] ? ` (${RESTORE_REASONS[s.reason]})` : '')
    ).join(', ')}`);
  }
  showLayoutNote(`Restored ${restored.length}/${sessions.length} — ${details.join(' — ')}`);
}

function onSaveResult(msg) {
  const skipped = msg.skipped_hosts || [];
  if (skipped.length) showLayoutNote(`Saved without ${skipped.join(', ')} (unreachable)`);
}

function onLayoutError(msg) {
  const messages = {
    busy:           'Another layout operation is still running.',
    capture_failed: 'Could not read one of the open sessions.',
    too_large:      'Too many sessions, windows, or panes to save.',
    restore_failed: 'Restore failed; nothing was changed.',
    not_found:      'That layout is gone.',
    store_full:     'No free layout slots left — delete one first.',
    save_failed:    'Could not write the layout file.',
  };
  showLayoutNote(messages[msg.code] || 'Layout operation failed.');
}

// ─── Clipboard ────────────────────────────────────────────────────────────────

// What a pane application copies (Claude Code's mouse selection, tmux
// copy-mode, OSC 52) lands in a tmux paste buffer, and the server forwards each
// new buffer here. Every connected browser hears it; only the one the user is
// in front of writes it, so a phone left open does not fight the PC.
let _clipboardOfferTimer = null;

function onClipboard(msg) {
  if (typeof msg.text !== 'string' || !msg.text) return;
  if (!document.hasFocus()) return;
  writeClipboard(msg.text).then(hideClipboardOffer, () => showClipboardOffer(msg.text));
}

function writeClipboard(text) {
  if (!navigator.clipboard || !navigator.clipboard.writeText) {
    return Promise.reject(new Error('clipboard API unavailable'));
  }
  return navigator.clipboard.writeText(text);
}

// Safari and Firefox only let a page write the clipboard close to a user
// gesture, and the copy arrives after a server round trip. When the write is
// refused, offer a button whose click is that gesture.
function showClipboardOffer(text) {
  const btn = document.getElementById('clipboard-offer');
  btn.textContent = `Copy ${text.length} characters`;
  btn.onclick = () => {
    writeClipboard(text).catch((e) => console.warn('clipboard write failed', e));
    hideClipboardOffer();
    focusActivePane();
  };
  btn.hidden = false;
  clearTimeout(_clipboardOfferTimer);
  _clipboardOfferTimer = setTimeout(hideClipboardOffer, 10000);
}

function hideClipboardOffer() {
  clearTimeout(_clipboardOfferTimer);
  _clipboardOfferTimer = null;
  const btn = document.getElementById('clipboard-offer');
  btn.hidden = true;
  btn.onclick = null;
}

function showLayoutNote(text) {
  const list = document.getElementById('layout-list');
  const note = document.createElement('div');
  note.id = 'layout-empty';
  note.textContent = text;
  list.prepend(note);
  setTimeout(() => note.remove(), 6000);
}

function setLayoutConflictOpen(open) {
  const sheet = document.getElementById('layout-conflict-sheet');
  sheet.classList.toggle('hidden', !open);
  sheet.setAttribute('aria-hidden', open ? 'false' : 'true');
}

function closeLayoutConflict() {
  _conflictLayout = '';
  setLayoutConflictOpen(false);
  focusActivePane();
}

function resolveLayoutConflict(mode) {
  const name = _conflictLayout;
  setLayoutConflictOpen(false);
  _conflictLayout = '';
  if (name) restoreLayout(name, mode);
}

function moveSidebarFocus(list, delta) {
  const items = [...list.querySelectorAll('.session-item, .window-item, .pane-item, .layout-item')];
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
    selectSession(item.dataset.sessionHost || currentHost, item.dataset.sessionName || '');
  } else if (item.classList.contains('window-item')) {
    switchWindow(Number(item.dataset.windowIndex));
  } else if (item.classList.contains('pane-item')) {
    const paneId = item.dataset.paneId || '';
    const isCurrentPane = paneId === activePaneId;
    selectPane(paneId, isCurrentPane ? { toggleZoom: true } : { forceZoom: true });
  } else if (item.classList.contains('layout-item')) {
    restoreLayout(item.dataset.layoutName || '');
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
document.getElementById('layout-list').addEventListener('keydown', handleSidebarListKeydown);

// ─── Sidebar action buttons ───────────────────────────────────────────────────

function wsSendType(type, extra) {
  if (!ws || ws.readyState !== WebSocket.OPEN) return false;
  markClientActive();
  return wsSend(type, extra);
}

// Plain send, for messages that are not the user acting on this tab: a probe
// or a diagnostic must not take resize mastership away from another device.
function wsSend(type, extra) {
  if (!ws || ws.readyState !== WebSocket.OPEN) return false;
  try { ws.send(JSON.stringify({ type, ...(extra || {}) })); return true; }
  catch (_) { return false; }
}

document.getElementById('btn-new-window').addEventListener('click', () => {
  wsSendType('new_window', { host: currentHost });
  focusActivePane();
});
document.getElementById('btn-new-session').addEventListener('click', () => {
  newSession(currentHost);
  focusActivePane();
});
document.getElementById('btn-save-layout').addEventListener('click', () => {
  _confirmDeleteLayout = '';
  _overwriteLayout = '';
  _savingLayout = true;
  renderLayoutList();
});

document.getElementById('layout-conflict-close').addEventListener('click', closeLayoutConflict);
document.getElementById('layout-conflict-cancel').addEventListener('click', closeLayoutConflict);
document.querySelector('#layout-conflict-sheet .sheet-backdrop')
  .addEventListener('click', closeLayoutConflict);
document.getElementById('layout-conflict-replace').addEventListener('click', () => {
  resolveLayoutConflict('replace');
});
document.getElementById('layout-conflict-duplicate').addEventListener('click', () => {
  resolveLayoutConflict('duplicate');
});
document.getElementById('layout-conflict-skip').addEventListener('click', () => {
  resolveLayoutConflict('skip');
});
document.addEventListener('keydown', (ev) => {
  if (ev.key !== 'Escape' || !_conflictLayout) return;
  ev.preventDefault();
  closeLayoutConflict();
}, true);   // capture, so xterm doesn't swallow it first
document.getElementById('host-auth-form').addEventListener('submit', (ev) => {
  ev.preventDefault();
  answerHostAuth(false);
});
document.getElementById('host-auth-cancel').addEventListener('click', () => answerHostAuth(true));
document.getElementById('host-auth-close').addEventListener('click', () => answerHostAuth(true));
document.querySelector('#host-auth-sheet .sheet-backdrop')
  .addEventListener('click', () => answerHostAuth(true));
document.addEventListener('keydown', (ev) => {
  if (ev.key !== 'Escape' || !_authPrompts.length) return;
  ev.preventDefault();
  answerHostAuth(true);
}, true);
document.getElementById('btn-split-h').addEventListener('click', () => {
  wsSendType('split_window', { direction: 'h', pane: activePaneId || '', gen: currentGen });
  focusActivePane();
});
document.getElementById('btn-split-v').addEventListener('click', () => {
  wsSendType('split_window', { direction: 'v', pane: activePaneId || '', gen: currentGen });
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

// Redraw the visible screen in place, leaving the scrollback untouched.
//
// The old frame cleared everything (\x1b[2J\x1b[3J) and replayed 2000 lines of
// tmux history on every refresh — and get_snapshot fires on pane focus, layout
// changes, alt-screen exit and resize, so that was a multi-hundred-millisecond
// reparse at unpredictable moments. The snapshot now carries only the pane's
// visible rows: each row is drawn at a known position and terminated with
// \x1b[K, which erases the rest of that line without scrolling anything.
//
// The pane's terminal modes come from tmux with the capture and are put back
// after the rows are drawn. A soft reset (\x1b[!p) used to stand in for that,
// but it dropped the scroll region the application still relies on — every
// later scroll then moved the wrong rows — and also bracketed paste and focus
// reporting, which tmux cannot report back; those are now left untouched.
function buildSnapshotFrame(msg, term) {
  const paneRows = Math.max(1, msg.pane_rows || term.rows || 1);
  const paneCols = Math.max(1, msg.pane_cols || term.cols || 1);
  const cursorRow = clamp((msg.cursor_y || 0) + 1, 1, paneRows);
  const cursorCol = clamp((msg.cursor_x || 0) + 1, 1, paneCols);
  const flag = (name, fallback) => (msg[name] === undefined ? fallback : !!msg[name]);
  const regionTop = clamp(msg.scroll_region_upper || 0, 0, paneRows - 1);
  const regionBottom = msg.scroll_region_lower === undefined || msg.scroll_region_lower < 0
    ? paneRows - 1
    : clamp(msg.scroll_region_lower, regionTop, paneRows - 1);
  const fullRegion = regionTop === 0 && regionBottom === paneRows - 1;
  const origin = flag('origin_flag', false) && !fullRegion;

  // The redraw always lands on the normal screen, even for a pane tmux has on
  // its alternate screen (vim, htop, Claude Code): the alternate buffer has no
  // scrollback, so HistoryOverlay would refuse to open and touch scrolling
  // would stop working in that pane.
  //
  // Draw with every mode that moves or clips the cursor at its default:
  // full scroll region, absolute addressing, autowrap, replace mode, plain
  // attributes and the ASCII charset. MOUSE_DISABLE_SEQS before and after the
  // rows neutralizes any mouse-enable sequence inside the captured content;
  // the modes tmux reports are switched back on at the end.
  const parts = [asciiBytes(
    '\x1b[?25l\x1b[?1049l' +
    '\x1b[r\x1b[?6l\x1b[?7h\x1b[4l\x1b[0m\x1b(B\x0f' +
    MOUSE_DISABLE_SEQS
  )];

  const rows = splitCaptureRows(b64ToUint8(msg.data || ''));
  for (let i = 0; i < paneRows; i++) {
    // Clear the row first: tmux 3.6 captures TABs as TABs, and a TAB only
    // moves the cursor, so whatever xterm had under its gap would stay.
    parts.push(asciiBytes(`\x1b[${i + 1};1H\x1b[2K`));
    if (i < rows.length) parts.push(rows[i]);
    parts.push(asciiBytes('\x1b[K'));   // clear to end of line, no scroll
  }

  // capture-pane -e leaves the last row's attributes active.
  let tail = '\x1b[0m' + MOUSE_DISABLE_SEQS;
  // Mouse tracking the application really asked for (Claude Code, htop, vim
  // with mouse=a) comes back from tmux's state, not from the captured bytes:
  // wheel and swipe reach the application only while it is on.
  if (flag('mouse_all_flag', false)) tail += '\x1b[?1003h';
  else if (flag('mouse_button_flag', false)) tail += '\x1b[?1002h';
  else if (flag('mouse_standard_flag', false)) tail += '\x1b[?1000h';
  if (flag('mouse_sgr_flag', false)) tail += '\x1b[?1006h';
  if (!fullRegion) tail += `\x1b[${regionTop + 1};${regionBottom + 1}r`;
  if (!flag('wrap_flag', true)) tail += '\x1b[?7l';
  if (flag('insert_flag', false)) tail += '\x1b[4h';
  tail += flag('keypad_cursor_flag', false) ? '\x1b[?1h' : '\x1b[?1l';
  tail += flag('keypad_flag', false) ? '\x1b=' : '\x1b>';
  // With origin mode on, cursor addressing is relative to the scroll region.
  if (origin) tail += `\x1b[?6h\x1b[${clamp(cursorRow - regionTop, 1, regionBottom - regionTop + 1)};${cursorCol}H`;
  else tail += `\x1b[${cursorRow};${cursorCol}H`;
  if (flag('cursor_flag', true)) tail += '\x1b[?25h';
  parts.push(asciiBytes(tail));
  return concatBytes(parts);
}

// capture-pane separates rows with a bare LF and does not pad short rows.
function splitCaptureRows(bytes) {
  const rows = [];
  let start = 0;
  for (let i = 0; i < bytes.length; i++) {
    if (bytes[i] === 0x0A) {
      rows.push(bytes.subarray(start, i));
      start = i + 1;
    }
  }
  if (start < bytes.length) rows.push(bytes.subarray(start));
  return rows;
}

function escHtml(s) {
  return s.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
}

function setStatus(state) {
  const el = document.getElementById('status');
  el.className = state;
  el.title     = state === 'connected' ? state : `${state} — tap to reconnect`;
  el.setAttribute('aria-label', el.title);
}

document.getElementById('status').addEventListener('click', () => {
  if (ws && ws.readyState === WebSocket.OPEN) return;
  reconnectNow();
});

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

window.addEventListener('focus', () => {
  activateClient();
  noteResume('focus');
  ensureConnected(true);
});
document.addEventListener('pointerdown', markClientActive, { passive: true });
document.addEventListener('visibilitychange', () => {
  if (document.visibilityState === 'hidden') {
    _clientActive = false;
  } else {
    // The socket may have died while the page was away without the close
    // event ever being delivered, so check it before trusting it.
    noteResume('visible');
    ensureConnected(true);
    if (document.hasFocus()) activateClient();
  }
});
// A phone waking up, a tab restored from the back/forward cache, and the
// network coming back are all resumes the visibility change alone can miss.
window.addEventListener('pageshow', (ev) => {
  noteResume(ev.persisted ? 'pageshow-bfcache' : 'pageshow');
  ensureConnected(true);
});
window.addEventListener('online', () => {
  noteResume('online');
  ensureConnected(true);
});
// Leaving for the back/forward cache with a socket still open makes the page
// less likely to be kept; pageshow above brings the connection back.
window.addEventListener('pagehide', () => {
  const socket = ws;
  if (socket) {
    ws = null;
    socket.onclose = null;
    try { socket.close(); } catch (_) {}
  }
});

// Initial state: open on desktop, closed on mobile
setSidebarOpen(!isMobileWidth());

// Re-evaluate sidebar visibility and pane layout on width crossing the breakpoint
let _prevMobile = isMobileWidth();
window.addEventListener('resize', () => {
  const nowMobile = isMobileWidth();
  if (currentVirtualKeyboardMode() === 'auto') {
    applyVirtualKeyboardMode('auto', false);
  }
  if (nowMobile !== _prevMobile) {
    setSidebarOpen(!nowMobile);
    _prevMobile = nowMobile;
    resetResizeCache();   // CSS mode swap — old _lastResize is stale
    applyTerminalScrollOptions();

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

function setPasteSheetOpen(open) {
  const sheet = document.getElementById('paste-sheet');
  sheet.classList.toggle('hidden', !open);
  sheet.setAttribute('aria-hidden', open ? 'false' : 'true');
}

function openPasteSheet() {
  HistoryOverlay.close();
  const box = document.getElementById('paste-text');
  box.value = '';
  setPasteSheetOpen(true);
  box.focus();
}

function closePasteSheet() {
  setPasteSheetOpen(false);
  focusActivePane();
}

function sendPasteSheetText() {
  const box = document.getElementById('paste-text');
  if (!box.value) return;
  sendPaneInput(box.value);
  closePasteSheet();
}

function preserveKeyboardState(ev) {
  // Keep virtual-key buttons from stealing focus away from xterm's textarea on iPhone.
  ev.preventDefault();
  markClientActive();
}

document.querySelectorAll('#bottombar button').forEach((btn) => {
  btn.addEventListener('pointerdown', preserveKeyboardState);
});

document.querySelectorAll('#bottombar .vkey').forEach((btn) => {
  btn.addEventListener('click', () => {
    sendVirtualKey(btn.dataset.key);
  });
});

document.getElementById('ctrl-toggle').addEventListener('click', () => {
  markClientActive();
  setCtrlActive(!_ctrlActive);
  // Ctrl should reopen the software keyboard if it was closed.
  focusActivePane();
});

document.getElementById('paste-toggle').addEventListener('click', openPasteSheet);
document.getElementById('paste-close').addEventListener('click', closePasteSheet);
document.getElementById('paste-cancel').addEventListener('click', closePasteSheet);
document.getElementById('paste-send').addEventListener('click', sendPasteSheetText);
document.querySelector('#paste-sheet .sheet-backdrop').addEventListener('click', closePasteSheet);

document.getElementById('virtual-keyboard-toggle').addEventListener('click', (e) => {
  const menu = document.getElementById('virtual-keyboard-menu');
  menu.hidden = !menu.hidden;
  e.stopPropagation();
});

document.querySelectorAll('#virtual-keyboard-menu [data-virtual-keyboard-mode]').forEach((btn) => {
  btn.addEventListener('click', () => {
    applyVirtualKeyboardMode(btn.dataset.virtualKeyboardMode);
    document.getElementById('virtual-keyboard-menu').hidden = true;
  });
});

// ─── File viewer ──────────────────────────────────────────────────────────────
// Browses the host being viewed, starting from a pane's working directory.
// Every request goes over the socket (list_dir / read_file); there is no
// request queue here — the server runs one at a time, and a request that
// arrives while one runs replaces any already waiting. So only one is ever
// left outstanding: the latest navigation (_files.reqId), or else one
// image a Markdown document shows (_mdImages.inFlight).

const FILES_HIDDEN_STORAGE_KEY = 'web-tmux-files-hidden';
const MARKDOWN_FILE_RE = /\.(md|markdown|mdown|mkd)$/i;
// Opened as an image; the server goes by the content, not the name.
const IMAGE_FILE_RE = /\.(png|jpe?g|gif|webp|avif|bmp|ico|svg)$/i;
const IMAGE_READ_LIMIT = 4 * 1024 * 1024;   // server.IMAGE_READ_LIMIT
// What one Markdown document may pull in, beyond which images keep their alt text.
const MD_IMAGE_MAX_COUNT = 30;
const MD_IMAGE_MAX_BYTES = 16 * 1024 * 1024;
const FILE_ERRORS = {
  not_found:         'No such file or folder.',
  not_a_directory:   'Not a folder.',
  permission_denied: 'Permission denied.',
  not_regular:       'Not a regular file (a device, FIFO or socket).',
  too_large:         'This folder has too much in it to list.',
  invalid_path:      'This name can’t be opened here.',
  host_changed:      'The host changed. Close and open the viewer again.',
  unavailable:       'The host did not answer.',
  unsupported_image: 'Not an image this browser can show.',
};

const _files = {
  open: false,
  host: '',
  lastId: 0,         // ids for every request, navigation or image
  reqId: -1,         // the navigation whose reply is awaited
  pending: null,     // { kind: 'dir'|'file', path, pane, image } awaiting its reply
  dir: '',           // folder being shown, or the one the open file is in
  entries: null,     // its listing
  truncated: false,
  file: null,        // { path, size, truncated, binary, text } or { path, size, image, src, dims }
  raw: false,        // Markdown shown as source
  fit: true,         // an image scaled to the sheet rather than 1:1
  showHidden: loadFilesShowHidden(),
};
let _markdown = null;

// Images in the Markdown document on screen, fetched one at a time as they
// scroll into view.
const _mdImages = {
  doc: '',           // the document they belong to
  cache: new Map(),  // path → { src } or { error }
  queue: [],         // paths waiting to be asked for
  inFlight: null,    // { id, path }
  count: 0,
  bytes: 0,
  observer: null,
};

function nextFileId() {
  _files.lastId = (_files.lastId + 1) % 2 ** 31;
  return _files.lastId;
}

function loadFilesShowHidden() {
  try { return localStorage.getItem(FILES_HIDDEN_STORAGE_KEY) === '1'; }
  catch (_) { return false; }
}

function joinPath(dir, name) {
  return dir === '/' ? `/${name}` : `${dir}/${name}`;
}

function parentPath(path) {
  return path.replace(/\/[^/]*$/, '') || '/';
}

function baseName(path) {
  return path === '/' ? '/' : path.slice(path.lastIndexOf('/') + 1);
}

function formatFileSize(n) {
  if (n < 1024) return `${n} B`;
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KiB`;
  return `${(n / 1024 / 1024).toFixed(1)} MiB`;
}

function setFileSheetOpen(open) {
  const sheet = document.getElementById('file-sheet');
  sheet.classList.toggle('hidden', !open);
  sheet.setAttribute('aria-hidden', open ? 'false' : 'true');
}

function openFileViewer(paneId) {
  HistoryOverlay.close();
  _files.open = true;
  _files.host = currentHost;
  _files.dir = '';
  _files.entries = null;
  _files.file = null;
  setFileSheetOpen(true);
  requestFileDir('', paneId || activePaneId || '');
  document.getElementById('file-body').focus({ preventScroll: true });
}

function closeFileViewer() {
  _files.open = false;
  _files.pending = null;
  _files.reqId = -1;   // whatever is still on its way is no longer wanted
  resetMdImages('');
  setFileSheetOpen(false);
  document.getElementById('file-body').replaceChildren();
  focusActivePane();
}

function sendFileRequest(req) {
  _files.pending = req;
  _files.reqId = nextFileId();
  // A document image still loading is dropped: this request may replace it
  // at the server, and its reply would land on a page that is going away.
  _mdImages.queue = [];
  _mdImages.inFlight = null;
  const extra = { id: _files.reqId, host: _files.host };
  if (req.path) extra.path = req.path;
  else if (req.pane) { extra.pane = req.pane; extra.gen = currentGen; }
  if (req.image) extra.image = true;
  if (!wsSendType(req.kind === 'dir' ? 'list_dir' : 'read_file', extra)) {
    _files.pending = null;
    showFileNote('Not connected.');
    return;
  }
  renderFileView();
}

// An empty path starts from `pane`'s cwd (or $HOME, when it has none).
function requestFileDir(path, pane) {
  sendFileRequest({ kind: 'dir', path, pane: path ? '' : pane });
}

function requestFile(path) {
  sendFileRequest({ kind: 'file', path, image: IMAGE_FILE_RE.test(path) });
}

function reloadFileView() {
  if (_files.file) requestFile(_files.file.path);
  else requestFileDir(_files.dir, activePaneId || '');
}

function fileReplyWanted(msg) {
  return _files.open && msg.id === _files.reqId && msg.host === _files.host;
}

function onDirListing(msg) {
  if (!fileReplyWanted(msg)) return;
  _files.pending = null;
  _files.dir = msg.path;
  _files.entries = msg.entries || [];
  _files.truncated = !!msg.truncated;
  _files.file = null;
  renderFileView();
  document.getElementById('file-body').scrollTop = 0;
}

function onFileContent(msg) {
  if (onMdImageReply(msg)) return;
  if (!fileReplyWanted(msg)) return;
  _files.pending = null;
  // A fresh file response starts a new document, even at the same path.
  // Keep cached images only for local redraws (for example, toggling Raw).
  resetMdImages('');
  const dir = parentPath(msg.path);
  if (dir !== _files.dir) {
    _files.dir = dir;      // reached through a link: Back lists its folder
    _files.entries = null;
  }
  if (msg.mime) {
    _files.file = {
      path: msg.path,
      size: msg.size,
      image: true,
      src: `data:${msg.mime};base64,${msg.data}`,
      dims: '',
    };
    _files.fit = true;
    renderFileView();
    document.getElementById('file-body').scrollTop = 0;
    return;
  }
  const bytes = b64ToUint8(msg.data || '');
  _files.file = {
    path: msg.path,
    size: msg.size,
    shown: bytes.length,
    truncated: !!msg.truncated,
    binary: !!msg.binary,
    text: msg.binary ? '' : new TextDecoder('utf-8').decode(bytes),
  };
  _files.raw = false;
  renderFileView();
  document.getElementById('file-body').scrollTop = 0;
}

function fileErrorText(msg) {
  if (msg.code === 'too_large' && msg.request === 'read_file') {
    return Number.isInteger(msg.size)
      ? `Too large to show: ${formatFileSize(msg.size)} (the limit is ${formatFileSize(IMAGE_READ_LIMIT)}).`
      : 'Too large to show.';
  }
  return FILE_ERRORS[msg.code] || 'Could not open it.';
}

function onFileError(msg) {
  if (onMdImageReply(msg)) return;
  if (!fileReplyWanted(msg)) return;
  const req = _files.pending;
  _files.pending = null;
  // A link (or a symlink) that named a folder: list it instead.
  if (msg.code === 'is_directory' && req && req.kind === 'file') {
    requestFileDir(req.path);
    return;
  }
  renderFileView();
  showFileNote(fileErrorText(msg));
}

function showFileNote(text) {
  const note = document.getElementById('file-note');
  note.textContent = text;
  note.hidden = !text;
}

function renderFileCrumbs() {
  const crumbs = document.getElementById('file-crumbs');
  crumbs.replaceChildren();
  if (!_files.dir) return;
  const parts = _files.dir.split('/').filter(Boolean);
  const add = (label, path) => {
    const btn = document.createElement('button');
    btn.type = 'button';
    btn.className = 'file-crumb';
    btn.textContent = label;
    btn.addEventListener('click', () => requestFileDir(path));
    crumbs.appendChild(btn);
  };
  add('/', '/');
  let path = '';
  parts.forEach((part) => {
    path += `/${part}`;
    add(part, path);
  });
  crumbs.scrollLeft = crumbs.scrollWidth;   // the end of a long path is the part that matters
}

function buildFileEntry(label, kind, onClick, extraClass, isImage) {
  const btn = document.createElement('button');
  btn.type = 'button';
  btn.className = `file-entry file-${kind}` + (extraClass ? ` ${extraClass}` : '');
  const icon = document.createElement('span');
  icon.className = 'file-icon';
  icon.setAttribute('aria-hidden', 'true');
  icon.textContent = kind === 'd' ? '\u{1F4C1}'
    : kind !== 'f' ? '◇'
    : isImage ? '\u{1F5BC}' : '\u{1F4C4}';
  const name = document.createElement('span');
  name.className = 'file-name';
  name.textContent = label;
  btn.append(icon, name);
  btn.addEventListener('click', onClick);
  return btn;
}

function renderFileList(body) {
  const list = document.createElement('div');
  list.className = 'file-list';
  if (_files.dir !== '/') {
    list.appendChild(buildFileEntry('..', 'd', () => requestFileDir(parentPath(_files.dir)), 'file-up'));
  }
  const entries = (_files.entries || [])
    .filter((e) => _files.showHidden || !e.name.startsWith('.'))
    .sort((a, b) => ((a.kind === 'd') === (b.kind === 'd')
      ? a.name.localeCompare(b.name)
      : (a.kind === 'd' ? -1 : 1)));
  entries.forEach((e) => {
    const path = joinPath(_files.dir, e.name);
    const label = e.name + (e.kind === 'd' ? '/' : '') + (e.link ? ' →' : '');
    list.appendChild(buildFileEntry(label, e.kind, () => {
      if (e.kind === 'd') requestFileDir(path);
      else requestFile(path);
    }, '', IMAGE_FILE_RE.test(e.name)));
  });
  if (!entries.length) {
    const empty = document.createElement('div');
    empty.className = 'file-empty';
    empty.textContent = (_files.entries || []).length ? 'Only hidden files here.' : 'Empty folder.';
    list.appendChild(empty);
  }
  body.appendChild(list);
  if (_files.truncated) showFileNote('Too many entries; only the first ones are listed.');
}

function markdownRenderer() {
  if (_markdown || !window.markdownit) return _markdown;
  // html: false escapes any raw HTML in the document, and markdown-it's own
  // link check drops javascript:, vbscript: and file: URLs.
  _markdown = window.markdownit({ html: false, linkify: false, typographer: false });
  // Column alignment comes out as style="text-align:…", which the page's CSP
  // (style-src 'self') refuses. Turn it into a class.
  _markdown.core.ruler.push('align_class', (state) => {
    state.tokens.forEach((token) => {
      if (token.type !== 'th_open' && token.type !== 'td_open') return;
      const style = token.attrGet('style');
      if (!style) return;
      token.attrs = token.attrs.filter(([name]) => name !== 'style');
      const align = style.replace('text-align:', '');
      if (['left', 'center', 'right'].includes(align)) token.attrJoin('class', `md-align-${align}`);
    });
  });
  return _markdown;
}

function isLocalUrl(href) {
  return !/^[a-z][a-z0-9+.-]*:/i.test(href) && !href.startsWith('//');
}

// A link or image source in a document, as a path on its host.
function documentPath(href, filePath) {
  const target = href.replace(/[?#].*$/, '');
  if (!target) return '';
  let path;
  try { path = decodeURIComponent(target); } catch (_) { path = target; }
  if (!path.startsWith('/')) path = joinPath(parentPath(filePath), path);
  // Let the host resolve . and .. after following any symlinks.
  return path;
}

// Links to the same host's files open in the viewer; everything else in a
// new tab. Images on the same host are fetched over the socket as they come
// into view (observeMdImages); remote ones can't load under the CSP and
// would only show as broken, so their alt text stands in.
function wireMarkdownContent(root, filePath) {
  root.querySelectorAll('a[href]').forEach((a) => {
    const href = a.getAttribute('href');
    if (!isLocalUrl(href)) {
      a.target = '_blank';
      a.rel = 'noopener noreferrer';
      return;
    }
    a.addEventListener('click', (ev) => {
      ev.preventDefault();
      const path = documentPath(href, filePath);
      if (path) requestFile(path);   // '' is an in-page anchor: headings carry no ids
    });
  });
  root.querySelectorAll('img').forEach((img) => {
    const src = img.getAttribute('src') || '';
    if (src.startsWith('data:')) return;
    const altText = img.getAttribute('alt') || src;
    const path = isLocalUrl(src) ? documentPath(src, filePath) : '';
    const cached = path && _mdImages.cache.get(path);
    if (cached && cached.src) {
      img.replaceWith(buildMdImage(cached.src, altText));
      return;
    }
    const alt = document.createElement('span');
    alt.className = 'md-img-alt';
    alt.textContent = `[image: ${altText}]`;
    if (cached) {
      alt.textContent = `[image: ${altText} \u2014 ${cached.error}]`;
    } else if (path) {
      alt.classList.add('md-img-slot');
      alt.dataset.path = path;
      alt.dataset.alt = altText;
    }
    img.replaceWith(alt);
  });
}

function buildMdImage(src, altText) {
  const img = document.createElement('img');
  img.className = 'md-img';
  img.alt = altText;
  img.src = src;
  return img;
}

function resetMdImages(docPath) {
  if (_mdImages.observer) _mdImages.observer.disconnect();
  _mdImages.observer = null;
  _mdImages.queue = [];
  if (docPath === _mdImages.doc) return;   // re-drawn, not replaced: keep what came
  _mdImages.doc = docPath;
  _mdImages.cache = new Map();
  _mdImages.inFlight = null;
  _mdImages.count = 0;
  _mdImages.bytes = 0;
}

function observeMdImages(doc) {
  const slots = doc.querySelectorAll('.md-img-slot');
  if (!slots.length) return;
  const observer = new IntersectionObserver((items) => {
    items.forEach((item) => {
      if (!item.isIntersecting) return;
      observer.unobserve(item.target);
      const path = item.target.dataset.path;
      if (!_mdImages.queue.includes(path) && _mdImages.inFlight?.path !== path) {
        _mdImages.queue.push(path);
      }
    });
    pumpMdImages();
  }, { root: document.getElementById('file-body'), rootMargin: '600px 0px' });
  _mdImages.observer = observer;
  slots.forEach((slot) => observer.observe(slot));
}

function settleMdImageSlots(path) {
  const entry = _mdImages.cache.get(path);
  document.querySelectorAll('#file-body .md-img-slot').forEach((slot) => {
    if (slot.dataset.path !== path) return;
    if (entry.src) {
      slot.replaceWith(buildMdImage(entry.src, slot.dataset.alt));
    } else {
      slot.classList.remove('md-img-slot');
      slot.textContent = `[image: ${slot.dataset.alt} \u2014 ${entry.error}]`;
    }
  });
}

// Asks for the next image waiting, unless a request is already out: one
// sent behind another could replace it at the server and never be answered.
function pumpMdImages() {
  if (!_files.open || _files.pending || _mdImages.inFlight) return;
  while (_mdImages.queue.length) {
    const path = _mdImages.queue.shift();
    if (!_mdImages.cache.has(path)
        && (_mdImages.count >= MD_IMAGE_MAX_COUNT || _mdImages.bytes >= MD_IMAGE_MAX_BYTES)) {
      _mdImages.cache.set(path, { error: 'not loaded: this document has too many images' });
    }
    if (_mdImages.cache.has(path)) {
      settleMdImageSlots(path);
      continue;
    }
    const id = nextFileId();
    if (!wsSend('read_file', { id, host: _files.host, path, image: true })) return;
    _mdImages.inFlight = { id, path };
    return;
  }
}

// True when `msg` answers the document image in flight (and is dealt with).
function onMdImageReply(msg) {
  const req = _mdImages.inFlight;
  if (!req || msg.id !== req.id || msg.host !== _files.host) return false;
  _mdImages.inFlight = null;
  if (msg.type === 'file_content' && msg.mime) {
    _mdImages.cache.set(req.path, { src: `data:${msg.mime};base64,${msg.data}` });
    _mdImages.count++;
    _mdImages.bytes += msg.size;
  } else {
    const text = msg.type === 'file_error' ? fileErrorText(msg) : FILE_ERRORS.unsupported_image;
    const reason = text.replace(/\.$/, '');
    _mdImages.cache.set(req.path, { error: reason.charAt(0).toLowerCase() + reason.slice(1) });
  }
  settleMdImageSlots(req.path);
  pumpMdImages();
  return true;
}

function renderImageFile(body, file) {
  const wrap = document.createElement('div');
  wrap.className = 'file-image-wrap' + (_files.fit ? ' fit' : '');
  const img = document.createElement('img');
  img.className = 'file-image';
  img.alt = baseName(file.path);
  img.src = file.src;
  img.addEventListener('load', () => {
    file.dims = `${img.naturalWidth}\u00D7${img.naturalHeight}`;
    document.getElementById('file-meta').textContent = fileMetaText(file);
  });
  img.addEventListener('error', () => showFileNote('The browser could not draw this image.'));
  img.addEventListener('click', toggleImageFit);
  wrap.appendChild(img);
  body.appendChild(wrap);
}

function toggleImageFit() {
  if (!_files.file || !_files.file.image) return;
  _files.fit = !_files.fit;
  renderFileView();
}

function fileMetaText(file) {
  return [_files.host, file.dims, formatFileSize(file.size)].filter(Boolean).join(' \u00B7 ');
}

function renderFileContent(body) {
  const file = _files.file;
  if (file.image) {
    renderImageFile(body, file);
    return;
  }
  if (file.binary) {
    showFileNote('Binary file; not shown.');
    return;
  }
  if (file.truncated) {
    showFileNote(`Showing the first ${formatFileSize(file.shown)} of ${formatFileSize(file.size)}.`);
  }
  const md = MARKDOWN_FILE_RE.test(file.path) && !_files.raw ? markdownRenderer() : null;
  if (md) {
    // Parsed in an inert <template> first: set on a live element, the HTML
    // would start fetching every image before they can be swapped out.
    const tpl = document.createElement('template');
    tpl.innerHTML = md.render(file.text);
    wireMarkdownContent(tpl.content, file.path);
    const doc = document.createElement('div');
    doc.className = 'md-body';
    doc.appendChild(tpl.content);
    body.appendChild(doc);
    observeMdImages(doc);
    return;
  }
  const pre = document.createElement('pre');
  pre.className = 'file-raw';
  pre.style.fontSize = `${currentFontSize()}px`;
  pre.textContent = file.text;
  body.appendChild(pre);
}

function renderFileView() {
  if (!_files.open) return;
  const file = _files.file;
  const viewingFile = !!file;
  const isMarkdown = viewingFile && !file.image && MARKDOWN_FILE_RE.test(file.path);
  document.getElementById('file-title').textContent =
    viewingFile ? baseName(file.path) : (_files.dir ? baseName(_files.dir) : 'Files');
  document.getElementById('file-back').hidden = !viewingFile;
  document.getElementById('file-crumbs').hidden = viewingFile;
  const meta = document.getElementById('file-meta');
  meta.hidden = !viewingFile;
  meta.textContent = viewingFile ? fileMetaText(file) : '';
  const hiddenBtn = document.getElementById('file-hidden');
  hiddenBtn.hidden = viewingFile;
  hiddenBtn.setAttribute('aria-pressed', _files.showHidden ? 'true' : 'false');
  const rawBtn = document.getElementById('file-raw');
  rawBtn.hidden = !isMarkdown || file.binary;
  rawBtn.setAttribute('aria-pressed', _files.raw ? 'true' : 'false');
  const fitBtn = document.getElementById('file-fit');
  fitBtn.hidden = !(viewingFile && file.image);
  fitBtn.setAttribute('aria-pressed', _files.fit ? 'false' : 'true');
  document.getElementById('file-copy').hidden = !viewingFile;
  if (!viewingFile) renderFileCrumbs();

  showFileNote('');
  // Raw and back keeps the document's images; anything else lets them go.
  resetMdImages(isMarkdown ? file.path : '');
  const body = document.getElementById('file-body');
  body.replaceChildren();
  body.classList.toggle('loading', !!_files.pending);
  if (_files.pending && !(viewingFile || _files.entries)) {
    const loading = document.createElement('div');
    loading.className = 'file-empty';
    loading.textContent = 'Loading…';
    body.appendChild(loading);
    return;
  }
  if (viewingFile) renderFileContent(body);
  else if (_files.entries) renderFileList(body);
}

document.getElementById('files-toggle').addEventListener('click', () => openFileViewer());
document.getElementById('file-close').addEventListener('click', closeFileViewer);
document.querySelector('#file-sheet .sheet-backdrop').addEventListener('click', closeFileViewer);
function backToFileDir() {
  _files.pending = null;
  _files.reqId = -1;   // ignore a file response arriving after Back
  _files.file = null;
  if (_files.entries) renderFileView();
  else requestFileDir(_files.dir);
}
document.getElementById('file-back').addEventListener('click', backToFileDir);
document.getElementById('file-reload').addEventListener('click', reloadFileView);
document.getElementById('file-hidden').addEventListener('click', () => {
  _files.showHidden = !_files.showHidden;
  try { localStorage.setItem(FILES_HIDDEN_STORAGE_KEY, _files.showHidden ? '1' : '0'); } catch (_) {}
  renderFileView();
});
document.getElementById('file-raw').addEventListener('click', () => {
  _files.raw = !_files.raw;
  renderFileView();
});
document.getElementById('file-fit').addEventListener('click', toggleImageFit);
document.getElementById('file-copy').addEventListener('click', () => {
  if (!_files.file) return;
  writeClipboard(_files.file.path).then(
    () => showFileNote('Path copied.'),
    () => showFileNote(_files.file.path));
});
document.addEventListener('keydown', (ev) => {
  if (ev.key !== 'Escape' || !_files.open) return;
  ev.preventDefault();
  closeFileViewer();
}, true);   // capture, so xterm doesn't swallow it first

// ─── Soft keyboard / viewport handling (touch devices) ───────────────────────
// On iOS, `height: 100vh` returns the LARGEST possible viewport (URL bar hidden),
// which is bigger than the actually visible area when the URL bar is showing.
// We pin #app's height to visualViewport.height so the layout always fits the
// real visible area. Re-fitting xterm is intentionally debounced on mobile:
// visualViewport fires repeatedly while the browser UI or keyboard animates,
// and a fit()/resize mid-gesture would resize the tmux pane (and so the overlay
// it is aligned to) underneath the user's finger.

function markTouchScroll() {
  _lastTouchScrollAt = Date.now();
}

function runViewportRefit() {
  _viewportRefitTimer = null;
  if (!usesTouchInterface()) return;
  if (_layoutApplying || Date.now() - _lastTouchScrollAt < MOBILE_TOUCH_SCROLL_IDLE_MS) {
    _viewportRefitTimer = setTimeout(runViewportRefit, MOBILE_VIEWPORT_REFIT_DELAY_MS);
    return;
  }

  const p = activePaneId && panes[activePaneId];
  if (!p) return;

  const wasFocused = p.term.textarea && document.activeElement === p.term.textarea;
  // The usual layout paths, not fit(): fit() sizes xterm to its element, a
  // column wider than the tmux pane once the touch margin is taken off, and
  // in a split the active pane's size is not the window's.
  if (_currentLayoutPanes.length > 1) scheduleLayout(_currentLayoutPanes, _currentLayoutStr);
  else fitSinglePane(activePaneId);
  if (wasFocused && Date.now() - _lastTouchScrollAt >= MOBILE_TOUCH_SCROLL_IDLE_MS) {
    requestAnimationFrame(() => {
      try { p.term.focus(); } catch (_) {}
    });
  }
}

function scheduleViewportRefit() {
  if (_viewportRefitTimer) clearTimeout(_viewportRefitTimer);
  _viewportRefitTimer = setTimeout(runViewportRefit, MOBILE_VIEWPORT_REFIT_DELAY_MS);
}

function applyViewportFix() {
  const vv  = window.visualViewport;
  const app = document.getElementById('app');
  if (!vv || !app) return;
  const width = Math.round(vv.width);
  const height = Math.round(vv.height);
  const widthChanged = width !== _lastViewportSize.width;
  const heightChanged = height !== _lastViewportSize.height;
  _lastViewportSize = { width, height };
  if (usesTouchInterface()) {
    app.style.height = `${vv.height}px`;
    if (widthChanged || heightChanged) {
      scheduleViewportRefit();
    }
  } else {
    app.style.height = '';
    if (_viewportRefitTimer) {
      clearTimeout(_viewportRefitTimer);
      _viewportRefitTimer = null;
    }
  }
}

(function setupViewportEvents() {
  const vv = window.visualViewport;
  if (!vv) return;
  vv.addEventListener('resize', applyViewportFix);
  window.addEventListener('resize', applyViewportFix);
  document.addEventListener('touchmove', markTouchScroll, { passive: true });
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
  const keyboardMenu = document.getElementById('virtual-keyboard-menu');
  if (keyboardMenu) keyboardMenu.hidden = true;
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

HistoryOverlay.init({
  getActivePane: () => (activePaneId ? panes[activePaneId] || null : null),
  isMobile:      usesTouchInterface,
  getTheme:      () => XTERM_THEMES[currentThemeName()] || XTERM_THEMES.dark,
  focusPane:     () => focusActivePane({ retries: 2 }),
});

const _host = location.hostname.split('.')[0];
updateHostLabel();

initTheme();
initFontSize();
initVirtualKeyboard();

if (window.matchMedia) {
  const coarsePointerQuery = window.matchMedia('(any-pointer: coarse)');
  const refreshAutoVirtualKeyboard = () => {
    if (currentVirtualKeyboardMode() === 'auto') applyVirtualKeyboardMode('auto', false);
  };
  if (coarsePointerQuery.addEventListener) {
    coarsePointerQuery.addEventListener('change', refreshAutoVirtualKeyboard);
  } else if (coarsePointerQuery.addListener) {
    coarsePointerQuery.addListener(refreshAutoVirtualKeyboard);
  }
}

// Nothing may touch the network until the document has finished loading.
// WebKit folds a fetch or a WebSocket started during the load into the page's
// own loading state, and on a history navigation served entirely from cache —
// instant, so our connection is under way before the load event fires — the
// browser is left showing "loading" for good on a page that works fine. The
// timer is a floor: if the load event never arrives, connect anyway.
let _connectStarted = false;
function connectAfterLoad() {
  if (_connectStarted) return;
  _connectStarted = true;
  registerServiceWorker();
  setTimeout(connect, 0);
}

// The worker answers the document request the browser makes on its own when
// it re-creates a discarded tab, which is the one load neither this page nor
// the stop button can do anything about. Failing to register is not fatal:
// the app works exactly as before, the browser just keeps showing "loading"
// on a restored tab.
function registerServiceWorker() {
  if (!('serviceWorker' in navigator)) return;
  navigator.serviceWorker.register('/sw.js')
    .catch((e) => console.warn('service worker registration failed', e));
}
if (document.readyState === 'complete') {
  connectAfterLoad();
} else {
  window.addEventListener('load', connectAfterLoad, { once: true });
  setTimeout(connectAfterLoad, CONNECT_AFTER_LOAD_FALLBACK_MS);
}
