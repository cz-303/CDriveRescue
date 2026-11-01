# CDriveRescue v1.1：Claude 修复报告（2026-11-01）

对应任务：`docs/CLAUDE_FIX_HANDOFF_20261101.md`。开始前核对：项目源文件自 2026-10-29 合并后没有变化，五项问题均仍存在。

测试先在 Linux（离屏 Qt）上运行，随后在用户的 Windows 11 上完成全量测试与打包，结果见“Windows 验收结果”。新 EXE 已实际启动验收，见文末。

## 各项状态

| # | 问题 | 状态 |
| --- | --- | --- |
| 1 | 已有硬链接被重复计数 | 已修复，Linux 上用模拟 Windows 行为的测试验证 |
| 2 | 个人文件夹读取默认路径 | 已修复，Known Folder API 调用为模拟验证；真实 API 只能在 Windows 上验证 |
| 3 | 迁移成功后不刷新 | 已修复，离屏 Qt 测试验证 |
| 4 | 缺少进程检测依赖仍可回收 | 已修复，测试验证 |
| 5 | DISM 入口 / 版本 / 文案 | 已完成，DISM 输出为模拟；版本统一为 1.1.0 |
| 6 | 回归与 EXE 验收 | Windows 全量测试通过；EXE 已重建、检查内容并实际启动验收 |

## 修改与行为变化

### 1. `src/core/disk_analyzer.py` → `scan_duplicate_files_fast()`

- 根因：Windows 上 `DirEntry.stat()` 的 `st_ino` / `st_dev` / `st_nlink` 恒为 0。文件身份改用 `os.stat()` 的 `(st_dev, st_ino)`；身份为 0 时该文件不参与去重。
- 以“实体”为单位：同一实体的多条路径只算一个，只哈希一次。
- 按 (卷, 大小) 分桶，主文件只在同卷内选择；`group_id` 带卷标识，多卷时不重复。
- 主文件优先选链接数最多（已共享）的实体；其余实体中链接数大于 1 的，执行器会拒绝合并，因此不列入副本、不计入可省空间。
- 相同或嵌套的扫描根只保留最外层；目录按真实路径去重。
- a/b 已为同一实体、c 为独立相同文件：现在只报告 1 个可合并副本（c），可省 4096 字节。

### 2. 个人文件夹实际位置

- `inspect_windows_known_folders()`：正常运行时通过 `SHGetKnownFolderPath` 读取（只读）；`custom_user_home` 模式保留。
- 读取失败的文件夹不出现在结果中，原因写入 `last_known_folder_errors`，界面显示“无法读取，不提供迁移”。
- 系统盘判断使用 `%SystemDrive%`（或 Windows 目录所在盘），不写死 C。
- 新增 `default_duplicate_scan_roots()`：重复附件默认扫描根从实际“文档/下载”派生；读取失败的根跳过。
- 界面：OneDrive 管理的文件夹卡片标注并禁用迁移；执行器的 OneDrive、重解析点、过期路径和目标冲突防护未改动。

### 3. `src/ui/lab_page.py` → 迁移后刷新

- 成功回调只用返回的 `KnownFolderItem` 更新卡片；结束回调在线程移出列表、闸门释放之后统一重新检测一次。成功、失败、回滚都会刷新；恢复事务后也会刷新。

### 4. `src/ui/workers.py` → 回收残留目录

- `recycle_orphan_dir()` 无条件检查占用：缺少 psutil、检测出错都抛出 `LockCheckUnavailable`，检测到占用时抛出 `ProcessLockedError`。三种情况都不会调用 Send2Trash；缺少 Send2Trash 时也不会改为永久删除。
- 复核对话框：无法检查占用时直接说明原因并停止，不再允许继续。只读扫描不受影响。

### 5. DISM、版本、文案

- C 盘体检页“系统级大文件”卡片新增“清理组件存储”：管理员检查 → 确认（说明立即删除被替代的旧组件、不用 `/ResetBase`、不自动重启、耗时、释放量不确定）→ 后台线程 → 进度条 → 结果对话框（成功、需要重启、失败与 DISM 输出）。执行期间写操作闸门占用，窗口拒绝关闭。SystemOptimizer 未加载时按钮禁用。
- `src/__init__.py` 版本改为 1.1.0（侧栏显示 v1.1.0）。`verify_build.py version-file` 从该值生成 EXE 版本资源，打包后检查 EXE 内嵌版本与之一致。
- 文案：WinSxS 描述删去“100% 原生安全，通常可释放 3GB~12GB”；体检建议删去“无损释放”“无损搬家”；硬链接确认框不再承诺“自动还原”，改为“会尝试还原，不完整时提示并保留 ._hl_bak 备份”。README 加入 v1.1 功能说明（标注开发中）。

### 打包脚本 `build_exe.bat`

- 重新打包前把上一版移到 `dist\CDriveRescue_prev` 保留，不再直接删除。
- 生成并嵌入版本资源（`--version-file`）。

## 修改文件

`src/__init__.py`、`src/core/disk_analyzer.py`、`src/ui/lab_page.py`、`src/ui/workers.py`、`src/ui/widgets.py`、`src/ui/pages.py`、`tests/test_gemini_foundation.py`、`tests/test_ui_lab.py`、`verify_build.py`、`build_exe.bat`、`README.md`。

未改动：`safety_guard.py`、`system_optimizer.py`、`app_mover.py`、`cache_cleaner.py`、`file_ops.py`、`contracts.py`、`main.py`、`main_window.py`。

## 新增测试

- `DuplicateScannerIdentityTests`（8 项）：模拟 Windows `DirEntry` 返回 0 的已有硬链接、主文件优先共享实体、全部已共享不成组、重叠扫描根、多卷分组、身份不可靠时跳过、执行后重新扫描、同大小不同内容。在旧扫描器上运行，其中 6 项失败，说明能抓到原问题。
- `KnownFolderResolutionTests`（5 项）：重定向到 D 盘、OneDrive 路径、API 失败、非 C 系统盘、迁移后重新扫描、自定义主目录模式、重复附件默认根。全部模拟，不读写真实配置。
- `LabUiTests` 新增 5 项：迁移后卡片显示新位置且按钮禁用、只刷新一次、失败后仍刷新并释放闸门、无法读取的文件夹提示与 OneDrive 禁用、回收前占用检查的四种失败情况都不调用 Send2Trash、复核框在无法检查时拒绝。在旧界面代码上运行，5 项全部失败。
- `DismEntryTests`（5 项）：成功并提示重启、失败输出、需要管理员、未加载优化器时禁用、执行中拒绝关闭窗口。

## 测试结果（Linux，Python 3.13，PyQt6 6.11，离屏）

```
python -X utf8 -m unittest discover -s tests -v
Ran 122 tests — OK (skipped=59)
```

- 通过 63 项。UI 测试 37 项全部实际运行，没有因缺少 PyQt6 而跳过。
- 跳过 59 项，均因需要 Windows：`Requires Windows NTFS` 41 项、`Windows migration` 17 项、`Windows junction` 1 项。
- `python -m compileall -q src main.py`：通过；全部源文件无 SyntaxWarning。

## Windows 中发现并修复的问题

首次在 Windows 上运行时，`test_recycle_refused_without_working_lock_check` 出错。原因是 Windows 的 %TEMP% 使用 8.3 短名（`C:\Users\ADMINI~1\...`），而 `os.path.realpath` 会把它展开为长名；复核代码直接拿原路径和展开后的路径比较，正常目录因此被误判为“路径解析后指向别处”。用户名较长的用户实际使用时也会遇到。

修复（`src/ui/workers.py` → `validate_orphan_path`）：扫描根判断和重定向判断两边都先取 realpath 再比较；联接点/符号链接仍逐级拒绝，并另外验证了符号链接重定向仍被拒绝。新增测试 `test_short_8dot3_names_are_not_mistaken_for_redirects`：修复前在 Linux 上可复现该错误，修复后通过。

## Windows 验收结果（2026-11-01，Windows 11，项目 .venv，Python 3.13.15）

```
.\.venv\Scripts\python.exe -X utf8 -m unittest discover -s tests -v
Ran 123 tests — OK（0 跳过）
```

- 123 项全部实际运行并通过，包括原先需要 Windows NTFS 的 59 项，以及真实 NTFS 下的硬链接重复扫描测试。
- 日志中的两个 Traceback 是测试故意制造失败时打印的错误日志；`NativeCommandError` 是 PowerShell 重定向输出时的包装提示，不是测试失败。

`.\build_exe.bat`（PyInstaller 6.22.3）：

- 打包前检查全部通过：使用项目 .venv 的 Python，PATH 中无 conda，依赖全部可导入。
- 上一版保留为 `dist\CDriveRescue_prev`。
- 打包后检查全部通过：内置 python313.dll 与 .venv 一致，Qt DLL 与平台插件齐全，没有混入外来 ICU DLL，资源文件齐全，模块中包含实验室页面与 SystemOptimizer，EXE 内嵌版本为 1.1.0。
- 唯一警告 `send2trash.mac` 是 macOS 专用模块，不影响 Windows。

另外直接读取 `dist\CDriveRescue\CDriveRescue.exe`（3,164,005 字节）内嵌的模块归档核对：版本资源为 1.1.0；`src.ui.lab_page`、`src.core.system_optimizer` 及全部引擎、psutil、send2trash、PyQt6 都在；代码为本次修复后的版本（含 8.3 短名修复、占用检查必做、DISM 入口、真实个人文件夹位置读取、按实体去重的扫描器）。

## 启动验收（2026-11-01，用户在 Windows 11 上确认）

运行 `dist\CDriveRescue\CDriveRescue.exe`：主窗口正常，侧栏显示 v1.1.0；C 盘体检页有“清理组件存储”按钮（未执行）；实验室个人文件夹检测显示真实位置；重复文件与卸载残留面板扫描正常。文件操作只在临时文件夹上验证。

注意：`build\CDriveRescue\CDriveRescue.exe` 是打包过程的中间文件，不能单独运行（会报 Failed to load Python DLL）；发布和使用的是 `dist\CDriveRescue`。
