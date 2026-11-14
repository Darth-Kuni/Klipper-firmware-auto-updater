# Moonraker application updates

Moonraker can update the KCM application shown in Mainsail/Fluidd's update manager. The updater section goes directly in your existing `moonraker.conf`; a new updater include file is not needed. Firmware compilation and board flashing remain explicit actions inside KCM.

## Recommended: update the Git clone

Choose the guided helper below or the manual configuration section. After the normal clone-and-launch installation, the helper is:

```sh
~/Klipper-firmware-auto-updater/kcm.sh moonraker-setup
```

The command detects the checkout and its repository. Select the actual `moonraker.conf` for your printer when prompted, then review and confirm the plan. You do not need to choose another application directory or re-enter the repository for the normal clone.

If you already know the configuration path, supply it explicitly:

```sh
~/Klipper-firmware-auto-updater/kcm.sh moonraker-setup --config "$HOME/printer_data/config/moonraker.conf"
```

Use your real path for multiple/custom printer instances. The command backs up affected configuration under `DATA_ROOT/backups/moonraker`, writes `[update_manager klipper-component-manager]` directly into that selected main configuration, and adds the shared base update-manager section if needed. When an old KCM-managed include exists, it moves only the updater section and preserves that extra file, its includes and unrelated settings. New setup does not create another include file. It checks for conflicts or concurrent edits. Application source stays in the current clone; saved profiles and jobs stay in the separate KCM data directory. When using the helper, you do not need to paste the block manually.

After setup:

1. Exit KCM and finish any firmware operations and printing.
2. Restart the intended Moonraker service. A usual instance uses `sudo systemctl restart moonraker`; custom instances use their own service name. Hosts provisioned by KCM use `kcm-moonraker-PRINTER_ID`.
3. Refresh updates in Mainsail/Fluidd and use the **klipper-component-manager** entry.
4. Continue launching `~/Klipper-firmware-auto-updater/kcm.sh`.

Setup does not restart services itself. Close KCM before Moonraker changes its application files.

## Manual Git configuration

As an alternative to the helper, back up your existing `moonraker.conf` and paste this section directly into it once. Replace `YOUR_USER` with your Linux username or use your actual clone path. The shared `[update_manager]` section should also exist once; keep it if present or add it if missing.

```ini
[update_manager klipper-component-manager]
type: git_repo
channel: dev
path: /home/YOUR_USER/Klipper-firmware-auto-updater
origin: https://github.com/Darth-Kuni/Klipper-firmware-auto-updater.git
primary_branch: main
is_system_service: False
```

The helper writes this same configuration using the full path of the detected clone. `channel: dev` follows source commits on `main`, rather than GitHub release tags. It does not select the release-based `stable` channel. KCM remains alpha software while hardware validation is outstanding.

Keep the application checkout clean so Moonraker can update it. Review local changes before updating; do not use a hard-reset recovery action to discard work you want to keep. The KCM updater section must occur once across the main file and every loaded include. If you previously used `kcm-updater.conf`, migrate that old entry before adding a direct section. Exit KCM and restart the intended idle Moonraker instance after saving the configuration, then refresh updates.

See Moonraker's [Git repo configuration](https://moonraker.readthedocs.io/en/latest/configuration/#git-repo-configuration) for its updater settings.

## Manual application update

Exit KCM and finish firmware work, then run:

```sh
git -C ~/Klipper-firmware-auto-updater pull --ff-only
```

Launch the app with the same `kcm.sh` path afterwards. If Git reports local changes or divergent commits, inspect them before retrying. This command preserves work through Git's normal checks and does not hard-reset the checkout.

The default saved-data directory is `~/.local/share/klipper-component-manager`, or `$XDG_DATA_HOME/klipper-component-manager`. It is outside the clone. With a custom `--data-dir`, keep it outside the checkout and use the same global argument for setup and normal launches:

```sh
~/Klipper-firmware-auto-updater/kcm.sh --data-dir /path/to/kcm-data moonraker-setup
```

## Migrate an old kcm-updater.conf

For the automatic migration, exit KCM and update the clone, then rerun setup:

```sh
git -C ~/Klipper-firmware-auto-updater pull --ff-only
~/Klipper-firmware-auto-updater/kcm.sh moonraker-setup
```

Choose the same actual `moonraker.conf` used by your printer. The helper backs up affected main/include configuration and the old managed file, then moves only the recognized KCM updater section into the selected main file so it appears once. It preserves `kcm-updater.conf`, its existing include directives and all unrelated settings, leaving the extra file available for other configuration. Saved printer profiles stay where they are. Restart the correct idle Moonraker instance afterwards.

For a manual migration instead, back up the affected files and move only `[update_manager klipper-component-manager]` and its options from the existing extra file into `moonraker.conf`, using the direct block shown above. Keep the old file, its include directives and unrelated sections/settings intact. The KCM updater itself should occur once across all loaded files. Keep a shared `[update_manager]` section used by other components. This manual route replaces the helper migration.

## Migrate a release ZIP updater to the clone

Exit the old application, clone the repository using the [recommended installation](installation.md#recommended-clone-and-launch), and run the clone's `moonraker-setup` against the same Moonraker configuration.

The helper backs up affected files and places the Git updater for the current clone directly in the selected `moonraker.conf`, migrating its old managed include when present. It does not move or recreate printer profiles. Use the same Linux user and data-directory setting to retain them. A conflicting manually written updater section needs review before setup can continue.

Restart the correct idle Moonraker instance and launch the clone afterwards. The old standalone application directory is no longer the update target; retain it until you have checked the new setup and backups.

## Optional: release ZIP updates

This alternative remains available for users who prefer a single-file release installation. It follows normal GitHub releases, rather than the `main` source branch.

From the downloaded `.pyz`, specify a dedicated application directory and the real Moonraker config:

```sh
python3 ~/klipper-component-manager.pyz moonraker-setup --app-dir "$HOME/klipper-component-manager-app" --config "$HOME/printer_data/config/moonraker.conf"
```

`--app-dir` explicitly chooses ZIP mode. It is separate from the automatic Git-clone mode; do not supply it together with `--checkout`. The directory must be outside Git checkouts, saved printer data and Moonraker's configuration folder.

After confirming the plan, setup installs the standalone app and its release metadata there, backs up configuration and writes the updater section directly into the selected `moonraker.conf`. It migrates a recognized old managed include when present. Exit KCM, restart the correct idle Moonraker instance and refresh updates. Launch the managed copy afterwards:

```sh
python3 ~/klipper-component-manager-app/klipper-component-manager.pyz
```

The release-mode section in `moonraker.conf` resembles:

```ini
[update_manager klipper-component-manager]
type: zip
channel: stable
repo: Darth-Kuni/Klipper-firmware-auto-updater
path: /home/YOUR_USER/klipper-component-manager-app
is_system_service: False
```

The application directory contains `klipper-component-manager.pyz` and `release_info.json` at its root. The metadata identifies the repository, exact release tag and fixed asset `klipper-component-manager-moonraker.zip`. `asset_name` belongs in that JSON, not in the Moonraker configuration. The `stable` channel means ordinary GitHub releases; it does not establish physical printer testing.

For manual ZIP installation instead of the helper, download the [Moonraker release bundle](https://github.com/Darth-Kuni/Klipper-firmware-auto-updater/releases/latest/download/klipper-component-manager-moonraker.zip) and its matching checksum manifest from the same release. Verify the checksum, extract the two root files into an empty dedicated app directory, then back up and add the ZIP updater directly to your actual `moonraker.conf` once, with your real application path. Keep profiles outside that directory and avoid leaving a duplicate updater in an older include.

See Moonraker's [ZIP application configuration](https://moonraker.readthedocs.io/en/latest/configuration/#zip-application-configuration) and [release metadata format](https://moonraker.readthedocs.io/en/latest/configuration/#the-release_infojson-file).

## Troubleshooting, backups and removal

If the updater is missing, check the actual `moonraker.conf` and its single KCM updater section, restart the intended Moonraker service and refresh updates. For Git mode, confirm the configured path is the same clone you launch, its branch is `main`, its origin is correct and its tracked files are clean. For ZIP mode, check the two root files, exact release tag and fixed bundle asset. An old managed include can be migrated by rerunning the updated setup helper.

If the program appears unchanged after updating, verify that you launched the configured application copy. A separately downloaded `.pyz` or old app directory will not reflect updates to a Git clone.

Setup prints its backup directory under `DATA_ROOT/backups/moonraker`. To roll configuration back manually, inspect those backups and any subsequent edits, then restore only the affected files. Configuration backups do not roll back board firmware. No application-updater operation flashes a board.

To disable KCM application updates, back up `moonraker.conf` and remove `[update_manager klipper-component-manager]` from it. For an older include setup, remove only the old KCM updater section and ensure no loaded file still contains that updater. Preserve the extra file, its includes and unrelated settings. Keep the shared `[update_manager]` section when other components use it. Restart the correct idle instance. Saved KCM profiles and build jobs remain in their separate data directory.
