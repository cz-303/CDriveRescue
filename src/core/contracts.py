"""
CDriveRescue (C盘焦虑拯救者) - 核心数据契约与三方接口规范 (Data Contracts v1.1)
===========================================================================
Responsible Model: Gemini (Chief System Architect)
Collaborators:
  - GPT: Implements `ICacheCleaner` (`src/core/cache_cleaner.py`),
         `IAppMover` (`src/core/app_mover.py`), and
         [NEW v1.1] `ISystemOptimizer` (`src/core/system_optimizer.py`)
  - Claude: Implements PyQt6 UI (`src/ui/main_window.py`, `src/ui/widgets.py`, `src/ui/workers.py`)

v1.1 Step-1 Upgrade Highlights:
1. `DuplicateGroup` & `HardlinkDedupeSummary`: For WeChat/QQ multi-received identical file
   deduplication via NTFS Hardlink (`os.link` / `mklink /H`) — zero file loss, instant space recovery.
2. `KnownFolderItem`: For Windows native personal folder migration (`Downloads`, `Desktop`,
   `Documents`, `Videos`, `Pictures`) via `SHSetKnownFolderPath`.
3. `OrphanResidueItem`: For uninstalled software orphan directory detection in `AppData` / `ProgramData`.
4. `ISystemOptimizer`: New abstract interface for GPT to implement v1.1 deep optimization operations.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Callable, Dict, List, Optional, Tuple


# ===========================================================================
# 1. 通用回调协议 (Progress Callback Signature)
# ===========================================================================
# 参数依次为:
#   - percent (int): 进度百分比 (0 ~ 100)
#   - current_action (str): 当前正在扫描/处理的路径或状态描述 (供 UI 进度条副标题展示)
#   - accumulated_bytes (int): 当前已累计发现/已释放/已迁移的字节数
ProgressCallback = Callable[[int, str, int], None]


# ===========================================================================
# 2. 核心枚举定义 (Core Enumerations)
# ===========================================================================
class RiskLevel(str, Enum):
    """清理或迁移操作的安全风险评级（供 GPT 规则库标注 & Claude 渲染不同颜色徽章）"""
    SAFE = "SAFE"          # 绿色放心清：零副作用（如系统临时文件、浏览器图片缓存、pip/npm 缓存、硬链接去重）
    CAUTION = "CAUTION"    # 黄色谨慎清：可能清除历史下载或需重新加载数据（如微信接收文件、卸载残留疑似项）
    DANGER = "DANGER"      # 红色禁止动：系统核心目录或运行中数据库，由 SafetyGuard 强制拦截


class CacheCategory(str, Enum):
    """缓存清理分类枚举（对应 Claude 清理页的四大折叠分组）"""
    SYSTEM = "SYSTEM"          # Windows 系统级缓存 (Temp, 更新残留, 缩略图, 休眠文件, 错误转储)
    SOCIAL_OFFICE = "SOCIAL_OFFICE"  # 国内高频社交与办公软件 (微信, QQ, 钉钉, 飞书, 企业微信)
    BROWSER = "BROWSER"        # 主流浏览器缓存 (Chrome, Edge, Firefox, Brave - 严禁触碰密码与 Cookie)
    DEVELOPER = "DEVELOPER"    # 开发者工具缓存 (pip, npm, pnpm, NuGet, Gradle, JetBrains, Docker/WSL提示)


class AnxietyLevel(str, Enum):
    """C盘空间焦虑指数等级（对应 Claude 首页仪表盘主题动态变色）"""
    HEALTHY = "HEALTHY"        # 翡翠绿 (焦虑指数 0~39，可用空间充足 > 25%)
    MODERATE = "MODERATE"      # 琥珀黄 (焦虑指数 40~74，可用空间告急 10%~25%)
    CRITICAL = "CRITICAL"      # 珊瑚红 (焦虑指数 75~100，C盘飘红 < 10% 或不足 15GB)


class MigrationStatus(str, Enum):
    """NTFS Junction (mklink /J) 软件搬家任务状态枚举"""
    READY = "READY"            # 可以安全搬家
    LOCKED = "LOCKED"          # 存在正在运行的进程占用，需先关闭软件
    MOVING = "MOVING"          # 正在执行五步原子搬家事务
    MOVED = "MOVED"            # 已成功搬家并建立 NTFS Junction 软链接
    BROKEN = "BROKEN"          # 软链接目标丢失或损坏
    RESTORED = "RESTORED"      # 已无损迁回 C 盘原位置
    FAILED_ROLLED_BACK = "FAILED_ROLLED_BACK"  # 搬家途中异常，已自动无损回滚


# ===========================================================================
# 3. 通用字节格式化工具函数 (Utility Helpers)
# ===========================================================================
def format_bytes(size_bytes: int, precision: int = 2) -> str:
    """将字节数转换为人类可读的 KB / MB / GB / TB 字符串"""
    if size_bytes < 0:
        return "0 B"
    if size_bytes < 1024:
        return f"{size_bytes} B"
    units = ["B", "KB", "MB", "GB", "TB", "PB"]
    value = float(size_bytes)
    unit_index = 0
    while value >= 1024.0 and unit_index < len(units) - 1:
        value /= 1024.0
        unit_index += 1
    return f"{value:.{precision}f} {units[unit_index]}"


# ===========================================================================
# 4. v1.0 核心数据模型 (Dataclasses)
# ===========================================================================
@dataclass
class ProcessLockInfo:
    """正在占用目标目录的进程信息（由 Gemini SafetyGuard 检出，供 Claude 弹窗提醒用户一键关闭）"""
    pid: int
    name: str
    exe_path: str
    locked_target: str

    def to_dict(self) -> Dict:
        return asdict(self)


@dataclass
class LargeFileItem:
    """大文件猎手扫描结果项（由 Gemini DiskAnalyzer 生成）"""
    path: str
    name: str
    extension: str
    size_bytes: int
    modified_timestamp: float
    risk_level: RiskLevel
    suggestion: str

    @property
    def size_human(self) -> str:
        return format_bytes(self.size_bytes)

    def to_dict(self) -> Dict:
        data = asdict(self)
        data["size_human"] = self.size_human
        return data


@dataclass
class SystemSlimItem:
    """系统级大块头专项瘦身项（如休眠文件 hiberfil.sys、虚拟内存 pagefile.sys、WinSxS 组件存储）"""
    id: str
    name: str
    path: str
    size_bytes: int
    exists: bool
    can_slim: bool
    command_hint: str        # 例如 "powercfg -h off" 或 "Dism.exe /Online /Cleanup-Image /StartComponentCleanup"
    description: str         # 白话文解释对系统的影响

    @property
    def size_human(self) -> str:
        return format_bytes(self.size_bytes)


@dataclass
class DriveHealthReport:
    """C盘空间健康度与焦虑诊断报告（由 Gemini DiskAnalyzer 生成，供 Claude 仪表盘渲染）"""
    drive_letter: str
    total_bytes: int
    used_bytes: int
    free_bytes: int
    usage_percent: float
    anxiety_score: int               # 0 ~ 100 分，分数越高代表 C 盘越危急
    anxiety_level: AnxietyLevel
    headline_diagnosis: str          # 一句话治愈诊断结论
    recommendations: List[str] = field(default_factory=list)
    system_slim_items: List[SystemSlimItem] = field(default_factory=list)
    available_target_drives: List[Tuple[str, int]] = field(default_factory=list)

    @property
    def total_human(self) -> str:
        return format_bytes(self.total_bytes)

    @property
    def used_human(self) -> str:
        return format_bytes(self.used_bytes)

    @property
    def free_human(self) -> str:
        return format_bytes(self.free_bytes)


@dataclass
class CacheItem:
    """单项缓存扫描结果（由 GPT CacheCleaner 生成，供 Claude 清理页列表展示）"""
    id: str
    name: str
    category: CacheCategory
    path: str
    matched_paths: List[str]
    size_bytes: int
    file_count: int
    risk_level: RiskLevel
    description: str
    selected_by_default: bool = True

    @property
    def size_human(self) -> str:
        return format_bytes(self.size_bytes)

    def to_dict(self) -> Dict:
        data = asdict(self)
        data["size_human"] = self.size_human
        return data


@dataclass
class CleanSummary:
    """缓存清理执行结果汇总（由 GPT CacheCleaner 返回，供 Claude 弹出治愈战报）"""
    cleaned_bytes: int
    cleaned_files_count: int
    skipped_locked_files: int
    used_recycle_bin: bool
    dry_run: bool
    errors: List[str] = field(default_factory=list)

    @property
    def cleaned_human(self) -> str:
        return format_bytes(self.cleaned_bytes)


@dataclass
class MigrationTask:
    """待搬家软件/大目录候选项（由 Gemini/GPT 扫描生成，供 Claude 软件搬家向导页展示）"""
    app_id: str
    app_name: str
    source_path: str
    recommended_target_root: str
    size_bytes: int
    file_count: int
    category_label: str
    is_already_junction: bool = False
    junction_target: Optional[str] = None
    running_locks: List[ProcessLockInfo] = field(default_factory=list)

    @property
    def size_human(self) -> str:
        return format_bytes(self.size_bytes)

    @property
    def status(self) -> MigrationStatus:
        if self.is_already_junction:
            return MigrationStatus.MOVED
        if self.running_locks:
            return MigrationStatus.LOCKED
        return MigrationStatus.READY


@dataclass
class MigrationRecord:
    """已完成的软件搬家持久化记录（写入 ~/.cdriverescue/migration_history.json）"""
    record_id: str
    app_name: str
    source_path: str
    target_path: str
    size_bytes: int
    file_count: int
    created_at: str
    status: MigrationStatus = MigrationStatus.MOVED
    error_message: Optional[str] = None

    @property
    def size_human(self) -> str:
        return format_bytes(self.size_bytes)

    def to_dict(self) -> Dict:
        data = asdict(self)
        data["status"] = self.status.value
        data["size_human"] = self.size_human
        return data

    @classmethod
    def from_dict(cls, data: Dict) -> "MigrationRecord":
        status_raw = data.get("status", MigrationStatus.MOVED.value)
        return cls(
            record_id=data["record_id"],
            app_name=data["app_name"],
            source_path=data["source_path"],
            target_path=data["target_path"],
            size_bytes=int(data.get("size_bytes", 0)),
            file_count=int(data.get("file_count", 0)),
            created_at=data.get("created_at", ""),
            status=MigrationStatus(status_raw),
            error_message=data.get("error_message"),
        )


# ===========================================================================
# 5. [NEW v1.1] 进阶深水区治理数据契约 (v1.1 Step-1 Data Contracts)
# ===========================================================================
@dataclass
class DuplicateGroup:
    """
    [v1.1 新增] 相同内容重复文件组（专为微信/QQ 群聊转发多次的相同视频/PDF/安装包设计）
    原理：通过将 duplicate_paths 替换为指向 master_path 的 NTFS 硬链接 (`os.link` / `mklink /H`)，
    微信聊天窗口依然能正常打开每一个文件，但物理磁盘只占 1 份空间！
    """
    group_id: str
    content_hash: str                # 首尾 64KB + 全文 SHA256 复合指纹
    file_name_sample: str            # 代表性文件名 (如 "2026产品发布会宣传片.mp4")
    single_size_bytes: int           # 单个文件字节大小
    master_path: str                 # 保留物理实体的主文件路径
    duplicate_paths: List[str]       # 其余具有完全相同内容但不同路径/命名的副本列表
    source_app: str = "WeChat/QQ"    # 所属应用标识

    @property
    def total_copies(self) -> int:
        return 1 + len(self.duplicate_paths)

    @property
    def savable_bytes(self) -> int:
        """建立 NTFS 硬链接后可无损省下的物理空间 = 单个体积 * 副本数"""
        return self.single_size_bytes * len(self.duplicate_paths)

    @property
    def savable_human(self) -> str:
        return format_bytes(self.savable_bytes)

    def to_dict(self) -> Dict:
        data = asdict(self)
        data["savable_bytes"] = self.savable_bytes
        data["savable_human"] = self.savable_human
        return data


@dataclass
class HardlinkDedupeSummary:
    """[v1.1 新增] 微信/QQ 重复文件 NTFS 硬链接去重执行结果"""
    processed_groups: int
    linked_files_count: int
    saved_bytes: int
    skipped_cross_volume: int
    errors: List[str] = field(default_factory=list)

    @property
    def saved_human(self) -> str:
        return format_bytes(self.saved_bytes)


@dataclass
class KnownFolderItem:
    """
    [v1.1 新增] Windows 系统个人文件夹（桌面/下载/文档/视频/图片）原生搬家项
    通过 Windows Shell API `SHSetKnownFolderPath` 迁移，而非普通软链接，与资源管理器 100% 原生兼容。
    """
    folder_key: str                  # "Downloads" | "Desktop" | "Documents" | "Videos" | "Pictures"
    display_name: str                # 如 "下载 (Downloads)"
    folder_guid: str                 # Windows KNOWNFOLDERID GUID
    current_path: str                # 当前所在路径 (如 C:\Users\Alice\Downloads)
    recommended_target_path: str     # 推荐迁移路径 (如 D:\Users\Alice\Downloads)
    size_bytes: int
    file_count: int
    is_on_system_drive: bool

    @property
    def size_human(self) -> str:
        return format_bytes(self.size_bytes)


@dataclass
class OrphanResidueItem:
    """
    [v1.1 新增] 卸载残留猎手（AppData / ProgramData 孤儿文件夹）扫描结果项
    对比 Windows 注册表 Uninstall 列表与当前安装目录，揪出“软件早已卸载、残留数据仍在占空间”的目录。
    """
    orphan_id: str
    folder_name: str
    path: str
    location_scope: str              # "AppData\\Local" | "AppData\\Roaming" | "ProgramData"
    size_bytes: int
    file_count: int
    last_modified_days_ago: int      # 距离最后一次修改过去了多少天
    confidence_level: RiskLevel      # SAFE (高置信度已卸载残留) / CAUTION (长期未动用疑似残留)
    reason: str                      # 判定依据说明（白话文）

    @property
    def size_human(self) -> str:
        return format_bytes(self.size_bytes)


# ===========================================================================
# 6. 核心安全与事务异常定义 (Custom Exceptions)
# ===========================================================================
class CDriveRescueError(Exception):
    """CDriveRescue 基础异常类"""


class CriticalPathForbiddenError(CDriveRescueError):
    """当尝试清理或移动 Windows 核心受保护目录时由 SafetyGuard 抛出"""


class ProcessLockedError(CDriveRescueError):
    """当目标目录存在正在运行的进程锁定时抛出"""
    def __init__(self, message: str, locks: List[ProcessLockInfo]):
        super().__init__(message)
        self.locks = locks


class InsufficientTargetSpaceError(CDriveRescueError):
    """当目标磁盘（如 D 盘）剩余空间不足源目录大小的 110% 时抛出"""


class MigrationRollbackError(CDriveRescueError):
    """当 mklink /J 搬家失败但已成功执行原子回滚（或回滚异常）时抛出"""
    def __init__(self, message: str, rolled_back_cleanly: bool = True):
        super().__init__(message)
        self.rolled_back_cleanly = rolled_back_cleanly


# ===========================================================================
# 7. 供 GPT 实现的抽象引擎接口 (Abstract Interfaces for GPT Modules)
# ===========================================================================
class ICacheCleaner(ABC):
    """GPT 负责实现的缓存扫描与清理器接口 (`src/core/cache_cleaner.py`)"""

    @abstractmethod
    def scan_caches(
        self,
        progress_cb: Optional[ProgressCallback] = None,
        categories: Optional[List[CacheCategory]] = None,
    ) -> List[CacheItem]:
        raise NotImplementedError

    @abstractmethod
    def clean_selected(
        self,
        items: List[CacheItem],
        use_recycle_bin: bool = True,
        dry_run: bool = False,
        progress_cb: Optional[ProgressCallback] = None,
    ) -> CleanSummary:
        raise NotImplementedError


class IAppMover(ABC):
    """GPT 负责实现的 NTFS Junction 无损软件搬家引擎接口 (`src/core/app_mover.py`)"""

    @abstractmethod
    def move_app_atomic(
        self,
        task: MigrationTask,
        target_root_dir: str,
        progress_cb: Optional[ProgressCallback] = None,
    ) -> MigrationRecord:
        raise NotImplementedError

    @abstractmethod
    def restore_app(
        self,
        record_id: str,
        progress_cb: Optional[ProgressCallback] = None,
    ) -> MigrationRecord:
        raise NotImplementedError

    @abstractmethod
    def list_migration_records(self) -> List[MigrationRecord]:
        raise NotImplementedError


class ISystemOptimizer(ABC):
    """
    [NEW v1.1] GPT 负责实现的系统深水区专项瘦身引擎接口 (`src/core/system_optimizer.py`)
    涵盖三大 v1.1 杀手锏功能：
    1. 微信/QQ 重复文件 NTFS 硬链接 (`mklink /H` / `os.link`) 无损去重
    2. Windows 系统个人文件夹（桌面/下载/文档/视频）原生 API 重定向与文件迁移
    3. WinSxS 系统更新组件库 (`Dism.exe /StartComponentCleanup`) 异步安全瘦身
    """

    @abstractmethod
    def deduplicate_via_hardlink(
        self,
        groups: List[DuplicateGroup],
        dry_run: bool = False,
        progress_cb: Optional[ProgressCallback] = None,
    ) -> HardlinkDedupeSummary:
        """
        将 DuplicateGroup 中的副本文件原子替换为指向 master_path 的 NTFS 硬链接：
        先将副本重命名为 `.hl_bak` -> 调用 `os.link(master_path, dup_path)` -> 校验 inode 一致后删除 `.hl_bak`。
        若失败立即将 `.hl_bak` 还原，确保聊天记录中的文件 100% 可正常点开！
        """
        raise NotImplementedError

    @abstractmethod
    def relocate_known_folder(
        self,
        item: KnownFolderItem,
        target_path: str,
        move_existing_files: bool = True,
        progress_cb: Optional[ProgressCallback] = None,
    ) -> KnownFolderItem:
        """
        调用 Windows Shell32 `SHSetKnownFolderPath` 将下载/桌面/文档等系统文件夹原生改绑至 D/E/F 盘，
        并将原目录下的文件安全平滑迁移至新目录。
        """
        raise NotImplementedError

    @abstractmethod
    def slim_winsxs_component_store(
        self,
        progress_cb: Optional[ProgressCallback] = None,
    ) -> Tuple[bool, str]:
        """执行 `Dism.exe /Online /Cleanup-Image /StartComponentCleanup` 安全清理过期的系统更新备份包"""
        raise NotImplementedError
