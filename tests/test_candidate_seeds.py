"""Task 8：20 只种子标的（设计文档 §2 D7）。"""

from stocklab.candidate.seeds import SEED_CODES, SEED_UNIVERSE
from stocklab.config.universe import ASSET_ETF, ASSET_STOCK


def test_twenty_instruments():
    assert len(SEED_UNIVERSE) == 20


def test_codes_are_unique_and_six_digits():
    assert len(set(SEED_CODES)) == 20
    assert all(len(c) == 6 and c.isdigit() for c in SEED_CODES)


def test_sixteen_stocks_four_etfs():
    stocks = [i for i in SEED_UNIVERSE if i.asset_type == ASSET_STOCK]
    etfs = [i for i in SEED_UNIVERSE if i.asset_type == ASSET_ETF]
    assert len(stocks) == 16
    assert len(etfs) == 4


def test_etf_whitelist_matches_paper_config():
    """ETF 集合必须与 paper 的分散白名单一致 —— 两处不一致会让
    「候选池里的 ETF」和「纪律臂能买的 ETF」对不上。"""
    from stocklab.paper.config import ETF_WHITELIST

    etf_codes = {i.code for i in SEED_UNIVERSE if i.asset_type == ASSET_ETF}
    assert etf_codes == set(ETF_WHITELIST)


def test_market_and_board_are_valid():
    for i in SEED_UNIVERSE:
        assert i.market in ("sz", "sh")
        assert i.board in ("main", "gem", "star", "bse")


def test_keeps_original_six():
    """原有 6 只是既有数据的来源，扩充不能把它们挤掉。"""
    assert {"000333", "600690", "510300", "510880", "512890",
            "518880"} <= set(SEED_CODES)


def test_has_a_gem_board_stock_for_limit_testing():
    """至少要有一只创业板（±20% 涨跌停），否则涨跌停约束测不到。"""
    assert any(i.board == "gem" for i in SEED_UNIVERSE)
