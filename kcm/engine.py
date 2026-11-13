"""Build and flash saved Klipper components using upstream tools.

All hardware operations are explicit. Builds use private source snapshots and
flashing consumes a complete, checksummed job, never the shared ``out`` folder.
"""
from __future__ import annotations

import contextlib
import hashlib
import json
import math
import os
import platform
import re
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import urllib.parse
import urllib.request
import uuid
from pathlib import Path
from typing import Callable

from .models import Component, Printer
from .storage import Store, atomic_json


class UpdateError(RuntimeError):
    """An update could not proceed; the job retains the useful details."""


def digest(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest() if hasattr(hashlib, "file_digest") else _digest(stream)


def _digest(stream) -> str:
    value = hashlib.sha256()
    for chunk in iter(lambda: stream.read(1024 * 1024), b""):
        value.update(chunk)
    return value.hexdigest()


def kconfig_values(text: str) -> dict[str, str]:
    """Ignore generated comments/order, retain disabled selections as well."""
    values = {}
    for line in text.splitlines():
        match = re.fullmatch(r"(CONFIG_[A-Za-z0-9_]+)=(.*)", line.strip())
        disabled = re.fullmatch(r"# (CONFIG_[A-Za-z0-9_]+) is not set", line.strip())
        if match:
            values[match[1]] = match[2]
        elif disabled:
            values[disabled[1]] = "n"
    return values


def config_changes(before: str, after: str) -> list[str]:
    old, new = kconfig_values(before), kconfig_values(after)
    return [f"{key}: {old.get(key, '<absent>')} -> {new.get(key, '<absent>')}"
            for key in sorted(old.keys() | new.keys()) if old.get(key) != new.get(key)]


def flash_commands(printer: Printer, component: Component, firmware: Path,
                   python: str = sys.executable) -> tuple[list[str] | None, list[str]]:
    """Return argv arrays, keeping application and bootloader identities separate."""
    base = [python, str(Path(printer.katapult_path) / "scripts" / "flashtool.py")]
    request = None
    if component.bootloader_entry == "auto":
        if component.can_uuid:
            request = base + ["-i", component.can_interface, "-u", component.can_uuid, "-r"]
        else:
            request = base + ["-d", component.application_device, "-b", str(component.application_baud), "-r"]
    if component.transport == "can":
        flash = base + ["-i", component.can_interface, "-u", component.can_uuid]
    else:
        flash = base + ["-d", component.flash_device, "-b", str(component.baud)]
    return request, flash + ["-f", str(firmware)]


class Runner:
    def __init__(self, output: Callable[[str], None] = print):
        self.output = output
        self.log: Path | None = None

    def run(self, argv: list[str], cwd: Path | str | None = None,
            interactive: bool = False, check: bool = True,
            protect_write: bool = False, quiet: bool = False) -> subprocess.CompletedProcess:
        command = "$ " + shlex.join(str(x) for x in argv)
        if not quiet:
            self.output(command)
        if self.log:
            with self.log.open("a", encoding="utf-8") as stream:
                stream.write(command + "\n")
        # During a firmware write, let the upstream tool finish rather than
        # forwarding Ctrl-C. Cancellation still works between boards.
        previous = None
        if protect_write:
            previous = signal.signal(signal.SIGINT, signal.SIG_IGN)
        try:
            if interactive:
                result = subprocess.run(argv, cwd=cwd, check=False)
            else:
                with subprocess.Popen(argv, cwd=cwd, stdout=subprocess.PIPE,
                                      stderr=subprocess.STDOUT, text=True,
                                      encoding="utf-8", errors="replace",
                                      start_new_session=protect_write and os.name == "posix") as proc:
                    chunks = []
                    assert proc.stdout is not None
                    for line in proc.stdout:
                        chunks.append(line)
                        if not quiet:
                            self.output(line.rstrip())
                        if self.log:
                            with self.log.open("a", encoding="utf-8") as stream:
                                stream.write(line)
                    result = subprocess.CompletedProcess(argv, proc.wait(), "".join(chunks))
            if check and result.returncode:
                raise UpdateError(f"Command exited {result.returncode}: {shlex.join(argv)}")
            return result
        finally:
            if previous is not None:
                signal.signal(signal.SIGINT, previous)


class Manager:
    def __init__(self, store: Store, output: Callable[[str], None] = print,
                 runner: Runner | None = None):
        self.store = store
        self.output = output
        self.runner = runner or Runner(output)

    def _git(self, path: Path | str, *args: str, check: bool = True) -> str:
        return (self.runner.run(["git", "-C", str(path), *args], check=check, quiet=True).stdout or "").strip()

    def _require_linux(self):
        if platform.system() != "Linux":
            raise UpdateError("Build/configure/flash must run on the Linux printer host. SSH into it first.")

    @contextlib.contextmanager
    def _lock(self):
        # One lock for the host/user, including different configured data dirs.
        # fcntl releases automatically after process exit; no stale PID cleanup.
        import fcntl
        lock_root = Path(os.environ.get("XDG_RUNTIME_DIR", str(Path.home() / ".cache")))
        lock_root.mkdir(parents=True, exist_ok=True)
        with (lock_root / "klipper-component-manager.lock").open("a") as stream:
            try:
                fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise UpdateError("Another component-manager operation is running.") from exc
            yield

    def _validate_source(self, printer: Printer):
        printer.validate()
        repo = Path(printer.klipper_path)
        if not (repo / "src" / "Kconfig").is_file():
            raise UpdateError(f"Klipper source not found at {repo}")
        top = self._git(repo, "rev-parse", "--show-toplevel")
        if Path(top).resolve() != repo.resolve():
            raise UpdateError("Klipper path must point at the repository root.")
        if self._git(repo, "status", "--porcelain", "--untracked-files=no"):
            raise UpdateError("Klipper has tracked local changes. Commit/reconcile them before updating.")
        if not self._git(repo, "symbolic-ref", "--quiet", "--short", "HEAD", check=False):
            raise UpdateError("Klipper is on a detached commit. Select a branch before updating.")
        for other in self.store.load():
            if other.id != printer.id and Path(other.klipper_path).resolve() == repo.resolve():
                raise UpdateError("Two printer profiles share this Klipper checkout. Coordinated shared-checkout updates are not supported yet.")

    def _snapshot(self, printer: Printer, target: Path, refresh: bool) -> tuple[str, str]:
        self._validate_source(printer)
        starting = self._git(printer.klipper_path, "rev-parse", "HEAD")
        self.runner.run(["git", "clone", "--no-hardlinks", "--no-checkout",
                         "--", printer.klipper_path, str(target)])
        commit = starting
        if refresh:
            ref = printer.source_ref
            if not re.fullmatch(r"origin/[A-Za-z0-9][A-Za-z0-9_./-]*", ref) or ".." in ref:
                raise UpdateError("Latest builds require an explicit origin/branch source reference.")
            remote = self._git(printer.klipper_path, "remote", "get-url", "origin")
            self._git(target, "remote", "set-url", "origin", remote)
            branch = ref.removeprefix("origin/")
            self._git(target, "fetch", "--no-tags", "origin", f"+refs/heads/{branch}:refs/remotes/origin/{branch}")
            commit = self._git(target, "rev-parse", "--verify", ref + "^{commit}")
        self._git(target, "checkout", "--detach", commit)
        # Klipper dependencies are ordinarily vendored; recurse only when the
        # selected source actually uses git submodules.
        if (target / ".gitmodules").exists():
            self._git(target, "submodule", "update", "--init", "--recursive")
        return starting, commit

    def doctor(self, printer: Printer) -> list[str]:
        messages = []
        printer.validate()
        for command in ("git", "make", "systemctl"):
            messages.append(("OK " if shutil.which(command) else "MISSING ") + command)
        for path, label in ((Path(printer.klipper_path) / "src/Kconfig", "Klipper Kconfig"),
                            (Path(printer.katapult_path) / "scripts/flashtool.py", "Katapult flashtool")):
            messages.append(("OK " if path.is_file() else "MISSING ") + label + f": {path}")
        for comp in printer.components:
            messages.append(("OK " if Path(comp.config_file).is_file() else "MISSING ") + f"{comp.name} build configuration")
            if comp.transport != "can":
                messages.append(("PRESENT " if Path(comp.flash_device).exists() else "NOT PRESENT (may appear in bootloader) ") + comp.flash_device)
        try:
            import serial  # noqa: F401 - pyserial is an upstream Katapult dependency
            messages.append("OK pyserial for Katapult USB/UART")
        except ImportError:
            messages.append("MISSING pyserial in this Python environment (needed for USB/UART)")
        if platform.system() != "Linux":
            messages.append("NOTICE run configure/build/flash on Linux over SSH")
        try:
            status = self._moonraker(printer, "/printer/info")
            messages.append("OK Moonraker, Klipper state: " + str(status.get("state", "unknown")))
        except Exception as exc:
            messages.append("UNREACHABLE Moonraker: " + str(exc))
        return messages

    def plan(self, printer: Printer) -> dict:
        printer.validate()
        return {"printer": printer.name, "service": printer.service,
                "source": printer.klipper_path, "latest_ref": printer.source_ref,
                "operations": ["Validate and build all components from one commit",
                               "Check idle/cool state, back up configuration, stop Klipper",
                               "Fast-forward host source if required", "Flash components in dependency order",
                               "Restart and check Klipper readiness"],
                "flash_order": [{"id": c.id, "name": c.name, "transport": c.transport,
                                 "via": c.via, "target": c.can_uuid if c.transport == "can" else c.flash_device}
                                for c in printer.ordered_components()]}

    def configure(self, printer: Printer, component: Component,
                  import_path: str | None = None, refresh: bool = False):
        self._require_linux()
        # The CLI configures a new component before adding it to the registry.
        component.validate()
        with self._lock(), tempfile.TemporaryDirectory(prefix="kcm-config-") as temp:
            source = Path(temp) / "source"
            _, commit = self._snapshot(printer, source, refresh)
            dest = Path(component.config_file)
            initial = Path(import_path).expanduser() if import_path else dest
            if initial.is_file():
                shutil.copyfile(initial, source / ".config")
            elif import_path:
                raise UpdateError(f"Configuration file not found: {initial}")
            if not import_path:
                self.output("Use your board manufacturer's settings: processor, clock, bootloader offset, interface and pins.")
                self.runner.run(["make", "menuconfig"], cwd=source, interactive=True)
            else:
                before = (source / ".config").read_text(encoding="utf-8")
                self.runner.run(["make", "olddefconfig"], cwd=source)
                changes = config_changes(before, (source / ".config").read_text(encoding="utf-8"))
                if changes:
                    self.output("Imported configuration needs review for this source version:\n" + "\n".join(changes))
                    self.runner.run(["make", "menuconfig"], cwd=source, interactive=True)
            config = source / ".config"
            if not config.is_file() or not kconfig_values(config.read_text(encoding="utf-8")):
                raise UpdateError("No firmware build configuration was saved.")
            dest.parent.mkdir(parents=True, exist_ok=True)
            temporary = dest.with_name(dest.name + ".tmp")
            shutil.copyfile(config, temporary)
            temporary.replace(dest)
            atomic_json(dest.with_suffix(".meta.json"), {"source_commit": commit, "sha256": digest(dest)})

    def jobs(self, printer: Printer) -> list[Path]:
        root = self.store.root / "jobs" / printer.id
        return sorted((p.parent for p in root.glob("*/manifest.json")), reverse=True)

    def build(self, printer: Printer, refresh: bool = False) -> Path:
        self._require_linux()
        if not printer.components:
            raise UpdateError("Add at least one component before building.")
        with self._lock():
            job = self.store.root / "jobs" / printer.id / (time.strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:8])
            job.mkdir(parents=True)
            previous_log = self.runner.log
            self.runner.log = job / "build.log"
            manifest = {"schema_version": 1, "state": "building", "printer": printer.to_dict(),
                        "components": [], "created": time.time()}
            atomic_json(job / "manifest.json", manifest)
            try:
                starting, commit = self._snapshot(printer, job / "source", refresh)
                manifest.update(host_start_commit=starting, commit=commit)
                atomic_json(job / "manifest.json", manifest)
                # Separate build trees avoid sharing Kconfig/generated files.
                for component in printer.ordered_components():
                    self.output(f"Building {component.name} for {commit[:12]}...")
                    source = job / component.id
                    self.runner.run(["git", "clone", "--no-hardlinks", "--no-checkout", "--", str(job / "source"), str(source)])
                    self._git(source, "checkout", "--detach", commit)
                    if (source / ".gitmodules").exists():
                        self._git(source, "submodule", "update", "--init", "--recursive")
                    config = Path(component.config_file)
                    before = config.read_text(encoding="utf-8")
                    if not kconfig_values(before):
                        raise UpdateError(f"{component.name}: empty or invalid Kconfig file.")
                    shutil.copyfile(config, source / ".config")
                    self.runner.run(["make", "olddefconfig"], cwd=source)
                    changes = config_changes(before, (source / ".config").read_text(encoding="utf-8"))
                    if changes:
                        (job / "config-changes.txt").write_text(component.name + "\n" + "\n".join(changes), encoding="utf-8")
                        raise UpdateError(f"{component.name}: build settings changed in this version. Review config-changes.txt, then use configure --latest before rebuilding.")
                    self.runner.run(["make", "-j", str(min(os.cpu_count() or 1, 4))], cwd=source)
                    binary = source / "out" / "klipper.bin"
                    if not binary.is_file() or not binary.stat().st_size:
                        raise UpdateError(f"{component.name}: no klipper.bin produced. This version supports Katapult targets only.")
                    record = {"id": component.id, "artifact": f"{component.id}/out/klipper.bin",
                              "sha256": digest(binary), "config_sha256": digest(config), "state": "built"}
                    manifest["components"].append(record)
                    atomic_json(job / "manifest.json", manifest)
                manifest["state"] = "ready"
                atomic_json(job / "manifest.json", manifest)
                self.output(f"All components built. Job: {job}")
                return job
            except BaseException as exc:
                manifest.update(state="build_failed", error=str(exc) or type(exc).__name__)
                atomic_json(job / "manifest.json", manifest)
                raise
            finally:
                self.runner.log = previous_log

    def _moonraker(self, printer: Printer, endpoint: str) -> dict:
        url = printer.moonraker_url.rstrip("/") + endpoint
        request = urllib.request.Request(url, headers={"Accept": "application/json"})
        with urllib.request.urlopen(request, timeout=8) as response:
            payload = json.load(response)
        if "result" not in payload:
            raise UpdateError("Moonraker returned no result; verify local authorization and the URL.")
        return payload["result"]

    def _idle(self, printer: Printer):
        # A disconnected/not-ready printer cannot prove it is idle or cool.
        info = self._moonraker(printer, "/printer/info")
        if info.get("state") != "ready":
            raise UpdateError("Klipper must be ready before an automatic update. Use the saved job for manual recovery if it is already offline.")
        objects = self._moonraker(printer, "/printer/objects/list").get("objects", [])
        required = {"print_stats", "heaters"}
        if not required.issubset(objects):
            raise UpdateError("Moonraker cannot report print and heater state.")
        status = self._moonraker(printer, "/printer/objects/query?print_stats&heaters").get("status", {})
        state = status.get("print_stats", {}).get("state")
        if state not in {"standby", "complete", "cancelled", "error"}:
            raise UpdateError(f"Printer is busy or paused ({state}). Finish/cancel the print first.")
        heaters = status.get("heaters", {}).get("available_heaters")
        if not isinstance(heaters, list):
            raise UpdateError("Heater inventory is unavailable.")
        if heaters:
            query = "&".join(urllib.parse.quote(h, safe="") for h in heaters)
            states = self._moonraker(printer, "/printer/objects/query?" + query).get("status", {})
            for heater in heaters:
                value = states.get(heater, {})
                numbers = [value.get("target"), value.get("temperature")]
                if not all(type(number) in (int, float) and math.isfinite(number) for number in numbers):
                    raise UpdateError(f"Cannot verify heater state: {heater}")
                if value["target"] != 0 or value["temperature"] >= 50:
                    raise UpdateError(f"{heater} must be off and below 50 C before flashing.")

    def _verify_service(self, printer: Printer):
        if urllib.parse.urlsplit(printer.moonraker_url).hostname not in {"localhost", "127.0.0.1", "::1"}:
            raise UpdateError("Use this host's loopback Moonraker URL, so idle checks apply to the service being updated.")
        info = self._moonraker(printer, "/printer/info")
        source = info.get("klipper_path")
        if not isinstance(source, str) or Path(source).resolve() != Path(printer.klipper_path).resolve():
            raise UpdateError("Moonraker's Klipper checkout does not match this printer profile.")
        response = self.runner.run(["systemctl", "show", "--property=MainPID", "--value", printer.service])
        try:
            pid = int((response.stdout or "").strip())
        except ValueError as exc:
            raise UpdateError("Cannot identify the Klipper service process.") from exc
        if pid <= 0 or type(info.get("process_id")) is not int or pid != info["process_id"]:
            raise UpdateError("Moonraker and the configured service refer to different Klipper processes.")

    def _verify_components(self, printer: Printer):
        status = self._moonraker(printer, "/printer/objects/query?configfile").get("status", {})
        config = status.get("configfile", {}).get("config")
        if not isinstance(config, dict):
            raise UpdateError("Cannot read the active MCU inventory through Moonraker.")
        mcus = {name: values for name, values in config.items() if name == "mcu" or name.startswith("mcu ")}
        matched = set()
        for name, values in mcus.items():
            if values.get("canbus_uuid"):
                candidates = [c for c in printer.components
                              if c.can_uuid.lower() == str(values["canbus_uuid"]).lower()
                              and c.can_interface == values.get("canbus_interface", "can0")]
            else:
                serial = values.get("serial", "")
                candidates = [c for c in printer.components if c.transport != "can" and not c.can_uuid
                              and serial and Path(c.application_device or c.flash_device).resolve() == Path(serial).resolve()]
            if len(candidates) != 1:
                raise UpdateError(f"Active component [{name}] must match exactly one saved board profile. Linux-process/proprietary MCUs require a separate updater in this version.")
            matched.add(candidates[0].id)
        if not mcus or matched != {c.id for c in printer.components}:
            raise UpdateError("Saved board profiles do not match the active printer's complete MCU inventory.")

    def _check_job(self, printer: Printer, job: Path) -> dict:
        expected = (self.store.root / "jobs" / printer.id).resolve()
        job = job.resolve()
        if job.parent != expected:
            raise UpdateError("Select a job from this printer's saved job directory.")
        manifest = json.loads((job / "manifest.json").read_text(encoding="utf-8"))
        if manifest.get("schema_version") != 1 or manifest.get("state") != "ready":
            raise UpdateError("Only a complete, unused ready build can be flashed. Failed jobs require recovery review and a new build.")
        if manifest.get("printer") != printer.to_dict():
            raise UpdateError("Printer/component settings changed since this build. Rebuild first.")
        commit = manifest.get("commit", "")
        if not re.fullmatch(r"[a-f0-9]{40,64}", commit):
            raise UpdateError("Job has no valid source commit.")
        records = manifest.get("components", [])
        if [r.get("id") for r in records] != [c.id for c in printer.ordered_components()]:
            raise UpdateError("The build does not contain every component in the required order.")
        for comp, record in zip(printer.ordered_components(), records):
            artifact = job / record["artifact"]
            if artifact.resolve() != (job / comp.id / "out/klipper.bin").resolve():
                raise UpdateError("Unexpected firmware artifact path.")
            if digest(artifact) != record.get("sha256") or digest(Path(comp.config_file)) != record.get("config_sha256"):
                raise UpdateError(f"{comp.name}: firmware or saved settings changed since the build.")
        return manifest

    def _service(self, printer: Printer, action: str, check: bool = True):
        return self.runner.run(["sudo", "-n", "systemctl", action, printer.service], check=check)

    def _backup_config(self, printer: Printer, job: Path):
        # Ask Moonraker for its real config directory instead of assuming
        # ~/printer_data, especially on KIAUH multi-instance installations.
        info = self._moonraker(printer, "/server/files/roots")
        roots = info if isinstance(info, list) else info.get("roots", [])
        config_root = next((Path(r["path"]) for r in roots if r.get("name") == "config"), None)
        if config_root is None or not config_root.is_dir():
            raise UpdateError("Cannot locate the printer configuration directory through Moonraker.")
        # Include the contents of symlinked macro/config files in the snapshot.
        # A broken link fails the backup before any service is stopped.
        shutil.copytree(config_root, job / "printer-config-backup", symlinks=False)

    def _wait_device(self, device: str, seconds: float = 15):
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            if Path(device).exists():
                return
            time.sleep(0.25)
        raise UpdateError(f"Bootloader serial device did not appear: {device}")

    def _ready(self, printer: Printer, seconds: float = 45):
        deadline = time.monotonic() + seconds
        last = "not connected"
        while time.monotonic() < deadline:
            try:
                info = self._moonraker(printer, "/printer/info")
                last = str(info.get("state_message", info.get("state", "unknown")))
                if info.get("state") == "ready":
                    return
            except Exception as exc:
                last = str(exc)
            time.sleep(1)
        raise UpdateError("Klipper did not become ready: " + last)

    def flash(self, printer: Printer, job_dir: Path,
              confirm: Callable[[str], bool]):
        self._require_linux()
        with self._lock():
            self._validate_source(printer)
            job = Path(job_dir).resolve()
            manifest = self._check_job(printer, job)
            current = self._git(printer.klipper_path, "rev-parse", "HEAD")
            if current != manifest.get("host_start_commit"):
                raise UpdateError("The live Klipper source changed since this build. Rebuild first.")
            if not (Path(printer.katapult_path) / "scripts/flashtool.py").is_file():
                raise UpdateError("Katapult's scripts/flashtool.py is missing.")
            self.runner.run([sys.executable, str(Path(printer.katapult_path) / "scripts/flashtool.py"), "--help"], quiet=True)
            if any(c.transport != "can" for c in printer.components):
                self.runner.run([sys.executable, "-c", "import serial"], quiet=True)
            self._verify_service(printer)
            self._verify_components(printer)
            # Fetch the already-built commit locally. No network/change to
            # running source occurs during the downtime phase.
            self._git(printer.klipper_path, "fetch", "--no-tags", str(job / "source"), manifest["commit"])
            ancestor = self.runner.run(["git", "-C", printer.klipper_path, "merge-base", "--is-ancestor", current, manifest["commit"]], check=False)
            if ancestor.returncode:
                raise UpdateError("Selected source cannot fast-forward the current Klipper branch.")
            self._idle(printer)
            if not confirm(f"Update {printer.name} to {manifest['commit'][:12]} and flash ALL {len(printer.components)} components?"):
                self.output("Update cancelled before stopping Klipper.")
                return
            # Authenticate while the printer is still running. Subsequent
            # privileged steps fail immediately rather than waiting mid-flash.
            self.output("Checking sudo authorization before taking the printer offline...")
            self.runner.run(["sudo", "-v"], interactive=True)
            previous_log = self.runner.log
            self.runner.log = job / "flash.log"
            stopped = False
            changed = False
            try:
                self._backup_config(printer, job)
                # Recheck immediately before taking control of the service.
                self._idle(printer)
                self._verify_service(printer)
                self._verify_components(printer)
                if self._git(printer.klipper_path, "rev-parse", "HEAD") != current:
                    raise UpdateError("Klipper source changed while preparing the update.")
                if self._git(printer.klipper_path, "status", "--porcelain", "--untracked-files=no"):
                    raise UpdateError("Klipper source has changed while preparing the update.")
                stopped = True
                self._service(printer, "stop")
                manifest.update(state="updating", stopped_at=time.time())
                atomic_json(job / "manifest.json", manifest)
                if current != manifest["commit"]:
                    changed = True  # merge may partly complete before error
                    self._git(printer.klipper_path, "merge", "--ff-only", manifest["commit"])
                for component, record in zip(printer.ordered_components(), manifest["components"]):
                    if component.bootloader_entry in {"manual", "already"}:
                        # Physical bootloader entry may have happened even if
                        # the user then cancels the prompt.
                        changed = True
                        if not confirm(f"Ensure {component.name} is in Katapult bootloader mode, then continue?"):
                            raise UpdateError("Cancelled between components.")
                    request, write = flash_commands(printer, component, job / record["artifact"])
                    record["state"] = "requesting_bootloader"
                    atomic_json(job / "manifest.json", manifest)
                    changed = True
                    if request:
                        self.runner.run(request)
                    if component.transport != "can":
                        self._wait_device(component.flash_device)
                    record["state"] = "flashing"
                    atomic_json(job / "manifest.json", manifest)
                    self.runner.run(write, protect_write=True)
                    record["state"] = "flashed"
                    atomic_json(job / "manifest.json", manifest)
                # CAN bridge resets remove/recreate the Linux interface.
                for interface in sorted({c.can_interface for c in printer.components if c.transport == "can" or c.can_uuid}):
                    self.runner.run(["sudo", "-n", "ip", "link", "set", interface, "up"])
                manifest["state"] = "verifying"
                atomic_json(job / "manifest.json", manifest)
                self._service(printer, "start")
                self._ready(printer)
                manifest.update(state="complete", completed=time.time())
                atomic_json(job / "manifest.json", manifest)
                self.output(f"Update complete; Klipper reports ready. Logs: {job}")
            except BaseException as exc:
                manifest.update(state="failed", error=str(exc) or type(exc).__name__, source_or_mcu_changed=changed)
                atomic_json(job / "manifest.json", manifest)
                if stopped:
                    if changed:
                        # A failed readiness check can leave a started service;
                        # explicitly return to a stopped, inspectable state.
                        self._service(printer, "stop", check=False)
                        self.output(f"Update incomplete. Klipper left stopped; inspect {job / 'manifest.json'} and flash.log before recovery.")
                    else:
                        self._service(printer, "start", check=False)
                raise
            finally:
                self.runner.log = previous_log
