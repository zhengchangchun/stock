import pytest

from stocklab.config.universe import ALLOWED_HOSTS, assert_host_allowed
from stocklab.data.errors import HostNotAllowed


def test_allowed_host_passes():
    assert_host_allowed("https://qt.gtimg.cn/q=sz000333")


def test_numeric_subdomain_rejected():
    with pytest.raises(HostNotAllowed):
        assert_host_allowed("https://1.push2.eastmoney.com/api/qt/clist/get")


def test_ip_literal_rejected():
    with pytest.raises(HostNotAllowed):
        assert_host_allowed("http://192.168.1.10/data")


def test_unknown_host_rejected():
    with pytest.raises(HostNotAllowed):
        assert_host_allowed("https://example.com/x")


def test_whitelist_is_exact():
    assert ALLOWED_HOSTS == frozenset(
        {
            "qt.gtimg.cn",
            "web.ifzq.gtimg.cn",
            "push2.eastmoney.com",
            "push2his.eastmoney.com",
        }
    )


# ---------- P17：标的池纳入 ETF ----------

def test_default_universe_contains_etfs_with_sh_market_and_etf_type():
    from stocklab.config.universe import DEFAULT_UNIVERSE

    by_code = {i.code: i for i in DEFAULT_UNIVERSE}
    for code in ("510300", "510880", "512890", "518880"):
        assert code in by_code, f"{code} 未进标的池"
        inst = by_code[code]
        assert inst.market == "sh"
        assert inst.asset_type == "etf"
        assert inst.is_etf and not inst.is_stock
        assert inst.lot == 100            # ETF 二级市场 1 手 = 100 份
        assert inst.tencent_code == f"sh{code}"
        assert inst.secid.startswith("1.")   # 沪市


def test_existing_stocks_default_to_stock_asset_type():
    from stocklab.config.universe import DEFAULT_UNIVERSE

    by_code = {i.code: i for i in DEFAULT_UNIVERSE}
    assert by_code["000333"].asset_type == "stock"
    assert by_code["600690"].asset_type == "stock"
    assert by_code["000333"].is_stock and not by_code["000333"].is_etf
    assert by_code["000333"].lot == 100


def test_instrument_type_reads_instruments_table(tmp_db):
    """`instrument_type` 是「按标的口径」的唯一真相来源（T2/T3 都用它）。"""
    from stocklab.config.universe import instrument_type
    from stocklab.store.db import connect
    from stocklab.store.migrate import init_db

    init_db(tmp_db)
    conn = connect(tmp_db)
    try:
        conn.execute("INSERT INTO instruments (code, name, market, board, type,"
                     " added_at) VALUES ('510300','沪深300ETF','sh','main','etf',"
                     " '2026-09-15T00:00:00+08:00')")
        conn.commit()
        assert instrument_type(conn, "510300") == "etf"
        assert instrument_type(conn, "999999") is None    # 未登记 → None，不猜
    finally:
        conn.close()


def test_default_universe_etf_seed_is_typed_etf_by_constructor_default():
    """不给 `asset_type` 的既有写法必须仍然得到 `stock`（不破坏历史的 8 处调用点）。"""
    from stocklab.config.universe import Instrument

    assert Instrument("000001", "平安银行", "sz", "main").asset_type == "stock"
