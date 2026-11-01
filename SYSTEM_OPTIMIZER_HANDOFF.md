# SystemOptimizer v1.1 交接说明

实现位于 `src/core/system_optimizer.py`，继承现有 `ISystemOptimizer`，不修改 Gemini 的数据契约，不新增第三方依赖。导入方式：

```python
from src.core.system_optimizer import SystemOptimizer

optimizer = SystemOptimizer()
```

三个方法都是同步接口，UI 应放在工作线程执行。进度回调遵循现有 `(percent, message, bytes_processed)` 约定，回调抛出普通异常不会破坏文件事务。线程锁和当前用户会话内的 Windows 命名互斥量阻止本引擎并行修改。

## 硬链接去重

- 每对文件首先调用 `SafetyGuard.validate_for_hardlink`，返回 `False` 时跳过。
- 额外检查本地 NTFS、实际卷标识、完整 SHA-256、文件身份、访问权限和附加数据流。读取期间使用 Windows 共享模式阻止内容写入。
- 按 `副本 → 副本._hl_bak → os.link → 身份/大小/内容校验 → 删除备份` 执行。失败时撤销本次创建的链接，再恢复原文件。
- 已有 `._hl_bak` 不会覆盖。回滚遇到路径被其他程序占用时，保留原备份和新占用文件，并在 `summary.errors` 中返回恢复路径。
- 拒绝数据库、已具有多个链接的副本、只读/压缩/稀疏/加密/离线文件、重解析点、附加流或权限不一致的文件。其他文件可继续处理，错误集中返回。
- `dry_run=True` 仍完成安全检查，不重命名、不建链；现有契约没有预演字段，因此 `linked_files_count` 和 `saved_bytes` 表示预计值。实际执行的 `saved_bytes` 也只是被替换文件的逻辑大小，不是磁盘可用空间的实测差值。

硬链接之后，两条路径共享内容和文件元数据，包括后续写入；原副本的独立时间戳不会保留。应仅用于不再修改的附件。多步文件操作不是断电原子事务，不能承诺所有故障下“100% 零损坏”。进程被强制结束时，固定后缀备份会保留；再次遇到该备份会停止处理该文件，不能直接删除后重试。

## 个人文件夹迁移

仅接受 Downloads、Desktop、Documents、Videos、Pictures 及匹配的 GUID。通过 `SHGetKnownFolderPath` 获取真实位置；传入条目过期时拒绝操作，避免移动错误目录。

目标须为本地 NTFS、不存在或为空，父目录须存在。拒绝源目标嵌套、与其他个人文件夹重叠、OneDrive/AppData 管理范围，以及含重解析点、硬链接、附加数据流等无法完整迁移的源目录。

执行空间检查后，先复制并核对全文哈希及源清单，再将源目录改名为事务备份，调用 `SHSetKnownFolderPath` 并读回确认，最后逐文件验证、清理备份。复制保留文件时间戳及所有者/组/DACL；不宣称保留 SACL、所有 NTFS 元数据或目录时间戳。不会直接修改注册表来绕过 Shell API 的策略失败。

`move_existing_files=False` 只更改位置，原文件保留；返回条目的大小和文件数对应新空目录。失败回滚也保留已复制到目标的文件，不覆盖目标内容。

持久事务日志默认位于用户主目录 `.cdriverescue/known_folder_transaction.json`；可通过构造参数 `state_dir` 或 `CDR_STATE_DIR` 配置。存在未结束日志时，必须先调用：

```python
message = optimizer.recover_known_folder_transaction()
```

恢复方法会还原未提交的目录切换；已提交的迁移保留目标位置和剩余备份，不自动删除崩溃后的副本。迁移已提交但备份清理失败时，方法返回新的 `KnownFolderItem`，同时将警告写入 `optimizer.last_warnings` 并保留日志。UI 必须展示警告和恢复提示。

## WinSxS 清理

检查管理员权限，从 Windows 系统目录定位 `Dism.exe`，隐藏子进程窗口，流式执行：

```text
Dism.exe /Online /Cleanup-Image /StartComponentCleanup /NoRestart
```

解析回车刷新的百分比、普通换行、小数点/逗号及 UTF-16/UTF-8 BOM/OEM 输出；执行成功前进度最多 99%。退出码 0 或 3010 表示成功，3010 明确提示需要重启。错误返回退出码和末尾输出。不使用 `/ResetBase`，不自动重启，也不强制终止 servicing 子进程。

## 验证与当前集成问题

```powershell
python -m unittest discover -s tests -p test_system_optimizer.py -v
python -m py_compile src/core/system_optimizer.py tests/test_system_optimizer.py
```

2026-10-29：专项 41 项全部通过。使用真实 NTFS 临时文件验证建链、内容锁、附加流拒绝、跨卷拒绝、失败回滚和迁移复制；文件夹配置写入与 DISM 子进程均模拟，未改动真实个人文件夹或执行系统清理。另对五个真实 `SHGetKnownFolderPath` 进行了只读验证。

全量 `unittest discover` 尚未通过。已有文件中的集成问题包括：

- 当前 `SafetyGuard` 删除了旧引擎仍调用的 `create_junction`，导致迁移测试错误；既有路径规范化及进程占用检测测试也失败。
- `DiskAnalyzer` 的硬链接重复扫描测试仍将已链接副本计入列表。
- 一个旧符号链接测试因当前权限不足报 WinError 1314；当前 Python 未装 PyQt6，10 项 UI 测试跳过。
- 当前个人文件夹扫描按用户主目录拼接路径，不能可靠反映已重定向或 OneDrive 管理的目录。UI/扫描器应改为使用 Windows 真实已知文件夹路径；本引擎会拒绝过期条目。

本次新增模块、专项测试和本文档，未覆盖上述已有文件的改动，也未重新打包 EXE。正式集成还需要在 UI 工作线程接入三种操作及恢复/警告展示。

## Windows API 参考

- [CreateHardLinkW：共享写入、同卷限制](https://learn.microsoft.com/en-us/windows/win32/api/winbase/nf-winbase-createhardlinkw)
- [SHSetKnownFolderPath](https://learn.microsoft.com/en-us/windows/win32/api/shlobj_core/nf-shlobj_core-shsetknownfolderpath)
- [SHGetKnownFolderPath](https://learn.microsoft.com/en-us/windows/win32/api/shlobj_core/nf-shlobj_core-shgetknownfolderpath)
- [WinSxS 组件存储清理](https://learn.microsoft.com/en-us/windows-hardware/manufacture/desktop/clean-up-the-winsxs-folder)
