"""验证结果的落库（Task 33，P7）：四态，**绝不静默覆盖**。

## 四态

| 态 | 条件 | 行为 |
|----|------|------|
| `inserted` | 该 `pred_id` 没有验证行 | 写入 |
| `identical` | 已有行，且**由该行反算的载荷 hash == 新 hash** | 不写，成功返回 |
| `rescored_after_data_gap` | 已有行是**不可评分**（`DATA`），新结果可评分 | 更新**结果列**，身份列不动；调用方必须**打印 ⚠️** |
| `conflict` | 其余一切「同 `pred_id` 不同内容」 | 抛 `VerificationConflict`，退出码 1，**不覆盖** |

## 为什么「不可评分 → 可评分」要单独放行

`verify run --target-date D` 在 D 当天收盘后跑，那时 D 的 bar **本来就不存在**
（P6 的 target_date 是日历的下一交易日，库里必然还没有它）。于是最早那次验证
必然写下一行 `DATA`。等 bar 到达后再跑，如果这也算「冲突」，
**日循环就永久卡死**：第一条记录把自己未来的路堵死了。

ADR-002 早就为这种情形留了口子（身份列不可变 + 结果列可更新 + `DELETE` 全禁）。
本模块据此放行**唯一一种**更新：`unscorable → scorable`，且：

- 身份列（`verification_id` / `pred_id` / `target_date` / `created_at`）**一个都不写**；
- 返回的状态是 `rescored_after_data_gap` 而不是 `identical`，CLI 必须**显式打印**；
- **反向不允许**：已可评分的成绩不许被降级成 `DATA`（那是用「数据没了」抹掉成绩）。

## 两道防线

1. **代码四态**：能把「为什么没写」讲清楚，并让冲突以非零退出码上报，而不是抛个栈。
2. **`UNIQUE INDEX ux_verifications_pred(pred_id)`**：结构性。绕过本模块直接 INSERT
   第二行也进不去；身份列另有 ADR-002 的触发器拦 UPDATE。
"""

from __future__ import annotations

import json
import sqlite3
from typing import Mapping

from stocklab.predict.model import canonical_json
from stocklab.verify.score import VERIFICATION_FIELDS, verification_hash_fields

#: `verifications` 的结果列（顺序即 INSERT 顺序）。身份列（`verification_id` /
#: `pred_id` / `target_date` / `created_at`）不在其中 —— 它们由库自己产生或不可变。
_INSERT_SQL = (
    "INSERT INTO verifications (pred_id, target_date, actual_close, actual_pct,"
    " benchmark_pct, hit_direction, hit_range, hit_levels, sim_pnl,"
    " score_direction, score_range, score_level, score_action, total_score,"
    " invalidated, attribution_auto, notes, created_at)"
    " VALUES (:pred_id, :target_date, :actual_close, :actual_pct,"
    " :benchmark_pct, :hit_direction, :hit_range, :hit_levels, :sim_pnl,"
    " :score_direction, :score_range, :score_level, :score_action, :total_score,"
    " :invalidated, :attribution_auto, :notes, :created_at)"
)

#: 「不可评分 → 可评分」补分时唯一允许更新的列集合（**身份列不在其中**）。
_RESCORE_COLUMNS: tuple[str, ...] = (
    "actual_close", "actual_pct", "benchmark_pct", "hit_direction", "hit_range",
    "hit_levels", "sim_pnl", "score_direction", "score_range", "score_level",
    "score_action", "total_score", "invalidated", "attribution_auto", "notes",
)


class VerificationConflict(RuntimeError):
    """同 `pred_id` 已有一条**内容不同**的验证记录。

    刻意不做 upsert：`verifications` 是准确率的账本，能被改写就等于准确率可以被编辑。
    要重算就查清「为什么同一份预测同一根 bar 会算出不同结果」——
    那一定是代码或数据出了变化，属于要留痕的事，不属于是要自动抹平的事。
    """


def verification_hash(payload: Mapping) -> str:
    """载荷的 sha256（只覆盖 `VERIFICATION_FIELDS`）。"""
    return verification_hash_fields(payload)


def find_verification(conn: sqlite3.Connection, pred_id: int) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM verifications WHERE pred_id=?", (pred_id,)).fetchone()


def verification_from_row(row: Mapping) -> dict:
    """由库里的行**重建**载荷（幂等判定与报告都基于它，不基于调用方手里的对象）。"""
    notes = json.loads(row["notes"] or "{}")
    return {
        "pred_id": row["pred_id"],
        "target_date": row["target_date"],
        # 可评分性由 `actual_close` 是否有值决定：不可评分的行结果列**一律为空**
        # （这样统计层没有任何办法把它当成 0 分混进分母）
        "scorable": row["actual_close"] is not None,
        # 原因码无独立列，存在 notes 里（它必须进 hash：原因不同 = 不是同一条记录）
        "reason_code": notes.get("reason_code"),
        "actual_close": row["actual_close"],
        "actual_pct": row["actual_pct"],
        "benchmark_pct": row["benchmark_pct"],
        "hit_direction": row["hit_direction"],
        "hit_range": row["hit_range"],
        "hit_levels": row["hit_levels"],
        "sim_pnl": row["sim_pnl"],
        "score_direction": row["score_direction"],
        "score_range": row["score_range"],
        "score_level": row["score_level"],
        "score_action": row["score_action"],
        "total_score": row["total_score"],
        "invalidated": row["invalidated"],
        "attribution_auto": row["attribution_auto"],
        "notes": notes,
    }


def _to_columns(payload: Mapping, *, now: str | None = None) -> dict:
    row = {k: payload.get(k) for k in VERIFICATION_FIELDS if k != "notes"}
    # `verifications.invalidated` 是 `INTEGER NOT NULL DEFAULT 0`，放不下 NULL。
    # 而 `invalidated=None` 的语义是**UNDETERMINED**（`invalidate_if` 原文解析不出来），
    # 与 0（「没失效」）不是一回事。这里存 0 是**列的类型约束**所迫，
    # **真值在 `notes.undetermined` 里**（含 "invalidate_if" 时该列无意义）。
    # 报告侧必须据此把这批行排除在「失效统计」之外，不许把 0 当成「没失效」。
    if row.get("invalidated") is None:
        row["invalidated"] = 0
    row["notes"] = canonical_json(payload["notes"])
    if now is not None:
        row["created_at"] = now
    return row


def _diff(old: Mapping, new: Mapping) -> list[str]:
    return [k for k in VERIFICATION_FIELDS
            if k != "notes" and old.get(k) != new.get(k)]


def _canonical(payload: Mapping) -> dict:
    """把载荷规约成**入库后的样子**，供 hash 比较使用。

    幂等判定必须比「库里的行」而不是「调用方手里的对象」：
    `invalidated` 在内存里可以是 `None`（UNDETERMINED），入库后被列类型逼成 `0`。
    若拿未规约的新载荷去比已规约的旧行，**同一份结果第二次跑就一定「冲突」** ——
    这条 bug 真的发生过：全历史回放里 13 条不可评分记录让第二次回放在
    `pred_id=27` 上崩掉（见 ERROR_DIARY #10）。hash 之前先过这里，两边才是同一把尺子。
    """
    return _to_columns(payload)


def insert_verification(conn: sqlite3.Connection, payload: Mapping, *,
                        now: str) -> tuple[str, int]:
    """四态写入，返回 `(state, verification_id)`。

    `state ∈ {"inserted", "identical", "rescored_after_data_gap"}`；
    `conflict` 抛 `VerificationConflict` 而不是返回值 —— 调用方忘了检查返回值时，
    冲突就会变成静默通过。
    """
    pred_id = int(payload["pred_id"])
    row = find_verification(conn, pred_id)
    if row is not None:
        old = verification_from_row(row)
        if verification_hash(_canonical(old)) == verification_hash(_canonical(payload)):
            return "identical", int(row["verification_id"])
        if not old["scorable"] and payload.get("scorable"):
            # 唯一放行的更新：DATA 缺口补分（见模块 docstring）
            sets = ", ".join(f"{c}=:{c}" for c in _RESCORE_COLUMNS)
            conn.execute(
                f"UPDATE verifications SET {sets} WHERE verification_id=:vid",
                {**_to_columns(payload), "vid": int(row["verification_id"])})
            conn.commit()
            return "rescored_after_data_gap", int(row["verification_id"])
        raise VerificationConflict(
            f"pred_id={pred_id} 已有一条内容不同的验证记录"
            f"（差异字段 {_diff(old, payload)}）—— verifications 是准确率的账本，"
            "不改写、不覆盖。同一份预测同一根 bar 算出不同结果，说明代码或数据变了，"
            "必须查清并留痕（要修就改代码后用 `--from/--to` 重新回放整段，"
            "或人工在 `attribution_manual` 列上标注）"
        )
    cur = conn.execute(_INSERT_SQL, _to_columns(payload, now=now))
    conn.commit()
    return "inserted", int(cur.lastrowid)
