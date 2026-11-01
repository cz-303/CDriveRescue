"""
可复用界面组件 (Claude)
=====================
焦虑指数仪表、风险徽章、卡片、进度面板，以及统一的异常文案转换。
"""
from __future__ import annotations

import os
import platform
import subprocess
from typing import Iterable, Optional

from PyQt6.QtCore import QRectF, Qt, QUrl, pyqtSignal
from PyQt6.QtGui import QColor, QDesktopServices, QFont, QPainter, QPen
from PyQt6.QtWidgets import (
    QButtonGroup, QDialog, QDialogButtonBox, QFrame, QHBoxLayout, QLabel, QMessageBox, QPlainTextEdit,
    QProgressBar, QPushButton, QSizePolicy, QVBoxLayout, QWidget,
)

from src.core.contracts import (
    AnxietyLevel, CDriveRescueError, CriticalPathForbiddenError, InsufficientTargetSpaceError,
    MigrationRollbackError, ProcessLockedError, RiskLevel, format_bytes,
)

# 与 theme.qss 保持一致的语义色
COLORS = {
    "ink": "#1F2A2E",
    "muted": "#6B7378",
    "track": "#E6E1D8",
    "healthy": "#2F8F5B",
    "moderate": "#C98A12",
    "critical": "#C9473D",
    "accent": "#1F6F78",
}

LEVEL_COLOR = {
    AnxietyLevel.HEALTHY: COLORS["healthy"],
    AnxietyLevel.MODERATE: COLORS["moderate"],
    AnxietyLevel.CRITICAL: COLORS["critical"],
}
LEVEL_TEXT = {
    AnxietyLevel.HEALTHY: "从容",
    AnxietyLevel.MODERATE: "紧绷",
    AnxietyLevel.CRITICAL: "告急",
}
RISK_TEXT = {RiskLevel.SAFE: "放心清", RiskLevel.CAUTION: "谨慎", RiskLevel.DANGER: "禁止"}


# ---------------------------------------------------------------------------
# 基础组件
# ---------------------------------------------------------------------------
class Card(QFrame):
    """白底圆角卡片。"""

    def __init__(self, title: str = "", parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.setObjectName("card")
        self.body = QVBoxLayout(self)
        self.body.setContentsMargins(20, 16, 20, 18)
        self.body.setSpacing(10)
        if title:
            label = QLabel(title)
            label.setObjectName("cardTitle")
            self.body.addWidget(label)


class RiskBadge(QLabel):
    def __init__(self, risk: RiskLevel, parent: Optional[QWidget] = None):
        super().__init__(RISK_TEXT.get(risk, str(risk)), parent)
        self.setObjectName(f"badge{risk.value.title()}")
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)


class Pill(QLabel):
    """小号圆角标签。kind: info / safe / caution / danger / muted。"""

    def __init__(self, text: str, kind: str = "info", parent: Optional[QWidget] = None):
        super().__init__(text, parent)
        self.setObjectName(f"pill_{kind}")
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)

    def set_kind(self, kind: str) -> None:
        self.setObjectName(f"pill_{kind}")
        self.style().unpolish(self)
        self.style().polish(self)


CONFIDENCE_TEXT = {RiskLevel.SAFE: "SAFE · 高置信", RiskLevel.CAUTION: "CAUTION · 需确认",
                   RiskLevel.DANGER: "DANGER · 禁止"}
CONFIDENCE_KIND = {RiskLevel.SAFE: "safe", RiskLevel.CAUTION: "caution", RiskLevel.DANGER: "danger"}


def confidence_pill(level: RiskLevel) -> Pill:
    """卸载残留置信度徽章。SAFE 只代表“较可能是残留”，不代表删除一定无影响。"""
    return Pill(CONFIDENCE_TEXT.get(level, str(level)), CONFIDENCE_KIND.get(level, "muted"))


def cell(widget: QWidget, align=Qt.AlignmentFlag.AlignLeft) -> QWidget:
    """把小部件放进表格单元格，留出边距。"""
    box = QWidget()
    lay = QHBoxLayout(box)
    lay.setContentsMargins(4, 0, 4, 0)
    if align != Qt.AlignmentFlag.AlignLeft:
        lay.addStretch(1)
    lay.addWidget(widget)
    if align != Qt.AlignmentFlag.AlignRight:
        lay.addStretch(1)
    return box


class SegmentedTabs(QWidget):
    """分段切换按钮。currentChanged(int)。"""

    currentChanged = pyqtSignal(int)

    def __init__(self, labels, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.setObjectName("segmented")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(4, 4, 4, 4)
        lay.setSpacing(4)
        self.group = QButtonGroup(self)
        self.group.setExclusive(True)
        self.buttons = []
        for idx, text in enumerate(labels):
            btn = QPushButton(text)
            btn.setObjectName("segment")
            btn.setCheckable(True)
            btn.setCursor(Qt.CursorShape.PointingHandCursor)
            self.group.addButton(btn, idx)
            self.buttons.append(btn)
            lay.addWidget(btn)
        lay.addStretch(1)
        self.group.idClicked.connect(self.currentChanged.emit)
        if self.buttons:
            self.buttons[0].setChecked(True)

    def set_current(self, index: int) -> None:
        self.buttons[index].setChecked(True)
        self.currentChanged.emit(index)


class StatTile(QFrame):
    """一个大数字 + 小标题。"""

    def __init__(self, caption: str, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.setObjectName("statTile")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 12, 16, 12)
        layout.setSpacing(2)
        self.value = QLabel("—")
        self.value.setObjectName("statValue")
        self.caption = QLabel(caption)
        self.caption.setObjectName("statCaption")
        layout.addWidget(self.value)
        layout.addWidget(self.caption)

    def set_value(self, text: str) -> None:
        self.value.setText(text)


class Banner(QFrame):
    """页面顶部提示条：info / warn / danger。"""

    def __init__(self, kind: str = "info", parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.setObjectName(f"banner_{kind}")
        self.layout_ = QHBoxLayout(self)
        self.layout_.setContentsMargins(14, 10, 14, 10)
        self.layout_.setSpacing(12)
        self.text = QLabel()
        self.text.setWordWrap(True)
        self.text.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.layout_.addWidget(self.text, 1)
        self.hide()

    def set_kind(self, kind: str) -> None:
        self.setObjectName(f"banner_{kind}")
        self.style().unpolish(self)
        self.style().polish(self)

    def show_text(self, text: str, kind: Optional[str] = None) -> None:
        if kind:
            self.set_kind(kind)
        self.text.setText(text)
        self.show()

    def add_button(self, button) -> None:
        self.layout_.addWidget(button)


class ProgressPanel(QWidget):
    """进度条 + 当前动作 + 累计体积。"""

    def __init__(self, bytes_caption: str = "已发现", parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.bytes_caption = bytes_caption
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(4)
        self.bar = QProgressBar()
        self.bar.setRange(0, 100)
        self.bar.setTextVisible(False)
        self.bar.setFixedHeight(8)
        row = QHBoxLayout()
        self.action = QLabel("")
        self.action.setObjectName("muted")
        self.action.setMinimumWidth(10)
        self.action.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self.amount = QLabel("")
        self.amount.setObjectName("muted")
        row.addWidget(self.action, 1)
        row.addWidget(self.amount)
        layout.addWidget(self.bar)
        layout.addLayout(row)
        self.hide()

    def begin(self, text: str = "准备中…") -> None:
        self.bar.setRange(0, 100)
        self.bar.setValue(0)
        self.action.setText(text)
        self.amount.setText("")
        self.show()

    def begin_busy(self, text: str) -> None:
        """后端没有进度回调时显示来回滚动的忙碌条。"""
        self.bar.setRange(0, 0)
        self.action.setText(text)
        self.amount.setText("")
        self.show()

    def update_progress(self, percent: int, action: str, size: int) -> None:
        if self.bar.maximum() == 0 and percent <= 0:
            self.action.setText(elide_middle(action, 90))
            return
        self.bar.setRange(0, 100)
        self.bar.setValue(percent)
        # 路径可能很长，只显示尾部
        self.action.setText(elide_middle(action, 90))
        # 没有字节语义的任务（DISM、残留扫描）不显示体积
        self.amount.setText(f"{self.bytes_caption} {format_bytes(size)}" if self.bytes_caption else "")

    def finish(self, text: str = "") -> None:
        self.bar.setRange(0, 100)
        self.bar.setValue(100)
        if text:
            self.action.setText(text)


class AnxietyGauge(QWidget):
    """C 盘焦虑指数环形仪表（0~100）。"""

    def __init__(self, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.score: Optional[int] = None
        self.level: Optional[AnxietyLevel] = None
        self.setMinimumSize(200, 200)
        self.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
        self.setFixedSize(210, 210)

    def set_score(self, score: int, level: AnxietyLevel) -> None:
        self.score, self.level = score, level
        self.update()

    def paintEvent(self, event):  # noqa: N802 - Qt API
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        side = min(self.width(), self.height())
        pen_w = 16
        rect = QRectF((self.width() - side) / 2 + pen_w, (self.height() - side) / 2 + pen_w,
                      side - 2 * pen_w, side - 2 * pen_w)
        start, span = 225 * 16, -270 * 16

        track = QPen(QColor(COLORS["track"]), pen_w)
        track.setCapStyle(Qt.PenCapStyle.RoundCap)
        painter.setPen(track)
        painter.drawArc(rect, start, span)

        color = QColor(LEVEL_COLOR.get(self.level, COLORS["muted"]))
        if self.score is not None:
            value = QPen(color, pen_w)
            value.setCapStyle(Qt.PenCapStyle.RoundCap)
            painter.setPen(value)
            painter.drawArc(rect, start, int(span * max(0, min(100, self.score)) / 100))

        painter.setPen(QColor(COLORS["ink"]))
        font = QFont(self.font())
        font.setPixelSize(52)
        font.setWeight(QFont.Weight.DemiBold)
        painter.setFont(font)
        number_rect = QRectF(rect.left(), rect.top() + rect.height() * 0.22, rect.width(), rect.height() * 0.40)
        painter.drawText(number_rect, Qt.AlignmentFlag.AlignCenter, "—" if self.score is None else str(self.score))

        font.setPixelSize(14)
        font.setWeight(QFont.Weight.Normal)
        painter.setFont(font)
        painter.setPen(QColor(COLORS["muted"]))
        caption_rect = QRectF(rect.left(), number_rect.bottom() - 2, rect.width(), 22)
        painter.drawText(caption_rect, Qt.AlignmentFlag.AlignCenter, "焦虑指数")

        if self.level is not None:
            font.setPixelSize(15)
            font.setWeight(QFont.Weight.DemiBold)
            painter.setFont(font)
            painter.setPen(color)
            level_rect = QRectF(rect.left(), rect.bottom() - 12, rect.width(), 24)
            painter.drawText(level_rect, Qt.AlignmentFlag.AlignCenter, LEVEL_TEXT[self.level])
        painter.end()


class DetailsDialog(QDialog):
    """展示长文本（扫描跳过记录、错误列表）。"""

    def __init__(self, title: str, intro: str, lines: Iterable[str], parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.setWindowTitle(title)
        self.resize(760, 460)
        layout = QVBoxLayout(self)
        if intro:
            label = QLabel(intro)
            label.setWordWrap(True)
            layout.addWidget(label)
        text = QPlainTextEdit()
        text.setReadOnly(True)
        text.setPlainText("\n".join(lines))
        layout.addWidget(text, 1)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.rejected.connect(self.reject)
        buttons.accepted.connect(self.accept)
        layout.addWidget(buttons)


# ---------------------------------------------------------------------------
# 工具函数
# ---------------------------------------------------------------------------
def elide_middle(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    keep = (limit - 1) // 2
    return text[:keep] + "…" + text[-(limit - keep - 1):]


def confirm(parent: QWidget, title: str, text: str, informative: str = "",
            yes_text: str = "继续", danger: bool = False) -> bool:
    """默认焦点在“取消”的确认框。"""
    box = QMessageBox(parent)
    box.setIcon(QMessageBox.Icon.Warning if danger else QMessageBox.Icon.Question)
    box.setWindowTitle(title)
    box.setText(text)
    if informative:
        box.setInformativeText(informative)
    yes = box.addButton(yes_text, QMessageBox.ButtonRole.AcceptRole)
    cancel = box.addButton("取消", QMessageBox.ButtonRole.RejectRole)
    box.setDefaultButton(cancel)
    box.setEscapeButton(cancel)
    if danger:
        yes.setObjectName("dangerButton")
    box.exec()
    return box.clickedButton() is yes


def describe_exception(exc: BaseException) -> tuple[str, str]:
    """把后端异常转为 (标题, 用户可读说明)。"""
    if isinstance(exc, ProcessLockedError):
        names = "\n".join(sorted({f"• {l.name}（PID {l.pid}）  {l.locked_target}" for l in exc.locks}))
        return "请先关闭相关程序", (
            "以下程序正在使用该目录。为避免文件残缺，本工具不会替你结束进程，"
            "请保存工作并手动关闭后重试：\n\n" + names)
    if isinstance(exc, MigrationRollbackError):
        if exc.rolled_back_cleanly:
            return "操作未完成，已回滚", (
                f"{exc}\n\n原位置已恢复为操作前的状态。目标盘上可能保留了一份未完成的副本，"
                "确认无用后可自行删除以释放空间。")
        return "需要人工检查", (
            f"{exc}\n\n自动回滚未能确认数据状态，本工具已保留所有副本和事务日志，没有删除任何文件。"
            "请不要手动删除名称中带 ._cdr_、._kf_bak_ 或 ._hl_bak 的文件夹或文件；"
            "可在对应页面使用“检查并恢复”，或联系熟悉的人协助检查。")
    if exc.__class__.__name__ == "LockCheckUnavailable":
        return "无法检查程序占用", str(exc)
    if isinstance(exc, InsufficientTargetSpaceError):
        return "空间不足", str(exc)
    if isinstance(exc, CriticalPathForbiddenError):
        return "安全护栏已拦截", str(exc)
    if isinstance(exc, FileExistsError):
        return "目标已存在", (f"目标位置已有同名文件或文件夹，为避免覆盖已拒绝操作：\n{exc}\n\n"
                         "请换一个目标目录，或先确认并处理已有的同名文件夹。")
    if isinstance(exc, ImportError) and "send2trash" in str(exc).lower():
        return "缺少回收站组件", ("未安装 Send2Trash，无法移入回收站。本工具不会自动改为永久删除；"
                             "请安装依赖（pip install Send2Trash）后重试。")
    if isinstance(exc, PermissionError):
        return "权限不足", f"{exc}\n\n可能需要关闭占用程序，或以管理员身份运行本工具。"
    if isinstance(exc, KeyError):
        return "记录不存在", f"找不到记录 {exc}，请刷新列表。"
    if isinstance(exc, CDriveRescueError):
        return "无法继续", str(exc)
    if isinstance(exc, (RuntimeError, ValueError, OSError)):
        return "操作未执行", str(exc) or exc.__class__.__name__
    return "发生意外错误", f"{exc.__class__.__name__}: {exc}"


def show_exception(parent: QWidget, exc: BaseException, context: str = "") -> None:
    title, text = describe_exception(exc)
    box = QMessageBox(parent)
    severe = isinstance(exc, MigrationRollbackError) and not exc.rolled_back_cleanly
    box.setIcon(QMessageBox.Icon.Critical if severe else QMessageBox.Icon.Warning)
    box.setWindowTitle(title)
    box.setText(f"{context}\n\n{text}" if context else text)
    tb = getattr(exc, "__traceback_text__", "")
    if tb:
        box.setDetailedText(tb)
    box.exec()


def reveal_in_explorer(path: str) -> None:
    """在资源管理器中定位文件；非 Windows 打开所在目录。"""
    if platform.system() == "Windows" and os.path.exists(path):
        # 参数列表方式调用，不经过 shell
        subprocess.Popen(["explorer", "/select,", os.path.normpath(path)])
        return
    folder = path if os.path.isdir(path) else os.path.dirname(path)
    QDesktopServices.openUrl(QUrl.fromLocalFile(folder))


def same_volume(a: str, b: str) -> bool:
    da, db = os.path.splitdrive(os.path.abspath(a))[0], os.path.splitdrive(os.path.abspath(b))[0]
    if da or db:
        return da.lower() == db.lower()
    try:
        probe = b
        while not os.path.exists(probe) and os.path.dirname(probe) != probe:
            probe = os.path.dirname(probe)
        return os.stat(a).st_dev == os.stat(probe).st_dev
    except OSError:
        return False
