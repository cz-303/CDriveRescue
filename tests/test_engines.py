"""Filesystem tests operate only within temporary directories.

Migration tests stub process detection (no running application in the fixture).
They use actual NTFS junctions, actual hashing, journal I/O, and directory switches.
"""
import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from src.core.app_mover import AppMover
from src.core.cache_cleaner import CacheCleaner
from src.core.contracts import (CacheCategory, CriticalPathForbiddenError, CDriveRescueError,
                                MigrationTask, MigrationRollbackError, MigrationStatus)
from src.core.file_ops import atomic_json, inventory
from src.core.safety_guard import SafetyGuard


class Fixture(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def make_cleaner(self):
        self.cache = self.root / "cache"
        self.cache.mkdir()
        self.file = self.cache / "缓存 & 1.bin"
        self.file.write_bytes(b"cache data")
        rules = self.root / "rules.json"
        rule = dict(id="test", name="cache", category="SYSTEM", paths=[str(self.cache)],
                    risk_level="SAFE", description="test", min_age_hours=0)
        rules.write_text(json.dumps({"rules": [rule]}), encoding="utf-8")
        return CacheCleaner(rules)


class CleanerTests(Fixture):
    def test_preview_then_delete(self):
        cleaner = self.make_cleaner()
        items = cleaner.scan_caches()
        preview = cleaner.clean_selected(items, use_recycle_bin=False, dry_run=True)
        self.assertEqual(preview.cleaned_bytes, 10)
        self.assertTrue(self.file.exists())
        summary = cleaner.clean_selected(items, use_recycle_bin=False)
        self.assertEqual(summary.cleaned_files_count, 1)
        self.assertFalse(self.file.exists())
        self.assertTrue(self.cache.is_dir())

    def test_changed_file_preserved(self):
        cleaner = self.make_cleaner()
        items = cleaner.scan_caches()
        self.file.write_bytes(b"new application data")
        summary = cleaner.clean_selected(items, use_recycle_bin=False)
        self.assertEqual(summary.cleaned_files_count, 0)
        self.assertTrue(summary.errors)

    def test_forged_item_preserved(self):
        cleaner = self.make_cleaner()
        items = cleaner.scan_caches()
        items[0].matched_paths = [str(self.root)]
        self.assertTrue(cleaner.clean_selected(items, False).errors)
        self.assertTrue(self.file.exists())

    def test_new_files_not_deleted(self):
        cleaner = self.make_cleaner()
        items = cleaner.scan_caches()
        fresh = self.cache / "fresh.txt"
        fresh.write_text("keep")
        cleaner.clean_selected(items, False)
        self.assertTrue(fresh.exists())

    def test_duplicate_selection_deduplicated(self):
        cleaner = self.make_cleaner()
        items = cleaner.scan_caches()
        self.assertEqual(cleaner.clean_selected(items + items, False).cleaned_files_count, 1)

    def test_category_filter(self):
        cleaner = self.make_cleaner()
        self.assertEqual(cleaner.scan_caches(categories=[CacheCategory.BROWSER]), [])

    @unittest.skipUnless(os.name == "nt", "Windows junction")
    def test_junction_is_never_followed(self):
        cleaner = self.make_cleaner()
        target = self.root / "valuable"
        target.mkdir()
        (target / "data.db").write_bytes(b"keep")
        link = self.cache / "link"
        SafetyGuard.create_junction(str(target), str(link))
        self.addCleanup(lambda: SafetyGuard.remove_junction_safely(str(link)) if os.path.lexists(link) else None)
        items = cleaner.scan_caches()
        self.assertEqual(items[0].file_count, 1)
        cleaner.clean_selected(items, False)
        self.assertEqual((target / "data.db").read_bytes(), b"keep")

    def test_recycle_failure_does_not_permanently_delete(self):
        cleaner = self.make_cleaner()
        def fail(_):
            raise PermissionError("locked")
        with patch.dict("sys.modules", {"send2trash": SimpleNamespace(send2trash=fail)}):
            summary = cleaner.clean_selected(cleaner.scan_caches(), True)
        self.assertEqual(summary.cleaned_files_count, 0)
        self.assertEqual(summary.skipped_locked_files, 1)
        self.assertTrue(self.file.exists())


class SafetyTests(Fixture):
    def test_traversal_not_whitelisted(self):
        with self.assertRaises(CriticalPathForbiddenError):
            SafetyGuard.validate_for_cleaning(r"C:\Windows\Temp\..\System32")

    def test_root_relative_and_ads_forbidden(self):
        for path in ["D:\\", "relative", r"C:\temp\file:stream", r"\\?\C:\temp", r"C:\Users\Alice\AppData"]:
            with self.subTest(path=path), self.assertRaises(CriticalPathForbiddenError):
                SafetyGuard.validate_for_cleaning(path)

    def test_missing_process_dependency_fails_closed(self):
        with patch("src.core.safety_guard.psutil", None), self.assertRaises(CDriveRescueError):
            SafetyGuard.ensure_no_locking_processes(str(self.root))

    def test_external_process_open_file_detected(self):
        proc = SimpleNamespace(info={"pid": 999991, "name": "editor", "exe": r"C:\Other\editor.exe", "cwd": r"C:\Other"},
                               open_files=lambda: [SimpleNamespace(path=r"C:\App\data.db")])
        with patch("src.core.safety_guard.psutil", SimpleNamespace(process_iter=lambda _: [proc])):
            self.assertEqual(len(SafetyGuard.find_locking_processes(r"C:\App")), 1)


@unittest.skipUnless(os.name == "nt", "Windows migration")
class MoverTests(Fixture):
    def setUp(self):
        super().setUp()
        self.source = self.root / "source" / "中文 & App"
        self.source.mkdir(parents=True)
        (self.source / "empty").mkdir()
        (self.source / "data.bin").write_bytes(b"important" * 100)
        self.destination = self.root / "destination"
        self.mover = AppMover(self.root / "state")
        self.task = MigrationTask("app", "Test App", str(self.source), str(self.destination), 0, 0, "test")
        self.locks = patch.object(SafetyGuard, "ensure_no_locking_processes", return_value=None)
        self.locks.start()
        self.addCleanup(self.locks.stop)
        # Remove junctions before TemporaryDirectory recursively cleans fixtures.
        self.addCleanup(self.unlink_fixture_links)

    def unlink_fixture_links(self):
        for directory, dirs, _ in os.walk(self.root, followlinks=False):
            for name in dirs[:]:
                path = Path(directory) / name
                if SafetyGuard.is_junction_or_symlink(str(path)):
                    SafetyGuard.remove_junction_safely(str(path))
                    dirs.remove(name)

    def move(self):
        return self.mover.move_app_atomic(self.task, str(self.destination))

    def test_move_and_restore_real_junction(self):
        record = self.move()
        self.assertTrue(SafetyGuard.is_junction_or_symlink(str(self.source)))
        self.assertEqual(record.size_bytes, 900)
        self.assertFalse(self.mover.journal_path.exists())
        self.assertEqual(list(self.source.parent.glob("*._cdr_backup_*")), [])
        (self.source / "new.txt").write_text("written after migration")
        restored = self.mover.restore_app(record.record_id)
        self.assertEqual(restored.status, MigrationStatus.RESTORED)
        self.assertFalse(SafetyGuard.is_junction_or_symlink(str(self.source)))
        self.assertEqual((self.source / "new.txt").read_text(), "written after migration")
        self.assertTrue(Path(record.target_path).exists())
        self.assertTrue((self.source / "empty").is_dir())

    def test_existing_target_never_overwritten(self):
        target = self.destination / self.source.name
        target.mkdir(parents=True)
        (target / "keep.txt").write_text("keep")
        with self.assertRaises(FileExistsError):
            self.move()
        self.assertEqual((target / "keep.txt").read_text(), "keep")

    def test_link_failure_rolls_back(self):
        with patch.object(SafetyGuard, "create_junction", side_effect=OSError("injected")):
            with self.assertRaises(MigrationRollbackError) as raised:
                self.move()
        self.assertTrue(raised.exception.rolled_back_cleanly)
        self.assertTrue((self.source / "data.bin").exists())
        self.assertFalse(SafetyGuard.is_junction_or_symlink(str(self.source)))

    def test_same_size_copy_corruption_detected(self):
        import shutil
        real_copy = shutil.copy2
        def corrupt(src, dst):
            real_copy(src, dst)
            Path(dst).write_bytes(b"x" * Path(dst).stat().st_size)
        with patch("src.core.app_mover.shutil.copy2", side_effect=corrupt):
            with self.assertRaises(MigrationRollbackError):
                self.move()
        self.assertEqual((self.source / "data.bin").read_bytes(), b"important" * 100)

    def test_history_write_failure_rolls_back(self):
        with patch.object(self.mover, "_save_record", side_effect=OSError("disk full")):
            with self.assertRaises(MigrationRollbackError) as raised:
                self.move()
        self.assertTrue(raised.exception.rolled_back_cleanly)
        self.assertTrue((self.source / "data.bin").exists())

    def test_cleanup_failure_keeps_committed_history(self):
        with patch.object(self.mover, "_cleanup_backup", side_effect=PermissionError("locked")):
            record = self.move()
        self.assertEqual(record.status, MigrationStatus.MOVED)
        self.assertTrue(record.error_message)
        self.assertTrue(self.mover.journal_path.exists())
        self.mover.recover_pending()
        self.assertFalse(self.mover.journal_path.exists())
        self.assertTrue(list(self.source.parent.glob("*._cdr_backup_*")))

    def test_restore_history_failure_rolls_back(self):
        record = self.move()
        with patch.object(self.mover, "_save_record", side_effect=OSError("disk full")):
            with self.assertRaises(MigrationRollbackError) as raised:
                self.mover.restore_app(record.record_id)
        self.assertTrue(raised.exception.rolled_back_cleanly)
        self.assertTrue(SafetyGuard.is_junction_or_symlink(str(self.source)))
        self.assertTrue((self.source / "data.bin").exists())

    def test_crash_after_rename_recoverable(self):
        with patch.object(SafetyGuard, "create_junction", side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                self.move()
        self.assertTrue(self.mover.journal_path.exists())
        self.mover.recover_pending()
        self.assertTrue((self.source / "data.bin").exists())
        self.assertFalse(SafetyGuard.is_junction_or_symlink(str(self.source)))

    def test_broken_link_reported(self):
        record = self.move()
        Path(record.target_path).rename(Path(record.target_path).with_name("renamed"))
        self.assertEqual(self.mover.list_migration_records()[0].status, MigrationStatus.BROKEN)

    def test_corrupt_history_blocks_operation(self):
        self.mover.state_dir.mkdir()
        self.mover.history_path.write_text("{broken")
        with self.assertRaises(ValueError):
            self.move()
        self.assertTrue(self.source.is_dir())

    def test_alternate_stream_rejected(self):
        Path(str(self.source / "data.bin") + ":custom").write_bytes(b"must preserve")
        with self.assertRaises(MigrationRollbackError):
            self.move()
        self.assertEqual(Path(str(self.source / "data.bin") + ":custom").read_bytes(), b"must preserve")

    def test_low_space_blocks_before_copy(self):
        from src.core.contracts import InsufficientTargetSpaceError
        with patch.object(SafetyGuard, "check_target_free_space", side_effect=InsufficientTargetSpaceError("full")):
            with self.assertRaises(MigrationRollbackError):
                self.move()
        self.assertTrue((self.source / "data.bin").exists())

    def test_permission_copy_failure_preserves_source(self):
        with patch("src.core.app_mover.copy_security", side_effect=PermissionError("cannot preserve ACL")):
            with self.assertRaises(MigrationRollbackError):
                self.move()
        self.assertTrue((self.source / "data.bin").exists())
        self.assertFalse(SafetyGuard.is_junction_or_symlink(str(self.source)))

    def test_restore_crash_after_link_rename_recovers(self):
        record = self.move()
        original = Path.rename
        def crash(path, target):
            if "._cdr_restore_" in path.name:
                raise KeyboardInterrupt()
            return original(path, target)
        with patch.object(Path, "rename", crash):
            with self.assertRaises(KeyboardInterrupt):
                self.mover.restore_app(record.record_id)
        self.mover.recover_pending()
        self.assertTrue(SafetyGuard.is_junction_or_symlink(str(self.source)))
        self.assertTrue((self.source / "data.bin").exists())

    def test_cross_volume_roundtrip(self):
        if Path.cwd().drive.lower() == self.root.drive.lower():
            self.skipTest("Fixture and workspace are on the same volume")
        with tempfile.TemporaryDirectory(prefix="cdr-test-", dir=Path.cwd()) as other:
            self.destination = Path(other)
            record = self.move()
            self.assertTrue((self.source / "data.bin").exists())
            self.mover.restore_app(record.record_id)
            self.assertFalse(SafetyGuard.is_junction_or_symlink(str(self.source)))

    def test_hardlink_rejected(self):
        os.link(self.source / "data.bin", self.source / "alias.bin")
        with self.assertRaises(MigrationRollbackError):
            self.move()
        self.assertTrue((self.source / "data.bin").exists())

    def test_journal_blocks_second_operation(self):
        with patch.object(SafetyGuard, "create_junction", side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                self.move()
        with self.assertRaises(RuntimeError):
            self.move()
        self.mover.recover_pending()


if __name__ == "__main__":
    unittest.main()
