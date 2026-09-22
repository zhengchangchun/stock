"""stocklab 命令行入口。

用法:
    .venv/bin/python -m stocklab.cli.main db init [--db PATH]
    .venv/bin/python -m stocklab.cli.main doctor
    .venv/bin/python -m stocklab.cli.main ops patrol [--fix]
    .venv/bin/python -m stocklab.cli.main ingest bars [--days N] [--code C]...
    .venv/bin/python -m stocklab.cli.main session tick
    .venv/bin/python -m stocklab.cli.main review daily
    .venv/bin/python -m stocklab.cli.main fixture record --name NAME

退出码：0 成功 / 2 用法错误或库不存在 / 1 运行失败。
项目内不实现任何定时/守护进程（ADR-001 D-05），本入口只做一次性动作。

`ingest bars` 是**唯一联网**的命令；其抓取逻辑在 `stocklab/data/fetch.py`
（分页 + 口径守卫，已有离线回放测试），本模块只负责把它接到配置与数据库上。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from stocklab.config import paths
from stocklab.config.settings import load_settings
from stocklab.config.universe import DEFAULT_UNIVERSE
from stocklab.data.fetch import EARLIEST as EARLIEST_ACTION_START
from stocklab.store import repo
from stocklab.store.db import connect
from stocklab.store.migrate import ensure_schema, init_db
# `paper/config.py` 是纯常量叶子模块（无 import），在模块级引用它不会成环 ——
# 这样 `SPEC_ARMS` 不需要在自己内部再抄一遍臂名字面量。
from stocklab.paper.config import ARM_AGENT, ARM_AGENT_RANDOM
from stocklab.cli.plugin import (cmd_plugin_approve, cmd_plugin_list,
                                 cmd_plugin_reject, cmd_plugin_sandbox,
                                 cmd_plugin_submit)
from stocklab.cli.candidate import cmd_candidate_run

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
    """输出数据健康度 JSON（供人读，也供 nanobot 侧调度判读）。

    取数逻辑在 `stocklab.session.review.doctor_report`（复盘报告要复用同一份，
    抽出函数以免两处各写一遍、日后数字对不上）。
    """
    from stocklab.session.review import doctor_report

    db = Path(db_path) if db_path else paths.DB_PATH
    if not db.exists():
        print(json.dumps({"error": "db not found; run `stocklab db init`"},
                         ensure_ascii=False))
        return 2
    conn = connect(db)
    try:
        out = doctor_report(conn)
    finally:
        conn.close()
    print(json.dumps(out, ensure_ascii=False, indent=2))
    return 0


# ---------- ingest ----------

def _bars_universe(conn):
    """K 线采集的标的集合：**库里的 `instruments`（股票 + ETF）**，库为空时退回 `DEFAULT_UNIVERSE`。

    为什么不再直接用 `DEFAULT_UNIVERSE`：库里现有 21 只种子（`candidate/seeds.py`），
    其中 **15 只不在 `DEFAULT_UNIVERSE`**（它只有 6 只，是首次建库的种子）——
    旧写法会静默只采 6 只，其余 15 只的 K 线永远停在旧日期（ERROR_DIARY #44 的同类坑，
    但那次是 `--code`，这次是**默认全量**）。

    与 `_tick_universe` 的差别只有一个：本函数连 ETF 一起采（基准/红利 ETF 也要日线），
    而 tick 只看 `type='stock'`。库是**真源**（`instruments` 可增删）。
    """
    from stocklab.config.universe import Instrument

    try:
        rows = conn.execute(
            "SELECT code, name, market, board, type FROM instruments"
            " WHERE active=1 ORDER BY code").fetchall()
    except sqlite3.Error:
        return tuple(DEFAULT_UNIVERSE)
    loaded = tuple(
        Instrument(r["code"], r["name"], r["market"], r["board"], r["type"])
        for r in rows)
    return loaded or tuple(DEFAULT_UNIVERSE)


def cmd_ingest_bars(args: argparse.Namespace) -> int:
    """真实抓取日K（联网）。这是 P2 的端到端验收命令。

    数据源：腾讯 `fqkline` **不复权** + `end` 锚点分页（ADR-003）。
    东财在本环境不可达（ADR-003 实测），且 qfq 不得落库（铁律①），
    故这里**没有**降级到 qfq 的分支 —— 宁可失败留痕，也不写口径错误的数据。

    标的集合取自**库里的 `instruments`**（股票 + ETF，见 `_bars_universe`）；`--code` 里出现
    库里没有的代码时**不静默跳过**：print 到 stderr 并退出 1（ERROR_DIARY #44）。
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

    ensure_schema(paths.DB_PATH)   # 写库入口前滚（P33）：链路上任一步都不许在旧 schema 上写
    conn = connect(paths.DB_PATH)
    try:
        universe = _bars_universe(conn)
        if args.code:
            known = {i.code for i in universe}
            unknown = [c for c in dict.fromkeys(args.code) if c not in known]
            if unknown:
                print(f"❌ --code 里有不在库 instruments 的代码，拒绝静默跳过: {unknown}",
                      file=sys.stderr)
                return 1
            wanted = set(args.code)
            universe = tuple(i for i in universe if i.code in wanted)

        cache = RawCache(paths.RAW_CACHE_DIR) if settings.cache_enabled else None
        client = HttpClient(policy_from_settings(settings), cache=cache)

        def fetch(code: str):
            inst = next(i for i in universe if i.code == code)
            return fetch_daily_bars(client, code=inst.tencent_code, start=start, end=end)

        repo.upsert_instruments(conn, universe, now=now)
        report = ingest_daily_bars(conn, client, cache, universe,
                                   start=start, end=end, now=now, fetch=fetch)
    finally:
        conn.close()

    print(json.dumps({"ok": report.ok_count, "failed": report.failed_codes,
                      "issues": report.total_issues, "bars": report.bars_written,
                      "start": start, "end": end}, ensure_ascii=False))
    return 0 if report.ok_count else 1


def _stock_universe(conn):
    """**需要复权链**的采集（`ingest actions`）的标的集合：库里 active **股票**。

    为什么不是 ETF：ADR-008 明确「ETF 不进复权链」（`assert_adjustable` 直接抛错），
    所以复权事件采集必须排掉 ETF；库为空时退回 `DEFAULT_UNIVERSE` 里的股票。
    与 `_bars_universe` 的差别：本函数只要股票，且库为空时不带 ETF。
    """
    from stocklab.config.universe import Instrument

    try:
        rows = conn.execute(
            "SELECT code, name, market, board, type FROM instruments"
            " WHERE active=1 AND type='stock' ORDER BY code").fetchall()
    except sqlite3.Error:
        rows = []
    loaded = tuple(
        Instrument(r["code"], r["name"], r["market"], r["board"], r["type"])
        for r in rows)
    return loaded or tuple(i for i in DEFAULT_UNIVERSE if i.is_stock)


def _reject_unknown_codes(universe, codes, *, cmd: str):
    """`--code` 里出现标的集合中没有的代码 → 打印并回 False（**不静默跳过**，#44）。"""
    if not codes:
        return True
    known = {i.code for i in universe}
    unknown = [c for c in dict.fromkeys(codes) if c not in known]
    if unknown:
        print(f"❌ {cmd} --code 里有不在标的集合里的代码，拒绝静默跳过: {unknown}",
              file=sys.stderr)
        return False
    return True


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

    ensure_schema(paths.DB_PATH)   # 写库入口前滚（P33）
    conn = connect(paths.DB_PATH)
    try:
        universe = _stock_universe(conn)
        if not _reject_unknown_codes(universe, args.code, cmd="ingest actions"):
            return 1
        if args.code:
            wanted = set(args.code)
            universe = tuple(i for i in universe if i.code in wanted)

        cache = RawCache(paths.RAW_CACHE_DIR) if settings.cache_enabled else None
        client = HttpClient(policy_from_settings(settings), cache=cache)
        out: dict = {"end": end, "start": args.start, "codes": {}}
        failed: list[str] = []

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

    ensure_schema(paths.DB_PATH)   # 写库入口前滚（P33）
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


# ---------- ingest valuation / moneyflow（P28：估值 + 资金流） ----------

def _cmd_ingest_series(args: argparse.Namespace, *, kind: str) -> int:
    """采集估值（`valuation`）或资金流（`moneyflow`）并落 PIT 表（联网）。

    `kind` ∈ {"valuation", "moneyflow"}。二者共享同一套编排，差异只在
    fetch / insert / 源站代码口径：
      - valuation：源 = 东财 datacenter，`code` = 6 位；落 `valuation_daily`；
      - moneyflow：源 = 新浪，`code` = 源站形式 `sz000333`；落 `money_flow_daily`。

    幂等：同 (code,date) 首写保留，重跑 → `rows=0`（验收 d）。源站重算历史 →
    `restated` 计数 + warn 留痕（不覆盖，非 PIT 对策，见 P28 计划 §4）。
    """
    from stocklab.data.fetch import (fetch_money_flow_daily,
                                     fetch_valuation_daily, policy_from_settings)
    from stocklab.data.http import HttpClient
    from stocklab.data.raw_cache import RawCache

    paths.ensure_dirs()
    settings = load_settings()
    now = datetime.now(TZ).isoformat(timespec="seconds")
    start = args.start or (date.today() - timedelta(days=args.days)).isoformat()
    end = args.end or _today()

    ensure_schema(paths.DB_PATH)    # 前滚 schema（P33：写库入口统一走 ensure_schema）

    cache = RawCache(paths.RAW_CACHE_DIR) if settings.cache_enabled else None
    client = HttpClient(policy_from_settings(settings), cache=cache)

    conn = connect(paths.DB_PATH)
    out: dict = {"kind": kind, "start": start, "end": end, "codes": {}}
    failed: list[str] = []
    try:
        universe = _bars_universe(conn)
        if not _reject_unknown_codes(universe, args.code, cmd=f"ingest {kind}"):
            return 1
        if args.code:
            wanted = set(args.code)
            universe = tuple(i for i in universe if i.code in wanted)

        repo.upsert_instruments(conn, universe, now=now)
        run_id = repo.record_job(conn, f"ingest_{kind}", status="running",
                                 started_at=now)
        for inst in universe:
            code = inst.code
            try:
                if kind == "valuation":
                    rows = fetch_valuation_daily(client, code=inst.code,
                                                 start=start, end=end)
                    written, restated = repo.insert_valuation(conn, rows, now=now)
                else:
                    rows = fetch_money_flow_daily(client, code=inst.tencent_code,
                                                  start=start, end=end)
                    written, restated = repo.insert_money_flow(conn, rows, now=now)
            except Exception as exc:                    # noqa: BLE001 — 必须留痕
                msg = f"{type(exc).__name__}: {exc}"
                repo.log_event(conn, "ingest", "error",
                               f"{code} {kind} 采集失败: {msg}",
                               context={"code": code, "job": f"ingest_{kind}"},
                               now=now)
                out["codes"][code] = {"error": msg}
                failed.append(code)
                continue
            if not rows and inst.is_etf:
                # ETF 没有估值/资金流是**显式覆盖记录**，不许静默缺失（验收 f）
                repo.log_event(conn, "ingest", "warn",
                               f"{code}（ETF）无 {kind} 数据（源返回空）",
                               context={"code": code, "job": f"ingest_{kind}"},
                               now=now)
            if restated:
                repo.log_event(conn, "ingest", "warn",
                               f"{code} 有 {restated} 行 {kind} 被源站重算"
                               "（首写保留，未覆盖）",
                               context={"code": code, "restated": restated},
                               now=now)
            out["codes"][code] = {
                "rows": written, "restated": restated,
                "first_date": rows[0][0].date if rows else None,
                "last_date": rows[-1][0].date if rows else None,
                "fetched": len(rows),
            }
        repo.finish_job(conn, run_id, status="ok" if not failed else "failed",
                        finished_at=now,
                        detail=f"{len(universe) - len(failed)}/{len(universe)} ok")
    finally:
        conn.close()
    print(json.dumps({"ok": len(universe) - len(failed), "failed": failed, **out},
                     ensure_ascii=False))
    return 0 if not failed else 1


def cmd_ingest_valuation(args: argparse.Namespace) -> int:
    return _cmd_ingest_series(args, kind="valuation")


def cmd_ingest_moneyflow(args: argparse.Namespace) -> int:
    return _cmd_ingest_series(args, kind="moneyflow")


# ---------- ingest financials（财报，东财 DMSK + F10，PIT 首写保留） ----------

def cmd_ingest_financials(args) -> int:
    """采集财报并落 `financial_reports`（联网；东财 DMSK 三表 + F10 公告日）。

    幂等：`(code, report_date, notice_date)` 已存在即跳过。
    首写保留：同键重采值变了 → 保留旧值 + `conflicts` 计数，不覆盖。
    会计恒等式与量级异常**只记 issue，不拒写**（留痕不丢数据）。
    """
    from datetime import date, datetime, timezone

    from stocklab.candidate.seeds import SEED_UNIVERSE
    from stocklab.data.fetch import fetch_financial_reports
    from stocklab.data.http import HttpClient
    from stocklab.data.ingest import ingest_financial_reports
    from stocklab.store.migrate import ensure_schema

    db_path = Path(args.db) if args.db else paths.DB_PATH
    ensure_schema(db_path)
    now = datetime.now(timezone.utc).isoformat()
    fetched_date = args.fetched_date or date.today().isoformat()
    wanted = set(args.code) if args.code else {i.code for i in SEED_UNIVERSE}
    client = HttpClient()
    conn = connect(db_path)
    written = conflicts = failed = 0
    try:
        for inst in SEED_UNIVERSE:
            if inst.code not in wanted:
                continue
            try:
                reports, refs = fetch_financial_reports(
                    client, code=inst.code, org_type=inst.org_type,
                    fetched_date=fetched_date)
            except Exception as exc:                       # noqa: BLE001
                failed += 1
                print(f"❌ {inst.code} {type(exc).__name__}: {exc}", file=sys.stderr)
                repo.log_event(conn, "ingest", "warn",
                               f"ingest financials {inst.code} 失败: {exc}",
                               now=now)
                continue
            r = ingest_financial_reports(conn, reports, refs, now=now)
            written += r.rows_written
            conflicts += r.conflicts
            period = (f"{reports[0].report_date}~{reports[-1].report_date}"
                      if reports else "（无财报，合法空）")
            print(f"{inst.code} 抓到 {len(reports)} 期 {period}"
                  f" 新写 {r.rows_written} 冲突 {r.conflicts}")
            for issue in r.issues:
                print(f"  ⚠️ {issue}", file=sys.stderr)
        print(f"合计：新写 {written} 行 / 首写保留冲突 {conflicts} / 失败 {failed} 只")
        return 1 if failed else 0
    finally:
        conn.close()


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


# ---------- backtest walkforward（离线；样本量实证，无策略结论） ----------

def cmd_backtest_walkforward(args: argparse.Namespace) -> int:
    """用**真实数据**切一次 walk-forward，回答「样本外样本量够不够」。

    **本命令不产生任何策略绩效结论**：它只报折分与样本量（交易日口径），
    不做预测、不评估策略、不比较基准。目的是在写第一个策略之前，
    先知道现有数据能不能支撑 120 个样本外交易日（R6 硬门槛）。

    数据入口沿用回测的硬规则：价格必须来自 `adjust.load_bars_adjusted`
    （复权读取层），复权链不可用的标的进 `skipped` 且**不回退**到不复权。
    """
    from stocklab.backtest.walkforward import (InsufficientData, build_report,
                                               render_markdown)
    from stocklab.calendar.trading_calendar import Calendar
    from stocklab.data import adjust

    as_of = args.as_of or args.end or _today()
    conn = connect(paths.DB_PATH)
    try:
        universe = _load_universe(conn)
        wanted = [i for i in universe if not args.code or i.code in args.code]
        dates_by_code: dict[str, list[str]] = {}
        skipped: dict[str, str] = {}
        for inst in wanted:
            try:
                loaded = adjust.load_bars_adjusted(conn, inst.code, as_of,
                                                   start=args.start or None)
            except adjust.AdjustError as exc:
                skipped[inst.code] = f"{type(exc).__name__}: {exc}"
                continue
            if loaded:
                dates_by_code[inst.code] = [b.date for b in loaded]
        if not dates_by_code:
            print(json.dumps({"error": "没有可用标的（全部被复权链拒绝）",
                              "skipped": skipped}, ensure_ascii=False))
            return 1

        calendar = Calendar.load(conn)
        # 会话轴 = 各标的行情日期的**交集**（公共交易日）。
        # 用交集而非并集：并集会让某些折的样本外窗口里「只有一部分标的有数据」，
        # 各折的标的集合不一致 → 折与折之间不可比。交集保证每折口径相同。
        # 代价是短历史标的会拉窄全局窗口 —— 所以下面把「被裁掉多少」如实报出来。
        per_code = {c: sorted(d) for c, d in dates_by_code.items()}
        axis = sorted(set.intersection(*(set(v) for v in per_code.values())))
        dropped = {c: len(v) - len(set(v) & set(axis)) for c, v in per_code.items()}
        if args.start:
            axis = [d for d in axis if d >= args.start]
        if args.end:
            axis = [d for d in axis if d <= args.end]
        # 交叉核对：轴上的日期必须是真交易日（日历没覆盖到的日期不算样本）
        axis = [d for d in axis if calendar.is_open(d)]

        report = build_report(
            generated=_today(), axis=axis, dates_by_code=dates_by_code,
            train=args.train, test=args.test, step=args.step, embargo=args.embargo,
            skipped=skipped, threshold=args.threshold,
        )
        # 公共轴把各标的的私有区间裁掉了多少（如实报出，不静默）
        report["session_axis"]["mode"] = "intersection"
        report["session_axis"]["dropped_by_code"] = dropped

    except InsufficientData as exc:
        # 显式失败：0 折不是「没有结论」，是「没跑」。退出码 2 让调度方能把
        # 「跑完了但样本量未达标」与「根本没跑成」区分开。
        print(f"❌ {exc}", file=sys.stderr)
        return 2
    finally:
        conn.close()

    out_md = Path(args.out) if args.out else (paths.REPORT_DIR / f"{_today()}-walkforward.md")
    out_json = out_md.with_suffix(".json")
    out_md.parent.mkdir(parents=True, exist_ok=True)
    out_md.write_text(render_markdown(report), encoding="utf-8")
    out_json.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    ss, th = report["sample_size"], report["threshold"]
    print(f"折数: {len(report['folds'])}")
    print(f"会话轴: {report['session_axis']['start']} ~ {report['session_axis']['end']}"
          f"（{report['session_axis']['n_sessions']} 个交易日）")
    print(f"样本外交易日: {ss['effective_n']} | 标的-日行数: {ss['oos_rows']}"
          f" | 标的数: {ss['n_codes']}")
    print(f"门槛 {th['threshold']} 交易日: {'✅ 达标' if th['meets'] else '❌ 未达标'}"
          f"（差 {th['deficit_days']} 日 / 还需 {th['extra_sessions_needed']} 个交易日"
          f" ≈ {th['extra_years_needed']:.2f} 年）")
    print(f"跳过（复权链拒绝，不回退）: {skipped or '无'}")
    print(f"📄 {out_md}\n📄 {out_json}")
    return 0


# ---------- backtest strategy（离线；注册表策略的 walk-forward 样本外绩效） ----------

def _parse_param_overrides(items) -> dict:
    """`--param fast=20` → `{"fast": 20}`（类型按字面量猜，真正的校验在 ParamSpec）。

    「同一个参数给两次」直接报错：本项目的实验纪律是**一次只改一个变量**，
    重复给值多半意味着调用方脚本拼错了命令行，静默取最后一个会掩盖它。
    """
    out: dict = {}
    for item in items or []:
        if "=" not in item:
            raise ValueError(f"--param 需要 k=v 形式，收到 {item!r}")
        key, raw = item.split("=", 1)
        key = key.strip()
        if key in out:
            raise ValueError(f"--param {key} 重复给出（{out[key]!r} 与 {raw!r}）："
                             "单变量原则要求一次只改一个参数，重复值多半是命令拼错")
        try:
            out[key] = int(raw)
        except ValueError:
            try:
                out[key] = float(raw)
            except ValueError:
                out[key] = raw        # 交给 ParamSpec.coerce 报出可读的类型错
    return out


def _load_adjusted(conn, universe, wanted, *, as_of: str, start: str | None):
    """按**复权链可用下界**读取各标的的复权 K 线。

    对存在不可定价事件（`adj_factor_blackout`）的标的，必须显式传
    `start = usable_from` —— `adjust.load_bars_adjusted` 的默认行为是「不缩窗口」，
    一旦跨缺口就抛 `MissingFactor`（缺陷必须浮出来，不许静默放弃那段历史）。
    本函数在**这里**做「放弃哪段历史」的决定，并把结果如实返回给调用方披露。

    返回 `(bars_by_code, skipped, window)`；`window[code]` 是实际读取起点。
    """
    from stocklab.data import adjust

    bars: dict = {}
    skipped: dict = {}
    window: dict = {}
    for inst in wanted:
        flo = adjust.usable_from(conn, inst.code)
        begin = start or flo
        if start and flo and flo > start:
            begin = flo          # 请求更早 → 抬高到可用下界（并记录，见 window）
        try:
            loaded = adjust.load_bars_adjusted(conn, inst.code, as_of, start=begin)
        except adjust.AdjustError as exc:
            skipped[inst.code] = f"{type(exc).__name__}: {exc}"
            continue
        if loaded:
            bars[inst.code] = loaded
            window[inst.code] = {"first_bar": loaded[0].date,
                                 "requested_start": start,
                                 "usable_from": flo,
                                 "clipped": bool(start and flo and flo > start)}
    return bars, skipped, window


def cmd_backtest_strategy(args: argparse.Namespace) -> int:
    """跑注册表里某个策略的 **walk-forward 样本外**绩效，并落报告（离线）。

    这条链第一次把四件事串起来：
      ① 策略来自**注册表**（未注册直接失败，不允许评估）；
      ② 价格来自复权读取层（跨缺口的标的被显式跳过，**不回退**）；
      ③ 逐折调用 `engine.run_backtest`，只取样本外段并复利拼接；
      ④ 同区间输出 `index_300` 与 `buy_and_hold` 对照，并明确写「是否跑赢」。

    **不做参数搜索**：`--param` 是给单变量实验用的显式入口；
    本命令的默认值就是文档默认值，报告里会把实际生效参数原样列出。
    """
    from stocklab.backtest.benchmark import compare_to_benchmark, resolve_benchmark
    from stocklab.backtest.metrics import annualize
    from stocklab.backtest.walkforward import (InsufficientData, split_walk_forward,
                                               threshold_arithmetic)
    from stocklab.calendar.trading_calendar import Calendar
    from stocklab.config.costs import CostModel
    from stocklab.strategies.base import ParamError
    from stocklab.strategies.evaluate import evaluate_walk_forward, render_markdown
    from stocklab.strategies.registry import NotRegistered, strategy_registry

    try:
        overrides = _parse_param_overrides(args.param)
    except ValueError as exc:
        print(f"❌ {exc}", file=sys.stderr)
        return 3
    try:
        strategy_registry.meta(args.strategy)     # 未注册在这里就失败
        # 参数校验**前置**：越界/写错名必须在读数据之前报出来，
        # 而不是跑了 131 折之后才抛栈（那时报告已经写了一半）。
        strategy_registry.get(args.strategy, **overrides)
    except NotRegistered as exc:
        print(f"❌ {exc}", file=sys.stderr)
        return 3
    except ParamError as exc:
        print(f"❌ 策略参数不合法：{exc}", file=sys.stderr)
        return 3

    as_of = args.as_of or args.end or _today()
    conn = connect(paths.DB_PATH)
    try:
        universe = _load_universe(conn)
        wanted = [i for i in universe if not args.code or i.code in args.code]
        bars, skipped, window = _load_adjusted(conn, universe, wanted, as_of=as_of,
                                               start=args.start)
        if not bars:
            print(json.dumps({"error": "没有可用标的（全部被复权链拒绝）",
                              "skipped": skipped}, ensure_ascii=False))
            return 1

        calendar = Calendar.load(conn)
        # 会话轴 = 各标的复权行情的**公共交易日** ∩ 日历（口径一致，折与折可比）
        axis = sorted(set.intersection(*(set(b.date for b in v) for v in bars.values())))
        axis = [d for d in axis if calendar.is_open(d)]
        if args.end:
            axis = [d for d in axis if d <= args.end]
        try:
            folds = split_walk_forward(axis, train=args.train, test=args.test,
                                       step=args.step, embargo=args.embargo)
        except InsufficientData as exc:
            print(f"❌ {exc}", file=sys.stderr)
            return 2

        costs = CostModel()
        kw = dict(folds=folds, bars_by_code=bars, calendar=calendar,
                  universe=tuple(i for i in universe if i.code in bars),
                  initial_cash=args.cash, costs=costs)
        perf = evaluate_walk_forward(args.strategy, **overrides, **kw)
        component = None
        if args.strategy != "buy_and_hold":
            component = evaluate_walk_forward("buy_and_hold", **kw)
        bench = resolve_benchmark(conn, args.benchmark,
                                  start=perf.oos_start, end=perf.oos_end)
        cmp = compare_to_benchmark(perf.stitched_nav, args.cash, bench, costs)
        if cmp.benchmark_metrics:
            # 基准的年化也补上（与策略同一把尺子；指数本身不含成本，已在报告披露）
            cmp.benchmark_metrics["annualized_return"] = annualize(
                cmp.benchmark_metrics["total_return"],
                cmp.benchmark_metrics["n_sessions"])
    finally:
        conn.close()

    report = {
        "generated": _today(),
        "session_axis": {"start": axis[0], "end": axis[-1], "n_sessions": len(axis),
                         "mode": "intersection∩calendar"},
        "read_window": window,
        "skipped": skipped,
        "params_requested": overrides,
        "threshold": threshold_arithmetic(
            train=args.train, test=args.test, step=args.step, n_sessions=len(axis),
            oos_trading_days=perf.sample_size.get("effective_n", 0),
            threshold=args.threshold),
        "performance": perf.as_dict(),
        "component_buy_and_hold": component.as_dict() if component else None,
        "benchmark": cmp.as_dict(),
    }
    out_md = Path(args.out) if args.out else (
        paths.REPORT_DIR / f"{_today()}-{args.strategy}-walkforward.md")
    out_json = out_md.with_suffix(".json")
    out_md.parent.mkdir(parents=True, exist_ok=True)
    out_md.write_text(render_markdown(perf, comparison=cmp, component=component,
                                      generated=_today()), encoding="utf-8")
    out_json.write_text(json.dumps(report, ensure_ascii=False, indent=2),
                        encoding="utf-8")

    m = perf.metrics
    ss = perf.sample_size
    print(f"策略: {args.strategy} | 参数: {perf.params}")
    print(f"折数: {len(perf.folds)} | 样本外: {perf.oos_start} ~ {perf.oos_end}")
    print(f"样本外交易日(effective_n): {ss.get('effective_n')} | "
          f"标的-日行数(不得当样本量): {ss.get('oos_rows')} | 标的数: {ss.get('n_codes')}")
    print(f"总收益: {m['total_return'] * 100:.2f}% | 年化: {m.get('annualized_return', 0) * 100:.2f}%"
          f" | 最大回撤: {m['max_drawdown'] * 100:.2f}% | Sharpe: {m['sharpe']:.3f}")
    print(f"成交笔数: {perf.n_trades} / 预热 {perf.n_trades_warmup}"
          f" | 换手: {perf.turnover_x:.3f}×"
          f" | 成本（样本外已扣）: {perf.costs_total:.2f} 元"
          f"（另有预热窗 {perf.costs_warmup:.2f}，不计入曲线）"
          f" | 被拒信号: {perf.n_rejected}")
    if component is not None:
        gap = perf.metrics["total_return"] - component.metrics["total_return"]
        print(f"对照 buy_and_hold（同一 walk-forward 口径）: "
              f"{component.metrics['total_return'] * 100:.2f}%"
              f" | 超额 {gap * 100:+.2f}pp → {'✅ 跑赢' if gap > 0 else '❌ 跑不赢'}")
    if cmp.status == "OK":
        verdict = "✅ 跑赢" if cmp.excess_return > 0 else "❌ 跑不赢"
        print(f"对照 {cmp.benchmark}: {cmp.benchmark_return * 100:.2f}% | "
              f"超额: {cmp.excess_return * 100:.2f}% → {verdict}")
    else:
        print(f"⚠️ 基准不可得（{cmp.note}）—— 不比较不等于跑赢")
    print(f"跳过（复权链拒绝，不回退）: {skipped or '无'}")
    print(f"📄 {out_md}\n📄 {out_json}")
    return 0


# ---------- features ----------

def _bar_from_row(row) -> "Bar":
    from stocklab.data.models import Bar

    return Bar(code=row["code"], date=row["date"], open=row["open"],
               high=row["high"], low=row["low"], close=row["close"],
               volume=row["volume"], amount=row["amount"],
               turnover=row["turnover"], source=row["source"],
               adj_mode=row["adj_mode"])


def cmd_predict_run(args: argparse.Namespace) -> int:
    """产出并落库 `asof` 的预测载荷（**离线**，只读 bars/features/adj 与本表）。

    **本命令不产出任何准确率数字**：预测准不准要由 P7 的次日验证器按 §8.2 评分后
    才可上报。这里的验收标准是「可证伪、可复现、可回放」三件事：

      - **可证伪**：`invalidate_if` 由算出来的关键位导出，P7 直接读它判 `invalidated`；
      - **可复现**：同 `asof` + 同 `model_version` 重复运行 → 载荷**逐字节一致**
        （报告文件里刻意不放任何时间戳，并打印 `payload_sha256` 供比对）；
      - **可回放**：整条链只依赖库里 `<= asof` 的行，任意历史 `asof` 都能算出
        「当时该给的」预测 —— 这是日后测准确率的前提。

    退出码：0 全部落库（含幂等 `identical`）；1 有 `conflict`（同键不同载荷，**不覆盖**）；
    2 非交易日 / 没有任何可出预测的标的 / **`--asof 今天` 但当天 K 线未定型**（见下）。

    ## `--asof 今天` 的定型闸门（P46 §T3）

    `asof` 就是今天时，先问一句「今天的 K 线定型了吗」—— 判据是
    `session.close.bars_finalized_on`（`bars_daily.fetched_at ≥ 当日 15:00`，
    **不看墙上时钟**）。没定型就 exit 2、**一行都不写**。

    这道闸门拦的是 2026-09-22 那次事故：`is_trade_date_closed(今天, now)` 在 15:00
    就为真，而当天 K 线要到 15:30 收盘链的 `ingest bars` 才刷新成终值 —— 于是
    patrol 的 15:00 槽算出了一批**基于半截 bar** 的 LIVE 预测，写进 append-only 的
    `predictions` 后退不回来，15:30 收盘链重算条条冲突（ERROR_DIARY #60）。

    **只在 `asof == 今天` 时生效**：历史日复算（回放）是整套预测体系的立足点，
    那时当天的 K 线早已定型，没有这个问题。
    """
    from stocklab.predict.service import NotASession, build_predictions
    from stocklab.predict.store import PredictionConflict, insert_prediction
    from stocklab.session import close as close_mod

    db = Path(args.db) if args.db else paths.DB_PATH
    if not db.exists():
        print(json.dumps({"error": "db not found; run `stocklab db init`"},
                         ensure_ascii=False), file=sys.stderr)
        return 2
    report_dir = Path(args.report_dir) if args.report_dir else paths.REPORT_DIR
    report_dir.mkdir(parents=True, exist_ok=True)
    now = datetime.now(TZ).isoformat(timespec="seconds")

    ensure_schema(db)   # 写库入口前滚（P33）
    conn = connect(db)
    try:
        # 定型闸门：必须在**任何写入之前**判掉（append-only 写错了退不回来）。
        if args.asof == _today():
            finalized, why = close_mod.bars_finalized_on(conn, args.asof)
            if not finalized:
                print(f"❌ 当日数据未定型，拒绝出预测（asof={args.asof}）：{why}"
                      "；预测用的是收盘价，等收盘链把当天 K 线刷成终值再跑"
                      "（`ops close` 15:30，或手工 `ingest bars` 之后重跑本命令）",
                      file=sys.stderr)
                return 2
        try:
            rep = build_predictions(conn, args.asof, args.code)
        except NotASession as exc:
            print(f"❌ {exc}", file=sys.stderr)
            return 2
        except ValueError as exc:            # 日历为空等
            print(f"❌ {exc}", file=sys.stderr)
            return 2

        if not rep["predictions"]:
            print(json.dumps({"asof_date": rep["asof_date"], "skipped": rep["skipped"]},
                             ensure_ascii=False), file=sys.stderr)
            print(f"❌ {rep['asof_date']} 没有任何可出预测的标的", file=sys.stderr)
            return 2

        states: dict[str, str] = {}
        conflicts: dict[str, str] = {}
        for p in rep["predictions"]:
            try:
                state, pred_id = insert_prediction(conn, p, now=now, origin="live")
                states[p["code"]] = f"{state}:{pred_id}"
            except PredictionConflict as exc:
                conflicts[p["code"]] = str(exc)
    finally:
        conn.close()

    path = Path(args.report) if args.report else \
        report_dir / f"{_today()}-predict-{args.asof}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    # 逐字节可复现：报告里**不放**任何随运行变化的东西 —— 时间戳
    # （created_at 只落库）与落库状态（`inserted` vs `identical`）都不写进文件。
    # 于是「同 asof + 同 model_version 两次运行」的文件 sha256 天然相等，
    # 文件本身就成了可复现性的证据；落库状态只出现在 stdout 与库里。
    blob = json.dumps(rep, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
    path.write_text(blob, encoding="utf-8")
    digest = hashlib.sha256(blob.encode("utf-8")).hexdigest()

    print(json.dumps({"report": str(path), "sha256": digest,
                      "asof_date": rep["asof_date"], "target_date": rep["target_date"],
                      "target_date_source": rep["target_date_source"],
                      "model_version": rep["model_version"],
                      "payload_sha256": rep["payload_sha256"],
                      "storage": states},
                     ensure_ascii=False, indent=2))
    for code, p in sorted(rep["payload_sha256"].items()):
        print(f"  {code}: payload_sha256={p}")
    if conflicts:
        for code, msg in sorted(conflicts.items()):
            print(f"❌ {code} 预测落库冲突：{msg}", file=sys.stderr)
        return 1
    for code, reason in sorted(rep["skipped"].items()):
        print(f"⚠️  {code} 跳过：{reason}", file=sys.stderr)
    return 0


def cmd_verify_run(args: argparse.Namespace) -> int:
    """给 `target_date` 的全部预测打分并落库（**离线只读** bars/adj + 写 verifications）。

    三件必须显式说出来的事（都不许静默）：

      - **不可评分**：结果列全空 + `attribution_auto=DATA` + 原因码；照样落库，
        但不进任何成功分母。目标日还没产生 K 线时这是**正常结论**，不是错误；
      - **归因**：程序只写 `DATA`；`SIGNAL/STRATEGY/MODEL/NOISE` 一律 `UNDETERMINED`，
        等人工在 `attribution_manual` 列回填（§9）；
      - **补分**：上次不可评分、这次数据到了 → 更新结果列并打印 ⚠️（唯一允许的更新）。

    退出码：0 全部落库（含幂等 `identical`）/ 2 该日没有任何预测 / 1 内容冲突（**不覆盖**）。
    """
    from stocklab.verify.service import NoPredictions, verify_target
    from stocklab.verify.store import VerificationConflict

    db = Path(args.db) if args.db else paths.DB_PATH
    if not db.exists():
        print(json.dumps({"error": "db not found; run `stocklab db init`"},
                         ensure_ascii=False), file=sys.stderr)
        return 2
    report_dir = Path(args.report_dir) if args.report_dir else paths.REPORT_DIR
    report_dir.mkdir(parents=True, exist_ok=True)
    now = datetime.now(TZ).isoformat(timespec="seconds")

    ensure_schema(db)   # 写库入口前滚（P33）
    conn = connect(db)
    try:
        try:
            rep = verify_target(conn, args.target_date, codes=args.code, now=now)
        except NoPredictions as exc:
            print(f"❌ {exc}", file=sys.stderr)
            return 2
        except VerificationConflict as exc:
            print(f"❌ {exc}", file=sys.stderr)
            return 1
    finally:
        conn.close()

    path = Path(args.report) if args.report else \
        report_dir / f"{_today()}-verify-{args.target_date}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    # 与 `predict run` 同纪律：文件里**不放运行态**（storage 在两次运行间会变：
    # inserted → identical），所以「同输入两次运行 sha256 相等」本身就是幂等证据。
    blob = json.dumps(_strip_run_state(rep), ensure_ascii=False, sort_keys=True,
                      indent=2) + "\n"
    path.write_text(blob, encoding="utf-8")
    digest = hashlib.sha256(blob.encode("utf-8")).hexdigest()

    print(json.dumps({"report": str(path), "sha256": digest,
                      "target_date": rep["target_date"],
                      "storage": rep["storage"],
                      "by_model_version": rep["by_model_version"]},
                     ensure_ascii=False, indent=2))
    for code in rep["unscorable"]:
        print(f"⚠️  {code['code']} [{code['model_version']}] 不可评分："
              f"{code['reason_code']} —— {code['reason']}", file=sys.stderr)
    for pred_id, state in sorted(rep["storage"].items()):
        if state.startswith("rescored_after_data_gap"):
            print(f"⚠️  pred_id={pred_id} 上次不可评分（数据未到），本次已补分"
                  f"（{state}）—— 这是 ADR-002 允许的唯一一种结果列更新",
                  file=sys.stderr)
    return 0


def cmd_verify_backfill(args: argparse.Namespace) -> int:
    """历史回放：逐日 `predict` + 次日打分，产出**第一份准确率报告**（**离线只读**）。

    报告里的每个数字都来自库里的 `verifications` 行（不是内存里攒的），
    所以「同 `--from/--to` 重复跑 → 报告逐字节一致」是结构性的，不是碰巧。

    退出码：0 完成（含全部幂等 `identical`）/ 2 用法或数据库问题 / 1 有预测冲突。
    """
    from stocklab.predict.service import PitCache
    from stocklab.verify.replay import backfill, load_verification_rows
    from stocklab.verify.report import render_markdown, summarize

    db = Path(args.db) if args.db else paths.DB_PATH
    if not db.exists():
        print(json.dumps({"error": "db not found; run `stocklab db init`"},
                         ensure_ascii=False), file=sys.stderr)
        return 2
    report_dir = Path(args.report_dir) if args.report_dir else paths.REPORT_DIR
    report_dir.mkdir(parents=True, exist_ok=True)

    ensure_schema(db)   # 写库入口前滚（P33）
    conn = connect(db)
    try:
        rep = backfill(conn, args.from_date, args.to_date, codes=args.code,
                       cache=PitCache(), progress=_progress)
        if rep["conflicts"]:
            for key, msg in sorted(rep["conflicts"].items()):
                print(f"❌ {key} 预测落库冲突：{msg}", file=sys.stderr)
            return 1
        rows = load_verification_rows(conn, args.from_date, args.to_date)
    finally:
        conn.close()

    summary = summarize(rows, from_date=args.from_date, to_date=args.to_date)
    md = render_markdown(summary)
    path = Path(args.report) if args.report else (
        report_dir / f"{_today()}-accuracy-{args.from_date}-{args.to_date}.md")
    path.parent.mkdir(parents=True, exist_ok=True)
    # 逐字节可复现：正文**不含**生成时间，只含区间与数字（见 verify/report.py）
    path.write_text(md, encoding="utf-8")
    digest = hashlib.sha256(md.encode("utf-8")).hexdigest()
    json_path = path.with_suffix(".json")
    json_path.write_text(json.dumps(summary, ensure_ascii=False, sort_keys=True,
                                    indent=2) + "\n", encoding="utf-8")

    print(json.dumps({
        "report": str(path), "summary_json": str(json_path), "sha256": digest,
        "from": args.from_date, "to": args.to_date,
        "n_targets": rep["n_sessions_in_range"],
        "n_verified_days": len({r["target_date"] for r in rows}),
        "n_rows": len(rows),
        "skipped": rep["skipped"],
        "by_model_version": {
            mv: {"n": g["n_predictions"], "scorable": g["n_scorable"],
                 "unscorable": g["n_unscorable"],
                 "effective_n_days": g["effective_n"],
                 "direction_accuracy_row": g["direction"]["accuracy_row"],
                 "direction_accuracy_daily": g["direction"]["accuracy_daily"],
                 "brier_row": g["direction"]["brier_row"],
                 "range_coverage_row": g["range"]["coverage_row"],
                 "sample_gate": g["sample_gate"]["label"]}
            for mv, g in summary["model_versions"].items()},
    }, ensure_ascii=False, indent=2))
    return 0


def cmd_verify_pending(args: argparse.Namespace) -> int:
    """补齐**已到期却没有验证行**的预测（幂等、append-only；P27）。

    ## 为什么需要单独一条命令

    `session tick` 步骤③ 有验证逻辑，但它的 cutoff 只来自 `trading_calendar`，
    而日历行由 `ingest index` 在 15:30+ 才写 —— 比当天最后一次 tick 晚。
    于是「今天到期」的预测**永远轮不到它被验证**（实测 2026-09-16：
    tick 15:23:35 / 日历行 15:32:56 → `verify_inserted+0`）。详见
    `stocklab/verify/pending.py` 的模块 docstring。

    本命令的到期判据取 `trading_calendar ∪ bars_daily`（日 K 只在收盘后入库，
    所以「有 bar」本身就是收盘已完成的正面证据），且**只补缺失的行**。

    ## 幂等与 append-only

    - 打分**完全复用** `stocklab/verify/service.verify_target`（不另造阈值/公式）；
    - 逐 `target_date` 调用，且只传该日**待验证的 `codes`** —— 已评分的行一个字节不碰；
    - 第二次跑时选择集为空 → 报「无待验证」、**零写入**
      （结构性保证还叠了一层 `ux_verifications_pred` 唯一索引）；
    - 命令**自检**：处理完再算一次待验证集合输出 `pending_after`，不为 0 → 退出码 1。

    退出码：0 完成（含无待验证）/ 2 判不出最新已收盘交易日或库不存在 / 1 有冲突或没补干净。
    """
    from stocklab.verify.pending import pending_predictions
    from stocklab.verify.service import NoPredictions, verify_target
    from stocklab.verify.store import VerificationConflict

    db = Path(args.db) if args.db else paths.DB_PATH
    if not db.exists():
        print(json.dumps({"error": "db not found; run `stocklab db init`"},
                         ensure_ascii=False), file=sys.stderr)
        return 2
    now = args.now or datetime.now(TZ).isoformat(timespec="seconds")
    only = set(args.code or [])

    ensure_schema(db)   # 写库入口前滚（P33）
    conn = connect(db)
    try:
        pend = pending_predictions(conn, now)
        if pend["n"] is None:
            # 判不了就说判不了：绝不给一个 0（0 会被读成「没有缺口」，#36）
            print(json.dumps({"error": pend["reason"], "note": pend["note"],
                              "evidence": pend["evidence"]},
                             ensure_ascii=False, sort_keys=True, indent=2),
                  file=sys.stderr)
            return 2
        rows = [r for r in pend["rows"] if not only or r["code"] in only]
        by_target: dict[str, list[dict]] = {}
        for r in rows:
            by_target.setdefault(r["target_date"], []).append(r)

        agg = {"inserted": 0, "identical": 0, "rescored_after_data_gap": 0}
        targets: dict[str, dict] = {}
        unscorable: list[dict] = []
        failed = False
        for target_date in sorted(by_target):
            codes = sorted({r["code"] for r in by_target[target_date]})
            try:
                rep = verify_target(conn, target_date, codes=codes, now=now)
            except (NoPredictions, VerificationConflict) as exc:
                print(f"❌ {target_date} 补分失败：{exc}", file=sys.stderr)
                failed = True
                continue
            n_inserted = 0
            for state in rep["storage"].values():
                key = state.split(":")[0]
                if key in agg:
                    agg[key] += 1
                if key == "inserted":
                    n_inserted += 1
                if key == "rescored_after_data_gap":
                    print(f"⚠️  {target_date} pred_id 上次不可评分（数据未到），"
                          f"本次已补分（{state}）—— ADR-002 允许的唯一一种结果列更新",
                          file=sys.stderr)
            unscorable.extend({"target_date": target_date, **u}
                              for u in rep["unscorable"])
            targets[target_date] = {"pending": len(by_target[target_date]),
                                    "inserted": n_inserted}
        after = pending_predictions(conn, now)
    finally:
        conn.close()

    out = {
        "latest_closed_session": pend["latest_closed_session"],
        "pending_before": pend["n"],
        "pending_after": after["n"],
        "targets": targets,
        **agg,
        "unscorable": unscorable,
        "note": None if pend["n"] else "无待验证的到期预测",
        "rule": pend["evidence"]["rule"],
        "hint": None if rows else pend["hint"],
    }
    print(json.dumps(out, ensure_ascii=False, sort_keys=True, indent=2))
    for u in unscorable:
        print(f"⚠️  {u['target_date']} {u['code']} [{u['model_version']}] 不可评分："
              f"{u['reason_code']} —— {u['reason']}", file=sys.stderr)
    if after["n"]:
        print(f"❌ 仍有 {after['n']} 条已到期预测没有验证行 —— 补分没补干净",
              file=sys.stderr)
    return 1 if (failed or after["n"]) else 0


def cmd_experiment_run(args: argparse.Namespace) -> int:
    """跑一个具名变体并产出实验报告（**离线**；**不写任何生产表**）。

    三件必须显式说出来的事：

      - **不落库**：变体不是模型版本。实验全程在内存里评估，
        `predictions` / `verifications` 一行都不写 —— 只有 `promoted` 才允许
        另开 ADR 与新 `model_version`（那是下一次决策的事）；
      - **口径一致**：打分/聚合/呈现三层全部复用 P7 的函数（见
        `stocklab/experiments/runner.py` 的模块 docstring），唯一替换的是「预测从哪来」；
      - **test 封存**：第一趟循环的范围里**根本没有 test 那些日期**；
        只有 validate `WIN` 才跑第二趟去读 test，读一次。

    退出码：0 完成 / 2 用法、数据库或切分问题 / 1 结论为 `falsified`（**不是错误**，
    但让 CI/脚本能区分「跑通了且否证」与「跑通了且晋级」）。
    """
    from stocklab.experiments.metrics import TestSetLeak
    from stocklab.experiments.runner import (NoReplayDays, run_experiment,
                                             write_report, write_report_at)
    from stocklab.experiments.split import SplitConfig, SplitConfigError
    from stocklab.experiments.variants import (MultiVariableVariant,
                                               UnknownVariant)

    db = Path(args.db) if args.db else paths.DB_PATH
    if not db.exists():
        print(json.dumps({"error": "db not found; run `stocklab db init`"},
                         ensure_ascii=False), file=sys.stderr)
        return 2
    report_dir = Path(args.report_dir) if args.report_dir else paths.REPORT_DIR
    try:
        cfg = SplitConfig(train=args.train_ratio, validate=args.validate_ratio,
                          test=args.test_ratio)
    except SplitConfigError as exc:
        print(f"❌ 切分配置不合法：{exc}", file=sys.stderr)
        return 2

    conn = connect(db)
    try:
        rep = run_experiment(conn, variant_name=args.variant,
                             from_date=args.from_date, to_date=args.to_date,
                             codes=args.code, split_config=cfg,
                             selection_split=args.selection_split,
                             evaluate_test_on_win=not args.keep_test_sealed,
                             progress=_progress)
    except (UnknownVariant, MultiVariableVariant, TestSetLeak) as exc:
        print(f"❌ 实验被拒绝：{type(exc).__name__}: {exc}", file=sys.stderr)
        return 2
    except (NoReplayDays, SplitConfigError) as exc:
        print(f"❌ {exc}", file=sys.stderr)
        return 2
    finally:
        conn.close()

    written = (write_report_at(rep, Path(args.report)) if args.report
               else write_report(rep, report_dir, _today()))

    # ---- 决策落库（Task 43）：**只落决策、不落预测** ----
    # 报告写完后才知道自己的 sha256，所以这一步排在 write_report 之后。
    # 幂等键含该 sha256：重跑同一场实验 → `identical`，不刷重复行。
    from stocklab.experiments.decisions import (decisions_from_report,
                                                record_decisions)
    try:
        conn = connect(db)
        try:
            recorded = record_decisions(
                conn, decisions_from_report(rep,
                                            report_sha256=written["sha256_json"]))
        finally:
            conn.close()
    except sqlite3.OperationalError as exc:
        if "no such table" not in str(exc):
            raise
        print(f"❌ 决策落库失败：{exc} —— 库没前滚，先跑 "
              "`stocklab db init`（幂等，会先自动备份）", file=sys.stderr)
        return 2

    print(json.dumps({
        "report": written["markdown"], "summary_json": written["json"],
        "sha256_md": written["sha256_md"], "sha256_json": written["sha256_json"],
        "metric_version": rep["metric_version"],
        "variant": rep["variant"]["name"],
        "changed_fields": rep["variant"]["changed_fields"],
        "range": rep["range"],
        "split_boundaries": rep["split_boundaries"],
        "selection_split": rep["selection_split"],
        "test_evaluated": rep["test_evaluated"],
        "gates": {name: s["gate"]["status"] for name, s in rep["splits"].items()},
        "verdict": rep["verdict"]["status"],
        "verdict_reasons": rep["verdict"]["reasons"],
        "counts": rep["counts"],
        "decisions_recorded": recorded,
    }, ensure_ascii=False, indent=2))
    if not rep["test_evaluated"]:
        print(f"🔒 test 段未打开：{rep['test_not_evaluated_reason']}", file=sys.stderr)
    return 1 if rep["verdict"]["status"] == "falsified" else 0


def cmd_trend_evaluate(args: argparse.Namespace) -> int:
    """跑预注册 `trend-state-hit-rate` 的正式版验证（**离线**；**不写任何生产表**）。

    三件必须显式说出来的事：

      - **口径照抄预注册**：状态定义 / `N=5` / 标的 / 判据全部写在
        `stocklab/trend/evaluate.py` 的常量里，本层一个口径都不新增；
      - **test 段封存**：validate 未达标**根本不读** test（不是「读了不报」）；
        达标才打开一次复核；
      - **不落库**：只 `SELECT` `bars_daily` / `raw_fetch_cache`，产物只有 `reports/`。

    退出码：0 完成（`WIN` / `inconclusive`）/ 2 用法、数据库或数据问题 /
    1 结论为 `falsified`（**不是错误**，但让 CI/脚本能区分「跑通了且否证」与「跑通了且达标」）。
    """
    from stocklab.experiments.split import SplitConfig, SplitConfigError
    from stocklab.trend.evaluate import (AdjustModeError, PreregViolation,
                                         UnmappedRows, run_evaluation, write_report)

    db = Path(args.db) if args.db else paths.DB_PATH
    if not db.exists():
        print(json.dumps({"error": "db not found; run `stocklab db init`"},
                         ensure_ascii=False), file=sys.stderr)
        return 2
    try:
        cfg = SplitConfig(train=args.train_ratio, validate=args.validate_ratio,
                          test=args.test_ratio)
    except SplitConfigError as exc:
        print(f"❌ 切分配置不合法：{exc}", file=sys.stderr)
        return 2

    conn = connect(db)
    try:
        rep = run_evaluation(conn, from_date=args.from_date, to_date=args.to_date,
                             split_config=cfg, min_days=args.min_days,
                             keep_test_sealed=args.keep_test_sealed)
    except (PreregViolation, AdjustModeError, UnmappedRows) as exc:
        print(f"❌ 实验被拒绝：{type(exc).__name__}: {exc}", file=sys.stderr)
        return 2
    except ValueError as exc:
        print(f"❌ {exc}", file=sys.stderr)
        return 2
    finally:
        conn.close()

    report_dir = Path(args.report_dir) if args.report_dir else paths.REPORT_DIR
    out = (Path(args.report) if args.report
           else report_dir / f"{_today()}-trend-state-hit-rate.md")
    written = write_report(rep, out)

    verdict = rep["verdict"]
    print(json.dumps({
        "report": written["markdown"], "summary_json": written["json"],
        "sha256_md": written["sha256_md"], "sha256_json": written["sha256_json"],
        "trend_metric_version": rep["trend_metric_version"],
        "framework_metric_version": rep["framework_metric_version"],
        "prereg_doc": rep["prereg_doc"],
        "universe": rep["universe"],
        "range": rep["range"],
        "horizon": rep["horizon"], "ma": rep["ma"],
        "split_boundaries": rep["split_boundaries"],
        "selection_split": rep["selection_split"],
        "test_evaluated": rep["test_evaluated"],
        "test_not_evaluated_reason": rep["test_not_evaluated_reason"],
        "validate_n_days": rep["splits"]["validate"]["n_days"],
        "criteria": verdict["criteria"],
        "verdict": verdict["status"],
        "verdict_reasons": verdict["reasons"],
        "bootstrap": rep["bootstrap"],
        "data_snapshot": rep["data_snapshot"],
        "counts": rep["counts"],
    }, ensure_ascii=False, indent=2))
    if not rep["test_evaluated"]:
        print(f"🔒 test 段未打开：{rep['test_not_evaluated_reason']}", file=sys.stderr)
    return 1 if verdict["status"] == "falsified" else 0


def _progress(target: str, done: int, total: int) -> None:
    if done % 250 == 0 or done == total:
        print(f"  … 回放 {done}/{total} 天（最近 {target}）", file=sys.stderr)


def _strip_run_state(rep: dict) -> dict:
    """去掉「这次跑成什么样」的信息（storage / verification_id 之外的运行态）。

    留下的只有**内容**：预测 id、分数、原因、分组。两次运行内容一致 → 文件一致。
    """
    out = {k: v for k, v in rep.items() if k != "storage"}
    out["rows"] = [{k: v for k, v in row.items() if k != "storage"}
                   for row in rep["rows"]]
    return out


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
    ensure_schema(db)
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


# ---------- calendar holidays（P30：已公告休市安排） ----------

def cmd_calendar_holidays_fetch(*, db=None, max_articles: int = 30,
                                client=None) -> int:
    """采集交易所休市安排公告并落 `market_holidays`（**联网**）。

    `client` 可注入（测试用回放 client；默认按配置建 `HttpClient`）。
    **fail-closed**：`fetch_holiday_notices` 解析不出就抛错 → 本命令返回 1，
    **一条都不落库**（旧表原样保留）。
    """
    from stocklab.calendar.holidays import load_holiday_table, save_holidays
    from stocklab.data.fetch import fetch_holiday_notices, policy_from_settings
    from stocklab.data.http import HttpClient
    from stocklab.data.raw_cache import RawCache

    paths.ensure_dirs()
    db_path = Path(db) if db else paths.DB_PATH
    now = datetime.now(TZ).isoformat(timespec="seconds")
    if client is None:
        settings = load_settings()
        cache = RawCache(paths.RAW_CACHE_DIR) if settings.cache_enabled else None
        client = HttpClient(policy_from_settings(settings), cache=cache)

    conn = connect(db_path)
    try:
        try:
            rows = fetch_holiday_notices(client, max_articles=max_articles)
        except Exception as exc:                       # fail-closed：一条都不落库
            repo.log_event(conn, "ingest", "error",
                           f"休市安排抓取/解析失败：{exc}", now=now)
            print(json.dumps({"error": f"{type(exc).__name__}: {exc}",
                              "inserted": 0}, ensure_ascii=False))
            return 1
        inserted = save_holidays(conn, rows, now=now)
        table = load_holiday_table(conn)
        b = table.bounds()
        out = {
            "parsed_rows": len(rows),
            "inserted": inserted,
            "closed_dates": len(table.closed),
            "annual_years": sorted(table.annual_years),
            "first": b[0] if b else None,
            "last": b[1] if b else None,
        }
    finally:
        conn.close()
    print(json.dumps(out, ensure_ascii=False))
    return 0


def cmd_calendar_holidays_show(*, db=None, limit: int = 40) -> int:
    """查已公告休市表（**离线只读**）。"""
    from stocklab.calendar.holidays import load_holiday_table

    db_path = Path(db) if db else paths.DB_PATH
    conn = connect(db_path)
    try:
        table = load_holiday_table(conn)
        closed = sorted(table.closed)
        recent = [r for r in table.rows if r.date in set(closed[-limit:])]
        out = {
            "rows": len(table.rows),
            "closed_dates": len(table.closed),
            "annual_years": sorted(table.annual_years),
            "bounds": table.bounds(),
            "closed": [{"date": r.date, "doc_kind": r.doc_kind,
                        "covered_year": r.covered_year, "published_at": r.published_at,
                        "source_url": r.source_url} for r in recent],
        }
    finally:
        conn.close()
    print(json.dumps(out, ensure_ascii=False, indent=2))
    return 0


def _cmd_calendar_holidays_fetch(args: argparse.Namespace) -> int:
    return cmd_calendar_holidays_fetch(db=args.db, max_articles=args.max_articles)


def _cmd_calendar_holidays_show(args: argparse.Namespace) -> int:
    return cmd_calendar_holidays_show(db=args.db, limit=args.limit)


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


# ---------- session tick（盘中采集 + 收盘回填 + 到期验证） ----------

def _tick_universe(conn):
    """tick 的标的集合：库里的 active 股票，库为空时退回 `DEFAULT_UNIVERSE`。

    数据库是**真源**（`instruments` 可增删），`DEFAULT_UNIVERSE` 只是首次建库的种子。
    库为空说明还没 `db init` / `ingest`，此时退回种子并在摘要里能看出来。
    """
    try:
        loaded = _load_universe(conn)
    except sqlite3.Error:
        return tuple(DEFAULT_UNIVERSE)
    return loaded or tuple(DEFAULT_UNIVERSE)


def cmd_session_tick(args: argparse.Namespace) -> int:
    """一次调用完成「采集 → 收盘回填 → 验证到期预测」并输出机器可读 JSON 摘要。

    给 cron 用：**stdout 只有一段 JSON**（异常另写 stderr），退出码 0/1/2。
    幂等：同 `ts` 的快照不重复写、已验证的预测不重复记分 —— 所以「每 2 小时跑一次」
    不会因为重试或补跑而多出任何一行。

    **本命令不入 raw_cache**：快照的全部价值在「此刻」，命中缓存 = 拿旧截面冒充新截面。
    """
    from stocklab.data.fetch import fetch_quotes, policy_from_settings
    from stocklab.data.http import HttpClient
    from stocklab.session.quotes import SNAPSHOT_SOURCE
    from stocklab.session.tick import run_tick

    db = Path(args.db) if args.db else paths.DB_PATH
    if not db.exists():
        print(json.dumps({"error": "db not found; run `stocklab db init`"},
                         ensure_ascii=False), file=sys.stderr)
        return 2
    now = args.now or datetime.now(TZ).isoformat(timespec="seconds")
    settings = load_settings()
    client = HttpClient(policy_from_settings(settings), cache=None)

    conn = connect(db)
    try:
        if not _has_session_tables(conn):
            print(json.dumps({"error": "quote_snapshots 不存在；先跑 "
                                       "`stocklab db init` 前滚 schema"},
                             ensure_ascii=False), file=sys.stderr)
            return 2
        ensure_schema(db)   # 写库入口前滚（P33）：先过 session 表守卫再补列
        universe = tuple(i for i in _tick_universe(conn)
                         if not args.code or i.code in args.code)
        summary = run_tick(
            conn, now=now, universe=universe, window=args.window,
            capture=not args.no_capture, source=SNAPSHOT_SOURCE,
            fetch=lambda codes: fetch_quotes(client, codes=codes),
        )
    finally:
        conn.close()

    blob = json.dumps(summary, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
    if args.out:
        out = Path(args.out)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(blob, encoding="utf-8")
    print(blob, end="")
    for a in summary["anomalies"]:
        print(f"⚠️  {a['kind']}: {a.get('detail') or a.get('codes') or ''}",
              file=sys.stderr)
    return int(summary["exit_code"])


def _has_session_tables(conn) -> bool:
    """schema 是否已前滚到 P11（`quote_snapshots` 存在）。"""
    return conn.execute(
        "SELECT COUNT(*) FROM sqlite_master WHERE type='table'"
        " AND name='quote_snapshots'").fetchone()[0] > 0


def cmd_session_backfill_close(args: argparse.Namespace) -> int:
    """手动补跑收盘回填（幂等；只补当日、只补 NULL）。

    这条命令存在的理由是**补跑**：收盘那次 tick 若因接口不通没跑成，
    第二天还能拿已落库的快照把 `amount`/`turnover` 补上。它不抓任何数据。
    """
    from stocklab.session import close as close_mod

    db = Path(args.db) if args.db else paths.DB_PATH
    if not db.exists():
        print(json.dumps({"error": "db not found; run `stocklab db init`"},
                         ensure_ascii=False), file=sys.stderr)
        return 2
    now = args.now or datetime.now(TZ).isoformat(timespec="seconds")
    ensure_schema(db)   # 写库入口前滚（P33）
    conn = connect(db)
    try:
        if not _has_session_tables(conn):
            print(json.dumps({"error": "quote_snapshots 不存在；先跑 "
                                       "`stocklab db init` 前滚 schema"},
                             ensure_ascii=False), file=sys.stderr)
            return 2
        out = close_mod.backfill_close_amounts(conn, args.date, now=now)
    finally:
        conn.close()
    print(json.dumps(out, ensure_ascii=False, sort_keys=True, indent=2))
    for code in out["skipped_missing_bar"]:
        print(f"⚠️  {code} {args.date} 无 bars_daily 当日行 —— 先跑 "
              f"`ingest bars` 再补跑回填", file=sys.stderr)
    return 1 if out["skipped_missing_bar"] else 0


# ---------- review daily（15:30 整体数据复盘） ----------

def cmd_review_daily(args: argparse.Namespace) -> int:
    """生成 `reports/YYYY-MM-DD-review.md` + `.json`（离线只读；不写任何数据表）。

    ⚠️ 报告里的准确率**默认是 PIT 历史回放口径**（当前 38.14% / Brier 0.6581）。
    口径由 `provenance` 判据按**实测行数**分列（LIVE / REPLAY），**不是**按
    「有没有实盘开关」—— 见 `session/review.py` 与 ERROR_DIARY #16。
    """
    from stocklab.session.review import build_review, render_markdown

    db = Path(args.db) if args.db else paths.DB_PATH
    if not db.exists():
        print(json.dumps({"error": "db not found; run `stocklab db init`"},
                         ensure_ascii=False), file=sys.stderr)
        return 2
    date = args.date or _today()
    ensure_schema(db)   # 写库入口前滚（P33）
    conn = connect(db)
    try:
        if not _has_session_tables(conn):
            print(json.dumps({"error": "quote_snapshots 不存在；先跑 "
                                       "`stocklab db init` 前滚 schema"},
                             ensure_ascii=False), file=sys.stderr)
            return 2
        rep = build_review(conn, date, n_sessions=args.window)
    finally:
        conn.close()

    md = render_markdown(rep)
    path = Path(args.out) if args.out else (paths.REPORT_DIR / f"{date}-review.md")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(md, encoding="utf-8")
    json_path = path.with_suffix(".json")
    # 与 predict/verify 报告同纪律：正文**不含生成时刻**，同输入两次运行逐字节一致。
    json_path.write_text(
        json.dumps(rep, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8")

    roll = rep["rolling"]
    live, replay = roll.get("live"), roll.get("replay")
    print(json.dumps({
        "report": str(path), "json": str(json_path),
        "date": date,
        "bars_latest_date": rep["freshness"]["bars_latest_date"],
        "snapshots": rep["freshness"]["snapshots"],
        "target_predictions": rep["day"]["predictions"],
        "target_verifications": {
            "n": rep["day"]["verifications"]["n"],
            "scorable": rep["day"]["verifications"]["scorable"],
            "unscorable": rep["day"]["verifications"]["unscorable"],
            # 逐版行数：升版后 `n` 是两个版本相加，**不是独立样本数**
            "by_model_version": rep["day"]["verifications"].get("by_model_version")},
        # 缺步检测（P27）：`null` = 判不了（**不是** 0，见 ERROR_DIARY #36）
        "pending_verifications": rep["pending"]["n"],
        "pending_verifications_detail": {
            k: v for k, v in rep["pending"].items() if k != "evidence"},
        "rolling_window": roll["window"],
        # 口径版本：下面 live/replay 两桶**只含这一版**（2026-09-21 升 v1.0.2）
        "model_version": roll.get("model_version"),
        "provenance": {
            "rule": roll["provenance"]["rule"],
            "live_rows": roll["provenance"]["live"]["n_rows"],
            "replay_rows": roll["provenance"]["replay"]["n_rows"],
            "rows_by_model_version": roll["provenance"].get("by_model_version")},
        # 旧版本读数（口径不同）：**不参与**上面的 live/replay 桶，也不得与它们相加/平均
        "excluded": roll.get("excluded"),
        "live": live,
        "replay": ({"n_rows": replay["n_rows"],
                    "direction_accuracy_daily": replay["direction_accuracy_daily"],
                    "direction_ci95": replay["direction_ci95"],
                    "brier_daily": replay["brier_daily"],
                    "effective_n_days": replay["effective_n_days"],
                    "sample_gate": replay["sample_gate"]} if replay else None),
        "experiment_decisions_rows": rep["experiments"]["rows"],
        "gaps": {k: rep["gaps"][k] for k in
                 ("bars_daily_rows", "amount_non_null", "amount_first_date",
                  "turnover_non_null", "turnover_first_date")},
        "disclosure": rep["disclosure"],
    }, ensure_ascii=False, sort_keys=True, indent=2))
    return 0


# ---------- 实盘账本 + 组合视图（P12） ----------

def _portfolio_conn(args):
    """打开库并确认 P12 的表已前滚；返回 `(conn, None)` 或 `(None, 退出码)`。"""
    db = Path(args.db) if args.db else paths.DB_PATH
    if not db.exists():
        print(json.dumps({"error": "db not found; run `stocklab db init`"},
                         ensure_ascii=False), file=sys.stderr)
        return None, 2
    conn = connect(db)
    has = conn.execute(
        "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name IN"
        " ('real_trades','cash_flows','ledger_idem')").fetchone()[0]
    if has < 3:
        conn.close()
        print(json.dumps({"error": "账本表不存在；先跑 `stocklab db init` 前滚 schema"},
                         ensure_ascii=False), file=sys.stderr)
        return None, 2
    return conn, None


def _emit(args, payload: dict, human: str) -> None:
    """`--json` 出机器可读、否则出人看的（两者同源）。"""
    if getattr(args, "json", False):
        print(json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2))
    else:
        print(human)


def cmd_trade_add(args: argparse.Namespace) -> int:
    """录入一笔实盘成交（append-only；改错只能 `trade reverse`）。"""
    from stocklab.portfolio.ledger import LedgerError, record_trade

    conn, code = _portfolio_conn(args)
    if conn is None:
        return code
    now = args.now or datetime.now(TZ).isoformat(timespec="seconds")
    try:
        out = record_trade(conn, date=args.date, code=args.code, side=args.side,
                           price=args.price, qty=args.qty, fee=args.fee,
                           note=args.note, now=now,
                           idem_key=args.idempotency_key,
                           allow_duplicate=args.allow_duplicate)
    except LedgerError as exc:
        print(json.dumps({"error": str(exc), "kind": type(exc).__name__},
                         ensure_ascii=False), file=sys.stderr)
        return 2
    finally:
        conn.close()
    verb = "幂等命中（未重复写入）" if out["state"] == "identical" else "已录入"
    _emit(args, out,
          f"✅ {verb} trade_id={out['trade_id']}  "
          f"{out['date']} {out['side']} {out['code']} {out['qty']}股"
          f" @{args.price} 费{args.fee}")
    return 0


def cmd_trade_reverse(args: argparse.Namespace) -> int:
    """冲正一笔成交：追加反向记录 + note，**不改原行**。"""
    from stocklab.portfolio.ledger import LedgerError, reverse_trade

    conn, code = _portfolio_conn(args)
    if conn is None:
        return code
    now = args.now or datetime.now(TZ).isoformat(timespec="seconds")
    try:
        out = reverse_trade(conn, args.trade_id, reason=args.reason, now=now,
                            idem_key=args.idempotency_key)
    except LedgerError as exc:
        print(json.dumps({"error": str(exc), "kind": type(exc).__name__},
                         ensure_ascii=False), file=sys.stderr)
        return 2
    finally:
        conn.close()
    _emit(args, out, f"✅ 已冲正 #{args.trade_id} → 新记录 trade_id={out['trade_id']}"
                     f"（原行未改动）")
    return 0


def cmd_cash_add(args: argparse.Namespace) -> int:
    """录入本金 / 现金流。`amount` **有符号**：正=流入，负=流出，须与 kind 一致。"""
    from stocklab.portfolio.ledger import LedgerError, record_cash_flow

    conn, code = _portfolio_conn(args)
    if conn is None:
        return code
    now = args.now or datetime.now(TZ).isoformat(timespec="seconds")
    try:
        out = record_cash_flow(conn, date=args.date, kind=args.kind,
                               amount=args.amount, note=args.note, now=now,
                               idem_key=args.idempotency_key,
                               allow_duplicate=args.allow_duplicate)
    except LedgerError as exc:
        print(json.dumps({"error": str(exc), "kind": type(exc).__name__},
                         ensure_ascii=False), file=sys.stderr)
        return 2
    finally:
        conn.close()
    verb = "幂等命中（未重复写入）" if out["state"] == "identical" else "已录入"
    _emit(args, out, f"✅ {verb} flow_id={out['flow_id']}  "
                     f"{out['date']} {out['kind']} {out['amount']:+,.2f}")
    return 0


def cmd_portfolio_show(args: argparse.Namespace) -> int:
    """组合视图（离线只读）。

    退出码：`0` 一切正常 / `1` **要人来看**（有标的缺现价，或有纪律条目 FAIL）
    / `2` 用法或库的问题。

    `1` 不是"命令失败"——视图照样打印完整。它让调度器能机械地发现
    「单票超 40%」或「某只票算不出市值」，而不必去解析人看的文本。
    """
    from stocklab.portfolio.view import build_portfolio, has_alarm, render_table

    conn, code = _portfolio_conn(args)
    if conn is None:
        return code
    now = args.now or datetime.now(TZ).isoformat(timespec="seconds")
    asof = args.asof or now[:10]
    try:
        view = build_portfolio(conn, asof)
    finally:
        conn.close()
    _emit(args, view, render_table(view))
    alarm = has_alarm(view)
    if alarm:
        for w in view["warnings"]:
            print(f"⚠️  {w}", file=sys.stderr)
    return 1 if alarm else 0


# ---------- 风险与仓位（P14） ----------

def _risk_conn(args):
    """打开库（只读用途）。与 `_portfolio_conn` 同款守卫，但不需要账本表。"""
    db = Path(args.db) if args.db else paths.DB_PATH
    if not db.exists():
        print(json.dumps({"error": "db not found; run `stocklab db init`"},
                         ensure_ascii=False), file=sys.stderr)
        return None, 2
    return connect(db), None


def _kelly_result(args, conn, code: str) -> dict:
    """回放 `--rule` → 凯利结论（p/b 的唯一合法来源，见 `risk.kelly` docstring）。

    实现在 `stocklab.risk.panel`（P15 从本函数搬出）—— **CLI 与 Web 页面调同一个
    函数**，避免两处各拼一遍口径（那就是第二个真相来源）。
    """
    from stocklab.risk.panel import build_risk_block

    asof = args.asof or datetime.now(TZ).date().isoformat()
    block = build_risk_block(conn, code, asof=asof, rule=args.rule,
                             horizon=args.horizon, frac=args.frac)
    if block["kelly"] is None:
        raise ValueError("；".join(block["errors"]) or "回放不可用")
    return block["kelly"]


def cmd_risk_kelly(args: argparse.Namespace) -> int:
    """凯利仓位建议。**p/b 只来自 PIT 回放 + 扣成本 + 按日聚类 + 报样本量。**

    预期（也是本项目的事实）：方向能力 ≈ 0 → 严格凯利输出 `NO_BET`。
    `NO_BET` 是**结论**，不是错误；退出码 0。
    """
    from stocklab.risk.kelly import render_report
    from stocklab.risk.rules import RULES

    conn, code_ = _risk_conn(args)
    if conn is None:
        return code_
    try:
        out = _kelly_result(args, conn, args.code)
    except ValueError as exc:
        print(json.dumps({"error": str(exc), "rules": list(RULES)},
                         ensure_ascii=False), file=sys.stderr)
        return 2
    finally:
        conn.close()
    _emit(args, out, render_report(out))
    return 0


def cmd_risk_size(args: argparse.Namespace) -> int:
    """仓位裁剪：凯利 f → 具体股数 + 逐条纪律约束。"""
    from stocklab.portfolio.view import build_portfolio
    from stocklab.risk.sizing import render_sizing, size_position

    conn, code_ = _risk_conn(args)
    if conn is None:
        return code_
    now = args.now or datetime.now(TZ).isoformat(timespec="seconds")
    asof = args.asof or now[:10]
    try:
        kelly = _kelly_result(args, conn, args.code)
        view = build_portfolio(conn, asof)
        held = next((p["qty"] for p in view["positions"]
                     if p["code"] == args.code), 0)
        pos = next((p for p in view["positions"] if p["code"] == args.code), None)
        market = (pos or {}).get("price")
        entry = args.entry if args.entry is not None else market
        if entry is None:
            raise ValueError(
                f"{args.code} 没有可用现价，也没有 --entry —— "
                f"没有价格就没有股数（不拿成本价冒充）")
        out = size_position(code=args.code, f_final=kelly["f_final"],
                            total_assets=view["total_assets"], cash=view["cash"],
                            price=float(entry), qty_held=int(held),
                            market_price=market)
    except ValueError as exc:
        print(json.dumps({"error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 2
    finally:
        conn.close()
    out["kelly_verdict"] = kelly["verdict"]
    out["kelly_verdict_label"] = kelly["verdict_label"]
    out["kelly_verdict_reason"] = kelly["verdict_reason"]
    out["kelly_rule"] = kelly["rule"]
    out["asof"] = asof
    _emit(args, out, render_sizing(out))
    return 0


# ---------- 看板 + 本地服务（P14） ----------

def _dashboard_provider(args):
    """返回一个「每次调用重新读库」的摘要提供者。

    **每次请求重算**（不缓存）：看板显示的是账本与行情的当前状态，
    缓存 30 秒就多一个「页面显示的和库里的不一样」的时段，而且没人知道是哪个。
    读库是只读的局部查询，代价远小于一次误读。
    """
    from stocklab.dashboard.summary import build_summary
    from stocklab.risk.panel import build_risk_block

    db = Path(args.db) if args.db else paths.DB_PATH
    fixed_asof = getattr(args, "asof", None)

    def provider() -> dict:
        asof = fixed_asof or datetime.now(TZ).date().isoformat()
        conn = connect(db)
        try:
            # 风险面板挂在**当前持仓里权重最大的那只**上（P14 step4 遗留的并入）。
            # 没有持仓 → risk=None；`build_summary` 的 docstring 说清了
            # `None` 是「未接入」而不是「风险为零」，页面分开渲染这两种情况。
            from stocklab.portfolio.view import build_portfolio
            from stocklab.risk.panel import risk_subject
            view = build_portfolio(conn, asof)
            subject = risk_subject(view)
            risk = (build_risk_block(conn, subject["code"], asof=asof,
                                     price=subject["price"])
                    if subject else None)
            return build_summary(conn, asof, risk_block=risk)
        finally:
            conn.close()

    return db, provider


def cmd_dashboard_build(args: argparse.Namespace) -> int:
    """生成**单文件** HTML 看板（零外部依赖，离线可双击）。"""
    from stocklab.dashboard.html import html_sha256, render_html

    db, provider = _dashboard_provider(args)
    if not db.exists():
        print(json.dumps({"error": "db not found; run `stocklab db init`"},
                         ensure_ascii=False), file=sys.stderr)
        return 2
    summary = provider()
    now = args.now or datetime.now(TZ).isoformat(timespec="seconds")
    doc = render_html(summary, built_at=now)
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(doc, encoding="utf-8")
    payload = {"out": str(out), "bytes": len(doc.encode("utf-8")),
               "sha256": html_sha256(doc), "asof": summary["asof"],
               "alarms": len(summary["alarms"])}
    _emit(args, payload,
          f"✅ 已生成单文件看板 {out}\n"
          f"   字节数 {payload['bytes']:,}   sha256 {payload['sha256'][:16]}…\n"
          f"   asof {payload['asof']}   告警 {payload['alarms']} 条")
    return 0


def cmd_lab_serve(args: argparse.Namespace) -> int:
    """起**唯一的 Web 服务**（查看 + 管理真实持仓）。**只绑回环**。

    2026-09-21 服务合并：这里曾经有两个网页服务（`dashboard serve` 只读快照 /
    `lab serve` 应用），默认端口都是 8791 —— 撞端口、且只能靠记忆区分。
    现在只留这一个：看板变成**离线单文件产物**（`dashboard build`），
    所有网页入口统一在 `/lab/*`（总览 / 模拟盘对照 / 候选池 / 定时任务 / 成交流水 /
    现金流 / 风险 / 数据 / 健康检查）。

    写路径（录入成交 / 冲正 / 录入本金 / 候选池「跑一次」）全程走既有账本函数，
    带 CSRF token 与幂等键。
    """
    from stocklab.labweb import app as labweb
    from stocklab.labweb.cand_data import CandLab
    from stocklab.labweb.ops_data import OpsLab

    try:
        labweb.assert_loopback(args.host)
        base_path = labweb.normalize_base_path(args.base_path)
    except (labweb.NonLoopbackHost, labweb.BadBasePath) as exc:
        print(json.dumps({"error": str(exc), "kind": type(exc).__name__},
                         ensure_ascii=False), file=sys.stderr)
        return 2

    db = Path(args.db) if args.db else paths.DB_PATH
    if not db.exists():
        print(json.dumps({"error": "db not found; run `stocklab db init`"},
                         ensure_ascii=False), file=sys.stderr)
        return 2
    ensure_schema(db)   # lab 应用写入口前滚（P33）
    ctx = labweb.Context(lab=labweb.Lab(db, asof=args.asof),
                         cand=CandLab(db), ops=OpsLab(db),
                         signer=labweb.TokenSigner(labweb.new_secret()),
                         base_path=base_path)
    try:
        ctx.lab.overview()          # 起服务前先算一次：算不出来就别占端口
    except Exception as exc:        # noqa: BLE001（错误必须变成可读的退出码）
        print(json.dumps({"error": f"{type(exc).__name__}: {exc}"},
                         ensure_ascii=False), file=sys.stderr)
        return 2
    try:
        httpd = labweb.make_server(args.host, args.port, ctx=ctx)
    except OSError as exc:
        print(json.dumps({"error": f"绑定 {args.host}:{args.port} 失败：{exc}"},
                         ensure_ascii=False), file=sys.stderr)
        return 1

    host, port = args.host, httpd.server_port
    print(f"✅ 持仓管理应用已启动：{host}:{port}（仅回环；可读可写）")
    print(f"   总览    http://{host}:{port}{base_path}/")
    print(f"   模拟盘  http://{host}:{port}{base_path}/paper")
    print(f"   候选池  http://{host}:{port}{base_path}/candidate")
    print(f"   成交    http://{host}:{port}{base_path}/trades")
    print(f"   健康    http://{host}:{port}{base_path}/health")
    print(f"   库      {db}")
    print("   ⚠️  写操作会**直接改真实账本**（append-only，改错只能冲正）。")
    print("   对外请走 nginx 反代 + Basic Auth；本项目**不常驻**（ADR-001 D-05）。")
    print("   停止：Ctrl-C")
    labweb.serve_forever(httpd)
    return 0


# ---------- 模拟盘（P19） ----------

def _paper_conn(args):
    """打开库并确认 P19 的表已前滚；返回 `(conn, None)` 或 `(None, 退出码)`。"""
    db = Path(args.db) if args.db else paths.DB_PATH
    if not db.exists():
        print(json.dumps({"error": "db not found; run `stocklab db init`"},
                         ensure_ascii=False), file=sys.stderr)
        return None, 2
    ensure_schema(db)   # 写库入口前滚（P33）
    conn = connect(db)
    has = conn.execute(
        "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name IN"
        " ('paper_accounts','paper_trades','paper_nav_daily')").fetchone()[0]
    if has < 3:
        conn.close()
        print(json.dumps({"error": "模拟盘表不存在；先跑 `stocklab db init` 前滚 schema"},
                         ensure_ascii=False), file=sys.stderr)
        return None, 2
    return conn, None


def _paper_fail(exc: Exception) -> int:
    """模拟盘的可预期失败一律**退出码 2 + 原因上 stderr**（不写半截状态）。"""
    print(json.dumps({"error": str(exc), "type": type(exc).__name__},
                     ensure_ascii=False, sort_keys=True), file=sys.stderr)
    return 2


def cmd_paper_init(args: argparse.Namespace) -> int:
    """建三条臂（纪律臂按 3 档 ETF 占比展开）。幂等：已存在不改写。"""
    from stocklab.paper import engine

    conn, code = _paper_conn(args)
    if conn is None:
        return code
    try:
        rep = engine.init_accounts(conn, now=args.now or datetime.now(TZ).isoformat(timespec="seconds"))
    except engine.PaperError as exc:
        return _paper_fail(exc)
    finally_out = conn.close
    try:
        print(json.dumps(rep, ensure_ascii=False, sort_keys=True, indent=2))
        print(json.dumps({"created": rep["created"], "accounts": rep["accounts"],
                          "start_date": rep["start_date"],
                          "initial_nav": rep["initial_nav"]},
                         ensure_ascii=False, sort_keys=True), file=sys.stderr)
    finally:
        finally_out()
    return 0


def cmd_paper_step(args: argparse.Namespace) -> int:
    """推进一天（幂等）。stdout = **稳定 JSON 状态载荷**，报告另落 `reports/paper/`。

    stdout 只由「库里的行 + PIT 收盘价」决定 —— 所以同日重跑逐字节一致；
    「这次有没有真的下单」这类过程信息走 stderr，不进 stdout（否则重跑就不一致了）。
    """
    from stocklab.paper import engine

    conn, code = _paper_conn(args)
    if conn is None:
        return code
    asof = args.asof
    try:
        payload = engine.step(conn, asof, now=args.now or datetime.now(TZ).isoformat(timespec="seconds"))
        rep = engine.build_report(conn, asof)
    except engine.PaperError as exc:
        conn.close()
        return _paper_fail(exc)
    path = Path(args.out) if args.out else (paths.REPORT_DIR / "paper"
                                            / f"{asof}-paper.md")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(engine.render_report(rep), encoding="utf-8")
    # 与 predict/verify/review 同纪律：正文不含生成时刻 → 同输入逐字节一致
    path.with_suffix(".json").write_text(
        json.dumps(rep, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8")
    conn.close()
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2))
    trades = sum(len(a["decisions"]) for a in payload["accounts"])
    print(json.dumps({"asof": asof, "report": str(path),
                      "accounts": len(payload["accounts"]),
                      "trades_on_asof": trades,
                      "note": "本步为幂等操作：同日重跑不重复下单"},
                     ensure_ascii=False, sort_keys=True), file=sys.stderr)
    return 0


def _show_today(args: argparse.Namespace) -> str:
    """`paper show` 的「今天」：`--now` 给了就按它推，否则取系统日。

    时钟是**参数**不是环境（错误日记 #30）—— 否则「今日还没净值」这条分支
    在测试里根本钉不住（真实日期一走到 09-16 就自动变绿）。
    """
    now = getattr(args, "now", None)
    if now:
        try:
            return datetime.fromisoformat(now).date().isoformat()
        except ValueError:
            return now[:10]
    return _today()


def cmd_paper_show(args: argparse.Namespace) -> int:
    """查模拟盘现状（离线只读，不写任何表、不落报告）。

    不带 `--asof` 时 asof 默认取今天；**今天还没有净值行**则回落到最新有净值的
    日期，并在 `asof_source` / `disclosure` 里如实写明「展示的不是今天」——
    直接返回 `accounts: []` 会被读成「模拟盘不存在」。
    """
    from stocklab.paper import engine

    conn, code = _paper_conn(args)
    if conn is None:
        return code
    try:
        payload = engine.show_payload(conn, _show_today(args), requested=args.asof)
    except engine.PaperError as exc:
        conn.close()
        return _paper_fail(exc)
    conn.close()
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2))
    print(json.dumps({"asof": payload["asof"],
                      "asof_source": payload["asof_source"],
                      "latest_nav_date": payload["latest_nav_date"],
                      "agent_n_reviews": payload["agent"]["n_reviews"],
                      "agent_n_trials_total": payload["agent"]["n_trials_total"],
                      "accounts": [{"account_id": a["account_id"], "nav": a["nav"],
                                    "cum_return": a["cum_return"],
                                    "max_or_last_drawdown": a["drawdown"]}
                                   for a in payload["accounts"]]},
                     ensure_ascii=False, sort_keys=True), file=sys.stderr)
    return 0


def cmd_paper_metrics(args: argparse.Namespace) -> int:
    """绩效对比（模块2 §4）：五个指标 + 样本量门禁。**离线只读**。

    数字全部来自 `labweb.paper_data.performance` —— 与 `/lab/paper` 新节是
    **同一个函数**（任务书 T4）：两处各算一遍必然会走样。

    只读：不写任何表、默认不落盘。`--out` 才写文本报告，`--json` 打完整载荷。
    样本不足时只给读数；结论性措辞由渲染层同源消费（门禁在 `sample_gate` 里）。
    """
    from stocklab.labweb import paper_data, paper_render
    from stocklab.paper import engine

    conn, code = _paper_conn(args)
    if conn is None:
        return code
    try:
        asof = engine.resolve_show_asof(conn, _show_today(args),
                                        requested=args.asof)["asof"]
        payload = paper_data.performance(conn, asof)
    finally:
        conn.close()
    text = paper_render.performance_text(payload)
    if args.out:
        path = Path(args.out)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2))
        return 0
    sys.stdout.write(text)
    gate = payload["sample_gate"]
    print(json.dumps({"asof": payload["asof"], "report": args.out,
                      "n_sessions": payload["n_sessions"],
                      "threshold": gate["threshold"],
                      "gate_status": gate["gate_status"],
                      "rows": [{"account_id": r["account_id"],
                                "total_return": r["total_return"],
                                "max_drawdown": r["max_drawdown"]}
                               for r in payload["rows"]]},
                     ensure_ascii=False, sort_keys=True), file=sys.stderr)
    return 0


# ---------- 智能体动态编排臂的 spec（P37） ----------

#: `paper spec` 只许管这两个账户：spec 是**这两个臂**的条文来源。
#: 在别的账户上写 spec 只会产出一张没人读的台账（而且会让人以为规则改了）。
SPEC_ARMS: tuple[str, ...] = (ARM_AGENT, ARM_AGENT_RANDOM)

#: 冲突退出码。与 `_paper_fail` 的「可预期失败」（2）分开：
#: 2 = 你给的东西不合法；1 = 你给的合法，但与库里已有的一版撞了。
EXIT_CONFLICT = 1


def _spec_conflict(arm: str, asof: str, existing: dict, mine: dict) -> int:
    print(json.dumps({
        "error": f"{arm} 在 {asof} 已有一版 spec —— append-only，本命令不改写历史",
        "hint": "要么把 --asof 改成一个还没复审过的交易日，要么先把两版的差异读清楚",
        "existing": {"decision_id": existing["decision_id"],
                     "created_at": existing["created_at"],
                     "agent_kind": existing["agent_kind"],
                     "model_id": existing["model_id"],
                     "n_trials": existing["n_trials"],
                     "spec_after": existing["spec_after"]},
        "attempted": mine,
    }, ensure_ascii=False, sort_keys=True), file=sys.stderr)
    return EXIT_CONFLICT


def cmd_paper_spec_set(args: argparse.Namespace) -> int:
    """写一版 spec（**只增**）：增量合并 → 校验 → 算 PIT 上下文指纹 → 落台账。

    幂等：同 `(arm, asof)` 且合并结果与已有那行**一致** → exit 0、台账仍 1 行。
    不一致 → exit 1，原行一个字节都不改。
    """
    from stocklab.paper import agent_context, agent_spec, store
    from stocklab.paper.config import ARM_AGENT, ARM_AGENT_RANDOM

    conn, code = _paper_conn(args)
    if conn is None:
        return code
    try:
        if args.arm not in SPEC_ARMS:
            raise agent_spec.SpecViolation(
                "arm", args.arm,
                f"`paper spec` 只管 {list(SPEC_ARMS)}（别的账户的条文不来自 spec）")
        accounts = {a["account_id"] for a in store.load_accounts(conn)}
        if args.arm not in accounts:
            raise agent_spec.SpecViolation(
                "arm", args.arm,
                f"账户不存在（现有 {sorted(accounts)}）—— 先跑 `paper init`")
        try:
            patch = json.loads(args.spec)
        except json.JSONDecodeError as exc:
            raise agent_spec.SpecViolation(
                "--spec", args.spec, f"不是合法 JSON（{exc.msg}）") from None
        before = agent_spec.spec_before(conn, args.arm, args.asof)
        after = agent_spec.apply_spec(before, patch)
        rejected = []
        if args.rejected:
            try:
                rejected = json.loads(args.rejected)
            except json.JSONDecodeError as exc:
                raise agent_spec.SpecViolation(
                    "--rejected", args.rejected, f"不是合法 JSON（{exc.msg}）") from None
            if not isinstance(rejected, list):
                raise agent_spec.SpecViolation("--rejected", rejected, "必须是 JSON 数组")
        ctx = agent_context.build_context(conn, arm=args.arm, asof=args.asof)
        context_sha256 = agent_context.context_sha256(ctx)
        existing = agent_spec.decision_on(conn, args.arm, args.asof)
        if existing is not None:
            same = (existing["spec_after"] == after
                    and existing["n_trials"] == args.n_trials
                    and existing["rejected"] == rejected)
            if not same:
                return _spec_conflict(args.arm, args.asof, existing, after)
            print(json.dumps({"arm": args.arm, "asof": args.asof,
                              "decision_id": existing["decision_id"],
                              "status": "已存在且一致（未写入）",
                              "spec": after,
                              "spec_sha256": agent_spec.spec_sha256(after),
                              "context_sha256": existing["context_sha256"]},
                             ensure_ascii=False, sort_keys=True, indent=2))
            return 0
        decision_id = agent_spec.record_decision(
            conn, arm=args.arm, asof=args.asof, spec_before=before, spec_after=after,
            agent_kind=agent_spec.AGENT_KIND_MANUAL,
            model_id=agent_spec.MANUAL_MODEL_ID,
            prompt_sha256=agent_spec.MANUAL_PROMPT_SHA256,
            seed=0, context_sha256=context_sha256,
            now=args.now or datetime.now(TZ).isoformat(timespec="seconds"),
            n_trials=args.n_trials, rejected=rejected,
            rationale=args.rationale or "")
    except (agent_spec.SpecViolation, agent_spec.DecisionConflict) as exc:
        conn.close()
        return _paper_fail(exc)
    conn.close()
    print(json.dumps({"arm": args.arm, "asof": args.asof,
                      "decision_id": decision_id, "status": "已写入",
                      "spec_before": before, "spec": after,
                      "spec_sha256": agent_spec.spec_sha256(after),
                      "n_trials": args.n_trials, "n_rejected": len(rejected),
                      "context_sha256": context_sha256},
                     ensure_ascii=False, sort_keys=True, indent=2))
    return 0


def cmd_paper_spec_show(args: argparse.Namespace) -> int:
    """查某一臂的当前 spec 与台账历史（**直接读库，不重算**）。"""
    from stocklab.paper import agent_spec, store
    from stocklab.paper.config import ARM_AGENT, ARM_AGENT_RANDOM

    conn, code = _paper_conn(args)
    if conn is None:
        return code
    try:
        if args.arm not in SPEC_ARMS:
            raise agent_spec.SpecViolation(
                "arm", args.arm, f"`paper spec` 只管 {list(SPEC_ARMS)}")
        asof = args.asof or _show_today(args)
        accounts = {a["account_id"] for a in store.load_accounts(conn)}
        rows = agent_spec.load_decisions(conn, args.arm)
        payload = {
            "arm": args.arm,
            "asof": asof,
            "account_exists": args.arm in accounts,
            "spec": agent_spec.current_spec(conn, args.arm, asof),
            "spec_sha256": agent_spec.spec_sha256(
                agent_spec.current_spec(conn, args.arm, asof)),
            "change_space": {name: f.range_text()
                             for name, f in agent_spec.SPEC_SCHEMA.items()},
            "default_spec": dict(agent_spec.AGENT_DEFAULT_SPEC),
            "summary": agent_spec.ledger_summary(conn, args.arm, asof),
            "reproducibility": agent_spec.reproducibility(conn, args.arm),
            "n_rows": len(rows),
            "history": rows,
        }
    except agent_spec.SpecViolation as exc:
        conn.close()
        return _paper_fail(exc)
    conn.close()
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2))
    print(json.dumps({"arm": args.arm, "asof": asof,
                      "n_reviews": payload["summary"]["n_reviews"],
                      "n_trials_total": payload["summary"]["n_trials_total"],
                      "current_spec_sha256": payload["spec_sha256"]},
                     ensure_ascii=False, sort_keys=True), file=sys.stderr)
    return 0


# ---------- chain（P26） ----------

def cmd_chain_accuracy(args: argparse.Namespace) -> int:
    """链路准确率视图（离线只读）：四段分列，样本不足就明说。

    默认写 `reports/<today>-chain-accuracy.{json,md}`；正文**不含生成时刻**，
    同输入两次运行逐字节一致（与 `predict` / `review` 报告同纪律）。
    """
    from stocklab.chain.accuracy import build_chain_accuracy, render_markdown

    db = Path(args.db) if args.db else paths.DB_PATH
    if not db.exists():
        print(json.dumps({"error": "db not found; run `stocklab db init`"},
                         ensure_ascii=False), file=sys.stderr)
        return 2
    conn = connect(db)
    try:
        rep = build_chain_accuracy(conn, from_date=args.from_date,
                                   to_date=args.to_date)
    finally:
        conn.close()

    md = render_markdown(rep)
    date = args.date or _today()
    path = Path(args.out) if args.out else (
        paths.REPORT_DIR / f"{date}-chain-accuracy.md")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(md, encoding="utf-8")
    json_path = path.with_suffix(".json")
    json_path.write_text(
        json.dumps(rep, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8")

    if args.json:
        print(json.dumps(rep, ensure_ascii=False, sort_keys=True, indent=2))
        return 0
    segs = {s["segment"]: {"n_days": s["n_days"],
                           "sufficient": s["gate"]["sufficient"],
                           "label": s["gate"]["label"]}
            for s in rep["segments"]}
    print(json.dumps({"report": str(path), "json": str(json_path),
                      "origin_rule": rep["origin_rule"],
                      "provenance_counts": rep["provenance_counts"],
                      "segments": segs},
                     ensure_ascii=False, sort_keys=True, indent=2))
    return 0


# ---------- ops patrol（P38：巡检本体在项目里） ----------

def cmd_ops_patrol(args: argparse.Namespace) -> int:
    """巡检 = 7 项体检（只读）+ 缺哪步补哪步；**退出码由项目定义**（ADR-019）。

    退出码：0 全绿 / 1 链路完整但有异常 / 2 判不了或断链（含拒绝跨库补步）。
    以前这个判定写在 nanobot cron 的任务正文里，于是出现「任务 ok 而巡检报异常」
    两个口径打架（见 `ops/patrol.py` 模块 docstring）。

    stdout = 完整 JSON（供机器读）；stderr = 每条异常一行 + **一行摘要**（供 cron
    直接贴回来，`patrol: exit=... ①ok ②ok ...`）。
    """
    from stocklab.ops import patrol

    payload = patrol.run_patrol(
        db_path=args.db, now=args.now, fix=args.fix,
        timeout_s=args.timeout_seconds, report_dir=args.report_dir)
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2))
    for a in payload.get("anomalies") or []:
        print(f"⚠️  {a['kind']}: {a.get('detail') or a.get('step') or ''}",
              file=sys.stderr)
    print(patrol.summary_line(payload), file=sys.stderr)
    return int(payload["exit_code"])


# ---------- ops close / monthly（P38：收盘链与月度刷新的顺序在项目里） ----------

def _print_ops_payload(payload: dict, summary_fn) -> int:
    """共用的输出形状：stdout = 完整 JSON；stderr = 异常逐条 + 一行摘要。

    与 `ops patrol` 一致（那边已经证明了这是好形状：cron/launchd 侧只需看退出码，
    人排查时 stderr 的一行就够定位）。
    """
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2))
    for a in payload.get("anomalies") or []:
        print(f"⚠️  {a.get('kind')}: {a.get('detail') or a.get('step') or ''}",
              file=sys.stderr)
    for key in ("refused", "error", "skipped", "journal_error"):
        if payload.get(key):
            print(f"⚠️  {key}: {payload[key]}", file=sys.stderr)
    print(summary_fn(payload), file=sys.stderr)
    return int(payload["exit_code"])


def cmd_ops_close(args: argparse.Namespace) -> int:
    """收盘链（ADR-020）：库备份 → 12 步（asof = **运行当天**）→ 事后体检。

    退出码：0 全绿（含非交易日跳过）/ 1 链跑完但有异常 / 2 断链或**还没收盘**。
    收盘前拒绝执行是硬约束：半截 bar 与当日 LIVE 预测都是 append-only。
    """
    from stocklab.ops import chain

    payload = chain.run_close(
        db_path=args.db, now=args.now, timeout_s=args.timeout_seconds,
        report_dir=args.report_dir, backup_dir=args.backup_dir)
    return _print_ops_payload(payload, chain.summary_line)


def cmd_ops_monthly(args: argparse.Namespace) -> int:
    """月度刷新：休市公告 → 长窗行情 → 财报复核 → 体检（每月 1 日 08:00）。"""
    from stocklab.ops import chain

    payload = chain.run_monthly(
        db_path=args.db, now=args.now, timeout_s=args.timeout_seconds,
        report_dir=args.report_dir)
    return _print_ops_payload(payload, chain.summary_line)


def cmd_ops_schedule(args: argparse.Namespace) -> int:
    """调度（launchd）：`generate` 只看不写，`install/uninstall/status/kickstart` 动系统。

    退出码：0 成功 / 1 有一步失败（失败项在 stderr 点名）/ 2 参数不合法。
    `generate` **不碰文件系统**（只看内容，便于先审后装）。
    """
    from stocklab.ops import schedule

    names = args.job or list(schedule.JOB_NAMES)
    unknown = [n for n in names if n not in schedule.JOB_BY_NAME]
    if unknown:
        print(f"未知任务：{'、'.join(unknown)}（可选："
              f"{'、'.join(schedule.JOB_NAMES)}）", file=sys.stderr)
        return 2
    pdir = Path(args.plist_dir) if args.plist_dir else None
    out: list[dict] = []

    if args.schedule_action == "generate":
        for name in names:
            job = schedule.JOB_BY_NAME[name]
            doc = schedule.render_plist(
                job, project_root=args.project_root, python=args.python,
                logs=args.log_dir)
            out.append({"job": name, "label": job.label,
                        "plist_path": str(schedule.plist_path(job, plist_dir=pdir)),
                        "triggers": len(job.calendar), "window": job.window,
                        "why": job.why,
                        "plist": doc.decode("utf-8")})
        print(json.dumps(out, ensure_ascii=False, sort_keys=True, indent=2))
        return 0

    if args.schedule_action == "status":
        out = [schedule.status(schedule.JOB_BY_NAME[n], plist_dir=pdir)
               for n in names]
        print(json.dumps(out, ensure_ascii=False, sort_keys=True, indent=2))
        for r in out:
            mark = "✅" if (r["loaded"] and r["plist_present"]) else "❌"
            print(f"{mark} {r['job']}: loaded={r['loaded']}"
                  f" plist={r['plist_present']} last_exit={r['last_exit_status']}"
                  f"（{r['window']}）", file=sys.stderr)
        return 0 if all(r["loaded"] and r["plist_present"] for r in out) else 1

    if args.schedule_action == "install":
        for name in names:
            out.append(schedule.install(
                schedule.JOB_BY_NAME[name], plist_dir=pdir,
                project_root=args.project_root, python=args.python,
                logs=args.log_dir))
            if args.kickstart and out[-1]["ok"]:
                out[-1]["kickstart"] = schedule.kickstart(
                    schedule.JOB_BY_NAME[name])
    elif args.schedule_action == "uninstall":
        for name in names:
            out.append(schedule.uninstall(schedule.JOB_BY_NAME[name],
                                          plist_dir=pdir))
    else:
        for name in names:
            out.append(schedule.kickstart(schedule.JOB_BY_NAME[name]))

    print(json.dumps(out, ensure_ascii=False, sort_keys=True, indent=2))
    ok = all(r.get("ok") for r in out)
    for r in out:
        state = "✅" if r.get("ok") else "❌"
        note = r.get("stderr") or r.get("detail") or ""
        print(f"{state} {r['job']}: {args.schedule_action} {note}".rstrip(),
              file=sys.stderr)
    return 0 if ok else 1


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

    ing_val = ing_sub.add_parser(
        "valuation", help="采集估值（东财 datacenter RPT_VALUEANALYSIS_DET，PIT 首写保留）")
    ing_val.add_argument("--code", action="append", default=None,
                         help="只采指定 6 位代码，可重复；默认全集")
    ing_val.add_argument("--days", type=int, default=8000,
                         help="回补的日历天数（默认 8000 ≈ 22 年，覆盖上市以来全历史；"
                              "源站决定实际深度）")
    ing_val.add_argument("--start", default=None, help="起始日期 YYYY-MM-DD")
    ing_val.add_argument("--end", default=None, help="结束日期 YYYY-MM-DD")
    ing_val.set_defaults(func=cmd_ingest_valuation)

    ing_mf = ing_sub.add_parser(
        "moneyflow", help="采集资金流（新浪 MoneyFlow，PIT 首写保留）")
    ing_mf.add_argument("--code", action="append", default=None,
                        help="只采指定 6 位代码，可重复；默认全集")
    ing_mf.add_argument("--days", type=int, default=8000,
                        help="回补的日历天数（默认 8000 ≈ 22 年；源站实测可回溯至"
                             " 2013-09-18）")
    ing_mf.add_argument("--start", default=None, help="起始日期 YYYY-MM-DD")
    ing_mf.add_argument("--end", default=None, help="结束日期 YYYY-MM-DD")
    ing_mf.set_defaults(func=cmd_ingest_moneyflow)

    ing_fin = ing_sub.add_parser(
        "financials", help="采集财报（东财 DMSK 数值 + F10 公告日/归母权益）")
    ing_fin.add_argument("--code", action="append", default=None,
                         help="只采指定代码，可重复；默认全部种子标的")
    ing_fin.add_argument("--fetched-date", help="采集日 YYYY-MM-DD（默认今天）")
    ing_fin.add_argument("--db")
    ing_fin.set_defaults(func=cmd_ingest_financials)

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

    bt_wf = bt_sub.add_parser(
        "walkforward", help="walk-forward 折分 + 样本量实证（不产生策略结论）")
    bt_wf.add_argument("--code", action="append", default=None,
                       help="标的（默认全部 active 股票），可重复")
    bt_wf.add_argument("--start", default=None, help="会话轴起始日期")
    bt_wf.add_argument("--end", default=None, help="会话轴结束日期")
    bt_wf.add_argument("--as-of", default=None, help="复权锚点日期（默认=end/今天）")
    bt_wf.add_argument("--train", type=int, default=250,
                       help="训练窗长度（交易日，默认 250 ≈ 1 年）")
    bt_wf.add_argument("--test", type=int, default=21,
                       help="测试窗长度（交易日，默认 21 ≈ 1 月）")
    bt_wf.add_argument("--step", type=int, default=None,
                       help="相邻折步进（默认 = --test，测试窗首尾相接）")
    bt_wf.add_argument("--embargo", type=int, default=5,
                       help="训练窗末尾剔除的交易日数（默认 5 ≈ 1 周，防标签跨切分点泄漏）")
    bt_wf.add_argument("--threshold", type=int, default=120,
                       help="样本外交易日硬门槛（默认 120，见 CLAUDE.md 度量纪律）")
    bt_wf.add_argument("--out", default=None,
                       help="报告输出路径（默认 reports/<日期>-walkforward.md）")
    bt_wf.set_defaults(func=cmd_backtest_walkforward)

    bt_st = bt_sub.add_parser(
        "strategy", help="注册表策略的 walk-forward **样本外**绩效 + 基准对照（离线）")
    bt_st.add_argument("--strategy", default="trend_ma",
                       help="策略 id（必须在 strategy_registry 里注册过；默认 trend_ma）")
    bt_st.add_argument("--code", action="append", default=None,
                       help="标的（默认全部 active 股票），可重复")
    bt_st.add_argument("--start", default=None,
                       help="读取起点（默认各标的的复权链可用下界；有缺口的标的须显式给）")
    bt_st.add_argument("--end", default=None, help="会话轴结束日期")
    bt_st.add_argument("--as-of", default=None, help="复权锚点日期（默认=end/今天）")
    bt_st.add_argument("--train", type=int, default=250,
                       help="训练窗长度（交易日，默认 250 ≈ 1 年；**只用于指标预热**）")
    bt_st.add_argument("--test", type=int, default=21,
                       help="测试窗长度（交易日，默认 21 ≈ 1 月）")
    bt_st.add_argument("--step", type=int, default=None,
                       help="相邻折步进（默认 = --test，测试窗首尾相接）")
    bt_st.add_argument("--embargo", type=int, default=5,
                       help="训练窗末尾剔除的交易日数（默认 5 ≈ 1 周）")
    bt_st.add_argument("--threshold", type=int, default=120,
                       help="样本外交易日硬门槛（默认 120，见 CLAUDE.md 度量纪律）")
    bt_st.add_argument("--cash", type=float, default=1_000_000.0,
                       help="初始本金（默认 100 万）")
    bt_st.add_argument("--benchmark", default="index_300",
                       help="基准名（默认 index_300）")
    bt_st.add_argument("--param", action="append", default=None, metavar="K=V",
                       help="策略参数覆盖（可重复；**不做搜索**，仅供单变量实验）")
    bt_st.add_argument("--out", default=None,
                       help="报告输出路径（默认 reports/<日期>-<策略>-walkforward.md）")
    bt_st.set_defaults(func=cmd_backtest_strategy)

    pred = sub.add_parser("predict", help="预测器（离线；载荷落库 + 可复现 + 可回放）")
    pred_sub = pred.add_subparsers(dest="predict_cmd", required=True)
    pred_run = pred_sub.add_parser(
        "run", help="产出并落库 asof 的预测载荷（PIT：只用 <= asof 的数据）")
    pred_run.add_argument("--asof", required=True,
                          help="预测基准日（必须是日历 ∩ 行情轴上的交易日）")
    pred_run.add_argument("--code", action="append",
                          help="只跑指定标的（可重复）；默认全部在用标的")
    pred_run.add_argument("--report", help="报告输出路径（默认 reports/<today>-predict-<asof>.json）")
    pred_run.add_argument("--report-dir", dest="report_dir", help="报告目录")
    pred_run.add_argument("--db", help="数据库路径（默认 data/stocklab.db）")
    pred_run.set_defaults(func=cmd_predict_run)

    ver = sub.add_parser("verify", help="验证器（离线只读；次日打分 + 归因 + 落库）")
    ver_sub = ver.add_subparsers(dest="verify_cmd", required=True)
    ver_run = ver_sub.add_parser(
        "run", help="给 target_date 的全部预测打分并落库（PIT：只用 <= target_date 的行）")
    ver_run.add_argument("--target-date", dest="target_date", required=True,
                         help="被验证的目标交易日 YYYY-MM-DD")
    ver_run.add_argument("--code", action="append",
                         help="只验证指定标的（可重复）；默认全部")
    ver_run.add_argument("--report", help="报告输出路径（默认 reports/<today>-verify-<target>.json）")
    ver_run.add_argument("--report-dir", dest="report_dir", help="报告目录")
    ver_run.add_argument("--db", help="数据库路径（默认 data/stocklab.db）")
    ver_run.set_defaults(func=cmd_verify_run)

    ver_bf = ver_sub.add_parser(
        "backfill", help="历史回放：逐日 predict + 次日打分，产出准确率报告")
    ver_bf.add_argument("--from", dest="from_date", required=True,
                        help="回放起点（按 **target_date** 计）")
    ver_bf.add_argument("--to", dest="to_date", required=True,
                        help="回放终点（按 **target_date** 计）")
    ver_bf.add_argument("--code", action="append", help="只跑指定标的（可重复）")
    ver_bf.add_argument("--report", help="报告输出路径（默认 reports/<today>-accuracy-<from>-<to>.md）")
    ver_bf.add_argument("--report-dir", dest="report_dir", help="报告目录")
    ver_bf.add_argument("--db", help="数据库路径（默认 data/stocklab.db）")
    ver_bf.set_defaults(func=cmd_verify_backfill)

    ver_pend = ver_sub.add_parser(
        "pending",
        help="补齐「已到期且没有验证行」的预测（幂等；收盘链里插在 predict run 之后）")
    ver_pend.add_argument("--db", help="数据库路径（默认 data/stocklab.db）")
    ver_pend.add_argument("--now", help="覆盖当前时刻（ISO8601；仅供测试/补跑）")
    ver_pend.add_argument("--code", action="append", default=None,
                          help="只处理指定代码，可重复；默认全部")
    ver_pend.set_defaults(func=cmd_verify_pending)

    exp = sub.add_parser(
        "experiment", help="单变量实验（离线；复用 P7 打分口径，**不写任何生产表**）")
    exp_sub = exp.add_subparsers(dest="experiment_cmd", required=True)
    exp_run = exp_sub.add_parser(
        "run", help="跑一个已注册的具名变体，产出样本外实验报告")
    exp_run.add_argument("--variant", required=True,
                         help="变体名（见 stocklab/experiments/variants.VARIANTS）")
    exp_run.add_argument("--from", dest="from_date", required=True,
                         help="回放起点（按 target_date 计）")
    exp_run.add_argument("--to", dest="to_date", required=True,
                         help="回放终点（按 target_date 计）")
    exp_run.add_argument(
        "--selection-split", dest="selection_split", default="validate",
        choices=["train", "validate"],
        help="用哪一段做**选择**依据；默认 validate。刻意**不提供 test** —— "
             "test 是封存段，只能被晋级评审读取一次")
    exp_run.add_argument("--train-ratio", type=float, default=0.60,
                         help="train 段比例（默认 0.60）")
    exp_run.add_argument("--validate-ratio", type=float, default=0.20,
                         help="validate 段比例（默认 0.20）")
    exp_run.add_argument("--test-ratio", type=float, default=0.20,
                         help="test 段比例（默认 0.20）")
    exp_run.add_argument(
        "--keep-test-sealed", dest="keep_test_sealed", action="store_true",
        help="即使 validate 达到 WIN 也不打开封存段 test（多变量比较轮次的"
             "预注册要求）；结论记 inconclusive，是否开 test 留给下一轮。"
             "这是一个只会更保守的开关：它不能强制读 test，只能阻止读")
    exp_run.add_argument("--code", action="append", help="只跑指定标的（可重复）")
    exp_run.add_argument("--report", help="报告输出路径（默认 reports/<today>-exp-<variant>.md）")
    exp_run.add_argument("--report-dir", dest="report_dir", help="报告目录")
    exp_run.add_argument("--db", help="数据库路径（默认 data/stocklab.db）")
    exp_run.set_defaults(func=cmd_experiment_run)

    sess = sub.add_parser("session", help="调度链（P11）：盘中采集 + 收盘回填")
    sess_sub = sess.add_subparsers(dest="session_action")
    sess_tick = sess_sub.add_parser(
        "tick", help="采集快照 + 收盘回填 + 验证到期预测，输出 JSON 摘要（供 cron）")
    sess_tick.add_argument("--db", help="数据库路径（默认 data/stocklab.db）")
    sess_tick.add_argument("--now", help="覆盖当前时刻（ISO8601；仅供测试/补跑）")
    sess_tick.add_argument("--code", action="append", default=None,
                           help="只处理指定代码，可重复；默认库内全部 active 标的")
    sess_tick.add_argument("--window", type=int, default=10,
                           help="最多回看多少个到期日（默认 10）")
    sess_tick.add_argument("--no-capture", action="store_true",
                           help="跳过联网采集，只做回填 + 验证（离线可跑）")
    sess_tick.add_argument("--out", help="把 JSON 摘要另存到这个路径")
    sess_tick.set_defaults(func=cmd_session_tick)

    sess_bf = sess_sub.add_parser(
        "backfill-close", help="手动补跑某交易日的 amount/turnover 回填（离线、幂等）")
    sess_bf.add_argument("--date", required=True, help="交易日 YYYY-MM-DD")
    sess_bf.add_argument("--db", help="数据库路径（默认 data/stocklab.db）")
    sess_bf.add_argument("--now", help="覆盖当前时刻（ISO8601；仅供测试）")
    sess_bf.set_defaults(func=cmd_session_backfill_close)

    review = sub.add_parser("review", help="复盘报告（P11；离线只读）")
    review_sub = review.add_subparsers(dest="review_action")
    rev_daily = review_sub.add_parser(
        "daily", help="生成 reports/<date>-review.md + .json（15:30 复盘）")
    rev_daily.add_argument("--date", help="复盘日期 YYYY-MM-DD（默认今天）")
    rev_daily.add_argument("--db", help="数据库路径（默认 data/stocklab.db）")
    rev_daily.add_argument("--window", type=int, default=30,
                           help="滚动准确率窗口（交易日，默认 30）")
    rev_daily.add_argument("--out", help="markdown 输出路径（.json 同名同目录）")
    rev_daily.set_defaults(func=cmd_review_daily)

    # ---------- 实盘账本 + 组合视图（P12） ----------
    trade = sub.add_parser("trade", help="实盘成交录入（append-only；改错只能冲正）")
    trade_sub = trade.add_subparsers(dest="trade_cmd", required=True)

    tr_add = trade_sub.add_parser("add", help="录入一笔成交")
    tr_add.add_argument("--date", required=True, help="成交日 YYYY-MM-DD（须在日历内且不晚于最近已收盘交易日）")
    tr_add.add_argument("--code", required=True, help="标的代码（须已在 instruments）")
    tr_add.add_argument("--side", required=True, choices=["buy", "sell"])
    tr_add.add_argument("--price", required=True, type=float, help="成交价（元）")
    tr_add.add_argument("--qty", required=True, type=int, help="股数（买入须 100 整数倍）")
    tr_add.add_argument("--fee", type=float, default=0.0, help="费用（元，≥0）")
    tr_add.add_argument("--note")
    tr_add.add_argument("--idempotency-key", help="幂等键：同键第二次进来不重复写入")
    tr_add.add_argument("--allow-duplicate", action="store_true",
                        help="确认「与已有记录完全相同」的是另一笔真单（不加唯一约束的原因）")
    tr_add.add_argument("--db")
    tr_add.add_argument("--now", help="覆盖当前时刻（测试/补录用）")
    tr_add.add_argument("--json", action="store_true")
    tr_add.set_defaults(func=cmd_trade_add)

    tr_rev = trade_sub.add_parser("reverse", help="冲正一笔成交（追加反向记录，不改原行）")
    tr_rev.add_argument("trade_id", type=int)
    tr_rev.add_argument("--reason", required=True, help="冲正原因（写进 note，必填）")
    tr_rev.add_argument("--idempotency-key")
    tr_rev.add_argument("--db")
    tr_rev.add_argument("--now")
    tr_rev.add_argument("--json", action="store_true")
    tr_rev.set_defaults(func=cmd_trade_reverse)

    cash = sub.add_parser("cash", help="本金 / 现金流录入（append-only）")
    cash_sub = cash.add_subparsers(dest="cash_cmd", required=True)
    ca_add = cash_sub.add_parser("add", help="录入一笔现金流（amount 有符号：正=流入）")
    ca_add.add_argument("--date", required=True)
    ca_add.add_argument("--kind", required=True,
                        choices=["deposit", "withdraw", "dividend", "fee", "tax", "other"])
    ca_add.add_argument("--amount", required=True, type=float,
                        help="有符号金额：deposit/dividend > 0，withdraw/fee/tax < 0")
    ca_add.add_argument("--note")
    ca_add.add_argument("--idempotency-key")
    ca_add.add_argument("--allow-duplicate", action="store_true")
    ca_add.add_argument("--db")
    ca_add.add_argument("--now")
    ca_add.add_argument("--json", action="store_true")
    ca_add.set_defaults(func=cmd_cash_add)

    pf = sub.add_parser("portfolio", help="组合视图（离线只读）")
    pf_sub = pf.add_subparsers(dest="portfolio_cmd", required=True)
    pf_show = pf_sub.add_parser(
        "show", help="组合视图；退出码 1 = 要人来看（缺现价或纪律 FAIL）")
    pf_show.add_argument("--asof", help="估值日 YYYY-MM-DD（默认今天）")
    pf_show.add_argument("--db")
    pf_show.add_argument("--now", help="覆盖当前时刻（测试用）")
    pf_show.add_argument("--json", action="store_true", help="输出稳定 JSON（P13 的接口）")
    pf_show.set_defaults(func=cmd_portfolio_show)

    risk = sub.add_parser(
        "risk", help="风险与仓位（P14）：凯利 / 仓位裁剪（p/b 只来自 PIT 回放）")
    risk_sub = risk.add_subparsers(dest="risk_action")

    def _add_risk_common(p, *, entry: bool = False, frac: bool = True):
        p.add_argument("--code", required=True, help="标的代码，如 000333")
        p.add_argument("--rule", default="trend-state",
                       choices=["trend-state", "ma-cross", "buy-hold"],
                       help="PIT 回放规则（p/b 的唯一合法来源）")
        if frac:
            p.add_argument("--frac", type=float, default=0.25,
                           help="分数凯利 k（默认 0.25）")
            p.add_argument("--horizon", type=int, default=5,
                           help="持有视界（交易日，默认 5）")
        if entry:
            p.add_argument("--entry", type=float, default=None,
                           help="拟成交价（默认取组合视图现价）")
        p.add_argument("--asof", help="asof 日期 YYYY-MM-DD（默认今天）")
        p.add_argument("--db", help="数据库路径（默认 data/stocklab.db）")
        p.add_argument("--now", help="当前时刻（默认系统时间）")
        p.add_argument("--json", action="store_true", help="输出机器可读结果")

    rk = risk_sub.add_parser("kelly", help="凯利仓位建议（无 edge 就如实输出 NO_BET）")
    _add_risk_common(rk)
    rk.set_defaults(func=cmd_risk_kelly)

    rs = risk_sub.add_parser("size", help="凯利 f → 具体股数 + 逐条纪律约束")
    _add_risk_common(rs, entry=True)
    rs.set_defaults(func=cmd_risk_size)

    dash = sub.add_parser(
        "dashboard",
        help="单文件看板（离线产物；网页服务统一走 `lab serve`）")
    dash_sub = dash.add_subparsers(dest="dashboard_action")
    dash_build = dash_sub.add_parser(
        "build", help="生成单文件 HTML 看板（零外部依赖，离线可双击）")
    dash_build.add_argument("--out", default="reports/dashboard.html",
                            help="输出路径（默认 reports/dashboard.html）")
    dash_build.add_argument("--asof", help="asof 日期 YYYY-MM-DD（默认今天）")
    dash_build.add_argument("--db", help="数据库路径（默认 data/stocklab.db）")
    dash_build.add_argument("--now", help="页面上的生成时刻（默认当前时间）")
    dash_build.add_argument("--json", action="store_true", help="输出机器可读结果")
    dash_build.set_defaults(func=cmd_dashboard_build)

    lab = sub.add_parser(
        "lab", help="Web 服务（P15，本机**唯一**的网页服务）：查看 + 管理持仓")
    lab_sub = lab.add_subparsers(dest="lab_action")
    lab_serve = lab_sub.add_parser(
        "serve", help="起 Web 应用（**只绑回环**；0.0.0.0 直接报错退出）")
    lab_serve.add_argument("--host", default="127.0.0.1",
                           help="绑定地址（只允许回环；默认 127.0.0.1）")
    lab_serve.add_argument("--port", type=int, default=8791,
                           help="端口（默认 8791；0 = 由内核分配）")
    lab_serve.add_argument("--base-path", default="/lab",
                           help="子路径前缀（默认 /lab；所有链接与表单 action 都带它）")
    lab_serve.add_argument("--db", help="数据库路径（默认 data/stocklab.db）")
    lab_serve.add_argument("--asof", help="固定 asof 日期（默认每次请求取今天）")
    lab_serve.set_defaults(func=cmd_lab_serve)

    paper = sub.add_parser(
        "paper", help="模拟盘（P19）：三臂并行、每日记净值，append-only")
    paper_sub = paper.add_subparsers(dest="paper_action", required=True)

    pp_init = paper_sub.add_parser(
        "init", help="建三条臂（arm-hold / arm-now / 3 档 arm-discipline）；幂等")
    pp_init.add_argument("--db")
    pp_init.add_argument("--now", help="覆盖当前时刻（测试/补录用）")
    pp_init.set_defaults(func=cmd_paper_init)

    pp_step = paper_sub.add_parser(
        "step", help="按 --asof 收盘推进一天（幂等：同日重跑不重复下单）")
    pp_step.add_argument("--asof", required=True, help="决策日 YYYY-MM-DD（PIT 收盘）")
    pp_step.add_argument("--out", help="报告路径（默认 reports/paper/<asof>-paper.md）")
    pp_step.add_argument("--db")
    pp_step.add_argument("--now", help="覆盖当前时刻（测试/补录用）")
    pp_step.set_defaults(func=cmd_paper_step)

    pp_show = paper_sub.add_parser("show", help="查模拟盘现状（离线只读）")
    pp_show.add_argument("--asof", help="asof 日期 YYYY-MM-DD（默认今天）")
    pp_show.add_argument("--db")
    pp_show.add_argument("--now", help="覆盖当前时刻（测试用）")
    pp_show.set_defaults(func=cmd_paper_show)

    pp_metrics = paper_sub.add_parser(
        "metrics", help="绩效对比（模块2 §4）：五指标 + 样本量门禁（离线只读）")
    pp_metrics.add_argument("--asof",
                            help="asof 日期 YYYY-MM-DD（默认今天；今天无净值则回落最新）")
    pp_metrics.add_argument("--db")
    pp_metrics.add_argument("--now", help="覆盖当前时刻（测试用）")
    pp_metrics.add_argument("--json", action="store_true",
                            help="把整份载荷打到 stdout（默认打文本表）")
    pp_metrics.add_argument("--out", help="文本报告落盘路径（默认不落盘）")
    pp_metrics.set_defaults(func=cmd_paper_metrics)

    pp_spec = paper_sub.add_parser(
        "spec", help="智能体动态编排臂（P37）的条文 spec：只增台账，不覆盖")
    pp_spec_sub = pp_spec.add_subparsers(dest="spec_action", required=True)

    pps_set = pp_spec_sub.add_parser(
        "set", help="写一版 spec（增量合并 + 校验 + PIT 上下文指纹；append-only）")
    pps_set.add_argument("--arm", required=True,
                         help="目标臂：arm-agent / arm-agent-random")
    pps_set.add_argument("--asof", required=True,
                         help="复审日 YYYY-MM-DD（= 这一版 spec 的生效起点）")
    pps_set.add_argument("--spec", required=True,
                         help='只写要改的字段，如 \'{"etf_target_pct": 12}\''
                              "（未知字段/越界一律拒绝，不夹紧）")
    pps_set.add_argument("--rationale", help="为什么这么改（写进台账，append-only）")
    pps_set.add_argument("--n-trials", dest="n_trials", type=int, default=1,
                         help="本次复审试了几版（默认 1；上限见 MAX_TRIALS_PER_REVIEW）")
    pps_set.add_argument("--rejected",
                         help="被拒的试错（JSON 数组，与 --n-trials 一起构成预算证据）")
    pps_set.add_argument("--db")
    pps_set.add_argument("--now", help="覆盖当前时刻（测试/补录用）")
    pps_set.set_defaults(func=cmd_paper_spec_set)

    pps_show = pp_spec_sub.add_parser(
        "show", help="查当前 spec / 变更空间 / 试错台账 / 复现性判定（离线只读）")
    pps_show.add_argument("--arm", required=True,
                          help="目标臂：arm-agent / arm-agent-random")
    pps_show.add_argument("--asof", help="asof 日期 YYYY-MM-DD（默认今天）")
    pps_show.add_argument("--db")
    pps_show.add_argument("--now", help="覆盖当前时刻（测试用）")
    pps_show.set_defaults(func=cmd_paper_spec_show)

    chain = sub.add_parser(
        "chain", help="全链路准确率视图（P26）：四段分列，样本不足就明说")
    chain_sub = chain.add_subparsers(dest="chain_action", required=True)
    ch_acc = chain_sub.add_parser(
        "accuracy", help="回放 / 实时 / 模拟盘 / 实盘四段并列（离线只读）")
    ch_acc.add_argument("--db", help="数据库路径（默认 data/stocklab.db）")
    ch_acc.add_argument("--from", dest="from_date",
                        help="起始日 YYYY-MM-DD（默认取库里最早 target_date）")
    ch_acc.add_argument("--to", dest="to_date",
                        help="截止日 YYYY-MM-DD（默认取库里最晚 target_date）")
    ch_acc.add_argument("--date", help="报告文件名里的日期（默认今天）")
    ch_acc.add_argument("--out", help="报告路径（默认 reports/<today>-chain-accuracy.md）")
    ch_acc.add_argument("--json", action="store_true", help="把整份报告打到 stdout")
    ch_acc.set_defaults(func=cmd_chain_accuracy)

    doctor = sub.add_parser("doctor", help="数据健康度报告（离线）")
    doctor.set_defaults(func=lambda _a: cmd_doctor())

    from stocklab.ops.chain import CLOSE_TIMEOUT_S, MONTHLY_TIMEOUT_S
    from stocklab.ops.patrol import DEFAULT_TIMEOUT_S as PATROL_TIMEOUT_S
    from stocklab.ops import schedule

    ops = sub.add_parser(
        "ops", help="运维（P38）：巡检 = 7 项体检 + 缺步补跑，退出码由本命令定义")
    ops_sub = ops.add_subparsers(dest="ops_action", required=True)
    ops_patrol = ops_sub.add_parser(
        "patrol", help="链路体检（**只读**）；--fix 按收盘链顺序补缺步 + 复检")
    ops_patrol.add_argument("--db", help="数据库路径（默认 data/stocklab.db）")
    ops_patrol.add_argument("--now", help="覆盖当前时刻（ISO8601；仅供测试/补跑）")
    ops_patrol.add_argument(
        "--fix", action="store_true",
        help="按收盘链顺序补跑缺失步骤（幂等；非默认库时受跨库守卫拒绝）")
    ops_patrol.add_argument(
        "--timeout-seconds", dest="timeout_seconds", type=float,
        default=PATROL_TIMEOUT_S,
        help=f"整轮预算（默认 {PATROL_TIMEOUT_S:.0f}s；用完即停并记 aborted）")
    ops_patrol.add_argument("--report-dir", dest="report_dir",
                            help="报告根目录（默认 reports/；体检只读：读 <它>/<date>-review.md"
                                 "、回执写 <它>/ops/latest-patrol.json）")
    ops_patrol.set_defaults(func=cmd_ops_patrol)

    ops_close = ops_sub.add_parser(
        "close", help="收盘链（ADR-020）：库备份 → 12 步 → 事后体检；asof = 运行当天")
    ops_close.add_argument("--db", help="数据库路径（默认 data/stocklab.db）")
    ops_close.add_argument("--now", help="覆盖当前时刻（ISO8601；仅供测试/补跑）")
    ops_close.add_argument(
        "--timeout-seconds", dest="timeout_seconds", type=float,
        default=CLOSE_TIMEOUT_S,
        help=f"整轮预算（默认 {CLOSE_TIMEOUT_S:.0f}s；用完即停并记 aborted）")
    ops_close.add_argument("--report-dir", dest="report_dir",
                           help="报告根目录（默认 reports/）：⑤ 复盘报告读 <它>/<date>-review.md，"
                                "回执写 <它>/ops/latest-close.json")
    ops_close.add_argument("--backup-dir", dest="backup_dir",
                           help="库备份目录（默认 data/backups/）")
    ops_close.set_defaults(func=cmd_ops_close)

    ops_monthly = ops_sub.add_parser(
        "monthly", help="月度刷新：休市公告 → 长窗行情 → 财报复核 → 体检")
    ops_monthly.add_argument("--db", help="数据库路径（默认 data/stocklab.db）")
    ops_monthly.add_argument("--now", help="覆盖当前时刻（ISO8601；仅供测试/补跑）")
    ops_monthly.add_argument(
        "--timeout-seconds", dest="timeout_seconds", type=float,
        default=MONTHLY_TIMEOUT_S,
        help=f"整轮预算（默认 {MONTHLY_TIMEOUT_S:.0f}s）")
    ops_monthly.add_argument("--report-dir", dest="report_dir",
                             help="报告根目录（默认 reports/）：⑤ 复盘报告读 <它>/<date>-review.md，"
                                  "回执写 <它>/ops/latest-monthly.json")
    ops_monthly.set_defaults(func=cmd_ops_monthly)

    ops_sched = ops_sub.add_parser(
        "schedule", help="调度（launchd）：plist 的生成 / 安装 / 卸载 / 查看")
    ops_sched_sub = ops_sched.add_subparsers(dest="schedule_action", required=True)
    for _name, _help in (
            ("generate", "只打印将要写入的 plist（不碰文件系统）"),
            ("install", "写 plist 到 ~/Library/LaunchAgents 并 bootstrap"),
            ("uninstall", "bootout 并删掉 plist"),
            ("status", "看三条任务加载了没 / 上次退出码"),
            ("kickstart", "立刻跑一次（安装后的自证）")):
        sp = ops_sched_sub.add_parser(_name, help=_help)
        sp.add_argument("--job", action="append", default=None,
                        help=f"只操作某条任务，可重复（默认全部："
                             f"{'、'.join(schedule.JOB_NAMES)}）")
        sp.add_argument("--plist-dir", dest="plist_dir",
                        help="plist 目录（默认 ~/Library/LaunchAgents）")
        sp.add_argument("--project-root", dest="project_root",
                        help="WorkingDirectory / PYTHONPATH（默认仓库根）")
        sp.add_argument("--python", help="解释器路径（默认当前 sys.executable）")
        sp.add_argument("--log-dir", dest="log_dir",
                        help="stdout/stderr 落盘目录（默认 data/logs/）")
        sp.set_defaults(func=cmd_ops_schedule)
    ops_sched_install = ops_sched_sub.choices["install"]
    ops_sched_install.add_argument(
        "--kickstart", action="store_true",
        help="装完立刻跑一次（巡检可安全自证；收盘链未收盘会拒绝执行）")

    feat = sub.add_parser("features", help="特征层（离线，只读 bars_daily）")
    feat_sub = feat.add_subparsers(dest="feat_action")
    feat_build = feat_sub.add_parser("build", help="为指定日期构建特征快照")
    feat_build.add_argument("--date", required=True, help="asof 日期 YYYY-MM-DD")
    feat_build.add_argument("--code", action="append", default=None,
                            help="只构建指定代码，可重复；默认全部 active 标的")
    feat_build.set_defaults(func=_cmd_features_build)

    from stocklab.trend.evaluate import MIN_DAYS as TREND_MIN_DAYS

    tr = sub.add_parser(
        "trend", help="趋势状态标签（双均线三态）的正式版验证 —— 离线、只读、不写生产表")
    tr_sub = tr.add_subparsers(dest="trend_cmd", required=True)
    tr_eval = tr_sub.add_parser(
        "evaluate",
        help="跑预注册 `trend-state-hit-rate`：M1 命中率 / M2 延续率 + 按日聚类 + "
             "F1/F2/F3 判定；test 段达标才打开")
    tr_eval.add_argument("--from", dest="from_date",
                         help="起点（按**实现日** t+N 计；默认全轴）")
    tr_eval.add_argument("--to", dest="to_date", help="终点（按实现日 t+N 计；默认全轴）")
    tr_eval.add_argument("--min-days", dest="min_days", type=int,
                         default=TREND_MIN_DAYS,
                         help=f"有效交易日门槛（预注册 F3；默认 {TREND_MIN_DAYS}）")
    tr_eval.add_argument("--train-ratio", type=float, default=0.60)
    tr_eval.add_argument("--validate-ratio", type=float, default=0.20)
    tr_eval.add_argument("--test-ratio", type=float, default=0.20)
    tr_eval.add_argument(
        "--keep-test-sealed", dest="keep_test_sealed", action="store_true",
        help="即使 validate 达标也不打开封存段 test（只会更保守：结论记 inconclusive）")
    tr_eval.add_argument(
        "--report", help="报告输出路径（默认 reports/<today>-trend-state-hit-rate.md）")
    tr_eval.add_argument("--report-dir", dest="report_dir", help="报告目录")
    tr_eval.add_argument("--db", help="数据库路径（默认 data/stocklab.db）")
    tr_eval.set_defaults(func=cmd_trend_evaluate)

    cal = sub.add_parser(
        "calendar", help="交易日历（P30）：已公告休市安排（只新增，不动既有子命令）")
    cal_sub = cal.add_subparsers(dest="calendar_cmd", required=True)
    cal_hol = cal_sub.add_parser("holidays", help="已公告的休市安排")
    cal_hol_sub = cal_hol.add_subparsers(dest="holidays_cmd", required=True)
    cal_hol_fetch = cal_hol_sub.add_parser(
        "fetch", help="采上交所休市安排公告并落 market_holidays（联网；fail-closed）")
    cal_hol_fetch.add_argument("--db", help="数据库路径（默认 data/stocklab.db）")
    cal_hol_fetch.add_argument("--max-articles", dest="max_articles", type=int,
                               default=30, help="最多抓几篇公告（默认 30）")
    cal_hol_fetch.set_defaults(func=_cmd_calendar_holidays_fetch)
    cal_hol_show = cal_hol_sub.add_parser("show", help="查已公告休市表（离线只读）")
    cal_hol_show.add_argument("--db", help="数据库路径（默认 data/stocklab.db）")
    cal_hol_show.add_argument("--limit", type=int, default=40,
                              help="列出最后 N 个休市日（默认 40）")
    cal_hol_show.set_defaults(func=_cmd_calendar_holidays_show)

    fixture = sub.add_parser("fixture", help="fixture 管理（离线）")
    fx_sub = fixture.add_subparsers(dest="fixture_action")
    fx_record = fx_sub.add_parser("record", help="把 raw_cache 登记为 fixture")
    fx_record.add_argument("--name", required=True)
    fx_record.add_argument("--source", default="tencent")
    fx_record.add_argument("--key", required=True, help="raw_cache 的 params_key")
    fx_record.add_argument("--note", default=None)
    fx_record.set_defaults(func=_cmd_fixture_record)

    pl = sub.add_parser(
        "plugin", help="插桩脚本管理：提交 / 审核 / 上线 / 查看（人工审核闸门）")
    pl_sub = pl.add_subparsers(dest="plugin_action", required=True)

    pl_submit = pl_sub.add_parser("submit", help="提交一版插桩脚本（预检 + 沙盒）")
    pl_submit.add_argument("file", help="脚本文件路径（.py）")
    pl_submit.add_argument("--plugin-id", required=True,
                           help="插桩编号：0-5（模块1）或 m2_a1/m2_a2/m2_a3/m2_b1"
                                "（模块2，D-33 不占 0-5 编号）")
    pl_submit.add_argument("--version", required=True, help="版本号，如 1.0.0")
    pl_submit.add_argument("--actor", required=True, help="提交人（审计用）")
    pl_submit.add_argument("--note", help="备注")
    pl_submit.add_argument("--now", help="覆盖当前时刻（测试用）")
    pl_submit.add_argument("--db")
    pl_submit.set_defaults(func=cmd_plugin_submit)

    pl_approve = pl_sub.add_parser("approve", help="人工审核通过并上线（会归档旧版）")
    pl_approve.add_argument("script_id")
    pl_approve.add_argument("--actor", required=True)
    pl_approve.add_argument("--reason", required=True, help="为什么批（审计必填）")
    pl_approve.add_argument("--now", help="覆盖当前时刻（测试用）")
    pl_approve.add_argument("--db")
    pl_approve.set_defaults(func=cmd_plugin_approve)

    pl_reject = pl_sub.add_parser("reject", help="人工审核驳回")
    pl_reject.add_argument("script_id")
    pl_reject.add_argument("--actor", required=True)
    pl_reject.add_argument("--reason", required=True)
    pl_reject.add_argument("--now", help="覆盖当前时刻（测试用）")
    pl_reject.add_argument("--db")
    pl_reject.set_defaults(func=cmd_plugin_reject)

    pl_list = pl_sub.add_parser("list", help="查看版本历史与状态（离线只读）")
    pl_list.add_argument("--plugin-id", help="只看某个插桩；不给则全部")
    pl_list.add_argument("--db")
    pl_list.set_defaults(func=cmd_plugin_list)

    pl_sandbox = pl_sub.add_parser(
        "sandbox", help="回放对比：该版本 vs 基线版本（离线只读；未指定 --baseline "
                        "时用现役 active，被比版本就是 active 则退到上一个版本）")
    pl_sandbox.add_argument("script_id")
    pl_sandbox.add_argument("--baseline", default=None,
                            help="基线版本 script_id（默认见上；须与候选同插件）")
    pl_sandbox.add_argument("--pool", default="short",
                            choices=["short", "mid", "long"])
    pl_sandbox.add_argument("--window-start", default="2015-01-01")
    pl_sandbox.add_argument("--window-end", default=None,
                            help="回放窗口结束日期（默认：命令执行当天，YYYY-MM-DD）")
    pl_sandbox.add_argument("--now", help="覆盖当前时刻（测试用）")
    pl_sandbox.add_argument("--db")
    pl_sandbox.set_defaults(func=cmd_plugin_sandbox)

    cand = sub.add_parser(
        "candidate", help="候选池筛选（模块1）：固定主干 + 插桩脚本")
    cand_sub = cand.add_subparsers(dest="candidate_action", required=True)

    cand_run = cand_sub.add_parser(
        "run", help="跑一遍候选池主流程（幂等：同 asof+run_kind 只产出一次）")
    cand_run.add_argument("--asof", required=True, help="截止日 YYYY-MM-DD（PIT）")
    cand_run.add_argument("--run-kind", default="weekly",
                          choices=["light", "weekly", "quarterly"])
    cand_run.add_argument("--out", help="报告路径（默认 reports/candidate/<asof>-<kind>.md）")
    cand_run.add_argument("--now", help="覆盖当前时刻（测试用）")
    cand_run.add_argument("--db")
    cand_run.set_defaults(func=cmd_candidate_run)

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
