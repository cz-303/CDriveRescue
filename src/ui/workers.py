"""
后台任务执行器 (Claude)
=====================
所有扫描与磁盘操作都在后台线程执行，界面线程只接收信号。

设计要点：
- 使用守护线程而不是 QThread：只读扫描在退出程序时可以直接结束，
  不会出现 "QThread destroyed while running" 崩溃。会修改磁盘的操作
  由 OperationGate 登记，主窗口在这些操作完成前拒绝关闭。
- 后端进度回调 `(percent, current_action, accumulated_bytes)` 可能每个文件触发一次，
  这里做节流（约 20 次/秒），避免信号洪水拖慢界面。
- 回调本身不会抛异常给后端，符合交接文档“回调失败不会中断磁盘事务”的约定。
"""
from __future__ import annotations

import logging
import threading
import time
import traceback
from typing import Any, Callable, Optional

from PyQt6.QtCore import QObject, QTimer, pyqtSignal

logger = logging.getLogger("UI.Workers")


class Worker(QObject):
    """在守护线程里运行一个函数，并通过 Qt 信号把进度与结果送回界面线程。"""

    # 字节数用 object 传递：pyqtSignal(int) 对应 C++ 32 位 int，超过 2 GB 会被静默截断
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
        self._last_emit = 0.0
        self._last_percent = -1
        self._thread: Optional[threading.Thread] = None
        # done 由后台线程发出、在界面线程执行；在界面线程里释放引用，
        # 避免 QObject 在后台线程中被析构导致闪退。
        self.done.connect(self._forget)

    def _forget(self) -> None:
        QTimer.singleShot(0, lambda: Worker._alive.discard(self))

    # -- progress -----------------------------------------------------------
    def _progress_cb(self, percent: int, action: str, size: int) -> None:
        try:
            now = time.monotonic()
            percent = int(max(0, min(100, percent)))
            if percent >= 100 or percent != self._last_percent or now - self._last_emit >= 0.05:
                self._last_emit = now
                self._last_percent = percent
                self.progress.emit(percent, str(action), int(size))
        except Exception:  # 进度显示失败不能影响后端
            logger.debug("progress emit failed", exc_info=True)

    # -- lifecycle ----------------------------------------------------------
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
    （清理、迁移、还原、恢复事务、关闭休眠）。只读扫描不经过这里。
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
