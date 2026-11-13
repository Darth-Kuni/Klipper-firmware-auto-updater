"""Regression tests for update integrity and hardware-operation boundaries.

No test opens a serial/CAN device, contacts a printer, or invokes a subprocess.
"""

from __future__ import annotations

from contextlib import nullcontext
from dataclasses import replace
import json
import math
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from kcm.engine import Manager, UpdateError, config_changes, digest, flash_commands
from kcm.models import Component, Printer
from kcm.storage import Store, atomic_json


START = "a" * 40
TARGET = "b" * 40


class RecordingRunner:
    def __init__(self, handler=None):
        self.log = None
        self.calls = []
        self.handler = handler

    def run(self, argv, **kwargs):
        argv = list(argv)
        self.calls.append((argv, kwargs))
        if self.handler:
            result = self.handler(argv, kwargs)
            if result is not None:
                return result
        return subprocess.CompletedProcess(argv, 0, stdout="")


def can_component(component_id="toolhead", uuid="112233445566", **kwargs):
    values = dict(id=component_id, name=component_id, config_file="/profiles/board.config",
                  transport="can", can_interface="can1", can_uuid=uuid)
    values.update(kwargs)
    return Component(**values)


def profile(*components):
    return Printer("printer", "My printer", "/home/pi/klipper", "/home/pi/katapult",
                   components=list(components))


class CommandTests(unittest.TestCase):
    def setUp(self):
        self.printer = profile()
        self.firmware = Path("/saved/job/toolhead/out/klipper.bin")
        self.tool = ["python-test", str(Path("/home/pi/katapult") / "scripts/flashtool.py")]

    def test_can_request_and_write_are_bound_to_saved_uuid_and_interface(self):
        request, write = flash_commands(self.printer, can_component(), self.firmware, "python-test")
        self.assertEqual(request, self.tool + ["-i", "can1", "-u", "112233445566", "-r"])
        self.assertEqual(write, self.tool + ["-i", "can1", "-u", "112233445566", "-f", str(self.firmware)])

    def test_usb_request_uses_application_identity_but_write_uses_bootloader_identity(self):
        component = Component("main", "Main", "/main.config", "usb",
                              application_device="/dev/serial/by-id/usb-Klipper_board",
                              flash_device="/dev/serial/by-id/usb-Katapult_board")
        request, write = flash_commands(self.printer, component, self.firmware, "python-test")
        self.assertEqual(request, self.tool + ["-d", component.application_device, "-b", "250000", "-r"])
        self.assertEqual(write, self.tool + ["-d", component.flash_device, "-b", "250000", "-f", str(self.firmware)])

    def test_uart_uses_distinct_saved_application_and_bootloader_baud_rates(self):
        component = Component("main", "UART board", "/main.config", "uart",
                              application_device="/dev/ttyAMA0", flash_device="/dev/ttyAMA0",
                              application_baud=250000, baud=115200)
        request, write = flash_commands(self.printer, component, self.firmware, "python-test")
        self.assertEqual(request, self.tool + ["-d", "/dev/ttyAMA0", "-b", "250000", "-r"])
        self.assertEqual(write, self.tool + ["-d", "/dev/ttyAMA0", "-b", "115200", "-f", str(self.firmware)])

    def test_bridge_enters_bootloader_over_can_then_flashes_over_usb(self):
        bridge = Component("bridge", "CAN bridge", "/bridge.config", "usb",
                           can_interface="can0", can_uuid="aabbccddeeff",
                           flash_device="/dev/serial/by-id/usb-Katapult_bridge")
        request, write = flash_commands(self.printer, bridge, self.firmware, "python-test")
        self.assertEqual(request, self.tool + ["-i", "can0", "-u", "aabbccddeeff", "-r"])
        self.assertEqual(write, self.tool + ["-d", bridge.flash_device, "-b", "250000", "-f", str(self.firmware)])

    def test_manual_or_already_bootloader_never_sends_a_reboot_request(self):
        for entry in ("manual", "already"):
            with self.subTest(entry=entry):
                request, write = flash_commands(self.printer, can_component(bootloader_entry=entry),
                                                self.firmware, "python-test")
                self.assertIsNone(request)
                self.assertNotIn("-r", write)


class ConfigMigrationTests(unittest.TestCase):
    def test_comments_and_order_do_not_require_hardware_settings_review(self):
        before = '# Old heading\nCONFIG_MCU="stm32g0b1xx"\n# CONFIG_USB is not set\nCONFIG_CANBUS=y\n'
        after = '# New generated heading\nCONFIG_CANBUS=y\nCONFIG_USB=n\nCONFIG_MCU="stm32g0b1xx"\n'
        self.assertEqual(config_changes(before, after), [])

    def test_changed_removed_and_new_options_are_all_reported(self):
        changes = config_changes("CONFIG_OFFSET=8192\nCONFIG_OLD=y\n", "CONFIG_OFFSET=16384\nCONFIG_NEW=y\n")
        self.assertEqual(changes, ["CONFIG_NEW: <absent> -> y", "CONFIG_OFFSET: 8192 -> 16384",
                                   "CONFIG_OLD: y -> <absent>"])


class WorkspaceTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parent)
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.store = Store(self.root / "data")
        self.runner = RecordingRunner()
        self.manager = Manager(self.store, output=lambda _: None, runner=self.runner)
        network_guard = patch.object(self.manager, "_moonraker", side_effect=AssertionError("Unexpected live network access in test"))
        network_guard.start()
        self.addCleanup(network_guard.stop)
        config_a = self.store.config_path("printer", "toolhead")
        config_a.write_text("CONFIG_MCU=\"stm32g0b1xx\"\nCONFIG_CANBUS=y\n", encoding="utf-8")
        config_b = self.store.config_path("printer", "main")
        config_b.write_text("CONFIG_MCU=\"stm32f446xx\"\nCONFIG_USBSERIAL=y\n", encoding="utf-8")
        self.printer = profile(can_component(config_file=str(config_a)),
                               can_component("main", "aabbccddeeff", config_file=str(config_b)))

    def ready_job(self):
        job = self.store.root / "jobs" / self.printer.id / "test-job"
        job.mkdir(parents=True)
        records = []
        for component in self.printer.ordered_components():
            relative = f"{component.id}/out/klipper.bin"
            artifact = job / relative
            artifact.parent.mkdir(parents=True)
            artifact.write_bytes(("firmware for " + component.id).encode())
            records.append({"id": component.id, "artifact": relative, "sha256": digest(artifact),
                            "config_sha256": digest(Path(component.config_file)), "state": "built"})
        manifest = {"schema_version": 1, "state": "ready", "printer": self.printer.to_dict(),
                    "commit": TARGET, "host_start_commit": START, "components": records}
        atomic_json(job / "manifest.json", manifest)
        return job, manifest

    @staticmethod
    def read_manifest(job):
        return json.loads((job / "manifest.json").read_text(encoding="utf-8"))


class JobIntegrityTests(WorkspaceTests):
    def test_complete_unchanged_job_is_accepted(self):
        job, expected = self.ready_job()
        self.assertEqual(self.manager._check_job(self.printer, job), expected)

    def test_missing_component_prevents_flashing(self):
        job, manifest = self.ready_job()
        manifest["components"].pop()
        atomic_json(job / "manifest.json", manifest)
        with self.assertRaises(UpdateError):
            self.manager._check_job(self.printer, job)

    def test_swapped_artifacts_prevent_flashing_even_with_matching_record_hashes(self):
        job, manifest = self.ready_job()
        first, second = manifest["components"]
        first["artifact"], second["artifact"] = second["artifact"], first["artifact"]
        first["sha256"], second["sha256"] = second["sha256"], first["sha256"]
        atomic_json(job / "manifest.json", manifest)
        with self.assertRaises(UpdateError):
            self.manager._check_job(self.printer, job)

    def test_saved_config_change_invalidates_previously_built_firmware(self):
        job, _ = self.ready_job()
        Path(self.printer.components[0].config_file).write_text("CONFIG_CANBUS=n\n", encoding="utf-8")
        with self.assertRaises(UpdateError):
            self.manager._check_job(self.printer, job)

    def test_firmware_change_invalidates_job(self):
        job, manifest = self.ready_job()
        (job / manifest["components"][0]["artifact"]).write_bytes(b"unexpected firmware")
        with self.assertRaises(UpdateError):
            self.manager._check_job(self.printer, job)

    def test_changed_board_identity_invalidates_job(self):
        job, _ = self.ready_job()
        self.printer.components[0].can_uuid = "000000000001"
        with self.assertRaises(UpdateError):
            self.manager._check_job(self.printer, job)

    def test_failed_or_already_used_jobs_cannot_be_reflashed_automatically(self):
        job, manifest = self.ready_job()
        for state in ("building", "build_failed", "failed", "complete", "updating"):
            with self.subTest(state=state):
                manifest["state"] = state
                atomic_json(job / "manifest.json", manifest)
                with self.assertRaises(UpdateError):
                    self.manager._check_job(self.printer, job)

    def test_job_from_another_printer_directory_is_refused(self):
        job, _ = self.ready_job()
        other = replace(self.printer, id="other")
        with self.assertRaises(UpdateError):
            self.manager._check_job(other, job)


class IdleStateTests(WorkspaceTests):
    def moonraker(self, state="standby", heater=None, inventory=None, info="ready"):
        heater = {"target": 0, "temperature": 25} if heater is None else heater
        inventory = ["extruder"] if inventory is None else inventory

        def response(printer, endpoint):
            if endpoint == "/printer/info":
                return {"state": info}
            if endpoint == "/printer/objects/list":
                return {"objects": ["print_stats", "heaters", *inventory]}
            if endpoint == "/printer/objects/query?print_stats&heaters":
                return {"status": {"print_stats": {"state": state},
                                    "heaters": {"available_heaters": inventory}}}
            return {"status": {"extruder": heater}}
        return response

    def test_idle_cool_printer_passes_without_hardware_commands(self):
        with patch.object(self.manager, "_moonraker", side_effect=self.moonraker()):
            self.manager._idle(self.printer)
        self.assertEqual(self.runner.calls, [])

    def test_printing_paused_or_unknown_print_state_is_blocked(self):
        for state in ("printing", "paused", None, "unexpected"):
            with self.subTest(state=state), patch.object(self.manager, "_moonraker", side_effect=self.moonraker(state=state)):
                with self.assertRaises(UpdateError):
                    self.manager._idle(self.printer)

    def test_disconnected_or_not_ready_printer_is_blocked(self):
        for state in ("shutdown", "error", "startup", None):
            with self.subTest(state=state), patch.object(self.manager, "_moonraker", side_effect=self.moonraker(info=state)):
                with self.assertRaises(UpdateError):
                    self.manager._idle(self.printer)

    def test_active_hot_missing_or_nonfinite_heater_state_is_blocked(self):
        unsafe = ({"target": 200, "temperature": 25}, {"target": 0, "temperature": 80},
                  {}, {"target": 0, "temperature": "25"}, {"target": 0, "temperature": math.nan},
                  {"target": False, "temperature": 25}, {"target": 0, "temperature": True})
        for heater in unsafe:
            with self.subTest(heater=heater), patch.object(self.manager, "_moonraker", side_effect=self.moonraker(heater=heater)):
                with self.assertRaises(UpdateError):
                    self.manager._idle(self.printer)


class ActiveIdentityTests(WorkspaceTests):
    def test_service_pid_and_source_must_match_the_checked_printer(self):
        self.runner.handler = lambda argv, kwargs: subprocess.CompletedProcess(argv, 0, stdout="1234\n")
        good = {"klipper_path": self.printer.klipper_path, "process_id": 1234}
        with patch.object(self.manager, "_moonraker", return_value=good):
            self.manager._verify_service(self.printer)
        for changes in ({"process_id": 5678}, {"process_id": True}, {"klipper_path": "/other/klipper"}):
            with self.subTest(changes=changes), patch.object(self.manager, "_moonraker", return_value=good | changes):
                with self.assertRaises(UpdateError):
                    self.manager._verify_service(self.printer)

    def test_nonlocal_moonraker_url_cannot_authorize_local_service_updates(self):
        with self.assertRaisesRegex(UpdateError, "loopback"):
            self.manager._verify_service(replace(self.printer, moonraker_url="http://printer-other:7125"))
        self.assertEqual(self.runner.calls, [])

    def test_active_can_inventory_must_match_every_saved_board(self):
        config = {"mcu": {"canbus_uuid": "112233445566", "canbus_interface": "can1"},
                  "mcu main": {"canbus_uuid": "AABBCCDDEEFF", "canbus_interface": "can1"},
                  "printer": {"kinematics": "corexy"}}
        with patch.object(self.manager, "_moonraker", return_value={"status": {"configfile": {"config": config}}}):
            self.manager._verify_components(self.printer)
        for incomplete in ({"mcu": config["mcu"]}, config | {"mcu extra": {"canbus_uuid": "000000000001"}},
                           config | {"mcu": {"canbus_uuid": "112233445566", "canbus_interface": "can0"}}):
            with self.subTest(config=incomplete), patch.object(self.manager, "_moonraker",
                    return_value={"status": {"configfile": {"config": incomplete}}}):
                with self.assertRaises(UpdateError):
                    self.manager._verify_components(self.printer)

    def test_unavailable_active_inventory_cannot_authorize_update(self):
        for payload in ({}, {"status": {}}, {"status": {"configfile": {"config": {}}}}):
            with self.subTest(payload=payload), patch.object(self.manager, "_moonraker", return_value=payload):
                with self.assertRaises(UpdateError):
                    self.manager._verify_components(self.printer)


class FlashLifecycleTests(WorkspaceTests):
    moonraker = IdleStateTests.moonraker

    def setUp(self):
        super().setUp()
        self.job, self.manifest = self.ready_job()
        self.patches = [
            patch.object(self.manager, "_require_linux"),
            patch.object(self.manager, "_lock", side_effect=lambda: nullcontext()),
            patch.object(self.manager, "_validate_source"),
            patch.object(self.manager, "_verify_service"),
            patch.object(self.manager, "_verify_components"),
            patch.object(self.manager, "_check_job", return_value=self.manifest),
            patch.object(self.manager, "_git", side_effect=self.git),
            patch.object(self.manager, "_backup_config"),
            patch.object(self.manager, "_wait_device"),
            patch.object(self.manager, "_ready"),
            patch.object(Path, "is_file", return_value=True),
        ]
        for active_patch in self.patches:
            active_patch.start()
            self.addCleanup(active_patch.stop)

    @staticmethod
    def git(path, *args, **kwargs):
        return START if args[:1] == ("rev-parse",) else ""

    def service_actions(self):
        return [argv[3] for argv, _ in self.runner.calls if argv[:3] == ["sudo", "-n", "systemctl"]]

    def writes(self):
        return [(argv, kwargs) for argv, kwargs in self.runner.calls if "-f" in argv]

    def test_busy_printer_never_stops_service_or_writes_firmware(self):
        for state in ("printing", "paused", None):
            self.runner.calls.clear()
            with self.subTest(state=state), patch.object(self.manager, "_moonraker", side_effect=self.moonraker(state=state)):
                with self.assertRaises(UpdateError):
                    self.manager.flash(self.printer, self.job, lambda _: True)
            self.assertEqual(self.service_actions(), [])
            self.assertEqual(self.writes(), [])

    def test_user_cancellation_leaves_service_and_ready_job_untouched(self):
        with patch.object(self.manager, "_idle"):
            self.manager.flash(self.printer, self.job, lambda _: False)
        self.assertEqual(self.service_actions(), [])
        self.assertEqual(self.writes(), [])
        self.assertEqual(self.read_manifest(self.job)["state"], "ready")

    def test_unknown_or_hot_heater_blocks_before_service_stop_or_firmware_write(self):
        for heater in ({}, {"target": 200, "temperature": 25}, {"target": 0, "temperature": math.nan}):
            self.runner.calls.clear()
            with self.subTest(heater=heater), patch.object(self.manager, "_moonraker", side_effect=self.moonraker(heater=heater)):
                with self.assertRaises(UpdateError):
                    self.manager.flash(self.printer, self.job, lambda _: True)
            self.assertEqual(self.service_actions(), [])
            self.assertEqual(self.writes(), [])

    def test_inventory_change_during_preparation_blocks_before_service_stop(self):
        self.manager._verify_components.side_effect = [None, UpdateError("board inventory changed")]
        with patch.object(self.manager, "_idle"), self.assertRaisesRegex(UpdateError, "inventory changed"):
            self.manager.flash(self.printer, self.job, lambda _: True)
        self.assertEqual(self.service_actions(), [])
        self.assertEqual(self.writes(), [])

    def test_partial_flash_failure_stops_service_and_retains_per_board_progress(self):
        writes = 0

        def handler(argv, kwargs):
            nonlocal writes
            if "-f" in argv:
                writes += 1
                if writes == 2:
                    raise UpdateError("simulated transport failure")

        self.runner.handler = handler
        with patch.object(self.manager, "_idle"), self.assertRaisesRegex(UpdateError, "transport failure"):
            self.manager.flash(self.printer, self.job, lambda _: True)
        manifest = self.read_manifest(self.job)
        self.assertEqual(manifest["state"], "failed")
        self.assertEqual([record["state"] for record in manifest["components"]], ["flashed", "flashing"])
        self.assertEqual(self.service_actions(), ["stop", "stop"])
        self.assertTrue(manifest["source_or_mcu_changed"])
        self.assertTrue(all(kwargs.get("protect_write") is True for _, kwargs in self.writes()))

    def test_success_restores_interface_then_starts_and_verifies_service(self):
        with patch.object(self.manager, "_idle"):
            self.manager.flash(self.printer, self.job, lambda _: True)
        manifest = self.read_manifest(self.job)
        self.assertEqual(manifest["state"], "complete")
        self.assertEqual(self.service_actions(), ["stop", "start"])
        self.manager._ready.assert_called_once_with(self.printer)
        commands = [argv for argv, _ in self.runner.calls]
        self.assertLess(commands.index(["sudo", "-n", "ip", "link", "set", "can1", "up"]),
                        commands.index(["sudo", "-n", "systemctl", "start", "klipper"]))

    def test_failed_reconnect_returns_started_service_to_stopped_state(self):
        self.manager._ready.side_effect = UpdateError("MCU did not reconnect")
        with patch.object(self.manager, "_idle"), self.assertRaisesRegex(UpdateError, "reconnect"):
            self.manager.flash(self.printer, self.job, lambda _: True)
        self.assertEqual(self.service_actions(), ["stop", "start", "stop"])
        self.assertEqual(self.read_manifest(self.job)["state"], "failed")

    def test_live_host_commit_change_blocks_all_firmware_writes(self):
        self.manager._git.side_effect = lambda path, *args, **kwargs: "c" * 40 if args[:1] == ("rev-parse",) else ""
        with self.assertRaisesRegex(UpdateError, "changed since this build"):
            self.manager.flash(self.printer, self.job, lambda _: True)
        self.assertEqual(self.service_actions(), [])
        self.assertEqual(self.writes(), [])


class BuildLifecycleTests(WorkspaceTests):
    def setUp(self):
        super().setUp()
        for active_patch in (patch.object(self.manager, "_require_linux"),
                             patch.object(self.manager, "_lock", side_effect=lambda: nullcontext()),
                             patch.object(self.manager, "_git", return_value=""),
                             patch.object(self.manager, "_snapshot", side_effect=self.snapshot)):
            active_patch.start()
            self.addCleanup(active_patch.stop)
        self.runner.handler = self.build_command
        self.compile_events = []
        self.fail_board = ""
        self.drift_board = ""

    @staticmethod
    def snapshot(printer, source, refresh):
        source.mkdir(parents=True)
        return START, TARGET

    def build_command(self, argv, kwargs):
        if argv[:2] == ["git", "clone"]:
            Path(argv[-1]).mkdir(parents=True)
        if argv[:2] == ["make", "olddefconfig"] and Path(kwargs["cwd"]).name == self.drift_board:
            config = Path(kwargs["cwd"]) / ".config"
            config.write_text(config.read_text(encoding="utf-8") + "CONFIG_NEW_HARDWARE_SETTING=y\n", encoding="utf-8")
        if argv[:2] == ["make", "-j"]:
            board = Path(kwargs["cwd"]).name
            self.compile_events.append(board)
            if board == self.fail_board:
                raise UpdateError("simulated compilation failure")
            artifact = Path(kwargs["cwd"]) / "out/klipper.bin"
            artifact.parent.mkdir()
            artifact.write_bytes(("built " + board).encode())

    def test_all_boards_finish_building_before_job_becomes_ready(self):
        job = self.manager.build(self.printer, refresh=True)
        manifest = self.read_manifest(job)
        self.assertEqual(self.compile_events, ["toolhead", "main"])
        self.assertEqual(manifest["state"], "ready")
        self.assertEqual([record["state"] for record in manifest["components"]], ["built", "built"])
        self.assertEqual(manifest["commit"], TARGET)
        self.assertEqual(self.manager._check_job(self.printer, job), manifest)
        self.assertFalse(any("-f" in argv or "systemctl" in argv for argv, _ in self.runner.calls))

    def test_failed_second_build_never_flashes_or_stops_the_printer(self):
        self.fail_board = "main"
        with self.assertRaisesRegex(UpdateError, "compilation failure"):
            self.manager.build(self.printer, refresh=True)
        job = self.manager.jobs(self.printer)[0]
        manifest = self.read_manifest(job)
        self.assertEqual(manifest["state"], "build_failed")
        self.assertEqual([record["id"] for record in manifest["components"]], ["toolhead"])
        self.assertFalse(any("-f" in argv or "systemctl" in argv for argv, _ in self.runner.calls))
        with self.assertRaises(UpdateError):
            self.manager._check_job(self.printer, job)

    def test_new_kconfig_setting_requires_review_and_preserves_saved_profile(self):
        self.drift_board = "toolhead"
        config = Path(self.printer.components[0].config_file)
        original = config.read_bytes()
        with self.assertRaisesRegex(UpdateError, "build settings changed"):
            self.manager.build(self.printer, refresh=True)
        job = self.manager.jobs(self.printer)[0]
        self.assertEqual(self.read_manifest(job)["state"], "build_failed")
        self.assertIn("CONFIG_NEW_HARDWARE_SETTING", (job / "config-changes.txt").read_text(encoding="utf-8"))
        self.assertEqual(config.read_bytes(), original)
        self.assertEqual(self.compile_events, [])


if __name__ == "__main__":
    unittest.main()
