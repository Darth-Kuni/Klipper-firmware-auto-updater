"""Check the reference catalog's schema and prevent executable board recipes."""

import json
from pathlib import Path
import re
import unittest
from urllib.parse import urlsplit


CATALOG = Path(__file__).resolve().parents[1] / "kcm" / "data" / "boards.json"
REQUIRED_FIELDS = {
    "id", "name", "manufacturer", "revision", "mcu", "family",
    "documentation_url", "source_urls", "notes", "transports", "initial_methods",
}
SETTINGS = {"clock", "can_pins", "usb_pins", "uart_pins", "application_offset"}


class BoardCatalogTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.catalog = json.loads(CATALOG.read_text(encoding="utf-8"))
        cls.boards = cls.catalog["boards"]

    def test_versioned_reference_schema(self):
        self.assertEqual(set(self.catalog), {"schema_version", "boards"})
        self.assertIs(type(self.catalog["schema_version"]), int)
        self.assertEqual(self.catalog["schema_version"], 1)
        self.assertIsInstance(self.boards, list)
        self.assertGreaterEqual(len(self.boards), 4)
        for board in self.boards:
            with self.subTest(board=board.get("id")):
                self.assertTrue(REQUIRED_FIELDS <= board.keys())
                self.assertTrue(board.keys() <= REQUIRED_FIELDS | {"settings"})
                for key in ("id", "name", "manufacturer", "revision", "mcu", "family"):
                    self.assertIsInstance(board[key], str)
                    self.assertTrue(board[key].strip())
                self.assertRegex(board["id"], r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
                self.assertIn(board["family"], {"stm32", "rp2040", "rp2350", "lpc176x"})

    def test_exact_board_revision_and_mcu_are_not_merged(self):
        ids = [board["id"] for board in self.boards]
        identities = [tuple(board[key].casefold() for key in
                            ("manufacturer", "name", "revision", "mcu"))
                      for board in self.boards]
        self.assertEqual(len(ids), len(set(ids)))
        self.assertEqual(len(identities), len(set(identities)))
        octopus = next(board for board in self.boards if board["id"] == "btt-octopus-v1-1-f446")
        pro = next(board for board in self.boards if board["id"] == "btt-octopus-pro-v1-0-f429")
        self.assertNotEqual(octopus["name"], pro["name"])
        self.assertEqual(octopus["mcu"], "STM32F446ZET6")
        self.assertEqual(pro["mcu"], "STM32F429ZGT6")
        self.assertNotEqual(octopus["settings"]["clock"], pro["settings"]["clock"])

    def test_documentation_links_are_web_references_with_primary_sources(self):
        for board in self.boards:
            with self.subTest(board=board["id"]):
                self.assertIsInstance(board["source_urls"], list)
                self.assertTrue(board["source_urls"])
                for url in [board["documentation_url"], *board["source_urls"]]:
                    self.assertIsInstance(url, str)
                    parsed = urlsplit(url)
                    self.assertIn(parsed.scheme, {"http", "https"})
                    self.assertTrue(parsed.hostname)
                    self.assertIsNone(parsed.username)
                    self.assertIsNone(parsed.password)
                    self.assertNotRegex(url, r"[\r\n\x00]")
                manufacturer_url = urlsplit(board["documentation_url"])
                self.assertEqual(manufacturer_url.hostname, "github.com")
                self.assertTrue(manufacturer_url.path.startswith("/bigtreetech/"))
                for url in board["source_urls"]:
                    parsed = urlsplit(url)
                    if parsed.hostname == "github.com":
                        self.assertIn(parsed.path.split("/")[1].casefold(), {"bigtreetech", "arksine"})
                    else:
                        self.assertEqual(parsed.hostname, "www.raspberrypi.com")

    def test_transport_and_initial_method_vocabularies_are_separate(self):
        all_transports = set()
        for board in self.boards:
            with self.subTest(board=board["id"]):
                for field, allowed in (
                    ("transports", {"can", "usb", "uart"}),
                    ("initial_methods", {"dfu", "uf2", "sdcard", "swd", "uart"}),
                ):
                    self.assertIsInstance(board[field], list)
                    self.assertTrue(board[field])
                    self.assertEqual(len(board[field]), len(set(board[field])))
                    self.assertTrue(set(board[field]) <= allowed)
                all_transports.update(board["transports"])
                if board["family"] in {"rp2040", "rp2350"}:
                    self.assertNotIn("dfu", board["initial_methods"])
        self.assertTrue({"can", "usb", "uart"} <= all_transports)

    def test_settings_are_reference_values_without_commands_or_kconfig(self):
        executable = re.compile(
            r"(?:\b(?:sudo|dfu-util|avrdude|bash|powershell|cmd\.exe)\b|"
            r"\bmake\s+(?:flash|menuconfig)|\bpython[23]?\s+|"
            r"CONFIG_[A-Z0-9_]+|[;&|`]|\$\(|[\r\n\x00])",
            re.IGNORECASE,
        )
        for board in self.boards:
            with self.subTest(board=board["id"]):
                self.assertIsInstance(board["notes"], list)
                self.assertTrue(board["notes"])
                settings = board.get("settings", {})
                self.assertIsInstance(settings, dict)
                self.assertTrue(settings.keys() <= SETTINGS)
                for key, value in settings.items():
                    if key == "application_offset":
                        self.assertIs(type(value), int)
                        self.assertGreaterEqual(value, 0)
                        self.assertEqual(value % 1024, 0)
                    else:
                        self.assertIsInstance(value, str)
                        self.assertTrue(value.strip())
                        self.assertIsNone(executable.search(value))
                for note in board["notes"]:
                    self.assertIsInstance(note, str)
                    self.assertTrue(note.strip())
                    self.assertNotRegex(note, r"(?:```|CONFIG_[A-Z0-9_]+|\$\(|\x00)")


if __name__ == "__main__":
    unittest.main()
