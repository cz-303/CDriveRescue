"""v1.1 深水区瘦身实验室界面测试（离屏）。后端为替身；回收站测试只在临时目录中操作。

缺少 PyQt6 时自动跳过。
"""
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

try:
    from PyQt6.QtCore import Qt
    from PyQt6.QtGui import QCloseEvent
    from PyQt6.QtWidgets import QApplication, QMessageBox
except ImportError:  # pragma: no cover
    QApplication = None

from src.core.contracts import (
    AnxietyLevel, CriticalPathForbiddenError, DriveHealthReport, DuplicateGroup, HardlinkDedupeSummary,
    KnownFolderItem, OrphanResidueItem, ProcessLockInfo, ProcessLockedError, RiskLevel,
)

G, M = 2**30, 2**20


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

    def __init__(self):
        self.dup_calls = []
        self.kf_calls = 0
        self.moved = {}                      # folder_key -> 新位置（模拟 Windows 已改绑）
        self.last_known_folder_errors = {}

    def analyze_drive_health(self, target_drive=None):
        return DriveHealthReport("C:\\", 100 * G, 90 * G, 10 * G, 90.0, 92, AnxietyLevel.CRITICAL, "诊断", [], [], [])

    @staticmethod
    def discover_target_drives(exclude_drive="C:\\"):
        return [("D:\\", 50 * G)]

    def scan_duplicate_files_fast(self, scan_roots=None, min_file_size=M, source_app_label="WeChat/QQ", progress_cb=None):
        self.dup_calls.append((scan_roots, min_file_size))
        progress_cb(50, "比对", 3 * G)
        self.groups = [DuplicateGroup("g1", "h1", "a.mp4", 2 * G, "/w/a.mp4", ["/w/a(1).mp4", "/w/a(2).mp4"]),
                       DuplicateGroup("g2", "h2", "b.pdf", M, "/w/b.pdf", ["/w/b(1).pdf"], "QQ")]
        return self.groups

    def inspect_windows_known_folders(self, custom_user_home=None, recommended_target_drive="D:\\"):
        self.kf_drive = recommended_target_drive
        self.kf_calls += 1
        dl = self.moved.get("Downloads")
        return [KnownFolderItem("Downloads", "下载 (Downloads)", "{374DE290-123F-4565-9164-39C4925E467B}",
                                dl or "/home/u/Downloads", "/tmp/x/Downloads", 5 * G, 10, dl is None),
                KnownFolderItem("Desktop", "桌面 (Desktop)", "{B4BFCC3A-DB2C-424C-B029-7FE99A87C641}",
                                "D:\\Desktop", "D:\\Users\\u\\Desktop", G, 3, False)]

    def discover_orphan_appdata_dirs(self, **kwargs):
        return [OrphanResidueItem("o1", "OldApp", "/nonexistent/OldApp", "AppData\\Roaming", 300 * M, 9, 200,
                                  RiskLevel.SAFE, "未匹配")]


class FakeOptimizer:
    def __init__(self, summary=None, error=None):
        self.summary = summary
        self.error = error
        self.dedupe_calls = []
        self.relocate_calls = []
        self.last_warnings = []
        self.journal_path = Path(tempfile.gettempdir()) / "cdr_no_such_journal.json"

    def deduplicate_via_hardlink(self, groups, dry_run=False, progress_cb=None):
        self.dedupe_calls.append((list(groups), dry_run))
        if self.error:
            raise self.error
        progress_cb(100, "done", 5 * G)
        return self.summary or HardlinkDedupeSummary(len(groups), 3, 4 * G, 0, [])

    def relocate_known_folder(self, item, target_path, move_existing_files=True, progress_cb=None):
        self.relocate_calls.append((item, target_path, move_existing_files))
        if self.error:
            raise self.error
        progress_cb(100, "ok", item.size_bytes)
        if getattr(self, "analyzer", None) is not None:
            self.analyzer.moved[item.folder_key] = target_path
        from dataclasses import replace
        return replace(item, current_path=target_path, is_on_system_drive=False)

    def recover_known_folder_transaction(self):
        return "没有待恢复的个人文件夹事务"


class FakeGuard:
    @staticmethod
    def is_admin():
        return False


@unittest.skipIf(QApplication is None, "PyQt6 not installed")
class LabUiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.boxes = []
        p = patch.object(QMessageBox, "exec", lambda box: self.boxes.append((box.text(), box.informativeText())) or 0)
        p.start()
        self.addCleanup(p.stop)
        for name in ("information", "warning", "critical"):
            p = patch.object(QMessageBox, name, side_effect=lambda parent, title, text, *a: self.boxes.append((text, "")))
            p.start()
            self.addCleanup(p.stop)
        p = patch("src.ui.lab_page.confirm", return_value=True)
        self.confirm = p.start()
        self.addCleanup(p.stop)

    def make(self, optimizer="default"):
        from src.ui.main_window import MainWindow
        self.analyzer = FakeAnalyzer()
        self.optimizer = FakeOptimizer() if optimizer == "default" else optimizer
        if self.optimizer is not None:
            self.optimizer.analyzer = self.analyzer
        win = MainWindow(disk_analyzer=self.analyzer, safety_guard=FakeGuard, cache_cleaner=None, app_mover=None,
                         system_optimizer=self.optimizer)
        self.addCleanup(win.deleteLater)
        return win

    # -- navigation -------------------------------------------------------------
    def test_lab_is_sixth_tab_and_v10_pages_remain(self):
        from src.ui.main_window import LAB_INDEX, NAV
        win = self.make()
        self.assertEqual(NAV[:5], ["C 盘体检", "缓存清理", "大文件猎手", "软件搬家", "迁移记录"])
        self.assertEqual(win.nav.count(), 6)
        win.nav.setCurrentRow(LAB_INDEX)
        self.assertIs(win.stack.currentWidget(), win.lab)
        win.lab.tabs.set_current(2)
        self.assertIs(win.lab.stack.currentWidget(), win.lab.orphans)

    def test_mainwindow_without_optimizer_still_builds(self):
        from src.ui.main_window import MainWindow
        win = MainWindow(disk_analyzer=FakeAnalyzer(), safety_guard=FakeGuard)
        self.addCleanup(win.deleteLater)
        self.assertIsNone(win.ctx.optimizer)

    # -- panel A ----------------------------------------------------------------
    def _scan_dupes(self, win):
        panel = win.lab.dedupe
        panel.scan()
        self.assertTrue(wait_until(lambda: panel._valid and not panel.busy))
        return panel

    def test_dedupe_passes_original_groups_and_releases_gate(self):
        win = self._win = self.make()
        panel = self._scan_dupes(win)
        self.assertEqual(panel.tree.topLevelItemCount(), 2)
        self.assertIn("可省", panel.summary.text())
        panel.dedupe(dry_run=True)
        self.assertTrue(wait_until(lambda: self.optimizer.dedupe_calls and not panel.busy))
        groups, dry = self.optimizer.dedupe_calls[-1]
        self.assertTrue(dry)
        self.assertIs(groups[0], self.analyzer.groups[0])
        self.assertTrue(panel._valid, "预演不应让扫描结果失效")
        self.assertTrue(any("没有修改任何文件" in t for t, _ in self.boxes))

        panel.dedupe(dry_run=False)
        self.assertTrue(wait_until(lambda: len(self.optimizer.dedupe_calls) == 2 and not panel.busy))
        self.assertFalse(self.optimizer.dedupe_calls[-1][1])
        self.assertTrue(wait_until(lambda: not win.gate.busy))
        self.assertFalse(panel._valid)
        self.assertEqual(panel.tree.topLevelItemCount(), 0)
        self.assertTrue(any("已立即生效" in t for t, _ in self.boxes))

    def test_dedupe_respects_unchecked_groups(self):
        win = self.make()
        panel = self._scan_dupes(win)
        panel.tree.topLevelItem(1).setCheckState(0, Qt.CheckState.Unchecked)
        panel.dedupe(dry_run=True)
        self.assertTrue(wait_until(lambda: self.optimizer.dedupe_calls and not panel.busy))
        self.assertEqual([g.group_id for g in self.optimizer.dedupe_calls[-1][0]], ["g1"])

    def test_dedupe_rollback_failure_is_called_out(self):
        summary = HardlinkDedupeSummary(1, 0, 0, 0, ["/w/a(1).mp4: 去重失败: x; 回滚未完成: y; 原件备份: /w/a(1).mp4._hl_bak"])
        win = self.make(FakeOptimizer(summary=summary))
        panel = self._scan_dupes(win)
        panel.dedupe(dry_run=False)
        self.assertTrue(wait_until(lambda: self.optimizer.dedupe_calls and not panel.busy and not win.gate.busy))
        self.assertTrue(any("._hl_bak" in info for _, info in self.boxes))

    def test_without_optimizer_scan_works_but_dedupe_disabled(self):
        win = self.make(optimizer=None)
        panel = self._scan_dupes(win)
        self.assertFalse(panel.link_btn.isEnabled())
        self.assertFalse(panel.preview_btn.isEnabled())
        self.assertFalse(panel.opt_banner.isHidden())

    def test_progress_bytes_over_2gb_reach_ui(self):
        from src.ui.workers import DuplicateHardlinkWorker, start_thread
        got = []
        t = DuplicateHardlinkWorker(FakeAnalyzer(), None, mode=DuplicateHardlinkWorker.SCAN)
        start_thread(t, on_progress=lambda p, a, s: got.append(s))
        self.assertTrue(wait_until(lambda: got and not t.isRunning()))
        self.assertEqual(got[0], 3 * G)

    # -- panel B ----------------------------------------------------------------
    def test_known_folders_cards_and_move(self):
        win = self.make()
        panel = win.lab.folders
        panel.scan()
        self.assertTrue(wait_until(lambda: panel.items and not panel.busy))
        self.assertEqual(self.analyzer.kf_drive, "D:\\")
        cards = [panel.grid.itemAt(i).widget() for i in range(panel.grid.count())]
        self.assertEqual(len(cards), 2)
        self.assertTrue(cards[0].move_btn.isEnabled())
        self.assertFalse(cards[1].move_btn.isEnabled(), "已不在系统盘的文件夹不能再搬")
        with tempfile.TemporaryDirectory() as tmp:
            target = os.path.join(tmp, "new_parent", "Downloads")
            panel.move(panel.items[0], target)
            self.assertTrue(wait_until(lambda: self.optimizer.relocate_calls and not win.gate.busy and not panel.busy))
            item, path, move_files = self.optimizer.relocate_calls[0]
            self.assertEqual(item.folder_key, "Downloads")
            self.assertEqual(path, target)
            self.assertTrue(move_files)
            self.assertTrue(os.path.isdir(os.path.dirname(target)), "确认后应创建目标的上级目录")

    def test_known_folder_refuses_nonempty_target_and_same_drive(self):
        win = self.make()
        panel = win.lab.folders
        item = FakeAnalyzer().inspect_windows_known_folders()[0]
        with tempfile.TemporaryDirectory() as tmp:
            Path(tmp, "keep.txt").write_text("x")
            panel.move(item, tmp)
        self.assertFalse(self.optimizer.relocate_calls)
        self.assertTrue(any("不是空文件夹" in t or "已经存在" in t for t, _ in self.boxes))
        c_item = KnownFolderItem("Videos", "视频", "{18989B1D-99B5-455B-841C-AB7C74E4DDFC}", "C:\\Users\\u\\Videos",
                                 "C:\\Other\\Videos", 1, 1, True)
        with patch("src.ui.lab_page.os.path.isabs", return_value=True):
            panel.move(c_item, "C:\\Other\\Videos")
        self.assertFalse(self.optimizer.relocate_calls)
        self.assertTrue(any("同一个磁盘" in t for t, _ in self.boxes))

    # -- panel C ----------------------------------------------------------------
    def test_orphan_scan_lists_items_with_badges(self):
        win = self.make()
        panel = win.lab.orphans
        panel.scan()
        self.assertTrue(wait_until(lambda: panel.items and not panel.busy))
        self.assertEqual(panel.table.rowCount(), 1)
        self.assertIsNotNone(panel.table.cellWidget(0, 5))
        self.assertIsNotNone(panel.table.cellWidget(0, 6))

    def test_orphan_guards_and_recycle(self):
        from src.core.safety_guard import SafetyGuard
        from src.ui import workers
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp, "Roaming")
            target = root / "OldApp"
            (target / "sub").mkdir(parents=True)
            (target / "sub" / "f.bin").write_bytes(b"x" * 10)
            item = OrphanResidueItem("o", "OldApp", str(target), "AppData\\Roaming", 10, 1, 200, RiskLevel.SAFE, "r")

            # 扫描根本身、根以外的目录、受保护厂商目录都必须拒绝
            for bad in (root, Path(tmp), root / "Microsoft"):
                bad.mkdir(exist_ok=True)
                bad_item = OrphanResidueItem("b", bad.name, str(bad), "x", 1, 1, 1, RiskLevel.SAFE, "r")
                with self.assertRaises((PermissionError, CriticalPathForbiddenError)):
                    workers.validate_orphan_path(bad_item, SafetyGuard, [str(root)])

            check = workers.inspect_orphan_dir(item, SafetyGuard, [str(root)])
            self.assertEqual(check.files_seen, 1)
            self.assertEqual(check.newest_change_days, 0)

            # 有进程占用时拒绝
            with patch.object(SafetyGuard, "ensure_no_locking_processes",
                              side_effect=ProcessLockedError("busy", [ProcessLockInfo(1, "app.exe", "x", "y")])), \
                 patch("src.ui.workers.importlib.util.find_spec", return_value=object()):
                with self.assertRaises(ProcessLockedError):
                    workers.recycle_orphan_dir(item, SafetyGuard, [str(root)])
            self.assertTrue(target.exists())

            # 正常移入回收站：用替身代替真正的回收站，只在临时目录内操作
            def fake_trash(path):
                import shutil
                shutil.rmtree(path)
            with patch("send2trash.send2trash", side_effect=fake_trash) as trash:
                workers.recycle_orphan_dir(item, SafetyGuard, [str(root)])
            trash.assert_called_once_with(str(target))
            self.assertFalse(target.exists())

    # -- v1.1 修复：迁移后刷新 ----------------------------------------------------------
    def _cards(self, panel):
        return [panel.grid.itemAt(i).widget() for i in range(panel.grid.count())
                if panel.grid.itemAt(i).widget() is not None and not panel.grid.itemAt(i).widget().isHidden()]

    def _move_downloads(self, win, panel, tmp):
        panel.scan()
        self.assertTrue(wait_until(lambda: panel.items and not panel.busy))
        target = os.path.join(tmp, "D", "Downloads")
        panel.move(next(i for i in panel.items if i.folder_key == "Downloads"), target)
        self.assertTrue(wait_until(lambda: self.analyzer.kf_calls >= 2 and not panel.busy and not win.gate.busy))
        QApplication.instance().processEvents()
        return target

    def test_cards_refresh_to_new_location_after_move(self):
        win = self.make()
        panel = win.lab.folders
        with tempfile.TemporaryDirectory() as tmp:
            target = self._move_downloads(win, panel, tmp)
            card = next(c for c in self._cards(panel) if c.item.folder_key == "Downloads")
            self.assertEqual(card.item.current_path, target, "卡片应显示 Windows 返回的新位置")
            self.assertFalse(card.item.is_on_system_drive)
            self.assertFalse(card.move_btn.isEnabled(), "已不在系统盘，不能再搬")
            self.assertEqual(self.analyzer.kf_calls, 2, "初次检测 + 迁移后刷新各一次，没有重复刷新")
            time.sleep(0.2)
            QApplication.instance().processEvents()
            self.assertEqual(self.analyzer.kf_calls, 2)
            self.assertFalse(panel._threads)

    def test_failed_move_still_refreshes_and_releases_gate(self):
        from src.core.contracts import MigrationRollbackError
        opt = FakeOptimizer(error=MigrationRollbackError("复制校验失败", rolled_back_cleanly=True))
        win = self.make(opt)
        panel = win.lab.folders
        with tempfile.TemporaryDirectory() as tmp:
            self._move_downloads(win, panel, tmp)
        self.assertEqual(self.analyzer.kf_calls, 2)
        self.assertFalse(win.gate.busy)
        card = next(c for c in self._cards(panel) if c.item.folder_key == "Downloads")
        self.assertEqual(card.item.current_path, "/home/u/Downloads")
        self.assertTrue(card.move_btn.isEnabled())
        self.assertTrue(any("没有迁移" in t for t, _ in self.boxes))

    def test_unreadable_folders_reported_and_onedrive_disabled(self):
        win = self.make()
        panel = win.lab.folders
        self.analyzer.last_known_folder_errors = {"Desktop": "桌面 (Desktop)：无法从 Windows 读取实际位置（HRESULT 0x80070005）"}
        with patch.dict(os.environ, {"OneDrive": "/home/u"}):
            panel.scan()
            self.assertTrue(wait_until(lambda: panel.items and not panel.busy))
            QApplication.instance().processEvents()
            dl = next(c for c in self._cards(panel) if c.item.folder_key == "Downloads")
            self.assertFalse(dl.move_btn.isEnabled(), "OneDrive 管理的文件夹不提供迁移")
        self.assertFalse(panel.error_banner.isHidden())
        self.assertIn("0x80070005", panel.error_banner.text.text())

    # -- v1.1 修复：无法检查占用时禁止回收 ------------------------------------------------
    def _orphan_fixture(self, tmp):
        root = Path(tmp, "Roaming")
        target = root / "OldApp"
        target.mkdir(parents=True)
        (target / "f.bin").write_bytes(b"x")
        item = OrphanResidueItem("o", "OldApp", str(target), "AppData\\Roaming", 1, 1, 200, RiskLevel.SAFE, "r")
        return root, target, item

    def test_recycle_refused_without_working_lock_check(self):
        from src.core import safety_guard as sg_mod
        from src.core.contracts import CDriveRescueError
        from src.core.safety_guard import SafetyGuard
        from src.ui import workers
        real_find_spec = workers.importlib.util.find_spec

        def no_psutil(name, *a, **k):
            return None if name == "psutil" else real_find_spec(name, *a, **k)

        cases = {
            "缺少 psutil（依赖检测）": (patch("src.ui.workers.importlib.util.find_spec", side_effect=no_psutil),
                                  workers.LockCheckUnavailable),
            "缺少 psutil（护栏本身）": (patch.object(sg_mod, "psutil", None), workers.LockCheckUnavailable),
            "占用检测异常": (patch.object(SafetyGuard, "ensure_no_locking_processes",
                                    side_effect=RuntimeError("access denied")), workers.LockCheckUnavailable),
            "检测到占用": (patch.object(SafetyGuard, "ensure_no_locking_processes",
                                  side_effect=ProcessLockedError("busy", [ProcessLockInfo(1, "app.exe", "x", "y")])),
                       ProcessLockedError),
        }
        for label, (ctx_patch, expected) in cases.items():
            with self.subTest(label), tempfile.TemporaryDirectory() as tmp:
                root, target, item = self._orphan_fixture(tmp)
                with ctx_patch, patch("send2trash.send2trash") as trash:
                    with self.assertRaises(expected):
                        workers.recycle_orphan_dir(item, SafetyGuard, [str(root)])
                    trash.assert_not_called()
                self.assertTrue(target.exists())
        self.assertTrue(issubclass(workers.LockCheckUnavailable, Exception))
        self.assertFalse(issubclass(workers.LockCheckUnavailable, CDriveRescueError))

    def test_short_8dot3_names_are_not_mistaken_for_redirects(self):
        """Windows 上 %TEMP% 常是 8.3 短名（如 C:\\Users\\ADMINI~1\\...），realpath 会展开为长名，不能因此拒绝。"""
        from src.core.safety_guard import SafetyGuard
        from src.ui import workers
        real_realpath = os.path.realpath

        def win_like_realpath(path, *a, **k):  # 模拟 GetFinalPathNameByHandle 把短名展开成长名
            return real_realpath(path, *a, **k).replace("SHORT~1", "LongUserName")

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp, "SHORT~1", "Roaming")
            target = root / "OldApp"
            target.mkdir(parents=True)
            (target / "f.bin").write_bytes(b"x")
            item = OrphanResidueItem("o", "OldApp", str(target), "AppData\\Roaming", 1, 1, 200, RiskLevel.SAFE, "r")
            with patch("os.path.realpath", side_effect=win_like_realpath):
                self.assertEqual(Path(workers.validate_orphan_path(item, SafetyGuard, [str(root)])).name, "OldApp")
                # 根目录用长名、条目用短名（或相反）也应视为同一个目录
                long_root = str(root).replace("SHORT~1", "LongUserName")
                workers.validate_orphan_path(item, SafetyGuard, [long_root])
                check = workers.inspect_orphan_dir(item, SafetyGuard, [str(root)])
                self.assertEqual(check.files_seen, 1)

    def test_confirm_dialog_refuses_when_lock_check_unavailable(self):
        from src.ui.workers import OrphanCheck
        win = self.make()
        panel = win.lab.orphans
        item = OrphanResidueItem("o", "OldApp", "/x/OldApp", "AppData\\Roaming", 1, 1, 200, RiskLevel.SAFE, "r")
        panel.items = [item]
        with patch("src.ui.lab_page.OrphanRecycleWorker") as worker_cls:
            panel._confirm_recycle(item, OrphanCheck("/x/OldApp", 300, 1, [], lock_check_available=False))
        worker_cls.assert_not_called()
        self.confirm.assert_not_called()
        self.assertFalse(win.gate.busy)
        self.assertTrue(any("无法确认" in t for t, _ in self.boxes))

    # -- lifecycle ---------------------------------------------------------------
    def test_close_refused_while_mutating_thread_runs(self):
        win = self.make()
        self.assertTrue(win.gate.acquire("硬链接去重"))
        ev = QCloseEvent()
        win.closeEvent(ev)
        self.assertFalse(ev.isAccepted())
        win.gate.release()
        ev = QCloseEvent()
        win.closeEvent(ev)
        self.assertTrue(ev.isAccepted())


if __name__ == "__main__":
    unittest.main()


@unittest.skipIf(QApplication is None, "PyQt6 not installed")
class DismEntryTests(unittest.TestCase):
    """C 盘体检页的 Windows 组件存储清理入口（DISM 输出全部模拟，不执行真实清理）。"""

    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        import threading
        from src.core.contracts import SystemSlimItem
        self.boxes = []
        p = patch.object(QMessageBox, "exec", lambda box: self.boxes.append(
            (box.windowTitle(), box.text(), box.informativeText(), box.detailedText())) or 0)
        p.start()
        self.addCleanup(p.stop)
        p = patch.object(QMessageBox, "information",
                         side_effect=lambda parent, title, text, *a: self.boxes.append((title, text, "", "")))
        p.start()
        self.addCleanup(p.stop)
        p = patch("src.ui.pages.confirm", return_value=True)
        self.confirm = p.start()
        self.addCleanup(p.stop)
        self.release = threading.Event()
        self.release.set()
        self.slim = SystemSlimItem("sys_winsxs_dism", "Windows 组件存储 (WinSxS)", "C:\\Windows\\WinSxS", 0,
                                   True, True, "Dism.exe ...", "说明")

    def make(self, result=(True, "Windows 组件清理完成"), admin=True, optimizer=True):
        from src.ui.main_window import MainWindow
        test = self

        class Analyzer(FakeAnalyzer):
            def analyze_drive_health(self, target_drive=None):
                return DriveHealthReport("C:\\", 100 * G, 90 * G, 10 * G, 90.0, 92, AnxietyLevel.CRITICAL,
                                         "诊断", [], [test.slim], [])

        class Optimizer(FakeOptimizer):
            calls = 0

            def slim_winsxs_component_store(self, progress_cb=None):
                Optimizer.calls += 1
                progress_cb(0, "正在启动 Windows 组件清理", 0)
                test.release.wait(5)
                progress_cb(55, "[=====  55.0%  ]", 0)
                return result

        class Guard(FakeGuard):
            @staticmethod
            def is_admin():
                return admin

        self.opt_cls = Optimizer
        win = MainWindow(disk_analyzer=Analyzer(), safety_guard=Guard, system_optimizer=Optimizer() if optimizer else None)
        self.addCleanup(win.deleteLater)
        self.assertTrue(wait_until(lambda: hasattr(win.dashboard, "dism_btn")))
        return win

    def wait_done(self, win):
        self.assertTrue(wait_until(lambda: win.dashboard._dism_thread is None and not win.gate.busy, 8))

    def test_success_with_restart_hint(self):
        win = self.make(result=(True, "Windows 组件清理完成，需要重启才能完成剩余操作\n[==100.0%==]"))
        win.dashboard.dism_btn.click()
        self.wait_done(win)
        self.assertEqual(self.opt_cls.calls, 1)
        title, text, info, details = next(b for b in self.boxes if b[0] == "组件清理完成")
        self.assertIn("需要重启", text)
        self.assertIn("手动重启", info)
        self.assertIn("100.0%", details)

    def test_failure_is_reported(self):
        win = self.make(result=(False, "DISM 清理失败，退出码 87；请查看 Windows\\Logs\\DISM\\dism.log\n错误: 87"))
        win.dashboard.dism_btn.click()
        self.wait_done(win)
        title, text, info, details = next(b for b in self.boxes if b[0] == "组件清理没有完成")
        self.assertIn("退出码 87", text)
        self.assertIn("错误: 87", details)

    def test_requires_admin(self):
        win = self.make(admin=False)
        win.dashboard.dism_btn.click()
        QApplication.instance().processEvents()
        self.assertEqual(self.opt_cls.calls, 0)
        self.confirm.assert_not_called()
        self.assertTrue(any("管理员" in b[0] for b in self.boxes))

    def test_disabled_without_optimizer(self):
        win = self.make(optimizer=False)
        self.assertFalse(win.dashboard.dism_btn.isEnabled())

    def test_close_refused_while_running(self):
        self.release.clear()
        win = self.make()
        win.dashboard.dism_btn.click()
        self.assertTrue(wait_until(lambda: win.gate.busy and win.dashboard.dism_progress.isVisible() is not None))
        with patch.object(QMessageBox, "warning", side_effect=lambda *a: self.boxes.append(("warn", a[2], "", ""))):
            ev = QCloseEvent()
            win.closeEvent(ev)
        self.assertFalse(ev.isAccepted(), "DISM 运行中不能关闭窗口")
        self.assertFalse(win.dashboard.dism_btn.isEnabled())
        self.release.set()
        self.wait_done(win)
        ev = QCloseEvent()
        win.closeEvent(ev)
        self.assertTrue(ev.isAccepted())
