"""复权因子链（ADR-001 D-01 / ADR-004）：条款解析 / 系数 / PIT 链 / 读取层。

本文件承载两条**必查的反证**（任务验收标准）：
  - `test_ex_dividend_day_has_no_fake_drop`：除权日不得产生假跌幅，
    并断言「用不复权价就是假跌幅」这一失效模式（否则测试没有鉴别力）；
  - `test_future_event_does_not_affect_past`：注入 `cqr > T` 的事件，
    T 日因子与复权价逐位不变。
"""

import json

import pytest

from stocklab.config.paths import FIXTURE_DIR
from stocklab.config.universe import Instrument
from stocklab.data import adjust
from stocklab.data.adjust import (
    AdjustError,
    MissingFactor,
    UnpriceableTerms,
    build_chain,
    event_factor,
    load_bars_adjusted,
    parse_terms,
)
from stocklab.data.models import Bar, CorpAction
from stocklab.data.raw_cache import load_fixture
from stocklab.data.sources import tencent
from stocklab.features import indicators as ind
from stocklab.store import repo
from stocklab.store.db import connect
from stocklab.store.migrate import init_db

NOW = "2026-09-15T20:00:00+08:00"


def _bar(date, close, *, code="000333", open_=None, volume=1000):
    o = close if open_ is None else open_
    return Bar(code=code, date=date, open=o, high=max(o, close) * 1.01,
               low=min(o, close) * 0.99, close=close, volume=volume,
               amount=None, turnover=None, source="test")


def _action(cqr, content, *, code="000333", fh_sh=None, djr=""):
    return CorpAction(code=code, cqr=cqr, djr=djr or cqr, content=content,
                      fh_sh=fh_sh)


# ---------- 条款解析（源站原文是唯一可信来源） ----------

@pytest.mark.parametrize("content,cash,ratio", [
    ("10派30元", 3.0, 0.0),
    ("10派13.0396元", 1.30396, 0.0),
    ("10送3股", 0.0, 0.3),
    ("10转增5股", 0.0, 0.5),
    ("10派20元转15股", 2.0, 1.5),
    ("10派0.5元送2股转3股", 0.05, 0.5),
    ("10派1元送2股", 0.1, 0.2),
])
def test_parse_terms_from_real_content(content, cash, ratio):
    terms = parse_terms(content)
    assert terms.cash == pytest.approx(cash)
    assert terms.share_ratio == pytest.approx(ratio)


def test_parse_terms_refuses_empty_content():
    """无原文 → 抛错。**不得**当成 k=1（那会把假跌幅留在序列里）。"""
    with pytest.raises(UnpriceableTerms, match="无条款原文"):
        parse_terms("")


def test_parse_terms_refuses_unrecognised_content():
    with pytest.raises(UnpriceableTerms, match="无「派/送/转」"):
        parse_terms("股东大会决议")


def test_parse_terms_refuses_pei_gu():
    """配股需要配股价，事件行里没有 → 拒绝猜测（不静默按现金处理）。"""
    with pytest.raises(UnpriceableTerms, match="配股"):
        parse_terms("10配3股")


# ---------- 单事件系数 ----------

def test_event_factor_pure_cash_matches_adr001_rule():
    """纯现金时退化为 ADR-001 的 `1 - 现金/除权前收盘`。"""
    terms = parse_terms("10派30元")
    assert event_factor(100.0, terms) == pytest.approx(1 - 3.0 / 100.0)


def test_event_factor_counts_bonus_shares():
    """送转必须进公式，否则 k 严重偏大（ADR-004 实测：0.957 vs 0.383）。"""
    terms = parse_terms("10派20元转15股")
    assert event_factor(46.99, terms) == pytest.approx((46.99 - 2.0) / (46.99 * 2.5))
    assert event_factor(46.99, terms) < 0.4          # 只扣现金会得 ~0.957


def test_event_factor_rejects_non_positive_pre_close():
    with pytest.raises(AdjustError, match="除权前收盘必须为正"):
        event_factor(0.0, parse_terms("10派1元"))


def test_event_factor_rejects_dividend_larger_than_price():
    """k <= 0 说明输入荒谬 —— 报错，不夹紧、不改口径。"""
    with pytest.raises(AdjustError, match="复权系数越界"):
        event_factor(1.0, parse_terms("10派30元"))


# ---------- PIT 因子链 ----------

def _chain_setup():
    """10 天递增 K 线 + 第 6 天一个纯现金事件（每 10 股派 2 元）。"""
    bars = [_bar(f"2026-03-{d:02d}", 10.0 + d * 0.1) for d in range(1, 11)]
    events = [_action("2026-03-06", "10派2元")]
    return bars, events


def test_chain_is_one_before_the_event():
    bars, events = _chain_setup()
    chain = build_chain(bars, events)
    pre = bars[4].close                       # 2026-03-05
    assert chain.factors["2026-03-05"] == pytest.approx(1.0)
    expected = 1 - (0.2 / pre)
    assert chain.factors["2026-03-06"] == pytest.approx(expected)
    assert chain.factors["2026-03-10"] == pytest.approx(expected)   # 累乘保持


def test_chain_has_no_nulls_and_covers_every_bar_date():
    """ADR-001：无事件的日期也要显式因子，禁 NULL。"""
    bars, events = _chain_setup()
    chain = build_chain(bars, events)
    assert set(chain.factors) == {b.date for b in bars}
    assert all(isinstance(v, float) and v > 0 for v in chain.factors.values())


def test_stock_without_events_gets_all_ones():
    """从未分红的标的：全 1，显式入库（不是 NULL、不是缺失）。"""
    bars = [_bar("2026-03-01", 10.0), _bar("2026-03-02", 10.1)]
    chain = build_chain(bars, [])
    assert chain.factors == {"2026-03-01": 1.0, "2026-03-02": 1.0}
    assert chain.unusable == () and chain.usable_from is None


def test_unpriceable_event_is_reported_and_bounds_usable_range():
    """解析不出条款的事件：**如实报告**，并把可用下界推到它之后。

    略过事件 k 会让 `factor(as_of)/factor(t)` 的分子分母同时漏乘同一项，
    故对 `t >= cqr` 的比值**自动抵消**（链仍精确）；早于 `cqr` 则不成立。
    """
    bars, events = _chain_setup()
    events.append(_action("2026-03-08", ""))          # 无原文
    chain = build_chain(bars, events)
    assert [u.cqr for u in chain.unusable] == ["2026-03-08"]
    assert chain.usable_from == "2026-03-08"
    # 链本身仍然建出来了，且事件日的因子只反映可定价事件
    assert chain.factors["2026-03-10"] == pytest.approx(chain.factors["2026-03-06"])


def test_event_before_bar_coverage_is_unpriceable():
    bars = [_bar("2026-03-01", 10.0)]
    chain = build_chain(bars, [_action("1999-01-01", "10派1元")])
    assert chain.usable_from == "1999-01-01"
    assert len(chain.unusable) == 1


# ---------- 验收标准①：除权日反证（假跌幅） ----------

def test_ex_dividend_day_has_no_fake_drop():
    """除权日：不复权价会跳空，复权后不得产生假跌幅。

    构造 10 派 2 元（每 10 股），除权前收盘 10.00 → 理论除权价 9.80。
    行情按理论价除权（9.80），则**真实日收益为 0**：
      - 不复权序列看到 -2.00%（假跌幅，正是回测失真的来源）；
      - 复权序列必须 ~0。
    两个断言缺一不可 —— 只断言复权那一侧的话，实现「什么都不做」也能过。
    """
    bars = [_bar("2026-03-05", 10.00), _bar("2026-03-06", 9.80)]
    events = [_action("2026-03-06", "10派2元")]
    chain = build_chain(bars, events)

    raw_ret = ind.pct_change_n(
        __import__("pandas").Series([b.close for b in bars]), 1).iloc[-1]
    assert raw_ret == pytest.approx(-0.02)            # 不复权：假跌幅确实存在

    adj = adjust.adjust_bars(bars, chain, "2026-03-06")
    adj_ret = ind.pct_change_n(
        __import__("pandas").Series([b.close for b in adj]), 1).iloc[-1]
    assert adj_ret == pytest.approx(0.0, abs=1e-12)   # 复权后：假跌幅消失
    assert adj[-1].close == pytest.approx(9.80)       # as_of 当日锚定真实价


def test_adjusted_prices_mark_themselves_as_qfq():
    """复权价必须自标 `adj_mode='qfq'` —— `repo.insert_bars` 会据此拒绝写库。

    这是铁律①从「约定」变成**物理不可能**的那一步：复权价落不进 `bars_daily`。
    """
    bars, events = _chain_setup()
    adj = adjust.adjust_bars(bars, build_chain(bars, events), "2026-03-10")
    assert {b.adj_mode for b in adj} == {"qfq"}
    conn = connect(":memory:")
    try:
        init_db  # noqa: B018 — 仅表明需要 schema，实际建表在下一行
        conn.executescript(__import__("stocklab.config.paths", fromlist=["x"])
                           .SCHEMA_SQL.read_text(encoding="utf-8"))
        with pytest.raises(ValueError, match="拒绝复权数据"):
            repo.insert_bars(conn, adj, now=NOW)
    finally:
        conn.close()


# ---------- 验收标准②：PIT 反证（未来事件不得影响过去） ----------

def test_future_event_does_not_affect_past():
    """注入 `cqr > T` 的事件：T 日因子与复权价**逐位不变**。

    防线的位置是 `build_chain` 里的 `pending[idx].cqr <= d` 过滤
    （与 Task 19 的教训一致：反证要打在真正的防线上，不能打在指标实现上）。
    本测试**先真红**：把该过滤改成 `<= dates[-1]`（全量累乘）后，
    `chain_at_T` 与 `adj_close` 两项断言立即失败。
    """
    bars, events = _chain_setup()
    t = "2026-03-07"
    base = build_chain(bars, events)

    future = list(events) + [_action("2026-03-09", "10派50元"),
                             _action("2026-03-10", "10转增10股")]
    with_future = build_chain(bars, future)

    assert with_future.factors[t] == base.factors[t]            # 因子逐位不变
    assert with_future.factors["2026-03-05"] == base.factors["2026-03-05"]

    adj_base = adjust.adjust_bars(bars, base, t)
    adj_future = adjust.adjust_bars(bars, with_future, t)
    assert [b.close for b in adj_future] == [b.close for b in adj_base]

    # 而未来事件**确实**生效于它自己的日期之后 —— 否则上面对比毫无意义
    assert with_future.factors["2026-03-09"] < base.factors["2026-03-09"]
    assert with_future.factors["2026-03-10"] < base.factors["2026-03-10"]


def test_future_event_does_not_change_asof_anchored_prices():
    """as_of=T 时，T 之后的事件连「多一次重基准」都不允许发生。"""
    bars, events = _chain_setup()
    t = "2026-03-08"
    a = adjust.adjust_bars(bars, build_chain(bars, events), t)
    b = adjust.adjust_bars(
        bars, build_chain(bars, list(events) + [_action("2026-03-10", "10派9元")]), t)
    assert [x.close for x in a] == [x.close for x in b]


# ---------- 读取层（含缺因子必须报错） ----------

def _seed_db(tmp_path, bars, actions, code="000333"):
    db = tmp_path / "t.db"
    init_db(db)
    conn = connect(db)
    # P17：复权链的读写入口按 `instruments.type` 判口径（白名单），未登记会被拒绝。
    # 走 `repo.upsert_instruments` 而**不是**裸 INSERT：本函数会被同一个 `tmp_path`
    # 调用两次（`test_load_bars_adjusted_is_pit_when_future_events_are_stored`），
    # 裸 INSERT 第二次必撞 `instruments.code` 主键；upsert 的 ON CONFLICT 让它幂等，
    # 与 `insert_bars`/`insert_corp_actions` 在这条路径上的行为一致。
    repo.upsert_instruments(
        conn, [Instrument(code, code, "sz", "main")], now=NOW)
    repo.insert_bars(conn, bars, now=NOW)
    if actions:
        repo.insert_corp_actions(conn, actions, now=NOW)
    return conn


def test_load_bars_adjusted_reads_from_db(tmp_path):
    bars, events = _chain_setup()
    conn = _seed_db(tmp_path, bars, events)
    try:
        out = load_bars_adjusted(conn, "000333", "2026-03-10")
        assert [b.date for b in out] == [b.date for b in bars]
        assert out[-1].close == pytest.approx(10.0 + 10 * 0.1)   # as_of 锚定真实价
        assert out[0].close < bars[0].close                      # 历史价被缩小
        assert all(b.adj_mode == "qfq" for b in out)
    finally:
        conn.close()


def test_load_bars_adjusted_is_pit_when_future_events_are_stored(tmp_path):
    """库里存着**未来**事件时，过去日期的复权价仍不得受影响（端到端 PIT）。"""
    bars, events = _chain_setup()
    conn = _seed_db(tmp_path, bars, events)
    try:
        before = load_bars_adjusted(conn, "000333", "2026-03-07")
    finally:
        conn.close()
    conn = _seed_db(tmp_path, bars, events + [_action("2026-03-09", "10派50元")],
                    code="000333")
    try:  # tmp_path 复用同一文件 → 第二次调用拿到的是带未来事件的库
        after = load_bars_adjusted(conn, "000333", "2026-03-07")
    finally:
        conn.close()
    assert [b.close for b in after] == [b.close for b in before]


def test_load_bars_adjusted_refuses_before_usable_from(tmp_path):
    bars, events = _chain_setup()
    events = events + [_action("2026-03-09", "10配3股")]     # 不可定价
    conn = _seed_db(tmp_path, bars, events)
    try:
        # as_of 落在事件之前：不可定价事件在**未来**，PIT 语义下不得影响过去
        assert load_bars_adjusted(conn, "000333", "2026-03-05")
        # as_of 跨过事件且默认不缩窗口 → 必须报错，不得交出「某几天错」的序列
        with pytest.raises(MissingFactor, match="跨越了无法定价的除权事件"):
            load_bars_adjusted(conn, "000333", "2026-03-09")
        # 显式缩窗口（调用方自己决定）→ 可用
        assert load_bars_adjusted(conn, "000333", "2026-03-09", start="2026-03-09")
    finally:
        conn.close()


def test_missing_factor_date_raises_instead_of_falling_back():
    """缺因子必须报错 —— 禁止回退到不复权（ERROR_DIARY 2026-09-14）。"""
    bars = [_bar("2026-03-01", 10.0), _bar("2026-03-02", 10.1)]
    chain = build_chain(bars, [])
    with pytest.raises(MissingFactor, match="拒绝回退到不复权价"):
        chain.at("2026-03-03")


# ---------- 真实响应回放：ADR-004 的两个实测结论 ----------

def _load(name, code):
    body, _ = load_fixture(FIXTURE_DIR, name)
    payload = json.loads(body.decode("utf-8"))
    return (tencent.parse_kline(payload, code),
            tencent.parse_corp_actions(payload, code))


def test_real_600690_bonus_share_event_enters_the_chain():
    """真实 fixture：`10送3股` 的 `fh_sh` 是**空串**，旧解析器会整条丢掉。

    送转-only 事件没有现金，`1 - 现金/收盘` 这种写法会把它算成 k=1（等于没复权）。
    本用例锁住「它必须进链、且 k ≈ 1/1.3」。
    """
    bars, actions = _load("tencent_fqkline_bfq_sh600690_old", "600690")
    assert [a.cqr for a in actions][0] == "1994-04-04"
    assert next(a for a in actions if a.cqr == "1994-04-04").fh_sh is None

    chain = build_chain(bars, actions)
    ev = next(e for e in chain.events if e.cqr == "1994-04-04")
    assert ev.content == "10送3股"
    assert ev.factor == pytest.approx(1.0 / 1.3, rel=1e-9)   # 纯送转 → 1/(1+0.3)
    assert ev.factor < 0.8


def test_real_600690_fh_sh_disagrees_with_content():
    """`fh_sh` 与原文现金额不一致是**实测常态**（ADR-004）：不可作条款真源。

    实测两例：`10派2元送2股` 的 fh_sh=1.2、`10派4.7元` 的 fh_sh=3.86。
    本用例把「以原文为准」这条契约钉在真实数据上。
    """
    _, actions = _load("tencent_fqkline_bfq_sh600690_old", "600690")
    by_cqr = {a.cqr: a for a in actions}
    assert by_cqr["2000-06-20"].content == "10派2元送2股"
    assert by_cqr["2000-06-20"].fh_sh == pytest.approx(1.2)      # 原文是 2 元
    assert by_cqr["1998-06-08"].fh_sh == pytest.approx(3.86)     # 原文是 4.7 元
    assert parse_terms(by_cqr["2000-06-20"].content).cash == pytest.approx(0.2)


def test_real_600690_unpriceable_events_bound_the_usable_range():
    """4 条无原文事件 → 如实报不可定价，并把可用下界推到最晚那条之后。

    这正是「缺事件会让链静默出错」的防护：**不猜、不略过、明确划出可用区间**。
    """
    bars, actions = _load("tencent_fqkline_bfq_sh600690_old", "600690")
    chain = build_chain(bars, actions)
    assert [u.cqr for u in chain.unusable] == [
        "1996-05-02", "1997-10-21", "1999-08-12", "2001-01-15"]
    assert chain.usable_from == "2001-01-15"
    # 下界之前拒绝服务（那一段的复权价不可知），下界之后正常
    with pytest.raises(MissingFactor, match="跨越了无法定价的除权事件"):
        adjust.adjust_bars(bars, chain, "1996-07-26")
    # 只要窗口不跨越不可定价事件就能服务：start 挪到最晚那条之后
    servable = adjust.adjust_bars(bars, chain, "2001-04-24", start=chain.usable_from)
    assert servable and servable[0].date == "2001-01-15"   # start 为闭区间


def test_real_000333_chain_is_fully_priceable():
    """000333 的 10 条事件都有原文 → 无不可定价事件，全区间可用。"""
    bars, actions = _load("tencent_fqkline_bfq_sz000333", "000333")
    chain = build_chain(bars, actions)
    assert chain.unusable == () and chain.usable_from is None
    assert len(chain.events) == 10
    assert all(0 < e.factor <= 1 for e in chain.events)


def test_real_000333_cash_only_event_removes_the_ex_day_fake_drop():
    """真实数据上的假跌幅：2026-06-29 除权（10派38元）。

    不复权：78.55 → 77.27 = **-1.63%**（这是回测里会看到的假跌幅）
    复权后：相对理论除权价 74.75 → **+3.37%**（真实收益，分红拖累被还原）
    两者都不是「约等于 0」—— 当天的真实涨跌是正的，本用例断言的是
    「复权后 = 用理论除权价重算的收益」，而不是「收益归零」。
    """
    bars, actions = _load("tencent_fqkline_bfq_sz000333", "000333")
    chain = build_chain(bars, actions)
    ev = next(e for e in chain.events if e.cqr == "2026-06-29")
    assert ev.content == "10派38元"
    assert ev.pre_close == pytest.approx(78.55)               # 真实除权前收盘

    as_of = "2026-06-29"
    raw = [b for b in bars if b.date <= as_of][-2:]
    adj = adjust.adjust_bars(bars, chain, as_of)[-2:]
    raw_ret = raw[1].close / raw[0].close - 1
    adj_ret = adj[1].close / adj[0].close - 1

    assert raw_ret == pytest.approx(-0.0163, abs=5e-4)        # 假跌幅
    assert adj_ret == pytest.approx(raw[1].close / (ev.pre_close - 3.8) - 1,
                                    rel=1e-12)
    assert adj_ret > 0.03                                     # 分红拖累被还原
    assert adj_ret - raw_ret > 0.04                           # 差异就是那笔分红
