"""实验决策落库（P8 Task 43）：把「哪个变体在哪一段是 WIN/LOSE」写成可 SQL 查的行。

## 为什么需要它

P8 上半的实验结论只活在**报告文件**里。文件是不可查询的：想问
「所有在 validate 段拿过 `WIN` 的变体」，只能把每份 markdown 读一遍。
本模块把报告的**判定结果**抽出来落成 append-only 的行。

## 只落决策，不落预测（P8 §1.1 继续有效）

变体**预测**依旧一行都不写 `predictions` / `verifications` —— 那条架构决策
（「变体不是模型版本」）没有任何松动。本表记的是「这次评估判了什么」，
粒度是 (变体, 分段, 指标)，与预测行数无关：一场回放 6000 行预测，
落库的是 4~6 行决策。

## 幂等语义（**读清楚再用**）

幂等键 = `(variant_id, split, metric, metric_version, gate_status, delta,
ci_low, ci_high)` —— **取语义（决策内容），不取呈现（报告文本）**。
措辞 / 时间戳 / 排版 / `report_sha256` 都不在键里。

| 情形 | 行为 |
|------|------|
| 库里没有该键 | `inserted` |
| 有该键，且语义字段逐字段相等 | `identical`：不写，成功返回（重跑同一场实验不会刷出一堆重复行） |
| 有该键，但 `decision` 不同 | 抛 `DecisionConflict` —— 闸门数字全同却给出不同 verdict，只能是「键撞了」或「有人绕过本模块改了行」，**不覆盖**、不静默 |

**换了区间/切分配置/数据快照 → 闸门数字变 → 追加新行**，旧结论原样保留。
这是 append-only 的本意：结论被推翻时追加新的，不改旧的
（`docs/experiments/README.md` 规则 7）。

**`report_sha256` 仍照写、照可查**（审计要它指回报告文件），只是不参与身份判定：
它哈希的是**整份报告字典**，连给人看的原因串都在内 —— 拿它当键，
**改一个错别字就多判一条决策**（ERROR_DIARY #17，实际污染过 4 行台账）。

## 为什么比的是「库里的样子」

ERROR_DIARY #10 的教训：幂等比较必须拿**读出来的行**做基准，
不能拿调用方手里的内存对象比。本模块的 `_canonical` 同时作用于写入侧与读取侧。
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from typing import Any, Mapping

from stocklab.store.db import transaction

#: 决策行的字段（顺序即 INSERT 顺序；`decision_id` / `created_at` 不在其中 ——
#: 前者由库产生，后者由本模块生成并**参与**比较）。
DECISION_FIELDS: tuple[str, ...] = (
    "variant_id", "split", "metric", "metric_version",
    "delta", "ci_low", "ci_high", "gate_status", "decision", "report_sha256",
)

#: 幂等键 = **语义**（决策内容）。（与 `schema.sql` 的 UNIQUE 约束**同源**，
#: 改一处必须改两处。）
#:
#: 措辞 / 时间戳 / 排版 / `report_sha256` 一律不进键：它们是「呈现」，
#: 改一个标点不该产生新决策行（ERROR_DIARY #17）。
IDEMPOTENCY_KEY: tuple[str, ...] = (
    "variant_id", "split", "metric", "metric_version",
    "gate_status", "delta", "ci_low", "ci_high",
)

#: 命中键之后，用来判「identical 还是冲突」的比较集 = 键 + `decision`。
#: `decision`（整场 verdict）是决策内容，但不在键里：它由 `selection_split` /
#: `test_evaluated` 这类开关决定，这些开关不进键 → 数字全同而 verdict 不同是可能的，
#: 那是**矛盾**（结论来源不同），必须报错而不是静默当成同一条。
COMPARED_FIELDS: tuple[str, ...] = IDEMPOTENCY_KEY + ("decision",)

_INSERT_SQL = (
    "INSERT INTO experiment_decisions (variant_id, split, metric, metric_version,"
    " delta, ci_low, ci_high, gate_status, decision, report_sha256, created_at)"
    " VALUES (:variant_id, :split, :metric, :metric_version, :delta, :ci_low,"
    " :ci_high, :gate_status, :decision, :report_sha256, :created_at)"
)

_SELECT_SQL = (
    "SELECT decision_id, variant_id, split, metric, metric_version, delta,"
    " ci_low, ci_high, gate_status, decision, report_sha256, created_at"
    " FROM experiment_decisions"
)


class DecisionConflict(RuntimeError):
    """同一个幂等键下已有一行**内容不同**的决策 —— 不覆盖，报错。"""


def _canonical(value: Any) -> Any:
    """把浮点规约到写入时的精度再比较。

    报告里的差值来自 `round(x, 6)` 一类的聚合；SQLite 的 REAL 是 IEEE754 双精度，
    往返本身无损，但**比较的两侧必须过同一套规约**（ERROR_DIARY #10）。
    """
    if isinstance(value, float):
        return round(value, 12)
    return value


def decisions_from_report(report: Mapping, *, report_sha256: str
                          ) -> list[dict[str, Any]]:
    """从 `runner.run_experiment` 的报告里抽出决策行（**纯函数**，不碰库）。

    每个 split 抽两行：`direction` 与 `brier` 各一行。两者共享该 split 的
    `gate_status`（gate 同时看两个指标才给结论），`decision` 取整场实验的 verdict。
    """
    verdict = report["verdict"]["status"]
    out: list[dict[str, Any]] = []
    for split_name, s in report["splits"].items():
        gate_status = s["gate"]["status"]
        for metric in ("direction", "brier"):
            stat = s["paired"][metric]
            ci = stat.get("ci95") or [None, None]
            out.append({
                "variant_id": report["variant"]["name"],
                "split": split_name,
                "metric": metric,
                "metric_version": report["metric_version"],
                "delta": stat.get("mean"),
                "ci_low": ci[0],
                "ci_high": ci[1],
                "gate_status": gate_status,
                "decision": verdict,
                "report_sha256": report_sha256,
            })
    return out


def record_decisions(conn: sqlite3.Connection, rows: list[Mapping],
                     *, now: str | None = None) -> dict[str, int]:
    """把决策行写进 `experiment_decisions`。返回 `{"inserted": n, "identical": m}`。

    键取语义（决策内容），不取呈现（报告文本）；改一个标点不得产生新决策行。

    单事务：要么全部写入，要么一行都不写（部分写入的台账比不写更糟 ——
    它会让人以为「只判了这两段」）。
    """
    stamp = now or datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    inserted = identical = 0
    with transaction(conn):
        for row in rows:
            key = {k: row[k] for k in IDEMPOTENCY_KEY}
            # `IS` 而非 `=`：`delta` / CI 可以是 NULL（INSUFFICIENT 段无 CI），
            # 而 SQLite 里 `NULL = NULL` 为假 —— 用 `=` 会让「同一条决策」查不到
            # 而重复插入。`IS` 是 null-safe 等值。
            where = " AND ".join(f"{k} IS :{k}" for k in IDEMPOTENCY_KEY)
            existing = conn.execute(
                f"{_SELECT_SQL} WHERE {where}", key).fetchall()
            if existing:
                # 键是语义键 → 查到的行**数字必然相同**；还能不同的只剩 `decision`
                # （整场 verdict），那说明结论来源矛盾，不是同一条决策。
                for prev in existing:
                    if all(_canonical(prev[f]) == _canonical(row[f])
                           for f in COMPARED_FIELDS):
                        identical += 1
                        break
                else:
                    raise DecisionConflict(
                        f"幂等键 {key} 下已有内容不同的决策行 "
                        f"(decision_id={existing[0]['decision_id']}) —— "
                        "同样一串闸门数字却给出不同 verdict，只能是键冲突或"
                        "被绕过写入；拒绝覆盖（append-only）"
                    )
                continue
            conn.execute(_INSERT_SQL, {**{f: row[f] for f in DECISION_FIELDS},
                                       "created_at": stamp})
            inserted += 1
    return {"inserted": inserted, "identical": identical}


def decisions_for(conn: sqlite3.Connection, *,
                  variant_id: str | None = None,
                  split: str | None = None,
                  gate_status: str | None = None) -> list[dict[str, Any]]:
    """按条件查决策行（「哪个变体在哪段是 WIN/LOSE」的 SQL 入口）。"""
    clauses, params = [], {}
    for field, value in (("variant_id", variant_id), ("split", split),
                         ("gate_status", gate_status)):
        if value is not None:
            clauses.append(f"{field} = :{field}")
            params[field] = value
    sql = _SELECT_SQL
    if clauses:
        sql += " WHERE " + " AND ".join(clauses)
    sql += " ORDER BY decision_id"
    return [dict(r) for r in conn.execute(sql, params)]
