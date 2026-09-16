import json
import threading
import unittest
from functools import partial
from http.client import HTTPConnection
from http.server import ThreadingHTTPServer
from unittest.mock import patch

import server
from web_security import AccessController


class AuthSessionEndpointTest(unittest.TestCase):
    TOKEN = 'a' * 32

    @classmethod
    def setUpClass(cls):
        cls.access = AccessController(
            ('http://127.0.0.1:8766',),
            (),
            access_token=cls.TOKEN,
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

    def setUp(self):
        server._AUTH_BUCKET.tokens = server._AUTH_BUCKET.capacity

    def _request(self, method, body=None, content_type='application/json', host='127.0.0.1:8766'):
        conn = HTTPConnection('127.0.0.1', self.port, timeout=5)
        headers = {'Host': host}
        if body is not None:
            headers['Content-Type'] = content_type
            headers['Content-Length'] = str(len(body))
        conn.request(method, '/auth/session', body=body, headers=headers)
        resp = conn.getresponse()
        data = resp.read()
        conn.close()
        return resp, data

    def _post(self, payload_bytes, **kwargs):
        return self._request('POST', body=payload_bytes, **kwargs)

    def test_valid_token_issues_cookie(self):
        resp, _ = self._post(json.dumps({'token': self.TOKEN}).encode())
        self.assertEqual(resp.status, 204)
        self.assertIn('web_tmux_session=', resp.getheader('Set-Cookie', ''))

    def test_wrong_token_is_rejected_without_cookie(self):
        resp, _ = self._post(json.dumps({'token': 'wrong'}).encode())
        self.assertEqual(resp.status, 403)
        self.assertIsNone(resp.getheader('Set-Cookie'))

    def test_disallowed_host_is_rejected(self):
        resp, _ = self._post(json.dumps({'token': self.TOKEN}).encode(), host='evil.example:8766')
        self.assertEqual(resp.status, 403)

    def test_get_and_head_are_not_allowed(self):
        for method in ('GET', 'HEAD'):
            with self.subTest(method=method):
                resp, _ = self._request(method)
                self.assertEqual(resp.status, 405)
                self.assertEqual(resp.getheader('Allow'), 'POST')
                self.assertIsNone(resp.getheader('Set-Cookie'))

    def test_malformed_requests_are_rejected(self):
        bad_bodies = (
            b'not json',
            b'[]',
            json.dumps({'token': self.TOKEN, 'extra': 1}).encode(),
            json.dumps({'token': 123}).encode(),
            json.dumps({}).encode(),
        )
        for body in bad_bodies:
            with self.subTest(body=body):
                resp, _ = self._post(body)
                self.assertEqual(resp.status, 400)

    def test_oversized_body_is_rejected(self):
        resp, _ = self._post(json.dumps({'token': 'x' * 2000}).encode())
        self.assertEqual(resp.status, 400)

    def test_wrong_content_type_is_rejected(self):
        resp, _ = self._post(json.dumps({'token': self.TOKEN}).encode(), content_type='text/plain')
        self.assertEqual(resp.status, 400)

    def test_repeated_failures_are_rate_limited(self):
        body = json.dumps({'token': 'wrong'}).encode()
        statuses = [self._post(body)[0].status for _ in range(6)]
        self.assertEqual(statuses.count(429), 1)
        self.assertEqual(statuses[-1], 429)


if __name__ == '__main__':
    unittest.main()
