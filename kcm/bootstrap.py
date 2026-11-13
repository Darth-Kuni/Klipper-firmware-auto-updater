"""Separate first-install workflow; a working Klipper connection is not required.

Bootloader installation is deliberately separate from routine MCU updates.
The program never guesses a board's processor, pins, clock, or flash offset.
"""
from __future__ import annotations

from dataclasses import asdict, replace
import json
import os
import platform
import re
import shlex
import shutil
import signal
import struct
import sys
import tempfile
import time
from pathlib import Path
from typing import Callable
import uuid

from .engine import Manager, Runner, UpdateError, config_changes, digest, flash_commands, kconfig_values
from .models import Component, Printer, validate_id
from .storage import Store, atomic_json


OFFICIAL_SOURCES = {"klipper": "https://github.com/Klipper3d/klipper.git",
                    "katapult": "https://github.com/Arksine/katapult.git"}
DEBIAN_PACKAGES = ("build-essential", "git", "python3-dev", "python3-venv", "python3-pip",
                   "python3-serial", "libffi-dev", "libusb-1.0-0-dev", "libncurses-dev", "pkg-config",
                   "dfu-util", "gcc-arm-none-eabi", "binutils-arm-none-eabi",
                   "libnewlib-arm-none-eabi", "stm32flash", "can-utils")
KATAPULT_GUIDE = "https://github.com/Arksine/katapult#building"


class Bootstrap:
    def __init__(self, store: Store, output: Callable[[str], None] = print, runner: Runner | None = None):
        self.store, self.output = store, output
        self.runner = runner or Runner(output)
        self.manager = Manager(store, output, self.runner)

    def _require_linux(self):
        self.manager._require_linux()

    def _lock(self):
        return self.manager._lock()

    @staticmethod
    def _os_release() -> dict[str, str]:
        values = {}
        try:
            for line in Path("/etc/os-release").read_text(encoding="utf-8").splitlines():
                if "=" in line and not line.startswith("#"):
                    key, value = line.split("=", 1)
                    tokens = shlex.split(value)
                    values[key] = tokens[0] if tokens else ""
        except OSError:
            pass
        return values

    def inspect_host(self) -> dict:
        """Inventory files/tools only; never probe or reset attached CAN nodes."""
        serial = {str(p) for kind in ("by-id", "by-path") for p in Path("/dev/serial", kind).glob("*")}
        serial.update(str(p) for pattern in ("ttyAMA*", "ttyS*", "ttyUSB*", "ttyACM*") for p in Path("/dev").glob(pattern))
        interfaces = []
        for interface in Path("/sys/class/net").glob("*"):
            try:
                if (interface / "type").read_text().strip() == "280":
                    interfaces.append({"name": interface.name, "state": (interface / "operstate").read_text().strip()})
            except OSError:
                continue
        mounts = []
        try:
            for line in Path("/proc/self/mounts").read_text().splitlines():
                mount = re.sub(r"\\([0-7]{3})", lambda m: chr(int(m[1], 8)), line.split()[1])
                info = Path(mount) / "INFO_UF2.TXT"
                if info.is_file():
                    mounts.append({"path": mount, "info": info.read_text(errors="replace")[:4096]})
        except (OSError, IndexError):
            pass
        commands = ("git", "make", "systemctl", "apt-get", "dfu-util", "arm-none-eabi-gcc", "ip")
        return {"platform": platform.system(), "os_release": self._os_release(),
                "python": sys.version.split()[0], "tools": {c: shutil.which(c) for c in commands},
                "serial_devices": sorted(serial), "can_interfaces": interfaces, "uf2_mounts": mounts}

    @staticmethod
    def _confirm(confirm, text):
        if not confirm(text):
            raise UpdateError("Setup cancelled before making the proposed change.")

    def prepare_sources(self, klipper_path, katapult_path, confirm) -> None:
        self._require_linux()
        sources = [(name, Path(path).expanduser()) for name, path in
                   (("klipper", klipper_path), ("katapult", katapult_path))]
        for _, path in sources:
            if not path.is_absolute() or path.is_symlink() or ".." in path.parts:
                raise UpdateError("Source destinations must be absolute paths without symlinks or '..'.")
        first, second = (path.resolve() for _, path in sources)
        if first == second or first in second.parents or second in first.parents:
            raise UpdateError("Klipper and Katapult need separate, non-nested source directories.")
        missing = []
        with self._lock():
            for name, path in sources:
                if path.exists():
                    if not (path / ".git").exists() or not (path / "src/Kconfig").is_file():
                        raise UpdateError(f"Existing destination is not a source checkout: {path}. It will not be overwritten.")
                    self.output(f"Reusing {name}: {path}; no update or reset will be performed.")
                else:
                    missing.append((name, path))
            if not missing:
                return
            proposal = "\n".join(f"Clone {OFFICIAL_SOURCES[name]} into {path}" for name, path in missing)
            self._confirm(confirm, proposal + "\nContinue with these source downloads?")
            for name, path in missing:
                path.parent.mkdir(parents=True, exist_ok=True)
                self.runner.run(["git", "clone", "--", OFFICIAL_SOURCES[name], str(path)])

    def install_dependencies(self, confirm) -> None:
        self._require_linux()
        release = self._os_release()
        families = set((release.get("ID", "") + " " + release.get("ID_LIKE", "")).split())
        if not families.intersection({"debian", "ubuntu"}) or not shutil.which("apt-get"):
            raise UpdateError("Automatic dependency installation supports Debian/Ubuntu hosts; install the documented packages with your distribution's package manager.")
        self._confirm(confirm, "Refresh apt package lists and install these build/flash tools:\n" +
                      " ".join(DEBIAN_PACKAGES) + "\nExisting packages will be retained. Continue?")
        with self._lock():
            self.runner.run(["sudo", "-v"], interactive=True)
            self.runner.run(["sudo", "-n", "apt-get", "update"])
            self.runner.run(["sudo", "-n", "apt-get", "install", "--yes", *DEBIAN_PACKAGES])

    def _board(self, printer: Printer, component: Component):
        # A new CAN board has no known Katapult UUID yet. Validate build identity
        # here; fully validate transport identity before its first Klipper write.
        replace(printer, components=[]).validate()
        validate_id(component.id, "Component ID")
        if not isinstance(component.name, str) or not component.name.strip():
            raise UpdateError("Give the physical board a name before configuring it.")
        if not Path(component.config_file).is_absolute():
            raise UpdateError("The board build configuration needs an absolute path.")

    def _config_path(self, printer, component) -> Path:
        return self.store.config_path(printer.id, component.id).with_suffix(".katapult.config")

    def _snapshot(self, repo: Path, destination: Path) -> str:
        if not (repo / "src/Kconfig").is_file():
            raise UpdateError(f"Katapult source is missing: {repo}")
        commit = self.manager._git(repo, "rev-parse", "HEAD")
        if not re.fullmatch(r"[0-9a-f]{40,64}", commit):
            raise UpdateError("Cannot identify the Katapult source commit.")
        if self.manager._git(repo, "status", "--porcelain", "--untracked-files=no"):
            raise UpdateError("Katapult has tracked source changes; reconcile them before building.")
        self.runner.run(["git", "clone", "--no-hardlinks", "--no-checkout", "--", str(repo), str(destination)])
        self.manager._git(destination, "checkout", "--detach", commit)
        if (destination / ".gitmodules").is_file():
            self.manager._git(destination, "submodule", "update", "--init", "--recursive")
        return commit

    def configure_katapult(self, printer, component, import_path=None) -> Path:
        self._require_linux()
        self._board(printer, component)
        with self._lock(), tempfile.TemporaryDirectory(prefix="kcm-katapult-", dir=self.store.root) as temporary:
            source = Path(temporary) / "source"
            self._snapshot(Path(printer.katapult_path), source)
            destination = self._config_path(printer, component)
            initial = Path(import_path).expanduser() if import_path else destination
            if initial.is_file():
                shutil.copyfile(initial, source / ".config")
            elif import_path:
                raise UpdateError(f"Imported Katapult configuration does not exist: {initial}")
            self.output("Confirm the exact board/revision, MCU, crystal, interface pins, bootloader-entry method, and application offset using its manufacturer's instructions.")
            self.output("Choose 'Do not build' for the Katapult deployment application. " + KATAPULT_GUIDE)
            self.runner.run(["make", "menuconfig"], cwd=source, interactive=True)
            config = source / ".config"
            if not config.is_file() or not kconfig_values(config.read_text(encoding="utf-8")):
                raise UpdateError("No Katapult configuration was saved.")
            self._no_deployer(kconfig_values(config.read_text(encoding="utf-8")))
            staged = destination.with_suffix(destination.suffix + ".tmp")
            shutil.copyfile(config, staged)
            staged.replace(destination)
            return destination

    @staticmethod
    def _no_deployer(values):
        origin, base = values.get("CONFIG_FLASH_APPLICATION_ADDRESS"), values.get("CONFIG_FLASH_BOOT_ADDRESS")
        if values.get("CONFIG_BUILD_DEPLOYER") == "y" or (origin is not None and base is not None and Bootstrap._address(origin) != Bootstrap._address(base)):
            raise UpdateError("Automatic bootloader installation does not support the Katapult deployer. Select 'Do not build' for the deployment application.")

    @staticmethod
    def _address(value):
        try:
            return int(value, 0)
        except (TypeError, ValueError) as exc:
            raise UpdateError("A required firmware flash address is missing or invalid; review the board build configuration.") from exc

    def _new_job(self, printer, component, kind):
        job = self.store.root / "bootstrap" / printer.id / component.id / (time.strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:8])
        job.mkdir(parents=True)
        manifest = {"schema_version": 1, "kind": kind, "state": "preparing", "printer_id": printer.id,
                    "component_id": component.id, "created": time.time()}
        atomic_json(job / "manifest.json", manifest)
        return job, manifest

    def build_katapult(self, printer, component) -> Path:
        self._require_linux()
        self._board(printer, component)
        with self._lock():
            job, manifest = self._new_job(printer, component, "katapult")
            previous, self.runner.log = self.runner.log, job / "build.log"
            try:
                saved = self._config_path(printer, component)
                shutil.copyfile(saved, job / "katapult.config")
                before = (job / "katapult.config").read_text(encoding="utf-8")
                if not kconfig_values(before):
                    raise UpdateError("The saved Katapult configuration is empty.")
                self._no_deployer(kconfig_values(before))
                source = job / "source"
                commit = self._snapshot(Path(printer.katapult_path), source)
                shutil.copyfile(job / "katapult.config", source / ".config")
                self.runner.run(["make", "olddefconfig"], cwd=source)
                changes = config_changes(before, (source / ".config").read_text(encoding="utf-8"))
                if changes:
                    (job / "config-changes.txt").write_text("\n".join(changes), encoding="utf-8")
                    raise UpdateError("Katapult configuration changed with this source; review/reconfigure before building.")
                self.runner.run(["make", "-j", str(min(os.cpu_count() or 1, 4))], cwd=source)
                artifacts = {}
                for name in ("katapult.bin", "katapult.uf2", "katapult.withclear.uf2"):
                    built = source / "out" / name
                    if built.is_file() and built.stat().st_size:
                        shutil.copyfile(built, job / name)
                        artifacts[name] = digest(job / name)
                if not artifacts:
                    raise UpdateError("Katapult produced no non-empty bootloader artifact.")
                manifest.update(state="ready", source_commit=commit, source_path=printer.katapult_path,
                                component=asdict(component), config_sha256=digest(job / "katapult.config"), artifacts=artifacts)
                atomic_json(job / "manifest.json", manifest)
                return job
            except BaseException as exc:
                manifest.update(state="failed", error=str(exc) or type(exc).__name__)
                atomic_json(job / "manifest.json", manifest)
                raise
            finally:
                self.runner.log = previous

    def _check_katapult(self, printer, component, job_dir):
        job = Path(job_dir).resolve()
        expected = (self.store.root / "bootstrap" / printer.id / component.id).resolve()
        if not expected.is_relative_to(self.store.root) or job.parent != expected or Path(job_dir).is_symlink():
            raise UpdateError("Select this board's saved Katapult build job.")
        manifest = json.loads((job / "manifest.json").read_text(encoding="utf-8"))
        if (type(manifest.get("schema_version")) is not int or manifest["schema_version"] != 1 or
                manifest.get("kind") != "katapult" or manifest.get("state") != "ready" or
                manifest.get("printer_id") != printer.id or manifest.get("component_id") != component.id or
                manifest.get("source_path") != printer.katapult_path):
            raise UpdateError("The Katapult build is incomplete, already used, or belongs to another board/source.")
        if not re.fullmatch(r"[0-9a-f]{40,64}", manifest.get("source_commit", "")):
            raise UpdateError("The Katapult job has no valid recorded source commit.")
        config = job / "katapult.config"
        if (config.is_symlink() or digest(config) != manifest.get("config_sha256") or
                digest(self._config_path(printer, component)) != manifest.get("config_sha256")):
            raise UpdateError("Katapult build settings changed since compilation; rebuild before installing.")
        artifacts = manifest.get("artifacts", {})
        if not isinstance(artifacts, dict) or not artifacts:
            raise UpdateError("No Katapult artifacts are recorded.")
        for name, checksum in artifacts.items():
            if name not in {"katapult.bin", "katapult.uf2", "katapult.withclear.uf2"} or (job / name).is_symlink() or digest(job / name) != checksum:
                raise UpdateError("Katapult artifact integrity check failed.")
        values = kconfig_values(config.read_text(encoding="utf-8"))
        self._no_deployer(values)
        return job, manifest, values

    def _inactive(self, printer):
        response = self.runner.run(["systemctl", "is-active", printer.service], check=False)
        state = (response.stdout or "").strip()
        if response.returncode not in (3, 4) or state not in {"inactive", "failed", "unknown"}:
            raise UpdateError(f"Klipper service {printer.service} must be stopped before initial setup (reported {state or 'unknown'}).")

    def _physical_confirm(self, printer, component, confirm, action):
        self._inactive(printer)
        self._confirm(confirm, f"{action} on '{component.name}' ({component.id}). Confirm this exact physical board is selected, heaters are cold and disconnected from power, hazardous outputs are isolated, and you have followed the board manufacturer's boot-mode instructions. Continue?")

    @staticmethod
    def _uf2_target(target: str, mcu: str, artifact: Path) -> Path:
        mount = Path(target).expanduser()
        if not mount.is_absolute() or mount.is_symlink() or not mount.is_dir() or not mount.is_mount():
            raise UpdateError("UF2 target must be an absolute, mounted BOOTSEL device directory.")
        info = mount / "INFO_UF2.TXT"
        if not info.is_file() or info.is_symlink():
            raise UpdateError("The mounted device has no valid INFO_UF2.TXT boot identity.")
        identity = info.read_text(errors="replace").upper()
        if mcu not in {"rp2040", "rp2350"} or (mcu == "rp2040" and not ("RP2040" in identity or "RPI-RP2" in identity)) or (mcu == "rp2350" and "RP2350" not in identity):
            raise UpdateError("Mounted UF2 boot device does not match the configured RP processor.")
        families = {"rp2040": {0xE48BFF56}, "rp2350": {0xE48BFF57, 0xE48BFF59, 0xE48BFF5A, 0xE48BFF5B}}
        data = artifact.read_bytes()
        if not data or len(data) % 512:
            raise UpdateError("Invalid UF2 artifact length.")
        for position in range(0, len(data), 512):
            first, second, flags, _, size, _, _, family = struct.unpack_from("<8I", data, position)
            if (first != 0x0A324655 or second != 0x9E5D5157 or not flags & 0x2000 or size > 476 or
                    family not in families[mcu] or struct.unpack_from("<I", data, position + 508)[0] != 0x0AB16F30):
                raise UpdateError("UF2 artifact processor family or block format does not match this board.")
        destination = mount / "katapult.uf2"
        if destination.is_symlink():
            raise UpdateError("UF2 destination must not be a symlink.")
        return destination

    def install_katapult(self, printer, component, job_dir, method: str, target: str, confirm) -> None:
        self._require_linux()
        self._board(printer, component)
        if method not in {"dfu", "uf2"}:
            raise UpdateError(f"{method} bootloader installation requires a board-specific manual walkthrough. No write was attempted. Use the manufacturer's guide and {KATAPULT_GUIDE}; do not flash deployer.bin as a generic replacement.")
        with self._lock():
            job, manifest, values = self._check_katapult(printer, component, job_dir)
            mcu = values.get("CONFIG_MCU", "").strip('"')
            if method == "dfu":
                if (values.get("CONFIG_MACH_STM32") != "y" or not mcu.startswith("stm32") or
                        self._address(values.get("CONFIG_FLASH_BOOT_ADDRESS")) != 0x08000000 or
                        self._address(values.get("CONFIG_FLASH_APPLICATION_ADDRESS")) != 0x08000000):
                    raise UpdateError("Automatic DFU requires a confirmed STM32 configuration with flash base 0x08000000.")
                if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", target) or "katapult.bin" not in manifest["artifacts"]:
                    raise UpdateError("DFU needs an exact USB serial number and a verified katapult.bin; VID:PID and wildcard targets are not accepted.")
                artifact, destination = job / "katapult.bin", None
            else:
                name = "katapult.withclear.uf2" if "katapult.withclear.uf2" in manifest["artifacts"] else "katapult.uf2"
                if name not in manifest["artifacts"]:
                    raise UpdateError("This build contains no verified UF2 bootloader.")
                artifact = job / name
                destination = self._uf2_target(target, mcu, artifact)
            self._physical_confirm(printer, component, confirm, f"Install Katapult using {method.upper()} target {target}")
            previous, self.runner.log = self.runner.log, job / "install.log"
            try:
                self._inactive(printer)
                self._check_katapult(printer, component, job)
                if method == "uf2":
                    destination = self._uf2_target(target, mcu, artifact)
                manifest.update(state="installing", install_method=method, install_target=target)
                atomic_json(job / "manifest.json", manifest)
                if method == "dfu":
                    self.runner.run(["sudo", "-v"], interactive=True)
                    self.runner.run(["sudo", "-n", "dfu-util", "-S", target, "-a", "0", "-s", "0x08000000:leave", "-D", str(artifact)], protect_write=True)
                else:
                    # BOOTSEL may disappear immediately after the final block.
                    previous_signal = signal.signal(signal.SIGINT, signal.SIG_IGN)
                    try:
                        with artifact.open("rb") as source, destination.open("wb") as output:
                            shutil.copyfileobj(source, output)
                            output.flush()
                            os.fsync(output.fileno())
                    finally:
                        signal.signal(signal.SIGINT, previous_signal)
                manifest.update(state="installed_unverified", finished=time.time())
                atomic_json(job / "manifest.json", manifest)
                self.output("Katapult upload finished. Reconnect/reboot as directed by the board manufacturer and verify its bootloader identity before flashing Klipper.")
            except BaseException as exc:
                manifest.update(state="failed", error=str(exc) or type(exc).__name__)
                atomic_json(job / "manifest.json", manifest)
                raise
            finally:
                self.runner.log = previous

    def first_klipper_flash(self, printer, component, firmware_path, confirm) -> None:
        self._require_linux()
        printer.validate()
        component.validate()
        with self._lock():
            selected = Path(firmware_path)
            if selected.is_symlink() or not selected.exists():
                raise UpdateError("Select an existing saved Klipper build job or its verified board artifact.")
            path = selected.resolve()
            if not path.is_dir() and len(path.parents) < 3:
                raise UpdateError("Select a saved Klipper build job or its verified board artifact.")
            job = path if path.is_dir() else path.parents[2]
            manifest = self.manager._check_job(printer, job)
            records = [r for r in manifest["components"] if r["id"] == component.id]
            if len(records) != 1 or component.to_dict() not in [c.to_dict() for c in printer.components]:
                raise UpdateError("Initial firmware must come from this board's validated all-component build job.")
            artifact = job / records[0]["artifact"]
            if artifact.is_symlink() or (path != job and path != artifact.resolve()):
                raise UpdateError("The selected firmware path is not the recorded board artifact.")
            current = self.manager._git(printer.klipper_path, "rev-parse", "HEAD")
            if manifest["commit"] != current:
                raise UpdateError("Build initial Klipper firmware from the current host source (without --latest), so the host and MCU versions match.")
            boot_config = self._config_path(printer, component)
            if not boot_config.is_file():
                raise UpdateError("Save/review this board's Katapult configuration first, so its MCU and application start address can be checked against Klipper.")
            installs = []
            for record_path in (self.store.root / "bootstrap" / printer.id / component.id).glob("*/manifest.json"):
                record = json.loads(record_path.read_text(encoding="utf-8"))
                if (record.get("kind") == "katapult" and record.get("state") == "installed_unverified"
                        and record.get("printer_id") == printer.id and record.get("component_id") == component.id):
                    installs.append(record)
            if installs:
                installed = max(installs, key=lambda record: record.get("finished", 0))
                if installed.get("config_sha256") != digest(boot_config):
                    raise UpdateError("Katapult settings changed after its recorded installation. Restore the installed bootloader's saved configuration or complete a new verified installation before flashing Klipper.")
            boot = kconfig_values(boot_config.read_text(encoding="utf-8"))
            firmware = kconfig_values(Path(component.config_file).read_text(encoding="utf-8"))
            if not boot.get("CONFIG_MCU") or boot["CONFIG_MCU"] != firmware.get("CONFIG_MCU"):
                raise UpdateError("Katapult and Klipper configurations select different MCU models.")
            if self._address(boot.get("CONFIG_LAUNCH_APP_ADDRESS")) != self._address(firmware.get("CONFIG_FLASH_APPLICATION_ADDRESS")):
                raise UpdateError("Klipper's application address does not match Katapult's launch address. Review the bootloader offset before flashing.")
            tool = Path(printer.katapult_path) / "scripts/flashtool.py"
            if not tool.is_file():
                raise UpdateError("Katapult flashtool.py is missing.")
            self._physical_confirm(printer, component, confirm, "Write initial Klipper firmware through the known Katapult bootloader")
            operation, journal = self._new_job(printer, component, "initial-klipper")
            journal.update(source_job=str(job), artifact=str(artifact), sha256=digest(artifact))
            previous, self.runner.log = self.runner.log, operation / "flash.log"
            try:
                self._inactive(printer)
                self.manager._check_job(printer, job)
                journal["state"] = "flashing"
                atomic_json(operation / "manifest.json", journal)
                _, command = flash_commands(printer, replace(component, bootloader_entry="already"), artifact)
                self.runner.run(command, protect_write=True)
                journal.update(state="flashed_unverified", finished=time.time())
                atomic_json(operation / "manifest.json", journal)
                self.output("Initial Klipper upload finished. The service remains stopped; finish all board setup and verify printer configuration before starting it.")
            except BaseException as exc:
                journal.update(state="failed", error=str(exc) or type(exc).__name__)
                atomic_json(operation / "manifest.json", journal)
                raise
            finally:
                self.runner.log = previous

    def list_dfu(self) -> str:
        self._require_linux()
        if not shutil.which('dfu-util'):
            raise UpdateError('Install the host flashing tools before listing DFU devices.')
        result = self.runner.run(['dfu-util', '-l'], check=False)
        if result.returncode:
            raise UpdateError('DFU listing failed. Check the USB cable, ROM boot mode and USB permissions; no firmware was written.')
        return result.stdout or 'No DFU devices reported.'

    def find_can_uuid(self, printer, can_interface, confirm) -> list[str]:
        self._require_linux()
        replace(printer, components=[]).validate()
        if not isinstance(can_interface, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,14}", can_interface):
            raise UpdateError("Enter a valid Linux CAN interface name.")
        with self._lock():
            self._inactive(printer)
            self._confirm(confirm, f"Query Katapult on {can_interface}? Confirm exactly ONE powered CAN node is connected to this bus, with the correct termination and bit rate, heaters cold and isolated. Queries with multiple powered nodes can disrupt the bus.")
            tool = Path(printer.katapult_path) / "scripts/flashtool.py"
            if not tool.is_file():
                raise UpdateError("Katapult flashtool.py is missing.")
            session = self.store.root / "setup" / ("can-query-" + uuid.uuid4().hex[:12])
            session.mkdir(parents=True)
            previous, self.runner.log = self.runner.log, session / "query.log"
            journal = {"schema_version": 1, "kind": "can-discovery", "state": "querying", "printer_id": printer.id, "interface": can_interface}
            try:
                self._inactive(printer)
                atomic_json(session / "manifest.json", journal)
                response = self.runner.run([sys.executable, str(tool), "-i", can_interface, "-q"])
                # Current Katapult prints 'Detected UUID: ..., Application: Katapult'.
                # Also recognize older canbus_uuid output, but never offer a node
                # reporting a running Klipper application as a bootloader target.
                found = set()
                for line in (response.stdout or "").splitlines():
                    if re.search(r"Application:\s*(?!Katapult\b|CanBoot\b)\S+", line, re.I):
                        continue
                    match = re.search(r"(?:canbus_uuid=|Detected UUID:\s*)([0-9a-fA-F]{12})\b", line)
                    if match:
                        found.add(match[1].lower())
                values = sorted(found)
                journal.update(state="complete", uuids=values)
                atomic_json(session / "manifest.json", journal)
                return values
            except BaseException as exc:
                journal.update(state="failed", error=str(exc) or type(exc).__name__)
                atomic_json(session / "manifest.json", journal)
                raise
            finally:
                self.runner.log = previous
