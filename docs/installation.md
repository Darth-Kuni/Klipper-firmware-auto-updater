# Installation details

## Recommended: clone and launch

SSH into the Linux printer host as your normal user. From your home directory (`cd ~` if needed):

```sh
git clone https://github.com/Darth-Kuni/Klipper-firmware-auto-updater.git
./Klipper-firmware-auto-updater/kcm.sh
```

On later visits:

```sh
~/Klipper-firmware-auto-updater/kcm.sh
```

The launcher checks for Python 3.10 or newer and runs the app from its own directory. It works when launched from another working directory and passes any command arguments to the program. Opening KCM needs no pip install, virtual environment or curl download. Firmware compilation and flashing tools are prepared separately through the guide.

If Git is missing on Debian/Ubuntu, install it and retry the clone:

```sh
sudo apt-get update
sudo apt-get install git
```

If Python is too old, use a supported Linux image with Python 3.10+. Do not replace the operating system's Python installation merely to run this app. Use a normal account; the guide asks for sudo when a chosen package or service action needs it.

The clone follows `main`. To update it manually, finish firmware work and close KCM first:

```sh
git -C ~/Klipper-firmware-auto-updater pull --ff-only
```

Review local changes or divergent commits if Git refuses the update. The update command does not hard-reset your checkout. For updates through Mainsail/Fluidd, choose the helper or manual section in the [Moonraker guide](moonraker.md). The helper command `~/Klipper-firmware-auto-updater/kcm.sh moonraker-setup` backs up configuration and writes the updater directly into your selected `moonraker.conf`.

## Start with your printer

Choose **First-time setup / setup guide**, or open it directly:

```sh
~/Klipper-firmware-auto-updater/kcm.sh setup
```

For a working printer, save a copy of its configuration, create its printer/component profiles and import known firmware build settings. Skip initial bootloader or new-service installation when those are already working. The guide can record existing Katapult installations, but the exact bootloader settings are still needed to check MCU/application-offset compatibility.

The [main setup instructions](../README.md#first-time-setup) cover board identification, Katapult installation, Klipper configuration/building, initial flashing, services and physical checks. Automatic host preparation targets Debian/Ubuntu with systemd; other distributions need their own package/service setup.

## Saved data

By default, profiles, settings, build jobs, setup records and logs are stored in `~/.local/share/klipper-component-manager`, or under `$XDG_DATA_HOME/klipper-component-manager` when that variable is set. Application files are in the Git checkout. Updating or switching application launch methods reuses the same saved data under the same Linux user and environment.

Keep a custom data directory outside the checkout and pass it on every launch:

```sh
~/Klipper-firmware-auto-updater/kcm.sh --data-dir /path/to/kcm-data
```

Back up that directory and the printer's configuration directory. Changing the application copy does not migrate data between different custom paths or Linux users.

## Optional: single-file download

Use this alternative when you prefer a release download over a Git checkout. The [latest release](https://github.com/Darth-Kuni/Klipper-firmware-auto-updater/releases/latest) provides `klipper-component-manager.pyz` and a matching `SHA256SUMS.txt`.

Download them on the Linux host, or download them elsewhere and copy them over SSH. With curl already available:

```sh
curl -fL https://github.com/Darth-Kuni/Klipper-firmware-auto-updater/releases/latest/download/klipper-component-manager.pyz -o klipper-component-manager.pyz
curl -fL https://github.com/Darth-Kuni/Klipper-firmware-auto-updater/releases/latest/download/SHA256SUMS.txt -o SHA256SUMS.txt
sha256sum --check --ignore-missing SHA256SUMS.txt
python3 ~/klipper-component-manager.pyz
```

Run those commands from your home directory and confirm the program reports `OK` in the checksum result. If a new release appears between downloads, get both files from the same tagged release page and check again. Python 3.10+ is still required.

To copy a program downloaded on another computer:

```sh
scp klipper-component-manager.pyz YOUR_USER@PRINTER_IP:~/
ssh YOUR_USER@PRINTER_IP
python3 ~/klipper-component-manager.pyz
```

Use your actual Linux account and printer hostname/IP. A `.pyz` bundles the app and board references; it does not bundle every Linux compiler or flashing tool. The guide prepares those when selected.

Existing single-file users can keep release-based updates. The [ZIP update instructions](moonraker.md#optional-release-zip-updates) explain the dedicated managed directory and `--app-dir` option.

## Optional: source ZIP or Python wheel

The release's source ZIP can be extracted and run with `python3 -m kcm` from the extracted directory. It has no `.git` history, so Git-based application updates require a real clone; use the recommended clone method to obtain one.

For a conventional Python package install, download the wheel from the [release page](https://github.com/Darth-Kuni/Klipper-firmware-auto-updater/releases/latest), check its checksum, then install it in a virtual environment. Replace the wheel placeholder with the actual downloaded filename:

```sh
python3 -m venv --system-site-packages ~/kcm-env
. ~/kcm-env/bin/activate
python3 -m pip install /path/to/downloaded/klipper_component_manager-VERSION-py3-none-any.whl
kcm
```

The `kcm` command in this example is provided by the wheel installation and its activated environment. The recommended Git launcher does not need that command. System-site-packages lets this environment use distribution-installed pyserial; an isolated custom environment needs pyserial installed into it before USB/UART flashing.

## Changing from a download to a clone

Exit the downloaded app, clone the repository using the recommended commands, and launch `kcm.sh` under the same Linux user with the same data-directory setting. Your saved profiles stay in the data directory and do not need copying into the clone.

If Moonraker was configured for the old ZIP application directory, run the cloned launcher's `moonraker-setup` command against the same real `moonraker.conf`. It backs up affected configuration and places the Git updater directly in that main file, migrating a recognized old managed include when present. Review any conflicting manually written updater section before proceeding. After restarting the correct idle Moonraker instance, use the clone's launcher for future visits.

For an existing Git installation with an old `kcm-updater.conf`, update the clone with the fast-forward command above, then rerun `moonraker-setup`. That migration backs up affected main/include files and the old managed file, then moves only the KCM updater section into `moonraker.conf`. It keeps the extra file, its includes and unrelated settings available and does not move saved profiles. Use this helper migration or the [manual migration procedure](moonraker.md#migrate-an-old-kcm-updaterconf), rather than adding a second copy of the updater.
