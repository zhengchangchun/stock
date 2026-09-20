"""沙盒回放引擎（设计文档 `2026-09-20-沙盒真回放-design.md`）。

## 观测单元是**调仓周期**，不是交易日

一个 20 日调仓周期里，20 个日度收益是**同一个持仓**产生的、高度自相关。
把它们当独立样本做 bootstrap 会算出**过窄的置信区间 → 假的 WIN**。所以
每个周期只产生**一个**观测。

代价（设计文档 §2 D1）：门槛从「120 交易日」变成「120 调仓周期」，
严格得多 —— 长期池（60 日）在可预见的历史长度内**永远出不了结论**。
这是算术，不降门槛迁就。

## 下游规则写死

等权、持到下一个调仓日、空池持现金（但计清仓成本）、扣成本、涨跌停
不可成交。**规则不随插桩版本变化** —— 否则「改下游」会成为另一条提分
路径，归因失效。

## 本模块在 candidate/ 下，不在 plugin/ 下

回放要调打分内核；`plugin/` 不得 import `candidate/`。所以回放住在这里，
由 `plugin/sandbox.py` 通过**注入**调用（见本模块 `period_returns`）。
"""

from __future__ import annotations

import sqlite3

from stocklab.backtest.portfolio import BoardUnknown, LIMIT_BY_BOARD, LIMIT_TOLERANCE
from stocklab.config.costs import CostModel
from stocklab.config.replay import REBALANCE_DAYS

#: 空池时的处置：持现金。**不是**「跳过该周期」—— 卖出上一期持仓是要付
#: 成本的，跳过会把那笔成本抹掉。
EMPTY_POOL_IS_CASH: bool = True

#: 每期等权买入的目标手数（整手）。
LOT = 100

#: 涨跌停判定时的比较容差（同 `backtest.portfolio.LIMIT_TOLERANCE`）。
#: `portfolio.py` 使用 1e-6 处理浮点表示误差（如 1.1*10 = 10.999…）。
#: 本模块使用同一常量，语义完全一致；若两处将来发散，需要统一评审后再改。
_LIMIT_SLACK = LIMIT_TOLERANCE


def rebalance_dates(trading_days: list[str], *, period: int, start: str,
                    end: str) -> list[str]:
    """窗口内每 `period` 个交易日一个调仓日。

    **末尾不足一个周期直接丢弃** —— 不截断成短周期，否则周期长度不一致、
    收益不可比。
    """
    days = [d for d in trading_days if start <= d <= end]
    if not days:
        return []
    out = [days[i] for i in range(0, len(days), period)]
    return [d for d in out if d <= end]


def _board_of(conn: sqlite3.Connection, code: str) -> str:
    row = conn.execute("SELECT board FROM instruments WHERE code = ?",
                       (code,)).fetchone()
    if row is None:
        raise BoardUnknown(f"{code} 不在 instruments 表里，无法判定板别")
    return str(row["board"])


def _limit_hit(prev_close: float, close: float, board: str) -> str | None:
    """返回 `'up'` / `'down'` / `None`。涨停买不进、跌停卖不出。"""
    if board not in LIMIT_BY_BOARD or prev_close <= 0:
        raise BoardUnknown(f"板别 {board!r} 不在 {sorted(LIMIT_BY_BOARD)} 中")
    limit = LIMIT_BY_BOARD[board]
    chg = (close - prev_close) / prev_close
    if chg >= limit - _LIMIT_SLACK:
        return "up"
    if chg <= -(limit - _LIMIT_SLACK):
        return "down"
    return None


def _close_on(conn: sqlite3.Connection, code: str, date: str) -> float | None:
    row = conn.execute(
        "SELECT close FROM bars_daily WHERE code = ? AND date = ?",
        (code, date)).fetchone()
    return None if row is None else float(row["close"])


def _prev_close(conn: sqlite3.Connection, code: str, date: str) -> float | None:
    row = conn.execute(
        "SELECT close FROM bars_daily WHERE code = ? AND date < ?"
        " ORDER BY date DESC LIMIT 1", (code, date)).fetchone()
    return None if row is None else float(row["close"])


def period_returns(conn: sqlite3.Connection, *, asof_dates: list[str],
                   pool: str, plugin_overrides: dict[str, int] | None = None,
                   costs: CostModel | None = None,
                   _pools_for_test: dict[str, list[str]] | None = None
                   ) -> list[float]:
    """逐周期收益序列（每个调仓周期一个观测）。

    `asof_dates`：调仓日序列，两两之间构成一个周期（长度 = len-1）。
    返回长度 = `len(asof_dates) - 1`。

    `_pools_for_test`：**测试接缝**，`{调仓日: [code, ...]}`。生产路径不传，
    此时池成员由 `score_pipeline` 现算。接缝只控制**池成员**；调仓日序列
    一律由 `asof_dates` 参数传入，两者职责不混。
    """
    effective_dates = list(asof_dates)

    if len(effective_dates) < 2:
        return []

    costs = costs or CostModel()

    from stocklab.candidate.run import score_pipeline  # 延迟 import，避免环

    def _members_on(day: str) -> list[str]:
        if _pools_for_test is not None:
            return list(_pools_for_test.get(day, []))
        res = score_pipeline(conn, asof=day, plugin_overrides=plugin_overrides)
        return sorted(m.code for m in res.members if m.pool == pool)

    out: list[float] = []
    for i in range(len(effective_dates) - 1):
        d0, d1 = effective_dates[i], effective_dates[i + 1]
        hold = _members_on(d0)
        nxt = _members_on(d1)

        # 期初在持 / 期末离场 / 期末新进
        entering = [c for c in hold if c not in nxt]   # 本期卖出（下期不持有）
        new = [c for c in nxt if c not in hold]        # 下期新买入

        p0 = {c: _close_on(conn, c, d0) for c in set(hold) | set(nxt)}
        p1 = {c: _close_on(conn, c, d1) for c in set(hold) | set(nxt)}

        # 周期收益：下一期持仓在 [d0, d1] 的价格变动（等权）
        gains: list[float] = []
        for c in nxt:
            if p0.get(c) and p1.get(c):
                gains.append(p1[c] / p0[c] - 1.0)
        gross = (sum(gains) / len(gains)) if gains else 0.0

        # 成本：卖出 entering（下期清仓）+ 买入 new（下期新建仓）
        # 费用比例相对于「每只标的名义市值 px * LOT」，再除以下期持仓数（等权分摊）
        n_next = max(len(nxt), 1)  # 空池时用 1 做分母，保持费用比例的量纲一致
        fee_ratio = 0.0

        for c in entering:
            px = p0.get(c)
            if px is None:
                continue
            board = _board_of(conn, c)
            prev = _prev_close(conn, c, d0)
            if _limit_hit(prev if prev is not None else px, px, board) == "down":
                continue  # 跌停卖不出
            _, fee = costs.total("sell", px, LOT)
            fee_ratio += fee / (px * LOT) / n_next

        for c in new:
            px = p0.get(c)
            if px is None:
                continue
            board = _board_of(conn, c)
            prev = _prev_close(conn, c, d0)
            if _limit_hit(prev if prev is not None else px, px, board) == "up":
                continue  # 涨停买不进
            _, fee = costs.total("buy", px, LOT)
            fee_ratio += fee / (px * LOT) / n_next

        out.append(gross - fee_ratio)
    return out


# ---------------------------------------------------------------------------
# Task 4：Δ 序列与训练/验证切分
# ---------------------------------------------------------------------------

#: 训练段占比。按**周期序号**切，不按日历 —— 保证两段周期长度相同。
SPLIT_TRAIN_RATIO: float = 0.7


def split_train_validate(deltas: list[float]) -> tuple[list[float], list[float]]:
    """按周期序号切训练段（前 70%）与验证段（后 30%）。

    两段**周期长度相同**（都来自同一套 `REBALANCE_DAYS`），所以 Δ 可比。

    边界情况：
    - 空列表 → ([], [])
    - 长度 1 → 整个列表放训练段，验证段为空（不满足 len > 1 的前提，不做切分）
    - 长度 2 → ([首], [尾])，保证验证段至少有一条
    """
    if not deltas:
        return [], []
    n_train = int(len(deltas) * SPLIT_TRAIN_RATIO)
    # 长度 > 1 时：至少 1 训练 + 1 验证；长度 == 1 时：整个归训练
    n_train = max(1, min(n_train, len(deltas) - 1)) if len(deltas) > 1 else 1
    return deltas[:n_train], deltas[n_train:]


def replay_period_deltas(conn: sqlite3.Connection, *, candidate_script_id: int,
                         baseline_script_id: int, pool: str,
                         window_start: str, window_end: str,
                         costs: CostModel | None = None,
                         trading_days: list[str] | None = None,
                         _pools_for: dict | None = None
                         ) -> tuple[list[float], list[float]]:
    """回放两个版本，返回 `(训练段 Δ, 验证段 Δ)`。

    Δ = 候选版本周期收益 − 基线版本周期收益。

    **单变量**：只有该 `plugin_id` 的两个版本不同；其余插件由
    `score_pipeline` 解析 active 版本（见 `period_returns`）。

    `_pools_for`：测试接缝，`{"cand": {日期: [code,...]}, "base": {...}}`，
    直接指定两个版本各自的成员，绕开 `score_pipeline` 调用。生产路径不传。

    注：`from stocklab.candidate.run import SEED_UNIVERSE` 在本函数里**不需要**。
    `score_pipeline`（在 `period_returns` 里延迟 import）自身已从 `candidate.run`
    导入 `SEED_UNIVERSE`，调用时 SEED_UNIVERSE 必然已在内存中。
    若不传 `_pools_for`，`period_returns` 会调 `score_pipeline`；
    若传了 `_pools_for`，直接走接缝，`score_pipeline` 不被调用。
    两种路径都不需要在这里额外 import SEED_UNIVERSE。
    """
    # 单变量检查：两个版本必须属于同一个 plugin_id。
    # 无论两个 script_id 是否相同，都查库拿 plugin_id。
    # 这确保「pin X」语义：score_pipeline 收到 {pid: X}，不会静默解析成 active 版本。
    pid = _plugin_id_of(conn, candidate_script_id)
    base_pid = _plugin_id_of(conn, baseline_script_id)
    if pid != base_pid:
        raise ValueError(
            f"两个版本属于不同插件（{pid!r} vs {base_pid!r}）—— 单变量原则"
            "要求只换同一个 plugin_id 的版本")
    eff_pid = pid

    period = REBALANCE_DAYS[pool]
    days = trading_days or _trading_days(conn, window_start, window_end)
    marks = rebalance_dates(days, period=period, start=window_start,
                            end=window_end)

    cand_overrides = {eff_pid: candidate_script_id}
    base_overrides = {eff_pid: baseline_script_id}

    cand = period_returns(
        conn, asof_dates=marks, pool=pool,
        plugin_overrides=cand_overrides, costs=costs,
        _pools_for_test=(_pools_for or {}).get("cand"))
    base = period_returns(
        conn, asof_dates=marks, pool=pool,
        plugin_overrides=base_overrides, costs=costs,
        _pools_for_test=(_pools_for or {}).get("base"))

    deltas = [c - b for c, b in zip(cand, base)]
    return split_train_validate(deltas)


# ---------------------------------------------------------------------------
# Task 7：基准超额与调仓日公开薄封装（供 sandbox 经注入调用）
# ---------------------------------------------------------------------------

#: 基准指数代码（腾讯口径，见 ADR-003 的指数源）。
BENCHMARK_CODE = "sh000300"


def benchmark_excess(conn: sqlite3.Connection, *, asof_dates: list[str],
                     pool: str, plugin_overrides: dict[str, int] | None = None,
                     benchmark: str = BENCHMARK_CODE,
                     costs: CostModel | None = None) -> float:
    """候选池相对基准指数的**超额收益**（同区间、同调仓日）。

    铁律要求「任何策略必须与 index_300 比较，跑不赢就明说」—— 所以这个
    数与版本 Δ **并列报告**，不是替代。
    """
    if len(asof_dates) < 2:
        return 0.0
    pool_r = period_returns(conn, asof_dates=asof_dates, pool=pool,
                            plugin_overrides=plugin_overrides, costs=costs)
    bench_r: list[float] = []
    for d0, d1 in zip(asof_dates, asof_dates[1:]):
        a, b = _close_on(conn, benchmark, d0), _close_on(conn, benchmark, d1)
        bench_r.append((b / a - 1.0) if (a and b) else 0.0)
    if not pool_r:
        return 0.0
    return sum(pool_r) / len(pool_r) - sum(bench_r) / len(bench_r)


def rebalance_marks(conn: sqlite3.Connection, *, pool: str, start: str,
                    end: str) -> list[str]:
    """窗口内的调仓日序列。供 `sandbox` 经注入调用（它不能 import 本模块）。"""
    days = _trading_days(conn, start, end)
    return rebalance_dates(days, period=REBALANCE_DAYS[pool],
                           start=start, end=end)


def _plugin_id_of(conn: sqlite3.Connection, script_id: int) -> str:
    from stocklab.plugin import store
    row = store.get_script(conn, script_id)
    if row is None:
        raise LookupError(f"脚本 {script_id} 不存在")
    return str(row["plugin_id"])


def _trading_days(conn: sqlite3.Connection, start: str,
                  end: str) -> list[str]:
    return [r[0] for r in conn.execute(
        "SELECT date FROM trading_calendar WHERE date BETWEEN ? AND ?"
        " ORDER BY date", (start, end))]
