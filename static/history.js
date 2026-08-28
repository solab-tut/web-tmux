'use strict';

// ─── History overlay ──────────────────────────────────────────────────────────
//
// Scrolling never touches xterm. xterm only ever paints the live screen; the
// scrollback is shown as real DOM rows inside a natively scrolling container,
// so the pixels themselves move on the compositor — momentum, a real scrollbar
// and native text selection all come for free, and the per-frame JS repaint
// (Viewport._handleScroll → scrollLines → refresh(0, rows-1)) disappears.
//
// Two states:
//   LIVE    — xterm visible. Desktop: overlay detached. Mobile: overlay attached
//             but transparent and empty (see "armed" below).
//   HISTORY — overlay opaque over the active pane, showing rendered buffer rows.
//
// Why the overlay stays mounted on mobile even in LIVE ("armed"): the browser
// picks the scroll container by hit-testing on the compositor thread at
// touchstart, before (or in parallel with) JS. An overlay created *during*
// touchstart would not receive the pan, and the gesture — and its momentum —
// would be lost. So the scroll surface must already be there when the finger
// lands. Armed costs nothing: the content is a single spacer div sized to the
// buffer, so scrollHeight is correct while the DOM stays empty. The real rows
// are built at touchstart, before the pan has moved a pixel.

(function () {

  // Rows materialised per build. The rest of the buffer is represented by the
  // spacer above them and built on demand as the user nears the top.
  const WINDOW_ROWS   = 2000;
  const CHUNK_ROWS    = 2000;
  // Distance from the top of the scroll container that triggers a prepend.
  const TOP_TRIGGER_PX = 1200;
  // A touch that moves less than this within this time is a tap, not a scroll.
  const TAP_MOVE_PX = 10;
  const TAP_MAX_MS  = 500;

  const ANSI_16 = [
    'black', 'red', 'green', 'yellow', 'blue', 'magenta', 'cyan', 'white',
    'brightBlack', 'brightRed', 'brightGreen', 'brightYellow',
    'brightBlue', 'brightMagenta', 'brightCyan', 'brightWhite',
  ];

  let deps = {
    getActivePane: () => null,
    isMobile:      () => false,
    getTheme:      () => ({}),
    focusPane:     () => {},
  };

  let root      = null;   // #hist
  let scroller  = null;   // #hist-scroll
  let pad       = null;   // #hist-pad
  let rowsEl    = null;   // #hist-rows
  let tailEl    = null;   // #hist-tail
  let exitBtn   = null;   // #hist-exit

  let attachedPane = null;   // pane record the overlay is currently a child of
  let engaged      = false;  // true = HISTORY state
  let rowH         = 0;
  let buildStart   = 0;      // first buffer line materialised in rowsEl
  let buildEnd     = 0;      // one past the last materialised line
  let totalLines   = 0;      // buffer.length at last sync
  let padFrame     = null;
  let touchStartY  = 0;
  let touchStartAt = 0;
  let touchMoved   = false;
  let touching     = false;
  let bottomResetTimer = null;

  // ─── Colour ────────────────────────────────────────────────────────────────

  function hex2(n) {
    return (n < 16 ? '0' : '') + n.toString(16);
  }

  function rgbHex(r, g, b) {
    return '#' + hex2(r & 0xff) + hex2(g & 0xff) + hex2(b & 0xff);
  }

  function rgbHexFromInt(v) {
    return rgbHex((v >> 16) & 0xff, (v >> 8) & 0xff, v & 0xff);
  }

  // Same mapping xterm uses for the 256-colour palette, so overlay rows are
  // pixel-identical to the live screen they replace.
  function paletteHex(idx, theme) {
    if (idx < 16) return theme[ANSI_16[idx]] || theme.foreground || '#d4d4d4';
    if (idx < 232) {
      const n = idx - 16;
      const step = (v) => (v === 0 ? 0 : 55 + v * 40);
      return rgbHex(step(Math.floor(n / 36)), step(Math.floor((n % 36) / 6)), step(n % 6));
    }
    const v = 8 + (idx - 232) * 10;
    return rgbHex(v, v, v);
  }

  function cellFg(cell, theme) {
    if (cell.isFgRGB()) return rgbHexFromInt(cell.getFgColor());
    if (cell.isFgPalette()) {
      let idx = cell.getFgColor();
      // xterm's drawBoldTextInBrightColors (on by default) promotes bold text
      // in the low 8 palette slots to their bright counterpart.
      if (cell.isBold() && idx < 8) idx += 8;
      return paletteHex(idx, theme);
    }
    return null;
  }

  function cellBg(cell, theme) {
    if (cell.isBgRGB()) return rgbHexFromInt(cell.getBgColor());
    if (cell.isBgPalette()) return paletteHex(cell.getBgColor(), theme);
    return null;
  }

  function cellStyle(cell, theme) {
    let fg = cellFg(cell, theme);
    let bg = cellBg(cell, theme);

    if (cell.isInverse()) {
      const prevFg = fg;
      fg = bg !== null ? bg : theme.background;
      bg = prevFg !== null ? prevFg : theme.foreground;
    }

    let s = '';
    if (fg !== null) s += 'color:' + fg + ';';
    if (bg !== null) s += 'background:' + bg + ';';
    if (cell.isBold()) s += 'font-weight:bold;';
    if (cell.isItalic()) s += 'font-style:italic;';
    if (cell.isDim()) s += 'opacity:.5;';
    if (cell.isUnderline() && cell.isStrikethrough()) s += 'text-decoration:underline line-through;';
    else if (cell.isUnderline()) s += 'text-decoration:underline;';
    else if (cell.isStrikethrough()) s += 'text-decoration:line-through;';
    return s;
  }

  // Identity of a cell's rendition, used to merge adjacent cells into one span.
  // CellData exposes the packed fg/bg attribute words; fall back to composing a
  // key from the public getters if that ever stops being true.
  function cellKey(cell) {
    if (typeof cell.fg === 'number' && typeof cell.bg === 'number') {
      return cell.fg + ',' + cell.bg;
    }
    return cell.getFgColor() + ',' + cell.getFgColorMode() + ',' +
           cell.getBgColor() + ',' + cell.getBgColorMode() + ',' +
           (cell.isBold() ? 1 : 0) + (cell.isDim() ? 1 : 0) + (cell.isItalic() ? 1 : 0) +
           (cell.isUnderline() ? 1 : 0) + (cell.isInverse() ? 1 : 0) +
           (cell.isStrikethrough() ? 1 : 0);
  }

  function escHtml(s) {
    return s.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
  }

  // ─── Row rendering ─────────────────────────────────────────────────────────

  // Trailing blanks with default background carry no information and only make
  // a native selection pick up ragged whitespace, so they are dropped.
  function lastMeaningfulCol(line, cell) {
    for (let x = line.length - 1; x >= 0; x--) {
      line.getCell(x, cell);
      const chars = cell.getChars();
      if (chars && chars !== ' ') return x;
      if (!cell.isBgDefault()) return x;
    }
    return -1;
  }

  function renderLine(line, cell, theme) {
    const last = lastMeaningfulCol(line, cell);
    if (last < 0) return '<div class="hist-row"></div>';

    let html = '';
    let runText = '';
    let runKey = null;
    let runStyle = '';

    for (let x = 0; x <= last; x++) {
      line.getCell(x, cell);
      if (cell.getWidth() === 0) continue;   // trailing half of a wide char
      const key = cellKey(cell);
      if (key !== runKey) {
        if (runKey !== null) html += span(runText, runStyle);
        runKey = key;
        runStyle = cellStyle(cell, theme);
        runText = '';
      }
      const chars = cell.getChars();
      runText += chars === '' ? ' ' : chars;
    }
    if (runKey !== null) html += span(runText, runStyle);
    return '<div class="hist-row">' + html + '</div>';
  }

  function span(text, style) {
    const t = escHtml(text);
    return style ? '<span style="' + style + '">' + t + '</span>' : '<span>' + t + '</span>';
  }

  function renderRange(buffer, start, end, theme) {
    const cell = buffer.getNullCell();
    const parts = [];
    for (let i = start; i < end; i++) {
      const line = buffer.getLine(i);
      parts.push(line ? renderLine(line, cell, theme) : '<div class="hist-row"></div>');
    }
    return parts.join('');
  }

  // ─── Geometry ──────────────────────────────────────────────────────────────

  function measureRowHeight(pane) {
    try {
      const cell = pane.term._core._renderService.dimensions.css.cell;
      if (cell && cell.height > 0) return cell.height;
    } catch (_) { /* fall through to measurement */ }
    const screen = pane.el.querySelector('.xterm-screen');
    if (screen && pane.term.rows > 0 && screen.clientHeight > 0) {
      return screen.clientHeight / pane.term.rows;
    }
    return 17;
  }

  // The overlay must be pixel-identical to the screen it replaces, so every
  // metric is taken from the terminal itself rather than restated in CSS. The
  // natural advance of the same font at the same size is the same charWidth
  // xterm measured, so columns line up without a letter-spacing correction.
  function applyMetrics(pane) {
    rowH = measureRowHeight(pane);
    const theme = deps.getTheme();
    root.style.setProperty('--hist-row-h', rowH + 'px');
    root.style.setProperty('--hist-bg', theme.background || '#1e1e1e');
    root.style.setProperty('--hist-fg', theme.foreground || '#d4d4d4');
    root.style.setProperty('--hist-font-family', pane.term.options.fontFamily);
    root.style.setProperty('--hist-font-size', pane.term.options.fontSize + 'px');

    // xterm's canvas is top-aligned in the pane and rarely fills it exactly
    // (rows * rowH ≤ pane height), so the leftover sits below the last live
    // row. The overlay bottom-aligns its content against the container, so it
    // needs the same slack underneath or everything would sit that many pixels
    // low — a visible jump the moment a scroll starts.
    const slack = Math.max(0, pane.el.clientHeight - pane.term.rows * rowH);
    tailEl.style.height = slack + 'px';
  }

  function maxScrollTop() {
    return Math.max(0, scroller.scrollHeight - scroller.clientHeight);
  }

  function atBottom() {
    return maxScrollTop() - scroller.scrollTop <= 1;
  }

  // ─── Content ───────────────────────────────────────────────────────────────

  // Armed content: no rows at all, just a spacer as tall as the whole buffer.
  // scrollHeight is therefore correct (so the container is pannable the instant
  // a finger lands on it) while the DOM stays empty and free to maintain.
  function syncSpacer(pane) {
    const buffer = pane.term.buffer.active;
    totalLines = buffer.length;
    if (buildStart >= buildEnd) {
      buildStart = buildEnd = totalLines;
      pad.style.height = (totalLines * rowH) + 'px';
    } else {
      pad.style.height = (buildStart * rowH) + 'px';
    }
  }

  function buildWindow(pane) {
    const buffer = pane.term.buffer.active;
    const theme = deps.getTheme();
    totalLines = buffer.length;
    const end = totalLines;
    const start = Math.max(0, end - WINDOW_ROWS);
    rowsEl.innerHTML = renderRange(buffer, start, end, theme);
    buildStart = start;
    buildEnd = end;
    pad.style.height = (start * rowH) + 'px';
  }

  // Materialise CHUNK_ROWS more lines above what is already built. Row height is
  // fixed, so the height added above the viewport is known exactly and scrollTop
  // can be compensated in the same frame — the view does not jump.
  function prependChunk(pane) {
    if (buildStart <= 0) return;
    const buffer = pane.term.buffer.active;
    const theme = deps.getTheme();
    const start = Math.max(0, buildStart - CHUNK_ROWS);
    const prevScrollTop = scroller.scrollTop;

    rowsEl.insertAdjacentHTML('afterbegin', renderRange(buffer, start, buildStart, theme));
    buildStart = start;
    pad.style.height = (start * rowH) + 'px';
    // The new rows are exactly as tall as the spacer they replaced, so the
    // total height is unchanged; restore scrollTop against any adjustment the
    // browser made while the DOM was in flux and the view does not move.
    scroller.scrollTop = prevScrollTop;
  }

  // ─── State transitions ─────────────────────────────────────────────────────

  function isAltScreen(term) {
    try { return term.buffer.active.type === 'alternate'; } catch (_) { return false; }
  }

  function canOpen(pane) {
    if (!pane || !pane.term) return false;
    if (isAltScreen(pane.term)) return false;
    return pane.term.buffer.active.length > pane.term.rows;
  }

  function attach(pane) {
    if (attachedPane === pane && root.parentElement === pane.el) return;
    detach();
    pane.el.appendChild(root);
    attachedPane = pane;
    root.hidden = false;
    applyMetrics(pane);
    resetContent();
    syncSpacer(pane);
    scroller.scrollTop = maxScrollTop();
  }

  // With a pane argument this is a no-op unless the overlay is that pane's:
  // closing a background pane must not tear down the scrollback being read.
  function detach(pane) {
    if (!attachedPane) return;
    if (pane && pane !== attachedPane) return;
    setEngaged(false);
    root.hidden = true;
    if (root.parentElement) root.parentElement.removeChild(root);
    attachedPane = null;
    resetContent();
  }

  function resetContent() {
    rowsEl.innerHTML = '';
    buildStart = buildEnd = 0;
    pad.style.height = '0px';
  }

  function setEngaged(on) {
    if (engaged === on) return;
    engaged = on;
    root.classList.toggle('engaged', on);
  }

  // Build the real rows and hold the view exactly where it is. Called before a
  // gesture starts moving (wheel / touchstart), never from a scroll handler.
  function materialise(pane) {
    const wasAtBottom = atBottom();
    const fromBottom = maxScrollTop() - scroller.scrollTop;
    buildWindow(pane);
    scroller.scrollTop = wasAtBottom ? maxScrollTop() : maxScrollTop() - fromBottom;
  }

  // ─── Public entry points ───────────────────────────────────────────────────

  function open(deltaPx) {
    if (!root) return false;
    const pane = deps.getActivePane();
    if (!canOpen(pane)) return false;
    attach(pane);
    materialise(pane);
    if (deltaPx) scroller.scrollTop = Math.max(0, scroller.scrollTop + deltaPx);
    setEngaged(!atBottom());
    return true;
  }

  function close() {
    if (bottomResetTimer) { clearTimeout(bottomResetTimer); bottomResetTimer = null; }
    if (!attachedPane) return;
    if (deps.isMobile()) {
      // Stay armed so the next touch is caught by a live scroll surface.
      setEngaged(false);
      resetContent();
      syncSpacer(attachedPane);
      scroller.scrollTop = maxScrollTop();
    } else {
      detach();
    }
  }

  // Keep the armed spacer in step with the buffer as output streams in, so the
  // container never goes un-scrollable behind the user's back. O(1).
  function noteOutput() {
    if (!root || engaged || padFrame) return;
    padFrame = requestAnimationFrame(() => {
      padFrame = null;
      if (engaged) return;
      if (!attachedPane) {
        // A pane with no scrollback yet cannot be armed; the first output that
        // gives it one is what makes the overlay pannable.
        if (deps.isMobile()) sync();
        return;
      }
      if (buildStart < buildEnd) return;   // rows still built; close() will reset
      if (attachedPane.term.buffer.active.length === totalLines) return;
      syncSpacer(attachedPane);
      scroller.scrollTop = maxScrollTop();
    });
  }

  // Re-arm against whatever pane/geometry is current. Safe to call often.
  function sync() {
    if (!root) return;
    const pane = deps.getActivePane();
    if (!deps.isMobile()) {
      if (!engaged) detach();
      else if (pane && attachedPane !== pane) detach();
      return;
    }
    if (!canOpen(pane)) { detach(); return; }
    if (attachedPane !== pane) { attach(pane); return; }
    if (!engaged) {
      applyMetrics(pane);
      syncSpacer(pane);
      scroller.scrollTop = maxScrollTop();
    }
  }

  // Text of the rows currently on screen, for the clipboard sheet. Mirrors what
  // getActivePaneViewportText() returns for the live terminal.
  function visibleText() {
    if (!engaged || !attachedPane) return null;
    const top = scroller.scrollTop;
    const bottom = top + scroller.clientHeight;
    const padH = pad.offsetHeight;
    const out = [];
    const rows = rowsEl.children;
    for (let i = 0; i < rows.length; i++) {
      const elTop = padH + i * rowH;
      if (elTop + rowH <= top) continue;
      if (elTop >= bottom) break;
      out.push(rows[i].textContent.replace(/\s+$/, ''));
    }
    return out.join('\n');
  }

  function scrollByPages(direction) {
    if (!root) return;
    const pane = deps.getActivePane();
    if (!attachedPane || !engaged) {
      if (direction > 0) return;          // already at the bottom in LIVE
      if (!open(0)) return;
    }
    if (!pane) return;
    if (buildStart >= buildEnd) materialise(pane);
    const delta = direction * Math.max(1, Math.floor(pane.term.rows / 2)) * rowH;
    scroller.scrollBy({ top: delta, behavior: 'smooth' });
    setEngaged(true);
  }

  // ─── Events ────────────────────────────────────────────────────────────────

  // Reaching the bottom mid-flick must not tear down the content underneath a
  // running momentum scroll, so the reset waits until the scroll has settled.
  function scheduleBottomReset() {
    if (bottomResetTimer) clearTimeout(bottomResetTimer);
    bottomResetTimer = setTimeout(() => {
      bottomResetTimer = null;
      if (!attachedPane || touching || !atBottom()) return;
      close();
    }, 180);
  }

  function onScroll() {
    if (!attachedPane) return;
    if (atBottom()) {
      setEngaged(false);
      scheduleBottomReset();
      return;
    }
    if (bottomResetTimer) { clearTimeout(bottomResetTimer); bottomResetTimer = null; }
    if (!engaged) {
      // A pan started from the armed state: rows may not exist yet on the
      // paths that cannot pre-build (programmatic scrolls, scrollbar drags).
      if (buildStart >= buildEnd) materialise(attachedPane);
      setEngaged(true);
    }
    if (scroller.scrollTop < TOP_TRIGGER_PX) prependChunk(attachedPane);
  }

  function onPaneWheel(e) {
    if (engaged) return;                       // overlay handles it natively
    if (e.deltaY >= 0) return;                 // downward wheel stays in LIVE
    const pane = deps.getActivePane();
    if (!canOpen(pane)) return;                // alt screen etc: leave it to xterm
    if (pane.term._core.coreMouseService.areMouseEventsActive) return;

    e.preventDefault();
    e.stopPropagation();                       // keep xterm's wheel handler out of it
    const px = e.deltaMode === 1 ? e.deltaY * rowH
             : e.deltaMode === 2 ? e.deltaY * scroller.clientHeight
             : e.deltaY;
    open(px);
  }

  function onOverlayTouchStart(e) {
    if (!attachedPane) return;
    touching = true;
    touchStartY = e.touches[0].clientY;
    touchStartAt = Date.now();
    touchMoved = false;
    if (bottomResetTimer) { clearTimeout(bottomResetTimer); bottomResetTimer = null; }
    // Built here, before the pan has moved a pixel — never from onScroll, where
    // it would cost a frame at the very start of the gesture.
    if (buildStart >= buildEnd) materialise(attachedPane);
  }

  function onOverlayTouchMove(e) {
    if (Math.abs(e.touches[0].clientY - touchStartY) > TAP_MOVE_PX) touchMoved = true;
  }

  function onOverlayTouchEnd(e) {
    touching = false;
    if (!attachedPane) return;
    if (!touchMoved && Date.now() - touchStartAt < TAP_MAX_MS && atBottom()) {
      // A tap on the transparent armed surface means "focus the terminal".
      // The synthesized click would otherwise move focus to the overlay's
      // (unfocusable) scroll div and blur the textarea we are about to focus,
      // so the tap's default action is suppressed. touchend is not a
      // scroll-blocking event, so a non-passive listener costs nothing here.
      if (e.cancelable) e.preventDefault();
      close();
      deps.focusPane();
      return;
    }
    scheduleBottomReset();
  }

  // ─── Init ──────────────────────────────────────────────────────────────────

  function init(options) {
    deps = Object.assign(deps, options || {});

    root     = document.getElementById('hist');
    scroller = document.getElementById('hist-scroll');
    pad      = document.getElementById('hist-pad');
    rowsEl   = document.getElementById('hist-rows');
    tailEl   = document.getElementById('hist-tail');
    exitBtn  = document.getElementById('hist-exit');
    if (!root || !scroller || !pad || !rowsEl || !tailEl) return;

    root.hidden = true;
    if (root.parentElement) root.parentElement.removeChild(root);

    scroller.addEventListener('scroll', onScroll, { passive: true });
    scroller.addEventListener('touchstart', onOverlayTouchStart, { passive: true });
    scroller.addEventListener('touchmove', onOverlayTouchMove, { passive: true });
    scroller.addEventListener('touchend', onOverlayTouchEnd, { passive: false });
    // The pane already is the active one; suppress the pane-wrap handler so a
    // selection drag inside the overlay is not interrupted by a refocus.
    root.addEventListener('mousedown', (e) => { if (engaged) e.stopPropagation(); });

    if (exitBtn) {
      exitBtn.addEventListener('click', () => {
        close();
        deps.focusPane();
      });
    }

    // Capture phase: this runs before xterm's own wheel listener on .xterm.
    const area = document.getElementById('pane-area');
    if (area) area.addEventListener('wheel', onPaneWheel, { capture: true, passive: false });
  }

  window.HistoryOverlay = {
    init,
    open,
    close,
    detach,
    sync,
    noteOutput,
    scrollByPages,
    visibleText,
    isEngaged: () => engaged,
  };
})();
