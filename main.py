#!/usr/bin/env python3
"""
CDriveRescue (C盘焦虑拯救者) - 桌面程序总装入口与依赖注入引导器 (v1.1)
===========================================================================
Responsible Model: Gemini (Chief System Architect)

Features (v1.0 + v1.1 Step-1 Upgrade):
1. Windows High-DPI scaling configuration and UAC Administrator check/elevation.
2. Automatic Dependency Injection:
   - Loads Gemini's `DiskAnalyzer` and `SafetyGuard`.
   - Dynamically binds GPT's `CacheCleaner` (`src/core/cache_cleaner.py`),
     `AppMover` (`src/core/app_mover.py`), and [NEW v1.1] `SystemOptimizer`
     (`src/core/system_optimizer.py`) once placed in `src/core/`.
   - Dynamically launches Claude's PyQt6 `MainWindow` (`src/ui/main_window.py`)
     with backward-compatible kwargs inspection so both v1.0 and v1.1 UI work.
"""

from __future__ import annotations

import argparse
import importlib
import importlib.util
import inspect
import logging
import os
import platform
import sys
import traceback
from pathlib import Path
from typing import Optional

from src.core.contracts import IAppMover, ICacheCleaner, ISystemOptimizer
from src.core.disk_analyzer import DiskAnalyzer
from src.core.safety_guard import SafetyGuard


def setup_logging() -> Path:
    """配置全局日志记录至 ~/.cdriverescue/app.log 与控制台"""
    app_data_dir = Path(os.environ.get("CDR_STATE_DIR", str(Path.home() / ".cdriverescue")))
    app_data_dir.mkdir(parents=True, exist_ok=True)
    log_file = app_data_dir / "app.log"

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        handlers=[
            logging.FileHandler(log_file, encoding="utf-8"),
            logging.StreamHandler(sys.stdout),
        ],
    )
    return log_file


def try_load_gpt_engines() -> tuple[Optional[ICacheCleaner], Optional[IAppMover], Optional[ISystemOptimizer]]:
    """
    动态探测并加载由 GPT 编写的 `CacheCleaner`、`AppMover` 与 v1.1 `SystemOptimizer` 模块。
    """
    logger = logging.getLogger("Bootstrap.GPTLoader")
    cleaner_instance: Optional[ICacheCleaner] = None
    mover_instance: Optional[IAppMover] = None
    optimizer_instance: Optional[ISystemOptimizer] = None

    try:
        cleaner_mod = importlib.import_module("src.core.cache_cleaner")
        cleaner_cls = getattr(cleaner_mod, "CacheCleaner", None)
        if cleaner_cls is not None:
            cleaner_instance = cleaner_cls()
            logger.info("已成功挂载 GPT 模块: src.core.cache_cleaner.CacheCleaner")
    except ModuleNotFoundError:
        logger.warning("尚未检测到 GPT 模块 `src/core/cache_cleaner.py`（等待合入）")
    except Exception as exc:
        logger.error("加载 GPT CacheCleaner 失败: %s", exc)

    try:
        mover_mod = importlib.import_module("src.core.app_mover")
        mover_cls = getattr(mover_mod, "AppMover", None)
        if mover_cls is not None:
            mover_instance = mover_cls()
            logger.info("已成功挂载 GPT 模块: src.core.app_mover.AppMover")
    except ModuleNotFoundError:
        logger.warning("尚未检测到 GPT 模块 `src/core/app_mover.py`（等待合入）")
    except Exception as exc:
        logger.error("加载 GPT AppMover 失败: %s", exc)

    try:
        opt_mod = importlib.import_module("src.core.system_optimizer")
        opt_cls = getattr(opt_mod, "SystemOptimizer", None)
        if opt_cls is not None:
            optimizer_instance = opt_cls()
            logger.info("已成功挂载 GPT v1.1 模块: src.core.system_optimizer.SystemOptimizer")
    except ModuleNotFoundError:
        logger.warning("尚未检测到 GPT v1.1 模块 `src/core/system_optimizer.py`（等待合入）")
    except Exception as exc:
        logger.error("加载 GPT SystemOptimizer 失败: %s", exc)

    return cleaner_instance, mover_instance, optimizer_instance


def run_cli_self_check(analyzer: DiskAnalyzer) -> int:
    """
    在终端运行 Gemini v1.1 底座的 C 盘健康度体检、个人文件夹检测与模块合入状态自检。
    """
    print("=" * 76)
    print("  CDriveRescue (C盘焦虑拯救者 v1.1) - Gemini 核心底座诊断模式")
    print("=" * 76)
    print(f"操作系统环境 : {platform.platform()}")
    print(f"管理员权限   : {'已获取 (UAC Admin)' if SafetyGuard.is_admin() else '普通用户模式 (受保护目录可能需要管理员权限)'}")

    report = analyzer.analyze_drive_health()
    print("-" * 76)
    print(f"[1] 系统盘健康诊断 ({report.drive_letter})")
    print(f"    总容量: {report.total_human} | 已用: {report.used_human} ({report.usage_percent}%) | 剩余: {report.free_human}")
    print(f"    焦虑指数: {report.anxiety_score} / 100  [{report.anxiety_level.value}]")
    print(f"    诊断结论: {report.headline_diagnosis}")
    print("\n[2] 针对性恢复建议:")
    for idx, rec in enumerate(report.recommendations, 1):
        print(f"    {idx}. {rec}")

    print("\n[3] 系统级空间巨兽与组件瘦身检测 (hiberfil / pagefile / WinSxS):")
    for slim in report.system_slim_items:
        status_str = f"占用 {slim.size_human}" if slim.size_bytes > 0 else ("可瘦身" if slim.exists else "未生成/已关闭")
        print(f"    - {slim.name}: {status_str} | 指令: `{slim.command_hint}`")

    print("\n[4] [v1.1 新增] Windows 系统个人文件夹驻留检测:")
    known_folders = analyzer.inspect_windows_known_folders()
    for kf in known_folders:
        loc_tag = "位于系统盘 (建议迁移)" if kf.is_on_system_drive else "已迁移至非系统盘"
        print(f"    - {kf.display_name:<18}: {kf.size_human:<10} ({kf.file_count} 个文件) [{loc_tag}] -> 推荐目标: {kf.recommended_target_path}")

    cleaner, mover, optimizer = try_load_gpt_engines()
    print("-" * 76)
    print("[5] 三方模块合入状态 (v1.0 + v1.1 扩展):")
    print("    - [Gemini v1.1] 核心契约 / 安全护栏 / 重复文件&孤儿残留雷达 : READY (已就绪)")
    print(f"    - [GPT v1.0]    缓存清理器 (CacheCleaner)                   : {'READY' if cleaner else 'PENDING (待放入 src/core/cache_cleaner.py)'}")
    print(f"    - [GPT v1.0]    无损搬家引擎 (AppMover)                     : {'READY' if mover else 'PENDING (待放入 src/core/app_mover.py)'}")
    print(f"    - [GPT v1.1]    深水区瘦身器 (SystemOptimizer)              : {'READY' if optimizer else 'PENDING (待放入 src/core/system_optimizer.py)'}")
    ui_exists = Path(__file__).parent.joinpath("src", "ui", "main_window.py").exists()
    for dependency in ("psutil", "send2trash", "PyQt6"):
        available = importlib.util.find_spec(dependency) is not None
        print(f"    - Dependency {dependency:<10}                                  : {'READY' if available else 'MISSING'}")
    print(f"    - [Claude v1.1] PyQt6 桌面视图 (MainWindow)                 : {'READY' if ui_exists else 'PENDING (待放入 src/ui/main_window.py)'}")
    print("=" * 76)
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="CDriveRescue (C盘焦虑拯救者 v1.1) 桌面客户端")
    parser.add_argument(
        "--cli-diagnose",
        action="store_true",
        help="以命令行模式运行 Gemini v1.1 底座磁盘健康诊断与模块自检",
    )
    parser.add_argument(
        "--no-elevate",
        action="store_true",
        help="跳过启动时的 Windows UAC 管理员自动提权请求",
    )
    args = parser.parse_args()

    setup_logging()
    logger = logging.getLogger("Bootstrap.Main")

    # 在 Windows 环境下按需请求管理员提权（保证 mklink /J 和 C:\Windows\Temp 清理畅通无阻）
    if platform.system() == "Windows" and not args.no_elevate and not args.cli_diagnose and os.environ.get("CDR_ELEVATE_ON_START") == "1":
        if not SafetyGuard.is_admin():
            logger.info("当前未以管理员身份运行，正在请求 Windows UAC 提权...")
            if SafetyGuard.request_admin_elevation():
                return 0

    analyzer = DiskAnalyzer()
    if args.cli_diagnose:
        return run_cli_self_check(analyzer)

    cleaner, mover, optimizer = try_load_gpt_engines()

    try:
        from PyQt6.QtCore import Qt
        from PyQt6.QtWidgets import QApplication

        ui_mod = importlib.import_module("src.ui.main_window")
        main_window_cls = getattr(ui_mod, "MainWindow")

        if hasattr(Qt.ApplicationAttribute, "AA_EnableHighDpiScaling"):
            QApplication.setAttribute(Qt.ApplicationAttribute.AA_EnableHighDpiScaling, True)

        app = QApplication(sys.argv)
        app.setApplicationName("CDriveRescue - C盘焦虑拯救者 v1.1")
        app.setOrganizationName("CDriveRescueTeam")

        qss_path = Path(__file__).parent / "src" / "ui" / "theme.qss"
        if qss_path.exists():
            app.setStyleSheet(qss_path.read_text(encoding="utf-8"))

        # 智能检查 Claude MainWindow.__init__ 签名，向下兼容 v1.0 并自动注入 v1.1 system_optimizer
        init_sig = inspect.signature(main_window_cls.__init__)
        kwargs = {
            "disk_analyzer": analyzer,
            "safety_guard": SafetyGuard,
            "cache_cleaner": cleaner,
            "app_mover": mover,
        }
        if "system_optimizer" in init_sig.parameters or any(
            p.kind == inspect.Parameter.VAR_KEYWORD for p in init_sig.parameters.values()
        ):
            kwargs["system_optimizer"] = optimizer

        window = main_window_cls(**kwargs)
        window.show()
        return app.exec()

    except ModuleNotFoundError:
        logger.info("尚未检测到 Claude UI 模块或 PyQt6 环境，自动切换至 Gemini CLI 诊断模式。")
        return run_cli_self_check(analyzer)
    except Exception as exc:
        logger.error("启动图形界面异常: %s\n%s", exc, traceback.format_exc())
        return run_cli_self_check(analyzer)


if __name__ == "__main__":
    sys.exit(main())
