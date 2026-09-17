from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import os
import secrets
import time
from dataclasses import dataclass
from http.cookies import SimpleCookie
from urllib.parse import urlsplit


COOKIE_NAME = 'web_tmux_session'
LOCAL_HOSTS = frozenset({'127.0.0.1', 'localhost', '::1'})


def _split_csv(value: str) -> tuple[str, ...]:
    return tuple(part.strip() for part in value.split(',') if part.strip())


def _b64url_encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b'=').decode('ascii')


def _b64url_decode(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + '=' * (-len(value) % 4))


def _host_parts(host: str) -> tuple[str, int | None] | None:
    if not host or any(ch in host for ch in '\r\n\0/@'):
        return None
    try:
        parsed = urlsplit(f'//{host}')
        if not parsed.hostname:
            return None
        return parsed.hostname.lower(), parsed.port
    except ValueError:
        return None


@dataclass(frozen=True)
class AuthContext:
    hostname: str
    identity: str
    remote: bool


@dataclass(frozen=True)
class AllowedOrigin:
    value: str
    scheme: str
    hostname: str
    http_port: int

    @property
    def http_host(self) -> str:
        default_port = 443 if self.scheme == 'https' else 80
        return self.hostname if self.http_port == default_port else f'{self.hostname}:{self.http_port}'

    @property
    def ws_host(self) -> str:
        return f'{self.hostname}:8765'

    @property
    def ws_source(self) -> str:
        scheme = 'wss' if self.scheme == 'https' else 'ws'
        return f'{scheme}://{self.ws_host}'

    @property
    def remote(self) -> bool:
        return self.hostname not in LOCAL_HOSTS


class AccessController:
    def __init__(
        self,
        origins: tuple[str, ...] = (),
        tailscale_users: tuple[str, ...] = (),
        *,
        secret: bytes | None = None,
        session_ttl: int = 8 * 60 * 60,
        now=time.time,
    ) -> None:
        parsed_origins: list[AllowedOrigin] = []
        seen: set[str] = set()
        for raw in origins:
            if raw in seen:
                continue
            parsed = urlsplit(raw)
            if (
                parsed.scheme not in {'http', 'https'}
                or not parsed.hostname
                or parsed.username is not None
                or parsed.password is not None
                or parsed.path not in {'', '/'}
                or parsed.query
                or parsed.fragment
            ):
                raise ValueError(f'invalid WEB_TMUX_ALLOWED_ORIGINS entry: {raw!r}')
            try:
                port = parsed.port or (443 if parsed.scheme == 'https' else 80)
            except ValueError as exc:
                raise ValueError(f'invalid WEB_TMUX_ALLOWED_ORIGINS entry: {raw!r}') from exc
            if port != 8766:
                raise ValueError(f'allowed HTTP origin must use port 8766: {raw!r}')
            canonical = f'{parsed.scheme}://{parsed.hostname.lower()}:{port}'
            if raw != canonical:
                raise ValueError(f'origin must be canonical and omit trailing slash: {canonical!r}')
            seen.add(raw)
            parsed_origins.append(AllowedOrigin(raw, parsed.scheme, parsed.hostname.lower(), port))

        if not parsed_origins:
            raise ValueError('WEB_TMUX_ALLOWED_ORIGINS must not be empty')

        users = frozenset(user.lower() for user in tailscale_users if user)
        if any(origin.remote for origin in parsed_origins) and not users:
            raise ValueError('WEB_TMUX_TAILSCALE_USERS is required for non-local origins')

        self.origins = tuple(parsed_origins)
        self.allowed_origin_values = tuple(origin.value for origin in self.origins)
        self.tailscale_users = users
        self.secret = secret or secrets.token_bytes(32)
        self.session_ttl = session_ttl
        self._now = now

    @classmethod
    def from_env(cls) -> 'AccessController':
        origins = _split_csv(os.environ.get('WEB_TMUX_ALLOWED_ORIGINS', ''))
        users = _split_csv(os.environ.get('WEB_TMUX_TAILSCALE_USERS', ''))
        return cls(origins, users)

    @property
    def csp_connect_sources(self) -> tuple[str, ...]:
        return tuple(origin.ws_source for origin in self.origins)

    def _origin_for_http_host(self, host: str) -> AllowedOrigin | None:
        parts = _host_parts(host)
        if parts is None:
            return None
        hostname, port = parts
        port = port or 80
        for origin in self.origins:
            if hostname == origin.hostname and port == origin.http_port:
                return origin
        return None

    def authorize_http(self, host: str, tailscale_login: str | None) -> AuthContext | None:
        origin = self._origin_for_http_host(host)
        if origin is None:
            return None
        if not origin.remote:
            return AuthContext(origin.hostname, 'local', False)
        login = (tailscale_login or '').strip().lower()
        if login not in self.tailscale_users:
            return None
        return AuthContext(origin.hostname, login, True)

    def _session_token(self, context: AuthContext) -> str:
        payload = {
            'exp': int(self._now()) + self.session_ttl,
            'host': context.hostname,
            'id': context.identity,
            'nonce': secrets.token_urlsafe(12),
        }
        encoded = _b64url_encode(json.dumps(payload, separators=(',', ':'), sort_keys=True).encode('utf-8'))
        signature = hmac.new(self.secret, encoded.encode('ascii'), hashlib.sha256).digest()
        return f'{encoded}.{_b64url_encode(signature)}'

    def session_cookie(self, context: AuthContext) -> str:
        parts = [
            f'{COOKIE_NAME}={self._session_token(context)}',
            'HttpOnly',
            'SameSite=Strict',
            'Path=/',
            f'Max-Age={self.session_ttl}',
        ]
        if context.remote:
            parts.append('Secure')
        return '; '.join(parts)

    def _cookie_token(self, cookie_header: str | None) -> str:
        if not cookie_header:
            return ''
        cookie = SimpleCookie()
        try:
            cookie.load(cookie_header)
        except Exception:
            return ''
        morsel = cookie.get(COOKIE_NAME)
        return morsel.value if morsel else ''

    def validate_session(self, cookie_header: str | None, context: AuthContext) -> bool:
        token = self._cookie_token(cookie_header)
        try:
            encoded, supplied_signature = token.split('.', 1)
            expected_signature = hmac.new(self.secret, encoded.encode('ascii'), hashlib.sha256).digest()
            if not hmac.compare_digest(_b64url_decode(supplied_signature), expected_signature):
                return False
            payload = json.loads(_b64url_decode(encoded))
            return (
                isinstance(payload, dict)
                and isinstance(payload.get('exp'), int)
                and payload['exp'] >= int(self._now())
                and payload.get('host') == context.hostname
                and payload.get('id') == context.identity
            )
        except (ValueError, TypeError, binascii.Error, json.JSONDecodeError):
            return False

    def authorize_websocket(
        self,
        *,
        origin_value: str | None,
        host: str,
        tailscale_login: str | None,
        cookie_header: str | None,
    ) -> tuple[AuthContext | None, str]:
        if not origin_value:
            return None, 'missing origin'
        origin = next((item for item in self.origins if item.value == origin_value), None)
        if origin is None:
            return None, 'origin not allowed'

        host_parts = _host_parts(host)
        if host_parts is None:
            return None, 'invalid host'
        hostname, port = host_parts
        if hostname != origin.hostname or port != 8765:
            return None, 'host does not match origin'

        if origin.remote:
            identity = (tailscale_login or '').strip().lower()
            if identity not in self.tailscale_users:
                return None, 'tailscale user not allowed'
        else:
            identity = 'local'
        context = AuthContext(origin.hostname, identity, origin.remote)
        if not self.validate_session(cookie_header, context):
            return None, 'invalid session'
        return context, ''
