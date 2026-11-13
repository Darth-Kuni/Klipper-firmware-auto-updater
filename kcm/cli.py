"""An SSH-friendly walkthrough for saved Klipper component profiles."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import subprocess
import sys

from .engine import Manager
from .models import Component, Printer
from .storage import Store
from . import __version__

VERSION = __version__


def _ask(label: str, default: str = "", *, required: bool = True) -> str:
    while True:
        suffix = f" [{default}]" if default else ""
        if not required and default:
            suffix += " (- clears)"
        value = input(f"{label}{suffix}: ").strip() or default
        if not required and value == "-":
            return ""
        if value or not required:
            return value
        print("Please enter a value.")


def _yes(label: str) -> bool:
    return input(f"{label} [y/N]: ").strip().lower() in {"y", "yes"}


def _choice(title: str, options: list[str], *, back: str = "Back") -> int:
    print(f"\n{title}")
    for number, option in enumerate(options, 1):
        print(f"  {number}. {option}")
    print(f"  0. {back}")
    while True:
        value = input("Choose: ").strip()
        if value.isdigit() and 0 <= int(value) <= len(options):
            return int(value)
        print("Choose one of the numbers shown.")


def _path(label: str, default: str = "") -> str:
    return str(Path(_ask(label, default)).expanduser().resolve())


def _save_printer(store: Store, printer: Printer) -> None:
    printer.validate()
    printers = store.load()
    printers = [entry for entry in printers if entry.id != printer.id]
    printers.append(printer)
    store.save(printers)


def _find_component(printer: Printer, component_id: str) -> Component:
    for component in printer.components:
        if component.id == component_id:
            return component
    raise ValueError(f"No component named {component_id!r} in {printer.id!r}.")


def _printer_settings(existing: Printer | None = None) -> Printer:
    print("\nPrinter setup — paths refer to this Linux host.")
    printer_id = existing.id if existing else _ask("Printer ID (letters, digits, - or _)")
    printer = Printer(
        id=printer_id,
        name=_ask("Display name", existing.name if existing else printer_id),
        klipper_path=_path("Klipper checkout", existing.klipper_path if existing else "~/klipper"),
        katapult_path=_path("Katapult checkout", existing.katapult_path if existing else "~/katapult"),
        service=_ask("Klipper systemd service", existing.service if existing else "klipper"),
        moonraker_url=_ask(
            "Moonraker URL", existing.moonraker_url if existing else "http://127.0.0.1:7125"
        ),
        source_ref=_ask("Update source ref", existing.source_ref if existing else "origin/master"),
        components=list(existing.components) if existing else [],
    )
    printer.validate()
    return printer


def _component_settings(store: Store, printer: Printer, existing: Component | None = None) -> Component:
    print("\nComponent setup")
    print("This saves the board's Klipper settings and its Katapult connection.")
    print("For a new board, the First-time setup guide covers Katapult installation first.")
    component_id = existing.id if existing else _ask("Component ID (letters, digits, - or _)")
    if existing is None and any(item.id == component_id for item in printer.components):
        raise ValueError(f"Component ID {component_id!r} already exists.")
    name = _ask("Display name", existing.name if existing else component_id)
    transport = _ask("Katapult flashing connection: can, usb, or uart", existing.transport if existing else "can").lower()
    if transport not in {"can", "usb", "uart"}:
        raise ValueError("Flashing connection must be can, usb, or uart.")
    entry = _ask("Bootloader entry: auto, manual, or already", existing.bootloader_entry if existing else "auto").lower()
    if entry in {"manual", "already"}:
        print("The update will stop Klipper, then ask you to confirm the board is in bootloader mode.")
    can_interface, can_uuid, app_device, flash_device = "can0", "", "", ""
    baud = existing.baud if existing else 250000
    application_baud = existing.application_baud if existing else 250000
    if transport == "can":
        can_interface = _ask("CAN interface", existing.can_interface if existing else "can0")
        can_uuid = _ask("Board CAN UUID (12 hex digits)", existing.can_uuid if existing else "")
    else:
        print("Use the device path for Katapult mode; it may differ from the running Klipper device.")
        flash_device = _ask("Katapult serial device", existing.flash_device if existing else "")
        if transport == "uart":
            baud = int(_ask("Katapult UART baud rate", str(baud)))
            application_baud = int(_ask("Running Klipper UART baud rate", str(application_baud)))
        if entry == "auto":
            bridge = _yes("Is the running board reached over CAN (a USB-to-CAN bridge)?")
            if bridge:
                can_interface = _ask("Application CAN interface", existing.can_interface if existing else "can0")
                can_uuid = _ask("Application CAN UUID", existing.can_uuid if existing else "")
            else:
                app_device = _ask("Running Klipper serial device", existing.application_device if existing else "")
        else:
            app_device = _ask("Running serial device (optional)", existing.application_device if existing else "", required=False)
    print("If this board is behind a bridge managed here, enter the bridge's component ID.")
    print("Add the bridge component first, then select its ID when adding downstream boards.")
    print("Downstream components will be flashed before their bridge.")
    via = _ask("Bridge component ID (optional)", existing.via if existing else "", required=False)
    component = Component(
        id=component_id, name=name, config_file=str(store.config_path(printer.id, component_id)),
        transport=transport, can_interface=can_interface, can_uuid=can_uuid,
        application_device=app_device, flash_device=flash_device, baud=baud, application_baud=application_baud,
        bootloader_entry=entry, via=via,
    )
    component.validate()
    candidate = Printer.from_dict(printer.to_dict())
    candidate.components = [item for item in candidate.components if item.id != component.id] + [component]
    candidate.validate()
    return component


def _configure(manager: Manager, printer: Printer, component: Component) -> None:
    print("\nSave the exact firmware build settings for this physical board and revision.")
    print("Check the processor, clock, communication pins/speed, and Katapult application offset.")
    print("If a build reported changed Kconfig settings, review against the newest configured source.")
    refresh = _yes("Fetch and configure the newest source version?")
    action = _choice("Build configuration", ["Import an existing Klipper .config", "Open Klipper make menuconfig"], back="Cancel")
    if action == 0:
        raise ValueError("Configuration cancelled; the component was not saved.")
    import_path = _path("Existing .config path") if action == 1 else None
    manager.configure(printer, component, import_path=import_path, refresh=refresh)


def _show_plan(manager: Manager, printer: Printer) -> None:
    print(json.dumps(manager.plan(printer), indent=2))


def _moonraker_setup(store: Store, *, app_dir: Path | None = None,
                     config: Path | None = None, repository: str | None = None):
    from .app_updates import default_repository, setup_updates
    app_dir = app_dir or Path(_path('Dedicated application update directory', '~/klipper-component-manager-app'))
    config = config or Path(_path('Existing Moonraker configuration', '~/printer_data/config/moonraker.conf'))
    repository = repository or _ask('GitHub release repository', default_repository())
    setup_updates(store, app_dir, config, repository, confirm=_yes, output=print)


def _flash_menu(manager: Manager, printer: Printer) -> None:
    jobs = manager.jobs(printer)
    if not jobs:
        print("No saved builds. Build the firmware first.")
        return
    selected = _choice("Choose a saved build", [str(job) for job in jobs])
    if selected:
        manager.flash(printer, jobs[selected - 1], confirm=_yes)


def _component_menu(store: Store, manager: Manager, printer: Printer) -> None:
    if not printer.components:
        print("No components yet.")
        return
    selected = _choice("Select component", [f"{item.name} ({item.id}, {item.transport})" for item in printer.components])
    if not selected:
        return
    component = printer.components[selected - 1]
    action = _choice(component.name, ["Edit connection and identity", "Edit/import build configuration", "Delete component", "Open first-time setup / bootloader guide"])
    if action == 1:
        updated = _component_settings(store, printer, component)
        printer.components[selected - 1] = updated
        _save_printer(store, printer)
        print("Component settings saved.")
    elif action == 2:
        _configure(manager, printer, component)
        print("Build configuration saved.")
    elif action == 3 and _yes(f"Remove profile {component.id!r}? Saved build jobs will remain"):
        candidate = Printer.from_dict(printer.to_dict())
        candidate.components = [item for item in candidate.components if item.id != component.id]
        candidate.validate()
        _save_printer(store, candidate)
        print("Component removed.")
    elif action == 4:
        from .onboarding import run_setup
        run_setup(store, manager, printer)


def _manage_printer(store: Store, manager: Manager, printer_id: str) -> None:
    while True:
        printer = store.get(printer_id)
        action = _choice(
            f"{printer.name} — {len(printer.components)} saved component(s)",
            ["Add component", "Manage component", "Check setup", "Show update plan",
             "Build newest configured source", "Build current checkout version", "Flash a saved build",
             "Edit printer settings", "Build latest and flash", "First-time setup / setup guide",
             "Configure Moonraker application updates"],
        )
        if not action:
            return
        try:
            if action == 1:
                component = _component_settings(store, printer)
                _configure(manager, printer, component)
                printer.components.append(component)
                _save_printer(store, printer)
                print(f"Saved {component.name}. Its build settings will be reused on future updates.")
            elif action == 2:
                _component_menu(store, manager, printer)
            elif action == 3:
                for line in manager.doctor(printer):
                    print(line)
            elif action == 4:
                _show_plan(manager, printer)
            elif action in {5, 6}:
                print(f"Saved build job: {manager.build(printer, refresh=action == 5)}")
            elif action == 7:
                _flash_menu(manager, printer)
            elif action == 8:
                _save_printer(store, _printer_settings(printer))
                print("Printer settings saved.")
            elif action == 9:
                job = manager.build(printer, refresh=True)
                print(f"Saved build job: {job}")
                manager.flash(printer, job, confirm=_yes)
            elif action == 10:
                from .onboarding import run_setup
                run_setup(store, manager, printer)
            elif action == 11:
                _moonraker_setup(store)
        except (ValueError, RuntimeError, OSError, KeyError, subprocess.SubprocessError) as exc:
            print(f"\nStopped: {exc}")


def _wizard(store: Store, manager: Manager) -> None:
    print(f"Klipper Component Manager {VERSION}")
    print("Run this walkthrough on the Linux host that runs your Klipper service(s).")
    print("Saved profiles contain the settings for each board. Build latest and flash updates them together.")
    while True:
        printers = store.load()
        options = [f"{item.name} ({item.id})" for item in printers] + ["Add a printer", "First-time setup / setup guide", "Configure Moonraker application updates"]
        choice = _choice("Printers", options, back="Exit")
        if not choice:
            return
        try:
            if choice == len(printers) + 1:
                printer = _printer_settings()
                if any(item.id == printer.id for item in printers):
                    raise ValueError("That printer ID already exists. Select it to edit its settings.")
                _save_printer(store, printer)
                _manage_printer(store, manager, printer.id)
            elif choice == len(printers) + 2:
                from .onboarding import run_setup
                run_setup(store, manager)
            elif choice == len(printers) + 3:
                _moonraker_setup(store)
            else:
                _manage_printer(store, manager, printers[choice - 1].id)
        except (ValueError, RuntimeError, OSError, KeyError, subprocess.SubprocessError) as exc:
            print(f"\nStopped: {exc}")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Guided Klipper builds and Katapult flashing on a Linux host.")
    parser.add_argument("--version", action="version", version=VERSION)
    default_root = Path(os.environ.get("XDG_DATA_HOME", str(Path.home() / ".local" / "share"))) / "klipper-component-manager"
    parser.add_argument("--data-dir", type=Path, default=default_root, help="profile and job storage directory")
    commands = parser.add_subparsers(dest="command")
    commands.add_parser("wizard", help="open the guided setup and update menus (default)")
    commands.add_parser("list", help="list saved printers and components")
    setup = commands.add_parser("setup", help="open the complete first-time setup and board guide")
    setup.add_argument("printer", nargs="?")
    boards = commands.add_parser("boards", help="find bundled board references and official documentation")
    boards.add_argument("query", nargs="*", help="board name, manufacturer, revision, or MCU")
    updater = commands.add_parser('moonraker-setup', help='install the standalone app and configure Moonraker release updates')
    updater.add_argument('--app-dir', type=Path, help='dedicated application directory outside Git and saved profiles')
    updater.add_argument('--config', type=Path, help='existing moonraker.conf for the intended printer')
    updater.add_argument('--repository', help='GitHub owner/repository; defaults to this release repository')
    for name in ("plan", "doctor"):
        commands.add_parser(name).add_argument("printer")
    build = commands.add_parser("build", help="compile all saved board profiles")
    build.add_argument("printer")
    build.add_argument("--latest", action="store_true", help="fetch and build the printer's configured source ref")
    flash = commands.add_parser("flash", help="flash an existing build, with explicit confirmation")
    flash.add_argument("printer")
    flash.add_argument("--job", required=True, type=Path)
    configure = commands.add_parser("configure", help="edit or import a component's build settings")
    configure.add_argument("printer")
    configure.add_argument("component")
    configure.add_argument("--import-config", type=Path)
    configure.add_argument("--latest", action="store_true", help="fetch the configured source ref and review changed build settings")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        store = Store(args.data_dir.expanduser().resolve())
        manager = Manager(store, output=print)
        if args.command in {None, "wizard"}:
            _wizard(store, manager)
        elif args.command == "setup":
            from .onboarding import run_setup
            run_setup(store, manager, store.get(args.printer) if args.printer else None)
        elif args.command == "boards":
            from .onboarding import show_boards
            show_boards(" ".join(args.query))
        elif args.command == 'moonraker-setup':
            _moonraker_setup(store, app_dir=args.app_dir, config=args.config, repository=args.repository)
        elif args.command == "list":
            printers = store.load()
            if not printers:
                print("No saved printers. Run kcm wizard to add one.")
            for printer in printers:
                print(f"{printer.id}: {printer.name}")
                for item in printer.components:
                    print(f"  {item.id}: {item.name} ({item.transport})")
        else:
            printer = store.get(args.printer)
            if args.command == "plan":
                _show_plan(manager, printer)
            elif args.command == "doctor":
                for line in manager.doctor(printer):
                    print(line)
            elif args.command == "build":
                print(f"Saved build job: {manager.build(printer, refresh=args.latest)}")
            elif args.command == "flash":
                manager.flash(printer, args.job.expanduser().resolve(), confirm=_yes)
            elif args.command == "configure":
                import_path = str(args.import_config.expanduser().resolve()) if args.import_config else None
                manager.configure(printer, _find_component(printer, args.component), import_path=import_path, refresh=args.latest)
                print("Build configuration saved.")
        return 0
    except (KeyboardInterrupt, EOFError):
        print("\nStopped. Check the saved job record before continuing an interrupted update.", file=sys.stderr)
        return 130
    except (ValueError, RuntimeError, OSError, KeyError, subprocess.SubprocessError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
