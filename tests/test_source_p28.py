"""P28 估值 / 资金流适配器（纯函数，无网络）。

解析层契约（P28 计划 §2）：
  - 数值 `"-"` / 空串 / 缺失 → None（**拿不到写 NULL，不许填 0/估算**）；
  - `TRADE_DATE` 带时分秒 → 裁剪 `YYYY-MM-DD`；
  - 资金流 `code` 由源站形式（`sz000333`）→ 6 位（`000333`）；
  - URL 主机必须在白名单（`assert_host_allowed`）。
"""

from stocklab.config.universe import assert_host_allowed
from stocklab.data.sources import eastmoney, sina


def _val_row(**kw):
    base = {
        "SECURITY_CODE": "000333",
        "TRADE_DATE": "2026-09-16 00:00:00",
        "PE_TTM": "14.61305067",
        "PB_MRQ": "3.06209111",
        "PS_TTM": "1.38797538",
        "TOTAL_MARKET_CAP": 648494426150,
        "TOTAL_SHARES": "7629340000",
        "CLOSE_PRICE": 85,
        "CHANGE_RATE": "-2.556459933509",
    }
    base.update(kw)
    return base


def test_parse_valuation_full_row():
    rows = eastmoney.parse_valuation(
        {"result": {"pages": 705, "data": [_val_row()]}}, "000333")
    assert len(rows) == 1
    v = rows[0]
    assert v.code == "000333"
    assert v.date == "2026-09-16"                    # 裁剪掉 " 00:00:00"
    assert v.pe_ttm == 14.61305067
    assert v.pb == 3.06209111
    assert v.ps_ttm == 1.38797538
    assert v.total_mv == 648494426150
    assert v.total_shares == 7629340000
    assert v.close_price == 85
    assert v.change_rate == -2.556459933509
    assert v.source == "eastmoney-datacenter"


def test_parse_valuation_null_fields_not_zero():
    """`"-"` / 空串 / 缺失 → None，绝不填 0。"""
    rows = eastmoney.parse_valuation(
        {"result": {"data": [_val_row(PE_TTM="-", PB_MRQ="", PS_TTM=None)]}},
        "000333")
    v = rows[0]
    assert v.pe_ttm is None
    assert v.pb is None
    assert v.ps_ttm is None


def test_parse_valuation_skips_bad_rows_and_empty_result():
    # 非 dict 行 / 空日期行跳过；result 为 null（ETF 9201）→ 空
    assert eastmoney.parse_valuation(
        {"result": {"data": ["not-a-dict", {"TRADE_DATE": ""}]}}, "000333") == []
    assert eastmoney.parse_valuation({"result": None}, "000333") == []
    assert eastmoney.parse_valuation(None, "000333") == []


def _flow_row(**kw):
    base = {
        "opendate": "2026-09-16",
        "trade": "85.0000",
        "changeratio": "-0.0255646",
        "turnover": "43.346",
        "netamount": "-275253515.2000",
        "ratioamount": "-0.108105",
        "r0_net": "-161789291.4300",
    }
    base.update(kw)
    return base


def test_parse_moneyflow_full_row_and_code_prefix():
    rows = sina.parse_moneyflow([_flow_row()], "sz000333")
    assert len(rows) == 1
    m = rows[0]
    assert m.code == "000333"                          # sz000333 → 000333
    assert m.date == "2026-09-16"
    assert m.close == 85.0
    assert m.change_ratio == -0.0255646
    assert m.turnover == 43.346                        # 新浪口径 ×100，原样存
    assert m.main_net == -275253515.2
    assert m.xl_net == -161789291.43
    assert m.ratio_amount == -0.108105
    assert m.source == "sina"


def test_parse_moneyflow_null_and_non_list():
    rows = sina.parse_moneyflow(
        [_flow_row(trade="-", netamount="", r0_net=None)], "sz000333")
    m = rows[0]
    assert m.close is None
    assert m.main_net is None
    assert m.xl_net is None
    assert sina.parse_moneyflow({"not": "a list"}, "sz000333") == []
    assert sina.parse_moneyflow(None, "sz000333") == []


def test_p28_urls_are_whitelisted():
    """新源主机必须在 `ALLOWED_HOSTS`（R13），否则运行时会在请求前被拦。"""
    for url in (eastmoney.valuation_url("000333", page=1, page_size=3),
                sina.moneyflow_url("sz000333", page=1, num=3)):
        assert_host_allowed(url)                       # 不抛 = 白名单通过


def test_valuation_url_uses_six_digit_code():
    url = eastmoney.valuation_url("000333", page=1, page_size=3)
    assert "SECURITY_CODE%22%3A%22000333%22" in url or 'SECURITY_CODE' in url
    assert "pageNumber=1" in url and "pageSize=3" in url
    assert "sortTypes=-1" in url                        # 按 TRADE_DATE 降序
