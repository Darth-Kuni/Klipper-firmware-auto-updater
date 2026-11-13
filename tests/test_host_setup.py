"""Verify new-host planning and guards without executing host commands."""

from contextlib import nullcontext
import json
from pathlib import Path, PurePosixPath
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from kcm.engine import Manager, UpdateError
from kcm.host_setup import HostSetup, systemd_arg
from kcm.models import Component, Printer
from kcm.storage import Store, atomic_json


class FakeRunner:
    """Record every command; simulate only files made by git/venv commands."""

    def __init__(self):
        self.calls = []
        self.log = None
        self.load_states = {}
        self.active_states = {}
        self.fail_when = None

    def run(self, argv, **kwargs):
        argv = [str(value) for value in argv]
        self.calls.append((argv, kwargs))
        if self.fail_when and self.fail_when(argv):
            raise UpdateError("simulated provisioning failure")
        stdout = ""
        if argv[:2] == ["systemctl", "show"]:
            stdout = self.load_states.get(argv[-1], "not-found")
        elif argv[:2] == ["systemctl", "is-active"]:
            stdout = self.active_states.get(argv[-1], "inactive")
        elif argv[:2] == ["git", "clone"]:
            Path(argv[-1]).mkdir(parents=True)
        elif argv[1:3] == ["-m", "venv"]:
            (Path(argv[-1]) / "bin").mkdir(parents=True)
        return subprocess.CompletedProcess(argv, 0, stdout, "")


class HostSetupTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(dir=Path(__file__).parent)
        self.addCleanup(self.temporary.cleanup)
        self.fixture = Path(self.temporary.name)
        self.store = Store(self.fixture / "state")
        self.runner = FakeRunner()
        self.host = HostSetup(self.store, output=lambda _text: None, runner=self.runner)
        self.printer = Printer("test", "Test printer", "/home/pi/klipper", "/home/pi/katapult")
        self.source = self.fixture / "klipper-source"
        (self.source / "scripts").mkdir(parents=True)
        (self.source / "klippy").mkdir()
        (self.source / "scripts/klippy-requirements.txt").write_text("# fixture\n", encoding="utf-8")
        (self.source / "klippy/klippy.py").write_text("# fixture\n", encoding="utf-8")
        self.sys_net = self.fixture / "sys-net"

        def mapped_path(value):
            # Keep profile fields as Linux paths while file fixtures live on the test OS.
            text = str(value)
            if text == "/home/pi/klipper":
                return self.source
            if text == "/sys/class/net":
                return self.sys_net
            return Path(value)

        for patcher in (
            patch.object(HostSetup, "_require_linux"),
            patch.object(HostSetup, "_user", return_value="pi"),
            patch.object(Manager, "_lock", side_effect=lambda: nullcontext()),
            patch("kcm.host_setup.Path", side_effect=mapped_path),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    @property
    def marker(self):
        return self.host._root(self.printer) / "install.json"

    def write_record(self, *, state="installed_stopped", configuration=False):
        atomic_json(self.marker, {"schema_version": 1, "state": state, "printer": self.printer.to_dict()})
        if configuration:
            config = self.host.data_path(self.printer) / "config/printer.cfg"
            config.parent.mkdir(parents=True, exist_ok=True)
            config.write_text("# user-reviewed fixture\n[printer]\nkinematics: none\n", encoding="utf-8")

    def can_device(self, kind="280"):
        target = self.sys_net / "can0/type"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(kind + "\n", encoding="ascii")

    def mutations(self):
        return [argv for argv, _kwargs in self.runner.calls if argv[0] != "systemctl"]

    def test_existing_service_blocks_before_mutation_or_confirmation(self):
        self.runner.load_states["klipper.service"] = "loaded"
        confirm = Mock(return_value=True)
        with self.assertRaisesRegex(UpdateError, "already exists"):
            self.host.install(self.printer, confirm)
        confirm.assert_not_called()
        self.assertEqual(self.mutations(), [])
        self.assertFalse(self.marker.exists())
        self.assertFalse(self.host.data_path(self.printer).exists())

    def test_unknown_service_state_fails_closed(self):
        self.runner.load_states["klipper.service"] = ""
        with self.assertRaisesRegex(UpdateError, "could not be checked"):
            self.host.install(self.printer, lambda _question: True)
        self.assertEqual(self.mutations(), [])
        self.assertFalse(self.marker.exists())

    def test_declining_install_leaves_environments_packages_and_services_unchanged(self):
        self.host.install(self.printer, lambda _question: False)
        self.assertEqual(self.mutations(), [])
        self.assertFalse(self.host._root(self.printer).exists())
        self.assertEqual((self.source / "scripts/klippy-requirements.txt").read_text(encoding="utf-8"), "# fixture\n")

    def test_new_service_units_and_moonraker_use_same_data_and_socket(self):
        self.host.install(self.printer, lambda _question: True)
        root = self.host._root(self.printer)
        data = self.host.data_path(self.printer)
        klipper_unit = (root / "systemd/klipper.service").read_text(encoding="utf-8")
        moonraker_unit = (root / "systemd/kcm-moonraker-test.service").read_text(encoding="utf-8")
        config = (data / "config/moonraker.conf").read_text(encoding="utf-8")
        self.assertIn("User=pi\n", klipper_unit)
        self.assertIn(systemd_arg(str(data / "config/printer.cfg")), klipper_unit)
        self.assertIn(systemd_arg(str(data / "comms/klippy.sock")), klipper_unit)
        self.assertIn(systemd_arg(str(data)), moonraker_unit)
        self.assertIn(systemd_arg(str(data / "config/moonraker.conf")), moonraker_unit)
        self.assertIn(f"klippy_uds_address: {data / 'comms/klippy.sock'}\n", config)
        self.assertIn("host: 127.0.0.1\n", config)
        self.assertIn("port: 7125\n", config)
        self.assertTrue((data / "config/printer.cfg.example").is_file())
        self.assertFalse((data / "config/printer.cfg").exists())
        self.assertEqual(json.loads(self.marker.read_text(encoding="utf-8"))["state"], "installed_stopped")
        self.assertFalse(any("start" in argv or "enable" in argv for argv, _ in self.runner.calls))

    def test_failed_install_records_failure_and_restores_log_without_starting(self):
        previous_log = self.fixture / "previous.log"
        self.runner.log = previous_log
        self.runner.fail_when = lambda argv: argv[:4] == ["sudo", "-n", "apt-get", "install"]
        with self.assertRaisesRegex(UpdateError, "simulated provisioning failure"):
            self.host.install(self.printer, lambda _question: True)
        record = json.loads(self.marker.read_text(encoding="utf-8"))
        self.assertEqual(record["state"], "failed")
        self.assertIn("simulated provisioning failure", record["error"])
        self.assertEqual(self.runner.log, previous_log)
        self.assertFalse(any("start" in argv or "enable" in argv for argv, _ in self.runner.calls))

    def test_existing_install_record_blocks_repeat_provisioning(self):
        self.write_record(state="failed")
        with self.assertRaisesRegex(UpdateError, "installation record"):
            self.host.install(self.printer, lambda _question: True)
        self.assertEqual(self.runner.calls, [])

    def test_missing_configuration_blocks_start(self):
        self.write_record()
        confirm = Mock(return_value=True)
        with self.assertRaisesRegex(UpdateError, "complete printer configuration"):
            self.host.start(self.printer, confirm)
        confirm.assert_not_called()
        self.assertEqual(self.runner.calls, [])

    def test_declining_first_start_never_enables_or_starts_services(self):
        self.write_record(configuration=True)
        with patch.object(self.host, "status", return_value={"ready": False}):
            self.assertEqual(self.host.start(self.printer, lambda _question: False), {"ready": False})
        self.assertEqual(self.runner.calls, [])
        self.assertEqual(json.loads(self.marker.read_text(encoding="utf-8"))["state"], "installed_stopped")

    def test_failed_or_changed_profile_blocks_start(self):
        self.write_record(state="failed", configuration=True)
        with self.assertRaisesRegex(UpdateError, "incomplete"):
            self.host.start(self.printer, lambda _question: True)
        self.write_record(configuration=True)
        self.printer.service = "another-klipper"
        with self.assertRaisesRegex(UpdateError, "profile changed"):
            self.host.start(self.printer, lambda _question: True)
        self.assertEqual(self.runner.calls, [])

    def test_confirmed_start_uses_only_the_recorded_services(self):
        self.write_record(configuration=True)
        with patch.object(self.host, "status", return_value={"ready": True}):
            result = self.host.start(self.printer, lambda _question: True)
        starts = [argv for argv, _ in self.runner.calls if argv[:4] == ["sudo", "-n", "systemctl", "start"]]
        self.assertEqual([argv[-1] for argv in starts], ["klipper", "kcm-moonraker-test"])
        self.assertEqual(json.loads(self.marker.read_text(encoding="utf-8"))["state"], "started")
        self.assertTrue(result["ready"])

    def test_can_invalid_interface_or_bitrate_rejected_before_commands(self):
        for interface, bitrate in (("../can0", 1_000_000), ("can0;reboot", 1_000_000),
                                   ("can0", True), ("can0", 123456), ("can0", "1000000")):
            with self.subTest(interface=interface, bitrate=bitrate):
                with self.assertRaises(UpdateError):
                    self.host.configure_can(self.printer, interface, bitrate, lambda _question: True)
        self.assertEqual(self.runner.calls, [])

    def test_can_missing_or_non_can_device_blocks_changes(self):
        with self.assertRaisesRegex(UpdateError, "not an available CAN"):
            self.host.configure_can(self.printer, "can0", 1_000_000, lambda _question: True)
        self.can_device(kind="1")
        with self.assertRaisesRegex(UpdateError, "not an available CAN"):
            self.host.configure_can(self.printer, "can0", 1_000_000, lambda _question: True)
        self.assertEqual(self.runner.calls, [])

    def test_active_printer_blocks_can_reset_before_confirmation(self):
        self.can_device()
        self.runner.active_states["klipper"] = "active"
        confirm = Mock(return_value=True)
        with self.assertRaisesRegex(UpdateError, "Stop klipper"):
            self.host.configure_can(self.printer, "can0", 1_000_000, confirm)
        confirm.assert_not_called()
        self.assertEqual(self.mutations(), [])

    def test_other_registered_active_printer_on_bus_blocks_can_reset(self):
        self.can_device()
        other = Printer("other", "Other printer", "/home/pi/other-klipper", "/home/pi/katapult", service="other-klipper", components=[
            Component("toolhead", "Toolhead", "/home/pi/toolhead.config", "can", can_uuid="112233445566")
        ])
        self.store.save([self.printer, other])
        self.runner.active_states["other-klipper"] = "active"
        with self.assertRaisesRegex(UpdateError, "Stop other-klipper"):
            self.host.configure_can(self.printer, "can0", 1_000_000, lambda _question: True)
        self.assertEqual(self.mutations(), [])

    def test_declining_can_configuration_leaves_link_untouched(self):
        self.can_device()
        self.host.configure_can(self.printer, "can0", 1_000_000, lambda _question: False)
        self.assertEqual(self.mutations(), [])

    def test_runtime_can_configuration_targets_validated_interface_in_order(self):
        self.can_device()
        self.host.configure_can(self.printer, "can0", 500_000, lambda _question: True)
        link_commands = [argv for argv, _ in self.runner.calls if argv[:4] == ["sudo", "-n", "ip", "link"]]
        self.assertEqual(link_commands[0][-2:], ["can0", "down"])
        self.assertIn(["sudo", "-n", "ip", "link", "set", "can0", "type", "can", "bitrate", "500000"], link_commands)
        self.assertEqual(link_commands[-1][-2:], ["can0", "up"])


class ServiceArgumentTests(unittest.TestCase):
    def test_systemd_escapes_quotes_dollars_and_specifiers(self):
        self.assertEqual(systemd_arg('/home/pi/50% "board"/$HOME'), '"/home/pi/50%% \\"board\\"/$$HOME"')

    def test_service_files_quote_custom_paths_without_shell_commands(self):
        store = SimpleNamespace(root=PurePosixPath('/data/50% "printer"/$HOME'))
        printer = Printer("test", "Test", '/home/pi/50% "klipper"/$CODE', "/home/pi/katapult")
        host = HostSetup(store, output=lambda _text: None, runner=FakeRunner())
        with patch.object(host, "_user", return_value="pi"), patch("kcm.host_setup.Path", PurePosixPath):
            units = host._service_files(printer)
        unit = units["klipper.service"]
        self.assertIn(systemd_arg(printer.klipper_path + "/klippy/klippy.py"), unit)
        self.assertIn(systemd_arg(str(host.data_path(printer) / "config/printer.cfg")), unit)
        self.assertNotIn("/bin/sh", unit)
        self.assertNotIn("/bin/bash", unit)
        self.assertIn("%%", unit)
        self.assertIn("$$HOME", unit)

    def test_control_characters_cannot_be_injected_into_unit_arguments(self):
        for character in ("\n", "\r", "\x00", "\x1f", "\x7f"):
            with self.subTest(character=repr(character)):
                with self.assertRaises(ValueError):
                    systemd_arg("/home/pi/" + character + "Injected=command")


if __name__ == "__main__":
    unittest.main()
