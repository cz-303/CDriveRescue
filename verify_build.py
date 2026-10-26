"""打包前后检查，由 build_exe.bat 调用。

用法：
    .venv\\Scripts\\python.exe verify_build.py preflight gui|cli   # 打包前：确认解释器和依赖
    .venv\\Scripts\\python.exe verify_build.py verify gui|cli      # 打包后：确认产物里真的带上了依赖

任一检查失败时返回非零退出码，让 build_exe.bat 停止并报错。
"""
from __future__ import annotations

import importlib
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
NAMES = {"gui": "CDriveRescue", "cli": "CDriveRescueCLI"}
MODULES = {
    "gui": ["PyInstaller", "PyQt6.QtCore", "PyQt6.QtWidgets", "psutil", "send2trash"],
    "cli": ["PyInstaller", "psutil", "send2trash"],
}


class Checker:
    def __init__(self) -> None:
        self.failed = 0

    def check(self, ok: bool, label: str, hint: str = "") -> bool:
        print(f"  [{'OK' if ok else 'FAIL'}] {label}")
        if not ok:
            self.failed += 1
            if hint:
                print(f"         {hint}")
        return ok


def expected_python_dll() -> str:
    return f"python{sys.version_info.major}{sys.version_info.minor}.dll"


def preflight(mode: str) -> int:
    c = Checker()
    exe = Path(sys.executable).resolve()
    venv = (ROOT / ".venv").resolve()
    print(f"打包解释器：Python {sys.version.split()[0]}  ({exe})")
    c.check(venv in exe.parents, "使用项目 .venv 中的 Python",
            "请用 .venv\\Scripts\\python.exe 运行，不要用 conda 或系统 Python。")
    leaks = [d for d in os.environ.get("PATH", "").split(os.pathsep)
             if d and any(k in d.lower() for k in ("conda", "miniforge", "mambaforge"))]
    c.check(not leaks, "打包时 PATH 中没有 conda 目录", "PATH 包含：" + "; ".join(leaks) if leaks else "")
    if os.environ.get("CONDA_PREFIX"):
        print(f"  [WARN] 检测到 conda 环境仍处于激活状态：{os.environ['CONDA_PREFIX']}")
        print("         建议先执行 conda deactivate，避免把 conda 的 DLL 混进安装包。")
    for name in MODULES[mode]:
        try:
            module = importlib.import_module(name)
            where = Path(getattr(module, "__file__", "") or "").resolve()
            inside = venv in where.parents
            c.check(inside, f"{name} 可导入（{where.parent if where.name else '内置'}）",
                    "该模块不在项目 .venv 中。" if not inside else "")
        except Exception as exc:  # noqa: BLE001 - 包括 DLL 加载失败
            c.check(False, f"{name} 可导入", f"{exc.__class__.__name__}: {exc}\n"
                    "         请执行：.venv\\Scripts\\python.exe -m pip install -r requirements.txt")
    if c.failed:
        print(f"\n打包前检查失败 {c.failed} 项，已停止打包。")
        return 1
    print("打包前检查通过。\n")
    return 0


def _find(root: Path, pattern: str) -> list[Path]:
    return sorted(root.rglob(pattern)) if root.exists() else []


def verify(mode: str) -> int:
    c = Checker()
    name = NAMES[mode]
    dist = ROOT / "dist" / name
    build = ROOT / "build" / name
    print(f"检查打包产物：{dist}")

    c.check((dist / f"{name}.exe").is_file(), f"{name}.exe 存在")

    want = expected_python_dll()
    found = {p.name.lower() for p in _find(dist, "python3*.dll") if p.name.lower() != "python3.dll"}
    c.check(found == {want}, f"内置 Python 与 .venv 一致（期望 {want}，实际 {', '.join(sorted(found)) or '无'}）",
            "产物里的 Python 版本不对，说明打包时用错了解释器。")

    if os.name == "nt":
        c.check(bool(_find(dist, "_psutil_windows*.pyd")), "psutil 扩展模块已打包")
    if mode == "gui":
        c.check(bool(_find(dist, "Qt6Core.dll")), "Qt6Core.dll 已打包")
        c.check(bool(_find(dist, "Qt6Widgets.dll")), "Qt6Widgets.dll 已打包")
        c.check(bool(_find(dist, "QtCore*.pyd")), "PyQt6.QtCore 扩展模块已打包")
        if os.name == "nt":
            c.check(bool(_find(dist, "qwindows.dll")), "Qt Windows 平台插件已打包")

    # 来自 PATH 上其他软件（conda、ICU 发行版等）的 DLL 不能混进安装包。
    # 例：conda 的 icuuc.dll(ICU 78) 只导出带版本号的函数，Qt6Core 需要的是 Windows 系统 ICU，
    # 混入后会导致 "DLL load failed while importing QtCore: 找不到指定的程序"。
    foreign = [p for p in _find(dist, "*.dll") if p.name.lower().startswith(("icuuc", "icuin", "icudt", "icu."))]
    c.check(not foreign, "没有混入外来的 ICU DLL",
            "混入：" + ", ".join(str(p.relative_to(dist)) for p in foreign) if foreign else "")

    data = ["src/core/cache_rules.json"]
    if mode == "gui":
        data += ["src/ui/theme.qss", "src/ui/assets/app.ico"]
    for rel in data:
        c.check(any(p.as_posix().endswith(rel) for p in _find(dist, Path(rel).name)), f"资源文件 {rel} 已打包")

    tocs = _find(build, "PYZ-*.toc")
    if tocs:
        text = "\n".join(t.read_text(encoding="utf-8", errors="ignore") for t in tocs)
        wanted = ["'send2trash'", "'psutil'", "'src.core.cache_cleaner'", "'src.core.app_mover'"]
        if mode == "gui":
            wanted += ["'PyQt6'", "'src.ui.main_window'", "'src.ui.pages'"]
        for token in wanted:
            c.check(token in text, f"Python 模块 {token.strip(chr(39))} 已打包")
    else:
        print("  [WARN] 未找到 PYZ 目录文件，跳过纯 Python 模块检查")

    if c.failed:
        print(f"\n打包产物检查失败 {c.failed} 项。不要分发这个版本。")
        return 1
    print("打包产物检查通过。")
    return 0


def main(argv: list[str]) -> int:
    if len(argv) != 3 or argv[1] not in ("preflight", "verify") or argv[2] not in NAMES:
        print(__doc__)
        return 2
    return preflight(argv[2]) if argv[1] == "preflight" else verify(argv[2])


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    raise SystemExit(main(sys.argv))
