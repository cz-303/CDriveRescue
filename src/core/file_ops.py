"""Strict filesystem primitives. A partial inventory is never a verified copy."""
from __future__ import annotations

import hashlib
import json
import os
import stat
import ctypes
from pathlib import Path

from .contracts import CriticalPathForbiddenError
from .safety_guard import SafetyGuard


def plain_path(value: str | Path) -> Path:
    path = Path(os.path.abspath(value))
    for parent in (*reversed(path.parents), path):
        if SafetyGuard.is_junction_or_symlink(str(parent)):
            raise CriticalPathForbiddenError(f"拒绝经过目录联接或重解析点: {parent}")
    return path


def inventory(root: Path, hashes: bool = False) -> dict:
    """Include empty directories; reject links, hard links and special files."""
    root = plain_path(root)
    if not root.is_dir():
        raise NotADirectoryError(root)
    result = {}
    stack = [root]
    while stack:
        directory = stack.pop()
        plain_path(directory)
        with os.scandir(directory) as entries:
            for entry in entries:
                path = plain_path(entry.path)
                info = path.lstat()
                relative = path.relative_to(root).as_posix()
                if stat.S_ISDIR(info.st_mode):
                    result[relative] = ("dir",)
                    stack.append(path)
                elif stat.S_ISREG(info.st_mode):
                    if info.st_nlink != 1 or getattr(info, "st_file_attributes", 0) & (0x4000 | 0x200):
                        raise ValueError(f"暂不迁移硬链接、加密或稀疏文件: {path}")
                    digest = ""
                    if hashes:
                        with path.open("rb") as stream:
                            digest = hashlib.file_digest(stream, "sha256").hexdigest()
                        after = path.stat()
                        if (info.st_size, info.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
                            raise OSError(f"校验期间文件发生变化: {path}")
                    result[relative] = ("file", info.st_size, info.st_mtime_ns, digest)
                else:
                    raise ValueError(f"不支持的文件类型: {path}")
    return result


def content_view(manifest: dict) -> dict:
    return {key: (value[0], value[1], value[3]) if value[0] == "file" else value
            for key, value in manifest.items()}


def atomic_json(path: Path, data) -> None:
    plain_path(path)
    tmp = path.with_name(path.name + ".new")
    plain_path(tmp)
    with tmp.open("w", encoding="utf-8") as stream:
        json.dump(data, stream, ensure_ascii=False, indent=2)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(tmp, path)


def copy_security(source: Path, target: Path) -> None:
    """Preserve owner/group and effective DACL, freezing target inheritance.

    Auditing/SACL is outside scope; insufficient rights abort before switching.
    """
    if os.name != "nt":
        return
    from ctypes import wintypes
    api = ctypes.WinDLL("advapi32", use_last_error=True)
    api.GetFileSecurityW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD)]
    api.SetFileSecurityW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, ctypes.c_void_p]
    needed = wintypes.DWORD()
    api.GetFileSecurityW(str(source), 7, None, 0, ctypes.byref(needed))
    if not needed.value:
        raise ctypes.WinError(ctypes.get_last_error())
    descriptor = ctypes.create_string_buffer(needed.value)
    if not api.GetFileSecurityW(str(source), 7, descriptor, needed.value, ctypes.byref(needed)):
        raise ctypes.WinError(ctypes.get_last_error())
    if not api.SetFileSecurityW(str(target), 7 | 0x80000000, descriptor):
        raise ctypes.WinError(ctypes.get_last_error())


def report(callback, percent: int, message: str, size: int) -> None:
    # A UI failure must not interrupt a committed filesystem transaction.
    if callback:
        try:
            callback(percent, message, size)
        except Exception:
            import logging
            logging.getLogger(__name__).exception("Progress callback failed")
