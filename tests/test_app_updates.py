"""Standalone app adoption uses temporary files, never printers or services."""
from contextlib import nullcontext
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import zipfile

from kcm import __version__
from kcm.app_updates import setup_updates
from kcm.engine import Manager, UpdateError
from kcm.moonraker import APP_NAME, ASSET_NAME, Installer
from kcm.storage import Store


REPOSITORY = 'Darth-Kuni/Klipper-firmware-auto-updater'


class ApplicationAdoptionTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='kcm-adoption-', dir=Path(__file__).resolve().parents[2])
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.store = Store(self.root / 'saved-profiles')
        self.sentinel = self.store.root / 'important-profile.json'
        self.sentinel.write_text('{"keep": true}', encoding='utf-8')
        self.app = self.root / 'application'
        self.config = self.root / 'printer-config/moonraker.conf'
        self.config.parent.mkdir()
        self.original = b'[server]\nhost: 127.0.0.1\nport: 7125\n'
        self.config.write_bytes(self.original)
        for active in (patch('kcm.app_updates.platform.system', return_value='Linux'),
                       patch.object(Manager, '_lock', side_effect=lambda: nullcontext())):
            active.start()
            self.addCleanup(active.stop)

    def run_setup(self, confirm=lambda _prompt: True):
        return setup_updates(self.store, self.app, self.config, REPOSITORY, confirm, output=lambda _line: None)

    def test_adoption_makes_complete_independent_app_and_matching_metadata(self):
        result = self.run_setup()
        with zipfile.ZipFile(self.app / APP_NAME) as archive:
            self.assertIn('__main__.py', archive.namelist())
            self.assertIn('kcm/data/boards.json', archive.namelist())
            self.assertIn('LICENSE', archive.namelist())
            project = json.loads(archive.read('kcm/data/project.json'))
            self.assertEqual(project['repository'], REPOSITORY)
        info = json.loads((self.app / 'release_info.json').read_text())
        self.assertEqual(info['project_name'], REPOSITORY.split('/')[1])
        self.assertEqual(info['version'], 'v' + __version__)
        self.assertEqual(info['asset_name'], ASSET_NAME)
        self.assertTrue(self.config.read_bytes().startswith(self.original))
        self.assertIn(b'[include kcm-updater.conf]', self.config.read_bytes())
        self.assertTrue(Path(result['backup_dir']).is_dir())
        self.assertEqual(self.sentinel.read_text(), '{"keep": true}')

    def test_decline_never_creates_app_or_edits_configuration(self):
        with self.assertRaisesRegex(UpdateError, 'cancelled'):
            self.run_setup(lambda _prompt: False)
        self.assertFalse(self.app.exists())
        self.assertEqual(self.config.read_bytes(), self.original)
        self.assertFalse((self.store.root / 'backups').exists())

    def test_existing_valid_application_is_not_repacked(self):
        self.run_setup()
        original = (self.app / APP_NAME).read_bytes()
        with patch('kcm.app_updates._make_program', side_effect=AssertionError('Existing app must remain intact')):
            result = self.run_setup()
        self.assertFalse(result['changed'])
        self.assertEqual((self.app / APP_NAME).read_bytes(), original)

    def test_config_failure_cleans_only_new_app_and_retains_saved_profiles(self):
        with patch.object(Installer, 'install', side_effect=UpdateError('simulated config failure')):
            with self.assertRaisesRegex(UpdateError, 'simulated config failure'):
                self.run_setup()
        self.assertFalse(self.app.exists())
        self.assertEqual(self.config.read_bytes(), self.original)
        self.assertEqual(self.sentinel.read_text(), '{"keep": true}')

    def test_managed_app_cannot_contain_saved_profiles(self):
        self.app = self.store.root / 'application'
        with self.assertRaisesRegex(UpdateError, 'separate from KCM'):
            self.run_setup()
        self.assertFalse(self.app.exists())

    def test_git_checkout_cannot_be_managed_as_zip_application(self):
        checkout = self.root / 'git-source'
        (checkout / '.git').mkdir(parents=True)
        self.app = checkout / 'application'
        with self.assertRaisesRegex(UpdateError, 'outside Git'):
            self.run_setup()
        self.assertFalse(self.app.exists())

    def test_unrelated_app_files_are_preserved_and_rejected(self):
        self.app.mkdir()
        important = self.app / 'user-notes.txt'
        important.write_text('keep this', encoding='utf-8')
        with self.assertRaisesRegex(UpdateError, 'unrelated files'):
            self.run_setup()
        self.assertEqual(important.read_text(), 'keep this')
        self.assertEqual(self.config.read_bytes(), self.original)


if __name__ == '__main__':
    unittest.main()
