"""Task 8：21 只种子标的（设计文档 §2 D7）。

P71 起断言对象**从字面 21 改成派生量**：真源仍是 `SEED_UNIVERSE`（D2：取值/语义
一字不动），而 `config/universes/seed21.csv` 是它的**冗余副本** —— 两者必须逐位一致，
所以「21」这个数字由那条对账判据守，而不是由这里的字面量守。
"""

from stocklab.candidate.seeds import SEED_CODES, SEED_UNIVERSE
from stocklab.config.universe import ASSET_ETF, ASSET_STOCK
from stocklab.config.universes import load_universe


def test_twentyone_instruments():
    # 字面 21 → 派生量：与 `config/universes/seed21.csv`（+ meta 的 sha）逐位对账
    universe = load_universe("seed21")
    assert len(SEED_UNIVERSE) == len(universe.members) == len(universe.codes)


def test_codes_are_unique_and_six_digits():
    assert len(set(SEED_CODES)) == len(SEED_UNIVERSE)
    assert all(len(c) == 6 and c.isdigit() for c in SEED_CODES)


def test_seventeen_stocks_four_etfs():
    stocks = [i for i in SEED_UNIVERSE if i.asset_type == ASSET_STOCK]
    etfs = [i for i in SEED_UNIVERSE if i.asset_type == ASSET_ETF]
    assert len(stocks) == 17
    assert len(etfs) == 4


def test_etf_whitelist_subset_of_seed_etfs():
    """paper.config.ETF_WHITELIST（分散工具白名单，故意只有 2 只——换成家电/家电ETF
    不算分散）是 seeds 里 ETF 集合（扫描宇宙，4 只）的子集，而非相等。
    两个概念不同：把等号改成 <= 是正确的，修改 paper/config.py 以使等号成立属于越界。"""
    from stocklab.paper.config import ETF_WHITELIST

    etf_codes = {i.code for i in SEED_UNIVERSE if i.asset_type == ASSET_ETF}
    assert set(ETF_WHITELIST) <= etf_codes


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
