# Contributing

Klipper Component Manager is a standalone Linux terminal application. Keep the downloadable program usable over SSH without another installer, a desktop session, or a browser interface.

## Local checks

Use Python 3.10 or newer and Git. Run checks from the repository root:

```sh
python -m unittest discover -s tests
python -m kcm --version
python -m kcm --data-dir ./build/smoke-state boards EBB36
```

The tests mock package installation, network services, serial/CAN access, and firmware writes. Source-snapshot integration tests run real Git processes against temporary local repositories with only the file transport allowed. They do not contact a printer or run systemd. Some Windows sandboxes prevent Git from starting its helper processes; run those tests in a normal development shell or the hosted CI environment.

CI runs the suite on Linux with Python 3.10 and 3.12 and Windows with Python 3.12. Windows verifies portable application behavior and packaging; builds, setup actions, service management, and real firmware flashing run on a Linux printer host.

## Firmware changes and board references

Keep application and bootloader identities separate, preserve per-board build settings, and validate saved job checksums before writes. A cancelled prompt must stop the action and leave an accurate setup record. Tests must cover a meaningful failure or device-selection boundary for changes to flashing behavior.

For board catalog changes, cite the manufacturer's exact model/revision documentation or the official Klipper board configuration. Record the MCU variant and identify which values are references. Do not infer a board revision from its USB descriptor or CAN UUID, apply unverified firmware settings, or claim hardware testing from a successful mocked test.

Hardware reports should state the exact PCB revision, MCU marking, communication method, bootloader and application offsets, software commits, and recovery method. Remove passwords, tokens, public IP addresses, and personal paths from logs before sharing them. Keep any physical validation separate from the automated software results.

## Build downloads

Build the wheel first so its checksum is included in the final manifest. Replace `OWNER/REPOSITORY` with the destination GitHub repository:

```sh
python -m pip wheel --no-deps --wheel-dir dist .
python tools/build_release.py --repository OWNER/REPOSITORY
```

The builder creates a versioned standalone `.pyz`, an identical fixed-name `.pyz`, source ZIP, fixed-name Moonraker update ZIP, and `SHA256SUMS.txt`. The update bundle contains `klipper-component-manager.pyz` and `release_info.json`; its repository metadata must match the destination. The `.pyz` and wheel must include the bundled board catalog. Keep generated files, virtual environments, source checkouts, and printer data out of Git.

To smoke-test a downloaded program, run its `--version` and `boards EBB36` commands with `--data-dir` pointing to a temporary directory. These commands should not install software or touch hardware.

## Releases

1. Update `kcm/__init__.py` and `pyproject.toml` to the same `MAJOR.MINOR.PATCH` version. Update the README and validation notes for the behavior actually delivered.
2. Run the complete suite and review the generated downloads. Confirm which boards, if any, were physically tested.
3. Push a matching `vMAJOR.MINOR.PATCH` tag to the intended repository. The tag workflow validates both version files, runs the suite on Linux, builds and checks the packages, and publishes the assets with GitHub's repository-scoped Actions token.

The release workflow runs only for pushed version tags in a repository that is not a fork. It does not create repositories, tags, commits, or pull requests, and it does not execute printer setup or hardware operations. Pull-request CI has read-only repository permissions and receives no release token.

Current releases are titled `vMAJOR.MINOR.PATCH Alpha` and published as ordinary GitHub releases so a stable-channel Moonraker update entry can discover them. This distribution setting does not change the alpha testing status. Update the release title and documentation when the project is ready to change that status.

Updating KCM through Moonraker replaces the application package. Flashing printer firmware remains an explicit operation inside KCM using the saved component profiles.
