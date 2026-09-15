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


# ---------- P17 / T3：按标的口径计价（ETF 卖出免印花税） ----------

def test_default_asset_class_is_stock_and_keeps_old_numbers(cm):
    """反证：不传 `asset_class` 的行为与 P17 之前**逐位相同**（老调用点零影响）。"""
    assert cm.asset_class == "stock"
    assert cm.stamp_tax_rate == pytest.approx(0.0005)
    assert cm.transfer_fee_rate == pytest.approx(0.00001)
    assert cm.fees("sell", 10.0, 1000) == pytest.approx(10.1)   # 既有断言同款


def test_etf_is_exempt_from_stamp_tax():
    """ETF 卖出**免印花税**；佣金 0.025% / 最低 5 元两项口径不变。"""
    etf = CostModel(asset_class="etf")
    assert etf.stamp_tax_rate == 0.0
    assert etf.commission_rate == pytest.approx(0.00025)   # 不变
    assert etf.min_commission == pytest.approx(5.0)        # 不变
    # 10.00 × 1000 = 10000 元 → 佣金 max(2.5, 5) = 5.0，ETF 无印花税/过户费
    assert etf.fees("sell", 10.0, 1000) == pytest.approx(5.0)


def test_same_amount_stock_vs_etf_sell_diff_is_the_stamp_tax():
    """同一金额：股票 vs ETF 卖出费用差 = **印花税那一份**（+ 过户费那一份）。

    100.00 × 1000 = 100000 元（金额取大到佣金不被最低值托底，差额才干净）：
      股票：佣金 25.0 + 过户费 1.0 + 印花税 50.0 = 76.0
      ETF ：佣金 25.0 + 过户费 0.0 + 印花税  0.0 = 25.0
    差额 51.0 = 印花税 50.0 + 过户费 1.0。
    """
    amount = 100.0 * 1000
    stock, etf = CostModel(), CostModel(asset_class="etf")
    s, e = stock.fees("sell", 100.0, 1000), etf.fees("sell", 100.0, 1000)
    assert s == pytest.approx(76.0) and e == pytest.approx(25.0)

    stamp_part = amount * 0.0005
    transfer_part = amount * 0.00001
    assert stamp_part == pytest.approx(50.0) and transfer_part == pytest.approx(1.0)
    assert s - e == pytest.approx(stamp_part + transfer_part)


def test_sell_diff_is_exactly_the_stamp_tax_when_transfer_is_equalised():
    """把过户费口径拉平后，差额**恰好**只剩印花税那一份（分解无残项）。

    上一条的差额里混了过户费，单独看它无法区分「免印花税」与「费率算错」。
    这条把过户费钉成同一个值，差额就只能来自印花税 —— 两个豁免各自独立可验证。
    """
    amount = 100.0 * 1000
    stock = CostModel()
    etf = CostModel(asset_class="etf", transfer_fee_rate=0.00001)  # 显式覆盖
    assert stock.fees("sell", 100.0, 1000) - etf.fees("sell", 100.0, 1000) \
        == pytest.approx(amount * 0.0005) == pytest.approx(50.0)


def test_etf_buy_side_also_has_no_stamp_tax():
    """买入本来就不收印花税 → ETF 与股票的**买入**差额只应来自过户费。"""
    stock, etf = CostModel(), CostModel(asset_class="etf")
    assert stock.fees("buy", 100.0, 1000) == pytest.approx(25.0 + 1.0)
    assert etf.fees("buy", 100.0, 1000) == pytest.approx(25.0)


def test_explicit_rates_override_asset_class_defaults():
    """显式传费率优先于 `asset_class` 的默认（按标的口径是**默认**，不是硬编码）。"""
    etf = CostModel(asset_class="etf", stamp_tax_rate=0.0005)
    assert etf.stamp_tax_rate == pytest.approx(0.0005)
    assert etf.fees("sell", 100.0, 1000) == pytest.approx(25.0 + 50.0)


def test_unknown_asset_class_is_rejected():
    """未知口径**报错**，不静默退化成股票费率（口径错必须响）。"""
    with pytest.raises(ValueError) as exc:
        CostModel(asset_class="cbond")
    assert "cbond" in str(exc.value)


def test_freeze_does_not_stop_rate_resolution():
    """`frozen=True` 下 `__post_init__` 仍能把 `None` 解析成具体费率。"""
    for m in (CostModel(), CostModel(asset_class="etf")):
        assert isinstance(m.stamp_tax_rate, float)
        assert isinstance(m.transfer_fee_rate, float)
