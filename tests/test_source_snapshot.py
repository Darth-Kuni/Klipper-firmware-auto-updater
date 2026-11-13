"""Integration tests for isolated source snapshots using local Git repositories.

These tests run real Git processes. Only the file transport is allowed; no
network, printer, service manager, compiler, or hardware operation is used.
"""

from __future__ import annotations

import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from kcm.engine import Manager
from kcm.models import Printer
from kcm.storage import Store


@unittest.skipUnless(shutil.which("git"), "Git is required for local source snapshot tests")
class SourceSnapshotTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="source-snapshot-", dir=Path(__file__).resolve().parent)
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        environment = patch.dict(os.environ, {
            "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_TERMINAL_PROMPT": "0", "GIT_ALLOW_PROTOCOL": "file",
        })
        environment.start()
        self.addCleanup(environment.stop)
        self.upstream = self.root / "upstream"
        self.active = self.root / "active"
        self.git("init", "--initial-branch=master", str(self.upstream))
        (self.upstream / "src").mkdir()
        (self.upstream / "src" / "Kconfig").write_text('mainmenu "Snapshot fixture"\n', encoding="utf-8")
        self.first = self.commit_version("first")
        self.git("clone", "--", str(self.upstream), str(self.active))
        self.second = self.commit_version("second")
        (self.active / ".config").write_text("CONFIG_BOARD=active_settings\n", encoding="utf-8")
        self.printer = Printer(
            id="printer", name="Local Git fixture", klipper_path=str(self.active),
            katapult_path=str(self.root / "katapult"), source_ref="origin/master",
        )
        self.manager = Manager(Store(self.root / "data"), output=lambda _: None)
        # The snapshot mechanics are portable; host-path validation requires
        # Linux, so bypass only that validation on this Windows test runner.
        validation = patch.object(self.manager, "_validate_source")
        validation.start()
        self.addCleanup(validation.stop)

    def git(self, *arguments: str, cwd: Path | None = None) -> str:
        try:
            result = subprocess.run(
                ["git", *arguments], cwd=cwd, check=True, text=True,
                encoding="utf-8", errors="replace", stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            )
        except subprocess.CalledProcessError as exc:
            self.fail(f"Local git command failed: {exc.stderr}")
        return result.stdout.strip()

    def commit_version(self, version: str) -> str:
        (self.upstream / "version.txt").write_text(version + "\n", encoding="utf-8")
        self.git("add", "--", "src/Kconfig", "version.txt", cwd=self.upstream)
        self.git("-c", "user.name=Test", "-c", "user.email=test@example.invalid",
                 "commit", "-m", version, cwd=self.upstream)
        return self.git("rev-parse", "HEAD", cwd=self.upstream)

    def active_state(self) -> tuple[str, str, str, str]:
        return (
            self.git("rev-parse", "HEAD", cwd=self.active),
            self.git("rev-parse", "origin/master", cwd=self.active),
            self.git("status", "--porcelain", cwd=self.active),
            (self.active / ".config").read_text(encoding="utf-8"),
        )

    def test_latest_snapshot_fetches_remote_commit_without_changing_active_checkout(self):
        previous = self.active_state()
        snapshot = self.root / "latest-snapshot"
        starting, target = self.manager._snapshot(self.printer, snapshot, refresh=True)
        self.assertEqual(starting, self.first)
        self.assertEqual(target, self.second)
        self.assertEqual(self.git("rev-parse", "HEAD", cwd=snapshot), self.second)
        self.assertEqual((snapshot / "version.txt").read_text(encoding="utf-8"), "second\n")
        self.assertEqual((self.active / "version.txt").read_text(encoding="utf-8"), "first\n")
        self.assertEqual(self.active_state(), previous)
        third = self.commit_version("third")
        self.assertNotEqual(third, target)
        self.assertEqual(self.git("rev-parse", "HEAD", cwd=snapshot), target)
        self.assertEqual(self.git("rev-parse", "origin/master", cwd=snapshot), target)

    def test_current_snapshot_uses_active_commit_even_when_upstream_has_advanced(self):
        previous = self.active_state()
        snapshot = self.root / "current-snapshot"
        starting, target = self.manager._snapshot(self.printer, snapshot, refresh=False)
        self.assertEqual(starting, self.first)
        self.assertEqual(target, self.first)
        self.assertEqual(self.git("rev-parse", "HEAD", cwd=snapshot), self.first)
        self.assertEqual((snapshot / "version.txt").read_text(encoding="utf-8"), "first\n")
        self.assertEqual(self.git("rev-parse", "HEAD", cwd=self.upstream), self.second)
        self.assertEqual(self.active_state(), previous)

    def test_saved_snapshot_commit_can_fast_forward_active_checkout_using_local_objects(self):
        snapshot = self.root / "prepared-snapshot"
        starting, target = self.manager._snapshot(self.printer, snapshot, refresh=True)
        self.git("fetch", "--no-tags", str(snapshot), target, cwd=self.active)
        self.git("merge-base", "--is-ancestor", starting, target, cwd=self.active)
        self.assertEqual(self.git("rev-parse", "HEAD", cwd=self.active), starting)
        self.git("merge", "--ff-only", target, cwd=self.active)
        self.assertEqual(self.git("rev-parse", "HEAD", cwd=self.active), target)
        self.assertEqual((self.active / "version.txt").read_text(encoding="utf-8"), "second\n")
        self.assertEqual((self.active / ".config").read_text(encoding="utf-8"), "CONFIG_BOARD=active_settings\n")


if __name__ == "__main__":
    unittest.main()
