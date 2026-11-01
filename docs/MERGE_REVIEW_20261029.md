# v1.1 合并复核（2026-10-29）

复核对象：本地当前工作树，相对于 Git HEAD `206bae9` 的合并改动及新增文件。此次只检查、运行测试和记录发现，没有修改业务代码，也没有重新打包。

## 已确认恢复的 1.0 行为

`SafetyGuard.create_junction` 已恢复；路径规范化、根目录/ADS/重解析点防护、缺少 psutil 时拒绝旧迁移，以及打开文件的进程占用检测均已恢复。`app_mover.py`、`cache_cleaner.py`、`file_ops.py` 相对于 HEAD 没有改动。此前旧引擎测试中的缺失接口和防护回归，在本轮测试中均未再出现。

## 仍需修复

### 1. 重复文件扫描误计已有硬链接（P2，已复现）

位置：`src/core/disk_analyzer.py:524`、`:559`；现有失败测试 `tests/test_gemini_foundation.py:239`。

Windows 下当前扫描采用的 `DirEntry.stat().st_ino` 返回 0，无法识别已有硬链接。真实 NTFS 临时文件复现：a.bin 与 b.bin 为同一实体，c.bin 为独立的相同内容文件，每个 4096 字节；扫描仍返回两个副本、可省 8192 字节，实际只能再消除一个独立副本。

同一轮对照读取中，`os.stat()` 能返回 a/b 相同、c 不同的真实 inode。修复应使用可靠的 `(st_dev, st_ino)`，按卷组织候选组，并对组内所有实体去重，不能只和第一个主文件比较；还应去除重复/嵌套扫描根造成的重复路径。考虑到执行器拒绝 `st_nlink != 1` 的副本，主文件选择也应优先保留已经共享的实体。

影响：扫描可省空间偏大，去重后重新扫描仍出现已合并文件。执行器会再次检查并跳过/拒绝，未在本次复现中造成文件损坏。

### 2. 个人文件夹仍读取默认路径，迁移后再次扫描结果错误（P2）

位置：`src/core/disk_analyzer.py:632`、`:637`；`src/core/system_optimizer.py` 的 `_folder_paths` 会核对真实 Windows 路径。

扫描器始终按用户主目录拼接 Downloads/Desktop 等名称，而不是查询 `SHGetKnownFolderPath`。用户已经改过位置、使用 OneDrive，或刚通过此软件迁移后，卡片会显示旧路径、错误占用或空目录。执行器会拒绝这类过期条目；用户即使重新扫描也无法消除路径不一致。

修复：生产环境从 Windows Known Folder API 读取路径；`custom_user_home` 仅作为显式测试/自定义模式保留。系统盘判断使用实际系统盘而非写死 C。重复附件默认扫描目录也应从实际 Documents/Downloads 派生。

### 3. 迁移成功后的自动刷新被 busy 判断吞掉（P2，逻辑复现）

位置：`src/ui/lab_page.py:568`、`:477`，以及 `_Panel._run` 的 on_done 清理。

`_on_moved()` 在 succeeded 回调里调用 `scan()`；此时迁移线程仍保留在 `_threads` 中，`busy` 为 True，scan 直接返回。随后 finished 只释放闸门和恢复按钮，没有重新检测。即使修复第 2 项，这条自动刷新路径仍不会启动。

通过从当前源文件提取这两个方法并对成功回调的忙碌状态进行模拟，确认刷新没有启动、旧条目仍保留。这不是完整 Qt 运行验证。

修复：在迁移 on_done 清理线程和释放闸门后刷新；也可以先用返回的 KnownFolderItem 更新卡片，再于 finished 后复查。增加“迁移成功后卡片显示新位置”的 UI 断言；现有测试只检查后端调用，没有覆盖这一点。

### 4. 卸载残留回收绕过缺失的进程检测依赖（安全一致性问题）

位置：`src/ui/workers.py:405`。

`recycle_orphan_dir` 仅在 `find_spec('psutil')` 不为空时调用占用检查；缺少依赖时仍会调用 Send2Trash。界面虽提示无法检查，但仍允许继续，与已经恢复的旧迁移“无法检查占用即拒绝”策略不一致。源码确认此分支；未对真实目录执行回收。

建议：回收前无条件调用 guard 的占用检查，让缺少依赖产生明确错误；补测“缺少 psutil 时不会调用 send2trash”。只读扫描可以继续提供。

## 发布前待办

- **DISM 尚无界面入口。** `slim_winsxs_component_store` 只在核心实现与契约中出现，UI 没有调用。`src/ui/pages.py:345` 只提供关闭休眠按钮。若本版开放组件清理，需要增加管理员检查、确认、后台任务、OperationGate 和结果展示。
- **版本标识未统一。** `src/__init__.py:2` 仍是 `1.0.0`，主窗口侧栏读取这个值，而引导入口和实验室写着 v1.1。
- **现有 EXE 仍不是这次合并的产物。** 直接读取 `dist/CDriveRescue/CDriveRescue.exe` 内嵌 PYZ 目录确认：包含 `src.ui.main_window`，不包含 `src.ui.lab_page` 和 `src.core.system_optimizer`。修复后需重新打包；本轮没有将旧 EXE 启动表现当成新代码验收结果。

## 验证结果与限制

执行：`python -X utf8 -m unittest discover -s tests -q`。

- 总计 99 项：76 通过、1 失败、22 跳过、0 错误。
- 唯一失败为上述已有硬链接重复扫描测试。
- 22 项均为 UI 测试，当前系统 Python 无可用 PyQt6，因而跳过；不能声称图形界面全部通过。
- 项目 `.venv/Scripts/python.exe` 在当前执行环境无法启动；其配置指向的 `C:\Users\Administrator\AppData\Local\Programs\Python\Python313\python.exe` 不存在。尝试借用包目录也遇到 Qt DLL/解释器扩展不兼容，未据此判断用户机器上的 PyQt6 安装损坏。
- 没有修改真实个人文件夹注册信息、执行 DISM 清理、回收用户目录或覆盖 EXE。

建议顺序：先修复扫描身份识别和真实个人文件夹路径，再修复成功刷新与占用检测缺失分支；随后用有效的项目解释器跑完 UI 测试，补齐本版计划开放的 DISM 入口、统一版本号并重新打包验收。
