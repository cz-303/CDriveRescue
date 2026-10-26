"""
各功能页面 (Claude)
=================
仪表盘 / 缓存清理 / 大文件猎手 / 软件搬家 / 迁移记录。

接入约定（见 BACKEND_HANDOFF.md）：
- 扫描与磁盘操作全部在后台线程；写操作经 OperationGate 串行化。
- 清理只使用同一个 CacheCleaner 实例最近一次扫描返回的 CacheItem 对象，不修改、不拼接；
  实际清理后旧结果失效，自动重新扫描。预演不消耗快照。
- 勾选状态由界面单独记录，不改 selected_by_default；CAUTION 默认不勾选。
- “预演”不显示为已释放，“回收站”不显示为已腾空。
- 不默认结束占用进程；提示用户手动关闭。
"""
from __future__ import annotations

import importlib.util
import os
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional

from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtWidgets import (
    QAbstractItemView, QCheckBox, QComboBox, QFileDialog, QGridLayout, QHBoxLayout,
    QHeaderView, QLabel, QMessageBox, QPushButton, QScrollArea, QSpinBox, QTableWidget,
    QTableWidgetItem, QTreeWidget, QTreeWidgetItem, QVBoxLayout, QWidget,
)

from src.core.contracts import (
    CacheCategory, CacheItem, CleanSummary, DriveHealthReport, MigrationRecord, MigrationStatus,
    MigrationTask, RiskLevel, format_bytes,
)
from src.ui.widgets import (
    AnxietyGauge, Banner, Card, DetailsDialog, ProgressPanel, RISK_TEXT, RiskBadge, StatTile,
    confirm, reveal_in_explorer, same_volume, show_exception,
)
from src.ui.workers import OperationGate, run_task

CATEGORY_TEXT = {
    CacheCategory.SYSTEM: "系统临时文件",
    CacheCategory.BROWSER: "浏览器缓存",
    CacheCategory.DEVELOPER: "开发工具缓存",
    CacheCategory.SOCIAL_OFFICE: "社交与办公软件",
}
STATUS_TEXT = {
    MigrationStatus.READY: "可迁移",
    MigrationStatus.LOCKED: "被占用",
    MigrationStatus.MOVING: "迁移中",
    MigrationStatus.MOVED: "已迁移",
    MigrationStatus.BROKEN: "联接失效",
    MigrationStatus.RESTORED: "已还原",
    MigrationStatus.FAILED_ROLLED_BACK: "失败已回滚",
}
SIZE_ROLE = Qt.ItemDataRole.UserRole + 1


class AppContext:
    """页面共享的后端对象。"""

    def __init__(self, analyzer, guard, cleaner, mover, gate: OperationGate):
        self.analyzer = analyzer
        self.guard = guard
        self.cleaner = cleaner
        self.mover = mover
        self.gate = gate

    @staticmethod
    def has_module(name: str) -> bool:
        return importlib.util.find_spec(name) is not None

    def pending_transaction(self) -> bool:
        path = getattr(self.mover, "journal_path", None)
        try:
            return bool(path and Path(path).exists())
        except OSError:
            return False


class SizeItem(QTableWidgetItem):
    """按字节数排序的表格单元。"""

    def __init__(self, size: int):
        super().__init__(format_bytes(size))
        self.setData(SIZE_ROLE, size)
        self.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)

    def __lt__(self, other):  # noqa: D401
        a, b = self.data(SIZE_ROLE), other.data(SIZE_ROLE)
        if a is not None and b is not None:
            return a < b
        return super().__lt__(other)


def _button(text: str, kind: str = "") -> QPushButton:
    btn = QPushButton(text)
    if kind:
        btn.setObjectName(kind)
    btn.setCursor(Qt.CursorShape.PointingHandCursor)
    return btn


def _label(text: str = "", name: str = "", wrap: bool = True) -> QLabel:
    lbl = QLabel(text)
    if name:
        lbl.setObjectName(name)
    lbl.setWordWrap(wrap)
    return lbl


def _table(headers: List[str]) -> QTableWidget:
    table = QTableWidget(0, len(headers))
    table.setHorizontalHeaderLabels(headers)
    table.verticalHeader().setVisible(False)
    table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
    table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
    table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
    table.setAlternatingRowColors(True)
    table.setShowGrid(False)
    table.setWordWrap(False)
    table.horizontalHeader().setHighlightSections(False)
    table.horizontalHeader().setStretchLastSection(True)
    table.horizontalHeader().setDefaultAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
    return table


def _local_time(iso: str) -> str:
    try:
        return datetime.fromisoformat(iso).astimezone().strftime("%Y-%m-%d %H:%M")
    except (TypeError, ValueError):
        return iso or "—"


class PageBase(QWidget):
    """标题 + 可滚动内容区。"""

    def __init__(self, ctx: AppContext, title: str, subtitle: str, scroll: bool = True):
        super().__init__()
        self.ctx = ctx
        self.setObjectName("pageRoot")
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        container = QWidget()
        container.setObjectName("pageRoot")
        self.content = QVBoxLayout(container)
        self.content.setContentsMargins(32, 26, 32, 26)
        self.content.setSpacing(16)

        head = QHBoxLayout()
        titles = QVBoxLayout()
        titles.setSpacing(4)
        titles.addWidget(_label(title, "pageTitle", wrap=False))
        titles.addWidget(_label(subtitle, "pageSub"))
        head.addLayout(titles, 1)
        self.header_actions = QHBoxLayout()
        self.header_actions.setSpacing(8)
        head.addLayout(self.header_actions)
        self.content.addLayout(head)

        if scroll:
            area = QScrollArea()
            area.setWidgetResizable(True)
            area.setWidget(container)
            outer.addWidget(area)
        else:
            outer.addWidget(container)

    def gate_acquire(self, name: str) -> bool:
        if self.ctx.gate.acquire(name):
            return True
        QMessageBox.information(self, "请稍候", f"“{self.ctx.gate.current}”正在进行，完成后再试。")
        return False


# ===========================================================================
# 1. 仪表盘
# ===========================================================================
class DashboardPage(PageBase):
    navigate = pyqtSignal(int)

    def __init__(self, ctx: AppContext):
        super().__init__(ctx, "C 盘体检", "看看系统盘现在有多挤，以及从哪里下手。")
        self.report: Optional[DriveHealthReport] = None
        self._loading = False

        self.refresh_btn = _button("重新体检")
        self.refresh_btn.clicked.connect(self.refresh)
        self.header_actions.addWidget(self.refresh_btn)

        self.pending_banner = Banner("danger")
        go = _button("前往处理")
        go.clicked.connect(lambda: self.navigate.emit(4))
        self.pending_banner.add_button(go)
        self.content.addWidget(self.pending_banner)

        self.env_banner = Banner("warn")
        self.content.addWidget(self.env_banner)

        # 概览卡片
        overview = Card()
        row = QHBoxLayout()
        row.setSpacing(28)
        self.gauge = AnxietyGauge()
        row.addWidget(self.gauge, 0, Qt.AlignmentFlag.AlignTop)
        right = QVBoxLayout()
        right.setSpacing(14)
        self.drive_label = _label("系统盘", "sectionTitle")
        self.headline = _label("正在体检…", "headline")
        right.addWidget(self.drive_label)
        right.addWidget(self.headline)
        tiles = QHBoxLayout()
        tiles.setSpacing(10)
        self.tile_total, self.tile_used, self.tile_free = StatTile("总容量"), StatTile("已用"), StatTile("剩余可用")
        for tile in (self.tile_total, self.tile_used, self.tile_free):
            tiles.addWidget(tile)
        right.addLayout(tiles)
        right.addStretch(1)
        row.addLayout(right, 1)
        overview.body.addLayout(row)
        self.content.addWidget(overview)

        grid = QGridLayout()
        grid.setHorizontalSpacing(16)
        grid.setVerticalSpacing(16)
        self.rec_card = Card("建议从这里开始")
        self.rec_box = QVBoxLayout()
        self.rec_box.setSpacing(8)
        self.rec_card.body.addLayout(self.rec_box)
        self.rec_card.body.addStretch(1)

        self.drive_card = Card("可作为迁移目标的磁盘")
        self.drive_box = QVBoxLayout()
        self.drive_box.setSpacing(6)
        self.drive_card.body.addLayout(self.drive_box)
        self.drive_card.body.addStretch(1)
        grid.addWidget(self.rec_card, 0, 0)
        grid.addWidget(self.drive_card, 0, 1)
        grid.setColumnStretch(0, 3)
        grid.setColumnStretch(1, 2)
        self.content.addLayout(grid)

        self.slim_card = Card("系统级大文件")
        self.slim_card.body.addWidget(_label(
            "这两个文件由 Windows 管理，不能直接删除，只能通过系统设置调整。", "muted"))
        self.slim_box = QVBoxLayout()
        self.slim_box.setSpacing(12)
        self.slim_card.body.addLayout(self.slim_box)
        self.content.addWidget(self.slim_card)
        self.content.addStretch(1)

        self._update_env_banner()

    # -- helpers ------------------------------------------------------------
    @staticmethod
    def _clear(layout) -> None:
        while layout.count():
            item = layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
            elif item.layout():
                DashboardPage._clear(item.layout())

    def _update_env_banner(self) -> None:
        notes = []
        if self.ctx.cleaner is None:
            notes.append("缓存清理模块未加载")
        if self.ctx.mover is None:
            notes.append("软件迁移模块未加载")
        if not self.ctx.has_module("psutil"):
            notes.append("缺少 psutil：无法检查进程占用，软件迁移已被禁用")
        if not self.ctx.has_module("send2trash"):
            notes.append("缺少 Send2Trash：无法移入回收站（不会自动改为永久删除）")
        if notes:
            self.env_banner.show_text("运行环境不完整 —— " + "；".join(notes) + "。可执行 pip install -r requirements-core.txt 补齐。")
        else:
            self.env_banner.hide()

    def set_pending(self, pending: bool) -> None:
        if pending:
            self.pending_banner.show_text(
                "检测到上次未完成的迁移或还原事务。在处理之前，新的迁移和还原会被阻止。", "danger")
        else:
            self.pending_banner.hide()

    # -- data ---------------------------------------------------------------
    def refresh(self) -> None:
        if self._loading:
            return
        self._loading = True
        self.refresh_btn.setEnabled(False)
        self.headline.setText("正在体检…")
        run_task(self.ctx.analyzer.analyze_drive_health, with_progress=False, name="health",
                 on_success=self._on_report, on_error=self._on_error, on_done=self._on_done)

    def _on_done(self) -> None:
        self._loading = False
        self.refresh_btn.setEnabled(True)

    def _on_error(self, exc) -> None:
        self.headline.setText("体检失败，请查看错误详情。")
        show_exception(self, exc, "读取系统盘信息时出错。")

    def _on_report(self, report: DriveHealthReport) -> None:
        self.report = report
        self.gauge.set_score(report.anxiety_score, report.anxiety_level)
        self.drive_label.setText(f"系统盘 {report.drive_letter}  ·  已用 {report.usage_percent}%")
        self.headline.setText(report.headline_diagnosis)
        self.tile_total.set_value(report.total_human)
        self.tile_used.set_value(report.used_human)
        self.tile_free.set_value(report.free_human)

        self._clear(self.rec_box)
        for idx, rec in enumerate(report.recommendations, 1):
            self.rec_box.addWidget(_label(f"{idx}.  {rec}"))

        self._clear(self.drive_box)
        if report.available_target_drives:
            for mount, free in report.available_target_drives:
                line = QHBoxLayout()
                line.addWidget(_label(mount, "sectionTitle", wrap=False))
                line.addStretch(1)
                line.addWidget(_label(f"可用 {format_bytes(free)}", "muted", wrap=False))
                self.drive_box.addLayout(line)
        else:
            self.drive_box.addWidget(_label("没有发现剩余空间超过 5 GB 的其他本地磁盘。", "muted"))

        self._clear(self.slim_box)
        for item in report.system_slim_items:
            self.slim_box.addWidget(self._slim_row(item))

    def _slim_row(self, item) -> QWidget:
        box = QWidget()
        lay = QHBoxLayout(box)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(16)
        text = QVBoxLayout()
        text.setSpacing(3)
        size = item.size_human if item.exists else "未生成 / 已关闭"
        text.addWidget(_label(f"{item.name}  ·  {size}", "sectionTitle"))
        text.addWidget(_label(item.description, "muted"))
        hint = _label(f"系统操作：{item.command_hint}", "muted")
        hint.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        text.addWidget(hint)
        lay.addLayout(text, 1)
        if item.id == "sys_hiberfil" and item.can_slim and item.exists:
            btn = _button("关闭休眠")
            btn.clicked.connect(self._disable_hibernation)
            lay.addWidget(btn, 0, Qt.AlignmentFlag.AlignTop)
        return box

    def _disable_hibernation(self) -> None:
        guard = self.ctx.guard
        if not guard.is_admin():
            QMessageBox.information(
                self, "需要管理员权限",
                "关闭休眠需要管理员权限。请点击左下角“以管理员身份重启”，然后再操作。\n\n"
                "也可以自己在“以管理员身份运行”的命令提示符中执行：powercfg -h off")
            return
        if not confirm(self, "关闭休眠", "将执行 powercfg -h off，删除休眠文件 hiberfil.sys。",
                       "影响：无法使用“休眠”，Windows 的“快速启动”也会随之关闭（开机可能稍慢）。"
                       "普通“睡眠”不受影响。以后可用 powercfg -h on 恢复。", yes_text="关闭休眠"):
            return
        if not self.gate_acquire("关闭休眠"):
            return
        run_task(guard.set_windows_hibernation, False, with_progress=False, name="hibernate",
                 on_success=self._on_hibernate, on_error=lambda e: show_exception(self, e),
                 on_done=self.ctx.gate.release)

    def _on_hibernate(self, result) -> None:
        ok, message = result
        if ok:
            QMessageBox.information(self, "已完成", message)
        else:
            QMessageBox.warning(self, "未能关闭休眠", message)
        self.refresh()


# ===========================================================================
# 2. 缓存清理
# ===========================================================================
class CachePage(PageBase):
    disk_changed = pyqtSignal()

    def __init__(self, ctx: AppContext):
        super().__init__(ctx, "缓存清理", "只匹配少数已核实的缓存位置，清理前请先关闭对应软件。")
        self.items: List[CacheItem] = []
        self.checked: Dict[str, bool] = {}
        self._valid = False      # 当前列表是否对应 cleaner 内有效的扫描快照
        self._scanning = False
        self._cleaning = False
        self._needs_rescan = False

        if ctx.cleaner is None:
            banner = Banner("warn")
            banner.show_text("缓存清理模块未加载，此页不可用。")
            self.content.addWidget(banner)
            self.content.addStretch(1)
            return

        self.scan_btn = _button("扫描缓存", "primaryButton")
        self.scan_btn.clicked.connect(self.scan)
        self.header_actions.addWidget(self.scan_btn)

        info = Banner("info")
        info.show_text("默认移入回收站：可以找回，但要清空回收站后才会真正腾出空间。"
                       "标记为“谨慎”的项目默认不勾选，请读完说明再决定。")
        self.content.addWidget(info)

        self.progress = ProgressPanel("已发现")
        self.content.addWidget(self.progress)

        self.tree = QTreeWidget()
        self.tree.setColumnCount(5)
        self.tree.setHeaderLabels(["项目", "风险", "体积", "文件数", "说明"])
        self.tree.setRootIsDecorated(True)
        self.tree.setUniformRowHeights(True)
        self.tree.setAlternatingRowColors(True)
        self.tree.setMinimumHeight(320)
        header = self.tree.header()
        header.setStretchLastSection(True)
        header.setDefaultAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.Interactive)
        self.tree.setColumnWidth(0, 270)
        self.tree.setColumnWidth(1, 74)
        self.tree.setColumnWidth(2, 96)
        self.tree.setColumnWidth(3, 70)
        self.tree.itemChanged.connect(self._on_item_changed)
        self.content.addWidget(self.tree, 1)

        self.empty_label = _label("点击右上角“扫描缓存”开始。扫描只读取，不会修改任何文件。", "muted")
        self.content.addWidget(self.empty_label)

        foot = QHBoxLayout()
        self.summary_label = _label("", "sectionTitle", wrap=False)
        self.errors_btn = _button("", "linkButton")
        self.errors_btn.clicked.connect(self._show_scan_errors)
        self.errors_btn.hide()
        foot.addWidget(self.summary_label)
        foot.addWidget(self.errors_btn)
        foot.addStretch(1)
        self.permanent_box = QCheckBox("永久删除（不经过回收站）")
        self.preview_btn = _button("预演所选")
        self.preview_btn.setToolTip("只计算会处理哪些文件，不删除任何东西")
        self.clean_btn = _button("清理所选", "primaryButton")
        self.preview_btn.clicked.connect(lambda: self.clean(dry_run=True))
        self.clean_btn.clicked.connect(lambda: self.clean(dry_run=False))
        self.permanent_box.toggled.connect(self._update_clean_button)
        foot.addWidget(self.permanent_box)
        foot.addWidget(self.preview_btn)
        foot.addWidget(self.clean_btn)
        self.content.addLayout(foot)
        self._update_actions()

    # -- scanning -----------------------------------------------------------
    def scan(self) -> None:
        if self._scanning or self._cleaning:
            return
        self._scanning = True
        self._valid = False
        self._update_actions()
        self.progress.bytes_caption = "已发现"
        self.progress.begin("正在扫描缓存…")
        run_task(self.ctx.cleaner.scan_caches, name="scan_caches",
                 on_progress=self.progress.update_progress, on_success=self._on_scanned,
                 on_error=self._on_scan_error, on_done=self._scan_done)

    def _scan_done(self) -> None:
        self._scanning = False
        self._update_actions()

    def _on_scan_error(self, exc) -> None:
        self.progress.hide()
        show_exception(self, exc, "扫描缓存失败。")

    def _on_scanned(self, items: List[CacheItem]) -> None:
        self.progress.finish("扫描完成")
        self.progress.hide()
        self.items = list(items)
        # 新扫描：勾选恢复为规则默认值（SAFE 且 selected_by_default）
        self.checked = {i.id: bool(i.selected_by_default and i.risk_level == RiskLevel.SAFE) for i in self.items}
        self._valid = True
        self._populate()
        errors = getattr(self.ctx.cleaner, "scan_errors", []) or []
        if errors:
            self.errors_btn.setText(f"{len(errors)} 个路径无法读取，已跳过")
            self.errors_btn.show()
        else:
            self.errors_btn.hide()
        if not self.items:
            self.empty_label.setText("没有找到可清理的匹配缓存。规则只覆盖少量已核实的位置，未匹配的软件会直接跳过。")
            self.empty_label.show()
        else:
            self.empty_label.hide()
        self._update_actions()

    def _populate(self) -> None:
        self.tree.blockSignals(True)
        self.tree.clear()
        groups: Dict[CacheCategory, QTreeWidgetItem] = {}
        for category in CacheCategory:
            members = [i for i in self.items if i.category == category]
            if not members:
                continue
            parent = QTreeWidgetItem(self.tree)
            parent.setText(0, CATEGORY_TEXT.get(category, category.value))
            parent.setText(2, format_bytes(sum(m.size_bytes for m in members)))
            parent.setText(3, str(sum(m.file_count for m in members)))
            for col in (2, 3):
                parent.setTextAlignment(col, Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            parent.setFlags(parent.flags() | Qt.ItemFlag.ItemIsUserCheckable | Qt.ItemFlag.ItemIsAutoTristate)
            font = parent.font(0)
            font.setBold(True)
            parent.setFont(0, font)
            groups[category] = parent
            for item in sorted(members, key=lambda m: m.size_bytes, reverse=True):
                child = QTreeWidgetItem(parent)
                child.setData(0, Qt.ItemDataRole.UserRole, item.id)
                child.setText(0, item.name)
                child.setFlags(child.flags() | Qt.ItemFlag.ItemIsUserCheckable)
                child.setCheckState(0, Qt.CheckState.Checked if self.checked.get(item.id) else Qt.CheckState.Unchecked)
                child.setText(2, item.size_human)
                child.setTextAlignment(2, Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
                child.setText(3, str(item.file_count))
                child.setTextAlignment(3, Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
                child.setText(4, item.description)
                tip = item.description + "\n\n匹配位置：\n" + "\n".join(item.matched_paths)
                for col in range(5):
                    child.setToolTip(col, tip)
                self.tree.setItemWidget(child, 1, self._badge_cell(item.risk_level))
            parent.setExpanded(True)
        self.tree.blockSignals(False)
        self._update_summary()

    @staticmethod
    def _badge_cell(risk: RiskLevel) -> QWidget:
        cell = QWidget()
        lay = QHBoxLayout(cell)
        lay.setContentsMargins(2, 0, 2, 0)
        lay.addWidget(RiskBadge(risk))
        lay.addStretch(1)
        return cell

    def _on_item_changed(self, tree_item: QTreeWidgetItem, column: int) -> None:
        if column != 0:
            return
        item_id = tree_item.data(0, Qt.ItemDataRole.UserRole)
        if item_id:
            self.checked[item_id] = tree_item.checkState(0) == Qt.CheckState.Checked
        else:  # 分组行：自动三态会同步子项，这里重新读一遍
            for idx in range(tree_item.childCount()):
                child = tree_item.child(idx)
                self.checked[child.data(0, Qt.ItemDataRole.UserRole)] = child.checkState(0) == Qt.CheckState.Checked
        self._update_summary()

    def selected_items(self) -> List[CacheItem]:
        return [i for i in self.items if self.checked.get(i.id)]

    def _update_summary(self) -> None:
        chosen = self.selected_items()
        if self.items:
            self.summary_label.setText(
                f"已选 {len(chosen)} 项 · {sum(i.file_count for i in chosen)} 个文件 · "
                f"{format_bytes(sum(i.size_bytes for i in chosen))}")
        else:
            self.summary_label.setText("")
        self._update_actions()

    def _update_clean_button(self) -> None:
        if self.permanent_box.isChecked():
            self.clean_btn.setText("永久删除所选")
            self.clean_btn.setObjectName("dangerButton")
        else:
            self.clean_btn.setText("移入回收站")
            self.clean_btn.setObjectName("primaryButton")
        self.clean_btn.style().unpolish(self.clean_btn)
        self.clean_btn.style().polish(self.clean_btn)

    def _update_actions(self) -> None:
        if self.ctx.cleaner is None:
            return
        busy = self._scanning or self._cleaning
        has_sel = bool(self.selected_items()) and self._valid
        self.scan_btn.setEnabled(not busy)
        self.scan_btn.setText("重新扫描" if self.items else "扫描缓存")
        self.tree.setEnabled(not busy)
        self.preview_btn.setEnabled(has_sel and not busy)
        self.clean_btn.setEnabled(has_sel and not busy)
        self.permanent_box.setEnabled(not busy)
        self._update_clean_button()

    def _show_scan_errors(self) -> None:
        errors = getattr(self.ctx.cleaner, "scan_errors", []) or []
        DetailsDialog("扫描时跳过的路径",
                      "这些路径无法读取（常见原因：正被软件占用、没有权限，或被安全护栏拦截），已跳过，不会被清理。",
                      errors, self).exec()

    # -- cleaning -----------------------------------------------------------
    def clean(self, dry_run: bool) -> None:
        chosen = self.selected_items()
        if not chosen or not self._valid:
            return
        permanent = self.permanent_box.isChecked()
        total = format_bytes(sum(i.size_bytes for i in chosen))
        files = sum(i.file_count for i in chosen)
        if not dry_run:
            cautious = [i.name for i in chosen if i.risk_level != RiskLevel.SAFE]
            extra = ("\n\n其中包含“谨慎”项目：" + "、".join(cautious) + "。请确认已读过说明。") if cautious else ""
            if permanent:
                ok = confirm(self, "永久删除", f"将永久删除 {files} 个文件（约 {total}），无法从回收站找回。",
                             "请先关闭浏览器和相关软件。扫描后有变化的文件会自动跳过。" + extra,
                             yes_text="永久删除", danger=True)
            else:
                ok = confirm(self, "移入回收站", f"将把 {files} 个文件（约 {total}）移入回收站。",
                             "请先关闭浏览器和相关软件。移入回收站后需清空回收站才会释放空间。" + extra,
                             yes_text="移入回收站")
            if not ok:
                return
            if not self.gate_acquire("清理缓存"):
                return
        self._cleaning = True
        self._update_actions()
        self.progress.bytes_caption = "可处理" if dry_run else "已处理"
        self.progress.begin("正在预演…" if dry_run else "正在清理…")

        def finished():
            self._cleaning = False
            if not dry_run:
                self.ctx.gate.release()
            self._update_actions()
            if self._needs_rescan:
                # 实际清理（或清理失败）后旧快照失效：自动重新扫描
                self._needs_rescan = False
                self.scan()

        run_task(self.ctx.cleaner.clean_selected, chosen, not permanent, dry_run, name="clean",
                 on_progress=self.progress.update_progress,
                 on_success=lambda s: self._on_cleaned(s),
                 on_error=lambda e: self._on_clean_error(e, dry_run),
                 on_done=finished)

    def _on_clean_error(self, exc, dry_run: bool) -> None:
        self.progress.hide()
        show_exception(self, exc, "预演失败。" if dry_run else "清理失败。")
        if not dry_run:
            # 执行失败时快照状态未知，保守地要求重新扫描
            self._valid = False
            self._needs_rescan = True

    def _on_cleaned(self, summary: CleanSummary) -> None:
        self.progress.hide()
        n, size = summary.cleaned_files_count, summary.cleaned_human
        if summary.dry_run:
            title = "预演结果"
            head = f"预演：会处理 {n} 个文件，约 {size}。\n这只是预演，没有删除任何文件。"
        elif summary.used_recycle_bin:
            title = "已移入回收站"
            head = f"已把 {n} 个文件（约 {size}）移入回收站。\n清空回收站后才会真正释放这部分磁盘空间。"
        else:
            title = "已永久删除"
            head = f"已永久删除 {n} 个文件，文件体积合计约 {size}。\n实际增加的可用空间可能略有不同。"
        notes = []
        if summary.skipped_locked_files:
            notes.append(f"{summary.skipped_locked_files} 个文件被占用或没有权限，已跳过。")
        other = len(summary.errors) - summary.skipped_locked_files
        if other > 0:
            notes.append(f"另有 {other} 个文件未处理（多为扫描后发生了变化），详见“显示详细信息”。")
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Icon.Information if not summary.errors else QMessageBox.Icon.Warning)
        box.setWindowTitle(title)
        box.setText(head)
        if notes:
            box.setInformativeText("\n".join(notes))
        if summary.errors:
            shown = summary.errors[:500]
            more = len(summary.errors) - len(shown)
            box.setDetailedText("\n".join(shown) + (f"\n…另有 {more} 条" if more > 0 else ""))
        box.exec()
        if not summary.dry_run:
            self._valid = False
            self._needs_rescan = True
            self.disk_changed.emit()


# ===========================================================================
# 3. 大文件猎手
# ===========================================================================
class LargeFilesPage(PageBase):
    def __init__(self, ctx: AppContext):
        super().__init__(ctx, "大文件猎手", "找出占地方的单个文件。这里只定位，不删除也不移动。")
        self.root = os.path.expanduser("~")
        self.results = []
        self._scanning = False

        self.scan_btn = _button("开始扫描", "primaryButton")
        self.scan_btn.clicked.connect(self.scan)
        self.header_actions.addWidget(self.scan_btn)

        controls = Card()
        row = QHBoxLayout()
        row.setSpacing(10)
        row.addWidget(_label("扫描位置", wrap=False))
        self.root_label = _label(self.root, "muted", wrap=False)
        self.root_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        row.addWidget(self.root_label, 1)
        change = _button("更换…")
        change.clicked.connect(self._choose_root)
        row.addWidget(change)
        row.addSpacing(16)
        row.addWidget(_label("不小于", wrap=False))
        self.threshold = QSpinBox()
        self.threshold.setRange(50, 100000)
        self.threshold.setSingleStep(100)
        self.threshold.setValue(500)
        self.threshold.setSuffix(" MB")
        row.addWidget(self.threshold)
        controls.body.addLayout(row)
        self.content.addWidget(controls)

        self.progress = ProgressPanel("已找到")
        self.content.addWidget(self.progress)

        self.table = _table(["文件名", "体积", "修改时间", "建议", "所在位置"])
        self.table.setSortingEnabled(True)
        self.table.setMinimumHeight(360)
        self.table.setColumnWidth(0, 230)
        self.table.setColumnWidth(1, 90)
        self.table.setColumnWidth(2, 130)
        self.table.setColumnWidth(3, 300)
        self.table.doubleClicked.connect(lambda *_: self._reveal())
        self.table.itemSelectionChanged.connect(self._update_actions)
        self.content.addWidget(self.table, 1)

        foot = QHBoxLayout()
        self.summary = _label("", "muted", wrap=False)
        foot.addWidget(self.summary)
        foot.addStretch(1)
        self.reveal_btn = _button("在资源管理器中显示")
        self.reveal_btn.clicked.connect(self._reveal)
        foot.addWidget(self.reveal_btn)
        self.content.addLayout(foot)
        self._update_actions()

    def _choose_root(self) -> None:
        path = QFileDialog.getExistingDirectory(self, "选择扫描位置", self.root)
        if path:
            self.root = os.path.normpath(path)
            self.root_label.setText(self.root)

    def _update_actions(self) -> None:
        self.scan_btn.setEnabled(not self._scanning)
        self.reveal_btn.setEnabled(bool(self.table.selectedItems()))

    def scan(self) -> None:
        if self._scanning:
            return
        self._scanning = True
        self._update_actions()
        self.progress.begin("正在扫描…（文件多时需要几分钟）")
        run_task(self.ctx.analyzer.scan_large_files, [self.root], self.threshold.value() * 1024 * 1024, 200,
                 name="large_files", on_progress=self.progress.update_progress,
                 on_success=self._on_results, on_error=lambda e: (self.progress.hide(), show_exception(self, e)),
                 on_done=self._done)

    def _done(self) -> None:
        self._scanning = False
        self._update_actions()

    def _on_results(self, results) -> None:
        self.progress.hide()
        self.results = results
        self.table.setSortingEnabled(False)
        self.table.setRowCount(len(results))
        for row, item in enumerate(results):
            name = QTableWidgetItem(item.name)
            name.setData(Qt.ItemDataRole.UserRole, item.path)
            name.setToolTip(item.path)
            self.table.setItem(row, 0, name)
            self.table.setItem(row, 1, SizeItem(item.size_bytes))
            mtime = datetime.fromtimestamp(item.modified_timestamp).strftime("%Y-%m-%d %H:%M")
            self.table.setItem(row, 2, QTableWidgetItem(mtime))
            tip = QTableWidgetItem(f"[{RISK_TEXT.get(item.risk_level, '')}] {item.suggestion}")
            tip.setToolTip(item.suggestion)
            self.table.setItem(row, 3, tip)
            loc = QTableWidgetItem(os.path.dirname(item.path))
            loc.setToolTip(item.path)
            self.table.setItem(row, 4, loc)
        self.table.setSortingEnabled(True)
        self.table.sortItems(1, Qt.SortOrder.DescendingOrder)
        total = sum(i.size_bytes for i in results)
        if results:
            self.summary.setText(f"找到 {len(results)} 个文件，合计 {format_bytes(total)}。双击可在资源管理器中定位。")
        else:
            self.summary.setText("没有找到达到阈值的文件。")
        self._update_actions()

    def _reveal(self) -> None:
        rows = {i.row() for i in self.table.selectedItems()}
        if not rows:
            return
        path = self.table.item(rows.pop(), 0).data(Qt.ItemDataRole.UserRole)
        reveal_in_explorer(path)


# ===========================================================================
# 4. 软件搬家
# ===========================================================================
class MigrationPage(PageBase):
    disk_changed = pyqtSignal()
    records_changed = pyqtSignal()

    def __init__(self, ctx: AppContext):
        super().__init__(ctx, "软件搬家", "把 C 盘上的大文件夹复制到其他盘，原位置留下目录联接，软件照常找得到。")
        self.tasks: List[MigrationTask] = []
        self._scanning = False
        self._moving = False

        if ctx.mover is None:
            banner = Banner("warn")
            banner.show_text("软件迁移模块未加载，此页不可用。")
            self.content.addWidget(banner)
            self.content.addStretch(1)
            return

        self.scan_btn = _button("扫描大文件夹", "primaryButton")
        self.scan_btn.clicked.connect(self.scan)
        self.header_actions.addWidget(self.scan_btn)

        warn = Banner("warn")
        warn.show_text(
            "只迁移你了解的普通软件或数据目录，并先关闭相关程序。系统组件、商店应用、驱动、服务，"
            "以及带自动更新或修复程序的软件可能迁移后失效 —— 软件自带“更改存储位置”功能时请优先用它。"
            "迁移会先完整复制并逐个校验，再切换目录；目标位置已存在同名文件夹时会拒绝。")
        self.content.addWidget(warn)

        self.dep_banner = Banner("danger")
        if not ctx.has_module("psutil"):
            self.dep_banner.show_text("缺少 psutil，无法检查哪些程序正在使用目录，迁移已被禁用。"
                                      "请执行 pip install psutil 后重启本工具。")
        self.content.addWidget(self.dep_banner)

        target_card = Card()
        row = QHBoxLayout()
        row.setSpacing(10)
        row.addWidget(_label("迁移到", wrap=False))
        self.target_combo = QComboBox()
        self.target_combo.setEditable(True)
        self.target_combo.setMinimumWidth(320)
        row.addWidget(self.target_combo, 1)
        browse = _button("选择…")
        browse.clicked.connect(self._choose_target)
        row.addWidget(browse)
        self.target_hint = _label("", "muted", wrap=False)
        row.addWidget(self.target_hint)
        target_card.body.addLayout(row)
        self.content.addWidget(target_card)
        self._fill_targets()

        self.progress = ProgressPanel("已发现")
        self.content.addWidget(self.progress)

        self.table = _table(["名称", "类别", "体积", "文件数", "状态", "位置"])
        self.table.setSortingEnabled(True)
        self.table.setMinimumHeight(320)
        self.table.setColumnWidth(0, 200)
        self.table.setColumnWidth(1, 200)
        self.table.setColumnWidth(2, 90)
        self.table.setColumnWidth(3, 70)
        self.table.setColumnWidth(4, 150)
        self.table.itemSelectionChanged.connect(self._update_actions)
        self.content.addWidget(self.table, 1)

        self.empty_label = _label(
            "“扫描大文件夹”会检查 Program Files、AppData、微信/QQ 数据目录下超过 200 MB 的文件夹，"
            "并检查是否有程序正在使用（需要几分钟）。也可以直接手动选择一个文件夹。", "muted")
        self.content.addWidget(self.empty_label)

        foot = QHBoxLayout()
        self.manual_btn = _button("手动选择文件夹…")
        self.manual_btn.clicked.connect(self._choose_source)
        foot.addWidget(self.manual_btn)
        foot.addStretch(1)
        self.move_btn = _button("迁移所选", "primaryButton")
        self.move_btn.clicked.connect(self.move_selected)
        foot.addWidget(self.move_btn)
        self.content.addLayout(foot)
        self._update_actions()

    # -- targets ------------------------------------------------------------
    def _fill_targets(self) -> None:
        current = self.target_combo.currentText()
        self.target_combo.clear()
        try:
            drives = self.ctx.analyzer.discover_target_drives(exclude_drive=self.ctx.analyzer.system_drive)
        except Exception:
            drives = []
        for mount, free in drives:
            self.target_combo.addItem(os.path.join(mount, "CDriveRescue_Moved"), mount)
            self.target_combo.setItemData(self.target_combo.count() - 1, f"可用 {format_bytes(free)}",
                                          Qt.ItemDataRole.ToolTipRole)
        if current:
            self.target_combo.setEditText(current)
        if drives:
            self.target_hint.setText(f"{drives[0][0]} 可用 {format_bytes(drives[0][1])}")
        else:
            self.target_hint.setText("未发现其他可用磁盘")

    def target_root(self) -> str:
        return self.target_combo.currentText().strip()

    def _choose_target(self) -> None:
        path = QFileDialog.getExistingDirectory(self, "选择迁移目标的父目录", self.target_root() or "")
        if path:
            self.target_combo.setEditText(os.path.normpath(path))

    # -- scanning -----------------------------------------------------------
    def _update_actions(self) -> None:
        if self.ctx.mover is None:
            return
        busy = self._scanning or self._moving
        task = self.selected_task()
        can_move = (task is not None and not task.is_already_junction and self.ctx.has_module("psutil"))
        self.scan_btn.setEnabled(not busy and self.ctx.has_module("psutil"))
        self.manual_btn.setEnabled(not busy)
        self.move_btn.setEnabled(can_move and not busy)

    def scan(self) -> None:
        if self._scanning or self._moving:
            return
        self._scanning = True
        self._update_actions()
        self.progress.bytes_caption = "已发现"
        self.progress.begin("正在扫描大文件夹并检查占用…")
        target = self.target_root() or r"D:\CDriveRescue_Moved"
        run_task(self.ctx.analyzer.discover_migratable_Directories, name="discover",
                 recommended_target_root=target, on_progress=self.progress.update_progress,
                 on_success=self._on_tasks, on_error=lambda e: (self.progress.hide(), show_exception(self, e)),
                 on_done=self._scan_done)

    def _scan_done(self) -> None:
        self._scanning = False
        self._update_actions()

    def _on_tasks(self, tasks: List[MigrationTask]) -> None:
        self.progress.hide()
        manual = [t for t in self.tasks if t.app_id.startswith("manual_")]
        self.tasks = manual + [t for t in tasks if not any(
            os.path.normcase(t.source_path) == os.path.normcase(m.source_path) for m in manual)]
        self._populate()
        if not self.tasks:
            self.empty_label.setText("没有发现超过 200 MB 的可迁移文件夹。可以手动选择一个文件夹。")
            self.empty_label.show()

    def _populate(self) -> None:
        self.table.setSortingEnabled(False)
        self.table.setRowCount(len(self.tasks))
        for row, task in enumerate(self.tasks):
            name = QTableWidgetItem(task.app_name)
            name.setData(Qt.ItemDataRole.UserRole, task.app_id)
            self.table.setItem(row, 0, name)
            self.table.setItem(row, 1, QTableWidgetItem(task.category_label))
            self.table.setItem(row, 2, SizeItem(task.size_bytes))
            count = QTableWidgetItem(str(task.file_count))
            count.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            self.table.setItem(row, 3, count)
            status = task.status
            text = STATUS_TEXT.get(status, status.value)
            tip = ""
            if status == MigrationStatus.LOCKED:
                names = sorted({l.name for l in task.running_locks})
                text = "被占用：" + "、".join(names[:2]) + ("…" if len(names) > 2 else "")
                tip = "扫描时这些程序正在使用该目录，请关闭后再迁移：\n" + "\n".join(
                    f"{l.name} (PID {l.pid})" for l in task.running_locks)
            elif status == MigrationStatus.MOVED:
                text = "已迁移"
                tip = f"已是目录联接 → {task.junction_target or '未知'}"
            status_item = QTableWidgetItem(text)
            if tip:
                status_item.setToolTip(tip)
            self.table.setItem(row, 4, status_item)
            loc = QTableWidgetItem(task.source_path)
            loc.setToolTip(task.source_path)
            self.table.setItem(row, 5, loc)
        self.table.setSortingEnabled(True)
        self.empty_label.setVisible(not self.tasks)
        self._update_actions()

    def selected_task(self) -> Optional[MigrationTask]:
        if self.ctx.mover is None:
            return None
        rows = {i.row() for i in self.table.selectedItems()}
        if not rows:
            return None
        app_id = self.table.item(rows.pop(), 0).data(Qt.ItemDataRole.UserRole)
        return next((t for t in self.tasks if t.app_id == app_id), None)

    # -- manual source ------------------------------------------------------
    def _choose_source(self) -> None:
        path = QFileDialog.getExistingDirectory(self, "选择要迁移的文件夹")
        if not path:
            return
        path = os.path.normpath(path)
        target = os.path.join(self.target_root() or r"D:\CDriveRescue_Moved", os.path.basename(path))
        try:
            self.ctx.guard.validate_for_migration(path, target)
        except Exception as exc:  # 安全护栏拦截
            show_exception(self, exc)
            return
        self._scanning = True
        self._update_actions()
        self.progress.begin("正在统计文件夹体积…")
        run_task(self.ctx.analyzer.calculate_dir_stats, path, with_progress=False, name="dir_stats",
                 on_success=lambda stats: self._add_manual(path, stats),
                 on_error=lambda e: show_exception(self, e), on_done=self._manual_done)

    def _manual_done(self) -> None:
        self.progress.hide()
        self._scanning = False
        self._update_actions()

    def _add_manual(self, path: str, stats) -> None:
        size, count = stats
        is_junc = bool(self.ctx.guard.is_junction_or_symlink(path))
        task = MigrationTask(f"manual_{abs(hash(os.path.normcase(path))) % 10**8}", os.path.basename(path), path,
                             self.target_root(), size, count, "手动选择", is_already_junction=is_junc,
                             junction_target=self.ctx.guard.resolve_junction_target(path) if is_junc else None)
        self.tasks = [task] + [t for t in self.tasks if os.path.normcase(t.source_path) != os.path.normcase(path)]
        self._populate()
        for row in range(self.table.rowCount()):
            if self.table.item(row, 0).data(Qt.ItemDataRole.UserRole) == task.app_id:
                self.table.selectRow(row)
                break

    # -- move ---------------------------------------------------------------
    def move_selected(self) -> None:
        task = self.selected_task()
        if task is None or task.is_already_junction:
            return
        root = self.target_root()
        if not root or not os.path.isabs(root):
            QMessageBox.warning(self, "请选择目标", "请填写或选择一个目标目录（完整路径，例如 D:\\CDriveRescue_Moved）。")
            return
        final = os.path.join(root, os.path.basename(task.source_path))
        if os.path.lexists(final):
            QMessageBox.warning(self, "目标已存在", f"目标位置已有同名文件夹：\n{final}\n\n为避免覆盖，请换一个目标目录。")
            return
        notes = [
            f"从：{task.source_path}",
            f"到：{final}",
            f"体积：约 {task.size_human}（{task.file_count} 个文件）" if task.size_bytes else "体积：迁移时统计",
            "",
            "• 请先关闭这个软件及所有可能用到这些文件的程序。",
            "• 过程：完整复制 → 逐个文件 SHA-256 校验 → 原文件夹换成目录联接 → 删除 C 盘原件。",
            "• 文件很多时可能需要较长时间，期间请不要关机。",
        ]
        if same_volume(task.source_path, root):
            notes.insert(4, "⚠ 目标与源在同一个磁盘上，迁移不会释放这个盘的空间。")
        if task.status == MigrationStatus.LOCKED:
            notes.insert(4, "⚠ 扫描时检测到程序正在使用该目录，请确认已关闭。")
        if not confirm(self, "确认迁移", f"迁移“{task.app_name}”？", "\n".join(notes), yes_text="开始迁移"):
            return
        if not self.gate_acquire(f"迁移 {task.app_name}"):
            return
        self._moving = True
        self._update_actions()
        self.progress.bytes_caption = "已复制"
        self.progress.begin("正在校验与复制…")

        def finished():
            self._moving = False
            self.ctx.gate.release()
            self.progress.hide()
            self._update_actions()
            self.records_changed.emit()

        run_task(self.ctx.mover.move_app_atomic, task, root, name="move",
                 on_progress=self.progress.update_progress,
                 on_success=lambda rec: self._on_moved(task, rec),
                 on_error=lambda e: (show_exception(self, e, f"迁移“{task.app_name}”没有完成。"),
                                     self.disk_changed.emit()),
                 on_done=finished)

    def _on_moved(self, task: MigrationTask, record: MigrationRecord) -> None:
        task.is_already_junction = True
        task.junction_target = record.target_path
        task.running_locks = []
        self._populate()
        self.disk_changed.emit()
        if record.error_message:
            QMessageBox.warning(self, "迁移已生效，但有收尾事项",
                                f"“{record.app_name}”已迁移到：\n{record.target_path}\n\n{record.error_message}\n\n"
                                "C 盘上可能暂时保留了备份文件夹（名称含 ._cdr_backup_），确认软件正常后再处理。")
        else:
            QMessageBox.information(self, "迁移完成",
                                    f"“{record.app_name}”已迁移到：\n{record.target_path}\n\n"
                                    f"原位置 {record.source_path} 现在是指向新位置的目录联接。"
                                    "请打开软件确认一切正常；如有问题，可在“迁移记录”页还原。")


# ===========================================================================
# 5. 迁移记录
# ===========================================================================
class RecordsPage(PageBase):
    disk_changed = pyqtSignal()
    pending_changed = pyqtSignal(bool)

    def __init__(self, ctx: AppContext):
        super().__init__(ctx, "迁移记录", "已迁移的文件夹、联接是否正常，以及还原入口。")
        self.records: List[MigrationRecord] = []
        self._loading = False
        self._busy = False
        self.loaded_once = False

        if ctx.mover is None:
            banner = Banner("warn")
            banner.show_text("软件迁移模块未加载，此页不可用。")
            self.content.addWidget(banner)
            self.content.addStretch(1)
            return

        self.refresh_btn = _button("刷新")
        self.refresh_btn.clicked.connect(self.refresh)
        self.header_actions.addWidget(self.refresh_btn)

        self.pending_banner = Banner("danger")
        self.recover_btn = _button("检查并恢复", "dangerButton")
        self.recover_btn.clicked.connect(self.recover)
        self.pending_banner.add_button(self.recover_btn)
        self.content.addWidget(self.pending_banner)

        self.progress = ProgressPanel("已复制")
        self.content.addWidget(self.progress)

        self.table = _table(["软件", "状态", "原位置", "实际存放位置", "体积", "迁移时间", "说明"])
        self.table.setMinimumHeight(340)
        for col, width in enumerate((130, 80, 200, 200, 80, 120)):
            self.table.setColumnWidth(col, width)
        self.table.itemSelectionChanged.connect(self._update_actions)
        self.content.addWidget(self.table, 1)
        self.empty_label = _label("还没有迁移记录。", "muted")
        self.content.addWidget(self.empty_label)

        foot = QHBoxLayout()
        foot.addWidget(_label("还原会把文件复制回原位置；完成后目标盘上的副本会保留，确认软件正常后可自行删除。",
                              "muted"), 1)
        self.open_btn = _button("打开存放位置")
        self.open_btn.clicked.connect(self._open_target)
        self.restore_btn = _button("还原到原位置", "primaryButton")
        self.restore_btn.clicked.connect(self.restore_selected)
        foot.addWidget(self.open_btn)
        foot.addWidget(self.restore_btn)
        self.content.addLayout(foot)
        self._update_actions()

    def showEvent(self, event):  # noqa: N802
        super().showEvent(event)
        if not self.loaded_once and self.ctx.mover is not None:
            self.refresh()

    # -- data ---------------------------------------------------------------
    def refresh(self) -> None:
        if self.ctx.mover is None or self._loading:
            return
        self.loaded_once = True
        self._loading = True
        self._update_actions()
        run_task(self.ctx.mover.list_migration_records, with_progress=False, name="records",
                 on_success=self._on_records,
                 on_error=lambda e: show_exception(self, e, "读取迁移记录失败。为保护数据，记录文件不会被覆盖。"),
                 on_done=self._loaded)
        self.check_pending()

    def _loaded(self) -> None:
        self._loading = False
        self._update_actions()

    def check_pending(self) -> bool:
        pending = self.ctx.pending_transaction()
        if self.ctx.mover is not None:
            if pending:
                self.pending_banner.show_text(
                    "检测到上次未完成的迁移或还原（可能是中途关机或程序异常退出）。处理前不能进行新的迁移或还原。"
                    "“检查并恢复”会依据事务日志把路径恢复到操作前的状态，不会删除任何未经验证的副本。")
            else:
                self.pending_banner.hide()
        self.pending_changed.emit(pending)
        return pending

    def _on_records(self, records: List[MigrationRecord]) -> None:
        self.records = sorted(records, key=lambda r: r.created_at, reverse=True)
        self.table.setRowCount(len(self.records))
        for row, rec in enumerate(self.records):
            name = QTableWidgetItem(rec.app_name)
            name.setData(Qt.ItemDataRole.UserRole, rec.record_id)
            self.table.setItem(row, 0, name)
            status = QTableWidgetItem(STATUS_TEXT.get(rec.status, rec.status.value))
            if rec.status == MigrationStatus.BROKEN:
                status.setForeground(Qt.GlobalColor.darkRed)
            self.table.setItem(row, 1, status)
            for col, value in ((2, rec.source_path), (3, rec.target_path)):
                cell = QTableWidgetItem(value)
                cell.setToolTip(value)
                self.table.setItem(row, col, cell)
            self.table.setItem(row, 4, SizeItem(rec.size_bytes))
            self.table.setItem(row, 5, QTableWidgetItem(_local_time(rec.created_at)))
            note = QTableWidgetItem(rec.error_message or "")
            note.setToolTip(rec.error_message or "")
            self.table.setItem(row, 6, note)
        self.empty_label.setVisible(not self.records)
        self._update_actions()

    def selected_record(self) -> Optional[MigrationRecord]:
        rows = {i.row() for i in self.table.selectedItems()}
        if not rows:
            return None
        rid = self.table.item(rows.pop(), 0).data(Qt.ItemDataRole.UserRole)
        return next((r for r in self.records if r.record_id == rid), None)

    def _update_actions(self) -> None:
        if self.ctx.mover is None:
            return
        rec = self.selected_record()
        busy = self._loading or self._busy
        self.refresh_btn.setEnabled(not busy)
        self.recover_btn.setEnabled(not busy)
        self.open_btn.setEnabled(rec is not None and os.path.isdir(rec.target_path))
        self.restore_btn.setEnabled(rec is not None and rec.status == MigrationStatus.MOVED and not busy
                                    and self.ctx.has_module("psutil"))

    def _open_target(self) -> None:
        rec = self.selected_record()
        if rec:
            reveal_in_explorer(rec.target_path)

    # -- restore ------------------------------------------------------------
    def restore_selected(self) -> None:
        rec = self.selected_record()
        if rec is None or rec.status != MigrationStatus.MOVED:
            return
        if self.check_pending():
            QMessageBox.warning(self, "请先处理未完成事务", "存在未完成的事务，请先点击“检查并恢复”。")
            return
        if not confirm(self, "还原到原位置", f"把“{rec.app_name}”还原回 {rec.source_path}？",
                       f"• 会把 {rec.size_human} 文件从 {rec.target_path} 复制回原位置，C 盘需要有足够的空余空间。\n"
                       "• 请先关闭这个软件。\n"
                       f"• 完成后 {rec.target_path} 上的副本会保留，确认软件正常后可以自行删除以释放空间。",
                       yes_text="开始还原"):
            return
        if not self.gate_acquire(f"还原 {rec.app_name}"):
            return
        self._busy = True
        self._update_actions()
        self.progress.begin("正在复制回原位置…")

        def finished():
            self._busy = False
            self.ctx.gate.release()
            self.progress.hide()
            self.disk_changed.emit()
            self.refresh()

        run_task(self.ctx.mover.restore_app, rec.record_id, name="restore",
                 on_progress=self.progress.update_progress, on_success=self._on_restored,
                 on_error=lambda e: show_exception(self, e, f"还原“{rec.app_name}”没有完成。"),
                 on_done=finished)

    def _on_restored(self, record: MigrationRecord) -> None:
        QMessageBox.information(self, "还原完成",
                                f"“{record.app_name}”已还原到：\n{record.source_path}\n\n"
                                f"目标盘副本仍保留在：\n{record.target_path}\n确认软件正常后，可以自行删除它以释放空间。"
                                + (f"\n\n{record.error_message}" if record.error_message and "残留" in record.error_message else ""))

    # -- recover ------------------------------------------------------------
    def recover(self) -> None:
        if not confirm(self, "检查并恢复未完成事务",
                       "根据事务日志检查上次中断的迁移或还原，并把路径恢复到操作前的状态。",
                       "不会删除任何未经验证的副本；如果状态有歧义，会保留全部文件，交给你人工检查。\n"
                       "请先关闭相关软件。", yes_text="开始检查"):
            return
        if not self.gate_acquire("恢复未完成事务"):
            return
        self._busy = True
        self._update_actions()

        def finished():
            self._busy = False
            self.ctx.gate.release()
            self.disk_changed.emit()
            self.refresh()

        run_task(self.ctx.mover.recover_pending, with_progress=False, name="recover",
                 on_success=lambda msg: QMessageBox.information(self, "检查完成", str(msg)),
                 on_error=lambda e: show_exception(self, e, "恢复没有完成。"), on_done=finished)
