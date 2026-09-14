"""stocklab 命令行入口。

用法:
    .venv/bin/python -m stocklab.cli.main db init [--db PATH]

退出码：0 成功 / 2 用法错误 / 1 运行失败。
项目内不实现任何定时/守护进程（ADR-001 D-05），本入口只做一次性动作。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from stocklab.config.paths import DB_PATH, ensure_dirs
from stocklab.store.migrate import init_db


def _cmd_db_init(args: argparse.Namespace) -> int:
    db_path = Path(args.db) if args.db else DB_PATH
    backup_dir = Path(args.backup_dir) if args.backup_dir else None
    backup = init_db(db_path, backup_dir=backup_dir)
    if backup is None:
        print(f"✅ 首次建库: {db_path}")
    else:
        print(f"✅ 已前滚迁移: {db_path}")
        print(f"   迁移前备份: {backup}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="stocklab", description="stock-lab CLI")
    sub = parser.add_subparsers(dest="command")

    db = sub.add_parser("db", help="数据库相关操作")
    db_sub = db.add_subparsers(dest="db_command")
    db_init = db_sub.add_parser("init", help="建库 / 前滚迁移（迁移前自动备份）")
    db_init.add_argument("--db", help="数据库路径（默认 data/stocklab.db）")
    db_init.add_argument("--backup-dir", help="备份目录（默认 <db 所在目录>/backups）")
    db_init.set_defaults(func=_cmd_db_init)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:      # argparse 用法错误 / --help：转成返回码，便于调用方判断
        return int(exc.code or 0)
    func = getattr(args, "func", None)
    if func is None:
        parser.print_usage(sys.stderr)
        return 2
    if not args.db:
        ensure_dirs()
    return func(args)


if __name__ == "__main__":
    raise SystemExit(main())
