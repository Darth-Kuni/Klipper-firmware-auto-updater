"""Exercise the Linux clone-and-run entry point without installation."""
from pathlib import Path
import os
import shutil
import subprocess
import sys
import tempfile
import unittest


@unittest.skipUnless(sys.platform.startswith('linux') and shutil.which('bash'), 'Linux Bash launcher checks')
class LauncherTests(unittest.TestCase):
    def setUp(self):
        if os.geteuid() == 0:
            self.skipTest('Success-path launcher checks run as a normal Linux user')
        self.project = Path(__file__).resolve().parents[1]
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.environment = os.environ.copy()
        self.environment.pop('PYTHONPATH', None)

    def test_cloned_launcher_runs_from_an_unrelated_directory(self):
        launcher = self.project / 'kcm.sh'
        self.assertTrue(os.access(launcher, os.X_OK), 'Git checkout must retain the executable bit')
        other_package = self.root / 'kcm'
        other_package.mkdir()
        (other_package / '__init__.py').write_text("raise AssertionError('Wrong checkout was loaded')\n", encoding='utf-8')
        result = subprocess.run([str(launcher), '--version'], cwd=self.root, env=self.environment,
                                text=True, capture_output=True, timeout=20)
        from kcm import __version__
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), __version__)

    def test_launcher_handles_spaces_and_forwards_command_arguments(self):
        clone = self.root / 'a checkout with spaces'
        clone.mkdir()
        shutil.copy2(self.project / 'kcm.sh', clone / 'kcm.sh')
        shutil.copytree(self.project / 'kcm', clone / 'kcm', ignore=shutil.ignore_patterns('__pycache__'))
        result = subprocess.run([str(clone / 'kcm.sh'), '--data-dir', str(self.root / 'profiles'), 'boards', 'EBB36'],
                                cwd=self.root, env=self.environment, text=True, capture_output=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('STM32G0B1CBT6', result.stdout)

    def test_unsupported_python_stops_before_starting_the_application(self):
        binary = self.root / 'bin'
        binary.mkdir()
        python = binary / 'python3'
        python.write_text('#!/bin/sh\nexit 1\n', encoding='utf-8')
        python.chmod(0o755)
        self.environment['PATH'] = str(binary) + os.pathsep + self.environment['PATH']
        result = subprocess.run([str(self.project / 'kcm.sh'), '--version'], cwd=self.root, env=self.environment,
                                text=True, capture_output=True, timeout=20)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('Python 3.10 or newer is required', result.stderr)

    def test_fresh_clone_configures_moonraker_for_that_checkout(self):
        clone = self.root / 'fresh-clone'
        environment = self.environment.copy()
        environment['GIT_ALLOW_PROTOCOL'] = 'file'
        def git(*arguments):
            return subprocess.run(['git', *arguments], env=environment, text=True, capture_output=True,
                                  check=True, timeout=30)
        git('clone', '--no-hardlinks', '--', str(self.project), str(clone))
        # Tag workflows start from a detached checkout. Make the fixture match
        # the normal main-branch clone a new user obtains from GitHub.
        git('-C', str(clone), 'checkout', '-B', 'main')
        git('-C', str(clone), 'update-ref', 'refs/remotes/origin/main', 'HEAD')
        git('-C', str(clone), 'config', 'branch.main.remote', 'origin')
        git('-C', str(clone), 'config', 'branch.main.merge', 'refs/heads/main')
        git('-C', str(clone), 'remote', 'set-url', 'origin', 'https://github.com/Darth-Kuni/Klipper-firmware-auto-updater.git')
        configuration = self.root / 'printer-config/moonraker.conf'
        configuration.parent.mkdir()
        configuration.write_text('[server]\nhost: 127.0.0.1\nport: 7125\n', encoding='utf-8')
        result = subprocess.run([str(clone / 'kcm.sh'), '--data-dir', str(self.root / 'profiles'),
                                 'moonraker-setup', '--config', str(configuration)], input='y\n',
                                cwd=self.root, env=environment, text=True, capture_output=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        snippet = configuration.with_name('kcm-updater.conf').read_text(encoding='utf-8')
        self.assertIn('type: git_repo', snippet)
        self.assertIn('path: ' + str(clone), snippet)
        self.assertNotIn('type: zip', snippet)
        self.assertIn('[include kcm-updater.conf]', configuration.read_text())
        self.assertEqual(git('-C', str(clone), 'status', '--porcelain', '--untracked-files=no').stdout.strip(), '')


if __name__ == '__main__':
    unittest.main()
