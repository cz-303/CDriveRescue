"""
CDriveRescue 主窗口 (Claude)
==========================
由 main.py 以如下方式构造：

    MainWindow(disk_analyzer=..., safety_guard=SafetyGuard, cache_cleaner=..., app_mover=...,
               system_optimizer=...)

cache_cleaner / app_mover / system_optimizer 可以为 None（模块未合入时），对应页面会显示不可用提示。
v1.1 新增第 6 个页签“深水区瘦身实验室”（src/ui/lab_page.py）。
"""
from __future__ import annotations

import logging
import os
import sys
from pathlib import Path

from PyQt6.QtCore import QSize, Qt
from PyQt6.QtGui import QColor, QIcon, QPalette
from PyQt6.QtWidgets import (
    QApplication, QHBoxLayout, QLabel, QListWidget, QListWidgetItem, QMainWindow, QMessageBox,
    QPushButton, QStackedWidget, QVBoxLayout, QWidget,
)

from src import __version__
from src.ui.pages import AppContext, CachePage, DashboardPage, LargeFilesPage, MigrationPage, RecordsPage
from src.ui.lab_page import LabPage
from src.ui.workers import BackendThread, OperationGate, Worker

logger = logging.getLogger("UI.MainWindow")

ICON_PATH = Path(__file__).resolve().parent / "assets" / "app.ico"
APP_USER_MODEL_ID = "CDriveRescue.Desktop"


def _set_windows_app_id() -> None:
    """
    让任务栏把窗口识别为独立程序，而不是 python.exe，从而显示本程序图标。
    必须在创建任何窗口之前调用；main.py 先导入本模块再创建 QApplication，满足这一点。
    """
    if sys.platform != "win32":
        return
    try:
        import ctypes
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(APP_USER_MODEL_ID)
    except Exception:  # 图标问题不应影响启动
        logger.debug("SetCurrentProcessExplicitAppUserModelID failed", exc_info=True)


_set_windows_app_id()


def app_icon() -> QIcon:
    if ICON_PATH.exists():
        return QIcon(str(ICON_PATH))
    png = ICON_PATH.with_suffix(".png")
    return QIcon(str(png)) if png.exists() else QIcon()

NAV = ["C 盘体检", "缓存清理", "大文件猎手", "软件搬家", "迁移记录", "深水区瘦身实验室"]
LAB_INDEX = 5


def _light_palette() -> QPalette:
    """固定浅色调色板，避免 Windows 深色模式下出现白底白字。"""
    pal = QPalette()
    roles = {
        QPalette.ColorRole.Window: "#F6F3EE",
        QPalette.ColorRole.WindowText: "#1F2A2E",
        QPalette.ColorRole.Base: "#FFFFFF",
        QPalette.ColorRole.AlternateBase: "#FBFAF7",
        QPalette.ColorRole.Text: "#1F2A2E",
        QPalette.ColorRole.Button: "#FFFFFF",
        QPalette.ColorRole.ButtonText: "#1F2A2E",
        QPalette.ColorRole.Highlight: "#1F6F78",
        QPalette.ColorRole.HighlightedText: "#FFFFFF",
        QPalette.ColorRole.ToolTipBase: "#1F2A2E",
        QPalette.ColorRole.ToolTipText: "#FFFFFF",
        QPalette.ColorRole.PlaceholderText: "#9AA0A3",
    }
    for role, color in roles.items():
        pal.setColor(role, QColor(color))
    pal.setColor(QPalette.ColorGroup.Disabled, QPalette.ColorRole.Text, QColor("#A8ADB0"))
    pal.setColor(QPalette.ColorGroup.Disabled, QPalette.ColorRole.ButtonText, QColor("#A8ADB0"))
    return pal


class MainWindow(QMainWindow):
    def __init__(self, disk_analyzer, safety_guard, cache_cleaner=None, app_mover=None,
                 system_optimizer=None):
        super().__init__()
        app = QApplication.instance()
        if app is not None:
            app.setStyle("Fusion")
            app.setWindowIcon(app_icon())
            app.setPalette(_light_palette())

        self.gate = OperationGate()
        self.ctx = AppContext(disk_analyzer, safety_guard, cache_cleaner, app_mover, self.gate,
                              optimizer=system_optimizer)

        self.setWindowTitle("C盘焦虑拯救者 · CDriveRescue")
        self.setWindowIcon(app_icon())
        self.resize(1180, 780)
        self.setMinimumSize(980, 640)

        root = QWidget()
        layout = QHBoxLayout(root)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        layout.addWidget(self._build_sidebar())

        self.stack = QStackedWidget()
        self.dashboard = DashboardPage(self.ctx)
        self.cache = CachePage(self.ctx)
        self.large = LargeFilesPage(self.ctx)
        self.migration = MigrationPage(self.ctx)
        self.records = RecordsPage(self.ctx)
        self.lab = LabPage(self.ctx)
        for page in (self.dashboard, self.cache, self.large, self.migration, self.records, self.lab):
            self.stack.addWidget(page)
        layout.addWidget(self.stack, 1)
        self.setCentralWidget(root)

        # 页面联动
        self.dashboard.navigate.connect(self.nav.setCurrentRow)
        self.cache.disk_changed.connect(self.dashboard.refresh)
        self.migration.disk_changed.connect(self.dashboard.refresh)
        self.migration.records_changed.connect(self._records_dirty)
        self.records.disk_changed.connect(self.dashboard.refresh)
        self.records.pending_changed.connect(self.dashboard.set_pending)
        self.lab.disk_changed.connect(self.dashboard.refresh)
        self.gate.changed.connect(self._on_gate_changed)

        self.nav.setCurrentRow(0)
        self.dashboard.refresh()
        if app_mover is not None:
            self.records.check_pending()

    # -- sidebar ------------------------------------------------------------
    def _build_sidebar(self) -> QWidget:
        bar = QWidget()
        bar.setObjectName("sidebar")
        bar.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        col = QVBoxLayout(bar)
        col.setContentsMargins(0, 24, 0, 18)
        col.setSpacing(4)

        brand = QVBoxLayout()
        brand.setContentsMargins(22, 0, 16, 18)
        brand.setSpacing(2)
        title = QLabel("C盘焦虑拯救者")
        title.setObjectName("brandTitle")
        sub = QLabel("CDriveRescue")
        sub.setObjectName("brandSub")
        brand.addWidget(title)
        brand.addWidget(sub)
        col.addLayout(brand)

        self.nav = QListWidget()
        self.nav.setObjectName("navList")
        self.nav.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        for text in NAV:
            item = QListWidgetItem(text)
            item.setSizeHint(QSize(0, 40))
            self.nav.addItem(item)
        self.nav.currentRowChanged.connect(self._switch_page)
        col.addWidget(self.nav, 1)

        foot = QVBoxLayout()
        foot.setContentsMargins(22, 0, 16, 0)
        foot.setSpacing(6)
        self.busy_label = QLabel("")
        self.busy_label.setObjectName("busyLabel")
        self.busy_label.setWordWrap(True)
        self.busy_label.hide()
        foot.addWidget(self.busy_label)
        is_admin = False
        try:
            is_admin = bool(self.ctx.guard.is_admin())
        except Exception:
            pass
        mode = QLabel("管理员模式" if is_admin else "普通用户模式")
        mode.setObjectName("sidebarFoot")
        foot.addWidget(mode)
        if not is_admin:
            self.elevate_btn = QPushButton("以管理员身份重启")
            self.elevate_btn.setObjectName("linkButton")
            self.elevate_btn.setStyleSheet("color:#8FC7CD; text-align:left; padding:0;")
            self.elevate_btn.setCursor(Qt.CursorShape.PointingHandCursor)
            self.elevate_btn.clicked.connect(self._elevate)
            foot.addWidget(self.elevate_btn)
        version = QLabel(f"v{__version__}")
        version.setObjectName("sidebarFoot")
        foot.addWidget(version)
        col.addLayout(foot)
        return bar

    def _switch_page(self, row: int) -> None:
        if row >= 0:
            self.stack.setCurrentIndex(row)

    def _records_dirty(self) -> None:
        self.records.loaded_once = False
        if self.stack.currentWidget() is self.records:
            self.records.refresh()
        else:
            self.records.check_pending()

    def _on_gate_changed(self, name: str) -> None:
        if name:
            self.busy_label.setText(f"● 正在进行：{name}")
            self.busy_label.show()
        else:
            self.busy_label.hide()

    def _elevate(self) -> None:
        if self.gate.busy:
            QMessageBox.information(self, "请稍候", f"“{self.gate.current}”完成后再重启。")
            return
        if self.ctx.guard.request_admin_elevation():
            QApplication.quit()
        else:
            QMessageBox.warning(self, "未能提权", "没有获得管理员权限（可能取消了 UAC 提示）。")

    # -- lifecycle ----------------------------------------------------------
    def closeEvent(self, event):  # noqa: N802
        if self.gate.busy or BackendThread.running(mutating=True):
            QMessageBox.warning(
                self, "操作进行中",
                f"“{self.gate.current or '磁盘操作'}”还没有完成。为避免留下未完成的事务，请等它结束后再关闭。")
            event.ignore()
            return
        scans = BackendThread.running(mutating=False)
        if scans:
            # QThread 不能在运行中被销毁；只读扫描没有数据风险，确认后直接结束进程。
            names = "、".join(sorted({t.label for t in scans}))
            box = QMessageBox(self)
            box.setIcon(QMessageBox.Icon.Question)
            box.setWindowTitle("扫描还在进行")
            box.setText(f"“{names}”还没完成。现在退出会直接停止扫描，不会影响任何文件。")
            quit_btn = box.addButton("退出", QMessageBox.ButtonRole.AcceptRole)
            stay = box.addButton("继续等待", QMessageBox.ButtonRole.RejectRole)
            box.setDefaultButton(stay)
            box.exec()
            if box.clickedButton() is not quit_btn:
                event.ignore()
                return
            logger.info("退出时停止只读扫描: %s", names)
            logging.shutdown()
            os._exit(0)
        # v1.0 页面的只读任务在守护线程中运行，可直接退出
        running = [w.name for w in list(Worker._alive) if w.is_running()]
        if running:
            logger.info("退出时仍有只读任务在运行，将直接结束: %s", running)
        event.accept()
