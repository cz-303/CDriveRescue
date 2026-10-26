r"""
CDriveRescue (C盘焦虑拯救者) - 磁盘健康度诊断、大文件猎手与可搬家软件雷达
===========================================================================
Responsible Model: Gemini (Chief System Architect & Disk I/O Specialist)

Core Responsibilities:
1. Compute the C-Drive Anxiety Score (0~100) and generate actionable, empathetic
   diagnostics for users suffering from low disk space.
2. Inspect system-level space hogs (`C:\hiberfil.sys`, `C:\pagefile.sys`, `C:\swapfile.sys`).
3. High-speed directory tree size aggregation using `os.scandir` without following
   reparse points/junctions (preventing double counting or infinite recursion).
4. Discover large standalone files (>500MB by default, e.g., `.iso`, `.zip`, `.mp4`, `.vhdx`)
   and large migratable software folders in `Program Files`, `AppData`, and WeChat/QQ dirs.
"""

from __future__ import annotations

import heapq
import os
import platform
import shutil
import string
from pathlib import Path
from typing import List, Optional, Tuple

try:
    import psutil  # type: ignore
except ImportError:
    psutil = None

from src.core.contracts import (
    AnxietyLevel,
    DriveHealthReport,
    LargeFileItem,
    MigrationTask,
    ProgressCallback,
    RiskLevel,
    SystemSlimItem,
    format_bytes,
)
from src.core.safety_guard import SafetyGuard


class DiskAnalyzer:
    """C盘空间深度体检、大文件扫描与软件搬家雷达引擎"""

    # 常见可安全归档或移至其他盘的大文件扩展名提示表
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
        r"""
        生成完整的 C 盘健康度与空间焦虑评估报告：
        - 综合已用百分比与绝对剩余 GB 数计算 0~100 焦虑指数
        - 自动探测本机可用的目标搬家数据盘（如 D:\, E:\）
        - 检测休眠文件 hiberfil.sys 与虚拟内存占用
        """
        drive = target_drive or self.system_drive
        probe = drive if os.path.exists(drive) else os.path.abspath(os.sep)
        usage = shutil.disk_usage(probe)

        total_gb = usage.total / (1024 ** 3)
        free_gb = usage.free / (1024 ** 3)
        used_pct = (usage.used / usage.total * 100.0) if usage.total > 0 else 0.0

        # 计算空间焦虑指数 (0~100)
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
        """
        结合使用率百分比与剩余绝对容量计算焦虑分：
        即使大硬盘使用率 85%，若剩余不到 15GB 也会触发系统卡顿与微信无法接收文件。
        """
        base_score = used_pct
        if free_gb < 10.0:
            base_score = max(base_score, 92.0 + (10.0 - free_gb) * 0.8)
        elif free_gb < 20.0:
            base_score = max(base_score, 78.0 + (20.0 - free_gb) * 1.2)
        elif free_gb < 35.0:
            base_score = max(base_score, 55.0 + (35.0 - free_gb) * 1.0)
        return int(min(100, max(0, round(base_score))))

    def inspect_system_slim_items(self, drive_root: str = "C:\\") -> List[SystemSlimItem]:
        """检测 C 盘根目录下看不见的系统级空间巨兽（hiberfil.sys 与 pagefile.sys）"""
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
        return items

    @staticmethod
    def discover_target_drives(exclude_drive: str = "C:\\") -> List[Tuple[str, int]]:
        """发现本机所有可用于存放搬家软件的其他逻辑磁盘分区（按剩余空间从大到小排序）"""
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
            recs.append("立即执行【深度缓存清理】，一键释放系统 Temp、浏览器及社交软件缩略图缓存。")
        elif anxiety_level == AnxietyLevel.MODERATE:
            headline = (
                f"C盘处于【中度紧绷】状态（剩余 {free_gb:.1f} GB / 共 {total_gb:.0f} GB），"
                "建议清理历史缓存并将大型软件搬家至数据盘。"
            )
            recs.append("定期清理开发工具包缓存（pip / npm / Gradle）与微信视频缓存。")
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
                f"非常适合通过 `mklink /J` 将 C 盘肥胖软件无损搬家过去。"
            )
        return headline, recs

    # -----------------------------------------------------------------------
    # 2. 高速目录体积计算与大文件猎手 (Fast Directory Size & Large File Hunter)
    # -----------------------------------------------------------------------
    @classmethod
    def calculate_dir_stats(cls, root_path: str) -> Tuple[int, int]:
        """
        使用非递归栈 + `os.scandir` 高速统计目录总字节数与文件总数。
        严格跳过 NTFS Junction / 符号链接子目录，避免重复统计已搬家的目录或陷入死循环。
        """
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
        min_size_bytes: int = 500 * 1024 * 1024,  # 默认 500 MB
        top_n: int = 50,
        progress_cb: Optional[ProgressCallback] = None,
    ) -> List[LargeFileItem]:
        """
        大文件猎手：扫描用户目录或指定路径下体积超过阈值的大文件，并按大小降序返回 Top-N。
        自动跳过 `C:\\Windows` 等核心系统目录，避免误导用户删除系统镜像。
        """
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

                # 跳过受保护的 Windows 核心目录
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

        # 按文件体积从大到小排序并包装为 LargeFileItem
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
        min_size_bytes: int = 200 * 1024 * 1024,  # 默认 >= 200MB 才推荐搬家
        recommended_target_root: str = r"D:\CDriveRescue_Moved",
        progress_cb: Optional[ProgressCallback] = None,
    ) -> List[MigrationTask]:
        r"""
        自动扫描 C 盘常见软件安装与大型数据堆积目录（Program Files、AppData\Local、微信/QQ 数据目录），
        找出适合通过 `mklink /J` 无损搬家的大体积文件夹，并检测当前是否已建立软链接或有进程占用。
        """
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
                # 先过一遍 SafetyGuard，剔除 Windows Defender、Microsoft 核心目录等不可搬家项
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

    @staticmethod
    def _safe_file_size(path: Path) -> int:
        try:
            if path.exists():
                return path.stat().st_size
        except OSError:
            pass
        return 0
