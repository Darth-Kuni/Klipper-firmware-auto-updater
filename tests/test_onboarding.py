"""Guided setup tests; every system, network, and hardware action is mocked."""

from __future__ import annotations

from contextlib import redirect_stdout
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from kcm.engine import UpdateError
from kcm.models import Component, Printer
from kcm.onboarding import (Guide, _draft_component, _export_snippet, _host_intro,
                            _katapult, _startup, board_catalog, mcu_snippet, run_setup)
from kcm.storage import Store


class OnboardingTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parent)
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.store = Store(self.root / "data")
        self.printer = Printer("voron", "Voron", "/home/pi/klipper", "/home/pi/katapult")
        self.store.save([self.printer])
        self.guide = Guide(self.store, self.printer)
        self.board = {"id": "toolhead", "name": "Toolhead", "model": "Example board", "manufacturer": "Example",
                      "revision": "V1.2", "mcu": "STM32G0B1", "family": "stm32", "catalog_id": "example",
                      "documentation_url": "https://example.invalid/official/manual", "source_urls": [],
                      "initial_methods": ["dfu"], "preferred_transport": "can", "identity_confirmed": True}

    def saved_bootloader_job(self):
        job = self.store.root / "bootstrap" / "voron" / "toolhead" / "saved-build"
        job.mkdir(parents=True)
        (job / "manifest.json").write_text('{"state": "ready", "kind": "katapult"}', encoding="utf-8")
        return job

    def test_catalog_search_matches_all_words_and_reads_packaged_resource_route(self):
        root = self.root / "catalog-fixture"
        (root / "data").mkdir(parents=True)
        boards = [{"id": "ebb36", "name": "EBB36", "manufacturer": "BIGTREETECH", "revision": "V1.2", "mcu": "STM32G0B1"},
                  {"id": "other", "name": "Other", "manufacturer": "Example", "mcu": "RP2040"}]
        (root / "data" / "boards.json").write_text(json.dumps({"schema_version": 1, "boards": boards}), encoding="utf-8")
        with patch("kcm.onboarding.resources.files", return_value=root) as files:
            result = board_catalog("bigtreetech stm32")
        files.assert_called_once_with("kcm")
        self.assertEqual(result, [boards[0]])

    def test_identity_evidence_is_retained_without_claiming_completion(self):
        self.board["identity_confirmed"] = False
        self.guide.set_board(self.board)
        resumed = Guide(self.store, self.printer)
        self.assertEqual(resumed.data["boards"]["toolhead"]["mcu"], "STM32G0B1")
        self.assertEqual(resumed.data["steps"]["identity/toolhead"]["status"], "needs_identification")
        self.assertNotIn("complete", resumed.data)

    def test_identity_evidence_rejects_credentialed_or_non_web_links(self):
        for url in ("file:///etc/shadow", "https://user:password@example.invalid/manual"):
            with self.subTest(url=url):
                self.board["documentation_url"] = url
                with self.assertRaises(ValueError):
                    self.guide.set_board(self.board)

    def test_viewing_host_information_does_not_complete_prerequisites(self):
        bootstrap = Mock()
        bootstrap.inspect_host.return_value = {"platform": "Linux", "python": "3.11", "tools": {"git": "/usr/bin/git"}}
        with patch("builtins.input", return_value=""), redirect_stdout(io.StringIO()):
            _host_intro(bootstrap, self.guide)
        self.assertEqual(self.guide.data["steps"]["host_inspection"]["status"], "observed")
        self.assertNotIn("host_prerequisites", self.guide.data["steps"])
        self.assertFalse(self.guide.data["user_checks"])

    def test_cancelled_system_action_is_not_recorded_as_installed(self):
        action = Mock(side_effect=UpdateError("Setup cancelled before changes"))
        with self.assertRaises(UpdateError):
            self.guide.perform("dependencies", action, "installed")
        self.assertEqual(Guide(self.store, self.printer).data["steps"]["dependencies"]["status"], "cancelled")

    def test_failed_action_retains_error_for_resumption(self):
        with self.assertRaises(UpdateError):
            self.guide.perform("katapult/toolhead/build", Mock(side_effect=UpdateError("Compiler missing")), "built")
        record = Guide(self.store, self.printer).data["steps"]["katapult/toolhead/build"]
        self.assertEqual(record["status"], "failed")
        self.assertIn("Compiler missing", record["details"])

    def test_pending_can_board_uses_no_fake_identity_or_registry_entry(self):
        component = _draft_component(self.store, self.printer, self.board)
        self.assertEqual(component.can_uuid, "")
        self.assertEqual(component.id, "toolhead")
        self.assertEqual(self.store.get("voron").components, [])

    def test_pending_usb_board_does_not_require_a_nonexistent_running_klipper_path(self):
        self.board["preferred_transport"] = "usb"
        component = _draft_component(self.store, self.printer, self.board)
        self.assertEqual(component.bootloader_entry, "manual")
        self.assertEqual(component.application_device, "")
        self.assertEqual(component.flash_device, "")

    def test_unconfirmed_board_cannot_reach_bootloader_actions(self):
        self.board["identity_confirmed"] = False
        self.guide.set_board(self.board)
        bootstrap = Mock()
        with patch("builtins.input", side_effect=["1"]), redirect_stdout(io.StringIO()):
            with self.assertRaises(ValueError):
                _katapult(self.store, self.printer, bootstrap, self.guide)
        self.assertEqual(bootstrap.mock_calls, [])

    def test_unknown_board_initial_install_is_a_manual_handoff(self):
        self.board["catalog_id"] = "custom"
        self.guide.set_board(self.board)
        self.saved_bootloader_job()
        bootstrap = Mock()
        with patch("builtins.input", side_effect=["1", "3", "1", "1"]), redirect_stdout(io.StringIO()):
            _katapult(self.store, self.printer, bootstrap, self.guide)
        bootstrap.install_katapult.assert_not_called()
        self.assertEqual(self.guide.data["steps"]["katapult/toolhead/install"]["status"], "manual_action_required")

    def test_documented_installer_runs_but_does_not_claim_hardware_verified(self):
        self.guide.set_board(self.board)
        job = self.saved_bootloader_job()
        bootstrap = Mock()
        bootstrap.install_katapult.return_value = None
        with patch("builtins.input", side_effect=["1", "3", "1", "1", "ABC123"]), redirect_stdout(io.StringIO()):
            _katapult(self.store, self.printer, bootstrap, self.guide)
        self.assertEqual(bootstrap.install_katapult.call_args.args[2:5], (job, "dfu", "ABC123"))
        self.assertEqual(self.guide.data["steps"]["katapult/toolhead/install"]["status"], "installed_unverified")

    def test_mcu_snippet_uses_running_identity_and_separate_uart_baud(self):
        self.printer.components = [
            Component("main", "Mainboard", "/main.config", "uart", application_device="/dev/ttyAMA0",
                      flash_device="/dev/ttyAMA0", baud=115200, application_baud=250000),
            Component("toolhead", "Toolhead", "/toolhead.config", "can", can_uuid="aabbccddeeff", can_interface="can1"),
        ]
        snippet = mcu_snippet(self.printer)
        self.assertIn("[mcu]\nserial: /dev/ttyAMA0\nbaud: 250000", snippet)
        self.assertIn("[mcu toolhead]\ncanbus_uuid: aabbccddeeff\ncanbus_interface: can1", snippet)
        self.assertNotIn("115200", snippet)
        self.assertNotIn("[heater", snippet)

    def test_connection_reference_export_preserves_real_printer_configuration(self):
        self.printer.components = [Component("toolhead", "Toolhead", "/toolhead.config", "can", can_uuid="aabbccddeeff")]
        actual_configuration = self.root / "printer.cfg"
        actual_configuration.write_text("[existing_config]\n", encoding="utf-8")
        host = Mock()
        host.guide.return_value = ["Review the full board configuration first."]
        with redirect_stdout(io.StringIO()):
            _export_snippet(self.store, self.printer, host, self.guide)
        self.assertEqual(actual_configuration.read_text(encoding="utf-8"), "[existing_config]\n")
        self.assertTrue((self.store.root / "setup" / "voron" / "mcu-connections.cfg").is_file())
        self.assertEqual(self.guide.data["steps"]["printer_configuration"]["status"], "references_exported")

    def test_ready_connection_does_not_mark_physical_checks_complete(self):
        host = Mock()
        host.status.return_value = {"ready": True, "moonraker": True, "klipper_state": "ready"}
        with patch("builtins.input", side_effect=["3"]), redirect_stdout(io.StringIO()):
            _startup(self.printer, host, self.guide)
        self.assertEqual(self.guide.data["steps"]["startup_status"]["status"], "ready_observed")
        self.assertNotIn("printer_configuration_checks", self.guide.data["user_checks"])
        host.start.assert_not_called()

    def test_viewing_checklist_makes_no_system_calls_or_completion_record(self):
        manager, bootstrap, host = Mock(), Mock(), Mock()
        with patch("builtins.input", side_effect=["10", "0"]), redirect_stdout(io.StringIO()):
            run_setup(self.store, manager, self.printer, bootstrap=bootstrap, host=host)
        self.assertEqual(manager.mock_calls + bootstrap.mock_calls + host.mock_calls, [])
        self.assertFalse(self.guide.path.exists())


if __name__ == "__main__":
    unittest.main()
