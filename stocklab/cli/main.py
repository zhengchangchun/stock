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
from stocklab.data.fetch import EARLIEST as EARLIEST_ACTION_START
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


# ---------- ingest actions（除权事件 + 因子链） ----------

def cmd_ingest_actions(args: argparse.Namespace) -> int:
    """采集除权除息事件并重建复权因子链（联网）。

    两步都做：事件落 `corp_actions` → 由「不复权 K 线 + 事件」重算整条
    PIT 因子链落 `adj_factors`。顺序不能反 —— 因子链的唯一输入是这两张表。

    事件默认取**全历史**（`--start` 可覆盖）：`corp_actions` 里缺一条事件与
    「该事件不存在」在库中无法区分，因子链会把缺席的事件当成没有，
    于是那之后的整段复权价都错且无任何报错（ADR-004 §后果）。
    """
    from stocklab.data import adjust
    from stocklab.data.fetch import fetch_corp_actions, policy_from_settings
    from stocklab.data.http import HttpClient
    from stocklab.data.raw_cache import RawCache

    paths.ensure_dirs()
    settings = load_settings()
    now = datetime.now(TZ).isoformat(timespec="seconds")
    end = _today()
    universe = tuple(i for i in DEFAULT_UNIVERSE
                     if not args.code or i.code in args.code)

    cache = RawCache(paths.RAW_CACHE_DIR) if settings.cache_enabled else None
    client = HttpClient(policy_from_settings(settings), cache=cache)
    out: dict = {"end": end, "start": args.start, "codes": {}}
    failed: list[str] = []

    conn = connect(paths.DB_PATH)
    try:
        repo.upsert_instruments(conn, universe, now=now)
        for inst in universe:
            code = inst.code
            try:
                actions = fetch_corp_actions(client, code=inst.tencent_code,
                                             start=args.start, end=end)
            except Exception as exc:                   # noqa: BLE001 — 必须留痕
                msg = f"{type(exc).__name__}: {exc}"
                repo.log_event(conn, "ingest", "error",
                               f"{code} 除权事件采集失败: {msg}",
                               context={"code": code, "job": "ingest_actions"},
                               now=now)
                out["codes"][code] = {"error": msg}
                failed.append(code)
                continue
            written, restated = repo.insert_corp_actions(conn, actions, now=now)
            if restated:
                repo.log_event(conn, "ingest", "warn",
                               f"{code} 有 {restated} 条除权事件被源站修订",
                               context={"code": code, "restated": restated}, now=now)
            bars, chain = adjust.load_chain(conn, code)
            n_factors = repo.insert_adj_factors(conn, code, chain, source="tencent",
                                                now=now) if bars else 0
            summary = adjust.chain_summary(chain)
            out["codes"][code] = {"events": written, "restated": restated,
                                  "factor_rows": n_factors, **summary}
            if chain.unusable:
                repo.log_event(conn, "ingest", "warn",
                               f"{code} 有 {len(chain.unusable)} 条事件无法定价，"
                               f"复权链在 {chain.usable_from} 之前不可用",
                               context={"code": code,
                                        "unusable": [u.cqr for u in chain.unusable]},
                               now=now)
        repo.finish_job(conn, repo.record_job(conn, "ingest_actions",
                                              status="running", started_at=now),
                        status="ok" if not failed else "failed", finished_at=now,
                        detail=f"{len(universe) - len(failed)}/{len(universe)} ok")
    finally:
        conn.close()

    print(json.dumps(out, ensure_ascii=False))
    return 0 if not failed else 1


# ---------- ingest index（基准行情） ----------

def cmd_ingest_index(args: argparse.Namespace) -> int:
    """采集**指数**日线（联网；index_300 基准锚）。

    落库口径固定 `adj_mode='none'`：指数没有分红送转，无复权概念 ——
    这正好与「抓取层只落不复权」的铁律①一致（不用为指数开例外）。
    代码用源站符号（`sh000300`），避免与 6 位基金/个股代码撞键。
    """
    from stocklab.calendar.trading_calendar import Calendar
    from stocklab.data.fetch import fetch_index_daily, policy_from_settings
    from stocklab.data.http import HttpClient
    from stocklab.data.raw_cache import RawCache

    paths.ensure_dirs()
    settings = load_settings()
    now = datetime.now(TZ).isoformat(timespec="seconds")
    start = args.start or (date.today() - timedelta(days=args.days)).isoformat()
    end = args.end or _today()
    cache = RawCache(paths.RAW_CACHE_DIR) if settings.cache_enabled else None
    client = HttpClient(policy_from_settings(settings), cache=cache)

    conn = connect(paths.DB_PATH)
    try:
        bars = fetch_index_daily(client, symbol=args.symbol, start=start, end=end)
        if not bars:
            repo.log_event(conn, "ingest", "error",
                           f"{args.symbol} 指数行情为空（{start}~{end}）",
                           context={"symbol": args.symbol}, now=now)
            print(json.dumps({"symbol": args.symbol, "bars": 0, "error": "empty"},
                             ensure_ascii=False))
            return 1
        written = repo.insert_bars(conn, bars, now=now)
        # 交易日历的唯一来源是指数日线的日期集合（ADR-001 B4）：抓到更长历史时
        # 顺带把日历前滚到同一窗口，否则回测的「交易日」仍被旧日历卡在 900 天里，
        # 更早的 K 线会静默不参与（看着像「策略没交易」，实则日历没覆盖）。
        n_cal = Calendar.from_dates(b.date for b in bars).save(
            conn, source=f"tencent:{args.symbol}", now=now)
        out = {"symbol": args.symbol, "bars": written, "calendar_rows": n_cal,
               "start": start, "end": end,
               "first_date": bars[0].date, "last_date": bars[-1].date,
               "adj_mode": bars[0].adj_mode}
    finally:
        conn.close()
    print(json.dumps(out, ensure_ascii=False))
    return 0


# ---------- adj rebuild（离线重算因子链 + 缺口） ----------

def cmd_adj_rebuild(args: argparse.Namespace) -> int:
    """由 `bars_daily` + `corp_actions` **离线**重算复权因子链与不可用区间。

    幂等且无网络：因子是纯函数（ADR-004），源站修订分红后必须能重算覆盖。
    同时重写 `adj_factor_blackout`（两者同源，缺一都会让读取层拒绝服务）。
    只处理 6 位股票代码 —— 指数（源站符号）没有复权概念，不写因子行。
    """
    from stocklab.data import adjust

    now = datetime.now(TZ).isoformat(timespec="seconds")
    conn = connect(paths.DB_PATH)
    out: dict = {"codes": {}}
    try:
        if args.code:
            codes = list(args.code)
        else:
            codes = [r["code"] for r in conn.execute(
                "SELECT DISTINCT code FROM bars_daily"
                " WHERE code GLOB '[0-9][0-9][0-9][0-9][0-9][0-9]' ORDER BY code")]
        for code in codes:
            bars, chain = adjust.load_chain(conn, code)
            if not bars:
                out["codes"][code] = {"error": "bars_daily 无该标的数据"}
                continue
            n = repo.insert_adj_factors(conn, code, chain, source=args.source, now=now)
            out["codes"][code] = {"factor_rows": n,
                                  **adjust.chain_summary(chain)}
    finally:
        conn.close()
    print(json.dumps(out, ensure_ascii=False))
    return 0


# ---------- backtest run（离线；复权价 + 成本 + 基准对照） ----------

def _load_universe(conn) -> tuple:
    from stocklab.config.universe import Instrument

    return tuple(Instrument(r["code"], r["name"], r["market"], r["board"])
                 for r in conn.execute(
                     "SELECT code, name, market, board FROM instruments"
                     " WHERE active=1 AND type='stock' ORDER BY code"))


def cmd_backtest_run(args: argparse.Namespace) -> int:
    """跑一次 buy_and_hold 回测并给出基准对照（**离线**，只读库）。

    三条硬约束都在这里被真正执行：
      ① 价格来自 `adjust.load_bars_adjusted`（复权读取层）→ 引擎再强制
         `adj_mode='qfq'`，不复权价一行都进不来；
      ② 复权链不可用（跨缺口）→ 读取层抛错，本命令把该标的记进 `skipped` 并**不**回退；
      ③ 成本走 `CostModel`（含最低 5 元佣金），T+1 与涨跌停由 `Portfolio` 执行，
         板别取自 `instruments.board`。
    """
    from stocklab.backtest.benchmark import compare_to_benchmark, resolve_benchmark
    from stocklab.backtest.engine import run_backtest
    from stocklab.backtest.strategies import BuyAndHold
    from stocklab.calendar.trading_calendar import Calendar
    from stocklab.config.costs import CostModel
    from stocklab.data import adjust

    as_of = args.as_of or args.end or _today()
    conn = connect(paths.DB_PATH)
    try:
        universe = _load_universe(conn)
        wanted = [i for i in universe if not args.code or i.code in args.code]
        bars: dict = {}
        skipped: dict = {}
        for inst in wanted:
            try:
                loaded = adjust.load_bars_adjusted(conn, inst.code, as_of,
                                                   start=args.start or None)
            except adjust.AdjustError as exc:
                skipped[inst.code] = f"{type(exc).__name__}: {exc}"
                continue
            if loaded:
                bars[inst.code] = loaded
        if not bars:
            print(json.dumps({"error": "没有可用标的（全部被复权链拒绝）",
                              "skipped": skipped}, ensure_ascii=False))
            return 1

        calendar = Calendar.load(conn)
        strategy_code = args.code[0] if args.code else sorted(bars)[0]
        start = args.start or bars[strategy_code][0].date
        end = args.end or bars[strategy_code][-1].date
        sessions = calendar.sessions(start, end)
        # **实际成交窗口**可能比请求窗口窄（日历没覆盖到的日期不是交易日）：
        # 报出真实区间，避免「请求 2001 年、实际只跑了 2013 年」这类看不出的错位。
        eff_start = sessions[0] if sessions else None
        eff_end = sessions[-1] if sessions else None
        costs = CostModel()
        res = run_backtest(bars, BuyAndHold(strategy_code), start=start, end=end,
                           initial_cash=args.cash, costs=costs, calendar=calendar,
                           universe=tuple(i for i in universe if i.code in bars))
        bench = resolve_benchmark(conn, args.benchmark, start=start, end=end)
        cmp = compare_to_benchmark(res.nav_points, args.cash, bench, costs)
        out = {
            "strategy": "buy_and_hold",
            "code": strategy_code,
            "start": start, "end": end,
            "session_start": eff_start, "session_end": eff_end,
            "initial_cash": args.cash,
            "final_nav": res.nav_points[-1].nav,
            "n_trades": len(res.trades),
            "costs_total": res.costs_total,
            "metrics": res.metrics,
            "trades_sample": [vars(t) for t in res.trades[:3]],
            "benchmark": cmp.as_dict(),
            "skipped": skipped,
            "disclosure": {
                "costs_included": True,
                "adjusted_prices": True,
                "insample": True,
                "note": "单标的 buy_and_hold 联通验证，非策略绩效结论（无 walk-forward）",
            },
        }
    finally:
        conn.close()
    print(json.dumps(out, ensure_ascii=False, indent=2))
    return 0


# ---------- features ----------

def _bar_from_row(row) -> "Bar":
    from stocklab.data.models import Bar

    return Bar(code=row["code"], date=row["date"], open=row["open"],
               high=row["high"], low=row["low"], close=row["close"],
               volume=row["volume"], amount=row["amount"],
               turnover=row["turnover"], source=row["source"],
               adj_mode=row["adj_mode"])


def cmd_features_build(date: str, codes: list[str] | None) -> int:
    """为指定日期构建全部标的的特征快照（**离线**，只读 `bars_daily` + `adj_factors`）。

    **P3 起特征在复权价上计算**（ADR-004）：不复权价在除权日会产生假跌幅
    （000333 的 `10派20元转15股` 那天不复权跌 ~63%），`ret_1d/ma/atr` 全部失真，
    回测收益随之失真。复权在**读取层**做，库里永远只有不复权 OHLC。

    行为：
    - 幂等：同键同 hash 的快照已存在 → 计 `identical`，不重复写（可安全重试）。
    - 冲突：同键但 hash 不同（行情被修订 / 特征代码被改而没升版本）→ 计
      `conflicts`、留 warn 事件，**不覆盖**，并以退出码 1 上报。
    - 缺失：历史不足、当日无 K 线、或**复权链不可用** → 计 `skipped` + warn 事件
      （不写假快照；绝不在复权不可用时退回不复权价 —— 那正是本任务要消灭的失真）。

    `regime_label` 暂为 NULL：市场状态需要指数行情，当前 `bars_daily` 里只有个股
    （见 docs/tasks/2026-09-15-p3-特征层-task17-20.md 的遗留问题）。
    """
    from stocklab.data import adjust
    from stocklab.features import snapshot

    db = paths.DB_PATH
    if not db.exists():
        print(json.dumps({"error": "db not found; run `stocklab db init`"},
                         ensure_ascii=False))
        return 2
    paths.ensure_dirs()
    init_db(db)
    now = datetime.now(TZ).isoformat(timespec="seconds")
    written = identical = 0
    skipped: list[str] = []
    conflicts: list[str] = []

    with connect(db) as conn:
        rows = conn.execute(
            "SELECT code FROM instruments WHERE active=1 ORDER BY code").fetchall()
        for row in rows:
            code = row["code"]
            if codes and code not in codes:
                continue
            raw_bars, chain = adjust.load_chain(conn, code)
            try:
                # 窗口从 usable_from 起：早于它的一段跨越了无法定价的除权事件，
                # 那几天的假跌幅无法还原 → 宁可少几天历史，也不交出失真的序列。
                bars = adjust.adjust_bars(raw_bars, chain, date, code=code,
                                          start=chain.usable_from)
            except adjust.AdjustError as exc:
                # 复权链缺因子 / 窗口仍跨越不可定价事件 → 记为缺失，**不退回不复权价**
                skipped.append(code)
                repo.log_event(conn, "features", "warn",
                               f"{code} {date} 复权链不可用，已跳过（不写不复权失真快照）",
                               context={"date": date, "reason": str(exc),
                                        "usable_from": chain.usable_from,
                                        "n_unusable": len(chain.unusable)},
                               now=now)
                continue
            snap = snapshot.build_snapshot(
                code, date, bars,
                data_version=f"bars:{date};adj:{len(chain.events)}events")
            if snap is None:
                skipped.append(code)
                repo.log_event(conn, "features", "warn",
                               f"{code} 无法构建 {date} 快照（历史不足或当日无K线）",
                               context={"date": date,
                                        "last_bar": bars[-1].date if bars else None},
                               now=now)
                continue
            existing = snapshot.find_snapshot(conn, code, date)
            if existing is not None:
                if existing["payload_hash"] == snap.payload_hash:
                    identical += 1
                else:
                    conflicts.append(code)
                    repo.log_event(
                        conn, "features", "warn",
                        f"{code} {date} 已有快照与重算结果不一致"
                        f"（行情修订或特征改动未升版本）",
                        context={"date": date,
                                 "existing": existing["payload_hash"],
                                 "recomputed": snap.payload_hash},
                        now=now)
                continue
            snapshot.save_snapshot(conn, snap, now=now)
            written += 1

    print(json.dumps({"date": date, "written": written, "identical": identical,
                      "conflicts": conflicts, "skipped": skipped,
                      "feature_version": registry_version()},
                     ensure_ascii=False))
    return 1 if conflicts else 0


def registry_version() -> str:
    from stocklab.features import registry

    return registry.FEATURE_VERSION


def _cmd_features_build(args: argparse.Namespace) -> int:
    """argparse 名字空间 → 领域参数（保持 `cmd_*` 可直接单测）。"""
    return cmd_features_build(args.date, args.code)


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

    ing_act = ing_sub.add_parser(
        "actions", help="采集除权除息事件并重建复权因子链（腾讯不复权 + 全历史）")
    ing_act.add_argument("--code", action="append", default=None,
                         help="只采指定代码，可重复；默认全集")
    ing_act.add_argument("--start", default=EARLIEST_ACTION_START,
                         help=f"事件回补起点（默认 {EARLIEST_ACTION_START} = 全历史）")
    ing_act.set_defaults(func=cmd_ingest_actions)

    ing_idx = ing_sub.add_parser(
        "index", help="采集指数日线（基准 index_300，adj_mode='none'）")
    ing_idx.add_argument("--symbol", default="sh000300",
                         help="指数符号（默认 sh000300 = 沪深300）")
    ing_idx.add_argument("--days", type=int, default=4000,
                         help="回补的日历天数（默认 4000 ≈ 11 年）")
    ing_idx.add_argument("--start", default=None, help="起始日期 YYYY-MM-DD")
    ing_idx.add_argument("--end", default=None, help="结束日期 YYYY-MM-DD")
    ing_idx.set_defaults(func=cmd_ingest_index)

    adj = sub.add_parser("adj", help="复权因子链（离线，只读 bars_daily + corp_actions）")
    adj_sub = adj.add_subparsers(dest="adj_action")
    adj_rebuild = adj_sub.add_parser(
        "rebuild", help="重算因子链与不可用区间（幂等、离线）")
    adj_rebuild.add_argument("--code", action="append", default=None,
                             help="只重建指定代码，可重复；默认全部 6 位股票代码")
    adj_rebuild.add_argument("--source", default="tencent")
    adj_rebuild.set_defaults(func=cmd_adj_rebuild)

    bt = sub.add_parser("backtest", help="回测（离线；复权价 + 成本 + 基准对照）")
    bt_sub = bt.add_subparsers(dest="bt_action")
    bt_run = bt_sub.add_parser("run", help="buy_and_hold 端到端回测 + 基准对照")
    bt_run.add_argument("--code", action="append", default=None,
                        help="回测标的（第一个作为策略标的），可重复")
    bt_run.add_argument("--start", default=None, help="起始日期（默认由复权链可用下界决定）")
    bt_run.add_argument("--end", default=None, help="结束日期")
    bt_run.add_argument("--as-of", default=None, help="复权锚点日期（默认=end/今天）")
    bt_run.add_argument("--cash", type=float, default=100_000.0, help="初始资金")
    bt_run.add_argument("--benchmark", default="index_300",
                        help="基准键（默认 index_300；无数据则报 UNDETERMINED）")
    bt_run.set_defaults(func=cmd_backtest_run)

    doctor = sub.add_parser("doctor", help="数据健康度报告（离线）")
    doctor.set_defaults(func=lambda _a: cmd_doctor())

    feat = sub.add_parser("features", help="特征层（离线，只读 bars_daily）")
    feat_sub = feat.add_subparsers(dest="feat_action")
    feat_build = feat_sub.add_parser("build", help="为指定日期构建特征快照")
    feat_build.add_argument("--date", required=True, help="asof 日期 YYYY-MM-DD")
    feat_build.add_argument("--code", action="append", default=None,
                            help="只构建指定代码，可重复；默认全部 active 标的")
    feat_build.set_defaults(func=_cmd_features_build)

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
