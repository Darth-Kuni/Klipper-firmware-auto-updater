# Version 0.4.1 validation

This is a standalone alpha for review and controlled testing on a printer.

## Completed checks

- The 198-test suite passes on the local Windows preparation host with subprocess resource warnings treated as errors. Four Linux-only launcher checks are skipped there and run on Ubuntu in GitHub Actions.
- Dependency installation, systemd operations, firmware writes, USB/CAN/UART traffic and UF2 device writes were simulated. Tests never connected to a printer or changed the test machine's services/packages.
- Source integration tests use real local Git repositories with network transport disabled, checking current/latest source snapshots, commit pinning and local fast-forward application.
- Regression checks cover unknown/incomplete board evidence, cancellation records, complete MCU inventory, configuration drift, build artifacts, distinct UART baud rates, exact DFU identity, UF2 family/volume identity, deployer rejection, bootloader/application MCU and offset consistency, partial-update records and leaving services stopped after failures.
- Fresh-host service tests check that existing services/configurations are preserved, generated socket/config paths agree, and physical startup is a separate confirmed action.
- The bundled catalog contains six manufacturer-sourced board references with exact revisions. It does not generate automatic firmware presets.

- Moonraker integration tests cover direct updater sections in the main configuration, migration from older managed includes while retaining their other settings, configuration backups, partial-write recovery, concurrent edits, application/data separation, adoption, cancellation and preserving an existing installation. Git-mode tests additionally check the checkout identity, clean tracked files, branch/tracking settings, SSH origins, worktrees and migration from an existing managed ZIP updater.
- Linux launcher checks cover the executable entry point from an unrelated working directory, selecting the correct application checkout, paths containing spaces, argument forwarding, unsupported Python versions, and a fresh local clone configuring its own Moonraker Git updater.

## Release checks

The Linux/Windows workflow is available in [GitHub Actions](https://github.com/Darth-Kuni/Klipper-firmware-auto-updater/actions/workflows/ci.yml).

The recommended installation clones the repository and runs the executable `kcm.sh` directly, without pip or a virtual environment. CI runs that launcher on Ubuntu with Python 3.10 and 3.12.

Optional release downloads include a single-file Python zip application, a source ZIP, a Python wheel and a Moonraker ZIP bundle. Archive integrity, bundled catalog availability, release metadata, checksums and isolated command-line launch are checked when packaging. No external installer integration is included.

## Not yet verified

Hardware, service and package-manager operations in the tests remain simulated. Actual Debian/Ubuntu dependency installation, Python environment provisioning on printer hosts, systemd startup, Klipper/Katapult toolchain compilation, bootloader transitions, CAN-interface behavior and physical firmware writes still require controlled hardware testing. GitHub Actions verifies software behavior on Ubuntu with Python 3.10 and 3.12, and Windows with Python 3.12; it does not establish hardware compatibility.

The first release does not automatically provision every printer. SD-card/SWD/ROM-UART bootloader methods, unknown board installation procedures, persistent Linux network configuration, complete machine pin/geometry configuration, optional browser frontend installation and physical commissioning include manual steps documented by the guide.

A ready software connection does not verify heater, endstop or movement wiring. Follow the official Klipper configuration checks before printing. There is no automatic rollback for partial firmware updates, and failed fresh-host provisioning needs inspection of its retained log and installation record before retrying.
