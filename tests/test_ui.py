"""UI behaviour tests (offscreen). Backends are test doubles; no files are modified.

Skipped automatically when PyQt6 is not installed.
"""
import os
import time
import unittest
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

try:
    from PyQt6.QtCore import Qt
    from PyQt6.QtWidgets import QApplication, QMessageBox
except ImportError:  # pragma: no cover
    QApplication = None

from src.core.contracts import (
    AnxietyLevel, CacheCategory, CacheItem, CleanSummary, DriveHealthReport, MigrationRecord,
    MigrationRollbackError, MigrationStatus, MigrationTask, ProcessLockInfo, ProcessLockedError, RiskLevel,
)


def wait_until(predicate, timeout=5.0):
    app = QApplication.instance()
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        app.processEvents()
        if predicate():
            return True
        time.sleep(0.01)
    app.processEvents()
    return predicate()


class FakeAnalyzer:
    system_drive = "C:\\"

    def analyze_drive_health(self, target_drive=None):
        return DriveHealthReport("C:\\", 100 * 2**30, 90 * 2**30, 10 * 2**30, 90.0, 92,
                                 AnxietyLevel.CRITICAL, "测试诊断", ["建议一"], [], [("D:\\", 50 * 2**30)])

    @staticmethod
    def discover_target_drives(exclude_drive="C:\\"):
        return [("D:\\", 50 * 2**30)]

    def scan_large_files(self, *a, **k):
        return []

    def discover_migratable_Directories(self, **kwargs):
        return [MigrationTask("m1", "BigApp", r"C:\Apps\BigApp", r"D:\CDriveRescue_Moved", 300 * 2**20, 10, "测试")]

    @staticmethod
    def calculate_dir_stats(path):
        return 0, 0


class FakeGuard:
    @staticmethod
    def is_admin():
        return False

    @staticmethod
    def validate_for_migration(a, b):
        return True

    @staticmethod
    def is_junction_or_symlink(p):
        return False

    @staticmethod
    def resolve_junction_target(p):
        return None


class FakeCleaner:
    def __init__(self):
        self.scan_errors = ["X: denied"]
        self.scans = 0
        self.clean_calls = []
        self.last = []

    def scan_caches(self, progress_cb=None, categories=None):
        self.scans += 1
        if progress_cb:
            progress_cb(50, "scanning", 10)
        self.last = [
            CacheItem("safe", "Safe", CacheCategory.BROWSER, "p", ["p"], 100, 2, RiskLevel.SAFE, "d", True),
            CacheItem("caution", "Caution", CacheCategory.SYSTEM, "q", ["q"], 200, 3, RiskLevel.CAUTION, "d", True),
        ]
        return self.last

    def clean_selected(self, items, use_recycle_bin=True, dry_run=False, progress_cb=None):
        self.clean_calls.append((list(items), use_recycle_bin, dry_run))
        return CleanSummary(sum(i.size_bytes for i in items), sum(i.file_count for i in items), 1,
                            use_recycle_bin, dry_run, ["locked file"])


class FakeMover:
    journal_path = "/nonexistent/pending_transaction.json"

    def __init__(self, error=None):
        self.error = error
        self.moves = []

    def list_migration_records(self):
        return [MigrationRecord("r1", "BigApp", r"C:\Apps\BigApp", r"D:\M\BigApp", 1, 1, "2026-10-26T10:00:00+00:00")]

    def move_app_atomic(self, task, root, progress_cb=None):
        self.moves.append((task, root))
        if self.error:
            raise self.error
        return MigrationRecord("r2", task.app_name, task.source_path, root + "\\BigApp", 1, 1, "2026-10-26T10:00:00+00:00")


@unittest.skipIf(QApplication is None, "PyQt6 not installed")
class UiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def make(self, cleaner=None, mover=None):
        from src.ui.main_window import MainWindow
        self.cleaner = cleaner or FakeCleaner()
        self.mover = mover or FakeMover()
        win = MainWindow(disk_analyzer=FakeAnalyzer(), safety_guard=FakeGuard, cache_cleaner=self.cleaner,
                         app_mover=self.mover)
        self.addCleanup(win.deleteLater)
        return win

    def setUp(self):
        self.boxes = []
        patcher = patch.object(QMessageBox, "exec", lambda box: self.boxes.append(box.text()) or 0)
        patcher.start()
        self.addCleanup(patcher.stop)
        for name in ("information", "warning", "critical"):
            p = patch.object(QMessageBox, name, side_effect=lambda parent, title, text, *a: self.boxes.append(text))
            p.start()
            self.addCleanup(p.stop)
        for target in ("src.ui.pages.confirm",):
            p = patch(target, return_value=True)
            p.start()
            self.addCleanup(p.stop)

    def test_dashboard_renders_report(self):
        win = self.make()
        self.assertTrue(wait_until(lambda: win.dashboard.report is not None))
        self.assertEqual(win.dashboard.gauge.score, 92)
        self.assertIn("测试诊断", win.dashboard.headline.text())

    def test_caution_not_checked_and_scan_errors_shown(self):
        win = self.make()
        win.cache.scan()
        self.assertTrue(wait_until(lambda: win.cache._valid and not win.cache._scanning))
        self.assertEqual([i.id for i in win.cache.selected_items()], ["safe"])
        self.assertFalse(win.cache.errors_btn.isHidden())
        # selected_by_default on the backend object is untouched
        self.assertTrue(self.cleaner.last[1].selected_by_default)

    def test_clean_passes_original_objects_and_rescans(self):
        win = self.make()
        win.cache.scan()
        self.assertTrue(wait_until(lambda: win.cache._valid and not win.cache._scanning))
        original = self.cleaner.last[0]
        win.cache.clean(dry_run=True)
        self.assertTrue(wait_until(lambda: not win.cache._cleaning))
        self.assertIs(self.cleaner.clean_calls[-1][0][0], original)
        self.assertEqual(self.cleaner.scans, 1, "dry run must not trigger a rescan")
        self.assertTrue(any("预演" in b and "没有删除" in b for b in self.boxes))

        win.cache.clean(dry_run=False)
        self.assertTrue(wait_until(lambda: self.cleaner.scans == 2 and not win.cache._scanning))
        items, recycle, dry = self.cleaner.clean_calls[-1]
        self.assertTrue(recycle)
        self.assertFalse(dry)
        self.assertFalse(win.gate.busy)
        self.assertTrue(any("回收站" in b and "清空回收站" in b for b in self.boxes))

    def test_permanent_requires_checkbox(self):
        win = self.make()
        win.cache.scan()
        self.assertTrue(wait_until(lambda: win.cache._valid and not win.cache._scanning))
        win.cache.permanent_box.setChecked(True)
        win.cache.clean(dry_run=False)
        self.assertTrue(wait_until(lambda: len(self.cleaner.clean_calls) == 1 and not win.cache._cleaning))
        self.assertFalse(self.cleaner.clean_calls[0][1])

    def test_gate_blocks_close_and_parallel_mutations(self):
        win = self.make()
        self.assertTrue(win.gate.acquire("测试操作"))
        from PyQt6.QtGui import QCloseEvent
        event = QCloseEvent()
        win.closeEvent(event)
        self.assertFalse(event.isAccepted())
        self.assertFalse(win.cache.gate_acquire("另一个"))
        win.gate.release()
        event = QCloseEvent()
        win.closeEvent(event)
        self.assertTrue(event.isAccepted())

    def _select_first_task(self, win):
        win.migration.scan()
        self.assertTrue(wait_until(lambda: win.migration.tasks and not win.migration._scanning))
        win.migration.table.selectRow(0)
        win.migration.target_combo.setEditText(r"D:\CDriveRescue_Moved" if os.name == "nt" else "/tmp/cdr_moved_target")

    def test_migration_success_marks_moved(self):
        win = self.make()
        with patch("src.ui.pages.AppContext.has_module", return_value=True):
            self._select_first_task(win)
            win.migration.move_selected()
            self.assertTrue(wait_until(lambda: self.mover.moves and not win.migration._moving))
        self.assertTrue(win.migration.tasks[0].is_already_junction)
        self.assertFalse(win.gate.busy)

    def test_migration_errors_are_explained(self):
        from src.ui.widgets import describe_exception
        lock = ProcessLockedError("busy", [ProcessLockInfo(1, "app.exe", "x", "y")])
        self.assertIn("app.exe", describe_exception(lock)[1])
        self.assertIn("不会替你结束进程", describe_exception(lock)[1])
        title, text = describe_exception(MigrationRollbackError("boom", rolled_back_cleanly=False))
        self.assertEqual(title, "需要人工检查")
        title, _ = describe_exception(MigrationRollbackError("boom", rolled_back_cleanly=True))
        self.assertEqual(title, "操作未完成，已回滚")

        win = self.make(mover=FakeMover(error=MigrationRollbackError("boom", False)))
        with patch("src.ui.pages.AppContext.has_module", return_value=True):
            self._select_first_task(win)
            win.migration.move_selected()
            self.assertTrue(wait_until(lambda: self.mover.moves and not win.migration._moving))
        self.assertFalse(win.migration.tasks[0].is_already_junction)
        self.assertFalse(win.gate.busy)
        self.assertTrue(any("没有完成" in b for b in self.boxes))

    def test_records_load_and_pending_banner(self):
        win = self.make()
        win.show()
        win.nav.setCurrentRow(4)
        self.assertTrue(wait_until(lambda: win.records.records and not win.records._loading))
        self.assertEqual(win.records.table.rowCount(), 1)
        with patch("src.ui.pages.AppContext.pending_transaction", return_value=True):
            self.assertTrue(win.records.check_pending())
        self.assertFalse(win.records.pending_banner.isHidden())
        self.assertFalse(win.dashboard.pending_banner.isHidden())

    def test_progress_bytes_over_2gb(self):
        from src.ui.workers import run_task
        from src.ui.widgets import ProgressPanel
        big = 5 * 2**30 + 123
        got = []
        panel = ProgressPanel("已发现")

        def job(progress_cb=None):
            progress_cb(40, "a", 3 * 2**30)
            progress_cb(100, "b", big)
            return big

        worker = run_task(job, on_progress=lambda p, a, s: (got.append(s), panel.update_progress(p, a, s)),
                          on_success=got.append)
        self.assertTrue(wait_until(lambda: not worker.is_running() and len(got) >= 3))
        self.assertEqual(got, [3 * 2**30, big, big])
        self.assertIn("5.00 GB", panel.amount.text())

    def test_missing_engines_disable_pages(self):
        from src.ui.main_window import MainWindow
        win = MainWindow(disk_analyzer=FakeAnalyzer(), safety_guard=FakeGuard, cache_cleaner=None, app_mover=None)
        self.addCleanup(win.deleteLater)
        self.assertIsNone(win.migration.selected_task())
        self.assertFalse(win.dashboard.env_banner.isHidden())


if __name__ == "__main__":
    unittest.main()
