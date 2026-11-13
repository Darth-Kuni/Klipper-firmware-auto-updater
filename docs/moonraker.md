# Moonraker application updates

This integration updates the standalone Klipper Component Manager application from normal GitHub releases. It does not run firmware jobs, install Katapult, or flash printer boards.

## Recommended setup

Run on the Linux host as the user who normally operates Moonraker/KCM:

```sh
python3 ~/klipper-component-manager.pyz moonraker-setup --app-dir "$HOME/klipper-component-manager-app" --config "$HOME/printer_data/config/moonraker.conf"
```

Use the actual configuration file for the intended Moonraker instance. If you changed KCM's data directory, put the same global `--data-dir PATH` before `moonraker-setup` and use it on every future launch.

The confirmation shows the application directory, saved-data directory, selected Moonraker configuration and generated updater section. Installation stages a complete standalone program, writes `release_info.json`, backs up configuration and creates a managed `kcm-updater.conf` include. It preserves unrelated configuration and detects conflicting sections or concurrent edits. It does not restart any service.

Afterwards:

1. Exit KCM and wait until all firmware operations and printing have finished.
2. Restart the correct Moonraker instance. For the usual service, `sudo systemctl restart moonraker`; custom instances may have a different name.
3. Refresh the update manager in Mainsail/Fluidd.
4. Use the managed program: `python3 ~/klipper-component-manager-app/klipper-component-manager.pyz`.

Keep profiles and artifacts outside the managed app directory. Moonraker replaces application files there during updates. The default KCM data directory is `~/.local/share/klipper-component-manager`; it is not part of the release ZIP.

## Expected configuration

Add the base section only if it is not already present:

```ini
[update_manager]

[include kcm-updater.conf]
```

The managed `kcm-updater.conf` contains:

```ini
[update_manager klipper-component-manager]
type: zip
channel: stable
repo: Darth-Kuni/Klipper-firmware-auto-updater
path: /home/YOUR_USER/klipper-component-manager-app
is_system_service: False
```

Replace the example path with the real dedicated application directory. Do not duplicate an existing updater section. An existing wildcard include such as `[include *.conf]` can already load the file, so an additional exact include may be unnecessary; the setup command checks this.

The application directory contains exactly:

```text
klipper-component-manager.pyz
release_info.json
```

For the first published release, metadata is:

```json
{
  "project_name": "Klipper-firmware-auto-updater",
  "project_owner": "Darth-Kuni",
  "version": "v0.3.0",
  "asset_name": "klipper-component-manager-moonraker.zip"
}
```

The version is the exact GitHub release tag. `asset_name` belongs in this JSON, not in the Moonraker configuration. No application service or restart hook is configured. The stable channel follows normal releases; it does not mean hardware validation is complete or that alpha software is production certified.

## Manual installation

Use this if you prefer to configure Moonraker yourself:

1. Create an empty, dedicated application directory outside any Git checkout, saved-data directory and printer configuration folder.
2. Download [the Moonraker release bundle](https://github.com/Darth-Kuni/Klipper-firmware-auto-updater/releases/latest/download/klipper-component-manager-moonraker.zip) and the matching `SHA256SUMS.txt` from the same release. Verify the checksum before extraction.
3. Extract the bundle into that empty directory. The two files above must be at its root, with no extra nested release folder.
4. Back up your Moonraker configuration, then add the updater section using your actual path and enable the base update manager if needed.
5. Exit KCM, restart the correct idle Moonraker instance, refresh updates, and launch the managed copy.

Do not point Moonraker's ZIP updater at the source checkout or the directory containing your printer profiles. To keep a source checkout, use a separate directory for the release-managed application.

## Backups, troubleshooting and removal

The automated configuration command keeps backups under `DATA_ROOT/backups/moonraker/`. Its output names the exact backup directory. Those backups cover the affected main/include files; always keep your own copy of the full printer configuration too.

If the updater does not appear, check the actual configuration path and includes, restart the correct service, then refresh its update manager. If it reports invalid state, verify the two root files, repository owner/name, exact version tag, correct fixed ZIP asset and a normal published GitHub release. Empty GitHub repositories and unreleased tags cannot supply an application update.

If the managed program seems unchanged after an update, confirm that you are launching the copy in the configured application directory rather than the original download. Close the terminal program before updating its files.

For manual configuration rollback, inspect the recorded backup and restore only the affected configuration files after checking for subsequent edits. The command does not automatically restart services. Application configuration backups do not roll back board firmware.

To disable updates, back up the configuration, remove the updater section/include, and ensure no wildcard include still loads `kcm-updater.conf`. Keep a shared `[update_manager]` section if other components use it. Restart the correct Moonraker instance when idle. Saved KCM profiles and firmware remain unchanged.

## Reference

The integration follows Moonraker's [ZIP application configuration](https://moonraker.readthedocs.io/en/latest/configuration/#zip-application-configuration) and [release_info.json format](https://moonraker.readthedocs.io/en/latest/configuration/#the-release_infojson-file).
