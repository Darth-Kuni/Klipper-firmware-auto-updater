"""First-install tests use fake command results and disposable workspace files."""
from contextlib import nullcontext
from dataclasses import asdict
import json
from pathlib import Path
import struct
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from kcm.bootstrap import Bootstrap, DEBIAN_PACKAGES
from kcm.engine import UpdateError, digest
from kcm.models import Component, Printer
from kcm.storage import Store, atomic_json


COMMIT = "a" * 40
STM32 = 'CONFIG_MACH_STM32=y\nCONFIG_MCU="stm32g0b1xx"\nCONFIG_FLASH_BOOT_ADDRESS=0x8000000\nCONFIG_FLASH_APPLICATION_ADDRESS=0x8000000\nCONFIG_LAUNCH_APP_ADDRESS=0x8002000\n# CONFIG_BUILD_DEPLOYER is not set\n'
RP2040 = 'CONFIG_MACH_RP2040=y\nCONFIG_MCU="rp2040"\nCONFIG_FLASH_BOOT_ADDRESS=0x10000100\nCONFIG_FLASH_APPLICATION_ADDRESS=0x10000100\nCONFIG_LAUNCH_APP_ADDRESS=0x10004000\n# CONFIG_BUILD_DEPLOYER is not set\n'


def uf2(family=0xE48BFF56):
    block = bytearray(512)
    struct.pack_into("<8I", block, 0, 0x0A324655, 0x9E5D5157, 0x2000, 0x10000000, 256, 0, 1, family)
    struct.pack_into("<I", block, 508, 0x0AB16F30)
    return bytes(block)


class FakeRunner:
    def __init__(self):
        self.log = None
        self.calls = []
        self.active = False
        self.fail_write = False
        self.query = "Detected UUID: aabbccddeeff, Application: Katapult\n"
        self.menu_config = STM32

    def run(self, argv, **kwargs):
        argv = list(argv)
        self.calls.append((argv, kwargs))
        result = subprocess.CompletedProcess(argv, 0, stdout="")
        if argv[:2] == ["systemctl", "is-active"]:
            result.returncode = 0 if self.active else 3
            result.stdout = "active\n" if self.active else "inactive\n"
        elif argv[:2] == ["make", "menuconfig"]:
            (Path(kwargs["cwd"]) / ".config").write_text(self.menu_config, encoding="utf-8")
        elif argv[:2] == ["make", "-j"]:
            output = Path(kwargs["cwd"]) / "out"
            output.mkdir()
            (output / "katapult.bin").write_bytes(b"compiled bootloader")
        elif "-q" in argv:
            result.stdout = self.query
        if self.fail_write and ("-D" in argv or "-f" in argv):
            raise UpdateError("simulated write failure")
        return result


class BootstrapTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parent)
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.store = Store(self.root / "data")
        self.runner = FakeRunner()
        self.messages = []
        self.bootstrap = Bootstrap(self.store, self.messages.append, self.runner)
        self.printer = Printer("printer", "Test printer", "/home/pi/klipper", "/home/pi/katapult")
        self.board = Component("toolhead", "Toolhead board", str(self.store.config_path("printer", "toolhead")), "can")
        for active_patch in (patch.object(self.bootstrap, "_require_linux"),
                             patch.object(self.bootstrap, "_lock", side_effect=lambda: nullcontext())):
            active_patch.start()
            self.addCleanup(active_patch.stop)

    def boot_job(self, config=STM32, artifact_name="katapult.bin", artifact=b"bootloader"):
        job, manifest = self.bootstrap._new_job(self.printer, self.board, "katapult")
        self.bootstrap._config_path(self.printer, self.board).write_text(config, encoding="utf-8")
        (job / "katapult.config").write_text(config, encoding="utf-8")
        (job / artifact_name).write_bytes(artifact)
        manifest.update(state="ready", source_commit=COMMIT, source_path=self.printer.katapult_path,
                        component=asdict(self.board), config_sha256=digest(job / "katapult.config"),
                        artifacts={artifact_name: digest(job / artifact_name)})
        atomic_json(job / "manifest.json", manifest)
        return job

    @staticmethod
    def manifest(job):
        return json.loads((job / "manifest.json").read_text(encoding="utf-8"))

    def hardware_writes(self):
        return [argv for argv, _ in self.runner.calls if "-D" in argv or "-f" in argv]

    def initial_job(self):
        self.board.can_uuid = "112233445566"
        self.printer.components = [self.board]
        Path(self.board.config_file).write_text('CONFIG_MCU="stm32g0b1xx"\nCONFIG_FLASH_APPLICATION_ADDRESS=0x8002000\n', encoding="utf-8")
        self.bootstrap._config_path(self.printer, self.board).write_text(STM32, encoding="utf-8")
        job = self.store.root / "jobs/printer/initial-build"
        binary = job / "toolhead/out/klipper.bin"
        binary.parent.mkdir(parents=True)
        binary.write_bytes(b"initial Klipper firmware")
        manifest = {"schema_version": 1, "state": "ready", "printer": self.printer.to_dict(),
                    "commit": COMMIT, "host_start_commit": COMMIT,
                    "components": [{"id": "toolhead", "artifact": "toolhead/out/klipper.bin",
                                     "sha256": digest(binary), "config_sha256": digest(Path(self.board.config_file)), "state": "built"}]}
        atomic_json(job / "manifest.json", manifest)
        return job, binary

    def test_host_inventory_does_not_execute_commands_or_probe_can(self):
        self.bootstrap.inspect_host()
        self.assertEqual(self.runner.calls, [])

    def test_prepare_sources_only_clones_official_missing_destinations_after_confirmation(self):
        klipper, katapult = self.root / "klipper", self.root / "katapult"
        proposals = []
        self.bootstrap.prepare_sources(klipper, katapult, lambda text: proposals.append(text) or True)
        self.assertEqual(len(proposals), 1)
        self.assertIn(str(klipper), proposals[0])
        commands = [argv for argv, _ in self.runner.calls]
        self.assertEqual(commands, [["git", "clone", "--", "https://github.com/Klipper3d/klipper.git", str(klipper)],
                                    ["git", "clone", "--", "https://github.com/Arksine/katapult.git", str(katapult)]])

    def test_source_download_cancellation_executes_nothing(self):
        with self.assertRaisesRegex(UpdateError, "cancelled"):
            self.bootstrap.prepare_sources(self.root / "klipper", self.root / "katapult", lambda _: False)
        self.assertEqual(self.runner.calls, [])

    def test_existing_noncheckout_or_nested_source_destinations_are_not_overwritten(self):
        occupied = self.root / "occupied"
        occupied.mkdir()
        (occupied / "user-file").write_text("keep", encoding="utf-8")
        with self.assertRaises(UpdateError):
            self.bootstrap.prepare_sources(occupied, self.root / "katapult", lambda _: True)
        with self.assertRaises(UpdateError):
            self.bootstrap.prepare_sources(occupied, occupied / "nested", lambda _: True)
        self.assertEqual((occupied / "user-file").read_text(), "keep")
        self.assertEqual(self.runner.calls, [])

    def test_dependency_install_is_vetted_scoped_and_uses_sudo_authorization_first(self):
        with patch.object(self.bootstrap, "_os_release", return_value={"ID": "debian"}), patch("kcm.bootstrap.shutil.which", return_value="/usr/bin/apt-get"):
            self.bootstrap.install_dependencies(lambda _: True)
        commands = [argv for argv, _ in self.runner.calls]
        self.assertEqual(commands[0], ["sudo", "-v"])
        self.assertEqual(commands[1], ["sudo", "-n", "apt-get", "update"])
        self.assertEqual(commands[2], ["sudo", "-n", "apt-get", "install", "--yes", *DEBIAN_PACKAGES])
        self.assertFalse(any("upgrade" in argv for argv in commands))

    def test_unsupported_distribution_does_not_guess_package_commands(self):
        with patch.object(self.bootstrap, "_os_release", return_value={"ID": "arch"}), self.assertRaises(UpdateError):
            self.bootstrap.install_dependencies(lambda _: True)
        self.assertEqual(self.runner.calls, [])

    def test_pending_can_board_can_be_configured_before_a_uuid_exists(self):
        def snapshot(repo, destination):
            destination.mkdir()
            return COMMIT
        with patch.object(self.bootstrap, "_snapshot", side_effect=snapshot):
            destination = self.bootstrap.configure_katapult(self.printer, self.board)
        self.assertEqual(self.board.can_uuid, "")
        self.assertEqual(destination.read_text(), STM32)

    def test_deployer_configuration_is_not_saved_as_a_generic_bootloader(self):
        self.runner.menu_config = STM32.replace("# CONFIG_BUILD_DEPLOYER is not set", "CONFIG_BUILD_DEPLOYER=y")
        def snapshot(repo, destination):
            destination.mkdir()
            return COMMIT
        with patch.object(self.bootstrap, "_snapshot", side_effect=snapshot), self.assertRaisesRegex(UpdateError, "deployer"):
            self.bootstrap.configure_katapult(self.printer, self.board)
        self.assertFalse(self.bootstrap._config_path(self.printer, self.board).exists())

    def test_bootloader_build_saves_config_commit_and_checked_artifact(self):
        self.bootstrap._config_path(self.printer, self.board).write_text(STM32, encoding="utf-8")
        def snapshot(repo, destination):
            destination.mkdir()
            return COMMIT
        with patch.object(self.bootstrap, "_snapshot", side_effect=snapshot):
            job = self.bootstrap.build_katapult(self.printer, self.board)
        manifest = self.manifest(job)
        self.assertEqual(manifest["state"], "ready")
        self.assertEqual(manifest["source_commit"], COMMIT)
        self.assertEqual(manifest["artifacts"]["katapult.bin"], digest(job / "katapult.bin"))
        self.assertEqual((job / "katapult.config").read_text(), STM32)
        self.assertEqual(self.hardware_writes(), [])

    def test_tampered_bootloader_blocks_before_any_subprocess(self):
        job = self.boot_job()
        (job / "katapult.bin").write_bytes(b"swapped image")
        with self.assertRaises(UpdateError):
            self.bootstrap.install_katapult(self.printer, self.board, job, "dfu", "ABC123", lambda _: True)
        self.assertEqual(self.runner.calls, [])

    def test_wrong_board_job_and_changed_profile_are_rejected(self):
        job = self.boot_job()
        other = Component("other", "Other", self.board.config_file, "can")
        with self.assertRaises(UpdateError):
            self.bootstrap.install_katapult(self.printer, other, job, "dfu", "ABC123", lambda _: True)
        self.bootstrap._config_path(self.printer, self.board).write_text(RP2040, encoding="utf-8")
        with self.assertRaises(UpdateError):
            self.bootstrap.install_katapult(self.printer, self.board, job, "dfu", "ABC123", lambda _: True)
        self.assertEqual(self.runner.calls, [])

    def test_active_service_prevents_initial_bootloader_write(self):
        job = self.boot_job()
        self.runner.active = True
        with self.assertRaisesRegex(UpdateError, "stopped"):
            self.bootstrap.install_katapult(self.printer, self.board, job, "dfu", "ABC123", lambda _: True)
        self.assertEqual(self.hardware_writes(), [])
        self.assertFalse(any(argv[0] == "sudo" for argv, _ in self.runner.calls))
        self.assertEqual(self.manifest(job)["state"], "ready")

    def test_dfu_is_processor_specific_and_requires_exact_serial(self):
        job = self.boot_job(RP2040)
        with self.assertRaisesRegex(UpdateError, "STM32"):
            self.bootstrap.install_katapult(self.printer, self.board, job, "dfu", "ABC123", lambda _: True)
        job = self.boot_job()
        for serial in ("0483:df11", "*", "", "--all"):
            with self.subTest(serial=serial), self.assertRaises(UpdateError):
                self.bootstrap.install_katapult(self.printer, self.board, job, "dfu", serial, lambda _: True)
        self.assertEqual(self.runner.calls, [])

    def test_dfu_uses_exact_serial_without_mass_erase_and_keeps_service_stopped(self):
        job = self.boot_job()
        self.bootstrap.install_katapult(self.printer, self.board, job, "dfu", "ABC123", lambda _: True)
        commands = [argv for argv, _ in self.runner.calls]
        self.assertIn(["sudo", "-v"], commands)
        expected = ["sudo", "-n", "dfu-util", "-S", "ABC123", "-a", "0", "-s", "0x08000000:leave", "-D", str(job / "katapult.bin")]
        self.assertIn(expected, commands)
        self.assertTrue(self.runner.calls[commands.index(expected)][1]["protect_write"])
        self.assertFalse(any("systemctl" in argv and "start" in argv for argv in commands))
        self.assertEqual(self.manifest(job)["state"], "installed_unverified")

    def test_declined_physical_preparation_never_writes_or_marks_installed(self):
        job = self.boot_job()
        with self.assertRaisesRegex(UpdateError, "cancelled"):
            self.bootstrap.install_katapult(self.printer, self.board, job, "dfu", "ABC123", lambda _: False)
        self.assertEqual(self.hardware_writes(), [])
        self.assertEqual(self.manifest(job)["state"], "ready")

    def test_failed_write_retains_failed_journal_without_service_restart(self):
        job = self.boot_job()
        self.runner.fail_write = True
        with self.assertRaisesRegex(UpdateError, "write failure"):
            self.bootstrap.install_katapult(self.printer, self.board, job, "dfu", "ABC123", lambda _: True)
        self.assertEqual(self.manifest(job)["state"], "failed")
        self.assertIn("write failure", self.manifest(job)["error"])
        self.assertFalse(any("start" in argv for argv, _ in self.runner.calls))

    def test_manual_and_unknown_install_methods_never_execute(self):
        for method in ("sdcard", "swd", "uart", "deployer", "unknown"):
            with self.subTest(method=method), self.assertRaisesRegex(UpdateError, "manual walkthrough"):
                self.bootstrap.install_katapult(self.printer, self.board, self.root, method, "", lambda _: True)
        self.assertEqual(self.runner.calls, [])

    def test_uf2_requires_real_boot_mount_and_matching_processor(self):
        job = self.boot_job(RP2040, "katapult.uf2", uf2())
        volume = self.root / "boot-volume"
        volume.mkdir()
        (volume / "INFO_UF2.TXT").write_text("Board-ID: RP2350", encoding="utf-8")
        with patch.object(Path, "is_mount", return_value=True), self.assertRaises(UpdateError):
            self.bootstrap.install_katapult(self.printer, self.board, job, "uf2", str(volume), lambda _: True)
        (volume / "INFO_UF2.TXT").write_text("Board-ID: RPI-RP2", encoding="utf-8")
        with patch.object(Path, "is_mount", return_value=False), self.assertRaises(UpdateError):
            self.bootstrap.install_katapult(self.printer, self.board, job, "uf2", str(volume), lambda _: True)
        self.assertFalse((volume / "katapult.uf2").exists())
        self.assertEqual(self.runner.calls, [])

    def test_uf2_family_mismatch_is_blocked_before_copy(self):
        job = self.boot_job(RP2040, "katapult.uf2", uf2(0xE48BFF59))
        volume = self.root / "boot-volume"
        volume.mkdir()
        (volume / "INFO_UF2.TXT").write_text("Board-ID: RPI-RP2", encoding="utf-8")
        with patch.object(Path, "is_mount", return_value=True), self.assertRaisesRegex(UpdateError, "family"):
            self.bootstrap.install_katapult(self.printer, self.board, job, "uf2", str(volume), lambda _: True)
        self.assertFalse((volume / "katapult.uf2").exists())

    def test_verified_uf2_is_copied_only_to_confirmed_boot_volume(self):
        image = uf2()
        job = self.boot_job(RP2040, "katapult.uf2", image)
        volume = self.root / "boot-volume"
        volume.mkdir()
        (volume / "INFO_UF2.TXT").write_text("Board-ID: RPI-RP2", encoding="utf-8")
        with patch.object(Path, "is_mount", return_value=True):
            self.bootstrap.install_katapult(self.printer, self.board, job, "uf2", str(volume), lambda _: True)
        self.assertEqual((volume / "katapult.uf2").read_bytes(), image)
        self.assertEqual(self.manifest(job)["state"], "installed_unverified")
        self.assertEqual(self.hardware_writes(), [])

    def test_first_klipper_flash_requires_a_validated_build_and_never_requests_reboot(self):
        job, binary = self.initial_job()
        with patch.object(self.bootstrap.manager, "_git", return_value=COMMIT), patch.object(Path, "is_file", return_value=True):
            self.bootstrap.first_klipper_flash(self.printer, self.board, job, lambda _: True)
        writes = self.hardware_writes()
        self.assertEqual(len(writes), 1)
        self.assertIn(str(binary), writes[0])
        self.assertNotIn("-r", writes[0])
        journals = list((self.store.root / "bootstrap").glob("*/*/*/manifest.json"))
        self.assertEqual(json.loads(journals[0].read_text())["state"], "flashed_unverified")
        self.assertFalse(any("start" in argv for argv, _ in self.runner.calls))

    def test_first_klipper_flash_rejects_arbitrary_or_tampered_binary(self):
        job, binary = self.initial_job()
        binary.write_bytes(b"tampered")
        with self.assertRaises(UpdateError):
            self.bootstrap.first_klipper_flash(self.printer, self.board, binary, lambda _: True)
        with self.assertRaises(UpdateError):
            self.bootstrap.first_klipper_flash(self.printer, self.board, self.root / "unknown.bin", lambda _: True)
        self.assertEqual(self.runner.calls, [])

    def test_first_klipper_flash_rejects_host_mcu_source_mismatch(self):
        job, _ = self.initial_job()
        with patch.object(self.bootstrap.manager, "_git", return_value="b" * 40), self.assertRaisesRegex(UpdateError, "host and MCU"):
            self.bootstrap.first_klipper_flash(self.printer, self.board, job, lambda _: True)
        self.assertEqual(self.hardware_writes(), [])

    def test_first_klipper_flash_rejects_wrong_mcu_or_launch_address(self):
        job, _ = self.initial_job()
        for config in (STM32.replace('"stm32g0b1xx"', '"stm32f446xx"'),
                       STM32.replace("CONFIG_LAUNCH_APP_ADDRESS=0x8002000", "CONFIG_LAUNCH_APP_ADDRESS=0x8004000")):
            self.bootstrap._config_path(self.printer, self.board).write_text(config, encoding="utf-8")
            with self.subTest(config=config), patch.object(self.bootstrap.manager, "_git", return_value=COMMIT), self.assertRaises(UpdateError):
                self.bootstrap.first_klipper_flash(self.printer, self.board, job, lambda _: True)
        self.assertEqual(self.hardware_writes(), [])

    def test_first_flash_rejects_settings_changed_after_bootloader_installation(self):
        job, _ = self.initial_job()
        record = self.store.root / "bootstrap" / self.printer.id / self.board.id / "installed-bootloader/manifest.json"
        atomic_json(record, {"kind": "katapult", "state": "installed_unverified", "printer_id": self.printer.id,
                             "component_id": self.board.id, "config_sha256": "0" * 64, "finished": 1})
        with patch.object(self.bootstrap.manager, "_git", return_value=COMMIT), self.assertRaisesRegex(UpdateError, "after its recorded installation"):
            self.bootstrap.first_klipper_flash(self.printer, self.board, job, lambda _: True)
        self.assertEqual(self.hardware_writes(), [])

    def test_deployer_origin_cannot_be_written_at_boot_base_even_if_flag_missing(self):
        config = STM32.replace("CONFIG_FLASH_APPLICATION_ADDRESS=0x8000000", "CONFIG_FLASH_APPLICATION_ADDRESS=0x8002000")
        job = self.boot_job(config)
        with self.assertRaisesRegex(UpdateError, "deployer"):
            self.bootstrap.install_katapult(self.printer, self.board, job, "dfu", "ABC123", lambda _: True)
        self.assertEqual(self.runner.calls, [])

    def test_can_discovery_requires_physical_isolation_confirmation(self):
        with self.assertRaisesRegex(UpdateError, "cancelled"):
            self.bootstrap.find_can_uuid(self.printer, "can0", lambda _: False)
        self.assertFalse(any("-q" in argv for argv, _ in self.runner.calls))

    def test_can_discovery_parses_current_and_legacy_katapult_output_only(self):
        self.runner.query = ("Detected UUID: AABBCCDDEEFF, Application: Katapult\n"
                             "Detected UUID: 000000000001, Application: Klipper\n"
                             "Found canbus_uuid=112233445566, Application: CanBoot\n")
        with patch.object(Path, "is_file", return_value=True):
            found = self.bootstrap.find_can_uuid(self.printer, "can0", lambda _: True)
        self.assertEqual(found, ["112233445566", "aabbccddeeff"])
        self.assertEqual(self.hardware_writes(), [])
        self.assertTrue(list((self.store.root / "setup").glob("*/manifest.json")))


if __name__ == "__main__":
    unittest.main()
