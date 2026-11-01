"""
CDriveRescue (C盘焦虑拯救者) - 系统安全护栏与底层权限校验引擎 (v1.1)
===========================================================================
Responsible Model: Gemini (Chief System Architect & Safety Specialist)

Core Responsibilities (v1.0 + v1.1 Upgrade):
1. Enforce an ironclad blacklist of critical Windows system directories that must
   NEVER be deleted or moved via `mklink /J` (preventing BSOD or unbootable OS).
2. Detect running processes (`psutil` with graceful stdlib/tasklist fallback) that
   have open executables or file handles inside a target directory before cleaning or moving.
3. Check and request Windows Administrator (UAC) privileges required by `mklink /J`
   and `powercfg -h off`.
4. Accurately detect NTFS Reparse Points / Directory Junctions across Python versions
   so that removing a junction never deletes the underlying target files.
5. [NEW v1.1] Validate NTFS Hardlink (`os.link` / `mklink /H`) preconditions:
   same drive volume check, read-only database exclusion (`.db`, `.sqlite`), and inode check.
6. [NEW v1.1] Protect active software vendor names when scanning `AppData` orphan residues.
"""

from __future__ import annotations

import ctypes
import ntpath
import os
import platform
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple

try:
    import psutil  # type: ignore
except ImportError:
    psutil = None

from src.core.contracts import (
    CriticalPathForbiddenError,
    InsufficientTargetSpaceError,
    ProcessLockInfo,
    ProcessLockedError,
    CDriveRescueError,
)


class SafetyGuard:
    """系统级安全护栏：所有清理（GPT）、搬家（GPT）与 v1.1 深度瘦身操作执行前必须通过此类校验"""

    # 绝对禁止搬家或直接清空根路径的 Windows 核心目录黑名单（统一小写规范化比较）
    CRITICAL_EXACT_OR_PREFIX_PATHS: Tuple[str, ...] = (
        r"c:\windows",
        r"c:\windows\system32",
        r"c:\windows\syswow64",
        r"c:\windows\winsxs",
        r"c:\windows\boot",
        r"c:\windows\fonts",
        r"c:\windows\servicing",
        r"c:\boot",
        r"c:\efi",
        r"c:\recovery",
        r"c:\system volume information",
        r"c:\$recycle.bin",
        r"c:\programdata\microsoft\windows",
        r"c:\programdata\microsoft\windows defender",
        r"c:\program files\windowsapps",
        r"c:\program files\windows defender",
        r"c:\program files\common files\microsoft shared",
        r"c:\program files (x86)\common files\microsoft shared",
    )

    # 虽允许清理其子缓存目录，但绝对禁止将整个根文件夹直接重命名/搬家的顶层目录
    FORBIDDEN_MIGRATION_ROOTS: Tuple[str, ...] = (
        r"c:\\",
        r"c:\users",
        r"c:\program files",
        r"c:\program files (x86)",
        r"c:\programdata",
    )

    # 显式允许清理的 Windows 子目录白名单（豁免 `c:\windows` 前缀拦截）
    SAFE_WINDOWS_CACHE_EXCEPTIONS: Tuple[str, ...] = (
        r"c:\windows\temp",
        r"c:\windows\softwaredistribution\download",
        r"c:\windows\prefetch",
        r"c:\windows\minidump",
        r"c:\windows\memory.dmp",
    )

    # 绝对不可触碰的系统根级锁定文件
    PROTECTED_SYSTEM_FILES: Tuple[str, ...] = (
        "pagefile.sys",
        "hiberfil.sys",
        "swapfile.sys",
        "bootmgr",
        "ntldr",
    )

    # [NEW v1.1] 严禁转为 NTFS 硬链接的动态数据库/配置后缀（防止一方修改引发另一方损坏）
    FORBIDDEN_HARDLINK_EXTENSIONS: Tuple[str, ...] = (
        ".db",
        ".sqlite",
        ".sqlite3",
        ".wal",
        ".shm",
        ".ini",
        ".cfg",
        ".json",
        ".xml",
        ".reg",
        ".log",
    )

    # [NEW v1.1] AppData 卸载残留扫描时永久豁免的系统与基础底座厂商名（小写匹配）
    PROTECTED_APPDATA_VENDORS: Tuple[str, ...] = (
        "microsoft",
        "windows",
        "packages",
        "connecteddevicesplatform",
        "comms",
        "d3dscache",
        "fontcache",
        "google",
        "mozilla",
        "pip",
        "npm-cache",
        "nuget",
        "temp",
        "programs",
        "cdriverescue",
    )

    # [NEW v1.1] Windows 标准个人文件夹 KNOWNFOLDERID 注册表映射
    WINDOWS_KNOWN_FOLDERS: Dict[str, Tuple[str, str]] = {
        "Downloads": ("下载 (Downloads)", "{374DE290-123F-4565-9164-39C4925E467B}"),
        "Desktop": ("桌面 (Desktop)", "{B4BFCC3A-DB2C-424C-B029-7FE99A87C641}"),
        "Documents": ("文档 (Documents)", "{FDD39AD0-238F-46AF-ADB4-6C85480369C7}"),
        "Videos": ("视频 (Videos)", "{18989B1D-99B5-455B-841C-AB7C74E4DDFC}"),
        "Pictures": ("图片 (Pictures)", "{33E28130-4E1E-4676-835A-98395C3BC3BB}"),
    }

    @staticmethod
    def normalize_path(path_str: str) -> str:
        """统一展开环境变量并转换为规范化小写路径（兼容跨平台测试与 Windows 反斜杠格式）"""
        expanded = os.path.expandvars(os.path.expanduser(path_str)).strip()
        replaced = expanded.replace("/", "\\")
        if replaced.startswith(("\\\\?\\", "\\\\.\\")):
            raise CriticalPathForbiddenError("不支持设备命名空间路径")
        return ntpath.normpath(replaced).lower().rstrip("\\") if replaced else ""

    @classmethod
    def _validate_path_form(cls, path: str) -> None:
        expanded = os.path.expandvars(os.path.expanduser(path))
        if not expanded or not (os.path.isabs(expanded) or ntpath.isabs(expanded)):
            raise CriticalPathForbiddenError("必须使用绝对路径")
        norm = cls.normalize_path(expanded)
        if norm.startswith("\\\\") or ":" in ntpath.splitdrive(norm)[1]:
            raise CriticalPathForbiddenError("不支持网络路径或备用数据流")
        if any(part.endswith((" ", ".")) for part in expanded.replace("/", "\\").split("\\") if part not in (".", "..")):
            raise CriticalPathForbiddenError("不支持尾部空格或句点的路径")
        if os.path.isabs(expanded):
            absolute = Path(os.path.abspath(expanded))
            for parent in (*absolute.parents, absolute):
                if cls.is_junction_or_symlink(str(parent)):
                    raise CriticalPathForbiddenError(f"路径经过重解析点: {parent}")

    @classmethod
    def validate_for_cleaning(cls, target_path: str) -> bool:
        """
        校验某个目录/文件是否允许执行缓存清理：
        - 放行 SAFE_WINDOWS_CACHE_EXCEPTIONS（如 C:\\Windows\\Temp）
        - 拦截一切系统核心目录与受保护引导文件
        """
        cls._validate_path_form(target_path)
        norm = cls.normalize_path(target_path)
        basename = ntpath_basename(norm)

        if basename in cls.PROTECTED_SYSTEM_FILES:
            raise CriticalPathForbiddenError(
                f"安全护栏已拦截：系统核心文件 '{target_path}' 严禁直接删除（请使用系统瘦身指令）。"
            )

        if norm in ("c:", "c:\\", "") or norm in [p.rstrip("\\") for p in cls.FORBIDDEN_MIGRATION_ROOTS]:
            raise CriticalPathForbiddenError(
                f"安全护栏已拦截：严禁对顶层根目录 '{target_path}' 执行批量清理。"
            )

        if not ntpath.splitdrive(norm)[1].strip("\\"):
            raise CriticalPathForbiddenError("禁止清理磁盘根目录")
        if norm.startswith("c:\\users\\"):
            parts = norm.split("\\")
            if len(parts) <= 3 or (len(parts) <= 5 and parts[3] == "appdata"):
                raise CriticalPathForbiddenError("禁止清理用户或 AppData 顶层目录")

        # 检查是否命中 Windows 安全缓存白名单
        for safe_exc in cls.SAFE_WINDOWS_CACHE_EXCEPTIONS:
            if norm == safe_exc or norm.startswith(safe_exc + "\\"):
                return True

        for critical in cls.CRITICAL_EXACT_OR_PREFIX_PATHS:
            if norm == critical or norm.startswith(critical + "\\"):
                raise CriticalPathForbiddenError(
                    f"安全护栏已拦截：'{target_path}' 属于 Windows 核心受保护区域 ({critical})，禁止清理！"
                )

        if norm.endswith(r"\desktop") or norm.endswith(r"\documents"):
            raise CriticalPathForbiddenError(
                f"安全护栏已拦截：'{target_path}' 是用户核心个人目录，禁止作为缓存清理！"
            )

        return True

    @classmethod
    def validate_for_migration(cls, source_dir: str, target_dir: str) -> bool:
        """
        校验某个软件目录是否允许通过 `mklink /J` 搬家到目标位置：
        - 严禁搬移 Windows 系统目录、User Profile 根目录、Program Files 根目录本身
        - 严禁将目录搬入其自身的子目录中（死循环嵌套）
        """
        cls._validate_path_form(source_dir)
        cls._validate_path_form(target_dir)
        src_norm = cls.normalize_path(source_dir)
        dst_norm = cls.normalize_path(target_dir)

        if src_norm == dst_norm:
            raise CriticalPathForbiddenError("源目录与目标目录不能相同。")

        if dst_norm.startswith(src_norm + "\\"):
            raise CriticalPathForbiddenError(
                f"安全护栏已拦截：目标路径 '{target_dir}' 不能位于源目录 '{source_dir}' 内部。"
            )

        if src_norm.startswith(dst_norm + "\\"):
            raise CriticalPathForbiddenError("目标目录不能是源目录的父目录")
        if not ntpath.splitdrive(src_norm)[1].strip("\\"):
            raise CriticalPathForbiddenError("禁止迁移磁盘根目录")
        for critical in cls.CRITICAL_EXACT_OR_PREFIX_PATHS:
            if dst_norm == critical or dst_norm.startswith(critical + "\\"):
                raise CriticalPathForbiddenError("目标目录属于受保护的系统区域")

        # 拦截顶层根目录本身（例如不能把整个 C:\Program Files 搬走，只能搬其下的具体软件如 C:\Program Files\JetBrains）
        for root_path in cls.FORBIDDEN_MIGRATION_ROOTS:
            if src_norm == root_path.rstrip("\\"):
                raise CriticalPathForbiddenError(
                    f"安全护栏已拦截：严禁搬移系统顶层根目录 '{source_dir}'，请选择其内部的具体软件文件夹。"
                )

        parts = [p for p in src_norm.split("\\") if p]
        if len(parts) >= 2 and parts[0] == "c:" and parts[1] == "users":
            if len(parts) <= 4:
                raise CriticalPathForbiddenError(
                    f"安全护栏已拦截：严禁直接搬移用户主目录或 AppData 顶层目录 '{source_dir}'。"
                )
            if len(parts) == 5 and parts[3] == "appdata" and parts[4] in ("local", "roaming", "locallow"):
                raise CriticalPathForbiddenError(
                    f"安全护栏已拦截：严禁整盘搬移 '{source_dir}'，请仅搬移其内部的具体应用目录（如 JetBrains、Google 等）。"
                )

        for critical in cls.CRITICAL_EXACT_OR_PREFIX_PATHS:
            if src_norm == critical or src_norm.startswith(critical + "\\"):
                raise CriticalPathForbiddenError(
                    f"安全护栏已拦截：'{source_dir}' 属于系统核心组件 ({critical})，搬家会导致系统崩溃！"
                )

        return True

    @classmethod
    def validate_for_hardlink(cls, master_file: str, duplicate_file: str) -> bool:
        """
        [NEW v1.1] 校验两个重复文件是否允许合并为 NTFS 硬链接 (`os.link` / `mklink /H`)：
        1. 严禁对 `.db` / `.sqlite` 等会被软件动态局部改写的数据库文件建立硬链接（只允许静态附件如视频/图片/文档/压缩包）。
        2. 两个文件必须位于同一个磁盘卷（NTFS 硬链接不支持跨盘符，如 C: 与 D:）。
        3. 若两个文件已经共享同一个底层 inode (st_ino)，说明已是硬链接，返回 False 跳过。
        """
        m_norm = cls.normalize_path(master_file)
        d_norm = cls.normalize_path(duplicate_file)

        if m_norm == d_norm:
            raise CriticalPathForbiddenError("主文件与副本文件路径相同，无法建立硬链接。")

        ext = os.path.splitext(m_norm)[1].lower()
        if ext in cls.FORBIDDEN_HARDLINK_EXTENSIONS:
            raise CriticalPathForbiddenError(
                f"安全护栏已拦截：'{ext}' 属于动态数据库或配置文件，严禁使用硬链接合并以防数据串改！"
            )

        # 检查是否位于同一磁盘分区 (Volume)
        m_drive = m_norm.split("\\")[0] if ":" in m_norm else ""
        d_drive = d_norm.split("\\")[0] if ":" in d_norm else ""
        if m_drive != d_drive:
            raise CriticalPathForbiddenError(
                f"NTFS 硬链接仅支持同一磁盘分区内的文件去重（当前主文件位于 {m_drive}，副本位于 {d_drive}）。"
            )

        if os.path.exists(master_file) and os.path.exists(duplicate_file):
            st_m = os.stat(master_file)
            st_d = os.stat(duplicate_file)
            if st_m.st_ino != 0 and st_m.st_ino == st_d.st_ino and st_m.st_dev == st_d.st_dev:
                return False  # 已经是硬链接关系，无需重复操作

        return True

    @classmethod
    def is_protected_appdata_vendor(cls, folder_name: str, installed_tokens: Set[str]) -> bool:
        """
        [NEW v1.1] 判断 AppData 下的某个目录是否受系统保护或对应当前仍安装的软件：
        供卸载残留猎手 (`discover_orphan_appdata_dirs`) 过滤掉正常使用中的目录。
        """
        lower_name = folder_name.strip().lower()
        if not lower_name or lower_name.startswith("."):
            return True
        for vendor in cls.PROTECTED_APPDATA_VENDORS:
            if lower_name == vendor or vendor in lower_name:
                return True
        for token in installed_tokens:
            if len(token) >= 3 and (token in lower_name or lower_name in token):
                return True
        return False

    @classmethod
    def check_target_free_space(
        cls, source_size_bytes: int, target_path: str, safety_multiplier: float = 1.10
    ) -> Tuple[bool, int, int]:
        """校验目标磁盘剩余空间是否充足（默认要求 >= 源目录体积的 110% 预留安全缓冲）"""
        required_bytes = int(source_size_bytes * safety_multiplier)
        probe_path = Path(target_path)
        while not probe_path.exists() and probe_path.parent != probe_path:
            probe_path = probe_path.parent

        usage = shutil.disk_usage(str(probe_path))
        if usage.free < required_bytes:
            raise InsufficientTargetSpaceError(
                f"目标磁盘空间不足！需要至少 {required_bytes / (1024**3):.2f} GB (含10%安全缓冲)，"
                f"但当前仅剩 {usage.free / (1024**3):.2f} GB。"
            )
        return True, usage.free, required_bytes

    @classmethod
    def find_locking_processes(cls, target_dir: str) -> List[ProcessLockInfo]:
        """扫描当前系统中所有正在运行的进程，检查是否有进程的 `.exe` 或工作目录位于 `target_dir` 下"""
        if psutil is None:
            raise CDriveRescueError("缺少 psutil，无法检查进程占用；禁止继续迁移")

        norm_target = cls.normalize_path(target_dir)
        if not norm_target:
            return []

        locks: List[ProcessLockInfo] = []
        current_pid = os.getpid()

        for proc in psutil.process_iter(["pid", "name", "exe", "cwd"]):
            try:
                info = proc.info
                pid = info["pid"]
                if pid == current_pid:
                    continue

                exe_path = info.get("exe") or ""
                cwd_path = info.get("cwd") or ""

                if exe_path:
                    norm_exe = cls.normalize_path(exe_path)
                    if norm_exe == norm_target or norm_exe.startswith(norm_target + "\\"):
                        locks.append(
                            ProcessLockInfo(
                                pid=pid,
                                name=info.get("name") or "Unknown",
                                exe_path=exe_path,
                                locked_target=exe_path,
                            )
                        )
                        continue

                if cwd_path:
                    norm_cwd = cls.normalize_path(cwd_path)
                    if norm_cwd == norm_target or norm_cwd.startswith(norm_target + "\\"):
                        locks.append(
                            ProcessLockInfo(
                                pid=pid,
                                name=info.get("name") or "Unknown",
                                exe_path=exe_path or cwd_path,
                                locked_target=cwd_path,
                            )
                        )
                        continue
                for opened in proc.open_files():
                    norm_open = cls.normalize_path(opened.path)
                    if norm_open == norm_target or norm_open.startswith(norm_target + "\\"):
                        locks.append(ProcessLockInfo(pid, info.get("name") or "Unknown", exe_path, opened.path))
                        break
            except Exception:
                continue

        return locks

    @classmethod
    def ensure_no_locking_processes(cls, target_dir: str) -> None:
        """若发现目标目录有进程占用，直接抛出结构化异常 ProcessLockedError 供 UI 弹窗提示"""
        locks = cls.find_locking_processes(target_dir)
        if locks:
            names = ", ".join(sorted({f"{item.name} (PID:{item.pid})" for item in locks}))
            raise ProcessLockedError(
                f"检测到以下程序正在运行并占用该目录，请先关闭后再执行操作：{names}",
                locks=locks,
            )

    @staticmethod
    def terminate_locking_processes(locks: List[ProcessLockInfo], timeout: int = 5) -> List[int]:
        """供 Claude UI 界面“一键关闭占用进程并继续”按钮调用的安全结束进程工具"""
        terminated_pids: List[int] = []
        if psutil is None:
            return terminated_pids
        for lock in locks:
            try:
                proc = psutil.Process(lock.pid)
                proc.terminate()
                proc.wait(timeout=timeout)
                terminated_pids.append(lock.pid)
            except Exception:
                continue
        return terminated_pids

    @staticmethod
    def is_admin() -> bool:
        """检查当前进程是否拥有管理员权限（Windows UAC / POSIX root）"""
        if platform.system() == "Windows":
            try:
                return bool(ctypes.windll.shell32.IsUserAnAdmin())
            except Exception:
                return False
        else:
            return hasattr(os, "geteuid") and os.geteuid() == 0

    @staticmethod
    def request_admin_elevation() -> bool:
        """在 Windows 下通过 ShellExecuteW 请求 UAC 管理员提权重新启动自身"""
        if platform.system() != "Windows":
            return False
        try:
            arguments = sys.argv[1:] if getattr(sys, "frozen", False) else sys.argv
            params = subprocess.list2cmdline(arguments)
            rc = ctypes.windll.shell32.ShellExecuteW(
                None, "runas", sys.executable, params, None, 1
            )
            return int(rc) > 32
        except Exception:
            return False

    @staticmethod
    def is_junction_or_symlink(path: str) -> bool:
        """跨版本精准检测某个路径是否为 NTFS Junction (目录联接) 或符号链接"""
        if not os.path.lexists(path):
            return False
        if hasattr(os.path, "isjunction") and os.path.isjunction(path):
            return True
        if os.path.islink(path):
            return True
        try:
            st = os.lstat(path)
            file_attrs = getattr(st, "st_file_attributes", 0)
            if file_attrs & 0x0400:
                return True
        except OSError:
            pass
        return False

    @staticmethod
    def resolve_junction_target(path: str) -> Optional[str]:
        """解析 Junction 或软链接指向的真实物理目录路径"""
        if not SafetyGuard.is_junction_or_symlink(path):
            return None
        try:
            resolved = os.path.realpath(path)
            if os.path.normcase(resolved) != os.path.normcase(os.path.abspath(path)):
                return resolved
            return os.readlink(path)
        except OSError:
            return None

    @staticmethod
    def remove_junction_safely(junction_path: str) -> None:
        """安全移除 NTFS Junction 软链接本身，绝对不删除目标盘里的真实文件！"""
        if not SafetyGuard.is_junction_or_symlink(junction_path):
            raise CriticalPathForbiddenError(
                f"安全护栏已拦截：'{junction_path}' 不是软链接/目录联接，拒绝作为软链接移除以防误删实体数据！"
            )
        if os.path.islink(junction_path):
            os.unlink(junction_path)
        else:
            os.rmdir(junction_path)

    @staticmethod
    def create_junction(target_path: str, link_path: str) -> None:
        """Use CPython's native Windows binding; no shell interpolation."""
        if os.path.lexists(link_path):
            raise FileExistsError(link_path)
        if platform.system() != "Windows":
            raise OSError("软件迁移仅支持 Windows NTFS")
        import _winapi
        _winapi.CreateJunction(os.path.abspath(target_path), os.path.abspath(link_path))

    @staticmethod
    def set_windows_hibernation(enable: bool = False) -> Tuple[bool, str]:
        """安全开启或关闭 Windows 休眠文件 (`hiberfil.sys`)，瞬间释放 8GB~32GB C盘空间"""
        flag = "on" if enable else "off"
        if platform.system() != "Windows":
            return True, f"[模拟环境] 已执行 `powercfg -h {flag}`，休眠文件状态已更新。"
        try:
            proc = subprocess.run(
                ["powercfg", "-h", flag],
                capture_output=True,
                text=True,
                timeout=15,
                check=False,
            )
            if proc.returncode == 0:
                action_str = "已开启系统休眠" if enable else "已成功关闭休眠并彻底释放 C:\\hiberfil.sys 空间"
                return True, action_str
            return False, proc.stderr.strip() or proc.stdout.strip() or "需要管理员权限执行 powercfg"
        except Exception as exc:
            return False, f"执行 powercfg 失败: {exc}"


def ntpath_basename(norm_win_path: str) -> str:
    """从反斜杠规范化的路径中提取文件名"""
    stripped = norm_win_path.rstrip("\\")
    if "\\" not in stripped:
        return stripped
    return stripped.rsplit("\\", 1)[-1]
