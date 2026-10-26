"""
CDriveRescue Core Foundation Package (Designed & Implemented by Gemini)
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
    IAppMover,
    ICacheCleaner,
    InsufficientTargetSpaceError,
    LargeFileItem,
    MigrationRecord,
    MigrationRollbackError,
    MigrationStatus,
    MigrationTask,
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
    "IAppMover",
    "ICacheCleaner",
    "InsufficientTargetSpaceError",
    "LargeFileItem",
    "MigrationRecord",
    "MigrationRollbackError",
    "MigrationStatus",
    "MigrationTask",
    "ProcessLockInfo",
    "ProcessLockedError",
    "ProgressCallback",
    "RiskLevel",
    "SafetyGuard",
    "SystemSlimItem",
    "format_bytes",
]
