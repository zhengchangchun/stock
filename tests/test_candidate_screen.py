"""Task 9：固定前置通用排雷（设计文档 §7.1）。主干逻辑，AI 不可改。"""

from stocklab.candidate.screen import (MAX_CONSECUTIVE_LIMIT_DOWN,
                                       MIN_HISTORY_DAYS, screen)
from stocklab.config.universe import ASSET_ETF, ASSET_STOCK, Instrument
from stocklab.data.models import Bar

STOCK = Instrument("000333", "美的集团", "sz", "main", ASSET_STOCK)


def bar(date, close, prev_close=None, volume=1000):
    return Bar(code=STOCK.code, date=date, open=close, high=close, low=close,
               close=close, volume=volume, amount=None, turnover=None,
               source="t", adj_mode="none")


def make_bars(n, start_close=10.0, *, dates=None):
    from datetime import date, timedelta

    out = []
    cur = date(2020, 1, 1)
    for i in range(n):
        d = dates[i] if dates else (cur + timedelta(days=i)).isoformat()
        out.append(bar(d, start_close))
    return out


def test_enough_history_passes():
    bars = make_bars(MIN_HISTORY_DAYS)
    r = screen(STOCK, bars, asof="2026-09-17")
    assert r.passed is True
    assert r.reason is None


def test_insufficient_history_is_rejected():
    bars = make_bars(MIN_HISTORY_DAYS - 1)
    r = screen(STOCK, bars, asof="2026-09-17")
    assert r.passed is False
    assert r.reason == "insufficient_history"
    assert str(MIN_HISTORY_DAYS) in r.detail


def test_three_consecutive_limit_down_is_rejected():
    """连续 3 日跌停（主板 −10%）。"""
    bars = make_bars(MIN_HISTORY_DAYS)
    closes = [10.0]
    for _ in range(MAX_CONSECUTIVE_LIMIT_DOWN):
        closes.append(round(closes[-1] * 0.90, 4))
    closes = closes[1:]
    bars = bars[: len(bars) - len(closes)] + [
        bar(b.date, c) for b, c in zip(bars[-len(closes):], closes)]
    r = screen(STOCK, bars, asof="2026-09-17")
    assert r.passed is False
    assert r.reason == "consecutive_limit_down"


def test_two_consecutive_limit_down_passes():
    """差一天就不算 —— 边界必须两侧都测。"""
    bars = make_bars(MIN_HISTORY_DAYS)
    closes = [round(10.0 * 0.9, 4), round(10.0 * 0.81, 4)]
    bars = bars[: len(bars) - 2] + [
        bar(b.date, c) for b, c in zip(bars[-2:], closes)]
    assert screen(STOCK, bars, asof="2026-09-17").passed is True


def test_gem_board_uses_twenty_percent_limit():
    """创业板 ±20%：跌 15% 不算跌停，跌 20% 才算。"""
    gem = Instrument("300750", "宁德时代", "sz", "gem", ASSET_STOCK)

    closes15 = [8.5, 7.225, 6.14]        # 各跌 15%，不是跌停
    bars = make_bars(MIN_HISTORY_DAYS)
    bars = bars[: len(bars) - 3] + [
        bar(b.date, c) for b, c in zip(bars[-3:], closes15)]
    assert screen(gem, bars, asof="2026-09-17").passed is True

    closes20 = [8.0, 6.4, 5.12]          # 各跌 20%，是跌停
    bars20 = make_bars(MIN_HISTORY_DAYS)
    bars20 = bars20[: len(bars20) - 3] + [
        bar(b.date, c) for b, c in zip(bars20[-3:], closes20)]
    r = screen(gem, bars20, asof="2026-09-17")
    assert r.passed is False
    assert r.reason == "consecutive_limit_down"


def test_same_drop_on_main_board_is_limit_down():
    """同样的跌幅放在主板（±10%）就是跌停 —— 证明板块阈值真的生效了。"""
    bars = make_bars(MIN_HISTORY_DAYS)
    closes = [9.0, 8.1, 7.29]            # 各跌 10%
    bars = bars[: len(bars) - 3] + [
        bar(b.date, c) for b, c in zip(bars[-3:], closes)]
    r = screen(STOCK, bars, asof="2026-09-17")
    assert r.passed is False
    assert r.reason == "consecutive_limit_down"


def test_suspension_is_rejected():
    """最近 20 个交易日内无成交 > 5 日。"""
    bars = make_bars(MIN_HISTORY_DAYS)
    for b in bars[-6:]:
        bars[bars.index(b)] = bar(b.date, b.close, volume=0)
    r = screen(STOCK, bars, asof="2026-09-17")
    assert r.passed is False
    assert r.reason == "suspended"


def test_five_suspended_days_passes():
    bars = make_bars(MIN_HISTORY_DAYS)
    for i in range(len(bars) - 5, len(bars)):
        bars[i] = bar(bars[i].date, bars[i].close, volume=0)
    assert screen(STOCK, bars, asof="2026-09-17").passed is True


def test_st_name_is_rejected():
    st = Instrument("000333", "ST美的", "sz", "main", ASSET_STOCK)
    r = screen(st, make_bars(MIN_HISTORY_DAYS), asof="2026-09-17")
    assert r.passed is False
    assert r.reason == "st_flag"
    assert "非 PIT" in r.detail        # 必须显式标注近似


def test_star_st_name_is_rejected():
    st = Instrument("000333", "*ST美的", "sz", "main", ASSET_STOCK)
    assert screen(st, make_bars(MIN_HISTORY_DAYS),
                  asof="2026-09-17").reason == "st_flag"


def test_etf_skips_st_check():
    """ETF 名称不含个股的 ST 语义，不该被误杀。"""
    etf = Instrument("510300", "沪深300ETF", "sh", "main", ASSET_ETF)
    assert screen(etf, make_bars(MIN_HISTORY_DAYS),
                  asof="2026-09-17").passed is True


def test_bars_after_asof_are_not_used():
    """PIT：asof 之后的行不得参与判定。"""
    bars = make_bars(MIN_HISTORY_DAYS - 10)
    future = [bar(f"2026-09-{d:02d}", 1.0, volume=0) for d in range(20, 26)]
    r = screen(STOCK, bars + future, asof="2026-09-17")
    assert r.reason == "insufficient_history"    # 未来行不算数
