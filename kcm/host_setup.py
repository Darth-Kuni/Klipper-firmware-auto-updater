"""Standalone Linux host preparation with staged files and explicit actions.

Existing services and printer configurations are preserved. New services stay
stopped until the user supplies and reviews a real machine configuration.
"""
from __future__ import annotations

import json
import os
import platform
import re
import shlex
import shutil
import sys
import time
import urllib.parse
from pathlib import Path

from .engine import Manager, Runner, UpdateError
from .models import Printer
from .storage import Store, atomic_json


def systemd_arg(value: str) -> str:
    if any(ord(c) < 32 or ord(c) == 127 for c in value):
        raise ValueError("Service arguments must not contain control characters")
    return '"' + value.replace('\\', '\\\\').replace('"', '\\"').replace('%', '%%').replace('$', '$$') + '"'


class HostSetup:
    def __init__(self, store: Store, output=print, runner: Runner | None = None):
        self.store = store
        self.output = output
        self.runner = runner or Runner(output)

    def data_path(self, printer: Printer) -> Path:
        return self.store.root / "host" / printer.id / "printer_data"

    def _root(self, printer: Printer) -> Path:
        return self.store.root / "host" / printer.id

    def _require_linux(self, debian: bool = False):
        if platform.system() != "Linux":
            raise UpdateError("Host installation runs on the Linux printer host. SSH into it first.")
        if os.geteuid() == 0:
            raise UpdateError("Run as your normal Linux user; the program requests sudo for specific steps.")
        if debian:
            from .bootstrap import Bootstrap
            release = Bootstrap._os_release()
            families = set((release.get("ID", "") + " " + release.get("ID_LIKE", "")).split())
            if not families.intersection({"debian", "ubuntu"}) or not shutil.which("apt-get"):
                raise UpdateError("Automatic host installation supports Debian/Ubuntu with systemd. Follow the host guide for your distribution.")

    def _user(self) -> str:
        import pwd
        return pwd.getpwuid(os.getuid()).pw_name

    def _moonraker_service(self, printer: Printer) -> str:
        return "kcm-moonraker-" + printer.id

    def _service_files(self, printer: Printer) -> dict[str, str]:
        user = self._user()
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_.-]*\$?", user):
            raise UpdateError("This user name requires manual systemd service setup.")
        data = self.data_path(printer)
        env = self._root(printer) / "venvs"
        command = " ".join(systemd_arg(str(arg)) for arg in (
            env / "klipper/bin/python", Path(printer.klipper_path) / "klippy/klippy.py",
            data / "config/printer.cfg", "-l", data / "logs/klippy.log", "-a", data / "comms/klippy.sock"))
        moon = " ".join(systemd_arg(str(arg)) for arg in (
            env / "moonraker/bin/python", "-m", "moonraker", "-d", data,
            "-c", data / "config/moonraker.conf", "-l", data / "logs/moonraker.log"))
        header = "# Managed by Klipper Component Manager; standalone host setup\n"
        return {
            printer.service.removesuffix(".service") + ".service": header + f"""[Unit]
Description=Klipper for {printer.id}
After=network.target

[Service]
Type=simple
User={user}
SupplementaryGroups=dialout
ExecStart={command}
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
""",
            self._moonraker_service(printer) + ".service": header + f"""[Unit]
Description=Moonraker for {printer.id}
After=network.target

[Service]
Type=simple
User={user}
ExecStart={moon}
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
""",
        }

    def _new_unit(self, name: str):
        result = self.runner.run(["systemctl", "show", "--property=LoadState", "--value", name], check=False, quiet=True)
        state = (result.stdout or "").strip()
        if state != "not-found":
            raise UpdateError(f"Service {name} already exists or could not be checked ({state or 'unknown'}). Use the existing installation or choose a new service name.")

    def _local_address(self, printer: Printer) -> tuple[str, int]:
        address = urllib.parse.urlsplit(printer.moonraker_url)
        if address.hostname not in {"localhost", "127.0.0.1"} or address.scheme != "http" or address.path not in {"", "/"}:
            raise UpdateError("New-host setup needs an http://127.0.0.1:PORT Moonraker URL. Existing installations can use the normal updater.")
        if address.port is None or address.port < 1024:
            raise UpdateError("Include an explicit unprivileged Moonraker port, normally http://127.0.0.1:7125.")
        return "127.0.0.1", address.port

    def plan(self, printer: Printer) -> dict:
        printer.validate()
        host, port = self._local_address(printer)
        return {"printer": printer.id, "data_path": str(self.data_path(printer)),
                "klipper_source": printer.klipper_path,
                "moonraker_source": str(self._root(printer) / "moonraker"),
                "services": [printer.service, self._moonraker_service(printer)],
                "moonraker_bind": f"{host}:{port}",
                "actions": ["Install Python/build/image dependencies using apt-get",
                            "Create separate Klipper and Moonraker Python environments",
                            "Download Moonraker from its official source repository",
                            "Stage and install two new systemd service files",
                            "Write a Moonraker config and a printer.cfg example",
                            "Leave services stopped until the machine configuration is reviewed"]}

    def install(self, printer: Printer, confirm):
        self._require_linux(debian=True)
        printer.validate()
        manager = Manager(self.store, output=self.output, runner=self.runner)
        with manager._lock():
            source = Path(printer.klipper_path)
            requirements = source / "scripts/klippy-requirements.txt"
            if not requirements.is_file() or not (source / "klippy/klippy.py").is_file():
                raise UpdateError("Download the Klipper source in the host-preparation step first.")
            root = self._root(printer)
            marker = root / "install.json"
            if marker.exists():
                raise UpdateError("This host profile has an installation record. Review status/logs; existing services will not be overwritten. Complete or repair the recorded installation before retrying.")
            for owned_path in (root / "moonraker", root / "venvs/klipper", root / "venvs/moonraker",
                               self.data_path(printer) / "config/moonraker.conf",
                               self.data_path(printer) / "config/printer.cfg.example"):
                if owned_path.exists() or owned_path.is_symlink():
                    raise UpdateError(f"Setup destination {owned_path} already exists. It will not be overwritten; choose a new host profile or review the existing installation.")
            units = self._service_files(printer)
            for name in units:
                self._new_unit(name)
            host, port = self._local_address(printer)
            self.output(json.dumps(self.plan(printer), indent=2))
            if not confirm("Install this NEW Klipper/Moonraker host setup? Existing printer services will be preserved"):
                return
            root.mkdir(parents=True, exist_ok=True)
            journal = {"schema_version": 1, "state": "installing", "printer": printer.to_dict(),
                       "data_path": str(self.data_path(printer)), "services": list(units),
                       "created": time.time(), "completed_steps": []}
            atomic_json(marker, journal)
            previous_log = self.runner.log
            self.runner.log = root / "install.log"
            try:
                self.runner.run(["sudo", "-v"], interactive=True)
                self.runner.run(["sudo", "-n", "apt-get", "update"])
                self.runner.run(["sudo", "-n", "apt-get", "install", "--yes",
                                 "python3-venv", "python3-dev", "build-essential", "git", "libffi-dev",
                                 "libjpeg-dev", "zlib1g-dev", "libopenjp2-7", "libsodium-dev", "pkg-config"])
                journal["completed_steps"].append("dependencies")
                atomic_json(marker, journal)
                data = self.data_path(printer)
                for folder in ("config", "logs", "comms", "gcodes", "database"):
                    (data / folder).mkdir(parents=True, exist_ok=True)
                moon_source = root / "moonraker"
                if moon_source.exists():
                    raise UpdateError("Moonraker destination already exists; inspect it before continuing installation.")
                self.runner.run(["git", "clone", "--", "https://github.com/Arksine/moonraker.git", str(moon_source)])
                for env_name in ("klipper", "moonraker"):
                    env = root / "venvs" / env_name
                    if env.exists():
                        raise UpdateError(f"Python environment {env} already exists; it will not be overwritten.")
                    self.runner.run([sys.executable, "-m", "venv", str(env)])
                    python = str(env / "bin/python")
                    if env_name == "klipper":
                        self.runner.run([python, "-m", "pip", "install", "-r", str(requirements)])
                    else:
                        self.runner.run([python, "-m", "pip", "install", str(moon_source)])
                    journal["completed_steps"].append(env_name + "_environment")
                    atomic_json(marker, journal)
                moon_config = data / "config/moonraker.conf"
                text = f"""# Created by Klipper Component Manager. Local access and SSH tunnels.
[server]
host: {host}
port: {port}
klippy_uds_address: {data / 'comms/klippy.sock'}

[authorization]
trusted_clients:
  127.0.0.1
  ::1
cors_domains:
  http://localhost:*
  http://127.0.0.1:*

[file_manager]
enable_object_processing: True

[machine]
provider: none
validate_service: False
"""
                with moon_config.open("x", encoding="utf-8") as stream:
                    stream.write(text)
                example = data / "config/printer.cfg.example"
                with example.open("x", encoding="utf-8") as stream:
                    stream.write("# Copy a verified printer/board configuration here and review all pins.\n"
                                 "# This example deliberately has no motor/heater pins and cannot run a printer.\n"
                                 "# Add the exported MCU snippet after checking it against the wiring.\n\n"
                                 "[pause_resume]\n\n[display_status]\n\n[virtual_sdcard]\n"
                                 f"path: {data / 'gcodes'}\n")
                staging = root / "systemd"
                staging.mkdir(exist_ok=True)
                for name, content in units.items():
                    staged = staging / name
                    staged.write_text(content, encoding="utf-8")
                    self._new_unit(name)  # recheck immediately before publishing
                    destination = Path("/etc/systemd/system") / name
                    self.runner.run(["sudo", "-n", "install", "-m", "0644", str(staged), str(destination)])
                    journal["completed_steps"].append("unit:" + name)
                    atomic_json(marker, journal)
                self.runner.run(["sudo", "-n", "systemctl", "daemon-reload"])
                journal.update(state="installed_stopped", completed=time.time())
                atomic_json(marker, journal)
                self.output(f"Host installed with services stopped. Supply and review {data / 'config/printer.cfg'} before starting.")
            except BaseException as exc:
                journal.update(state="failed", error=str(exc) or type(exc).__name__)
                atomic_json(marker, journal)
                raise
            finally:
                self.runner.log = previous_log

    def guide(self, printer: Printer) -> list[str]:
        data = self.data_path(printer)
        return [
            "Host OS / SSH: install a supported Debian/Ubuntu-based Linux image, enable SSH, then copy and run the standalone program.",
            "https://www.klipper3d.org/Installation.html",
            f"For a new host managed here, printer configuration belongs at {data / 'config/printer.cfg'}.",
            "Start from the exact printer manufacturer's configuration or Klipper's matching printer-/generic- file; review every motor, heater, sensor and endstop pin.",
            "https://github.com/Klipper3d/klipper/tree/master/config",
            "https://www.klipper3d.org/Config_checks.html",
            "A board firmware profile configures its MCU; a complete printer.cfg also needs machine geometry, wiring and limits. The program does not invent those values.",
            "For CAN: match the bus bitrate to Katapult/Klipper, fit two end terminators, and configure automatic interface restoration using your Linux network manager.",
            "https://www.klipper3d.org/CANBUS.html",
            "Optional browser interface: install Mainsail using its official manual setup guide. New Moonraker instances here listen on loopback; use SSH forwarding or deliberately configure LAN access and authorization using the official docs.",
            "https://github.com/mainsail-crew/gb-docs/blob/main/setup/getting-started/manual-setup.md",
            "https://moonraker.readthedocs.io/en/latest/configuration/#authorization",
        ]

    def status(self, printer: Printer) -> dict:
        result = {"ready": False, "klipper_state": "unreachable", "state_message": "",
                  "moonraker": False, "data_path": str(self.data_path(printer)),
                  "config_file": str(self.data_path(printer) / "config/printer.cfg"),
                  "managed": (self._root(printer) / "install.json").is_file()}
        try:
            manager = Manager(self.store, output=self.output, runner=self.runner)
            info = manager._moonraker(printer, "/printer/info")
            result.update(moonraker=True, klipper_state=info.get("state", "unknown"),
                          state_message=info.get("state_message", ""),
                          ready=False)
            result["config_file"] = info.get("config_file", info.get("configfile", result["config_file"]))
            if info.get("state") == "ready":
                manager._verify_service(printer)
                result["ready"] = True
        except (OSError, ValueError, RuntimeError) as exc:
            result["state_message"] = str(exc)
        return result

    def start(self, printer: Printer, confirm) -> dict:
        self._require_linux()
        printer.validate()
        marker = self._root(printer) / "install.json"
        if not marker.is_file():
            raise UpdateError("This program does not own the service setup. Use your existing service controls and the status check.")
        record = json.loads(marker.read_text(encoding="utf-8"))
        infrastructure = ("id", "klipper_path", "service", "moonraker_url")
        old_profile = record.get("printer", {})
        if record.get("state") not in {"installed_stopped", "started", "start_failed"} or any(old_profile.get(key) != printer.to_dict()[key] for key in infrastructure):
            raise UpdateError("Host installation is incomplete or its profile changed. Review the installation record first.")
        config = self.data_path(printer) / "config/printer.cfg"
        if not config.is_file() or not config.stat().st_size:
            raise UpdateError(f"A complete printer configuration is required at {config}.")
        if not confirm(f"Have you reviewed the complete {config}, checked wiring/pins and made the printer ready for its FIRST start?"):
            return self.status(printer)
        with Manager(self.store, output=self.output, runner=self.runner)._lock():
            self.runner.run(["sudo", "-v"], interactive=True)
            started = []
            try:
                for service in (printer.service, self._moonraker_service(printer)):
                    self.runner.run(["sudo", "-n", "systemctl", "enable", service])
                    self.runner.run(["sudo", "-n", "systemctl", "start", service])
                    started.append(service)
                record.update(state="started", started=time.time())
                atomic_json(marker, record)
            except BaseException as exc:
                record.update(state="start_failed", error=str(exc) or type(exc).__name__, started_services=started)
                atomic_json(marker, record)
                raise
        result = self.status(printer)
        self.output("Services started. Follow the temperature, endstop, motor and heater checks before printing.")
        return result

    def configure_can(self, printer: Printer, interface: str, bitrate: int, confirm):
        self._require_linux()
        printer.validate()
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,14}", interface):
            raise UpdateError("Enter a valid CAN interface name, such as can0.")
        if type(bitrate) is not int or bitrate not in {100000, 125000, 250000, 500000, 1000000}:
            raise UpdateError("Choose the bitrate documented for your CAN setup.")
        kind = Path("/sys/class/net") / interface / "type"
        if not kind.is_file() or kind.read_text().strip() != "280":
            raise UpdateError(f"{interface} is not an available CAN network device. Connect/configure the host CAN adapter first.")
        manager = Manager(self.store, output=self.output, runner=self.runner)
        with manager._lock():
            printers = {p.id: p for p in [*self.store.load(), printer]}
            for other in printers.values():
                if other.id == printer.id or any(c.can_interface == interface and (c.transport == "can" or c.can_uuid) for c in other.components):
                    state = self.runner.run(["systemctl", "is-active", other.service], check=False, quiet=True)
                    if (state.stdout or "").strip() not in {"inactive", "failed", "unknown"}:
                        raise UpdateError(f"Stop {other.service} before changing this CAN interface.")
            if not confirm(f"Configure {interface} at {bitrate} bit/s now? Confirm no other printer uses this bus; this resets the link"):
                return
            self.runner.run(["sudo", "-v"], interactive=True)
            self.runner.run(["sudo", "-n", "ip", "link", "set", interface, "down"])
            self.runner.run(["sudo", "-n", "ip", "link", "set", interface, "type", "can", "bitrate", str(bitrate)])
            self.runner.run(["sudo", "-n", "ip", "link", "set", interface, "txqueuelen", "128"])
            self.runner.run(["sudo", "-n", "ip", "link", "set", interface, "up"])
            self.output("CAN link configured for this session. Configure persistence/restoration using your Linux network manager: https://www.klipper3d.org/CANBUS.html")
