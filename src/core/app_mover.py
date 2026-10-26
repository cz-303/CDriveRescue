"""Journaled copy/verify/switch migration. Not an OS-atomic cross-volume move.

Ordinary, closed application folders only. Recovery retains all ambiguous data.
Restore intentionally retains the destination copy until the user verifies the app.
"""
from __future__ import annotations

import ctypes
import json
import os
import shutil
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from threading import RLock

from .contracts import IAppMover, MigrationRecord, MigrationRollbackError, MigrationStatus
from .file_ops import atomic_json, content_view, copy_security, inventory, plain_path, report
from .safety_guard import SafetyGuard


class AppMover(IAppMover):
    def __init__(self, state_dir=None):
        self.state_dir = Path(state_dir or os.environ.get("CDR_STATE_DIR", str(Path.home() / ".cdriverescue")))
        self._lock = RLock()
        self.history_path = self.state_dir / "migration_history.json"
        self.journal_path = self.state_dir / "pending_transaction.json"

    @contextmanager
    def _exclusive(self):
        with self._lock:
            plain_path(self.state_dir)
            self.state_dir.mkdir(parents=True, exist_ok=True)
            lock_path = plain_path(self.state_dir / "operation.lock")
            with lock_path.open("a+b") as stream:
                stream.seek(0, 2)
                if stream.tell() == 0:
                    stream.write(b"0")
                    stream.flush()
                stream.seek(0)
                if os.name == "nt":
                    import msvcrt
                    msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
                try:
                    yield
                finally:
                    stream.seek(0)
                    if os.name == "nt":
                        msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
                    else:
                        fcntl.flock(stream, fcntl.LOCK_UN)

    def _read_records(self):
        plain_path(self.history_path)
        if not self.history_path.exists():
            return []
        values = json.loads(self.history_path.read_text(encoding="utf-8"))
        if not isinstance(values, list):
            raise ValueError("迁移记录损坏，拒绝覆盖")
        records = [MigrationRecord.from_dict(value) for value in values]
        if len({r.record_id for r in records}) != len(records):
            raise ValueError("迁移记录 ID 重复")
        return records

    def _save_record(self, record):
        records = self._read_records()
        records = [r for r in records if r.record_id != record.record_id] + [record]
        atomic_json(self.history_path, [r.to_dict() for r in records])

    def _ensure_ready(self):
        self._read_records()
        if self.journal_path.exists():
            raise RuntimeError("存在未完成事务，请先调用 recover_pending() 检查并恢复")

    @staticmethod
    def _ntfs(path):
        if os.name != "nt":
            raise OSError("迁移仅支持 Windows NTFS 本地磁盘")
        anchor = str(Path(path).anchor)
        fs = ctypes.create_unicode_buffer(32)
        if not ctypes.windll.kernel32.GetVolumeInformationW(anchor, None, 0, None, None, None, fs, 32):
            raise ctypes.WinError()
        if fs.value.upper() != "NTFS" or ctypes.windll.kernel32.GetDriveTypeW(anchor) != 3:
            raise ValueError("仅支持 NTFS 固定磁盘，不支持网络或可移动磁盘")

    @staticmethod
    def _check_streams(root, manifest):
        """copy2 cannot preserve arbitrary NTFS streams; reject them explicitly."""
        if os.name != "nt":
            return
        from ctypes import wintypes

        class StreamData(ctypes.Structure):
            _fields_ = [("size", ctypes.c_longlong), ("name", wintypes.WCHAR * 296)]

        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.FindFirstStreamW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, ctypes.POINTER(StreamData), wintypes.DWORD]
        kernel.FindFirstStreamW.restype = wintypes.HANDLE
        kernel.FindNextStreamW.argtypes = [wintypes.HANDLE, ctypes.POINTER(StreamData)]
        kernel.FindClose.argtypes = [wintypes.HANDLE]
        for relative in [None, *manifest]:
            path = root if relative is None else root / relative
            data = StreamData()
            handle = kernel.FindFirstStreamW(str(path), 0, ctypes.byref(data), 0)
            if handle == ctypes.c_void_p(-1).value:
                error = ctypes.get_last_error()
                if error == 38:  # No streams (e.g. empty directories).
                    continue
                raise ctypes.WinError(error)
            try:
                while True:
                    if data.name != "::$DATA":
                        raise ValueError(f"暂不支持含备用数据流的目录: {path}")
                    if not kernel.FindNextStreamW(handle, ctypes.byref(data)):
                        if ctypes.get_last_error() != 38:
                            raise ctypes.WinError(ctypes.get_last_error())
                        break
            finally:
                kernel.FindClose(handle)

    @staticmethod
    def _matches_link(source, target):
        return (SafetyGuard.is_junction_or_symlink(str(source)) and
                os.path.normcase(os.path.realpath(source)) == os.path.normcase(str(target.resolve())))

    def _copy_verify(self, source, target, callback):
        before = inventory(source, hashes=True)
        self._check_streams(source, before)
        total = sum(v[1] for v in before.values() if v[0] == "file")
        SafetyGuard.check_target_free_space(total, str(target.parent))
        plain_path(target)
        target.mkdir()  # Never merge or overwrite an existing destination.
        copied = 0
        for relative, value in before.items():
            src, dst = plain_path(source / relative), plain_path(target / relative)
            if value[0] == "dir":
                dst.mkdir(parents=True, exist_ok=True)
            else:
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src, dst)
                copied += value[1]
                report(callback, 10 + int(60 * copied / max(1, total)), f"复制 {relative}", copied)
        for relative in sorted(before, key=lambda p: len(Path(p).parts), reverse=True):
            copy_security(source / relative, target / relative)
        copy_security(source, target)
        after = inventory(source, hashes=True)
        self._check_streams(source, after)
        if before != after or content_view(before) != content_view(inventory(target, hashes=True)):
            raise OSError("源文件发生变化或 SHA-256 校验不一致，拒绝切换目录")
        return before, total

    def move_app_atomic(self, task, target_root_dir, progress_cb=None):
        with self._exclusive():
            self._ensure_ready()
            source = plain_path(task.source_path)
            root = plain_path(target_root_dir)
            target = root / source.name
            backup = source.with_name(source.name + "._cdr_backup_" + uuid.uuid4().hex)
            SafetyGuard.validate_for_migration(str(source), str(target))
            self._ntfs(source)
            self._ntfs(root)
            if not source.is_dir():
                raise NotADirectoryError(source)
            if os.path.lexists(target):
                raise FileExistsError(target)
            # Logs must not live inside either side of the migration.
            for path in (source, target):
                if self.state_dir.resolve().is_relative_to(path):
                    raise ValueError("事务记录不能位于被迁移的目录内")
            SafetyGuard.ensure_no_locking_processes(str(source))
            root.mkdir(parents=True, exist_ok=True)
            transaction = {"kind": "move", "source": str(source), "target": str(target),
                           "backup": str(backup), "phase": "copying", "id": uuid.uuid4().hex}
            atomic_json(self.journal_path, transaction)
            committed = False
            try:
                manifest, size = self._copy_verify(source, target, progress_cb)
                SafetyGuard.ensure_no_locking_processes(str(source))
                transaction["phase"] = "switching"
                atomic_json(self.journal_path, transaction)
                source.rename(backup)
                if inventory(backup, hashes=True) != manifest:
                    raise OSError("目录切换前文件发生变化")
                SafetyGuard.create_junction(str(target), str(source))
                if not self._matches_link(source, target):
                    raise OSError("目录联接校验失败")
                record = MigrationRecord(transaction["id"], task.app_name, str(source), str(target),
                                         size, sum(v[0] == "file" for v in manifest.values()),
                                         datetime.now(timezone.utc).isoformat())
                # Persist history before deleting any original data.
                self._save_record(record)
                committed = True
                transaction["phase"] = "committed"
                atomic_json(self.journal_path, transaction)
                self._cleanup_backup(backup, target)
                self.journal_path.unlink()
                report(progress_cb, 100, "迁移完成", size)
                return record
            except Exception as exc:
                if committed:
                    record.error_message = f"迁移已完成，备份或事务日志待处理: {exc}"
                    return record
                clean = self._rollback_move(transaction)
                raise MigrationRollbackError(f"迁移未完成: {exc}; 目标副本保留以便检查", clean) from exc

    @staticmethod
    def _cleanup_backup(backup, target):
        if not backup.exists():
            return
        plain_path(backup)
        if not backup.is_absolute() or "._cdr_backup_" not in backup.name or backup == target:
            raise ValueError("非法备份清理路径")
        # No deletion if either side changed after the switch.
        if content_view(inventory(backup, True)) != content_view(inventory(target, True)):
            raise OSError("备份和目标不再一致，保留备份")
        shutil.rmtree(backup)

    def _rollback_move(self, transaction):
        source, target, backup = (Path(transaction[k]) for k in ("source", "target", "backup"))
        try:
            if backup.exists():
                plain_path(backup)
                if os.path.lexists(source):
                    if not self._matches_link(source, target):
                        return False
                    # Preserve changes made through the junction before rollback.
                    if content_view(inventory(backup, True)) != content_view(inventory(target, True)):
                        return False
                    SafetyGuard.remove_junction_safely(str(source))
                backup.rename(source)
            elif not source.is_dir() or SafetyGuard.is_junction_or_symlink(str(source)):
                return False
            self.journal_path.unlink(missing_ok=True)
            return True
        except Exception:
            return False

    def restore_app(self, record_id, progress_cb=None):
        with self._exclusive():
            self._ensure_ready()
            record = next((r for r in self._read_records() if r.record_id == record_id), None)
            if record is None:
                raise KeyError(record_id)
            if record.status != MigrationStatus.MOVED:
                raise ValueError("仅允许还原已迁移记录")
            source, target = Path(record.source_path), plain_path(record.target_path)
            plain_path(source.parent)
            if not self._matches_link(source, target) or not target.is_dir():
                raise ValueError("原位置联接失效或指向已改变，拒绝覆盖")
            stage = source.with_name(source.name + "._cdr_restore_" + uuid.uuid4().hex)
            saved_link = source.with_name(source.name + "._cdr_link_" + uuid.uuid4().hex)
            SafetyGuard.validate_for_migration(str(target), str(stage))
            SafetyGuard.ensure_no_locking_processes(str(target))
            SafetyGuard.ensure_no_locking_processes(str(source))
            transaction = {"kind": "restore", "id": record_id, "source": str(source), "target": str(target),
                           "stage": str(stage), "saved_link": str(saved_link), "phase": "copying"}
            atomic_json(self.journal_path, transaction)
            committed = False
            try:
                manifest, size = self._copy_verify(target, stage, progress_cb)
                SafetyGuard.ensure_no_locking_processes(str(target))
                if not self._matches_link(source, target):
                    raise OSError("原目录联接发生变化")
                transaction["phase"] = "switching"
                atomic_json(self.journal_path, transaction)
                source.rename(saved_link)
                stage.rename(source)
                if inventory(target, True) != manifest:
                    raise OSError("还原期间目标文件发生变化")
                record.status = MigrationStatus.RESTORED
                record.size_bytes = size
                record.file_count = sum(v[0] == "file" for v in manifest.values())
                record.error_message = "已还原；目标盘副本保留，请确认软件正常后自行处理。"
                self._save_record(record)
                committed = True
                if not self._matches_link(saved_link, target):
                    raise OSError("待移除联接发生变化")
                SafetyGuard.remove_junction_safely(str(saved_link))
                self.journal_path.unlink()
                report(progress_cb, 100, "还原完成，目标盘副本保留", size)
                return record
            except Exception as exc:
                if committed:
                    record.error_message = f"还原完成，残留联接或日志待处理: {exc}"
                    return record
                clean = self._rollback_restore(transaction)
                raise MigrationRollbackError(f"还原未完成: {exc}", clean) from exc

    def _rollback_restore(self, transaction):
        source, target, stage, saved = (Path(transaction[k]) for k in ("source", "target", "stage", "saved_link"))
        try:
            if os.path.lexists(saved):
                if not self._matches_link(saved, target):
                    return False
                if os.path.lexists(source):
                    plain_path(source)
                    if os.path.lexists(stage):
                        return False
                    source.rename(stage)  # Retain every copied/changed byte.
                saved.rename(source)
            if not self._matches_link(source, target):
                return False
            self.journal_path.unlink(missing_ok=True)
            return True
        except Exception:
            return False

    def recover_pending(self):
        """Explicit recovery API. Does not delete unverified copies or backups."""
        with self._exclusive():
            if not self.journal_path.exists():
                return "没有待恢复事务"
            plain_path(self.journal_path)
            tx = json.loads(self.journal_path.read_text(encoding="utf-8"))
            source, target = Path(tx["source"]), Path(tx["target"])
            plain_path(source.parent)
            plain_path(target)
            # Validate journal relationships before acting on persisted paths.
            if tx["kind"] == "move":
                backup = plain_path(tx["backup"])
                if backup.parent != source.parent or not backup.name.startswith(source.name + "._cdr_backup_"):
                    raise ValueError("事务备份路径不合法")
                SafetyGuard.validate_for_migration(str(backup), str(target))
            elif tx["kind"] == "restore":
                stage, saved = Path(tx["stage"]), Path(tx["saved_link"])
                if (stage.parent != source.parent or saved.parent != source.parent or
                    not stage.name.startswith(source.name + "._cdr_restore_") or
                    not saved.name.startswith(source.name + "._cdr_link_")):
                    raise ValueError("还原事务路径不合法")
                SafetyGuard.validate_for_migration(str(target), str(stage))
            else:
                raise ValueError("未知事务类型")
            records = self._read_records()
            expected = MigrationStatus.MOVED if tx["kind"] == "move" else MigrationStatus.RESTORED
            committed = any(r.record_id == tx["id"] and r.status == expected and
                            r.source_path == str(source) and r.target_path == str(target) for r in records)
            if committed:
                valid = self._matches_link(source, target) if tx["kind"] == "move" else source.is_dir() and not SafetyGuard.is_junction_or_symlink(str(source))
                if not valid:
                    raise RuntimeError("已提交事务的当前目录状态异常，保留全部数据")
                # Cleanup may have been interrupted; do not delete partially removed backups.
                self.journal_path.unlink()
                return "事务已提交；备份或残留联接保留，请检查后处理"
            ok = self._rollback_move(tx) if tx["kind"] == "move" else self._rollback_restore(tx)
            if not ok:
                raise MigrationRollbackError("状态有歧义，保留全部文件与事务日志，请人工检查", False)
            return "已恢复操作前路径；未完成副本保留"

    def list_migration_records(self):
        with self._exclusive():
            records = self._read_records()
            for record in records:
                if self.journal_path.exists():
                    record.error_message = "存在待处理事务，请先检查 recover_pending()；原有副本暂时保留"
                if record.status == MigrationStatus.MOVED:
                    if not Path(record.target_path).is_dir() or not self._matches_link(Path(record.source_path), Path(record.target_path)):
                        record.status = MigrationStatus.BROKEN
                        record.error_message = "目录联接或目标目录失效"
            return records
