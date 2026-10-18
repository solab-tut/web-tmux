import asyncio
import os
import shlex
import shutil
import subprocess
import tempfile
import unittest

import tmux_hosts
from tmux_hosts import LOCAL_HOST, HostDirectory, TmuxHost


REMOTE = TmuxHost('lan-box', 'tmux', '/state/ssh')


def _remote_words(argv):
    """The command the remote login shell gets, split the way it would."""
    i = argv.index('--')
    alias, command = argv[i + 1:]
    return alias, shlex.split(command)


class ArgvTest(unittest.TestCase):
    def test_local_runs_tmux_directly(self):
        self.assertEqual(LOCAL_HOST.argv(['list-sessions']), ['tmux', 'list-sessions'])
        self.assertEqual(LOCAL_HOST.control_argv('web'),
                         ['tmux', '-CC', 'attach-session', '-t', 'web'])

    def test_remote_alias_follows_double_dash_and_is_followed_by_one_command(self):
        argv = REMOTE.argv(['has-session', '-t', '=web'])
        self.assertEqual(argv[0], 'ssh')
        alias, words = _remote_words(argv)
        self.assertEqual(alias, 'lan-box')
        self.assertEqual(words, ['exec', 'tmux', 'has-session', '-t', '=web'])
        self.assertIn('-T', argv)
        self.assertNotIn('-tt', argv)
        self.assertIn('ControlMaster=no', argv)
        self.assertIn('BatchMode=yes', argv)
        self.assertIn('StrictHostKeyChecking=yes', argv)
        self.assertIn('ControlPath=/state/ssh/ssh-%C', argv)

    def test_control_client_gets_a_tty_without_escape_char(self):
        argv = REMOTE.control_argv('web')
        self.assertIn('-tt', argv)
        self.assertEqual(argv[argv.index('-e') + 1], 'none')
        self.assertLess(argv.index('-tt'), argv.index('--'))
        _, words = _remote_words(argv)
        self.assertEqual(words, ['exec', 'tmux', '-u', '-CC', 'attach-session', '-t', 'web'])

    def test_hostile_session_names_survive_the_remote_shell(self):
        for name in ('a b', "x'y", '$(id)', ';rm -rf ~', '`id`', 'back\\slash', '日本語', '"q"'):
            with self.subTest(name=name):
                _, words = _remote_words(REMOTE.argv(['new-session', '-d', '-s', name]))
                self.assertEqual(words, ['exec', 'tmux', 'new-session', '-d', '-s', name])

    def test_master_is_started_explicitly(self):
        argv = REMOTE.master_argv()
        for flag in ('-N', '-f'):
            self.assertIn(flag, argv)
        self.assertIn('ControlMaster=yes', argv)
        # -M on top of ControlMaster=yes would make it an "ask" master that
        # wants every shared session approved through askpass.
        self.assertNotIn('-M', argv)
        self.assertNotIn('-M', REMOTE.master_argv(interactive=True))
        self.assertIn('ControlPersist=10m', argv)
        self.assertEqual(argv[-2:], ['--', 'lan-box'])

    def test_interactive_master_may_prompt_and_lives_longer(self):
        argv = REMOTE.master_argv(interactive=True)
        self.assertIn('BatchMode=no', argv)
        self.assertNotIn('BatchMode=yes', argv)
        self.assertIn('ControlPersist=8h', argv)
        self.assertIn('BatchMode=yes', REMOTE.master_argv())

    def test_custom_tmux_path(self):
        host = TmuxHost('mac', '/opt/homebrew/bin/tmux', '/state/ssh')
        _, words = _remote_words(host.argv(['-V']))
        self.assertEqual(words, ['exec', '/opt/homebrew/bin/tmux', '-V'])


class HostsFromEnvTest(unittest.TestCase):
    def parse(self, value):
        return tmux_hosts.hosts_from_env({'WEB_TMUX_SSH_HOSTS': value}, directory='/state/ssh')

    def test_unset_means_local_only(self):
        self.assertEqual(tmux_hosts.hosts_from_env({}, directory='/state/ssh'), ())
        self.assertEqual(self.parse('  '), ())

    def test_aliases_and_tmux_paths(self):
        hosts = self.parse('a, b.lan:/opt/homebrew/bin/tmux')
        self.assertEqual([(h.name, h.tmux_bin) for h in hosts],
                         [('a', 'tmux'), ('b.lan', '/opt/homebrew/bin/tmux')])

    def test_anything_but_a_plain_alias_fails_closed(self):
        for value in ('-oProxyCommand=x', 'user@box', 'box:22', 'local', 'a,a', 'a,,b',
                      'a b', 'box:tmux', 'box:/bin/t mux', "box:/bin/x'"):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    self.parse(value)

    def test_local_tmux_does_not_inherit_our_settings(self):
        env = tmux_hosts.tmux_env({'WEB_TMUX_STATE_DIR': '/dev-state', 'WEB_TMUX_SSH_HOSTS': 'a',
                                   'HOME': '/home/me', 'PATH': '/bin'})
        self.assertEqual(env, {'HOME': '/home/me', 'PATH': '/bin'})

    def test_socket_dir_must_fit_a_unix_socket_path(self):
        with self.assertRaises(ValueError):
            tmux_hosts.hosts_from_env({'WEB_TMUX_SSH_HOSTS': 'a'}, directory='/' + 'x' * 80)


class VersionAndClassifyTest(unittest.TestCase):
    def test_versions(self):
        cases = {
            'tmux 3.6': (3, 6), 'tmux 3.3a': (3, 3), 'tmux next-3.5': (3, 5),
            'tmux master': (99, 0), 'tmux openbsd-7.4': (99, 0), 'tmux 2.9a': (2, 9),
            'bash: tmux: command not found': None, '': None,
        }
        for text, version in cases.items():
            with self.subTest(text=text):
                self.assertEqual(tmux_hosts.parse_tmux_version(text), version)
        self.assertTrue(tmux_hosts.version_ok((3, 2)))
        self.assertFalse(tmux_hosts.version_ok((3, 1)))
        self.assertFalse(tmux_hosts.version_ok(None))

    def test_classify(self):
        self.assertEqual(tmux_hosts.classify(0, ''), 'ok')
        self.assertEqual(tmux_hosts.classify(255, 'ssh: connect to host'), 'unreachable')
        self.assertEqual(tmux_hosts.classify(1, 'no server running on /tmp/tmux-1/default'), 'no_server')
        self.assertEqual(tmux_hosts.classify(1, 'error connecting to /tmp/tmux-1/default'), 'no_server')
        self.assertEqual(tmux_hosts.classify(127, 'sh: tmux: not found'), 'no_tmux')
        self.assertEqual(tmux_hosts.classify(1, 'something'), 'error')


class OutputLimitTest(unittest.TestCase):
    """Output is bounded while it is read: an endless writer is stopped,
    not buffered until the timeout."""

    def run_argv(self, script, timeout=10.0):
        return asyncio.run(tmux_hosts.run_argv(['sh', '-c', script], timeout=timeout))

    def test_endless_stdout_is_stopped_at_the_limit(self):
        rc, out, err = self.run_argv('yes')
        self.assertEqual(rc, tmux_hosts.RC_TOO_LARGE)
        self.assertEqual(out, '')
        self.assertEqual(err, 'output too large')

    def test_output_within_the_limit_is_returned_whole(self):
        size = tmux_hosts.CAPTURE_LIMIT
        rc, out, _ = self.run_argv(f'head -c {size} /dev/zero | tr "\\0" x')
        self.assertEqual(rc, 0)
        self.assertEqual(len(out), size)

    def test_stderr_flood_is_drained_and_cut(self):
        rc, out, err = self.run_argv('head -c 3000000 /dev/zero | tr "\\0" e >&2; echo done')
        self.assertEqual((rc, out), (0, 'done\n'))
        self.assertEqual(len(err), tmux_hosts.STDERR_LIMIT)

    def test_stdin_is_fed(self):
        rc, out, _ = asyncio.run(tmux_hosts.run_argv(['cat'], timeout=5.0, stdin=b'a\nb\n'))
        self.assertEqual((rc, out), (0, 'a\nb\n'))


class _Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


class HostDirectoryTest(unittest.TestCase):
    def setUp(self):
        self.calls = []
        self.answers = {}       # host -> (rc, out, err) for list-sessions
        self.delays = {}
        self.masters = {}       # host -> ensure_master result
        self.changes = 0
        self.clock = _Clock()
        self.hosts = [LOCAL_HOST, TmuxHost('a', 'tmux', '/s'), TmuxHost('b', 'tmux', '/s')]

    async def runner(self, host, *args, timeout=None):
        self.calls.append((host.name, args[0]))
        await asyncio.sleep(self.delays.get(host.name, 0))
        if args[0] == '-V':
            return 0, 'tmux 3.4\n', ''
        return self.answers.get(host.name, (0, '', ''))

    async def master(self, host):
        return self.masters.get(host.name, '')

    def directory(self):
        def changed():
            self.changes += 1
        return HostDirectory(self.hosts, on_change=changed, runner=self.runner,
                             master=self.master, clock=self.clock)

    def test_lists_sessions_per_host(self):
        self.answers = {'local': (0, 'web\t2\t1\n', ''), 'a': (0, 'dev\t1\t0\nx y\t3\t0\n', '')}
        d = self.directory()
        asyncio.run(d.refresh_all())
        snap = {h['host']: h for h in d.snapshot()}
        self.assertEqual(snap['local']['sessions'], [{'name': 'web', 'windows': 2, 'attached': True}])
        self.assertEqual([s['name'] for s in snap['a']['sessions']], ['dev', 'x y'])
        self.assertTrue(snap['b']['reachable'])
        self.assertEqual(d.sessions('a'), {'dev', 'x y'})

    def test_no_tmux_server_is_reachable_and_empty(self):
        self.answers = {'a': (1, '', 'no server running on /tmp/tmux-1000/default')}
        d = self.directory()
        asyncio.run(d.refresh('a'))
        self.assertTrue(d.status['a'].reachable)
        self.assertEqual(d.status['a'].sessions, [])

    def test_unreachable_host_backs_off(self):
        self.masters = {'a': 'unreachable'}
        d = self.directory()
        asyncio.run(d.refresh('a'))
        self.assertFalse(d.status['a'].reachable)
        self.assertEqual(d.status['a'].code, 'unreachable')
        self.clock.now += 20
        self.assertFalse(d._due('a'))
        self.clock.now += 20
        self.assertTrue(d._due('a'))
        asyncio.run(d.refresh('a'))
        self.clock.now += 40
        self.assertFalse(d._due('a'))     # second failure: 60 s
        self.clock.now += 30
        self.assertTrue(d._due('a'))

    def test_old_tmux_is_unsupported(self):
        async def old(host, *args, timeout=None):
            return (0, 'tmux 3.0a\n', '') if args[0] == '-V' else (0, '', '')
        d = HostDirectory(self.hosts, runner=old, master=self.master, clock=self.clock)
        asyncio.run(d.refresh('a'))
        self.assertEqual(d.status['a'].code, 'unsupported_tmux')
        self.assertFalse(d.status['a'].reachable)

    def test_slow_host_does_not_hold_up_the_others(self):
        self.delays = {'b': 0.3}
        d = self.directory()

        async def scenario():
            task = asyncio.ensure_future(d.refresh_all())
            await asyncio.sleep(0.05)
            done_early = d.status['a'].reachable
            await task
            return done_early
        self.assertTrue(asyncio.run(scenario()))

    def test_concurrent_refreshes_share_one_call(self):
        self.delays = {'a': 0.05}
        d = self.directory()

        async def scenario():
            await asyncio.gather(d.refresh('a', force=True), d.refresh('a', force=True))
        asyncio.run(scenario())
        self.assertEqual(self.calls.count(('a', 'list-sessions')), 1)

    def test_on_change_fires_only_when_something_changed(self):
        self.answers = {'a': (0, 'dev\t1\t0\n', '')}
        d = self.directory()
        asyncio.run(d.refresh('a'))
        self.assertEqual(self.changes, 1)
        asyncio.run(d.refresh('a', force=True))
        self.assertEqual(self.changes, 1)
        self.answers = {'a': (0, 'dev\t1\t1\n', '')}
        asyncio.run(d.refresh('a', force=True))
        self.assertEqual(self.changes, 2)


class AuthFailureTest(unittest.TestCase):
    def test_tells_credentials_from_network(self):
        self.assertTrue(tmux_hosts.auth_failed('me@10.0.0.2: Permission denied (publickey,password).'))
        self.assertTrue(tmux_hosts.auth_failed('Received disconnect: Too many authentication failures'))
        self.assertFalse(tmux_hosts.auth_failed('ssh: connect to host 10.0.0.2 port 22: No route to host'))
        self.assertFalse(tmux_hosts.auth_failed(''))


@unittest.skipUnless(shutil.which('tmux'), 'tmux is not installed')
class FakeSshIntegrationTest(unittest.TestCase):
    """Real tmux behind a stand-in `ssh` that runs the remote command with
    sh -c, the way sshd hands it to a POSIX login shell. Checks the quoting
    and the restore cwd check end to end without an sshd."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        bin_dir = os.path.join(self.tmp, 'bin')
        os.mkdir(bin_dir)
        ssh = os.path.join(bin_dir, 'ssh')
        with open(ssh, 'w') as fh:
            fh.write('#!/bin/sh\n'
                     'while [ "$1" != "--" ]; do shift; done\n'
                     'shift 2\n'                       # '--' and the alias
                     'exec sh -c "$1"\n')
        os.chmod(ssh, 0o755)
        wrapper = os.path.join(bin_dir, 'tmux-test')
        self.socket = os.path.join(self.tmp, 'tmux.sock')
        with open(wrapper, 'w') as fh:
            fh.write(f'#!/bin/sh\nexec tmux -S {shlex.quote(self.socket)} "$@"\n')
        os.chmod(wrapper, 0o755)
        self.host = TmuxHost('fake', wrapper, self.tmp)
        self.path = os.environ['PATH']
        os.environ['PATH'] = bin_dir + os.pathsep + self.path
        self.addCleanup(self._restore)

    def _restore(self):
        os.environ['PATH'] = self.path
        subprocess.run(['tmux', '-S', self.socket, 'kill-server'],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    def test_session_name_round_trips_through_the_remote_shell(self):
        name = "it's $(id) `x` ; a"

        async def scenario():
            rc, _, _ = await tmux_hosts.run(self.host, 'new-session', '-d', '-s', name)
            self.assertEqual(rc, 0)
            rc, out, _ = await tmux_hosts.run(self.host, 'list-sessions', '-F', '#{session_name}')
            return rc, out
        rc, out = asyncio.run(scenario())
        self.assertEqual(rc, 0)
        self.assertEqual(out.strip(), name)

    def test_existing_dirs_checks_on_the_host(self):
        os.environ.setdefault('WEB_TMUX_ALLOWED_ORIGINS', 'https://host.example.ts.net:8766')
        os.environ.setdefault('WEB_TMUX_TAILSCALE_USERS', 'owner@example.com')
        import server
        here = os.path.join(self.tmp, "dir with 'quote'")
        os.mkdir(here)
        found = asyncio.run(server._existing_dirs(self.host, [here, '/definitely/gone']))
        self.assertIsNotNone(found)
        existing, home = found
        self.assertEqual(existing, {here})
        self.assertTrue(home.startswith('/'))


class SignInIntegrationTest(unittest.TestCase):
    """The whole askpass path with real processes: a stand-in `ssh` whose
    master asks "$SSH_ASKPASS" for a password the way OpenSSH does, the
    real helper, the broker's socket, and an `ask` standing in for the
    browser."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='wt', dir='/tmp')   # short: unix socket paths
        self.addCleanup(shutil.rmtree, self.tmp, True)
        bin_dir = os.path.join(self.tmp, 'bin')
        os.mkdir(bin_dir)
        self.up = os.path.join(self.tmp, 'up')
        ssh = os.path.join(bin_dir, 'ssh')
        with open(ssh, 'w') as fh:
            fh.write(f'''#!/bin/sh
case " $* " in
  *" -O check "*) [ -e {self.up} ] && exit 0; exit 255 ;;
  *" -O exit "*) rm -f {self.up}; exit 0 ;;
  *"ControlMaster=yes"*)
    case " $* " in *"BatchMode=yes"*) echo "me@box: Permission denied (password)." >&2; exit 255 ;; esac
    tries=0
    while [ $tries -lt 3 ]; do
      tries=$((tries+1))
      pw=$("$SSH_ASKPASS" "me@box's password: ") || break
      [ "$pw" = "s3cret" ] && touch {self.up} && exit 0
    done
    echo "me@box: Permission denied (password)." >&2; exit 255 ;;
esac
exit 255
''')
        os.chmod(ssh, 0o755)
        self.path = os.environ['PATH']
        os.environ['PATH'] = bin_dir + os.pathsep + self.path
        self.addCleanup(lambda: os.environ.__setitem__('PATH', self.path))
        self.host = TmuxHost('box', 'tmux', self.tmp)

    def run_sign_in(self, answers, interactive=True):
        import askpass
        prompts = []

        async def scenario():
            broker = askpass.AskpassBroker(self.tmp)
            await broker.start()
            tmux_hosts.configure_askpass(broker)
            tmux_hosts._masters.clear()
            try:
                async def ask(prompt):
                    prompts.append(prompt)
                    return answers.pop(0) if answers else None
                return await tmux_hosts.ensure_master(self.host, ask=ask if interactive else None)
            finally:
                tmux_hosts.configure_askpass(None)
                await broker.stop()
        return asyncio.run(scenario()), prompts

    def test_right_password_signs_in(self):
        code, prompts = self.run_sign_in(['s3cret'])
        self.assertEqual(code, '')
        self.assertEqual(prompts, ["me@box's password: "])

    def test_retry_after_a_wrong_password(self):
        code, prompts = self.run_sign_in(['nope', 's3cret'])
        self.assertEqual(code, '')
        self.assertEqual(len(prompts), 2)

    def test_cancel_is_auth_required(self):
        code, prompts = self.run_sign_in([])
        self.assertEqual(code, 'auth_required')
        self.assertEqual(len(prompts), 1)

    def test_background_start_never_asks(self):
        code, prompts = self.run_sign_in(['s3cret'], interactive=False)
        self.assertEqual(code, 'auth_required')
        self.assertEqual(prompts, [])

    def test_stop_masters_closes_the_signed_in_connection(self):
        code, _ = self.run_sign_in(['s3cret'])
        self.assertEqual(code, '')
        self.assertTrue(os.path.exists(self.up))

        asyncio.run(tmux_hosts.stop_masters([LOCAL_HOST, self.host]))

        self.assertFalse(os.path.exists(self.up))
        self.assertNotIn('box', tmux_hosts._masters)

    def test_helper_without_a_token_refuses(self):
        import askpass
        self.assertEqual(askpass.main(['askpass', 'pw:'], {}), 1)
        self.assertEqual(askpass.main(['askpass', 'ok?'], {'SSH_ASKPASS_PROMPT': 'confirm',
                                                          askpass.TOKEN_ENV: 't',
                                                          askpass.SOCKET_ENV: '/x'}), 1)


if __name__ == '__main__':
    unittest.main()
