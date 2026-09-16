"""每日复盘（P11，15:30）：`reports/YYYY-MM-DD-review.md` + `.json`。

## 这份报告最重要的一条纪律：口径不许漂

库里 6055 条验证行**全部**来自 `verify backfill` 的 PIT 历史回放。回放是合法的样本外
评估（P6 模型无参数拟合，见 `verify/replay.py` 的说明），但它**不是实盘记录**。
「38.14% / Brier 0.6581」写成「实盘表现」就是把口径漂了 —— 这是本项目最贵的一类错。

所以报告把准确率拆成 **LIVE** 与 **REPLAY** 两个桶分列，各自标注口径与样本量门槛，
并写死判据（见 `PROVENANCE_RULE`）。当前 LIVE 桶为 0 行时报告里就是 0 行，
不拿回放的数字去填实盘的格子。

## 为什么判据取「`created_at` 日期 == `asof_date`」

`verifications` / `predictions` 表里**没有**来源列，加列要改 append-only 表的历史，
不做。而「这个预测产生于它预测的那个交易日当天」是**结构可判**的：
历史回放与事后补跑的 `created_at` 必然晚于 `asof_date`（回放一次补几年），
实盘则必然相同（收盘后出次日预测）。

判据的**边界**也明写在报告的 `provenance.rule` 里：`created_at` 是**入库时刻**
而非决策时刻 —— 当天补跑一个历史 `asof` 会被记成实盘。这条限制不藏。
"""

from __future__ import annotations

import sqlite3
from collections.abc import Mapping, Sequence
from typing import Any

from stocklab.experiments.metrics import daily_stats
from stocklab.verify.report import MIN_DAYS

#: 口径判据原文（进报告，供审计者逐字复核）。
PROVENANCE_RULE = (
    "「实盘(LIVE)」⟺ 该预测的 created_at 日期 == 它的 asof_date"
    "（预测产生于它所预测的那个交易日当天）；其余（历史回放、事后补跑）一律计入"
    "「回放/补跑(REPLAY)」。边界：created_at 是**入库时刻**而非决策时刻 —— "
    "当天补跑一个历史 asof 会被记成 LIVE。"
)

#: 实盘桶里天数远小于门槛时，报告里必须同时出现这句话。
INSUFFICIENT = "样本不足，仅供观察"

#: `doctor` 检查的表（与既有 CLI 输出逐项一致）。
DOCTOR_TABLES = ("instruments", "bars_daily", "features_daily", "predictions",
                 "verifications", "data_quality", "system_events", "job_runs")


# ---------- 口径 ----------

def classify(asof_date: str, created_at: str) -> str:
    """`live` / `replay`（判据见模块 docstring）。

    刻意用**字符串前 10 位**比较而不是 SQL 的 `date()`：`created_at` 带 `+08:00`
    偏移，SQLite 的 `date()` 会先折成 UTC —— 凌晨 00:30 的预测会被折回前一天，
    在交易日边界上静默错分。字符串比较没有这个时区陷阱。
    """
    return "live" if (created_at or "")[:10] == (asof_date or "") else "replay"


def load_rows(conn: sqlite3.Connection, from_date: str, to_date: str) -> list[dict]:
    """读出 `[from, to]` 内的验证行，并**带上来源判据所需的 `created_at`**。

    与 `verify.replay.load_verification_rows` 同一份 join，多取一个
    `p.created_at`（那边不取，因为报告不需要分口径）。不修改既有函数 ——
    基线报告的口径一个字节都不许动。
    """
    from stocklab.verify.store import verification_from_row

    rows = conn.execute(
        "SELECT v.*, p.code AS p_code, p.asof_date AS p_asof,"
        " p.model_version AS p_model_version, p.created_at AS p_created_at,"
        " p.origin AS p_origin"
        " FROM verifications v JOIN predictions p ON p.pred_id = v.pred_id"
        " WHERE v.target_date BETWEEN ? AND ?"
        " ORDER BY p.model_version, v.target_date, p.code",
        (from_date, to_date)).fetchall()
    out: list[dict] = []
    for r in rows:
        out.append({**verification_from_row(r), "code": r["p_code"],
                    "asof_date": r["p_asof"], "model_version": r["p_model_version"],
                    "verification_id": int(r["verification_id"]),
                    "pred_created_at": r["p_created_at"],
                    #: `origin`（P32 来源列）原样带出；`provenance` 仍是**推断**
                    #: （classify）。断言 vs 推断的选择在 `chain.accuracy` 里做，
                    #: 这里只负责把两样都提供出来（P11 复盘仍用 provenance 推断口径）。
                    "origin": r["p_origin"],
                    "provenance": classify(r["p_asof"], r["p_created_at"])})
    return out


def _bucket(rows: Sequence[Mapping], *, min_days: int = MIN_DAYS) -> dict:
    """一个口径桶的准确率：**按日聚类**（同一交易日的标的先聚成日均值）+ CI。

    刻意复用 `experiments.metrics.daily_stats`（公开函数，且有测试把它与
    `verify.report._daily` 钉成同式），而不是再写第三份「按日聚合」。
    """
    scorable = [r for r in rows if r["scorable"]]
    by_day_dir: dict[str, list[float]] = {}
    by_day_brier: dict[str, list[float]] = {}
    by_day_base: dict[str, dict[str, list[float]]] = {}
    for r in scorable:
        d = r["target_date"]
        by_day_dir.setdefault(d, []).append(float(r["hit_direction"]))
        brier = r["notes"].get("brier")
        if brier is not None:
            by_day_brier.setdefault(d, []).append(float(brier))
        actual = r["notes"].get("actual_class")
        if actual:
            for name, guess in (("always_up", "up"), ("always_flat", "flat"),
                                ("always_down", "down")):
                by_day_base.setdefault(name, {}).setdefault(d, []).append(
                    1.0 if actual == guess else 0.0)

    direction = daily_stats(by_day_dir)
    brier = daily_stats(by_day_brier)
    n_days = direction["n_days"]
    return {
        "n_rows": len(rows),
        "n_scorable": len(scorable),
        "n_unscorable": len(rows) - len(scorable),
        "effective_n_days": n_days,
        "direction_accuracy_daily": direction["mean"],
        "direction_ci95": direction["ci95"],
        "brier_daily": brier["mean"],
        "brier_ci95": brier["ci95"],
        "baselines_daily": {k: daily_stats(v)["mean"]
                            for k, v in sorted(by_day_base.items())},
        "sample_gate": {
            "min_days": min_days,
            "meets": n_days >= min_days,
            "label": "样本充足" if n_days >= min_days else INSUFFICIENT,
        },
    }


def rolling_accuracy(conn: sqlite3.Connection, *, end_date: str,
                     n_sessions: int = 30, min_days: int = MIN_DAYS) -> dict:
    """截至 `end_date` 的滚动准确率，**按口径分列**。

    窗口 = 库里 `target_date <= end_date` 的最近 `n_sessions` 个交易日
    （取交易日而不是自然日：A 股的样本单位是交易日）。
    """
    days = [r["target_date"] for r in conn.execute(
        "SELECT DISTINCT target_date FROM verifications WHERE target_date <= ?"
        " ORDER BY target_date DESC LIMIT ?", (end_date, n_sessions))]
    if not days:
        return {"window": {"end": end_date, "n_sessions": 0, "start": None},
                "provenance": {"rule": PROVENANCE_RULE,
                               "live": {"n_rows": 0}, "replay": {"n_rows": 0}},
                "live": None, "replay": None,
                "note": "窗口内没有任何验证行 —— 不是「准确率是 0」，是「没有样本」"}
    start = min(days)
    rows = load_rows(conn, start, end_date)
    live = [r for r in rows if r["provenance"] == "live"]
    replay = [r for r in rows if r["provenance"] == "replay"]
    return {
        "window": {"end": end_date, "start": start, "n_sessions": len(days)},
        "provenance": {
            "rule": PROVENANCE_RULE,
            "live": {"n_rows": len(live)},
            "replay": {"n_rows": len(replay)},
        },
        "live": _bucket(live, min_days=min_days) if live else None,
        "replay": _bucket(replay, min_days=min_days) if replay else None,
        "note": (
            "**口径分列**：`live` 是实盘累计，`replay` 是 PIT 历史回放。"
            "两者不可相加、不可互相顶替。LIVE 为 null 表示窗口内一条实盘记录都没有 —— "
            "此时报告里**不得**出现任何被称为「实盘表现」的数字。"
        ),
    }


# ---------- 健康度 / 新鲜度 ----------

def doctor_report(conn: sqlite3.Connection) -> dict:
    """数据健康度（原 `cmd_doctor` 的取数逻辑，抽出来给复盘复用，输出逐字段不变）。"""
    out: dict = {}
    for table in DOCTOR_TABLES:
        out[table] = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
    row = conn.execute("SELECT MAX(date) AS d FROM bars_daily").fetchone()
    out["latest_bar_date"] = row["d"] if row else None
    out["open_issues"] = conn.execute(
        "SELECT COUNT(*) FROM data_quality WHERE resolved=0").fetchone()[0]
    last_job = conn.execute(
        "SELECT job_name, status, detail, finished_at FROM job_runs"
        " ORDER BY run_id DESC LIMIT 1").fetchone()
    out["last_job"] = dict(last_job) if last_job else None
    return out


def freshness(conn: sqlite3.Connection, date: str) -> dict:
    """数据新鲜度：行情最新日期、覆盖标的、当日采集次数与缺口。"""
    latest = conn.execute("SELECT MAX(date) AS d FROM bars_daily").fetchone()["d"]
    per_code = {r["code"]: r["last"] for r in conn.execute(
        "SELECT code, MAX(date) AS last FROM bars_daily GROUP BY code ORDER BY code")}
    instruments = [r["code"] for r in conn.execute(
        "SELECT code FROM instruments WHERE active=1 AND type='stock' ORDER BY code")]
    missing = [c for c in instruments if c not in per_code]

    snap_rows = conn.execute(
        "SELECT code, ts FROM quote_snapshots WHERE trade_date=? ORDER BY ts, code",
        (date,)).fetchall()
    slots = sorted({r["ts"] for r in snap_rows})
    per_code_snap: dict[str, int] = {}
    for r in snap_rows:
        per_code_snap[r["code"]] = per_code_snap.get(r["code"], 0) + 1

    return {
        "bars_latest_date": latest,
        "bars_latest_by_code": per_code,
        "instruments_active": instruments,
        "codes_without_bars": missing,
        "snapshots": {
            "trade_date": date,
            "n_rows": len(snap_rows),
            "n_codes": len(per_code_snap),
            "slots": slots,
            "n_slots": len(slots),
            "per_code": dict(sorted(per_code_snap.items())),
            "codes_expected": instruments,
            "codes_no_snapshot": [c for c in instruments if c not in per_code_snap],
        },
    }


def gap_manifest(conn: sqlite3.Connection, date: str) -> dict:
    """数据缺口清单：`amount`/`turnover` 何时开始有值 + 未验证的到期日。"""
    total = conn.execute("SELECT COUNT(*) AS n FROM bars_daily").fetchone()["n"]
    amt = conn.execute(
        "SELECT COUNT(amount) AS n, MIN(CASE WHEN amount IS NOT NULL THEN date END)"
        " AS first FROM bars_daily").fetchone()
    tur = conn.execute(
        "SELECT COUNT(turnover) AS n,"
        " MIN(CASE WHEN turnover IS NOT NULL THEN date END) AS first"
        " FROM bars_daily").fetchone()
    snap = conn.execute(
        "SELECT COUNT(*) AS n, MIN(trade_date) AS first, MAX(trade_date) AS last"
        " FROM quote_snapshots").fetchone()
    return {
        "bars_daily_rows": total,
        "amount_non_null": amt["n"],
        "amount_first_date": amt["first"],
        "turnover_non_null": tur["n"],
        "turnover_first_date": tur["first"],
        "quote_snapshots": {"rows": snap["n"], "first_trade_date": snap["first"],
                            "last_trade_date": snap["last"]},
        "note": (
            "`bars_daily` 的历史 NULL **保持 NULL**：它的语义是「当日未采集」，"
            "不是 0、不是待插值。回填只作用于当日，历史行一行不动。"
            "`amount_non_null` / `amount_first_date` 就是这件事的机器可读证据："
            "本报告日期之前的历史永远是 NULL，从回填上线那天起才开始有值。"
        ),
    }


def experiments_state(conn: sqlite3.Connection) -> dict:
    """实验台账现状（**只读**：一行都不写、不改）。"""
    n = conn.execute("SELECT COUNT(*) AS n FROM experiment_decisions").fetchone()["n"]
    rows = conn.execute(
        "SELECT variant_id, split, metric, gate_status, decision,"
        " MAX(decision_id) AS last_id FROM experiment_decisions GROUP BY variant_id"
        " ORDER BY variant_id").fetchall()
    return {
        "rows": n,
        "latest_by_variant": {r["variant_id"]: {
            "last_decision_id": r["last_id"], "split": r["split"],
            "metric": r["metric"], "gate_status": r["gate_status"],
            "decision": r["decision"]} for r in rows},
        "note": "台账 append-only；本报告只读不写（ADR-005 的键取语义不取呈现）",
    }


def day_results(conn: sqlite3.Connection, date: str) -> dict:
    """当日预测与验证结果（`target_date == date`）。"""
    preds = conn.execute(
        "SELECT model_version, COUNT(*) AS n FROM predictions"
        " WHERE target_date=? GROUP BY model_version ORDER BY model_version",
        (date,)).fetchall()
    vers = conn.execute(
        "SELECT COUNT(*) AS n, SUM(v.actual_close IS NOT NULL) AS scorable"
        " FROM verifications v WHERE v.target_date=?", (date,)).fetchone()
    unscorable = conn.execute(
        "SELECT COUNT(*) AS n FROM verifications v WHERE v.target_date=?"
        " AND v.actual_close IS NULL", (date,)).fetchone()["n"]
    states = conn.execute(
        "SELECT v.pred_id, v.actual_close, v.hit_direction, v.total_score,"
        " p.code AS code, p.asof_date AS asof_date,"
        " p.model_version AS model_version, p.created_at AS pred_created_at"
        " FROM verifications v JOIN predictions p ON p.pred_id=v.pred_id"
        " WHERE v.target_date=? ORDER BY p.model_version, p.code", (date,)).fetchall()
    return {
        "target_date": date,
        "predictions": {"n": sum(r["n"] for r in preds),
                        "by_model_version": {r["model_version"]: r["n"]
                                             for r in preds}},
        "verifications": {
            "n": vers["n"] or 0,
            "scorable": vers["scorable"] or 0,
            "unscorable": unscorable,
            "rows": [{**dict(r),
                      "provenance": classify(r["asof_date"], r["pred_created_at"])}
                     for r in states],
        },
    }


def build_review(conn: sqlite3.Connection, date: str, *,
                 n_sessions: int = 30) -> dict:
    """组装复盘报告数据（**不含生成时刻**：同输入两次运行 → 逐字节一致）。"""
    from stocklab.session.tick import closed_through, load_calendar
    from stocklab.verify.pending import pending_predictions

    cal, cal_error = load_calendar(conn)
    cal_dates = set(cal.all_dates)
    report = {
        "date": date,
        "doctor": doctor_report(conn),
        "session": {
            "calendar_error": cal_error,
            "calendar_covers_date": date in cal_dates,
            "calendar_says_session": cal.is_open(date) if cal_dates else None,
            # 复盘按定义发生在收盘之后 → 把当日视作已收盘（`23:59` 是**声明**
            # 而不是猜测：本命令的语义就是「这一天走完了，回头看」）
            "closed_through": closed_through(cal, f"{date}T23:59:59+08:00"),
            "calendar_range": ({"first": min(cal_dates), "last": max(cal_dates)}
                               if cal_dates else None),
        },
        "freshness": freshness(conn, date),
        "day": day_results(conn, date),
        "rolling": rolling_accuracy(conn, end_date=date, n_sessions=n_sessions),
        "experiments": experiments_state(conn),
        "gaps": gap_manifest(conn, date),
        # 缺步检测（P27）：有没有「已到期却从未被打分」的预测。
        # 锚点沿用本函数的既有写法（复盘按定义发生在收盘之后）→ 本报告仍然
        # **无时钟依赖**，「同输入两次运行逐字节一致」的性质不受影响。
        "pending": pending_predictions(conn, f"{date}T23:59:59+08:00"),
    }
    report["disclosure"] = _disclosure(report)
    return report


def _disclosure(report: Mapping) -> dict:
    """口径声明：**按实测行数取值，不按「有没有实盘开关」取值**（ERROR_DIARY #16）。

    这里的分支条件是「LIVE 桶里到底有多少行」这个**被描述的量本身**，
    不是「系统有没有实盘能力」这类代理开关。
    """
    live = report["rolling"].get("live")
    n_live = (live or {}).get("n_rows", 0)
    replay = report["rolling"].get("replay")
    return {
        "accuracy_provenance": (
            f"LIVE(实盘累计) {n_live} 行；REPLAY(PIT 历史回放) "
            f"{(replay or {}).get('n_rows', 0)} 行"
        ),
        "is_live_performance": n_live > 0,
        "note": (
            "报告里的准确率**默认按 PIT 历史回放口径标注**。当前 LIVE 桶为空时，"
            "任何数字都**不得**被称为「实盘表现」—— 回放证明的是「模型口径在历史上会给出"
            "什么分数」，不含实盘执行摩擦。"
            f"判据：{PROVENANCE_RULE}"
        ),
        "costs": "验证分数含 A 股成本（最低 5 元佣金 + 印花税 + 滑点）；"
                 "指数对照不含成本且不可直接交易",
        "no_rolling_deletion": "本项目不做任何滚动清理：所有表（含 quote_snapshots）历史全留",
    }


# ---------- 渲染 ----------

def _pct(x: Any, digits: int = 2) -> str:
    return "—" if x is None else f"{x * 100:.{digits}f}%"


def _num(x: Any, digits: int = 4) -> str:
    return "—" if x is None else f"{x:.{digits}f}"


def _window_line(name: str, bucket: dict | None) -> str:
    if not bucket:
        return (f"- **{name}**：窗口内 0 行 —— 不是「准确率是 0」，是「没有样本」"
                f"（不得用另一个口径的数字顶替）")
    gate = bucket["sample_gate"]
    lo, hi = (bucket["direction_ci95"] or [None, None])
    return (f"- **{name}**：方向准确率（按日聚类）{_pct(bucket['direction_accuracy_daily'])}"
            f"，95% CI [{_pct(lo)}, {_pct(hi)}]；Brier {_num(bucket['brier_daily'])}；"
            f"有效样本 {bucket['effective_n_days']} 个交易日"
            f"（{bucket['n_rows']} 行）→ **{gate['label']}**")


def render_markdown(report: Mapping) -> str:
    """报告正文。**不含生成时刻** —— 同输入两次运行逐字节一致（幂等证据）。"""
    d = report["date"]
    doc = report["doctor"]
    fr = report["freshness"]
    day = report["day"]
    roll = report["rolling"]
    gaps = report["gaps"]
    exp = report["experiments"]
    disc = report["disclosure"]

    lines = [
        f"# {d} 数据复盘",
        "",
        f"> {disc['note']}",
        "",
        "## 1. 数据健康度",
        "",
        f"- 最新 bar 日期：`{doc['latest_bar_date']}`；未决数据问题：{doc['open_issues']}",
        f"- 表行数：" + "、".join(f"`{t}`={doc[t]}" for t in DOCTOR_TABLES),
        f"- 最近一次作业：{doc['last_job'] or '（无）'}",
        "",
        "## 2. 数据新鲜度",
        "",
        f"- 日历覆盖：`{report['session']['calendar_range']}`；"
        f"本日 `{d}` 在日历内：{report['session']['calendar_covers_date']}；"
        f"最近已收盘交易日：`{report['session']['closed_through']}`",
        f"- 各标的 bars 最新日期：{fr['bars_latest_by_code']}",
        f"- 活跃标的中无 bar 的：{fr['codes_without_bars'] or '无'}",
        f"- 当日快照：`{d}` 共 {fr['snapshots']['n_rows']} 行 / "
        f"{fr['snapshots']['n_codes']} 标的 / {fr['snapshots']['n_slots']} 个时点"
        f"（时点：{fr['snapshots']['slots'] or '无'}）",
        f"- 当日无快照的标的：{fr['snapshots']['codes_no_snapshot'] or '无'}",
        "",
        "## 3. 当日预测与验证",
        "",
        f"- `target_date={d}` 的预测：{day['predictions']['n']} 条"
        f"（{day['predictions']['by_model_version'] or '无'}）",
        f"- 验证：{day['verifications']['n']} 条，其中可评分 "
        f"{day['verifications']['scorable']}、不可评分 {day['verifications']['unscorable']}",
        _pending_line(report.get("pending")),
        "",
        "## 4. 滚动准确率",
        "",
        f"- 窗口：`{roll['window']['start']}` ~ `{roll['window']['end']}`"
        f"（{roll['window']['n_sessions']} 个交易日）",
        f"- 口径判据：{roll['provenance']['rule']}",
        _window_line("LIVE（实盘累计）", roll.get("live")),
        _window_line("REPLAY（PIT 历史回放）", roll.get("replay")),
        "",
        f"- 对照（REPLAY 桶）：{_baseline_line(roll.get('replay'))}",
        f"- {roll['note']}",
        "",
        "## 5. 实验台账现状",
        "",
        f"- `experiment_decisions` 行数：{exp['rows']}（{exp['note']}）",
    ]
    for vid, info in exp["latest_by_variant"].items():
        lines.append(f"  - `{vid}` → {info['decision']} / gate={info['gate_status']}"
                     f"（{info['split']}·{info['metric']}，decision_id={info['last_decision_id']}）")
    lines += [
        "",
        "## 6. 数据缺口清单",
        "",
        f"- `bars_daily` 共 {gaps['bars_daily_rows']} 行；`amount` 非 NULL "
        f"{gaps['amount_non_null']} 行（首日 `{gaps['amount_first_date']}`）；"
        f"`turnover` 非 NULL {gaps['turnover_non_null']} 行"
        f"（首日 `{gaps['turnover_first_date']}`）",
        f"- `quote_snapshots`：{gaps['quote_snapshots']}",
        f"- {gaps['note']}",
        "",
        "## 7. 口径声明",
        "",
        f"- 准确率口径：{disc['accuracy_provenance']}；"
        f"本报告是否含实盘表现：**{disc['is_live_performance']}**",
        f"- 成本：{disc['costs']}",
        f"- 清理策略：{disc['no_rolling_deletion']}",
        "",
    ]
    return "\n".join(lines)


def _pending_line(pending: Mapping | None) -> str:
    """「到期未验证」那一行（P27 缺步检测）。

    三种取值**长得完全不一样**，因为它们要求三种不同的行动：

      - `n > 0` → 账本正在丢数据，给出**可操作**的补跑命令；
      - `n == 0` → 正常；
      - `n is None` → **判不了**（日历与行情两侧都判不出已收盘交易日），
        **不许显示成 0** —— 0 会被读成「没有缺口」（ERROR_DIARY #36）。
    """
    if not pending:
        return "- 到期未验证：**未评估**（本报告未带 pending 字段）"
    n = pending.get("n")
    if n is None:
        return (f"- 到期未验证：**判不了**（`{pending.get('reason')}` —— "
                f"日历与行情两侧都判不出「已收盘交易日」；这是判不了，不是没有缺口）")
    if n == 0:
        return (f"- 到期未验证：0 条"
                f"（判据：`target_date <= "
                f"{pending['latest_closed_session']}` 且无验证行）")
    return (f"- ⚠️ **到期未验证：{n} 条**（"
            + "、".join(f"`{d}`×{c}" for d, c in sorted(pending["by_target_date"].items()))
            + f"）—— 这些预测的账本正在丢数据，跑 `stocklab verify pending` 补"
              f"（幂等、append-only；判据：`{pending['evidence']['rule']}`）")


def _baseline_line(bucket: dict | None) -> str:
    if not bucket:
        return "无样本"
    b = bucket["baselines_daily"]
    return ("永远猜 up {a[always_up]}、flat {a[always_flat]}、down {a[always_down]}"
            "（按日聚类）".format(a={k: _pct(v) for k, v in b.items()}))
