"""Run install.sh against a local repository standing in for GitHub."""
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parent.parent

# The stand-in's setup.sh: records that it ran, and fails on request.
SETUP_STUB = '''#!/bin/sh
echo "setup ran in $(pwd)"
[ -f "$HOME/setup-fails" ] && { echo "Tailscale is not connected"; exit 1; }
exit 0
'''


@unittest.skipUnless(shutil.which('bash') and shutil.which('git'), 'needs bash and git')
class InstallTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='web-tmux-install-')
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.home = self.root / 'home'
        self.home.mkdir()
        self.env = {
            'PATH': os.environ['PATH'],
            'HOME': str(self.home),
            'GIT_CONFIG_NOSYSTEM': '1',
            'GIT_AUTHOR_NAME': 't', 'GIT_AUTHOR_EMAIL': 't@example.com',
            'GIT_COMMITTER_NAME': 't', 'GIT_COMMITTER_EMAIL': 't@example.com',
        }
        self.repo = self.root / 'repo'
        self.git('init', '-q', '-b', 'main', str(self.repo), cwd=self.root)
        for tag in ('v1.2', 'v1.9', 'v1.10'):
            self.commit(tag)
            self.git('tag', tag)
        self.commit('unreleased')
        self.env['WEB_TMUX_REPO'] = str(self.repo)
        self.target = self.home / 'web-tmux'

    def git(self, *args, cwd=None):
        return subprocess.run(['git', *args], cwd=cwd or self.repo, env=self.env,
                              check=True, capture_output=True, text=True).stdout.strip()

    def commit(self, label):
        (self.repo / 'setup.sh').write_text(SETUP_STUB)
        (self.repo / 'setup.sh').chmod(0o755)
        (self.repo / 'VERSION').write_text(label + '\n')
        self.git('add', '-A')
        self.git('commit', '-q', '-m', label)

    def install(self, path=None, **env):
        return subprocess.run(['bash', str(ROOT / 'install.sh')],
                              env={**self.env, **({'PATH': path} if path else {}), **env},
                              cwd=self.root, capture_output=True, text=True, timeout=60,
                              stdin=subprocess.DEVNULL)

    def installed(self):
        return (self.target / 'VERSION').read_text().strip()

    def test_a_fresh_install_takes_the_latest_release_and_runs_setup(self):
        result = self.install()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.installed(), 'v1.10')   # not v1.9: compared as numbers
        self.assertIn(f'setup ran in {self.target}', result.stdout)

    def test_running_again_updates_to_the_new_release(self):
        self.install()
        self.commit('v1.11')
        self.git('tag', 'v1.11')
        result = self.install()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.installed(), 'v1.11')
        self.assertIn('Updating', result.stdout)

    def test_a_branch_can_be_asked_for(self):
        result = self.install(WEB_TMUX_REF='main')
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.installed(), 'unreleased')
        self.commit('later')
        result = self.install(WEB_TMUX_REF='main')
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.installed(), 'later')

    def test_a_directory_that_is_not_web_tmux_is_left_alone(self):
        self.target.mkdir()
        (self.target / 'notes.txt').write_text('mine')
        result = self.install()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('WEB_TMUX_DIR', result.stderr)
        self.assertEqual(sorted(p.name for p in self.target.iterdir()), ['notes.txt'])

    def test_a_checkout_of_another_repository_is_left_alone(self):
        self.git('init', '-q', str(self.target), cwd=self.root)
        self.git('remote', 'add', 'origin', 'https://example.com/other.git', cwd=self.target)
        result = self.install()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('not web-tmux', result.stderr)

    def test_local_changes_stop_an_update(self):
        self.install()
        (self.target / 'VERSION').write_text('edited\n')
        result = self.install()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('local changes', result.stderr)
        self.assertEqual(self.installed(), 'edited')

    def test_missing_tools_are_listed_before_anything_is_cloned(self):
        # Every command on PATH except tmux.
        bin_dir = self.root / 'bin'
        bin_dir.mkdir()
        for directory in os.environ['PATH'].split(os.pathsep):
            if not os.path.isdir(directory):
                continue
            for name in os.listdir(directory):
                link = bin_dir / name
                if name != 'tmux' and not link.exists():
                    link.symlink_to(os.path.join(directory, name))
        result = self.install(path=str(bin_dir))
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('web-tmux needs: tmux', result.stdout)
        self.assertFalse(self.target.exists())

    def test_a_setup_that_cannot_finish_says_where_to_finish_it(self):
        (self.home / 'setup-fails').touch()
        result = self.install()
        self.assertEqual(result.returncode, 1)
        self.assertIn('Tailscale is not connected', result.stdout)
        self.assertIn(f'cd {self.target} && ./setup.sh', result.stdout)

    def test_ssh_and_https_urls_name_the_same_repository(self):
        script = (ROOT / 'install.sh').read_text().rsplit('main "$@"', 1)[0]
        keys = subprocess.run(
            ['bash', '-c', script + '''
for url in https://github.com/solab-tut/web-tmux.git git@github.com:solab-tut/web-tmux.git \\
           ssh://git@github.com/solab-tut/web-tmux https://github.com/solab-tut/web-tmux/; do
  repo_key "$url"
done'''], capture_output=True, text=True, check=True).stdout.split()
        self.assertEqual(set(keys), {'github.com/solab-tut/web-tmux'})


if __name__ == '__main__':
    unittest.main()
