#!/usr/bin/env python3
"""
Where tmux runs: this machine, or another one reached over ssh.

A remote host is an ssh_config alias named in WEB_TMUX_SSH_HOSTS. How to get
there (HostName, User, ProxyJump through a tailnet node for a LAN-only box,
IdentityFile) lives in the server user's ~/.ssh/config, never in the browser:
the browser can only pick one of the configured aliases.

Every tmux invocation goes through TmuxHost.argv(), so the rest of the code
runs the same tmux commands whether the server is local or remote. Remote
commands share one ssh connection per host (ControlMaster), started
explicitly by ensure_master() — see there for why it is not left to
ControlMaster=auto.

HostDirectory keeps a cached, never-blocking view of every host's sessions
for the sidebar; a dead host costs a background timeout, not a stalled UI.

A host whose key has a passphrase, or that wants a password, is signed in to
interactively: ensure_master(host, ask=...) starts the shared connection with
BatchMode off and ssh's prompts routed to the browser (see askpass.py). Every
other ssh call stays in BatchMode and rides that connection.
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import re
import shlex
import signal
import time
from dataclasses import dataclass, field
from typing import Callable, Iterable, Mapping

log = logging.getLogger(__name__)

LOCAL = 'local'

# An ssh_config alias only: no options ('-'), no user@ or :port, no spaces.
ALIAS_RE = re.compile(r'^[A-Za-z0-9][A-Za-z0-9._-]{0,62}$')

# %paste-buffer-changed, refresh-client -C WxH and the formats the server
# reads all exist from 3.2. send-keys -K (prefix routing) needs 3.4; older
# servers get the prefix key passed through literally instead.
MIN_TMUX = (3, 2)
PREFIX_ROUTING_TMUX = (3, 4)

CAPTURE_LIMIT = 256 * 1024
STDERR_LIMIT = 2 * 1024
LOCAL_TIMEOUT = 5.0
# ConnectTimeout counts per hop, and a LAN host behind ProxyJump has two.
REMOTE_TIMEOUT = 12.0
MASTER_TIMEOUT = 15.0
# A sign-in waits on a person typing, possibly once per jump hop.
AUTH_MASTER_TIMEOUT = 300.0
CONTROL_PERSIST = '10m'
# A connection someone typed a password for is kept much longer, so leaving
# the browser for lunch doesn't mean typing it again.
AUTH_CONTROL_PERSIST = '8h'

# A unix socket path is limited to 108 bytes (104 on macOS); %C expands to
# 40 hex characters.
_SOCKET_PATH_MAX = 100
_SOCKET_NAME = 'ssh-%C'
_SOCKET_NAME_EXPANDED = len('ssh-') + 40

# rc for "ssh could not run the command": its own transport failures, and our
# timeouts, which are reported the same way.
RC_UNREACHABLE = 255


@dataclass(frozen=True)
class TmuxHost:
    name: str                  # LOCAL or an ssh_config alias
    tmux_bin: str = 'tmux'
    control_dir: str = ''      # where the ControlMaster sockets live

    @property
    def is_local(self) -> bool:
        return self.name == LOCAL

    @property
    def timeout(self) -> float:
        return LOCAL_TIMEOUT if self.is_local else REMOTE_TIMEOUT

    def _ssh_options(self, master: str = 'no') -> list[str]:
        opts = {
            'BatchMode':             'yes',   # never prompt: a prompt would hang the call
            'ConnectTimeout':        '5',
            'StrictHostKeyChecking': 'yes',   # unknown host keys are refused, not learned
            'ControlMaster':         master,
            'ControlPath':           os.path.join(self.control_dir, _SOCKET_NAME),
            'ServerAliveInterval':   '15',
            'ServerAliveCountMax':   '3',
            'LogLevel':              'ERROR',  # no "Connection closed." in the control PTY
            'ClearAllForwardings':   'yes',
            'ForwardAgent':          'no',
            'ForwardX11':            'no',
            'PermitLocalCommand':    'no',
        }
        argv = ['ssh']
        for key, value in opts.items():
            argv += ['-o', f'{key}={value}']
        return argv

    def _remote(self, words: list[str]) -> str:
        # ssh joins its arguments and hands them to the remote login shell,
        # so the command goes over as one string quoted for a POSIX shell.
        # `exec` keeps no shell sitting between sshd and tmux.
        return 'exec ' + shlex.join(words)

    def argv(self, tmux_args: Iterable[str]) -> list[str]:
        """One tmux command, run to completion."""
        words = [self.tmux_bin, *tmux_args]
        if self.is_local:
            return words
        # `--` goes before the alias: ssh stops reading options at the
        # destination, so after it, `--` would be part of the remote command.
        return [*self._ssh_options(), '-T', '--', self.name, self._remote(words)]

    def control_argv(self, session: str) -> list[str]:
        """The long-lived `tmux -CC` client, run on a PTY."""
        words = [self.tmux_bin, '-CC', 'attach-session', '-t', session]
        if self.is_local:
            return words
        # -u: a non-interactive ssh login often has no UTF-8 locale, and the
        # tmux client would replace non-ASCII text with '_'.
        # -e none: '~.' at the start of a line we write must not be read as
        # an ssh escape.
        words.insert(1, '-u')
        return [*self._ssh_options(), '-tt', '-e', 'none', '--', self.name, self._remote(words)]

    def shell_argv(self, script: str) -> list[str]:
        """A POSIX sh script, for the few checks tmux itself can't answer."""
        if self.is_local:
            return ['sh', '-c', script]
        return [*self._ssh_options(), '-T', '--', self.name, self._remote(['sh', '-c', script])]

    def master_argv(self, interactive: bool = False) -> list[str]:
        argv = self._ssh_options(master='yes')
        if interactive:
            # Let ssh ask; SSH_ASKPASS sends the questions to the browser.
            argv[argv.index('BatchMode=yes')] = 'BatchMode=no'
        persist = AUTH_CONTROL_PERSIST if interactive else CONTROL_PERSIST
        # ControlMaster=yes alone, never with -M as well: ssh counts that as
        # -M given twice, an "ask" master that wants askpass to approve
        # every command sharing the connection — which ours refuses.
        return [*argv, '-o', f'ControlPersist={persist}', '-N', '-f', '--', self.name]

    @property
    def log_path(self) -> str:
        return os.path.join(self.control_dir, f'{self.name}.log')

    def master_ctl_argv(self, command: str) -> list[str]:
        """`ssh -O check|exit` against this host's master socket."""
        return ['ssh', '-o', f'ControlPath={os.path.join(self.control_dir, _SOCKET_NAME)}',
                '-O', command, '--', self.name]


LOCAL_HOST = TmuxHost(LOCAL)


def control_dir() -> str:
    # Imported here so this module has no import-time dependency on the
    # layout store's environment lookups.
    import layout_store
    return os.path.join(layout_store.store_dir(), 'ssh')


def hosts_from_env(environ: Mapping[str, str] | None = None,
                   directory: str | None = None) -> tuple[TmuxHost, ...]:
    """WEB_TMUX_SSH_HOSTS=alias1,alias2:/opt/homebrew/bin/tmux

    Fails closed (ValueError) on anything that isn't plainly an alias, like
    AccessController.from_env: a typo must stop the server, not quietly turn
    into an ssh option.
    """
    environ = os.environ if environ is None else environ
    raw = environ.get('WEB_TMUX_SSH_HOSTS', '').strip()
    if not raw:
        return ()
    directory = control_dir() if directory is None else directory
    if len(directory) + 1 + _SOCKET_NAME_EXPANDED > _SOCKET_PATH_MAX:
        raise ValueError('WEB_TMUX_SSH_HOSTS: the state directory path is too long for ssh sockets')

    hosts: list[TmuxHost] = []
    seen: set[str] = set()
    for item in raw.split(','):
        item = item.strip()
        name, sep, tmux_bin = item.partition(':')
        if not ALIAS_RE.match(name) or name == LOCAL:
            raise ValueError(f'WEB_TMUX_SSH_HOSTS: invalid host alias {name!r}')
        if name in seen:
            raise ValueError(f'WEB_TMUX_SSH_HOSTS: duplicate host {name!r}')
        if sep:
            if (not tmux_bin.startswith('/')
                    or any(ch.isspace() or ord(ch) < 0x20 or ch in '\'"\\' for ch in tmux_bin)):
                raise ValueError(f'WEB_TMUX_SSH_HOSTS: tmux path for {name!r} must be absolute')
        seen.add(name)
        hosts.append(TmuxHost(name, tmux_bin or 'tmux', directory))
    return tuple(hosts)


def ensure_control_dir(hosts: Iterable[TmuxHost]) -> None:
    for directory in {h.control_dir for h in hosts if not h.is_local}:
        os.makedirs(directory, mode=0o700, exist_ok=True)
        os.chmod(directory, 0o700)


# ──────────────────────────────────────────────────────────── askpass

_broker = None   # askpass.AskpassBroker, once the server has started one


def configure_askpass(broker) -> None:
    global _broker
    _broker = broker


def ssh_env(token: str = '') -> dict[str, str] | None:
    """Environment for an ssh process. Without a token its askpass refuses
    every prompt: nothing but an interactive sign-in may ask anyone."""
    if _broker is None:
        return None
    return {**os.environ, **_broker.env(token)}


def tmux_env(environ: Mapping[str, str] | None = None) -> dict[str, str]:
    """Environment for a local tmux: ours, minus web-tmux's own settings.

    The tmux command that finds no server running starts one, and that
    server keeps the environment as its global one for every shell it
    starts. A web-tmux started from such a shell would then pick up the
    settings of whichever web-tmux happened to start tmux — a dev
    worktree's WEB_TMUX_STATE_DIR, say, and with it another layout store."""
    environ = os.environ if environ is None else environ
    return {k: v for k, v in environ.items() if not k.startswith('WEB_TMUX_')}


def process_env(host: TmuxHost) -> dict[str, str] | None:
    return tmux_env() if host.is_local else ssh_env()


# ───────────────────────────────────────────────────────────── pure helpers

def parse_tmux_version(text: str) -> tuple[int, int] | None:
    """`tmux -V` → (major, minor). 'master' and OpenBSD's base tmux track
    the latest release; anything unrecognised is None (unsupported)."""
    text = text.strip()
    m = re.match(r'^tmux (?:next-)?(\d+)\.(\d+)', text)
    if m:
        return int(m.group(1)), int(m.group(2))
    if re.match(r'^tmux (?:master|openbsd-[\d.]+)$', text):
        return (99, 0)
    return None


def version_ok(version: tuple[int, int] | None) -> bool:
    return version is not None and version >= MIN_TMUX


def classify(rc: int, stderr: str) -> str:
    """'ok' | 'unreachable' | 'no_server' | 'no_tmux' | 'error'."""
    if rc == 0:
        return 'ok'
    if rc == RC_UNREACHABLE:
        return 'unreachable'
    if 'no server running' in stderr or 'error connecting to' in stderr:
        return 'no_server'
    if rc == 127:
        return 'no_tmux'
    return 'error'


# ssh's last words when the trouble was credentials, not the network.
_AUTH_FAILURE_RE = re.compile(
    r'Permission denied|Too many authentication failures|Authentication failed'
    r'|incorrect passphrase|bad passphrase', re.IGNORECASE)


def auth_failed(log_text: str) -> bool:
    return bool(_AUTH_FAILURE_RE.search(log_text))


def sanitize(text: str, limit: int = 200) -> str:
    """For logs only: one line, no control characters."""
    return ''.join(ch if ch.isprintable() else ' ' for ch in text[:limit]).strip()


# ──────────────────────────────────────────────────────────────── running

RC_TOO_LARGE = 1   # output went over the limit; the process was stopped


async def run_argv(argv: list[str], *, timeout: float, stdin: bytes | None = None,
                   env: dict[str, str] | None = None,
                   limit: int = CAPTURE_LIMIT) -> tuple[int, str, str]:
    """(rc, stdout, stderr). A timeout or a missing binary reads as rc 255.

    Output is bounded while it is read, not after: stdout past `limit`
    (CAPTURE_LIMIT unless the caller needs more) stops the process
    (rc RC_TOO_LARGE, no output), and
    stderr past STDERR_LIMIT is read and thrown away. A remote host — or a
    huge paste buffer — can't make the server hold more than that.

    The process gets its own session. Stopping it then stops everything it
    started (a ProxyJump ssh, a remote-shell helper) that would otherwise
    keep the pipes open; and with no controlling terminal, ssh can only ask
    through SSH_ASKPASS, never on whatever terminal the server happened to
    be started from."""
    try:
        proc = await asyncio.create_subprocess_exec(
            *argv,
            stdin=asyncio.subprocess.PIPE if stdin is not None else asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
            start_new_session=True,
        )
    except OSError as exc:
        log.warning('could not run %s: %s', argv[0], type(exc).__name__)
        return RC_UNREACHABLE, '', ''

    def stop() -> None:
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.killpg(proc.pid, signal.SIGKILL)

    async def feed() -> None:
        if stdin is None:
            return
        with contextlib.suppress(OSError):   # it may exit (or be stopped) first
            proc.stdin.write(stdin)
            await proc.stdin.drain()
            proc.stdin.close()

    async def collect() -> tuple[bytes, bool]:
        out = await _read_capped(proc.stdout, limit)
        if out is None:
            stop()
            return b'', True
        return out, False

    async def interact():
        _, collected, err = await asyncio.gather(
            feed(), collect(), _read_capped(proc.stderr, STDERR_LIMIT, drain=True))
        await proc.wait()
        return (*collected, err)

    try:
        out, too_large, err = await asyncio.wait_for(interact(), timeout=timeout)
    except asyncio.TimeoutError:
        stop()
        with contextlib.suppress(Exception):
            await proc.wait()
        return RC_UNREACHABLE, '', 'timeout'
    if too_large:
        log.warning('%s produced more than %d bytes of output; stopped it', argv[0], limit)
        return RC_TOO_LARGE, '', 'output too large'
    return (
        proc.returncode or 0,
        out.decode('utf-8', errors='surrogateescape'),
        err.decode('utf-8', errors='replace'),
    )


async def _read_capped(stream, limit: int, *, drain: bool = False) -> bytes | None:
    """Read `stream` to EOF keeping at most `limit` bytes. Past the limit:
    None, or with `drain` the first `limit` bytes once the rest has been
    read and dropped (so the writer never blocks on a full pipe)."""
    buf = bytearray()
    while True:
        chunk = await stream.read(64 * 1024)
        if not chunk:
            return bytes(buf)
        room = limit - len(buf)
        if len(chunk) > room:
            if not drain:
                return None
            buf += chunk[:max(room, 0)]
            continue
        buf += chunk


async def run(host: TmuxHost, *args: str, timeout: float | None = None,
              limit: int = CAPTURE_LIMIT) -> tuple[int, str, str]:
    """One tmux command on `host`."""
    return await run_argv(host.argv(args), timeout=timeout or host.timeout,
                          env=process_env(host), limit=limit)


async def run_shell(host: TmuxHost, script: str, stdin: bytes, *,
                    limit: int = CAPTURE_LIMIT, timeout: float | None = None,
                    ) -> tuple[int, str, str]:
    return await run_argv(host.shell_argv(script), timeout=timeout or host.timeout, stdin=stdin,
                          env=process_env(host), limit=limit)


# host name -> (the task starting its master, whether that start may ask)
_masters: dict[str, tuple[asyncio.Future, bool]] = {}


async def ensure_master(host: TmuxHost, ask=None) -> str:
    """Make sure `host` has a live shared ssh connection: '' when it does,
    else 'unreachable' or 'auth_required'.

    Started on its own with -f and stdio off any pipe. With
    ControlMaster=auto, whichever ordinary call happened to come first would
    become the master and keep its stdout pipe (or the control client's PTY)
    open for the whole ControlPersist time, so that call would never see EOF.
    All other calls use ControlMaster=no: they ride the socket when it is
    there and connect on their own when it isn't.

    With `ask` (an async prompt -> answer|None), passphrase and password
    prompts go to it; without, a host that needs one is 'auth_required'.
    """
    if host.is_local:
        return ''
    entry = _masters.get(host.name)
    if entry is not None and not entry[0].done():
        code = await asyncio.shield(entry[0])
        # A background start found the host wants a password while someone
        # was already asking to sign in: that sign-in still has to happen.
        if code != 'auth_required' or ask is None or entry[1]:
            return code
    interactive = ask is not None and _broker is not None
    task = asyncio.ensure_future(_start_master(host, ask if interactive else None))
    _masters[host.name] = (task, interactive)
    return await asyncio.shield(task)


async def _start_master(host: TmuxHost, ask=None) -> str:
    rc, _, _ = await run_argv(host.master_ctl_argv('check'), timeout=LOCAL_TIMEOUT)
    if rc == 0:
        return ''
    token = _broker.open(ask) if ask is not None else ''
    # ssh's stderr — its own and that of any ProxyJump ssh it starts — goes
    # to a per-host log: the only place that tells "wrong password" from
    # "no route". A file, not a pipe, because the forked master keeps it.
    try:
        fd = os.open(host.log_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    except OSError:
        fd = os.open(os.devnull, os.O_WRONLY)
    try:
        proc = await asyncio.create_subprocess_exec(
            *host.master_argv(interactive=bool(token)),
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=fd,
            env=ssh_env(token),
            start_new_session=True,
        )
    except OSError as exc:
        log.warning('host %s: could not run ssh: %s', host.name, type(exc).__name__)
        if token:
            _broker.close(token)
        return 'unreachable'
    finally:
        os.close(fd)
    try:
        rc = await asyncio.wait_for(proc.wait(),
                                    timeout=AUTH_MASTER_TIMEOUT if token else MASTER_TIMEOUT)
    except asyncio.TimeoutError:
        with contextlib.suppress(ProcessLookupError):
            proc.kill()
        rc = RC_UNREACHABLE
    finally:
        if token:
            _broker.close(token)
    if rc == 0:
        return ''
    code = 'auth_required' if auth_failed(_read_tail(host.log_path)) else 'unreachable'
    log.warning('host %s: ssh connection failed rc=%d (%s; see %s)',
                host.name, rc, code, host.log_path)
    return code


def _read_tail(path: str, size: int = 4096) -> str:
    try:
        with open(path, 'rb') as fh:
            fh.seek(0, os.SEEK_END)
            fh.seek(max(0, fh.tell() - size))
            return fh.read().decode('utf-8', errors='replace')
    except OSError:
        return ''


MASTER_EXIT_TIMEOUT = 2.0


async def stop_master(host: TmuxHost) -> None:
    """Close `host`'s shared connection, and with it every ssh session riding
    on it (the control client, in-flight commands)."""
    if host.is_local:
        return
    entry = _masters.get(host.name)
    if entry is not None and not entry[0].done():
        # A start in flight would bring the connection straight back.
        with contextlib.suppress(Exception):
            await asyncio.wait_for(asyncio.shield(entry[0]), MASTER_TIMEOUT)
    await run_argv(host.master_ctl_argv('exit'), timeout=MASTER_EXIT_TIMEOUT)
    _masters.pop(host.name, None)


async def stop_masters(hosts: Iterable[TmuxHost]) -> None:
    """Close every host's shared connection. In parallel and quick: this runs
    while the server is being asked to stop."""
    remote = [h for h in hosts if not h.is_local]
    await asyncio.gather(*(run_argv(h.master_ctl_argv('exit'), timeout=MASTER_EXIT_TIMEOUT)
                           for h in remote))
    for host in remote:
        _masters.pop(host.name, None)


# ──────────────────────────────────────────────────────── host directory

REACHABLE_TTL = 10.0
UNREACHABLE_BACKOFF = (30.0, 60.0, 120.0, 300.0)
SESSION_FMT = '#{session_name}\t#{session_windows}\t#{session_attached}'


@dataclass
class HostStatus:
    reachable: bool | None = None      # None: not checked yet
    code: str = ''                     # why it isn't usable: unreachable/auth_required/unsupported_tmux/error/disconnected
    version: tuple[int, int] | None = None
    sessions: list[dict] = field(default_factory=list)
    checked_at: float = 0.0
    failures: int = 0


def parse_sessions(raw: str) -> list[dict]:
    sessions = []
    for line in raw.splitlines():
        parts = line.split('\t', 2)
        if len(parts) != 3 or not parts[0]:
            continue
        name, windows, attached = parts
        sessions.append({
            'name':     name,
            'windows':  int(windows) if windows.isdigit() else 0,
            'attached': attached not in ('', '0'),
        })
    return sessions


class HostDirectory:
    """Cached status of every configured host.

    snapshot() never waits. refresh() is single-flight per host, and hosts
    are refreshed in parallel, so one host timing out delays nobody else.

    A host someone disconnected stays 'disconnected', with nothing checked
    and no ssh started for it, until connect() is called for it.
    """

    def __init__(self, hosts: Iterable[TmuxHost], *,
                 on_change: Callable[[], None] | None = None,
                 runner=run, master=ensure_master, clock=time.monotonic) -> None:
        self.hosts: dict[str, TmuxHost] = {h.name: h for h in hosts}
        self.status: dict[str, HostStatus] = {name: HostStatus() for name in self.hosts}
        self.on_change = on_change
        self._run = runner
        self._master = master
        self._clock = clock
        self._inflight: dict[str, asyncio.Task] = {}
        self._disconnected: set[str] = set()

    def snapshot(self) -> list[dict]:
        out = []
        for name in self.hosts:
            st = self.status[name]
            out.append({
                'host':      name,
                'reachable': st.reachable,
                'code':      st.code,
                'sessions':  [dict(s) for s in st.sessions],
            })
        return out

    def sessions(self, name: str) -> set[str]:
        st = self.status.get(name)
        return {s['name'] for s in st.sessions} if st else set()

    def disconnect(self, name: str) -> None:
        self._disconnected.add(name)
        self._mark(name, reachable=False, code='disconnected')

    def connect(self, name: str) -> None:
        if name in self._disconnected:
            self._disconnected.discard(name)
            self.status[name].failures = 0

    def _due(self, name: str) -> bool:
        if name in self._disconnected:
            return False
        st = self.status[name]
        if st.reachable is None:
            return True
        if st.reachable:
            wait = REACHABLE_TTL
        else:
            wait = UNREACHABLE_BACKOFF[min(st.failures, len(UNREACHABLE_BACKOFF)) - 1]
        return self._clock() - st.checked_at >= wait

    async def refresh(self, name: str, force: bool = False) -> HostStatus:
        if not force and not self._due(name):
            return self.status[name]
        task = self._inflight.get(name)
        if task is None or task.done():
            task = asyncio.ensure_future(self._refresh(name))
            self._inflight[name] = task
        await asyncio.shield(task)
        return self.status[name]

    async def refresh_all(self, force: bool = False) -> None:
        await asyncio.gather(*(self.refresh(name, force) for name in self.hosts))

    async def preflight(self, host: TmuxHost) -> str:
        """'' when `host` can be used now, else a fixed error code.

        Ensures the ssh master and a supported tmux, without listing
        sessions — what switch, save and restore need before they start.
        """
        if host.is_local:
            return ''
        if host.name in self._disconnected:
            return 'disconnected'
        code = await self._master(host)
        if code:
            self._mark(host.name, reachable=False, code=code)
            return code
        st = self.status.setdefault(host.name, HostStatus())
        if st.version is None:
            rc, out, err = await self._run(host, '-V')
            kind = classify(rc, err)
            if kind == 'unreachable':
                self._mark(host.name, reachable=False, code='unreachable')
                return 'unreachable'
            st.version = parse_tmux_version(out) if rc == 0 else None
        if not version_ok(st.version):
            if st.code != 'unsupported_tmux':
                log.warning('host %s: tmux missing or older than %d.%d',
                            host.name, *MIN_TMUX)
            st.version = None
            self._mark(host.name, reachable=False, code='unsupported_tmux')
            return 'unsupported_tmux'
        return ''

    async def _refresh(self, name: str) -> None:
        host = self.hosts[name]
        code = await self.preflight(host)
        if code:
            return
        rc, out, err = await self._run(host, 'list-sessions', '-F', SESSION_FMT)
        kind = classify(rc, err)
        if kind == 'ok':
            self._mark(name, reachable=True, code='', sessions=parse_sessions(out))
        elif kind == 'no_server':
            self._mark(name, reachable=True, code='', sessions=[])
        elif kind == 'unreachable':
            self._mark(name, reachable=False, code='unreachable')
        else:
            log.warning('host %s: list-sessions failed rc=%d %s', name, rc, sanitize(err))
            self._mark(name, reachable=False, code='error')

    def _mark(self, name: str, *, reachable: bool, code: str,
              sessions: list[dict] | None = None) -> None:
        if name in self._disconnected and code != 'disconnected':
            return   # a check that was already under way when it was disconnected
        st = self.status.setdefault(name, HostStatus())
        before = (st.reachable, st.code, st.sessions)
        st.reachable = reachable
        st.code = code
        st.checked_at = self._clock()
        if reachable:
            st.failures = 0
        else:
            st.failures += 1
            st.sessions = []
            if code == 'unreachable':
                st.version = None   # the next host may answer with another tmux
        if sessions is not None:
            st.sessions = sessions
        if before != (st.reachable, st.code, st.sessions) and self.on_change:
            self.on_change()

    def set_sessions(self, name: str, sessions: list[dict]) -> None:
        """The active host's list, straight from its control client."""
        if name in self.status:
            self._mark(name, reachable=True, code='', sessions=[
                {'name': s['name'], 'windows': s['windows'], 'attached': s['attached']}
                for s in sessions
            ])
