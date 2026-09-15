"""Task 38：三段日期切分（`stocklab/experiments/split.py`）。

必须被钉住的四件事：

  1. 三段**按日期连续、互不重叠、并集 = 全部交易日**（不重不漏）；
  2. 比例之和必须为 1，且**非正**的比例被拒（空的一段 = 假装跑过）；
  3. 切完出现空段 → **直接报错**，不许产出一份「某段没跑」的实验报告；
  4. 边界（首日/末日/天数）可被读出并落进报告 —— 读者能自己核对 test 是哪一段。
"""

from __future__ import annotations

import pytest

from stocklab.experiments.split import (SELECTION_SPLITS, SPLIT_NAMES, SplitConfig,
                                        SplitConfigError, boundaries, split_days)


def _days(n: int, start: str = "2024-01-01") -> list[str]:
    """n 个连续的假交易日（`2024-01-01` 起按月/日递增到合法范围内）。"""
    out = []
    y, m, d = 2024, 1, 1
    for _ in range(n):
        out.append(f"{y:04d}-{m:02d}-{d:02d}")
        d += 1
        if d > 28:
            d, m = 1, m + 1
        if m > 12:
            m, y = 1, y + 1
    return out


def test_segments_are_contiguous_disjoint_and_cover_everything():
    days = _days(100)
    seg = split_days(days, SplitConfig())
    assert [d for name in SPLIT_NAMES for d in seg[name]] == days      # 不重不漏
    assert seg["train"][-1] < seg["validate"][0] < seg["test"][0]      # 按日期有序
    assert (len(seg["train"]), len(seg["validate"]), len(seg["test"])) == (60, 20, 20)


def test_the_last_segment_absorbs_the_rounding_remainder():
    """10 天 × 0.6/0.2/0.2：`int()` 会丢掉零头，零头必须归最后一段，不许有孤儿日。"""
    days = _days(10)
    seg = split_days(days)
    total = sum(len(seg[n]) for n in SPLIT_NAMES)
    assert total == 10
    assert len(seg["train"]) == 6 and len(seg["validate"]) == 2
    assert len(seg["test"]) == 2


def test_boundaries_report_first_last_and_count():
    days = _days(50)
    b = boundaries(split_days(days))
    assert b["train"]["first_date"] == days[0]
    assert b["train"]["n_days"] == 30
    assert b["test"]["last_date"] == days[-1]
    assert set(b) == set(SPLIT_NAMES)


def test_ratios_must_sum_to_one():
    with pytest.raises(SplitConfigError, match="比例之和"):
        SplitConfig(train=0.5, validate=0.2, test=0.2)


def test_non_positive_ratio_is_refused():
    with pytest.raises(SplitConfigError, match="必须为正"):
        SplitConfig(train=1.0, validate=0.0, test=0.0)
    with pytest.raises(SplitConfigError, match="必须为正"):
        SplitConfig(train=1.2, validate=-0.1, test=-0.1)


def test_a_config_that_would_produce_an_empty_segment_is_refused():
    """4 天 × 0.6/0.2/0.2 → validate = `int(0.8)` = **0 段**。

    空段会被 `summarize([])` 渲染成一份「0 个交易日」的报告，而 gate 只会说
    「样本不足」—— 「这段没跑」与「这段跑了但样本少」长得一模一样。
    必须在切分这一步就炸掉（ERROR_DIARY：「为空时是『没有』还是『没填』？」）。
    """
    with pytest.raises(SplitConfigError, match="为空"):
        split_days(_days(4))


def test_too_few_days_for_three_segments_is_refused():
    with pytest.raises(SplitConfigError, match="不足以切成"):
        split_days(_days(2))


def test_selection_splits_exclude_test():
    """`test` 是封存段，**不在**允许用来选择的清单里。"""
    assert SELECTION_SPLITS == ("train", "validate")
    assert "test" not in SELECTION_SPLITS


def test_config_is_serialisable_into_the_report():
    assert SplitConfig().as_dict() == {"train": 0.6, "validate": 0.2, "test": 0.2}
