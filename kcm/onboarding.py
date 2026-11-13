"""Resumable first-time setup, board evidence, and hardware handoffs."""

from __future__ import annotations

from importlib import resources
import json
from pathlib import Path
import subprocess
import time
from urllib.parse import urlsplit

from .engine import UpdateError
from .models import Component, Printer, validate_id
from .storage import Store, atomic_json


KLIPPER_INSTALL = "https://www.klipper3d.org/Installation.html"
KLIPPER_CHECKS = "https://www.klipper3d.org/Config_checks.html"
CAN_GUIDE = "https://www.klipper3d.org/CANBUS.html"
KATAPULT_GUIDE = "https://github.com/Arksine/katapult"


def board_catalog(query: str = "") -> list[dict]:
    """Read bundled references, including from a downloadable zip application."""
    content = resources.files("kcm").joinpath("data/boards.json").read_text(encoding="utf-8")
    data = json.loads(content)
    boards = data["boards"] if isinstance(data, dict) else data
    if not isinstance(boards, list):
        raise ValueError("Bundled board catalog is invalid")
    words = query.casefold().split()
    return [board for board in boards if all(word in " ".join(
        str(board.get(field, "")) for field in ("id", "name", "manufacturer", "revision", "mcu", "family")
    ).casefold() for word in words)]


def show_boards(query: str = "") -> None:
    boards = board_catalog(query)
    if not boards:
        print("No matching bundled reference. First-time setup also accepts a board's official manual.")
    for board in boards:
        print(f"\n{board['name']} — {board.get('revision', 'confirm revision')} — {board.get('mcu', 'confirm chip marking')}")
        print("  " + board["documentation_url"])
        for url in board.get("source_urls", []):
            if url != board["documentation_url"]:
                print("  " + url)
        notes = board.get("notes", [])
        for note in notes if isinstance(notes, list) else [notes]:
            print("  " + note)
    print("\nCompare the physical board revision and chip marking with its official documentation.")


def _evidence_url(value: str) -> str:
    if not isinstance(value, str):
        raise ValueError("Documentation must be an HTTP(S) link")
    try:
        url = urlsplit(value)
        valid = url.scheme in {"https", "http"} and url.hostname and not url.username and not url.password
    except ValueError:
        valid = False
    if not valid or any(ord(char) < 32 for char in value):
        raise ValueError("Documentation must be an HTTP(S) link without credentials")
    return value


def _confirm_required(message: str) -> bool:
    """Abort on No even if a system adapter normally returns None on decline."""
    from . import cli as ui
    if not ui._yes(message):
        raise UpdateError("Setup cancelled at confirmation. Check the action log before resuming any partial setup.")
    return True


def _show_inspection(inspected: dict) -> None:
    release = inspected.get("os_release", {})
    print(f"\nHost: {release.get('PRETTY_NAME', inspected.get('platform', 'unknown'))}")
    print("Python: " + str(inspected.get("python", "unknown")))
    print("Build and flashing tools:")
    for name, location in inspected.get("tools", {}).items():
        print(f"  {name}: {'available' if location else 'missing'}")
    print("USB/serial device paths:")
    for device in inspected.get("serial_devices", []):
        print("  " + str(device))
    if not inspected.get("serial_devices"):
        print("  None currently visible. A board may appear only in its bootloader mode.")
    print("CAN interfaces:")
    for interface in inspected.get("can_interfaces", []):
        print(f"  {interface['name']}: {interface.get('state', 'unknown')}")
    if not inspected.get("can_interfaces"):
        print("  None currently visible. Check the adapter and operating system network setup.")
    for mount in inspected.get("uf2_mounts", []):
        print("RP boot volume: " + str(mount["path"]))
        print(str(mount.get("info", "")).strip())


class Guide:
    """Save individual observations/actions; viewing never means completion."""

    def __init__(self, store: Store, printer: Printer):
        validate_id(printer.id, "Printer ID")
        self.store = store
        self.printer_id = printer.id
        self.path = store.root / "setup" / printer.id / "guide.json"
        if self.path.exists():
            self.data = json.loads(self.path.read_text(encoding="utf-8"))
            if not isinstance(self.data, dict) or type(self.data.get("schema_version")) is not int or self.data.get("schema_version") != 1 or self.data.get("printer_id") != printer.id:
                raise ValueError("Setup checklist has an unsupported schema or printer identity")
            for field in ("steps", "boards", "user_checks"):
                if not isinstance(self.data.get(field), dict):
                    raise ValueError("Setup checklist is missing its records")
        else:
            self.data = {"schema_version": 1, "printer_id": printer.id, "steps": {}, "boards": {}, "user_checks": {}}

    def save(self) -> None:
        atomic_json(self.path, self.data)

    def record(self, stage: str, status: str, details=None) -> None:
        self.data["steps"][stage] = {"status": status, "updated": time.time(), "details": details}
        self.save()

    def set_board(self, identity: dict) -> None:
        validate_id(identity.get("id", ""), "Component ID")
        for key in ("name", "model", "revision", "mcu"):
            if not isinstance(identity.get(key), str) or not identity[key].strip():
                raise ValueError(f"Board identification requires {key}")
        _evidence_url(identity.get("documentation_url", ""))
        for url in identity.get("source_urls", []):
            _evidence_url(url)
        self.data["boards"][identity["id"]] = identity
        self.record("identity/" + identity["id"], "confirmed" if identity.get("identity_confirmed") is True else "needs_identification", identity)

    def checklist(self) -> list[str]:
        if not self.data["steps"]:
            return ["No setup actions recorded. Select a stage to begin."]
        return [f"{key}: {record['status']}" for key, record in sorted(self.data["steps"].items())]

    def perform(self, stage: str, action, status: str = "completed"):
        try:
            result = action()
            if result is False:
                self.record(stage, "cancelled")
                return result
            details = str(result) if isinstance(result, Path) else result
            self.record(stage, status, details)
            return result
        except (ValueError, RuntimeError, OSError, subprocess.SubprocessError) as exc:
            self.record(stage, "cancelled" if "cancel" in str(exc).lower() else "failed", str(exc))
            raise


def mcu_snippet(printer: Printer) -> str:
    """Connection references only: no motion, heater, or pin assumptions."""
    printer.validate()
    lines = ["# Connection references only. Merge into a reviewed printer configuration.",
             "# This file does not define motors, heaters, endstops, or safe pin settings."]
    for component in printer.components:
        section = "mcu" if component.id == "main" else "mcu " + component.id
        lines += ["", f"# {component.name}", f"[{section}]"]
        if component.can_uuid:
            lines += [f"canbus_uuid: {component.can_uuid}", f"canbus_interface: {component.can_interface}"]
        elif component.application_device:
            lines += [f"serial: {component.application_device}"]
            if component.transport == "uart":
                lines += [f"baud: {component.application_baud}"]
        else:
            lines += ["# Running Klipper serial path is missing. Identify it before using this section."]
    return "\n".join(lines) + "\n"


def _host_intro(bootstrap, guide: Guide) -> None:
    from . import cli as ui
    print("\n1. Install a supported Debian/Raspberry Pi OS image and connect the host to your network.")
    print("2. Enable SSH using your OS image's setup, then SSH in as your normal user.")
    print("3. Run this app on that host. Keep a copy of any working printer configuration.")
    print("A USB or CAN identifier does not establish the physical board model or revision.")
    print(KLIPPER_INSTALL)
    inspected = bootstrap.inspect_host()
    _show_inspection(inspected)
    guide.record("host_inspection", "observed", inspected)
    if ui._yes("Have you completed the Linux/network/SSH steps and saved any existing printer configuration?"):
        guide.data["user_checks"]["host_prerequisites"] = True
        guide.record("host_prerequisites", "user_confirmed")


def _identify(guide: Guide) -> None:
    from . import cli as ui
    query = ui._ask("Search board name/manufacturer (blank lists references)", required=False)
    boards = board_catalog(query)
    selected = ui._choice("Board references", [f"{b['name']} ({b.get('revision', '')}, {b.get('mcu', '')})" for b in boards] + ["Other board: use its official documentation"])
    if not selected:
        return
    board = boards[selected - 1] if selected <= len(boards) else {}
    if board:
        print("\nOfficial reference: " + board["documentation_url"])
        for source in board.get("source_urls", []):
            print(source)
        notes = board.get("notes", [])
        for note in notes if isinstance(notes, list) else [notes]:
            print(note)
        if board.get("settings"):
            print("Reference settings — verify against the exact revision before using:")
            print(json.dumps(board["settings"], indent=2))
    else:
        print("Read the model/revision printed on the PCB and the MCU marking on its chip.")
        print("Use the manufacturer's manual or Klipper's board configuration header.")
        print("Custom boards get a manual initial-bootloader handoff; build settings stay under your control.")
    component_id = ui._ask("Component ID (for example main, toolhead, or probe)")
    validate_id(component_id, "Component ID")
    if component_id in guide.data["boards"] and not ui._yes("Replace this board's recorded identification?"):
        return
    model = ui._ask("Board model", board.get("name", ""))
    manufacturer = ui._ask("Manufacturer", board.get("manufacturer", ""))
    revision = ui._ask("Physical PCB revision", board.get("revision", ""))
    mcu = ui._ask("MCU chip marking", board.get("mcu", ""))
    url = _evidence_url(ui._ask("Official documentation URL", board.get("documentation_url", "")))
    transports = board.get("transports", ["can", "usb", "uart"])
    print("Connections covered by this reference: " + ", ".join(transports))
    default_transport = "can" if "can" in transports else transports[0]
    preferred_transport = ui._ask("Planned Katapult connection: can, usb, or uart", default_transport).lower()
    if preferred_transport not in {"can", "usb", "uart"}:
        raise ValueError("Planned connection must be can, usb, or uart")
    if board and preferred_transport not in transports:
        raise ValueError("This reference does not document that connection. Select a supported connection or use the custom/manual board route.")
    confirmed = ui._yes("Have you compared this PCB revision AND MCU chip marking with the documentation?")
    guide.set_board({"id": component_id, "name": ui._ask("Component display name", model), "catalog_id": board.get("id", "custom"),
                     "model": model, "manufacturer": manufacturer, "revision": revision, "mcu": mcu,
                     "family": board.get("family", ""), "documentation_url": url,
                     "source_urls": board.get("source_urls", []), "initial_methods": board.get("initial_methods", []),
                     "transports": transports, "reference_settings": board.get("settings", {}),
                     "preferred_transport": preferred_transport, "identity_confirmed": confirmed})
    print("Board evidence saved. USB discovery is an aid; the physical identification remains your check.")


def _select_board(guide: Guide) -> dict | None:
    from . import cli as ui
    boards = list(guide.data["boards"].values())
    if not boards:
        print("Identify a board first. This saves its revision, MCU marking, and documentation.")
        return None
    selected = ui._choice("Choose physical component", [f"{b['id']}: {b['name']} ({b['revision']}, {b['mcu']})" for b in boards])
    return boards[selected - 1] if selected else None


def _require_identity(board: dict) -> None:
    if board.get("identity_confirmed") is not True or not board.get("revision") or not board.get("mcu"):
        raise ValueError("Confirm the physical PCB revision and MCU marking in board identification before configuring or flashing it")


def _draft_component(store: Store, printer: Printer, board: dict) -> Component:
    for component in printer.components:
        if component.id == board["id"]:
            return component
    # A fresh CAN board has no discoverable Katapult UUID yet. Drafts stay in
    # the setup checklist until real connection identifiers can be entered.
    transport = board.get("preferred_transport", "can")
    return Component(id=board["id"], name=board["name"], config_file=str(store.config_path(printer.id, board["id"])),
                     transport=transport, can_uuid=board.get("detected_can_uuid", ""),
                     bootloader_entry="auto" if transport == "can" else "manual")


def _build_hints(store: Store, printer: Printer, board: dict) -> None:
    print(f"Board: {board['model']} / {board['revision']} / {board['mcu']}")
    settings = board.get("reference_settings", {})
    if not settings and board.get("catalog_id") != "custom":
        match = next((entry for entry in board_catalog() if entry['id'] == board.get('catalog_id')), {})
        settings = match.get("settings", {})
    for label, value in settings.items():
        print(f"  {label.replace('_', ' ')}: {value}")
    boot = store.config_path(printer.id, board['id']).with_suffix('.katapult.config')
    if boot.is_file():
        from .engine import kconfig_values
        config = kconfig_values(boot.read_text(encoding='utf-8'))
        launch = config.get('CONFIG_LAUNCH_APP_ADDRESS')
        if launch:
            print(f"Saved Katapult application address: {launch}. Klipper must use the same address.")
            try:
                offset = int(launch, 0) - int(config['CONFIG_FLASH_START'], 0)
                if offset > 0 and offset % 1024 == 0:
                    print(f"That corresponds to a {offset // 1024} KiB Klipper bootloader offset for this saved configuration.")
            except (ValueError, KeyError):
                pass
    print(board['documentation_url'])


def _saved_job(root: Path, title: str) -> Path | None:
    from . import cli as ui
    jobs = []
    for path in root.glob("*/manifest.json"):
        try:
            if json.loads(path.read_text(encoding='utf-8')).get('state') == 'ready':
                jobs.append(path.parent)
        except (OSError, ValueError, AttributeError):
            continue
    jobs.sort(reverse=True)
    if not jobs:
        print("No saved build here. Configure and build first.")
        return None
    selected = ui._choice(title, [str(path) for path in jobs])
    return jobs[selected - 1] if selected else None


def _katapult(store: Store, printer: Printer, bootstrap, guide: Guide) -> None:
    from . import cli as ui
    board = _select_board(guide)
    if board is None:
        return
    _require_identity(board)
    component = _draft_component(store, printer, board)
    _build_hints(store, printer, board)
    print("\nKatapult is the bootloader that accepts future firmware uploads. Klipper is the printer firmware.")
    print("Initial Katapult installation uses the board's original flashing method. Read its exact manual first.")
    print("Katapult application offset and the later Klipper bootloader offset must match.")
    print(board["documentation_url"])
    print(KATAPULT_GUIDE)
    action = ui._choice("Katapult setup", ["Configure Katapult for this board", "Build Katapult image", "Install a saved Katapult image", "Record an existing/manual Katapult installation", "Find a newly installed CAN board's UUID (one isolated board)", "List connected STM32 DFU device serial numbers"])
    key = "katapult/" + component.id
    if action == 1:
        imported = ui._path("Saved Katapult .config") if ui._yes("Import an existing Katapult configuration?") else None
        guide.perform(key + "/configure", lambda: bootstrap.configure_katapult(printer, component, import_path=imported), "configured")
    elif action == 2:
        job = guide.perform(key + "/build", lambda: bootstrap.build_katapult(printer, component), "built")
        print(f"Saved bootloader build: {job}")
    elif action == 3:
        job = _saved_job(store.root / "bootstrap" / printer.id / component.id, "Select Katapult build")
        if job is None:
            return
        print("Select only the initial method documented for this exact board. This replaces its bootloader.")
        methods = ["dfu", "uf2", "sdcard", "swd", "uart"]
        selected = ui._choice("Initial method", ["STM32 USB DFU (exact USB serial)", "RP2040/RP2350 boot volume (UF2)", "SD card: manual board instructions", "SWD programmer: manual board instructions", "UART ROM loader: manual board instructions"])
        if not selected:
            return
        method = methods[selected - 1]
        if method in {"sdcard", "swd", "uart"} or board.get("catalog_id") == "custom" or method not in board.get("initial_methods", []):
            print("Use the saved build with the board manufacturer's documented initial installation procedure.")
            print("This setup path leaves the physical bootloader installation to you.")
            print(board["documentation_url"])
            print(f"Saved build directory: {job}")
            guide.record(key + "/install", "manual_action_required", {"method": method, "job": str(job)})
            return
        target = ui._ask("Exact DFU device serial number") if method == "dfu" else ui._path("Mounted RP boot volume containing INFO_UF2.TXT")
        guide.perform(key + "/install", lambda: bootstrap.install_katapult(printer, component, job, method, target, confirm=_confirm_required), "installed_unverified")
        print("Installation action finished. Confirm the board enters Katapult before proceeding.")
    elif action == 4:
        if ui._yes("Have you installed Katapult on this exact board and verified its bootloader connection/settings?"):
            guide.record(key + "/install", "user_verified_existing", {"documentation": board["documentation_url"]})
            print("Use Configure Katapult to import/review the exact installed bootloader settings.")
            print("The first Klipper flash checks that saved Katapult and Klipper MCU/application offsets match.")
    elif action == 5:
        interface = ui._ask("CAN interface", component.can_interface)
        uuids = guide.perform(key + "/discovery", lambda: bootstrap.find_can_uuid(printer, interface, confirm=_confirm_required), "observed")
        print("Discovered UUIDs: " + ", ".join(uuids))
        if len(uuids) == 1 and ui._yes("Is this the only powered CAN board you physically isolated for this setup?"):
            board["detected_can_uuid"] = uuids[0]
            guide.set_board(board)
            print("Saved UUID for the connection-profile step.")
    elif action == 6:
        print(bootstrap.list_dfu())
        print('For a DFU install, use the serial="..." value for your physically selected board. Never choose a VID:PID as a unique board identity.')


def _connection_profile(store: Store, printer: Printer, manager, guide: Guide) -> None:
    from . import cli as ui
    board = _select_board(guide)
    if board is None:
        return
    _require_identity(board)
    _build_hints(store, printer, board)
    if board.get("preferred_transport") in {"usb", "uart"} and not any(c.id == board["id"] for c in printer.components):
        print("For a fresh serial board, use manual bootloader entry and its current Katapult device path.")
        print("After the first Klipper flash, inspect the new Klipper device path and update this connection profile.")
        print("Automatic entry can then use the running Klipper path while flashing uses the Katapult path.")
    component = ui._component_settings(store, printer, _draft_component(store, printer, board))
    if board.get("catalog_id") != "custom" and board.get("transports") and component.transport not in board["transports"]:
        raise ValueError("That connection is not covered by the saved board reference. Correct the connection or identify the board through the custom/manual route.")
    def save_profile():
        ui._configure(manager, printer, component)
        printer.components = [entry for entry in printer.components if entry.id != component.id] + [component]
        ui._save_printer(store, printer)
        return {"config_file": component.config_file, "transport": component.transport}
    guide.perform("klipper/" + component.id + "/profile", save_profile, "configured")
    print("Saved connection identity and Klipper build settings. Future updates reuse them.")


def _initial_flash(store: Store, printer: Printer, manager, bootstrap, guide: Guide) -> None:
    from . import cli as ui
    print("\nFor initial installation, build from the current host source so host and MCU versions agree.")
    print("Have the required USB/CAN adapter running before flashing boards reached through it.")
    print("For a completely new USB-to-CAN bridge, flash the bridge over USB first, then configure the CAN link and downstream boards.")
    print("Once the bridge runs Klipper, use the CAN guide to find its application UUID and finish its saved profile: " + CAN_GUIDE)
    print("Routine Update later handles the complete dependency order.")
    selected = ui._choice("First Klipper firmware", ["Build all configured components", "Flash one component from a saved build"])
    if selected == 1:
        job = guide.perform("klipper/build", lambda: manager.build(printer, refresh=False), "built")
        print(f"Saved firmware build: {job}")
    elif selected == 2:
        if not printer.components:
            raise ValueError("Save at least one component connection/build profile first")
        selected_component = ui._choice("Choose component", [c.id + ": " + c.name for c in printer.ordered_components()])
        if not selected_component:
            return
        component = printer.ordered_components()[selected_component - 1]
        board = guide.data["boards"].get(component.id)
        if board is None:
            raise ValueError("Record and confirm this component's physical board identity first")
        _require_identity(board)
        job = _saved_job(store.root / "jobs" / printer.id, "Select checksummed Klipper build")
        if job is None:
            return
        guide.perform("klipper/" + component.id + "/initial_flash", lambda: bootstrap.first_klipper_flash(printer, component, job, confirm=_confirm_required), "flashed_unverified")


def _export_snippet(store: Store, printer: Printer, host, guide: Guide) -> None:
    if not printer.components:
        raise ValueError("Create component connection profiles before exporting references")
    destination = store.root / "setup" / printer.id / "mcu-connections.cfg"
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(mcu_snippet(printer), encoding="utf-8")
    print(destination.read_text(encoding="utf-8"))
    print(f"Saved connection references: {destination}")
    print("Use your printer manufacturer's full Klipper configuration as the starting point.")
    print("Review pins, heaters, sensors, endstops, and movement limits before starting the printer.")
    for line in host.guide(printer):
        print(line)
    guide.record("printer_configuration", "references_exported", str(destination))


def _startup(printer: Printer, host, guide: Guide) -> None:
    from . import cli as ui
    selected = ui._choice("Startup and physical checks", ["Show service/configuration instructions", "Start configured services", "Check current startup status", "Record manual printer configuration checks"])
    if selected == 1:
        for line in host.guide(printer):
            print(line)
        guide.record("startup_instructions", "viewed")
    elif selected == 2:
        guide.perform("host_services/start", lambda: host.start(printer, confirm=_confirm_required), "start_requested")
    elif selected == 3:
        status = host.status(printer)
        print("Moonraker: " + ("reachable" if status.get("moonraker") else "unreachable"))
        print("Klipper state: " + str(status.get("klipper_state", "unknown")))
        if status.get("state_message"):
            print(str(status["state_message"]))
        if status.get("config_file"):
            print("Printer configuration: " + str(status["config_file"]))
        guide.record("startup_status", "ready_observed" if status.get("ready") is True else "needs_attention", status)
        print("A ready connection does not establish correct heater, endstop, or movement wiring.")
        print(KLIPPER_CHECKS)
    elif selected == 4:
        print("Follow Klipper's configuration checks on the physical printer:")
        print(KLIPPER_CHECKS)
        print("This program will not move axes, home, or energize heaters as a test.")
        if ui._yes("Have you personally completed the applicable official temperature, endstop, motor, and heater checks?"):
            guide.data["user_checks"]["printer_configuration_checks"] = True
            guide.record("physical_configuration_checks", "user_verified", KLIPPER_CHECKS)


def _choose_printer(store: Store) -> Printer | None:
    from . import cli as ui
    printers = store.load()
    selected = ui._choice("First-time setup: select printer", [f"{p.name} ({p.id})" for p in printers] + ["Create a printer setup profile"])
    if not selected:
        return None
    if selected <= len(printers):
        return printers[selected - 1]
    printer = ui._printer_settings()
    if any(saved.id == printer.id for saved in printers):
        raise ValueError("That printer ID already exists. Select its saved setup profile.")
    ui._save_printer(store, printer)
    return printer


def run_setup(store: Store, manager, printer: Printer | None = None, *, bootstrap=None, host=None) -> None:
    """Open independent numbered stages, retaining progress across SSH sessions."""
    from . import cli as ui
    if bootstrap is None:
        from .bootstrap import Bootstrap
        bootstrap = Bootstrap(store, output=print)
    if host is None:
        from .host_setup import HostSetup
        host = HostSetup(store, output=print)
    printer = printer or _choose_printer(store)
    if printer is None:
        return
    guide = Guide(store, printer)
    print("\nFirst-time setup: complete the applicable stages, then use Build latest and flash for later updates.")
    print("Each board needs exact physical identification and its own saved build settings.")
    while True:
        printer = store.get(printer.id)
        selected = ui._choice(f"Setup guide — {printer.name}", [
            "Linux host, SSH, and existing configuration", "Identify a board using official references",
            "Install dependencies and prepare source checkouts", "Katapult bootloader setup for a board",
            "Save a board connection and Klipper build settings", "Build / first-flash Klipper firmware",
            "Export MCU connection references and configuration instructions", "Install Klipper/Moonraker host services",
            "Startup status and manual physical checks", "Show saved setup checklist",
        ])
        if not selected:
            return
        try:
            if selected == 1:
                _host_intro(bootstrap, guide)
            elif selected == 2:
                _identify(guide)
            elif selected == 3:
                action = ui._choice("Host preparation", ["Install required Linux packages", "Prepare Klipper and Katapult source checkouts", "Inspect host without changes", "Configure a CAN interface for this session"])
                if action == 1:
                    guide.perform("dependencies", lambda: bootstrap.install_dependencies(confirm=_confirm_required), "installed")
                elif action == 2:
                    guide.perform("sources", lambda: bootstrap.prepare_sources(printer.klipper_path, printer.katapult_path, confirm=_confirm_required), "prepared")
                elif action == 3:
                    inspected = bootstrap.inspect_host()
                    _show_inspection(inspected)
                    guide.record("host_inspection", "observed", inspected)
                elif action == 4:
                    interface = ui._ask("CAN interface", "can0")
                    bitrate = int(ui._ask("CAN bitrate matching the boards' saved firmware", "1000000"))
                    guide.perform("can_runtime/" + interface, lambda: host.configure_can(printer, interface, bitrate, confirm=_confirm_required), "runtime_configured")
                    print("This configures the current session. Configure your OS network backend for persistence:")
                    print(CAN_GUIDE)
            elif selected == 4:
                _katapult(store, printer, bootstrap, guide)
            elif selected == 5:
                _connection_profile(store, printer, manager, guide)
            elif selected == 6:
                _initial_flash(store, printer, manager, bootstrap, guide)
            elif selected == 7:
                _export_snippet(store, printer, host, guide)
            elif selected == 8:
                print("For an existing working installation, preserve its services and use startup status instead.")
                guide.perform("host_services/install", lambda: host.install(printer, confirm=_confirm_required), "installed_not_started")
            elif selected == 9:
                _startup(printer, host, guide)
            elif selected == 10:
                print("\n".join(guide.checklist()))
                print(f"Saved checklist: {guide.path}")
                print("Viewed instructions and command success do not certify a fully checked printer.")
        except (ValueError, RuntimeError, OSError, KeyError, subprocess.SubprocessError) as exc:
            print(f"\nSetup paused at this stage: {exc}")
