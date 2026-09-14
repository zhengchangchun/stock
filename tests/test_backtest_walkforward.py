"""Task 24：Walk-forward 切分器（R7 自由度的可执行形式）+ 样本量纪律（R6）。

本文件覆盖四类「不许静默」的要求：
  1. 绝不 shuffle（会话轴必须严格升序，乱序/重复输入直接报错）
  2. train/test 之间有 purge/embargo，且边界日期**不在任何一折里**
  3. 折数随数据长度变化，且**数据不足时显式报错**（不得静默少跑）
  4. 样本量单位是**交易日**，不是「标的-日行数」（禁止拿行数当样本量）
"""
import pytest

from stocklab.backtest.walkforward import (DEFAULT_MIN_OOS_DAYS, Fold,
                                           InsufficientData, build_report,
                                           render_markdown,
                                           sample_size_report,
                                           split_walk_forward,
                                           threshold_arithmetic, unused_tail)


def sessions(n=20, *, day0=1):
    """严格升序的假会话轴（ISO 日期，月份用不到 31 天以上的场景）。"""
    return [f"2026-01-{day0 + i:02d}" for i in range(n)]


def idx(axis, d):
    return axis.index(d)


# ---------- 基本形状 ----------

def test_basic_split():
    folds = split_walk_forward(sessions(20), train=5, test=3)
    assert len(folds) > 0
    f = folds[0]
    assert len(f.train_dates) == 5
    assert len(f.test_dates) == 3
    # 端点由日期派生（D2：不允许端点与日期元组不一致）
    assert (f.train_start, f.train_end) == (f.train_dates[0], f.train_dates[-1])
    assert (f.test_start, f.test_end) == (f.test_dates[0], f.test_dates[-1])


def test_fold_indices_are_sequential():
    folds = split_walk_forward(sessions(20), train=5, test=3)
    assert [f.index for f in folds] == list(range(len(folds)))


def test_test_always_after_train():
    """每一折的 test 严格晚于 train —— 这是「不许全样本调参」的可执行形式。"""
    for f in split_walk_forward(sessions(20), train=5, test=3):
        assert max(f.train_dates) < min(f.test_dates)


def test_no_overlap_between_test_windows():
    folds = split_walk_forward(sessions(20), train=5, test=3)
    seen = set()
    for f in folds:
        assert not (seen & set(f.test_dates)), "测试窗口重叠 = 同一段数据被反复当测试集"
        seen |= set(f.test_dates)


def test_windows_are_time_ordered_across_folds():
    """折与折之间 train/test 都单调前移，绝不回退。"""
    folds = split_walk_forward(sessions(30), train=6, test=3, step=3)
    for a, b in zip(folds, folds[1:]):
        assert b.train_start > a.train_start
        assert b.test_start > a.test_start


# ---------- step 语义 ----------

def test_step_defaults_to_test_size():
    axis = sessions(20)
    folds = split_walk_forward(axis, train=5, test=3)
    a, b = folds[0], folds[1]
    assert idx(axis, b.train_start) - idx(axis, a.train_start) == 3


def test_folds_are_contiguous_with_explicit_step():
    axis = sessions(20)
    folds = split_walk_forward(axis, train=5, test=3, step=3)
    for a, b in zip(folds, folds[1:]):
        assert idx(axis, b.train_start) - idx(axis, a.train_start) == 3
        # test 窗口首尾相接（step == test），不留缝也不重叠
        assert axis[idx(axis, a.test_end) + 1] == b.test_start


def test_step_larger_than_test_leaves_gap():
    axis = sessions(30)
    folds = split_walk_forward(axis, train=5, test=3, step=7)
    a, b = folds[0], folds[1]
    assert not set(a.test_dates) & set(b.test_dates)
    assert idx(axis, b.test_start) - idx(axis, a.test_end) == 5  # 被跳过的 5 天


# ---------- 折数随数据长度变化的边界（不足必须报错） ----------

@pytest.mark.parametrize("n,expected", [
    (8, 1),   # 恰好 train(5)+test(3)
    (9, 1),
    (10, 1),
    (11, 2),  # +step(3)
    (14, 3),
    (17, 4),
    (20, 5),
])
def test_fold_count_scales_with_data_length(n, expected):
    folds = split_walk_forward(sessions(n), train=5, test=3)
    assert len(folds) == expected


def test_insufficient_data_raises_instead_of_silently_running_less():
    """偏离计划 D1：计划写「返回空表」，本实现**显式报错**。

    静默返回 [] 会让调用方把「0 折」读成「跑完了、没有结论」，
    从而在没有样本外证据的情况下继续往下走（本项目最怕的静默降级）。
    """
    with pytest.raises(InsufficientData) as ei:
        split_walk_forward(sessions(7), train=5, test=3)
    msg = str(ei.value)
    assert "7" in msg and "8" in msg          # 现有会话数 / 最少需要多少
    assert isinstance(ei.value, ValueError)   # 仍是 ValueError，调用方好接


def test_exactly_enough_data_gives_one_fold():
    folds = split_walk_forward(sessions(8), train=5, test=3)
    assert len(folds) == 1


def test_insufficient_data_reports_deficit():
    with pytest.raises(InsufficientData) as ei:
        split_walk_forward(sessions(0), train=5, test=3)
    assert ei.value.n_sessions == 0
    assert ei.value.n_required == 8
    assert ei.value.deficit == 8


# ---------- purge / embargo ----------

def test_embargo_purges_train_boundary_dates():
    axis = sessions(20)
    folds = split_walk_forward(axis, train=5, test=3, embargo=1)
    for f in folds:
        assert len(f.train_dates) == 4
        assert len(f.purged_dates) == 1
        # 被剔除的那一天：train 窗口的最后一天，且**不在** train/test 任一侧
        assert f.purged_dates[0] == axis[idx(axis, f.test_start) - 1]
        assert f.purged_dates[0] not in f.train_dates
        assert f.purged_dates[0] not in f.test_dates


def test_embargo_creates_gap_between_train_end_and_test_start():
    axis = sessions(20)
    folds = split_walk_forward(axis, train=5, test=3, embargo=2)
    for f in folds:
        assert idx(axis, f.test_start) - idx(axis, f.train_end) == 3  # 2 剔除 + 1 步进


def test_no_embargo_means_train_touches_test():
    axis = sessions(20)
    folds = split_walk_forward(axis, train=5, test=3, embargo=0)
    for f in folds:
        assert f.purged_dates == ()
        assert idx(axis, f.test_start) - idx(axis, f.train_end) == 1


def test_embargo_does_not_change_test_windows():
    """purge 只动训练侧：测试窗口必须逐折完全一致，否则无法比较。"""
    axis = sessions(20)
    a = split_walk_forward(axis, train=5, test=3, embargo=0)
    b = split_walk_forward(axis, train=5, test=3, embargo=2)
    assert [f.test_dates for f in a] == [f.test_dates for f in b]
    assert len(a) == len(b)


# ---------- 参数校验 ----------

@pytest.mark.parametrize("kwargs", [
    {"train": 0, "test": 3},
    {"train": -1, "test": 3},
    {"train": 5, "test": 0},
])
def test_non_positive_windows_rejected(kwargs):
    with pytest.raises(ValueError):
        split_walk_forward(sessions(20), **kwargs)


@pytest.mark.parametrize("embargo", [-1, 5, 6])
def test_embargo_out_of_range_rejected(embargo):
    with pytest.raises(ValueError):
        split_walk_forward(sessions(20), train=5, test=3, embargo=embargo)


def test_non_positive_step_rejected():
    with pytest.raises(ValueError):
        split_walk_forward(sessions(20), train=5, test=3, step=0)


def test_shuffled_sessions_rejected():
    """绝不 shuffle：乱序输入必须报错，而不是被内部排序「修好」。"""
    axis = sessions(20)
    axis[3], axis[10] = axis[10], axis[3]
    with pytest.raises(ValueError) as ei:
        split_walk_forward(axis, train=5, test=3)
    assert "升序" in str(ei.value)


def test_duplicate_sessions_rejected():
    axis = sessions(20)
    axis[5] = axis[4]
    with pytest.raises(ValueError):
        split_walk_forward(axis, train=5, test=3)


def test_unsorted_input_not_silently_sorted():
    """反证：乱序输入绝不能被「排一下就好」地接受。"""
    axis = list(reversed(sessions(20)))
    with pytest.raises(ValueError):
        split_walk_forward(axis, train=5, test=3)


# ---------- 样本量：交易日 ≠ 行数 ----------

def test_sample_size_is_counted_in_trading_days_not_rows():
    axis = sessions(20)
    folds = split_walk_forward(axis, train=5, test=3)
    oos = [d for f in folds for d in f.test_dates]
    rep = sample_size_report(folds, dates_by_code={"000333": oos, "600690": oos})
    # 5 折 × 3 个交易日 = 15 个**交易日**（不重叠，可直接相加）
    assert rep["oos_trading_days"] == 15
    assert rep["oos_unique_days"] == 15
    # 2 个标的 → 行数翻倍，但**有效样本量不变**
    assert rep["oos_rows"] == 30
    assert rep["unit"] == "trading_day"
    assert rep["n_codes"] == 2
    assert rep["rows_per_day"] == 2.0


def test_rows_do_not_inflate_effective_sample_size():
    """把行数当样本量是 R6 的红线：加标的只增加行数，不增加有效样本量。"""
    axis = sessions(20)
    folds = split_walk_forward(axis, train=5, test=3)
    oos = [d for f in folds for d in f.test_dates]
    one = sample_size_report(folds, dates_by_code={"000333": oos})
    two = sample_size_report(folds, dates_by_code={"000333": oos, "600690": oos})
    assert two["oos_trading_days"] == one["oos_trading_days"]
    assert two["oos_rows"] == 2 * one["oos_rows"]
    assert two["effective_n"] == one["effective_n"] == 15
    assert two["rows_per_day"] == 2.0


def test_only_dates_inside_test_windows_count_as_rows():
    """标的自身的历史再长也没用：只有落在样本外窗口里的行才算样本外行。"""
    axis = sessions(20)
    folds = split_walk_forward(axis, train=5, test=3)
    oos = [d for f in folds for d in f.test_dates]
    rep = sample_size_report(
        folds, dates_by_code={"000333": oos, "600690": axis})  # 600690 喂全部 20 天
    assert rep["oos_rows_by_code"]["600690"] == 15   # 只有 15 天在样本外窗口内
    assert rep["oos_rows"] == 30


def test_test_windows_never_overlap_so_days_can_be_summed():
    axis = sessions(40)
    folds = split_walk_forward(axis, train=8, test=4, step=4)
    oos = [d for f in folds for d in f.test_dates]
    rep = sample_size_report(folds, dates_by_code={"000333": oos})
    assert rep["oos_trading_days"] == rep["oos_unique_days"] == 4 * len(folds)
    assert rep["overlap_detected"] is False


def test_overlapping_test_windows_are_flagged():
    """结构性反证：若哪天有人把测试窗口做成重叠，报告必须自己喊出来。"""
    a = Fold(0, ("2026-01-01",), ("2026-01-02", "2026-01-03"))
    b = Fold(1, ("2026-01-04",), ("2026-01-03", "2026-01-04"))  # 故意重叠
    rep = sample_size_report([a, b], dates_by_code={"000333": ["2026-01-02",
                                                              "2026-01-03",
                                                              "2026-01-04"]})
    assert rep["overlap_detected"] is True
    assert rep["oos_trading_days"] == 4        # 朴素相加
    assert rep["oos_unique_days"] == 3         # 真实交易日
    assert rep["effective_n"] == 3             # 有效样本量取去重值


# ---------- 120 交易日硬门槛 ----------

def test_threshold_met_reports_min_history():
    out = threshold_arithmetic(train=250, test=21, step=21, n_sessions=3095,
                               oos_trading_days=2835)
    assert out["meets"] is True
    assert out["threshold"] == DEFAULT_MIN_OOS_DAYS == 120
    assert out["deficit_days"] == 0
    # 达标所需的最少历史（精确值，不是「train + 门槛 + test」这种粗算）：
    # ceil(120/21) = 6 折 → 首折 train 250 + test 21，之后每折再要 step=21
    #   = 250 + 21 + 5×21 = 376
    # 反直觉但正确：376 < 250+120+21=391，因为 6 折给出 6×21=126 > 120 个样本外交易日。
    assert out["min_folds_required"] == 6
    assert out["min_sessions_required"] == 376


def test_threshold_not_met_gives_concrete_arithmetic():
    out = threshold_arithmetic(train=500, test=21, step=21, n_sessions=600,
                               oos_trading_days=84)
    assert out["meets"] is False
    assert out["deficit_days"] == 36
    # ceil(120/21) = 6 折 → 500 + 21 + 5*21 = 626
    assert out["min_folds_required"] == 6
    assert out["min_sessions_required"] == 626
    assert out["extra_sessions_needed"] == 26
    assert out["extra_years_needed"] == pytest.approx(26 / 243, abs=1e-9)
    assert "标的" in out["note"]  # 明确写出「加标的不顶用」


def test_more_codes_do_not_fix_a_sample_size_deficit():
    out = threshold_arithmetic(train=500, test=21, step=21, n_sessions=600,
                               oos_trading_days=84)
    # 按日聚类：有效样本量是交易日，与标的数无关
    assert "不增加" in out["note"] or "无效" in out["note"]


def test_threshold_boundary_exactly_120_meets():
    out = threshold_arithmetic(train=250, test=21, step=21, n_sessions=400,
                               oos_trading_days=120)
    assert out["meets"] is True
    assert out["deficit_days"] == 0


# ---------- 报告：可复现 + 显式失败 + 不含 in-sample 结论 ----------

def test_build_report_is_reproducible():
    """同一输入 → 同一折分（报告可复现，不依赖时间/随机）。"""
    axis = sessions(30)
    kw = dict(generated="2026-09-15", axis=axis,
              dates_by_code={"000333": axis}, train=6, test=3)
    a, b = build_report(**kw), build_report(**kw)
    assert a == b
    assert [f["test_start"] for f in a["folds"]] == \
           [f["test_start"] for f in b["folds"]]


def test_build_report_raises_on_insufficient_data():
    with pytest.raises(InsufficientData):
        build_report(generated="2026-09-15", axis=sessions(5),
                     dates_by_code={"000333": sessions(5)}, train=5, test=3)


def test_build_report_declares_no_in_sample_conclusion():
    axis = sessions(20)
    rep = build_report(generated="2026-09-15", axis=axis,
                       dates_by_code={"000333": axis}, train=5, test=3)
    assert rep["disclosure"]["insample"] is False
    assert rep["disclosure"]["strategy_conclusion"] is None
    md = render_markdown(rep)
    assert "不含" in md and "交易日" in md
    assert "策略" in md  # 明确声明不做策略结论


def test_markdown_reports_both_day_count_and_row_count():
    """报告必须同时给「交易日数」与「行数」，避免读者把行数当样本量。"""
    axis = sessions(20)
    rep = build_report(generated="2026-09-15", axis=axis,
                       dates_by_code={"000333": axis, "600690": axis},
                       train=5, test=3)
    md = render_markdown(rep)
    assert "样本外**交易日**" in md
    assert "标的-日行数" in md
    assert rep["sample_size"]["effective_n"] == 15
    assert rep["sample_size"]["oos_rows"] == 30


# ---------- Fold 不可变 ----------

def test_fold_is_frozen():
    f = Fold(0, ("2026-01-01",), ("2026-01-02",))
    with pytest.raises(Exception):
        f.index = 1
    assert f.purged_dates == ()


# ---------- 尾部未覆盖必须报出（不许静默截断） ----------

def test_unused_tail_counts_days_no_fold_touches():
    axis = sessions(20)
    folds = split_walk_forward(axis, train=5, test=3)
    assert len(folds) == 5
    # 末折 train 起点 = axis[12]，test = axis[17:20] → 正好落到轴的最后一天
    assert folds[-1].train_start == axis[12]
    assert folds[-1].test_end == axis[-1]
    assert unused_tail(folds, axis) == ()


def test_unused_tail_is_reported_not_hidden():
    # 22 个交易日 / train=8 / test=4：末折起点 = axis[8]，test = axis[16:20]，
    # 剩下 axis[20:22] 两天凑不出完整 test 窗 → 必须被报出来
    axis = sessions(22)
    folds = split_walk_forward(axis, train=8, test=4)
    tail = unused_tail(folds, axis)
    assert tail == tuple(axis[20:22])

    rep = build_report(generated="2026-09-15", axis=axis,
                       dates_by_code={"000333": axis}, train=8, test=4)
    assert rep["unused_tail"]["n_days"] == 2
    assert rep["unused_tail"]["first"] == axis[20]
    assert rep["unused_tail"]["last"] == axis[21]
    assert "未被任何折用到" in render_markdown(rep)


def test_unused_tail_shrinks_with_smaller_test_window():
    """尾巴大小由 test 决定：test 越大浪费越多 —— 报告里要能看见这个量。"""
    axis = sessions(40)
    small = unused_tail(split_walk_forward(axis, train=10, test=3), axis)
    big = unused_tail(split_walk_forward(axis, train=10, test=9), axis)
    assert len(big) >= len(small)
