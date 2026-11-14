"""Updater integration tests never download releases or restart services."""
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch
import zipfile

from kcm.engine import UpdateError
from kcm.moonraker import (APP_NAME, ASSET_NAME, INLINE_BEGIN, INLINE_END, MANAGED_MARKER, SNIPPET_NAME,
                           Installer, render_git_update_config, render_update_config)
from kcm.storage import Store


REPOSITORY = "TestUser/klipper-component-manager"


class RenderTests(unittest.TestCase):
    def test_zip_updater_targets_stable_standalone_app_without_service_or_flash_hooks(self):
        text = render_update_config(REPOSITORY, Path("/home/pi/klipper-component-manager-app"))
        self.assertIn("type: zip\n", text)
        self.assertIn("channel: stable\n", text)
        self.assertIn(f"repo: {REPOSITORY}\n", text)
        self.assertIn("is_system_service: False\n", text)
        self.assertIn("path: /home/pi/klipper-component-manager-app\n", text)
        for dangerous_or_wrong in ("asset_name:", "managed_services:", "install_script:", "action=", "persistent_files:"):
            self.assertNotIn(dangerous_or_wrong, text)

    def test_github_repository_url_is_normalized(self):
        text = render_update_config("https://github.com/TestUser/klipper-component-manager.git/", Path("/home/pi/app"))
        self.assertIn(f"repo: {REPOSITORY}\n", text)

    def test_repository_and_path_cannot_inject_configuration(self):
        for repository in ("x/y\nmanaged_services: klipper", "x/y/tree/main", "https://evil.example/x/y", "x/.."):
            with self.subTest(repository=repository), self.assertRaises(UpdateError):
                render_update_config(repository, Path("/home/pi/app"))
        for path in ("relative/app", "/home/pi/a\n[bad]", "/home/pi/a # comment", "/home/pi/../app"):
            with self.subTest(path=path), self.assertRaises(UpdateError):
                render_update_config(REPOSITORY, Path(path))


class InstallerTests(unittest.TestCase):
    def setUp(self):
        # This sibling of the source checkout is writable in the desktop
        # sandbox, and remains outside the Git checkout when these run in CI.
        temporary = tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parents[2])
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.store = Store(self.root / "saved-data")
        self.app = self.root / "kcm-app"
        self.config = self.root / "printer-data/config/moonraker.conf"
        self.config.parent.mkdir(parents=True)
        self.original = b"# My original configuration\r\n[server]\r\nport: 7125\r\n"
        self.config.write_bytes(self.original)
        self.messages = []
        self.installer = Installer(self.store, self.messages.append)

    def stage(self, **metadata_changes):
        self.app.mkdir(exist_ok=True)
        with zipfile.ZipFile(self.app / APP_NAME, "w") as archive:
            archive.writestr("__main__.py", "print('test app')\n")
        metadata = {"project_owner": "TestUser", "project_name": "klipper-component-manager",
                    "version": "v0.2.0", "asset_name": ASSET_NAME}
        metadata.update(metadata_changes)
        (self.app / "release_info.json").write_text(json.dumps(metadata), encoding="utf-8")

    def install(self, confirm=lambda _: True):
        return self.installer.install(REPOSITORY, self.app, self.config, confirm)

    def test_new_target_preflight_is_read_only_and_does_not_expose_existing_config(self):
        self.config.write_bytes(self.original + b"\n[secrets]\npassword: secret-value\n")
        plan = self.installer.validate_target(REPOSITORY, self.app, self.config)
        self.assertFalse(self.app.exists())
        self.assertFalse((self.config.parent / SNIPPET_NAME).exists())
        self.assertNotIn("secret-value", json.dumps(plan))
        self.assertEqual(plan["repository"], REPOSITORY)
        self.assertTrue(plan["changed"])

    def test_success_preserves_original_bytes_and_backs_up_before_config_edits(self):
        self.stage()
        result = self.install()
        updated = self.config.read_bytes()
        self.assertTrue(updated.startswith(self.original))
        self.assertIn(b"[update_manager]\r\n", updated)
        self.assertNotIn(f"[include {SNIPPET_NAME}]".encode(), updated)
        self.assertIn(render_update_config(REPOSITORY, self.app).replace('\n', '\r\n').encode(), updated)
        self.assertFalse((self.config.parent / SNIPPET_NAME).exists())
        self.assertEqual((Path(result["backup_dir"]) / self.config.name).read_bytes(), self.original)
        self.assertTrue(result["restart_required"])
        self.assertFalse((self.store.root / "registry.json").exists())

    def test_repeated_install_is_idempotent_without_extra_backups_or_prompts(self):
        self.stage()
        self.install()
        first = self.config.read_bytes()
        result = self.install(lambda _: self.fail("No change should not ask for confirmation"))
        self.assertFalse(result["changed"])
        self.assertFalse(result["restart_required"])
        self.assertEqual(self.config.read_bytes(), first)
        self.assertEqual(len(list((self.store.root / "backups/moonraker").iterdir())), 1)

    def test_declined_plan_leaves_all_configuration_untouched(self):
        self.stage()
        with self.assertRaisesRegex(UpdateError, "cancelled"):
            self.install(lambda _: False)
        self.assertEqual(self.config.read_bytes(), self.original)
        self.assertFalse((self.config.parent / SNIPPET_NAME).exists())
        self.assertFalse((self.store.root / "backups").exists())

    def test_missing_app_or_release_metadata_is_not_fabricated(self):
        with self.assertRaises(UpdateError):
            self.install()
        self.stage()
        (self.app / "release_info.json").unlink()
        with self.assertRaises(UpdateError):
            self.install()
        self.assertEqual(self.config.read_bytes(), self.original)

    def test_release_metadata_selects_exact_repository_tag_and_zip_asset(self):
        for changes in ({"project_owner": "AnotherUser"}, {"project_name": "other-project"},
                        {"version": ""}, {"asset_name": "source.zip"}):
            with self.subTest(changes=changes):
                self.stage(**changes)
                with self.assertRaises(UpdateError):
                    self.install()
        self.assertEqual(self.config.read_bytes(), self.original)

    def test_invalid_python_archive_is_rejected_before_config_mutation(self):
        self.stage()
        (self.app / APP_NAME).write_bytes(b"not an archive")
        with self.assertRaises(UpdateError):
            self.install()
        self.assertEqual(self.config.read_bytes(), self.original)

    def test_application_directory_cannot_include_saved_profiles_or_config(self):
        for app in (self.store.root, self.store.root / "app", self.store.root.parent,
                    self.config.parent, self.config.parent / "app"):
            with self.subTest(app=app), self.assertRaises(UpdateError):
                self.installer.validate_target(REPOSITORY, app, self.config)

    def test_app_directory_cannot_be_inside_git_checkout(self):
        repository = self.root / "git-checkout"
        (repository / ".git").mkdir(parents=True)
        with self.assertRaisesRegex(UpdateError, "Git checkouts"):
            self.installer.validate_target(REPOSITORY, repository / "app", self.config)

    def test_app_directory_with_unrelated_files_is_protected(self):
        self.stage()
        keep = self.app / "printer.cfg"
        keep.write_text("valuable configuration", encoding="utf-8")
        with self.assertRaisesRegex(UpdateError, "unrelated files"):
            self.install()
        self.assertEqual(keep.read_text(), "valuable configuration")

    def test_existing_primary_update_manager_in_included_file_is_reused(self):
        self.stage()
        self.config.write_bytes(self.original + b"\r\n[include services.conf]\r\n")
        (self.config.parent / "services.conf").write_text("[update_manager]\nrefresh_interval: 24\n", encoding="utf-8")
        self.install()
        self.assertNotIn(b"[update_manager]\r\n", self.config.read_bytes())
        self.assertIn(b'[update_manager klipper-component-manager]', self.config.read_bytes())
        self.assertFalse((self.config.parent / SNIPPET_NAME).exists())

    def test_existing_wildcard_include_does_not_receive_duplicate_direct_include(self):
        self.stage()
        self.config.write_bytes(self.original + b"\r\n[update_manager]\r\n[include *.conf]\r\n")
        self.install()
        self.assertNotIn(f"[include {SNIPPET_NAME}]".encode(), self.config.read_bytes())
        self.assertFalse((self.config.parent / SNIPPET_NAME).exists())

    def test_dangling_legacy_relative_include_is_removed_for_inline_setup(self):
        self.stage()
        self.config.write_bytes(self.original + f"\r\n[update_manager]\r\n[include ./{SNIPPET_NAME}]\r\n".encode())
        self.install()
        self.assertNotIn(SNIPPET_NAME.encode(), self.config.read_bytes())
        self.assertIn(b'[update_manager klipper-component-manager]', self.config.read_bytes())

    def test_extra_file_with_unrelated_settings_is_preserved(self):
        self.stage()
        snippet = self.config.parent / SNIPPET_NAME
        snippet.write_text("# My own file\n[custom]\nx: y\n", encoding="utf-8")
        self.config.write_bytes(self.original + f'\r\n[include {SNIPPET_NAME}]\r\n'.encode())
        self.install()
        self.assertEqual(snippet.read_text(), "# My own file\n[custom]\nx: y\n")
        self.assertIn(b'[update_manager klipper-component-manager]', self.config.read_bytes())
        self.assertIn(f'[include {SNIPPET_NAME}]'.encode(), self.config.read_bytes())

    def test_existing_kcm_section_in_another_include_is_not_silently_overridden(self):
        self.stage()
        self.config.write_bytes(self.original + b"\n[include custom.conf]\n")
        (self.config.parent / "custom.conf").write_text("[update_manager klipper-component-manager]\ntype: git_repo\n", encoding="utf-8")
        with self.assertRaisesRegex(UpdateError, "already configured"):
            self.install()
        self.assertFalse((self.config.parent / SNIPPET_NAME).exists())

    def test_concurrent_main_or_included_config_edits_block_stale_plan(self):
        self.stage()
        self.config.write_bytes(self.original + b"\n[include services.conf]\n")
        included = self.config.parent / "services.conf"
        included.write_text("[update_manager]\n", encoding="utf-8")
        def confirm(_):
            included.write_text("[update_manager]\nrefresh_interval: 48\n", encoding="utf-8")
            return True
        with self.assertRaisesRegex(UpdateError, "changed while awaiting"):
            self.install(confirm)
        self.assertFalse((self.config.parent / SNIPPET_NAME).exists())

    def test_main_write_failure_preserves_original_main(self):
        self.stage()
        import kcm.moonraker as module
        original_writer = module._write_atomic
        def fail_main(path, data, mode=0o600):
            if path == self.config:
                raise OSError("simulated disk failure")
            return original_writer(path, data, mode)
        with patch.object(module, "_write_atomic", side_effect=fail_main), self.assertRaises(OSError):
            self.install()
        self.assertEqual(self.config.read_bytes(), self.original)
        self.assertFalse((self.config.parent / SNIPPET_NAME).exists())
        self.assertEqual(len(list((self.store.root / "backups/moonraker").iterdir())), 1)

    def test_managed_inline_update_keeps_backup_of_previous_configuration(self):
        self.stage()
        self.install()
        changed = self.config.read_bytes().replace(b"channel: stable", b"channel: beta")
        self.config.write_bytes(changed)
        result = self.install()
        self.assertIn(b"channel: stable", self.config.read_bytes())
        self.assertEqual((Path(result["backup_dir"]) / self.config.name).read_bytes(), changed)
        self.assertEqual(self.config.read_bytes().count(MANAGED_MARKER.encode()), 1)

    def legacy(self, *, wildcard=False, extra=b''):
        snippet = self.config.parent / SNIPPET_NAME
        legacy = render_update_config(REPOSITORY, self.app).encode() + extra
        snippet.write_bytes(legacy)
        include = b'[include *.conf]' if wildcard else f'[include {SNIPPET_NAME}]'.encode()
        original = self.original + b'\r\n[update_manager]\r\n' + include + b'\r\n'
        self.config.write_bytes(original)
        return snippet, legacy, original

    def test_legacy_updater_moves_inline_and_preserves_extra_settings_and_include(self):
        self.stage()
        extra = b'\n# My webcam settings\n[webcam my_camera]\nlocation: frame\nsnapshot_url: http://camera/snapshot\n'
        snippet, old_extra, old_main = self.legacy(extra=extra)
        result = self.install()
        self.assertTrue(snippet.exists())
        self.assertIn(extra, snippet.read_bytes())
        self.assertIn(MANAGED_MARKER.encode(), snippet.read_bytes())
        self.assertNotIn(b'[update_manager klipper-component-manager]', snippet.read_bytes())
        self.assertIn(f'[include {SNIPPET_NAME}]'.encode(), self.config.read_bytes())
        self.assertEqual(self.config.read_bytes().count(b'[update_manager klipper-component-manager]'), 1)
        backup = Path(result['backup_dir'])
        self.assertEqual((backup / self.config.name).read_bytes(), old_main)
        self.assertEqual((backup / SNIPPET_NAME).read_bytes(), old_extra)
        after = snippet.read_bytes(), self.config.read_bytes()
        self.assertFalse(self.install(lambda _: self.fail('Repeated migration must be a no-op'))['changed'])
        self.assertEqual((snippet.read_bytes(), self.config.read_bytes()), after)

    def test_legacy_wildcard_include_and_other_settings_remain_loaded(self):
        self.stage()
        extra = b'\n# Existing update preferences\n[update_manager]\nrefresh_interval: 24\n'
        snippet, _, _ = self.legacy(wildcard=True, extra=extra)
        self.install()
        self.assertIn(extra, snippet.read_bytes())
        self.assertIn(b'[include *.conf]', self.config.read_bytes())
        self.assertNotIn(b'[update_manager klipper-component-manager]', snippet.read_bytes())
        self.assertEqual(self.config.read_bytes().count(b'[update_manager]\r\n'), 1)

    def test_nested_legacy_include_keeps_other_configuration_unchanged(self):
        self.stage()
        snippet, _, _ = self.legacy(extra=b'\n[custom]\nvalue: keep\n')
        self.config.write_bytes(self.original + b'\r\n[include services.conf]\r\n')
        included = self.config.parent / 'services.conf'
        services = (b'# Other services\n[update_manager]\nrefresh_interval: 48\n\n'
                    + f'[include ./{SNIPPET_NAME}]\n'.encode()
                    + b'\n[update_manager other_app]\ntype: git_repo\npath: ~/another\n')
        included.write_bytes(services)
        self.install()
        self.assertEqual(included.read_bytes(), services)
        self.assertIn(b'[custom]\nvalue: keep\n', snippet.read_bytes())
        self.assertNotIn(b'[update_manager klipper-component-manager]', snippet.read_bytes())
        self.assertIn(b'[update_manager klipper-component-manager]', self.config.read_bytes())

    def test_declining_legacy_migration_preserves_both_files(self):
        self.stage()
        snippet, old_extra, old_main = self.legacy(extra=b'\n[custom]\nvalue: keep\n')
        with self.assertRaisesRegex(UpdateError, 'cancelled'):
            self.install(lambda _: False)
        self.assertEqual(snippet.read_bytes(), old_extra)
        self.assertEqual(self.config.read_bytes(), old_main)
        self.assertFalse((self.store.root / 'backups').exists())

    def test_unmanaged_legacy_updater_requires_review(self):
        self.stage()
        snippet, old_extra, old_main = self.legacy()
        unmanaged = old_extra.replace((MANAGED_MARKER + '\n').encode(), b'', 1)
        snippet.write_bytes(unmanaged)
        with self.assertRaises(UpdateError):
            self.install()
        self.assertEqual(snippet.read_bytes(), unmanaged)
        self.assertEqual(self.config.read_bytes(), old_main)

    def test_legacy_file_edit_during_confirmation_is_not_overwritten(self):
        self.stage()
        snippet, old_extra, old_main = self.legacy()
        changed = old_extra + b'\n[custom]\nvalue: a concurrent edit\n'
        def confirm(_):
            snippet.write_bytes(changed)
            return True
        with self.assertRaisesRegex(UpdateError, 'changed while awaiting'):
            self.install(confirm)
        self.assertEqual(snippet.read_bytes(), changed)
        self.assertEqual(self.config.read_bytes(), old_main)

    def test_migration_write_failure_restores_all_affected_files(self):
        self.stage()
        snippet, old_extra, old_main = self.legacy(extra=b'\n[custom]\nvalue: keep\n')
        import kcm.moonraker as module
        writer = module._write_atomic
        destinations = []
        def fail_second_configuration_write(path, data, mode=0o600):
            if path in {self.config, snippet}:
                destinations.append(path)
                if len(destinations) == 2:
                    raise OSError('simulated migration failure')
            return writer(path, data, mode)
        with patch.object(module, '_write_atomic', side_effect=fail_second_configuration_write):
            with self.assertRaisesRegex(OSError, 'simulated migration failure'):
                self.install()
        self.assertGreaterEqual(len(destinations), 2)
        self.assertEqual(snippet.read_bytes(), old_extra)
        self.assertEqual(self.config.read_bytes(), old_main)

    def test_new_wildcard_conflict_during_confirmation_blocks_stale_plan(self):
        self.stage()
        original = self.original + b'\n[include extensions/*.conf]\n'
        self.config.write_bytes(original)
        directory = self.config.parent / 'extensions'
        directory.mkdir()
        def confirm(_):
            (directory / 'new.conf').write_text('[update_manager klipper-component-manager]\ntype: git_repo\n', encoding='utf-8')
            return True
        with self.assertRaises(UpdateError):
            self.install(confirm)
        self.assertEqual(self.config.read_bytes(), original)

    def test_utf8_bom_and_original_crlf_bytes_are_preserved(self):
        self.stage()
        original = b'\xef\xbb\xbf' + self.original
        self.config.write_bytes(original)
        self.install()
        updated = self.config.read_bytes()
        self.assertTrue(updated.startswith(original))
        self.assertNotIn(b'\n', updated.replace(b'\r\n', b''))
        self.assertFalse(self.install(lambda _: self.fail('BOM must not defeat idempotence'))['changed'])

    def test_matching_manual_inline_section_is_adopted_without_duplicate(self):
        self.stage()
        manual = render_update_config(REPOSITORY, self.app).replace(MANAGED_MARKER + '\n', '', 1)
        self.config.write_bytes(self.original + b'\r\n[update_manager]\r\n' + manual.encode())
        self.install()
        self.assertEqual(self.config.read_bytes().count(b'[update_manager klipper-component-manager]'), 1)
        self.assertFalse((self.config.parent / SNIPPET_NAME).exists())

    def test_manual_inline_section_pointing_to_another_app_is_not_overwritten(self):
        self.stage()
        manual = render_update_config(REPOSITORY, self.root / 'another-app').replace(MANAGED_MARKER + '\n', '', 1)
        original = self.original + manual.encode()
        self.config.write_bytes(original)
        with self.assertRaises(UpdateError):
            self.install()
        self.assertEqual(self.config.read_bytes(), original)

    def test_existing_main_block_and_legacy_copy_converge_to_one_updater(self):
        self.stage()
        snippet, _, original = self.legacy(extra=b'\n[custom]\nvalue: keep\n')
        self.config.write_bytes(original + render_update_config(REPOSITORY, self.app).encode())
        self.install()
        self.assertEqual(self.config.read_bytes().count(b'[update_manager klipper-component-manager]'), 1)
        self.assertNotIn(b'[update_manager klipper-component-manager]', snippet.read_bytes())
        self.assertIn(b'[custom]\nvalue: keep\n', snippet.read_bytes())

    def test_malformed_inline_boundaries_are_rejected_without_losing_user_options(self):
        self.stage()
        self.install()
        valid = self.config.read_bytes()
        begin = INLINE_BEGIN.encode()
        end = INLINE_END.encode()
        for changed in (valid.replace(begin, begin + b'\r\nmax_upload_size: 4096', 1),
                        valid.replace(end, end + b'\r\nchannel: beta', 1)):
            with self.subTest(changed=changed):
                self.config.write_bytes(changed)
                with self.assertRaises(UpdateError):
                    self.install()
                self.assertEqual(self.config.read_bytes(), changed)


class GitInstallerTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parents[2])
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.checkout = self.root / "checkout"
        self.checkout.mkdir()
        self.store = Store(self.root / "saved-data")
        self.config = self.root / "printer-data/config/moonraker.conf"
        self.config.parent.mkdir(parents=True)
        self.original = b"[server]\nport: 7125\n"
        self.config.write_bytes(self.original)
        (self.checkout / "kcm").mkdir()
        (self.checkout / "kcm/cli.py").write_text("def main(): pass\n", encoding="utf-8")
        launcher = self.checkout / "kcm.sh"
        launcher.write_text("#!/usr/bin/env bash\npython3 -m kcm\n", encoding="utf-8")
        launcher.chmod(0o755)
        self.git("init", "-b", "main")
        self.git("config", "user.name", "KCM Test")
        self.git("config", "user.email", "test@example.invalid")
        self.git("add", "kcm.sh", "kcm/cli.py")
        self.git("commit", "-m", "Fixture application")
        self.git("remote", "add", "origin", f"https://github.com/{REPOSITORY}.git")
        self.git("config", "branch.main.remote", "origin")
        self.git("config", "branch.main.merge", "refs/heads/main")
        self.installer = Installer(self.store, output=lambda _: None)

    def git(self, *arguments):
        return subprocess.run(["git", "-C", str(self.checkout), *arguments],
                              capture_output=True, text=True, check=True, timeout=10).stdout.strip()

    def install(self, confirm=lambda _: True):
        return self.installer.install_git(REPOSITORY, self.checkout, self.config, confirm)

    def test_git_renderer_uses_main_without_release_metadata_or_service_hooks(self):
        text = render_git_update_config(REPOSITORY, Path("/home/pi/KCM"))
        for field in ("type: git_repo\n", "channel: dev\n", "primary_branch: main\n",
                      f"origin: https://github.com/{REPOSITORY}.git\n", "is_system_service: False\n"):
            self.assertIn(field, text)
        for forbidden in ("asset_name:", "managed_services:", "install_script:", "action=", "release_info"):
            self.assertNotIn(forbidden, text)

    def test_real_checkout_preflight_is_read_only_and_needs_no_zip_metadata(self):
        plan = self.installer.validate_git_target(REPOSITORY, self.checkout, self.config)
        self.assertEqual(plan["mode"], "git_repo")
        self.assertEqual(plan["checkout"], str(self.checkout))
        self.assertFalse((self.checkout / "release_info.json").exists())
        self.assertEqual(self.config.read_bytes(), self.original)
        self.assertEqual(self.git("status", "--porcelain"), "")
        self.assertFalse((self.config.parent / SNIPPET_NAME).exists())

    def test_git_install_backs_up_preserves_checkout_and_is_idempotent(self):
        commit = self.git("rev-parse", "HEAD")
        result = self.install()
        self.assertEqual((Path(result["backup_dir"]) / self.config.name).read_bytes(), self.original)
        self.assertIn("type: git_repo", self.config.read_text())
        self.assertFalse((self.config.parent / SNIPPET_NAME).exists())
        self.assertEqual(self.git("rev-parse", "HEAD"), commit)
        self.assertEqual(self.git("status", "--porcelain"), "")
        repeated = self.install(lambda _: self.fail("Identical configuration must not prompt"))
        self.assertFalse(repeated["changed"])

    def test_git_decline_makes_no_configuration_or_checkout_changes(self):
        with self.assertRaisesRegex(UpdateError, "cancelled"):
            self.install(lambda _: False)
        self.assertEqual(self.config.read_bytes(), self.original)
        self.assertFalse((self.config.parent / SNIPPET_NAME).exists())
        self.assertFalse((self.store.root / "backups").exists())
        self.assertEqual(self.git("status", "--porcelain"), "")

    def test_origin_identity_is_checked_and_ssh_origin_remains_usable(self):
        for origin in (f"git@github.com:{REPOSITORY}.git", f"ssh://git@github.com/{REPOSITORY}.git"):
            self.git("remote", "set-url", "origin", origin)
            plan = self.installer.validate_git_target(REPOSITORY, self.checkout, self.config)
            self.assertIn(f"origin: {origin}\n", plan["config"])
        for origin in ("https://github.com/SomeoneElse/project.git", "https://token@github.com/TestUser/klipper-component-manager.git", REPOSITORY):
            self.git("remote", "set-url", "origin", origin)
            with self.subTest(origin=origin), self.assertRaises(UpdateError):
                self.installer.validate_git_target(REPOSITORY, self.checkout, self.config)

    def test_nonmain_dirty_or_missing_launcher_checkout_is_rejected(self):
        self.git("checkout", "-b", "experiment")
        with self.assertRaises(UpdateError):
            self.installer.validate_git_target(REPOSITORY, self.checkout, self.config)
        self.git("checkout", "main")
        module = self.checkout / "kcm/cli.py"
        original = module.read_bytes()
        module.write_bytes(original + b"# local modification\n")
        with self.assertRaisesRegex(UpdateError, "tracked local changes"):
            self.installer.validate_git_target(REPOSITORY, self.checkout, self.config)
        module.write_bytes(original)
        (self.checkout / "kcm.sh").unlink()
        with self.assertRaisesRegex(UpdateError, "launcher"):
            self.installer.validate_git_target(REPOSITORY, self.checkout, self.config)

    def test_git_mode_still_protects_saved_data_and_moonraker_config(self):
        nested_store = Store(self.checkout / "private-profiles")
        with self.assertRaisesRegex(UpdateError, "saved data"):
            Installer(nested_store).validate_git_target(REPOSITORY, self.checkout, self.config)
        nested_config = self.checkout / "printer-data/config/moonraker.conf"
        nested_config.parent.mkdir(parents=True)
        nested_config.write_bytes(self.original)
        with self.assertRaisesRegex(UpdateError, "configuration directory"):
            self.installer.validate_git_target(REPOSITORY, self.checkout, nested_config)

    def test_git_worktree_file_is_accepted_as_an_exact_checkout_root(self):
        worktree = self.root / "worktree"
        self.git("worktree", "add", "--force", str(worktree), "main")
        self.assertTrue((worktree / ".git").is_file())
        plan = self.installer.validate_git_target(REPOSITORY, worktree, self.config)
        self.assertEqual(plan["checkout"], str(worktree))

    def test_switch_from_managed_zip_to_git_requires_approval_and_keeps_backup(self):
        snippet = self.config.parent / SNIPPET_NAME
        old = render_update_config(REPOSITORY, self.root / "old-app")
        snippet.write_bytes(old.encode())
        self.config.write_bytes(self.original + f"\n[update_manager]\n[include {SNIPPET_NAME}]\n".encode())
        with self.assertRaisesRegex(UpdateError, "cancelled"):
            self.install(lambda _: False)
        self.assertEqual(snippet.read_text(), old)
        result = self.install()
        self.assertEqual((Path(result["backup_dir"]) / SNIPPET_NAME).read_text(), old)
        self.assertIn("type: git_repo", self.config.read_text())
        self.assertTrue(snippet.exists())
        self.assertNotIn('[update_manager klipper-component-manager]', snippet.read_text())
        self.assertIn(f'[include {SNIPPET_NAME}]', self.config.read_text())


if __name__ == "__main__":
    unittest.main()
