"""不可用因子区间：必须被拒绝，不许静默通过（ADR-004 §无法定价事件）。

背景：`adj_factors` 里存在**非 NULL 的错误值** —— 无法定价的除权事件
（`FHcontent` 为空 / 含配股）算不出复权系数 k，其 k 不进因子链，于是该事件
之后的因子都缺乘一个 k。数值看着正常，拿它算**跨越该事件**的收益就是假的
（分红被算成暴跌）。600690 有 5 个此类事件，2001-01-15 之前的 1745 根 K 线
因此不可用于收益计算。

本组测试同时给出**对照**：同一段价格序列，事件可定价时假跌幅消失 ——
证明假跌幅来自「缺事件」，不是来自价格本身。
"""

import sqlite3

import pytest

from stocklab.config.paths import SCHEMA_SQL
from stocklab.data import adjust
from stocklab.data.models import Bar, CorpAction
from stocklab.store import repo

NOW = "2026-09-15T00:00:00+08:00"
DATES = ["2020-01-02", "2020-01-03", "2020-01-06", "2020-01-07", "2020-01-08",
         "2020-01-09", "2020-01-10"]
CLOSES = [10.0, 10.0, 9.5, 9.6, 9.7, 9.25, 9.3]


@pytest.fixture
def conn():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    c.executescript(SCHEMA_SQL.read_text(encoding="utf-8"))
    yield c
    c.close()


def bars_of(code):
    return [Bar(code=code, date=d, open=c, high=c, low=c, close=c, volume=1000,
                amount=c * 1000, turnover=1.0, source="test")
            for d, c in zip(DATES, CLOSES)]


def seed(conn, code, *, content):
    """写入 K 线 + 一条除权事件（2020-01-06）。"""
    repo.insert_bars(conn, bars_of(code), now=NOW)
    repo.insert_corp_actions(conn, [CorpAction(code=code, cqr="2020-01-06",
                                               djr="2020-01-03", content=content,
                                               fh_sh=None, source="test")],
                             now=NOW)


def test_blackout_row_written_for_unpriceable_event(conn):
    seed(conn, "000001", content="")          # FHcontent 为空 → 无法定价
    _, chain = adjust.load_chain(conn, "000001")
    assert len(chain.unusable) == 1
    repo.insert_adj_factors(conn, "000001", chain, source="test", now=NOW)

    rows = conn.execute("SELECT cqr, reason FROM adj_factor_blackout").fetchall()
    assert [r["cqr"] for r in rows] == ["2020-01-06"]
    assert "无条款原文" in rows[0]["reason"]
    assert adjust.usable_from(conn, "000001") == "2020-01-06"


def test_usable_view_exposes_the_interval(conn):
    seed(conn, "000001", content="")
    _, chain = adjust.load_chain(conn, "000001")
    repo.insert_adj_factors(conn, "000001", chain, source="test", now=NOW)

    row = conn.execute("SELECT * FROM v_adj_usable WHERE code='000001'").fetchone()
    assert row["usable_from"] == "2020-01-06"
    assert row["n_unusable"] == 1


def test_priceable_event_writes_no_blackout(conn):
    seed(conn, "000002", content="10派5元")
    _, chain = adjust.load_chain(conn, "000002")
    repo.insert_adj_factors(conn, "000002", chain, source="test", now=NOW)
    assert conn.execute("SELECT COUNT(*) FROM adj_factor_blackout").fetchone()[0] == 0
    assert adjust.usable_from(conn, "000002") is None


def test_read_layer_rejects_window_crossing_unusable_event(conn):
    """跨缺口的窗口必须抛错（不能返回一条「大部分对、某几天错」的序列）。"""
    seed(conn, "000001", content="")
    _, chain = adjust.load_chain(conn, "000001")
    repo.insert_adj_factors(conn, "000001", chain, source="test", now=NOW)

    with pytest.raises(adjust.MissingFactor, match="无法定价"):
        adjust.load_bars_adjusted(conn, "000001", as_of="2020-01-10")


def test_read_layer_allows_window_starting_at_usable_from(conn):
    """把窗口挪到缺口之后（start = usable_from）是**显式**决定，此时可用。"""
    seed(conn, "000001", content="")
    _, chain = adjust.load_chain(conn, "000001")
    repo.insert_adj_factors(conn, "000001", chain, source="test", now=NOW)

    bars = adjust.load_bars_adjusted(conn, "000001", as_of="2020-01-10",
                                     start="2020-01-06")
    assert [b.date for b in bars] == DATES[2:]
    assert all(b.adj_mode == "qfq" for b in bars)


def test_stale_blackout_table_is_refused_not_ignored(conn):
    """库里缺口记录缺失（旧的 adj_factors 已写、blackout 没写）→ 拒绝服务。

    这是「静默使用不可用因子」的正面反证：如果读取层不看 blackout，
    它会对这段历史照常返回价格，而那段收益是算不出来的。
    """
    seed(conn, "000001", content="")
    _, chain = adjust.load_chain(conn, "000001")
    repo.insert_adj_factors(conn, "000001", chain, source="test", now=NOW)
    conn.execute("DELETE FROM adj_factor_blackout")          # 模拟旧库/未重建

    with pytest.raises(adjust.StaleFactorTable, match="一致"):
        adjust.load_bars_adjusted(conn, "000001", as_of="2020-01-10")


def test_extra_blackout_row_is_also_refused(conn):
    seed(conn, "000002", content="10派5元")
    _, chain = adjust.load_chain(conn, "000002")
    repo.insert_adj_factors(conn, "000002", chain, source="test", now=NOW)
    conn.execute("INSERT INTO adj_factor_blackout VALUES (?,?,?,?,?)",
                 ("000002", "2020-01-09", "过期记录", "test", NOW))

    with pytest.raises(adjust.StaleFactorTable, match="库中有而链没有"):
        adjust.load_bars_adjusted(conn, "000002", as_of="2020-01-10")


def test_silent_use_of_unusable_factor_produces_a_fake_drop(conn):
    """**反证核心**：绕过守卫读因子，除权日会算出一个假的 -5% 跌幅。

    同一段原始价格序列，事件可定价时该假跌幅为 0 —— 差异只来自「事件能不能定价」。
    """

    def ex_date_return(code, content):
        seed(conn, code, content=content)
        bars, chain = adjust.load_chain(conn, code)
        raw = {b.date: b.close for b in bars}
        base = chain.factors["2020-01-10"]           # 直接读链，绕过 adjust_bars 的窗口守卫
        p_prev = raw["2020-01-03"] * base / chain.factors["2020-01-03"]
        p_ex = raw["2020-01-06"] * base / chain.factors["2020-01-06"]
        return p_ex / p_prev - 1.0

    # 事件不可定价：k 不在链里 → 除权日的真实跌幅被原样当成「收益」
    assert ex_date_return("000001", "") == pytest.approx(-0.05)
    # 对照：同价格序列，事件可定价（10派5元 → k=0.95）→ 假跌幅消失
    assert ex_date_return("000002", "10派5元") == pytest.approx(0.0)


def test_refused_window_never_reaches_the_backtest(conn):
    """端到端：守卫抛错时，回测引擎一行价格都拿不到（不可能算出假净值）。"""
    from stocklab.backtest.engine import run_backtest
    from stocklab.calendar.trading_calendar import Calendar
    from stocklab.config.costs import CostModel
    from stocklab.config.universe import Instrument

    seed(conn, "000001", content="")
    _, chain = adjust.load_chain(conn, "000001")
    repo.insert_adj_factors(conn, "000001", chain, source="test", now=NOW)

    with pytest.raises(adjust.MissingFactor):
        adj = adjust.load_bars_adjusted(conn, "000001", as_of="2020-01-10")
        run_backtest({"000001": adj}, None, start=DATES[0], end=DATES[-1],
                     initial_cash=100_000.0, costs=CostModel(),
                     calendar=Calendar.from_dates(DATES),
                     universe=[Instrument("000001", "测试", "sz", "main")])


def test_usable_from_of_real_gap_shape():
    """600690 的真实缺口形状（离线合成复现）：多事件下 usable_from 取最晚者。"""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA_SQL.read_text(encoding="utf-8"))
    try:
        repo.insert_bars(conn, bars_of("600690"), now=NOW)
        repo.insert_corp_actions(conn, [
            CorpAction("600690", "2020-01-03", "2019-12-31", "", None, "test"),
            CorpAction("600690", "2020-01-06", "2020-01-03", "10送3股", None, "test"),
        ], now=NOW)
        _, chain = adjust.load_chain(conn, "600690")
        repo.insert_adj_factors(conn, "600690", chain, source="test", now=NOW)
        assert adjust.usable_from(conn, "600690") == "2020-01-03"
        assert len(adjust.load_blackouts(conn, "600690")) == 1
    finally:
        conn.close()
