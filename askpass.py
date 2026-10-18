#!/usr/bin/env python3
"""
Passphrase and password prompts for ssh, answered from the browser.

ssh runs with SSH_ASKPASS pointing at this file and SSH_ASKPASS_REQUIRE=force,
so whenever it needs a key passphrase or a password — for the host itself or
for a ProxyJump hop on the way — it runs this file with the prompt as its
argument and reads the answer from its stdout.

Run as a program, this file is that helper: it hands the prompt to the
server over a unix socket and prints what comes back. The server side
(AskpassBroker) passes the prompt to the one browser that asked to connect.

Only an ssh started for an interactive sign-in carries a token. Every other
ssh web-tmux runs gets none, and the helper refuses at once — so a
background session-list refresh can never put a dialog in front of anyone,
and a jump host without BatchMode can never sit waiting on a terminal.

Answers are never stored and never logged: they go from the WebSocket
message straight to this helper's stdout.
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import secrets
import shlex
import socket
import sys
from typing import Awaitable, Callable

SOCKET_ENV = 'WEB_TMUX_ASKPASS_SOCK'
TOKEN_ENV = 'WEB_TMUX_ASKPASS_TOKEN'
SOCKET_NAME = 'askpass.sock'
WRAPPER_NAME = 'askpass'

MAX_PROMPT = 512          # what ssh asks, as shown to the user
MAX_ANSWER = 1024
MAX_PROMPTS = 8           # per sign-in: password retries times jump hops
HELPER_TIMEOUT = 300.0    # someone has to find their phone and type

log = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────── the helper (ssh side)

def main(argv: list[str], environ=os.environ) -> int:
    # SSH_ASKPASS_PROMPT=confirm is a yes/no question (ssh-add -c, host key
    # changes); nothing here is meant to say yes to those. 'none' is only a
    # notice — acknowledge it and move on.
    kind = environ.get('SSH_ASKPASS_PROMPT', '')
    if kind == 'confirm':
        return 1
    if kind == 'none':
        return 0
    path, token = environ.get(SOCKET_ENV, ''), environ.get(TOKEN_ENV, '')
    if not path or not token:
        return 1
    prompt = argv[1] if len(argv) > 1 else ''
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
            sock.settimeout(HELPER_TIMEOUT)
            sock.connect(path)
            sock.sendall(json.dumps({'token': token, 'prompt': prompt[:MAX_PROMPT]}).encode() + b'\n')
            raw = b''
            while not raw.endswith(b'\n'):
                chunk = sock.recv(4096)
                if not chunk:
                    break
                raw += chunk
        reply = json.loads(raw or b'{}')
    except (OSError, ValueError):
        return 1
    answer = reply.get('answer')
    if not isinstance(answer, str):
        return 1
    sys.stdout.write(answer + '\n')
    sys.stdout.flush()
    return 0


# ─────────────────────────────────────────────────────── the broker (server side)

Ask = Callable[[str], Awaitable[str | None]]


class AskpassBroker:
    """Owns the socket the helper talks to, and the open sign-ins.

    open(ask) returns a token for one ssh invocation; each prompt that
    arrives with it is passed to `ask`, whose answer (None to refuse) goes
    back to ssh. close(token) ends it.
    """

    def __init__(self, directory: str) -> None:
        self.directory = directory
        self.socket_path = os.path.join(directory, SOCKET_NAME)
        self.wrapper_path = os.path.join(directory, WRAPPER_NAME)
        self._sessions: dict[str, list] = {}   # token -> [ask, prompts so far]
        self._server: asyncio.AbstractServer | None = None

    async def start(self) -> None:
        os.makedirs(self.directory, mode=0o700, exist_ok=True)
        os.chmod(self.directory, 0o700)
        self._write_wrapper()
        with contextlib.suppress(FileNotFoundError):
            os.unlink(self.socket_path)
        self._server = await asyncio.start_unix_server(self._handle, path=self.socket_path)
        os.chmod(self.socket_path, 0o600)

    def _write_wrapper(self) -> None:
        # ssh executes SSH_ASKPASS directly, so it must be a program; this
        # one runs the helper with the server's own interpreter.
        here = os.path.abspath(__file__)
        script = f'#!/bin/sh\nexec {shlex.quote(sys.executable)} {shlex.quote(here)} "$@"\n'
        tmp = self.wrapper_path + '.tmp'
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o700)
        with os.fdopen(fd, 'w') as fh:
            fh.write(script)
        os.replace(tmp, self.wrapper_path)

    async def stop(self) -> None:
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
            self._server = None
        with contextlib.suppress(FileNotFoundError):
            os.unlink(self.socket_path)

    def env(self, token: str = '') -> dict[str, str]:
        """What every ssh web-tmux runs gets in its environment."""
        return {
            'SSH_ASKPASS': self.wrapper_path,
            'SSH_ASKPASS_REQUIRE': 'force',
            SOCKET_ENV: self.socket_path,
            TOKEN_ENV: token,
        }

    def open(self, ask: Ask) -> str:
        token = secrets.token_urlsafe(24)
        self._sessions[token] = [ask, 0]
        return token

    def close(self, token: str) -> None:
        self._sessions.pop(token, None)

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        answer = None
        try:
            line = await asyncio.wait_for(reader.readline(), timeout=5.0)
            msg = json.loads(line or b'{}')
            session = self._sessions.get(msg.get('token')) if isinstance(msg, dict) else None
            if session is not None and session[1] < MAX_PROMPTS:
                session[1] += 1
                prompt = msg.get('prompt')
                answer = await session[0](prompt if isinstance(prompt, str) else '')
        except (asyncio.TimeoutError, ValueError, OSError):
            answer = None
        except Exception:
            log.exception('askpass prompt failed')
            answer = None
        try:
            writer.write(json.dumps({'answer': answer}).encode() + b'\n')
            await writer.drain()
        except OSError:
            pass
        finally:
            writer.close()
            with contextlib.suppress(Exception):
                await writer.wait_closed()


def clean_prompt(prompt: str) -> str:
    """ssh's prompt, or a keyboard-interactive one from the remote server,
    made safe to show: printable characters only, bounded."""
    text = ''.join(ch if ch.isprintable() else ' ' for ch in prompt[:MAX_PROMPT])
    return ' '.join(text.split())


if __name__ == '__main__':
    sys.exit(main(sys.argv))
