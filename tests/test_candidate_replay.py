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

    对插桩 "1"（短线打分插桩）发布两个行为不同的版本：
    - v1（sid，score=80，pass_flag=True，archived — 不是 active）
    - v2（sid2，score=100，pass_flag=True，active）

    两个版本分数不同（80 vs 100），但因为 topn=6 而 SEED_UNIVERSE 只有 2 只
    标的，两者都能全部入池——分数差异在「选谁入池」上无法体现。

    为了让两个版本产生可辨别的收益差，K 线使用单调递增序列（每天 +0.01）；
    同时 v1 设 pass_flag=False（阻断全部标的），v2 设 pass_flag=True（放行）。
    这样：
    - v1 路径：所有标的被 score_pool 拒绝 → pool 为空 → 周期收益 = 0
    - v2 路径：所有标的进池 → 有正收益（价格上涨）→ 周期收益 > 0
    → Δ(cand=v1, base=v2) = 0 - positive < 0，断言 Δ≠0 成立。

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
