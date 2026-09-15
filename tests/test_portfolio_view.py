"""P12 / Task 58：组合视图 + 稳定 JSON 接口。"""

import json

import pytest

from stocklab.portfolio.view import (
    PRICE_POLICY,
    build_portfolio,
    daily_close,
    has_alarm,
    render_table,
    weekly_close,
)
from stocklab.store.db import connect
from stocklab.store.migrate import init_db

NOW = "2026-09-15T16:00:00+08:00"
CAL = ("2026-09-09", "2026-09-10", "2026-09-11", "2026-09-14")

#: `--json` 的顶层字段集合 = P13 的接口契约。改动必须同步改 ADR/文档。
TOP_LEVEL_FIELDS = {
    "asof", "cash", "cash_breakdown", "market_value_priced", "market_value_missing",
    "total_assets", "net_invested", "total_pnl_incl_fee", "total_return_incl_fee",
    "positions", "missing_price_codes", "discipline", "advisory", "warnings",
    "ledger", "price_policy", "cost_policy",
}

POSITION_FIELDS = {
    "code", "name", "qty", "avg_cost_incl_fee", "avg_cost_excl_fee",
    "cost_basis_incl_fee", "cost_basis_excl_fee",
    "price", "price_source", "price_asof", "price_detail", "market_value",
    "float_pnl_incl_fee", "float_pnl_excl_fee", "weight_of_total_assets",
    "realized_pnl_incl_fee", "realized_pnl_excl_fee", "fees_paid", "status",
}

CHECK_FIELDS = {"check", "subject", "status", "detail", "numbers"}
ADVISORY_FIELDS = {"rule", "code", "pct", "shares", "note"}


@pytest.fixture
def conn(tmp_db):
    init_db(tmp_db)
    c = connect(tmp_db)
    c.execute("INSERT INTO instruments (code, name, market, board, added_at)"
              " VALUES ('000333','美的集团','sz','main',?)", (NOW,))
    c.executemany(
        "INSERT INTO trading_calendar (date, is_open, source, created_at)"
        " VALUES (?,1,'tencent',?)", [(d, NOW) for d in CAL])
    c.commit()
    yield c
    c.close()


def seed_real_account(conn):
    """用户真实账本：20,000 本金 + 2026-09-14 买入 000333 100@86.80 费 5.09。"""
    conn.execute("INSERT INTO cash_flows (date, kind, amount, note, created_at)"
                 " VALUES ('2026-09-14','deposit',20000.0,'本金',?)", (NOW,))
    conn.execute("INSERT INTO real_trades (date, code, side, price, qty, fee, note,"
                 " created_at) VALUES ('2026-09-14','000333','buy',86.80,100,5.09,NULL,?)",
                 (NOW,))
    conn.commit()


def add_bar(conn, code, date, close):
    conn.execute(
        "INSERT INTO bars_daily (code, date, open, high, low, close, volume, adj_mode,"
        " source, fetched_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
        (code, date, close, close, close, close, 1000, "none", "tencent", NOW))


def add_snapshot(conn, code, trade_date, ts, price):
    conn.execute(
        "INSERT INTO quote_snapshots (code, trade_date, ts, price, volume, source,"
        " fetched_at) VALUES (?,?,?,?,?,?,?)",
        (code, trade_date, ts, price, 1000, "tencent", NOW))


# ---------- 接口契约（防止以后悄悄改名） ----------

def test_json_schema_is_pinned(conn):
    seed_real_account(conn)
    add_bar(conn, "000333", "2026-09-14", 86.80)
    view = build_portfolio(conn, "2026-09-14")
    assert set(view) == TOP_LEVEL_FIELDS, (
        f"顶层字段变了：多了 {set(view) - TOP_LEVEL_FIELDS}，"
        f"少了 {TOP_LEVEL_FIELDS - set(view)} —— P13 会静默渲染出空格子")
    assert set(view["positions"][0]) == POSITION_FIELDS
    assert set(view["cash_breakdown"]) == {"net_deposits", "trade_cash", "other_cash"}
    assert set(view["ledger"]) == {"trades", "cash_flows"}
    for c in view["discipline"]:
        assert set(c) == CHECK_FIELDS
    for a in view["advisory"]:
        assert set(a) == ADVISORY_FIELDS


def test_json_is_serializable_and_deterministic(conn):
    seed_real_account(conn)
    add_bar(conn, "000333", "2026-09-14", 86.80)
    a = build_portfolio(conn, "2026-09-14")
    b = build_portfolio(conn, "2026-09-14")
    assert json.dumps(a, sort_keys=True, ensure_ascii=False) == \
           json.dumps(b, sort_keys=True, ensure_ascii=False), "同输入两次结果必须逐字节一致"


# ---------- 真实账本 ----------

def test_real_account_cash_is_fee_inclusive(conn):
    """含费口径：20000 − 8680 − 5.09 = 11314.91（不是用户口述的 11320）。"""
    seed_real_account(conn)
    add_bar(conn, "000333", "2026-09-14", 86.80)
    view = build_portfolio(conn, "2026-09-14")
    assert view["cash"] == 11314.91
    assert view["cash_breakdown"]["net_deposits"] == 20000.0
    assert view["cash_breakdown"]["trade_cash"] == -8685.09
    assert view["net_invested"] == 20000.0


def test_real_account_totals_asof_0914(conn):
    seed_real_account(conn)
    add_bar(conn, "000333", "2026-09-14", 86.80)
    view = build_portfolio(conn, "2026-09-14")
    pos = view["positions"][0]
    assert pos["price_source"] == "bars"
    assert pos["price_asof"] == "2026-09-14"
    assert pos["market_value"] == 8680.00
    assert pos["avg_cost_incl_fee"] == 86.8509
    assert pos["avg_cost_excl_fee"] == 86.80
    assert pos["float_pnl_incl_fee"] == -5.09, "含费口径下买入当天必然浮亏一个手续费"
    assert pos["float_pnl_excl_fee"] == 0.0
    assert view["total_assets"] == 19994.91
    assert view["total_pnl_incl_fee"] == -5.09


def test_real_account_overweight_position_fails_discipline(conn):
    """当前持仓 43.41% > 40% —— 必须显式报警（这是本仓的真实状态）。"""
    seed_real_account(conn)
    add_bar(conn, "000333", "2026-09-14", 86.80)
    view = build_portfolio(conn, "2026-09-14")
    by = {c["check"]: c for c in view["discipline"]}
    overweight = by["single_position_max_40pct"]
    assert overweight["status"] == "FAIL"
    assert overweight["numbers"]["weight_pct"] == pytest.approx(43.41, abs=0.01)
    assert by["cash_band_45_60pct"]["status"] == "PASS"
    assert any(w.startswith("[FAIL]") for w in view["warnings"])
    assert has_alarm(view) is True


def test_snapshot_price_used_for_0915(conn):
    """09-15 有当日快照 → 现价取快照（ts 最大），并如实标注来源。"""
    seed_real_account(conn)
    add_bar(conn, "000333", "2026-09-14", 86.80)
    add_snapshot(conn, "000333", "2026-09-15", "20260915135048", 87.44)
    add_snapshot(conn, "000333", "2026-09-15", "20260915135109", 87.45)
    view = build_portfolio(conn, "2026-09-15")
    pos = view["positions"][0]
    assert pos["price"] == 87.45
    assert pos["price_source"] == "snapshot"
    assert pos["price_asof"] == "2026-09-15"
    assert pos["market_value"] == 8745.00
    assert pos["float_pnl_incl_fee"] == pytest.approx(59.91)
    assert view["total_assets"] == 20059.91


def test_close_based_stop_loss_does_not_use_the_intraday_snapshot(conn):
    """收盘价口径要用日收盘，**不能**拿盘中快照判「收盘破没破位」。"""
    seed_real_account(conn)
    add_bar(conn, "000333", "2026-09-14", 86.80)
    add_snapshot(conn, "000333", "2026-09-15", "20260915135109", 87.45)
    view = build_portfolio(conn, "2026-09-15")
    c = {x["check"]: x for x in view["discipline"]}["stop_loss_close_85"]
    assert c["numbers"]["close"] == 86.80        # bars 的 09-14 收盘，不是 87.45
    assert c["numbers"]["close_asof"] == "2026-09-14"
    assert c["status"] == "PASS"


def test_no_add_above_87_warns_on_0915(conn):
    """87.45 ≥ 87 → 禁补仓（WARN）。"""
    seed_real_account(conn)
    add_bar(conn, "000333", "2026-09-14", 86.80)
    add_snapshot(conn, "000333", "2026-09-15", "20260915135109", 87.45)
    view = build_portfolio(conn, "2026-09-15")
    c = {x["check"]: x for x in view["discipline"]}["no_add_above_87"]
    assert c["status"] == "WARN"
    assert "禁止补仓" in c["detail"]


def test_asof_does_not_see_future_trades(conn):
    """09-14 的视图不能看见 09-15 的成交 —— 那是未来。"""
    seed_real_account(conn)
    add_bar(conn, "000333", "2026-09-14", 86.80)
    conn.execute("INSERT INTO real_trades (date, code, side, price, qty, fee, note,"
                 " created_at) VALUES ('2026-09-15','000333','buy',87.00,100,5.09,NULL,?)",
                 (NOW,))
    conn.commit()
    view = build_portfolio(conn, "2026-09-14")
    assert view["positions"][0]["qty"] == 100
    assert view["ledger"]["trades"] == 1
    assert view["cash"] == 11314.91
    view15 = build_portfolio(conn, "2026-09-15")
    assert view15["positions"][0]["qty"] == 200


# ---------- missing_price ----------

def test_missing_price_is_excluded_and_alarmed(conn):
    seed_real_account(conn)
    view = build_portfolio(conn, "2026-09-14")      # 没有任何行情
    pos = view["positions"][0]
    assert pos["status"] == "missing_price"
    assert pos["price"] is None and pos["market_value"] is None
    assert pos["weight_of_total_assets"] is None
    assert view["missing_price_codes"] == ["000333"]
    assert view["market_value_priced"] == 0.0
    assert view["total_assets"] == 11314.91, "缺现价不许用成本价冒充，市值就是 0"
    assert has_alarm(view) is True
    assert any("无可用现价" in w for w in view["warnings"])


def test_missing_price_weight_check_is_undetermined_not_pass(conn):
    seed_real_account(conn)
    view = build_portfolio(conn, "2026-09-14")
    c = {x["check"]: x for x in view["discipline"]}["single_position_max_40pct"]
    assert c["status"] == "UNDETERMINED"


def test_no_price_means_no_stop_loss_verdict(conn):
    seed_real_account(conn)
    view = build_portfolio(conn, "2026-09-14")
    by = {x["check"]: x for x in view["discipline"]}
    assert by["stop_loss_close_85"]["status"] == "UNDETERMINED"
    assert by["stop_loss_weekly_83_25"]["status"] == "UNDETERMINED"


def test_missing_price_position_does_not_break_the_rest(conn):
    """一只票缺价不该让整张视图算不出来 —— 其余标的照常定价并汇总。"""
    conn.execute("INSERT INTO instruments (code, name, market, board, added_at)"
                 " VALUES ('600690','海尔智家','sh','main',?)", (NOW,))
    conn.execute("INSERT INTO cash_flows (date, kind, amount, note, created_at)"
                 " VALUES ('2026-09-14','deposit',20000.0,NULL,?)", (NOW,))
    for code in ("000333", "600690"):
        conn.execute("INSERT INTO real_trades (date, code, side, price, qty, fee, note,"
                     " created_at) VALUES ('2026-09-14',?,'buy',10.0,100,0.0,NULL,?)",
                     (code, NOW))
    conn.commit()
    add_bar(conn, "000333", "2026-09-14", 20.0)
    view = build_portfolio(conn, "2026-09-14")
    assert view["missing_price_codes"] == ["600690"]
    assert view["market_value_priced"] == 2000.00
    assert view["total_assets"] == view["cash"] + 2000.00
    assert len(view["positions"]) == 2


# ---------- 纪律检查的完整性 ----------

def test_all_six_checks_are_always_reported(conn):
    seed_real_account(conn)
    add_bar(conn, "000333", "2026-09-14", 86.80)
    view = build_portfolio(conn, "2026-09-14")
    names = [c["check"] for c in view["discipline"]]
    assert names == [
        "single_position_max_40pct", "cash_band_45_60pct", "stop_loss_close_85",
        "stop_loss_weekly_83_25", "no_add_above_87", "cash_per_trade_max_5pct"]


def test_weekly_close_stops_at_the_week_boundary(conn):
    """周线取本周内的收盘；上周的不算。2026-09-14 是周一。"""
    add_bar(conn, "000333", "2026-09-11", 86.26)     # 上周五
    add_bar(conn, "000333", "2026-09-14", 86.80)     # 本周一
    assert weekly_close(conn, "000333", "2026-09-14") == (86.80, "2026-09-14")
    # 周一之前的那一天（上周五）→ 它所在周只有它自己
    assert weekly_close(conn, "000333", "2026-09-11") == (86.26, "2026-09-11")


def test_weekly_close_is_none_for_a_week_with_no_bars(conn):
    add_bar(conn, "000333", "2026-09-11", 86.26)
    assert weekly_close(conn, "000333", "2026-09-14") == (None, None)


def test_daily_close_falls_back_to_earlier_date(conn):
    """asof 当天无 bar（停牌/未采集）→ 用更早的收盘，并如实标注是哪天。"""
    add_bar(conn, "000333", "2026-09-11", 86.26)
    assert daily_close(conn, "000333", "2026-09-14") == (86.26, "2026-09-11")


def test_cash_per_trade_gives_the_5pct_budget(conn):
    seed_real_account(conn)
    add_bar(conn, "000333", "2026-09-14", 86.80)
    view = build_portfolio(conn, "2026-09-14")
    c = {x["check"]: x for x in view["discipline"]}["cash_per_trade_max_5pct"]
    assert c["numbers"]["limit_amount"] == pytest.approx(999.75, abs=0.01)


def test_advisory_translates_rules_into_shares(conn):
    """纪律是百分比，人要的是股数。"""
    seed_real_account(conn)
    add_bar(conn, "000333", "2026-09-14", 86.80)
    view = build_portfolio(conn, "2026-09-14")
    by = {a["rule"]: a for a in view["advisory"]}
    assert by["trim_light_10pct"]["shares"] == 10
    assert by["trim_on_break_20pct"]["shares"] == 20


# ---------- 边界 ----------

def test_empty_ledger_view_does_not_crash(conn):
    view = build_portfolio(conn, "2026-09-14")
    assert view["cash"] == 0.0
    assert view["positions"] == []
    assert view["total_assets"] == 0.0
    assert view["total_return_incl_fee"] is None, "净投入为 0 时收益率没有定义，给 null 不是 0"
    assert view["net_invested"] == 0.0


def test_positive_return_after_a_rise(conn):
    seed_real_account(conn)
    add_bar(conn, "000333", "2026-09-14", 86.80)
    add_snapshot(conn, "000333", "2026-09-15", "20260915135109", 87.45)
    view = build_portfolio(conn, "2026-09-15")
    assert view["total_pnl_incl_fee"] == pytest.approx(59.91)
    assert view["total_return_incl_fee"] == pytest.approx(59.91 / 20000, abs=1e-6)


def test_render_table_contains_key_numbers(conn):
    seed_real_account(conn)
    add_bar(conn, "000333", "2026-09-14", 86.80)
    text = render_table(build_portfolio(conn, "2026-09-14"))
    assert "000333" in text and "美的集团" not in text  # 表里只有 code，name 在 JSON
    assert "11,314.91" in text
    assert "19,994.91" in text
    assert "86.8509" in text                 # 含费均价
    assert "86.8000" in text                 # 不含费均价
    assert "FAIL" in text and "43.41" in text
    assert PRICE_POLICY[:12] in text


def test_render_table_marks_missing_price(conn):
    seed_real_account(conn)
    text = render_table(build_portfolio(conn, "2026-09-14"))
    assert "无可用现价" in text
    assert "不用成本价冒充" in text
