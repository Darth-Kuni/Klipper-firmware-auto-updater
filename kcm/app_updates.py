"""Adopt the standalone program into a dedicated Moonraker update directory."""
from __future__ import annotations

from importlib import resources
import json
import os
from pathlib import Path
import platform
import tempfile
import zipapp
import zipfile

from . import __version__
from .engine import Manager, UpdateError
from .moonraker import APP_NAME, ASSET_NAME, Installer, normalize_repository
from .storage import Store, atomic_json


def default_repository() -> str:
    return normalize_repository(json.loads(resources.files('kcm').joinpath('data/project.json').read_text(encoding='utf-8'))['repository'])


def checkout_directory() -> Path | None:
    """Recognize a source checkout without treating an installed wheel as Git."""
    root = Path(__file__).resolve().parent.parent
    if root.is_dir() and (root / '.git').exists() and (root / 'kcm.sh').is_file():
        return root
    return None


def setup_git_updates(store: Store, checkout: Path, moonraker_config: Path,
                      repository: str | None, confirm, output=print) -> dict:
    if platform.system() != 'Linux':
        raise UpdateError('Configure Moonraker application updates on its Linux host over SSH.')
    with Manager(store, output=output)._lock():
        result = Installer(store, output=output).install_git(
            repository or default_repository(), checkout, moonraker_config, confirm)
        output(f'Continue launching the app with: {Path(checkout).expanduser() / "kcm.sh"}')
        output('Exit the application before Moonraker updates its checkout. Saved profiles remain in the data directory.')
        return result


def _resource_copy(source, destination: Path):
    destination.mkdir(parents=True, exist_ok=True)
    for item in source.iterdir():
        if item.is_dir() and item.name == 'data':
            _resource_copy(item, destination / item.name)
        elif item.is_file() and item.name.endswith(('.py', '.json', '.md', '.txt')):
            (destination / item.name).write_bytes(item.read_bytes())


def _make_program(destination: Path, repository: str):
    # Repack from bundled package resources: this works from a .pyz, a wheel,
    # and a source checkout without a compiler, Git, or another download.
    with tempfile.TemporaryDirectory(prefix='kcm-package-', dir=destination.parent) as directory:
        staging = Path(directory)
        _resource_copy(resources.files('kcm'), staging / 'kcm')
        (staging / 'kcm/data/project.json').write_text(
            json.dumps({'schema_version': 1, 'repository': repository}, indent=2) + '\n', encoding='utf-8')
        (staging / '__main__.py').write_text(
            "import sys\nif sys.version_info < (3, 10):\n"
            "    print('Python 3.10 or newer is required.', file=sys.stderr)\n"
            "    raise SystemExit(2)\nfrom kcm.cli import main\nraise SystemExit(main())\n", encoding='utf-8')
        license_file = resources.files('kcm').joinpath('data/LICENSE.txt')
        if license_file.is_file():
            (staging / 'LICENSE').write_bytes(license_file.read_bytes())
        # The output must live outside its input tree.
        descriptor, temporary = tempfile.mkstemp(prefix='.kcm-program-', suffix='.pyz', dir=destination.parent)
        os.close(descriptor)
        packed = Path(temporary)
        try:
            zipapp.create_archive(staging, target=packed, interpreter='/usr/bin/env python3', compressed=True)
            with zipfile.ZipFile(packed) as check:
                if check.testzip() or 'kcm/data/boards.json' not in check.namelist():
                    raise UpdateError('The packaged application is incomplete.')
            os.replace(packed, destination)
        finally:
            packed.unlink(missing_ok=True)


def setup_updates(store: Store, app_dir: Path, moonraker_config: Path,
                  repository: str | None, confirm, output=print) -> dict:
    if platform.system() != 'Linux':
        raise UpdateError('Configure Moonraker application updates on its Linux host over SSH.')
    repository = normalize_repository(repository or default_repository())
    installer = Installer(store, output=output)
    with Manager(store, output=output)._lock():
        plan = installer.validate_target(repository, app_dir, moonraker_config)
        destination = Path(plan['app_dir'])
        existing = list(destination.iterdir()) if destination.exists() else []
        if existing:
            # Existing valid installations are preserved; Moonraker handles
            # their subsequent release updates.
            installer.validate_target(repository, destination, moonraker_config, require_installed=True)
        migration = ("The old KCM updater section will move into the main configuration; its extra file and other settings stay in place.\n"
                     if plan.get('legacy_migration') else '')
        prompt = (f"Set up Moonraker updates for the standalone application from {repository}?\n"
                  f"Application: {destination / APP_NAME}\n"
                  f"Saved profiles and backups: {store.root}\n"
                  f"Moonraker configuration: {plan['moonraker_config']}\n"
                  f"Updater section will be written directly in: {plan['moonraker_config']}\n\n{plan['config']}\n"
                  + migration +
                  "The configuration files will be backed up. No printer firmware is flashed and no services are restarted.")
        if not confirm(prompt):
            raise UpdateError('Moonraker application setup cancelled before changes.')
        adopted = not existing
        try:
            if adopted:
                destination.mkdir(parents=True, exist_ok=True, mode=0o700)
                _make_program(destination / APP_NAME, repository)
                owner, name = repository.split('/')
                atomic_json(destination / 'release_info.json', {
                    'project_name': name, 'project_owner': owner, 'version': 'v' + __version__,
                    'asset_name': ASSET_NAME})
            # A single combined plan was approved above. The lower layer still
            # performs its own preflight and checks before changing config.
            result = installer.install(repository, destination, moonraker_config, confirm=lambda _prompt: True)
        except BaseException:
            if adopted:
                for name in (APP_NAME, 'release_info.json'):
                    (destination / name).unlink(missing_ok=True)
                if destination.is_dir() and not any(destination.iterdir()):
                    destination.rmdir()
            raise
        output(f'Launch the managed application with: python3 {destination / APP_NAME}')
        output('Exit KCM before updating its application files through Moonraker. Saved board profiles remain in the data directory.')
        return result
