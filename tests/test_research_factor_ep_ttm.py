"""P101 T3：候选 #3 `ep_ttm` 研究侧取值器的可用例（新增，旧文件不改）。

八组（任务书 §3）：

1. `pe_ttm > 0` ⇒ 值 = `1/pe_ttm`（**手算值**，不是「不是 None」）。
2. `pe_ttm = 0` / `< 0` / `NULL` ⇒ 不在 `_ep_ttm_map` 键里（`ep_ttm(...)` 返 `None`）。
3. **PIT 锚是等式** `date == asof`：同一只标的在 `asof` 前天／后天各有行 ⇒ 取到的是
   `asof` 那行（且**只有** `asof` 那行能取到 —— 前一天的值再诱人也不许泄漏）。
4. 批量 vs 单只一致：`_ep_ttm_map` 与逐只 `ep_ttm` 逐键相同。
5. **SQL 条数不随标的数增长**：连接级 `set_trace_callback` 证明只发 1 条业务 SQL。
6. 未登记名字仍 fail-closed：`pe_pct_756` / `ann_count_5d` / `mf_net_surprise_20d`
   **本档都没有**登记 ⇒ `selected_factors` 一律 `PreregError`。
7. 登记完整性：`RESEARCH_FACTORS` 里每个名字在 `_research_map` 都有分支 ——
   反向自检（把假名字临时塞进常量后调 `_research_map` 必抛，P97 的既有手法）。
8. 缺省逐位不变（L7）：不传 `--factor` 时 report JSON / `render_md` / `summary_line`
   与 P97 交付后逐字节相同 —— **复用 p97 的夹具与黄金 digest**，不另造一套。
"""

from __future__ import annotations

import pytest

from stocklab.research import factor, xsec
from stocklab.store.db import connect
from tests.test_research_factor_p97 import (
    GOLDEN_MD_DIGEST, GOLDEN_REPORT_DIGEST, GOLDEN_SUMMARY_DIGEST,
    _default_report, _digest, _normalize_md,
)
from tests.test_research_signal import _weekdays

NOW = "2026-09-29T00:00:00+08:00"


def _ep_db(tmp_db, rows: list[tuple], dates: list[str] | None = None):
    """日历（可选）＋ `valuation_daily` 的夹具库，`rows` = `(code, date, pe_ttm)`。

    只插 `pe_ttm` 一列因子值，其余可空列一律留 `NULL`（本因子只读 `pe_ttm`）。
    """
    from stocklab.store.migrate import init_db

    init_db(tmp_db)
    c = connect(tmp_db)
    if dates:
        c.executemany(
            "INSERT INTO trading_calendar (date, is_open, source, created_at)"
            " VALUES (?,1,'t',?)", [(d, NOW) for d in dates])
    c.executemany(
        "INSERT INTO valuation_daily (code, date, pe_ttm, source, fetched_at,"
        " created_at, resp_sha256) VALUES (?,?,?,'t',?,?,'x')",
        [(code, d, pe, NOW, NOW) for code, d, pe in rows])
    c.commit()
    c.close()


# ---------------------------------------------------------------------------
# 1. 逐字公式：pe_ttm > 0 ⇒ 1 / pe_ttm（手算值）
# ---------------------------------------------------------------------------

def test_ep_ttm_is_reciprocal_of_positive_pe(tmp_db):
    """① `pe_ttm = 8.0` ⇒ `0.125`；`pe_ttm = 4.0` ⇒ `0.25`（手算）。"""
    dates = _weekdays(10)
    _ep_db(tmp_db, [("600000", dates[0], 8.0), ("600001", dates[0], 4.0)], dates)
    c = connect(tmp_db)
    try:
        assert factor.ep_ttm(c, "600000", dates[0]) == pytest.approx(0.125)
        assert factor.ep_ttm(c, "600001", dates[0]) == pytest.approx(0.25)
        assert factor._ep_ttm_map(c, dates[0]) == {
            "600000": pytest.approx(0.125), "600001": pytest.approx(0.25)}
    finally:
        c.close()


# ---------------------------------------------------------------------------
# 2. 缺失语义：0 / 负 / NULL ⇒ 不进结果集（不补 0）
# ---------------------------------------------------------------------------

def test_ep_ttm_drops_zero_negative_and_null_pe(tmp_db):
    """② 亏损（负 PE）判缺失、`pe_ttm = 0` 判缺失、`NULL` 判缺失 —— 三者都不补 0。

    `pe_ttm < 0` 尤其重要：取绝对值会把亏损公司排成「极便宜」（方向错）。
    """
    dates = _weekdays(10)
    _ep_db(tmp_db, [
        ("600000", dates[0], 10.0),     # 唯一有效
        ("600001", dates[0], 0.0),
        ("600002", dates[0], -50.0),
        ("600003", dates[0], None),
    ], dates)
    c = connect(tmp_db)
    try:
        got = factor._ep_ttm_map(c, dates[0])
        assert got == {"600000": pytest.approx(0.1)}
        for code in ("600001", "600002", "600003"):
            assert code not in got
            assert factor.ep_ttm(c, code, dates[0]) is None
    finally:
        c.close()


# ---------------------------------------------------------------------------
# 3. PIT 锚是等式 `date == asof`（不是 `<=`）
# ---------------------------------------------------------------------------

def test_ep_ttm_pit_anchor_is_equality_not_less_or_equal(tmp_db):
    """③ `asof` 前天／后天各有一行、值不同 ⇒ 取到的**恰好**是 `date == asof` 那行。

    若实现写成 `date <= asof`（像 `mf_ratio_5d` 那样），会取到 `asof` 前天那行
    （`1/10 = 0.1`）⇒ 本用例红。
    """
    dates = _weekdays(10)
    asof = dates[2]
    _ep_db(tmp_db, [
        ("600000", dates[1], 10.0),     # 昨天：诱饵
        ("600000", asof, 20.0),         # 今天：唯一应取
        ("600000", dates[3], 5.0),      # 明天：诱饵（PIT 不许读未来）
    ], dates)
    c = connect(tmp_db)
    try:
        assert factor.ep_ttm(c, "600000", asof) == pytest.approx(0.05)
    finally:
        c.close()


def test_ep_ttm_is_none_when_only_neighbouring_dates_have_rows(tmp_db):
    """③ 续：只有昨天／明天有行、`asof` 当天零行 ⇒ `None`（等式锚的必然推论）。"""
    dates = _weekdays(10)
    _ep_db(tmp_db, [
        ("600000", dates[1], 10.0),
        ("600000", dates[3], 5.0),
    ], dates)
    c = connect(tmp_db)
    try:
        assert factor.ep_ttm(c, "600000", dates[2]) is None
    finally:
        c.close()


# ---------------------------------------------------------------------------
# 4. 批量 vs 单只一致
# ---------------------------------------------------------------------------

def test_ep_ttm_map_matches_the_single_code_entry(tmp_db):
    """④ `_ep_ttm_map` 的逐键值与逐只调 `ep_ttm` 相同（防单只入口另走一条口径）。"""
    dates = _weekdays(10)
    codes = [f"60000{i}" for i in range(6)]
    rows = [(code, dates[0], 5.0 + i) for i, code in enumerate(codes)]
    rows.append(("600099", dates[0], -3.0))          # 缺失的那只
    _ep_db(tmp_db, rows, dates)
    c = connect(tmp_db)
    try:
        batch = factor._ep_ttm_map(c, dates[0])
        singles = {code: factor.ep_ttm(c, code, dates[0]) for code in codes}
        assert set(batch) == set(codes)
        assert singles == {code: batch[code] for code in codes}
        assert factor.ep_ttm(c, "600099", dates[0]) is None
    finally:
        c.close()


# ---------------------------------------------------------------------------
# 5. SQL 条数不随标的数增长
# ---------------------------------------------------------------------------

def test_ep_ttm_map_issues_exactly_one_sql_for_the_whole_cross_section(tmp_db):
    """⑤ 连接级 `set_trace_callback` 计数：一次 `_ep_ttm_map` 只发 **1** 条 SQL。

    8 只标的同日 ⇒ 结果 8 键，而 SQL 仍只 1 条（一次覆盖全截面；不许逐只查）。
    `connect()` 的 PRAGMA 发生在回调注册之前，不进计数。
    """
    dates = _weekdays(10)
    codes = [f"60000{i}" for i in range(8)]
    _ep_db(tmp_db, [(code, dates[0], 6.0 + i)
                    for i, code in enumerate(codes)], dates)
    c = connect(tmp_db)
    try:
        stmts: list[str] = []
        c.set_trace_callback(stmts.append)
        got = factor._ep_ttm_map(c, dates[0])
        c.set_trace_callback(None)
        assert len(got) == 8
        assert len(stmts) == 1, f"只许 1 条 SQL，实发 {len(stmts)}：{stmts}"
        assert "valuation_daily" in stmts[0]
    finally:
        c.close()


# ---------------------------------------------------------------------------
# 6. 未登记名字仍 fail-closed（本档只登记 ep_ttm）
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("name", ["pe_pct_756", "ann_count_5d",
                                  "mf_net_surprise_20d", "ep_ttm_x"])
def test_unregistered_research_names_still_fail_closed(name):
    """⑥ 本档只登记 `ep_ttm`：其余候选名字一律 `PreregError`（exit 2、零输出）。"""
    assert name not in factor.FACTOR_SOURCES
    with pytest.raises(xsec.PreregError):
        factor.selected_factors([name])


def test_ep_ttm_is_the_only_added_name_versus_p97():
    """⑥ 续：`RESEARCH_FACTORS` 相对 P97 只多 `ep_ttm` 一个名字。"""
    assert set(factor.RESEARCH_FACTORS) == {"mf_ratio_5d", "ep_ttm"}
    assert factor.FACTOR_SOURCES["ep_ttm"] == "research"
    assert "ep_ttm" not in factor.FACTOR_FEATURE_KEYS


# ---------------------------------------------------------------------------
# 7. 登记完整性（反向自检）
# ---------------------------------------------------------------------------

def test_every_registered_research_factor_has_a_dispatch_branch(
        tmp_db, monkeypatch):
    """⑦ 每个登记名都能在 `_research_map` 找到分支；假名字塞进常量后仍 fail-closed。"""
    dates = _weekdays(10)
    _ep_db(tmp_db, [("600000", dates[0], 10.0)], dates)
    c = connect(tmp_db)
    try:
        for name in factor.RESEARCH_FACTORS:
            factor._research_map(c, dates[0], name)      # 有分支 ⇒ 不抛
        monkeypatch.setattr(factor, "RESEARCH_FACTORS",
                            factor.RESEARCH_FACTORS + ("__no_branch__",))
        with pytest.raises(xsec.PreregError):
            factor._research_map(c, dates[0], "__no_branch__")
    finally:
        c.close()


# ---------------------------------------------------------------------------
# 8. 缺省逐位不变（L7）：复用 p97 的黄金 digest，不另造一套
# ---------------------------------------------------------------------------

def test_default_path_is_byte_identical_after_registering_ep_ttm(
        tmp_db, tmp_path, monkeypatch):
    """⑧ 多登记一个名字**不得**改动缺省路径的任何字段／顺序（P97 的三条黄金 digest）。"""
    rep = _default_report(monkeypatch, tmp_db, tmp_path)
    assert _digest(rep) == GOLDEN_REPORT_DIGEST
    assert _digest(_normalize_md(factor.render_md(rep), tmp_path / "p.md")) == \
        GOLDEN_MD_DIGEST
    assert _digest(factor.summary_line(rep)) == GOLDEN_SUMMARY_DIGEST


def test_default_report_still_carries_no_new_key(tmp_db, tmp_path, monkeypatch):
    """⑧ 续：缺省路径**一个新键都不出现**（`research` 段只有传 `--factor` 才有）。"""
    rep = _default_report(monkeypatch, tmp_db, tmp_path)
    for key in ("research", "factor_tag", "selected_factors", "factor_sources",
                "verdict"):
        assert key not in rep, f"缺省路径不得新增键 {key}"
