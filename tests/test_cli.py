"""CLI workflow tests; all update operations are mocked."""

import contextlib
import io
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from kcm import cli
from kcm.models import Component, Printer
from kcm.storage import Store


class CliTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parent)
        self.addCleanup(self.temp.cleanup)
        self.store = Store(Path(self.temp.name))
        self.component = Component(
            id="toolhead", name="Toolhead", config_file=str(self.store.config_path("voron", "toolhead")),
            transport="can", can_uuid="abcdef123456",
        )
        self.printer = Printer(
            id="voron", name="Voron", klipper_path="/home/pi/klipper", katapult_path="/home/pi/katapult",
            components=[self.component],
        )
        self.store.save([self.printer])

    def test_configure_latest_passes_source_choice_and_import(self):
        imported = Path(self.temp.name) / "saved.config"
        with patch.object(cli, "Manager") as manager_class, contextlib.redirect_stdout(io.StringIO()):
            result = cli.main([
                "--data-dir", self.temp.name, "configure", "voron", "toolhead", "--latest",
                "--import-config", str(imported),
            ])
        self.assertEqual(result, 0)
        args, kwargs = manager_class.return_value.configure.call_args
        self.assertEqual(args[0].id, "voron")
        self.assertEqual(args[1].id, "toolhead")
        self.assertEqual(kwargs, {"import_path": str(imported.resolve()), "refresh": True})

    def test_configure_defaults_to_current_source(self):
        with patch.object(cli, "Manager") as manager_class, contextlib.redirect_stdout(io.StringIO()):
            result = cli.main(["--data-dir", self.temp.name, "configure", "voron", "toolhead"])
        self.assertEqual(result, 0)
        self.assertEqual(manager_class.return_value.configure.call_args.kwargs, {"import_path": None, "refresh": False})

    def test_uart_wizard_preserves_different_application_and_bootloader_baud(self):
        answers = ["main", "Mainboard", "uart", "auto", "/dev/ttyS1", "115200", "250000", "n", "/dev/ttyS0", ""]
        with patch("builtins.input", side_effect=answers), contextlib.redirect_stdout(io.StringIO()):
            component = cli._component_settings(self.store, self.printer)
        self.assertEqual(component.transport, "uart")
        self.assertEqual(component.baud, 115200)
        self.assertEqual(component.application_baud, 250000)
        self.assertEqual(component.flash_device, "/dev/ttyS1")
        self.assertEqual(component.application_device, "/dev/ttyS0")

    def test_edit_can_clear_existing_bridge_relationship(self):
        bridge = Component(
            id="bridge", name="Bridge", config_file=str(self.store.config_path("voron", "bridge")),
            transport="usb", can_uuid="111111111111", flash_device="/dev/serial/by-id/katapult-bridge",
        )
        self.component.via = "bridge"
        self.printer.components.insert(0, bridge)
        with patch("builtins.input", side_effect=["", "", "", "", "", "-"]), contextlib.redirect_stdout(io.StringIO()):
            updated = cli._component_settings(self.store, self.printer, self.component)
        self.assertEqual(updated.via, "")
        self.assertEqual(updated.can_uuid, self.component.can_uuid)

    def test_existing_printer_wizard_can_exit_without_operations(self):
        manager = Mock()
        with patch("builtins.input", side_effect=["1", "0", "0"]), contextlib.redirect_stdout(io.StringIO()):
            cli._wizard(self.store, manager)
        self.assertEqual(manager.mock_calls, [])

    def test_build_and_flash_menu_uses_the_newly_built_job(self):
        manager = Mock()
        job = Path(self.temp.name) / "job"
        manager.build.return_value = job
        with patch("builtins.input", side_effect=["9", "0"]), contextlib.redirect_stdout(io.StringIO()):
            cli._manage_printer(self.store, manager, "voron")
        manager.build.assert_called_once()
        self.assertEqual(manager.build.call_args.kwargs, {"refresh": True})
        args, kwargs = manager.flash.call_args
        self.assertEqual(args[1], job)
        self.assertIs(kwargs["confirm"], cli._yes)

    def test_wizard_configuration_does_not_fetch_by_default(self):
        manager = Mock()
        with patch("builtins.input", side_effect=["", "2"]), contextlib.redirect_stdout(io.StringIO()):
            cli._configure(manager, self.printer, self.component)
        self.assertEqual(manager.configure.call_args.kwargs, {"import_path": None, "refresh": False})

    def test_empty_confirmation_never_authorizes_flash(self):
        with patch("builtins.input", return_value=""):
            self.assertFalse(cli._yes("Flash firmware?"))

    def test_setup_command_opens_the_selected_saved_printer(self):
        with patch("kcm.onboarding.run_setup") as setup, patch.object(cli, "Manager"):
            result = cli.main(["--data-dir", self.temp.name, "setup", "voron"])
        self.assertEqual(result, 0)
        self.assertEqual(setup.call_args.args[2].id, "voron")

    def test_boards_command_joins_search_terms_without_running_updates(self):
        with patch("kcm.onboarding.show_boards") as boards, patch.object(cli, "Manager") as manager:
            result = cli.main(["--data-dir", self.temp.name, "boards", "EBB36", "V1.2"])
        self.assertEqual(result, 0)
        boards.assert_called_once_with("EBB36 V1.2")
        self.assertEqual(manager.return_value.mock_calls, [])


if __name__ == "__main__":
    unittest.main()
