import json
import os
import unittest
from unittest.mock import patch

os.environ.setdefault(
    'WEB_TMUX_ALLOWED_ORIGINS', 'https://host.example.ts.net:8766'
)
os.environ.setdefault('WEB_TMUX_TAILSCALE_USERS', 'owner@example.com')

import server
from web_security import AccessController


class AccessControllerTest(unittest.TestCase):
    def setUp(self):
        self.now = [1_000_000]
        self.controller = AccessController(
            ('https://host.example.ts.net:8766',),
            ('owner@example.com',),
            secret=b'x' * 32,
            now=lambda: self.now[0],
        )

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
        context = self.controller.authorize_http(
            'host.example.ts.net:8766', 'owner@example.com'
        )
        cookie = self.controller.session_cookie(context).split(';', 1)[0]
        self.assertTrue(self.controller.validate_session(cookie, context))
        self.assertFalse(self.controller.validate_session(cookie + 'x', context))

        self.now[0] += 8 * 60 * 60 + 1
        self.assertFalse(self.controller.validate_session(cookie, context))

    def test_invalid_remote_configuration_fails_closed(self):
        with self.assertRaises(ValueError):
            AccessController(('https://host.example.ts.net:8766',), ())

    def test_missing_allowed_origins_fails_closed(self):
        with self.assertRaises(ValueError):
            AccessController()


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

    def test_valid_layout_messages(self):
        msg = server._validated_message(json.dumps({'type': 'save_layout', 'name': '作業用'}))
        self.assertEqual(msg['name'], '作業用')
        server._validated_message(json.dumps({'type': 'list_layouts'}))
        server._validated_message(json.dumps({'type': 'delete_layout', 'name': 'a'}))
        server._validated_message(
            json.dumps({'type': 'restore_layout', 'name': 'a', 'mode': 'replace'})
        )
        server._validated_message(
            json.dumps({'type': 'restore_layout', 'name': 'a', 'mode': 'skip'})
        )

    def test_layout_names_are_independent_of_live_sessions(self):
        # A slot names a session that may not exist yet — that is the whole
        # point of restoring it — so _allowed_sessions must not gate these.
        msg = server._validated_message(
            json.dumps({'type': 'restore_layout', 'name': 'not-a-session'})
        )
        self.assertEqual(msg['name'], 'not-a-session')

    def test_bad_layout_messages_are_rejected(self):
        bad_messages = (
            json.dumps({'type': 'save_layout', 'name': ''}),
            json.dumps({'type': 'save_layout', 'name': '   '}),
            json.dumps({'type': 'save_layout', 'name': 'x' * 65}),
            json.dumps({'type': 'save_layout', 'name': 'a\nb'}),
            json.dumps({'type': 'save_layout', 'name': 42}),
            json.dumps({'type': 'save_layout', 'name': 'a', 'overwrite': 'yes'}),
            json.dumps({'type': 'save_layout', 'name': 'a', 'mode': 'replace'}),
            json.dumps({'type': 'restore_layout', 'name': 'a', 'mode': 'nuke'}),
            json.dumps({'type': 'delete_layout'}),
            json.dumps({'type': 'list_layouts', 'name': 'a'}),
        )
        for raw in bad_messages:
            with self.subTest(raw=raw):
                with self.assertRaises(server.PolicyViolation):
                    server._validated_message(raw)


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
