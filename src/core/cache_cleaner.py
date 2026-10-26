"""Rule-based cleaner: only files in this instance's scan snapshot can be removed."""
from __future__ import annotations

import glob
import json
import os
import time
from pathlib import Path
from threading import RLock

from .contracts import CacheCategory, CacheItem, CleanSummary, ICacheCleaner, RiskLevel
from .file_ops import plain_path, report
from .safety_guard import SafetyGuard


class CacheCleaner(ICacheCleaner):
    def __init__(self, rules_path=None):
        self.rules_path = Path(rules_path) if rules_path else Path(__file__).with_name("cache_rules.json")
        self.rules = json.loads(self.rules_path.read_text(encoding="utf-8"))["rules"]
        self._snapshots = {}
        self.scan_errors = []
        self._lock = RLock()
        ids = set()
        for rule in self.rules:
            if rule["id"] in ids:
                raise ValueError("缓存规则 ID 重复")
            ids.add(rule["id"])
            CacheCategory(rule["category"])
            RiskLevel(rule["risk_level"])
            if not rule["paths"] or rule.get("min_age_hours", 0) < 0:
                raise ValueError("无效缓存规则")

    @staticmethod
    def _signature(path):
        info = path.stat(follow_symlinks=False)
        return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns)

    def _files(self, root, cutoff):
        stack = [root]
        while stack:
            path = stack.pop()
            try:
                plain_path(path)
                SafetyGuard.validate_for_cleaning(str(path))
                if path.is_dir():
                    with os.scandir(path) as entries:
                        stack.extend(Path(entry.path) for entry in entries)
                elif path.is_file():
                    info = path.stat()
                    if info.st_nlink == 1 and info.st_mtime <= cutoff:
                        yield path, self._signature(path)
            except (OSError, ValueError, RuntimeError) as exc:
                self.scan_errors.append(f"{path}: {exc}")
            except Exception as exc:
                self.scan_errors.append(f"跳过 {path}: {exc}")

    def scan_caches(self, progress_cb=None, categories=None):
        with self._lock:
            self._snapshots = {}
            self.scan_errors = []
            results, seen = [], set()
            rules = [r for r in self.rules if categories is None or CacheCategory(r["category"]) in categories]
            for index, rule in enumerate(rules):
                if rule["risk_level"] == "DANGER":
                    continue
                files, roots = {}, []
                cutoff = time.time() - rule.get("min_age_hours", 24) * 3600
                for pattern in rule["paths"]:
                    expanded = os.path.expandvars(os.path.expanduser(pattern))
                    if "%" in expanded or not os.path.isabs(expanded):
                        continue
                    for match in glob.iglob(expanded):
                        root = Path(os.path.abspath(match))
                        roots.append(str(root))
                        for path, signature in self._files(root, cutoff):
                            key = os.path.normcase(str(path))
                            if key not in seen:
                                files[str(path)] = signature
                                seen.add(key)
                if files:
                    size = sum(s[2] for s in files.values())
                    item = CacheItem(rule["id"], rule["name"], CacheCategory(rule["category"]),
                                     roots[0], list(dict.fromkeys(roots)), size, len(files),
                                     RiskLevel(rule["risk_level"]), rule["description"],
                                     rule.get("selected_by_default", False) and rule["risk_level"] == "SAFE")
                    self._snapshots[item.id] = (item.to_dict(), files)
                    results.append(item)
                report(progress_cb, int((index + 1) * 100 / max(1, len(rules))),
                       rule["name"], sum(i.size_bytes for i in results))
            report(progress_cb, 100, "扫描完成", sum(i.size_bytes for i in results))
            return results

    def clean_selected(self, items, use_recycle_bin=True, dry_run=False, progress_cb=None):
        with self._lock:
            summary = CleanSummary(0, 0, 0, use_recycle_bin, dry_run)
            candidates = {}
            for item in items:
                snapshot = self._snapshots.get(item.id)
                if not snapshot or item.to_dict() != snapshot[0]:
                    summary.errors.append(f"{item.id}: 扫描结果已失效或被修改，请重新扫描")
                    continue
                candidates.update(snapshot[1])
            trash = None
            if use_recycle_bin and not dry_run:
                from send2trash import send2trash
                trash = send2trash
            for index, (name, signature) in enumerate(candidates.items()):
                try:
                    path = plain_path(name)
                    SafetyGuard.validate_for_cleaning(str(path))
                    if self._signature(path) != signature or path.stat().st_nlink != 1:
                        raise ValueError("文件自扫描后发生变化，请重新扫描")
                    if not dry_run:
                        if trash:
                            trash(str(path))
                        else:
                            path.unlink()
                    summary.cleaned_bytes += signature[2]
                    summary.cleaned_files_count += 1
                except PermissionError as exc:
                    summary.skipped_locked_files += 1
                    summary.errors.append(f"{name}: {exc}")
                except Exception as exc:
                    summary.errors.append(f"{name}: {exc}")
                report(progress_cb, int((index + 1) * 100 / max(1, len(candidates))), name, summary.cleaned_bytes)
            if not dry_run:
                self._snapshots = {}
            report(progress_cb, 100, "预演完成" if dry_run else "清理完成", summary.cleaned_bytes)
            return summary
