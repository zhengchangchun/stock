"""策略生命周期的**派生**读出口：连续优化失败次数 + 冻结到期时间（P87 / D1–D4）。

## 为什么是「派生」而不是「加两列」

需求 00 §全局硬性约束 6 与 06 §D-6 都说「加字段」（`连续优化失败次数` / `冻结到期时间`），
但落点是**只读 fold**：`validation_cycles` 挂着 append-only 触发器
（`schema.sql` 的 `trg_validation_cycles_no_update`），一个「累计失败次数」列根本
**没法维护**（每次判定都得改写行，而改写会被 `RAISE(ABORT)` 拒）。
所以两个数一律从 `m2_judgements` 这一张既有台账**算出来**：
它的 `branch` / `asof_date` / `evidence_json.freeze_days` 是**唯一真源**，
本模块**一个字节都不写库**（`INSERT` / `UPDATE` / `DELETE` 一个都没有，
也不 `import` 任何写入模块）。这与 `financial_reports` 的派生指标不落库同款（ADR-016）。

## 三个数的定义（口径改一处就够，因为这里没有第二份数据）

| 数 | 定义 |
|---|---|
| `fail_streak` | 从该 `script_id` **最新**一条判定往回数：`branch='optimize'` 计一次；`'freeze'` / `'tune'` **停**（归零）；`'insufficient'` **穿透**（跳过继续往回看）。D3 的例：`[freeze, optimize, insufficient, optimize]` ⇒ 2 |
| `freeze_due` | 该 `script_id` **最新**一条 `branch='freeze'`（`asof_date <= asof`）的 `asof_date` ＋ 该行 `evidence.freeze_days` 个**自然日**。该行没有 `freeze_days` ⇒ `due=None`（**不是** 0、不是空串）。`due <= asof` 即「已到点」 |
| `due_freezes` | 「到期或已过期」的冻结版本清单（按 `script_id` 去重、只列**算得出到期日**的冻结），带「过期了几天」 |

**PIT 守卫**：所有读都过 `asof_date <= asof` 这一道（唯一读点在 `judgement_tail`）——
「拿未来的判定算今天的状态」是这一站最容易犯的错，所以它写在**取数**里而不是写在注释里。

## 触发行为：只提示、不执行（D6）

`fail_streak >= limits.STRATEGY_FAIL_STREAK_LIMIT` ⇒ `force_archive=True` ＋ 一句
`archive_reason`。**`branch` 的取值域一个字的改动都没有**（`archive` 不是新分支，
`m2_judgements.branch` 的 CHECK 不动），`conclusion` 也照原规则算，
**绝不自动归档** —— 归档仍走人工 `approve`（D-1 / D-24）。
"""

from __future__ import annotations

import datetime
import json
import sqlite3

from stocklab.config import limits
from stocklab.m2 import config as m2_config

#: 失败（＝优化后仍不达标）的分支（D2）。
FAILING_BRANCH: str = m2_config.BRANCH_OPTIMIZE

#: **终止连胜**的分支（D2）：`freeze` 是停用、`tune` 是换了参数集 —— 两者都说明
#: 「上一版的故事到此为止」，所以连续失败从 0 重数。
CLEARING_BRANCHES: tuple[str, ...] = (m2_config.BRANCH_FREEZE, m2_config.BRANCH_TUNE)

#: **穿透**的分支（D2/D3）：`insufficient` 只表示读数不足，不是「这一版失败了」，
#: 所以它既不递增也不归零。
TRANSPARENT_BRANCH: str = m2_config.BRANCH_INSUFFICIENT

#: 尾部摘要的行数上限 —— 它是**读出口的显示窗口**，不是判定口径
#: （`fail_streak` 的扫描不受它限制：`insufficient` 穿透可能要走很远）。
TAIL_LIMIT: int = 20

#: `force_archive` 为真时追加进 `judge()` 的 `actions` 的那一句（D6/D8）。
ARCHIVE_ACTION: str = (
    "按 00 §全局硬性约束 6 建议**强制归档**该策略版本、不再迭代"
    "（归档仍走人工 `approve`，本判定不执行）")


def _day(value: object) -> str:
    """严格 `YYYY-MM-DD`（宽松解析会让 `2026-3-1` 与 `2026-03-01` 比成两个日子）。"""
    text = str(value).strip()
    try:
        parsed = datetime.date.fromisoformat(text)
    except ValueError:
        raise ValueError(f"asof={value!r} 不是 YYYY-MM-DD —— 拒绝，不做补零/猜测") from None
    return parsed.isoformat()


def _add_days(day: str, days: int) -> str:
    return (datetime.date.fromisoformat(day)
            + datetime.timedelta(days=int(days))).isoformat()


def _lateness(due: str, asof: str) -> int:
    """`asof` 比 `due` 晚了几天（负数 = 还没到点）。`due == asof` ⇒ 0（已到点）。"""
    return (datetime.date.fromisoformat(asof)
            - datetime.date.fromisoformat(due)).days


def _evidence(evidence_json: object) -> dict:
    """`evidence_json` → dict。坏 JSON / 非对象一律当空 —— 读出口不该因为一行坏数据
    整个不可用，但也**不猜**它想说什么（要不到就是 `None`）。"""
    try:
        parsed = json.loads(evidence_json or "{}")
    except (TypeError, ValueError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _freeze_days(row: sqlite3.Row) -> int | None:
    value = _evidence(row["evidence_json"]).get("freeze_days")
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _tail_rows(conn: sqlite3.Connection, script_id: int, *,
               limit: int | None, asof: str | None) -> list[sqlite3.Row]:
    """**本模块唯一的取数点** —— PIT 守卫只在这里写一次，别的函数都经它读。"""
    sql = (f"SELECT judgement_id, cycle_id, script_id, asof_date, branch,"
           f" evidence_json FROM {m2_config.TABLE_JUDGEMENTS} WHERE script_id = ?")
    args: list[object] = [int(script_id)]
    if asof is not None:
        sql += " AND asof_date <= ?"          # PIT：不读未来的判定
        args.append(str(asof))
    sql += " ORDER BY asof_date DESC, judgement_id DESC"
    if limit is not None:
        sql += " LIMIT ?"
        args.append(int(limit))
    return list(conn.execute(sql, tuple(args)))


def judgement_tail(conn: sqlite3.Connection, script_id: int,
                   limit: int | None = None, *, asof: str | None = None) -> list[dict]:
    """该 `script_id` 的判定尾部（**最新在前**），摊平成只读摘要行。

    每行的键固定为 `judgement_id / cycle_id / asof_date / branch / branch_label /
    freeze_days` —— 不把整份 `evidence_json` 塞出来（读出口的形状要稳定，
    而上游的 `evidence` 是会长大的）。`asof` 给了就做 PIT 截断。
    """
    return [{
        "judgement_id": int(row["judgement_id"]),
        "cycle_id": int(row["cycle_id"]),
        "asof_date": str(row["asof_date"]),
        "branch": str(row["branch"]),
        "branch_label": m2_config.BRANCH_LABELS.get(str(row["branch"]),
                                                    str(row["branch"])),
        "freeze_days": _freeze_days(row),
    } for row in _tail_rows(conn, script_id, limit=limit,
                            asof=None if asof is None else _day(asof))]


def fail_streak(conn: sqlite3.Connection, script_id: int, asof: str) -> int:
    """连续优化失败次数（D2/D3，尾部连续 + `insufficient` 穿透）。"""
    streak = 0
    for row in judgement_tail(conn, script_id, asof=asof):
        branch = row["branch"]
        if branch == TRANSPARENT_BRANCH:
            continue
        if branch != FAILING_BRANCH:
            break
        streak += 1
    return streak


def _latest_freeze(conn: sqlite3.Connection, script_id: int,
                   asof: str) -> dict | None:
    for row in judgement_tail(conn, script_id, asof=asof):
        if row["branch"] == m2_config.BRANCH_FREEZE:
            return row
    return None


def freeze_due(conn: sqlite3.Connection, script_id: int, asof: str) -> dict:
    """冻结到期日（D4）。键固定 5 个；「没有冻结记录」与「有冻结但没有到期日」
    都落 `due=None`，靠 `found` 与 `freeze_days` 分辨是哪一种。"""
    day = _day(asof)
    row = _latest_freeze(conn, script_id, day)
    if row is None:
        return {"found": False, "asof_date": None, "freeze_days": None,
                "due": None, "overdue": None}
    days = row["freeze_days"]
    if days is None:                     # 该行没写冻结期 ⇒ 无到期日（不猜 90、不猜 0）
        return {"found": True, "asof_date": row["asof_date"], "freeze_days": None,
                "due": None, "overdue": None}
    due = _add_days(row["asof_date"], days)
    return {"found": True, "asof_date": row["asof_date"], "freeze_days": days,
            "due": due, "overdue": _lateness(due, day) >= 0}


def due_freezes(conn: sqlite3.Connection, asof: str) -> list[dict]:
    """「到期或已过期」的冻结版本清单（按 `script_id` 去重、按到期日升序）。

    每个 `script_id` 只看**最新**那条冻结判定 —— 旧冻结的到期日不是当前状态。
    算不出到期日的冻结（没有 `freeze_days`）**不进清单**：它不是「立刻到期」。
    """
    day = _day(asof)
    script_ids = [int(r[0]) for r in conn.execute(
        f"SELECT DISTINCT script_id FROM {m2_config.TABLE_JUDGEMENTS}"
        " WHERE asof_date <= ? ORDER BY script_id", (day,))]
    rows = []
    for script_id in script_ids:
        row = _latest_freeze(conn, script_id, day)
        if row is None or row["freeze_days"] is None:
            continue
        due = _add_days(row["asof_date"], row["freeze_days"])
        late = _lateness(due, day)
        if late < 0:
            continue
        rows.append({"script_id": script_id, "judgement_id": row["judgement_id"],
                     "cycle_id": row["cycle_id"], "asof_date": row["asof_date"],
                     "freeze_days": row["freeze_days"], "due": due,
                     "overdue_days": late})
    rows.sort(key=lambda r: (r["due"], r["script_id"]))
    return rows


def _archive_reason(streak: int, limit: int) -> str:
    return (f"连续优化失败 {streak} 次 ≥ 阈值 {limit} 次"
            "（需求 00 §全局硬性约束 6：同一策略版本连续 3 次优化验证失败 ⇒ "
            "强制归档、不再迭代）—— 建议归档该策略版本；"
            "归档动作仍走人工 approve（D-1/D-24），本判定不执行")


def _note(frozen: dict, asof: str) -> str:
    """人话摘要（D7 的 `--json` 之外那一面）。「无冻结记录」这句是 G2 的判据，
    去重在这里只有一处措辞 —— 渲染层不再自己编一句。"""
    if not frozen["found"]:
        return ("无冻结记录：该策略版本没有任何 branch=freeze 的判定 ⇒ freeze_due=null"
                "（不是 0 天、不是空串）")
    if frozen["due"] is None:
        return (f"有冻结判定（asof {frozen['asof_date']}）但该行 evidence.freeze_days 为空"
                " ⇒ 无到期日（freeze_due=null）")
    late = _lateness(frozen["due"], asof)
    tail = f"已过期 {late} 天" if late >= 0 else f"还有 {-late} 天到点"
    return (f"冻结判定 asof {frozen['asof_date']}，冻结 {frozen['freeze_days']} 天 ⇒ "
            f"到期 {frozen['due']}（{tail}）")


def snapshot(conn: sqlite3.Connection, script_id: int, asof: str) -> dict:
    """D7 的载荷形状。**键名与顺序固定**（测试逐位钉住 `SHAPE`）：

    `script_id / asof / fail_streak / limit / force_archive / tail / freeze_due /
    overdue / archive_reason / note`

    入口名是 `snapshot` 而不是 `summarize`：`m2/` 里**不许出现 `def summarize`**
    （`tests/test_m2_scores.py::FORBIDDEN_DEFS` 把那个名字留给指标算法，见任务书 §7）。

    `freeze_due` 与 `overdue` **永远在**：没有冻结记录时是 `null`（不是省略、不是 0）。
    """
    day = _day(asof)
    limit = int(limits.STRATEGY_FAIL_STREAK_LIMIT)
    streak = fail_streak(conn, script_id, day)
    frozen = freeze_due(conn, script_id, day)
    force = streak >= limit
    return {
        "script_id": int(script_id),
        "asof": day,
        "fail_streak": streak,
        "limit": limit,
        "force_archive": force,
        "tail": judgement_tail(conn, script_id, TAIL_LIMIT, asof=day),
        "freeze_due": frozen["due"],
        "overdue": frozen["overdue"],
        "archive_reason": _archive_reason(streak, limit) if force else None,
        "note": _note(frozen, day),
    }


def evidence_block(conn: sqlite3.Connection, script_id: int, asof: str) -> dict:
    """`judge()` 落进 `evidence.lifecycle` 的子对象（D6/D8）。

    它是 `snapshot` 的**投影**（不是第二份实现）—— 判定台账里不需要 `note` 与
    `tail` 的展示形态，但需要「算到了哪一步」：`fail_streak` / `limit` /
    `force_archive` / `archive_reason` / `archive_action`。
    """
    snap = snapshot(conn, script_id, asof)
    return {
        "fail_streak": snap["fail_streak"],
        "limit": snap["limit"],
        "force_archive": snap["force_archive"],
        "archive_reason": snap["archive_reason"],
        "archive_action": ARCHIVE_ACTION if snap["force_archive"] else None,
        "freeze_due": snap["freeze_due"],
        "overdue": snap["overdue"],
    }
