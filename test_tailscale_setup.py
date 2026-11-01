import contextlib
import io
import json
import os
import shutil
import stat
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import tailscale_setup
from tailscale_setup import SetupError

ROOT = Path(__file__).resolve().parent
HOST = 'box.tail1234.ts.net'
ORIGIN = f'https://{HOST}:8766'
FORWARDED = {'Web': {f'{HOST}:8766': {'Handlers': {'/': {'Proxy': 'http://127.0.0.1:8766'}}}}}

# Stands in for the tailscale CLI: `status` and `serve status` answer from
# JSON files beside it, and anything else is recorded in calls.log.
FAKE_CLI = '''\
import json, os, sys
here = os.path.dirname(os.path.abspath(__file__))
args = [a for a in sys.argv[1:] if a != '--json']
name = {('status',): 'status.json', ('serve', 'status'): 'serve.json'}.get(tuple(args))
if name is None:
    with open(os.path.join(here, 'calls.log'), 'a') as log:
        log.write(' '.join(args) + '\\n')
    sys.exit(0)
try:
    with open(os.path.join(here, name)) as stream:
        sys.stdout.write(stream.read())
except FileNotFoundError:
    sys.exit(1)
'''


def status(**changes):
    data = {
        'BackendState': 'Running',
        'CertDomains': [HOST],
        'Self': {'DNSName': HOST + '.', 'UserID': 7},
        'User': {'7': {'LoginName': 'you@example.com'}},
    }
    me = changes.pop('Self', None)
    if me is not None:
        data['Self'] = {**data['Self'], **me}
    data.update(changes)
    return data


class MachineTest(unittest.TestCase):
    def test_a_connected_machine_gives_its_address_and_owner(self):
        this = tailscale_setup.machine(status())
        self.assertEqual(this.origin, ORIGIN)
        self.assertEqual(this.login, 'you@example.com')

    def test_each_missing_piece_says_what_to_turn_on(self):
        cases = (
            (status(BackendState='NeedsLogin'), 'tailscale up'),
            (status(Self={'DNSName': ''}), 'MagicDNS'),
            (status(CertDomains=None), 'HTTPS Certificates'),
            (status(Self={'DNSName': 'box;rm -rf.ts.net.'}), 'by hand'),
        )
        for data, hint in cases:
            with self.subTest(hint=hint):
                with self.assertRaises(SetupError) as caught:
                    tailscale_setup.machine(data)
                self.assertIn(hint, str(caught.exception))

    def test_a_tagged_machine_names_no_login(self):
        self.assertIsNone(tailscale_setup.machine(status(Self={'Tags': ['tag:server']})).login)

    def test_only_plain_logins_go_into_a_file_bash_sources(self):
        for login in ('you@example.com', 'you@github', 'first.last+x@sub.example.org'):
            with self.subTest(login=login):
                self.assertTrue(tailscale_setup.valid_login(login))
        for login in ('', 'a b@example.com', '$(id)@example.com', 'x@example.com\nEVIL=1', "o'neil@x", 'a,b'):
            with self.subTest(login=login):
                self.assertFalse(tailscale_setup.valid_login(login))


class ConfigTest(unittest.TestCase):
    def test_the_example_is_filled_in_and_keeps_its_comments(self):
        example = (ROOT / '.web-tmux.env.example').read_text()
        text = tailscale_setup.config_text(example, ORIGIN, 'you@example.com')
        self.assertIn(f'WEB_TMUX_ALLOWED_ORIGINS={ORIGIN}\n', text)
        self.assertIn('WEB_TMUX_TAILSCALE_USERS=you@example.com\n', text)
        self.assertIn('# WEB_TMUX_SSH_HOSTS=', text)
        self.assertNotIn('example.ts.net', text)

    def test_written_at_mode_600_and_never_over_an_existing_file(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, '.web-tmux.env')
            tailscale_setup.write_config(path, 'A=1\n')
            self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o600)
            with self.assertRaises(FileExistsError):
                tailscale_setup.write_config(path, 'B=2\n')
            self.assertEqual(Path(path).read_text(), 'A=1\n')

    def test_origins_are_read_back_from_a_config(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, '.web-tmux.env')
            Path(path).write_text(f'# note\nWEB_TMUX_ALLOWED_ORIGINS="{ORIGIN}, https://b.ts.net:8766"\n')
            self.assertEqual(tailscale_setup.config_origins(path), [ORIGIN, 'https://b.ts.net:8766'])


class ServeProblemsTest(unittest.TestCase):
    def test_a_forwarded_port_is_fine(self):
        self.assertEqual(tailscale_setup.serve_problems(FORWARDED, [ORIGIN]), [])

    def test_a_missing_forward_comes_with_the_command(self):
        (message, args), = tailscale_setup.serve_problems({}, [ORIGIN])
        self.assertIn(ORIGIN, message)
        self.assertEqual(args, ['serve', '--bg', '--https=8766', 'http://127.0.0.1:8766'])

    def test_a_forward_to_something_else_is_a_problem(self):
        other = {'Web': {f'{HOST}:8766': {'Handlers': {'/': {'Proxy': 'http://127.0.0.1:9000'}}}}}
        self.assertEqual(len(tailscale_setup.serve_problems(other, [ORIGIN])), 1)

    def test_the_old_websocket_port_is_pointed_out_but_nothing_else(self):
        web = {'Web': {
            **FORWARDED['Web'],
            f'{HOST}:8765': {'Handlers': {'/': {'Proxy': 'http://127.0.0.1:8765'}}},
            f'{HOST}:9119': {'Handlers': {'/': {'Proxy': 'http://127.0.0.1:9119'}}},
        }}
        (message, args), = tailscale_setup.serve_problems(web, [ORIGIN])
        self.assertEqual(args, ['serve', '--https=8765', 'off'])

    def test_plain_http_origins_need_no_serve(self):
        self.assertEqual(tailscale_setup.serve_problems({}, ['http://127.0.0.2:8766']), [])


class SetupTest(unittest.TestCase):
    """setup as setup.sh runs it, in a checkout of its own, with the stand-in
    CLI and with sudo taken out."""

    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='web-tmux-setup-')
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        shutil.copyfile(ROOT / '.web-tmux.env.example', self.directory / '.web-tmux.env.example')
        self.cli = self.directory / 'tailscale'
        self.cli.write_text(f'#!{sys.executable}\n' + FAKE_CLI)
        self.cli.chmod(0o700)
        for patcher in (
            patch.object(tailscale_setup, 'find_cli', lambda: str(self.cli)),
            patch.object(tailscale_setup, 'sudo_prefix', lambda: []),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)
        cwd = os.getcwd()
        os.chdir(self.directory)
        self.addCleanup(os.chdir, cwd)

    def answer(self, name, data):
        (self.directory / name).write_text(json.dumps(data))

    def run_setup(self, assume_yes=False, tty=False):
        out = io.StringIO()
        with patch.object(sys.stdin, 'isatty', lambda: tty), contextlib.redirect_stdout(out):
            code = tailscale_setup.setup(assume_yes)
        return code, out.getvalue()

    def calls(self):
        log = self.directory / 'calls.log'
        return log.read_text().splitlines() if log.exists() else []

    def config(self):
        return self.directory / '.web-tmux.env'

    def test_yes_writes_the_config_and_forwards_the_port(self):
        self.answer('status.json', status())
        self.answer('serve.json', {})
        code, out = self.run_setup(assume_yes=True)
        self.assertEqual(code, 0, out)
        self.assertEqual(stat.S_IMODE(self.config().stat().st_mode), 0o600)
        text = self.config().read_text()
        self.assertIn(f'WEB_TMUX_ALLOWED_ORIGINS={ORIGIN}\n', text)
        self.assertIn('WEB_TMUX_TAILSCALE_USERS=you@example.com\n', text)
        self.assertEqual(self.calls(), ['serve --bg --https=8766 http://127.0.0.1:8766'])
        self.assertIn(f'open {ORIGIN}/', out)

    def test_a_forward_that_cannot_be_run_is_left_to_the_user(self):
        self.answer('status.json', status())
        self.answer('serve.json', {})
        with patch.object(tailscale_setup, 'sudo_prefix', lambda: ['/nonexistent/sudo']):
            code, out = self.run_setup(assume_yes=True)
        self.assertEqual(code, 0, out)
        self.assertIn('run the command above yourself', out)
        self.assertTrue(self.config().exists())

    def test_without_a_terminal_or_yes_nothing_is_written(self):
        self.answer('status.json', status())
        code, out = self.run_setup()
        self.assertEqual(code, 1)
        self.assertFalse(self.config().exists())
        self.assertIn('Nothing written', out)

    def test_an_existing_config_is_kept_and_its_origin_checked(self):
        self.config().write_text(f'WEB_TMUX_ALLOWED_ORIGINS={ORIGIN}\nWEB_TMUX_TAILSCALE_USERS=me@x.org\n')
        self.config().chmod(0o600)
        self.answer('serve.json', {})
        code, out = self.run_setup()
        self.assertEqual(code, 0, out)
        self.assertIn('existing', out)
        self.assertIn('me@x.org', self.config().read_text())
        self.assertIn('tailscale serve --bg --https=8766 http://127.0.0.1:8766', out)
        self.assertEqual(self.calls(), [], 'ran a command nobody agreed to')

    def test_a_config_server_sh_would_refuse_is_pointed_out(self):
        self.config().write_text(f'WEB_TMUX_ALLOWED_ORIGINS={ORIGIN}\n')
        self.config().chmod(0o644)
        self.answer('serve.json', FORWARDED)
        code, out = self.run_setup()
        self.assertEqual(code, 0, out)
        self.assertIn('chmod 600', out)

    def test_tailscale_not_ready_leaves_no_config(self):
        self.answer('status.json', status(BackendState='Stopped'))
        code, out = self.run_setup(assume_yes=True)
        self.assertEqual(code, 1)
        self.assertIn('tailscale up', out)
        self.assertFalse(self.config().exists())

    def test_tailscaled_not_answering_leaves_no_config(self):
        code, out = self.run_setup(assume_yes=True)   # no status.json: the CLI fails
        self.assertEqual(code, 1)
        self.assertIn('Is Tailscale running', out)
        self.assertFalse(self.config().exists())

    def test_no_cli_points_to_the_download(self):
        with patch.object(tailscale_setup, 'find_cli', lambda: None):
            code, out = self.run_setup(assume_yes=True)
        self.assertEqual(code, 1)
        self.assertIn('tailscale.com/download', out)

    def test_a_tagged_machine_asks_who_may_use_it(self):
        self.answer('status.json', status(Self={'Tags': ['tag:server']}))
        self.answer('serve.json', FORWARDED)
        code, out = self.run_setup(assume_yes=True)
        self.assertEqual(code, 1)
        self.assertIn('tagged', out)
        with patch('builtins.input', lambda prompt: 'owner@example.com'):
            code, out = self.run_setup(assume_yes=True, tty=True)
        self.assertEqual(code, 0, out)
        self.assertIn('WEB_TMUX_TAILSCALE_USERS=owner@example.com\n', self.config().read_text())


class CheckServeTest(unittest.TestCase):
    def test_server_sh_check_warns_on_stderr_only(self):
        with tempfile.TemporaryDirectory() as directory:
            cli = Path(directory) / 'tailscale'
            cli.write_text(f'#!{sys.executable}\n' + FAKE_CLI)
            cli.chmod(0o700)
            (Path(directory) / 'serve.json').write_text('{}')
            out, err = io.StringIO(), io.StringIO()
            with patch.object(tailscale_setup, 'find_cli', lambda: str(cli)), \
                    contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                self.assertEqual(tailscale_setup.check_serve(ORIGIN), 0)
            self.assertEqual(out.getvalue(), '')
            self.assertIn('WARNING', err.getvalue())
            self.assertIn('--https=8766', err.getvalue())


if __name__ == '__main__':
    unittest.main()
