"""趋势状态标签的**正式版验证**：M1 条件命中率 / M2 状态延续率 / F1·F2·F3 判定。

预注册（**只读**）：`docs/experiments/2026-09-15-trend-state-hit-rate.md`。
本模块是它的执行层 —— 口径一个字都不自己发明；预注册没写死、实现必须选一个的
地方，全部在 `docs/plans/2026-09-16-trend-state-hit-rate.md` §4 里**跑之前定死**
（L1–L7），报告里逐条打印。

## 度量口径（三条纪律）

1. **按日聚类，且聚类发生在「比较」这一步**（`metrics.py` 模块 docstring 的纪律）。
   同一交易日的所有标的进同一簇：A 股同涨同跌，2 只标的同一天不是 2 个独立样本。
   判别式写成了测试：把同一天的样本**复制几份**（日级均值不变），
   日聚类区间必须一字不变 —— 按行重采样会让它变窄，那是我们防的方向。
2. **配对在同一天内做**：`Δ_d = 当天条件命中率 − 当天无条件命中率`，
   再对 `Δ_d` 序列按日 bootstrap。两边吃的是**同一批行**，
   当天全市场共同的涨跌在相减时被消掉。
3. **行按实现日 `t+N` 归段**（L6）：P8 按「打分日」归段，这里的打分日就是 `t+N`。
   若按信号日 `t` 归段，validate 最后 5 天的标签会吃到 test 段的价格 ——
   跨段标签泄漏，等价于没有 embargo。

## 判定顺序（照抄预注册 §1，不许重排）

```
有效交易日 < 120            → inconclusive，test 不打开（F3）
(a) 或 (b) 至少一条成立      → validate 达标 → 允许打开 test 复核（只读一次）
两条皆不成立                 → falsified，test 不打开
```

- **(a)**：UP 与 DOWN **两侧**的 `Δ`（条件命中率 − 无条件命中率）日聚类 95% CI
  **下界均 > 0**（F1 的「任一」= 有一侧不成立就整体不成立）；
- **(b)**：UP 与 DOWN **两侧**的延续率差值日聚类 95% CI **下界均 > 0**（F2 + L5）。

打开 test 后：validate 成立的判据**在 test 上同样成立** → `WIN`；否则 `falsified`
（沿用 `metrics.decide` 的纪律：样本外的胜利没能复现 → 不许嘴硬）。
`keep_test_sealed=True`（CLI `--keep-test-sealed`）只会更保守：即使达标也不读 test，
记 `inconclusive`，把「是否打开」留给下一轮。

## 一行都不写生产表

只 `SELECT` `bars_daily`（`adj_mode='none'`，不复权）与 `raw_fetch_cache`（只读哈希）。
实验产物全在内存与 `reports/` 里。
"""

from __future__ import annotations

import hashlib
import json
import random
import sqlite3
import statistics
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from stocklab.experiments import metrics
from stocklab.experiments.split import SPLIT_NAMES, SplitConfig, boundaries, split_days
from stocklab.trend.state import (HORIZON, MA_LONG, MA_SHORT, STATES, STATE_DOWN,
                                  STATE_FLAT, STATE_UP, TREND_STATES,
                                  state_series)

#: 预注册文档（唯一口径来源；本模块只读它）。
PREREG_DOC = "docs/experiments/2026-09-15-trend-state-hit-rate.md"

#: 预注册标的（§3）：`000333` / `600690` / `sh000300`（指数作对照）。
#: **硬编码**，不从 `instruments` 读 —— 库里后来新增的 ETF 属于扩大样本量，
#: 那是改设计、需另立预注册（任务硬约束 1）。传入别的代码直接拒收。
PREREG_CODES: tuple[str, ...] = ("000333", "600690", "sh000300")

#: bootstrap 的重采样次数与种子：**复用 P8 的先验固定值**（写死 → 可复现）。
BOOTSTRAP_N: int = metrics.BOOTSTRAP_N
BOOTSTRAP_SEED: int = metrics.BOOTSTRAP_SEED

#: 样本量门槛（交易日）：与 `verify.report.MIN_DAYS` **同源同值**，不另写一个 120。
MIN_DAYS: int = metrics.MIN_DAYS

#: 本模块的度量口径版本（与 P8 的 `METRIC_VERSION` 是两套：那边的配对指标是
#: 方向/Brier/覆盖/超额，这边是命中率与延续率）。报告里两个都写。
TREND_METRIC_VERSION = "trend-state-v1"

#: 预注册 §4.1 的探索性读数（**非结论**，只用于口径自检：我的实现是否读对了口径）。
#: 键 = (状态, 指标)：`hit` = 全样本条件命中率，`persist` = 延续率。
PREREG_EXPLORE = {
    "n_rows": 13972,
    "hit": {STATE_UP: 0.5199, STATE_DOWN: 0.4844},
    "baseline": {STATE_UP: 0.5125, STATE_DOWN: 0.4816},
    "persist": {"000333": {STATE_UP: 0.7185, STATE_DOWN: 0.6820, STATE_FLAT: 0.6125},
                "600690": {STATE_UP: 0.7365, STATE_DOWN: 0.7287, STATE_FLAT: 0.5996},
                "sh000300": {STATE_UP: 0.7251, STATE_DOWN: 0.6789, STATE_FLAT: 0.5680}},
}


class AdjustModeError(RuntimeError):
    """标的没有**不复权**日线 —— 预注册 §3 要求不复权，拒绝拿复权价冒充。"""


class PreregViolation(ValueError):
    """请求的标的/参数超出预注册 —— 改设计必须另立预注册文档。"""


class UnmappedRows(RuntimeError):
    """有行的实现日落在交易日轴之外，无法归段 —— 拒绝静默丢样本。"""


@dataclass(frozen=True)
class Row:
    """一个 `(标的, 信号日)` 样本。

    `date`/`close`/`state` 是**信号日 t** 的；`realization_date`/`forward_return`/
    `hit`/`state_next`/`same_state` 是**实现日 t+N** 的。归段与聚类都按实现日。
    """

    code: str
    date: str
    close: float
    state: str
    realization_date: str
    forward_return: float
    hit: int
    tie: bool
    state_next: str
    same_state: int

    def as_dict(self) -> dict:
        return {"code": self.code, "date": self.date, "close": self.close,
                "state": self.state, "realization_date": self.realization_date,
                "forward_return": self.forward_return, "hit": self.hit,
                "tie": self.tie, "state_next": self.state_next,
                "same_state": self.same_state}


# ---------- 取数 ----------

def load_closes(conn: sqlite3.Connection, code: str, *,
                from_date: str | None = None, to_date: str | None = None
                ) -> list[tuple[str, float]]:
    """该标的**不复权**（`adj_mode='none'`）日线收盘，按日期升序。

    不复权是预注册 §3 写死的口径；库里只有复权行时必须**报错**，
    不许退化成「有什么用什么」—— 那会让报告里的口径与预注册不符，却看不出来。
    """
    sql = ("SELECT date, close FROM bars_daily WHERE code=? AND adj_mode='none'")
    args: list[Any] = [code]
    if from_date:
        sql += " AND date >= ?"
        args.append(from_date)
    if to_date:
        sql += " AND date <= ?"
        args.append(to_date)
    sql += " ORDER BY date"
    rows = [(r["date"], float(r["close"])) for r in conn.execute(sql, args)]
    if rows:
        return rows
    # 空结果有两种完全不同的原因，**不许合并**（ERROR_DIARY：「为空时是
    # 『没有』还是『没填』？」）：区间外有数据 → 区间选错了；
    # 一行不复权都没有 → 口径不满足。分别给出各自的错。
    n_none = conn.execute(
        "SELECT COUNT(*) c FROM bars_daily WHERE code=? AND adj_mode='none'",
        (code,)).fetchone()["c"]
    total = conn.execute("SELECT COUNT(*) c FROM bars_daily WHERE code=?",
                         (code,)).fetchone()["c"]
    if n_none:
        raise ValueError(
            f"{code} 在 [{from_date}, {to_date}] 内没有日线 —— 该区间的交易日轴为空"
            f"（库里共有 {n_none} 行不复权日线，是区间选错了，不是标的没数据）"
        )
    if total:
        raise AdjustModeError(
            f"{code} 有 {total} 行日线，但没有一行是 adj_mode='none'（不复权）—— "
            "预注册 §3 要求不复权收盘，拒绝拿复权价冒充"
        )
    raise ValueError(f"库里没有 {code} 的任何日线（先跑采集/回填）")


# ---------- 行构造 ----------

def make_rows(code: str, series: Sequence[tuple[str, float]], *,
              horizon: int = HORIZON, ma_short: int = MA_SHORT,
              ma_long: int = MA_LONG) -> list[Row]:
    """把一条收盘序列变成样本行（**PIT**：状态只用 `≤ t` 的收盘）。

    - 状态算不出来的点（不足 `ma_long` 根）不是样本；
    - `t+horizon` 越过序列末端的点也**不是样本**（不是记 0 ——
      「还没到期」和「到期没中」是两件事）；
    - 前向收益只用 `t+horizon` 的收盘做**标签**，不参与状态。
    """
    dates = [d for d, _ in series]
    closes = [c for _, c in series]
    states = state_series(closes, ma_short=ma_short, ma_long=ma_long)
    out: list[Row] = []
    for i, st in enumerate(states):
        if st is None:
            continue
        j = i + horizon
        if j >= len(closes):
            break
        prev, fwd = closes[i], closes[j]
        out.append(Row(
            code=code, date=dates[i], close=prev, state=st,
            realization_date=dates[j], forward_return=(fwd - prev) / prev,
            hit=1 if fwd > prev else 0, tie=fwd == prev,
            state_next=states[j], same_state=1 if states[j] == st else 0,
        ))
    return out


# ---------- 按日聚类的 bootstrap ----------

def _boot_ci(daily: Sequence[float], *, n_boot: int = BOOTSTRAP_N,
             seed: int = BOOTSTRAP_SEED) -> list[float] | None:
    """对**日级值**做有放回重采样的 95% 分位 CI（与 `metrics.bootstrap_daily_ci`
    同一个估计量：同样的重采样次数、同样的分位下标约定）。

    `len(daily) < 2` → `None` —— 不编造一个宽度。
    """
    n = len(daily)
    if n < 2:
        return None
    rng = random.Random(seed)
    boots = []
    for _ in range(n_boot):
        draw = [daily[rng.randrange(n)] for _ in range(n)]
        boots.append(statistics.fmean(draw))
    boots.sort()
    lo = boots[max(0, int(0.025 * n_boot) - 1)]
    hi = boots[min(n_boot - 1, int(0.975 * n_boot))]
    return [lo, hi]


def _by_day(rows: Sequence[Row]) -> dict[str, list[Row]]:
    out: dict[str, list[Row]] = {}
    for r in rows:
        out.setdefault(r.realization_date, []).append(r)
    return out


def _delta_block(sub: Sequence[Row], all_rows: Sequence[Row], *,
                 day_value, base_value, n_boot: int, seed: int) -> dict:
    """按日配对：`Δ_d = day_value(当天子集) − base_value(当天全部行)`。

    `day_value` / `base_value` 都是以「一天的行」为输入的标量函数。
    两边吃同一批日 —— 当天全市场共同的涨跌在相减时被消掉。
    """
    sub_by_day = _by_day(sub)
    all_by_day = _by_day(all_rows)
    deltas = [day_value(sub_by_day[d]) - base_value(all_by_day[d])
              for d in sorted(sub_by_day)]
    return {
        "n_days": len(deltas),
        "delta": statistics.fmean(deltas) if deltas else None,
        "delta_ci95": _boot_ci(deltas, n_boot=n_boot, seed=seed),
        "daily_delta": deltas,
        "n_boot": n_boot, "seed": seed,
        "method": "day_clustered_percentile_bootstrap",
    }


def _hit_rate(rows: Sequence[Row]) -> float:
    return statistics.fmean([r.hit for r in rows])


def _persist_rate(rows: Sequence[Row]) -> float:
    return statistics.fmean([r.same_state for r in rows])


def evaluate_segment(rows: Sequence[Row], *, label: str = "",
                     n_boot: int = BOOTSTRAP_N, seed: int = BOOTSTRAP_SEED) -> dict:
    """一个分段的 M1 / M2（三态各一份），全部按日聚类。

    返回 `{"label", "n_rows", "n_days", "codes", "n_ties", "M1", "M2"}`。
    `M1[S]` / `M2[S]` 的键：`n_rows` / `n_days` / 自身比率 / 无条件基线 /
    `delta` / `delta_ci95` / `by_code`（逐标的，仅登记）。
    """
    all_rows = list(rows)
    days = sorted({r.realization_date for r in all_rows})
    out: dict[str, Any] = {
        "label": label, "n_rows": len(all_rows), "n_days": len(days),
        "codes": sorted({r.code for r in all_rows}),
        "n_ties": sum(1 for r in all_rows if r.tie),
        "M1": {}, "M2": {},
    }
    if not all_rows:
        return out

    m1: dict[str, Any] = {}
    m2: dict[str, Any] = {}
    for state in STATES:
        sub = [r for r in all_rows if r.state == state]
        block = {"n_rows": len(sub), "state": state,
                 "hit_rate": _hit_rate(sub) if sub else None,
                 "baseline_rate": _hit_rate(
                     [r for r in all_rows if r.realization_date in
                      {x.realization_date for x in sub}]) if sub else None,
                 "by_code": {}}
        block.update(_delta_block(sub, all_rows, day_value=_hit_rate,
                                  base_value=_hit_rate, n_boot=n_boot, seed=seed))
        if sub:
            for code in sorted({r.code for r in sub}):
                cs = [r for r in sub if r.code == code]
                block["by_code"][code] = {
                    "n_rows": len(cs), "hit_rate": _hit_rate(cs),
                    "baseline_rate": _hit_rate(
                        [r for r in all_rows if r.code == code and
                         r.realization_date in {x.realization_date for x in cs}]),
                }
        m1[state] = block

        freq = len(sub) / len(all_rows)
        pblock = {"n_rows": len(sub), "state": state,
                  "persistence": _persist_rate(sub) if sub else None,
                  "baseline_freq": freq,
                  "by_code": {}}
        pblock.update(_delta_block(
            sub, all_rows,
            day_value=_persist_rate,
            base_value=lambda day: len([r for r in day if r.state == state]) / len(day),
            n_boot=n_boot, seed=seed))
        if sub:
            for code in sorted({r.code for r in sub}):
                cs = [r for r in sub if r.code == code]
                pblock["by_code"][code] = {
                    "n_rows": len(cs), "persistence": _persist_rate(cs),
                    "baseline_freq": len(cs) / len([r for r in all_rows
                                                    if r.code == code]),
                }
        m2[state] = pblock
    out["M1"], out["M2"] = m1, m2
    return out


# ---------- 判定 ----------

def _side_holds(block: Mapping[str, Any], key: str) -> bool:
    """某一侧的判据是否成立：`key` 的 CI **下界 > 0**。

    CI 为 `None`（有效日 < 2）→ 不成立：算不出区间就不许说「显著」。
    """
    ci = block.get(key)
    return bool(ci and ci[0] > 0)


def criteria_of(seg: Mapping[str, Any]) -> dict[str, bool]:
    """判据 (a)/(b)：**UP 与 DOWN 两侧都要成立**（F1/F2 的「任一」= 全称）。"""
    m1, m2 = seg.get("M1", {}), seg.get("M2", {})
    a = all(_side_holds(m1.get(s, {}), "delta_ci95") for s in TREND_STATES)
    b = all(_side_holds(m2.get(s, {}), "delta_ci95") for s in TREND_STATES)
    return {"a": bool(a), "b": bool(b)}


def _fmt_side(block: Mapping[str, Any], key: str) -> str:
    d, ci = block.get("delta"), block.get(key)
    if d is None or ci is None:
        return "样本不足（算不出区间）"
    return f"Δ={d:+.4f}，95% CI [{ci[0]:+.4f}, {ci[1]:+.4f}]"


def judge(*, validate: Mapping[str, Any], days: int | None = None,
          min_days: int = MIN_DAYS, test: Mapping[str, Any] | None = None,
          test_days: int | None = None) -> dict:
    """F1/F2/F3 + 达标规则 → `WIN` / `falsified` / `inconclusive` / `pending_test`。

    `pending_test` 不是最终结论：它表示「validate 达标，允许打开 test」，
    由调用方（`run_evaluation`）决定是否真的打开。
    """
    n_days = days if days is not None else int(validate.get("n_days") or 0)
    crit = criteria_of(validate)
    reasons: list[str] = []
    m1, m2 = validate["M1"], validate["M2"]
    for s in TREND_STATES:
        reasons.append(f"(a) {s} 条件命中率 − 无条件基线：{_fmt_side(m1[s], 'delta_ci95')}")
    for s in TREND_STATES:
        reasons.append(f"(b) {s} 延续率 − 该状态无条件频率：{_fmt_side(m2[s], 'delta_ci95')}")

    base = {"criteria": crit, "n_days": n_days, "min_days": min_days,
            "test_evaluated": False}

    if n_days < min_days:
        return {**base, "status": "inconclusive", "requires_test": False,
                "reasons": [f"F3：validate 段有效交易日 {n_days} < 门槛 {min_days} —— "
                            "样本不足，仅供观察；不许当作「可以调参重跑」的理由",
                            *reasons]}
    if not (crit["a"] or crit["b"]):
        return {**base, "status": "falsified", "requires_test": False,
                "reasons": ["判据 (a) 与 (b) **皆不成立** → falsified："
                            "不打开 test、不进策略库、不进实盘", *reasons]}
    if test is None:
        held = [k for k in ("a", "b") if crit[k]]
        return {**base, "status": "pending_test", "requires_test": True,
                "reasons": [f"判据 {'、'.join(held)} 成立 → validate 达标，"
                            "允许打开 test 段复核一次", *reasons]}

    t_days = test_days if test_days is not None else int(test.get("n_days") or 0)
    t_crit = criteria_of(test)
    base["test_evaluated"] = True
    base["test_n_days"] = t_days
    if t_days < min_days:
        return {**base, "status": "inconclusive", "requires_test": True,
                "reasons": [f"test 段有效交易日 {t_days} < 门槛 {min_days} —— "
                            "封存段的样本不足以复核，结论只能是 inconclusive",
                            *reasons]}
    held = [k for k in ("a", "b") if crit[k]]
    missing = [k for k in held if not t_crit[k]]
    if missing:
        return {**base, "status": "falsified", "requires_test": True,
                "test_criteria": t_crit,
                "reasons": [f"validate 成立的判据 {'、'.join(held)} 在 **test 上没能复现**"
                            f"（未复现：{'、'.join(missing)}）→ falsified："
                            "样本外的胜利没能复现，不许嘴硬", *reasons]}
    return {**base, "status": "WIN", "requires_test": True, "test_criteria": t_crit,
            "reasons": [f"validate 与 test **两段都**满足判据 {'、'.join(held)} → WIN",
                        *reasons]}


# ---------- 数据快照 ----------

def snapshot_hash(series_by_code: Mapping[str, Sequence[tuple[str, float]]]) -> str:
    """行情快照哈希：`code|date|close` 逐行拼接后 sha256（**确定性**）。"""
    h = hashlib.sha256()
    for code in sorted(series_by_code):
        for date, close in series_by_code[code]:
            h.update(f"{code}|{date}|{close!r}\n".encode())
    return h.hexdigest()


def raw_cache_hash(conn: sqlite3.Connection) -> dict:
    """`raw_fetch_cache` 的内容哈希（预注册 §3：快照以它当前内容为准）。"""
    try:
        rows = list(conn.execute(
            "SELECT cache_id, content_sha256 FROM raw_fetch_cache ORDER BY cache_id"))
    except sqlite3.OperationalError as exc:
        return {"n_entries": 0, "sha256": None, "error": f"{type(exc).__name__}: {exc}"}
    h = hashlib.sha256()
    for r in rows:
        h.update(f"{r['cache_id']}|{r['content_sha256']}\n".encode())
    return {"n_entries": len(rows), "sha256": h.hexdigest(), "error": None}


# ---------- 全流程 ----------

def _rows_for(series_by_code: Mapping[str, Sequence[tuple[str, float]]], *,
              horizon: int, ma_short: int, ma_long: int) -> list[Row]:
    out: list[Row] = []
    for code in sorted(series_by_code):
        out.extend(make_rows(code, series_by_code[code], horizon=horizon,
                             ma_short=ma_short, ma_long=ma_long))
    return out


def run_evaluation(conn: sqlite3.Connection, *, from_date: str | None = None,
                   to_date: str | None = None, split_config: SplitConfig | None = None,
                   min_days: int = MIN_DAYS, keep_test_sealed: bool = False,
                   codes: Sequence[str] = PREREG_CODES,
                   session_axis: Sequence[str] | None = None,
                   n_boot: int = BOOTSTRAP_N, seed: int = BOOTSTRAP_SEED) -> dict:
    """跑完整轮：取数 → 切段 → M1/M2 → 判定 → （达标才）打开 test → 报告 dict。

    **不含任何时间戳**：同参数两次运行的 md/json 哈希天然相等（幂等的证据）。
    """
    if tuple(codes) != PREREG_CODES and not set(codes) <= set(PREREG_CODES):
        raise PreregViolation(
            f"标的 {list(codes)!r} 超出预注册 {list(PREREG_CODES)!r} —— "
            f"扩大样本量属于改设计，须另立预注册（见 {PREREG_DOC} §3）"
        )
    cfg = split_config or SplitConfig()

    series = {c: load_closes(conn, c, from_date=from_date, to_date=to_date)
              for c in codes}
    series = {c: s for c, s in series.items() if s}
    if not series:
        raise ValueError("三个预注册标的在指定区间内都没有不复权日线")

    axis = list(session_axis) if session_axis is not None else _session_axis(conn, codes)
    axis = [d for d in axis if (not from_date or d >= from_date)
            and (not to_date or d <= to_date)]
    if not axis:
        raise ValueError(f"[{from_date}, {to_date}] 内没有交易日")
    seg = split_days(axis, cfg)

    rows = _rows_for(series, horizon=HORIZON, ma_short=MA_SHORT, ma_long=MA_LONG)
    axis_set = set(axis)
    outside = [r for r in rows if r.realization_date not in axis_set]
    rows = [r for r in rows if r.realization_date in axis_set]
    if not rows:
        raise UnmappedRows("没有任何样本行的实现日落在交易日轴内 —— 检查日历与行情覆盖")

    splits: dict[str, Any] = {}
    train_rows = [r for r in rows if r.realization_date in set(seg["train"])]
    validate_rows = [r for r in rows if r.realization_date in set(seg["validate"])]
    if not train_rows or not validate_rows:
        raise UnmappedRows(
            f"切分后 train/validate 里有空段（train={len(train_rows)}、"
            f"validate={len(validate_rows)} 行）—— 拒绝产出「某一段没跑」的报告"
        )
    splits["train"] = evaluate_segment(train_rows, label="train",
                                       n_boot=n_boot, seed=seed)
    validate_metrics = evaluate_segment(validate_rows, label="validate",
                                        n_boot=n_boot, seed=seed)
    splits["validate"] = validate_metrics

    verdict = judge(validate=validate_metrics, days=validate_metrics["n_days"],
                    min_days=min_days)
    test_evaluated = False
    test_reason: str | None = None
    if verdict["requires_test"] and not keep_test_sealed:
        test_rows = [r for r in rows if r.realization_date in set(seg["test"])]
        test_metrics = evaluate_segment(test_rows, label="test",
                                        n_boot=n_boot, seed=seed)
        splits["test"] = test_metrics
        verdict = judge(validate=validate_metrics, days=validate_metrics["n_days"],
                        min_days=min_days, test=test_metrics,
                        test_days=test_metrics["n_days"])
        test_evaluated = True
    else:
        test_reason = _sealed_reason(verdict, keep_test_sealed)
        if verdict["requires_test"] and keep_test_sealed:
            verdict = {**verdict, "status": "inconclusive", "requires_test": True,
                       "reasons": [
                           "validate 段达标，但本轮声明**封存 test**"
                           f"（`keep_test_sealed=True`）→ 记 inconclusive，"
                           "「是否打开 test」留给下一轮复现评审", *verdict["reasons"]]}

    rep: dict[str, Any] = {
        "trend_metric_version": TREND_METRIC_VERSION,
        "framework_metric_version": metrics.METRIC_VERSION,
        "prereg_doc": PREREG_DOC,
        "prereg_codes": list(PREREG_CODES),
        "universe": sorted(series),
        "hypothesis": ("仅用 t 日及之前收盘可算出的双均线趋势状态（UP/DOWN/FLAT）"
                       "对标的打标签，「未来 5 个交易日收益方向」的条件命中率显著高于"
                       "无条件基线，且状态自身具有显著延续性"),
        "horizon": HORIZON,
        "ma": {"short": MA_SHORT, "long": MA_LONG},
        "state_definition": {
            "UP": "close_t > MA20_t 且 MA20_t > MA60_t",
            "DOWN": "close_t < MA20_t 且 MA20_t < MA60_t",
            "FLAT": "其余（无分位阈值、无平滑、无迟滞）",
            "note": "三条不等式都是**严格**的；均线含当日收盘",
        },
        "price_mode": "不复权收盘（bars_daily.adj_mode='none'，PIT）",
        "range": {"from": axis[0], "to": axis[-1], "n_days": len(axis),
                  "from_arg": from_date, "to_arg": to_date},
        "split_config": cfg.as_dict(),
        "split_boundaries": boundaries(seg),
        "row_key": "realization_date（= 信号日 t + N；归段与聚类都按它，L6）",
        "selection_split": "validate",
        "splits": splits,
        "verdict": verdict,
        "test_evaluated": test_evaluated,
        "test_not_evaluated_reason": test_reason,
        "bootstrap": {"n_boot": n_boot, "seed": seed,
                      "method": "day_clustered_percentile_bootstrap",
                      "note": "按**交易日**重采样，不按行；种子写死 → 同输入同区间"},
        "data_snapshot": {
            "bars_sha256": snapshot_hash(series),
            "bars_rows": sum(len(s) for s in series.values()),
            "raw_cache": raw_cache_hash(conn),
            "unmapped_rows_outside_axis": len(outside),
            "note": ("bars_sha256 = 逐行 `code|date|close` 的 sha256（只含本次读入的行）；"
                     "raw_cache = raw_fetch_cache 的 (cache_id, content_sha256) 摘要"),
        },
        "counts": {"rows_total": len(rows) + len(outside),
                   "rows_used": len(rows),
                   "train_rows": len(train_rows), "validate_rows": len(validate_rows),
                   "test_rows": len(splits["test"]) if test_evaluated else 0},
        "robustness": _robustness(series, seg, n_boot=n_boot, seed=seed,
                                  horizon=HORIZON, ma_short=MA_SHORT, ma_long=MA_LONG),
        "selfcheck": _selfcheck(rows),
        "criteria_landing": _CRITERIA_LANDING,
        "notes": _NOTES,
    }
    md = render_markdown(rep)
    rep["report_sha256"] = hashlib.sha256(md.encode("utf-8")).hexdigest()
    return rep


def _session_axis(conn: sqlite3.Connection, codes: Sequence[str]) -> list[str]:
    """交易日轴：日历 ∩ 行情（与 P8 的 `session_dates` 同源）。"""
    from stocklab.verify.replay import session_dates

    return session_dates(conn, list(codes))


def _sealed_reason(verdict: Mapping[str, Any], keep_test_sealed: bool) -> str:
    """test 没被评估的**真实**原因（不许把「没达标」说成「策略封存」）。"""
    status = verdict["status"]
    if status == "pending_test" and keep_test_sealed:
        return ("validate 段**达标**，但本轮声明封存 test（`keep_test_sealed=True`）→ "
                "封存段不打开；「是否开 test」留给下一轮复现评审")
    return (f"validate 段判定 = `{status}`（未达标）→ 封存段不打开。"
            "这正是纪律要求的：validate 输了就不许再看 test，"
            "否则「用 test 挑口径」会以「我只是看一眼」的形式发生")


def _robustness(series: Mapping[str, Sequence[tuple[str, float]]],
                seg: Mapping[str, Sequence[str]], *, n_boot: int, seed: int,
                horizon: int, ma_short: int, ma_long: int) -> list[dict]:
    """稳健性附表（预注册 §2）：**只能看，不得据此改判据或宣布达标**。"""
    variants = ([("N=1", 1, ma_short, ma_long), ("N=10", 10, ma_short, ma_long),
                 ("N=20", 20, ma_short, ma_long),
                 ("MA(10,30)", horizon, 10, 30), ("MA(5,20)", horizon, 5, 20)])
    validate_days = set(seg["validate"])
    out: list[dict] = []
    for label, n, ms, ml in variants:
        rows = [r for r in _rows_for(series, horizon=n, ma_short=ms, ma_long=ml)
                if r.realization_date in validate_days]
        metrics_block = evaluate_segment(rows, label=label, n_boot=n_boot, seed=seed)
        out.append({
            "label": label, "horizon": n, "ma": {"short": ms, "long": ml},
            "n_rows": metrics_block["n_rows"], "n_days": metrics_block["n_days"],
            "M1": {s: {k: metrics_block["M1"][s][k]
                       for k in ("delta", "delta_ci95", "n_days")} for s in STATES},
            "M2": {s: {k: metrics_block["M2"][s][k]
                       for k in ("delta", "delta_ci95", "n_days")} for s in STATES},
            "note": "只作稳健性参考，**不许**据此改判据或宣布达标（预注册 §2）",
        })
    return out


def _selfcheck(rows: Sequence[Row]) -> dict:
    """口径自检（**全样本、非结论**）：我的实现读对了预注册 §4.1 的口径吗？

    全样本含 validate/test 两段、无 walk-forward 隔离 → **不构成任何采纳依据**。
    它唯一的用途是：如果这里的数字与预注册 §4.1 的探索性读数差得远，
    说明我理解的口径与预注册作者不是同一个，必须先查清楚再谈结论。
    """
    full = evaluate_segment(rows, label="full-sample-selfcheck")
    return {
        "warning": "全样本、无 walk-forward 隔离 —— **非结论**，不得引用为能力证明",
        "n_rows": full["n_rows"], "n_days": full["n_days"],
        "hit": {s: full["M1"][s]["hit_rate"] for s in STATES},
        "baseline": {s: full["M1"][s]["baseline_rate"] for s in STATES},
        "persist_by_code": {s: {c: b["persistence"]
                                for c, b in full["M2"][s]["by_code"].items()}
                            for s in STATES},
        "prereg_reading": PREREG_EXPLORE,
        "prereg_reading_source": f"{PREREG_DOC} §4.1（探索性读数，非结论）",
    }


#: 预注册没写死、实现必须选一个的点 —— 逐条打印进报告（跑之前定死，见 plan §4）。
_CRITERIA_LANDING: dict[str, str] = {
    "L1 命中定义": "hit = 1 ⟺ close_{t+5} > close_t（不复权）；无 FLAT 带；"
                   "价格完全相等的平局计 0 并单独计数（报告里的 n_ties）",
    "L2 聚类单位": "交易日（实现日）；同一天的所有标的/状态进同一簇；"
                   "bootstrap 按日重采样，不按行",
    "L3 F1 的 Δ": "当日配对：Δ_d = mean(hit|state=S, 日 d) − mean(hit|全部行, 日 d)，"
                  "再对 Δ_d 按日 bootstrap（与 metrics.paired_daily_delta 同一纪律）",
    "L4 F2 的基线": "无条件频率 = 当日 state=S 的行占比；延续率差值同样当日配对",
    "L5 F2 的判定范围": "UP 与 DOWN 两侧都要成立（与 F1 的「两侧」对称）；FLAT 只登记",
    "L6 归段键": "实现日 t+N（P8 按打分日归段；此处打分日=t+N），"
                 "避免 validate 的标签吃到 test 段价格",
    "L7 标的": "硬编码预注册三标的；传入其它代码直接拒收（扩大样本量须另立预注册）",
}

_NOTES: dict[str, str] = {
    "not_persisted": "本报告每一行都是内存里的评估结果 —— 一行都没写进生产表"
                     "（只读 bars_daily / raw_fetch_cache）",
    "effective_n": "有效样本量按**交易日**计；行数只作参考，不得当样本量",
    "sealed_test": "test 段是封存段：validate 未达标就**根本不算**（不是「算了不报」），"
                   "达标才打开一次",
    "no_rescue": "跑输 / 不显著一律如实写：不许事后换窗口、换指标、换分位阈值",
}


# ---------- 呈现 ----------

def _pct(x: float | None) -> str:
    return "—" if x is None else f"{x * 100:.2f}%"


def _ci(ci: Sequence[float] | None) -> str:
    return "—" if not ci else f"[{ci[0]:+.4f}, {ci[1]:+.4f}]"


def _m1_table(m: Mapping[str, Any]) -> list[str]:
    L = ["| 状态 | 样本行 | 有效交易日 | 条件命中率 | 无条件基线 | Δ（日均） | 按日聚类 95% CI |",
         "|------|-------:|-----------:|-----------:|-----------:|-----------:|-----------------|"]
    for s in STATES:
        b = m["M1"][s]
        L.append(f"| {s} | {b['n_rows']} | {b['n_days']} | {_pct(b['hit_rate'])} | "
                 f"{_pct(b['baseline_rate'])} | {_pct(b['delta'])} | {_ci(b['delta_ci95'])} |")
    return L


def _m2_table(m: Mapping[str, Any]) -> list[str]:
    L = ["| 状态 | 样本行 | 延续率 P(state_{t+5}=state_t) | 该状态无条件频率 | Δ（日均） | 按日聚类 95% CI |",
         "|------|-------:|---------------------------:|-----------------:|-----------:|-----------------|"]
    for s in STATES:
        b = m["M2"][s]
        L.append(f"| {s} | {b['n_rows']} | {_pct(b['persistence'])} | "
                 f"{_pct(b['baseline_freq'])} | {_pct(b['delta'])} | "
                 f"{_ci(b['delta_ci95'])} |")
    return L


def _by_code_table(m: Mapping[str, Any], key: str, value: str) -> list[str]:
    codes = sorted({c for s in STATES for c in m["M1"][s]["by_code"]})
    L = [f"| 标的 | 状态 | {key} | 样本行 |", "|------|------|------:|-------:|"]
    for code in codes:
        for s in STATES:
            b = m["M1"][s]["by_code"].get(code)
            if b:
                L.append(f"| {code} | {s} | {_pct(b[value])} | {b['n_rows']} |")
    return L


def render_markdown(rep: Mapping[str, Any]) -> str:
    """把报告渲染成 markdown（**确定性**：不含生成时间、不含自身 sha256）。"""
    v = rep["verdict"]
    L: list[str] = []
    A = L.append
    A(f"# 正式版实验报告：趋势状态标签（双均线三态）命中率与延续率")
    A("")
    A(f"> {rep['hypothesis']}")
    A("")
    A(f"- **预注册（唯一口径来源，只读）** `{rep['prereg_doc']}`")
    A(f"- **口径版本** `{rep['trend_metric_version']}`（度量层自研）；"
      f"框架口径 `{rep['framework_metric_version']}`")
    A(f"- **状态定义** UP：`{rep['state_definition']['UP']}`；"
      f"DOWN：`{rep['state_definition']['DOWN']}`；FLAT：{rep['state_definition']['FLAT']}"
      f"（{rep['state_definition']['note']}）")
    A(f"- **主视界** N = {rep['horizon']} 交易日；**均线** MA{rep['ma']['short']} / "
      f"MA{rep['ma']['long']}（含当日收盘，无平滑无迟滞）")
    A(f"- **标的** {rep['universe']}（预注册三标的：{'、'.join(rep['prereg_codes'])}；"
      f"指数作对照。**不含** ETF —— 扩大样本量须另立预注册）")
    A(f"- **价格口径** {rep['price_mode']}")
    A(f"- **区间** {rep['range']['from']} → {rep['range']['to']}"
      f"（{rep['range']['n_days']} 个交易日）")
    A(f"- **归段键** {rep['row_key']}")
    A(f"- **bootstrap** 种子 `{rep['bootstrap']['seed']}`、次数 "
      f"`{rep['bootstrap']['n_boot']}`（{rep['bootstrap']['note']}）")
    A("")
    A("## 1. 三段切分（按日期连续不重叠）")
    A("")
    A(f"配置：train={rep['split_config']['train']} / validate="
      f"{rep['split_config']['validate']} / test={rep['split_config']['test']}")
    A("")
    A("| 段 | 首日 | 末日 | 交易日数 | 本次是否评估 |")
    A("|----|------|------|---------:|-------------|")
    for name in SPLIT_NAMES:
        b = rep["split_boundaries"][name]
        evaluated = ("是" if name in rep["splits"] else
                     "**否**（封存段不打开，原因见 §4）")
        A(f"| {name} | {b['first_date']} | {b['last_date']} | {b['n_days']} | {evaluated} |")
    A("")
    A(f"- 有效交易日（行数）：train {rep['counts']['train_rows']}、"
      f"validate {rep['counts']['validate_rows']}、test {rep['counts']['test_rows']}")
    A(f"- 落在交易日轴之外、**未参与**的行："
      f"{rep['data_snapshot']['unmapped_rows_outside_axis']}（显式计数，不静默丢弃）")
    A("")
    A("## 2. validate 段结果（判定只用这一段）")
    A("")
    A(f"有效交易日 **{rep['splits']['validate']['n_days']}** 天"
      f"（门槛 {v['min_days']}）；样本行 {rep['splits']['validate']['n_rows']}；"
      f"平局（价格完全相等）{rep['splits']['validate']['n_ties']} 行")
    A("")
    A("### 2.1 M1 条件命中率（未来 5 日方向）vs 无条件基线")
    A("")
    L.extend(_m1_table(rep["splits"]["validate"]))
    A("")
    A("### 2.2 M2 状态延续率")
    A("")
    L.extend(_m2_table(rep["splits"]["validate"]))
    A("")
    A("### 2.3 逐标的（M1 条件命中率）")
    A("")
    L.extend(_by_code_table(rep["splits"]["validate"], "条件命中率", "hit_rate"))
    A("")
    A("### 2.4 逐标的（M2 延续率）")
    A("")
    codes = sorted({c for s in STATES
                    for c in rep["splits"]["validate"]["M2"][s]["by_code"]})
    A("| 标的 | 状态 | 延续率 | 无条件频率 | 样本行 |")
    A("|------|------|------:|----------:|-------:|")
    for code in codes:
        for s in STATES:
            b = rep["splits"]["validate"]["M2"][s]["by_code"].get(code)
            if b:
                A(f"| {code} | {s} | {_pct(b['persistence'])} | "
                  f"{_pct(b['baseline_freq'])} | {b['n_rows']} |")
    A("")
    A("## 3. 判定（F1 / F2 / F3 + 达标规则）")
    A("")
    A(f"**`{v['status']}`**")
    A("")
    c = v["criteria"]
    A(f"- 判据 (a)：{'**成立**' if c['a'] else '不成立'}（UP 与 DOWN 两侧 Δ 的 CI 下界均 > 0）")
    A(f"- 判据 (b)：{'**成立**' if c['b'] else '不成立'}（UP 与 DOWN 两侧延续率差值的 CI 下界均 > 0）")
    A(f"- 有效交易日 {v['n_days']} / 门槛 {v['min_days']}"
      f"（F3：{'满足' if v['n_days'] >= v['min_days'] else '**不满足**'}）")
    for r in v["reasons"]:
        A(f"- {r}")
    A("")
    A("## 4. test 段（封存段）")
    A("")
    A(f"- `test_evaluated` = **`{rep['test_evaluated']}`**")
    if rep["test_not_evaluated_reason"]:
        A(f"- 未打开的原因：{rep['test_not_evaluated_reason']}")
    A("")
    if "test" in rep["splits"]:
        t = rep["splits"]["test"]
        A(f"test 段有效交易日 **{t['n_days']}** 天、样本行 {t['n_rows']}；"
          f"test 段判据：a={v.get('test_criteria', {}).get('a')}、"
          f"b={v.get('test_criteria', {}).get('b')}")
        A("")
        A("### 4.1 test 段 M1")
        A("")
        L.extend(_m1_table(t))
        A("")
        A("### 4.2 test 段 M2")
        A("")
        L.extend(_m2_table(t))
    else:
        A("test 段**没有打开**：按预注册 §1 的达标规则，未达标就不许读封存段。"
          "报告里因此**没有任何 test 段数字** —— 这与「test 跑了但不好看」"
          "是两件不同的事。")
    A("")
    A("## 5. 稳健性附表（**不得**据此改判据或宣布达标，预注册 §2）")
    A("")
    A("| 变体 | 有效交易日 | M1 UP Δ [CI] | M1 DOWN Δ [CI] | M2 UP Δ [CI] | M2 DOWN Δ [CI] |")
    A("|------|-----------:|--------------|----------------|--------------|----------------|")
    for r in rep["robustness"]:
        A(f"| {r['label']} | {r['n_days']} | {_pct(r['M1'][STATE_UP]['delta'])} "
          f"{_ci(r['M1'][STATE_UP]['delta_ci95'])} | "
          f"{_pct(r['M1'][STATE_DOWN]['delta'])} "
          f"{_ci(r['M1'][STATE_DOWN]['delta_ci95'])} | "
          f"{_pct(r['M2'][STATE_UP]['delta'])} "
          f"{_ci(r['M2'][STATE_UP]['delta_ci95'])} | "
          f"{_pct(r['M2'][STATE_DOWN]['delta'])} "
          f"{_ci(r['M2'][STATE_DOWN]['delta_ci95'])} |")
    A("")
    A("## 6. 口径自检（**全样本、非结论**）")
    A("")
    A(f"{rep['selfcheck']['warning']}。用途只有一个：核对实现与预注册 §4.1 的"
      "探索性读数是否读的是同一套口径。")
    A("")
    A("| 量 | 本次全样本 | 预注册 §4.1 探索性读数 |")
    A("|----|-----------|------------------------|")
    pr = rep["selfcheck"]["prereg_reading"]
    for s in TREND_STATES:
        A(f"| {s} 条件命中率 | {_pct(rep['selfcheck']['hit'][s])} | "
          f"{pr['hit'][s]:.4f} |")
        A(f"| {s} 无条件基线 | {_pct(rep['selfcheck']['baseline'][s])} | "
          f"{pr['baseline'][s]:.4f} |")
    A(f"| 样本行 | {rep['selfcheck']['n_rows']} | {pr['n_rows']} |")
    A("")
    A("## 7. train 段（**调试区，非证据**）")
    A("")
    A("train 是调试区：它的数字**不构成样本外证据**，不得据此晋级或选口径。")
    A("")
    L.extend(_m1_table(rep["splits"]["train"]))
    A("")
    A("## 8. 口径落地说明（预注册未写死、实现必须选一的点）")
    A("")
    for k, text in rep["criteria_landing"].items():
        A(f"- **{k}**：{text}")
    A("")
    A("## 9. 数据快照与复现")
    A("")
    A(f"- 行情快照 sha256（逐行 `code|date|close`）："
      f"`{rep['data_snapshot']['bars_sha256']}`（{rep['data_snapshot']['bars_rows']} 行）")
    rc = rep["data_snapshot"]["raw_cache"]
    A(f"- `raw_fetch_cache`：{rc['n_entries']} 条 → sha256 `{rc['sha256']}`")
    A(f"- bootstrap 种子 `{rep['bootstrap']['seed']}`；次数 `{rep['bootstrap']['n_boot']}`")
    A("- 本报告正文的 sha256 记在同名 `.json` 的 `report_sha256` 字段 —— "
      "**正文里不写它自己**（自指哈希会让「同参数两次运行哈希相同」变成不可能的承诺）；"
      "正文不含时间戳，故同参数两次运行的哈希天然相等，可用 "
      "`sha256sum <报告>.md` 复核")
    A(f"- 复现：`stocklab trend evaluate --report <path>`（口径全部写死，无随机）")
    A("")
    A("## 10. 口径声明")
    A("")
    for text in rep["notes"].values():
        A(f"- {text}")
    A("- 判定判据**提前定死**：判据 (a)/(b) 至少一条成立才允许打开 test；"
      "「差一点」= `falsified`，不许挪口径、不许挪切分、不许换指标。")
    A("")
    return "\n".join(L) + "\n"


def write_report(rep: Mapping[str, Any], md_path: Path) -> dict:
    """写 `md_path` + 同名 `.json`，返回路径与 sha256（正文无时间戳 → 幂等）。"""
    md_path = Path(md_path)
    md_path.parent.mkdir(parents=True, exist_ok=True)
    json_path = md_path.with_suffix(".json")
    md = render_markdown(rep)
    blob = json.dumps(rep, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
    md_path.write_text(md, encoding="utf-8")
    json_path.write_text(blob, encoding="utf-8")
    return {"markdown": str(md_path), "json": str(json_path),
            "sha256_md": hashlib.sha256(md.encode("utf-8")).hexdigest(),
            "sha256_json": hashlib.sha256(blob.encode("utf-8")).hexdigest()}
