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
import hashlib
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
    2 非交易日 / 没有任何可出预测的标的。
    """
    from stocklab.predict.service import NotASession, build_predictions
    from stocklab.predict.store import PredictionConflict, insert_prediction

    db = Path(args.db) if args.db else paths.DB_PATH
    if not db.exists():
        print(json.dumps({"error": "db not found; run `stocklab db init`"},
                         ensure_ascii=False), file=sys.stderr)
        return 2
    report_dir = Path(args.report_dir) if args.report_dir else paths.REPORT_DIR
    report_dir.mkdir(parents=True, exist_ok=True)
    now = datetime.now(TZ).isoformat(timespec="seconds")

    conn = connect(db)
    try:
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
                state, pred_id = insert_prediction(conn, p, now=now)
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
