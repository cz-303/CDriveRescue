"""
深水区瘦身实验室 (v1.1 Lab) —— Claude
====================================
三个子面板：
A. 微信/QQ 重复文件 → NTFS 硬链接无损去重（DuplicateHardlinkWorker）
B. Windows 个人文件夹（下载/桌面/文档/视频/图片）原生搬家（KnownFolderMoveWorker）
C. AppData 卸载残留孤儿猎手（OrphanScanWorker / OrphanRecycleWorker）

接入约定：
- 扫描与写操作全部在 QThread 中执行；写操作经 OperationGate 串行化，进行中主窗口拒绝关闭。
- 去重只把最近一次扫描返回的原始 DuplicateGroup 对象交给 SystemOptimizer。
- “预演”不显示为已释放；去重后的空间立即释放（不经过回收站）；残留目录移入回收站后需清空回收站才释放。
- 残留目录不会默认勾选或批量删除：逐个复核（路径、深层文件最近改动、进程占用）后再确认。
- SystemOptimizer 未加载时，面板 A 仍可扫描，写操作按钮禁用并说明原因。
"""
from __future__ import annotations

import ntpath
import os
from pathlib import Path
from typing import Dict, List, Optional

from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtWidgets import (
    QAbstractItemView, QComboBox, QFileDialog, QGridLayout, QHBoxLayout, QHeaderView, QLabel,
    QLineEdit, QMessageBox, QPushButton, QSpinBox, QStackedWidget, QTableWidgetItem, QTreeWidget,
    QTreeWidgetItem, QVBoxLayout, QWidget,
)

from src.core.contracts import RiskLevel, format_bytes
from src.ui.pages import AppContext, PageBase, SizeItem, _button, _label, _table
from src.ui.widgets import (
    Banner, Card, DetailsDialog, Pill, ProgressPanel, SegmentedTabs, cell, confidence_pill, confirm,
    reveal_in_explorer, show_exception,
)
from src.ui.workers import (
    BackendThread, DuplicateHardlinkWorker, KnownFolderMoveWorker, OrphanRecycleWorker,
    OrphanScanWorker, start_thread,
)


def _default_dup_roots(analyzer=None) -> List[str]:
    """默认扫描范围：由分析器从 Windows 实际的“文档/下载”位置派生；旧版分析器回退为主目录拼接。"""
    getter = getattr(analyzer, "default_duplicate_scan_roots", None)
    if callable(getter):
        try:
            return list(getter())
        except Exception:
            return []
    home = os.path.expanduser("~")
    return [os.path.join(home, "Documents", "WeChat Files"),
            os.path.join(home, "Documents", "Tencent Files"),
            os.path.join(home, "Downloads")]


def _onedrive_roots() -> List[str]:
    roots = []
    for name in ("OneDrive", "OneDriveConsumer", "OneDriveCommercial"):
        value = os.environ.get(name)
        if value:
            roots.append(ntpath.normcase(ntpath.normpath(value)))
    return roots


def _under_onedrive(path: str) -> bool:
    norm = ntpath.normcase(ntpath.normpath(path or ""))
    return any(norm == r or norm.startswith(r.rstrip("\\") + "\\") for r in _onedrive_roots())


def _drive_of(path: str) -> str:
    """Windows 路径的盘符字母（"D:\\x" → "D"）；无盘符时返回空字符串。"""
    drive = ntpath.splitdrive(path or "")[0]
    return drive[0].upper() if len(drive) == 2 and drive[1] == ":" else ""


class _Panel(QWidget):
    """子面板基类：共享上下文、闸门与线程管理。"""

    disk_changed = pyqtSignal()

    def __init__(self, ctx: AppContext):
        super().__init__()
        self.ctx = ctx
        self.lay = QVBoxLayout(self)
        self.lay.setContentsMargins(0, 0, 0, 0)
        self.lay.setSpacing(14)
        self._threads: List[BackendThread] = []

    def _run(self, thread: BackendThread, *, on_success=None, on_error=None, on_progress=None, on_done=None):
        self._threads.append(thread)

        def done():
            if thread in self._threads:
                self._threads.remove(thread)
            if on_done:
                on_done()
        return start_thread(thread, on_success=on_success, on_error=on_error, on_progress=on_progress,
                            on_done=done)

    def gate_acquire(self, name: str) -> bool:
        if self.ctx.gate.acquire(name):
            return True
        QMessageBox.information(self, "请稍候", f"“{self.ctx.gate.current}”正在进行，完成后再试。")
        return False

    @property
    def busy(self) -> bool:
        # 线程结束到结果送达界面之间仍视为忙碌：线程在 on_done（finished 信号）处理后才移出列表
        return bool(self._threads)


# ===========================================================================
# 面板 A：重复文件硬链接去重
# ===========================================================================
class DedupePanel(_Panel):
    def __init__(self, ctx: AppContext):
        super().__init__(ctx)
        self.groups = []                  # 最近一次扫描的原始 DuplicateGroup
        self.checked: Dict[str, bool] = {}
        self._valid = False
        self.roots = _default_dup_roots(self.ctx.analyzer)

        info = Banner("info")
        info.show_text(
            "微信/QQ 里同一个视频、PDF、压缩包被转发几次，C 盘就存几份。这里把内容完全相同的副本换成 NTFS 硬链接："
            "每个文件仍在原位置、原文件名，聊天记录里照常能打开，但磁盘上只占一份。"
            "注意：合并后这些位置共享同一份数据——以后直接修改并保存其中一个，其他位置也会一起变；删除其中一个不影响其他。"
            "只适合收到后不再修改的附件。数据库、配置、日志类文件会被自动排除。")
        self.lay.addWidget(info)

        self.opt_banner = Banner("warn")
        if ctx.optimizer is None:
            self.opt_banner.show_text("系统优化引擎 (SystemOptimizer) 未加载：可以扫描查看，但不能执行去重。")
        self.lay.addWidget(self.opt_banner)

        controls = Card()
        row = QHBoxLayout()
        row.setSpacing(10)
        row.addWidget(_label("扫描范围", wrap=False))
        self.roots_label = _label("", "muted")
        row.addWidget(self.roots_label, 1)
        add_btn = _button("添加目录…")
        add_btn.clicked.connect(self._add_root)
        reset_btn = _button("恢复默认")
        reset_btn.clicked.connect(self._reset_roots)
        row.addWidget(add_btn)
        row.addWidget(reset_btn)
        row.addSpacing(12)
        row.addWidget(_label("文件不小于", wrap=False))
        self.min_size = QSpinBox()
        self.min_size.setRange(1, 4096)
        self.min_size.setValue(1)
        self.min_size.setSuffix(" MB")
        row.addWidget(self.min_size)
        self.scan_btn = _button("扫描重复文件", "primaryButton")
        self.scan_btn.clicked.connect(self.scan)
        row.addWidget(self.scan_btn)
        controls.body.addLayout(row)
        self.lay.addWidget(controls)
        self._show_roots()

        self.progress = ProgressPanel("可省")
        self.lay.addWidget(self.progress)

        self.tree = QTreeWidget()
        self.tree.setColumnCount(5)
        self.tree.setHeaderLabels(["文件", "副本数", "单个大小", "可省空间", "来源"])
        self.tree.setAlternatingRowColors(True)
        self.tree.setUniformRowHeights(True)
        self.tree.setMinimumHeight(300)
        header = self.tree.header()
        header.setDefaultAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        header.setStretchLastSection(True)
        self.tree.setColumnWidth(0, 380)
        self.tree.setColumnWidth(1, 70)
        self.tree.setColumnWidth(2, 96)
        self.tree.setColumnWidth(3, 120)
        self.tree.itemChanged.connect(self._on_item_changed)
        self.tree.itemDoubleClicked.connect(self._reveal)
        self.lay.addWidget(self.tree, 1)

        self.empty = _label("点击“扫描重复文件”开始。扫描只读取文件，会对同样大小的文件计算校验值，文件多时需要几分钟。", "muted")
        self.lay.addWidget(self.empty)

        foot = QHBoxLayout()
        self.summary = _label("", "sectionTitle", wrap=False)
        foot.addWidget(self.summary)
        foot.addStretch(1)
        self.preview_btn = _button("预演")
        self.preview_btn.setToolTip("逐个校验并计算可释放的空间，不修改任何文件")
        self.preview_btn.clicked.connect(lambda: self.dedupe(dry_run=True))
        self.link_btn = _button("一键转 NTFS 硬链接（不伤聊天记录）", "primaryButton")
        self.link_btn.clicked.connect(lambda: self.dedupe(dry_run=False))
        foot.addWidget(self.preview_btn)
        foot.addWidget(self.link_btn)
        self.lay.addLayout(foot)
        self._update_actions()

    # -- scan roots -----------------------------------------------------------
    def _show_roots(self) -> None:
        lines = []
        for r in self.roots:
            lines.append(r + ("" if os.path.isdir(r) else "（不存在，跳过）"))
        self.roots_label.setText("\n".join(lines))

    def _add_root(self) -> None:
        path = QFileDialog.getExistingDirectory(self, "添加扫描目录")
        if path:
            path = os.path.normpath(path)
            if os.path.normcase(path) not in {os.path.normcase(r) for r in self.roots}:
                self.roots.append(path)
                self._show_roots()

    def _reset_roots(self) -> None:
        self.roots = _default_dup_roots(self.ctx.analyzer)
        self._show_roots()

    # -- scanning -------------------------------------------------------------
    def scan(self) -> None:
        if self.busy:
            return
        self._valid = False
        self.progress.begin("正在查找重复文件…")
        thread = DuplicateHardlinkWorker(self.ctx.analyzer, self.ctx.optimizer, mode=DuplicateHardlinkWorker.SCAN,
                                         scan_roots=list(self.roots), min_file_size=self.min_size.value() * 1024 * 1024)
        self._run(thread, on_progress=self.progress.update_progress, on_success=self._on_groups,
                  on_error=lambda e: (self.progress.hide(), show_exception(self, e, "扫描重复文件失败。")),
                  on_done=self._update_actions)
        self._update_actions()

    def _on_groups(self, groups) -> None:
        self.progress.hide()
        self.groups = list(groups)
        self.checked = {g.group_id: True for g in self.groups}
        self._valid = True
        self._populate()
        if self.groups:
            self.empty.hide()
        else:
            self.empty.setText("没有发现可合并的重复文件（已排除数据库/配置/日志类文件和已经是硬链接的文件）。")
            self.empty.show()

    def _populate(self) -> None:
        self.tree.blockSignals(True)
        self.tree.clear()
        for g in self.groups:
            top = QTreeWidgetItem(self.tree)
            top.setData(0, Qt.ItemDataRole.UserRole, g.group_id)
            top.setText(0, g.file_name_sample)
            top.setToolTip(0, f"SHA-256: {g.content_hash}")
            top.setFlags(top.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            top.setCheckState(0, Qt.CheckState.Checked if self.checked.get(g.group_id) else Qt.CheckState.Unchecked)
            top.setText(1, f"{g.total_copies} 份")
            top.setText(2, format_bytes(g.single_size_bytes))
            top.setText(4, g.source_app)
            font = top.font(0)
            font.setBold(True)
            top.setFont(0, font)
            self.tree.setItemWidget(top, 3, cell(Pill(f"可省 {g.savable_human}", "safe")))
            for role, path in [("保留实体", g.master_path)] + [("副本 → 硬链接", p) for p in g.duplicate_paths]:
                child = QTreeWidgetItem(top)
                child.setText(0, path)
                child.setToolTip(0, path)
                child.setText(1, role)
                child.setData(0, Qt.ItemDataRole.UserRole + 2, path)
        self.tree.blockSignals(False)
        self._update_summary()

    def _on_item_changed(self, item: QTreeWidgetItem, column: int) -> None:
        gid = item.data(0, Qt.ItemDataRole.UserRole)
        if column == 0 and gid:
            self.checked[gid] = item.checkState(0) == Qt.CheckState.Checked
            self._update_summary()

    def _reveal(self, item: QTreeWidgetItem, _col: int) -> None:
        path = item.data(0, Qt.ItemDataRole.UserRole + 2)
        if path:
            reveal_in_explorer(path)

    def selected_groups(self):
        return [g for g in self.groups if self.checked.get(g.group_id)]

    def _update_summary(self) -> None:
        chosen = self.selected_groups()
        if self.groups:
            copies = sum(len(g.duplicate_paths) for g in chosen)
            self.summary.setText(f"已选 {len(chosen)} 组 · {copies} 个副本 · 可省 "
                                 f"{format_bytes(sum(g.savable_bytes for g in chosen))}")
        self._update_actions()

    def _update_actions(self) -> None:
        busy = self.busy
        can = bool(self.selected_groups()) and self._valid and not busy and self.ctx.optimizer is not None
        self.scan_btn.setEnabled(not busy)
        self.scan_btn.setText("重新扫描" if self.groups else "扫描重复文件")
        self.tree.setEnabled(not busy)
        self.preview_btn.setEnabled(can)
        self.link_btn.setEnabled(can)

    # -- dedupe ---------------------------------------------------------------
    def dedupe(self, dry_run: bool) -> None:
        chosen = self.selected_groups()
        if not chosen or not self._valid or self.ctx.optimizer is None:
            return
        copies = sum(len(g.duplicate_paths) for g in chosen)
        total = format_bytes(sum(g.savable_bytes for g in chosen))
        if not dry_run:
            if not confirm(self, "转为 NTFS 硬链接",
                           f"把 {copies} 个重复副本换成硬链接，预计释放 {total}？",
                           "• 每个文件都保留原路径和文件名，微信/QQ 里照常能打开。\n"
                           "• 合并后共享同一份数据：以后直接修改并保存其中任何一个，其他位置会一起改变；"
                           "删除其中一个不影响其他。\n"
                           "• 请先完全退出微信和 QQ。\n"
                           "• 每个副本合并前会再做一次全文 SHA-256 校验；单个文件出错时会尝试还原，"
                           "还原不完整会明确提示并保留以 ._hl_bak 结尾的备份，不影响其他文件。\n"
                           "• 释放的空间立即生效，不经过回收站。",
                           yes_text="开始合并"):
                return
            if not self.gate_acquire("硬链接去重"):
                return
        self.progress.begin("正在预演…" if dry_run else "正在合并…")

        def finished():
            if not dry_run:
                self.ctx.gate.release()
            self.progress.hide()
            self._update_actions()

        thread = DuplicateHardlinkWorker(self.ctx.analyzer, self.ctx.optimizer, mode=DuplicateHardlinkWorker.DEDUPE,
                                         groups=chosen, dry_run=dry_run)
        self._run(thread, on_progress=self.progress.update_progress,
                  on_success=lambda s: self._on_summary(s, dry_run),
                  on_error=lambda e: (show_exception(self, e, "去重没有完成。"),
                                      None if dry_run else self._mark_stale()),
                  on_done=finished)
        self._update_actions()

    def _mark_stale(self) -> None:
        self._valid = False
        self.groups = []
        self.checked = {}
        self.tree.clear()
        self.summary.setText("")
        self.empty.setText("文件已有变动，请重新扫描查看剩余的重复项。")
        self.empty.show()
        self.disk_changed.emit()

    def _on_summary(self, s, dry_run: bool) -> None:
        severe = [e for e in s.errors if "回滚未完成" in e or "人工检查" in e]
        if dry_run:
            title = "预演结果"
            head = f"预演：可以合并 {s.linked_files_count} 个副本，预计释放 {s.saved_human}。\n这只是预演，没有修改任何文件。"
        else:
            title = "去重完成"
            head = f"已把 {s.linked_files_count} 个副本换成硬链接，释放 {s.saved_human}（已立即生效）。"
        notes = []
        if s.skipped_cross_volume:
            notes.append(f"{s.skipped_cross_volume} 个副本与保留的文件不在同一个磁盘，硬链接不能跨盘，已跳过。")
        other = len(s.errors) - len(severe)
        if other > 0:
            notes.append(f"{other} 个副本未处理（常见原因：文件正被占用、只读、权限不同或扫描后发生变化），详见“显示详细信息”。")
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Icon.Critical if severe else
                    (QMessageBox.Icon.Warning if s.errors else QMessageBox.Icon.Information))
        box.setWindowTitle(title)
        box.setText(head)
        if severe:
            notes.insert(0, f"⚠ 有 {len(severe)} 个文件自动还原没有完成。原件已以 ._hl_bak 结尾保留在原文件夹，"
                            "请不要删除它们，并找熟悉的人协助检查。")
        if notes:
            box.setInformativeText("\n".join(notes))
        if s.errors:
            box.setDetailedText("\n".join(s.errors[:500]) + (f"\n…另有 {len(s.errors) - 500} 条" if len(s.errors) > 500 else ""))
        box.exec()
        if not dry_run:
            self._mark_stale()


# ===========================================================================
# 面板 B：个人文件夹原生搬家
# ===========================================================================
class FolderCard(Card):
    move_requested = pyqtSignal(object, str)

    def __init__(self, item, can_move: bool, reason_disabled: str = ""):
        super().__init__()
        self.item = item
        self.setObjectName("folderCard")
        head = QHBoxLayout()
        title = _label(item.display_name, "cardTitle", wrap=False)
        head.addWidget(title)
        head.addStretch(1)
        if item.is_on_system_drive:
            head.addWidget(Pill("在系统盘", "caution"))
        else:
            head.addWidget(Pill(f"已在 {_drive_of(item.current_path) or '其他'} 盘", "safe"))
        self.body.addLayout(head)
        self.body.addWidget(_label(f"{item.size_human} · {item.file_count} 个文件", "statValueSmall", wrap=False))
        cur = _label(item.current_path, "muted")
        cur.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.body.addWidget(cur)
        row = QHBoxLayout()
        row.addWidget(_label("迁移到", wrap=False))
        self.target = QLineEdit(item.recommended_target_path)
        row.addWidget(self.target, 1)
        self.body.addLayout(row)
        self.move_btn = _button(f"搬到 {_drive_of(item.recommended_target_path) or '目标'} 盘", "primaryButton")
        onedrive = _under_onedrive(item.current_path)
        self.move_btn.setEnabled(can_move and item.is_on_system_drive and not onedrive)
        if onedrive:
            self.move_btn.setToolTip("这个文件夹由 OneDrive 管理，请在 OneDrive 设置里更改位置")
            self.body.insertWidget(1, Pill("OneDrive 管理", "muted"))
        elif reason_disabled:
            self.move_btn.setToolTip(reason_disabled)
        elif not item.is_on_system_drive:
            self.move_btn.setToolTip("已经不在系统盘上")
        self.target.textChanged.connect(
            lambda t: self.move_btn.setText(f"搬到 {_drive_of(t) or '目标'} 盘") if t.strip() else None)
        self.move_btn.clicked.connect(lambda: self.move_requested.emit(self.item, self.target.text().strip()))
        foot = QHBoxLayout()
        foot.addStretch(1)
        foot.addWidget(self.move_btn)
        self.body.addLayout(foot)


class KnownFoldersPanel(_Panel):
    def __init__(self, ctx: AppContext):
        super().__init__(ctx)
        self.items = []

        info = Banner("info")
        info.show_text(
            "用 Windows 自己的“更改文件夹位置”接口改绑，资源管理器、浏览器默认下载位置等都会跟着走，不需要目录联接。"
            "过程：复制全部文件并逐个校验 → 改绑到新位置 → 校验无误后删除 C 盘原件。"
            "目标文件夹必须不存在或为空；由 OneDrive 管理的文件夹会被拒绝，请在 OneDrive 设置里操作。")
        self.lay.addWidget(info)

        self.pending = Banner("danger")
        self.recover_btn = _button("检查并恢复", "dangerButton")
        self.recover_btn.clicked.connect(self.recover)
        self.pending.add_button(self.recover_btn)
        self.lay.addWidget(self.pending)

        self.opt_banner = Banner("warn")
        if ctx.optimizer is None:
            self.opt_banner.show_text("系统优化引擎 (SystemOptimizer) 未加载：可以查看文件夹占用，但不能迁移。")
        self.lay.addWidget(self.opt_banner)

        self.error_banner = Banner("warn")
        self.lay.addWidget(self.error_banner)

        bar = QHBoxLayout()
        bar.addWidget(_label("推荐目标盘", wrap=False))
        self.drive_combo = QComboBox()
        self.drive_combo.setMinimumWidth(200)
        bar.addWidget(self.drive_combo)
        bar.addStretch(1)
        self.scan_btn = _button("检测个人文件夹", "primaryButton")
        self.scan_btn.clicked.connect(self.scan)
        bar.addWidget(self.scan_btn)
        self.lay.addLayout(bar)
        self._fill_drives()

        self.progress = ProgressPanel("已复制")
        self.lay.addWidget(self.progress)

        self.grid_host = QWidget()
        self.grid = QGridLayout(self.grid_host)
        self.grid.setContentsMargins(0, 0, 0, 0)
        self.grid.setHorizontalSpacing(14)
        self.grid.setVerticalSpacing(14)
        self.lay.addWidget(self.grid_host)
        self.empty = _label("点击“检测个人文件夹”，查看下载、桌面、文档、视频、图片各占了多少 C 盘空间。", "muted")
        self.lay.addWidget(self.empty)
        self.lay.addStretch(1)
        self.check_pending()

    def _fill_drives(self) -> None:
        self.drive_combo.clear()
        try:
            drives = self.ctx.analyzer.discover_target_drives(exclude_drive=self.ctx.analyzer.system_drive)
        except Exception:
            drives = []
        for mount, free in drives:
            self.drive_combo.addItem(f"{mount}  （可用 {format_bytes(free)}）", mount)
        if not drives:
            self.drive_combo.addItem("D:\\", "D:\\")

    def check_pending(self) -> bool:
        path = getattr(self.ctx.optimizer, "journal_path", None)
        try:
            pending = bool(path and Path(path).exists())
        except OSError:
            pending = False
        if pending:
            self.pending.show_text("检测到上次没有完成的个人文件夹迁移（可能是中途关机或程序异常退出）。"
                                   "处理前不能进行新的迁移。“检查并恢复”会根据事务日志恢复到一致状态，"
                                   "不会删除任何未经验证的副本。")
        else:
            self.pending.hide()
        return pending

    # -- scan -----------------------------------------------------------------
    def scan(self) -> None:
        if self.busy:
            return
        self.scan_btn.setEnabled(False)
        self.progress.begin_busy("正在统计个人文件夹体积…")
        drive = self.drive_combo.currentData() or "D:\\"
        thread = KnownFolderMoveWorker(self.ctx.analyzer, self.ctx.optimizer, mode=KnownFolderMoveWorker.INSPECT,
                                       recommended_target_drive=drive)
        self._run(thread, on_progress=self.progress.update_progress, on_success=self._on_items,
                  on_error=lambda e: show_exception(self, e, "检测个人文件夹失败。"),
                  on_done=self._scan_done)

    def _scan_done(self) -> None:
        self.progress.hide()
        self.scan_btn.setEnabled(True)
        self.check_pending()

    def _on_items(self, result) -> None:
        items, errors = result if isinstance(result, tuple) else (result, {})
        self.items = list(items)
        if errors:
            self.error_banner.show_text("以下个人文件夹无法从 Windows 读取实际位置，因此不显示、也不提供迁移：\n"
                                        + "\n".join(errors.values()))
        else:
            self.error_banner.hide()
        self._render()

    def _render(self) -> None:
        while self.grid.count():
            w = self.grid.takeAt(0).widget()
            if w:
                w.deleteLater()
        reason = "" if self.ctx.optimizer is not None else "系统优化引擎未加载"
        for idx, item in enumerate(self.items):
            card = FolderCard(item, can_move=self.ctx.optimizer is not None, reason_disabled=reason)
            card.move_requested.connect(self.move)
            self.grid.addWidget(card, idx // 2, idx % 2)
        self.empty.setVisible(not self.items)

    # -- move -----------------------------------------------------------------
    def move(self, item, target: str) -> None:
        if self.ctx.optimizer is None:
            return
        if self.check_pending():
            QMessageBox.warning(self, "请先处理未完成事务", "存在未完成的个人文件夹迁移，请先点击“检查并恢复”。")
            return
        if not target or not os.path.isabs(target):
            QMessageBox.warning(self, "目标路径无效", "请填写完整的目标路径，例如 D:\\Users\\你的名字\\Downloads。")
            return
        if _drive_of(target) and _drive_of(target) == _drive_of(item.current_path):
            QMessageBox.warning(self, "目标在同一个盘",
                                "目标和当前位置在同一个磁盘上，迁移不会释放这个盘的空间。请换一个其他盘上的位置。")
            return
        if os.path.exists(target) and (not os.path.isdir(target) or any(os.scandir(target))):
            QMessageBox.warning(self, "目标不是空文件夹",
                                f"{target}\n已经存在且不是空文件夹。为避免合并或覆盖文件，请换一个位置，或先清空它。")
            return
        parent = os.path.dirname(target.rstrip("\\/"))
        create_parent = not os.path.isdir(parent)
        lines = [
            f"从：{item.current_path}",
            f"到：{target}",
            f"大小：{item.size_human}（{item.file_count} 个文件）",
            "",
            "• 请先关闭正在使用这个文件夹的程序（例如浏览器下载、Office 打开的文档）。",
            "• 文件会先完整复制并逐个校验，确认无误后才改绑并删除 C 盘原件；中途失败会自动回滚。",
            "• 文件很多时需要较长时间，期间请不要关机。",
        ]
        if create_parent:
            lines.append(f"• 会先创建上级文件夹：{parent}")
        if not confirm(self, "迁移个人文件夹", f"把“{item.display_name}”搬到 {_drive_of(target)} 盘？",
                       "\n".join(lines), yes_text="开始迁移"):
            return
        if not self.gate_acquire(f"迁移{item.display_name}"):
            return
        self.scan_btn.setEnabled(False)
        self.progress.begin("正在复制并校验…")

        def finished():
            # 线程已从 _threads 移除（busy 为 False）后再刷新；成功、失败、回滚都重新检测一次
            self.ctx.gate.release()
            self.progress.hide()
            self.scan_btn.setEnabled(True)
            self.check_pending()
            self.disk_changed.emit()
            self.scan()

        thread = KnownFolderMoveWorker(self.ctx.analyzer, self.ctx.optimizer, mode=KnownFolderMoveWorker.MOVE,
                                       item=item, target_path=target, create_parent=create_parent)
        self._run(thread, on_progress=self.progress.update_progress, on_success=self._on_moved,
                  on_error=lambda e: show_exception(self, e, f"“{item.display_name}”没有迁移。"),
                  on_done=finished)

    def _on_moved(self, result) -> None:
        item, warnings = result
        # 先用后端返回的新条目更新卡片；结束回调里会再从 Windows 重新检测一次
        self.items = [item if old.folder_key == item.folder_key else old for old in self.items]
        self._render()
        if warnings:
            QMessageBox.warning(self, "迁移已生效，但有收尾事项",
                                f"“{item.display_name}”现在位于：\n{item.current_path}\n\n" + "\n".join(warnings) +
                                "\n\nC 盘上可能暂时保留了名称含 ._kf_bak_ 的备份文件夹，确认新位置一切正常后再处理。")
        else:
            QMessageBox.information(self, "迁移完成",
                                    f"“{item.display_name}”现在位于：\n{item.current_path}\n\n"
                                    "资源管理器左侧的快捷入口会自动指向新位置。")

    # -- recover --------------------------------------------------------------
    def recover(self) -> None:
        if self.ctx.optimizer is None:
            return
        if not confirm(self, "检查并恢复", "根据事务日志检查上次中断的个人文件夹迁移，并恢复到一致的状态。",
                       "不会删除任何未经验证的副本；状态有歧义时会保留全部文件，交给你人工检查。", yes_text="开始检查"):
            return
        if not self.gate_acquire("恢复个人文件夹事务"):
            return
        self.recover_btn.setEnabled(False)

        def finished():
            self.ctx.gate.release()
            self.recover_btn.setEnabled(True)
            self.check_pending()
            if self.items:
                self.scan()

        thread = KnownFolderMoveWorker(self.ctx.analyzer, self.ctx.optimizer, mode=KnownFolderMoveWorker.RECOVER)
        self._run(thread, on_success=lambda msg: QMessageBox.information(self, "检查完成", str(msg)),
                  on_error=lambda e: show_exception(self, e, "恢复没有完成。"), on_done=finished)


# ===========================================================================
# 面板 C：卸载残留孤儿猎手
# ===========================================================================
class OrphanPanel(_Panel):
    def __init__(self, ctx: AppContext):
        super().__init__(ctx)
        self.items = []

        warn = Banner("warn")
        warn.show_text(
            "这里列出的是名字对不上任何已安装软件、体积超过 50 MB 的 AppData / ProgramData 文件夹。"
            "判断只依据名称和修改时间，可能误判（例如绿色软件、名称与安装名不同的软件数据、游戏存档）。"
            "SAFE 只表示“较可能是残留”，不代表删除一定没有影响。每个文件夹移入回收站前都会再复核一次；"
            "移入后可以从回收站还原，清空回收站后才会释放空间。")
        self.lay.addWidget(warn)

        bar = QHBoxLayout()
        self.summary = _label("", "sectionTitle", wrap=False)
        bar.addWidget(self.summary)
        bar.addStretch(1)
        self.scan_btn = _button("扫描卸载残留", "primaryButton")
        self.scan_btn.clicked.connect(self.scan)
        bar.addWidget(self.scan_btn)
        self.lay.addLayout(bar)

        self.progress = ProgressPanel("")
        self.lay.addWidget(self.progress)

        self.table = _table(["文件夹", "位置", "体积", "文件数", "目录修改", "置信度", ""])
        self.table.setMinimumHeight(300)
        for col, width in enumerate((210, 130, 90, 70, 90, 150)):
            self.table.setColumnWidth(col, width)
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.verticalHeader().setDefaultSectionSize(40)
        self.table.itemSelectionChanged.connect(self._show_detail)
        self.table.doubleClicked.connect(lambda *_: self._reveal())
        self.lay.addWidget(self.table, 1)

        self.detail = _label("点击“扫描卸载残留”开始。扫描只读取，不会修改任何文件。", "muted")
        self.detail.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.lay.addWidget(self.detail)

    # -- scan -----------------------------------------------------------------
    def scan(self) -> None:
        if self.busy:
            return
        self.scan_btn.setEnabled(False)
        self.progress.begin_busy("正在比对已安装软件并统计目录体积…")
        self._run(OrphanScanWorker(self.ctx.analyzer), on_progress=self.progress.update_progress,
                  on_success=self._on_items, on_error=lambda e: show_exception(self, e, "扫描卸载残留失败。"),
                  on_done=self._scan_done)

    def _scan_done(self) -> None:
        self.progress.hide()
        self.scan_btn.setEnabled(True)

    def _on_items(self, items) -> None:
        self.items = list(items)
        self._populate()
        if self.items:
            self.detail.setText("选中一行查看判断依据和完整路径；双击可在资源管理器中打开。")
        else:
            self.detail.setText("没有发现超过 50 MB 的疑似残留文件夹。")

    def _populate(self) -> None:
        self.table.setSortingEnabled(False)
        self.table.setRowCount(len(self.items))
        for row, it in enumerate(self.items):
            name = QTableWidgetItem(it.folder_name)
            name.setData(Qt.ItemDataRole.UserRole, it.orphan_id)
            name.setToolTip(it.path)
            self.table.setItem(row, 0, name)
            self.table.setItem(row, 1, QTableWidgetItem(it.location_scope))
            self.table.setItem(row, 2, SizeItem(it.size_bytes))
            count = QTableWidgetItem(str(it.file_count))
            count.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            self.table.setItem(row, 3, count)
            self.table.setItem(row, 4, QTableWidgetItem(f"{it.last_modified_days_ago} 天前"))
            self.table.setCellWidget(row, 5, cell(confidence_pill(it.confidence_level)))
            btn = QPushButton("移入回收站")
            btn.setObjectName("smallButton")
            btn.setCursor(Qt.CursorShape.PointingHandCursor)
            btn.clicked.connect(lambda _=False, oid=it.orphan_id: self.request_recycle(oid))
            self.table.setCellWidget(row, 6, cell(btn))
        total = sum(i.size_bytes for i in self.items)
        self.summary.setText(f"{len(self.items)} 个疑似残留 · 共 {format_bytes(total)}" if self.items else "")

    def _item_by_id(self, oid):
        return next((i for i in self.items if i.orphan_id == oid), None)

    def _selected(self):
        rows = {i.row() for i in self.table.selectedItems()}
        if not rows:
            return None
        return self._item_by_id(self.table.item(rows.pop(), 0).data(Qt.ItemDataRole.UserRole))

    def _show_detail(self) -> None:
        it = self._selected()
        if it:
            self.detail.setText(f"{it.path}\n{it.reason}")

    def _reveal(self) -> None:
        it = self._selected()
        if it:
            reveal_in_explorer(it.path)

    def _set_row_buttons(self, enabled: bool) -> None:
        for row in range(self.table.rowCount()):
            w = self.table.cellWidget(row, 6)
            if w:
                w.setEnabled(enabled)

    # -- recycle --------------------------------------------------------------
    def request_recycle(self, oid) -> None:
        it = self._item_by_id(oid)
        if it is None or self.busy:
            return
        self._set_row_buttons(False)
        self.progress.begin_busy(f"正在复核 {it.folder_name}…")
        self._run(OrphanRecycleWorker(it, self.ctx.guard, mode=OrphanRecycleWorker.INSPECT),
                  on_success=lambda check: self._confirm_recycle(it, check),
                  on_error=lambda e: show_exception(self, e, f"“{it.folder_name}”未通过复核，不会处理。"),
                  on_done=lambda: (self.progress.hide(), self._set_row_buttons(not self.ctx.gate.busy)))

    def _confirm_recycle(self, it, check) -> None:
        lines = [f"路径：{check.path}", f"体积：{it.size_human}（{it.file_count} 个文件）"]
        if check.newest_change_days is None:
            lines.append("目录内没有找到文件。")
        else:
            flag = "  ⚠ 最近仍有改动，可能还在被使用" if check.newest_change_days < 30 else ""
            lines.append(f"目录内最近一次文件改动：{check.newest_change_days} 天前{flag}")
        if not check.lock_check_available:
            QMessageBox.warning(self, "无法检查程序占用",
                                f"无法确认“{it.folder_name}”是否正被程序使用（缺少 psutil 或检测出错），"
                                "为安全起见不会移入回收站。\n\n请安装依赖（pip install psutil）后重试。")
            return
        if check.locks:
            names = "、".join(sorted({l.name for l in check.locks}))
            QMessageBox.warning(self, "请先关闭相关程序",
                                f"以下程序正在使用“{it.folder_name}”：{names}\n\n这说明它多半不是残留，已停止处理。")
            return
        else:
            lines.append("未检测到程序正在使用。")
        lines += ["", f"判断依据：{it.reason}", "", "移入回收站后可以还原；清空回收站后才会释放空间。"]
        danger = it.confidence_level != RiskLevel.SAFE or (check.newest_change_days is not None and check.newest_change_days < 30)
        if not confirm(self, "移入回收站", f"把“{it.folder_name}”移入回收站？", "\n".join(lines),
                       yes_text="移入回收站", danger=danger):
            return
        if not self.gate_acquire(f"移入回收站：{it.folder_name}"):
            return
        self._set_row_buttons(False)
        self.progress.begin_busy(f"正在移入回收站：{it.folder_name}…")

        def finished():
            self.ctx.gate.release()
            self.progress.hide()
            self._set_row_buttons(True)

        self._run(OrphanRecycleWorker(it, self.ctx.guard, mode=OrphanRecycleWorker.RECYCLE),
                  on_success=lambda path: self._on_recycled(it, path),
                  on_error=lambda e: show_exception(self, e, f"“{it.folder_name}”没有移入回收站。"),
                  on_done=finished)

    def _on_recycled(self, it, path: str) -> None:
        self.items = [i for i in self.items if i.orphan_id != it.orphan_id]
        self._populate()
        self.disk_changed.emit()
        QMessageBox.information(self, "已移入回收站",
                                f"“{it.folder_name}”已移入回收站。\n如果发现有软件出问题，可以从回收站还原。"
                                "\n清空回收站后才会释放这部分空间。")


# ===========================================================================
# 实验室页面
# ===========================================================================
class LabPage(PageBase):
    disk_changed = pyqtSignal()

    PANELS = ("A · 重复文件硬链接去重", "B · 个人文件夹搬家", "C · 卸载残留猎手")

    def __init__(self, ctx: AppContext):
        super().__init__(ctx, "深水区瘦身实验室",
                         "v1.1 实验功能：重复附件合并、个人文件夹搬家、卸载残留清理。每一步都会先让你确认。")
        badge = Pill("v1.1 Lab", "info")
        self.header_actions.addWidget(badge)
        self.tabs = SegmentedTabs(self.PANELS)
        self.content.addWidget(self.tabs)
        self.stack = QStackedWidget()
        self.dedupe = DedupePanel(ctx)
        self.folders = KnownFoldersPanel(ctx)
        self.orphans = OrphanPanel(ctx)
        for panel in (self.dedupe, self.folders, self.orphans):
            self.stack.addWidget(panel)
            panel.disk_changed.connect(self.disk_changed.emit)
        self.content.addWidget(self.stack, 1)
        self.tabs.currentChanged.connect(self.stack.setCurrentIndex)

    def readonly_threads_running(self) -> List[BackendThread]:
        return BackendThread.running(mutating=False)
