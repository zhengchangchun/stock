"""P104 T4：候选 #6 `pe_pct_756` 研究侧取值器的可用例（新增）。

本因子是 P95 §5.2 的 **VAL-B**：当前 `pe_ttm` 在**自身最近 756 个交易日**的
正值样本里的 **ECDF 百分位**（`#{ s ∈ S : s <= x } / |S|`，`x = pe_ttm[code, d0]`）。
它与 VAL-A（`ep_ttm`，同源不同构）的差别：VAL-A 是 `1/pe_ttm` 的**绝对值**（横截面
比较），VAL-B 是**时序自比** ⇒ 行业/规模差异在自身历史内被消掉。

口径（ADR-045 P104 追加段）：
- **ECDF 三条歧义锁死**：**含** `x` 自身 ＋ 右闭 `<=` ＋ 并列不取平均名次
  ⇒ 值域 **`(0, 1]`**（最大值样本 = `1.0`、最小值样本 = `1/|S|` > 0）；
- `d0` = `trading_calendar` 里 `<= asof` 的最后一个交易日（`asof` 允许不是交易日）；
- 缺失（一律 `None`，**不补 0、不取绝对值、不 winsorize**）：`x` 为 `NULL` 或 `<= 0`；
  `|S| < 252`；窗内 `NULL` / `<= 0` 的样本只从 `S` 剔除；
- 只读 `pe_ttm` 一列；每个 `asof` 只发 2 条 SQL（日历 ＋ 窗口）。

十四组（任务书 §4 的 C1–C14）＋ 一组 T3 证据（C15）：
C1 ECDF 手算（含最值两侧）／C2 含自身的反向自检／C3 右闭／C4 `|S|` 252 vs 251 边界／
C5 `x` 为 `NULL`/`0`/负 ⇒ `None`／C6 窗内 `0`/负/`NULL` 只从 `S` 剔除／C7 缺行只影响
本只／C8 `d0` 取 `<= asof` 的最后交易日／C9 窗序（倒序索引必红）／C10 批量 vs 单只／
C11 分派 ＋ fail-closed 兜底／C12 注册表／C13 缺省逐位不变／C14 `members` 无意义／
C15 诊断键自动生效（T3）。
"""

from __future__ import annotations

from datetime import date, timedelta

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


def _val_db(tmp_db, dates: list[str], rows: list[tuple]) -> None:
    """日历 ＋ `valuation_daily` 的夹具库；`rows` = `(code, date, pe_ttm)`。

    只插 `pe_ttm` 一列因子值，其余可空列一律留 `NULL`（本因子只读 `pe_ttm`）。
    """
    from stocklab.store.migrate import init_db

    init_db(tmp_db)
    c = connect(tmp_db)
    c.executemany(
        "INSERT INTO trading_calendar (date, is_open, source, created_at)"
        " VALUES (?,1,'t',?)", [(d, NOW) for d in dates])
    c.executemany(
        "INSERT INTO valuation_daily (code, date, pe_ttm, source, fetched_at,"
        " created_at, resp_sha256) VALUES (?,?,?,'t',?,?,'x')",
        [(code, d, pe, NOW, NOW) for code, d, pe in rows])
    c.commit()
    c.close()


def _series(dates: list[str], values: list[float | None], code: str) -> list[tuple]:
    """`dates[k]` ⇒ `values[k]` 的行序列（`None` 即插一条 `NULL`）。"""
    return [(code, d, v) for d, v in zip(dates, values)]


# ---------------------------------------------------------------------------
# C1 ECDF 手算（含「最大值 ⇒ 1.0」「最小值 ⇒ 1/|S| > 0」）
# ---------------------------------------------------------------------------

def test_c1_ecdf_is_hand_computed_including_both_extremes(tmp_db):
    """C1：`|S| = 252` 的三只标的 ⇒ 逐位断言手算值。

    - 600000：`pe = 1..252`，`d0` 的值 = `252.0` = **最大值** ⇒ `252/252 = 1.0`；
    - 600001：`d0` 的值 = `1.0` = **最小值** ⇒ `1/252`（> 0，不是 0.0）；
    - 600002：`S` 仍是 `{1..252}` 但把 `d0` 的位置换成 `100.0` ⇒ `100/252`。
    """
    dates = _weekdays(252)
    a = [float(k + 1) for k in range(252)]                    # 1..252，d0 = 252
    b = [float(k + 2) for k in range(252)]                    # 2..253
    b[251] = 1.0                                             # d0 = 1.0（最小）
    c_ = [float(k + 1) for k in range(252)]                   # 1..252
    c_[99], c_[251] = 252.0, 100.0                            # S 仍是 {1..252}，d0 = 100
    rows = _series(dates, a, "600000") + _series(dates, b, "600001") \
        + _series(dates, c_, "600002")
    _val_db(tmp_db, dates, rows)
    c = connect(tmp_db)
    try:
        got = factor._pe_pct_756_map(c, dates[251])
        assert got["600000"] == 1.0
        assert got["600001"] == 1 / 252
        assert got["600002"] == 100 / 252
        assert 0.0 < got["600001"] <= 1.0        # 最小值样本不为 0
    finally:
        c.close()


# ---------------------------------------------------------------------------
# C2 「含自身」的反向自检（剔掉 x 会得到不同的数）
# ---------------------------------------------------------------------------

def test_c2_current_value_is_counted_in_its_own_reference_set(tmp_db):
    """C2：若实现把 `x` 从 `S` 里剔掉，`100/252` 会变成 `99/251` ⇒ 用例必须能区分。

    这是「含自身」的**反向自检**：两条读数不同才说明用例有区分力。
    """
    dates = _weekdays(252)
    vals = [float(k + 1) for k in range(252)]                 # 1..252（互异）
    vals[99], vals[251] = 252.0, 100.0                        # S 仍是 {1..252}，d0 = 100
    _val_db(tmp_db, dates, _series(dates, vals, "600000"))
    c = connect(tmp_db)
    try:
        got = factor._pe_pct_756_map(c, dates[251])["600000"]
        assert got == 100 / 252                              # 含自身
        assert got != 99 / 251                               # 不含自身会是这个数
        assert abs(got - 99 / 251) > 1e-4                    # 两者确实不同
    finally:
        c.close()


# ---------------------------------------------------------------------------
# C3 右闭（`<=` 而非 `<`）
# ---------------------------------------------------------------------------

def test_c3_boundary_is_closed_on_the_right(tmp_db):
    """C3：`S = {1.0} ∪ {2.0 × 251}`、`x = 2.0` ⇒ `<=` 把等于 `x` 的样本计入分子。

    `<=` ⇒ `252/252 = 1.0`；若实现用 `<` ⇒ `1/252`（用例红）。
    """
    dates = _weekdays(252)
    vals = [1.0] + [2.0] * 251
    _val_db(tmp_db, dates, _series(dates, vals, "600000"))
    c = connect(tmp_db)
    try:
        got = factor._pe_pct_756_map(c, dates[251])["600000"]
        assert got == 1.0
        assert got != 1 / 252
    finally:
        c.close()


# ---------------------------------------------------------------------------
# C4 `|S|` 边界：252 ⇒ 有值；251 ⇒ None（两种减到 251 的成因各一条）
# ---------------------------------------------------------------------------

def test_c4_min_samples_gate_sits_on_the_positive_sample_count(tmp_db):
    """C4：`|S| = 252` ⇒ 有值；`|S| = 251` ⇒ `None`（边界两侧）。

    - 600001：窗内**行齐全**，但一天 `pe_ttm = 0.0` ⇒ 只从 `S` 剔除 ⇒ `|S| = 251`；
    - 600002：窗内**缺一行** ⇒ `|S| = 251`。
    两者 `d0` 的行都在且 `> 0` ⇒ `None` 只能来自 `|S| < 252` 这一条闸门。
    """
    dates = _weekdays(252)
    ok = [3.0] * 252
    zero = [3.0] * 252
    zero[10] = 0.0
    missing = [(d, 3.0) for k, d in enumerate(dates) if k != 10]
    rows = _series(dates, ok, "600000") + _series(dates, zero, "600001") \
        + [("600002", d, v) for d, v in missing]
    _val_db(tmp_db, dates, rows)
    c = connect(tmp_db)
    try:
        got = factor._pe_pct_756_map(c, dates[251])
        assert got["600000"] == 1.0
        assert "600001" not in got
        assert "600002" not in got
        assert factor.pe_pct_756(c, "600001", dates[251]) is None
        assert factor.pe_pct_756(c, "600002", dates[251]) is None
    finally:
        c.close()


# ---------------------------------------------------------------------------
# C5 `x` 为 NULL / 0 / 负 ⇒ None（不取绝对值、不补 0）
# ---------------------------------------------------------------------------

def test_c5_non_positive_or_null_current_value_is_missing(tmp_db):
    """C5：窗内前 252 日全是 `5.0`（⇒ `|S| = 252` 已过闸），只有 `d0` 那天异常：
    `NULL` / `0.0` / `-3.0` ⇒ 三只都 `None` —— 把 `None` 归因到 `x` 而非 `|S|`。

    若实现把 `-3.0` 取绝对值当 `3.0`、或把 `0.0` 当样本，都会返回一个数（用例红）。
    另加一只 `d0 = 5.0` 的正对照，证明夹具本身能出值。
    """
    dates = _weekdays(253)
    rows = []
    for code, bad in (("600000", None), ("600001", 0.0), ("600002", -3.0)):
        vals = [5.0] * 252 + [bad]
        rows += _series(dates, vals, code)
    rows += _series(dates, [5.0] * 253, "600003")
    _val_db(tmp_db, dates, rows)
    c = connect(tmp_db)
    try:
        got = factor._pe_pct_756_map(c, dates[252])
        assert got["600003"] == 1.0                 # 正对照：夹具能出值
        for code in ("600000", "600001", "600002"):
            assert code not in got
            assert factor.pe_pct_756(c, code, dates[252]) is None
    finally:
        c.close()


# ---------------------------------------------------------------------------
# C6 窗内 0 / 负 / NULL 只从参照集剔除（≠ 当 0、≠ 取绝对值）
# ---------------------------------------------------------------------------

def test_c6_non_positive_window_samples_are_dropped_not_zeroed(tmp_db):
    """C6：窗内除 `d0` 外全是 `10.0`，另夹 `0.0` / `-5.0` / `NULL` 各一天。

    正确（只剔除）⇒ `S = {10.0 × 251, 5.0}`、`|S| = 252`、`#{s <= 5} = 1`
    ⇒ `1/252`。若把 `0.0` 或 `|-5.0|` 当样本 ⇒ `2/253` 或 `3/254`（用例红）。
    """
    dates = _weekdays(255)
    vals = [10.0] * 251 + [0.0, -5.0, None, 5.0]
    _val_db(tmp_db, dates, _series(dates, vals, "600000"))
    c = connect(tmp_db)
    try:
        got = factor._pe_pct_756_map(c, dates[254])["600000"]
        assert got == 1 / 252
        assert got != 2 / 253            # 把 0.0 当样本
        assert got != 3 / 254            # 把 0.0 与 |-5.0| 都当样本
        assert got < 0.01
    finally:
        c.close()


# ---------------------------------------------------------------------------
# C7 缺行只影响本只标的的 |S|
# ---------------------------------------------------------------------------

def test_c7_missing_rows_only_shrink_that_code_reference_set(tmp_db):
    """C7：600001 窗内缺 2 行 ⇒ `|S| = 250 < 252` ⇒ `None`，**不影响** 600000 的值。"""
    dates = _weekdays(252)
    rows = _series(dates, [4.0] * 252, "600000")
    rows += [("600001", d, 4.0) for k, d in enumerate(dates) if k not in (5, 200)]
    _val_db(tmp_db, dates, rows)
    c = connect(tmp_db)
    try:
        got = factor._pe_pct_756_map(c, dates[251])
        assert got == {"600000": 1.0}
        assert factor.pe_pct_756(c, "600001", dates[251]) is None
    finally:
        c.close()


# ---------------------------------------------------------------------------
# C8 `d0` = `<= asof` 的最后一个交易日（asof 可以不是交易日）
# ---------------------------------------------------------------------------

def test_c8_d0_is_the_last_trading_day_at_or_before_asof(tmp_db):
    """C8：`asof` 传一个**不在日历里**的自然日 ⇒ `d0` 取 `<= asof` 的最后交易日。

    与 P103 同一手法（VAL-A 用 `date == asof` 等式，本档用 `<= asof` 的 `d0`）。
    """
    dates = _weekdays(253)
    vals = [float(k + 1) for k in range(253)]
    _val_db(tmp_db, dates, _series(dates, vals, "600000"))
    asof = (date.fromisoformat(dates[-1]) + timedelta(days=1)).isoformat()
    assert asof not in dates                      # 日历外
    c = connect(tmp_db)
    try:
        assert factor._pe_pct_756_map(c, asof) == \
            factor._pe_pct_756_map(c, dates[-1])
        assert factor.pe_pct_756(c, "600000", asof) == \
            factor.pe_pct_756(c, "600000", dates[-1])
    finally:
        c.close()


# ---------------------------------------------------------------------------
# C9 窗序：倒序索引必红（翻升序后取 `[-1]`）
# ---------------------------------------------------------------------------

def test_c9_window_is_sorted_ascending_before_taking_the_last_day(tmp_db):
    """C9：`_recent_trading_dates` 返回**倒序**（最近的在前）。

    把窗内**最旧**那天的 `pe_ttm` 设成 `1e6`：正确实现（翻升序 ⇒ `d0` = 最新那天）
    ⇒ `x = 252.0` ⇒ `#{s <= 252} = 251` ⇒ `251/252`；若照字面用倒序的 `window[-1]`
    ⇒ `x = 1e6` ⇒ `252/252 = 1.0`（用例红）。
    """
    dates = _weekdays(252)
    vals = [float(k + 1) for k in range(252)]
    vals[0] = 1e6                                  # 最旧那天（倒序索引会取到它）
    _val_db(tmp_db, dates, _series(dates, vals, "600000"))
    c = connect(tmp_db)
    try:
        got = factor._pe_pct_756_map(c, dates[251])["600000"]
        assert got == 251 / 252
        assert got != 1.0
    finally:
        c.close()


# ---------------------------------------------------------------------------
# C10 批量 `_pe_pct_756_map` 与单只 `pe_pct_756` 逐位相同
# ---------------------------------------------------------------------------

def test_c10_batch_map_matches_the_single_code_entry(tmp_db):
    """C10：同一 `(code, asof)` 上批量与单只**逐位相同**；缺值那只 `is None`。"""
    dates = _weekdays(252)
    rows = _series(dates, [float(k + 1) for k in range(252)], "600000") \
        + _series(dates, [float(252 - k) for k in range(252)], "600001") \
        + _series(dates, [7.0] * 250, "600002")     # 缺 2 行 ⇒ |S| = 250
    _val_db(tmp_db, dates, rows)
    c = connect(tmp_db)
    try:
        batch = factor._pe_pct_756_map(c, dates[251])
        assert set(batch) == {"600000", "600001"}
        for code in batch:
            assert factor.pe_pct_756(c, code, dates[251]) == batch[code]
        assert factor.pe_pct_756(c, "600002", dates[251]) is None
    finally:
        c.close()


# ---------------------------------------------------------------------------
# C11 分派 ＋ fail-closed 兜底
# ---------------------------------------------------------------------------

def test_c11_dispatch_and_fail_closed_fallback(tmp_db, monkeypatch):
    """C11：`_research_map` / `_research_value` 分派到新因子；未登记名仍 `PreregError`；
    登记了但**没分支**的名字仍由末尾兜底挡住。"""
    dates = _weekdays(252)
    _val_db(tmp_db, dates, _series(dates, [float(k + 1) for k in range(252)],
                                   "600000"))
    c = connect(tmp_db)
    try:
        assert factor._research_map(c, dates[251], "pe_pct_756") == \
            factor._pe_pct_756_map(c, dates[251])
        assert factor._research_value(c, "600000", dates[251], "pe_pct_756") == \
            factor._pe_pct_756_map(c, dates[251])["600000"]
        for name in factor.RESEARCH_FACTORS:          # 每个登记名都有分支
            factor._research_map(c, dates[251], name)
        monkeypatch.setattr(factor, "RESEARCH_FACTORS",
                            factor.RESEARCH_FACTORS + ("__no_branch__",))
        with pytest.raises(xsec.PreregError):
            factor._research_map(c, dates[251], "__no_branch__")
    finally:
        c.close()
    assert factor.selected_factors(["pe_pct_756"]) == ["pe_pct_756"]
    with pytest.raises(xsec.PreregError):
        factor.selected_factors(["pe_pct_756x"])


# ---------------------------------------------------------------------------
# C12 注册表
# ---------------------------------------------------------------------------

def test_c12_registry_entry_and_overlap_rules(tmp_db):
    """C12：`FACTOR_SOURCES["pe_pct_756"] == "research"`；在 `RESEARCH_FACTORS` 里；
    与 `PROMOTABLE_FACTORS` 无交集；**不**进 `FACTOR_FEATURE_KEYS`。"""
    from stocklab.candidate.run import FACTOR_FEATURE_KEYS
    assert factor.FACTOR_SOURCES["pe_pct_756"] == "research"
    assert "pe_pct_756" in factor.RESEARCH_FACTORS
    assert set(factor.RESEARCH_FACTORS) == {"mf_ratio_5d", "ep_ttm",
                                            "ann_count_5d",
                                            "mf_net_surprise_20d",
                                            "pe_pct_756"}
    assert not (set(factor.RESEARCH_FACTORS) & set(factor.PROMOTABLE_FACTORS))
    assert "pe_pct_756" not in FACTOR_FEATURE_KEYS
    assert "pe_pct_756" not in factor.SECONDARY_FACTORS
    assert "pe_pct_756" not in factor.MAIN_FACTORS


# ---------------------------------------------------------------------------
# C13 缺省逐位不变（L9：复用 p97 夹具与黄金 digest）
# ---------------------------------------------------------------------------

def test_c13_default_path_is_byte_identical(tmp_db, tmp_path, monkeypatch):
    """C13：不传 `--factor` ⇒ report JSON / `render_md` / `summary_line` 逐字节不变
    （多登记一个名字**不得**改动缺省路径的任何字段／顺序）。"""
    rep = _default_report(monkeypatch, tmp_db, tmp_path)
    assert _digest(rep) == GOLDEN_REPORT_DIGEST
    assert _digest(_normalize_md(factor.render_md(rep), tmp_path / "p.md")) == \
        GOLDEN_MD_DIGEST
    assert _digest(factor.summary_line(rep)) == GOLDEN_SUMMARY_DIGEST


# ---------------------------------------------------------------------------
# C14 `members` 对本分支无意义（传与不传同值）
# ---------------------------------------------------------------------------

def test_c14_members_is_ignored_by_the_new_branch(tmp_db):
    """C14：`_research_map(..., "pe_pct_756", members)` 与不传 `members` **逐位相同**。"""
    dates = _weekdays(252)
    vals = [float(k + 1) for k in range(252)]
    _val_db(tmp_db, dates, _series(dates, vals, "600000")
            + _series(dates, vals, "999998"))       # 宇宙外的标的也在结果里
    c = connect(tmp_db)
    try:
        plain = factor._research_map(c, dates[251], "pe_pct_756")
        with_members = factor._research_map(c, dates[251], "pe_pct_756",
                                            ("600000",))
        assert with_members == plain
        assert with_members == factor._pe_pct_756_map(c, dates[251])
    finally:
        c.close()


# ---------------------------------------------------------------------------
# C15 诊断键自动生效（T3：零代码改动）
# ---------------------------------------------------------------------------

def _seed_pe(db, synth, codes) -> None:
    """给 `codes` 逐日写一条**时变**的 `pe_ttm`（`> 0` ⇒ 参照集才够 252）。"""
    c = connect(db)
    try:
        for i, code in enumerate(codes):
            c.executemany(
                "INSERT INTO valuation_daily (code, date, pe_ttm, source,"
                " fetched_at, created_at, resp_sha256)"
                " VALUES (?,?,?,'t',?,?,'x')",
                [(code, d, float((i + 1) * 10 + k), NOW, NOW)
                 for k, d in enumerate(synth.days)])
        c.commit()
    finally:
        c.close()


def test_c15_selected_factor_carries_the_two_diagnostic_keys(
        tmp_db, tmp_path, monkeypatch):
    """C15（T3）：`--factor pe_pct_756` ⇒ 研究侧读数块里**自动**带上
    `zero_ratio_p50` / `tie_ratio_p50`（P102 的通用实现 + `source=research` 分流，
    本档零代码改动）；`MAIN_FACTORS` 的读数块不带这两个键。"""
    synth = _Synth(_weekdays(600))
    _install(monkeypatch, synth)
    db = _synth_db(tmp_path / "pe.db", synth)
    _seed_pe(db, synth, CODES[:12])
    prereg = _prereg(tmp_path / "p.md",
                     factors=list(factor.MAIN_FACTORS) + ["pe_pct_756"])
    c = connect(db)
    try:
        rep = factor.run_factor_ic(c, pool="short", start=START,
                                   end=synth.days[-1], prereg_path=prereg,
                                   factors=["pe_pct_756"])
    finally:
        c.close()
    assert rep["selected_factors"] == ["pe_pct_756"]
    assert rep["factor_sources"] == {"pe_pct_756": "research"}
    f = rep["research"]["factors"]["pe_pct_756"]
    assert f["value_coverage_p50"] == float(len(CODES[:12]))
    assert 0.0 <= f["zero_ratio_p50"] <= 1.0
    assert 0.0 <= f["tie_ratio_p50"] <= 1.0
    assert "zero_ratio_p50" not in rep["factors"]["mom20"]
