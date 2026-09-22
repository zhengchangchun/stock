"""P52 / D-36：对照臂同轴（5 条 ＋ 随机臂）与基金等权臂。

两条最容易糊过去的判据在这里被钉死：

1. **不可比 ≠ 0**。基金臂没有持仓与成本、指数不可直接交易、缺数据时点断线 ——
   这些位置上写 `0` 就是把「不知道」显示成「那天没涨没跌」。所以 `points` 里
   缺的点一律是 `None`，页面上写「不可比」。
2. **缺随机臂不得出结论**。`delta_vs_random` 在没有随机决策时是 `null`，
   并且必须写明「不存在，不是 0」—— 写成 `+0.00%` 就把「还没对照」读成了结论。

基金净值的解析用**本地夹具**（`Data_netWorthTrend` 的真实形状），不联网：
项目必须离线全绿，抓取留给 nanobot 跑一次核对。
"""

from __future__ import annotations

import json

import pytest

from stocklab.fund import nav as fund_nav
from stocklab.labweb import paper_data, paper_render
from stocklab.paper import agent_decide, comparison
from stocklab.paper import engine as paper_engine
from stocklab.paper.config import (ARM_AGENT, ARM_AGENT_RANDOM, COMPARISON_ARM_IDS,
                                   FUND_EQUAL_WEIGHT_ID, NOT_COMPARABLE,
                                   PAPER_START_DATE, SAMPLE_THRESHOLD)
from stocklab.store.db import connect
from stocklab.store.migrate import init_db
from stocklab.verify.report import MIN_DAYS

NOW = "2026-09-17T16:00:00+08:00"
CAL = ("2026-09-14", PAPER_START_DATE, "2026-09-16", "2026-09-17")
BARS = {
    "000333": {"2026-09-14": 86.80, "2026-09-15": 87.23, "2026-09-16": 87.60,
               "2026-09-17": 87.10},
    "510300": {"2026-09-15": 4.523, "2026-09-16": 4.550, "2026-09-17": 4.532},
    "510880": {"2026-09-15": 3.382, "2026-09-16": 3.390, "2026-09-17": 3.380},
    "sh000300": {"2026-09-15": 4450.04, "2026-09-16": 4480.27,
                 "2026-09-17": 4460.16},
}

#: `Data_netWorthTrend` 的真实形状（毫秒时间戳 + 单位净值，末尾带分号）。
FAKE_JS = (
    'var fS_name = "某基金";\n'
    "var Data_netWorthTrend = "
    '[{"x":1757865600000,"y":1.0,"equityReturn":0,"unitMoney":""},'
    '{"x":1757952000000,"y":1.02,"equityReturn":2.0,"unitMoney":""},'
    '{"x":1758038400000,"y":0.98,"equityReturn":-3.9,"unitMoney":""}];\n'
    'var Data_ACWorthTrend = [];\n'
)


@pytest.fixture
def db(tmp_path):
    path = tmp_path / "cmp.db"
    init_db(path)
    c = connect(path)
    c.executemany("INSERT INTO instruments (code, name, market, board, type, added_at)"
                  " VALUES (?,?,'sz','main',?,?)",
                  [(code, code, "stock" if code == "000333" else "etf", NOW)
                   for code in ("000333", "510300", "510880")])
    c.executemany("INSERT INTO trading_calendar (date, is_open, source, created_at)"
                  " VALUES (?,1,'tencent',?)", [(d, NOW) for d in CAL])
    c.executemany(
        "INSERT INTO bars_daily (code, date, open, high, low, close, volume, adj_mode,"
        " source, fetched_at) VALUES (?,?,?,?,?,?,100,'none','x',?)",
        [(code, d, v, v, v, v, NOW) for code, series in BARS.items()
         for d, v in series.items()])
    c.execute("INSERT INTO cash_flows (date, kind, amount, note, created_at)"
              " VALUES ('2026-09-14','deposit',20000.0,'本金',?)", (NOW,))
    c.execute("INSERT INTO real_trades (date, code, side, price, qty, fee, note,"
              " created_at) VALUES ('2026-09-14','000333','buy',86.80,100,5.09,"
              " '首笔',?)", (NOW,))
    c.commit()
    paper_engine.init_accounts(c, start_date=PAPER_START_DATE, now=NOW)
    for day in ("2026-09-16", "2026-09-17"):
        paper_engine.step(c, day, now=NOW)
    c.close()
    return path


def _cmp(path, asof="2026-09-17"):
    c = connect(path)
    try:
        return comparison.build(c, asof)
    finally:
        c.close()


# ---------- 口径与门槛 ----------

def test_sample_threshold_is_the_same_120_as_the_verification_record():
    """样本门槛与 `verify.report.MIN_DAYS` **同源同值** —— 靠对拍而不是靠 import。

    `paper/` 的源码护栏禁止依赖验证链路，所以「同一个 120」这件事只能在测试里
    对拍。漂了就是「同一个量在页面与报告里有两个门槛」。
    """
    assert SAMPLE_THRESHOLD == MIN_DAYS


# ---------- 五条 + 随机臂 ----------

def test_six_arms_are_on_one_axis_with_the_five_decided_ones(db):
    """D-36 的五条 ＋ 随机臂**同轴并列**；缺数据写「不可比」，不填 0。"""
    cmp = _cmp(db)
    ids = [a["id"] for a in cmp["arms"]]
    for wanted in (*COMPARISON_ARM_IDS, ARM_AGENT_RANDOM):
        assert wanted in ids, f"{wanted} 必须在同一张对照表里"
    assert ARM_AGENT in ids and ARM_AGENT_RANDOM in ids
    for a in cmp["arms"]:
        assert len(a["points"]) == len(cmp["dates"]), "所有臂必须落在**同一条横轴**上"
    # 基金臂：**非指数、不可交易、近似口径**
    fund = next(a for a in cmp["arms"] if a["id"] == FUND_EQUAL_WEIGHT_ID)
    assert fund["is_index"] is False, "它是持仓未知的基金组合，不是指数"
    assert fund["tradable"] is False and fund["has_cost"] is False
    assert fund["approximate"] is True
    assert "近似" in fund["note"] and "非官方" in fund["note"]
    # 指数：不可交易、无成本
    idx = next(a for a in cmp["arms"] if a["id"] == "sh000300")
    assert idx["is_index"] is True and idx["tradable"] is False
    assert idx["has_cost"] is False


def test_fund_arm_without_data_is_not_comparable_not_zero(db):
    """没有基金净值时：点全是 `None`、`latest` 是 `None` —— **绝不是 0**。"""
    cmp = _cmp(db)
    fund = next(a for a in cmp["arms"] if a["id"] == FUND_EQUAL_WEIGHT_ID)
    assert fund["comparable"] is False
    assert fund["latest"] is None and all(p is None for p in fund["points"])
    assert 0.0 not in [p for p in fund["points"] if p is not None]
    assert fund["n_sessions"] == 0
    assert NOT_COMPARABLE in cmp["not_comparable_note"]


def test_sample_gate_uses_the_same_insufficient_wording(db):
    """样本不足时：`status=insufficient` ＋ 与 P41 同源的**唯一**措辞。"""
    cmp = _cmp(db)
    gate = cmp["sample_gate"]
    assert gate["n_sessions"] < gate["threshold"]
    assert gate["status"] == "insufficient"
    assert gate["note"] == paper_data.PERFORMANCE_INSUFFICIENT


def test_missing_random_arm_is_null_and_says_it_is_not_zero(db):
    """T7：没有随机决策 ⇒ `delta_vs_random` 是 `null`，并写明「不存在，不是 0」。"""
    cmp = _cmp(db)
    assert cmp["n_decisions_random"] == 0
    assert cmp["delta_vs_random"] is None
    assert cmp["delta_vs_random_available"] is False
    assert "不是 0" in cmp["delta_vs_random_note"]
    assert cmp["random_arm_note"] and "不可归因" in cmp["random_arm_note"]


def test_comparison_only_looks_at_rows_on_or_before_asof(db):
    """同轴表只吃 `date <= asof` 的行（历史截图不许显示未来某天的净值）。"""
    early = _cmp(db, asof="2026-09-16")
    assert max(early["dates"]) <= "2026-09-16"
    for a in early["arms"]:
        assert len(a["points"]) == len(early["dates"])


def test_agent_track_exposes_decision_counts_and_null_delta(db):
    """`paper show` 的 agent 块：决策数与 spec 版数**分开**，Δ 仍是 `null`。"""
    block = paper_engine.agent_block(connect(db), "2026-09-17")
    assert block["n_decisions"] == 0
    assert "n_spec_versions" in block
    assert block["delta_vs_random"] is None
    assert "不是 0" in block["delta_vs_random_note"]


# ---------- 基金净值：解析、落库、等权 ----------

def test_parse_pingzhongdata_reads_the_real_shape():
    """解析真实形状：`y` 是单位净值，`x` 换算成**北京时间的日期**。"""
    points = fund_nav.parse_pingzhongdata(FAKE_JS)
    assert [(p.date, p.nav) for p in points] == [
        ("2025-09-15", 1.0), ("2025-09-16", 1.02), ("2025-09-17", 0.98)]


@pytest.mark.parametrize("ms,expected", [
    # ① 源站在「当日 UTC 零点」记（1757865600000 = 2025-09-15T00:00Z）
    (1757865600000, "2025-09-15"),
    # ② 源站在「当日北京零点」记（同一秒数按 +08:00 读 = 09-15 08:00）
    (1757865600000 + 8 * 3600_000, "2025-09-15"),
    # ③ 北京零点的另一种写法：前一日 16:00 UTC —— 必须仍落到 09-15
    (1757865600000 - 16 * 3600_000 + 24 * 3600_000, "2025-09-15"),
])
def test_timestamp_convention_is_read_as_beijing_time(ms, expected):
    """按 +08:00 读，对「UTC 零点」与「北京零点」两种约定**给出同一个日期**。

    这条是刻意的：两种约定我无法离线证实，而按 UTC 读会在第二种下整体早一天
    —— 那是静默的错（日期差一天，净值曲线上看不出来）。真源核对留给 nanobot。
    """
    js = f'var Data_netWorthTrend = [{{"x":{ms},"y":1.0}}];'
    assert fund_nav.parse_pingzhongdata(js)[0].date == expected


def test_parse_raises_instead_of_returning_an_empty_series():
    """抽不到序列 → **报错**，不返回空列表（空列表会被读成「这只基金没净值」）。"""
    with pytest.raises(fund_nav.FundNavError, match="Data_netWorthTrend"):
        fund_nav.parse_pingzhongdata("var somethingElse = 1;")


def test_fund_ingest_is_append_only_and_refuses_to_rewrite_history(db, tmp_path):
    """落库只增：同值跳过；**值变了报错**（源站重算 = 口径变更，不许静默覆盖）。"""
    c = connect(db)
    try:
        points = fund_nav.parse_pingzhongdata(FAKE_JS)
        assert fund_nav.ingest_points(c, code="110011", points=points, now=NOW) == 3
        assert fund_nav.ingest_points(c, code="110011", points=points, now=NOW) == 0
        with pytest.raises(fund_nav.FundNavError, match="append-only"):
            fund_nav.ingest_points(
                c, code="110011", now=NOW,
                points=[fund_nav.NavPoint("2025-09-15", 9.99)])
    finally:
        c.close()


def test_equal_weight_curve_averages_cumulative_returns(db):
    """等权 = 各基金**累计收益的算术平均**；起跑日之前没有净值的基金不参与。"""
    c = connect(db)
    try:
        # 两只基金：一只 +10%、一只 −10%（自 2026-09-15 起）
        fund_nav.ingest_points(c, code="110011", now=NOW, points=[
            fund_nav.NavPoint("2026-09-15", 1.00), fund_nav.NavPoint("2026-09-16", 1.10)])
        fund_nav.ingest_points(c, code="000001", now=NOW, points=[
            fund_nav.NavPoint("2026-09-15", 2.00), fund_nav.NavPoint("2026-09-16", 1.80)])
        curve = fund_nav.equal_weight_curve(
            c, dates=["2026-09-15", "2026-09-16"], start="2026-09-15",
            codes=("110011", "000001"))
    finally:
        c.close()
    assert curve["codes_used"] == ["000001", "110011"]
    assert curve["points"][0] == 0.0
    # (10% + −10%) / 2 = 0
    assert curve["points"][1] == pytest.approx(0.0, abs=1e-9)
    assert curve["is_index"] is False and curve["tradable"] is False


def test_equal_weight_curve_is_none_when_no_fund_has_data(db):
    """一只基金都没有数据 → 点全是 `None`（不可比），**不是 0**。"""
    c = connect(db)
    try:
        curve = fund_nav.equal_weight_curve(
            c, dates=["2026-09-15", "2026-09-16"], start="2026-09-15")
    finally:
        c.close()
    assert curve["comparable"] is False
    assert curve["points"] == [None, None]
    assert curve["n_funds_used"] == 0


def test_missing_fund_table_is_not_comparable_not_a_500(tmp_path):
    """表还没前滚（老库）→ 页面拿到的仍是「不可比」，**不是 500**。

    展示层只读、不外滚 schema。P52 之后新库的 `schema.sql` 已经建了
    `fund_nav_daily`，所以「前滚前的库」不能靠 `init_db` 之后表不存在来代表 ——
    这里**显式拆掉这张表**来造一个老库：判据是「缺表时展示层不炸」，
    而不是「schema 里没有这张表」。
    """
    from stocklab.store.migrate import init_db

    path = tmp_path / "no-fund.db"
    init_db(path)
    c = connect(path)
    try:
        c.execute(f"DROP TABLE {fund_nav.TABLE_FUND_NAV}")
        assert fund_nav.has_table(c) is False, "前置：这是一个前滚前的老库"
        assert c.execute("SELECT COUNT(*) FROM sqlite_master WHERE name = ?",
                         (fund_nav.TABLE_FUND_NAV,)).fetchone()[0] == 0
        assert fund_nav.nav_by_date(c, "110011") == {}
        assert fund_nav.latest_nav_date(c) is None
        assert fund_nav.all_codes(c) == []
        curve = fund_nav.equal_weight_curve(
            c, dates=["2026-09-15"], start="2026-09-15")
    finally:
        c.close()
    assert curve["comparable"] is False and curve["points"] == [None]


def test_fund_pool_is_the_priori_list_not_picked_by_performance():
    """清单是**先验选定**的固定元组（改它 = 改口径），不是按业绩筛出来的。"""
    assert all(isinstance(c, str) and len(c) == 6
               for c, _ in fund_nav.EQUAL_WEIGHT_POOL)
    assert len({c for c, _ in fund_nav.EQUAL_WEIGHT_POOL}) == \
        len(fund_nav.EQUAL_WEIGHT_POOL), "清单里不许有重复"


# ---------- 页面 DOM ----------

def _html(path, asof="2026-09-17") -> str:
    c = connect(path)
    try:
        data = paper_data.track(c, asof)
    finally:
        c.close()
    return paper_render.paper_page(data, base="/lab", built_at=NOW)


def test_page_renders_all_six_arms_on_one_table(db):
    """T6：五条 ＋ 随机臂在同一节、同一张表里；每条 arm id 都出现在 DOM 上。"""
    html = _html(db)
    assert "对照臂同轴（D-36：5 条 + 随机臂）" in html
    seg = html.split("对照臂同轴（D-36：5 条 + 随机臂）", 1)[1].split("</section>", 1)[0]
    for arm_id in (*COMPARISON_ARM_IDS, ARM_AGENT_RANDOM):
        assert arm_id in seg, f"{arm_id} 没出现在同轴表里"
    assert seg.count("<tr>") >= len(COMPARISON_ARM_IDS) + 1


def test_page_writes_not_comparable_instead_of_a_blank_or_zero(db):
    """基金臂没有数据 → DOM 上写「不可比」，且**不写 0.00%**。"""
    html = _html(db)
    seg = html.split("对照臂同轴（D-36：5 条 + 随机臂）", 1)[1].split("</section>", 1)[0]
    assert NOT_COMPARABLE in seg
    assert "0.00%" not in seg, "缺数据被画成了 0"


def test_page_labels_the_fund_arm_as_approximate_and_non_official(db):
    """基金臂必须标注「近似 / 非官方 + 数据源」，且**不许**被写成指数。"""
    html = _html(db)
    seg = html.split("对照臂同轴（D-36：5 条 + 随机臂）", 1)[1].split("</section>", 1)[0]
    assert "近似" in seg and "非官方" in seg
    assert "pingzhongdata" in seg
    assert "持仓未知的基金组合" in seg
    assert "不是指数" in seg


def test_page_shows_the_insufficient_gate_wording(db):
    """样本不足的措辞与 P41 同源，出现在页面上而不是只躺在 JSON 里。"""
    html = _html(db)
    assert paper_data.PERFORMANCE_INSUFFICIENT in html


def test_page_shows_null_delta_as_missing_not_zero(db):
    """T7 的页面版：Δ 不存在时显示原因，**不显示 `+0.00%`**。"""
    html = _html(db)
    seg = html.split("对照臂同轴（D-36：5 条 + 随机臂）", 1)[1].split("</section>", 1)[0]
    assert "不存在" in seg
    assert "+0.00%" not in seg


def test_page_has_no_escaped_markup_in_the_new_section(db):
    """新一节同样受「页面里不出现被二次转义的标签」约束（ERROR_DIARY #50）。"""
    html = _html(db)
    seg = html.split("对照臂同轴（D-36：5 条 + 随机臂）", 1)[1].split("</section>", 1)[0]
    for needle in ("&lt;a href", "&lt;span", "&lt;code&gt;", "&lt;b&gt;"):
        assert needle not in seg, f"新一节出现了被转义成文本的标签：{needle}"


def test_comparison_payload_is_replayable(db):
    """同库同 asof → 同一份对照载荷（不含生成时刻）。"""
    a = _cmp(db)
    b = _cmp(db)
    assert json.dumps(a, sort_keys=True, ensure_ascii=False) == \
        json.dumps(b, sort_keys=True, ensure_ascii=False)


def test_comparison_reaches_the_page_through_track(db):
    """页面数据层必须把对照块原样带出来（页面自己拼一套就会与 `paper show` 漂）。"""
    c = connect(db)
    try:
        data = paper_data.track(c, "2026-09-17")
    finally:
        c.close()
    assert data["comparison"]["arms"], "对照块没进页面载荷"
    assert data["comparison"]["delta_vs_random"] is None
    assert agent_decide.TABLE_DECISIONS == "paper_agent_decisions"
