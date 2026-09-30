"""P103 T4：候选 #5 `mf_net_surprise_20d` 研究侧取值器的可用例（新增）。

本因子是 P95 §5.1 的 **MF-B**：`( main_net[asof] − mean(窗) ) / stdev(窗)`，
**时序标准化**（只与自身历史比 ⇒ 规模项在分子分母里自动约掉、不依赖
`valuation_daily` ⇒ 可用窗口保持 2010+）。窗口 = **最近 20 个交易日**。

它与前三档的**语义差异**（本档的核心口径）：**缺失语义是标准的 `None`**——
窗内任一 `main_net` 为 `NULL`、或行缺失、或行数 < 20 ⇒ 该标的**不进截面**；
`σ == 0` **也判缺失**（**不给 `0.0`**，给 0 会被读成「无异动」）。这与 P102
`ann_count_5d` 的「0 是真值」**相反**（P95 §5.1 明写的 `std == 0 ⇒ None`）。

十一组（任务书 §4 的 C1–C11）：

1. 窗口边界：窗外（第 21 个交易日）的极值不影响 `μ`/`σ`；
2. 手算对拍：`(vals[-1] − fmean(vals)) / stdev(vals)` 与返回**逐位相同**；
3. 窗内任一 `main_net IS NULL` ⇒ 该 code 不进结果；
4. 窗内缺一行 ⇒ 同上（不是「有几日算几日」）；
5. `σ == 0`（20 个值相同）⇒ 不进结果（**不是** `0.0`）；
6. 交易日不足 20 个 ⇒ `{}`；
7. 单只入口与批量映射一致 ＋ 缺值返回 `None`；
8. 诊断键 `zero_ratio_p50` / `tie_ratio_p50` 对新因子**自动生效**（§3 零改动）；
9. 未登记名仍 fail-closed ＋ 兜底分支；
10. 缺省逐位不变（复用 p97 夹具与黄金 digest）；
11. 其它三个研究侧因子的行为逐位不变。
"""

from __future__ import annotations

import math
import statistics

import pytest

from stocklab.research import factor, xsec
from stocklab.store.db import connect
from tests.test_research_factor import (
    CODES, START, _Synth, _install, _prereg, _synth_db,
)
from tests.test_research_factor_p97 import (
    GOLDEN_MD_DIGEST, GOLDEN_REPORT_DIGEST, GOLDEN_SUMMARY_DIGEST,
    _default_report, _digest, _normalize_md,
)
from tests.test_research_signal import _weekdays

NOW = "2026-09-30T00:00:00+08:00"

#: 手算基准用的 20 个值（`dates[k]` ⇒ `float(k+1)`）。
_VALS = [float(k + 1) for k in range(1, 21)]


def _mf_db(tmp_db, dates: list[str], rows: list[tuple]) -> None:
    """日历 ＋ `money_flow_daily` 的夹具库；`rows` = `(code, date, main_net)`。

    只插 `main_net` 一列因子值，其余可空列一律留 `NULL`（本因子只读 `main_net`）。
    """
    from stocklab.store.migrate import init_db

    init_db(tmp_db)
    c = connect(tmp_db)
    c.executemany(
        "INSERT INTO trading_calendar (date, is_open, source, created_at)"
        " VALUES (?,1,'t',?)", [(d, NOW) for d in dates])
    c.executemany(
        "INSERT INTO money_flow_daily (code, date, main_net, source, fetched_at,"
        " created_at, resp_sha256) VALUES (?,?,?,'t',?,?,'x')",
        [(code, d, v, NOW, NOW) for code, d, v in rows])
    c.commit()
    c.close()


def _rising(dates: list[str], code: str = "600000") -> list[tuple]:
    """`dates[k]` ⇒ `float(k+1)`（严格递增 ⇒ `σ > 0`；末位即 `x_now`）。"""
    return [(code, d, float(k + 1)) for k, d in enumerate(dates)]


# ---------------------------------------------------------------------------
# C1 / C2 窗口边界与手算对拍
# ---------------------------------------------------------------------------

def test_c1_the_day_just_before_the_window_does_not_leak(tmp_db):
    """C1：`asof` 前有 21 个交易日 —— 窗口 = 最近 20 个，**第 21 个（窗外）值极大**。

    若实现把窗外那天也读进来，`μ` 会被拉爆、返回值立刻从 ≈1.6058 跳到 ≈−1 ⇒ 用例红。
    """
    dates = _weekdays(30)[:21]
    asof = dates[20]
    rows = [("600000", dates[0], 1e9)]            # 窗外那天的极值
    rows += _rising(dates[1:])                    # 窗内 20 天：1..20
    _mf_db(tmp_db, dates, rows)
    c = connect(tmp_db)
    try:
        got = factor._mf_net_surprise_20d_map(c, asof)
        assert got == {"600000": pytest.approx(
            (_VALS[-1] - statistics.fmean(_VALS)) / statistics.stdev(_VALS))}
        assert abs(got["600000"]) < 10.0          # 极值没进来
    finally:
        c.close()


def test_c2_hand_computed_value_is_the_formula_bitwise(tmp_db):
    """C2：20 个已知值（`1..20`）⇒ `(20 − 10.5) / √35`，与返回**逐位相同**。"""
    dates = _weekdays(30)[:20]
    asof = dates[19]
    _mf_db(tmp_db, dates, _rising(dates))
    c = connect(tmp_db)
    try:
        got = factor.mf_net_surprise_20d(c, "600000", asof)
        # 与公式在同一组 vals 上**逐位**相同（`==`，不是 approx）
        assert got == (_VALS[-1] - statistics.fmean(_VALS)) / \
            statistics.stdev(_VALS)
        # 手算：μ = 10.5、σ = √(665/19) = √35 ⇒ (20 − 10.5)/√35
        assert got == pytest.approx(9.5 / math.sqrt(35.0), abs=1e-12)
        assert got == pytest.approx(1.6058, abs=1e-4)
    finally:
        c.close()


def test_c2b_window_counts_trading_days_not_natural_days(tmp_db):
    """C2 续：窗口是「`trading_calendar` 里 `<= asof` 的最后 20 个**交易日**」。

    日历跨了很长的自然日区间，按自然日窗口会取不到 20 行 ⇒ 返回 `{}` 或错值。
    """
    dates = ["2026-01-05", "2026-01-09", "2026-01-20", "2026-01-21",
             "2026-02-03", "2026-02-04", "2026-02-05", "2026-03-02",
             "2026-03-03", "2026-03-04", "2026-03-05", "2026-04-01",
             "2026-04-02", "2026-04-03", "2026-05-06", "2026-05-07",
             "2026-06-01", "2026-06-02", "2026-06-03", "2026-07-01"]
    _mf_db(tmp_db, dates, _rising(dates))
    c = connect(tmp_db)
    try:
        got = factor._mf_net_surprise_20d_map(c, dates[-1])
        assert got == {"600000": pytest.approx(
            (_VALS[-1] - statistics.fmean(_VALS)) / statistics.stdev(_VALS))}
    finally:
        c.close()


# ---------------------------------------------------------------------------
# C3 / C4 / C5 缺失语义：NULL / 缺行 / σ == 0 —— 一律不进结果（不补 0）
# ---------------------------------------------------------------------------

def test_c3_null_inside_the_window_drops_the_code(tmp_db):
    """C3：窗内任一 `main_net IS NULL` ⇒ 该 code **不在**结果里（`None`，不补 0）。"""
    dates = _weekdays(30)[:20]
    rows = _rising(dates)
    rows[5] = ("600000", dates[5], None)
    _mf_db(tmp_db, dates, rows)
    c = connect(tmp_db)
    try:
        got = factor._mf_net_surprise_20d_map(c, dates[19])
        assert "600000" not in got and got == {}
        assert factor.mf_net_surprise_20d(c, "600000", dates[19]) is None
    finally:
        c.close()


def test_c4_missing_row_inside_the_window_drops_the_code(tmp_db):
    """C4：20 个交易日里**少一行** ⇒ 该 code 不进结果（不是「有几日算几日」）。"""
    dates = _weekdays(30)[:20]
    rows = [("600000", d, float(k + 1)) for k, d in enumerate(dates) if k != 7]
    _mf_db(tmp_db, dates, rows)
    c = connect(tmp_db)
    try:
        got = factor._mf_net_surprise_20d_map(c, dates[19])
        assert "600000" not in got and got == {}
        assert factor.mf_net_surprise_20d(c, "600000", dates[19]) is None
    finally:
        c.close()


def test_c5_zero_stdev_is_missing_not_zero(tmp_db):
    """C5：20 个值完全相同 ⇒ `σ == 0` ⇒ **不进结果**（**不是** `0.0`）。"""
    dates = _weekdays(30)[:20]
    _mf_db(tmp_db, dates, [("600000", d, 5.0) for d in dates])
    c = connect(tmp_db)
    try:
        got = factor._mf_net_surprise_20d_map(c, dates[19])
        assert "600000" not in got
        assert got != {"600000": 0.0}
        assert factor.mf_net_surprise_20d(c, "600000", dates[19]) is None
    finally:
        c.close()


# ---------------------------------------------------------------------------
# C6 日历不足 20 个交易日 ⇒ {}
# ---------------------------------------------------------------------------

def test_c6_short_calendar_yields_empty_map(tmp_db):
    """C6：`asof` 前不足 20 个交易日 ⇒ 窗口算不出 ⇒ `_mf_net_surprise_20d_map`
    返回 `{}`（单只入口同规返回 `None`）。"""
    dates = _weekdays(30)[:19]
    asof = dates[-1]
    _mf_db(tmp_db, dates, _rising(dates))
    c = connect(tmp_db)
    try:
        assert factor._mf_net_surprise_20d_map(c, asof) == {}
        assert factor.mf_net_surprise_20d(c, "600000", asof) is None
    finally:
        c.close()


# ---------------------------------------------------------------------------
# C7 单只入口与批量映射一致
# ---------------------------------------------------------------------------

def test_c7_single_entry_matches_the_batch_map(tmp_db):
    """C7：`mf_net_surprise_20d(c, code, a) == _mf_net_surprise_20d_map(c, a)[code]`；
    缺值那只 `is None`（`_research_value` 同一支）。"""
    dates = _weekdays(30)[:20]
    rows = _rising(dates, "600000") + [("600001", d, float(k + 1))
                                       for k, d in enumerate(dates) if k != 3]
    _mf_db(tmp_db, dates, rows)
    c = connect(tmp_db)
    try:
        batch = factor._mf_net_surprise_20d_map(c, dates[19])
        assert set(batch) == {"600000"}          # 600001 缺一行 ⇒ 不进
        assert factor.mf_net_surprise_20d(c, "600000", dates[19]) == \
            batch["600000"]
        assert factor.mf_net_surprise_20d(c, "600001", dates[19]) is None
        assert factor._research_value(c, "600000", dates[19],
                                      "mf_net_surprise_20d") == batch["600000"]
        assert factor._research_value(c, "600001", dates[19],
                                      "mf_net_surprise_20d") is None
    finally:
        c.close()


def test_c7b_batch_map_ignores_members_like_the_other_market_factors(tmp_db):
    """C7 续：`_research_map(..., "mf_net_surprise_20d", members)` **忽略** `members`
    （该参数只对 `ann_count_5d` 有意义）⇒ 与直接调映射逐位相同。"""
    dates = _weekdays(30)[:20]
    _mf_db(tmp_db, dates, _rising(dates))
    c = connect(tmp_db)
    try:
        assert factor._research_map(c, dates[19], "mf_net_surprise_20d",
                                    ("999998",)) == \
            factor._mf_net_surprise_20d_map(c, dates[19])
        assert factor._research_map(c, dates[19], "mf_net_surprise_20d") == \
            factor._mf_net_surprise_20d_map(c, dates[19])
    finally:
        c.close()


# ---------------------------------------------------------------------------
# C8 诊断键自动生效（§3：零改动）
# ---------------------------------------------------------------------------

def _seed_main_net(db, synth, codes, extra_codes=()) -> None:
    """给 `codes`（＋宇宙**外**的 `extra_codes`）逐日写一条**时变**的 `main_net`。

    必须时变：恒定序列会让 `σ == 0` ⇒ 全被当缺失剔除，用例失去意义。
    """
    c = connect(db)
    try:
        for i, code in enumerate(list(codes) + list(extra_codes)):
            c.executemany(
                "INSERT INTO money_flow_daily (code, date, main_net, source,"
                " fetched_at, created_at, resp_sha256)"
                " VALUES (?,?,?,'t',?,?,'x')",
                [(code, d, float((i + 1) * 100 + k), NOW, NOW)
                 for k, d in enumerate(synth.days)])
        c.commit()
    finally:
        c.close()


def test_c8_selected_factor_carries_the_two_diagnostic_keys(
        tmp_db, tmp_path, monkeypatch):
    """C8：`--factor mf_net_surprise_20d` ⇒ 研究侧读数块里**自动**带上
    `zero_ratio_p50` / `tie_ratio_p50`（P102 的通用实现，本档零代码改动）；
    覆盖度按**宇宙成员**裁剪（宇宙外的 2 只不计）。"""
    synth = _Synth(_weekdays(600))
    _install(monkeypatch, synth)
    db = _synth_db(tmp_path / "mf.db", synth)
    _seed_main_net(db, synth, CODES[:12], extra_codes=("999998", "999999"))
    prereg = _prereg(tmp_path / "p.md",
                     factors=list(factor.MAIN_FACTORS)
                     + ["mf_net_surprise_20d"])
    c = connect(db)
    try:
        rep = factor.run_factor_ic(c, pool="short", start=START,
                                   end=synth.days[-1], prereg_path=prereg,
                                   factors=["mf_net_surprise_20d"])
    finally:
        c.close()
    assert rep["selected_factors"] == ["mf_net_surprise_20d"]
    assert rep["factor_sources"] == {"mf_net_surprise_20d": "research"}
    f = rep["research"]["factors"]["mf_net_surprise_20d"]
    assert f["value_coverage_p50"] == float(len(CODES[:12]))   # 不是 12 ＋ 2
    assert 0.0 <= f["zero_ratio_p50"] <= 1.0
    assert 0.0 <= f["tie_ratio_p50"] <= 1.0
    # 只有被选的 research 因子带这两个键；MAIN_FACTORS 的读数块不带
    assert "zero_ratio_p50" not in rep["factors"]["mom20"]


# ---------------------------------------------------------------------------
# C9 未登记名仍 fail-closed ＋ 兜底分支
# ---------------------------------------------------------------------------

def test_c9_unregistered_name_still_fails_closed(tmp_db, monkeypatch):
    """C9：`mf_net_surprise_20dx` 未登记 ⇒ `PreregError`；登记名放行；
    登记了但**没分支**的名字仍由末尾兜底 `PreregError` 挡住。"""
    assert factor.FACTOR_SOURCES["mf_net_surprise_20d"] == "research"
    with pytest.raises(xsec.PreregError):
        factor.selected_factors(["mf_net_surprise_20dx"])
    assert factor.selected_factors(["mf_net_surprise_20d"]) == \
        ["mf_net_surprise_20d"]
    dates = _weekdays(30)[:20]
    _mf_db(tmp_db, dates, _rising(dates))
    c = connect(tmp_db)
    try:
        for name in factor.RESEARCH_FACTORS:          # 每个登记名都有分支
            factor._research_map(c, dates[19], name)
        monkeypatch.setattr(factor, "RESEARCH_FACTORS",
                            factor.RESEARCH_FACTORS + ("__no_branch__",))
        with pytest.raises(xsec.PreregError):
            factor._research_map(c, dates[19], "__no_branch__")
    finally:
        c.close()


# ---------------------------------------------------------------------------
# C10 缺省逐位不变（复用 p97 夹具与黄金 digest，同一手法）
# ---------------------------------------------------------------------------

def test_c10_default_path_is_byte_identical(tmp_db, tmp_path, monkeypatch):
    """C10：不传 `--factor` ⇒ report JSON / `render_md` / `summary_line` 逐字节不变。"""
    rep = _default_report(monkeypatch, tmp_db, tmp_path)
    assert _digest(rep) == GOLDEN_REPORT_DIGEST
    assert _digest(_normalize_md(factor.render_md(rep), tmp_path / "p.md")) == \
        GOLDEN_MD_DIGEST
    assert _digest(factor.summary_line(rep)) == GOLDEN_SUMMARY_DIGEST


# ---------------------------------------------------------------------------
# C11 其它三个研究侧因子的行为逐位不变
# ---------------------------------------------------------------------------

def test_c11_the_other_three_research_factors_are_untouched(tmp_db):
    """C11：`mf_ratio_5d` / `ep_ttm` / `ann_count_5d` 在小表上的返回值与形状不变。"""
    from stocklab.store.migrate import init_db

    dates = _weekdays(30)[:10]
    asof = dates[6]                     # 第 7 个交易日 ⇒ 6 日公告窗也够
    init_db(tmp_db)
    c = connect(tmp_db)
    c.executemany(
        "INSERT INTO trading_calendar (date, is_open, source, created_at)"
        " VALUES (?,1,'t',?)", [(d, NOW) for d in dates])
    # MF-A：最近 5 个交易日（dates[2..6]）的 ratio_amount 均值（1..5 ⇒ 3.0）
    c.executemany(
        "INSERT INTO money_flow_daily (code, date, ratio_amount, source,"
        " fetched_at, created_at, resp_sha256) VALUES (?,?,?,'t',?,?,'x')",
        [("600000", d, float(k + 1), NOW, NOW)
         for k, d in enumerate(dates[2:7])])
    # VAL-A：pe_ttm > 0 ⇒ 1/pe；<= 0 ⇒ 缺失
    c.executemany(
        "INSERT INTO valuation_daily (code, date, pe_ttm, source, fetched_at,"
        " created_at, resp_sha256) VALUES (?,?,?,'t',?,?,'x')",
        [("600000", asof, 8.0, NOW, NOW),
         ("600001", asof, -2.0, NOW, NOW)])
    # EV-A：notice_date ∈ (d₋₅, d₀] =(dates[1], dates[6]] ⇒ dates[2..6]，共 5 条
    c.executemany(
        "INSERT INTO announcements (code, art_code, notice_date, title, source,"
        " fetched_at, created_at, resp_sha256)"
        " VALUES (?,?,?,'t','t',?,?,'x')",
        [("600000", f"AN{k}", d, NOW, NOW)
         for k, d in enumerate(dates[2:7])])
    c.commit()
    try:
        assert factor._research_map(c, asof, "mf_ratio_5d") == \
            {"600000": pytest.approx(3.0)}
        assert factor._research_map(c, asof, "ep_ttm") == \
            {"600000": pytest.approx(0.125)}
        assert factor._research_map(c, asof, "ann_count_5d",
                                    ["600000", "600001"]) == \
            {"600000": 5.0, "600001": 0.0}
        # 单只入口同规
        assert factor._research_value(c, "600000", asof,
                                      "mf_ratio_5d") == pytest.approx(3.0)
        assert factor._research_value(c, "600001", asof, "ep_ttm") is None
        assert factor._research_value(c, "600001", asof, "ann_count_5d") == 0.0
    finally:
        c.close()
