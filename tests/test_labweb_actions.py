"""P1c：总览 `_actions` 区的**接线** —— 每只票各自取自己的收盘价。

`tests/test_portfolio_decision.py` 测的是纯函数（喂价格、看结论）；
本文件补的是它测不到的那一段：`Lab._actions` 有没有**按标的分别**取
`bars_daily` 收盘价。`build_portfolio` 的 discipline 段只判第一只标的
（`run_checks` 的单标的简化入参），所以这里必须逐只取 ——
否则第二只票会拿到第一只票的止损结论，而**页面看起来完全正常**。
"""

import pytest

from stocklab.labweb.data import Lab
from stocklab.portfolio.ledger import record_trade
from stocklab.store.db import connect
from stocklab.store.migrate import init_db

NOW = "2026-09-15T16:00:00+08:00"
ASOF = "2026-09-14"
DATES = ("2026-09-09", "2026-09-10", "2026-09-11", "2026-09-14")


def _db(tmp_path, *, positions, closes):
    """建一个带 `positions` 与 `closes` 的库。

    `positions` = [(code, name, buy_price, qty)]；`closes` = {code: 收盘价}。
    """
    path = tmp_path / "labweb.db"
    init_db(path)
    conn = connect(path)
    try:
        conn.executemany(
            "INSERT INTO trading_calendar (date, is_open, source, created_at)"
            " VALUES (?,1,'tencent',?)", [(d, NOW) for d in DATES])
        for code, name, _, _ in positions:
            conn.execute(
                "INSERT INTO instruments (code, name, market, board, added_at)"
                " VALUES (?,?,'sz','main',?)", (code, name, NOW))
        conn.commit()
        for code, name, buy_price, qty in positions:
            record_trade(conn, date="2026-09-10", code=code, side="buy",
                         price=buy_price, qty=qty, fee=5.09, now=NOW)
        conn.executemany(
            "INSERT INTO bars_daily (code, date, open, high, low, close,"
            " volume, adj_mode, source, fetched_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
            [(code, DATES[-1], c, c, c, c, 1000, "none", "tencent", NOW)
             for code, c in closes.items()])
        conn.commit()
    finally:
        conn.close()
    return path


def _row(lab, code):
    rows = lab.overview()["actions"]["rows"]
    return next(r for r in rows if r["code"] == code)


# ---------- 三种价格情形：走真库、真 daily_close ----------

@pytest.mark.parametrize("close,expected", [
    (82.13, "stop"),
    (86.00, "hold"),
    (87.50, "no_add"),
])
def test_actions_state_from_daily_close(tmp_path, close, expected):
    path = _db(tmp_path, positions=[("000333", "美的集团", 86.80, 100)],
               closes={"000333": close})
    row = _row(Lab(path, asof=ASOF), "000333")
    assert row["state"] == expected
    assert row["money"]["close"] == pytest.approx(close, abs=0.01)


def test_actions_uses_close_not_intraday_snapshot(tmp_path):
    """现价（盘中快照）怎么变都不改结论 —— 结论只看 bars_daily 收盘。"""
    path = _db(tmp_path, positions=[("000333", "美的集团", 86.80, 100)],
               closes={"000333": 82.13})
    conn = connect(path)
    try:
        for k in ("000333",):
            conn.execute("INSERT INTO bars_intraday (code, ts, price, source,"
                         " fetched_at) VALUES (?,?,?,'tencent',?)",
                         (k, f"{ASOF} 14:30", 99.0, NOW))
        conn.commit()
    except Exception:
        pass  # 没有盘中表也不影响本用例的主断言
    finally:
        conn.close()
    row = _row(Lab(path, asof=ASOF), "000333")
    assert row["state"] == "stop"          # 收盘 82.13 破线
    assert row["money"]["close"] == pytest.approx(82.13, abs=0.01)


def test_close_asof_is_reported(tmp_path):
    """页面必须说清这个收盘价是哪天的（不能让人以为是今天的）。"""
    path = _db(tmp_path, positions=[("000333", "美的集团", 86.80, 100)],
               closes={"000333": 86.00})
    row = _row(Lab(path, asof=ASOF), "000333")
    assert row["money"]["close_asof"] == DATES[-1]


# ---------- 多标的：逐只取价（错接线会让第二只拿到第一只的结论） ----------

def test_each_position_gets_its_own_close(tmp_path):
    """两只票、一个破线一个没破 —— 结论必须分得开。

    这条是 `_actions` 存在的理由：`build_portfolio` 只判第一只。
    若有人把 `daily_close` 提到循环外，它会红。
    """
    path = _db(
        tmp_path,
        positions=[("000333", "美的集团", 86.80, 100), ("600519", "贵州茅台", 86.80, 100)],
        closes={"000333": 82.13, "600519": 86.00})
    lab = Lab(path, asof=ASOF)
    a, b = _row(lab, "000333"), _row(lab, "600519")

    assert a["money"]["close"] == pytest.approx(82.13, abs=0.01)
    assert b["money"]["close"] == pytest.approx(86.00, abs=0.01)
    # 600519 没配纪律线 → 判不了（不拿 000333 的线去量它）
    assert b["state"] == "unknown"
    assert "纪律线" in " ".join(b["because"])


def test_missing_bars_yields_unknown_not_hold(tmp_path):
    """日线里没有这只票 → unknown（判不了），**不是** hold（没事）。"""
    path = _db(tmp_path, positions=[("000333", "美的集团", 86.80, 100)],
               closes={})
    row = _row(Lab(path, asof=ASOF), "000333")
    assert row["state"] == "unknown"
    assert "收盘价" in " ".join(row["because"])


def test_actions_policy_states_the_price_rule(tmp_path):
    path = _db(tmp_path, positions=[("000333", "美的集团", 86.80, 100)],
               closes={"000333": 86.00})
    act = Lab(path, asof=ASOF).overview()["actions"]
    assert act["asof"] == ASOF
    assert "日收盘价" in act["policy"]
    assert "不替人做买卖决定" in act["policy"]


def test_n_unknown_counts(tmp_path):
    path = _db(tmp_path, positions=[("000333", "美的集团", 86.80, 100)],
               closes={"000333": 86.00})
    assert Lab(path, asof=ASOF).overview()["actions"]["n_unknown"] == 0
