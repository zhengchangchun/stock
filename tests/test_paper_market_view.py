"""P84 / T2：`market` 块（`paper/market_view.py`）。

| 断言 | 被摘掉后会红的实现 |
|---|---|
| 逐字段的算得对（含 5/20/60 首尾口径） | 用错端点 / 用错窗口长度 |
| 塞 `asof` 之后的行 → 块逐字节不变（PIT） | 用 `MAX(date)` 而不加 `<= asof` |
| 空表 / 缺表 ⇒ `n_total: 0` ＋ `None`，**不抛** | 夹具库上直接 `IndexError` |
| 标的数 ×10 ⇒ SQL 条数**不变** | 逐标的循环查价（K8 的失败判据） |
| 块内没有任何前向字段名（K5） | 往里塞 `predicted_return` / `signal` |

夹具口径（全部手算过，见用例里的注释）：
`000001` `000002` `000003` 三天收盘价，指数两天收盘价。
"""

import json
import sqlite3

import pytest

from stocklab.paper import market_view
from stocklab.store.db import connect
from stocklab.store.migrate import init_db

NOW = "2026-09-22T16:00:00+08:00"
ASOF = "2026-09-24"


@pytest.fixture
def conn(tmp_db):
    init_db(tmp_db)
    c = connect(tmp_db)
    c.executemany(
        "INSERT INTO bars_daily (code, date, open, high, low, close, volume, adj_mode,"
        " source, fetched_at) VALUES (?,?,?,?,?,?,100,'none','x',?)",
        [(code, d, v, v, v, v, NOW)
         # 09-22 收盘 / 09-23 收盘 / 09-24 收盘
         for code, series in {
             "000001": {"2026-09-22": 10.0, "2026-09-23": 11.0, "2026-09-24": 11.0},
             "000002": {"2026-09-22": 10.0, "2026-09-23": 10.0, "2026-09-24": 9.0},
             "000003": {"2026-09-22": 10.0, "2026-09-23": 12.0, "2026-09-24": 12.0},
             # 只有一天数据 ⇒ 算不出涨跌幅，但仍计入 n_total
             "000004": {"2026-09-24": 5.0},
             "sh000300": {"2026-09-22": 4000.0, "2026-09-23": 4100.0,
                          "2026-09-24": 4059.0},
             "sh000905": {"2026-09-22": 6000.0, "2026-09-23": 6000.0,
                          "2026-09-24": 6180.0},
         }.items()
         for d, v in series.items()])
    c.commit()
    yield c
    c.close()


def _with_valuation(conn, date, rows):
    conn.executemany(
        "INSERT INTO valuation_daily (code, date, pe_ttm, pb, source, fetched_at,"
        " created_at, resp_sha256) VALUES (?,?,?,?,'x',?,?,'h')",
        [(code, date, pe, pb, NOW, NOW) for code, pe, pb in rows])
    conn.commit()


def _with_money_flow(conn, date, rows):
    conn.executemany(
        "INSERT INTO money_flow_daily (code, date, main_net, source, fetched_at,"
        " created_at, resp_sha256) VALUES (?,?,?,'x',?,?,'h')",
        [(code, date, net, NOW, NOW) for code, net in rows])
    conn.commit()


def _with_calendar(conn, dates):
    conn.executemany(
        "INSERT INTO trading_calendar (date, is_open, source, created_at)"
        " VALUES (?,1,'x',?)", [(d, NOW) for d in dates])
    conn.commit()


# ---------- ① 逐字段 ----------

def test_t2_index_levels_and_realized_returns(conn):
    """`ret_N_pct` = 最近 N 个收盘价**首尾**比 —— 数据不足 ⇒ None（不缩小窗口）。"""
    block = market_view.market_block(conn, asof=ASOF)
    idx = block["index"]
    assert block["asof"] == ASOF                      # 决策日，不是数据日
    assert set(idx) == {"sh000300", "sh000905"}
    assert idx["sh000300"]["level"] == 4059.0
    assert idx["sh000300"]["price_asof"] == "2026-09-24"
    # 只有 3 个收盘价 < 5 ⇒ 5/20/60 全部 None（**不拿 3 天冒充 5 天**）
    assert idx["sh000300"]["ret_5_pct"] is None
    assert idx["sh000300"]["ret_20_pct"] is None
    assert idx["sh000300"]["ret_60_pct"] is None


def test_t2_index_returns_use_the_head_and_tail_of_the_window(tmp_db):
    """60 个收盘价（101…160）：`ret_N` = 最近 N 个的**首尾比**，不是「N 日收益率」。"""
    init_db(tmp_db)
    c = connect(tmp_db)
    try:
        days = [f"2026-06-{i:02d}" for i in range(1, 31)] \
            + [f"2026-07-{i:02d}" for i in range(1, 31)]
        c.executemany(
            "INSERT INTO bars_daily (code, date, open, high, low, close, volume,"
            " adj_mode, source, fetched_at)"
            " VALUES ('sh000300',?,?,?,?,?,100,'none','x',?)",
            [(d, 100.0 + i, 100.0 + i, 100.0 + i, 100.0 + i, NOW)
             for i, d in enumerate(days, start=1)])
        c.commit()
        block = market_view.market_block(c, asof=days[-1])
        idx = block["index"]["sh000300"]
    finally:
        c.close()
    assert idx["level"] == 160.0
    # 最近 5 个 = 156…160；最近 20 个 = 141…160；最近 60 个 = 101…160
    assert idx["ret_5_pct"] == pytest.approx((160.0 / 156.0 - 1) * 100, abs=1e-4)
    assert idx["ret_20_pct"] == pytest.approx((160.0 / 141.0 - 1) * 100, abs=1e-4)
    assert idx["ret_60_pct"] == pytest.approx((160.0 / 101.0 - 1) * 100, abs=1e-4)
    # `sh000905` 一行都没有 ⇒ 整条省略（不是报个 0 出来）
    assert "sh000905" not in block["index"]


def test_t2_breadth_counts_and_median(conn):
    """涨跌家数 ＋ 中位涨跌幅：`000004` 只有一天数据 ⇒ 只进 `n_total`。"""
    b = market_view.market_block(conn, asof=ASOF)["breadth"]
    assert b["asof"] == "2026-09-24"
    assert b["n_total"] == 4                       # 该日 4 根 bar
    # 000001: 11/11-1 = 0（平）；000002: 9/10-1 = -10%（跌）；
    # 000003: 12/12-1 = 0（平）；000004 无前值 ⇒ 不参与涨跌计数
    assert (b["n_up"], b["n_down"], b["n_flat"]) == (0, 1, 2)
    assert b["up_ratio"] == 0.0                    # 0 / (0+1+2)
    assert b["median_change_pct"] == 0.0           # median([-10, 0, 0]) = 0


def test_t2_breadth_prefers_the_rows_own_pre_close(conn):
    """行自带 `pre_close` 时**按它的定义**用它（改前收 ⇒ 涨跌家数跟着变）。"""
    conn.execute("UPDATE bars_daily SET pre_close = 8.0"
                 " WHERE code = '000002' AND date = '2026-09-24'")
    conn.commit()
    b = market_view.market_block(conn, asof=ASOF)["breadth"]
    # 000002: 9/8-1 = +12.5% ⇒ 涨
    assert (b["n_up"], b["n_down"], b["n_flat"]) == (1, 0, 2)
    assert b["median_change_pct"] == 0.0           # median([0, 12.5, 0]) = 0.0


def test_t2_valuation_percentiles_and_the_250d_anchor(conn):
    """估值：中位数 / p25 / p75 / pb 中位数，以及与 250 交易日前中位数的对照。"""
    _with_calendar(conn, ["2026-09-22", "2026-09-23", "2026-09-24"])
    _with_valuation(conn, "2026-09-24",
                    [("000001", 10.0, 1.0), ("000002", 20.0, 2.0),
                     ("000003", 30.0, 3.0), ("000004", 40.0, 4.0)])
    # 250 交易日窗口不足 ⇒ 锚点取「<= asof 的最后 250 个交易日里最早那个」= 09-22
    _with_valuation(conn, "2026-09-22",
                    [("000001", 5.0, 1.0), ("000002", 10.0, 2.0),
                     ("000003", 15.0, 3.0)])
    v = market_view.market_block(conn, asof=ASOF)["valuation"]
    assert v["asof"] == "2026-09-24"
    assert v["n_total"] == 4
    assert v["pe_ttm_median"] == 25.0              # median([10,20,30,40])
    assert v["pe_ttm_p25"] == 17.5                 # 线性插值
    assert v["pe_ttm_p75"] == 32.5
    assert v["pb_median"] == 2.5
    assert v["pe_ttm_median_250d_ago"] == 10.0     # median([5,10,15])
    assert v["pe_ttm_median_change_pct"] == 150.0  # (25-10)/10*100


def test_t2_money_flow_uses_yi_and_counts_inflow_share(conn):
    _with_money_flow(conn, "2026-09-24",
                     [("000001", 1e8), ("000002", -3e8), ("000003", 2e8),
                      ("000004", 0.0)])
    m = market_view.market_block(conn, asof=ASOF)["money_flow"]
    assert m["asof"] == "2026-09-24"
    assert m["n_total"] == 4
    assert m["main_net_sum_yi"] == 0.0             # (1-3+2+0) 亿 = 0
    assert m["net_inflow_ratio"] == 0.5            # 2 只 > 0（1 亿、2 亿）/ 4 只


def test_t2_notes_state_that_this_is_a_description_not_a_signal(conn):
    notes = market_view.market_block(conn, asof=ASOF)["notes"]
    assert 1 <= len(notes) <= 3
    assert any("状态描述" in n and "不是方向信号" in n for n in notes)


# ---------- ② PIT ----------

def test_t2_rows_after_asof_do_not_change_the_block(conn):
    before = market_view.market_block(conn, asof=ASOF)
    conn.executemany(
        "INSERT INTO bars_daily (code, date, open, high, low, close, volume, adj_mode,"
        " source, fetched_at) VALUES (?,?,?,?,?,?,100,'none','x',?)",
        [("000001", "2026-09-25", 99.0, 99.0, 99.0, 99.0, NOW),
         ("sh000300", "2026-09-25", 9999.0, 9999.0, 9999.0, 9999.0, NOW)])
    _with_valuation(conn, "2026-09-25", [("000001", 999.0, 9.0)])
    _with_money_flow(conn, "2026-09-25", [("000001", 1e12)])
    after = market_view.market_block(conn, asof=ASOF)
    assert json.dumps(after, ensure_ascii=False, sort_keys=True) \
        == json.dumps(before, ensure_ascii=False, sort_keys=True)


# ---------- ③ 空 / 缺表 ----------

def test_t2_empty_tables_give_zeros_and_none_without_raising(tmp_db):
    init_db(tmp_db)
    c = connect(tmp_db)
    try:
        block = market_view.market_block(c, asof=ASOF)
    finally:
        c.close()
    assert block["asof"] == ASOF
    assert block["index"] == {}
    b = block["breadth"]
    assert (b["asof"], b["n_total"], b["up_ratio"]) == (None, 0, None)
    assert b["median_change_pct"] is None
    v = block["valuation"]
    assert (v["asof"], v["n_total"]) == (None, 0)
    for key in ("pe_ttm_median", "pe_ttm_p25", "pe_ttm_p75", "pb_median",
                "pe_ttm_median_250d_ago", "pe_ttm_median_change_pct"):
        assert v[key] is None, key
    m = block["money_flow"]
    assert (m["asof"], m["n_total"], m["main_net_sum_yi"],
            m["net_inflow_ratio"]) == (None, 0, None, None)


def test_t2_a_bare_database_without_any_table_is_also_fine(tmp_db):
    """连表都没有（不是空表、是没建表）⇒ 同样返回、同样不抛。"""
    c = sqlite3.connect(tmp_db)
    try:
        block = market_view.market_block(c, asof=ASOF)
    finally:
        c.close()
    assert block["index"] == {} and block["breadth"]["n_total"] == 0
    assert block["valuation"]["n_total"] == 0
    assert block["money_flow"]["n_total"] == 0


# ---------- ④ 一条 SQL / 不逐标的 ----------

class _Counting:
    """数 SQL 条数的薄壳（K8：加一只标的不许加一条 SQL）。"""

    def __init__(self, conn):
        self._conn = conn
        self.sql: list[str] = []

    def execute(self, sql, *args, **kwargs):
        self.sql.append(" ".join(str(sql).split()))
        return self._conn.execute(sql, *args, **kwargs)

    def __getattr__(self, name):
        return getattr(self._conn, name)


def _code_count_at(asof: str, n: int) -> list[tuple]:
    return [(f"{i:06d}", asof, 10.0, 10.0, 10.0, 10.0, NOW) for i in range(n)]


def test_t2_sql_count_does_not_grow_with_the_number_of_codes(tmp_db):
    init_db(tmp_db)
    c = connect(tmp_db)
    try:
        c.executemany(
            "INSERT INTO bars_daily (code, date, open, high, low, close, volume,"
            " adj_mode, source, fetched_at)"
            " VALUES (?,?,?,?,?,?,100,'none','x',?)",
            _code_count_at("2026-09-23", 10) + _code_count_at("2026-09-24", 10))
        c.commit()
        small = _Counting(c)
        market_view.market_block(small, asof=ASOF)
        n_small = len(small.sql)
        c.executemany(
            "INSERT INTO bars_daily (code, date, open, high, low, close, volume,"
            " adj_mode, source, fetched_at)"
            " VALUES (?,?,?,?,?,?,100,'none','x',?)",
            _code_count_at("2026-09-23", 400)[10:]
            + _code_count_at("2026-09-24", 400)[10:])
        c.commit()
        big = _Counting(c)
        market_view.market_block(big, asof=ASOF)
    finally:
        c.close()
    assert n_small == len(big.sql), (n_small, big.sql)
    assert n_small <= 12, big.sql          # 上限：指数 1 + 排名 1 + 横截面 2 + 估值 3 + 资金流 2


# ---------- ⑤ 只描述不预测 ----------

_FORWARD_KEYS = ("predict", "forecast", "target", "prob", "signal", "score",
                 "rating", "advice", "recommend", "kelly", "momentum",
                 "direction", "expected")


def _all_keys(obj) -> list[str]:
    if isinstance(obj, dict):
        return [k for k in obj] + [x for v in obj.values() for x in _all_keys(v)]
    if isinstance(obj, list):
        return [x for v in obj for x in _all_keys(v)]
    return []


def test_t2_the_block_has_no_forward_looking_field_names(conn):
    keys = _all_keys(market_view.market_block(conn, asof=ASOF))
    for key in keys:
        for banned in _FORWARD_KEYS:
            assert banned not in key.lower(), f"市场块里出现了前向字段：{key}"
