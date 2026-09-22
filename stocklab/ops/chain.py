"""`ops close` / `ops monthly`（P38）：把定时任务的**执行顺序**也搬进项目。

## 为什么要搬（还有一次真出过的事故）

2026-09-22 用户拍板：调度不用 nanobot cron —— **时钟交给系统**（launchd，见
`ops/schedule.py`），「什么时候跑」由 plist 定义，「跑什么、按什么顺序、什么算失败」
由本模块定义。理由与 ADR-019 同源：写在 agent 任务正文里的知识改不了一次、测不了、
丢了只能手抄（09-18 网关重置丢过一次）。

**事故**：旧正文在跑 ingest **之前**就算 `LATEST = max(bars_daily.date)`，而 15:30 那一刻
库里最新的一行还是**上一个交易日** —— 于是 `session backfill-close` / `predict run` /
`review daily` / `paper step --asof $LATEST` 全部在补昨天。幂等，所以不报错、不留痕，
只是「今天的链看起来跑过了」。本模块的口径改成 **`asof` = 运行当天**（收盘日），
收盘前**拒绝执行**（exit 2）—— 半截 bar 与当日 LIVE 预测都是 append-only，写错了
退不回来。

## 三条判据（都有测试钉住）

1. **非交易日整轮跳过**：判定复用巡检那一份（`patrol.session_day`：周末 → 休市表 →
   交易日历），`False` 就写一行 `job_runs(skipped)` 并 exit 0 —— 与 07 §非交易日全部
   跳过一致；
2. **收盘前拒绝执行**：`is_trade_date_closed(今天, now)` 为假 → exit 2，**一步都不跑**
   （连备份都不做：备份会覆盖 `data/backups/` 里当天的文件）；
3. **跑完再体检一次**：退出码取「链本身」与「事后体检（`patrol.check_db`）」的较大者
   —— 链跑完了但数据不健康（`missing`/`stale`）同样是红的。这一条让 cron 侧只剩
   「非 0 就贴回来看」。

### 判据说「不知道」时照跑（不是漏判）

`session_day()` 在「休市表没覆盖今年、日历最大日 < 今天」时返回 `None`（判不了），
本模块**只在明确 `False` 时跳过**。这是刻意选的：正常交易日在 15:30 那一刻，日历里
**还没有今天**（`ingest index` 就是本轮的第一步），所以 `None` 是**每个交易日的常态**
—— 拿它当闸门等于这条链永远不跑。「今天休市」的可靠证据只有两种：周末，或休市表
（`market_holidays`，ADR-013）覆盖到的节假日。

## `report_dir` 只回答一件事

它 = **报告根目录**（默认 `paths.REPORT_DIR`）：⑤ 复盘报告读 `<根>/<date>-review.md`，
回执写 `<根>/ops/latest-<job>.json`。两处用同一个根，才有「回执里说缺的 ⑤，就是页面上
那一份报告」这种一致性；一个参数两个含义迟早让两边各看一份文件。

## 与巡检的分工

| | 问题 | 触发 |
|---|---|---|
| `ops patrol --fix` | 「**现在**缺哪一步」→ 补哪一步（只补缺口） | 每 30 分钟 |
| `ops close` | 「今天这一天**整条链**跑一遍」→ 顺序固定、asof 固定 | 每交易日 15:30 |

两者共用 `ops/runner.py`（同一个执行器、同三条停止线、同一份跨库守卫）。
"""

from __future__ import annotations

import sqlite3
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from stocklab.config import paths
from stocklab.ops import journal
from stocklab.ops.patrol import check_db, ro_connect, session_day
from stocklab.ops.runner import (EXIT_ANOMALY, EXIT_BLOCKED, EXIT_OK, Step,
                                 cross_db_refusal, default_runner, run_steps,
                                 worst_code)
from stocklab.session.tick import is_trade_date_closed
from stocklab.store.migrate import backup_db

TZ = ZoneInfo("Asia/Shanghai")

#: 收盘链整轮预算：实测全量约 90 s（ingest 段占大头），给 15 分钟余量。
CLOSE_TIMEOUT_S = 900.0

#: 月度刷新整轮预算：长窗 `ingest bars --days 12000` 实测 22 s，但它是联网长跑。
MONTHLY_TIMEOUT_S = 1800.0

#: 关闭链步骤（**顺序 = 依赖顺序**，一个都不能少）。
#:
#: `supports_db` 为 `False` 的三类是「不接受 `--db`」的命令：采集（固定写
#: `paths.DB_PATH`）与 `doctor`（它**不接受任何参数**）。它们出现在计划里就意味着
#: 这条链只能在默认库上跑 —— 由 `runner.cross_db_refusal` 拒绝执行，不是靠自觉。
CLOSE_STEPS: tuple[Step, ...] = (
    Step("ingest_index", ("ingest", "index"), False,
         "基准 sh000300 日线，并**顺带前滚交易日历**（日历是指数日线唯一的产物）"),
    Step("ingest_bars", ("ingest", "bars", "--days", "30"), False,
         "21 只标的的日 K 增量（长窗 12000 天是月度刷新的事：日链会撞源站脏行）"),
    Step("ingest_actions", ("ingest", "actions", "--start", "{action_start}"), False,
         "除权事件 + 复权因子链（回看 400 天覆盖新事件；复权口径是预测的输入）"),
    Step("ingest_valuation", ("ingest", "valuation", "--days", "30"), False,
         "PIT 估值表（东财 datacenter；首写保留，增量口径）"),
    Step("ingest_moneyflow", ("ingest", "moneyflow", "--days", "30"), False,
         "资金流表（新浪 MoneyFlow；首写保留，增量口径）"),
    Step("session_tick", ("session", "tick"), True,
         "采一次盘中/收盘快照（append-only；盘中跑也就是多几个时点）"),
    Step("session_backfill_close", ("session", "backfill-close", "--date", "{asof}"),
         True,
         "用**当日收盘后**快照回填 amount/turnover（只补 NULL，取不到就不写）"),
    Step("predict_run", ("predict", "run", "--asof", "{asof}"), True,
         "明日预测落库（PIT：只用 <= asof 的数据；asof = 运行当天，不是库里最新那行）"),
    Step("verify_pending", ("verify", "pending"), True,
         "补齐「已到期却没有验证行」的预测（必须紧跟 predict run）"),
    Step("review_daily", ("review", "daily", "--date", "{asof}"), True,
         "当日复盘报告（离线只读，产出 reports/<asof>-review.md）"),
    Step("paper_step", ("paper", "step", "--asof", "{asof}"), True,
         "模拟盘按 asof 收盘推进一天（幂等：同日重跑不重复下单）"),
    Step("doctor", ("doctor",), False,
         "数据健康度报告（不接受任何参数；放在最后当全链的自证）"),
)

CLOSE_STEP_BY_NAME: dict[str, Step] = {s.name: s for s in CLOSE_STEPS}
CLOSE_STEP_ORDER: tuple[str, ...] = tuple(s.name for s in CLOSE_STEPS)

#: 月度刷新步骤。它替代 nanobot 侧原来的「C 月度刷新（日历+财报+长窗行情）」。
MONTHLY_STEPS: tuple[Step, ...] = (
    Step("calendar_holidays_fetch", ("calendar", "holidays", "fetch"), True,
         "抓上交所休市安排并落库（fail-closed：抓不到就不写，不是猜）"),
    Step("ingest_bars_long", ("ingest", "bars", "--days", "12000"), False,
         "长窗行情回补（捕获源站修订 / 覆盖当月新加入的标的；日链只跑 30 天）"),
    Step("ingest_financials", ("ingest", "financials"), False,
         "东财三表全量复核（季报频率，月度一次足够；已入库的期数 rows=0）"),
    Step("doctor", ("doctor",), False,
         "数据健康度报告（含 financial_reports 覆盖检查）"),
)

MONTHLY_STEP_BY_NAME: dict[str, Step] = {s.name: s for s in MONTHLY_STEPS}
MONTHLY_STEP_ORDER: tuple[str, ...] = tuple(s.name for s in MONTHLY_STEPS)


def _as_datetime(value: datetime | str) -> datetime:
    dt = value if isinstance(value, datetime) else datetime.fromisoformat(str(value))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=TZ)
    return dt.astimezone(TZ)


def _bars_rows(db_path: Path, trade_date: str) -> int | None:
    """asof 当天入库了几行日 K（`None` = 连表都读不到）。"""
    conn = ro_connect(db_path)
    try:
        row = conn.execute("SELECT COUNT(*) FROM bars_daily WHERE date=?",
                           (trade_date,)).fetchone()
    except sqlite3.Error:
        return None
    finally:
        conn.close()
    return int(row[0]) if row else 0


def _seal(payload: dict, *, db: Path, stamp: str, started_at: str,
          report_dir: Path | str | None, job: str) -> dict:
    """写回执（`<报告根>/ops/latest-<job>.json` + `job_runs` 一行）并把结果塞回载荷。"""
    return journal.seal(payload, db_path=db, job_name=job, stamp=stamp,
                        detail=summary_line(payload), started_at=started_at,
                        report_dir=report_dir)


def summary_line(payload: dict) -> str:
    """**一行**回执（launchd 日志与 `/lab/ops` 页面用同一份）。"""
    job = payload.get("job") or "ops"
    if payload.get("skipped"):
        return f"{job}: skipped（{payload['skipped']}）"
    if payload.get("error") or payload.get("refused"):
        return (f"{job}: blocked exit={payload.get('exit_code')}"
                f"（{payload.get('error') or payload.get('refused')}）")
    steps = payload.get("steps") or []
    total = len(CLOSE_STEPS if job == "close" else MONTHLY_STEPS)
    bad = [f"{s['name']}={s.get('exit_code')}" for s in steps
           if s.get("exit_code") != 0]
    after = (payload.get("after") or {}).get("exit_code")
    return (f"{job}: exit={payload.get('exit_code')} asof={payload.get('asof')}"
            f" steps={len(steps)}/{total} bad={','.join(bad) or '-'}"
            f" after={after} anomalies={len(payload.get('anomalies') or [])}")


def _step_anomalies(steps: list[dict]) -> list[dict]:
    """跑过的步骤里非 0 的 → 异常条目（1 = 如实报了异常，≥2 = 链断在这里）。"""
    out: list[dict] = []
    for s in steps:
        if s.get("exit_code") == 0:
            continue
        code = s.get("exit_code")
        out.append({
            "kind": "step_failed" if (code is None or code >= EXIT_BLOCKED)
                    else "step_anomaly",
            "step": s["name"], "exit_code": code,
            "detail": (s.get("stderr_tail") or s.get("stdout_tail")
                       or s.get("error") or "")[-200:],
        })
    return out


def run_close(*, db_path: Path | str | None = None, now: str | None = None,
              runner=None, timeout_s: float = CLOSE_TIMEOUT_S,
              report_dir: Path | str | None = None,
              backup_dir: Path | str | None = None) -> dict:
    """收盘链 = 库备份 → 12 步（**asof = 运行当天**）→ 事后体检 → 回执。

    退出码：0 全绿（含非交易日跳过）/ 1 链跑完但事后体检有 `missing`/`stale` 或某步
    退出 1 / 2 断链（库不在 / 还没收盘 / 某步 ≥2 / **整轮预算用尽或单步超时** /
    事后体检判不了 / 事后仍无当日 bar）。

    `runner` 是注入点（测试用假执行器，绝不真起子进程）。
    """
    job = "close"
    started_at = datetime.now(TZ).isoformat(timespec="seconds")
    stamp = now or started_at
    dt = _as_datetime(stamp)
    asof = dt.date().isoformat()
    db = Path(db_path) if db_path else paths.DB_PATH
    runner = runner or default_runner
    payload: dict = {"job": job, "now": stamp, "db": str(db), "asof": asof}

    if not db.exists():
        payload.update({
            "error": f"库不存在（{db}）；先跑 `stocklab db init`",
            "steps": [], "anomalies": [{"kind": "db_missing",
                                        "detail": f"{db} 不存在"}],
            "exit_code": EXIT_BLOCKED, "ok": False,
        })
        return payload

    # 非交易日 / 还没收盘：两条都必须**在任何写入之前**判掉。
    pre = check_db(db, stamp, report_dir=report_dir)
    sday = pre["session_day"]
    payload["session_day"] = sday
    if sday["is_trading_day"] is False:
        payload.update({
            "steps": [], "anomalies": [], "exit_code": EXIT_OK, "ok": True,
            "skipped": f"非交易日（{sday['why']}）→ 整轮跳过（07 §非交易日全部跳过）",
        })
        return _seal(payload, db=db, stamp=stamp, started_at=started_at,
                     report_dir=report_dir, job=job)

    if not is_trade_date_closed(asof, stamp):
        reason = (f"今天（{asof} {stamp[11:16]}）还没收盘 → 拒绝执行："
                  "半截 bar 与当日 LIVE 预测都是 append-only，写错了退不回来")
        payload.update({
            "refused": reason, "steps": [],
            "anomalies": [{"kind": "before_close", "detail": reason}],
            "exit_code": EXIT_BLOCKED, "ok": False,
        })
        return _seal(payload, db=db, stamp=stamp, started_at=started_at,
                     report_dir=report_dir, job=job)

    refusal = cross_db_refusal(db, list(CLOSE_STEP_ORDER), CLOSE_STEP_BY_NAME,
                               what="ops close")
    if refusal:
        payload.update({
            "refused": refusal, "steps": [],
            "anomalies": [{"kind": "cross_db_refused", "detail": refusal}],
            "exit_code": EXIT_BLOCKED, "ok": False,
        })
        return _seal(payload, db=db, stamp=stamp, started_at=started_at,
                     report_dir=report_dir, job=job)

    # 0) 库备份（07 D-7 的固定步）。备份失败就**不跑链**：这条链会往库里写一整天的
    #    数据，没有当天备份时继续跑，等于把「能回退」这个前提悄悄丢掉。
    bdir = Path(backup_dir) if backup_dir else paths.BACKUP_DIR
    try:
        payload["backup"] = str(backup_db(db, bdir, "preclose"))
    except (OSError, sqlite3.Error) as exc:
        payload.update({
            "error": f"库备份失败：{exc}", "steps": [],
            "anomalies": [{"kind": "backup_failed", "detail": str(exc)}],
            "exit_code": EXIT_BLOCKED, "ok": False,
        })
        return _seal(payload, db=db, stamp=stamp, started_at=started_at,
                     report_dir=report_dir, job=job)

    steps_out, aborted = run_steps(
        list(CLOSE_STEP_ORDER), registry=CLOSE_STEP_BY_NAME, runner=runner,
        db_path=db, now=stamp, asof=asof, timeout_s=timeout_s)

    after = check_db(db, stamp, report_dir=report_dir)
    rows = _bars_rows(db, asof)
    anomalies = _step_anomalies(steps_out)
    if rows == 0:
        reason = (f"{asof} 的日线一行都没有 —— ingest 段跑过之后仍然没有，"
                  "要么源站没给，要么这根本不是交易日（但判定说它是）")
        anomalies.append({"kind": "bars_missing_after_ingest",
                          "date": asof, "detail": reason})
    if aborted:
        payload["aborted"] = aborted

    codes = [worst_code(steps_out, aborted)]
    if rows == 0:
        codes.append(EXIT_ANOMALY)
    codes.append(after["exit_code"])          # 事后体检：判不了（2）也照实传上去

    payload.update({
        "steps": steps_out,
        "bars_rows": rows,
        "after": {k: after[k] for k in ("checks", "titles", "verdict",
                                        "calendar", "latest_closed_session",
                                        "anomalies", "exit_code", "ok")},
        "anomalies": anomalies,
        "exit_code": max(codes),
    })
    payload["ok"] = payload["exit_code"] == EXIT_OK
    return _seal(payload, db=db, stamp=stamp, started_at=started_at,
                 report_dir=report_dir, job=job)


def run_monthly(*, db_path: Path | str | None = None, now: str | None = None,
                runner=None, timeout_s: float = MONTHLY_TIMEOUT_S,
                report_dir: Path | str | None = None) -> dict:
    """月度刷新 = 休市公告 → 长窗行情 → 财报 → 体检（跑在每月 1 日 08:00）。

    **没有交易日判定**：它是「不管今天是不是交易日都要做」的维护活，且全部幂等。
    退出码与收盘链同构（0 / 1 / 2）。
    """
    job = "monthly"
    started_at = datetime.now(TZ).isoformat(timespec="seconds")
    stamp = now or started_at
    db = Path(db_path) if db_path else paths.DB_PATH
    runner = runner or default_runner
    payload: dict = {"job": job, "now": stamp, "db": str(db),
                     "asof": _as_datetime(stamp).date().isoformat()}

    if not db.exists():
        payload.update({
            "error": f"库不存在（{db}）；先跑 `stocklab db init`", "steps": [],
            "anomalies": [{"kind": "db_missing", "detail": f"{db} 不存在"}],
            "exit_code": EXIT_BLOCKED, "ok": False,
        })
        return payload

    refusal = cross_db_refusal(db, list(MONTHLY_STEP_ORDER), MONTHLY_STEP_BY_NAME,
                               what="ops monthly")
    if refusal:
        payload.update({
            "refused": refusal, "steps": [],
            "anomalies": [{"kind": "cross_db_refused", "detail": refusal}],
            "exit_code": EXIT_BLOCKED, "ok": False,
        })
        return _seal(payload, db=db, stamp=stamp, started_at=started_at,
                     report_dir=report_dir, job=job)

    steps_out, aborted = run_steps(
        list(MONTHLY_STEP_ORDER), registry=MONTHLY_STEP_BY_NAME, runner=runner,
        db_path=db, now=stamp, timeout_s=timeout_s)

    after = check_db(db, stamp, report_dir=report_dir)
    if aborted:
        payload["aborted"] = aborted
    codes = [worst_code(steps_out, aborted), after["exit_code"]]
    payload.update({
        "steps": steps_out,
        "after": {k: after[k] for k in ("checks", "titles", "verdict",
                                        "calendar", "latest_closed_session",
                                        "anomalies", "exit_code", "ok")},
        "anomalies": _step_anomalies(steps_out),
        "exit_code": max(codes),
    })
    payload["ok"] = payload["exit_code"] == EXIT_OK
    return _seal(payload, db=db, stamp=stamp, started_at=started_at,
                 report_dir=report_dir, job=job)


__all__ = ["CLOSE_STEPS", "CLOSE_STEP_BY_NAME", "CLOSE_STEP_ORDER",
           "CLOSE_TIMEOUT_S", "MONTHLY_STEPS", "MONTHLY_STEP_BY_NAME",
           "MONTHLY_STEP_ORDER", "MONTHLY_TIMEOUT_S",
           "run_close", "run_monthly", "summary_line"]
