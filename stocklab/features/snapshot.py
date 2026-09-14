"""特征快照与稳定哈希（Task 18）。

三件事：
1. **PIT 裁剪**（`usable_bars`）：只保留 `date <= asof` 的 K 线 —— 这是 R2 的入口。
2. **稳定哈希**：canonical JSON（键序固定、分隔符固定、非 ASCII 不转义）→ SHA256。
   同一份输入两次计算必须字节级一致；这是「快照可复现」的唯一依据。
3. **落库**：经 `repo`（数据库唯一写入口）写入 append-only 的 `features_daily`。
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass

from stocklab.data.models import Bar
from stocklab.features import registry
from stocklab.store import repo


def canonical_json(obj) -> str:
    """确定性 JSON 文本：键序/分隔符固定，非 ASCII 不转义。

    `allow_nan=False`：NaN / Infinity 不是合法 JSON，序列化阶段就硬失败，
    绝不让 `{"ma20":NaN}` 这种非法文本进入库或下游。
    """
    return json.dumps(obj, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False, allow_nan=False, default=str)


def payload_hash(payload: dict) -> str:
    return hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()


def params_hash(params: dict | None) -> str:
    """对**实际生效**的参数取哈希（显式写默认值 == 不写）。"""
    return hashlib.sha256(
        canonical_json(registry.effective_params(params)).encode("utf-8")
    ).hexdigest()


def _observations(bar: Bar) -> tuple:
    """用于判断「同一天的两条记录是否矛盾」的观测字段。

    刻意不含 `source`：同样的数值来自哪个源不算冲突；
    但复权口径（`adj_mode`）必须一致 —— 那是语义契约，不是来源差异。
    """
    return (bar.open, bar.high, bar.low, bar.close, bar.volume, bar.amount,
            bar.turnover, bar.adj_mode)


def usable_bars(bars, date: str) -> list[Bar]:
    """裁剪出 `date`（含）之前、按日期升序的 K 线。

    - 乱序输入 → 排序后输出（结果与入参顺序无关）。
    - 同一天出现**取值不同**的两条记录 → `ValueError`：静默取一条会让特征值
      依赖入参顺序，属于「无法判断就用错数」的典型（ERROR_DIARY 2026-09-14）。
    - 同一天出现**完全相同**的重复记录 → 视为同一根 K 线（`bars_daily` 的
      `PRIMARY KEY (code, date)` 本就不允许两条）。
    """
    by_date: dict[str, Bar] = {}
    for bar in bars:
        if bar.date > date:
            continue
        seen = by_date.get(bar.date)
        if seen is None:
            by_date[bar.date] = bar
        elif _observations(seen) != _observations(bar):
            raise ValueError(
                f"{bar.code} 在 {bar.date} 存在两条取值不同的记录："
                f"{_observations(seen)} vs {_observations(bar)}；"
                "无法判断哪条为真，拒绝静默取舍"
            )
    return [by_date[d] for d in sorted(by_date)]


@dataclass(frozen=True)
class FeatureSnapshot:
    code: str
    date: str
    feature_version: str
    feature_set: str
    core: dict
    payload: dict
    payload_hash: str
    params_hash: str
    data_version: str

    def db_row(self, now: str) -> dict:
        row = {c: self.core.get(c) for c in registry.CORE_COLUMNS}
        row.update({
            "code": self.code, "date": self.date,
            "feature_version": self.feature_version,
            "feature_set": self.feature_set,
            "json_payload": canonical_json(self.payload),
            "payload_hash": self.payload_hash,
            "params_hash": self.params_hash,
            "data_version": self.data_version,
            "created_at": now,
        })
        return row


def build_snapshot(code: str, date: str, bars, *, params: dict | None = None,
                   data_version: str = "", extra: dict | None = None,
                   feature_version: str = registry.FEATURE_VERSION
                   ) -> FeatureSnapshot | None:
    """构建 `date` 日的特征快照。

    返回 None 的两种情形（调用方必须记为「缺失」，不得当成 0 或沿用旧值）：
    1. `date`（含）之前的 K 线不足 `required_history(params)` 根；
    2. `date` 当天**没有** K 线 —— 停牌 / 非交易日 / 数据缺口。
       第 2 条是刻意的：拿前一日的收盘数据贴上今日的标签，就是最典型的前视污染。

    `extra` 里的长尾特征会进 `json_payload`，但不进宽表列。
    """
    usable = usable_bars(bars, date)
    if len(usable) < registry.required_history(params):
        return None
    if usable[-1].date != date:
        return None
    core = registry.compute_core(usable, params=params)
    payload = {**core, **(extra or {})}
    return FeatureSnapshot(
        code=code, date=date, feature_version=feature_version,
        feature_set=registry.FEATURE_SET, core=core, payload=payload,
        payload_hash=payload_hash(payload),
        params_hash=params_hash(params),
        data_version=data_version,
    )


def save_snapshot(conn: sqlite3.Connection, snap: FeatureSnapshot, *,
                  now: str) -> int:
    """写入快照，返回 `snapshot_id`。

    append-only：同 (code, date, feature_version, feature_set) 重复写会抛
    `sqlite3.IntegrityError`（改数值必须升 `feature_version`，schema A1）。
    """
    return repo.insert_feature_snapshot(conn, snap.db_row(now))


def find_snapshot(conn: sqlite3.Connection, code: str, date: str,
                  feature_version: str = registry.FEATURE_VERSION,
                  feature_set: str = registry.FEATURE_SET) -> sqlite3.Row | None:
    """按唯一键取已有快照（CLI 幂等判重用）。"""
    return conn.execute(
        "SELECT snapshot_id, payload_hash FROM features_daily"
        " WHERE code=? AND date=? AND feature_version=? AND feature_set=?",
        (code, date, feature_version, feature_set),
    ).fetchone()


def latest_snapshot_id(conn: sqlite3.Connection, code: str, date: str,
                       feature_version: str = registry.FEATURE_VERSION) -> int | None:
    row = conn.execute(
        "SELECT snapshot_id FROM features_daily WHERE code=? AND date=?"
        " AND feature_version=? ORDER BY snapshot_id DESC LIMIT 1",
        (code, date, feature_version),
    ).fetchone()
    return int(row["snapshot_id"]) if row else None
