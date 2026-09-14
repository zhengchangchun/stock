"""Task 10：东财适配器（长历史日K + 资金流）。

诚实声明：本文件的 payload **是按东财文档格式构造的合成样本，不是录制的真实响应**。
原因：ADR-001 §1 实测东财 `kline/get` 返回空响应（限流），东财在 v1 只作**交叉校验源**，
不作主源；真实可用性属 Task 12（长历史数据源实测）的范围。
腾讯侧则有真实录制的 fixture（tests/test_source_tencent.py）。
"""

import pytest

from stocklab.config.universe import DEFAULT_UNIVERSE
from stocklab.data.sources import eastmoney

KLINE_PAYLOAD = {
    "rc": 0,
    "data": {
        "code": "000333",
        "klines": [
            "2026-09-11,10.00,10.50,10.80,9.90,123456,129000000.00,9.0,5.0,0.50,1.20",
            "2026-09-14,10.50,10.30,10.60,10.20,234567,241000000.00,4.0,-1.9,-0.20,2.30",
        ],
    },
}

FLOW_PAYLOAD = {"data": {"klines": [
    "2026-09-11,1000.0,-200.0,300.0,400.0,600.0",
    "2026-09-14,-500.0,100.0,-100.0,-200.0,-300.0",
]}}


# ---------- URL 与请求头 ----------

def test_referer_header_present():
    """东财不带 Referer 会被拒（总纲第 5 节）。"""
    assert "Referer" in eastmoney.HEADERS
    assert eastmoney.HEADERS["Referer"].startswith("https://")


def test_kline_url_builder_has_range():
    """B2：必须能指定 beg/end 才能拿到长历史（ADR-001 D-02 的分页手段）。"""
    url = eastmoney.kline_url("0.000333", beg="20150101", end="20260914")
    assert "beg=20150101" in url
    assert "end=20260914" in url
    assert "secid=0.000333" in url


def test_kline_url_defaults_to_unadjusted():
    """铁律①：默认 fqt=0（不复权），抓取层不下发复权价。"""
    assert "fqt=0" in eastmoney.kline_url("0.000333", beg="0", end="20500101")


def test_kline_url_accepts_secid_from_universe():
    url = eastmoney.kline_url(DEFAULT_UNIVERSE[0].secid, beg="20150101", end="20260914")
    assert "secid=0.000333" in url                     # 深市 0.，沪市 1.
    assert DEFAULT_UNIVERSE[1].secid == "1.600690"


def test_kline_url_is_daily():
    assert "klt=101" in eastmoney.kline_url("0.000333", beg="0", end="20500101")


# ---------- 日K 解析 ----------

def test_parse_kline():
    bars = eastmoney.parse_kline(KLINE_PAYLOAD, "000333", adj_mode="none")
    assert len(bars) == 2
    b = bars[0]
    assert b.date == "2026-09-11"
    assert b.open == pytest.approx(10.00)
    assert b.close == pytest.approx(10.50)      # 第 2 列是收盘（同为 O,C,H,L 顺序）
    assert b.high == pytest.approx(10.80)
    assert b.low == pytest.approx(9.90)
    assert b.volume == 123456 * 100             # 手 → 股
    assert b.amount == pytest.approx(129000000.00)   # 东财成交额已是「元」，不换算
    assert b.source == "eastmoney"


def test_parse_kline_turnover():
    assert eastmoney.parse_kline(KLINE_PAYLOAD, "000333")[0].turnover == pytest.approx(1.20)


def test_parse_kline_amount_consistent_with_price_times_volume():
    """单位自洽断言（评审 C3）：amount ≈ close × volume，容差 1%。"""
    for b in eastmoney.parse_kline(KLINE_PAYLOAD, "000333"):
        implied = b.close * b.volume
        assert abs(b.amount - implied) / implied < 0.01


def test_parse_kline_empty():
    assert eastmoney.parse_kline({"data": None}, "000333", adj_mode="none") == []
    assert eastmoney.parse_kline({}, "000333", adj_mode="none") == []
    assert eastmoney.parse_kline({"data": {"klines": []}}, "000333") == []


def test_parse_kline_skips_short_row():
    payload = {"data": {"klines": ["2026-09-11,10.00", "2026-09-14,10.5,10.3,10.6,10.2,"
                                   "2345,2410000.0,4,1,1,2"]}}
    assert len(eastmoney.parse_kline(payload, "000333", adj_mode="none")) == 1


def test_parse_kline_skips_non_numeric_row():
    payload = {"data": {"klines": ["2026-09-11,x,10.50,10.80,9.90,123456,129000000.0,1,1,1,1"]}}
    assert eastmoney.parse_kline(payload, "000333", adj_mode="none") == []


def test_parse_kline_missing_turnover_column_is_none():
    payload = {"data": {"klines": ["2026-09-11,10.00,10.50,10.80,9.90,123456,129000000.0"]}}
    bars = eastmoney.parse_kline(payload, "000333")
    assert len(bars) == 1 and bars[0].turnover is None


# ---------- 资金流 ----------

def test_parse_money_flow():
    rows = eastmoney.parse_money_flow(FLOW_PAYLOAD)
    assert len(rows) == 2
    assert rows[0]["date"] == "2026-09-11"
    assert rows[0]["main_net"] == pytest.approx(1000.0)
    assert rows[0]["big_net"] == pytest.approx(400.0)
    assert rows[0]["xl_net"] == pytest.approx(600.0)
    # 校验自洽：主力 = 大单 + 超大单
    for r in rows:
        assert r["main_net"] == pytest.approx(r["big_net"] + r["xl_net"])


def test_parse_money_flow_empty_and_malformed():
    assert eastmoney.parse_money_flow({"data": None}) == []
    assert eastmoney.parse_money_flow({"data": {"klines": ["2026-09-11,1,2"]}}) == []
    assert eastmoney.parse_money_flow(
        {"data": {"klines": ["2026-09-11,a,b,c,d,e"]}}
    ) == []


def test_money_flow_url_uses_allowed_host():
    assert eastmoney.FFLOW_URL.startswith("https://push2.eastmoney.com/")
    assert eastmoney.KLINE_URL.startswith("https://push2his.eastmoney.com/")
