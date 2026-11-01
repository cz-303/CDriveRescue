r"""
CDriveRescue (C盘焦虑拯救者) - Gemini 核心底座与数据契约单元测试套件 (v1.1)
===========================================================================
Responsible Model: Gemini (Chief System Architect)

Verifies (v1.0 + v1.1 Step-1 Upgrade):
1. Data contract serialization, byte formatting, and enum behaviors (`contracts.py`).
2. `SafetyGuard` blacklist enforcement for `C:\Windows`, `System32`, `pagefile.sys`,
   user roots, and whitelist pass-through for `C:\Windows\Temp`.
3. `SafetyGuard` symlink/junction detection and safe junction removal without deleting target files.
4. `DiskAnalyzer` anxiety score calculation, fast directory statistics, large file hunter,
   and migratable folder discovery.
5. [NEW v1.1] `SafetyGuard.validate_for_hardlink` protection against `.db`/`.sqlite` and cross-drive links.
6. [NEW v1.1] `DiskAnalyzer.scan_duplicate_files_fast` 3-stage lightning duplicate detector and inode skip.
7. [NEW v1.1] `DiskAnalyzer.discover_orphan_appdata_dirs` uninstalled software residue hunter.
8. [NEW v1.1] `DiskAnalyzer.inspect_windows_known_folders` personal directory detector.
"""

from __future__ import annotations

import os
import tempfile
import time
import unittest
from unittest.mock import patch
from pathlib import Path

from src.core.contracts import (
    CacheCategory,
    CacheItem,
    CriticalPathForbiddenError,
    MigrationRecord,
    MigrationStatus,
    RiskLevel,
    format_bytes,
)
from src.core.disk_analyzer import DiskAnalyzer
from src.core.safety_guard import SafetyGuard


class TestGeminiCoreFoundationV11(unittest.TestCase):
    """验证 Gemini 负责的全部 v1.0 + v1.1 核心底座模块与三方契约"""

    def test_format_bytes_and_contracts(self) -> None:
        self.assertEqual(format_bytes(0), "0 B")
        self.assertEqual(format_bytes(512), "512 B")
        self.assertEqual(format_bytes(1024), "1.00 KB")
        self.assertEqual(format_bytes(10 * 1024 * 1024), "10.00 MB")
        self.assertEqual(format_bytes(2 * 1024 ** 3), "2.00 GB")

        item = CacheItem(
            id="wechat_cache",
            name="微信图片视频缩略图缓存",
            category=CacheCategory.SOCIAL_OFFICE,
            path=r"C:\Users\Demo\Documents\WeChat Files\wxid_123\FileStorage\Cache",
            matched_paths=[r"C:\Users\Demo\Documents\WeChat Files\wxid_123\FileStorage\Cache"],
            size_bytes=5 * (1024 ** 3),
            file_count=12800,
            risk_level=RiskLevel.SAFE,
            description="仅清理缩略图与临时缓存，不删除聊天记录",
        )
        self.assertEqual(item.size_human, "5.00 GB")
        self.assertEqual(item.to_dict()["size_human"], "5.00 GB")

        record = MigrationRecord(
            record_id="rec_001",
            app_name="JetBrains PyCharm",
            source_path=r"C:\Program Files\JetBrains\PyCharm",
            target_path=r"D:\CDriveRescue_Moved\PyCharm",
            size_bytes=3 * (1024 ** 3),
            file_count=4500,
            created_at="2026-10-26T18:00:00",
            status=MigrationStatus.MOVED,
        )
        restored = MigrationRecord.from_dict(record.to_dict())
        self.assertEqual(restored.app_name, "JetBrains PyCharm")
        self.assertEqual(restored.status, MigrationStatus.MOVED)

    def test_safety_guard_cleaning_blacklist_and_whitelist(self) -> None:
        for forbidden in [
            r"C:\Windows",
            r"C:\Windows\System32",
            r"C:\Windows\System32\drivers",
            r"C:\Windows\WinSxS",
            r"C:\pagefile.sys",
            r"C:\hiberfil.sys",
            r"C:\Program Files",
            r"C:\Users\Alice\Desktop",
        ]:
            with self.assertRaises(CriticalPathForbiddenError, msg=f"Failed to block {forbidden}"):
                SafetyGuard.validate_for_cleaning(forbidden)

        for allowed in [
            r"C:\Windows\Temp",
            r"C:\Windows\SoftwareDistribution\Download",
            r"C:\Users\Alice\AppData\Local\Temp",
            r"C:\Users\Alice\AppData\Local\Google\Chrome\User Data\Default\Cache",
        ]:
            self.assertTrue(SafetyGuard.validate_for_cleaning(allowed))

    def test_safety_guard_migration_rules(self) -> None:
        for bad_src in [
            r"C:\Windows\System32",
            r"C:\Program Files",
            r"C:\Users\Alice",
            r"C:\Users\Alice\AppData",
            r"C:\Users\Alice\AppData\Local",
        ]:
            with self.assertRaises(CriticalPathForbiddenError, msg=f"Failed to block migration of {bad_src}"):
                SafetyGuard.validate_for_migration(bad_src, r"D:\Moved\Target")

        with self.assertRaises(CriticalPathForbiddenError):
            SafetyGuard.validate_for_migration(
                r"C:\Program Files\JetBrains",
                r"C:\Program Files\JetBrains\SubFolder",
            )

        self.assertTrue(
            SafetyGuard.validate_for_migration(
                r"C:\Program Files\JetBrains\PyCharm 2026.2",
                r"D:\CDriveRescue_Moved\PyCharm 2026.2",
            )
        )

    def test_symlink_and_safe_junction_removal(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            real_target = Path(tmpdir) / "RealTargetOnDDrive"
            real_target.mkdir()
            secret_file = real_target / "important_data.db"
            secret_file.write_bytes(b"X" * 4096)

            link_path = Path(tmpdir) / "CDriveLink"
            if os.name == "nt":
                SafetyGuard.create_junction(str(real_target), str(link_path))
            else:
                os.symlink(str(real_target), str(link_path), target_is_directory=True)

            self.assertTrue(SafetyGuard.is_junction_or_symlink(str(link_path)))
            self.assertFalse(SafetyGuard.is_junction_or_symlink(str(real_target)))

            SafetyGuard.remove_junction_safely(str(link_path))
            self.assertFalse(link_path.exists())
            self.assertTrue(secret_file.exists())
            self.assertEqual(secret_file.stat().st_size, 4096)

    def test_disk_analyzer_stats_and_large_files(self) -> None:
        analyzer = DiskAnalyzer()
        self.assertGreaterEqual(analyzer._compute_anxiety_score(94.0, 8.0), 90)
        self.assertLess(analyzer._compute_anxiety_score(45.0, 120.0), 50)

        with tempfile.TemporaryDirectory() as tmpdir:
            app_dir = Path(tmpdir) / "MockJetBrains"
            app_dir.mkdir()
            big_file = app_dir / "installer_archive.iso"
            big_file.write_bytes(b"0" * (2 * 1024 * 1024))

            total_bytes, file_count = analyzer.calculate_dir_stats(str(app_dir))
            self.assertEqual(total_bytes, 2 * 1024 * 1024)
            self.assertEqual(file_count, 1)

            large_files = analyzer.scan_large_files(
                root_paths=[tmpdir],
                min_size_bytes=1 * 1024 * 1024,
                top_n=10,
            )
            self.assertEqual(len(large_files), 1)
            self.assertEqual(large_files[0].name, "installer_archive.iso")
            self.assertEqual(large_files[0].risk_level, RiskLevel.CAUTION)

            # 测试可搬家大目录自动发现雷达
            with patch.object(SafetyGuard, "find_locking_processes", return_value=[]):
                tasks = analyzer.discover_migratable_Directories(
                    custom_scan_roots=[(tmpdir, "测试软件目录")],
                    min_size_bytes=1 * 1024 * 1024,
                    recommended_target_root=os.path.join(tmpdir, "MovedOut"),
                )
            self.assertEqual(len(tasks), 1)
            self.assertEqual(tasks[0].app_name, "MockJetBrains")
            self.assertEqual(tasks[0].status, MigrationStatus.READY)

    def test_v11_hardlink_validation_and_duplicate_scanner(self) -> None:
        """验证 v1.1 微信/QQ 重复文件三级闪电哈希扫描与硬链接防呆校验"""
        analyzer = DiskAnalyzer()

        # 1. 禁止对 .db / .sqlite 数据库文件建立硬链接
        with self.assertRaises(CriticalPathForbiddenError):
            SafetyGuard.validate_for_hardlink(
                r"C:\WeChat\Msg\MicroMsg.db",
                r"C:\WeChat\Msg\MicroMsg_backup.db",
            )

        # 2. 禁止跨盘符建立 NTFS 硬链接
        with self.assertRaises(CriticalPathForbiddenError):
            SafetyGuard.validate_for_hardlink(
                r"C:\WeChat\Video\demo.mp4",
                r"D:\WeChat\Video\demo.mp4",
            )

        # 3. 真实文件系统测试：扫描 3 个内容完全一致的微信群聊视频文件，并测试硬链接去重后跳过已链接项
        with tempfile.TemporaryDirectory() as tmpdir:
            wx_dir = Path(tmpdir) / "WeChat Files" / "wxid_test" / "FileStorage" / "File"
            wx_dir.mkdir(parents=True)

            payload = b"WECHAT_VIDEO_HEADER_" + (b"A" * (1200 * 1024)) + b"_FOOTER_END"
            f1 = wx_dir / "report_v1.mp4"
            f2 = wx_dir / "report_v1(1).mp4"
            f3 = wx_dir / "report_v1(2).mp4"
            f_diff = wx_dir / "different_video.mp4"
            db_file1 = wx_dir / "chat_index.db"
            db_file2 = wx_dir / "chat_index_copy.db"

            f1.write_bytes(payload)
            f2.write_bytes(payload)
            f3.write_bytes(payload)
            f_diff.write_bytes(b"DIFFERENT_" + payload[10:])  # 相同大小但内容不同
            db_file1.write_bytes(payload)
            db_file2.write_bytes(payload)

            groups = analyzer.scan_duplicate_files_fast(
                scan_roots=[str(wx_dir)],
                min_file_size=500 * 1024,
            )
            # .db 文件应被自动排除，f_diff 因首尾指纹不同被排除，仅 f1/f2/f3 组成 1 个重复组
            self.assertEqual(len(groups), 1)
            grp = groups[0]
            self.assertEqual(grp.total_copies, 3)
            self.assertEqual(grp.savable_bytes, len(payload) * 2)

            # 模拟将 f2 转为指向 f1 的硬链接，再次扫描时应只剩 1 个未硬链接副本 (f3)
            os.remove(f2)
            os.link(f1, f2)
            self.assertFalse(SafetyGuard.validate_for_hardlink(str(f1), str(f2)))

            groups_after = analyzer.scan_duplicate_files_fast(
                scan_roots=[str(wx_dir)],
                min_file_size=500 * 1024,
            )
            self.assertEqual(len(groups_after), 1)
            self.assertEqual(len(groups_after[0].duplicate_paths), 1)

    def test_v11_orphan_appdata_hunter_and_known_folders(self) -> None:
        """验证 v1.1 AppData 卸载残留猎手与系统个人文件夹检测"""
        analyzer = DiskAnalyzer()

        with tempfile.TemporaryDirectory() as tmpdir:
            local_appdata = Path(tmpdir) / "AppData" / "Local"
            local_appdata.mkdir(parents=True)

            # 1) 受保护的系统厂商目录 Microsoft (应被豁免)
            ms_dir = local_appdata / "Microsoft"
            ms_dir.mkdir()
            (ms_dir / "cache.bin").write_bytes(b"1" * (2 * 1024 * 1024))

            # 2) 当前仍安装活跃的软件目录 JetBrains (应被豁免)
            jb_dir = local_appdata / "JetBrains"
            jb_dir.mkdir()
            (jb_dir / "index.bin").write_bytes(b"2" * (2 * 1024 * 1024))

            # 3) 早已卸载 90 天的孤儿软件残留目录 OldUninstalledGameStudio (应被检出为 SAFE)
            orphan_dir = local_appdata / "OldUninstalledGameStudio"
            orphan_dir.mkdir()
            (orphan_dir / "leftover.pak").write_bytes(b"3" * (2 * 1024 * 1024))
            old_ts = time.time() - (90 * 86400)
            os.utime(orphan_dir, (old_ts, old_ts))

            orphans = analyzer.discover_orphan_appdata_dirs(
                appdata_roots=[(str(local_appdata), "AppData\\Local")],
                installed_app_names={"jetbrains", "pycharm", "wechat"},
                min_size_bytes=1 * 1024 * 1024,
                stale_days_threshold=60,
            )
            self.assertEqual(len(orphans), 1)
            self.assertEqual(orphans[0].folder_name, "OldUninstalledGameStudio")
            self.assertEqual(orphans[0].confidence_level, RiskLevel.SAFE)
            self.assertGreaterEqual(orphans[0].last_modified_days_ago, 89)

            # 验证系统个人文件夹检测
            dl_dir = Path(tmpdir) / "Downloads"
            dl_dir.mkdir()
            (dl_dir / "movie.mkv").write_bytes(b"4" * (1024 * 1024))
            known_items = analyzer.inspect_windows_known_folders(
                custom_user_home=tmpdir,
                recommended_target_drive="D:\\",
            )
            self.assertEqual(len(known_items), 5)
            self.assertEqual(known_items[0].folder_key, "Downloads")
            self.assertEqual(known_items[0].size_bytes, 1024 * 1024)


if __name__ == "__main__":
    unittest.main()


class _WinLikeEntry:
    """模拟 Windows 的 os.DirEntry：stat() 返回的 st_ino / st_dev / st_nlink 恒为 0。"""

    def __init__(self, entry):
        self._entry = entry
        self.name = entry.name
        self.path = entry.path

    def is_dir(self, follow_symlinks=True):
        return self._entry.is_dir(follow_symlinks=follow_symlinks)

    def is_file(self, follow_symlinks=True):
        return self._entry.is_file(follow_symlinks=follow_symlinks)

    def is_symlink(self):
        return self._entry.is_symlink()

    def stat(self, follow_symlinks=True):
        st = self._entry.stat(follow_symlinks=follow_symlinks)
        fields = list(st[:10])
        fields[1] = fields[2] = fields[3] = 0  # st_ino, st_dev, st_nlink
        return os.stat_result(fields)


class _WinLikeScandir:
    def __init__(self, real, path):
        self._it = real(path)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self._it.close()

    def __iter__(self):
        return (_WinLikeEntry(e) for e in self._it)


def _stat_with(real_stat, dev_for=None, zero_ino=False):
    """包装 os.stat：按路径改写 st_dev（模拟多卷）或把 st_ino 置 0（模拟不可靠身份）。"""
    def fake(path, *args, **kwargs):
        st = real_stat(path, *args, **kwargs)
        fields = list(st[:10])
        if dev_for:
            dev = dev_for(str(path))
            if dev is not None:
                fields[2] = dev
        if zero_ino:
            fields[1] = 0
        return os.stat_result(fields)
    return fake


class DuplicateScannerIdentityTests(unittest.TestCase):
    """v1.1 修复：按文件实体 (st_dev, st_ino) 去重，与硬链接执行器的规则保持一致。"""

    SIZE = 4096

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / "WeChat Files"
        self.root.mkdir()
        self.payload = b"X" * self.SIZE
        self.analyzer = DiskAnalyzer()

    def write(self, rel, data=None):
        p = self.root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(self.payload if data is None else data)
        return p

    def scan(self, roots=None):
        return self.analyzer.scan_duplicate_files_fast(scan_roots=roots or [str(self.root)], min_file_size=1024)

    def test_existing_hardlink_counted_once_even_when_direntry_ino_is_zero(self):
        a = self.write("a.bin")
        b = self.root / "b.bin"
        os.link(a, b)
        c = self.write("c.bin")
        real_scandir = os.scandir
        with patch("os.scandir", lambda p=".": _WinLikeScandir(real_scandir, p)):
            groups = self.scan()
        self.assertEqual(len(groups), 1)
        g = groups[0]
        self.assertEqual(g.duplicate_paths, [str(c)])
        self.assertIn(g.master_path, (str(a), str(b)))
        self.assertEqual(g.savable_bytes, self.SIZE)

    def test_master_prefers_shared_entity_and_skips_multi_link_copies(self):
        # c 是独立实体（可合并）；a/b 已共享实体 → 应作为主文件
        c = self.write("c.bin")
        a = self.write("a.bin")
        os.link(a, self.root / "b.bin")
        g, = self.scan()
        self.assertIn(Path(g.master_path).name, ("a.bin", "b.bin"))
        self.assertEqual(g.duplicate_paths, [str(c)])
        # 再加入另一组已共享实体 d/e：执行器会拒绝合并它，不能计入可省空间
        d = self.write("d.bin")
        os.link(d, self.root / "e.bin")
        g, = self.scan()
        self.assertEqual(g.duplicate_paths, [str(c)])
        self.assertEqual(g.savable_bytes, self.SIZE)

    def test_all_entities_shared_yields_no_group(self):
        a = self.write("a.bin")
        os.link(a, self.root / "b.bin")
        d = self.write("d.bin")
        os.link(d, self.root / "e.bin")
        self.assertEqual(self.scan(), [])

    def test_overlapping_and_repeated_roots_do_not_double_count(self):
        self.write("x.bin")
        self.write("sub/y.bin")
        self.write("sub/deeper/z.bin")
        roots = [str(self.root), str(self.root / "sub"), str(self.root), str(self.root / "sub" / "deeper") + os.sep]
        groups = self.scan(roots)
        self.assertEqual(len(groups), 1)
        g = groups[0]
        self.assertEqual(g.total_copies, 3)
        self.assertEqual(len(set(g.duplicate_paths + [g.master_path])), 3)
        self.assertEqual(g.savable_bytes, 2 * self.SIZE)

    def test_groups_are_per_volume(self):
        self.write("volA/x1.bin")
        self.write("volA/x2.bin")
        self.write("volB/y1.bin")
        self.write("volB/y2.bin")
        self.write("volC/only.bin")
        real_stat = os.stat
        devs = {"volA": 101, "volB": 202, "volC": 303}

        def dev_for(path):
            for name, dev in devs.items():
                if os.sep + name + os.sep in path:
                    return dev
            return None
        with patch("os.stat", _stat_with(real_stat, dev_for=dev_for)):
            groups = self.scan()
        self.assertEqual(len(groups), 2, "每个卷各自成组；volC 只有一个文件，不应与其他卷配对")
        for g in groups:
            vol = "volA" if "volA" in g.master_path else "volB"
            self.assertTrue(all(vol in p for p in g.duplicate_paths))
            self.assertEqual(len(g.duplicate_paths), 1)
        self.assertEqual(len({g.group_id for g in groups}), 2)

    def test_unreliable_identity_is_skipped(self):
        self.write("a.bin")
        self.write("b.bin")
        real_stat = os.stat
        with patch("os.stat", _stat_with(real_stat, zero_ino=True)):
            self.assertEqual(self.scan(), [])

    def test_rescan_after_linking_reports_nothing_left(self):
        a = self.write("a.bin")
        b = self.write("b.bin")
        c = self.write("c.bin")
        g, = self.scan()
        self.assertEqual(g.savable_bytes, 2 * self.SIZE)
        master = Path(g.master_path)
        for dup in g.duplicate_paths:  # 模拟执行器：把每个副本换成指向主文件的硬链接
            os.remove(dup)
            os.link(master, dup)
        self.assertEqual(self.scan(), [])
        self.assertEqual(os.stat(a).st_ino, os.stat(b).st_ino)
        self.assertEqual(os.stat(a).st_ino, os.stat(c).st_ino)

    def test_different_content_same_size_not_grouped(self):
        self.write("a.bin")
        self.write("b.bin", b"Y" * self.SIZE)
        self.assertEqual(self.scan(), [])


class KnownFolderResolutionTests(unittest.TestCase):
    """v1.1 修复：个人文件夹位置来自 Windows Known Folder API（此处全部为模拟，不读写真实配置）。"""

    GUIDS = {key: guid for key, (_, guid) in SafetyGuard.WINDOWS_KNOWN_FOLDERS.items()}

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.analyzer = DiskAnalyzer()
        self.docs = Path(self.tmp.name) / "docs"
        self.docs.mkdir()
        (self.docs / "a.bin").write_bytes(b"1" * 3000)

    def resolve_with(self, mapping, system_drive="C:"):
        by_guid = {self.GUIDS[k]: v for k, v in mapping.items()}

        def fake_query(guid):
            value = by_guid.get(guid)
            if isinstance(value, Exception) or value is None:
                raise value or OSError("HRESULT 0x80070002")
            return value
        with patch.object(DiskAnalyzer, "_query_known_folder_path", staticmethod(fake_query)), \
             patch.object(DiskAnalyzer, "actual_system_drive", staticmethod(lambda: system_drive)):
            return {i.folder_key: i for i in self.analyzer.inspect_windows_known_folders()}

    def test_redirected_onedrive_failure_and_sizes(self):
        items = self.resolve_with({
            "Downloads": r"D:\Data\Downloads",
            "Documents": str(self.docs),
            "Videos": r"C:\Users\u\Videos",
            "Pictures": r"C:\Users\u\OneDrive\Pictures",
            "Desktop": OSError("HRESULT 0x80070005"),
        })
        self.assertEqual(items["Downloads"].current_path, r"D:\Data\Downloads")
        self.assertFalse(items["Downloads"].is_on_system_drive)
        self.assertTrue(items["Videos"].is_on_system_drive)
        self.assertEqual(items["Pictures"].current_path, r"C:\Users\u\OneDrive\Pictures")
        self.assertEqual(items["Documents"].size_bytes, 3000, "体积按实际位置统计")
        self.assertNotIn("Desktop", items, "读取失败的文件夹不能用猜测路径代替")
        self.assertIn("Desktop", self.analyzer.last_known_folder_errors)
        self.assertIn("0x80070005", self.analyzer.last_known_folder_errors["Desktop"])

    def test_system_drive_not_hardcoded(self):
        items = self.resolve_with({"Downloads": r"E:\Users\u\Downloads", "Videos": r"C:\Media\Videos"},
                                  system_drive="E:")
        self.assertTrue(items["Downloads"].is_on_system_drive)
        self.assertFalse(items["Videos"].is_on_system_drive)

    def test_rescan_after_migration_reflects_new_location(self):
        before = self.resolve_with({"Downloads": r"C:\Users\u\Downloads"})
        self.assertTrue(before["Downloads"].is_on_system_drive)
        after = self.resolve_with({"Downloads": r"D:\Users\u\Downloads"})
        self.assertEqual(after["Downloads"].current_path, r"D:\Users\u\Downloads")
        self.assertFalse(after["Downloads"].is_on_system_drive)

    def test_custom_home_mode_unchanged(self):
        with patch.object(DiskAnalyzer, "_query_known_folder_path", side_effect=AssertionError("不应调用")):
            items = self.analyzer.inspect_windows_known_folders(custom_user_home=self.tmp.name)
        self.assertEqual(len(items), 5)

    def test_default_duplicate_roots_follow_real_locations(self):
        mapping = {self.GUIDS["Documents"]: r"D:\Docs"}

        def fake_query(guid):
            if guid in mapping:
                return mapping[guid]
            raise OSError("HRESULT 0x80070002")
        with patch("src.core.disk_analyzer.platform.system", return_value="Windows"), \
             patch.object(DiskAnalyzer, "_query_known_folder_path", staticmethod(fake_query)):
            roots = self.analyzer.default_duplicate_scan_roots()
        self.assertEqual(roots, [os.path.join(r"D:\Docs", "WeChat Files"), os.path.join(r"D:\Docs", "Tencent Files")],
                         "下载文件夹读取失败时跳过，不猜测")
