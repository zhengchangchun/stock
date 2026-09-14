import pytest

from stocklab.config.costs import CostModel


@pytest.fixture
def cm():
    return CostModel()


def test_buy_fill_price_slips_up(cm):
    assert cm.fill_price("buy", 10.0) == pytest.approx(10.005)


def test_sell_fill_price_slips_down(cm):
    assert cm.fill_price("sell", 10.0) == pytest.approx(9.995)


def test_buy_fees_exclude_stamp_tax(cm):
    # 10.00 × 1000 = 10000 元
    # 佣金 max(10000*0.00025, 5) = max(2.5, 5) = 5.0
    # 过户费 10000*0.00001 = 0.1
    # 印花税 买入不收 = 0
    assert cm.fees("buy", 10.0, 1000) == pytest.approx(5.1)


def test_sell_fees_include_stamp_tax(cm):
    # 佣金 5.0 + 过户费 0.1 + 印花税 10000*0.0005 = 5.0
    assert cm.fees("sell", 10.0, 1000) == pytest.approx(10.1)


def test_min_commission_kicks_in_on_small_trade(cm):
    """小额成交必须按最低 5 元计（评审 C2）。"""
    # 1000 元成交额：佣金按比例是 0.25 元，但最低 5 元
    assert cm.fees("buy", 10.0, 100) == pytest.approx(5.0 + 0.01)


def test_large_trade_commission_is_proportional(cm):
    # 1_000_000 元：佣金 250 元（超过最低）
    assert cm.fees("buy", 100.0, 10000) == pytest.approx(250.0 + 10.0)


def test_small_trade_costs_more_than_proportional(cm):
    """核心性质：小额交易的实际费率远高于名义费率。

    实测比率 ≈ 19.3x（5.01/1000 = 0.501% vs 260/1e6 = 0.026%），
    故断言取 10x 而非计划文件里写错的 100x —— 见 docs/tasks 记录。
    """
    small = cm.fees("buy", 10.0, 100) / 1000
    large = cm.fees("buy", 100.0, 10000) / 1_000_000
    assert small > large * 10


def test_costs_are_rounded_to_cent(cm):
    fee = cm.fees("buy", 3.333, 333)
    assert round(fee, 2) == fee


def test_total_returns_fill_and_fee(cm):
    price, fee = cm.total("buy", 10.0, 1000)
    assert price == pytest.approx(10.005)
    assert fee == pytest.approx(cm.fees("buy", price, 1000))


def test_cost_model_is_frozen(cm):
    with pytest.raises(Exception):
        cm.commission_rate = 0.1
