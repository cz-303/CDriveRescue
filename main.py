#!/usr/bin/env python3
"""
CDriveRescue (C盘焦虑拯救者) - 桌面程序总装入口与依赖注入引导器
===========================================================================
Responsible Model: Gemini (Chief System Architect)

Features:
1. Windows High-DPI scaling configuration and UAC Administrator check/elevation.
2. Automatic Dependency Injection:
   - Loads Gemini's `DiskAnalyzer` and `SafetyGuard`.
   - Dynamically binds GPT's `CacheCleaner` (`src/core/cache_cleaner.py`) and
     `AppMover` (`src/core/app_mover.py`) once placed in `src/core/`.
   - Dynamically launches Claude's PyQt6 `MainWindow` (`src/ui/main_window.py`)
     once placed in `src/ui/`, or runs an interactive CLI diagnostic report if
     GPT/Claude modules have not been dropped in yet (`--cli-diagnose`).
"""

from __future__ import annotations

import argparse
import importlib
import importlib.util
import logging
import os
import platform
import sys
import traceback
from pathlib import Path
from typing import Optional

from src.core.contracts import IAppMover, ICacheCleaner, format_bytes
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


def try_load_gpt_engines() -> tuple[Optional[ICacheCleaner], Optional[IAppMover]]:
    """
    动态探测并加载由 GPT 编写的 `CacheCleaner` 与 `AppMover` 模块。
    如果用户尚未把 GPT 生成的文件放入 `src/core/`，则优雅返回 None 并给出清晰提示。
    """
    logger = logging.getLogger("Bootstrap.GPTLoader")
    cleaner_instance: Optional[ICacheCleaner] = None
    mover_instance: Optional[IAppMover] = None

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

    return cleaner_instance, mover_instance


def run_cli_self_check(analyzer: DiskAnalyzer) -> int:
    """
    当尚未放入 Claude 的 PyQt6 界面文件或传入 `--cli-diagnose` 时，
    在终端直接运行 Gemini 底座的 C 盘健康度体检与安全护栏自检报告。
    """
    print("=" * 72)
    print("  CDriveRescue (C盘焦虑拯救者) - Gemini 核心底座诊断模式")
    print("=" * 72)
    print(f"操作系统环境 : {platform.platform()}")
    print(f"管理员权限   : {'已获取 (UAC Admin)' if SafetyGuard.is_admin() else '普通用户模式 (受保护目录可能需要管理员权限)'}")

    report = analyzer.analyze_drive_health()
    print("-" * 72)
    print(f"[1] 系统盘健康诊断 ({report.drive_letter})")
    print(f"    总容量: {report.total_human} | 已用: {report.used_human} ({report.usage_percent}%) | 剩余: {report.free_human}")
    print(f"    焦虑指数: {report.anxiety_score} / 100  [{report.anxiety_level.value}]")
    print(f"    诊断结论: {report.headline_diagnosis}")
    print("\n[2] 针对性恢复建议:")
    for idx, rec in enumerate(report.recommendations, 1):
        print(f"    {idx}. {rec}")

    print("\n[3] 系统级空间巨兽检测 (hiberfil.sys / pagefile.sys):")
    for slim in report.system_slim_items:
        status_str = f"占用 {slim.size_human}" if slim.exists else "未生成/已关闭 (0 B)"
        print(f"    - {slim.name}: {status_str} | 指令提示: `{slim.command_hint}`")

    cleaner, mover = try_load_gpt_engines()
    print("-" * 72)
    print("[4] 三方模块合入状态:")
    print("    - [Gemini] 核心契约 / 安全护栏 / 磁盘分析引擎 : READY (已就绪)")
    print(f"    - [GPT]    缓存清理器 (CacheCleaner)         : {'READY' if cleaner else 'PENDING (待放入 src/core/cache_cleaner.py)'}")
    print(f"    - [GPT]    软件迁移引擎 (AppMover)           : {'READY' if mover else 'PENDING (待放入 src/core/app_mover.py)'}")
    ui_exists = Path(__file__).parent.joinpath("src", "ui", "main_window.py").exists()
    for dependency in ("psutil", "send2trash", "PyQt6"):
        available = importlib.util.find_spec(dependency) is not None
        print(f"    - Dependency {dependency}: {'READY' if available else 'MISSING'}")
    print(f"    - [Claude] PyQt6 桌面视图 (MainWindow)       : {'READY' if ui_exists else 'PENDING (待放入 src/ui/main_window.py)'}")
    print("=" * 72)
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="CDriveRescue (C盘焦虑拯救者) 桌面客户端")
    parser.add_argument(
        "--cli-diagnose",
        action="store_true",
        help="以命令行模式运行 Gemini 底座磁盘健康诊断与模块自检",
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

    cleaner, mover = try_load_gpt_engines()

    # 尝试加载并启动 Claude 编写的 PyQt6 主界面
    try:
        from PyQt6.QtCore import Qt
        from PyQt6.QtWidgets import QApplication

        ui_mod = importlib.import_module("src.ui.main_window")
        main_window_cls = getattr(ui_mod, "MainWindow")

        # 开启 Windows 高分屏自适应渲染
        if hasattr(Qt.ApplicationAttribute, "AA_EnableHighDpiScaling"):
            QApplication.setAttribute(Qt.ApplicationAttribute.AA_EnableHighDpiScaling, True)

        app = QApplication(sys.argv)
        app.setApplicationName("CDriveRescue - C盘焦虑拯救者")
        app.setOrganizationName("CDriveRescueTeam")

        # 自动加载 Claude 的 theme.qss 样式表（如果存在）
        qss_path = Path(__file__).parent / "src" / "ui" / "theme.qss"
        if qss_path.exists():
            app.setStyleSheet(qss_path.read_text(encoding="utf-8"))

        window = main_window_cls(
            disk_analyzer=analyzer,
            safety_guard=SafetyGuard,
            cache_cleaner=cleaner,
            app_mover=mover,
        )
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
