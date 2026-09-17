import asyncio
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


NOW = '2026-09-17T13:20:11+09:00'
LAYOUT_SPLIT = '81f0,200x50,0,0[200x25,0,0,0,200x24,0,26,1]'   # 2 panes, one split
LAYOUT_SOLO  = '5963,80x24,0,0,7'                              # 1 pane, no split


def _session(session, *, attached=False, solo=False):
    """One entry of a snapshot's `sessions` list, for restore tests."""
    layout = LAYOUT_SOLO if solo else LAYOUT_SPLIT
    panes = [{'cwd': '/', 'active': True}] if solo else \
            [{'cwd': '/', 'active': True}, {'cwd': '/', 'active': False}]
    return {
        'session': session, 'attached': attached,
        'width': 200, 'height': 50, 'pane_order': 'layout', 'active_window': 0,
        'windows': [{'index': 0, 'name': 'w', 'active': True, 'layout': layout, 'panes': panes}],
    }


# 'web' is attached (the one the browser was looking at when it was saved);
# both sessions have a split so a "fail this session's split" test can target
# one without touching the other.
SNAP = {'name': 'slot', 'saved_at': NOW,
        'sessions': [_session('web', attached=True), _session('dev')]}


class RestoreSnapshotTest(unittest.TestCase):
    """Drives _restore_snapshot with tmux stubbed out."""

    def setUp(self):
        self.capture_calls = []
        self.run_calls = []
        self.fail_on = None          # verb that fails on every call
        self.fail_nth = {}           # {verb: 0-based occurrence index that fails}
        self.verb_counts = {}
        # What list-sessions reports. Deliberately disjoint from SNAP's names
        # and from the fake control client's own session by default, so a
        # test has to opt in to a conflict rather than get one by accident.
        self.live = 'other\t0\n'
        self.tmux = _FakeTmuxControl()
        patcher = patch.multiple(
            server,
            tmux=self.tmux,
            _run_tmux_capture=self._capture,
            _run_tmux=self._run,
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    async def _capture(self, *args):
        self.capture_calls.append(list(args))
        verb = args[0]
        if verb == 'list-sessions':
            return 0, self.live
        if verb == self.fail_on:
            return 1, ''
        idx = self.verb_counts.get(verb, 0)
        self.verb_counts[verb] = idx + 1
        if idx == self.fail_nth.get(verb, -1):
            return 1, ''
        if verb == 'new-session':
            return 0, '0\t%10\n'
        if verb in ('new-window', 'split-window'):
            return 0, '%%%d\n' % (10 + len(self.capture_calls))
        return 0, ''

    async def _run(self, *args):
        self.run_calls.append(list(args))
        return 0

    def restore(self, snap=None, mode='auto'):
        ws = object()
        with patch.object(server, '_send_current_view', _noop):
            return asyncio.run(server._restore_snapshot(ws, snap or SNAP, mode))

    def _verbs(self):
        return [call[0] for call in self.capture_calls]

    def _renames(self):
        return [c for c in self.run_calls if c[0] == 'rename-session']

    def _kills(self):
        return [c for c in self.run_calls if c[0] == 'kill-session']

    def test_restores_every_session_independently(self):
        results = self.restore()

        self.assertEqual(results, [
            {'session': 'web', 'target': 'web', 'status': 'restored'},
            {'session': 'dev', 'target': 'dev', 'status': 'restored'},
        ])
        self.assertEqual(self._verbs(), [
            'list-sessions',
            'new-session', 'split-window', 'select-layout', 'select-window', 'select-pane',
            'new-session', 'split-window', 'select-layout', 'select-window', 'select-pane',
        ])
        self.assertEqual({r[3] for r in self._renames()}, {'web', 'dev'})
        self.assertEqual(self._kills(), [])

    def test_focuses_the_session_that_was_attached_at_save_time(self):
        self.restore()

        # 'web' was attached=True in the snapshot. The control client (which
        # started on an unrelated session) moves there exactly once, at the
        # very end — not mid-batch as part of either session's own build.
        self.assertEqual(self.tmux.commands, ['switch-client -t "web"'])
        self.assertEqual(self.tmux.session, 'web')

    def test_one_sessions_split_failure_does_not_block_the_other(self):
        self.fail_nth = {'split-window': 0}   # web's only split-window call

        results = self.restore()

        self.assertEqual(results, [
            {'session': 'web', 'target': 'web', 'status': 'failed'},
            {'session': 'dev', 'target': 'dev', 'status': 'restored'},
        ])
        kills = self._kills()
        self.assertEqual(len(kills), 1)
        self.assertTrue(kills[0][2].startswith('web-tmux-restore-'))
        self.assertEqual([r[3] for r in self._renames()], ['dev'])
        # Focus falls back to the first session that actually came back.
        self.assertEqual(self.tmux.commands, ['switch-client -t "dev"'])
        self.assertEqual(self.tmux.session, 'dev')

    def test_layout_failure_is_not_fatal(self):
        self.fail_on = 'select-layout'

        results = self.restore()

        self.assertEqual([r['status'] for r in results], ['restored', 'restored'])
        self.assertEqual(self._kills(), [])

    def test_all_sessions_failing_leaves_focus_untouched(self):
        self.fail_on = 'split-window'

        results = self.restore()

        self.assertEqual([r['status'] for r in results], ['failed', 'failed'])
        self.assertEqual(self._renames(), [])
        self.assertEqual(self.tmux.commands, [])          # never switched
        self.assertEqual(self.tmux.session, 'scratch')     # left exactly as it was

    def test_websocket_stays_unsubscribed_for_the_whole_batch(self):
        seen_during_batch = []

        async def spy_run(*args):
            seen_during_batch.append(ws in self.tmux.subscribers)
            self.run_calls.append(list(args))
            return 0

        ws = object()
        self.tmux.subscribers.append(ws)
        with patch.object(server, '_run_tmux', spy_run), \
             patch.object(server, '_send_current_view', _noop):
            asyncio.run(server._restore_snapshot(ws, SNAP, 'auto'))

        self.assertTrue(seen_during_batch)
        self.assertFalse(any(seen_during_batch))    # unsubscribed for every call, not just some
        self.assertIn(ws, self.tmux.subscribers)     # restored afterwards

    def test_replace_kills_only_the_conflicting_sessions(self):
        self.live = 'web\t0\n'   # only 'web' collides; 'dev' does not exist yet

        results = self.restore(mode='replace')

        self.assertEqual([r['target'] for r in results], ['web', 'dev'])
        kills = self._kills()
        self.assertEqual(len(kills), 1)
        self.assertEqual(kills[0][2], 'web')

    def test_duplicate_only_renames_the_conflicting_sessions(self):
        self.live = 'web\t0\n'   # only 'web' collides

        results = self.restore(mode='duplicate')

        self.assertEqual([r['target'] for r in results], ['web-2', 'dev'])
        self.assertEqual(self._kills(), [])

    def test_skip_ignores_conflicts_and_restores_only_non_conflicting_sessions(self):
        self.live = 'web\t0\n'   # only 'web' collides

        results = self.restore(mode='skip')

        self.assertEqual(results, [
            {'session': 'web', 'target': 'web', 'status': 'skipped'},
            {'session': 'dev', 'target': 'dev', 'status': 'restored'},
        ])
        self.assertEqual(self._verbs(), [
            'list-sessions',
            'new-session', 'split-window', 'select-layout', 'select-window', 'select-pane',
        ])
        self.assertEqual([r[3] for r in self._renames()], ['dev'])
        self.assertEqual(self._kills(), [])
        self.assertEqual(self.tmux.commands, ['switch-client -t "dev"'])
        self.assertEqual(self.tmux.session, 'dev')

    def test_duplicate_does_not_collide_with_another_session_in_the_same_batch(self):
        # The snapshot itself holds 'web' and 'web-2'; only 'web' is actually
        # live. Renaming 'web' naively to 'web-2' would land on the batch's
        # OWN 'web-2' entry once that one gets restored.
        snap = {'name': 'slot', 'saved_at': NOW,
                'sessions': [_session('web', attached=True), _session('web-2')]}
        self.live = 'web\t0\n'

        results = self.restore(snap, mode='duplicate')

        self.assertEqual([r['target'] for r in results], ['web-3', 'web-2'])

    def test_switches_before_killing_when_the_target_is_attached(self):
        # The control client is presently attached to 'web' — the session
        # about to be replaced. It must move to the throwaway name before
        # 'web' is killed, or TmuxControl would recreate an empty 'web' out
        # from under the restore (see _ensure_session_exists).
        self.tmux.session = 'web'
        self.live = 'web\t1\n'

        results = self.restore(mode='replace')

        self.assertEqual(results[0], {'session': 'web', 'target': 'web', 'status': 'restored'})
        switch = next(c for c in self.tmux.commands if c.startswith('switch-client -t "web-tmux-restore-'))
        temp = switch.split('"')[1]
        kill = next(c for c in self.run_calls if c[0] == 'kill-session')
        self.assertEqual(kill[2], 'web')
        rename = next(c for c in self._renames() if c[2] == temp)
        self.assertEqual(rename[3], 'web')
        self.assertEqual(self.tmux.session, 'web')   # ends back on 'web' after rename


class _FakeTmuxControl:
    def __init__(self):
        # Deliberately not a name SNAP restores, so tests don't get a
        # switch-client dance by accident — only tests that opt in (by
        # setting self.tmux.session to a colliding name) exercise that path.
        self.session = 'scratch'
        self.subscribers = []
        self.commands = []

    async def send_command(self, cmd):
        self.commands.append(cmd)
        return ''


async def _noop(*args, **kwargs):
    return None


class WithExistingCwdTest(unittest.TestCase):
    def test_keeps_a_directory_that_is_still_there(self):
        argv = ['split-window', '-t', '%1', '-c', '/']
        self.assertEqual(server._with_existing_cwd(argv), argv)

    def test_falls_back_to_home_when_the_directory_is_gone(self):
        argv = ['split-window', '-t', '%1', '-c', '/definitely/not/here']
        self.assertEqual(server._with_existing_cwd(argv)[-1], os.path.expanduser('~'))

    def test_uses_the_trailing_flag_not_a_window_named_dash_c(self):
        argv = ['new-session', '-n', '-c', '-c', '/', '-x', '80', '-y', '24']
        self.assertEqual(server._with_existing_cwd(argv), argv)

    def test_leaves_argv_without_a_cwd_alone(self):
        argv = ['select-layout', '-t', 'web:0', '81f0,80x24,0,0,1']
        self.assertEqual(server._with_existing_cwd(argv), argv)


if __name__ == '__main__':
    unittest.main()
