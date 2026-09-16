import json
import unittest
from unittest.mock import patch

import server
from web_security import AccessController


class AccessControllerTest(unittest.TestCase):
    def setUp(self):
        self.now = [1_000_000]
        self.access_token = 'a' * 32
        self.controller = AccessController(
            (
                'http://127.0.0.1:8766',
                'https://host.example.ts.net:8766',
            ),
            ('owner@example.com',),
            access_token=self.access_token,
            secret=b'x' * 32,
            now=lambda: self.now[0],
        )

    def test_local_session_requires_exact_origin_host_and_cookie(self):
        context = self.controller.authorize_http('127.0.0.1:8766', None)
        self.assertIsNotNone(context)
        cookie = self.controller.session_cookie(context).split(';', 1)[0]

        accepted, reason = self.controller.authorize_websocket(
            origin_value='http://127.0.0.1:8766',
            host='127.0.0.1:8765',
            tailscale_login=None,
            cookie_header=cookie,
        )
        self.assertEqual(reason, '')
        self.assertEqual(accepted.identity, 'local')

        for origin in (None, 'https://evil.example'):
            accepted, _ = self.controller.authorize_websocket(
                origin_value=origin,
                host='127.0.0.1:8765',
                tailscale_login=None,
                cookie_header=cookie,
            )
            self.assertIsNone(accepted)

    def test_remote_session_requires_allowed_tailscale_identity(self):
        context = self.controller.authorize_http(
            'host.example.ts.net:8766', 'owner@example.com'
        )
        self.assertIsNotNone(context)
        cookie_header = self.controller.session_cookie(context)
        self.assertIn('Secure', cookie_header)
        cookie = cookie_header.split(';', 1)[0]

        accepted, _ = self.controller.authorize_websocket(
            origin_value='https://host.example.ts.net:8766',
            host='host.example.ts.net:8765',
            tailscale_login='intruder@example.com',
            cookie_header=cookie,
        )
        self.assertIsNone(accepted)

        accepted, reason = self.controller.authorize_websocket(
            origin_value='https://host.example.ts.net:8766',
            host='host.example.ts.net:8765',
            tailscale_login='owner@example.com',
            cookie_header=cookie,
        )
        self.assertEqual(reason, '')
        self.assertEqual(accepted.identity, 'owner@example.com')

    def test_cookie_tampering_expiry_and_host_binding(self):
        context = self.controller.authorize_http('127.0.0.1:8766', None)
        cookie = self.controller.session_cookie(context).split(';', 1)[0]
        self.assertTrue(self.controller.validate_session(cookie, context))
        self.assertFalse(self.controller.validate_session(cookie + 'x', context))

        self.now[0] += 8 * 60 * 60 + 1
        self.assertFalse(self.controller.validate_session(cookie, context))

    def test_invalid_remote_configuration_fails_closed(self):
        with self.assertRaises(ValueError):
            AccessController(
                ('https://host.example.ts.net:8766',), (), access_token=self.access_token
            )

    def test_missing_or_short_access_token_fails_closed(self):
        for token in ('', 'short', 'a' * 31):
            with self.subTest(token=token):
                with self.assertRaises(ValueError):
                    AccessController(access_token=token)

    def test_verify_access_token(self):
        self.assertTrue(self.controller.verify_access_token(self.access_token))
        self.assertFalse(self.controller.verify_access_token('b' * 32))
        self.assertFalse(self.controller.verify_access_token(''))
        self.assertFalse(self.controller.verify_access_token(None))
        self.assertFalse(self.controller.verify_access_token(12345))


class MessageValidationTest(unittest.TestCase):
    def setUp(self):
        self.targets = patch.multiple(
            server,
            _allowed_sessions={'safe'},
            _allowed_windows={1},
            _allowed_panes={'%1'},
        )
        self.targets.start()

    def tearDown(self):
        self.targets.stop()

    def test_valid_input(self):
        msg = server._validated_message(
            json.dumps({'type': 'input', 'pane': '%1', 'data': 'hello'})
        )
        self.assertEqual(msg['pane'], '%1')

    def test_tmux_target_injection_is_rejected(self):
        with self.assertRaises(server.PolicyViolation):
            server._validated_message(json.dumps({
                'type': 'input',
                'pane': '%1; kill-server',
                'data': 'x',
            }))

    def test_non_object_unknown_fields_and_wrong_types_are_rejected(self):
        bad_messages = (
            '[]',
            json.dumps({'type': 'get_state', 'extra': True}),
            json.dumps({'type': 'resize', 'cols': '80', 'rows': 24}),
            json.dumps({'type': 'get_history', 'pane': '%1', 'lines': True}),
        )
        for raw in bad_messages:
            with self.subTest(raw=raw):
                with self.assertRaises(server.PolicyViolation):
                    server._validated_message(raw)

    def test_input_size_and_resize_bounds(self):
        with self.assertRaises(server.PolicyViolation):
            server._validated_message(json.dumps({
                'type': 'input',
                'pane': '%1',
                'data': 'x' * (server.MAX_INPUT_BYTES + 1),
            }))
        with self.assertRaises(server.PolicyViolation):
            server._validated_message(json.dumps({
                'type': 'resize',
                'cols': 501,
                'rows': 24,
            }))


class HeaderAndRateLimitTest(unittest.TestCase):
    def test_security_headers_are_strict(self):
        headers = dict(server._security_headers())
        self.assertIn("frame-ancestors 'none'", headers['Content-Security-Policy'])
        self.assertNotIn('unsafe-inline', headers['Content-Security-Policy'])
        self.assertEqual(headers['X-Frame-Options'], 'DENY')
        self.assertEqual(headers['X-Content-Type-Options'], 'nosniff')

    def test_token_bucket_is_bounded(self):
        bucket = server.TokenBucket(rate=1, capacity=2)
        self.assertTrue(bucket.consume())
        self.assertTrue(bucket.consume())
        self.assertFalse(bucket.consume())


if __name__ == '__main__':
    unittest.main()
