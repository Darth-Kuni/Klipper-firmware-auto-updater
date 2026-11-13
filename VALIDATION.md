# Version 0.3.0 validation

This is a standalone alpha for review and controlled testing on a printer.

## Completed checks

- All 170 automated tests pass with subprocess resource warnings treated as errors.
- Dependency installation, systemd operations, firmware writes, USB/CAN/UART traffic and UF2 device writes were simulated. Tests never connected to a printer or changed the test machine's services/packages.
- Three integration tests used real local Git repositories with network transport disabled, checking current/latest source snapshots, commit pinning and local fast-forward application.
- Regression checks cover unknown/incomplete board evidence, cancellation records, complete MCU inventory, configuration drift, build artifacts, distinct UART baud rates, exact DFU identity, UF2 family/volume identity, deployer rejection, bootloader/application MCU and offset consistency, partial-update records and leaving services stopped after failures.
- Fresh-host service tests check that existing services/configurations are preserved, generated socket/config paths agree, and physical startup is a separate confirmed action.
- The bundled catalog contains six manufacturer-sourced board references with exact revisions. It does not generate automatic firmware presets.

- Moonraker integration tests cover managed includes, configuration backups, concurrent edits, application/data separation, adoption, cancellation and preserving an existing installation.

## Release checks

The Linux/Windows workflow is available in [GitHub Actions](https://github.com/Darth-Kuni/Klipper-firmware-auto-updater/actions/workflows/ci.yml).

The release contains a single-file Python zip application, a source ZIP and a Python wheel. Archive integrity, bundled catalog availability and isolated command-line launch are checked when packaging. No external installer integration is included.

## Not yet verified

GitHub CI passed on Ubuntu with Python 3.10 and 3.12, and Windows with Python 3.12. The local preparation host was Windows. Hardware, service and package-manager operations in the tests remain simulated. Actual Debian/Ubuntu dependency installation, Python environment provisioning on printer hosts, systemd startup, Klipper/Katapult toolchain compilation, bootloader transitions, CAN-interface behavior and physical firmware writes still require controlled hardware testing.

The first release does not automatically provision every printer. SD-card/SWD/ROM-UART bootloader methods, unknown board installation procedures, persistent Linux network configuration, complete machine pin/geometry configuration, optional browser frontend installation and physical commissioning include manual steps documented by the guide.

A ready software connection does not verify heater, endstop or movement wiring. Follow the official Klipper configuration checks before printing. There is no automatic rollback for partial firmware updates, and failed fresh-host provisioning needs inspection of its retained log and installation record before retrying.
