"""
CDriveRescue (C盘焦虑拯救者) - 核心数据契约与三方接口规范 (Data Contracts)
===========================================================================
Responsible Model: Gemini (Chief System Architect)
Collaborators:
  - GPT: Implements `ICacheCleaner` (`src/core/cache_cleaner.py`) & `IAppMover` (`src/core/app_mover.py`)
  - Claude: Implements PyQt6 UI (`src/ui/main_window.py`, `src/ui/widgets.py`, `src/ui/workers.py`)

This module locks down all cross-module data structures, enums, callback signatures,
custom exceptions, and abstract base classes so that Gemini, GPT, and Claude code
can be dropped into the same project directory and run with zero interface conflicts.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field, asdict
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
    SAFE = "SAFE"          # 绿色放心清：零副作用（如系统临时文件、浏览器图片缓存、pip/npm 缓存）
    CAUTION = "CAUTION"    # 黄色谨慎清：可能清除历史下载或需重新加载数据（如微信接收文件、Windows更新回滚包）
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
# 4. 核心数据模型 (Dataclasses - 跨模块通信载体)
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
    suggestion: str  # 例如："孤立安装包 (.iso/.exe)，安装完毕后可安全删除或移至 D 盘"

    @property
    def size_human(self) -> str:
        return format_bytes(self.size_bytes)

    def to_dict(self) -> Dict:
        data = asdict(self)
        data["size_human"] = self.size_human
        return data


@dataclass
class SystemSlimItem:
    """系统级大块头专项瘦身项（如休眠文件 hiberfil.sys、虚拟内存 pagefile.sys）"""
    id: str
    name: str
    path: str
    size_bytes: int
    exists: bool
    can_slim: bool
    command_hint: str        # 例如 "powercfg -h off"
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
    available_target_drives: List[Tuple[str, int]] = field(default_factory=list)  # 例如 [("D:\\", free_bytes)]

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
    path: str                        # 展开环境变量后的真实目录路径（或主路径）
    matched_paths: List[str]         # 实际匹配到的所有子路径列表
    size_bytes: int
    file_count: int
    risk_level: RiskLevel
    description: str                 # 白话文清理影响说明（如："仅清理微信图片视频缩略图缓存，不删聊天记录"）
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
    source_path: str                 # C 盘原路径，如 C:\Program Files\JetBrains\PyCharm
    recommended_target_root: str     # 推荐目标盘根目录，如 D:\CDriveRescue_Moved
    size_bytes: int
    file_count: int
    category_label: str              # 如 "大型开发工具" / "微信聊天数据目录" / "独立游戏客户端"
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
    source_path: str                 # C 盘原位置（现为 Junction 软链接）
    target_path: str                 # D/E 盘真实物理存储位置
    size_bytes: int
    file_count: int
    created_at: str                  # ISO-8601 时间戳
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
# 5. 核心安全与事务异常定义 (Custom Exceptions)
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
# 6. 供 GPT 实现的抽象引擎接口 (Abstract Interfaces for GPT Modules)
# ===========================================================================
class ICacheCleaner(ABC):
    """GPT 负责实现的缓存扫描与清理器接口 (`src/core/cache_cleaner.py`)"""

    @abstractmethod
    def scan_caches(
        self,
        progress_cb: Optional[ProgressCallback] = None,
        categories: Optional[List[CacheCategory]] = None,
    ) -> List[CacheItem]:
        """根据 cache_rules.json 扫描所有匹配的缓存项并计算体积"""
        raise NotImplementedError

    @abstractmethod
    def clean_selected(
        self,
        items: List[CacheItem],
        use_recycle_bin: bool = True,
        dry_run: bool = False,
        progress_cb: Optional[ProgressCallback] = None,
    ) -> CleanSummary:
        """清理用户勾选的缓存项（支持移入回收站或干跑测试）"""
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
        """
        执行五步原子软件搬家法：
        1. 预检空间 (>= 1.1x) 与 SafetyGuard 进程占用/路径黑名单校验
        2. 完整复制 C 盘源目录至目标盘
        3. 校验文件数量与字节数
        4. 将源目录重命名为 `._backup_tmp`，执行 `mklink /J` 创建目录联接
        5. 验证软链接可用后删除备份并写入 migration_history.json；若异常则自动还原 `._backup_tmp`
        """
        raise NotImplementedError

    @abstractmethod
    def restore_app(
        self,
        record_id: str,
        progress_cb: Optional[ProgressCallback] = None,
    ) -> MigrationRecord:
        """一键无损还原：移除 C 盘软链接本身（不删目标盘数据），将实体文件移回 C 盘原位"""
        raise NotImplementedError

    @abstractmethod
    def list_migration_records(self) -> List[MigrationRecord]:
        """读取并检验所有历史搬家记录及 Junction 软链接存活状态"""
        raise NotImplementedError
