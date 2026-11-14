"""Configure updates for the KCM application, without running firmware jobs.

Moonraker's zip updater requires release_info.json beside the installed app:
https://moonraker.readthedocs.io/en/latest/configuration/#zip-application-configuration
https://moonraker.readthedocs.io/en/latest/configuration/#the-release_infojson-file
The asset_name setting belongs in that JSON, rather than moonraker.conf.
Source checkouts use the separate git_repo schema:
https://moonraker.readthedocs.io/en/latest/configuration/#git-repo-configuration
"""
from __future__ import annotations

import glob
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat
import subprocess
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
INLINE_BEGIN = "# BEGIN KCM APPLICATION UPDATER"
INLINE_END = "# END KCM APPLICATION UPDATER"
_HEADER = re.compile(r"^[ \t]*\[([^]\r\n]+)\][ \t]*(?:[#;][^\r\n]*)?\r?$", re.MULTILINE)


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


def _origin_repository(origin: str) -> str:
    """Normalize only credential-free GitHub Git origins, without exposing them."""
    if not isinstance(origin, str) or any(ord(c) < 33 for c in origin):
        raise UpdateError("The checkout origin must be a credential-free GitHub HTTPS or SSH URL.")
    if origin.startswith("git@github.com:"):
        value = origin[len("git@github.com:"):].rstrip("/")
        if value.endswith(".git"):
            value = value[:-4]
        return normalize_repository(value)
    if origin.startswith("ssh://"):
        try:
            parsed = urlsplit(origin)
            valid = (parsed.hostname == "github.com" and parsed.username == "git" and parsed.password is None
                     and parsed.port in (None, 22) and not parsed.query and not parsed.fragment)
        except ValueError:
            valid = False
        if not valid:
            raise UpdateError("The checkout origin must be a credential-free GitHub HTTPS or SSH URL.")
        return normalize_repository("https://github.com/" + parsed.path.lstrip("/"))
    if origin.startswith("https://"):
        return normalize_repository(origin)
    raise UpdateError("The checkout origin must be a credential-free GitHub HTTPS or SSH URL.")


def render_git_update_config(repo: str, checkout: Path, *, origin: str | None = None) -> str:
    """Render Moonraker's Git updater; the existing ZIP renderer is unchanged."""
    repository = normalize_repository(repo)
    path = _config_path_value(checkout)
    remote = origin or f"https://github.com/{repository}.git"
    if _origin_repository(remote).lower() != repository.lower():
        raise UpdateError("The Git origin does not match the requested application repository.")
    remote = remote.rstrip("/")
    if not remote.endswith(".git"):
        remote += ".git"
    return (f"{MANAGED_MARKER}\n"
            "# Board profiles and jobs stay in KCM's separate data directory.\n"
            f"[{SECTION_NAME}]\n"
            "type: git_repo\n"
            "channel: dev\n"
            f"path: {path}\n"
            f"origin: {remote}\n"
            "primary_branch: main\n"
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


def _sections(text: str) -> list[dict]:
    offset = 1 if text.startswith("\ufeff") else 0
    matches = list(_HEADER.finditer(text[offset:]))
    sections = []
    for number, match in enumerate(matches):
        start, header_end = match.start() + offset, match.end() + offset
        if text[header_end:header_end + 2] == "\r\n":
            header_end += 2
        elif text[header_end:header_end + 1] == "\n":
            header_end += 1
        end = matches[number + 1].start() + offset if number + 1 < len(matches) else len(text)
        sections.append({"name": match[1].strip(), "start": start, "header_end": header_end, "end": end})
    return sections


def _option_values(text: str) -> dict[str, str]:
    values = {}
    for line in text.splitlines():
        match = re.match(r"^[ \t]*([A-Za-z0-9_]+)[ \t]*[:=][ \t]*(.*)$", line)
        if match:
            values[match[1].lower()] = re.split(r"[ \t]+[#;]", match[2], maxsplit=1)[0].strip()
    return values


def _section_data_end(text: str, section: dict) -> int:
    """Keep blank/comment lines before the next section outside replacements."""
    cursor, end = section["start"], section["header_end"]
    for line in text[section["start"]:section["end"]].splitlines(keepends=True):
        cursor += len(line)
        if line.strip() and not line.lstrip().startswith(("#", ";")):
            end = cursor
    return end


def _remove_section_data(text: str, section: dict) -> str:
    """Remove only a section's header/options; retain every comment/blank byte."""
    retained = []
    for line in text[section["header_end"]:section["end"]].splitlines(keepends=True):
        if not line.strip() or line.lstrip().startswith(("#", ";")):
            retained.append(line)
    return text[:section["start"]] + "".join(retained) + text[section["end"]:]


def _owned_prefix(text: str, section_start: int) -> int | None:
    cursor = section_start
    for line in reversed(text[:section_start].splitlines(keepends=True)):
        cursor -= len(line)
        value = line.strip().lstrip("\ufeff")
        if value == MANAGED_MARKER:
            return cursor + (1 if line.startswith("\ufeff") else 0)
        if value and not value.startswith(("#", ";")):
            break
    return None


def _inline_configuration(text: str, rendered: str) -> str:
    sections = [section for section in _sections(text) if section["name"].lower() == SECTION_NAME]
    if len(sections) > 1:
        raise UpdateError("The main Moonraker configuration contains duplicate KCM updater sections; review them before setup.")
    newline = "\r\n" if "\r\n" in text else "\n"
    block = newline.join((INLINE_BEGIN, rendered.rstrip("\r\n").replace("\r\n", "\n").replace("\n", newline), INLINE_END, ""))
    offset = 1 if text.startswith("\ufeff") else 0
    marker = lambda value: list(re.finditer(r"^[ \t]*" + re.escape(value) + r"[ \t]*\r?$", text[offset:], re.MULTILINE))
    begins, ends = marker(INLINE_BEGIN), marker(INLINE_END)
    if begins or ends:
        if len(begins) != 1 or len(ends) != 1 or not sections:
            raise UpdateError("KCM's inline configuration markers are incomplete or ambiguous; review the main configuration.")
        start, end = begins[0].start() + offset, ends[0].end() + offset
        if text[end:end + 1] == "\n":
            end += 1
        if start >= end or not start <= sections[0]["start"] < end:
            raise UpdateError("KCM's inline configuration markers do not surround its updater section.")
        if any(section["name"].lower() != SECTION_NAME for section in _sections(text[start:end])):
            raise UpdateError("KCM's marked inline block contains another section; unrelated settings will not be overwritten.")
        if any(line.strip() and not line.lstrip().startswith(("#", ";"))
               for line in text[start:sections[0]["start"]].splitlines()):
            raise UpdateError("KCM's marked inline block contains settings before its updater section; review the marker boundaries before setup.")
        if _section_data_end(text, sections[0]) > ends[0].start() + offset:
            raise UpdateError("KCM updater settings extend past its inline end marker; review the marker boundaries before setup.")
        return text[:start] + block + text[end:]
    if not sections:
        separator = newline if text.endswith(("\n", "\r")) else newline + newline
        return text + separator + block
    section = sections[0]
    owned_start = _owned_prefix(text, section["start"])
    if owned_start is None:
        current = _option_values(text[section["header_end"]:section["end"]])
        desired = _option_values(rendered)
        try:
            current_repo = (_origin_repository(current.get("origin", "")) if current.get("type") == "git_repo"
                            else normalize_repository(current.get("repo", "")))
            desired_repo = (_origin_repository(desired.get("origin", "")) if desired.get("type") == "git_repo"
                            else normalize_repository(desired.get("repo", "")))
            same = (current.get("type") == desired.get("type") and current_repo.lower() == desired_repo.lower()
                    and current.get("path") and Path(current["path"]).expanduser().resolve() == Path(desired["path"]).expanduser().resolve())
        except (ValueError, UpdateError):
            same = False
        if not same:
            raise UpdateError("The existing manual KCM updater section has a different repository/path identity. Review it before setup can adopt it.")
    start = owned_start if owned_start is not None else section["start"]
    return text[:start] + block + text[_section_data_end(text, section):]


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
        if main == snippet:
            raise UpdateError("Select the main Moonraker configuration rather than the legacy KCM settings file.")
        if snippet.is_symlink():
            raise UpdateError("The legacy KCM settings file must not be a symlink.")
        originals: dict[Path, bytes] = {}
        references: dict[Path, tuple[Path, bool]] = {}
        headers: list[tuple[Path, str]] = []
        include_matches: dict[str, list[str]] = {}
        dangling: dict[Path, list[tuple[int, int]]] = {}
        pending = [main]
        while pending:
            selected = pending.pop().absolute()
            path = selected.resolve()
            references[selected] = (path, selected.is_symlink())
            if path in originals:
                continue
            if len(originals) >= 256:
                raise UpdateError("Too many included Moonraker files; configure the updater manually.")
            originals[path] = _read(path)
            content = _text(originals[path], path)
            for section in _sections(content):
                header = section["name"]
                if header.lower().startswith("include "):
                    target = Path(header.split(maxsplit=1)[1].strip()).expanduser()
                    pattern = os.path.normpath(str(target if target.is_absolute() else path.parent / target))
                    matches = sorted(item for item in glob.glob(pattern) if Path(item).is_file())
                    include_matches[pattern] = matches
                    if not glob.has_magic(pattern) and Path(pattern).resolve() == snippet and not snippet.exists():
                        dangling.setdefault(path, []).append((section["start"], section["header_end"]))
                    pending.extend(Path(item) for item in matches)
                else:
                    headers.append((path, header.lower()))
        # The old settings file may be present even if it is no longer loaded.
        # Keep it, and remove only a recognized updater section when necessary.
        old_snippet = None
        if snippet.exists():
            if not snippet.is_file():
                raise UpdateError("The legacy KCM settings file must be a regular file.")
            old_snippet = originals.setdefault(snippet, _read(snippet))
            references[snippet] = (snippet.resolve(), False)
        conflicts = [str(path) for path, section in headers if section == SECTION_NAME and path not in {main, snippet}]
        if conflicts:
            raise UpdateError("An existing KCM update-manager section is already configured in " + ", ".join(conflicts) + ". Move/review that section before configuring it directly in the main Moonraker file.")
        updated = {}
        migrated = False
        if old_snippet is not None:
            legacy = _text(old_snippet, snippet)
            updater_sections = [section for section in _sections(legacy) if section["name"].lower() == SECTION_NAME]
            if updater_sections:
                first = next((line.strip().lstrip("\ufeff") for line in legacy.splitlines() if line.strip()), "")
                if first != MANAGED_MARKER or len(updater_sections) != 1:
                    raise UpdateError("The legacy settings file contains an unmanaged or ambiguous KCM updater section; it will not be removed automatically.")
                updated[snippet] = _remove_section_data(legacy, updater_sections[0]).encode("utf-8")
                migrated = True
        for path, spans in dangling.items():
            text = _text(originals[path], path)
            for start, end in sorted(spans, reverse=True):
                text = text[:start] + text[end:]
            updated[path] = text.encode("utf-8")
        text = _text(updated.get(main, originals[main]), main)
        newline = "\r\n" if "\r\n" in text else "\n"
        additions = []
        if not any(section == "update_manager" for _, section in headers):
            text += (newline if text.endswith(("\n", "\r")) else newline + newline) + "[update_manager]" + newline
            additions.append("[update_manager]")
        inline = _inline_configuration(text, _text(snippet_data, main))
        updated[main] = inline.encode("utf-8")
        updated = {path: data for path, data in updated.items() if data != originals[path]}
        for selected, (resolved, symlink) in references.items():
            if resolved in updated and symlink:
                raise UpdateError(f"An included configuration that needs editing is a symlink: {selected}. Select/review its real file first.")
        return {"originals": originals, "references": references, "include_matches": include_matches,
                "updates": updated, "old_snippet": old_snippet, "new_main": updated.get(main, originals[main]),
                "additions": additions, "legacy_migration": migrated, "changed": bool(updated)}

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
                "target": str(main), "modified_files": [str(path) for path in edits["updates"]],
                "legacy_migration": edits["legacy_migration"],
                "restart_required": edits["changed"]}

    def validate_target(self, repository: str, app_dir: Path, moonraker_config: Path,
                        *, require_installed: bool = False) -> dict:
        """Read-only preflight; a new app directory may be staged after this call.

        Existing configuration bytes are deliberately omitted from the public
        result, so logging this plan cannot expose Moonraker credentials.
        """
        return {key: value for key, value in self._plan(repository, app_dir, moonraker_config,
                    require_installed=require_installed).items() if key != "_edits"}

    @staticmethod
    def _git(checkout: Path, *arguments: str) -> str:
        environment = os.environ.copy()
        environment.update(GIT_OPTIONAL_LOCKS="0", GIT_TERMINAL_PROMPT="0")
        try:
            result = subprocess.run(["git", "-c", "core.fsmonitor=false", "-C", str(checkout), *arguments],
                                    capture_output=True, text=True, encoding="utf-8", errors="replace",
                                    timeout=10, env=environment, check=False)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise UpdateError("Cannot inspect the local Git checkout; install Git and verify the selected path.") from exc
        if result.returncode:
            # stderr may include a credentialed origin; do not echo it.
            raise UpdateError("Cannot validate the local Git checkout. Verify its origin, main branch, and tracking configuration.")
        return result.stdout.strip()

    def _git_plan(self, repository: str, checkout: Path, moonraker_config: Path) -> dict:
        repository = normalize_repository(repository)
        selected, config_selected = Path(checkout).expanduser(), Path(moonraker_config).expanduser()
        _config_path_value(selected)
        if selected.is_symlink() or not selected.is_dir() or not (selected / ".git").exists():
            raise UpdateError("Select the root of an existing KCM Git checkout, including its .git directory or worktree file.")
        if not config_selected.is_absolute() or config_selected.is_symlink() or not config_selected.is_file():
            raise UpdateError("Select the existing, regular moonraker.conf file for the intended printer instance.")
        app, main = selected.resolve(), config_selected.resolve()
        if app == Path(app.anchor) or app == Path.home().resolve() or _overlaps(app, self.store.root):
            raise UpdateError("The Git checkout must be separate from KCM's profiles, jobs, and other saved data.")
        if _overlaps(app, main.parent):
            raise UpdateError("The Git checkout must be separate from Moonraker's configuration directory.")
        launcher, module = app / "kcm.sh", app / "kcm/cli.py"
        if (not launcher.is_file() or launcher.is_symlink() or not module.is_file() or module.is_symlink()
                or (os.name == "posix" and not os.access(launcher, os.X_OK))):
            raise UpdateError("The KCM checkout must contain kcm/cli.py and an executable kcm.sh launcher.")
        if Path(self._git(app, "rev-parse", "--show-toplevel")).resolve() != app:
            raise UpdateError("The selected application path is not the exact Git checkout root.")
        branch = self._git(app, "symbolic-ref", "--quiet", "--short", "HEAD")
        if branch != "main":
            raise UpdateError("Moonraker application updates require this checkout's local main branch.")
        origin = self._git(app, "remote", "get-url", "origin")
        if _origin_repository(origin).lower() != repository.lower():
            raise UpdateError("The checkout origin belongs to another GitHub repository.")
        if (self._git(app, "config", "--get", "branch.main.remote") != "origin" or
                self._git(app, "config", "--get", "branch.main.merge") != "refs/heads/main"):
            raise UpdateError("The checkout's main branch must track origin/main before enabling Moonraker updates.")
        if self._git(app, "status", "--porcelain", "--untracked-files=no"):
            raise UpdateError("The application checkout has tracked local changes. Commit/reconcile them before enabling updates.")
        snippet = main.parent / SNIPPET_NAME
        configuration = render_git_update_config(repository, app, origin=origin)
        edits = self._configuration(main, snippet, configuration.encode("utf-8"))
        return {"repository": repository, "app_dir": str(app), "checkout": str(app), "mode": "git_repo",
                "moonraker_config": str(main), "snippet": str(snippet), "config": configuration,
                "changed": edits["changed"], "additions": edits["additions"], "_edits": edits,
                "target": str(main), "modified_files": [str(path) for path in edits["updates"]],
                "legacy_migration": edits["legacy_migration"],
                "restart_required": edits["changed"]}

    def validate_git_target(self, repository: str, checkout: Path, moonraker_config: Path) -> dict:
        """Read-only Git preflight; never fetch, reset, copy, or run firmware."""
        return {key: value for key, value in self._git_plan(repository, checkout, moonraker_config).items()
                if key != "_edits"}

    def install_git(self, repository: str, checkout: Path, moonraker_config: Path, confirm) -> dict:
        return self._install_plan(self._git_plan(repository, checkout, moonraker_config), confirm)

    def install(self, repository: str, app_dir: Path, moonraker_config: Path, confirm) -> dict:
        plan = self._plan(repository, app_dir, moonraker_config, require_installed=True)
        return self._install_plan(plan, confirm)

    def _install_plan(self, plan: dict, confirm) -> dict:
        if not plan["changed"]:
            self.output("Moonraker application updates are already configured.")
            return {key: value for key, value in plan.items() if key != "_edits"}
        main, snippet = Path(plan["moonraker_config"]), Path(plan["snippet"])
        channel = "main-branch" if plan.get("mode") == "git_repo" else "stable"
        migration = ("\nRemove only the old KCM updater section from " + str(snippet) +
                     "; retain that file, its other settings, and its live includes.") if plan["legacy_migration"] else ""
        prompt = (f"Configure Moonraker {channel} application updates from {plan['repository']}\n"
                  f"Application directory: {plan['app_dir']}\nWrite the updater directly into: {main}"
                  + migration + "\n\n"
                  + plan["config"] + "\nBack up affected configuration files and apply this configuration? No services will be restarted.")
        if not confirm(prompt):
            raise UpdateError("Moonraker setup cancelled before changing configuration.")
        if plan.get("mode") == "git_repo":
            refreshed = self._git_plan(plan["repository"], Path(plan["checkout"]), main)
            if refreshed["config"] != plan["config"]:
                raise UpdateError("The checkout identity changed while awaiting confirmation; review it and try again.")
        edits = plan["_edits"]
        # Re-scan include globs as well as known files: a new matching file can
        # introduce a conflicting updater while the confirmation is displayed.
        refreshed = self._configuration(main, snippet, plan["config"].encode("utf-8"))
        if (refreshed["include_matches"] != edits["include_matches"] or
                refreshed["originals"] != edits["originals"] or refreshed["updates"] != edits["updates"]):
            raise UpdateError("Moonraker configuration changed while awaiting confirmation; review the new configuration and try again.")
        for selected, (resolved, symlink) in edits["references"].items():
            if selected.resolve() != resolved or selected.is_symlink() != symlink:
                raise UpdateError("An included configuration path changed while awaiting confirmation; review it and try again.")
        for path, original in edits["originals"].items():
            if _read(path) != original:
                raise UpdateError("Moonraker configuration changed while awaiting confirmation; review the new configuration and try again.")
        old_snippet = _read(snippet) if snippet.exists() else None
        if old_snippet != edits["old_snippet"] or snippet.is_symlink() or main.is_symlink():
            raise UpdateError("The legacy KCM settings file changed while awaiting confirmation; try again.")
        backup = self.store.root / "backups" / "moonraker" / (time.strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:8])
        backup.mkdir(parents=True, mode=0o700)
        backup_paths = {main, *edits["updates"]}
        records = {}
        for number, path in enumerate(sorted(backup_paths)):
            if path == main or path == snippet:
                relative = Path(path.name)
            elif path.is_relative_to(main.parent):
                relative = Path("includes") / path.relative_to(main.parent)
            else:
                relative = Path("external-includes") / str(number) / path.name
            destination = backup / relative
            destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            _write_atomic(destination, edits["originals"][path])
            records[str(path)] = relative.as_posix()
        atomic_json(backup / "backup-info.json", {"repository": plan["repository"], "moonraker_config": str(main),
                    "legacy_settings": str(snippet), "files": records, "created": time.time()})
        modes = {path: stat.S_IMODE(path.stat().st_mode) for path in edits["updates"]}
        applied = []
        try:
            # Write the selected main file last, after the old duplicate updater
            # and any dangling legacy include references have been removed.
            for path in sorted(edits["updates"], key=lambda path: (path == main, str(path))):
                if path.is_symlink() or _read(path) != edits["originals"][path]:
                    raise UpdateError("A configuration file changed during setup; review it and retry.")
                _write_atomic(path, edits["updates"][path], modes[path])
                applied.append(path)
        except BaseException as original_error:
            failures = []
            for path in reversed(applied):
                try:
                    _write_atomic(path, edits["originals"][path], modes[path])
                except BaseException:
                    failures.append(str(path))
            if failures:
                raise UpdateError(f"Configuration changes failed and automatic restoration failed for {', '.join(failures)}. Restore the saved originals from {backup}.") from original_error
            raise
        result = {key: value for key, value in plan.items() if key != "_edits"}
        result["backup_dir"] = str(backup)
        self.output(f"Moonraker updater saved directly in {main}. Backups: {backup}")
        self.output("Restart the intended Moonraker instance when the printer is idle, then refresh its update manager. This entry updates the KCM application; firmware flashing remains an explicit KCM operation.")
        return result
