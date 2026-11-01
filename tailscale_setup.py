"""What Tailscale knows about this machine, for setup.sh and server.sh.

  python tailscale_setup.py setup [--yes]   write .web-tmux.env from `tailscale
                                            status`, then see to `tailscale serve`
  python tailscale_setup.py check-serve ORIGINS
                                            warn about `tailscale serve` only

Nothing here needs root: reading `tailscale status` and `tailscale serve
status` works as any user. Only changing what `tailscale serve` forwards may
need sudo, and setup asks before it runs that.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass
from urllib.parse import urlsplit

PORT = 8766
OLD_WS_PORT = 8765   # web-tmux before the WebSocket moved onto PORT
CONFIG = '.web-tmux.env'
EXAMPLE = '.web-tmux.env.example'
ADMIN_DNS = 'https://login.tailscale.com/admin/dns'
# The Mac App Store and standalone apps ship the CLI inside the app, off PATH.
MAC_APP_CLI = '/Applications/Tailscale.app/Contents/MacOS/Tailscale'

# .web-tmux.env is sourced by bash, so what goes into it must not be able to
# say anything to bash: a tailnet name, and a login such as you@example.com,
# you@github or you@passkey.
_DNS_NAME_RE = re.compile(r'[a-z0-9]([a-z0-9-]*[a-z0-9])?(\.[a-z0-9]([a-z0-9-]*[a-z0-9])?)+')
_LOGIN_RE = re.compile(r'[A-Za-z0-9._%+-]+(@[A-Za-z0-9.-]+)?')


class SetupError(Exception):
    """Something the person running setup has to fix; the message says how."""


@dataclass(frozen=True)
class Machine:
    dns_name: str
    login: str | None   # None for a tagged machine, which belongs to no one

    @property
    def origin(self) -> str:
        return f'https://{self.dns_name}:{PORT}'


def sudo_prefix() -> list[str]:
    # Linux needs root to change tailscaled's config unless the user was made
    # its operator; the macOS apps don't.
    return ['sudo'] if sys.platform.startswith('linux') else []


def shown(args: list[str]) -> str:
    return ' '.join([*sudo_prefix(), 'tailscale', *args])


def find_cli() -> str | None:
    override = os.environ.get('TAILSCALE')
    if override:
        return override
    found = shutil.which('tailscale')
    if found:
        return found
    return MAC_APP_CLI if os.access(MAC_APP_CLI, os.X_OK) else None


def read_json(cli: str, *args: str) -> dict | None:
    """`tailscale <args> --json`, or None if it can't be had: no tailscaled,
    or a CLI that answers differently."""
    try:
        result = subprocess.run([cli, *args, '--json'], capture_output=True, text=True,
                                timeout=10, stdin=subprocess.DEVNULL)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    try:
        data = json.loads(result.stdout or '{}')
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


def machine(status: dict) -> Machine:
    state = status.get('BackendState')
    if state != 'Running':
        raise SetupError(
            f'Tailscale is not connected on this machine (state: {state or "unknown"}).\n'
            f'Connect it with `{shown(["up"])}`, then run ./setup.sh again.')
    me = status.get('Self') or {}
    dns_name = (me.get('DNSName') or '').rstrip('.').lower()
    if not dns_name:
        raise SetupError(
            'This machine has no tailnet name, so it cannot have an HTTPS address.\n'
            f'Turn on MagicDNS at {ADMIN_DNS}, then run ./setup.sh again.')
    if not _DNS_NAME_RE.fullmatch(dns_name):
        raise SetupError(f'Unexpected tailnet name {dns_name!r}; set up .web-tmux.env by hand (see the README).')
    if dns_name not in [str(name).rstrip('.').lower() for name in status.get('CertDomains') or []]:
        raise SetupError(
            'HTTPS certificates are off for this tailnet, and tailscale serve needs them.\n'
            f'Turn on "HTTPS Certificates" at {ADMIN_DNS}, then run ./setup.sh again.')
    login = None
    if not me.get('Tags'):
        user = (status.get('User') or {}).get(str(me.get('UserID'))) or {}
        login = user.get('LoginName') or None
    return Machine(dns_name, login)


def valid_login(login: str) -> bool:
    return bool(_LOGIN_RE.fullmatch(login))


def config_text(example: str, origin: str, login: str) -> str:
    """The example config with this machine's values filled in, keeping its
    comments (the optional ssh hosts)."""
    lines = []
    for line in example.splitlines():
        if line.startswith('WEB_TMUX_ALLOWED_ORIGINS='):
            line = f'WEB_TMUX_ALLOWED_ORIGINS={origin}'
        elif line.startswith('WEB_TMUX_TAILSCALE_USERS='):
            line = f'WEB_TMUX_TAILSCALE_USERS={login}'
        lines.append(line)
    return '# Written by setup.sh from `tailscale status`.\n' + '\n'.join(lines) + '\n'


def write_config(path: str, text: str) -> None:
    # O_EXCL: never overwrite a config someone wrote meanwhile. Mode 600 from
    # the start, as server.sh refuses anything else.
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, 'w') as stream:
        stream.write(text)
    os.chmod(path, 0o600)


def config_origins(path: str) -> list[str]:
    origins: list[str] = []
    with open(path) as stream:
        for line in stream:
            name, sep, value = line.strip().partition('=')
            if sep and name == 'WEB_TMUX_ALLOWED_ORIGINS':
                origins = [o.strip() for o in value.strip().strip('"\'').split(',') if o.strip()]
    return origins


def _proxy(web: dict, host: str) -> str | None:
    handler = ((web.get(host) or {}).get('Handlers') or {}).get('/') or {}
    return handler.get('Proxy')


def serve_problems(serve_status: dict, origins: list[str]) -> list[tuple[str, list[str]]]:
    """What `tailscale serve` gets wrong for web-tmux, each with the tailscale
    arguments that put it right."""
    web = serve_status.get('Web') or {}
    target = f'http://127.0.0.1:{PORT}'
    problems = []
    for origin in origins:
        parts = urlsplit(origin)
        if parts.scheme != 'https':
            continue
        if _proxy(web, parts.netloc) != target:
            problems.append((f'tailscale serve does not forward {origin}/ to web-tmux.',
                             ['serve', '--bg', f'--https={parts.port}', target]))
    old_target = f'http://127.0.0.1:{OLD_WS_PORT}'
    if any(host.endswith(f':{OLD_WS_PORT}') and _proxy(web, host) == old_target for host in web):
        problems.append((f'web-tmux no longer uses port {OLD_WS_PORT}, but tailscale serve still forwards it.',
                         ['serve', f'--https={OLD_WS_PORT}', 'off']))
    return problems


def check_serve(origins: str) -> int:
    """server.sh's check at start: warn, never stop."""
    cli = find_cli()
    serve = read_json(cli, 'serve', 'status') if cli else None
    if serve is None:
        return 0
    wanted = [o.strip() for o in origins.split(',') if o.strip()]
    for message, args in serve_problems(serve, wanted):
        print(f'WARNING: {message} Run:\n  {shown(args)}', file=sys.stderr)
    return 0


def _confirm(question: str, default: bool, assume_yes: bool) -> bool:
    if assume_yes:
        return True
    if not sys.stdin.isatty():
        return False
    try:
        answer = input(f'{question} [{"Y/n" if default else "y/N"}] ').strip().lower()
    except EOFError:
        return False
    return default if not answer else answer in ('y', 'yes')


def setup(assume_yes: bool = False) -> int:
    cli = find_cli()
    if cli is None:
        print('The Tailscale CLI was not found. Install Tailscale (https://tailscale.com/download),\n'
              'connect this machine to your tailnet, then run ./setup.sh again.')
        return 1

    if os.path.exists(CONFIG):
        print(f'Using the existing {CONFIG}.')
        if os.stat(CONFIG).st_mode & 0o777 != 0o600:
            print(f'WARNING: server.sh only reads {CONFIG} at mode 600: chmod 600 {CONFIG}')
        origins = config_origins(CONFIG)
    else:
        status = read_json(cli, 'status')
        if status is None:
            print('Could not read `tailscale status`. Is Tailscale running? Start it, then run ./setup.sh again.')
            return 1
        try:
            this = machine(status)
        except SetupError as exc:
            print(exc)
            return 1
        login = this.login
        if login is None:
            # A tagged machine: whoever will use the browser can't be read off it.
            if not sys.stdin.isatty():
                print('This machine is tagged in Tailscale, so the login to allow cannot be read off it.\n'
                      f'Run ./setup.sh in a terminal, or write {CONFIG} by hand (see the README).')
                return 1
            login = input('Tailscale login that may use web-tmux (e.g. you@example.com): ').strip()
        if not valid_login(login):
            print(f'Unexpected Tailscale login {login!r}; write {CONFIG} by hand (see the README).')
            return 1
        print(f'This machine is {this.dns_name} on your tailnet.\n'
              f'web-tmux will answer at {this.origin}/ and only to {login}.')
        if not _confirm(f'Write {CONFIG}?', True, assume_yes):
            print(f'Nothing written. Run ./setup.sh again, or copy {EXAMPLE} to {CONFIG} (mode 600) and fill it in.')
            return 1
        with open(EXAMPLE) as stream:
            write_config(CONFIG, config_text(stream.read(), this.origin, login))
        print(f'Wrote {CONFIG}.')
        origins = [this.origin]

    serve = read_json(cli, 'serve', 'status')
    if serve is None:
        print('Could not read `tailscale serve status`; check it once Tailscale is running.')
    else:
        for message, args in serve_problems(serve, origins):
            print(f'{message}\n  {shown(args)}')
            if _confirm('Run it now?', False, assume_yes):
                try:
                    ok = subprocess.run([*sudo_prefix(), cli, *args]).returncode == 0
                except OSError:   # no sudo on this machine, say
                    ok = False
                if not ok:
                    print('That did not work; run the command above yourself.')

    print('\nNext: start web-tmux with ./server.sh')
    for origin in origins:
        print(f'then open {origin}/ in a browser on your tailnet.')
    return 0


def main(argv: list[str]) -> int:
    if argv[:1] == ['setup'] and set(argv[1:]) <= {'--yes'}:
        return setup(assume_yes='--yes' in argv[1:])
    if argv[:1] == ['check-serve'] and len(argv) == 2:
        return check_serve(argv[1])
    print(__doc__, file=sys.stderr)
    return 2


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
