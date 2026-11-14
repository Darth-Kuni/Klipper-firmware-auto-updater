# Changelog

## v0.4.1

- Write the application updater directly in the selected `moonraker.conf` rather than creating an extra include file.
- Move older KCM-managed updater sections into the main configuration after backup, preserving the existing extra file, its includes and unrelated settings.
- Show the manual updater block in the README and keep setup safe to repeat.

## v0.4.0

- Make Git clone plus the executable `kcm.sh` launcher the recommended installation flow; no package installation or virtual environment is needed to open the guide.
- Support Moonraker application updates directly in the cloned checkout, with automatic Git-mode detection and protected configuration backups.
- Keep single-file release downloads and ZIP-based Moonraker updates available for existing installations.
- Simplify the README and move alternate installation methods into a separate guide.

## v0.3.0

- Publish the standalone application, wheel, source archive and checksums through GitHub releases.
- Add Moonraker ZIP application updates with a fixed release asset and matching release metadata.
- Add the `moonraker-setup` command and menu actions: install a dedicated app copy, back up configuration and add a managed update include while preserving printer profiles.
- Add Linux/Windows CI and tag-based release automation.
- Expand download, installation, setup, update, backup and troubleshooting instructions.

This is alpha software. Automated software checks do not replace physical printer and bootloader validation.

## v0.2.0

- Provide a standalone single-file terminal application with a resumable first-time setup guide.
- Include six manufacturer-sourced board references, Katapult configuration/build helpers, initial DFU/UF2 installation paths, and guided manual routes.
- Add initial Klipper flashing and new-host Klipper/Moonraker service setup.

## v0.1.0

- Introduce saved per-component firmware profiles, isolated builds, Katapult CAN/USB/UART flashing, dependency ordering and update journals.
