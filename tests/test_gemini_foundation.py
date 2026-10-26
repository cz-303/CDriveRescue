"""
CDriveRescue (C盘焦虑拯救者) - Gemini 核心底座与数据契约单元测试套件
===========================================================================
Responsible Model: Gemini (Chief System Architect)

Verifies:
1. Data contract serialization, byte formatting, and enum behaviors (`contracts.py`).
2. `SafetyGuard` blacklist enforcement for `C:\Windows`, `System32`, `pagefile.sys`,
   user roots, and whitelist pass-through for `C:\Windows\Temp`.
3. `SafetyGuard` symlink/junction detection and safe junction removal without deleting target files.
4. `DiskAnalyzer` anxiety score calculation, fast directory statistics, large file hunter,
   and migratable folder discovery.
"""

from __future__ import annotations

import os
import tempfile
import unittest
from unittest.mock import patch
from pathlib import Path

from src.core.contracts import (
    AnxietyLevel,
    CacheCategory,
    CacheItem,
    CriticalPathForbiddenError,
    MigrationRecord,
    MigrationStatus,
    MigrationTask,
    RiskLevel,
    format_bytes,
)
from src.core.disk_analyzer import DiskAnalyzer
from src.core.safety_guard import SafetyGuard


class TestGeminiCoreFoundation(unittest.TestCase):
    """验证 Gemini 负责的全部核心底座模块与三方契约"""

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
        # 1. 严禁清理 Windows 核心目录及引导文件
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

        # 2. 放行安全的 Windows Temp 与应用缓存目录
        for allowed in [
            r"C:\Windows\Temp",
            r"C:\Windows\SoftwareDistribution\Download",
            r"C:\Users\Alice\AppData\Local\Temp",
            r"C:\Users\Alice\AppData\Local\Google\Chrome\User Data\Default\Cache",
        ]:
            self.assertTrue(SafetyGuard.validate_for_cleaning(allowed))

    def test_safety_guard_migration_rules(self) -> None:
        # 1. 严禁搬移系统核心目录、Program Files 根目录或 AppData 顶层目录
        for bad_src in [
            r"C:\Windows\System32",
            r"C:\Program Files",
            r"C:\Users\Alice",
            r"C:\Users\Alice\AppData",
            r"C:\Users\Alice\AppData\Local",
        ]:
            with self.assertRaises(CriticalPathForbiddenError, msg=f"Failed to block migration of {bad_src}"):
                SafetyGuard.validate_for_migration(bad_src, r"D:\Moved\Target")

        # 2. 严禁将目录搬移到其自身子目录中
        with self.assertRaises(CriticalPathForbiddenError):
            SafetyGuard.validate_for_migration(
                r"C:\Program Files\JetBrains",
                r"C:\Program Files\JetBrains\SubFolder",
            )

        # 3. 允许搬移具体的第三方大型软件目录或微信数据目录
        self.assertTrue(
            SafetyGuard.validate_for_migration(
                r"C:\Program Files\JetBrains\PyCharm 2026.2",
                r"D:\CDriveRescue_Moved\PyCharm 2026.2",
            )
        )
        self.assertTrue(
            SafetyGuard.validate_for_migration(
                r"C:\Users\Alice\Documents\WeChat Files\wxid_abc123",
                r"D:\CDriveRescue_Moved\wxid_abc123",
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

            # 移除软链接本身后，目标盘中的实体文件必须完好无损
            SafetyGuard.remove_junction_safely(str(link_path))
            self.assertFalse(link_path.exists())
            self.assertTrue(secret_file.exists())
            self.assertEqual(secret_file.stat().st_size, 4096)

    def test_disk_analyzer_stats_and_large_files(self) -> None:
        analyzer = DiskAnalyzer()

        # 验证焦虑分算法
        self.assertGreaterEqual(analyzer._compute_anxiety_score(94.0, 8.0), 90)
        self.assertLess(analyzer._compute_anxiety_score(45.0, 120.0), 50)

        with tempfile.TemporaryDirectory() as tmpdir:
            app_dir = Path(tmpdir) / "MockJetBrains"
            app_dir.mkdir()
            big_file = app_dir / "installer_archive.iso"
            big_file.write_bytes(b"0" * (2 * 1024 * 1024))  # 2 MB

            total_bytes, file_count = analyzer.calculate_dir_stats(str(app_dir))
            self.assertEqual(total_bytes, 2 * 1024 * 1024)
            self.assertEqual(file_count, 1)

            # 测试大文件猎手 (设阈值为 1MB)
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


if __name__ == "__main__":
    unittest.main()
