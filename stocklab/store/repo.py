"""仓储层（Task 14）：**数据库的唯一写入口**。

纪律：业务代码不得自行拼 INSERT/UPDATE，一律经本模块 ——
这样「哪些表可覆盖、哪些表 append-only」只有一个地方需要审计。

append-only 的**刻意例外**：`bars_daily` 允许覆盖。
理由：行情数据会被源站修订（送股、更正的错误价、补发数据），
若禁止覆盖，错误的旧值将永久留在库里，比允许修订危险得多。
`adj_mode` 是主键的一部分（`PRIMARY KEY (code, date)` 之外的口径区分见
`insert_bars` 的 note）—— 不同复权口径**不得互相覆盖**。

`features_daily` 则是**真正的 append-only**：同键重写直接拒绝（见
`insert_feature_snapshot`），因为快照会被预测长期引用、必须不可变。

失败留痕（铁律③）：本模块只负责写，问题由调用方检查返回值与异常。
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from typing import Mapping, Sequence
from zoneinfo import ZoneInfo

from stocklab.config.universe import Instrument
from stocklab.data.models import Bar, CorpAction
from stocklab.quality.checks import Issue

TZ = ZoneInfo("Asia/Shanghai")


def now_iso() -> str:
    return datetime.now(TZ).isoformat(timespec="seconds")


def upsert_instruments(conn: sqlite3.Connection, instruments: Sequence[Instrument],
                       *, now: str) -> int:
    """登记标的。`added_at` 是首次入库时间，**不随再次 upsert 变化**。"""
    rows = [(i.code, i.name, i.market, i.board, "stock", now) for i in instruments]
    conn.executemany(
        "INSERT INTO instruments (code, name, market, board, type, added_at)"
        " VALUES (?,?,?,?,?,?)"
        " ON CONFLICT(code) DO UPDATE SET name=excluded.name,"
        " market=excluded.market, board=excluded.board",
        rows,
    )
    conn.commit()
    return len(rows)


def insert_bars(conn: sqlite3.Connection, bars: Sequence[Bar], *, now: str) -> int:
    """写入日线。`bars_daily` 允许覆盖（源站会修订历史，见模块 docstring）。

    **拒绝任何 `adj_mode != "none"` 的 bar**（铁律①：抓取层绝不下发复权价）。
    这是刻意把「口径错误」变成**写入口的硬错误**而不是静默降级：
    schema 的 PK 是 `(code, date)`，若允许 qfq 写入，它会**静默覆盖**同一天
    的不复权价，下游拿到的价格贴错标签且无任何报错（ERROR_DIARY 2026-09-14
    「解析器取不到就回退」是同一类错误的另一种形态）。

    计划接口原文写「`adj_mode` 不同视为不同记录」，与 schema 的 PK 互斥
    （同一天放不下两条口径）→ 按「哪个意图更强」取舍：铁律①优先，改为拒绝。
    """
    bad = [b.code for b in bars if b.adj_mode != "none"]
    if bad:
        raise ValueError(
            f"insert_bars 拒绝复权数据（adj_mode != 'none'）：{sorted(set(bad))}；"
            "bars_daily 只存不复权价（铁律①）"
        )
    rows = [(b.code, b.date, b.open, b.high, b.low, b.close, b.volume, b.amount,
             b.turnover, b.adj_mode, 1 if b.volume == 0 else 0, b.source, now)
            for b in bars]
    conn.executemany(
        "INSERT INTO bars_daily (code, date, open, high, low, close, volume, amount,"
        " turnover, adj_mode, is_suspended, source, fetched_at)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)"
        " ON CONFLICT(code, date) DO UPDATE SET"
        " open=excluded.open, high=excluded.high, low=excluded.low,"
        " close=excluded.close, volume=excluded.volume, amount=excluded.amount,"
        " turnover=excluded.turnover, adj_mode=excluded.adj_mode,"
        " is_suspended=excluded.is_suspended, source=excluded.source,"
        " fetched_at=excluded.fetched_at",
        rows,
    )
    conn.commit()
    return len(rows)


def insert_corp_actions(conn: sqlite3.Connection, actions: Sequence[CorpAction], *,
                        now: str) -> tuple[int, int]:
    """写入除权除息事件，返回 `(写入条数, 被修订条数)`。

    幂等：`PRIMARY KEY (code, cqr)` 冲突时更新条款列，**`first_seen` 永不重置**
    （`INSERT OR REPLACE` 会删行重插并把未列出的列打回默认值 —— ERROR_DIARY
    2026-09-15 记过这个坑，故此处一律用 `ON CONFLICT DO UPDATE` 并显式列出列）。

    修订数是刻意返回的：源站改口径（例如把税后 `fh_sh` 改成税前）会**改变复权价**，
    下游必须能看见「这条事件被动过」，而不是无声地换掉历史因子。
    """
    rows = [(a.code, a.cqr, a.djr, a.fh_sh, a.content, a.source, now, now)
            for a in actions]
    existing = {r["cqr"]: r for r in conn.execute(
        "SELECT cqr, djr, fh_sh, content FROM corp_actions WHERE code=?",
        (actions[0].code,))} if actions else {}
    restated = 0
    for a in actions:
        old = existing.get(a.cqr)
        if old is not None and (old["djr"], old["fh_sh"], old["content"]) != (
                a.djr, a.fh_sh, a.content):
            restated += 1
    conn.executemany(
        "INSERT INTO corp_actions (code, cqr, djr, fh_sh, content, source,"
        " first_seen, last_seen) VALUES (?,?,?,?,?,?,?,?)"
        " ON CONFLICT(code, cqr) DO UPDATE SET"
        " djr=excluded.djr, fh_sh=excluded.fh_sh, content=excluded.content,"
        " source=excluded.source, last_seen=excluded.last_seen",
        rows,
    )
    conn.commit()
    return len(rows), restated


def insert_adj_factors(conn: sqlite3.Connection, code: str, chain, *,
                       source: str, now: str) -> int:
    """把因子链落 `adj_factors`（每个 K 线日一行，`factor` 显式、禁 NULL）。

    允许覆盖（与 `bars_daily` 同理由）：因子是**纯函数**——
    由不复权 K 线 + 事件链算出，重算结果必须能落库修正；若做成 append-only，
    源站修订分红后错误的旧因子会永久留在库里（比允许覆盖危险得多，ADR-001 §5 同款取舍）。

    无事件的标的也会写入全 1 行 —— ADR-001 明确要求显式默认值，禁止用 NULL 表示。
    """
    rows = [(code, d, f, source, now) for d, f in sorted(chain.factors.items())]
    conn.executemany(
        "INSERT INTO adj_factors (code, date, factor, source, fetched_at)"
        " VALUES (?,?,?,?,?)"
        " ON CONFLICT(code, date) DO UPDATE SET"
        " factor=excluded.factor, source=excluded.source,"
        " fetched_at=excluded.fetched_at",
        rows,
    )
    # 不可用区间与因子**同批重写**（两者必须同源）：只写因子不写 blackout，
    # 或反之，都会让「库里的可用性记录」与「链的真实缺口」不一致 ——
    # 读取层会因此对一段算不出收益的历史放行（静默假收益）。
    conn.execute("DELETE FROM adj_factor_blackout WHERE code=?", (code,))
    conn.executemany(
        "INSERT INTO adj_factor_blackout (code, cqr, reason, source, fetched_at)"
        " VALUES (?,?,?,?,?)",
        [(code, u.cqr, u.reason, source, now) for u in chain.unusable],
    )
    conn.commit()
    return len(rows)


FEATURE_SNAPSHOT_COLUMNS: tuple[str, ...] = (
    "code", "date", "feature_version", "feature_set", "close", "ma20", "ma60",
    "atr14", "vol_ratio_5_20", "ret_1d", "ret_5d", "main_net_5d", "pe_pct_3y",
    "regime_label", "json_payload", "payload_hash", "params_hash",
    "data_version", "created_at",
)

_INSERT_FEATURE_SQL = (
    "INSERT INTO features_daily (" + ", ".join(FEATURE_SNAPSHOT_COLUMNS) + ")"
    " VALUES (" + ", ".join(f":{c}" for c in FEATURE_SNAPSHOT_COLUMNS) + ")"
)


def insert_feature_snapshot(conn: sqlite3.Connection, row: Mapping[str, object]) -> int:
    """写特征快照（`features_daily`），返回 `snapshot_id`。

    **刻意不做 upsert**（与 `bars_daily` 的覆盖语义相反）：`features_daily` 是
    append-only，`UNIQUE(code, date, feature_version, feature_set)` 冲突时直接抛
    `IntegrityError`。理由：同一个 `snapshot_id` 必须永远对应同一组数值 ——
    覆盖会让长期引用它的 `predictions.feature_snapshot_id` 无声地指向另一组数值。
    要重算就升 `feature_version`（schema A1）。

    列名做白名单校验：多传/漏传都报错，避免写入口静默丢字段。
    """
    unknown = sorted(set(row) - set(FEATURE_SNAPSHOT_COLUMNS))
    if unknown:
        raise ValueError(f"features_daily 未知列：{unknown}")
    missing = [c for c in FEATURE_SNAPSHOT_COLUMNS if c not in row]
    if missing:
        raise ValueError(f"features_daily 缺少列：{missing}")
    cur = conn.execute(_INSERT_FEATURE_SQL,
                       {c: row[c] for c in FEATURE_SNAPSHOT_COLUMNS})
    conn.commit()
    return int(cur.lastrowid)


def insert_quality_issues(conn: sqlite3.Connection, issues: Sequence[Issue], *,
                          source: str, now: str) -> int:
    """写质量问题。按 `UNIQUE(date, source, code, issue_type)` 去重并累加 `occurrences`。

    去重是必要的：同一问题每天都会重新检出，若每次新增一行，
    `data_quality` 会被噪声淹没，真正的新问题反而看不见。
    """
    rows = [(i.date, source, i.code, i.issue_type, i.severity, i.detail, now, now)
            for i in issues]
    conn.executemany(
        "INSERT INTO data_quality (date, source, code, issue_type, severity, detail,"
        " first_seen, last_seen) VALUES (?,?,?,?,?,?,?,?)"
        " ON CONFLICT(date, source, code, issue_type) DO UPDATE SET"
        " last_seen=excluded.last_seen, severity=excluded.severity,"
        " detail=excluded.detail, occurrences=data_quality.occurrences+1",
        rows,
    )
    conn.commit()
    return len(rows)


def log_event(conn: sqlite3.Connection, module: str, level: str, message: str, *,
              context: dict | None = None, now: str | None = None) -> int:
    cur = conn.execute(
        "INSERT INTO system_events (ts, module, level, message, context_json)"
        " VALUES (?,?,?,?,?)",
        (now or now_iso(), module, level, message,
         json.dumps(context or {}, ensure_ascii=False)),
    )
    conn.commit()
    return int(cur.lastrowid)


def record_job(conn: sqlite3.Connection, job_name: str, *, status: str,
               started_at: str, scheduled_at: str | None = None,
               detail: str | None = None) -> int:
    """登记一次作业运行，返回 `run_id`（用 `finish_job` 收尾）。"""
    cur = conn.execute(
        "INSERT INTO job_runs (job_name, scheduled_at, started_at, status, detail)"
        " VALUES (?,?,?,?,?)",
        (job_name, scheduled_at, started_at, status, detail),
    )
    conn.commit()
    return int(cur.lastrowid)


def finish_job(conn: sqlite3.Connection, run_id: int, *, status: str,
               finished_at: str, detail: str | None = None) -> None:
    conn.execute(
        "UPDATE job_runs SET status=?, finished_at=?, detail=? WHERE run_id=?",
        (status, finished_at, detail, run_id),
    )
    conn.commit()


def latest_bar_date(conn: sqlite3.Connection, code: str) -> str | None:
    row = conn.execute(
        "SELECT MAX(date) AS d FROM bars_daily WHERE code=?", (code,)
    ).fetchone()
    return row["d"] if row and row["d"] else None
