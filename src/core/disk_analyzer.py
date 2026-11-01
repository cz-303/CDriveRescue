r"""
CDriveRescue (C盘焦虑拯救者) - 磁盘健康度诊断、大文件猎手与 v1.1 深度扫描雷达
===========================================================================
Responsible Model: Gemini (Chief System Architect & Disk I/O Specialist)

Core Responsibilities (v1.0 + v1.1 Step-1 Upgrade):
1. Compute the C-Drive Anxiety Score (0~100) and generate actionable diagnostics.
2. Inspect system-level space hogs (`hiberfil.sys`, `pagefile.sys`, `WinSxS`).
3. High-speed directory tree size aggregation (`os.scandir`) skipping junctions.
4. Discover large standalone files (>500MB) and large migratable software folders.
5. [NEW v1.1] Two-stage ultra-fast duplicate file scanner (`scan_duplicate_files_fast`):
   groups by exact file size -> 64KB head/tail fast fingerprint -> full SHA-256 verification,
   specifically targeting WeChat/QQ `FileStorage\MsgAttach` & `File` duplicate attachments.
6. [NEW v1.1] Windows Known Folders inspector (`inspect_windows_known_folders`):
   evaluates `Downloads`, `Desktop`, `Documents`, `Videos`, and `Pictures` on C: drive.
7. [NEW v1.1] Uninstalled software orphan hunter (`discover_orphan_appdata_dirs`):
   cross-references Windows Uninstall Registry & `Program Files` against `AppData`.
"""

from __future__ import annotations

import hashlib
import heapq
import os
import platform
import re
import shutil
import string
import time
from collections import defaultdict
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple

try:
    import psutil  # type: ignore
except ImportError:
    psutil = None

from src.core.contracts import (
    AnxietyLevel,
    DriveHealthReport,
    DuplicateGroup,
    KnownFolderItem,
    LargeFileItem,
    MigrationTask,
    OrphanResidueItem,
    ProgressCallback,
    RiskLevel,
    SystemSlimItem,
    format_bytes,
)
from src.core.safety_guard import SafetyGuard


class DiskAnalyzer:
    """C盘空间深度体检、大文件扫描、软件搬家雷达与 v1.1 深水区分析引擎"""

    SAFE_ARCHIVE_EXTENSIONS = {
        ".iso": "光盘镜像安装包，安装完成后通常可直接删除或移至 D 盘备份",
        ".zip": "压缩归档包，建议解压确认后移至非系统盘保存",
        ".7z": "7-Zip 压缩包，建议移至非系统盘归档",
        ".rar": "WinRAR 压缩包，建议移至非系统盘归档",
        ".mp4": "超高清视频/录屏文件，可安全剪切至其他数据盘",
        ".mkv": "高清视频媒体文件，可安全剪切至其他数据盘",
        ".mov": "高清视频素材，可安全剪切至其他数据盘",
        ".bak": "历史备份文件，确认无需回滚后可移至外部存储",
        ".dmp": "程序崩溃内存转储文件 (Crash Dump)，若无需调试可直接清理",
    }

    CAUTION_EXTENSIONS = {
        ".exe": "大型安装程序或独立运行包，请确认是下载的安装包而非已安装软件主程序",
        ".msi": "Windows 安装包，若已安装完毕可清理",
        ".vhdx": "WSL2 / Hyper-V / Docker 虚拟磁盘镜像，体积巨大，强烈建议使用软链接或 wsl --export 迁移",
        ".vmdk": "VMware 虚拟机磁盘文件，建议将虚拟机目录整体迁移至 D/E 盘",
    }

    def __init__(self, system_drive: str = "C:\\") -> None:
        self.system_drive = system_drive if platform.system() == "Windows" else "/"

    # -----------------------------------------------------------------------
    # 1. C盘焦虑指数与健康度诊断 (Drive Health & Anxiety Score)
    # -----------------------------------------------------------------------
    def analyze_drive_health(self, target_drive: Optional[str] = None) -> DriveHealthReport:
        drive = target_drive or self.system_drive
        probe = drive if os.path.exists(drive) else os.path.abspath(os.sep)
        usage = shutil.disk_usage(probe)

        total_gb = usage.total / (1024 ** 3)
        free_gb = usage.free / (1024 ** 3)
        used_pct = (usage.used / usage.total * 100.0) if usage.total > 0 else 0.0

        anxiety_score = self._compute_anxiety_score(used_pct, free_gb)
        if anxiety_score >= 75:
            anxiety_level = AnxietyLevel.CRITICAL
        elif anxiety_score >= 40:
            anxiety_level = AnxietyLevel.MODERATE
        else:
            anxiety_level = AnxietyLevel.HEALTHY

        slim_items = self.inspect_system_slim_items(drive)
        target_drives = self.discover_target_drives(exclude_drive=drive)

        headline, recs = self._build_diagnosis_copy(
            used_pct=used_pct,
            free_gb=free_gb,
            total_gb=total_gb,
            anxiety_level=anxiety_level,
            slim_items=slim_items,
            target_drives=target_drives,
        )

        return DriveHealthReport(
            drive_letter=drive,
            total_bytes=usage.total,
            used_bytes=usage.used,
            free_bytes=usage.free,
            usage_percent=round(used_pct, 1),
            anxiety_score=anxiety_score,
            anxiety_level=anxiety_level,
            headline_diagnosis=headline,
            recommendations=recs,
            system_slim_items=slim_items,
            available_target_drives=target_drives,
        )

    @staticmethod
    def _compute_anxiety_score(used_pct: float, free_gb: float) -> int:
        base_score = used_pct
        if free_gb < 10.0:
            base_score = max(base_score, 92.0 + (10.0 - free_gb) * 0.8)
        elif free_gb < 20.0:
            base_score = max(base_score, 78.0 + (20.0 - free_gb) * 1.2)
        elif free_gb < 35.0:
            base_score = max(base_score, 55.0 + (35.0 - free_gb) * 1.0)
        return int(min(100, max(0, round(base_score))))

    def inspect_system_slim_items(self, drive_root: str = "C:\\") -> List[SystemSlimItem]:
        """检测 C 盘根目录下看不见的系统级空间巨兽（hiberfil.sys、pagefile.sys 与 WinSxS 更新备份）"""
        items: List[SystemSlimItem] = []
        root = Path(drive_root)

        # 1. 休眠文件 hiberfil.sys
        hiber_path = root / "hiberfil.sys"
        hiber_size = self._safe_file_size(hiber_path)
        items.append(
            SystemSlimItem(
                id="sys_hiberfil",
                name="Windows 深度休眠文件 (hiberfil.sys)",
                path=str(hiber_path),
                size_bytes=hiber_size,
                exists=hiber_size > 0,
                can_slim=True,
                command_hint="powercfg -h off",
                description=(
                    "用于将整个内存状态写入硬盘以支持深度休眠。台式机或常插电笔记本关闭后"
                    "可瞬间释放 8GB~32GB 空间，且完全不影响正常的合盖睡眠 (Sleep)。"
                ),
            )
        )

        # 2. 虚拟内存页面文件 pagefile.sys
        page_path = root / "pagefile.sys"
        page_size = self._safe_file_size(page_path)
        items.append(
            SystemSlimItem(
                id="sys_pagefile",
                name="Windows 虚拟内存交换文件 (pagefile.sys)",
                path=str(page_path),
                size_bytes=page_size,
                exists=page_size > 0,
                can_slim=False,
                command_hint="sysdm.cpl -> 高级 -> 性能设置 -> 虚拟内存",
                description=(
                    "系统虚拟内存文件。物理内存 >= 32GB 的用户可在系统属性中将其上限调优或迁移至 D 盘 SSD，"
                    "但不建议彻底禁用，以免大型游戏或编译时内存溢出崩溃。"
                ),
            )
        )

        # 3. [NEW v1.1] WinSxS 系统更新组件存储瘦身项
        winsxs_path = root / "Windows" / "WinSxS"
        items.append(
            SystemSlimItem(
                id="sys_winsxs_dism",
                name="Windows 组件存储 (WinSxS)",
                path=str(winsxs_path),
                size_bytes=0,  # 避免启动时阻塞遍历整个 WinSxS
                exists=winsxs_path.exists(),
                can_slim=True,
                command_hint="Dism.exe /Online /Cleanup-Image /StartComponentCleanup",
                description=(
                    "使用 Windows 自带的 DISM 组件清理（StartComponentCleanup），删除已被新版本替代的旧系统组件。"
                    "能释放多少空间取决于系统的更新情况；需要管理员权限，可能耗时较长。"
                ),
            )
        )
        return items

    @staticmethod
    def discover_target_drives(exclude_drive: str = "C:\\") -> List[Tuple[str, int]]:
        norm_exclude = os.path.normcase(exclude_drive.rstrip("\\/"))
        candidates: List[Tuple[str, int]] = []

        if psutil is not None:
            for part in psutil.disk_partitions(all=False):
                if "cdrom" in part.opts.lower() or not part.fstype:
                    continue
                mount = part.mountpoint
                if os.path.normcase(mount.rstrip("\\/")) == norm_exclude:
                    continue
                try:
                    usage = shutil.disk_usage(mount)
                    if usage.free > 5 * (1024 ** 3):
                        candidates.append((mount, usage.free))
                except OSError:
                    continue
        elif platform.system() == "Windows":
            for letter in string.ascii_uppercase:
                mount = f"{letter}:\\"
                if os.path.normcase(mount.rstrip("\\/")) == norm_exclude:
                    continue
                if os.path.exists(mount):
                    try:
                        usage = shutil.disk_usage(mount)
                        if usage.free > 5 * (1024 ** 3):
                            candidates.append((mount, usage.free))
                    except OSError:
                        continue

        candidates.sort(key=lambda item: item[1], reverse=True)
        return candidates

    @staticmethod
    def _build_diagnosis_copy(
        used_pct: float,
        free_gb: float,
        total_gb: float,
        anxiety_level: AnxietyLevel,
        slim_items: List[SystemSlimItem],
        target_drives: List[Tuple[str, int]],
    ) -> Tuple[str, List[str]]:
        recs: List[str] = []
        if anxiety_level == AnxietyLevel.CRITICAL:
            headline = (
                f"C盘处于【重度空间焦虑】状态！仅剩 {free_gb:.1f} GB 可用 ({used_pct:.1f}% 已满)，"
                "随时可能导致微信拒收文件、系统更新失败或软件卡顿。"
            )
            recs.append("先执行【缓存清理】；聊天附件重复较多时，可在实验室用【重复附件硬链接合并】释放重复占用的空间。")
        elif anxiety_level == AnxietyLevel.MODERATE:
            headline = (
                f"C盘处于【中度紧绷】状态（剩余 {free_gb:.1f} GB / 共 {total_gb:.0f} GB），"
                "建议清理历史缓存并将大型软件及“下载/桌面”目录搬家至数据盘。"
            )
            recs.append("使用 v1.1【卸载残留猎手】清理 AppData 中已卸载软件遗留的孤儿文件夹。")
        else:
            headline = (
                f"C盘空间健康充沛（剩余 {free_gb:.1f} GB 可用，使用率 {used_pct:.1f}%），"
                "继续保持良好的软件安装与缓存管理习惯即可。"
            )
            recs.append("新安装大型软件或游戏时，建议直接安装至非系统盘或使用本工具建立软链接。")

        for item in slim_items:
            if item.id == "sys_hiberfil" and item.exists and item.size_bytes > 2 * (1024 ** 3):
                recs.append(
                    f"检测到休眠文件占用 {item.size_human}，点击仪表盘【一键关闭休眠】可立即无损抢回该空间。"
                )

        if target_drives:
            best_drive, best_free = target_drives[0]
            recs.append(
                f"检测到目标数据盘 {best_drive} 拥有 {format_bytes(best_free)} 充裕空间，"
                f"可以把 C 盘上的大文件夹或个人文件夹迁移过去（迁移前会完整复制并逐个校验）。"
            )
        return headline, recs

    # -----------------------------------------------------------------------
    # 2. 高速目录体积计算与大文件猎手 (Fast Directory Size & Large File Hunter)
    # -----------------------------------------------------------------------
    @classmethod
    def calculate_dir_stats(cls, root_path: str) -> Tuple[int, int]:
        if not os.path.exists(root_path) or SafetyGuard.is_junction_or_symlink(root_path):
            return 0, 0

        total_bytes = 0
        file_count = 0
        stack = [root_path]

        while stack:
            current = stack.pop()
            try:
                with os.scandir(current) as it:
                    for entry in it:
                        try:
                            if entry.is_symlink() or SafetyGuard.is_junction_or_symlink(entry.path):
                                continue
                            if entry.is_dir(follow_symlinks=False):
                                stack.append(entry.path)
                            elif entry.is_file(follow_symlinks=False):
                                total_bytes += entry.stat(follow_symlinks=False).st_size
                                file_count += 1
                        except OSError:
                            continue
            except OSError:
                continue

        return total_bytes, file_count

    def scan_large_files(
        self,
        root_paths: Optional[List[str]] = None,
        min_size_bytes: int = 500 * 1024 * 1024,
        top_n: int = 50,
        progress_cb: Optional[ProgressCallback] = None,
    ) -> List[LargeFileItem]:
        if root_paths is None:
            user_home = os.path.expanduser("~")
            root_paths = [user_home]

        min_heap: List[Tuple[int, str, float]] = []
        if top_n <= 0:
            return []
        scanned_dirs = 0
        total_found_bytes = 0

        for start_root in root_paths:
            if not os.path.exists(start_root) or SafetyGuard.is_junction_or_symlink(start_root):
                continue
            stack = [start_root]
            while stack:
                current = stack.pop()
                scanned_dirs += 1

                if progress_cb and scanned_dirs % 25 == 0:
                    pct = min(95, scanned_dirs // 10)
                    progress_cb(pct, f"正在扫描大文件: {current}", total_found_bytes)

                norm_curr = SafetyGuard.normalize_path(current)
                if any(
                    norm_curr == crit or norm_curr.startswith(crit + "\\")
                    for crit in SafetyGuard.CRITICAL_EXACT_OR_PREFIX_PATHS
                ):
                    continue

                try:
                    with os.scandir(current) as it:
                        for entry in it:
                            try:
                                if entry.is_symlink() or SafetyGuard.is_junction_or_symlink(entry.path):
                                    continue
                                if entry.is_dir(follow_symlinks=False):
                                    stack.append(entry.path)
                                elif entry.is_file(follow_symlinks=False):
                                    st = entry.stat(follow_symlinks=False)
                                    if st.st_size >= min_size_bytes:
                                        total_found_bytes += st.st_size
                                        if len(min_heap) < top_n:
                                            heapq.heappush(min_heap, (st.st_size, entry.path, st.st_mtime))
                                        elif st.st_size > min_heap[0][0]:
                                            heapq.heapreplace(min_heap, (st.st_size, entry.path, st.st_mtime))
                            except OSError:
                                continue
                except OSError:
                    continue

        if progress_cb:
            progress_cb(100, "大文件扫描完成", total_found_bytes)

        sorted_entries = sorted(min_heap, key=lambda x: x[0], reverse=True)
        results: List[LargeFileItem] = []
        for size_b, fpath, mtime in sorted_entries:
            name = os.path.basename(fpath)
            ext = os.path.splitext(name)[1].lower()
            risk, suggestion = self._classify_large_file(fpath, ext)
            results.append(
                LargeFileItem(
                    path=fpath,
                    name=name,
                    extension=ext or "无后缀",
                    size_bytes=size_b,
                    modified_timestamp=mtime,
                    risk_level=risk,
                    suggestion=suggestion,
                )
            )
        return results

    def _classify_large_file(self, fpath: str, ext: str) -> Tuple[RiskLevel, str]:
        norm = SafetyGuard.normalize_path(fpath)
        if os.path.basename(norm) in SafetyGuard.PROTECTED_SYSTEM_FILES:
            return RiskLevel.DANGER, "系统核心交换/休眠文件，严禁直接删除"
        if ext in self.SAFE_ARCHIVE_EXTENSIONS:
            return RiskLevel.CAUTION, "安装包或归档文件；仅凭扩展名无法判断是否仍有用途，请自行确认"
        if ext in self.CAUTION_EXTENSIONS:
            return RiskLevel.CAUTION, self.CAUTION_EXTENSIONS[ext]
        if "\\downloads\\" in norm:
            return RiskLevel.CAUTION, "下载目录内的用户文件，请确认用途后再处理"
        return RiskLevel.CAUTION, "大型数据文件，建议确认所属软件后再决定移动或清理"

    # -----------------------------------------------------------------------
    # 3. 可搬家肥胖软件与大目录自动发现雷达 (Migratable App Discovery)
    # -----------------------------------------------------------------------
    def discover_migratable_Directories(
        self,
        custom_scan_roots: Optional[List[Tuple[str, str]]] = None,
        min_size_bytes: int = 200 * 1024 * 1024,
        recommended_target_root: str = r"D:\CDriveRescue_Moved",
        progress_cb: Optional[ProgressCallback] = None,
    ) -> List[MigrationTask]:
        if custom_scan_roots is None:
            user_home = os.path.expanduser("~")
            custom_scan_roots = [
                (r"C:\Program Files", "大型桌面软件 (Program Files)"),
                (r"C:\Program Files (x86)", "32位桌面软件 (Program Files x86)"),
                (os.path.join(user_home, "AppData", "Local"), "应用本地数据与开发缓存 (AppData\\Local)"),
                (os.path.join(user_home, "AppData", "Roaming"), "应用漫游配置与插件包 (AppData\\Roaming)"),
                (os.path.join(user_home, "Documents", "WeChat Files"), "微信聊天与文件存储目录"),
                (os.path.join(user_home, "Documents", "Tencent Files"), "QQ 聊天与接收文件目录"),
            ]

        tasks: List[MigrationTask] = []
        total_candidates_bytes = 0
        total_roots = max(1, len(custom_scan_roots))

        for idx, (root_dir, category_label) in enumerate(custom_scan_roots):
            if not os.path.exists(root_dir):
                continue
            try:
                entries = list(os.scandir(root_dir))
            except OSError:
                continue

            for entry in entries:
                if not entry.is_dir(follow_symlinks=False) and not SafetyGuard.is_junction_or_symlink(entry.path):
                    continue

                dir_path = entry.path
                try:
                    SafetyGuard.validate_for_migration(
                        dir_path, os.path.join(recommended_target_root, entry.name)
                    )
                except Exception:
                    continue

                is_junc = SafetyGuard.is_junction_or_symlink(dir_path)
                junc_target = SafetyGuard.resolve_junction_target(dir_path) if is_junc else None

                if is_junc and junc_target and os.path.exists(junc_target):
                    size_b, f_count = self.calculate_dir_stats(junc_target)
                else:
                    size_b, f_count = self.calculate_dir_stats(dir_path)

                if size_b < min_size_bytes and not is_junc:
                    continue

                total_candidates_bytes += size_b
                locks = SafetyGuard.find_locking_processes(dir_path)
                app_id = f"mig_{abs(hash(SafetyGuard.normalize_path(dir_path))) % (10**8)}"

                tasks.append(
                    MigrationTask(
                        app_id=app_id,
                        app_name=entry.name,
                        source_path=dir_path,
                        recommended_target_root=recommended_target_root,
                        size_bytes=size_b,
                        file_count=f_count,
                        category_label=category_label,
                        is_already_junction=is_junc,
                        junction_target=junc_target,
                        running_locks=locks,
                    )
                )

                if progress_cb:
                    pct = int(((idx + 1) / total_roots) * 95)
                    progress_cb(pct, f"发现可搬家目录: {entry.name} ({format_bytes(size_b)})", total_candidates_bytes)

        tasks.sort(key=lambda t: t.size_bytes, reverse=True)
        if progress_cb:
            progress_cb(100, f"共发现 {len(tasks)} 个可搬家大目录", total_candidates_bytes)
        return tasks

    # -----------------------------------------------------------------------
    # 4. [NEW v1.1] 微信/QQ 重复文件三级闪电哈希与硬链接去重扫描器
    # -----------------------------------------------------------------------
    def scan_duplicate_files_fast(
        self,
        scan_roots: Optional[List[str]] = None,
        min_file_size: int = 1 * 1024 * 1024,  # 默认 >= 1MB 的文件才纳入去重，速度极快且收益高
        source_app_label: str = "WeChat/QQ",
        progress_cb: Optional[ProgressCallback] = None,
    ) -> List[DuplicateGroup]:
        r"""
        [v1.1 核心算法] 三级漏斗重复文件扫描器（按“文件实体”而不是“路径”去重）：

        文件身份：Windows 上 `DirEntry.stat()` 的 st_ino / st_dev / st_nlink 恒为 0，
        因此身份一律通过 `os.stat()` 取得 `(st_dev, st_ino)`；取不到可靠身份（为 0）的文件保守跳过。

        - 扫描根：相同或嵌套的扫描根只保留最外层；目录按真实路径去重，同一路径只登记一次。
        - 实体：指向同一 `(st_dev, st_ino)` 的多条路径（已有硬链接）视为一个实体，只哈希一次。
        - 第一级：按 (卷, 精确字节大小) 分桶；硬链接不能跨卷，主文件只在同一卷内选择。
        - 第二级：同桶实体读取首尾各 64KB 计算快速指纹。
        - 第三级：首尾指纹一致的实体计算全文 SHA-256。
        - 主文件：优先保留链接数最多（已被共享）的实体。
        - 副本：其余实体中仅统计链接数为 1 的实体——执行器会拒绝合并已有其他硬链接的副本，
          因此这类实体既不列入 duplicate_paths，也不计入可省空间。
        """
        if scan_roots is None:
            scan_roots = self.default_duplicate_scan_roots()
        roots = self._dedupe_scan_roots(scan_roots)

        # (st_dev, st_ino) -> {"paths": [...], "size": int, "nlink": int}
        entities: Dict[Tuple[int, int], Dict] = {}
        seen_paths: Set[str] = set()
        seen_dirs: Set[str] = set()
        stack = list(roots)

        while stack:
            current = stack.pop()
            dir_key = os.path.normcase(os.path.realpath(current))
            if dir_key in seen_dirs:
                continue
            seen_dirs.add(dir_key)
            try:
                iterator = os.scandir(current)
            except OSError:
                continue
            with iterator:
                for entry in iterator:
                    try:
                        if entry.is_symlink() or SafetyGuard.is_junction_or_symlink(entry.path):
                            continue
                        if entry.is_dir(follow_symlinks=False):
                            stack.append(entry.path)
                            continue
                        if not entry.is_file(follow_symlinks=False):
                            continue
                        if os.path.splitext(entry.name)[1].lower() in SafetyGuard.FORBIDDEN_HARDLINK_EXTENSIONS:
                            continue
                        path_key = os.path.normcase(os.path.abspath(entry.path))
                        if path_key in seen_paths:
                            continue
                        seen_paths.add(path_key)
                        # 大小用 DirEntry 预筛（Windows 上可靠且无需额外系统调用），身份必须用 os.stat
                        if entry.stat(follow_symlinks=False).st_size < min_file_size:
                            continue
                        st = os.stat(entry.path, follow_symlinks=False)
                        if st.st_size < min_file_size:
                            continue
                        if not st.st_ino or not st.st_dev:
                            continue  # 身份不可靠：无法判断是否已是硬链接，保守地不参与去重
                        key = (st.st_dev, st.st_ino)
                        entity = entities.get(key)
                        if entity is None:
                            entities[key] = {"paths": [entry.path], "size": st.st_size, "nlink": st.st_nlink}
                        else:
                            entity["paths"].append(entry.path)
                    except OSError:
                        continue

        buckets: Dict[Tuple[int, int], List[Tuple[int, int]]] = defaultdict(list)
        for key, entity in entities.items():
            buckets[(key[0], entity["size"])].append(key)
        candidate_buckets = [(bk, keys) for bk, keys in buckets.items() if len(keys) > 1]
        total_buckets = max(1, len(candidate_buckets))
        duplicate_groups: List[DuplicateGroup] = []
        accumulated_savable = 0

        def links(key: Tuple[int, int]) -> int:
            entity = entities[key]
            return max(int(entity["nlink"] or 1), len(entity["paths"]))

        for b_idx, ((dev, fsize), keys) in enumerate(candidate_buckets):
            edge_buckets: Dict[str, List[Tuple[int, int]]] = defaultdict(list)
            for key in keys:
                edge_hash = self._compute_edge_hash(entities[key]["paths"][0], fsize)
                if edge_hash:
                    edge_buckets[edge_hash].append(key)

            for edge_keys in edge_buckets.values():
                if len(edge_keys) < 2:
                    continue
                full_buckets: Dict[str, List[Tuple[int, int]]] = defaultdict(list)
                for key in edge_keys:
                    full_h = self._compute_full_sha256(entities[key]["paths"][0])
                    if full_h:
                        full_buckets[full_h].append(key)

                for sha256_hex, same_keys in full_buckets.items():
                    if len(same_keys) < 2:
                        continue
                    ordered = sorted(same_keys, key=lambda k: (-links(k), sorted(entities[k]["paths"])[0].lower()))
                    master_key, others = ordered[0], ordered[1:]
                    mergeable = [k for k in others if links(k) == 1]
                    if not mergeable:
                        continue
                    master_path = sorted(entities[master_key]["paths"], key=str.lower)[0]
                    grp = DuplicateGroup(
                        group_id=f"dup_{sha256_hex[:10]}_{dev & 0xFFFFFF:06x}",
                        content_hash=sha256_hex,
                        file_name_sample=os.path.basename(master_path),
                        single_size_bytes=fsize,
                        master_path=master_path,
                        duplicate_paths=sorted((entities[k]["paths"][0] for k in mergeable), key=str.lower),
                        source_app=source_app_label,
                    )
                    accumulated_savable += grp.savable_bytes
                    duplicate_groups.append(grp)

            if progress_cb:
                pct = int(((b_idx + 1) / total_buckets) * 95)
                progress_cb(pct, f"正在比对重复文件指纹 ({b_idx + 1}/{total_buckets})", accumulated_savable)

        duplicate_groups.sort(key=lambda g: g.savable_bytes, reverse=True)
        if progress_cb:
            progress_cb(100, f"共发现 {len(duplicate_groups)} 组可硬链接去重的重复文件", accumulated_savable)
        return duplicate_groups

    @staticmethod
    def _dedupe_scan_roots(scan_roots: List[str]) -> List[str]:
        """去掉不存在、重复以及嵌套在其他根之内的扫描根（按真实路径比较）。"""
        resolved: List[Tuple[str, str]] = []
        for root in scan_roots:
            if not root or not os.path.isdir(root):
                continue
            real = os.path.normcase(os.path.realpath(os.path.abspath(root)))
            resolved.append((real, os.path.abspath(root)))
        resolved.sort(key=lambda pair: len(pair[0]))
        kept: List[Tuple[str, str]] = []
        for real, original in resolved:
            if any(real == k or real.startswith(k.rstrip(os.sep) + os.sep) for k, _ in kept):
                continue
            kept.append((real, original))
        return [original for _, original in kept]

    def default_duplicate_scan_roots(self) -> List[str]:
        """默认扫描：实际“文档”下的 WeChat Files / Tencent Files，以及实际“下载”文件夹。读取失败的位置跳过。"""
        roots: List[str] = []
        documents = self.resolve_known_folder("Documents")
        downloads = self.resolve_known_folder("Downloads")
        if documents:
            roots += [os.path.join(documents, "WeChat Files"), os.path.join(documents, "Tencent Files")]
        if downloads:
            roots.append(downloads)
        return roots

    @staticmethod
    def _compute_edge_hash(file_path: str, file_size: int, chunk_size: int = 65536) -> Optional[str]:
        """读取文件头部 64KB 与尾部 64KB 生成极速复合指纹"""
        try:
            h = hashlib.blake2b(digest_size=16)
            with open(file_path, "rb") as fp:
                head = fp.read(chunk_size)
                h.update(head)
                if file_size > chunk_size * 2:
                    fp.seek(file_size - chunk_size)
                    tail = fp.read(chunk_size)
                    h.update(tail)
            return h.hexdigest()
        except OSError:
            return None

    @staticmethod
    def _compute_full_sha256(file_path: str, buffer_size: int = 1024 * 1024) -> Optional[str]:
        """分块计算文件完整 SHA-256 校验值"""
        try:
            h = hashlib.sha256()
            with open(file_path, "rb") as fp:
                while chunk := fp.read(buffer_size):
                    h.update(chunk)
            return h.hexdigest()
        except OSError:
            return None

    # -----------------------------------------------------------------------
    # 5. [NEW v1.1] Windows 系统个人文件夹（桌面/下载/文档/视频）检测
    # -----------------------------------------------------------------------
    def inspect_windows_known_folders(
        self,
        custom_user_home: Optional[str] = None,
        recommended_target_drive: str = "D:\\",
    ) -> List[KnownFolderItem]:
        r"""
        检测五大系统个人文件夹（Downloads / Desktop / Documents / Videos / Pictures）的实际位置与体积，
        供 GPT 的 `ISystemOptimizer.relocate_known_folder` 一键搬家。

        - 正常运行：通过 Windows `SHGetKnownFolderPath` 读取实际位置（已重定向、OneDrive 管理、
          本工具迁移过的位置都能反映出来）。
        - `custom_user_home`：显式测试/自定义模式，按 `<custom_user_home>\<Key>` 拼接。
        - 某个文件夹读取失败时不猜测路径：该文件夹不出现在结果中，原因记录在
          `self.last_known_folder_errors[key]`，界面据此显示“无法读取，不提供迁移”。
        - 是否位于系统盘按实际系统盘（%SystemDrive%）判断，不写死 C 盘。
        """
        self.last_known_folder_errors: Dict[str, str] = {}
        user_home = custom_user_home or os.path.expanduser("~")
        username = os.path.basename(user_home.rstrip("\\/")) or "User"
        system_drive = self.actual_system_drive()
        target_root = recommended_target_drive.rstrip("\\/")
        results: List[KnownFolderItem] = []

        for key, (disp_name, guid) in SafetyGuard.WINDOWS_KNOWN_FOLDERS.items():
            if custom_user_home is not None:
                curr_path = os.path.join(custom_user_home, key)
            else:
                try:
                    curr_path = self._query_known_folder_path(guid)
                except Exception as exc:  # 读取失败时不把猜测路径当成已确认路径
                    self.last_known_folder_errors[key] = f"{disp_name}：无法从 Windows 读取实际位置（{exc}）"
                    continue

            size_b, f_count = self.calculate_dir_stats(curr_path) if os.path.exists(curr_path) else (0, 0)
            results.append(
                KnownFolderItem(
                    folder_key=key,
                    display_name=disp_name,
                    folder_guid=guid,
                    current_path=curr_path,
                    recommended_target_path=f"{target_root}\\Users\\{username}\\{key}",
                    size_bytes=size_b,
                    file_count=f_count,
                    is_on_system_drive=self._is_on_drive(curr_path, system_drive),
                )
            )

        results.sort(key=lambda item: item.size_bytes, reverse=True)
        return results

    def resolve_known_folder(self, key: str) -> Optional[str]:
        """返回个人文件夹的实际位置；Windows 上读取失败返回 None。非 Windows（开发/测试）按主目录拼接。"""
        entry = SafetyGuard.WINDOWS_KNOWN_FOLDERS.get(key)
        if entry is None:
            return None
        if platform.system() != "Windows":
            return os.path.join(os.path.expanduser("~"), key)
        try:
            return self._query_known_folder_path(entry[1])
        except Exception:
            return None

    @staticmethod
    def _query_known_folder_path(guid: str) -> str:
        """调用 Shell32 `SHGetKnownFolderPath` 读取个人文件夹实际位置（只读，不修改任何配置）。"""
        if platform.system() != "Windows":
            raise OSError("只能在 Windows 上读取个人文件夹位置")
        import ctypes
        import uuid
        from ctypes import wintypes

        class _GUID(ctypes.Structure):
            _fields_ = [("Data1", wintypes.DWORD), ("Data2", wintypes.WORD),
                        ("Data3", wintypes.WORD), ("Data4", ctypes.c_ubyte * 8)]

        raw = uuid.UUID(guid).bytes_le
        folder_id = _GUID.from_buffer_copy(raw)
        shell32, ole32 = ctypes.windll.shell32, ctypes.windll.ole32
        shell32.SHGetKnownFolderPath.argtypes = [ctypes.POINTER(_GUID), wintypes.DWORD, wintypes.HANDLE,
                                                 ctypes.POINTER(ctypes.c_void_p)]
        shell32.SHGetKnownFolderPath.restype = ctypes.c_long
        ole32.CoTaskMemFree.argtypes = [ctypes.c_void_p]
        buffer = ctypes.c_void_p()
        hr = shell32.SHGetKnownFolderPath(ctypes.byref(folder_id), 0, None, ctypes.byref(buffer))
        try:
            if hr < 0 or not buffer.value:
                raise OSError(f"SHGetKnownFolderPath 失败: HRESULT 0x{hr & 0xFFFFFFFF:08X}")
            path = ctypes.wstring_at(buffer.value)
        finally:
            if buffer.value:
                ole32.CoTaskMemFree(buffer)
        if not path:
            raise OSError("Windows 返回了空路径")
        return path

    @staticmethod
    def actual_system_drive() -> str:
        """实际系统盘（如 "C:"）。非 Windows 返回 "/"。"""
        if platform.system() != "Windows":
            return "/"
        drive = os.environ.get("SystemDrive", "")
        if not drive:
            try:
                import ctypes
                buf = ctypes.create_unicode_buffer(260)
                if ctypes.windll.kernel32.GetWindowsDirectoryW(buf, 260):
                    drive = os.path.splitdrive(buf.value)[0]
            except Exception:
                drive = ""
        return (drive or "C:").rstrip("\\/")

    @staticmethod
    def _is_on_drive(path: str, system_drive: str) -> bool:
        if system_drive == "/":
            return os.path.isabs(path)
        import ntpath
        return ntpath.splitdrive(path)[0].casefold() == system_drive.casefold()

    # -----------------------------------------------------------------------
    # 6. [NEW v1.1] 卸载残留猎手（AppData / ProgramData 孤儿目录智能识别）
    # -----------------------------------------------------------------------
    def discover_orphan_appdata_dirs(
        self,
        appdata_roots: Optional[List[Tuple[str, str]]] = None,
        installed_app_names: Optional[Set[str]] = None,
        min_size_bytes: int = 50 * 1024 * 1024,  # 默认 >= 50MB
        stale_days_threshold: int = 60,          # 默认超过 60 天未修改视为高置信度残留
    ) -> List[OrphanResidueItem]:
        """
        [v1.1 创新功能] 扫描 `AppData\\Local`、`AppData\\Roaming` 与 `ProgramData`，
        对比当前已安装软件词根集合（来自 `Program Files` 与注册表 `Uninstall`），
        精准揪出“软件早已卸载，但残留文件夹仍霸占数 GB 空间”的孤儿目录。
        """
        if appdata_roots is None:
            user_home = os.path.expanduser("~")
            appdata_roots = [
                (os.path.join(user_home, "AppData", "Local"), "AppData\\Local"),
                (os.path.join(user_home, "AppData", "Roaming"), "AppData\\Roaming"),
                (r"C:\ProgramData", "ProgramData"),
            ]

        active_tokens = installed_app_names if installed_app_names is not None else self._collect_installed_app_tokens()
        orphans: List[OrphanResidueItem] = []
        now_ts = time.time()

        for root_dir, scope_label in appdata_roots:
            if not os.path.exists(root_dir):
                continue
            try:
                entries = list(os.scandir(root_dir))
            except OSError:
                continue

            for entry in entries:
                if not entry.is_dir(follow_symlinks=False) or SafetyGuard.is_junction_or_symlink(entry.path):
                    continue

                folder_name = entry.name
                if SafetyGuard.is_protected_appdata_vendor(folder_name, active_tokens):
                    continue

                size_b, f_count = self.calculate_dir_stats(entry.path)
                if size_b < min_size_bytes:
                    continue

                try:
                    mtime = entry.stat(follow_symlinks=False).st_mtime
                except OSError:
                    mtime = now_ts

                days_ago = max(0, int((now_ts - mtime) / 86400))
                if days_ago >= stale_days_threshold:
                    confidence = RiskLevel.SAFE
                    reason = (
                        f"未在系统已安装程序列表中找到匹配项，且该目录已连续 {days_ago} 天无任何读写活动，"
                        "极可能是已卸载软件遗留的孤儿数据。"
                    )
                else:
                    confidence = RiskLevel.CAUTION
                    reason = (
                        f"未在标准安装目录中找到同名程序（最近修改于 {days_ago} 天前），"
                        "可能是绿色版软件配置或近期刚卸载软件的残留，建议确认后移入回收站。"
                    )

                orphan_id = f"orph_{abs(hash(SafetyGuard.normalize_path(entry.path))) % (10**8)}"
                orphans.append(
                    OrphanResidueItem(
                        orphan_id=orphan_id,
                        folder_name=folder_name,
                        path=entry.path,
                        location_scope=scope_label,
                        size_bytes=size_b,
                        file_count=f_count,
                        last_modified_days_ago=days_ago,
                        confidence_level=confidence,
                        reason=reason,
                    )
                )

        orphans.sort(key=lambda item: item.size_bytes, reverse=True)
        return orphans

    @staticmethod
    def _collect_installed_app_tokens() -> Set[str]:
        """收集本机 Program Files 及 Windows 注册表 Uninstall 中的活跃软件词根（小写化）"""
        tokens: Set[str] = set()
        program_dirs = [r"C:\Program Files", r"C:\Program Files (x86)"]
        for pdir in program_dirs:
            if os.path.exists(pdir):
                try:
                    for entry in os.scandir(pdir):
                        if entry.is_dir(follow_symlinks=False):
                            for word in re.split(r"[\s\-_]+", entry.name.lower()):
                                if len(word) >= 3:
                                    tokens.add(word)
                except OSError:
                    pass

        if platform.system() == "Windows":
            try:
                import winreg  # type: ignore

                uninstall_paths = [
                    r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall",
                    r"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall",
                ]
                for hive in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
                    for upath in uninstall_paths:
                        try:
                            with winreg.OpenKey(hive, upath) as key:
                                num_subkeys = winreg.QueryInfoKey(key)[0]
                                for i in range(num_subkeys):
                                    try:
                                        sub_name = winreg.EnumKey(key, i)
                                        with winreg.OpenKey(key, sub_name) as subkey:
                                            for val_name in ("DisplayName", "Publisher"):
                                                try:
                                                    val, _ = winreg.QueryValueEx(subkey, val_name)
                                                    for word in re.split(r"[\s\-_]+", str(val).lower()):
                                                        if len(word) >= 3:
                                                            tokens.add(word)
                                                except OSError:
                                                    pass
                                    except OSError:
                                        continue
                        except OSError:
                            continue
            except ImportError:
                pass

        return tokens

    @staticmethod
    def _safe_file_size(path: Path) -> int:
        try:
            if path.exists():
                return path.stat().st_size
        except OSError:
            pass
        return 0
