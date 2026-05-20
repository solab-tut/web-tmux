# web-tmux

A lightweight web frontend for [tmux](https://github.com/tmux/tmux). Access and control your tmux sessions from any browser — including mobile — over a local network or securely via Tailscale.

[日本語](README.ja.md)

```
Browser (xterm.js)  ←─WebSocket─→  server.py  ←─PTY─→  tmux -CC
```

## Features

- Sidebar showing sessions, windows, and panes with live updates
- Switch, rename, and delete sessions and windows inline
- Create new windows, sessions, and splits (horizontal / vertical)
- Pane zoom: click a pane in the sidebar to zoom in; click again to return to split view
- Terminal automatically resizes to match the browser viewport
- Color themes: Dark (default), Light, Nord
- Adjustable font size (11–18 px), persisted in `localStorage`
- **Mobile-friendly:** fullscreen single-pane view, virtual keyboard (Esc / Ctrl / Tab / Enter / arrows), clipboard sheet, scroll buttons, IME support

## Requirements

- Python 3.10 or later
- tmux
- [`websockets`](https://pypi.org/project/websockets/) Python package
- A modern browser (Chrome, Safari, Firefox)
- Internet access to load xterm.js from jsDelivr CDN

## Installation

### macOS

```bash
# Install dependencies if needed
brew install tmux

# Clone the repository
git clone https://github.com/solab-tut/web-tmux.git
cd web-tmux

# Install the Python dependency
python3 -m pip install websockets

# Start the server
./start.sh
```

### Linux (Debian / Ubuntu)

```bash
# Install dependencies
sudo apt install tmux python3 python3-pip

# Clone the repository
git clone https://github.com/solab-tut/web-tmux.git
cd web-tmux

# Install the Python dependency
python3 -m pip install websockets

# Start the server
./start.sh
```

Open **http://127.0.0.1:8766/** in your browser.

### Configuration

**Custom tmux session name** (default: `web`):

```bash
TMUX_SESSION=my-session ./start.sh
```

`start.sh` stops any existing server process before starting, so re-running it is always safe.

## Usage

### Sidebar

| Section | What you can do |
|---------|-----------------|
| **Sessions** | Click to switch; ✏ to rename; 🗑 to delete |
| **Windows** | Click to switch (zoomed windows unzoom automatically) |
| **Panes** | Click active pane → toggle zoom on/off; click another pane → zoom to it |
| **+** (windows row) | New window in current session |
| **+** (sessions row) | New session |
| **⇿ / ⇕** | Split active pane horizontally / vertically |

### Theme and font size

Click the **◑** (theme) or **Aa** (font size) icons in the top-right corner to open a dropdown. Both settings are saved in `localStorage` and restored on the next visit.

| Theme | Description |
|-------|-------------|
| Dark  | VS Code Dark-inspired dark theme (default) |
| Light | Light background theme |
| Nord  | Nordic colour palette dark theme |

Font size options: 11, 12, 13, 14, 16, 18 px.

### Mobile

On screens ≤ 768 px wide:

- Only the active pane is shown fullscreen
- Tap the **☰** button to open / close the sidebar
- **Bottom toolbar** — virtual keys: `Esc`, `Ctrl`, `Tab`, `Enter`, arrow keys
  - `Ctrl` toggle applies a Control modifier to the next keystroke
- **Top-right buttons** — half-page scroll up/down, clipboard sheet (copy viewport text / paste text to terminal)

## Remote access with Tailscale

[Tailscale Serve](https://tailscale.com/kb/1312/serve) exposes web-tmux to your Tailnet over HTTPS with no extra authentication setup — Tailscale device authentication acts as the access layer.

### How it works

web-tmux listens on two local ports:

| Port | Purpose |
|------|---------|
| 8766 | Static files (HTTP) |
| 8765 | WebSocket terminal I/O |

Both need to be exposed via `tailscale serve`. The browser automatically upgrades the WebSocket connection to `wss://` when the page is served over HTTPS.

### Setup

```bash
tailscale serve --bg --https=8766 http://127.0.0.1:8766
tailscale serve --bg --https=8765 http://127.0.0.1:8765
```

> **Linux:** `tailscale serve` requires `sudo`. On macOS it typically does not.

Access the app at:

```
https://<machine-name>.<tailnet>.ts.net:8766/
```

### Verify

```bash
tailscale serve status
```

Expected output:

```
https://<machine-name>.<tailnet>.ts.net:8765/ (tailnet only)
|-- / proxy http://127.0.0.1:8765

https://<machine-name>.<tailnet>.ts.net:8766/ (tailnet only)
|-- / proxy http://127.0.0.1:8766
```

### Stop

```bash
tailscale serve --https=8766 off
tailscale serve --https=8765 off
```

> **Linux:** `sudo` is required here as well.

### Public access (Tailscale Funnel)

> **Note:** This section is untested.

To access web-tmux from outside your Tailnet, use `tailscale funnel` instead of `serve`. Because web-tmux has **no built-in authentication**, place a reverse proxy with HTTP Basic Auth (or equivalent) in front of it before enabling Funnel.

## Security notes

- The server binds to `127.0.0.1` only and is not directly reachable from the network.
- **There is no built-in authentication.** For any remote access, use Tailscale Serve (Tailnet-scoped) or a reverse proxy with authentication.
- The WebSocket URL switches automatically between `ws://` (HTTP) and `wss://` (HTTPS).

## File layout

```
web-tmux/
├── server.py          # HTTP + WebSocket server
├── tmux_control.py    # tmux -CC control-mode wrapper
├── layout_parser.py   # tmux layout string parser
├── start.sh           # Startup / restart script
└── static/
    ├── index.html
    ├── style.css
    └── app.js
```

## Logs

```bash
tail -f server.log
```
