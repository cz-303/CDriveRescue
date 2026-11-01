"""Real NTFS fixtures; Windows folder registration and DISM are always mocked."""
from __future__ import annotations

import io
import json
import os
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import MagicMock, patch

from src.core import system_optimizer as mod
from src.core.contracts import DuplicateGroup, KnownFolderItem, MigrationRollbackError


@unittest.skipUnless(os.name == "nt", "Requires Windows NTFS")
class OptimizerTests(unittest.TestCase):
    def setUp(self):
        # Keep fixtures outside AppData, which real folder relocation forbids.
        self.temp = tempfile.TemporaryDirectory(prefix="optimizer_test_", dir=Path.cwd())
        self.root = Path(self.temp.name)
        self.addCleanup(self.temp.cleanup)
        self.optimizer = mod.SystemOptimizer(str(self.root / "state"))
        self.master = self.root / "master.bin"
        self.duplicate = self.root / "duplicate.bin"
        self.payload = b"immutable attachment\x00" * 2048
        self.master.write_bytes(self.payload)
        self.duplicate.write_bytes(self.payload)
        self.group = DuplicateGroup("test", "untrusted-scan-hash", "sample", len(self.payload),
                                    str(self.master), [str(self.duplicate)])
        self.backup = Path(str(self.duplicate) + "._hl_bak")

    def dedup(self, **kwargs):
        return self.optimizer.deduplicate_via_hardlink([self.group], **kwargs)

    def assert_originals(self):
        self.assertEqual(self.master.read_bytes(), self.payload)
        self.assertEqual(self.duplicate.read_bytes(), self.payload)
        self.assertFalse(os.path.samefile(self.master, self.duplicate))
        self.assertFalse(self.backup.exists())

    def test_real_hardlink(self):
        events = []
        with patch.object(mod.SafetyGuard, "validate_for_hardlink", wraps=mod.SafetyGuard.validate_for_hardlink) as guard:
            result = self.dedup(progress_cb=lambda *args: events.append(args))
        self.assertEqual(result.errors, [])
        self.assertEqual((result.processed_groups, result.linked_files_count, result.saved_bytes), (1, 1, len(self.payload)))
        self.assertTrue(os.path.samefile(self.master, self.duplicate))
        self.assertEqual(self.duplicate.read_bytes(), self.payload)
        self.assertFalse(self.backup.exists())
        self.assertEqual(events[-1][0], 100)
        guard.assert_called_once_with(str(self.master), str(self.duplicate))

    def test_dry_run_only_projects_savings(self):
        result = self.dedup(dry_run=True)
        self.assertEqual(result.errors, [])
        self.assertEqual(result.saved_bytes, len(self.payload))
        self.assert_originals()

    def test_already_linked_skips(self):
        self.duplicate.unlink()
        os.link(self.master, self.duplicate)
        result = self.dedup()
        self.assertEqual((result.linked_files_count, result.saved_bytes, result.errors), (0, 0, []))

    def test_duplicate_entries_count_once_in_preview(self):
        self.group.duplicate_paths *= 2
        result = self.dedup(dry_run=True)
        self.assertEqual(result.linked_files_count, 1)
        self.assert_originals()

    def test_full_hash_rejects_same_size_different_content(self):
        self.duplicate.write_bytes(b"x" * len(self.payload))
        result = self.dedup()
        self.assertEqual(result.linked_files_count, 0)
        self.assertEqual(len(result.errors), 1)
        self.assertEqual(self.duplicate.read_bytes(), b"x" * len(self.payload))

    def test_backup_collision_is_never_overwritten(self):
        self.backup.write_bytes(b"previous recovery data")
        result = self.dedup()
        self.assertEqual(result.linked_files_count, 0)
        self.assertEqual(self.backup.read_bytes(), b"previous recovery data")
        self.assertEqual(self.duplicate.read_bytes(), self.payload)

    def test_link_failure_rolls_back(self):
        with patch.object(mod.os, "link", side_effect=OSError("injected link failure")):
            result = self.dedup()
        self.assertEqual(len(result.errors), 1)
        self.assert_originals()

    def test_verification_failure_rolls_back(self):
        with patch.object(mod.os.path, "samefile", return_value=False):
            result = self.dedup()
        self.assertEqual(len(result.errors), 1)
        self.assert_originals()

    def test_backup_removal_failure_rolls_back(self):
        unlink = os.unlink

        def fail_backup(path, *args, **kwargs):
            if Path(path) == self.backup:
                raise PermissionError("injected backup removal failure")
            return unlink(path, *args, **kwargs)

        with patch.object(mod.os, "unlink", side_effect=fail_backup):
            result = self.dedup()
        self.assertEqual(len(result.errors), 1)
        self.assert_originals()

    def test_rollback_collision_preserves_both_files(self):
        def replace_during_link(*args):
            self.duplicate.write_bytes(b"concurrent new data")
            raise OSError("injected collision")

        with patch.object(mod.os, "link", side_effect=replace_during_link):
            result = self.dedup()
        self.assertEqual(len(result.errors), 1)
        self.assertEqual(self.backup.read_bytes(), self.payload)
        self.assertEqual(self.duplicate.read_bytes(), b"concurrent new data")

    def test_keyboard_interrupt_restores_original(self):
        with patch.object(mod.os, "link", side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                self.dedup()
        self.assert_originals()

    def test_existing_multiple_links_not_counted_as_saved(self):
        os.link(self.duplicate, self.root / "third.bin")
        result = self.dedup()
        self.assertEqual(result.saved_bytes, 0)
        self.assertEqual(len(result.errors), 1)
        self.assert_originals()

    def test_database_duplicate_is_rejected(self):
        db = self.duplicate.with_suffix(".db")
        self.duplicate.rename(db)
        self.group.duplicate_paths = [str(db)]
        result = self.dedup()
        self.assertEqual(result.saved_bytes, 0)
        self.assertEqual(db.read_bytes(), self.payload)

    def test_alternate_stream_is_preserved_by_refusal(self):
        stream = str(self.duplicate) + ":Zone.Identifier"
        with open(stream, "wb") as writer:
            writer.write(b"[ZoneTransfer]\r\nZoneId=3")
        result = self.dedup()
        self.assertEqual(result.saved_bytes, 0)
        self.assertEqual(len(result.errors), 1)
        with open(stream, "rb") as reader:
            self.assertEqual(reader.read(), b"[ZoneTransfer]\r\nZoneId=3")
        self.assert_originals()

    def test_permissions_difference_rejected(self):
        with patch.object(mod, "_security", side_effect=[b"acl1", b"acl2"]):
            result = self.dedup()
        self.assertEqual(result.saved_bytes, 0)
        self.assert_originals()

    def test_cross_volume_is_counted_and_preserved(self):
        with tempfile.TemporaryDirectory() as other_dir:
            other = Path(other_dir) / "duplicate.bin"
            other.write_bytes(self.payload)
            if other.stat().st_dev == self.master.stat().st_dev:
                self.skipTest("No second test volume")
            self.group.duplicate_paths = [str(other)]
            result = self.dedup()
            self.assertEqual(result.skipped_cross_volume, 1)
            self.assertEqual(result.saved_bytes, 0)
            self.assertEqual(other.read_bytes(), self.payload)

    def test_path_guards_reject_ambiguous_and_protected_paths(self):
        for value in ("relative", "\\root_relative", "F:\\", "C:\\Windows\\test.bin",
                      str(self.root / "file:stream"), str(self.root / "trailing.")):
            with self.subTest(value=value), self.assertRaises(mod.CriticalPathForbiddenError):
                mod._path(value)

    def test_native_lock_denies_content_write(self):
        with mod._read_locked(self.master):
            with self.assertRaises(PermissionError):
                self.master.write_bytes(b"overwrite")
        self.assertEqual(self.master.read_bytes(), self.payload)

    def test_callback_failure_does_not_break_commit(self):
        with self.assertLogs("src.core.file_ops", level="ERROR"):
            result = self.dedup(progress_cb=MagicMock(side_effect=RuntimeError("UI failed")))
        self.assertEqual(result.errors, [])
        self.assertTrue(os.path.samefile(self.master, self.duplicate))

    def test_operation_is_not_reentrant(self):
        with self.optimizer._operation():
            with self.assertRaises(RuntimeError):
                self.dedup()
        self.assertEqual(self.dedup().errors, [])

    def folder_fixture(self):
        self.folders = {}
        for key, guid in mod._ALLOWED_FOLDERS.items():
            folder = self.root / key
            folder.mkdir()
            self.folders[guid] = str(folder)
        self.source = self.root / "Downloads"
        (self.source / "sub").mkdir()
        (self.source / "empty").mkdir()
        (self.source / "sub" / "attachment.bin").write_bytes(self.payload)
        (self.source / "note.txt").write_bytes(b"note")
        self.target = self.root / "new_downloads"
        self.item = KnownFolderItem("Downloads", "Downloads", mod._ALLOWED_FOLDERS["Downloads"],
                                    str(self.source), str(self.target), len(self.payload) + 4, 2, False)

        def get(guid):
            return self.folders[str(mod.uuid.UUID(guid))]

        def set_path(guid, path):
            self.folders[str(mod.uuid.UUID(guid))] = str(path)

        get_patch = patch.object(self.optimizer, "_get_known_path", side_effect=get)
        set_patch = patch.object(self.optimizer, "_set_known_path", side_effect=set_path)
        self.getter = get_patch.start()
        self.setter = set_patch.start()
        self.addCleanup(get_patch.stop)
        self.addCleanup(set_patch.stop)

    def relocate(self, **kwargs):
        return self.optimizer.relocate_known_folder(self.item, str(self.target), **kwargs)

    def test_relocation_real_files_and_mocked_shell(self):
        self.folder_fixture()
        events = []
        with patch.object(mod.SafetyGuard, "check_target_free_space", wraps=mod.SafetyGuard.check_target_free_space) as space:
            result = self.relocate(progress_cb=lambda *args: events.append(args))
        space.assert_any_call(self.item.size_bytes, str(self.target))
        self.assertEqual(result.current_path, str(self.target))
        self.assertEqual((result.size_bytes, result.file_count), (len(self.payload) + 4, 2))
        self.assertEqual(self.item.current_path, str(self.source))
        self.assertEqual((self.target / "sub" / "attachment.bin").read_bytes(), self.payload)
        self.assertTrue((self.target / "empty").is_dir())
        self.assertFalse(self.source.exists())
        self.assertFalse(self.optimizer.journal_path.exists())
        self.assertEqual(list(self.root.glob("Downloads._kf_bak_*")), [])
        self.assertEqual(events[-1][0], 100)

    def test_redirect_without_moving_preserves_original(self):
        self.folder_fixture()
        result = self.relocate(move_existing_files=False)
        self.assertEqual((result.size_bytes, result.file_count), (0, 0))
        self.assertEqual((self.source / "sub" / "attachment.bin").read_bytes(), self.payload)
        self.assertEqual(list(self.target.iterdir()), [])

    def test_shell_failure_restores_original_folder(self):
        self.folder_fixture()
        self.setter.side_effect = OSError("injected shell failure")
        with self.assertRaises(MigrationRollbackError) as error:
            self.relocate()
        self.assertTrue(error.exception.rolled_back_cleanly)
        self.assertEqual((self.source / "sub" / "attachment.bin").read_bytes(), self.payload)
        self.assertEqual((self.target / "sub" / "attachment.bin").read_bytes(), self.payload)
        self.assertFalse(self.optimizer.journal_path.exists())

    def test_copy_failure_leaves_original_and_no_redirect(self):
        self.folder_fixture()
        with patch.object(self.optimizer, "_copy_files", side_effect=OSError("injected disk full")):
            with self.assertRaises(MigrationRollbackError) as error:
                self.relocate()
        self.assertTrue(error.exception.rolled_back_cleanly)
        self.setter.assert_not_called()
        self.assertEqual((self.source / "sub" / "attachment.bin").read_bytes(), self.payload)

    def test_copy_mutation_detected_before_switch(self):
        self.folder_fixture()
        copier = self.optimizer._copy_files

        def mutate(*args):
            copier(*args)
            (self.source / "note.txt").write_bytes(b"changed by application")

        with patch.object(self.optimizer, "_copy_files", side_effect=mutate):
            with self.assertRaises(MigrationRollbackError):
                self.relocate()
        self.setter.assert_not_called()
        self.assertEqual((self.source / "note.txt").read_bytes(), b"changed by application")

    def test_stale_known_folder_item_rejected(self):
        self.folder_fixture()
        self.item = replace(self.item, current_path=str(self.root / "old_location"))
        with self.assertRaises(ValueError):
            self.relocate()
        self.setter.assert_not_called()

    def test_mismatched_guid_rejected(self):
        self.folder_fixture()
        self.item = replace(self.item, folder_guid=mod._ALLOWED_FOLDERS["Desktop"])
        with self.assertRaises(ValueError):
            self.relocate()
        self.setter.assert_not_called()

    def test_target_collision_rejected(self):
        self.folder_fixture()
        self.target.mkdir()
        (self.target / "user.txt").write_bytes(b"user data")
        with self.assertRaises(FileExistsError):
            self.relocate()
        self.assertEqual((self.target / "user.txt").read_bytes(), b"user data")
        self.setter.assert_not_called()

    def test_nested_target_rejected(self):
        self.folder_fixture()
        self.target = self.source / "nested"
        with self.assertRaises(ValueError):
            self.relocate()
        self.setter.assert_not_called()

    def test_other_known_folder_target_rejected(self):
        self.folder_fixture()
        self.target = self.root / "Desktop"
        with self.assertRaises(ValueError):
            self.relocate()
        self.setter.assert_not_called()

    def test_low_space_fails_before_copying(self):
        self.folder_fixture()
        with patch.object(mod.SafetyGuard, "check_target_free_space", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                self.relocate()
        self.assertFalse(self.target.exists())
        self.setter.assert_not_called()

    def test_committed_cleanup_failure_retains_backup_and_recovery(self):
        self.folder_fixture()
        with patch.object(self.optimizer, "_cleanup_known_backup", side_effect=PermissionError("injected cleanup failure")):
            with self.assertLogs(mod.__name__, level="WARNING"):
                result = self.relocate()
        self.assertEqual(result.current_path, str(self.target))
        self.assertEqual(len(self.optimizer.last_warnings), 1)
        tx = json.loads(self.optimizer.journal_path.read_text(encoding="utf-8"))
        backup = Path(tx["backup"])
        self.assertEqual((backup / "sub" / "attachment.bin").read_bytes(), self.payload)
        self.optimizer.recover_known_folder_transaction()
        self.assertTrue(backup.exists())
        self.assertFalse(self.optimizer.journal_path.exists())

    def test_uncommitted_crash_journal_restores_source(self):
        self.folder_fixture()
        backup = self.source.with_name("Downloads._kf_bak_test")
        self.source.rename(backup)
        self.target.mkdir()
        self.optimizer.state_dir.mkdir()
        mod.atomic_json(self.optimizer.journal_path, {
            "version": 1, "guid": self.item.folder_guid, "folder_key": "Downloads",
            "source": str(self.source), "target": str(self.target), "backup": str(backup),
            "move_files": True, "phase": "switching",
        })
        self.optimizer.recover_known_folder_transaction()
        self.assertEqual((self.source / "sub" / "attachment.bin").read_bytes(), self.payload)
        self.assertFalse(self.optimizer.journal_path.exists())

    def test_committed_target_modified_keeps_original_backup(self):
        self.folder_fixture()
        cleaner = self.optimizer._cleanup_known_backup

        def mutate(backup, target, manifest):
            (target / "note.txt").write_bytes(b"changed after switch")
            return cleaner(backup, target, manifest)

        with patch.object(self.optimizer, "_cleanup_known_backup", side_effect=mutate):
            with self.assertLogs(mod.__name__, level="WARNING"):
                self.relocate()
        tx = json.loads(self.optimizer.journal_path.read_text(encoding="utf-8"))
        self.assertEqual((Path(tx["backup"]) / "note.txt").read_bytes(), b"note")
        self.assertEqual((self.target / "note.txt").read_bytes(), b"changed after switch")

    def test_commit_journal_failure_restores_shell_and_source(self):
        self.folder_fixture()
        save = mod.atomic_json

        def fail_commit(path, data):
            if data["phase"] == "committed":
                raise OSError("injected journal failure")
            save(path, data)

        with patch.object(mod, "atomic_json", side_effect=fail_commit):
            with self.assertRaises(MigrationRollbackError) as error:
                self.relocate()
        self.assertTrue(error.exception.rolled_back_cleanly)
        self.assertEqual(self.folders[self.item.folder_guid], str(self.source))
        self.assertEqual((self.source / "note.txt").read_bytes(), b"note")
        self.assertEqual((self.target / "note.txt").read_bytes(), b"note")

    def test_recreated_source_retains_all_copies(self):
        self.folder_fixture()
        setter = self.setter.side_effect

        def recreate(guid, path):
            setter(guid, path)
            self.source.mkdir()
            (self.source / "fresh.txt").write_bytes(b"fresh")

        self.setter.side_effect = recreate
        with self.assertLogs(mod.__name__, level="WARNING"):
            self.relocate()
        tx = json.loads(self.optimizer.journal_path.read_text(encoding="utf-8"))
        self.assertEqual((Path(tx["backup"]) / "note.txt").read_bytes(), b"note")
        self.assertEqual((self.source / "fresh.txt").read_bytes(), b"fresh")
        self.assertEqual((self.target / "note.txt").read_bytes(), b"note")

    def run_dism(self, output=b"[ 10.0%]\r[ 55.5%]\r[100.0%]\r\n", code=0):
        process = MagicMock()
        process.__enter__.return_value = process
        process.stdout = io.BytesIO(output)
        process.wait.return_value = code
        events = []
        with patch.object(mod.SafetyGuard, "is_admin", return_value=True), \
             patch.object(self.optimizer, "_dism_executable", return_value=Path("C:/Windows/System32/Dism.exe")), \
             patch.object(mod.subprocess, "Popen", return_value=process) as popen:
            result = self.optimizer.slim_winsxs_component_store(lambda *args: events.append(args))
        return result, events, popen.call_args

    def test_dism_streams_percentages_and_safe_arguments(self):
        result, events, call = self.run_dism()
        self.assertTrue(result[0])
        self.assertEqual([event[0] for event in events], [0, 10, 55, 99, 100])
        self.assertIn("/StartComponentCleanup", call.args[0])
        self.assertNotIn("/ResetBase", call.args[0])
        self.assertIn("/NoRestart", call.args[0])
        self.assertFalse(call.kwargs["shell"])
        self.assertEqual(call.kwargs["creationflags"], mod.subprocess.CREATE_NO_WINDOW)

    def test_dism_utf16_and_decimal_comma(self):
        result, events, _ = self.run_dism("[20,5%]\r[90.0%]".encode("utf-16"), 3010)
        self.assertTrue(result[0])
        self.assertEqual([event[0] for event in events], [0, 20, 90, 100])

    def test_dism_error_never_reports_success(self):
        result, events, _ = self.run_dism(b"[100.0%]\rError: 5\r\nAccess denied", 5)
        self.assertFalse(result[0])
        self.assertIn("Access denied", result[1])
        self.assertNotIn(100, [event[0] for event in events])

    def test_dism_requires_admin(self):
        with patch.object(mod.SafetyGuard, "is_admin", return_value=False), \
             patch.object(mod.subprocess, "Popen") as popen:
            result = self.optimizer.slim_winsxs_component_store()
        self.assertFalse(result[0])
        popen.assert_not_called()

    def test_dism_launch_error_is_returned(self):
        with patch.object(mod.SafetyGuard, "is_admin", return_value=True), \
             patch.object(self.optimizer, "_dism_executable", side_effect=FileNotFoundError("missing dism")):
            result = self.optimizer.slim_winsxs_component_store()
        self.assertFalse(result[0])
        self.assertIn("missing dism", result[1])


if __name__ == "__main__":
    unittest.main()
