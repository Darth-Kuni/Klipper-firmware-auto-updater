# Klipper Component Manager

[![CI](https://github.com/Darth-Kuni/Klipper-firmware-auto-updater/actions/workflows/ci.yml/badge.svg)](https://github.com/Darth-Kuni/Klipper-firmware-auto-updater/actions/workflows/ci.yml)

A standalone Linux terminal app that walks you through Klipper/Katapult setup, remembers each board's settings, and rebuilds and flashes your configured components together. Version 0.4.0 is an alpha for controlled testing.

## Install and launch

SSH into your Linux printer host as your normal user. Run these two commands from your home directory (`cd ~` first if needed):

```sh
git clone https://github.com/Darth-Kuni/Klipper-firmware-auto-updater.git
./Klipper-firmware-auto-updater/kcm.sh
```

The launcher requires Python 3.10 or newer and runs the app directly from the checkout. No Python package installation or virtual environment is needed to open it. The setup guide installs firmware build tools when you choose that step.

On later visits, run:

```sh
~/Klipper-firmware-auto-updater/kcm.sh
```

<details>
<summary>If Git is missing on Debian/Ubuntu</summary>

```sh
sudo apt-get update
sudo apt-get install git
```

Then run the two install commands above. If the system Python is older than 3.10, use a supported Linux image with a newer Python interpreter.

</details>

Choose **First-time setup / setup guide** for a new printer. For a working printer, save its printer/component profiles and import the known firmware build settings; skip hardware installation and host-service steps you have already completed. Keep a copy of the working printer configuration before changing firmware.

[Installation details and optional single-file/package downloads](docs/installation.md) | [Releases](https://github.com/Darth-Kuni/Klipper-firmware-auto-updater/releases) | [Report an issue](https://github.com/Darth-Kuni/Klipper-firmware-auto-updater/issues)

## First-time setup

The numbered guide saves your actions, observations and confirmations so you can leave SSH and return. Work through the applicable stages for each physical board:

1. **Prepare Linux and SSH.** Inspect available tools, USB/UART paths, CAN interfaces and RP boot volumes.
2. **Identify each board.** Search the bundled manufacturer references, compare the physical PCB revision and MCU marking, and save the documentation. Unknown boards use your references and a manual initial-install route.
3. **Prepare software.** Install build/flashing dependencies on supported Debian/Ubuntu hosts and download official Klipper/Katapult sources. Existing checkouts are preserved.
4. **Install Katapult.** Configure and build it in an isolated checkout. Follow the board's physical boot-mode instructions. Supported automatic initial methods are STM32 USB DFU selected by exact serial and RP2040/RP2350 UF2 using a matching mounted boot volume.
5. **Save connections and Klipper settings.** Use reference hints alongside upstream menuconfig or import a known configuration. Save real CAN UUIDs or serial identities. The MCU and application address must agree with the saved Katapult configuration before the first Klipper flash.
6. **Build and first-flash Klipper.** Build from the host's current source commit, then flash each board through Katapult. Initial setup can proceed without a working Moonraker connection; the Klipper service stays stopped.
7. **Prepare the printer configuration.** Export MCU connection references and merge them into a complete reviewed configuration. Supply the correct motor/heater pins, dimensions and limits for your machine.
8. **Install host services if needed.** The new-host path creates separate Python environments and new Klipper/Moonraker systemd services after confirmation. Existing services are preserved; new services stay stopped.
9. **Start and check.** Review the complete configuration, start the intended services, check the Klipper process, and perform the official physical checks before printing.

```sh
~/Klipper-firmware-auto-updater/kcm.sh setup
~/Klipper-firmware-auto-updater/kcm.sh setup PRINTER_ID
~/Klipper-firmware-auto-updater/kcm.sh boards EBB36
~/Klipper-firmware-auto-updater/kcm.sh boards M8P
```

Viewing a guide or receiving a successful firmware-write result does not complete the physical commissioning checks.

## Board references and connections

The catalog covers six exact references: EBB36 CAN V1.2, EBB42 CAN V1.2, Octopus V1.1 STM32F446, Octopus Pro V1.0 STM32F429, Manta M8P V2.0 STM32H723, and SB2209 CAN V1.0 RP2040. Manufacturer links and clock/pin information help you choose settings; they are not automatically applied build presets.

Compare the physical revision and MCU marking with the documentation. A USB descriptor or CAN UUID does not identify the board revision. A factory SD-card bootloader offset is not automatically the correct Katapult offset.

| Task | Behavior |
| --- | --- |
| Dependencies and source downloads | Confirmed Debian/Ubuntu package installation and official Git clones. |
| Firmware configuration | Reference hints, upstream menuconfig, saved board settings and drift checks. |
| STM32 initial Katapult installation | Exact DFU serial and verified build; no generic mass erase or deployer. |
| RP2040/RP2350 initial installation | Verified UF2 image and matching mounted BOOTSEL volume. |
| SD card, SWD or ROM UART initial installation | Handoff to the board-specific manufacturer procedure. |
| CAN identity and link | Confirmed single-node Katapult discovery and current-session interface setup; persistent network configuration remains guided. |
| Fresh Klipper/Moonraker services | New environments/services with existing services preserved. |
| Browser frontend and physical calibration | Official instructions; no automatic homing, motion or heater tests. |

Katapult installation is separate from routine Klipper updates. UART uploads through an installed Katapult are supported; initial ROM-UART installation is a manual method. Custom boards use manual initial-install instructions.

For a fresh USB-to-CAN bridge, make the bridge operational over USB before reaching downstream boards through it. Follow the [CAN guide](https://www.klipper3d.org/CANBUS.html) to identify the running bridge and finish its profile. Later batch updates flash downstream boards before their bridge.

USB/UART paths can differ between Katapult and Klipper. Use manual bootloader entry for the first flash, then inspect and save the running Klipper path before enabling automatic entry. Serial-device group access may require a new SSH login.

## Update printer firmware

Once every component profile is saved, choose **Build latest and flash**. KCM resolves the configured source branch once, builds all boards before downtime, checks artifacts and device/service identity, backs up the printer configuration, advances a clean Klipper source checkout and flashes in dependency order. It restores CAN interfaces and checks that Klipper returns ready.

The individual commands are also available:

```sh
~/Klipper-firmware-auto-updater/kcm.sh build PRINTER_ID --latest
~/Klipper-firmware-auto-updater/kcm.sh flash PRINTER_ID --job /path/printed/by/build
~/Klipper-firmware-auto-updater/kcm.sh configure PRINTER_ID COMPONENT_ID --latest
~/Klipper-firmware-auto-updater/kcm.sh doctor PRINTER_ID
~/Klipper-firmware-auto-updater/kcm.sh plan PRINTER_ID
```

Omit `--latest` to build the current host commit. Review and reconfigure if upstream Kconfig settings changed. Source updates do not automatically migrate every OS/Python dependency. An incomplete firmware update leaves Klipper stopped with logs and per-board progress; automatic firmware rollback is not implemented.

## Update this app through Moonraker

Run this once from the clone:

```sh
~/Klipper-firmware-auto-updater/kcm.sh moonraker-setup
```

Select the actual `moonraker.conf` when prompted and review the plan. KCM detects this checkout, backs up affected configuration and writes its updater include. Saved printer profiles and firmware jobs stay in their separate data directory.

Exit KCM, wait for firmware work and printing to finish, and restart the intended Moonraker instance. A standard instance commonly uses `sudo systemctl restart moonraker`; use your actual service name for custom/multiple instances. Refresh updates in Mainsail/Fluidd, then update **klipper-component-manager** there. Continue launching the same `~/Klipper-firmware-auto-updater/kcm.sh` afterwards.

This Git updater follows the repository's `main` branch using Moonraker's `dev` channel. It tracks source changes rather than GitHub release tags. **Updating the app does not compile or flash printer boards.** Exit KCM before updating its files.

For a manual application update, exit KCM and run:

```sh
git -C ~/Klipper-firmware-auto-updater pull --ff-only
```

If Git reports local changes or divergence, inspect them before retrying. There is no hard-reset step. See [Moonraker setup, migration and recovery](docs/moonraker.md) for the generated configuration and the optional release-ZIP route.

## Data and backups

Saved profiles, build settings, artifacts, setup records and logs live in `$XDG_DATA_HOME/klipper-component-manager`, or `~/.local/share/klipper-component-manager` by default. They are outside the Git checkout, so application updates preserve them. Keep a custom `--data-dir PATH` outside the checkout and use the same value on later launches.

Back up that whole KCM data directory and your printer's configuration directory. Before a routine firmware update, KCM saves the active printer configuration in the build job. Moonraker setup backups live under `backups/moonraker` in the KCM data directory. These do not replace a host SD-card image backup or a copy of a board's original firmware.

If flashing fails, leave the printer stopped, inspect the manifest/logs and identify which components completed. Use the known board-specific recovery procedure after reviewing the recorded versions and configurations. There is no automatic whole-printer firmware rollback.

## New-host services and configuration

Automatic host provisioning targets Debian/Ubuntu with systemd. The guide prints `DATA_ROOT/host/PRINTER_ID/printer_data`. It creates `printer.cfg.example` with host-side sections; merge those sections into your complete reviewed machine configuration before starting.

New Moonraker instances listen on loopback. Optional browser setup follows [Mainsail's manual instructions](https://github.com/mainsail-crew/gb-docs/blob/main/setup/getting-started/manual-setup.md). Configure the intended SSH tunnel/proxy and network authorization. KCM manages services directly in this new-host configuration.

A failed host installation retains `install.json` and `install.log`. Inspect a partially provisioned host before retrying. Working installations can skip the new-service stage.

## Troubleshooting

| Symptom | What to check |
| --- | --- |
| `git: command not found` | Use the small Git installation section above, then clone and launch. |
| Python version error | Run `python3 --version`; use Python 3.10+. |
| Launcher path not found | The default directory is `~/Klipper-firmware-auto-updater`; use the exact capitalization or your actual clone path. |
| pyserial import or serial permission error | Use system Python after the guide's dependency step. Check the device group, often `dialout` on Debian/Ubuntu, then reconnect SSH if group membership changed. |
| No USB/DFU/UF2 device | Check a data-capable cable, power selection and the manufacturer's boot button/jumper procedure. RP BOOTSEL differs from STM32 DFU. |
| CAN timeout or unavailable bridge | Check adapter, wiring, common ground, termination and matching firmware bitrate. Bring up a fresh bridge over USB first. Discovery requires the single-node isolation confirmation. |
| Build settings changed | Review with the `configure ... --latest` launcher command above and rebuild; do not substitute MCU, clock, pin or offset defaults. |
| Wrong service/source/MCU inventory | Correct the saved profiles to match the intended running instance. |
| Existing services prevent new-host install | Preserve the working installation and skip that stage, or select separate service/source paths. |
| Moonraker updater missing or invalid | Check the selected config/include, actual clone path, `main` branch and origin. Restart the correct idle instance and refresh updates; see [the detailed guide](docs/moonraker.md). |

## Limits, development and removal

KCM operates on one Linux host with several saved printer service profiles. Central control across remote hosts is not implemented. Independently updated printers need separate Klipper source checkouts; shared checkouts are detected among saved profiles. Routine batch updates require supported Katapult profiles for the full active MCU inventory. Linux-process and proprietary MCU updaters are outside that operation.

The app uses the Python standard library. USB/UART flashing also needs pyserial in its interpreter; the guided Debian dependency stage installs the system package. Run KCM as your normal user. It asks for concrete privileged package/service changes when needed.

See [CONTRIBUTING.md](CONTRIBUTING.md) for tests and packaging, [VALIDATION.md](VALIDATION.md) for verification and outstanding hardware work, and [optional installation methods](docs/installation.md) for single-file downloads or wheels.

To remove the app, finish firmware work and exit it. Disable its Moonraker updater include if configured, ensuring no wildcard include still loads it, then restart the intended idle Moonraker instance. Keep a shared `[update_manager]` section used by other components. You can remove the application checkout after checking its path. Keep the separate data directory to retain your saved profiles; host services provisioned by the guide require separate inspection/removal.

Official references: [Klipper installation](https://www.klipper3d.org/Installation.html), [configuration checks](https://www.klipper3d.org/Config_checks.html), [CAN setup](https://www.klipper3d.org/CANBUS.html), [Katapult](https://github.com/Arksine/katapult), and [Moonraker installation](https://moonraker.readthedocs.io/en/latest/installation/). Board sources are bundled in the catalog.

Version 0.4.0 has not been tested on physical printers. Linux provisioning, initial bootloader writes and commissioning still need controlled hardware validation. See [LICENSE](LICENSE) for the license.
