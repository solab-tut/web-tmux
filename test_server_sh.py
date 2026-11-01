"""Exercise launcher recovery with real processes and isolated TCP ports."""
import json
import os
from pathlib import Path
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
import unittest


ROOT = Path(__file__).resolve().parent
FAKE_SERVER = '''\
import os, signal, socket, time
sockets = []
for port in PORTS:
    s = socket.socket()
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    s.bind(('127.0.0.1', port))
    s.listen()
    sockets.append(s)
signal.signal(signal.SIGTERM, lambda *_: exit(0))
open('ready', 'w').write(str(os.getpid()))
while True:
    time.sleep(.1)
'''


@unittest.skipUnless(sys.platform.startswith('linux') and shutil.which('bash'),
                     'launcher PID recovery uses Linux /proc')
class ServerLauncherTest(unittest.TestCase):
    ORIGIN = 'https://box.example.ts.net:8766'

    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='web-tmux-launcher-')
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        with socket.socket() as sock:
            sock.bind(('127.0.0.1', 0))
            self.ports = [sock.getsockname()[1]]
        self.make_checkout(self.directory)
        self.env = {key: value for key, value in os.environ.items()
                    if not key.startswith('WEB_TMUX_')}
        self.env['PATH'] = str(self.directory / 'bin') + os.pathsep + os.environ['PATH']
        self.addCleanup(self.stop_checkout)

    def make_checkout(self, directory):
        (directory / '.venv/bin').mkdir(parents=True)
        (directory / '.venv/bin/python').symlink_to(sys.executable)
        (directory / 'bin').mkdir()
        tmux = directory / 'bin/tmux'
        tmux.write_text('#!/bin/sh\nexit 0\n')
        tmux.chmod(0o700)
        # Stands in for the real CLI, which on a developer's machine would
        # report that machine's serve config: `serve status --json` answers
        # with serve.json, or fails as if tailscaled were down.
        tailscale = directory / 'bin/tailscale'
        tailscale.write_text('#!/bin/sh\nexec cat "$(dirname "$0")/../serve.json"\n')
        tailscale.chmod(0o700)
        launcher = (ROOT / 'server.sh').read_text()
        launcher = launcher.replace('PORT="8766"', f'PORT="{self.ports[0]}"')
        (directory / 'server.sh').write_text(launcher)
        config = directory / '.web-tmux.env'
        config.write_text(f'WEB_TMUX_ALLOWED_ORIGINS={self.ORIGIN}\n'
                          'WEB_TMUX_TAILSCALE_USERS=me@example.com\n')
        config.chmod(0o600)
        (directory / 'server.py').write_text(f'PORTS = {self.ports!r}\n' + FAKE_SERVER)

    def run_launcher(self, *args):
        return subprocess.run(['bash', str(self.directory / 'server.sh'), *args],
                              env=self.env, text=True, capture_output=True, timeout=20)

    def stop_checkout(self):
        result = self.run_launcher('stop')
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def start_process(self, directory=None, script='server.py'):
        directory = directory or self.directory
        (directory / 'ready').unlink(missing_ok=True)
        proc = subprocess.Popen([str(directory / '.venv/bin/python'), script],
                                cwd=directory, env=self.env,
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        def cleanup():
            if proc.poll() is None:
                proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=5)
        self.addCleanup(cleanup)
        deadline = time.monotonic() + 5
        while not (directory / 'ready').exists():
            self.assertIsNone(proc.poll(), 'test server exited before binding its ports')
            if time.monotonic() >= deadline:
                self.fail('test server did not start')
            time.sleep(.02)
        return proc

    def assert_listening(self):
        for port in self.ports:
            with socket.create_connection(('127.0.0.1', port), timeout=1):
                pass

    def test_start_and_repeated_restart_release_the_port(self):
        pids = []
        for args in [(), ('restart',), ('start',)]:
            result = self.run_launcher(*args)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            pids.append(int((self.directory / 'server.pid').read_text()))
            self.assert_listening()
        self.assertEqual(len(set(pids)), 3)

    def test_restart_recovers_missing_pid_file(self):
        old = self.start_process()
        result = self.run_launcher('restart')
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn('recovering server', result.stdout)
        self.assertIsNotNone(old.poll())
        self.assertNotEqual(int((self.directory / 'server.pid').read_text()), old.pid)
        self.assert_listening()

    def test_stale_pid_is_preserved_while_checkout_server_is_recovered(self):
        (self.directory / 'other.py').write_text('import time\nwhile True: time.sleep(.1)\n')
        other = subprocess.Popen([sys.executable, 'other.py'], cwd=self.directory)
        self.addCleanup(other.wait)
        self.addCleanup(other.terminate)
        old = self.start_process()
        (self.directory / 'server.pid').write_text(str(other.pid))
        result = self.run_launcher()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn('unrelated pid', result.stderr)
        self.assertIsNone(other.poll())
        self.assertIsNotNone(old.poll())
        self.assert_listening()

    def test_another_checkout_on_the_same_port_is_not_stopped(self):
        temporary = tempfile.TemporaryDirectory(prefix='web-tmux-other-')
        self.addCleanup(temporary.cleanup)
        other_dir = Path(temporary.name)
        self.make_checkout(other_dir)
        other = self.start_process(other_dir)
        (self.directory / 'server.pid').write_text(str(other.pid))
        result = self.run_launcher()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('already in use', result.stderr)
        self.assertIsNone(other.poll())
        self.assert_listening()

    def test_unrelated_listener_in_same_directory_is_not_stopped(self):
        shutil.copyfile(self.directory / 'server.py', self.directory / 'other.py')
        other = self.start_process(script='other.py')
        (self.directory / 'server.pid').write_text(str(other.pid))
        result = self.run_launcher()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('already in use', result.stderr)
        self.assertIsNone(other.poll())
        self.assert_listening()

    def test_stop_recovers_missing_pid_file_even_before_child_is_reaped(self):
        old = self.start_process()
        result = self.run_launcher('stop')
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIsNotNone(old.poll())
        self.assertFalse((self.directory / 'server.pid').exists())
        for port in self.ports:
            with socket.socket() as sock:
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                sock.bind(('127.0.0.1', port))

    def write_serve_status(self, web):
        (self.directory / 'serve.json').write_text(json.dumps({'Web': web}))

    def test_start_says_which_address_to_open(self):
        result = self.run_launcher()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn(f'Open  {self.ORIGIN}/', result.stdout)

    def test_missing_configuration_stops_with_a_pointer(self):
        (self.directory / '.web-tmux.env').unlink()
        result = self.run_launcher()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('.web-tmux.env', result.stderr)
        self.assertNotIn('Traceback', result.stderr)
        self.assertFalse((self.directory / 'server.pid').exists())

    def test_serve_config_that_reaches_the_server_brings_no_warning(self):
        self.write_serve_status({'box.example.ts.net:8766': {
            'Handlers': {'/': {'Proxy': f'http://127.0.0.1:{self.ports[0]}'}}}})
        result = self.run_launcher()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertNotIn('WARNING', result.stderr)

    def test_missing_serve_and_the_old_ws_port_are_pointed_out(self):
        self.write_serve_status({'box.example.ts.net:8765': {
            'Handlers': {'/': {'Proxy': 'http://127.0.0.1:8765'}}}})
        result = self.run_launcher()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn(f'tailscale serve --bg --https=8766 http://127.0.0.1:{self.ports[0]}',
                      result.stderr)
        self.assertIn('tailscale serve --https=8765 off', result.stderr)
