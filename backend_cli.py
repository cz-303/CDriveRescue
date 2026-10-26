"""Backend CLI. Clean defaults to preview; mutations require explicit flags."""
from __future__ import annotations
import argparse
import json
import sys
from dataclasses import asdict
from pathlib import Path
from src.core.app_mover import AppMover
from src.core.cache_cleaner import CacheCleaner
from src.core.contracts import MigrationTask


def main(argv=None):
    parser = argparse.ArgumentParser(description="CDriveRescue 后端工具；清理默认只预演")
    parser.add_argument("--state-dir", help="迁移记录目录")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("scan", help="只读扫描缓存")
    clean = commands.add_parser("clean", help="预演指定规则；--execute 才执行")
    clean.add_argument("--ids", required=True, help="逗号分隔的规则 ID")
    clean.add_argument("--execute", action="store_true")
    clean.add_argument("--permanent", action="store_true", help="永久删除；默认移入回收站")
    move = commands.add_parser("move", help="复制、校验并建立目录联接")
    move.add_argument("source")
    move.add_argument("target_root")
    move.add_argument("--execute", action="store_true", required=True)
    restore = commands.add_parser("restore", help="还原并保留目标盘副本")
    restore.add_argument("record_id")
    restore.add_argument("--execute", action="store_true", required=True)
    commands.add_parser("records", help="读取迁移记录")
    recover = commands.add_parser("recover", help="恢复未完成事务")
    recover.add_argument("--execute", action="store_true", required=True)
    args = parser.parse_args(argv)
    try:
        if args.command in ("scan", "clean"):
            cleaner = CacheCleaner()
            items = cleaner.scan_caches()
            if args.command == "scan":
                result = {"items": [i.to_dict() for i in items], "scan_errors": cleaner.scan_errors}
            else:
                ids = {part.strip() for part in args.ids.split(",") if part.strip()}
                if not ids or not ids <= {rule["id"] for rule in cleaner.rules}:
                    raise ValueError("清理规则 ID 为空或未知")
                summary = cleaner.clean_selected([i for i in items if i.id in ids], not args.permanent, not args.execute)
                result = asdict(summary)
                result["space_note"] = ("预演体积，未释放空间" if summary.dry_run else
                                        "移入回收站，尚未释放磁盘空间" if summary.used_recycle_bin else
                                        "文件逻辑体积；实际可用空间变化可能不同")
                result["scan_errors"] = cleaner.scan_errors
        else:
            mover = AppMover(args.state_dir)
            if args.command == "records":
                result = [r.to_dict() for r in mover.list_migration_records()]
            elif args.command == "recover":
                result = {"message": mover.recover_pending()}
            elif args.command == "restore":
                result = mover.restore_app(args.record_id).to_dict()
            else:
                task = MigrationTask("cli", Path(args.source).name, args.source, args.target_root, 0, 0, "用户选择")
                result = mover.move_app_atomic(task, args.target_root).to_dict()
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 1 if isinstance(result, dict) and result.get("errors") else 0
    except Exception as exc:
        print(json.dumps({"error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 1


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    raise SystemExit(main())
