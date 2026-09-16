# web-tmux

A lightweight web frontend for [tmux](https://github.com/tmux/tmux). Access and control your tmux sessions from a browser — including mobile — on the same machine or through Tailscale Serve.

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
- A modern browser (Chrome, Safari, Firefox)

`setup.sh` handles the Python environment automatically. If [uv](https://github.com/astral-sh/uv) is already installed, it runs `uv sync --frozen` from `uv.lock`. Otherwise, it uses an existing Python 3.10+ installation with `venv` + `pip`. The script never downloads uv itself or executes `curl | sh`.

The browser terminal assets are vendored under `static/vendor/`, so the app does
not need CDN access at runtime.

## Installation

### macOS

```bash
brew install tmux
git clone https://github.com/solab-tut/web-tmux.git
cd web-tmux
./setup.sh    # creates .venv and installs dependencies
cp .web-tmux.env.example .web-tmux.env && chmod 600 .web-tmux.env
echo "WEB_TMUX_ACCESS_TOKEN=$(openssl rand -hex 32)" >> .web-tmux.env
./server.sh   # start the server
```

### Linux (Ubuntu 22.04 / 24.04)

```bash
sudo apt install tmux
git clone https://github.com/solab-tut/web-tmux.git
cd web-tmux
./setup.sh    # creates .venv and installs dependencies
cp .web-tmux.env.example .web-tmux.env && chmod 600 .web-tmux.env
echo "WEB_TMUX_ACCESS_TOKEN=$(openssl rand -hex 32)" >> .web-tmux.env
./server.sh   # start the server
```

### Linux (Ubuntu 20.04)

Ubuntu 20.04 ships Python 3.8 by default. Install Python 3.10+ first, or install uv separately using its [official instructions](https://docs.astral.sh/uv/getting-started/installation/). To install Python manually:

```bash
sudo add-apt-repository ppa:deadsnakes/ppa
sudo apt update
sudo apt install tmux python3.10 python3.10-venv
git clone https://github.com/solab-tut/web-tmux.git
cd web-tmux
./setup.sh
cp .web-tmux.env.example .web-tmux.env && chmod 600 .web-tmux.env
echo "WEB_TMUX_ACCESS_TOKEN=$(openssl rand -hex 32)" >> .web-tmux.env
./server.sh
```

Open **http://127.0.0.1:8766/** in your browser. The first visit shows an access-token prompt — see [Access token](#access-token) below.

### Configuration

**Custom tmux session name** (default: `web`):

```bash
TMUX_SESSION=my-session ./server.sh
```

**Start / stop:**

```bash
./server.sh        # start (or restart if already running)
./server.sh start  # same as above
./server.sh stop   # stop without restarting
```

`server.sh` only stops its own process after the PID file, working directory, and command line have been verified. If the PID belongs to another process or ports 8765/8766 are occupied, startup aborts without killing anything.

### Access token

`WEB_TMUX_ACCESS_TOKEN` is required to start the server, even for local-only use — binding to `127.0.0.1` keeps out other hosts, but not other Unix users on the same machine. The quickstart above generates one automatically; to create or replace it manually:

```bash
cp .web-tmux.env.example .web-tmux.env   # skip if the file already exists
chmod 600 .web-tmux.env
echo "WEB_TMUX_ACCESS_TOKEN=$(openssl rand -hex 32)" >> .web-tmux.env
./server.sh stop && ./server.sh start    # restart to pick up the new token
```

- The token must be at least 32 characters; the server refuses to start otherwise.
- On first visit — or after any restart, which invalidates existing cookies — the browser shows an access-token prompt. Enter the value from `.web-tmux.env`; the browser then holds an HttpOnly session cookie for eight hours and never stores the token itself.
- Running web-tmux on more than one machine? Generate a **separate token per machine** rather than reusing one. A shared token means a leak on any single machine compromises all of them; a unique token per machine limits a leak to that machine and lets you rotate just that one.
- To revoke access immediately (e.g. a suspected leak), replace the token and restart the server. The restart also invalidates every existing session cookie, since the HMAC signing key is regenerated each time the process starts.

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

[Tailscale Serve](https://tailscale.com/kb/1312/serve) exposes web-tmux to your Tailnet over HTTPS. web-tmux validates the exact browser origin and the `Tailscale-User-Login` identity header before issuing a short-lived session cookie.

### How it works

web-tmux listens on two local ports:

| Port | Purpose |
|------|---------|
| 8766 | Static files (HTTP) |
| 8765 | WebSocket terminal I/O |

Both need to be exposed via `tailscale serve`. The browser automatically upgrades the WebSocket connection to `wss://` when the page is served over HTTPS.

### Setup

If `.web-tmux.env` doesn't exist yet, create it first (see [Access token](#access-token) — its 600 permissions and `WEB_TMUX_ACCESS_TOKEN` are required regardless of Tailscale use). Then edit it to add your Tailnet HTTPS origin and Tailscale login:

```dotenv
WEB_TMUX_ALLOWED_ORIGINS=http://127.0.0.1:8766,http://localhost:8766,https://<machine>.<tailnet>.ts.net:8766
WEB_TMUX_TAILSCALE_USERS=you@example.com
```

`server.sh` refuses to load `.web-tmux.env` unless its mode is exactly 600. Non-local origins are rejected unless their Tailscale login is explicitly listed; `http://127.0.0.1:8766` and `http://localhost:8766` are allowed by default without listing `WEB_TMUX_ALLOWED_ORIGINS`.

| Variable | Meaning |
|----------|---------|
| `WEB_TMUX_ALLOWED_ORIGINS` | Comma-separated exact-match list of allowed HTTP origins |
| `WEB_TMUX_TAILSCALE_USERS` | Comma-separated exact-match list of allowed `Tailscale-User-Login` values |
| `WEB_TMUX_ACCESS_TOKEN` | Shared secret (min 32 characters) required to obtain a session cookie via `POST /auth/session` — see [Access token](#access-token) |

Restart the server after editing `.web-tmux.env`:

```bash
./server.sh stop && ./server.sh start
```

Then expose both ports via `tailscale serve`:

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

**Do not enable Funnel for web-tmux.** The application is designed for a private, owner-only Tailnet. Internet exposure and multi-user authorization are outside its security model.

## Security notes

- The server binds to `127.0.0.1` only; remote access must go through Tailscale Serve. Loopback binding is a host boundary, not a user boundary — any local Unix user can open a TCP connection to it, so `POST /auth/session` also requires the shared secret below before issuing a session.
- HTTP and WebSocket requests require an allowed Host/origin. Tailnet requests also require an allowed `Tailscale-User-Login`.
- `POST /auth/session` issues an HttpOnly, SameSite=Strict, HMAC-signed cookie, but only when the JSON body's `token` field matches `WEB_TMUX_ACCESS_TOKEN` from `.web-tmux.env` (a random secret of at least 32 characters, e.g. `openssl rand -hex 32`; the server refuses to start without one). `GET /auth/session` always returns 405. The cookie expires after eight hours or a server restart. Tailnet HTTPS cookies also carry the Secure attribute. Failed attempts are rate-limited.
- Missing origins, mismatched hosts, invalid cookies, wrong access tokens, and unauthorized users are rejected before tmux state is generated. Tagged Tailscale devices without an identity header cannot connect, and a local Unix user without the access token cannot obtain a session even from `127.0.0.1`.
- WebSocket traffic is limited to eight connections, 64 KiB per message, and 8 KiB per input message, with compression disabled. Control rate, input rate, and outbound queues are also bounded.
- HTTP responses include CSP, frame-embedding protection, MIME-sniffing protection, Referrer Policy, and a restricted Permissions Policy.
- Invalid origins, identities, messages, and rate-limit violations are recorded in `server.log` without terminal input, cookie values, or access tokens.
- Keep `tailscale serve status` limited to the two tailnet-only listeners shown above.

## Third-party browser assets

- `xterm@5.3.0`
- `xterm-addon-fit@0.8.0`
- `xterm-addon-unicode11@0.4.0`

These assets are distributed under the MIT license. Their license files are
included next to the vendored files in `static/vendor/`.

## File layout

```
web-tmux/
├── server.py             # HTTP + WebSocket server
├── web_security.py       # Origin, Host, identity, and signed-cookie checks
├── tmux_control.py       # tmux -CC control-mode wrapper
├── layout_parser.py      # tmux layout string parser
├── test_security.py      # Authentication, validation, and limit tests
├── test_server.py        # POST /auth/session HTTP handler tests
├── pyproject.toml        # Python project and pinned dependency
├── uv.lock               # Lock file for uv
├── requirements.txt      # Pinned dependency for venv + pip
├── .web-tmux.env.example # Tailnet configuration example
├── setup.sh              # One-time environment setup (creates .venv)
├── server.sh             # Start / stop the server
└── static/
    ├── index.html
    ├── style.css
    ├── app.js
    └── vendor/          # vendored xterm.js runtime assets and licenses
```

## Tests

After running `setup.sh`, run the test suite with the project virtual environment. Importing `server` requires `WEB_TMUX_ACCESS_TOKEN` to be set (the module-level `AccessController` fails closed without it), so provide a throwaway value:

```bash
WEB_TMUX_ACCESS_TOKEN=$(openssl rand -hex 32) .venv/bin/python -m unittest -v
```

## Logs

```bash
tail -f server.log
```

`server.log` is recreated at startup with mode 600. Rejection reasons are logged, but terminal contents, input data, and cookie values are not.
