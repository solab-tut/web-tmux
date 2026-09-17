import os
import threading
import unittest
from functools import partial
from http.client import HTTPConnection
from http.server import ThreadingHTTPServer
from unittest.mock import patch

os.environ.setdefault(
    'WEB_TMUX_ALLOWED_ORIGINS', 'https://host.example.ts.net:8766'
)
os.environ.setdefault('WEB_TMUX_TAILSCALE_USERS', 'owner@example.com')

import server
from web_security import AccessController


class AuthSessionEndpointTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.access = AccessController(
            ('https://host.example.ts.net:8766',),
            ('owner@example.com',),
            secret=b'z' * 32,
        )
        cls.patcher = patch.object(server, 'ACCESS', cls.access)
        cls.patcher.start()
        handler = partial(server.SecureStaticHandler, directory=server.STATIC_DIR)
        cls.httpd = ThreadingHTTPServer(('127.0.0.1', 0), handler)
        cls.port = cls.httpd.server_address[1]
        cls.thread = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()
        cls.patcher.stop()

    def _request(
        self,
        method,
        *,
        host='host.example.ts.net:8766',
        login='owner@example.com',
    ):
        conn = HTTPConnection('127.0.0.1', self.port, timeout=5)
        headers = {'Host': host}
        if login is not None:
            headers['Tailscale-User-Login'] = login
        conn.request(method, '/auth/session', headers=headers)
        resp = conn.getresponse()
        data = resp.read()
        conn.close()
        return resp, data

    def test_allowed_tailscale_user_gets_cookie(self):
        resp, _ = self._request('GET')
        self.assertEqual(resp.status, 204)
        self.assertIn('web_tmux_session=', resp.getheader('Set-Cookie', ''))
        self.assertIn('Secure', resp.getheader('Set-Cookie', ''))

    def test_missing_or_wrong_tailscale_user_is_rejected(self):
        resp, _ = self._request('GET', login=None)
        self.assertEqual(resp.status, 403)
        self.assertIsNone(resp.getheader('Set-Cookie'))
        resp, _ = self._request('GET', login='intruder@example.com')
        self.assertEqual(resp.status, 403)
        self.assertIsNone(resp.getheader('Set-Cookie'))

    def test_disallowed_host_is_rejected(self):
        resp, _ = self._request('GET', host='evil.example:8766')
        self.assertEqual(resp.status, 403)

    def test_get_and_head_issue_cookie(self):
        for method in ('GET', 'HEAD'):
            with self.subTest(method=method):
                resp, _ = self._request(method)
                self.assertEqual(resp.status, 204)
                self.assertIn('web_tmux_session=', resp.getheader('Set-Cookie', ''))


if __name__ == '__main__':
    unittest.main()
