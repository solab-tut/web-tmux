import asyncio
import os
import re
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
        path='/auth/session',
        host='host.example.ts.net:8766',
        login='owner@example.com',
    ):
        conn = HTTPConnection('127.0.0.1', self.port, timeout=5)
        headers = {'Host': host}
        if login is not None:
            headers['Tailscale-User-Login'] = login
        conn.request(method, path, headers=headers)
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

    def test_served_files_are_cacheable_but_still_locked_down(self):
        cases = (
            ('/auth/session', 'no-store', True),
            ('/', 'no-cache', False),
            ('/app.js', 'no-cache', False),
            ('/sw.js', 'no-cache', False),
            ('/vendor/xterm/5.3.0/lib/xterm.js', 'immutable', False),
        )
        for path, expected, unstored in cases:
            with self.subTest(path=path):
                resp, _ = self._request('GET', path=path)
                cache_control = resp.getheader('Cache-Control', '')
                self.assertIn(expected, cache_control)
                self.assertEqual('no-store' in cache_control, unstored)
                self.assertIn(
                    "frame-ancestors 'none'",
                    resp.getheader('Content-Security-Policy', ''),
                )


class ServiceWorkerShellTest(unittest.TestCase):
    """The worker answers restored tabs from its cache, so its precache list
    has to stay in step with what the page actually loads: a file the page
    pulls in but the worker never cached would be fetched over the network at
    exactly the moment the network is unreliable."""

    def setUp(self):
        with open(os.path.join(server.STATIC_DIR, 'index.html')) as fh:
            self.index = fh.read()
        with open(os.path.join(server.STATIC_DIR, 'sw.js')) as fh:
            self.worker = fh.read()

    def test_precache_covers_every_asset_the_page_loads(self):
        referenced = set(re.findall(r'(?:src|href)="(?!data:|https?:)([^"]+)"', self.index))
        self.assertIn('app.js', referenced)   # the regex still finds things
        cached = set(re.findall(r"'(/[^']*)'", self.worker))
        for asset in sorted(referenced):
            with self.subTest(asset=asset):
                self.assertTrue(
                    os.path.exists(os.path.join(server.STATIC_DIR, asset)),
                    f'{asset} is referenced but missing',
                )
                self.assertIn(f'/{asset}', cached, f'{asset} is not precached')

    def test_the_cookie_endpoint_is_never_served_from_the_cache(self):
        self.assertIn("url.pathname === AUTH_PATH", self.worker)
        self.assertIn(f"AUTH_PATH = '{server.AUTH_PATH}'", self.worker)


NOW = '2026-09-17T13:20:11+09:00'
LAYOUT_SPLIT = '81f0,200x50,0,0[200x25,0,0,0,200x24,0,26,1]'   # 2 panes, one split
LAYOUT_SOLO  = '5963,80x24,0,0,7'                              # 1 pane, no split


def _session(session, *, attached=False, solo=False, host='local'):
    """One entry of a snapshot's `sessions` list, for restore tests."""
    layout = LAYOUT_SOLO if solo else LAYOUT_SPLIT
    panes = [{'cwd': '/', 'active': True}] if solo else \
            [{'cwd': '/', 'active': True}, {'cwd': '/', 'active': False}]
    return {
        'host': host, 'session': session, 'attached': attached,
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

    async def _capture(self, *args, host=None):
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

    async def _run(self, *args, host=None):
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
            {'host': 'local', 'session': 'web', 'target': 'web', 'status': 'restored'},
            {'host': 'local', 'session': 'dev', 'target': 'dev', 'status': 'restored'},
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
            {'host': 'local', 'session': 'web', 'target': 'web', 'status': 'failed'},
            {'host': 'local', 'session': 'dev', 'target': 'dev', 'status': 'restored'},
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

        async def spy_run(*args, host=None):
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
            {'host': 'local', 'session': 'web', 'target': 'web', 'status': 'skipped'},
            {'host': 'local', 'session': 'dev', 'target': 'dev', 'status': 'restored'},
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

        self.assertEqual(results[0], {'host': 'local', 'session': 'web', 'target': 'web', 'status': 'restored'})
        switch = next(c for c in self.tmux.commands if c.startswith('switch-client -t "web-tmux-restore-'))
        temp = switch.split('"')[1]
        kill = next(c for c in self.run_calls if c[0] == 'kill-session')
        self.assertEqual(kill[2], 'web')
        rename = next(c for c in self._renames() if c[2] == temp)
        self.assertEqual(rename[3], 'web')
        self.assertEqual(self.tmux.session, 'web')   # ends back on 'web' after rename


REMOTE = server.TmuxHost('lan-box', 'tmux', '/state/ssh')


class _FakeDirectory:
    def __init__(self, codes=None):
        self.codes = codes or {}
        self.refreshed = []

    async def preflight(self, host):
        return self.codes.get(host.name, '')

    async def refresh(self, name, force=False):
        self.refreshed.append(name)

    async def refresh_all(self, force=False):
        pass

    def set_sessions(self, name, sessions):
        pass

    def snapshot(self):
        return []

    def sessions(self, name):
        return set()

    def disconnect(self, name):
        self.codes[name] = 'disconnected'

    def connect(self, name):
        self.codes.pop(name, None)


class MultiHostRestoreTest(RestoreSnapshotTest):
    """The same stubs, with a second (ssh) host configured."""

    def setUp(self):
        super().setUp()
        self.hosts_seen = []
        self.directory = _FakeDirectory()
        self.dirs = {'local': ({'/'}, '/home/me'), 'lan-box': (set(), '/home/remote')}

        async def existing_dirs(host, cwds):
            return self.dirs.get(host.name)

        patcher = patch.multiple(
            server,
            HOSTS={'local': server.LOCAL_HOST, 'lan-box': REMOTE},
            DIRECTORY=self.directory,
            _existing_dirs=existing_dirs,
            _refresh_hosts_soon=lambda force=False: None,
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    async def _capture(self, *args, host=None):
        self.hosts_seen.append((host.name if host else None, args[0]))
        return await super()._capture(*args, host=host)

    def snap(self):
        return {'name': 'slot', 'saved_at': NOW,
                'sessions': [_session('web', attached=True), _session('dev', host='lan-box'),
                             _session('ops', host='gone-box')]}

    def test_each_session_is_rebuilt_on_its_own_host(self):
        results = self.restore(self.snap())

        self.assertEqual([(r['host'], r['session'], r['status']) for r in results], [
            ('local', 'web', 'restored'),
            ('lan-box', 'dev', 'restored'),
            ('gone-box', 'ops', 'failed'),
        ])
        self.assertEqual(results[2]['reason'], 'host_not_configured')
        self.assertIn(('lan-box', 'new-session'), self.hosts_seen)
        self.assertNotIn(('local', 'new-session'),
                         [h for h in self.hosts_seen if h[0] == 'lan-box'])

    def test_missing_remote_cwd_falls_back_to_the_remote_home(self):
        self.restore(self.snap())

        remote_new = [c for (h, _), c in zip(self.hosts_seen, self.capture_calls)
                      if h == 'lan-box' and c[0] == 'new-session'][0]
        self.assertEqual(remote_new[remote_new.index('-c') + 1], '/home/remote')

    def test_unreachable_host_fails_only_its_own_sessions(self):
        self.directory.codes = {'lan-box': 'unreachable'}

        results = self.restore(self.snap())

        self.assertEqual([r['status'] for r in results], ['restored', 'failed', 'failed'])
        self.assertEqual(results[1]['reason'], 'unreachable')
        self.assertNotIn('lan-box', [h for h, _ in self.hosts_seen])

    def test_focus_never_moves_to_another_host(self):
        snap = {'name': 'slot', 'saved_at': NOW,
                'sessions': [_session('dev', attached=True, host='lan-box')]}

        results = self.restore(snap)

        self.assertEqual(results[0]['status'], 'restored')
        self.assertEqual(self.tmux.commands, [])
        self.assertEqual(self.tmux.session, 'scratch')

    def test_same_name_on_another_host_is_not_the_attached_session(self):
        # The control client is on local 'web'; restoring lan-box's 'web'
        # must not do the switch-before-kill dance on the local client.
        self.tmux.session = 'web'
        self.live = 'web\t1\n'
        snap = {'name': 'slot', 'saved_at': NOW,
                'sessions': [_session('web', host='lan-box')]}

        self.restore(snap, mode='replace')

        self.assertFalse(any(c.startswith('switch-client') for c in self.tmux.commands))


class _Client:
    def __init__(self):
        self.queued, self.sent = [], []
        self._closed = False

    def enqueue(self, data):
        self.queued.append(server.json.loads(data))
        return True

    async def send(self, data):
        self.sent.append(server.json.loads(data))


class _SwitchingTmux:
    def __init__(self, ok=True):
        self.host = server.LOCAL_HOST
        self.session = 'web'
        self.subscribers = []
        self.ok = ok
        self.during_switch = None
        self.commands = []
        self.target_gen = 0
        self.inputs = []
        self.panes = []          # what get_initial_state finds in the window

    async def switch_target(self, host, session):
        self.during_switch = list(self.subscribers)
        self.target_gen += 1
        if self.ok:
            self.host, self.session = host, session
        return self.ok

    async def write_input(self, pane, data, *, target_gen=None):
        self.inputs.append((pane, data, target_gen))

    async def send_command(self, cmd):
        self.commands.append(cmd)
        return ''

    async def get_initial_state(self):
        return {'host': self.host.name, 'session': self.session,
                'sessions': [{'name': self.session, 'windows': 1, 'attached': True, 'active': True}],
                'windows': [], 'panes': [{'id': pane} for pane in self.panes],
                'active_pane': '', 'layout': ''}


class SwitchHostTest(unittest.TestCase):
    def setUp(self):
        self.directory = _FakeDirectory()
        self.tmux = _SwitchingTmux()
        self.runs = []
        self.rc = 0

        async def run(*args, host=None):
            self.runs.append((host.name, args))
            return self.rc

        patcher = patch.multiple(
            server,
            HOSTS={'local': server.LOCAL_HOST, 'lan-box': REMOTE},
            DIRECTORY=self.directory,
            tmux=self.tmux,
            _run_tmux=run,
            _refresh_hosts_soon=lambda force=False: None,
            _allowed_sessions={},
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    def switch(self, client, **kwargs):
        asyncio.run(server._switch_host(client, 'lan-box', 'dev', **kwargs))

    def test_every_subscriber_gets_init_and_nothing_flows_during_the_switch(self):
        me, other = _Client(), _Client()
        self.tmux.subscribers += [me, other]

        self.switch(me)

        self.assertEqual(self.tmux.during_switch, [])
        for client in (me, other):
            self.assertEqual([m['type'] for m in client.queued], ['init'])
            self.assertEqual(client.queued[0]['host'], 'lan-box')
        self.assertEqual(self.tmux.subscribers, [me, other])
        self.assertEqual(server._allowed_sessions['lan-box'], {'dev'})

    def test_preflight_failure_changes_nothing(self):
        self.directory.codes = {'lan-box': 'unreachable'}
        me = _Client()
        self.tmux.subscribers.append(me)

        self.switch(me)

        self.assertIsNone(self.tmux.during_switch)
        self.assertEqual(me.sent, [{'type': 'host_error', 'host': 'lan-box', 'code': 'unreachable'}])
        self.assertEqual(self.tmux.host.name, 'local')

    def test_session_gone_is_reported_before_switching(self):
        self.rc = 1
        me = _Client()

        self.switch(me)

        self.assertEqual(self.runs, [('lan-box', ('has-session', '-t', '=dev'))])
        self.assertEqual(me.sent[0]['code'], 'session_gone')
        self.assertIsNone(self.tmux.during_switch)

    def test_new_session_is_created_on_the_host_first(self):
        me = _Client()

        self.switch(me, create=True)

        self.assertIn(('lan-box', ('new-session', '-d', '-s', 'dev')), self.runs)
        self.assertEqual(self.tmux.host.name, 'lan-box')

    def test_failed_switch_still_resends_the_view_and_reports(self):
        self.tmux.ok = False
        me = _Client()
        self.tmux.subscribers.append(me)

        self.switch(me)

        self.assertEqual(me.queued[0]['type'], 'init')
        self.assertEqual(me.queued[0]['host'], 'local')
        self.assertEqual(me.sent[-1]['code'], 'switch_failed')
        self.assertEqual(self.tmux.subscribers, [me])

    def test_closed_client_is_not_resubscribed(self):
        me, gone = _Client(), _Client()
        gone._closed = True
        self.tmux.subscribers += [me, gone]

        self.switch(me)

        self.assertEqual(self.tmux.subscribers, [me])


class DisconnectHostTest(SwitchHostTest):
    def setUp(self):
        super().setUp()
        self.stopped = []

        async def stop_master(host):
            self.stopped.append(host.name)
        patcher = patch.object(server.tmux_hosts, 'stop_master', stop_master)
        patcher.start()
        self.addCleanup(patcher.stop)

    def disconnect(self, client):
        asyncio.run(server._disconnect_host(client, 'lan-box'))

    def test_other_host_is_closed_without_moving_anyone(self):
        me = _Client()
        self.tmux.subscribers.append(me)

        self.disconnect(me)

        self.assertIsNone(self.tmux.during_switch)
        self.assertEqual(me.queued, [])
        self.assertEqual(self.stopped, ['lan-box'])
        self.assertEqual(self.directory.codes, {'lan-box': 'disconnected'})

    def test_active_host_sends_everyone_back_to_the_local_session_first(self):
        me, other = _Client(), _Client()
        self.tmux.host, self.tmux.session = REMOTE, 'dev'
        self.tmux.subscribers += [me, other]

        self.disconnect(me)

        self.assertEqual(self.tmux.during_switch, [])
        self.assertEqual((self.tmux.host.name, self.tmux.session), ('local', server.SESSION))
        for client in (me, other):
            self.assertEqual([m['type'] for m in client.queued], ['init'])
            self.assertEqual(client.queued[0]['host'], 'local')
        self.assertEqual(self.tmux.subscribers, [me, other])
        self.assertEqual(self.stopped, ['lan-box'])

    def test_failed_move_keeps_the_connection(self):
        self.tmux.ok = False
        self.tmux.host = REMOTE
        me = _Client()

        self.disconnect(me)

        self.assertEqual(me.sent, [{'type': 'host_error', 'host': 'lan-box', 'code': 'switch_failed'}])
        self.assertEqual(self.stopped, [])
        self.assertEqual(self.directory.codes, {})

    def test_connecting_again_lifts_the_disconnect(self):
        self.directory.codes['lan-box'] = 'disconnected'

        async def ensure_master(host, ask=None):
            return self.directory.codes.get(host.name, '')
        with patch.object(server.tmux_hosts, 'ensure_master', ensure_master):
            code = asyncio.run(server._sign_in(_Client(), 'lan-box'))
        self.assertEqual(code, '')

    def test_only_remote_hosts_can_be_disconnected(self):
        with self.assertRaises(server.PolicyViolation):
            server._validated_message(server.json.dumps({'type': 'disconnect_host', 'host': 'local'}))
        server._validated_message(server.json.dumps({'type': 'disconnect_host', 'host': 'lan-box'}))


class UnknownPaneTest(unittest.TestCase):
    """A pane the server hasn't listed yet is looked up, not a violation."""

    def setUp(self):
        self.tmux = _SwitchingTmux()
        patcher = patch.multiple(server, tmux=self.tmux, _allowed_panes={'%0'})
        patcher.start()
        self.addCleanup(patcher.stop)

    def handle(self, **msg):
        msg = server._validated_message(server.json.dumps(msg))
        asyncio.run(server._handle_msg(_Client(), msg))

    def test_a_pane_split_off_a_moment_ago_is_accepted(self):
        self.tmux.panes = ['%0', '%1']
        self.handle(type='input', pane='%1', data='ls\r', gen=0)
        self.assertEqual(self.tmux.inputs, [('%1', b'ls\r', 0)])
        self.assertEqual(server._allowed_panes, {'%0', '%1'})

    def test_a_pane_outside_the_window_is_dropped_without_closing(self):
        self.tmux.panes = ['%0']
        for msg in ({'type': 'input', 'pane': '%7', 'data': 'x', 'gen': 0},
                    {'type': 'get_snapshot', 'pane': '%7'},
                    {'type': 'select_pane', 'pane': '%7'},
                    {'type': 'split_window', 'direction': 'h', 'pane': '%7'}):
            with self.subTest(msg=msg['type']):
                self.handle(**msg)
        self.assertEqual(self.tmux.inputs, [])
        self.assertEqual(self.tmux.commands, [])

    def test_malformed_pane_ids_are_still_violations(self):
        for msg in ({'type': 'input', 'pane': '%1; kill-server', 'data': 'x', 'gen': 0},
                    {'type': 'get_snapshot', 'pane': 'x'},
                    {'type': 'split_window', 'direction': 'h', 'pane': '%%'}):
            with self.subTest(msg=msg['type']):
                with self.assertRaises(server.PolicyViolation):
                    server._validated_message(server.json.dumps(msg))


class ResizeMasterTest(unittest.TestCase):
    """Only someone using a browser makes it the one that sizes the window."""

    def setUp(self):
        self.tmux = _SwitchingTmux()
        patcher = patch.multiple(server, tmux=self.tmux, _allowed_panes={'%0'},
                                 _resize_master=None)
        patcher.start()
        self.addCleanup(patcher.stop)

    def type(self, client, data):
        msg = server._validated_message(server.json.dumps(
            {'type': 'input', 'pane': '%0', 'data': data, 'gen': 0}))
        asyncio.run(server._handle_msg(client, msg))

    def test_typing_takes_the_window_size(self):
        client = _Client()
        self.type(client, 'ls\r')
        self.assertIs(server._resize_master, client)

    def test_query_replies_and_focus_reports_do_not(self):
        # What xterm 5.3 sends by itself, which every open browser sends alike.
        replies = ('\x1b[?1;2c', '\x1b[>0;276;0c', '\x1b[0n', '\x1b[12;40R',
                   '\x1b[?2026;2$y', '\x1b[8;24;80t', '\x1b]11;rgb:1e1e/1e1e/1e1e\x1b\\',
                   '\x1bP1$r0m\x1b\\', '\x1b[>0;276;0c\x1b[?2026;2$y', '\x1b[I', '\x1b[O')
        for data in replies:
            with self.subTest(data=data):
                self.type(_Client(), data)
                self.assertIsNone(server._resize_master)
        # They still reach the pane: the application asked for them.
        self.assertEqual(len(self.tmux.inputs), len(replies))


class StaleViewTest(unittest.TestCase):
    """Pane ids from a view of the host we've left must not reach the new one."""

    def setUp(self):
        self.tmux = _SwitchingTmux()
        self.tmux.target_gen = 2
        patcher = patch.multiple(server, tmux=self.tmux, _allowed_panes={'%0'})
        patcher.start()
        self.addCleanup(patcher.stop)

    def handle(self, **msg):
        msg = server._validated_message(server.json.dumps(msg))
        asyncio.run(server._handle_msg(_Client(), msg))

    def test_input_from_the_previous_view_is_dropped(self):
        self.handle(type='input', pane='%0', data='rm -rf ~\r', gen=1)
        self.assertEqual(self.tmux.inputs, [])

    def test_input_from_the_current_view_carries_its_generation(self):
        self.handle(type='input', pane='%0', data='ls\r', gen=2)
        self.assertEqual(self.tmux.inputs, [('%0', b'ls\r', 2)])

    def test_input_must_say_which_view_it_came_from(self):
        for gen in (None, '2', True, -1):
            msg = {'type': 'input', 'pane': '%0', 'data': 'x'}
            if gen is not None:
                msg['gen'] = gen
            with self.subTest(gen=gen):
                with self.assertRaises(server.PolicyViolation):
                    server._validated_message(server.json.dumps(msg))

    def test_other_pane_messages_from_the_previous_view_are_dropped(self):
        self.handle(type='split_window', direction='h', pane='%0', gen=1)
        self.handle(type='select_pane', pane='%0', gen=1)
        self.assertEqual(self.tmux.commands, [])
        self.handle(type='select_pane', pane='%0', gen=2)
        self.assertEqual(self.tmux.commands, ['select-pane -t %0'])

    def test_views_carry_the_generation(self):
        state = asyncio.run(self.tmux.get_initial_state())
        with patch.object(server, 'DIRECTORY', _FakeDirectory()):
            msg = server.json.loads(server._view_message('init', state))
        self.assertEqual(msg['gen'], 2)


class HostMessageValidationTest(unittest.TestCase):
    def setUp(self):
        patcher = patch.multiple(
            server,
            HOSTS={'local': server.LOCAL_HOST, 'lan-box': REMOTE},
            tmux=_SwitchingTmux(),
            _allowed_sessions={'local': {'web'}, 'lan-box': {'dev'}},
            _allowed_windows={0},
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    def valid(self, **msg):
        return server._validated_message(server.json.dumps(msg))

    def test_session_must_exist_on_the_named_host(self):
        self.valid(type='select_session', session='dev', host='lan-box')
        self.valid(type='select_session', session='web')
        with self.assertRaises(server.PolicyViolation):
            self.valid(type='select_session', session='web', host='lan-box')
        with self.assertRaises(server.PolicyViolation):
            self.valid(type='kill_session', session='dev')

    def test_unknown_host_is_rejected(self):
        for host in ('elsewhere', 42, '', '-oProxyCommand=x'):
            with self.subTest(host=host):
                with self.assertRaises(server.PolicyViolation):
                    self.valid(type='new_session', host=host)

    def test_window_from_another_hosts_view_is_not_a_violation(self):
        self.valid(type='kill_window', window=7, host='lan-box')
        with self.assertRaises(server.PolicyViolation):
            self.valid(type='kill_window', window=7)
        with self.assertRaises(server.PolicyViolation):
            self.valid(type='kill_window', window=7, host='local')

    def test_window_handler_ignores_a_stale_host(self):
        runs = []

        async def run(*args, host=None):
            runs.append(args)
            return 0
        with patch.object(server, '_run_tmux', run):
            asyncio.run(server._handle_msg(_Client(), {'type': 'kill_window', 'window': 0,
                                                        'host': 'lan-box'}))
        self.assertEqual(runs, [])


class SignInTest(unittest.TestCase):
    """A host that wants a password: prompts go to the browser that asked,
    and the switch it was trying to make carries on afterwards."""

    def setUp(self):
        self.directory = _FakeDirectory({'lan-box': 'auth_required'})
        self.tmux = _SwitchingTmux()
        self.master_calls = []
        self.password = 's3cret'

        async def ensure_master(host, ask=None):
            self.master_calls.append(host.name)
            for _ in range(3):
                answer = await ask("me@lan-box's password: ")
                if answer == self.password:
                    self.directory.codes = {}
                    return ''
            return 'auth_required'

        async def run(*args, host=None):
            return 0

        patcher = patch.multiple(
            server,
            HOSTS={'local': server.LOCAL_HOST, 'lan-box': REMOTE},
            DIRECTORY=self.directory,
            tmux=self.tmux,
            _run_tmux=run,
            _refresh_hosts_soon=lambda force=False: None,
            _allowed_sessions={},
            _auth_prompts={},
        )
        patcher.start()
        self.addCleanup(patcher.stop)
        patcher = patch.object(server.tmux_hosts, 'ensure_master', ensure_master)
        patcher.start()
        self.addCleanup(patcher.stop)

    async def answer_prompts(self, client, answers):
        """Plays the browser: answers each prompt as it shows up."""
        for answer in answers:
            while not any(m['type'] == 'host_auth_prompt' for m in client.sent):
                await asyncio.sleep(0.001)
            prompt = next(m for m in client.sent if m['type'] == 'host_auth_prompt')
            client.sent.remove(prompt)
            msg = {'type': 'host_auth_answer', 'id': prompt['id']}
            msg.update(answer)
            await server._handle_msg(client, server._validated_message(server.json.dumps(msg)))

    def test_switch_signs_in_then_switches(self):
        me = _Client()
        self.tmux.subscribers.append(me)

        async def scenario():
            await server._switch_host(me, 'lan-box', 'dev')
            await self.answer_prompts(me, [{'answer': 'wrong'}, {'answer': 's3cret'}])
            for _ in range(100):
                if self.tmux.host.name == 'lan-box':
                    break
                await asyncio.sleep(0.005)
        asyncio.run(scenario())

        self.assertEqual(self.tmux.host.name, 'lan-box')
        self.assertEqual(me.queued[-1]['type'], 'init')
        self.assertEqual(server._auth_prompts, {})

    def test_cancel_stops_asking_and_reports(self):
        me = _Client()

        async def scenario():
            server._start_sign_in(me, 'lan-box')
            await self.answer_prompts(me, [{'cancel': True}])
            for _ in range(100):
                if any(m['type'] == 'host_error' for m in me.sent):
                    break
                await asyncio.sleep(0.005)
        asyncio.run(scenario())

        # ensure_master asked three times; only the first reached the browser.
        self.assertEqual([m['type'] for m in me.sent], ['host_error'])
        self.assertEqual(me.sent[0]['code'], 'auth_cancelled')
        self.assertEqual(self.tmux.host.name, 'local')

    def test_only_the_asked_client_can_answer(self):
        me, other = _Client(), _Client()

        async def scenario():
            server._start_sign_in(me, 'lan-box')
            while not me.sent:
                await asyncio.sleep(0.001)
            prompt_id = me.sent[0]['id']
            await server._handle_msg(other, {'type': 'host_auth_answer', 'id': prompt_id,
                                             'answer': 's3cret'})
            return server._auth_prompts[prompt_id][1].done()
        self.assertFalse(asyncio.run(scenario()))

    def test_answer_validation(self):
        ok = {'type': 'host_auth_answer', 'id': 'abc_DEF-123', 'answer': 'pässwörd !"#$'}
        server._validated_message(server.json.dumps(ok))
        server._validated_message(server.json.dumps({'type': 'host_auth_answer', 'id': 'a',
                                                     'cancel': True}))
        for bad in ({'answer': 'a\nb'}, {'answer': 'x' * 1025}, {'answer': 42},
                    {'id': '../x'}, {'id': ''}, {'cancel': 'yes'}):
            with self.subTest(bad=bad):
                with self.assertRaises(server.PolicyViolation):
                    server._validated_message(server.json.dumps({**ok, **bad}))
        with self.assertRaises(server.PolicyViolation):
            server._validated_message(server.json.dumps({'type': 'connect_host', 'host': 'local'}))
        server._validated_message(server.json.dumps({'type': 'connect_host', 'host': 'lan-box'}))


class SaveSkipsUnreachableHostTest(unittest.TestCase):
    def test_unreachable_host_is_named_not_fatal(self):
        async def capture(*args, host=None):
            if args[0] == 'list-sessions':
                return 0, 'web\t1\n'
            if args[0] == 'list-windows':
                return 0, '0\t' + LAYOUT_SOLO + '\t1\t80\t24\tw\n'
            return 0, '0\t%1\t0\t1\t/\n'

        with patch.multiple(server,
                            HOSTS={'local': server.LOCAL_HOST, 'lan-box': REMOTE},
                            DIRECTORY=_FakeDirectory({'lan-box': 'unreachable'}),
                            _run_tmux_capture=capture):
            snap, code, skipped = asyncio.run(server._capture_all_sessions('slot'))
        self.assertEqual(code, '')
        self.assertEqual(skipped, ['lan-box'])
        self.assertEqual([(s['host'], s['session']) for s in snap['sessions']], [('local', 'web')])


class _FakeTmuxControl:
    def __init__(self):
        # Deliberately not a name SNAP restores, so tests don't get a
        # switch-client dance by accident — only tests that opt in (by
        # setting self.tmux.session to a colliding name) exercise that path.
        self.session = 'scratch'
        self.host = server.LOCAL_HOST
        self.subscribers = []
        self.commands = []

    async def send_command(self, cmd):
        self.commands.append(cmd)
        return ''


async def _noop(*args, **kwargs):
    return None


class SnapshotDeliveryTest(unittest.TestCase):
    def test_snapshot_is_queued_with_broadcasts_not_sent_directly(self):
        class Client:
            def __init__(self):
                self.queued, self.sent = [], []

            def enqueue(self, data):
                self.queued.append(data)
                return True

            async def send(self, data):
                self.sent.append(data)

        async def fake_snapshot(pane, deliver):
            deliver({
                'data': b'row', 'cursor_x': 1, 'cursor_y': 2,
                'pane_cols': 80, 'pane_rows': 24, 'alternate_on': 0,
                'scroll_region_upper': 0, 'scroll_region_lower': 23,
            })

        client = Client()
        tmux = type('Tmux', (), {'snapshot_pane': staticmethod(fake_snapshot)})()
        with patch.multiple(server, tmux=tmux, _allowed_panes={'%3'}):
            asyncio.run(server._handle_msg(client, {'type': 'get_snapshot', 'pane': '%3'}))
        self.assertEqual(client.sent, [])
        self.assertEqual(len(client.queued), 1)
        msg = server.json.loads(client.queued[0])
        self.assertEqual(msg['type'], 'snapshot')
        self.assertEqual(msg['pane'], '%3')
        self.assertEqual(msg['data'], 'cm93')   # base64 of b'row'
        self.assertEqual(msg['scroll_region_lower'], 23)


if __name__ == '__main__':
    unittest.main()
