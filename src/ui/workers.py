"""
后台任务执行器 (Claude)
=====================
所有扫描与磁盘操作都在后台线程执行，界面线程只接收信号。

两类执行器：
1. `Worker`（v1.0 页面使用）：守护线程 + Qt 信号。只读扫描在退出程序时可以直接结束。
2. `BackendThread` 及其子类（v1.1 深水区实验室使用）：QThread。
   - `DuplicateHardlinkWorker`：重复文件扫描 / 硬链接去重（含预演）
   - `OrphanScanWorker`：AppData 卸载残留扫描
   - `OrphanRecycleWorker`：残留目录复核 / 移入回收站
   - `KnownFolderMoveWorker`：个人文件夹检测 / 迁移 / 恢复未完成事务

共同约定：
- 后端进度回调 `(percent, current_action, accumulated_bytes)` 做节流（约 20 次/秒）。
- 字节数用 object 传递：pyqtSignal(int) 对应 C++ 32 位 int，超过 2 GB 会被静默截断。
- 进度回调本身不会抛异常给后端，回调失败不会中断磁盘事务。
- 会修改磁盘的操作由 OperationGate 登记；主窗口在这些操作完成前拒绝关闭。
- QThread 对象在界面线程里、线程结束后才释放引用，避免 "QThread destroyed while running"。
"""
from __future__ import annotations

import importlib.util
import logging
import os
import threading
import time
import traceback
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable, List, Optional

from PyQt6.QtCore import QObject, QThread, QTimer, pyqtSignal

logger = logging.getLogger("UI.Workers")


class _Throttle:
    """把高频进度回调压到约 20 次/秒；100% 与百分比变化总是发出。"""

    def __init__(self, emit: Callable[[int, str, int], None]):
        self._emit = emit
        self._last_emit = 0.0
        self._last_percent = -1

    def __call__(self, percent: int, action: str, size: int) -> None:
        try:
            now = time.monotonic()
            percent = int(max(0, min(100, percent)))
            if percent >= 100 or percent != self._last_percent or now - self._last_emit >= 0.05:
                self._last_emit = now
                self._last_percent = percent
                self._emit(percent, str(action), int(size))
        except Exception:  # 进度显示失败不能影响后端
            logger.debug("progress emit failed", exc_info=True)


# ===========================================================================
# v1.0：守护线程执行器
# ===========================================================================
class Worker(QObject):
    """在守护线程里运行一个函数，并通过 Qt 信号把进度与结果送回界面线程。"""

    progress = pyqtSignal(int, str, object)
    succeeded = pyqtSignal(object)
    failed = pyqtSignal(object)  # 异常对象，带 __traceback_text__ 属性
    done = pyqtSignal()

    _alive: set["Worker"] = set()  # 防止 Python 端提前回收

    def __init__(self, fn: Callable[..., Any], *args, with_progress: bool = True, name: str = "", **kwargs):
        super().__init__()
        self._fn = fn
        self._args = args
        self._kwargs = kwargs
        self._with_progress = with_progress
        self.name = name or getattr(fn, "__name__", "task")
        self._progress_cb = _Throttle(self.progress.emit)
        self._thread: Optional[threading.Thread] = None
        # done 由后台线程发出、在界面线程执行；在界面线程里释放引用，
        # 避免 QObject 在后台线程中被析构导致闪退。
        self.done.connect(self._forget)

    def _forget(self) -> None:
        QTimer.singleShot(0, lambda: Worker._alive.discard(self))

    def start(self) -> "Worker":
        Worker._alive.add(self)
        self._thread = threading.Thread(target=self._run, name=f"cdr-{self.name}", daemon=True)
        self._thread.start()
        return self

    def is_running(self) -> bool:
        return bool(self._thread and self._thread.is_alive())

    def _run(self) -> None:
        try:
            kwargs = dict(self._kwargs)
            if self._with_progress:
                kwargs["progress_cb"] = self._progress_cb
            result = self._fn(*self._args, **kwargs)
        except BaseException as exc:  # noqa: BLE001 - 所有异常都要交给界面展示
            exc.__traceback_text__ = traceback.format_exc()  # type: ignore[attr-defined]
            logger.error("后台任务 %s 失败: %s\n%s", self.name, exc, exc.__traceback_text__)  # type: ignore[attr-defined]
            self.failed.emit(exc)
        else:
            self.succeeded.emit(result)
        finally:
            self.done.emit()


class OperationGate(QObject):
    """
    全局“写操作”闸门：同一时间只允许一个会修改磁盘或系统设置的操作
    （清理、迁移、还原、恢复事务、关闭休眠、硬链接去重、个人文件夹迁移、移入回收站）。
    只读扫描不经过这里。
    """

    changed = pyqtSignal(str)  # 当前操作名；空字符串表示空闲

    def __init__(self):
        super().__init__()
        self._current = ""

    @property
    def current(self) -> str:
        return self._current

    @property
    def busy(self) -> bool:
        return bool(self._current)

    def acquire(self, name: str) -> bool:
        if self._current:
            return False
        self._current = name
        self.changed.emit(name)
        return True

    def release(self) -> None:
        self._current = ""
        self.changed.emit("")


def run_task(fn, *args, on_success=None, on_error=None, on_progress=None, on_done=None,
             with_progress: bool = True, name: str = "", **kwargs) -> Worker:
    """便捷封装：创建、连接并启动一个 Worker。"""
    worker = Worker(fn, *args, with_progress=with_progress, name=name, **kwargs)
    if on_progress:
        worker.progress.connect(on_progress)
    if on_success:
        worker.succeeded.connect(on_success)
    if on_error:
        worker.failed.connect(on_error)
    if on_done:
        worker.done.connect(on_done)
    return worker.start()


# ===========================================================================
# v1.1：QThread 执行器
# ===========================================================================
class BackendThread(QThread):
    """
    v1.1 实验室后台线程基类。子类实现 `execute()`，返回值经 `succeeded` 发出，
    异常经 `failed` 发出（附 `__traceback_text__`）。`mutating` 表示是否修改磁盘。
    """

    progress = pyqtSignal(int, str, object)
    succeeded = pyqtSignal(object)
    failed = pyqtSignal(object)

    _alive: set["BackendThread"] = set()

    def __init__(self, label: str, mutating: bool, parent: Optional[QObject] = None):
        super().__init__(parent)
        self.label = label
        self.mutating = mutating
        self._progress_cb = _Throttle(self.progress.emit)
        self.finished.connect(self._forget)

    # -- lifecycle ----------------------------------------------------------
    def start(self, *args) -> "BackendThread":  # type: ignore[override]
        BackendThread._alive.add(self)
        super().start(*args)
        return self

    def _forget(self) -> None:
        def release():
            self.wait(2000)  # finished 发出后线程会在极短时间内真正退出
            BackendThread._alive.discard(self)
        QTimer.singleShot(0, release)

    @classmethod
    def running(cls, mutating: Optional[bool] = None) -> List["BackendThread"]:
        return [t for t in list(cls._alive)
                if t.isRunning() and (mutating is None or t.mutating == mutating)]

    def run(self) -> None:  # noqa: D401 - QThread API
        try:
            result = self.execute()
        except BaseException as exc:  # noqa: BLE001
            exc.__traceback_text__ = traceback.format_exc()  # type: ignore[attr-defined]
            logger.error("后台任务 %s 失败: %s\n%s", self.label, exc, exc.__traceback_text__)  # type: ignore[attr-defined]
            self.failed.emit(exc)
        else:
            self.succeeded.emit(result)

    def execute(self) -> Any:
        raise NotImplementedError

    @staticmethod
    def _require(obj, what: str):
        if obj is None:
            raise RuntimeError(f"{what}未加载，此功能不可用。")
        return obj


class DuplicateHardlinkWorker(BackendThread):
    """
    微信/QQ 重复文件：
    - mode="scan"   → disk_analyzer.scan_duplicate_files_fast()，返回 List[DuplicateGroup]
    - mode="dedupe" → system_optimizer.deduplicate_via_hardlink()，返回 HardlinkDedupeSummary
    dedupe 只接收最近一次扫描返回的原始 DuplicateGroup 对象。
    """

    SCAN = "scan"
    DEDUPE = "dedupe"

    def __init__(self, disk_analyzer=None, system_optimizer=None, *, mode: str = SCAN,
                 groups: Optional[Iterable] = None, dry_run: bool = False,
                 scan_roots: Optional[List[str]] = None, min_file_size: int = 1024 * 1024,
                 parent: Optional[QObject] = None):
        if mode not in (self.SCAN, self.DEDUPE):
            raise ValueError(mode)
        label = "扫描重复文件" if mode == self.SCAN else ("预演硬链接去重" if dry_run else "硬链接去重")
        super().__init__(label, mutating=(mode == self.DEDUPE and not dry_run), parent=parent)
        self.disk_analyzer = disk_analyzer
        self.system_optimizer = system_optimizer
        self.mode = mode
        self.groups = list(groups or [])
        self.dry_run = dry_run
        self.scan_roots = scan_roots
        self.min_file_size = min_file_size

    def execute(self):
        if self.mode == self.SCAN:
            analyzer = self._require(self.disk_analyzer, "磁盘分析引擎")
            return analyzer.scan_duplicate_files_fast(
                scan_roots=self.scan_roots, min_file_size=self.min_file_size, progress_cb=self._progress_cb)
        optimizer = self._require(self.system_optimizer, "系统优化引擎 (SystemOptimizer)")
        return optimizer.deduplicate_via_hardlink(self.groups, dry_run=self.dry_run, progress_cb=self._progress_cb)


class WinSxSCleanupWorker(BackendThread):
    """
    Windows 组件存储清理：system_optimizer.slim_winsxs_component_store() → (成功?, 说明文字)。
    运行 `Dism.exe /Online /Cleanup-Image /StartComponentCleanup /NoRestart`（由后端决定参数，
    不使用 /ResetBase，不自动重启）。属于写操作：进行中主窗口拒绝关闭。
    """

    def __init__(self, system_optimizer=None, parent: Optional[QObject] = None):
        super().__init__("清理 Windows 组件存储", mutating=True, parent=parent)
        self.system_optimizer = system_optimizer

    def execute(self):
        optimizer = self._require(self.system_optimizer, "系统优化引擎 (SystemOptimizer)")
        return optimizer.slim_winsxs_component_store(progress_cb=self._progress_cb)


def dism_needs_restart(message: str) -> bool:
    """后端在退出码 3010 时于首行注明需要重启。"""
    first = (message or "").splitlines()[0] if message else ""
    return "重启" in first


class OrphanScanWorker(BackendThread):
    """AppData / ProgramData 卸载残留扫描 → List[OrphanResidueItem]。后端无进度回调，界面显示忙碌状态。"""

    def __init__(self, disk_analyzer=None, parent: Optional[QObject] = None, **scan_kwargs):
        super().__init__("扫描卸载残留", mutating=False, parent=parent)
        self.disk_analyzer = disk_analyzer
        self.scan_kwargs = scan_kwargs

    def execute(self):
        analyzer = self._require(self.disk_analyzer, "磁盘分析引擎")
        self._progress_cb(0, "正在比对已安装软件并统计目录体积…", 0)
        return analyzer.discover_orphan_appdata_dirs(**self.scan_kwargs)


class KnownFolderMoveWorker(BackendThread):
    """
    Windows 个人文件夹（下载/桌面/文档/视频/图片）：
    - mode="inspect" → (List[KnownFolderItem], {folder_key: 无法读取的原因})
    - mode="move"    → system_optimizer.relocate_known_folder() → (KnownFolderItem, warnings)
    - mode="recover" → system_optimizer.recover_known_folder_transaction() → str
    move 时如 create_parent=True，会先创建目标的上级目录（只创建空目录，不移动任何文件）。
    """

    INSPECT = "inspect"
    MOVE = "move"
    RECOVER = "recover"

    def __init__(self, disk_analyzer=None, system_optimizer=None, *, mode: str = INSPECT,
                 item=None, target_path: str = "", create_parent: bool = False,
                 recommended_target_drive: str = "D:\\", parent: Optional[QObject] = None):
        if mode not in (self.INSPECT, self.MOVE, self.RECOVER):
            raise ValueError(mode)
        label = {self.INSPECT: "检测个人文件夹", self.MOVE: "迁移个人文件夹",
                 self.RECOVER: "恢复个人文件夹事务"}[mode]
        super().__init__(label, mutating=(mode != self.INSPECT), parent=parent)
        self.disk_analyzer = disk_analyzer
        self.system_optimizer = system_optimizer
        self.mode = mode
        self.item = item
        self.target_path = target_path
        self.create_parent = create_parent
        self.recommended_target_drive = recommended_target_drive

    def execute(self):
        if self.mode == self.INSPECT:
            analyzer = self._require(self.disk_analyzer, "磁盘分析引擎")
            self._progress_cb(0, "正在统计个人文件夹体积…", 0)
            items = analyzer.inspect_windows_known_folders(recommended_target_drive=self.recommended_target_drive)
            return items, dict(getattr(analyzer, "last_known_folder_errors", None) or {})
        optimizer = self._require(self.system_optimizer, "系统优化引擎 (SystemOptimizer)")
        if self.mode == self.RECOVER:
            return optimizer.recover_known_folder_transaction()
        if self.create_parent:
            parent = Path(self.target_path).parent
            if not parent.is_dir():
                parent.mkdir(parents=True, exist_ok=True)
        result = optimizer.relocate_known_folder(self.item, self.target_path, move_existing_files=True,
                                                 progress_cb=self._progress_cb)
        return result, list(getattr(optimizer, "last_warnings", []) or [])


# ---------------------------------------------------------------------------
# 卸载残留：复核与移入回收站
# 后端契约没有提供“移入回收站”接口，这里在界面层实现，并做比扫描时更严格的复核。
# ---------------------------------------------------------------------------
def default_orphan_roots() -> List[str]:
    """与 DiskAnalyzer.discover_orphan_appdata_dirs 的默认扫描根保持一致。"""
    home = os.path.expanduser("~")
    return [os.path.join(home, "AppData", "Local"),
            os.path.join(home, "AppData", "Roaming"),
            os.environ.get("ProgramData", r"C:\ProgramData")]


@dataclass
class OrphanCheck:
    path: str
    newest_change_days: Optional[int]   # 目录内最近一次文件改动距今天数；无文件时为 None
    files_seen: int
    locks: list = field(default_factory=list)
    lock_check_available: bool = True


def _norm(path: str) -> str:
    return os.path.normcase(os.path.abspath(path))


def _real(path: str) -> str:
    """真实路径（解析短名与链接）后再规范大小写，用于判断两个路径是否为同一位置。"""
    return os.path.normcase(os.path.realpath(os.path.abspath(path)))


def validate_orphan_path(item, safety_guard, allowed_roots: Optional[List[str]] = None) -> Path:
    """
    移入回收站前的硬性检查（任何一项不满足就拒绝）：
    - 必须是某个扫描根（AppData\\Local / Roaming / ProgramData）的直接子目录，不能是根本身
    - 目录存在，自身和上级都不是联接点/符号链接
    - 不属于受保护的系统厂商目录，不是本工具的状态目录
    - 通过 SafetyGuard.validate_for_cleaning
    """
    # 比较前统一用 realpath：Windows 上 %TEMP% 等路径可能是 8.3 短名（ADMINI~1），realpath 会展开为长名
    roots = {_real(r) for r in (allowed_roots or default_orphan_roots())}
    path = Path(os.path.abspath(item.path))
    if _real(str(path.parent)) not in roots:
        raise PermissionError(f"只允许处理 AppData\\Local、AppData\\Roaming、ProgramData 下的一级目录：{path}")
    if not path.is_dir():
        raise FileNotFoundError(f"目录已不存在：{path}")
    for p in (path, *path.parents):
        if safety_guard.is_junction_or_symlink(str(p)):
            raise PermissionError(f"路径经过联接点或符号链接，拒绝处理：{p}")
    if _real(str(path)) != _norm(os.path.join(os.path.realpath(path.parent), path.name)):
        raise PermissionError(f"路径解析后指向别处，拒绝处理：{path}")
    if safety_guard.is_protected_appdata_vendor(path.name, set()):
        raise PermissionError(f"“{path.name}”属于受保护的系统或常用组件目录。")
    state_dir = _real(os.environ.get("CDR_STATE_DIR", str(Path.home() / ".cdriverescue")))
    if state_dir == _real(str(path)) or state_dir.startswith(_real(str(path)) + os.sep):
        raise PermissionError("该目录包含本工具的状态记录。")
    safety_guard.validate_for_cleaning(str(path))
    return path


def inspect_orphan_dir(item, safety_guard, allowed_roots=None) -> OrphanCheck:
    """复核：重新检查路径，并统计目录内最近一次文件改动（目录自身修改时间并不反映深层文件的改动）。"""
    path = validate_orphan_path(item, safety_guard, allowed_roots)
    newest, count, stack = None, 0, [str(path)]
    while stack:
        current = stack.pop()
        try:
            with os.scandir(current) as entries:
                for entry in entries:
                    try:
                        if entry.is_symlink():
                            continue
                        st = entry.stat(follow_symlinks=False)
                        if entry.is_dir(follow_symlinks=False):
                            if not (getattr(st, "st_file_attributes", 0) & 0x400):
                                stack.append(entry.path)
                        else:
                            count += 1
                            newest = st.st_mtime if newest is None else max(newest, st.st_mtime)
                    except OSError:
                        continue
        except OSError:
            continue
    days = None if newest is None else max(0, int((time.time() - newest) // 86400))
    has_psutil = importlib.util.find_spec("psutil") is not None
    locks = []
    if has_psutil:
        try:
            locks = list(safety_guard.find_locking_processes(str(path)))
        except Exception:  # 占用检测失败时按“无法检查”处理
            has_psutil = False
    return OrphanCheck(str(path), days, count, locks, has_psutil)


class LockCheckUnavailable(RuntimeError):
    """无法确认目录是否被程序占用（缺少 psutil 或检测出错）。写操作必须停止。"""


def recycle_orphan_dir(item, safety_guard, allowed_roots=None) -> str:
    """
    重新校验后把目录移入回收站。
    - 占用检查无条件执行：缺少 psutil 或检测出错都视为“无法确认”，拒绝写操作（与旧迁移“无法检查即拒绝”一致）。
    - 缺少 Send2Trash 时报错，不会改为永久删除。
    """
    path = validate_orphan_path(item, safety_guard, allowed_roots)
    if importlib.util.find_spec("psutil") is None:
        raise LockCheckUnavailable("缺少 psutil，无法确认是否有程序正在使用该目录，已停止移入回收站。"
                                   "请安装依赖（pip install psutil）后重试。")
    try:
        safety_guard.ensure_no_locking_processes(str(path))
    except Exception as exc:
        from src.core.contracts import ProcessLockedError
        if isinstance(exc, ProcessLockedError):
            raise
        raise LockCheckUnavailable(f"检查程序占用时出错，已停止移入回收站：{exc}") from exc
    try:
        from send2trash import send2trash
    except ImportError as exc:
        raise ImportError("send2trash 未安装，无法移入回收站") from exc
    send2trash(str(path))
    if os.path.lexists(path):
        raise OSError(f"移入回收站后目录仍然存在，请手动检查：{path}")
    return str(path)


class OrphanRecycleWorker(BackendThread):
    """卸载残留目录：mode="inspect" 复核（只读）→ OrphanCheck；mode="recycle" 移入回收站 → 路径。"""

    INSPECT = "inspect"
    RECYCLE = "recycle"

    def __init__(self, item, safety_guard, *, mode: str = INSPECT, allowed_roots=None,
                 parent: Optional[QObject] = None):
        if mode not in (self.INSPECT, self.RECYCLE):
            raise ValueError(mode)
        super().__init__("复核残留目录" if mode == self.INSPECT else "移入回收站",
                         mutating=(mode == self.RECYCLE), parent=parent)
        self.item = item
        self.safety_guard = safety_guard
        self.mode = mode
        self.allowed_roots = allowed_roots

    def execute(self):
        if self.mode == self.INSPECT:
            return inspect_orphan_dir(self.item, self.safety_guard, self.allowed_roots)
        return recycle_orphan_dir(self.item, self.safety_guard, self.allowed_roots)


def start_thread(thread: BackendThread, on_success=None, on_error=None, on_progress=None,
                 on_done=None) -> BackendThread:
    """便捷封装：连接信号并启动一个 BackendThread。on_done 在成功或失败处理之后执行。"""
    if on_progress:
        thread.progress.connect(on_progress)
    if on_success:
        thread.succeeded.connect(on_success)
    if on_error:
        thread.failed.connect(on_error)
    if on_done:
        thread.finished.connect(on_done)
    return thread.start()
