"""Create a source ZIP without local checkouts, environments, or printer data."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import re
import shutil
import tempfile
import zipfile
import zipapp


def main() -> None:
    root = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--repository', help='GitHub owner/repository used by Moonraker')
    arguments = parser.parse_args()
    project = json.loads((root / 'kcm/data/project.json').read_text(encoding='utf-8'))
    repository = arguments.repository or project['repository']
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]*/[A-Za-z0-9][A-Za-z0-9_.-]*', repository):
        parser.error('Repository must be a GitHub owner/name pair')
    owner, project_name = repository.split('/')
    destination = root / "dist"
    destination.mkdir(exist_ok=True)
    version_text = (root / "kcm/__init__.py").read_text(encoding="utf-8")
    version = re.search(r'__version__ = "([0-9.]+)"', version_text).group(1)
    name = f"klipper-component-manager-{version}"
    files = [root / item for item in ("README.md", "VALIDATION.md", "LICENSE", "pyproject.toml", ".gitignore")]
    for optional in ('CONTRIBUTING.md', 'CHANGELOG.md', '.gitattributes', 'kcm.sh'):
        if (root / optional).is_file():
            files.append(root / optional)
    files.extend((root / '.github/workflows').glob('*.yml'))
    files.extend((root / 'docs').glob('*.md'))
    files.extend((root / 'examples').glob('*.conf'))
    for folder, pattern in (("kcm", "*.py"), ("tools", "*.py"), ("tests", "test_*.py")):
        files.extend((root / folder).glob(pattern))
    files.extend((root / "kcm/data").glob("*.json"))
    files.extend((root / "kcm/data").glob("*.md"))
    files.extend((root / "kcm/data").glob("*.txt"))
    archive = destination / f"{name}.zip"
    with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as stream:
        for path in sorted(files):
            stream.write(path, str(Path(name) / path.relative_to(root)))
    with zipfile.ZipFile(archive) as stream:
        broken = stream.testzip()
        if broken:
            raise RuntimeError(f"Archive verification failed: {broken}")
        print(f"Created {archive} ({len(stream.namelist())} files)")
    program = destination / f"{name}.pyz"
    with tempfile.TemporaryDirectory(prefix="release-", dir=destination) as temp:
        staging = Path(temp)
        for path in files:
            relative = path.relative_to(root)
            if relative.parts[0] == "kcm" or path.name in {"LICENSE", "README.md"}:
                target = staging / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(path, target)
        (staging / 'kcm/data/project.json').write_text(
            json.dumps({'schema_version': 1, 'repository': repository}, indent=2) + '\n', encoding='utf-8')
        (staging / "__main__.py").write_text(
            "import sys\nif sys.version_info < (3, 10):\n"
            "    print('Klipper Component Manager requires Python 3.10 or newer.', file=sys.stderr)\n"
            "    raise SystemExit(2)\nfrom kcm.cli import main\nraise SystemExit(main())\n", encoding="utf-8")
        zipapp.create_archive(staging, target=program, interpreter="/usr/bin/env python3", compressed=True)
    with zipfile.ZipFile(program) as stream:
        if stream.testzip() or any("kiauh" in member.lower() for member in stream.namelist()):
            raise RuntimeError("Standalone program archive validation failed")
    print(f"Created standalone program {program}")
    stable_program = destination / 'klipper-component-manager.pyz'
    shutil.copyfile(program, stable_program)
    release_info = {'project_name': project_name, 'project_owner': owner, 'version': 'v' + version,
                    'asset_name': 'klipper-component-manager-moonraker.zip'}
    updater_bundle = destination / release_info['asset_name']
    with zipfile.ZipFile(updater_bundle, 'w', compression=zipfile.ZIP_DEFLATED) as stream:
        stream.write(program, 'klipper-component-manager.pyz')
        stream.writestr('release_info.json', json.dumps(release_info, indent=2) + '\n')
    with zipfile.ZipFile(updater_bundle) as stream:
        if stream.testzip():
            raise RuntimeError('Moonraker bundle verification failed')
    print(f'Created Moonraker update bundle {updater_bundle}')
    packages = [program, stable_program, archive, updater_bundle, *destination.glob(f"*{version}*.whl")]
    (destination / "SHA256SUMS.txt").write_text(
        "".join(f"{hashlib.sha256(path.read_bytes()).hexdigest()}  {path.name}\n" for path in packages),
        encoding="utf-8",
    )


if __name__ == "__main__":
    main()
