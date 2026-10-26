"""
CDriveRescue (C盘焦虑拯救者) - 系统安全护栏与底层权限校验引擎
===========================================================================
Responsible Model: Gemini (Chief System Architect & Safety Specialist)

Core Responsibilities:
1. Enforce an ironclad blacklist of critical Windows system directories that must
   NEVER be deleted or moved via `mklink /J` (preventing BSOD or unbootable OS).
2. Detect running processes (`psutil` with graceful stdlib/tasklist fallback) that
   have open executables or file handles inside a target directory before cleaning or moving.
3. Check and request Windows Administrator (UAC) privileges required by `mklink /J`
   and `powercfg -h off`.
4. Accurately detect NTFS Reparse Points / Directory Junctions across Python versions
   so that removing a junction never deletes the underlying target files.
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
from typing import List, Optional, Tuple

try:
    import psutil  # type: ignore
except ImportError:
    psutil = None  # Graceful fallback to stdlib shutil.disk_usage & tasklist

from src.core.contracts import (
    CriticalPathForbiddenError,
    InsufficientTargetSpaceError,
    ProcessLockInfo,
    ProcessLockedError,
    CDriveRescueError,
)


class SafetyGuard:
    """系统级安全护栏：所有清理（GPT）与搬家（GPT）操作执行前必须通过此类校验"""

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

        # 检查是否落入核心系统黑名单
        for critical in cls.CRITICAL_EXACT_OR_PREFIX_PATHS:
            if norm == critical or norm.startswith(critical + "\\"):
                raise CriticalPathForbiddenError(
                    f"安全护栏已拦截：'{target_path}' 属于 Windows 核心受保护区域 ({critical})，禁止清理！"
                )

        # 防止误删用户桌面/文档根目录本身
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

        # 拦截 C:\Users\<Username> 根目录或 AppData 根目录本身
        parts = [p for p in src_norm.split("\\") if p]
        if len(parts) >= 2 and parts[0] == "c:" and parts[1] == "users":
            # C:\Users\Alice (len 3) 或 C:\Users\Alice\AppData (len 4) 或 C:\Users\Alice\AppData\Local (len 5)
            if len(parts) <= 4:
                raise CriticalPathForbiddenError(
                    f"安全护栏已拦截：严禁直接搬移用户主目录或 AppData 顶层目录 '{source_dir}'。"
                )
            if len(parts) == 5 and parts[3] == "appdata" and parts[4] in ("local", "roaming", "locallow"):
                raise CriticalPathForbiddenError(
                    f"安全护栏已拦截：严禁整盘搬移 '{source_dir}'，请仅搬移其内部的具体应用目录（如 JetBrains、Google 等）。"
                )

        # 拦截所有核心黑名单及其子目录
        for critical in cls.CRITICAL_EXACT_OR_PREFIX_PATHS:
            if src_norm == critical or src_norm.startswith(critical + "\\"):
                raise CriticalPathForbiddenError(
                    f"安全护栏已拦截：'{source_dir}' 属于系统核心组件 ({critical})，搬家会导致系统崩溃！"
                )

        return True

    @classmethod
    def check_target_free_space(
        cls, source_size_bytes: int, target_path: str, safety_multiplier: float = 1.10
    ) -> Tuple[bool, int, int]:
        """
        校验目标磁盘剩余空间是否充足（默认要求 >= 源目录体积的 110% 预留安全缓冲）
        返回: (is_enough, target_free_bytes, required_bytes)
        """
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
        """
        扫描当前系统中所有正在运行的进程，检查是否有进程的 `.exe` 或打开的文件位于 `target_dir` 下。
        防止在微信、IDE 或浏览器未关闭时强行搬家导致文件残缺。
        """
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

                # 1. 检查进程可执行文件路径是否位于目标目录下
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

                # 2. 检查进程当前工作目录是否位于目标目录下
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
        """
        跨版本精准检测某个路径是否为 NTFS Junction (目录联接) 或符号链接。
        兼容 Python 3.8+ `os.path.isjunction` 与底层 `FILE_ATTRIBUTE_REPARSE_POINT`。
        """
        if not os.path.lexists(path):
            return False
        if hasattr(os.path, "isjunction") and os.path.isjunction(path):
            return True
        if os.path.islink(path):
            return True
        try:
            st = os.lstat(path)
            # Windows FILE_ATTRIBUTE_REPARSE_POINT = 0x0400
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
        """
        安全移除 NTFS Junction 软链接本身，绝对不删除目标盘里的真实文件！
        注意：在 Windows 下不能对 Junction 调用 `shutil.rmtree`，而必须使用 `os.rmdir` 或 `os.unlink`。
        """
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
        """
        安全开启或关闭 Windows 休眠文件 (`hiberfil.sys`)，瞬间释放 8GB~32GB C盘空间。
        注意：关闭休眠后不影响电脑正常睡眠 (Sleep)，仅关闭深度休眠 (Hibernate)。
        """
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
