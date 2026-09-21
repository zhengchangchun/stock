"""验证服务（Task 34，P7）：取实际行情 → 打分 → 落库。

## 本模块负责的三件事

1. **取「实际」时只用 `<= target_date` 的行**。`load_scoring_bars` 的 `as_of`
   字面就是 `target_date`，没有第二个上限来源；`score.score_prediction` 再用
   「`asof_date` 与 `target_date` 两根都必须**正好**在序列里」兜一道 ——
   `>=` / 「取最近一根」这类宽松查找会拿**别的交易日**冒充目标日，
   那是本项目最怕的一类错（数字合法、结论全错）。
2. **不可评分显式化**。目标日无 bar / 停牌 / 复权不可用 → `scorable=False` +
   `reason_code` + 归因 `DATA`，**照样落库**（「这天没验证成」本身就是要留痕的事实），
   但结果列全空，所以任何统计都不可能把它当 0 分算进分母。
3. **归因只写程序判得了的那一个**。`DATA` 由本层判定；
   `SIGNAL` / `STRATEGY` / `MODEL` / `NOISE` 一律 `UNDETERMINED`，
   留 `verifications.attribution_manual` 给人工回填（§9 + ADR-002）。

## 复权口径（评分侧）

实际收益 = `adj_close(target) / adj_close(asof) - 1`，复权**锚定在 `target_date`**。
比值对锚点不变，所以这与「用 asof 锚定」等价，但锚在 target 更省事：
一次 `load_bars_adjusted(..., as_of=target_date)` 就能拿到两根。
取不到复权价时**绝不**用不复权价顶替（`NOT_ADJUSTABLE`）。

指数（`sh000300`）走**不复权**价：指数没有分红送转，`adj_mode='none'` 就是它的真实点位
（`backtest/benchmark.py` 的既有口径），且它只做对照、不参与可评分性判定。
"""

from __future__ import annotations

import sqlite3
from collections.abc import Mapping, Sequence

from stocklab.backtest.benchmark import INDEX_300_SYMBOL
from stocklab.config.costs import CostModel
from stocklab.data import adjust
from stocklab.data.models import Bar
from stocklab.predict.store import payload_from_row
from stocklab.store import repo
from stocklab.verify.score import (ATTRIBUTION_DATA, ATTRIBUTION_UNDETERMINED,
                                   CAPITAL, parse_invalidate_bounds,
                                   score_prediction)
from stocklab.verify.store import insert_verification


class NoPredictions(RuntimeError):
    """该 `target_date` 一条预测都没有 —— 说明 `predict` 还没跑，不是「今天全对」。"""


def _parse_invalidate_bounds(text: str | None):
    """`(下界, 上界)` 或 `(None, ['invalidate_if'])`（给契约自洽性测试用）。"""
    bounds = parse_invalidate_bounds(text)
    return (bounds, []) if bounds else (None, ["invalidate_if"])


# ---------- 取数 ----------

def load_raw_bars(conn: sqlite3.Connection, code: str, upto: str, *,
                  cache=None) -> tuple[list[Bar], set[str]]:
    """`code` 在 `<= upto` 的**不复权**日 K + 其中的停牌日期集合。

    刻意读不复权表：日期与停牌状态与复权口径无关，而 `adj_factors` 只覆盖到
    因子链算得出的日期 —— 用它判「bar 存不存在」会把「复权链有缺口」误报成
    「没有这根 bar」，两种不可评分必须能区分。
    """
    if cache is None:
        from stocklab.predict.service import _read_raw

        bars, suspended = _read_raw(conn, code)
    else:
        bars, suspended = cache.raw_bars(conn, code)
    # 全量读一次、按日期过滤：回放时每天都做一次 SELECT 会把几千根 K 线重读几千遍
    keep = [b for b in bars if b.date <= upto]
    return keep, {d for d in suspended if d <= upto}


def load_scoring_bars(conn: sqlite3.Connection, code: str, target_date: str,
                      *, cache=None) -> tuple[list[Bar], list[Bar], set[str], str | None]:
    """返回 `(复权 bars, 不复权 bars, 停牌日集合, 复权层报错)`。

    复权层报错**不在这里抛**：把 `[]` 交给打分器，它会给出 `NOT_ADJUSTABLE` +
    `DATA` 归因，与「没有 bar」「停牌」在报告里长得完全不一样（这是刻意的：
    三种缺口的处置方式不同 —— 一个要修管道、一个要等复牌、一个要修因子链）。
    """
    raw, suspended = load_raw_bars(conn, code, target_date, cache=cache)
    floor = (cache.usable_from(conn, code) if cache is not None
             else adjust.usable_from(conn, code))
    try:
        if cache is None:
            adjusted = adjust.load_bars_adjusted(conn, code, target_date, start=floor)
        else:
            bars, chain = cache.chain(conn, code)
            adjust.assert_blackout_current(conn, code, chain)
            adjusted = adjust.adjust_bars(bars, chain, target_date, code=code,
                                          start=floor)
        return adjusted, raw, suspended, None
    except adjust.AdjustError as exc:
        return [], raw, suspended, f"{type(exc).__name__}: {exc}"


def index_pct_for(conn: sqlite3.Connection, asof: str, target: str,
                  symbol: str = INDEX_300_SYMBOL) -> float | None:
    """基准指数在 `[asof, target]` 的涨跌（不复权点位，**不含成本**）。

    缺数据 → `None`。指数缺失**不**影响预测可评分性：预测说的是个股，
    指数只是对照；把「指数没采到」变成「这只票不可评分」会白白丢掉真实样本。
    """
    rows = {r["date"]: r["close"] for r in conn.execute(
        "SELECT date, close FROM bars_daily WHERE code=? AND date IN (?, ?)",
        (symbol, asof, target))}
    a, t = rows.get(asof), rows.get(target)
    if not a or not t:
        return None
    return float(t) / float(a) - 1.0


# ---------- 组装 ----------

def verify_target(conn: sqlite3.Connection, target_date: str, *,
                  costs: CostModel | None = None, capital: float = CAPITAL,
                  codes: Sequence[str] | None = None,
                  model_version: str | None = None, cache=None,
                  now: str | None = None) -> dict:
    """给 `target_date` 的**全部**预测打分并落库。

    返回 `{"target_date", "rows", "storage", "by_model_version", "unscorable", "notes"}`，
    **不含任何时间戳** —— 同一份输入重复运行必须逐字段一致（幂等判定的前提）。

    ## `model_version`：只评**这一版**的预测（`None` = 全部版本）

    存在的理由不是「多一个参数」，是有一次真实碰撞：**回放要跨版本重跑**。
    换版本（`v1.0.1 → v1.0.2`）后重放整段历史，本意是「新版本写新行、旧版本原样留着」；
    但不带版本过滤时，`verify_target` 会把**已经评过分的旧版本行**按**新口径**重算一遍，
    而 `insert_verification` 的 append-only 守卫（正确）会当场报「同一份预测同一根 bar
    算出不同结果」。2026-09-21 实测就是这样撞上的（`pred_id=1831`，差异字段
    `actual_pct`/`benchmark_pct`/`sim_pnl`）。

    两个选择都是错的、只有这个是对的：① 连带重算旧版本 = 用新口径**篡改历史账本**；
    ② 只把冲突当噪声忽略 = 把守卫关掉。故：**回放只评自己那一批**，
    旧版本的验证行保留为「旧口径时期」的历史（与 `predictions` 的 append-only 语义一致）。
    """
    costs = costs or CostModel()
    sql = "SELECT * FROM predictions WHERE target_date=? AND status='ok'"
    params: list = [target_date]
    if codes:
        sql += f" AND code IN ({','.join('?' * len(codes))})"
        params.extend(codes)
    if model_version is not None:
        sql += " AND model_version=?"
        params.append(model_version)
    sql += " ORDER BY model_version, code"
    prows = conn.execute(sql, tuple(params)).fetchall()
    if not prows:
        raise NoPredictions(
            f"{target_date} 没有任何预测 —— 先跑 `predict run --asof <上一交易日>`，"
            "否则「没有预测」会被读成「今天不需要验证」")

    rows: list[dict] = []
    storage: dict[str, str] = {}
    for r in prows:
        pred = payload_from_row(r)
        pred["pred_id"] = int(r["pred_id"])
        adjusted, raw, suspended, err = load_scoring_bars(
            conn, r["code"], target_date, cache=cache)
        payload = score_prediction(
            pred, bars=adjusted, raw_bars=raw, suspended=suspended, costs=costs,
            capital=capital, adjust_error=err,
            index_pct=index_pct_for(conn, r["asof_date"], target_date))
        state, vid = insert_verification(conn, payload, now=now or repo.now_iso())
        storage[str(r["pred_id"])] = f"{state}:{vid}"
        # 报告用的元数据（**不在** `VERIFICATION_FIELDS` 里 → 不进内容 hash）
        rows.append({**payload, "code": r["code"], "asof_date": r["asof_date"],
                     "model_version": r["model_version"],
                     "verification_id": vid, "storage": state})

    return {
        "target_date": target_date,
        "rows": rows,
        "storage": storage,
        "by_model_version": _group(rows),
        "unscorable": [{"pred_id": r["pred_id"], "code": r["code"],
                        "model_version": r["model_version"],
                        "reason_code": r["reason_code"],
                        "reason": r["notes"]["reason"]}
                       for r in rows if not r["scorable"]],
        "notes": {
            "attribution": (
                f"程序只能判 `{ATTRIBUTION_DATA}`（数据缺口）。"
                f"`SIGNAL` / `STRATEGY` / `MODEL` / `NOISE` 一律写 "
                f"`{ATTRIBUTION_UNDETERMINED}` 并留 `attribution_manual` 给人工标注 —— "
                "代码判不了这四类，硬判等于造假归因（总纲 §9）"
            ),
            "unscorable": (
                "不可评分的预测**照样落库**（结果列全空 + `attribution_auto=DATA`），"
                "但**不计入任何成功分母** —— 结果列为 NULL 而不是 0 就是为了让统计层"
                "没有机会把它静默平均成「预测错了」"
            ),
            "index": (
                f"指数 `{INDEX_300_SYMBOL}` 走**不复权**点位（指数无分红送转）"
                "且**不含成本** —— 它不可直接交易，只作参照；"
                "与 `action` 直接可比的是同标的同日 buy_and_hold（含成本）"
            ),
        },
    }


def _group(rows: Sequence[Mapping]) -> dict[str, dict]:
    """按 `model_version` 分组计数。

    **v1.0.0 与 v1.0.1 必须各自成组**：v1.0.0 含已知 bug 的错误预测，
    把它混进 v1.0.1 会同时污染两个版本的准确率 —— 既掩盖 bug 的影响，
    也把修好之后的成绩拉低。分组是这里唯一的防线。
    """
    groups: dict[str, dict] = {}
    for row in rows:
        g = groups.setdefault(row["model_version"], {
            "n": 0, "scorable": 0, "unscorable": 0, "pred_ids": [],
            "unscorable_reasons": {}})
        g["n"] += 1
        g["pred_ids"].append(row["pred_id"])
        if row["scorable"]:
            g["scorable"] += 1
        else:
            g["unscorable"] += 1
            g["unscorable_reasons"][row["reason_code"]] = (
                g["unscorable_reasons"].get(row["reason_code"], 0) + 1)
    for g in groups.values():
        g["pred_ids"].sort()
    return groups
