"""预测载荷的落库（Task 30，P6）：四态写入，**绝不静默覆盖**。

## 四态（对齐 `cmd_features_build` 的既有做法）

| 态 | 条件 | 行为 |
|----|------|------|
| `inserted` | 无同键行 | 写入 |
| `identical` | 同键行存在，且**由该行反算的载荷 hash == 新 hash** | 不写，成功返回 |
| `conflict` | 同键行存在，hash 不同 | 抛 `PredictionConflict`，退出码 1 |
| `skipped` | 数据不足 / 复权窗口不可用 | 在 `service` 层，不写任何行 |

## 两道防线，为什么都要

1. **代码四态**：能把「为什么没写」讲清楚（幂等重试 vs 真的冲突），
   并让 `conflict` 以非零退出码上报，而不是抛个栈。
2. **`UNIQUE (code, asof_date, model_version)` + append-only DELETE 触发器**：
   结构性。代码检查只在**本函数**里成立；唯一索引让「别的写入方静默覆盖」
   也不可能。两条叠加后，`INSERT OR REPLACE` 会因隐式删行撞上 append-only
   触发器而 ABORT（`test_silent_upsert_is_structurally_impossible` 钉住了这点）。

「反算 hash」的意思是：从库里那一行**重建**载荷（`payload_from_row`），
再按同一套规范化规则算 hash。这比「存一个 hash 列」更强 ——
它验证的是「库里躺着的这些字段，原样重算一遍还是不是同一个预测」，
而不只是「当时算过的那个 hash 还在不在」。
"""

from __future__ import annotations

import json
import sqlite3
from typing import Mapping

from stocklab.predict.model import canonical_json, payload_hash

#: `predictions` 里属于 §8.1 载荷契约的列（顺序即 INSERT 顺序）。
PAYLOAD_COLUMNS: tuple[str, ...] = (
    "code", "asof_date", "target_date", "direction_up", "direction_flat",
    "direction_down", "range_lo", "range_hi", "key_levels_json", "action",
    "size_pct", "invalidate_if", "strategy_mix_json", "model_version",
)

_INSERT_SQL = (
    "INSERT INTO predictions (code, asof_date, target_date, direction_up,"
    " direction_flat, direction_down, range_lo, range_hi, key_levels_json,"
    " action, size_pct, invalidate_if, strategy_mix_json, model_version,"
    " status, created_at, origin)"
    " VALUES (:code, :asof_date, :target_date, :direction_up, :direction_flat,"
    " :direction_down, :range_lo, :range_hi, :key_levels_json, :action,"
    " :size_pct, :invalidate_if, :strategy_mix_json, :model_version,"
    " 'ok', :created_at, :origin)"
)


class PredictionConflict(RuntimeError):
    """同 `(code, asof_date, model_version)` 已存在**不同**的载荷。

    刻意**不**做 upsert：`predictions` 会被 `verifications` 长期引用
    （`pred_id` FK），覆盖会让历史验证指向另一组数字，且**全程没有报错**
    —— 这正是本项目最怕的那类错误（ERROR_DIARY 2026-09-15「INSERT OR REPLACE
    会重置未列出的列」是同款）。要改预测就升 `model_version`。
    """


def find_prediction(conn: sqlite3.Connection, code: str, asof_date: str,
                    model_version: str) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM predictions WHERE code=? AND asof_date=? AND model_version=?",
        (code, asof_date, model_version),
    ).fetchone()


def payload_from_row(row: Mapping) -> dict:
    """由库里的行**重建** §8.1 载荷（用于 hash 比对与导出）。"""
    return {
        "code": row["code"],
        "asof_date": row["asof_date"],
        "target_date": row["target_date"],
        "direction": {"up": row["direction_up"], "flat": row["direction_flat"],
                      "down": row["direction_down"]},
        "range_80": [row["range_lo"], row["range_hi"]],
        "key_levels": json.loads(row["key_levels_json"] or "[]"),
        "action": row["action"],
        "size_pct": row["size_pct"],
        "invalidate_if": row["invalidate_if"],
        "strategy_mix": json.loads(row["strategy_mix_json"] or "{}"),
        "model_version": row["model_version"],
    }


def payload_to_row(payload: Mapping, *, now: str) -> dict:
    """§8.1 载荷 → `predictions` 的列。JSON 列用规范化序列化（与 hash 同源）。"""
    d = payload["direction"]
    return {
        "code": payload["code"],
        "asof_date": payload["asof_date"],
        "target_date": payload["target_date"],
        "direction_up": d["up"],
        "direction_flat": d["flat"],
        "direction_down": d["down"],
        "range_lo": payload["range_80"][0],
        "range_hi": payload["range_80"][1],
        "key_levels_json": canonical_json(payload["key_levels"]),
        "action": payload["action"],
        "size_pct": payload["size_pct"],
        "invalidate_if": payload["invalidate_if"],
        "strategy_mix_json": canonical_json(payload["strategy_mix"]),
        "model_version": payload["model_version"],
        "created_at": now,
    }


def insert_prediction(conn: sqlite3.Connection, payload: Mapping, *,
                      now: str, origin: str) -> tuple[str, int]:
    """四态写入，返回 `(state, pred_id)`。

    `origin ∈ {"live", "replay"}` 是**必填**关键字参数（P32）：它把「这条预测是
    回放产生还是实时产生」写成入库时就确定的字段，供 `chain accuracy` 断言分段。
    判据由**写入路径**定：`predict run` 写 `live`，`verify backfill` 写 `replay`。
    不设默认值 —— 谁忘了传就 TypeError，杜绝「静默漏标 → 退回推断」的假可信。
    库里 `origin TEXT CHECK(origin IN ('live','replay'))` 是第二道结构性防线。

    `state ∈ {"inserted", "identical"}`；`conflict` 抛 `PredictionConflict`
    而不是返回值 —— 调用方若忘了检查返回值，冲突就会变成静默通过。
    """
    key = (payload["code"], payload["asof_date"], payload["model_version"])
    row = find_prediction(conn, *key)
    if row is not None:
        if payload_hash(payload_from_row(row)) == payload_hash(payload):
            return "identical", int(row["pred_id"])
        raise PredictionConflict(
            f"{key[0]} {key[1]} 的预测已被 model_version={key[2]!r} 占用，"
            f"且新旧载荷不同（旧 {payload_hash(payload_from_row(row))[:12]}… vs "
            f"新 {payload_hash(payload)[:12]}…）。predictions 是 append-only："
            "不改写、不覆盖。要出新预测请升 model_version"
            "（与 features_daily 要改就升 feature_version 同款纪律）"
        )
    row = payload_to_row(payload, now=now)
    row["origin"] = origin
    cur = conn.execute(_INSERT_SQL, row)
    conn.commit()
    return "inserted", int(cur.lastrowid)
