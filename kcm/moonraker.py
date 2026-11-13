"""Configure updates for the standalone application, without running firmware jobs.

Moonraker's zip updater requires release_info.json beside the installed app:
https://moonraker.readthedocs.io/en/latest/configuration/#zip-application-configuration
https://moonraker.readthedocs.io/en/latest/configuration/#the-release_infojson-file
The asset_name setting belongs in that JSON, rather than moonraker.conf.
"""
from __future__ import annotations

import fnmatch
import glob
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat
import tempfile
import time
from urllib.parse import urlsplit
import uuid
import zipfile

from .engine import UpdateError
from .storage import Store, atomic_json


ASSET_NAME = "klipper-component-manager-moonraker.zip"
APP_NAME = "klipper-component-manager.pyz"
SECTION_NAME = "update_manager klipper-component-manager"
SNIPPET_NAME = "kcm-updater.conf"
MANAGED_MARKER = "# Managed by Klipper Component Manager: application updates only."
_HEADER = re.compile(r"^\s*\[([^]\r\n]+)\]\s*(?:[#;].*)?$", re.MULTILINE)


def normalize_repository(repository: str) -> str:
    """Accept a GitHub owner/repo slug or its public HTTPS repository URL."""
    if not isinstance(repository, str) or any(ord(c) < 32 for c in repository):
        raise UpdateError("Enter a GitHub repository as owner/name or its HTTPS repository URL.")
    value = repository.strip().rstrip("/")
    if value.startswith("https://"):
        parsed = urlsplit(value)
        if parsed.netloc.lower() != "github.com" or parsed.query or parsed.fragment:
            raise UpdateError("Use a public github.com repository URL without credentials or extra URL parameters.")
        value = parsed.path.strip("/")
        if value.endswith(".git"):
            value = value[:-4]
    parts = value.split("/")
    if (len(parts) != 2 or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9-]{0,38}", parts[0]) or
            not re.fullmatch(r"[A-Za-z0-9_.-]{1,100}", parts[1]) or parts[1] in {".", ".."}):
        raise UpdateError("Repository must identify exactly one GitHub owner and repository name.")
    return value


def _config_path_value(directory: Path) -> str:
    directory = Path(directory)
    value = directory.as_posix()
    if not (directory.is_absolute() or PurePosixPath(value).is_absolute()):
        raise UpdateError("The application update directory must be an absolute path.")
    if ".." in directory.parts or any(ord(c) < 32 or c in "#;[]" for c in value):
        raise UpdateError("Use an application directory without '..', control characters, or configuration comment/header characters.")
    return value


def render_update_config(repo: str, install_dir: Path) -> str:
    repository = normalize_repository(repo)
    path = _config_path_value(install_dir)
    return (f"{MANAGED_MARKER}\n"
            "# Board profiles and jobs stay in KCM's separate data directory.\n"
            f"[{SECTION_NAME}]\n"
            "type: zip\n"
            "channel: stable\n"
            f"repo: {repository}\n"
            f"path: {path}\n"
            "is_system_service: False\n"
            "info_tags:\n"
            "    desc=Klipper Component Manager application\n")


def _read(path: Path) -> bytes:
    try:
        return path.read_bytes()
    except OSError as exc:
        raise UpdateError(f"Cannot read configuration {path}: {exc}") from exc


def _text(data: bytes, path: Path) -> str:
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise UpdateError(f"Configuration must be UTF-8 text: {path}") from exc


def _overlaps(first: Path, second: Path) -> bool:
    return first == second or first in second.parents or second in first.parents


def _git_ancestor(path: Path) -> Path | None:
    return next((parent for parent in (path, *path.parents) if (parent / ".git").exists()), None)


def _write_atomic(path: Path, data: bytes, mode: int = 0o600) -> None:
    """Write beside the destination, so replacement stays on one filesystem."""
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp", delete=False) as stream:
            temporary = Path(stream.name)
            os.chmod(temporary, mode)
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        temporary = None
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


class Installer:
    def __init__(self, store: Store, output=print):
        self.store, self.output = store, output

    def _installed(self, repository: str, app_dir: Path) -> dict:
        info_path, executable = app_dir / "release_info.json", app_dir / APP_NAME
        if not info_path.is_file() or not executable.is_file() or info_path.is_symlink() or executable.is_symlink():
            raise UpdateError(f"Install {APP_NAME} and release_info.json into the dedicated application directory first.")
        try:
            info = json.loads(info_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise UpdateError("The application release_info.json is unreadable or invalid.") from exc
        if not isinstance(info, dict):
            raise UpdateError("The application release_info.json must be a JSON object.")
        owner, name = repository.split("/")
        if (str(info.get("project_owner", "")).lower() != owner.lower() or
                str(info.get("project_name", "")).lower() != name.lower()):
            raise UpdateError("Installed release metadata belongs to another GitHub repository.")
        version = info.get("version")
        if not isinstance(version, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.+-]*", version):
            raise UpdateError("Installed release metadata needs its exact GitHub release tag as version.")
        if info.get("asset_name") != ASSET_NAME:
            raise UpdateError(f"Installed release metadata must select the {ASSET_NAME} asset.")
        try:
            with zipfile.ZipFile(executable) as archive:
                if "__main__.py" not in archive.namelist():
                    raise UpdateError("The standalone application archive has no __main__.py entry point.")
        except (OSError, zipfile.BadZipFile) as exc:
            raise UpdateError("The standalone .pyz application is not a readable Python archive.") from exc
        return info

    def _configuration(self, main: Path, snippet: Path, snippet_data: bytes) -> dict:
        originals: dict[Path, bytes] = {}
        headers: list[tuple[Path, str]] = []
        include_patterns: list[str] = []
        pending = [main]
        while pending:
            path = pending.pop().resolve()
            if path in originals or path == snippet:
                continue
            if len(originals) >= 256:
                raise UpdateError("Too many included Moonraker files; configure the updater manually.")
            data = _read(path)
            originals[path] = data
            content = _text(data, path).lstrip("\ufeff")
            for match in _HEADER.finditer(content):
                header = match[1].strip()
                if header.lower().startswith("include "):
                    pattern = header.split(maxsplit=1)[1].strip()
                    target = Path(pattern).expanduser()
                    absolute = os.path.normpath(str(target if target.is_absolute() else path.parent / target))
                    include_patterns.append(absolute)
                    pending.extend(Path(item) for item in glob.glob(absolute) if Path(item).is_file())
                else:
                    headers.append((path, header.lower()))
        conflicts = [str(path) for path, section in headers if section == SECTION_NAME]
        if conflicts:
            raise UpdateError("An existing KCM update-manager section is already configured in " + ", ".join(conflicts) + ". Review/migrate it before using the managed include.")
        old_snippet = None
        if snippet.exists():
            if snippet.is_symlink() or not snippet.is_file():
                raise UpdateError("The managed updater include must be a regular file.")
            old_snippet = _read(snippet)
            lines = _text(old_snippet, snippet).splitlines()
            if not lines or lines[0] != MANAGED_MARKER:
                raise UpdateError(f"Existing {snippet.name} is not a KCM-managed file; it will not be overwritten.")
        main_data = originals[main]
        newline = b"\r\n" if b"\r\n" in main_data else b"\n"
        additions = []
        if not any(section == "update_manager" for _, section in headers):
            additions.append("[update_manager]")
        if not any(fnmatch.fnmatchcase(str(snippet), pattern) for pattern in include_patterns):
            additions.append(f"[include {snippet.name}]")
        updated_main = main_data
        if additions:
            separator = newline if main_data.endswith((b"\n", b"\r")) else newline + newline
            updated_main += separator + (newline + newline).join(part.encode() for part in additions) + newline
        return {"originals": originals, "old_snippet": old_snippet,
                "new_main": updated_main, "new_snippet": snippet_data, "additions": additions,
                "changed": updated_main != main_data or old_snippet != snippet_data}

    def _plan(self, repository: str, app_dir: Path, moonraker_config: Path,
              *, require_installed: bool = False) -> dict:
        repository = normalize_repository(repository)
        app_selected, config_selected = Path(app_dir).expanduser(), Path(moonraker_config).expanduser()
        _config_path_value(app_selected)
        if not config_selected.is_absolute() or config_selected.is_symlink() or not config_selected.is_file():
            raise UpdateError("Select the existing, regular moonraker.conf file for the intended printer instance.")
        if app_selected.is_symlink():
            raise UpdateError("Use a dedicated application directory rather than a symlink.")
        app, main = app_selected.resolve(), config_selected.resolve()
        if app == Path(app.anchor) or app == Path.home().resolve():
            raise UpdateError("The update directory must be dedicated to the KCM application.")
        if _overlaps(app, self.store.root):
            raise UpdateError("The application update directory must be separate from KCM's profiles, jobs, and other saved data.")
        if _overlaps(app, main.parent):
            raise UpdateError("The application update directory must be separate from Moonraker's configuration directory.")
        git = _git_ancestor(app)
        if git is not None:
            raise UpdateError(f"Moonraker zip applications must be outside Git checkouts; found {git / '.git'}.")
        if app.exists():
            if not app.is_dir():
                raise UpdateError("The application update path must be a directory.")
            unexpected = sorted(entry.name for entry in app.iterdir() if entry.name not in {APP_NAME, "release_info.json"})
            if unexpected:
                raise UpdateError("The update directory contains unrelated files that Moonraker would replace: " + ", ".join(unexpected))
        release = self._installed(repository, app) if require_installed else None
        snippet = main.parent / SNIPPET_NAME
        configuration = render_update_config(repository, app)
        edits = self._configuration(main, snippet, configuration.encode("utf-8"))
        return {"repository": repository, "app_dir": str(app), "moonraker_config": str(main),
                "snippet": str(snippet), "config": configuration, "release": release,
                "changed": edits["changed"], "additions": edits["additions"], "_edits": edits,
                "restart_required": edits["changed"]}

    def validate_target(self, repository: str, app_dir: Path, moonraker_config: Path,
                        *, require_installed: bool = False) -> dict:
        """Read-only preflight; a new app directory may be staged after this call.

        Existing configuration bytes are deliberately omitted from the public
        result, so logging this plan cannot expose Moonraker credentials.
        """
        return {key: value for key, value in self._plan(repository, app_dir, moonraker_config,
                    require_installed=require_installed).items() if key != "_edits"}

    def install(self, repository: str, app_dir: Path, moonraker_config: Path, confirm) -> dict:
        plan = self._plan(repository, app_dir, moonraker_config, require_installed=True)
        if not plan["changed"]:
            self.output("Moonraker application updates are already configured.")
            return {key: value for key, value in plan.items() if key != "_edits"}
        main, snippet = Path(plan["moonraker_config"]), Path(plan["snippet"])
        prompt = (f"Configure Moonraker stable application updates from {plan['repository']}\n"
                  f"Application directory: {plan['app_dir']}\nManaged include: {snippet}\n"
                  f"Append to {main}: {', '.join(plan['additions']) or 'no additions'}\n\n"
                  + plan["config"] + "\nBack up affected configuration files and apply this configuration? No services will be restarted.")
        if not confirm(prompt):
            raise UpdateError("Moonraker setup cancelled before changing configuration.")
        edits = plan["_edits"]
        for path, original in edits["originals"].items():
            if _read(path) != original:
                raise UpdateError("Moonraker configuration changed while awaiting confirmation; review the new configuration and try again.")
        old_snippet = _read(snippet) if snippet.exists() else None
        if old_snippet != edits["old_snippet"] or snippet.is_symlink() or main.is_symlink():
            raise UpdateError("The managed updater include changed while awaiting confirmation; try again.")
        backup = self.store.root / "backups" / "moonraker" / (time.strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:8])
        backup.mkdir(parents=True, mode=0o700)
        _write_atomic(backup / main.name, edits["originals"][main])
        if old_snippet is not None:
            _write_atomic(backup / snippet.name, old_snippet)
        atomic_json(backup / "backup-info.json", {"repository": plan["repository"], "moonraker_config": str(main), "snippet": str(snippet), "created": time.time()})
        main_mode = stat.S_IMODE(main.stat().st_mode)
        snippet_mode = stat.S_IMODE(snippet.stat().st_mode) if snippet.exists() else main_mode
        try:
            _write_atomic(snippet, edits["new_snippet"], snippet_mode)
            if edits["new_main"] != edits["originals"][main]:
                _write_atomic(main, edits["new_main"], main_mode)
        except BaseException:
            if old_snippet is None:
                snippet.unlink(missing_ok=True)
            else:
                _write_atomic(snippet, old_snippet, snippet_mode)
            raise
        result = {key: value for key, value in plan.items() if key != "_edits"}
        result["backup_dir"] = str(backup)
        self.output(f"Moonraker updater configuration saved. Backups: {backup}")
        self.output("Restart the intended Moonraker instance when the printer is idle, then refresh its update manager. This entry updates the KCM application; firmware flashing remains an explicit KCM operation.")
        return result
