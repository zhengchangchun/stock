"""P102 T4：候选 #4 `ann_count_5d` 研究侧取值器的可用例（新增）。

窗口是 **`(d₋₅, d₀]` 的日期区间**（P95 §5.4 EV-A），不是「最近 5 个交易日的闭集」：
`announcements` 里 19.5% 的行 `notice_date` 落在周六/周日（P102 §0.6 实测），闭集
读法会把它们静默丢弃。本因子还有一条与前三档**不同**的语义 —— **「0 是真值」**
（窗口内没公告 ⇒ `0.0`，不是 `None`）。

十二组（任务书 §4 的 C1–C12）：

1. 窗口边界：`d₋₅` 不计 / `d₋₄` 计 / `d₀` 计 / `d₀+1` 不计；
2. 周末公告（非交易日但落在窗口内）计入；
3. 「0 是真值」：成员全给值，无公告即 `0.0`；
4. 库外 code 给 `0.0`、不抛；
5. 日历不足 6 个交易日 ⇒ `{}`；
6. 单只入口无公告 ⇒ `0.0`（`float`，不是 `None`）；
7. 单只入口与批量映射逐位一致；
8. `_research_map` 的分派与 `members` 透传（`members=None` ⇒ 空成员集）；
9. 未登记名仍 fail-closed；
10. 缺省逐位不变（复用 p97 夹具与黄金 digest）；
11. 两个诊断键的算法（手算值）；
12. 诊断键不在缺省路径出现，且被选时确实落在研究侧读数块里。
"""

from __future__ import annotations

import json

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

#: 夹具日历：`_weekdays(12, START)` 的前 12 个交易日。`asof` 取第 8 个
#: （`2015-01-12`，周一）⇒ 窗口 = `(2015-01-05, 2015-01-12]`，其间夹着
#: `2015-01-10`（周六）与 `2015-01-11`（周日）。
_ASOF_IDX = 7


def _cal() -> list[str]:
    return _weekdays(12, START)


def _ann_db(tmp_db, dates: list[str], rows: list[tuple]):
    """日历 ＋ `announcements` 的夹具库；`rows` = `(code, art_code, notice_date)`。"""
    from stocklab.store.migrate import init_db

    init_db(tmp_db)
    c = connect(tmp_db)
    c.executemany(
        "INSERT INTO trading_calendar (date, is_open, source, created_at)"
        " VALUES (?,1,'t',?)", [(d, NOW) for d in dates])
    c.executemany(
        "INSERT INTO announcements (code, art_code, notice_date, title, source,"
        " fetched_at, created_at, resp_sha256) VALUES (?,?,?,'t','t',?,?,'x')",
        [(code, art, d, NOW, NOW) for code, art, d in rows])
    c.commit()
    c.close()


# ---------------------------------------------------------------------------
# C1 / C2 窗口是 (d₋₅, d₀] 的日期区间
# ---------------------------------------------------------------------------

def test_c1_window_is_half_open_date_interval(tmp_db):
    """C1：`d₋₅`（左端那天）不计、`d₋₄` 计、`d₀` 计、`d₀+1`（自然日）不计。"""
    cal = _cal()
    asof = cal[_ASOF_IDX]
    left, d_m4, d_p1 = cal[_ASOF_IDX - 5], cal[_ASOF_IDX - 4], "2015-01-13"
    assert (left, d_m4, asof) == ("2015-01-05", "2015-01-06", "2015-01-12")
    _ann_db(tmp_db, cal, [
        ("600001", "AN1", left),        # d₋₅：开区间左端 ⇒ 不计
        ("600002", "AN2", d_m4),        # d₋₄：计
        ("600003", "AN3", asof),        # d₀：计
        ("600004", "AN4", d_p1),        # d₀+1 自然日：不计
    ])
    c = connect(tmp_db)
    try:
        got = factor._ann_count_5d_map(
            c, asof, ["600001", "600002", "600003", "600004"])
        assert got == {"600001": 0.0, "600002": 1.0, "600003": 1.0, "600004": 0.0}
    finally:
        c.close()


def test_c2_weekend_notice_inside_the_window_is_counted(tmp_db):
    """C2：`notice_date` 落在窗口内的周六（非交易日）**计入**（区间读法）。

    集合读法（只数那 5 个交易日当天的公告）会把它丢掉 —— 那是 19.5% 的样本。
    """
    cal = _cal()
    asof = cal[_ASOF_IDX]
    _ann_db(tmp_db, cal, [("600005", "AN5", "2015-01-10")])   # 周六
    c = connect(tmp_db)
    try:
        assert factor._ann_count_5d_map(c, asof, ["600005"]) == {"600005": 1.0}
        assert factor.ann_count_5d(c, "600005", asof) == 1.0
    finally:
        c.close()


# ---------------------------------------------------------------------------
# C3 / C4 「0 是真值」与库外 code
# ---------------------------------------------------------------------------

def test_c3_zero_is_a_real_value_for_every_member(tmp_db):
    """C3：3 个成员只有 1 只有公告 ⇒ 仍是 3 个键，另 2 只为 `0.0`（不是缺失）。"""
    cal = _cal()
    asof = cal[_ASOF_IDX]
    _ann_db(tmp_db, cal, [("600002", "AN2", cal[_ASOF_IDX - 4])])
    c = connect(tmp_db)
    try:
        got = factor._ann_count_5d_map(c, asof, ["600001", "600002", "600003"])
        assert set(got) == {"600001", "600002", "600003"}
        assert got == {"600001": 0.0, "600002": 1.0, "600003": 0.0}
        assert all(v is not None for v in got.values())
    finally:
        c.close()


def test_c4_codes_outside_the_db_still_get_zero(tmp_db):
    """C4：`members` 含库里没有的 code ⇒ 给 `0.0`、不抛。"""
    cal = _cal()
    asof = cal[_ASOF_IDX]
    _ann_db(tmp_db, cal, [("600002", "AN2", asof)])
    c = connect(tmp_db)
    try:
        got = factor._ann_count_5d_map(c, asof, ["999998", "600002"])
        assert got == {"999998": 0.0, "600002": 1.0}
    finally:
        c.close()


# ---------------------------------------------------------------------------
# C5 日历不足
# ---------------------------------------------------------------------------

def test_c5_short_calendar_yields_empty_map(tmp_db):
    """C5：`asof` 前不足 6 个交易日 ⇒ 窗口算不出 ⇒ `_ann_count_5d_map` 返回 `{}`。"""
    cal = _cal()[:3]
    asof = cal[-1]
    _ann_db(tmp_db, cal, [("600001", "AN1", asof)])
    c = connect(tmp_db)
    try:
        assert factor._ann_count_5d_map(c, asof, ["600001"]) == {}
        # 单只入口同规：空窗 ⇒ 真 0（永不 None）
        assert factor.ann_count_5d(c, "600001", asof) == 0.0
    finally:
        c.close()


# ---------------------------------------------------------------------------
# C6 / C7 单只入口
# ---------------------------------------------------------------------------

def test_c6_single_entry_is_zero_float_when_no_announcement(tmp_db):
    """C6：`ann_count_5d(conn, code, asof)` 无公告 ⇒ `0.0` 且 `isinstance(float)`。"""
    cal = _cal()
    asof = cal[_ASOF_IDX]
    _ann_db(tmp_db, cal, [])
    c = connect(tmp_db)
    try:
        v = factor.ann_count_5d(c, "600001", asof)
        assert isinstance(v, float) and v == 0.0
    finally:
        c.close()


def test_c7_single_entry_matches_the_batch_map(tmp_db):
    """C7：单只入口与批量映射逐位一致（防单只入口另走一条口径）。"""
    cal = _cal()
    asof = cal[_ASOF_IDX]
    _ann_db(tmp_db, cal, [
        ("600001", "AN1", cal[_ASOF_IDX - 4]),
        ("600001", "AN2", asof),
        ("600002", "AN3", asof),
    ])
    c = connect(tmp_db)
    try:
        batch = factor._ann_count_5d_map(c, asof, ["600001", "600002", "600003"])
        for code in ("600001", "600002", "600003"):
            assert factor.ann_count_5d(c, code, asof) == batch[code]
        assert batch["600001"] == 2.0
    finally:
        c.close()


# ---------------------------------------------------------------------------
# C8 分派与 members 透传
# ---------------------------------------------------------------------------

def test_c8_research_map_dispatches_and_passes_members_through(tmp_db):
    """C8：`_research_map(..., name="ann_count_5d", members)` 结果与直接调逐位相同。

    另钉 `members=None` 那条探测路径：**按空成员集处理** ⇒ 只返回有公告的标的行。
    """
    cal = _cal()
    asof = cal[_ASOF_IDX]
    _ann_db(tmp_db, cal, [
        ("600002", "AN2", cal[_ASOF_IDX - 4]),
        ("600003", "AN3", asof),
        ("600001", "AN1", cal[_ASOF_IDX - 5]),      # 左端那天 ⇒ 不入窗口
    ])
    members = ["600001", "600002", "600003"]
    c = connect(tmp_db)
    try:
        assert factor._research_map(c, asof, "ann_count_5d", members) == \
            factor._ann_count_5d_map(c, asof, members)
        # `members=None` ⇒ 空成员集：只返回窗口内**有公告**的标的
        assert factor._research_map(c, asof, "ann_count_5d") == \
            {"600002": 1.0, "600003": 1.0}
        # 单只入口走 `ann_count_5d()`，永不 None
        assert factor._research_value(c, "600001", asof, "ann_count_5d") == 0.0
    finally:
        c.close()


# ---------------------------------------------------------------------------
# C9 未登记名仍 fail-closed
# ---------------------------------------------------------------------------

def test_c9_unregistered_name_still_fails_closed():
    """C9：`ann_count_5dx` 未登记 ⇒ `selected_factors` 抛 `PreregError`（exit 2）。"""
    assert factor.FACTOR_SOURCES["ann_count_5d"] == "research"
    with pytest.raises(xsec.PreregError):
        factor.selected_factors(["ann_count_5dx"])
    # 登记名本身放行
    assert factor.selected_factors(["ann_count_5d"]) == ["ann_count_5d"]


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
# C11 诊断键算法
# ---------------------------------------------------------------------------

def test_c11_zero_and_tie_ratios_are_hand_computed_medians():
    """C11：手算。三天的截面（n=4）：零值占比 2/4、0/4、1/4 ⇒ P50 = 0.25；
    并列占比 2/4、0/4、3/4 ⇒ P50 = 0.5。缺席的那天不进统计。"""
    values_by_mark = {
        "d0": {"a": 0.0, "b": 0.0, "c": 1.0, "d": 2.0},
        "d1": {"a": 1.0, "b": 2.0, "c": 3.0, "d": 4.0},
        "d2": {"a": 0.0, "b": 1.0, "c": 1.0, "d": 1.0},
    }
    periods = [("d0", "d1"), ("d1", "d2"), ("d2", "d3"), ("d3", "d4")]
    got = factor.zero_tie_diag(values_by_mark, periods)
    assert got == {"zero_ratio_p50": pytest.approx(0.25),
                   "tie_ratio_p50": pytest.approx(0.5)}
    assert factor.zero_tie_diag({}, periods) == {"zero_ratio_p50": None,
                                                 "tie_ratio_p50": None}


# ---------------------------------------------------------------------------
# C12 诊断键只在研究侧读数块里（缺省不出现）
# ---------------------------------------------------------------------------

def test_c12_diagnostic_keys_absent_from_the_default_path(tmp_db, tmp_path,
                                                         monkeypatch):
    """C12：缺省产物里不存在 `zero_ratio_p50` / `tie_ratio_p50`（一个键都不许变）。"""
    rep = _default_report(monkeypatch, tmp_db, tmp_path)
    blob = json.dumps(rep, ensure_ascii=False)
    assert "zero_ratio_p50" not in blob
    assert "tie_ratio_p50" not in blob


def _seed_announcements(db, synth, codes, extra_codes=()):
    """给 `codes`（＋宇宙**外**的 `extra_codes`）在 `synth.days` 上逐日写一条公告。"""
    c = connect(db)
    try:
        for i, code in enumerate(list(codes) + list(extra_codes)):
            c.executemany(
                "INSERT INTO announcements (code, art_code, notice_date, title,"
                " source, fetched_at, created_at, resp_sha256)"
                " VALUES (?,?,?,'t','t',?,?,'x')",
                [(code, f"AN{i:03d}-{k:04d}", d, NOW, NOW)
                 for k, d in enumerate(synth.days)])
        c.commit()
    finally:
        c.close()


def test_selected_ann_count_5d_carries_the_two_diagnostic_keys(
        tmp_db, tmp_path, monkeypatch):
    """C12 续：`--factor ann_count_5d` ⇒ 两个键落在研究侧读数块里，且覆盖度 =
    宇宙成员数（「0 是真值」⇒ 每个成员都有值、宇宙外的公告被裁掉）。"""
    synth = _Synth(_weekdays(600))
    _install(monkeypatch, synth)
    db = _synth_db(tmp_path / "ann.db", synth)
    _seed_announcements(db, synth, CODES[:12], extra_codes=("999998", "999999"))
    prereg = _prereg(tmp_path / "p.md",
                     factors=list(factor.MAIN_FACTORS) + ["ann_count_5d"])
    c = connect(db)
    try:
        rep = factor.run_factor_ic(c, pool="short", start=START,
                                   end=synth.days[-1], prereg_path=prereg,
                                   factors=["ann_count_5d"])
    finally:
        c.close()
    f = rep["research"]["factors"]["ann_count_5d"]
    assert f["value_coverage_p50"] == float(len(CODES))     # 不是 12 ＋ 2
    assert 0.0 <= f["zero_ratio_p50"] <= 1.0
    assert 0.0 <= f["tie_ratio_p50"] <= 1.0
    # 只有被选的 research 因子带这两个键；MAIN_FACTORS 的读数块不带
    assert "zero_ratio_p50" not in rep["factors"]["mom20"]
    assert rep["selected_factors"] == ["ann_count_5d"]
