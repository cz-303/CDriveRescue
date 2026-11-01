"""Windows v1.1 optimizers, with verified data transitions and explicit recovery.

Run these synchronous methods in the UI's worker thread. Hard links share future
writes and metadata: they are suitable only for attachments the user treats as
immutable. Rollback cannot promise protection against hardware failure or hostile
concurrent directory replacement. No method silently terminates applications.
"""
from __future__ import annotations

import codecs
import ctypes
import hashlib
import json
import logging
import ntpath
import os
import re
import shutil
import stat
import subprocess
import threading
import uuid
from collections import deque
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path
from typing import List, Optional, Tuple

from .contracts import (
    CriticalPathForbiddenError, DuplicateGroup, HardlinkDedupeSummary,
    ISystemOptimizer, KnownFolderItem, MigrationRollbackError, ProgressCallback,
)
from .file_ops import atomic_json, content_view, copy_security, inventory, plain_path, report
from .safety_guard import SafetyGuard

logger = logging.getLogger(__name__)
_PROCESS_LOCK = threading.Lock()
_BACKUP_SUFFIX = "._hl_bak"
_ALLOWED_FOLDERS = {
    "Downloads": "374de290-123f-4565-9164-39c4925e467b",
    "Desktop": "b4bfcc3a-db2c-424c-b029-7fe99a87c641",
    "Documents": "fdd39ad0-238f-46af-adb4-6c85480369c7",
    "Videos": "18989b1d-99b5-455b-841c-ab7c74e4ddfc",
    "Pictures": "33e28130-4e1e-4676-835a-98395c3bc3bb",
}


class _GUID(ctypes.Structure):
    _fields_ = [("Data1", ctypes.c_uint32), ("Data2", ctypes.c_uint16),
                ("Data3", ctypes.c_uint16), ("Data4", ctypes.c_ubyte * 8)]

    @classmethod
    def parse(cls, value: str):
        return cls.from_buffer_copy(uuid.UUID(value).bytes_le)


def _require_windows():
    if os.name != "nt":
        raise OSError("此操作仅支持 Windows")


def _identity(info):
    return info.st_dev, info.st_ino


def _signature(info):
    return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns


def _path(value) -> Path:
    """Do not rely on the v1.1 guard's string-only path normalization."""
    raw = os.fspath(value)
    if not raw or not os.path.isabs(raw) or (os.name == "nt" and not ntpath.splitdrive(raw)[0]):
        raise CriticalPathForbiddenError("必须提供绝对路径")
    if raw.startswith(("\\\\", "//")) or ":" in ntpath.splitdrive(raw)[1]:
        raise CriticalPathForbiddenError("不支持网络、设备命名空间或备用数据流路径")
    if any(p not in (".", "..") and p.endswith((" ", "."))
           for p in raw.replace("/", "\\").split("\\") if p):
        raise CriticalPathForbiddenError("不支持末尾空格或句点的路径")
    result = plain_path(os.path.normpath(raw))
    if result == Path(result.anchor):
        raise CriticalPathForbiddenError("禁止操作磁盘根目录")
    norm = os.path.normcase(str(result))
    protected = list(SafetyGuard.CRITICAL_EXACT_OR_PREFIX_PATHS)
    protected.extend(os.environ[k] for k in ("SystemRoot", "ProgramFiles", "ProgramFiles(x86)") if k in os.environ)
    for entry in protected:
        base = os.path.normcase(os.path.abspath(entry))
        if norm == base or norm.startswith(base + os.sep):
            raise CriticalPathForbiddenError(f"禁止操作系统或程序目录: {result}")
    if result == Path.home():
        raise CriticalPathForbiddenError("禁止操作用户主目录")
    return result


def _ntfs(path: Path):
    from ctypes import wintypes
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.GetVolumePathNameW.argtypes = [wintypes.LPCWSTR, wintypes.LPWSTR, wintypes.DWORD]
    kernel.GetVolumeInformationW.argtypes = [wintypes.LPCWSTR, wintypes.LPWSTR, wintypes.DWORD,
                                            ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p,
                                            wintypes.LPWSTR, wintypes.DWORD]
    probe = path
    while not probe.exists() and probe != probe.parent:
        probe = probe.parent
    volume, fs = ctypes.create_unicode_buffer(32768), ctypes.create_unicode_buffer(64)
    if not kernel.GetVolumePathNameW(str(probe), volume, len(volume)):
        raise ctypes.WinError(ctypes.get_last_error())
    if not kernel.GetVolumeInformationW(volume.value, None, 0, None, None, None, fs, len(fs)):
        raise ctypes.WinError(ctypes.get_last_error())
    if fs.value.upper() != "NTFS":
        raise ValueError(f"仅支持 NTFS: {path}")


def _security(path: Path) -> bytes:
    from ctypes import wintypes
    api = ctypes.WinDLL("advapi32", use_last_error=True)
    api.GetFileSecurityW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, ctypes.c_void_p,
                                   wintypes.DWORD, ctypes.POINTER(wintypes.DWORD)]
    size = wintypes.DWORD()
    api.GetFileSecurityW(str(path), 7, None, 0, ctypes.byref(size))
    if not size.value:
        raise ctypes.WinError(ctypes.get_last_error())
    buffer = ctypes.create_string_buffer(size.value)
    if not api.GetFileSecurityW(str(path), 7, buffer, size.value, ctypes.byref(size)):
        raise ctypes.WinError(ctypes.get_last_error())
    return buffer.raw


def _no_streams(path: Path):
    """Reject alternate streams rather than discarding Zone.Identifier or user data."""
    from ctypes import wintypes

    class StreamData(ctypes.Structure):
        _fields_ = [("size", ctypes.c_longlong), ("name", wintypes.WCHAR * 296)]

    api = ctypes.WinDLL("kernel32", use_last_error=True)
    api.FindFirstStreamW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, ctypes.POINTER(StreamData), wintypes.DWORD]
    api.FindFirstStreamW.restype = wintypes.HANDLE
    api.FindNextStreamW.argtypes = [wintypes.HANDLE, ctypes.POINTER(StreamData)]
    api.FindClose.argtypes = [wintypes.HANDLE]
    data = StreamData()
    handle = api.FindFirstStreamW(str(path), 0, ctypes.byref(data), 0)
    if handle == ctypes.c_void_p(-1).value:
        error = ctypes.get_last_error()
        if error == 38:
            return
        raise ctypes.WinError(error)
    try:
        while True:
            if data.name != "::$DATA":
                raise ValueError(f"含备用数据流，保留原文件: {path}")
            if not api.FindNextStreamW(handle, ctypes.byref(data)):
                if ctypes.get_last_error() != 38:
                    raise ctypes.WinError(ctypes.get_last_error())
                break
    finally:
        api.FindClose(handle)


@contextmanager
def _read_locked(path: Path):
    """Deny content writes while allowing the rename/unlink needed by the transaction."""
    import msvcrt
    from ctypes import wintypes
    api = ctypes.WinDLL("kernel32", use_last_error=True)
    api.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                               ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
    api.CreateFileW.restype = wintypes.HANDLE
    api.CloseHandle.argtypes = [wintypes.HANDLE]
    handle = api.CreateFileW(str(path), 0x80000000, 1 | 4, None, 3, 0x00200000, None)
    if handle == ctypes.c_void_p(-1).value:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        fd = msvcrt.open_osfhandle(handle, os.O_RDONLY | os.O_BINARY)
    except BaseException:
        api.CloseHandle(handle)
        raise
    with os.fdopen(fd, "rb") as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or not info.st_ino:
            raise ValueError(f"非普通文件或无法取得文件标识: {path}")
        yield stream


def _digest(stream):
    stream.seek(0)
    return hashlib.file_digest(stream, "sha256").hexdigest()


class SystemOptimizer(ISystemOptimizer):
    def __init__(self, state_dir: Optional[str] = None):
        self.state_dir = Path(state_dir or os.environ.get("CDR_STATE_DIR", str(Path.home() / ".cdriverescue"))).absolute()
        self.journal_path = self.state_dir / "known_folder_transaction.json"
        self.last_warnings: List[str] = []

    @contextmanager
    def _operation(self):
        _require_windows()
        if not _PROCESS_LOCK.acquire(blocking=False):
            raise RuntimeError("已有系统优化操作正在运行")
        api, mutex, acquired = None, None, False
        try:
            from ctypes import wintypes
            api = ctypes.WinDLL("kernel32", use_last_error=True)
            api.CreateMutexW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR]
            api.CreateMutexW.restype = wintypes.HANDLE
            api.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
            api.ReleaseMutex.argtypes = [wintypes.HANDLE]
            api.CloseHandle.argtypes = [wintypes.HANDLE]
            key = hashlib.sha256(str(Path.home()).casefold().encode()).hexdigest()[:24]
            mutex = api.CreateMutexW(None, False, "Local\\CDriveRescue.SystemOptimizer." + key)
            if not mutex:
                raise ctypes.WinError(ctypes.get_last_error())
            outcome = api.WaitForSingleObject(mutex, 0)
            acquired = outcome in (0, 0x80)
            if not acquired:
                raise RuntimeError("另一程序实例正在执行系统优化")
            self.last_warnings = []
            yield
        finally:
            if acquired:
                api.ReleaseMutex(mutex)
            if mutex:
                api.CloseHandle(mutex)
            _PROCESS_LOCK.release()

    def deduplicate_via_hardlink(
        self, groups: List[DuplicateGroup], dry_run: bool = False,
        progress_cb: Optional[ProgressCallback] = None,
    ) -> HardlinkDedupeSummary:
        """In dry-run, linked_files_count/saved_bytes are *projections*, not changes."""
        summary = HardlinkDedupeSummary(0, 0, 0, 0)
        with self._operation():
            total = sum(len(group.duplicate_paths) for group in groups)
            done, seen = 0, set()
            for group in groups:
                summary.processed_groups += 1
                for dup_name in group.duplicate_paths:
                    done += 1
                    try:
                        # This guard is always the first per-pair validation.
                        if not SafetyGuard.validate_for_hardlink(group.master_path, dup_name):
                            continue
                        master, duplicate = _path(group.master_path), _path(dup_name)
                        token = os.path.normcase(str(duplicate))
                        if token in seen:
                            continue
                        seen.add(token)
                        if master.stat().st_dev != duplicate.stat().st_dev:
                            summary.skipped_cross_volume += 1
                            continue
                        _ntfs(master)
                        if duplicate.suffix.lower() in SafetyGuard.FORBIDDEN_HARDLINK_EXTENSIONS:
                            raise CriticalPathForbiddenError("副本是动态数据库或配置文件，拒绝合并")
                        saved = self._link_one(master, duplicate, dry_run)
                        summary.linked_files_count += 1
                        summary.saved_bytes += saved
                    except Exception as exc:
                        # The supplied guard raises before returning for cross-drive pairs.
                        try:
                            cross = os.stat(group.master_path).st_dev != os.stat(dup_name).st_dev
                        except OSError:
                            cross = False
                        if cross:
                            summary.skipped_cross_volume += 1
                        summary.errors.append(f"{dup_name}: {exc}")
                    finally:
                        report(progress_cb, int(done * 100 / max(1, total)),
                               ("预演: " if dry_run else "去重: ") + str(dup_name), summary.saved_bytes)
            report(progress_cb, 100, "预演完成（未修改文件）" if dry_run else "去重完成，请检查跳过及错误项", summary.saved_bytes)
        return summary

    @staticmethod
    def _link_one(master: Path, duplicate: Path, dry_run: bool) -> int:
        backup = duplicate.with_name(duplicate.name + _BACKUP_SUFFIX)
        if os.path.lexists(backup):
            raise FileExistsError(f"存在上次备份，拒绝覆盖: {backup}")
        with _read_locked(master) as master_stream, _read_locked(duplicate) as dup_stream:
            m, d = os.fstat(master_stream.fileno()), os.fstat(dup_stream.fileno())
            if _identity(m) == _identity(d):
                raise ValueError("文件已共享同一实体，请重新扫描")
            if d.st_nlink != 1:
                raise ValueError("副本已有其他硬链接，跳过以免误报释放空间")
            for path, info in ((master, m), (duplicate, d)):
                if getattr(info, "st_file_attributes", 0) & (1 | 0x200 | 0x400 | 0x800 | 0x1000 | 0x4000):
                    raise ValueError(f"只读、稀疏、压缩、离线、加密或重解析文件暂不支持: {path}")
                _no_streams(path)
            if m.st_size != d.st_size or _digest(master_stream) != _digest(dup_stream):
                raise ValueError("全文 SHA-256 或大小不同，拒绝去重")
            if _security(master) != _security(duplicate):
                raise ValueError("文件所有者或访问权限不同，拒绝合并")
            if dry_run:
                return d.st_size
            renamed, linked = False, False
            try:
                if _signature(master.stat()) != _signature(m) or _signature(duplicate.stat()) != _signature(d):
                    raise OSError("文件在校验后发生变化")
                os.rename(duplicate, backup)  # On Windows this never overwrites backup.
                renamed = True
                os.link(master, duplicate)
                linked = True
                if (not os.path.samefile(master, duplicate) or
                    _identity(duplicate.stat()) != _identity(m) or duplicate.stat().st_size != d.st_size or
                    _identity(backup.stat()) != _identity(d)):
                    raise OSError("新硬链接或备份校验失败")
                if _digest(master_stream) != _digest(dup_stream):
                    raise OSError("切换后内容校验失败")
                os.unlink(backup)
                return d.st_size
            except BaseException as exc:
                try:
                    if renamed:
                        if _identity(_path(backup).stat()) != _identity(d):
                            raise OSError("备份标识改变，保留文件供人工检查")
                        if os.path.lexists(duplicate):
                            if not linked or _identity(_path(duplicate).stat()) != _identity(m):
                                raise OSError("原路径被其他文件占用，拒绝覆盖")
                            os.unlink(duplicate)
                        os.rename(backup, duplicate)
                except Exception as rollback:
                    raise MigrationRollbackError(f"去重失败: {exc}; 回滚未完成: {rollback}; 原件备份: {backup}", False) from exc
                raise

    @staticmethod
    def _get_known_path(guid: str) -> str:
        _require_windows()
        shell, ole = ctypes.windll.shell32, ctypes.windll.ole32
        shell.SHGetKnownFolderPath.argtypes = [ctypes.POINTER(_GUID), ctypes.c_uint32, ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p)]
        shell.SHGetKnownFolderPath.restype = ctypes.c_int32
        ole.CoTaskMemFree.argtypes = [ctypes.c_void_p]
        output = ctypes.c_void_p()
        try:
            hr = shell.SHGetKnownFolderPath(ctypes.byref(_GUID.parse(guid)), 0x4000, None, ctypes.byref(output))
            if hr < 0:
                raise OSError(f"SHGetKnownFolderPath 失败: HRESULT 0x{hr & 0xffffffff:08X}")
            if not output.value:
                raise OSError("Windows 返回空文件夹路径")
            return ctypes.wstring_at(output)
        finally:
            if output.value:
                ole.CoTaskMemFree(output)

    @staticmethod
    def _set_known_path(guid: str, path: Path):
        shell = ctypes.windll.shell32
        shell.SHSetKnownFolderPath.argtypes = [ctypes.POINTER(_GUID), ctypes.c_uint32, ctypes.c_void_p, ctypes.c_wchar_p]
        shell.SHSetKnownFolderPath.restype = ctypes.c_int32
        hr = shell.SHSetKnownFolderPath(ctypes.byref(_GUID.parse(guid)), 0, None, str(path))
        if hr < 0:
            # No registry bypass: failures may be caused by policy or OneDrive management.
            raise OSError(f"SHSetKnownFolderPath 失败: HRESULT 0x{hr & 0xffffffff:08X}")

    def _folder_paths(self, item, target_path):
        expected = _ALLOWED_FOLDERS.get(item.folder_key)
        if expected is None or uuid.UUID(item.folder_guid) != uuid.UUID(expected):
            raise ValueError("文件夹名称与受支持的 KNOWNFOLDERID 不一致")
        source, target = _path(self._get_known_path(expected)), _path(target_path)
        if source != _path(item.current_path):
            raise ValueError(f"文件夹位置已改变，请重新扫描；Windows 当前路径为 {source}")
        if source == target or source.is_relative_to(target) or target.is_relative_to(source):
            raise ValueError("源目标不能相同或互相包含")
        for key, guid in _ALLOWED_FOLDERS.items():
            if key == item.folder_key:
                continue
            other = _path(self._get_known_path(guid))
            if target == other or target.is_relative_to(other) or other.is_relative_to(target):
                raise ValueError(f"目标与其他个人文件夹 {key} 重叠")
            if source == other or other.is_relative_to(source):
                raise ValueError("源目录包含其他已注册个人文件夹，拒绝迁移")
        for name in ("OneDrive", "OneDriveConsumer", "OneDriveCommercial", "APPDATA", "LOCALAPPDATA"):
            if os.environ.get(name):
                special = Path(os.path.abspath(os.environ[name]))
                if any(p == special or p.is_relative_to(special) or special.is_relative_to(p) for p in (source, target)):
                    raise ValueError(f"目录与 {name} 管理区域重叠，请使用对应的官方设置")
        if not source.is_dir():
            raise NotADirectoryError(source)
        if target.exists() and (not target.is_dir() or any(target.iterdir())):
            raise FileExistsError("目标必须不存在或为空目录，不自动合并或覆盖文件")
        if not target.parent.is_dir():
            raise FileNotFoundError("请先创建目标的父目录")
        if any(self.state_dir == p or self.state_dir.is_relative_to(p) for p in (source, target)):
            raise ValueError("事务记录目录不能位于迁移范围内")
        _ntfs(source)
        _ntfs(target)
        return source, target

    @staticmethod
    def _manifest(root):
        result = inventory(root, hashes=True)
        for path in [root, *(root / name for name in result)]:
            _no_streams(path)
        return result

    @staticmethod
    def _copy_files(source, target, manifest, callback):
        total = sum(v[1] for v in manifest.values() if v[0] == "file")
        copied = 0
        for name, value in manifest.items():
            src, dst = _path(source / name), _path(target / name)
            if value[0] == "dir":
                dst.mkdir()  # manifest traversal visits parents before children
            else:
                with _read_locked(src) as reader, dst.open("xb") as writer:
                    while chunk := reader.read(1024 * 1024):
                        writer.write(chunk)
                        copied += len(chunk)
                        report(callback, min(75, 5 + int(70 * copied / max(1, total))),
                               f"复制 {name}", copied)
                    writer.flush()
                    os.fsync(writer.fileno())
                shutil.copystat(src, dst, follow_symlinks=False)
        for name in sorted(manifest, key=lambda n: len(Path(n).parts), reverse=True):
            copy_security(source / name, target / name)
        copy_security(source, target)

    def relocate_known_folder(
        self, item: KnownFolderItem, target_path: str, move_existing_files: bool = True,
        progress_cb: Optional[ProgressCallback] = None,
    ) -> KnownFolderItem:
        with self._operation():
            _path(self.state_dir)
            self.state_dir.mkdir(parents=True, exist_ok=True)
            if self.journal_path.exists():
                raise RuntimeError(f"存在未完成事务，请先调用 recover_known_folder_transaction(): {self.journal_path}")
            source, target = self._folder_paths(item, target_path)
            # Required contract check, followed by a fresh size check when copying.
            SafetyGuard.check_target_free_space(item.size_bytes, str(target))
            manifest = self._manifest(source) if move_existing_files else {}
            size = sum(v[1] for v in manifest.values() if v[0] == "file")
            SafetyGuard.check_target_free_space(size, str(target))
            backup = source.with_name(source.name + "._kf_bak_" + uuid.uuid4().hex)
            tx = {"version": 1, "guid": item.folder_guid, "folder_key": item.folder_key,
                  "source": str(source), "target": str(target), "backup": str(backup),
                  "move_files": move_existing_files, "phase": "copying"}
            atomic_json(self.journal_path, tx)
            switched = False
            try:
                target.mkdir(exist_ok=True)
                if any(target.iterdir()):
                    raise FileExistsError("目标目录在预检后出现文件，停止操作")
                if move_existing_files:
                    self._copy_files(source, target, manifest, progress_cb)
                    if self._manifest(source) != manifest or content_view(self._manifest(target)) != content_view(manifest):
                        raise OSError("复制校验失败或源目录在复制期间变化")
                    tx["phase"] = "switching"
                    atomic_json(self.journal_path, tx)
                    os.rename(source, backup)
                    if self._manifest(backup) != manifest:
                        raise OSError("源目录在切换时发生变化")
                report(progress_cb, 85, "更新 Windows 个人文件夹位置", size)
                self._set_known_path(item.folder_guid, target)
                if _path(self._get_known_path(item.folder_guid)) != target:
                    raise OSError("Windows 未确认新的文件夹位置")
                tx["phase"] = "committed"
                atomic_json(self.journal_path, tx)
                switched = True
                # Once committed, never roll back after any original has been deleted.
                if move_existing_files:
                    if os.path.lexists(source):
                        raise OSError(f"原路径被其他程序重新创建，保留备份以便检查: {source}")
                    self._cleanup_known_backup(backup, target, manifest)
                self.journal_path.unlink()
            except BaseException as exc:
                if switched:
                    warning = f"文件夹已迁移，备份或事务日志待处理: {exc}; {backup}"
                    self.last_warnings.append(warning)
                    logger.warning(warning)
                else:
                    ok, detail = self._rollback_folder(tx)
                    if not isinstance(exc, Exception) and ok:
                        raise
                    raise MigrationRollbackError(f"个人文件夹迁移失败: {exc}; {detail}", ok) from exc
            report(progress_cb, 100, self.last_warnings[-1] if self.last_warnings else "个人文件夹迁移完成", size)
            return replace(item, current_path=str(target), size_bytes=size,
                           file_count=sum(v[0] == "file" for v in manifest.values()),
                           is_on_system_drive=target.drive.casefold() == os.environ.get("SystemDrive", "C:").casefold())

    @staticmethod
    def _cleanup_known_backup(backup, target, manifest):
        backup, target = _path(backup), _path(target)
        if "._kf_bak_" not in backup.name or backup == target:
            raise ValueError("非法备份目录")
        # Per-file validation allows safe retries after an interrupted cleanup.
        for name, value in manifest.items():
            if value[0] != "file":
                continue
            old, new = _path(backup / name), _path(target / name)
            if not old.exists():
                continue
            with _read_locked(old) as old_stream, _read_locked(new) as new_stream:
                _no_streams(old)
                _no_streams(new)
                if _digest(old_stream) != value[3] or _digest(new_stream) != value[3]:
                    raise OSError(f"文件在提交后变化，保留备份: {old}")
                if _identity(old.stat()) != _identity(os.fstat(old_stream.fileno())):
                    raise OSError("备份路径已被替换")
                old.unlink()
        for name, value in sorted(manifest.items(), key=lambda pair: len(Path(pair[0]).parts), reverse=True):
            if value[0] == "dir":
                _path(backup / name).rmdir()
        backup.rmdir()  # Nonempty directories/new files are never recursively deleted.

    def _rollback_folder(self, tx):
        source, target, backup = (Path(tx[k]) for k in ("source", "target", "backup"))
        try:
            current = _path(self._get_known_path(tx["guid"]))
            if current not in (source, target):
                raise OSError("Windows 文件夹位置被第三方改变，停止自动回滚")
            if backup.exists():
                _path(backup)
                if os.path.lexists(source):
                    raise FileExistsError(f"原路径出现新内容，保留全部副本: {source}")
                os.rename(backup, source)
            if not source.is_dir():
                raise OSError("原目录不存在，保留事务日志")
            if current == target:
                self._set_known_path(tx["guid"], source)
                if _path(self._get_known_path(tx["guid"])) != source:
                    raise OSError("Windows 文件夹位置回滚验证失败")
            self.journal_path.unlink(missing_ok=True)
            return True, f"原位置已恢复；目标副本保留在 {target}"
        except Exception as exc:
            return False, f"需要人工检查，全部剩余副本及日志保留: {exc}"

    def recover_known_folder_transaction(self) -> str:
        """Explicit recovery; do not discard post-crash target files or partial backups."""
        with self._operation():
            if not self.journal_path.exists():
                return "没有待恢复的个人文件夹事务"
            tx = json.loads(_path(self.journal_path).read_text(encoding="utf-8"))
            if tx.get("version") != 1 or tx.get("folder_key") not in _ALLOWED_FOLDERS:
                raise ValueError("无法识别事务日志")
            if uuid.UUID(tx["guid"]) != uuid.UUID(_ALLOWED_FOLDERS[tx["folder_key"]]):
                raise ValueError("事务 GUID 与文件夹不匹配")
            source, target, backup = (_path(tx[k]) for k in ("source", "target", "backup"))
            if (backup.parent != source.parent or not backup.name.startswith(source.name + "._kf_bak_") or
                source == target or target.is_relative_to(source) or source.is_relative_to(target)):
                raise ValueError("事务路径关系不合法")
            current = _path(self._get_known_path(tx["guid"]))
            if tx["phase"] == "committed" or current == target:
                if current != target or not target.is_dir():
                    raise MigrationRollbackError("已提交事务状态不一致，需要人工检查", False)
                self.journal_path.unlink()
                return f"迁移已生效；未删除残余备份 {backup}，请确认内容后自行处理"
            ok, detail = self._rollback_folder(tx)
            if not ok:
                raise MigrationRollbackError(detail, False)
            return detail

    @staticmethod
    def _dism_executable() -> Path:
        # Query the OS rather than trusting PATH/current directory.
        api = ctypes.windll.kernel32
        api.GetWindowsDirectoryW.argtypes = [ctypes.c_wchar_p, ctypes.c_uint32]
        buffer = ctypes.create_unicode_buffer(32768)
        length = api.GetWindowsDirectoryW(buffer, len(buffer))
        if not 0 < length < len(buffer):
            raise OSError("无法确定 Windows 系统目录")
        windows = Path(buffer.value)
        folder = "Sysnative" if ctypes.sizeof(ctypes.c_void_p) == 4 and (windows / "Sysnative").exists() else "System32"
        executable = windows / folder / "Dism.exe"
        if not executable.is_file():
            raise FileNotFoundError(executable)
        return executable

    @staticmethod
    def _dism_lines(stream):
        # DISM commonly uses OEM text; handle BOM-tagged UTF-16/UTF-8 as well.
        prefix = stream.read(2)
        if not prefix:
            return
        if prefix in (b"\xff\xfe", b"\xfe\xff"):
            encoding = "utf-16"
        elif b"\x00" in prefix:
            encoding = "utf-16-le"
        elif prefix == b"\xef\xbb":
            encoding = "utf-8-sig"
        else:
            encoding = f"cp{ctypes.windll.kernel32.GetOEMCP()}"
        decoder = codecs.getincrementaldecoder(encoding)(errors="replace")
        line = ""
        data = prefix
        while data:
            for char in decoder.decode(data):
                if char in "\r\n":
                    if line:
                        yield line
                        line = ""
                else:
                    line = (line + char)[-8192:]
            data = stream.read(1)
        line += decoder.decode(b"", final=True)
        if line:
            yield line

    def slim_winsxs_component_store(
        self, progress_cb: Optional[ProgressCallback] = None,
    ) -> Tuple[bool, str]:
        try:
            with self._operation():
                if not SafetyGuard.is_admin():
                    return False, "DISM 组件清理需要管理员权限，请以管理员身份运行软件"
                executable = self._dism_executable()
                command = [str(executable), "/Online", "/Cleanup-Image", "/StartComponentCleanup", "/NoRestart"]
                report(progress_cb, 0, "正在启动 Windows 组件清理", 0)
                tail, percent = deque(maxlen=30), 0
                with subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                      stdin=subprocess.DEVNULL, shell=False, bufsize=0,
                                      creationflags=subprocess.CREATE_NO_WINDOW) as process:
                    assert process.stdout is not None
                    for line in self._dism_lines(process.stdout):
                        tail.append(line)
                        matches = re.findall(r"(?<![\d.])(\d{1,3}(?:[.,]\d+)?)\s*%", line)
                        if matches:
                            value = float(matches[-1].replace(",", "."))
                            if 0 <= value <= 100:
                                percent = max(percent, min(99, int(value)))
                        report(progress_cb, percent, line, 0)
                    code = process.wait()
                details = "\n".join(tail)
                if code in (0, 3010):
                    message = "Windows 组件清理完成" + ("，需要重启才能完成剩余操作" if code == 3010 else "")
                    report(progress_cb, 100, message, 0)
                    return True, message + ("\n" + details if details else "")
                return False, f"DISM 清理失败，退出码 {code}；请查看 Windows\\Logs\\DISM\\dism.log\n{details}"
        except Exception as exc:
            return False, f"未能完成 DISM 组件清理: {exc}"
