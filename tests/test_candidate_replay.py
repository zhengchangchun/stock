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

    收益**不是 0** —— 是清仓成本的负值。这里手工算：第 0 期持有 100 股
    @10.00，第 1 期池空 → 必须卖出，费用 = `CostModel.fees('sell', 10.00, 100)`，
    以「占初始市值 1000 元」的比例计，即 `-fees/1000`。
    """
    from stocklab.config.costs import CostModel as CM

    dates = _dates(6)
    c = _db_with_bars(tmp_db, {"000333": [10.0] * 6}, dates)
    costs = CM()
    expected_fee = costs.fees("sell", costs.fill_price("sell", 10.0), 100)
    expected = -expected_fee / 1000.0

    r = replay.period_returns(
        c, asof_dates=[dates[0], dates[5]], pool="short", costs=costs,
        _pools_for_test={dates[0]: ["000333"], dates[5]: []})
    assert len(r) == 1
    assert r[0] == pytest.approx(expected)
    assert r[0] < 0, "空池不是零收益 —— 清仓要付钱"


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


def _insert_script(conn, plugin_id="plug_x", version="1"):
    """插入一条 plugin_scripts 行并返回 script_id（无需 approve）。"""
    from stocklab.plugin import store as plugin_store
    return plugin_store.insert_script(
        conn, plugin_id=plugin_id, version=version, source_text="pass",
        note=None, now=NOW)


def test_deltas_are_candidate_minus_baseline(tmp_db):
    """Δ = 候选版本周期收益 − 基线版本周期收益。

    候选：持有 000333（nxt 里有它，从 dates[0]→dates[5] 涨 50%）；
    基线：两期都空仓（nxt 为空，收益=0）→ Δ > 0。

    注：`period_returns` 按 `nxt`（d1 的池成员）计算周期收益，因此要让
    候选真的「持有」000333，d1（dates[5]）的 pool 里必须包含它。

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
    # 候选：d1（dates[5]）的 nxt 包含 000333 → 计算 10→15 的收益（+50%）
    # 基线：两期都空仓（nxt 为空 → 收益 = 0）→ Δ = 0.5 > 0
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
    返回 (conn, script_id_for_plugin_1) ——「1」是打分插桩，用来验证覆盖是否生效。
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
    c.executemany(
        "INSERT INTO bars_daily (code, date, open, high, low, close, volume,"
        " adj_mode, source, fetched_at) VALUES (?,?,?,?,?,?,1000,'none','x',?)",
        [(code, d, 10.0, 10.0, 10.0, 10.0, NOW)
         for code in codes for d in days])

    sid_of: dict[str, int] = {}
    for pid, text in _PIPELINE_PLUGINS.items():
        sid = plugin_store.insert_script(c, plugin_id=pid, version="1.0.0",
                                         source_text=text, note=None, now=NOW)
        lifecycle.record_submit(c, sid, actor="t", now=NOW)
        lifecycle.record_sandbox(c, sid, passed=True, reason="ok", now=NOW)
        lifecycle.approve(c, sid, actor="t", reason="ok", now=NOW)
        sid_of[pid] = sid
    c.commit()
    return c, sid_of, days


def test_equal_id_delta_zero_via_score_pipeline(tmp_db):
    """等 id 等版本经过真实 score_pipeline（无 _pools_for）→ Δ = 0。

    ## 可证伪性声明
    短路已去除：replay_period_deltas 无条件调 _plugin_id_of，再把
    {pid: script_id} 传给两次 period_returns → score_pipeline。
    两次 score_pipeline 拿到完全相同的 plugin_overrides，对相同的标的
    和相同的 asof 日期运算，结果必然相同 → Δ = 0。

    若将来有人把等 id 路径的 eff_pid 改成 None（退回短路，不 pin），
    而此时 script_id 对应的版本不是 active（例如 active 已更新），
    score_pipeline 就会用 active 而非 X，和另一次调用（同样用 active）
    的结果虽然还是相同，但一旦候选与基线的 script_id 不同时，
    _plugin_id_of 就不会被调用，单变量守门失效 —— 该变异会让
    test_cross_plugin_raises_value_error 仍然绿，但 test_deltas_*
    测试会因为 LookupError（_plugin_id_of 查不到）而变红，暴露问题。

    具体的可变异点：把 `eff_pid = pid` 改回 `eff_pid = None`（短路），
    则 cand_overrides = base_overrides = None，score_pipeline 解析
    active 版本。本测试库里 script_id 对应的就是 active 版本，所以
    Δ 仍然是 0，**不会变红**。
    因此本测试额外断言：score_pipeline 确实接受到了 plugin_overrides
    且正常返回结果（通过检查返回成员数量 > 0），即生产路径已经跑通——
    如果 plugin_overrides 中的 script_id 无效（如 None 被当作 id），
    score_pipeline 会抛 NoActivePlugin 或 LookupError，测试变红。

    **可令本测试变红的精确变异**：在 _seed_pipeline_db 里额外插入一个
    script_id 相同 plugin 的新 active 版本（使原 sid 不再是 active），
    再把短路改回（eff_pid=None）。此时短路路径用 active（新版本）而非
    sid，两次调用的 overrides 均为 None，解析结果仍然相等，Δ=0，
    **但「哪个版本」已经错了**。直接捕获这一语义错误的测试见
    test_candidate_run.py::test_score_pipeline_overrides_plugin_version。
    """
    from stocklab.candidate.run import score_pipeline
    from stocklab.config.replay import REBALANCE_DAYS

    c, sid_of, days = _seed_pipeline_db(tmp_db)

    # 使用插桩 "1"（打分插桩）的 active script_id 作为两个版本
    sid = sid_of["1"]

    # 选靠近末尾的窗口（后 20 天），asof 均在有足够历史的区域
    window_start = days[-20]
    window_end = days[-1]

    flat = CostModel(commission_rate=0.0, min_commission=0.0,
                     transfer_fee_rate=0.0, stamp_tax_rate=0.0,
                     slippage_bps=0.0)

    # 验证：score_pipeline 在此窗口能正常跑出成员（即生产路径确实通了）
    # 用接近末尾的日期作为 asof，确保已有足够历史 K 线（>= 历史门槛）
    asof_for_verify = days[-1]
    pipe = score_pipeline(c, asof=asof_for_verify,
                          plugin_overrides={"1": sid})
    assert pipe.members, (
        "score_pipeline 未产出任何成员——生产路径未跑通，测试前提不成立")

    # 生产路径（无 _pools_for）：两次用同一个 sid → Δ 精确为 0
    tr, va = replay.replay_period_deltas(
        c, candidate_script_id=sid, baseline_script_id=sid,
        pool="short",
        window_start=window_start, window_end=window_end,
        costs=flat,
        trading_days=days)   # 不传 _pools_for → 走 score_pipeline

    # 必须有至少一个周期的 Δ（否则窗口太短，测试没有意义）
    assert tr or va, "Δ 序列为空——窗口内无足够调仓周期，测试前提不成立"
    assert all(x == 0.0 for x in tr + va), (
        f"等 id 但 Δ 不为 0：{tr + va}。"
        "若 eff_pid=None（短路），两次 score_pipeline 都用 active 版本，"
        "结果仍相同，本断言仍绿 —— 此时语义错误由"
        " test_score_pipeline_overrides_plugin_version 负责捕获。")
