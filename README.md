# web-tmux

A lightweight web frontend for [tmux](https://github.com/tmux/tmux). Access and control your tmux sessions from a browser — including mobile — through Tailscale Serve.

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
- Save every open session's shape (window names, split layout, each pane's working directory) to a named layout and restore it later
- **Mobile-friendly:** fullscreen single-pane view, virtual keyboard (Esc / Ctrl / Tab / Enter / arrows), IME support

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
# Edit the Tailnet HTTPS origin and Tailscale login in .web-tmux.env
./server.sh   # start the server
```

### Linux (Ubuntu 22.04 / 24.04)

```bash
sudo apt install tmux
git clone https://github.com/solab-tut/web-tmux.git
cd web-tmux
./setup.sh    # creates .venv and installs dependencies
cp .web-tmux.env.example .web-tmux.env && chmod 600 .web-tmux.env
# Edit the Tailnet HTTPS origin and Tailscale login in .web-tmux.env
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
# Edit the Tailnet HTTPS origin and Tailscale login in .web-tmux.env
./server.sh
```

Complete the [Tailscale setup](#remote-access-with-tailscale), then open the Tailnet HTTPS URL in your browser. Direct browser access through localhost is intentionally disabled.

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
| **Layouts** | Click to restore a saved layout; 🗑 to delete it |
| **+** (layouts row) | Save every currently open session under one named layout |

A layout is a snapshot of your whole workspace — every session that was open
when you saved it, each with its window names, how its panes are split, and
each pane's working directory. Commands that were running and what was on
screen are not saved; restoring gives you the same sessions and frames back,
with fresh shells in them. A working directory that no longer exists falls
back to `$HOME`.

Restoring recreates each session under the name it had when it was saved. If
any of those names are already running, you are asked whether to **Replace**
them, restore them **as copies** alongside the existing ones under free
names, or **Cancel** — the choice applies to all of the conflicting sessions
at once. Sessions that don't conflict are restored either way. Each session
is built under a throwaway name first, so a failure restoring one of them
never disturbs the others or your existing sessions; a note in the sidebar
says if any session couldn't be restored.

Layouts live in `~/.local/share/web-tmux/layouts.json` (see
[Security notes](#security-notes)).

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
  - **Paste** opens a paste sheet. Long-press its text area, paste, then tap **Send to terminal**

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

Create `.web-tmux.env`, keep its mode at 600, and configure only your Tailnet HTTPS origin and Tailscale login:

```dotenv
WEB_TMUX_ALLOWED_ORIGINS=https://<machine>.<tailnet>.ts.net:8766
WEB_TMUX_TAILSCALE_USERS=you@example.com
```

`server.sh` refuses to load `.web-tmux.env` unless its mode is exactly 600. The server also fails closed when the origin list is empty or a remote origin has no allowed Tailscale login. Do not add localhost or `127.0.0.1`: browser access is intended to go through Tailscale Serve only.

| Variable | Meaning |
|----------|---------|
| `WEB_TMUX_ALLOWED_ORIGINS` | Comma-separated exact-match list of allowed HTTP origins |
| `WEB_TMUX_TAILSCALE_USERS` | Comma-separated exact-match list of allowed `Tailscale-User-Login` values |

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

- The server binds to `127.0.0.1` only, and the allowed origin configuration excludes localhost, so browser access must go through Tailscale Serve. Loopback binding is a host boundary, not a user boundary: another local Unix user can forge proxy headers, so this deployment model assumes a personal machine without untrusted local users.
- HTTP and WebSocket requests require an allowed Host/origin. Tailnet requests also require an allowed `Tailscale-User-Login`.
- After validating the Tailscale identity, `GET /auth/session` automatically issues an HttpOnly, SameSite=Strict, HMAC-signed cookie. The browser refreshes it before every WebSocket connection, including reconnects after mobile suspension or a server restart. Tailnet HTTPS cookies also carry the Secure attribute.
- Missing origins, mismatched hosts, invalid cookies, and unauthorized users are rejected before tmux state is generated. Tagged Tailscale devices without an identity header cannot connect.
- WebSocket traffic is limited to eight connections, 64 KiB per message, and 8 KiB per input message, with compression disabled. Control rate, input rate, and outbound queues are also bounded.
- HTTP responses include CSP, frame-embedding protection, MIME-sniffing protection, Referrer Policy, and a restricted Permissions Policy.
- Invalid origins, identities, messages, and rate-limit violations are recorded in `server.log` without terminal input or cookie values.
- Saved layouts are written to `~/.local/share/web-tmux/layouts.json` with mode 600 inside a mode 700 directory. They record working-directory paths, so treat the file as private; it never leaves the machine and is not part of the repository.
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
├── layout_store.py       # Saved session layouts: capture, restore plan, store
├── test_security.py      # Authentication, validation, and limit tests
├── test_server.py        # Session HTTP handler and layout restore tests
├── test_layout_store.py  # Layout capture, ordering, planning, and store tests
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

Saved layouts live outside the repository, in
`~/.local/share/web-tmux/layouts.json` (override the directory with
`WEB_TMUX_STATE_DIR`).

## Tests

After running `setup.sh`, run the test suite with the project virtual environment:

```bash
.venv/bin/python -m unittest -v
```

## Logs

```bash
tail -f server.log
```

`server.log` is recreated at startup with mode 600. Rejection reasons are logged, but terminal contents, input data, and cookie values are not.
