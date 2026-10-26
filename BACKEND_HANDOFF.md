# 后端交接说明

2026-10-26。本次交付缓存清理、软件迁移与还原后端，保持 Gemini 的 `contracts.py` 接口不变。界面仍由 Claude 接入。

## 文件与接入

| 文件 | 职责 |
| --- | --- |
| `src/core/cache_rules.json` | 7 条保守匹配规则：用户 Temp、Chrome、Edge、Firefox、pip、npm、旧版微信 Cache |
| `src/core/cache_cleaner.py` | 扫描快照、预演、回收站与永久清理 |
| `src/core/app_mover.py` | 校验复制、NTFS 联接、事务记录、还原及中断恢复 |
| `src/core/file_ops.py` | 严格目录清单、SHA-256、权限复制与 JSON 原子替换 |
| `backend_cli.py` | 无界面时的使用入口 |
| `tests/test_engines.py` | 29 项新增行为与故障测试 |

`main.py` 已能以无参构造加载 `CacheCleaner()`、`AppMover()`。构造器额外接受测试/部署配置：`CacheCleaner(rules_path=...)`、`AppMover(state_dir=...)`。默认记录目录为 `~/.cdriverescue`，可用 `CDR_STATE_DIR` 修改。

## Claude 接口注意事项

1. 在后台线程调用扫描与磁盘操作，进度回调保持 `(percent, current_action, accumulated_bytes)`。回调失败会记日志，不会中断磁盘事务。
2. **清理必须复用同一个 CacheCleaner 实例和其最近扫描返回的 CacheItem。** 不要修改或自行拼接对象。新一轮扫描会使旧结果失效；实际清理后应重新扫描。预演不消耗快照。
3. 用户勾选通过选择 items 列表表达，不修改 `selected_by_default`。CAUTION 不默认勾选；展示规则描述和扫描错误 `cleaner.scan_errors`。
4. `CleanSummary.cleaned_bytes` 是处理文件的逻辑体积。`dry_run=True` 不能显示为已释放；`used_recycle_bin=True` 必须显示为移入回收站，不能显示为磁盘已腾空。永久删除也不保证逻辑字节数等于可用空间变化。
5. 清理期间新增、改动、替换的文件会跳过。回收站失败不会降级为永久删除。`skipped_locked_files` 包括权限拒绝，不能把所有此类错误都断言为文件占用。
6. 迁移前让用户关闭相关应用；不要默认结束进程。`psutil` 缺失会阻止迁移。扫描进程是尽力检测，不是所有句柄都可见，也不是对并发修改的排他锁。
7. 提供 `list_migration_records()`、`restore_app(record_id)`，并展示 `error_message`。BROKEN 表示联接或目标失效，不代表自动修复。
8. `pending_transaction.json` 存在时新迁移/还原会阻止。展示恢复入口，调用额外 API `recover_pending()`；该方法保留有歧义的副本，不会猜测性删除。
9. 还原成功后**保留目标盘副本**；向用户说明需确认软件正常后自行处理。异常回滚同样保留未完成副本，可能需要用户后续释放空间。
10. `MigrationRollbackError.rolled_back_cleanly=False` 时应明确显示需要人工检查，不得宣称已无损恢复。提交后清理失败返回成功记录和提示，并保留事务日志/备份。

## 迁移行为和边界

- 仅支持 Windows 固定 NTFS 卷上的普通目录；可以同卷迁移，但这不会释放该卷空间。
- 不覆盖已有目标、不跟随重解析点，拒绝硬链接、稀疏/EFS 文件和备用数据流。含 `Zone.Identifier` 的文件也会被保守拒绝。
- 校验目录清单、文件数、字节数、修改时间及 SHA-256；任何读取/复制错误都不能作为成功跳过。
- 复制所有者、组和有效 DACL；目标权限被冻结为受保护 DACL，避免继承较宽的目标目录权限。这会改变后续权限继承行为。不能设置权限时迁移失败；不承诺保留 SACL 审计、所有 NTFS 元数据或软件授权绑定。
- 先复制校验，再重命名源目录备份、建立联接、写入历史，最后才清理备份。跨卷操作不是操作系统原子事务，持久化日志用于中断恢复；不承诺应对任意硬件损坏。
- 假定用户已关闭相关程序、路径由当前用户控制。针对主动并发替换路径的攻击，当前实现不是基于全程句柄锁定的隔离机制。
- 系统组件、服务、驱动、商店应用以及有修复/更新器的软件，不应仅因为目录能被扫描到就承诺兼容。使用应用官方迁移方式优先。
- 缓存规则为初始保守集合，未验证每个软件的当前发行版布局；不匹配时跳过。没有扩大到 QQ、钉钉、飞书、pnpm 等尚无验证路径的目录。

## 验证结果

Windows 11、CPython 3.14.8 下运行 `python -m unittest discover -s tests -v`：**34 项通过，0 项跳过**。

覆盖：扫描后文件变更/新增、伪造扫描对象、重复选择、目录联接隔离、回收站异常不降级、路径穿越、依赖缺失阻止迁移、进程打开文件检测、真实 NTFS 联接、真实跨卷迁移和还原、空目录、同大小内容损坏、空间不足、权限复制失败、历史写入失败、备份清理失败、迁移与还原中断恢复、硬链接/备用流拒绝、损坏历史和失效联接。

迁移测试里的进程占用检查用测试替身替换（测试目录没有实际应用）；回收站失败使用替身注入。文件复制、哈希校验、ACL 设置、目录切换和跨卷联接操作实际执行。没有在真实安装的软件上做兼容性测试，也没有验证真实回收站后端。

`main.py --cli-diagnose` 已验证可加载两个引擎，UI 为 PENDING。当前环境缺少 psutil、Send2Trash、PyQt6、PyInstaller，pip 下载受网络权限阻断；没有生成或验证 EXE。打包脚本已补上动态导入、规则资源和失败退出，并验证缺少 UI 时正确停止。

## 后续验收

在可安装依赖的开发机上安装依赖；使用一次性测试目录验证真实回收站及占用检测；完成 UI 后验证线程、异常提示与打包。按软件逐个验证迁移后的启动、更新、卸载和恢复，再考虑扩大支持范围。

## 界面接入（Claude，2026-10-26）

`src/ui/` 已接入，入口仍为 `main.py` 中的 `MainWindow(disk_analyzer=..., safety_guard=..., cache_cleaner=..., app_mover=...)`。上面 10 条注意事项均已在界面中落实，对应代码在 `src/ui/pages.py` 文件头注释中列出。后端接口未修改；仅为 `disk_analyzer.py` 中三个含反斜杠的文档字符串加了 `r` 前缀，消除 Python 3.12+ 的 SyntaxWarning。

界面测试 `tests/test_ui.py`（9 项）在 Linux 离屏模式下通过，后端为替身。尚未在 Windows 真机上运行图形界面，也未打包 EXE。

发现但未修改的后端问题，供后续处理：

- `DiskAnalyzer.discover_migratable_Directories` 对每个候选目录都调用一次 `find_locking_processes`，后者遍历所有进程的 `open_files()`；候选目录多时扫描会很慢（界面已在后台运行并提示需要几分钟）。缺少 psutil 时它会直接抛出异常而不是跳过占用检查，界面已在缺少 psutil 时禁用该按钮。
- 该方法的 `app_id` 使用 `hash()`，每次启动进程哈希种子不同，ID 不稳定；目前界面只在单次扫描内使用，不受影响。
- `SafetyGuard.terminate_locking_processes` 存在，但按交接要求界面没有提供“结束进程”按钮。
