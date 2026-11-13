"""Tests for target identity, flash dependency order, and profile integrity."""

from dataclasses import replace
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from kcm.models import Component, Printer
from kcm.storage import Store, atomic_json


def can_component(component_id="toolhead", uuid="aabbccddeeff", **kwargs):
    values = dict(id=component_id, name=component_id, config_file=f"/profiles/{component_id}.config",
                  transport="can", can_uuid=uuid)
    values.update(kwargs)
    return Component(**values)


def printer(*components):
    return Printer("printer-1", "Printer one", "/home/pi/klipper", "/home/pi/katapult",
                   components=list(components))


class ProfileValidationTests(unittest.TestCase):
    def test_can_profile_roundtrip(self):
        profile = printer(can_component())
        self.assertEqual(Printer.from_dict(profile.to_dict()), profile)

    def test_serial_profile_requires_separate_flashing_target(self):
        component = Component("main", "Main board", "/main.config", "usb",
                              application_device="/dev/serial/by-id/usb-Klipper_main",
                              flash_device="/dev/serial/by-id/usb-Katapult_main")
        component.validate()
        with self.assertRaisesRegex(ValueError, "Flashing device"):
            replace(component, flash_device="").validate()

    def test_bridge_can_identity_with_usb_flash_target(self):
        component = Component("bridge", "Bridge", "/bridge.config", "usb",
                              can_uuid="012345abcdef", flash_device="/dev/serial/by-id/usb-Katapult_bridge")
        component.validate()

    def test_manual_serial_entry_does_not_need_application_path(self):
        Component("main", "Main", "/main.config", "uart", bootloader_entry="manual",
                  flash_device="/dev/ttyS0").validate()

    def test_application_and_bootloader_baud_rates_are_saved_independently(self):
        component = Component("main", "Main", "/main.config", "uart",
                              application_device="/dev/ttyAMA0", flash_device="/dev/ttyAMA0",
                              application_baud=250000, baud=115200)
        self.assertEqual(Component.from_dict(component.to_dict()), component)
        for baud in (True, "250000", 0, 1199, 4000001):
            with self.subTest(baud=baud), self.assertRaises(ValueError):
                replace(component, application_baud=baud).validate()

    def test_auto_serial_entry_requires_application_identity(self):
        with self.assertRaisesRegex(ValueError, "Automatic bootloader entry"):
            Component("main", "Main", "/main.config", "uart", flash_device="/dev/ttyS0").validate()

    def test_no_unsafe_ids_or_paths(self):
        for component_id in ("../main", "main/child", "Main", "-main", "", "a" * 65):
            with self.subTest(component_id=component_id), self.assertRaises(ValueError):
                can_component(component_id).validate()
        for config in ("main.config", "/tmp/../main.config"):
            with self.subTest(config=config), self.assertRaises(ValueError):
                can_component(config_file=config).validate()

    def test_invalid_device_paths_rejected(self):
        for device in ("ttyS0", "/tmp/ttyS0", "/dev/../tmp/ttyS0", "/dev/ttyS0;reboot", "-d"):
            with self.subTest(device=device), self.assertRaises(ValueError):
                Component("main", "Main", "/main.config", "usb", application_device=device,
                          flash_device=device).validate()

    def test_can_uuid_and_interface_are_validated(self):
        for uuid in ("", "abcdef", "aabbccddeefg", "aabbccddeeff00"):
            with self.subTest(uuid=uuid), self.assertRaises(ValueError):
                can_component(uuid=uuid).validate()
        with self.assertRaises(ValueError):
            can_component(can_interface="can0;reboot").validate()

    def test_duplicate_can_target_is_case_insensitive(self):
        with self.assertRaisesRegex(ValueError, "Duplicate CAN target"):
            printer(can_component("a"), can_component("b", "AABBCCDDEEFF")).validate()

    def test_same_uuid_on_distinct_buses_is_allowed(self):
        printer(can_component("a"), can_component("b", can_interface="can1")).validate()

    def test_duplicate_usb_flash_identity_rejected(self):
        first = Component("main", "Main", "/main.config", "usb", bootloader_entry="manual",
                          flash_device="/dev/ttyACM0")
        with self.assertRaisesRegex(ValueError, "Duplicate flashing device"):
            printer(first, replace(first, id="other")).validate()

    def test_duplicate_component_id_rejected(self):
        with self.assertRaisesRegex(ValueError, "Duplicate component ID"):
            printer(can_component(), can_component(uuid="112233445566")).validate()

    def test_downstream_nodes_precede_nested_bridges(self):
        profile = printer(can_component("outer", "000000000001"),
                          can_component("inner", "000000000002", via="outer"),
                          can_component("toolhead", "000000000003", via="inner"))
        self.assertEqual([c.id for c in profile.ordered_components()], ["toolhead", "inner", "outer"])

    def test_order_is_deterministic_with_unrelated_nodes(self):
        profile = printer(can_component("a", "000000000001"), can_component("b", "000000000002"))
        self.assertEqual([c.id for c in profile.ordered_components()], ["a", "b"])

    def test_missing_parent_and_cycles_rejected(self):
        with self.assertRaisesRegex(ValueError, "Unknown upstream bridge"):
            printer(can_component(via="missing")).validate()
        with self.assertRaisesRegex(ValueError, "cycle"):
            printer(can_component("a", via="b"),
                    can_component("b", "112233445566", via="a")).validate()

    def test_self_dependency_rejected(self):
        with self.assertRaisesRegex(ValueError, "itself"):
            can_component(via="toolhead").validate()

    def test_source_paths_and_service_are_not_shell_fragments(self):
        for changes in ({"klipper_path": "~/klipper"}, {"katapult_path": "/home/pi/../katapult"},
                        {"service": "klipper; reboot"}, {"source_ref": "--all"}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                replace(printer(), **changes).validate()
        replace(printer(), service="klipper@printer-1.service").validate()

    def test_moonraker_credentials_and_malformed_urls_rejected(self):
        for url in ("file:///tmp/moonraker", "http://u:p@localhost:7125", "http://localhost:wrong", "http://localhost?x=1"):
            with self.subTest(url=url), self.assertRaises(ValueError):
                replace(printer(), moonraker_url=url).validate()

    def test_unknown_fields_fail_instead_of_silent_loss(self):
        data = can_component().to_dict()
        data["unknown"] = "future setting"
        with self.assertRaisesRegex(ValueError, "Unknown Component fields"):
            Component.from_dict(data)


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parent)
        self.addCleanup(self.temporary.cleanup)
        self.store = Store(Path(self.temporary.name))

    def test_empty_store(self):
        self.assertEqual(self.store.load(), [])

    def test_save_load_preserves_settings_and_all_printers(self):
        profiles = [printer(can_component()), replace(printer(), id="another", source_ref="v0.13.0")]
        self.store.save(profiles)
        self.assertEqual(self.store.load(), profiles)
        self.assertEqual(self.store.get("printer-1"), profiles[0])

    def test_duplicate_printer_ids_fail_before_writing(self):
        self.store.save([printer()])
        original = self.store.registry_path.read_bytes()
        with self.assertRaisesRegex(ValueError, "Duplicate printer ID"):
            self.store.save([printer(), printer()])
        self.assertEqual(self.store.registry_path.read_bytes(), original)

    def test_unknown_or_missing_schema_is_refused(self):
        for schema in (None, 2, "1", True):
            with self.subTest(schema=schema):
                atomic_json(self.store.registry_path, {"schema_version": schema, "printers": []})
                with self.assertRaisesRegex(ValueError, "Unsupported profile schema"):
                    self.store.load()

    def test_corrupt_registry_is_not_overwritten_by_load(self):
        self.store.registry_path.write_text("broken", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "Cannot read profile registry"):
            self.store.load()
        self.assertEqual(self.store.registry_path.read_text(encoding="utf-8"), "broken")

    def test_config_path_validates_segments(self):
        path = self.store.config_path("printer-1", "toolhead")
        self.assertEqual(path, self.store.root / "profiles" / "printer-1" / "toolhead.config")
        for bad in ("../escape", "/tmp/escape", "a/b"):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                self.store.config_path(bad, "toolhead")

    def test_failed_atomic_replace_keeps_previous_registry_and_cleans_temporary(self):
        self.store.save([printer()])
        original = self.store.registry_path.read_bytes()
        with patch("kcm.storage.os.replace", side_effect=OSError("disk failure")):
            with self.assertRaises(OSError):
                self.store.save([])
        self.assertEqual(self.store.registry_path.read_bytes(), original)
        self.assertEqual(list(self.store.root.glob("*.tmp")), [])

    def test_missing_printer_is_explicit(self):
        with self.assertRaises(KeyError):
            self.store.get("missing")


if __name__ == "__main__":
    unittest.main()
