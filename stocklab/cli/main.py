"""stocklab 命令行入口。

用法:
    .venv/bin/python -m stocklab.cli.main db init [--db PATH]
    .venv/bin/python -m stocklab.cli.main doctor
    .venv/bin/python -m stocklab.cli.main ingest bars [--days N] [--code C]...
    .venv/bin/python -m stocklab.cli.main fixture record --name NAME

退出码：0 成功 / 2 用法错误或库不存在 / 1 运行失败。
项目内不实现任何定时/守护进程（ADR-001 D-05），本入口只做一次性动作。

`ingest bars` 是**唯一联网**的命令；其抓取逻辑在 `stocklab/data/fetch.py`
（分页 + 口径守卫，已有离线回放测试），本模块只负责把它接到配置与数据库上。
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from stocklab.config import paths
from stocklab.config.settings import load_settings
from stocklab.config.universe import DEFAULT_UNIVERSE
from stocklab.store import repo
from stocklab.store.db import connect
from stocklab.store.migrate import init_db

TZ = ZoneInfo("Asia/Shanghai")


def _today() -> str:
    return datetime.now(TZ).date().isoformat()


# ---------- db ----------

def _cmd_db_init(args: argparse.Namespace) -> int:
    db_path = Path(args.db) if args.db else paths.DB_PATH
    backup_dir = Path(args.backup_dir) if args.backup_dir else None
    backup = init_db(db_path, backup_dir=backup_dir)
    if backup is None:
        print(f"✅ 首次建库: {db_path}")
    else:
        print(f"✅ 已前滚迁移: {db_path}")
        print(f"   迁移前备份: {backup}")
    return 0


# ---------- doctor ----------

def cmd_doctor(db_path: Path | None = None) -> int:
    """输出数据健康度 JSON（供人读，也供 nanobot 侧调度判读）。"""
    db = Path(db_path) if db_path else paths.DB_PATH
    if not db.exists():
        print(json.dumps({"error": "db not found; run `stocklab db init`"},
                         ensure_ascii=False))
        return 2
    out: dict = {}
    conn = connect(db)
    try:
        for table in ("instruments", "bars_daily", "features_daily", "predictions",
                      "verifications", "data_quality", "system_events", "job_runs"):
            out[table] = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        row = conn.execute("SELECT MAX(date) AS d FROM bars_daily").fetchone()
        out["latest_bar_date"] = row["d"] if row else None
        out["open_issues"] = conn.execute(
            "SELECT COUNT(*) FROM data_quality WHERE resolved=0").fetchone()[0]
        last_job = conn.execute(
            "SELECT job_name, status, detail, finished_at FROM job_runs"
            " ORDER BY run_id DESC LIMIT 1").fetchone()
        out["last_job"] = dict(last_job) if last_job else None
    finally:
        conn.close()
    print(json.dumps(out, ensure_ascii=False, indent=2))
    return 0


# ---------- ingest ----------

def cmd_ingest_bars(args: argparse.Namespace) -> int:
    """真实抓取日K（联网）。这是 P2 的端到端验收命令。

    数据源：腾讯 `fqkline` **不复权** + `end` 锚点分页（ADR-003）。
    东财在本环境不可达（ADR-003 实测），且 qfq 不得落库（铁律①），
    故这里**没有**降级到 qfq 的分支 —— 宁可失败留痕，也不写口径错误的数据。
    """
    from stocklab.data.fetch import fetch_daily_bars, policy_from_settings
    from stocklab.data.http import HttpClient
    from stocklab.data.ingest import ingest_daily_bars
    from stocklab.data.raw_cache import RawCache

    paths.ensure_dirs()
    settings = load_settings()
    now = datetime.now(TZ).isoformat(timespec="seconds")
    start = (date.today() - timedelta(days=args.days)).isoformat()
    end = _today()
    universe = tuple(i for i in DEFAULT_UNIVERSE
                     if not args.code or i.code in args.code)

    cache = RawCache(paths.RAW_CACHE_DIR) if settings.cache_enabled else None
    client = HttpClient(policy_from_settings(settings), cache=cache)

    def fetch(code: str):
        inst = next(i for i in universe if i.code == code)
        return fetch_daily_bars(client, code=inst.tencent_code, start=start, end=end)

    conn = connect(paths.DB_PATH)
    try:
        repo.upsert_instruments(conn, universe, now=now)
        report = ingest_daily_bars(conn, client, cache, universe,
                                   start=start, end=end, now=now, fetch=fetch)
    finally:
        conn.close()

    print(json.dumps({"ok": report.ok_count, "failed": report.failed_codes,
                      "issues": report.total_issues, "bars": report.bars_written,
                      "start": start, "end": end}, ensure_ascii=False))
    return 0 if report.ok_count else 1


def _cmd_fixture_record(args: argparse.Namespace) -> int:
    """把 raw_cache 里的一份响应登记为可回放 fixture（离线脚本，不联网）。"""
    from stocklab.data.raw_cache import record_fixture

    src = paths.RAW_CACHE_DIR / args.source
    matches = sorted(src.glob(f"{args.key}.*.bin"))
    if not matches:
        print(f"❌ 未找到缓存: {src}/{args.key}.*.bin", file=sys.stderr)
        return 1
    body = matches[0].read_bytes()
    meta = json.loads(matches[0].with_suffix(".json").read_text(encoding="utf-8"))
    p = record_fixture(paths.FIXTURE_DIR, args.name, body,
                       {"source": args.source, **meta,
                        "note": args.note or "从 raw_cache 登记（真实响应）"})
    print(f"✅ {p}")
    return 0


# ---------- parser ----------

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="stocklab", description="stock-lab CLI")
    sub = parser.add_subparsers(dest="command")

    db = sub.add_parser("db", help="数据库相关操作")
    db_sub = db.add_subparsers(dest="db_command")
    db_init = db_sub.add_parser("init", help="建库 / 前滚迁移（迁移前自动备份）")
    db_init.add_argument("--db", help="数据库路径（默认 data/stocklab.db）")
    db_init.add_argument("--backup-dir", help="备份目录（默认 <db 所在目录>/backups）")
    db_init.set_defaults(func=_cmd_db_init)

    ingest = sub.add_parser("ingest", help="采集数据（联网）")
    ing_sub = ingest.add_subparsers(dest="ingest_target")
    ing_bars = ing_sub.add_parser("bars", help="采集日K（腾讯不复权 + 分页）")
    ing_bars.add_argument("--days", type=int, default=1200,
                          help="回补的日历天数（默认 1200）")
    ing_bars.add_argument("--code", action="append", default=None,
                          help="只采指定代码，可重复；默认全集")
    ing_bars.set_defaults(func=cmd_ingest_bars)

    doctor = sub.add_parser("doctor", help="数据健康度报告（离线）")
    doctor.set_defaults(func=lambda _a: cmd_doctor())

    fixture = sub.add_parser("fixture", help="fixture 管理（离线）")
    fx_sub = fixture.add_subparsers(dest="fixture_action")
    fx_record = fx_sub.add_parser("record", help="把 raw_cache 登记为 fixture")
    fx_record.add_argument("--name", required=True)
    fx_record.add_argument("--source", default="tencent")
    fx_record.add_argument("--key", required=True, help="raw_cache 的 params_key")
    fx_record.add_argument("--note", default=None)
    fx_record.set_defaults(func=_cmd_fixture_record)

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
    if not getattr(args, "db", None):
        paths.ensure_dirs()
    return func(args)


if __name__ == "__main__":
    raise SystemExit(main())
