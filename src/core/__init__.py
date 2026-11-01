"""
CDriveRescue Core Foundation Package v1.1 (Designed & Implemented by Gemini)
Exports all shared data contracts, safety guards, and disk analysis engines
for seamless integration with GPT's engines and Claude's PyQt6 UI.
"""

from src.core.contracts import (
    AnxietyLevel,
    CacheCategory,
    CacheItem,
    CDriveRescueError,
    CleanSummary,
    CriticalPathForbiddenError,
    DriveHealthReport,
    DuplicateGroup,
    HardlinkDedupeSummary,
    IAppMover,
    ICacheCleaner,
    InsufficientTargetSpaceError,
    ISystemOptimizer,
    KnownFolderItem,
    LargeFileItem,
    MigrationRecord,
    MigrationRollbackError,
    MigrationStatus,
    MigrationTask,
    OrphanResidueItem,
    ProcessLockInfo,
    ProcessLockedError,
    ProgressCallback,
    RiskLevel,
    SystemSlimItem,
    format_bytes,
)
from src.core.disk_analyzer import DiskAnalyzer
from src.core.safety_guard import SafetyGuard

__all__ = [
    "AnxietyLevel",
    "CacheCategory",
    "CacheItem",
    "CDriveRescueError",
    "CleanSummary",
    "CriticalPathForbiddenError",
    "DiskAnalyzer",
    "DriveHealthReport",
    "DuplicateGroup",
    "HardlinkDedupeSummary",
    "IAppMover",
    "ICacheCleaner",
    "InsufficientTargetSpaceError",
    "ISystemOptimizer",
    "KnownFolderItem",
    "LargeFileItem",
    "MigrationRecord",
    "MigrationRollbackError",
    "MigrationStatus",
    "MigrationTask",
    "OrphanResidueItem",
    "ProcessLockInfo",
    "ProcessLockedError",
    "ProgressCallback",
    "RiskLevel",
    "SafetyGuard",
    "SystemSlimItem",
    "format_bytes",
]
