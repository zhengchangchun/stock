"""`ops patrol`（P38）：巡检**本体**在项目里，nanobot 只当闹钟（ADR-019）。

## 为什么要有这条命令

2026-09-22 之前，「每 30 分钟巡检」的判定标准（7 项体检、收盘链顺序、什么算异常、
汇报格式）全写在 nanobot cron 的任务正文里。代价实测暴露过两次：

1. **两个状态口径对不上**：nanobot 的任务状态看「这一轮 agent 跑完没」→ `ok`；
   项目命令的退出码是「数据链对不对」→ `1`。当天 10:02 就是「任务 ok 而巡检在
   报异常」；
2. **知识不在版本库里**：任务正文丢了（09-18 网关重置丢过一次）只能从
   `docs/ops/*.md` 手抄回来，且没有任何测试钉住它。

本模块把「体检 + 缺哪步补哪步 + 结论」搬进项目，**退出码由项目定义**：

| 码 | 含义 |
|---|---|
| 0 | 7 项体检全绿（`ok` / `skipped`） |
| 1 | 链路完整但有异常（有 `missing`/`stale` 项，或补步有一步非 0） |
| 2 | **判不了或断链**（最新已收盘交易日判不出 / ④ 判不了 / 库不可读 / 补步致命 / 超时） |

nanobot 侧正文于是只剩一句：跑 `ops patrol --fix`，非 0 就把 stderr 那行贴回来。

## 四条不许越过的线

1. **体检只读**：`check_chain` 走 `file:...?mode=ro`，结构上不可能被体检写坏
   （与 `quality/dbfingerprint.py` 同一把锁）。
2. **补步走子进程、不改主干**：每条补步 = 一条**现成的** CLI 命令（逐字等于 cron
   里原来那行）。本模块不 import 业务函数、不重实现任何规则 —— 07 的「主干固定
   不可修改」在这里是结构性的：它连调用的机会都没有。
3. **判不了就说判不了**：`latest_closed_session` 为 `None`、④ 为 `None` 一律
   退出码 2，**不给 0**（ERROR_DIARY #36：0 会被读成「没问题」）。
4. **`--fix` 不跨库**：采集类命令不接受 `--db`（固定写 `paths.DB_PATH`），所以
   计划里只要含这一类步骤，`--db` 指向非默认库时**拒绝执行**而不是照跑 ——
   否则数据会被写进没打算写的地方。
"""

from __future__ import annotations

import sqlite3
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from stocklab.calendar.holidays import load_holiday_table
from stocklab.config import paths
from stocklab.ops import journal
from stocklab.ops.runner import (DEFAULT_TIMEOUT_S, EXIT_ANOMALY, EXIT_BLOCKED,
                                 EXIT_OK, Step, build_argv, cross_db_refusal,
                                 default_runner, run_steps, worst_code)
from stocklab.predict.version import MODEL_VERSION
from stocklab.session import close as close_mod
from stocklab.session.tick import is_trade_date_closed, load_calendar
from stocklab.verify.pending import latest_closed_session, pending_predictions

TZ = ZoneInfo("Asia/Shanghai")

# ---------- 体检状态（机器可读、测试可断言） ----------

OK = "ok"                # 在位
MISSING = "missing"      # 该有却没有 → 计异常、排补步
STALE = "stale"          # 在位但没跟到最新 → 计异常、排补步
SKIPPED = "skipped"      # 本轮不该要求它（非交易日 / 未 paper init）→ **不计异常**
UNKNOWN = "unknown"      # **判不了**（证据不足）→ 只在点名的地方计异常，见 verdict()

ANOMALY_STATUSES = frozenset({MISSING, STALE})

CHECK_ORDER: tuple[str, ...] = (
    "bars", "snapshot", "predictions", "verifications",
    "review", "paper_nav", "pit_tables",
)

CIRCLED = "①②③④⑤⑥⑦"

CHECK_TITLE = {
    "bars": "① 当日 bars 是否入库",
    "snapshot": "② 当日快照/盘中 tick 是否采到",
    "predictions": "③ 明日预测是否已落库",
    "verifications": "④ 已到期预测是否都有验证行",
    "review": "⑤ 当日 review 报告是否存在",
    "paper_nav": "⑥ 模拟盘净值是否已记",
    "pit_tables": "⑦ 三张 PIT 表是否跟到最新",
}

CALENDAR_TITLE = "日历（`ingest index` 的唯一产物）是否跟到最新"

#: 补步注册表。**顺序 = 收盘链顺序**，`plan()` 的产出按它排序。
STEPS: tuple[Step, ...] = (
    Step("ingest_index", ("ingest", "index"), False,
         "指数日线是交易日历的唯一来源（ADR-001 B4）；日历没跟到 L 时补它"),
    Step("ingest_bars", ("ingest", "bars", "--days", "30"), False,
         "L 的日线缺行 → 采 30 天增量（长窗 12000 天是月度任务的事）"),
    Step("ingest_actions", ("ingest", "actions", "--start", "{action_start}"), False,
         "除权事件 + 复权因子链（回看 400 天覆盖新事件）"),
    Step("ingest_valuation", ("ingest", "valuation", "--days", "30"), False,
         "估值表（东财 datacenter，PIT 首写保留）"),
    Step("ingest_moneyflow", ("ingest", "moneyflow", "--days", "30"), False,
         "资金流表（新浪 MoneyFlow，PIT 首写保留）"),
    Step("session_tick", ("session", "tick"), True,
         "盘中快照（append-only，身份键含源站 ts；非交易日不会多写一行）"),
    Step("session_backfill_close", ("session", "backfill-close", "--date", "{asof}"), True,
         "用当日**收盘后**快照回填 amount/turnover（只补 NULL，取不到就不写）"),
    Step("predict_run", ("predict", "run", "--asof", "{asof}"), True,
         "明日预测落库（PIT：只用 <= asof 的数据）"),
    Step("verify_pending", ("verify", "pending"), True,
         "补齐「已到期却没有验证行」的预测（幂等、append-only）"),
    Step("review_daily", ("review", "daily", "--date", "{asof}"), True,
         "当日复盘报告（离线只读，产出 reports/<L>-review.md）"),
    Step("paper_step", ("paper", "step", "--asof", "{asof}"), True,
         "模拟盘按 asof 收盘推进一天（幂等：同日重跑不重复下单）"),
)

STEP_ORDER: tuple[str, ...] = tuple(s.name for s in STEPS)
STEP_BY_NAME: dict[str, Step] = {s.name: s for s in STEPS}


def _item(status: str, **detail) -> dict:
    return {"status": status, **detail}


def _as_datetime(value: datetime | str) -> datetime:
    dt = value if isinstance(value, datetime) else datetime.fromisoformat(str(value))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=TZ)
    return dt.astimezone(TZ)


def _one(conn: sqlite3.Connection, sql: str, params: tuple = (), default=None):
    """一条查询取第一列；**表不存在**（老库未前滚）→ 返回 `default`，不炸。"""
    try:
        row = conn.execute(sql, params).fetchone()
    except sqlite3.Error:
        return default
    return None if row is None else row[0]


def ro_connect(db_path: Path) -> sqlite3.Connection:
    """**只读**连接（`mode=ro`）。体检路径不许写库 —— 这是结构保证，不是纪律。

    公开给 `ops/chain.py` 用（它只看「今天入库了几行」，同样不许写）。
    """
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def session_day(when: datetime, cal, hol) -> dict:
    """今天是不是交易日 —— 三个证据源，按可信度排序；都不覆盖 → `None`（判不了）。

    刻意**不猜**：`None` 时体检把②报成 `unknown`（不假报缺步），但**仍然**会去采
    一次（采集本身幂等，源站说不是交易日就不会多写一行）。

    公开（不带下划线）：`ops/chain.py` 的收盘链要用**同一个**判定决定是否执行 ——
    两份判定迟早分叉，而分叉的代价是往库写当日半截数据。
    """
    today = when.date().isoformat()
    if when.weekday() >= 5:
        return {"is_trading_day": False, "why": "weekend"}
    if hol is not None and hol.covers(today):
        return {"is_trading_day": not hol.is_closed(today), "why": "market_holidays"}
    if cal.is_open(today):
        return {"is_trading_day": True, "why": "trading_calendar"}
    if cal.all_dates and cal.all_dates[-1] >= today:
        return {"is_trading_day": False, "why": "trading_calendar_closed"}
    return {"is_trading_day": None, "why": "calendar_not_covered"}


# ---------- 体检（**只读**） ----------

def check_chain(conn: sqlite3.Connection, now: str, *, report_dir: Path | str | None = None,
                model_version: str = MODEL_VERSION) -> dict:
    """7 项体检 + 日历项。**本函数一行都不写**（连接由调用方保证只读）。

    返回的 `exit_code` / `anomalies` 是**体检当时的**结论；`--fix` 之后要重跑一次
    再定稿（补步有没有把它修好，只能看补完之后的库）。
    """
    dt = _as_datetime(now)
    today = dt.date().isoformat()
    latest, evidence = latest_closed_session(conn, now)
    cal, cal_error = load_calendar(conn)
    try:
        hol = load_holiday_table(conn)
    except sqlite3.Error:                       # 老库没有 market_holidays
        hol = None
    sday = session_day(dt, cal, hol)

    checks: dict[str, dict] = {}

    # ① 当日 bars -------------------------------------------------------------
    bars_latest = _one(conn, "SELECT MAX(date) FROM bars_daily")
    if latest is None:
        checks["bars"] = _item(UNKNOWN, required=None, latest_date=bars_latest,
                               reason="最新已收盘交易日判不出 → 不知道该有哪一天的行")
    else:
        rows = _one(conn, "SELECT COUNT(*) FROM bars_daily WHERE date=?", (latest,)) or 0
        nulls = _one(conn, "SELECT COUNT(*) FROM bars_daily WHERE date=?"
                           " AND amount IS NULL", (latest,)) or 0
        snaps = _one(conn, "SELECT COUNT(*) FROM quote_snapshots WHERE trade_date=?",
                     (latest,), default=None)
        checks["bars"] = _item(
            OK if rows else MISSING, required=latest, latest_date=bars_latest, rows=rows,
            amount_null_rows=nulls, snapshots_for_date=snaps,
            reason=None if rows else f"{latest} 的日线一行都没有")

    # ② 当日快照 / 盘中 tick ---------------------------------------------------
    try:
        tss = [r[0] for r in conn.execute(
            "SELECT DISTINCT ts FROM quote_snapshots WHERE trade_date=?", (today,))]
        snap_table = True
    except sqlite3.Error:                       # 老库未前滚 P11
        tss, snap_table = [], False
    n_closed = sum(1 for t in tss if close_mod.is_closed_snapshot(t))
    closed_now = is_trade_date_closed(today, now)
    trading = sday["is_trading_day"]
    if trading is False:
        checks["snapshot"] = _item(
            SKIPPED, trade_date=today, rows=len(tss), rows_after_close=n_closed,
            why=sday["why"],
            reason="非交易日 → 本轮不要求采到新快照（07 §非交易日全部跳过，不算异常）")
    elif not snap_table:
        checks["snapshot"] = _item(UNKNOWN, trade_date=today,
                                   reason="quote_snapshots 表不存在（老库未前滚）")
    elif not tss:
        checks["snapshot"] = _item(
            MISSING if trading else UNKNOWN, trade_date=today, rows=0, rows_after_close=0,
            why=sday["why"],
            reason=("今天还没有任何快照" if trading else
                    "日历与休市表都没覆盖今天 → 仍会去采一次（幂等），但不假报为缺步"))
    elif closed_now and n_closed == 0:
        checks["snapshot"] = _item(
            MISSING, trade_date=today, rows=len(tss), rows_after_close=0,
            reason="已过收盘时刻，但今天还没有一条**收盘后**（ts ≥ 15:00）的快照")
    else:
        checks["snapshot"] = _item(OK, trade_date=today, rows=len(tss),
                                   rows_after_close=n_closed)

    # ③ 明日预测 -------------------------------------------------------------
    if latest is None:
        checks["predictions"] = _item(UNKNOWN, required=None, model_version=model_version,
                                      reason="最新已收盘交易日判不出")
    else:
        n = _one(conn, "SELECT COUNT(*) FROM predictions WHERE asof_date=?"
                       " AND model_version=?", (latest, model_version)) or 0
        tgt = _one(conn, "SELECT MAX(target_date) FROM predictions WHERE asof_date=?"
                         " AND model_version=?", (latest, model_version))
        checks["predictions"] = _item(
            OK if n else MISSING, asof=latest, model_version=model_version, n=n,
            target_date=tgt,
            reason=None if n else f"asof={latest} 的 {model_version} 预测一行都没有")

    # ④ 已到期预测的验证行 -----------------------------------------------------
    pend = pending_predictions(conn, now)
    if pend["n"] is None:
        checks["verifications"] = _item(UNKNOWN, n=None, reason=pend["reason"],
                                        note=pend["note"], evidence=pend["evidence"])
    elif pend["n"] == 0:
        checks["verifications"] = _item(OK, n=0, by_target_date={})
    else:
        checks["verifications"] = _item(MISSING, n=pend["n"],
                                        by_target_date=pend["by_target_date"],
                                        hint=pend.get("hint"),
                                        reason=f"{pend['n']} 条已到期预测没有验证行")

    # ⑤ 当日 review 报告 -------------------------------------------------------
    rdir = Path(report_dir) if report_dir else paths.REPORT_DIR
    rpath = (rdir / f"{latest}-review.md") if latest else None
    exists = bool(rpath and rpath.exists())
    checks["review"] = _item(
        (OK if exists else MISSING) if latest else UNKNOWN,
        required=latest, path=(str(rpath) if rpath else None),
        reason=None if exists else (f"{rpath} 不存在" if rpath else "最新已收盘交易日判不出"))

    # ⑥ 模拟盘净值 -------------------------------------------------------------
    accounts = _one(conn, "SELECT COUNT(*) FROM paper_accounts", default=None)
    if not accounts:
        checks["paper_nav"] = _item(
            SKIPPED, accounts=accounts,
            reason="未 paper init（或老库无该表）→ 跳过，不算异常")
    else:
        nav_max = _one(conn, "SELECT MAX(date) FROM paper_nav_daily", default=None)
        rows_nav = (_one(conn, "SELECT COUNT(*) FROM paper_nav_daily WHERE date=?",
                         (latest,), default=None) if latest else 0) or 0
        checks["paper_nav"] = _item(
            OK if rows_nav else MISSING, accounts=accounts, required=latest,
            latest_date=nav_max, rows_for_required=rows_nav,
            reason=None if rows_nav else f"{latest} 没有净值行")

    # ⑦ 三张 PIT 表 ------------------------------------------------------------
    val = _one(conn, "SELECT MAX(date) FROM valuation_daily", default=None)
    mf = _one(conn, "SELECT MAX(date) FROM money_flow_daily", default=None)
    adj = _one(conn, "SELECT COUNT(*) FROM adj_factors", default=None)
    if latest is None:
        checks["pit_tables"] = _item(UNKNOWN, valuation_max=val, money_flow_max=mf,
                                     adj_factors_rows=adj, required=None,
                                     reason="最新已收盘交易日判不出")
    else:
        bad: list[str] = []
        if val is None or val < latest:
            bad.append(f"valuation_daily max={val}")
        if mf is None or mf < latest:
            bad.append(f"money_flow_daily max={mf}")
        if not adj:
            bad.append(f"adj_factors={adj} 行")
        checks["pit_tables"] = _item(
            STALE if bad else OK, valuation_max=val, money_flow_max=mf,
            adj_factors_rows=adj, required=latest, reason="；".join(bad) or None)

    # 日历经（第 8 项）：它不在 cron 点名的 7 项里，但它是「`ingest index` 跑了没」
    # 的**唯一**可观测量（日历由指数日线生成），缺了它就找不到该补哪一步。
    cal_max = max(cal.all_dates) if cal.all_dates else None
    if latest is None:
        calendar = _item(UNKNOWN, max_date=cal_max, required=None, error=cal_error)
    else:
        ok_cal = bool(cal_max and cal_max >= latest)
        calendar = _item(OK if ok_cal else STALE, max_date=cal_max, required=latest,
                         error=cal_error,
                         reason=None if ok_cal else
                         "日历没跟到最新已收盘交易日 —— `ingest index` 是它唯一的来源")

    out = {
        "now": now,
        "today": today,
        "latest_closed_session": {"date": latest, "evidence": evidence},
        "session_day": sday,
        "calendar": calendar,
        "checks": {name: checks[name] for name in CHECK_ORDER},
        "titles": {**CHECK_TITLE, "calendar": CALENDAR_TITLE},
    }
    v = verdict(out)
    out["verdict"] = v
    out["anomalies"] = _anomalies(out, v)
    out["exit_code"] = v["exit_code"]
    out["ok"] = v["exit_code"] == EXIT_OK
    return out


def check_db(db_path: Path | str, now: str, *,
             report_dir: Path | str | None = None) -> dict:
    """`check_chain` 的只读入口（自开自关连接）。"""
    conn = ro_connect(Path(db_path))
    try:
        return check_chain(conn, now, report_dir=report_dir)
    finally:
        conn.close()


def verdict(snap: dict) -> dict:
    """体检结果 → 退出码 + 理由（**纯函数**）。

    `unknown` 的待遇是刻意分开的：

    - **④ 判不了**（日历与行情两侧都没证据）→ 2：这是「不知道有没有缺口」，
      与「没有缺口」是两句不同的话，不许给 0（ERROR_DIARY #36）；
    - **② 判不了**（不知道今天是不是交易日）→ **不**计异常：它只说明「这几天
      日历没覆盖」，补步仍然会去采一次；把它当异常会变成每天都报的假警。
    """
    latest = (snap.get("latest_closed_session") or {}).get("date")
    if latest is None:
        return {"exit_code": EXIT_BLOCKED,
                "reasons": ["最新已收盘交易日判不出（日历与行情两侧都没有证据）→ 不猜"]}
    checks = snap["checks"]
    if checks["verifications"]["status"] == UNKNOWN:
        return {"exit_code": EXIT_BLOCKED,
                "reasons": ["④ 判不了（fail-closed：不把「判不了」读成「没有缺口」）"]}
    bad = [f"{CHECK_TITLE[name]}={checks[name]['status']}"
           for name in CHECK_ORDER if checks[name]["status"] in ANOMALY_STATUSES]
    if snap.get("calendar", {}).get("status") in ANOMALY_STATUSES:
        bad.append(f"{CALENDAR_TITLE}={snap['calendar']['status']}")
    if bad:
        return {"exit_code": EXIT_ANOMALY, "reasons": bad}
    return {"exit_code": EXIT_OK, "reasons": []}


def _anomalies(snap: dict, v: dict) -> list[dict]:
    out: list[dict] = []
    if v["exit_code"] == EXIT_BLOCKED:
        out.append({"kind": "undetermined" if
                    (snap["latest_closed_session"] or {}).get("date") else "no_closed_session",
                    "detail": "；".join(v["reasons"])})
        return out
    checks = snap["checks"]
    for name in CHECK_ORDER:
        item = checks[name]
        if item["status"] in ANOMALY_STATUSES:
            out.append({"kind": f"check_{name}", "item": CHECK_TITLE[name],
                        "status": item["status"],
                        "detail": item.get("reason") or item["status"]})
    cal = snap.get("calendar") or {}
    if cal.get("status") in ANOMALY_STATUSES:
        out.append({"kind": "check_calendar", "item": CALENDAR_TITLE,
                    "status": cal["status"],
                    "detail": cal.get("reason") or cal["status"]})
    return out


# ---------- 缺步补跑（计划 → 子进程） ----------

def plan(snap: dict) -> dict:
    """体检结果 → 该补哪几步（**纯函数**：不碰库、不读时钟）。

    判定**逐项对应一个体检项**，没有第二条隐藏规则：某个项不在位，就排上能把它拉回
    来的那条命令。唯一的「连带」是 `bars` 缺行时一并排上 `session_backfill_close`
    —— 没有 bar 行时连「amount 是不是 NULL」都查不出来，而回填本来就只在 bar 到位
    后才有意义（先后由链序保证）。

    刻意**不**写成「从第一步一路跑到最后」：那样每天都白跑七条命令，而白跑正是本
    仓库反复强调要避免的（07 §调度约束：幂等，但白跑）。

    ## 一条**不许补**的缺口：`predict run --asof 今天`（P46 §T2）

    ③ 缺预测时**不能无条件**排 `predict_run`。当 `latest_closed_session` 就是今天时，
    当天的 K 线还没定型（`ingest bars` 是 15:30 收盘链的第一步），此刻补出来的预测
    基于**半截 bar**，而 `predictions` 是 append-only —— 15:30 收盘链把当日 K 线覆盖成
    真收盘价后重算，必然条条冲突（2026-09-22 实测 17 条，收盘链 exit 1，当天预测永久
    停在收盘前口径，见 ERROR_DIARY #60）。

    ⇒ **盘中归 patrol，收盘后归 close**。今天 asof 的这一步进 `skipped` 而不是
    `steps`，并**写明原因**（非静默：读者要能从回执里看出「有人故意没补」，
    而不是把 ③ missing 读成「patrol 忘了」）。

    只跳过**这一步**：昨天的缺口照旧补（那时 K 线已定型），补步功能整体不受影响。
    """
    latest = (snap.get("latest_closed_session") or {}).get("date")
    if latest is None:
        return {"steps": [], "skipped": [], "exit_code": EXIT_BLOCKED,
                "reasons": ["最新已收盘交易日判不出 → 不猜、不补"]}
    today = snap.get("today")
    checks = snap["checks"]
    wanted: list[str] = []
    skipped: list[dict] = []
    reasons: list[str] = []

    bars = checks["bars"]
    if bars["status"] != OK:
        wanted += ["ingest_bars", "session_backfill_close"]
        reasons.append(f"① {bars.get('reason') or '日线缺行'} → 采 30 天增量；回填一并排上"
                       "（没有 bar 行时连 amount 是否 NULL 都查不出来，链序保证 bars 在前）")
    elif bars.get("amount_null_rows") and bars.get("snapshots_for_date"):
        wanted.append("session_backfill_close")
        # 措辞按**实测**（P46 §9.4 记下、P51 T2 修）：
        # ① amount 为 NULL 的那几行是**当日没有快照**的标的（快照只覆盖了一部分标的），
        #    不是「有快照却没取到量」；旧文案把这两件事说成了一件事。
        # ② `snapshots_for_date` 是 `COUNT(*)`（**行**，一个标的一天可能多个时点），
        #    实测真库 2026-09-22 = 68 行 / 17 个标的 —— 旧文案写成「N 个标的」也是错的。
        # 判定逻辑不动：单变量原则 —— 这条是文案 bug，不是判据 bug（不新增查询）。
        reasons.append(f"① {latest} 已有 {bars['snapshots_for_date']} 行当日快照"
                       f"（时点见 ②），另有 {bars['amount_null_rows']} 行 amount/turnover"
                       " 为 NULL —— 那几行正是**没有当日快照**的标的（快照只覆盖了部分"
                       "标的）→ 回填补上")

    if snap.get("calendar", {}).get("status") == STALE:
        wanted.append("ingest_index")
        reasons.append("日历没跟到最新已收盘交易日（`ingest index` 是它唯一的来源）")

    if checks["pit_tables"]["status"] == STALE:
        wanted += ["ingest_actions", "ingest_valuation", "ingest_moneyflow"]
        reasons.append(f"⑦ {checks['pit_tables'].get('reason')}")

    if checks["snapshot"]["status"] in (MISSING, UNKNOWN):
        wanted.append("session_tick")
        reasons.append(f"② {checks['snapshot'].get('reason') or checks['snapshot']['status']}")

    if checks["predictions"]["status"] == MISSING:
        if today is not None and latest == today:
            skipped.append({
                "step": "predict_run",
                "reason": (f"asof={latest} 就是**今天**：当天的 K 线要到收盘链的 "
                           "`ingest bars` 才定型，此刻补 `predict run` 会把半截 bar "
                           "算出的预测写进 append-only 的 predictions —— 这条归 "
                           "`close`（15:30），patrol 不碰（P46 §T2）"),
            })
        else:
            wanted.append("predict_run")
            reasons.append(f"③ {checks['predictions'].get('reason')}")

    if checks["verifications"]["status"] == MISSING:
        wanted.append("verify_pending")
        reasons.append(f"④ {checks['verifications'].get('reason')} → `verify pending`")

    if checks["review"]["status"] == MISSING:
        wanted.append("review_daily")
        reasons.append(f"⑤ {checks['review'].get('reason')}")

    if checks["paper_nav"]["status"] == MISSING:
        wanted.append("paper_step")
        reasons.append(f"⑥ {checks['paper_nav'].get('reason')}")

    picked = [name for name in STEP_ORDER if name in set(wanted)]
    return {"steps": picked, "skipped": skipped,
            "exit_code": verdict(snap)["exit_code"], "reasons": reasons}


def _fix_refusal(db_path: Path, steps: list[str]) -> str | None:
    """`--fix` 的跨库守卫（模块 docstring 第 4 条）。

    规则与收盘链**共用一份**（`runner.cross_db_refusal`）—— 两边各写一份，迟早有
    一边忘了改。
    """
    return cross_db_refusal(db_path, steps, STEP_BY_NAME, what="--fix")


def _seal(payload: dict, *, db: Path, stamp: str,
          report_dir: Path | str | None) -> dict:
    """写回执（`reports/ops/latest-patrol.json` + `job_runs` 一行）。

    只在**跑过体检**之后调：库不存在时不写（那条路连库都没建，回执本身
    会先把库文件造出来——“库不存在”就不该留下痕迹）。
    """
    return journal.seal(payload, db_path=db, job_name="patrol", stamp=stamp,
                        detail=summary_line(payload), report_dir=report_dir)


def run_patrol(*, db_path: Path | str | None = None, now: str | None = None,
               fix: bool = False, runner=None,
               timeout_s: float = DEFAULT_TIMEOUT_S,
               report_dir: Path | str | None = None) -> dict:
    """一轮巡检 = 体检 → 计划 → （可选）补步 → **复检** → 定稿退出码。

    复检不能省：补步有没有把它修好，只能看**补完之后**的库。
    退出码取「复检结论」与「补步结果」的**较大者**（2 > 1 > 0，越大越严重）。

    `runner` 是注入点（测试用假执行器，绝不真起子进程）。
    """
    db = Path(db_path) if db_path else paths.DB_PATH
    stamp = now or datetime.now(TZ).isoformat(timespec="seconds")
    runner = runner or default_runner
    payload: dict = {"job": "patrol", "now": stamp, "db": str(db), "fix": bool(fix)}
    if not db.exists():
        payload.update({
            "error": "db not found; run `stocklab db init`",
            "checks": {},
            "anomalies": [{"kind": "db_missing", "detail": f"{db} 不存在"}],
            "verdict": {"exit_code": EXIT_BLOCKED, "reasons": ["库不存在"]},
            "exit_code": EXIT_BLOCKED, "ok": False,
        })
        return payload

    before = check_db(db, stamp, report_dir=report_dir)
    payload.update(before)
    p = plan(before)
    payload["plan"] = p

    steps_out: list[dict] = []
    aborted: dict | None = None
    if fix and p["steps"]:
        refusal = _fix_refusal(db, p["steps"])
        if refusal:
            payload.update({
                "steps": [], "refused": refusal,
                "anomalies": list(before["anomalies"]) + [
                    {"kind": "fix_refused", "detail": refusal}],
                "exit_code": EXIT_BLOCKED, "ok": False,
            })
            return _seal(payload, db=db, stamp=stamp, report_dir=report_dir)
        steps_out, aborted = run_steps(
            p["steps"], registry=STEP_BY_NAME, runner=runner, db_path=db,
            asof=before["latest_closed_session"]["date"], now=stamp,
            timeout_s=timeout_s)

    after = check_db(db, stamp, report_dir=report_dir) if steps_out else before
    final = max(after["exit_code"], worst_code(steps_out, aborted))

    anomalies = list(after["anomalies"])
    for s in steps_out:
        if s.get("exit_code") != 0:
            anomalies.append({
                "kind": ("step_failed" if (s.get("exit_code") is None or
                                           s["exit_code"] >= EXIT_BLOCKED)
                         else "step_anomaly"),
                "step": s["name"], "exit_code": s.get("exit_code"),
                "detail": (s.get("stderr_tail") or s.get("stdout_tail")
                           or s.get("error") or "")[-200:],
            })

    payload.update({
        "latest_closed_session": after["latest_closed_session"],
        "session_day": after["session_day"],
        "calendar": after["calendar"],
        "checks": after["checks"],
        "titles": after["titles"],
        "verdict": after["verdict"],
        "steps": steps_out,
        "after_exit_code": after["exit_code"],
        "anomalies": anomalies,
        "exit_code": final,
        "ok": final == EXIT_OK,
    })
    if aborted:
        payload["aborted"] = aborted
    return _seal(payload, db=db, stamp=stamp, report_dir=report_dir)


def summary_line(payload: dict) -> str:
    """**一行**给 cron 用（体检 7 项各自状态 + 补了哪几步 + 跳过了哪几步 + 异常数）。

    `skip=` 是刻意露出来的：③ 缺预测而 patrol 故意不补时，摘要行必须让 launchd 日志
    与 `/lab/ops` 同时看得见「有人没补」，否则读者只能看到 `③missing` 而误以为漏跑
    （ERROR_DIARY #54：不许让「被跳过」在退出码/摘要上长得像「跑完了都成功」）。
    """
    checks = payload.get("checks") or {}
    if not checks:
        return (f"patrol: exit={payload.get('exit_code')} 未体检（"
                f"{payload.get('error') or payload.get('refused') or '?'}）")
    marks = " ".join(f"{CIRCLED[i]}{checks[name]['status']}"
                     for i, name in enumerate(CHECK_ORDER) if name in checks)
    fixed = ",".join(s["name"] for s in (payload.get("steps") or [])) or "-"
    skip = ",".join(s["step"] for s in ((payload.get("plan") or {}).get("skipped")
                                        or []))
    latest = (payload.get("latest_closed_session") or {}).get("date")
    return (f"patrol: exit={payload['exit_code']} latest_closed={latest}"
            f" calendar={(payload.get('calendar') or {}).get('status')}"
            f" {marks} fixed={fixed}"
            + (f" skip={skip}" if skip else "")
            + f" anomalies={len(payload.get('anomalies') or [])}")
