"""Task 10：三池分流（设计文档 §7.2）。"""

import pytest

from stocklab.candidate.pools import (POOL_LONG, POOL_MID, POOL_SHORT, POOL_TOPN,
                                      eligible_pools, select_top)
from stocklab.config.universe import ASSET_ETF, ASSET_STOCK, Instrument

STOCK = Instrument("000333", "美的集团", "sz", "main", ASSET_STOCK)
ETF = Instrument("510300", "沪深300ETF", "sh", "main", ASSET_ETF)


def test_stock_enters_all_three_pools():
    assert eligible_pools(STOCK) == (POOL_SHORT, POOL_MID, POOL_LONG)


def test_etf_only_enters_short_pool():
    """ETF 没有财报，进不了中期/长期池。"""
    assert eligible_pools(ETF) == (POOL_SHORT,)


def test_pool_topn_matches_doc():
    """文档 01 §候选池数量建议的上限：短期 3-6 / 中期 4-8 / 长期 3-5。"""
    assert POOL_TOPN == {"short": 6, "mid": 8, "long": 5}


def test_select_top_takes_highest_scores():
    scored = [{"code": f"{i:06d}", "adj_score": float(i)} for i in range(10)]
    top = select_top(scored, POOL_SHORT)
    assert [r["code"] for r in top] == ["000009", "000008", "000007",
                                        "000006", "000005", "000004"]


def test_select_top_respects_explicit_topn():
    scored = [{"code": f"{i:06d}", "adj_score": float(i)} for i in range(10)]
    assert len(select_top(scored, POOL_SHORT, topn=2)) == 2


def test_select_top_is_deterministic_on_ties():
    """同分时按 code 升序 —— 否则每次跑的池子不一样，报告无法逐字节复现。"""
    scored = [{"code": "600000", "adj_score": 50.0},
              {"code": "000001", "adj_score": 50.0}]
    assert [r["code"] for r in select_top(scored, POOL_SHORT, topn=1)] == ["000001"]


def test_select_top_handles_fewer_than_topn():
    scored = [{"code": "000001", "adj_score": 1.0}]
    assert len(select_top(scored, POOL_LONG)) == 1


def test_select_top_empty():
    assert select_top([], POOL_MID) == []


def test_select_top_unknown_pool_raises_on_empty_list():
    """未知池名必须抛 ValueError（guard 在切片之前）。"""
    with pytest.raises(ValueError, match="未知池"):
        select_top([], "invalid")


def test_select_top_unknown_pool_raises_on_nonempty_list():
    """非空列表传入未知池名，guard 也必须在切片前触发，确保不会绕过。"""
    scored = [{"code": "000001", "adj_score": 1.0}]
    with pytest.raises(ValueError, match="未知池"):
        select_top(scored, "bogus_pool")
