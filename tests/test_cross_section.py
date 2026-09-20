"""Task 8：横截面分位（极性统一「越大越好」）。"""

import pytest

from stocklab.candidate import cross_section as cs


def test_polarity_declares_inv_days_as_smaller_is_better():
    assert cs.POLARITY["inv_days"] is False
    for k in ("roe", "gross_margin", "gm_yoy_pp", "fcf_margin"):
        assert cs.POLARITY[k] is True, f"{k} 应当越大越好"


def test_percentile_ranks_higher_value_higher():
    rows = {"a": {"roe": 0.10}, "b": {"roe": 0.20}, "c": {"roe": 0.30}}
    out = cs.build(rows, asof="2026-09-20")
    assert out["a"]["roe_pct"] < out["b"]["roe_pct"] < out["c"]["roe_pct"]
    assert out["c"]["roe_pct"] == pytest.approx(1.0)


def test_inv_days_polarity_is_inverted():
    """存货周转天数越小越好 → 分位必须取反，否则池子会选反。"""
    rows = {"a": {"inv_days": 30.0}, "b": {"inv_days": 90.0}}
    out = cs.build(rows, asof="2026-09-20")
    assert out["a"]["inv_days_pct"] > out["b"]["inv_days_pct"]


def test_na_factor_gets_none_not_zero():
    """不可算的因子给 None —— 0.0 会把「不知道」伪装成「最差」。"""
    rows = {"a": {"roe": 0.10, "gross_margin": None}, "b": {"roe": 0.20}}
    out = cs.build(rows, asof="2026-09-20")
    assert out["a"]["gross_margin_pct"] is None
    assert out["a"]["roe_pct"] is not None


def test_cross_section_n_per_factor():
    """金融股的 inv_days 不可算 → 该因子的 n 比 roe 小。"""
    rows = {"a": {"roe": 0.1, "inv_days": 30.0},
            "b": {"roe": 0.2, "inv_days": None},
            "c": {"roe": 0.3}}
    out = cs.build(rows, asof="2026-09-20")
    assert out["a"]["roe_n"] == 3
    assert out["a"]["inv_days_n"] == 1


def test_single_value_cross_section_gives_neutral_not_certain():
    """只有一个样本时分位无意义 —— 给 None，不给 1.0。"""
    rows = {"a": {"roe": 0.1}}
    out = cs.build(rows, asof="2026-09-20")
    assert out["a"]["roe_pct"] is None
    assert out["a"]["roe_n"] == 1


def test_period_is_carried_through():
    rows = {"a": {"roe": 0.1, "period": "2026Q2"},
            "b": {"roe": 0.2, "period": "2026Q2"}}
    out = cs.build(rows, asof="2026-09-20")
    assert out["a"]["period"] == "2026Q2"


def test_mixed_periods_are_marked():
    """横截面里各标的最新报告期可能不同（4 月是年报与一季报混排）。"""
    rows = {"a": {"roe": 0.1, "period": "2026Q2"},
            "b": {"roe": 0.2, "period": "2025Q4"}}
    out = cs.build(rows, asof="2026-09-20")
    assert out["a"]["period_mixed"] is True
