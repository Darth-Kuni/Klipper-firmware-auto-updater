# Klipper Component Manager

Standalone Linux terminal software for guided Klipper/Katapult setup and repeatable firmware updates. Version 0.3.0 is an alpha for controlled testing. It is independent of other installers.

## Download and install

[Download the latest program](https://github.com/Darth-Kuni/Klipper-firmware-auto-updater/releases/latest/download/klipper-component-manager.pyz) | [All releases and source downloads](https://github.com/Darth-Kuni/Klipper-firmware-auto-updater/releases) | [Report an issue](https://github.com/Darth-Kuni/Klipper-firmware-auto-updater/issues)

### What you need

- A Linux printer host accessible directly or over SSH, with **Python 3.10 or newer**.
- A normal Linux user with sudo access for package, service and initial DFU actions.
- Internet access for initial source/tool downloads and later release updates.
- The board's exact model/revision, its manufacturer documentation, and the required USB/CAN adapter, cable or programmer.
- A saved copy of any working printer configuration before changing firmware.

Automatic host preparation targets Debian/Ubuntu-based systems with systemd. Other distributions can use manually installed dependencies and their own documented service setup. The downloadable program includes its Python code, setup guide and board catalog; MCU build toolchains are installed separately by the guided dependency step.

### Recommended: one-file download on the printer host

SSH into the Linux host from another computer:

```sh
ssh YOUR_USER@PRINTER_IP
```

Then run these commands **on that Linux host**:

```sh
cd ~
python3 --version
curl -fL https://github.com/Darth-Kuni/Klipper-firmware-auto-updater/releases/latest/download/klipper-component-manager.pyz -o klipper-component-manager.pyz
python3 klipper-component-manager.pyz
```

Choose **First-time setup / setup guide** for a new printer. For a working printer, create its printer/component profiles and import known build configurations before attempting an update. Run the program as your normal user, not with sudo.

If curl is missing on Debian/Ubuntu:

```sh
sudo apt-get update
sudo apt-get install curl
```

If Python is older than 3.10, use a supported Linux image with a newer Python interpreter. The application does not replace the operating system's Python installation.

### Check the download

Download the release's checksum manifest and verify the files you downloaded:

```sh
curl -fL https://github.com/Darth-Kuni/Klipper-firmware-auto-updater/releases/latest/download/SHA256SUMS.txt -o SHA256SUMS.txt
sha256sum --check --ignore-missing SHA256SUMS.txt
```

Confirm that `klipper-component-manager.pyz` reports `OK`. For a fixed release instead of the moving latest link, use `/releases/download/v0.3.0/` in both URLs. A checksum mismatch means you should download the program and manifest from the same release again.

### Download elsewhere and copy over SSH

From the directory containing the downloaded program on your computer:

```sh
scp klipper-component-manager.pyz YOUR_USER@PRINTER_IP:~/
ssh YOUR_USER@PRINTER_IP
python3 ~/klipper-component-manager.pyz
```

Use your actual account and printer IP/hostname; there is no assumed default password.

### Optional: source or Python package installation

The source checkout runs directly:

```sh
git clone https://github.com/Darth-Kuni/Klipper-firmware-auto-updater.git ~/klipper-firmware-auto-updater-source
cd ~/klipper-firmware-auto-updater-source
python3 -m kcm
```

For a conventional `kcm` command, install the downloaded wheel or source into a virtual environment:

```sh
python3 -m venv --system-site-packages ~/kcm-env
. ~/kcm-env/bin/activate
python3 -m pip install /path/to/downloaded/klipper_component_manager-0.3.0-py3-none-any.whl
kcm
```

The system-site-packages option lets this environment use distribution-installed pyserial. If using an isolated custom environment, install pyserial into it before USB/UART flashing. The one-file download is the simplest way to start; a source checkout is not required.

**Command convention:** examples below use `kcm`. With the one-file download, replace that word with `python3 ~/klipper-component-manager.pyz`.

## First-time setup

The numbered guide saves individual actions, observations and your confirmations, so you can leave an SSH session and return. Viewing instructions does not certify a commissioned printer.

1. **Prepare Linux and SSH.** Follow the OS/network instructions and inspect available tools, USB/UART paths, CAN interfaces and RP boot volumes.
2. **Identify each board.** Search the bundled manufacturer references, compare the physical PCB revision and MCU marking, and save the documentation. Unknown boards use your own references and a manual initial-install route.
3. **Prepare software.** Install build/flashing dependencies on supported Debian/Ubuntu hosts and download official Klipper/Katapult sources. Existing source checkouts are preserved.
4. **Install Katapult.** Configure and build it in an isolated checkout. Follow the board's physical boot-mode instructions. The program supports initial STM32 USB DFU installation selected by exact serial number and matching RP2040/RP2350 UF2 boot volumes.
5. **Save connections and Klipper settings.** Use the references alongside upstream menuconfig or import a known configuration. Store actual CAN UUIDs or serial identities when available. MCU and application address must agree with the saved Katapult configuration before the first flash.
6. **Build and first-flash Klipper.** Build from the host's current source commit, then flash each board through Katapult. Initial setup does not require an already working Moonraker connection. The Klipper service stays stopped.
7. **Prepare the printer configuration.** Export MCU connection snippets and merge them into a complete reviewed configuration. The app does not invent motor/heater pins, dimensions or limits.
8. **Install host services if needed.** The new-host path creates separate Python environments and new Klipper/Moonraker systemd services after confirmation. Existing services are preserved; new services stay stopped.
9. **Start and check.** Review the complete configuration, start the new services, check the intended Klipper process, and perform the official physical checks before printing.

```sh
kcm setup
kcm setup PRINTER_ID
kcm boards EBB36
kcm boards M8P
```

## Board references

The first catalog covers six exact references: EBB36 CAN V1.2, EBB42 CAN V1.2, Octopus V1.1 STM32F446, Octopus Pro V1.0 STM32F429, Manta M8P V2.0 STM32H723, and SB2209 CAN V1.0 RP2040. Each includes manufacturer links and relevant clock/pin information. They are reference data, not automatically applied build presets.

A USB descriptor or CAN UUID does not establish a physical model/revision. A factory SD-card bootloader offset is not automatically the correct Katapult offset. Check the exact board and its documentation.

## Automation and manual steps

| Task | Behavior |
| --- | --- |
| OS image, networking and SSH | Guided instructions after which the app runs on Linux. |
| Dependencies and source downloads | Confirmed Debian/Ubuntu package installation and official Git clones. |
| Board firmware configuration | Reference hints, upstream menuconfig, saved settings and drift checks. |
| STM32 initial Katapult installation | Exact DFU serial and verified build; no generic mass erase or deployer. |
| RP2040/RP2350 initial installation | Verified UF2 image and matching mounted BOOTSEL volume. |
| SD card, SWD or ROM UART initial installation | Explicit handoff to the board-specific manufacturer procedure. |
| CAN identity and link | Confirmed single-node Katapult discovery; current-session interface setup. Persistent network configuration remains guided. |
| Fresh Klipper/Moonraker services | New isolated environments/services; no overwrite of existing services. |
| Browser frontend and physical calibration | Official instructions; no automatic homing, motion or heater tests. |

Katapult installation is separate from ordinary updates. UART uploads through an installed Katapult are supported; initial ROM-UART bootloader installation is a separate manual method. Custom boards use manual initial-install instructions even if their method resembles a catalog board.

For a new USB-to-CAN bridge, first make the bridge operational over USB before reaching downstream boards through it. Follow the [CAN guide](https://www.klipper3d.org/CANBUS.html) to identify the running bridge and complete its saved profile. Later batch updates flash downstream boards before their bridge.

USB/UART device paths may change between Katapult and Klipper. Use manual bootloader entry for the first flash, then inspect and save the running Klipper path before enabling automatic entry. Serial access may need membership in the distro's serial-device group, often dialout, followed by a new SSH login.

## New-host services and configuration

Automatic host provisioning targets Debian/Ubuntu with systemd. The guide prints the new data directory: DATA_ROOT/host/PRINTER_ID/printer_data. It creates printer.cfg.example with host-side sections, but a complete reviewed printer.cfg is required before starting. Merge the required sections into your machine configuration; the example alone cannot operate a printer.

New Moonraker instances listen on loopback. Optional browser setup uses [Mainsail's official manual instructions](https://github.com/mainsail-crew/gb-docs/blob/main/setup/getting-started/manual-setup.md). Deliberately configure an SSH tunnel/proxy and network authorization. Moonraker's own service-management provider is disabled in this new-host configuration; the terminal app manages services directly.

A failed host installation retains install.json and install.log. The checklist resumes, but a partially provisioned host needs inspection rather than automatic undo/retry. Existing working installations can skip host-service installation.

## Update the program through Moonraker

Moonraker can update this standalone application's files from GitHub releases. That operation **does not compile or flash printer boards**. Use the app's firmware commands for board updates.

From the Linux host, run:

```sh
python3 ~/klipper-component-manager.pyz moonraker-setup --app-dir "$HOME/klipper-component-manager-app" --config "$HOME/printer_data/config/moonraker.conf"
```

Choose the actual `moonraker.conf` path for your printer instance. The command shows its plan and asks before writing. It:

1. Installs the standalone app and release metadata into the dedicated application directory.
2. Backs up affected Moonraker configuration files under the separate KCM data directory.
3. Creates `kcm-updater.conf` and adds its include plus the base update manager section if missing.
4. Preserves saved profiles, build settings, artifacts and logs outside the application directory.

Exit KCM, wait until the printer is idle, then restart the intended Moonraker service to load the configuration. A standard service is commonly named `moonraker`:

```sh
sudo systemctl restart moonraker
```

Use your real service name for multi-instance/custom setups. Hosts created by this app use `kcm-moonraker-PRINTER_ID`. The setup command itself does not restart services.

Refresh the update manager in Mainsail/Fluidd. The **klipper-component-manager** entry tracks normal GitHub releases. After setup, launch the managed copy so you actually run future updates:

```sh
python3 ~/klipper-component-manager-app/klipper-component-manager.pyz
```

Close KCM before updating its application files through Moonraker, and wait for any firmware operation to finish first. If you use a custom `--data-dir`, pass the same value when configuring updates and launching the managed copy.

The managed configuration resembles:

```ini
[update_manager klipper-component-manager]
type: zip
channel: stable
repo: Darth-Kuni/Klipper-firmware-auto-updater
path: /home/YOUR_USER/klipper-component-manager-app
is_system_service: False
```

The path must be a dedicated directory outside Git checkouts, saved printer data and Moonraker's config folder. The fixed asset name is recorded in `release_info.json`; it is not an `asset_name` option in moonraker.conf. The stable channel means normal GitHub releases; the program is still alpha software.

See [Moonraker setup, manual installation and recovery](docs/moonraker.md) for included configurations, alternate paths and troubleshooting. If you prefer manual application updates, download a newer standalone program from [Releases](https://github.com/Darth-Kuni/Klipper-firmware-auto-updater/releases) and launch it with your same saved data directory.

## Update printer firmware

Choose **Build latest and flash** once every component profile is saved. The program resolves the configured origin/branch once, builds all boards before downtime, checks artifact/config hashes and printer/service/MCU identity, backs up configuration, advances a clean host source checkout and flashes in dependency order. It restores CAN interfaces and checks that Klipper returns ready.

```sh
kcm build PRINTER_ID --latest
kcm flash PRINTER_ID --job /path/printed/by/build
kcm configure PRINTER_ID COMPONENT_ID --latest
kcm doctor PRINTER_ID
kcm plan PRINTER_ID
```

Omit --latest to build the current host commit. Reconfigure if upstream Kconfig settings changed. Source updates do not automatically migrate every OS/Python dependency. An incomplete update leaves Klipper stopped with logs and per-board progress; there is no automatic firmware rollback.

## Data and limitations

Data is stored in $XDG_DATA_HOME/klipper-component-manager or ~/.local/share/klipper-component-manager. Back it up. Use the same global --data-dir PATH on later runs if choosing another location.

This version operates on one Linux host, with several saved printer service profiles. Central control across remote hosts is not implemented. Independently updated printers need separate source checkouts; shared checkouts are detected among saved profiles. Routine batch updates require supported Katapult profiles for the full active MCU inventory; Linux-process and proprietary MCU updaters are outside that operation.

The app uses the Python standard library. USB/UART flashing also needs pyserial in the interpreter launching it; the Debian dependency stage installs the system package. Make it available separately if using a custom virtual environment. Run as a normal user; sudo is requested for concrete privileged changes.

## Development and references

Run tests with `python3 -m unittest discover -s tests`. Build source ZIP and standalone program with `python3 tools/build_release.py`. See [VALIDATION.md](VALIDATION.md) for checks and outstanding hardware work.

Official references: [Klipper installation](https://www.klipper3d.org/Installation.html), [configuration checks](https://www.klipper3d.org/Config_checks.html), [CAN setup](https://www.klipper3d.org/CANBUS.html), [Katapult](https://github.com/Arksine/katapult), and [Moonraker installation](https://moonraker.readthedocs.io/en/latest/installation/). Board sources are bundled in the catalog.

Version 0.3.0 has not been tested on physical printers. Linux provisioning, initial bootloader writes and commissioning need controlled hardware validation before general release. Licensed under the MIT license; see [LICENSE](LICENSE).


## Backup and recovery

Keep a copy of your whole KCM data directory and your printer's configuration directory. Default KCM data is `~/.local/share/klipper-component-manager`; a custom XDG or --data-dir location changes that. Firmware build jobs, both board configurations, setup records and update logs live there.

Before a routine firmware update, the app backs up the active printer configuration into the build job. Moonraker setup backups live under `backups/moonraker` in the data directory. Those backups do not replace an image backup of your host's SD card or a copy of a board's original firmware.

If flashing fails, leave the printer stopped, inspect the retained manifest and logs, and identify which components completed. Do not keep retrying arbitrary board targets. Restore/reflash through the known board-specific recovery method after reviewing the recorded versions and configurations. There is no automatic whole-printer firmware rollback.

## Troubleshooting

| Symptom | What to check |
| --- | --- |
| Python version error | Run `python3 --version`; the app requires 3.10+. |
| Command not found: kcm | Use `python3 /path/to/klipper-component-manager.pyz`, or activate the virtual environment used for a wheel install. |
| pyserial/serial import error | Install pyserial into the interpreter running the app, or use system Python after the guided dependency step. |
| USB/UART permission denied | Check the device's owner/group. On Debian/Ubuntu the serial group is often dialout; add your actual user if appropriate, then log out of SSH and reconnect. Avoid world-writable serial-device permissions. |
| No USB/DFU/UF2 device | Check the data-capable cable, board power selection, physical boot button/jumper procedure, and manufacturer instructions. RP BOOTSEL/UF2 is distinct from STM32 DFU. |
| No CAN devices or timeout | Check adapter, wiring, common ground, matching firmware bitrate and termination. Katapult discovery requires the single-node isolation confirmation; already assigned Klipper nodes may not appear. |
| Bridge not available during initial setup | First make the USB-to-CAN bridge operational over USB, then configure the interface and downstream boards. |
| Build settings changed | Review the reported Kconfig changes using configure --latest and rebuild. Do not silently substitute MCU, clock, pins or offsets. |
| Wrong service, source or active MCU inventory | Correct the saved printer/component profiles so they match the intended running instance. An update does not bypass this check. |
| Existing services prevent new-host install | Preserve the working installation and skip that stage, or explicitly choose separate service/source paths for a new instance. |
| Moonraker updater invalid/missing | Check release_info.json, the include/config path, a normal published GitHub release, and that the app directory is outside Git and saved data. Restart the correct idle Moonraker instance, then refresh updates. |

## Remove the application

Close KCM and finish any firmware work first. To stop Moonraker managing it, back up moonraker.conf, remove `[include kcm-updater.conf]`, remove the updater include only if no wildcard include still loads it, and restart the correct Moonraker instance when idle. Keep a shared `[update_manager]` section if other components use it.

You can then remove the dedicated app directory or downloaded program after checking the exact path. **Keep your KCM data directory if you want to retain printer profiles and recovery logs.** Removing the app does not undo installed Katapult firmware or remove Klipper/Moonraker host services.

## Help and contributing

Open an [issue](https://github.com/Darth-Kuni/Klipper-firmware-auto-updater/issues) with the program version, Linux/Python version, exact board revision/MCU, connection type, selected operation and relevant log excerpt. Remove passwords, tokens, network credentials and other private printer configuration before posting. See [CONTRIBUTING.md](CONTRIBUTING.md) for software checks and release versioning.
