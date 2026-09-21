"""Task 3：回放引擎 —— 调仓日、周期收益（等权/空池/成本/涨跌停）。"""

import pytest

from stocklab.candidate import replay
from stocklab.config.costs import CostModel
from stocklab.store.db import connect
from stocklab.store.migrate import init_db

NOW = "2026-09-20T16:00:00+08:00"


def _dates(n, start="2026-01-01"):
    from datetime import date, timedelta
    d0 = date.fromisoformat(start)
    return [(d0 + timedelta(days=i)).isoformat() for i in range(n)]


# ---------- 调仓日 ----------

def test_rebalance_dates_take_every_kth_day():
    days = _dates(20)
    got = replay.rebalance_dates(days, period=5, start=days[0], end=days[-1])
    assert got == [days[0], days[5], days[10], days[15]]


def test_rebalance_dates_drop_partial_tail():
    """末尾不足一个周期的部分**丢弃**，不截断成短周期。"""
    days = _dates(13)
    got = replay.rebalance_dates(days, period=5, start=days[0], end=days[-1])
    assert got == [days[0], days[5], days[10]]
    assert days[12] not in got


def test_rebalance_dates_respect_window_bounds():
    days = _dates(30)
    got = replay.rebalance_dates(days, period=10, start=days[5], end=days[25])
    assert got == [days[5], days[15], days[25]]


def test_rebalance_dates_yields_only_start_when_window_shorter_than_period():
    days = _dates(3)
    assert replay.rebalance_dates(days, period=5, start=days[0],
                                  end=days[-1]) == [days[0]]


# ---------- 周期收益 ----------

def _db_with_bars(tmp_db, prices: dict[str, list[float]], dates: list[str]):
    """建库：标的 + 日历 + 逐日收盘价（价格序列与 dates 等长）。"""
    init_db(tmp_db)
    c = connect(tmp_db)
    c.executemany(
        "INSERT INTO instruments (code, name, market, board, type, added_at)"
        " VALUES (?,?,'sz','main','stock',?)",
        [(code, code, NOW) for code in prices])
    c.executemany("INSERT INTO trading_calendar (date, is_open, source,"
                  " created_at) VALUES (?,1,'t',?)", [(d, NOW) for d in dates])
    c.executemany(
        "INSERT INTO bars_daily (code, date, open, high, low, close, volume,"
        " adj_mode, source, fetched_at) VALUES (?,?,?,?,?,?,1000,'none','x',?)",
        [(code, d, p, p, p, p, NOW)
         for code, series in prices.items()
         for d, p in zip(dates, series)])
    c.commit()
    return c


def test_empty_pool_holds_cash_but_pays_liquidation_cost(tmp_db):
    """空池 → 不产生持仓收益，但仍要卖掉上一期持仓、付清仓成本。

    收益**不是 0** —— 是清仓成本的负值。成本按**每只名义仓位**
    `POSITION_NOTIONAL` 换算（ADR-017 D-13，不是 1 手），且滑点如实计入（D-14）。
    这里手工算：第 0 期持有 A，第 1 期池空 → 卖出 1 条腿。
    """
    from stocklab.config.costs import CostModel as CM
    from stocklab.config.replay import POSITION_NOTIONAL

    dates = _dates(6)
    c = _db_with_bars(tmp_db, {"000333": [10.0] * 6}, dates)
    costs = CM()
    px = 10.0
    qty = int(POSITION_NOTIONAL / px)          # 与实现同式（整股截断）
    expected_fee_ratio = costs.fees("sell", px, qty) / (px * qty)
    # n_hold = 1（hold 只有一只），成交 1 条腿
    expected_slippage = costs.slippage_bps / 10_000.0
    expected = -(expected_fee_ratio + expected_slippage)

    r = replay.period_returns(
        c, asof_dates=[dates[0], dates[5]], pool="short", costs=costs,
        _pools_for_test={dates[0]: ["000333"], dates[5]: []})
    assert len(r) == 1
    assert r[0] == pytest.approx(expected)
    assert r[0] < 0, "空池不是零收益 —— 清仓要付钱"
    # 口径哨兵：按 1 手（100 股）算会差一个数量级（0.5010% vs 0.0260% 费率）
    assert abs(r[0]) < 0.01, "成本仍是 1 手口径（被放大到离群值）"


def test_flat_prices_give_zero_return_before_costs(tmp_db):
    dates = _dates(6)
    c = _db_with_bars(tmp_db, {"000333": [10.0] * 6}, dates)
    r = replay.period_returns(
        c, asof_dates=[dates[0], dates[5]], pool="short",
        costs=CostModel(commission_rate=0.0, min_commission=0.0,
                        transfer_fee_rate=0.0, stamp_tax_rate=0.0,
                        slippage_bps=0.0),
        _pools_for_test={dates[0]: [], dates[5]: []})
    assert len(r) == 1 and r[0] == 0.0


# ---------------------------------------------------------------------------
# Finding 1（look-ahead）证伪测试：d0 池 != d1 池，且两池收益不同
# ---------------------------------------------------------------------------

def test_gross_uses_d0_pool_not_d1_pool(tmp_db):
    """周期 [d0, d1] 的毛收益必须来自 **d0 池**（hold），不是 d1 池（nxt）。

    ## 夹具
    - A 涨（10 → 20，+100%）；B 跌（10 → 5，-50%）
    - d0 池 = ["A"]（在 d0 决定的持仓 → 本期实际持有 A）
    - d1 池 = ["B"]（在 d1 才知道 → 属于下一期）
    - 期望：本期毛收益 = A 的 [d0,d1] 收益 = +100%
    - 旧代码（gross iterate nxt=["B"]）：+50% 变成 -50%，红。

    ## 可证伪性
    把 `period_returns` 的 `for c in tradable_hold` 改回 `for c in nxt`，
    本测试立即变红（结果由 +100% 变成 -50%）。
    """
    dates = _dates(6)
    c = _db_with_bars(
        tmp_db,
        {
            "000A00": [10.0, 12.0, 14.0, 16.0, 18.0, 20.0],  # +100%
            "000B00": [10.0, 9.0, 8.0, 7.0, 6.0, 5.0],       # -50%
        },
        dates,
    )
    flat = CostModel(commission_rate=0.0, min_commission=0.0,
                     transfer_fee_rate=0.0, stamp_tax_rate=0.0,
                     slippage_bps=0.0)
    r = replay.period_returns(
        c, asof_dates=[dates[0], dates[5]], pool="short", costs=flat,
        _pools_for_test={dates[0]: ["000A00"], dates[5]: ["000B00"]})
    assert len(r) == 1
    # 期望：hold=A 在 [d0,d5] 涨 100%，扣掉「d1 处 A→B 调仓」的免佣费用（flat）。
    # flat costs 下 fee=0，因此结果精确等于 +1.0。
    assert r[0] == pytest.approx(1.0), (
        f"期望 hold 池 A 的 +100% 收益，实际 {r[0]}——"
        "若为 -0.5，说明 gross 仍在 iterate nxt（look-ahead 未修复）")


def test_limit_up_at_d0_excludes_from_gross(tmp_db):
    """Finding 2：`d0` 涨停买不进的标的**不进 gross**（原代码仅挡了 fee）。

    ## 夹具
    - A: prev_close 10.0，d0 close 11.0（+10%，主板涨停）；随后跌回 5.0
    - d0 池 = ["A"]，d1 池 = ["A"]（同一只，无调仓变化）
    - 旧代码：gross iterate nxt=[A]，把 [d0=11 → d1=5] 的 -54.5% 记为收益。
      **Finding 2 揭示**：即便 fee 侧挡了，收益侧照样错。
    - 新代码：A 在 d0 涨停 → 从 gross 里剔除 → gross=0；持现金。

    ## 可证伪性
    移除 tradable_hold 里的涨停过滤（即改回 `for c in hold`），本测试立即变红。
    """
    # 5 天：d(-1) prev_close=10.0，d0=11.0（涨停），d1..d3=5.0
    # 用 6 个交易日，让 prev_close(d0) = day[0] 的 close
    days = _dates(6)
    prices = [10.0, 11.0, 8.0, 6.0, 5.0, 5.0]
    c = _db_with_bars(tmp_db, {"000A00": prices}, days)
    flat = CostModel(commission_rate=0.0, min_commission=0.0,
                     transfer_fee_rate=0.0, stamp_tax_rate=0.0,
                     slippage_bps=0.0)
    # d0 = days[1]（close=11.0，prev_close=10.0，涨停），d1 = days[5]（close=5.0）
    r = replay.period_returns(
        c, asof_dates=[days[1], days[5]], pool="short", costs=flat,
        _pools_for_test={days[1]: ["000A00"], days[5]: ["000A00"]})
    assert len(r) == 1
    # 期望：A 在 d0 涨停不可买 → 不进 gross → gross=0；hold=nxt=[A] → 无调仓账单
    # 结果 = 0.0
    assert r[0] == pytest.approx(0.0), (
        f"期望涨停剔除后 gross=0，实际 {r[0]}——"
        "涨跌停仍只挡了 fee，收益侧仍被计入（Finding 2 未修复）")


def test_cost_leg_uses_d1_prices_and_limits(tmp_db):
    """Finding 1 知会效应：调仓账单的价格/涨跌停判定在 **d1**（交易实际发生日）。

    ## 夹具
    - A：从 10 涨到 20（供 sell-at-d1 用）
    - d0 池 = ["A"]，d1 池 = []（清仓）
    - 期望：gross = A 从 [d0,d1] 的 +100%；成本 = 在 d1 卖 A（p1=20）
      fee_ratio = `fees(sell, 20, qty) / (20 × qty) / 1`，qty = `int(名义仓位/20)`
      （ADR-017 D-13：按名义仓位，**不是** 1 手），另加 1 条腿的滑点（D-14）
    - 旧代码：fee 用 p0=10 → 结果不同；且用 `total()` → 滑点被丢掉。

    ## 可证伪性
    把成本循环里的 `p1.get(c)` 改回 `p0.get(c)`，本测试即变红。
    """
    from stocklab.config.costs import CostModel as CM
    from stocklab.config.replay import POSITION_NOTIONAL
    dates = _dates(6)
    c = _db_with_bars(tmp_db, {"000A00": [10.0, 12.0, 14.0, 16.0, 18.0, 20.0]},
                      dates)
    costs = CM()  # 非零费率，可以体现价格差异
    p1 = 20.0
    qty = int(POSITION_NOTIONAL / p1)
    expected_fee = costs.fees("sell", p1, qty)
    expected = (1.0 - expected_fee / (p1 * qty)
                - costs.slippage_bps / 10_000.0)
    r = replay.period_returns(
        c, asof_dates=[dates[0], dates[5]], pool="short", costs=costs,
        _pools_for_test={dates[0]: ["000A00"], dates[5]: []})
    assert len(r) == 1
    assert r[0] == pytest.approx(expected), (
        f"期望调仓 fee 用 d1 价格 {p1} 且按名义仓位，实际 {r[0]} vs {expected}——"
        "若接近旧值，说明 fee 仍在用 p0 价格或 1 手口径")
    assert r[0] < expected + 1e-9


def test_benchmark_excess_skips_missing_bench_periods(tmp_db):
    """Finding 3：基准 bar 缺失时**跳过该周期**（不零填充）。

    ## 夹具
    - 池：A（+50% 每期）
    - 基准 sh000300：只有 dates[0] 和 dates[5] 有 bar；中间的 dates[2] 无 bar
    - 用三段调仓：dates[0] → dates[2] → dates[5]
      - 段 1 `[d0,d2]`：基准缺 d2 bar → 跳过
      - 段 2 `[d2,d5]`：基准缺 d2 bar → 跳过
    - 期望：所有段都跳过 → aligned 空 → 返回 0.0（不是零填充参与均值）
    """
    dates = _dates(6)
    c = _db_with_bars(tmp_db,
                      {"000A00": [10.0, 12.0, 14.0, 16.0, 18.0, 20.0]},
                      dates)
    # 只给基准 dates[0] 与 dates[5] 有 bar
    c.execute("INSERT INTO instruments (code, name, market, board, type,"
              " added_at) VALUES ('sh000300','沪深300指数','sh','main',"
              "'index',?)", (NOW,))
    c.executemany(
        "INSERT INTO bars_daily (code, date, open, high, low, close, volume,"
        " adj_mode, source, fetched_at) VALUES ('sh000300',?,?,?,?,?,1000,"
        "'none','x',?)",
        [(dates[0], 100.0, 100.0, 100.0, 100.0, NOW),
         (dates[5], 200.0, 200.0, 200.0, 200.0, NOW)])
    c.commit()
    flat = CostModel(commission_rate=0.0, min_commission=0.0,
                     transfer_fee_rate=0.0, stamp_tax_rate=0.0,
                     slippage_bps=0.0)
    # 三段：[d0,d2],[d2,d5]，基准都缺 d2 bar → 全部跳过 → 返回 0.0
    # 用 _pools_for_test 走进 period_returns；但 benchmark_excess 会先算池收益
    # 池：三段都持有 A → gross 每段都为正
    # 结论：即使池有真实正收益，基准全跳过 → 无法比较 → 返回 0.0
    val = replay.benchmark_excess(
        c, asof_dates=[dates[0], dates[2], dates[5]], pool="short",
        plugin_overrides=None, benchmark="sh000300", costs=flat)
    assert val == 0.0, (
        f"期望基准全跳过时返回 0.0（无对齐周期），实际 {val}——"
        "若非 0，说明仍在用零填充（Finding 3 未修复）")


# ---------- Task 4：Δ 与切分 ----------

def test_split_is_by_period_index_not_calendar():
    """按周期**序号**切 70/30。"""
    deltas = list(range(10))
    tr, va = replay.split_train_validate(deltas)
    assert tr == [0, 1, 2, 3, 4, 5, 6]
    assert va == [7, 8, 9]


def test_split_handles_short_series():
    tr, va = replay.split_train_validate([1.0, 2.0])
    assert tr == [1.0]
    assert va == [2.0]


def test_split_empty():
    assert replay.split_train_validate([]) == ([], [])


def test_split_len_one():
    """长度 1 时整个序列归训练段，验证段为空（无法切分，无推断价值）。"""
    tr, va = replay.split_train_validate([3.14])
    assert tr == [3.14]
    assert va == []


def test_split_keeps_order():
    tr, va = replay.split_train_validate([1, 2, 3, 4, 5])
    assert tr == [1, 2, 3]
    assert va == [4, 5]


def test_split_ratio_is_constant():
    assert replay.SPLIT_TRAIN_RATIO == 0.7


# ---------------------------------------------------------------------------
# 回放成本口径（ADR-017 D-13 名义仓位 / D-14 滑点如实计入）
# ---------------------------------------------------------------------------

def _zero_model(**kw) -> CostModel:
    """只留指定项的成本模型：其余全 0（隔离被测项）。"""
    base = dict(commission_rate=0.0, min_commission=0.0, transfer_fee_rate=0.0,
                stamp_tax_rate=0.0, slippage_bps=0.0)
    base.update(kw)
    return CostModel(**base)


def test_qty_truncates_to_whole_shares():
    """`qty = max(1, int(N / px))` —— **整股截断**（不是四舍五入）。

    代价：实际名义额 `px × qty` 可能略小于 `POSITION_NOTIONAL`，
    所以「最低佣金不生效」的充分条件落在 `px × qty ≥ 20000` 上。
    """
    from stocklab.config.replay import POSITION_NOTIONAL
    assert replay._qty_for(17.3) == int(POSITION_NOTIONAL / 17.3) == 5780
    assert 17.3 * 5780 < POSITION_NOTIONAL        # 截断 → 略小于名义仓位
    assert replay._qty_for(1e9) == 1              # 买不起 1 股 → 仍按 1 股（不静默变 0）


def test_min_commission_boundary_is_20k_notional():
    """最低佣金只在名义额 < 2 万元时生效（20000 × 0.025% 恰 = 5 元）。

    这是「≥ 2 万是常数区间、不存在可调的 N」这条推导的算术基础。
    隔离佣金：把印花税/过户费置 0。
    """
    costs = _zero_model(commission_rate=0.00025, min_commission=5.0)
    assert costs.fees("buy", 10.0, 1000) == 5.0     # 名义 10000 → 2.5 < 5 → 最低佣金生效
    assert costs.fees("buy", 10.0, 2000) == 5.0     # 名义 20000 → 恰相等（临界点）
    assert costs.fees("buy", 10.0, 3000) == 7.5     # 名义 30000 → 比例佣金生效


def test_cost_is_insensitive_to_notional_above_20k(tmp_db, monkeypatch):
    """正向采信条件（设计 §5.1）：N ∈ {2万,5万,10万,50万} 两两等价。

    夹具：4 只价格 17.3（**非整数**，故意触发整股截断）的标的，
    四个调仓日每期只换 1 只（1 卖 + 1 买）→ 3 个周期。

    容差不是「逐位相同」：`fees()` 末尾有 `round(…, 2)`，不同 N 的四舍五入
    落到不同的分位上 → 只差一个极小的量。
    """
    dates = _dates(6)
    px = 17.3
    c = _db_with_bars(tmp_db, {code: [px] * 6
                               for code in ("000A00", "000B00", "000C00", "000D00")},
                      dates)
    pools = {dates[0]: ["000A00"], dates[1]: ["000B00"],
             dates[2]: ["000C00"], dates[3]: ["000D00"]}
    marks = [dates[0], dates[1], dates[2], dates[3]]

    series: list[list[float]] = []
    for notional in (20_000.0, 50_000.0, 100_000.0, 500_000.0):
        monkeypatch.setattr(replay, "POSITION_NOTIONAL", notional)
        r = replay.period_returns(c, asof_dates=marks, pool="short",
                                  costs=CostModel(), _pools_for_test=pools)
        assert len(r) == 3
        series.append(r)

    for i in range(len(series)):
        for j in range(i + 1, len(series)):
            for k in range(3):
                assert abs(series[i][k] - series[j][k]) < 1e-7, (
                    f"N 档间逐期收益不同：{series[i][k]} vs {series[j][k]}"
                    "—— 说明最低佣金仍在生效")
            assert abs(sum(series[i]) / 3 - sum(series[j]) / 3) < 1e-6


def test_notional_below_20k_is_different_and_costlier(tmp_db, monkeypatch):
    """反向证伪（设计 §5.2）：N = 1 万必须与 10 万**不同**，且 1 万档成本更高。

    1 万 × 0.025% = 2.5 < 5 → 最低佣金必生效。
    **若这一条也相同 → 推导错了 → 停下来重查**（不得挑一个好看的 N）。
    """
    dates = _dates(6)
    c = _db_with_bars(tmp_db, {"000A00": [10.0] * 6, "000B00": [10.0] * 6},
                      dates)
    pools = {dates[0]: ["000A00"], dates[5]: ["000B00"]}
    marks = [dates[0], dates[5]]

    monkeypatch.setattr(replay, "POSITION_NOTIONAL", 10_000.0)
    r_small = replay.period_returns(c, asof_dates=marks, pool="short",
                                    costs=CostModel(), _pools_for_test=pools)
    monkeypatch.setattr(replay, "POSITION_NOTIONAL", 100_000.0)
    r_big = replay.period_returns(c, asof_dates=marks, pool="short",
                                  costs=CostModel(), _pools_for_test=pools)

    assert r_small[0] != pytest.approx(r_big[0], abs=1e-7), (
        "1 万档与 10 万档结果相同——最低佣金没生效，推导错了")
    assert r_small[0] < r_big[0], "1 万档成本应**更高**（净收益更低）"


def test_fee_uses_reference_price_not_slippage_price(tmp_db):
    """决策 C3：费用用 `fees()`（**参考价**），不用 `total()`（滑点价）。

    否则同一笔滑点会被算两遍。这里把滑点调到 100bps（1%）放大差异：
    用参考价 hand-calc，若实现改成滑点价则断言变红。
    """
    dates = _dates(6)
    c = _db_with_bars(tmp_db, {"000A00": [10.0] * 6}, dates)
    costs = _zero_model(commission_rate=0.00025, stamp_tax_rate=0.0005,
                        slippage_bps=100.0)
    px = 10.0
    qty = int(replay.POSITION_NOTIONAL / px)
    expected_fee_ratio = costs.fees("sell", px, qty) / (px * qty)
    expected_slippage = costs.slippage_bps / 10_000.0

    r = replay.period_returns(
        c, asof_dates=[dates[0], dates[5]], pool="short", costs=costs,
        _pools_for_test={dates[0]: ["000A00"], dates[5]: []})
    assert r[0] == pytest.approx(-(expected_fee_ratio + expected_slippage))
    # 若费用改用滑点价（9.9 而非 10.0），费率会低 1% → 断言变红
    assert r[0] != pytest.approx(-(expected_fee_ratio * 0.99 + expected_slippage))


def test_slippage_only_charged_on_filled_legs(tmp_db):
    """决策 C4：滑点**只对成交的腿**收（持仓不动不收）。

    - (a) 零换手 `hold == nxt` → 滑点恰为 0（不是「整仓收一遍」）
    - (b) 全换手且 n_hold = 1 → 滑点 = `2 × bps / 10000`
    - (c) n_hold = 2 且换 1 只（1 卖 + 1 买）→ 滑点 = `2 × bps / 10000 / 2`
    """
    flat = _zero_model(slippage_bps=5.0)
    dates = _dates(6)
    marks = [dates[0], dates[5]]
    c = _db_with_bars(tmp_db, {code: [10.0] * 6
                               for code in ("000A00", "000B00", "000C00")}, dates)

    def _run(pools):
        # 价格全平 → gross = 0 → 返回值就是「负的成本」，滑点可直接读出
        return replay.period_returns(c, asof_dates=marks, pool="short",
                                     costs=flat, _pools_for_test=pools)[0]

    # (a) 零换手：d0 池 == d1 池
    assert _run({dates[0]: ["000A00"], dates[5]: ["000A00"]}) == 0.0, \
        "零换手不该产生滑点"

    # (b) 全换手、n_hold = 1（1 卖 + 1 买 = 2 条腿）
    assert _run({dates[0]: ["000A00"], dates[5]: ["000B00"]}) == pytest.approx(
        -(2 * 5.0 / 10_000.0))

    # (c) n_hold = 2，只换 1 只（1 卖 + 1 买 = 2 条腿，每腿占 1/2 仓）
    assert _run({dates[0]: ["000A00", "000B00"],
                 dates[5]: ["000B00", "000C00"]}) == pytest.approx(
        -(2 * 5.0 / 10_000.0 / 2))


def test_slippage_lowers_net_return(tmp_db):
    """方向：同一序列，含滑点的净收益必须**低于**不含滑点的。"""
    dates = _dates(6)
    pools = {dates[0]: ["000A00"], dates[5]: ["000B00"]}
    marks = [dates[0], dates[5]]

    c = _db_with_bars(tmp_db, {"000A00": [10.0] * 6, "000B00": [10.0] * 6},
                      dates)
    without = replay.period_returns(c, asof_dates=marks, pool="short",
                                    costs=_zero_model(), _pools_for_test=pools)
    with_slip = replay.period_returns(c, asof_dates=marks, pool="short",
                                      costs=_zero_model(slippage_bps=5.0),
                                      _pools_for_test=pools)
    assert with_slip[0] < without[0]
    assert without[0] == 0.0, "零成本模型下应有换手也等于 0"


def _insert_script(conn, plugin_id="plug_x", version="1"):
    """插入一条 plugin_scripts 行并返回 script_id（无需 approve）。"""
    from stocklab.plugin import store as plugin_store
    return plugin_store.insert_script(
        conn, plugin_id=plugin_id, version=version, source_text="pass",
        note=None, now=NOW)


def test_deltas_are_candidate_minus_baseline(tmp_db):
    """Δ = 候选版本周期收益 − 基线版本周期收益。

    候选：持有 000333（hold 里有它，从 dates[0]→dates[5] 涨 50%）；
    基线：两期都空仓（hold 为空，收益=0）→ Δ > 0。

    注：**Finding 1 修复后**，`period_returns` 按 `hold`（d0 池成员）计算
    周期收益（不再是 nxt）。要让候选真的「持有」000333 并在本期得到 +50%，
    d0（dates[0]）的 pool 里必须包含它。

    短路已去除：replay_period_deltas 无论等 id 与否都调 _plugin_id_of，
    所以这里必须先插入真实的 plugin_scripts 行。
    """
    dates = _dates(6)
    c = _db_with_bars(tmp_db, {"000333": [10.0, 11.0, 12.0, 13.0, 14.0, 15.0]},
                      dates)
    sid = _insert_script(c)
    flat = CostModel(commission_rate=0.0, min_commission=0.0,
                     transfer_fee_rate=0.0, stamp_tax_rate=0.0,
                     slippage_bps=0.0)
    # 候选：d0（dates[0]）的 hold 包含 000333 → 计算 10→15 的收益（+50%）
    # 基线：两期都空仓（hold 为空 → 收益 = 0）→ Δ = 0.5 > 0
    tr, va = replay.replay_period_deltas(
        c, candidate_script_id=sid, baseline_script_id=sid, pool="short",
        window_start=dates[0], window_end=dates[-1], costs=flat,
        _pools_for={"cand": {dates[0]: ["000333"], dates[5]: ["000333"]},
                    "base": {dates[0]: [], dates[5]: []}})
    allv = tr + va
    assert allv and all(x > 0 for x in allv)      # 涨了且基线空仓 → Δ > 0


def test_deltas_zero_when_versions_identical(tmp_db):
    """等 id 时两次 period_returns 拿到相同 pool → Δ = 0（测试接缝路径）。

    短路已去除：replay_period_deltas 仍调 _plugin_id_of，所以必须插入真实脚本行。
    """
    dates = _dates(6)
    c = _db_with_bars(tmp_db, {"000333": [10.0] * 6}, dates)
    sid = _insert_script(c)
    flat = CostModel(commission_rate=0.0, min_commission=0.0,
                     transfer_fee_rate=0.0, stamp_tax_rate=0.0,
                     slippage_bps=0.0)
    same = {dates[0]: ["000333"], dates[5]: ["000333"]}
    tr, va = replay.replay_period_deltas(
        c, candidate_script_id=sid, baseline_script_id=sid, pool="short",
        window_start=dates[0], window_end=dates[-1], costs=flat,
        _pools_for={"cand": same, "base": same})
    assert all(x == 0.0 for x in tr + va)


def test_cross_plugin_raises_value_error(tmp_db):
    """不同 plugin_id 的两个版本必须 raise ValueError（单变量原则守门）。"""
    from stocklab.plugin import store as plugin_store

    init_db(tmp_db)
    c = connect(tmp_db)
    sid_a = plugin_store.insert_script(
        c, plugin_id="plug_a", version="1", source_text="pass_a",
        note=None, now=NOW)
    sid_b = plugin_store.insert_script(
        c, plugin_id="plug_b", version="1", source_text="pass_b",
        note=None, now=NOW)

    with pytest.raises(ValueError, match="单变量原则"):
        replay.replay_period_deltas(
            c, candidate_script_id=sid_a, baseline_script_id=sid_b,
            pool="short", window_start="2026-01-01", window_end="2026-01-31")


# ---------------------------------------------------------------------------
# 生产路径：等 id 经过 score_pipeline（无 _pools_for），Δ = 0
# ---------------------------------------------------------------------------

#: 能跑通 score_pipeline 所需的最小插件集（同 test_candidate_run.py::PLUGINS）。
_PIPELINE_PLUGINS = {
    "0": "def run(ctx):\n    return {'pass_flag': True, 'risk_note': []}\n",
    "1": "def run(ctx):\n    return {'score': 80.0, 'pass_flag': True,"
         " 'reason': '量价', 'risk_list': []}\n",
    "2": "def run(ctx):\n    return {'score': 60.0, 'pass_flag': True,"
         " 'reason': '景气', 'risk_list': []}\n",
    "3": "def run(ctx):\n    return {'score': 40.0, 'pass_flag': True,"
         " 'reason': '护城河', 'risk_list': []}\n",
    "4": "def run(ctx):\n    return {'final_score': ctx['raw_score'],"
         " 'risk_out': []}\n",
}


def _seed_pipeline_db(tmp_db):
    """建一个能跑通 score_pipeline 的最小库（标的 + 日历 + K 线 + 5 active 插桩）。

    形态同 test_candidate_run.py::_seed_db，保证生产路径可通。

    对插桩 "1"（短线打分插桩）发布两个行为不同的版本：
    - v1（sid_v1，score=80，pass_flag=True，archived — 不是 active）
    - v2（sid_v2，score=0，pass_flag=False，active — 主动拒绝全部标的）

    判别两个版本靠的是 **pass_flag**，不是分数：topn=6 而 SEED_UNIVERSE 只有
    2 只标的，若两版都放行，两版都会全部入池，分数差异在「选谁入池」上体现不出来。

    所以让 v1 放行、v2 拒绝，使两版在「有无持仓」上分叉；配合单调递增的 K 线
    （起点 10.0，每天 +0.01）：
    - v1 路径：所有标的进 pool → 有正收益（价格上涨）→ 周期收益 > 0
    - v2 路径：所有标的被 score_pool 拒绝 → pool 为空 → 周期收益 = 0
    → Δ(cand=v1, base=v2) = positive - 0 > 0，断言 Δ≠0 成立。

    可证伪性：若 eff_pid 被改回 None（短路），两侧都解析 active（v2），
    两次 score_pipeline 相同，Δ=0，下方「Δ≠0」断言立即变红。

    返回 (conn, sid_v1_for_plugin_1, sid_v2_for_plugin_1, days)。
    """
    from datetime import date as _date, timedelta as _timedelta
    from stocklab.plugin import lifecycle, store as plugin_store

    init_db(tmp_db)
    c = connect(tmp_db)

    codes = ["000333", "600690"]
    c.executemany(
        "INSERT INTO instruments (code, name, market, board, type, added_at)"
        " VALUES (?,?,'sz','main','stock',?)",
        [(code, f"标的{code}", NOW) for code in codes])

    days: list[str] = []
    cur = _date(2025, 6, 1)
    while len(days) < 300:   # 需要 ≥ 历史门槛（同 _seed_db），否则 pre_screen 拒绝所有标的
        if cur.weekday() < 5:
            days.append(cur.isoformat())
        cur += _timedelta(days=1)

    c.executemany("INSERT INTO trading_calendar (date, is_open, source,"
                  " created_at) VALUES (?,1,'t',?)", [(d, NOW) for d in days])
    # 单调递增价格：起点 10.0，每日 +0.01 → 300 天后约 13.0
    # 让「有标的在池」的版本与「空池」版本产生可辨别的周期收益差
    c.executemany(
        "INSERT INTO bars_daily (code, date, open, high, low, close, volume,"
        " adj_mode, source, fetched_at) VALUES (?,?,?,?,?,?,1000,'none','x',?)",
        [(code, d, 10.0 + i * 0.01, 10.0 + i * 0.01,
          10.0 + i * 0.01, 10.0 + i * 0.01, NOW)
         for code in codes for i, d in enumerate(days)])

    # 插桩 "0"、"2"、"3"、"4"：直接用 _PIPELINE_PLUGINS 里的源码
    sid_of: dict[str, int] = {}
    for pid in ("0", "2", "3", "4"):
        text = _PIPELINE_PLUGINS[pid]
        sid = plugin_store.insert_script(c, plugin_id=pid, version="1.0.0",
                                         source_text=text, note=None, now=NOW)
        lifecycle.record_submit(c, sid, actor="t", now=NOW)
        lifecycle.record_sandbox(c, sid, passed=True, reason="ok", now=NOW)
        lifecycle.approve(c, sid, actor="t", reason="ok", now=NOW)
        sid_of[pid] = sid

    # 插桩 "1" v1：pass_flag=True，score=80（将成为 archived）
    text_v1 = ("def run(ctx):\n"
               "    return {'score': 80.0, 'pass_flag': True,"
               " 'reason': '量价v1', 'risk_list': []}\n")
    sid_v1 = plugin_store.insert_script(c, plugin_id="1", version="1.0.0",
                                         source_text=text_v1, note=None, now=NOW)
    lifecycle.record_submit(c, sid_v1, actor="t", now=NOW)
    lifecycle.record_sandbox(c, sid_v1, passed=True, reason="ok", now=NOW)
    lifecycle.approve(c, sid_v1, actor="t", reason="ok", now=NOW)

    # 插桩 "1" v2：pass_flag=False，score=0（主动拒绝全部标的）
    # 这让 v1（放行）和 v2（拒绝）在「有无持仓」上产生明确分叉，
    # 配合递增价格，使 Δ(cand=v1, base=v2) 在有价格变动的周期必然非零。
    text_v2 = ("def run(ctx):\n"
               "    return {'score': 0.0, 'pass_flag': False,"
               " 'reason': '量价v2拒绝', 'risk_list': []}\n")
    sid_v2 = plugin_store.insert_script(c, plugin_id="1", version="2.0.0",
                                         source_text=text_v2, note=None, now=NOW)
    lifecycle.record_submit(c, sid_v2, actor="t", now=NOW)
    lifecycle.record_sandbox(c, sid_v2, passed=True, reason="ok", now=NOW)
    lifecycle.approve(c, sid_v2, actor="t", reason="v2 上线（拒绝策略）", now=NOW)
    # approve v2 时，lifecycle 自动把 v1 archived → v2 是 active，v1 是 archived

    c.commit()
    return c, sid_v1, sid_v2, days


def test_equal_id_delta_zero_via_score_pipeline(tmp_db):
    """等 id 等版本经过真实 score_pipeline（无 _pools_for）→ Δ = 0。
    不同版本（同插件）经过真实 score_pipeline → Δ ≠ 0（夹具可证伪）。

    ## 夹具设计

    `_seed_pipeline_db` 对插桩 "1" 发布两个行为不同的版本：
    - v1（sid_v1，pass_flag=True/score=80，archived — 不是 active）
    - v2（sid_v2，pass_flag=False/score=0，active — 拒绝所有标的）

    K 线使用单调递增价格（起点 10.0，每日 +0.01），使得「有标的入池」
    和「空池」产生可辨别的周期收益差。

    ## Δ == 0 路径（等 id，cand=v1，base=v1）

    两侧 `cand_overrides = base_overrides = {"1": sid_v1}` →
    两次 `score_pipeline` 都用 v1（pass_flag=True，放行标的）→
    持仓相同 → 收益相同 → Δ = 0。

    ## Δ ≠ 0 路径（不同版本，cand=v1，base=v2）——证伪断言

    `cand_overrides = {"1": sid_v1}` vs `base_overrides = {"1": sid_v2}` →
    - cand：v1 放行 → 标的在池 → 有正收益（价格上涨）
    - base：v2 拒绝 → 池为空 → 收益 = 0
    → Δ = positive - 0 > 0，`assert any(Δ ≠ 0)` 成立。

    ## 可令证伪断言变红的精确变异

    把生产代码 `eff_pid = pid` 改回 `eff_pid = None`（恢复短路，不 pin）：
    - `cand_overrides = base_overrides = None`（或含 None 值）
    - 两次 `score_pipeline` 均解析 active（v2，pass_flag=False）
    - 两次池都为空 → 两次收益都为 0 → Δ = 0
    → `assert any(x != 0.0 for x in tr2 + va2)` **立即变红**。

    这就是能真正证伪「版本钉住」的断言。
    """
    c, sid_v1, sid_v2, days = _seed_pipeline_db(tmp_db)

    # 选靠近末尾的窗口（后 20 天），asof 均在有足够历史的区域
    window_start = days[-20]
    window_end = days[-1]

    flat = CostModel(commission_rate=0.0, min_commission=0.0,
                     transfer_fee_rate=0.0, stamp_tax_rate=0.0,
                     slippage_bps=0.0)

    # ── 路径 A：等 id（cand=v1, base=v1），Δ = 0 ──
    tr, va = replay.replay_period_deltas(
        c, candidate_script_id=sid_v1, baseline_script_id=sid_v1,
        pool="short",
        window_start=window_start, window_end=window_end,
        costs=flat,
        trading_days=days)

    assert tr or va, "Δ 序列为空——窗口内无足够调仓周期，测试前提不成立"
    assert all(x == 0.0 for x in tr + va), (
        f"等 id 但 Δ 不为 0：{tr + va}")

    # ── 路径 B：不同版本（cand=v1/放行，base=v2/拒绝），Δ ≠ 0 ──
    # 这是真正的证伪断言：
    # 若 eff_pid 被改回 None（短路），两侧均用 active(v2/拒绝)，
    # 两次池都为空，Δ = 0，此断言立即变红。
    tr2, va2 = replay.replay_period_deltas(
        c, candidate_script_id=sid_v1, baseline_script_id=sid_v2,
        pool="short",
        window_start=window_start, window_end=window_end,
        costs=flat,
        trading_days=days)

    assert tr2 or va2, "Δ2 序列为空——窗口内无足够调仓周期，测试前提不成立"
    assert any(x != 0.0 for x in tr2 + va2), (
        "cand(v1,放行) vs base(v2,拒绝) 的 Δ 全为 0——"
        "版本钉住未生效：两次 score_pipeline 解析了相同版本（均为 active）。"
        "若 eff_pid=None（短路），此断言立即变红。")
