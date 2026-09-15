"""P12 / Task 58：CLI —— trade add / trade reverse / cash add / portfolio show。"""

import json

import pytest

from stocklab.cli.main import main
from stocklab.store.db import connect
from stocklab.store.migrate import init_db

NOW = "2026-09-15T16:00:00+08:00"
CAL = ("2026-09-09", "2026-09-10", "2026-09-11", "2026-09-14")


@pytest.fixture
def db(tmp_db):
    init_db(tmp_db)
    c = connect(tmp_db)
    c.execute("INSERT INTO instruments (code, name, market, board, added_at)"
              " VALUES ('000333','美的集团','sz','main',?)", (NOW,))
    c.executemany(
        "INSERT INTO trading_calendar (date, is_open, source, created_at)"
        " VALUES (?,1,'tencent',?)", [(d, NOW) for d in CAL])
    c.commit()
    c.close()
    return tmp_db


def run(db, *argv, capsys):
    code = main([*argv, "--db", str(db), "--now", NOW])
    out, err = capsys.readouterr()
    return code, out, err


def add_real_buy(db, capsys, **kw):
    argv = ["trade", "add", "--date", "2026-09-14", "--code", "000333",
            "--side", "buy", "--price", "86.80", "--qty", "100", "--fee", "5.09"]
    for k, v in kw.items():
        argv += [f"--{k.replace('_', '-')}"] + ([] if v is True else [str(v)])
    return run(db, *argv, capsys=capsys)


# ---------- trade add ----------

def test_trade_add_prints_the_row(db, capsys):
    code, out, err = add_real_buy(db, capsys)
    assert code == 0, err
    assert "已录入" in out and "trade_id=1" in out
    assert "2026-09-14 buy 000333 100股 @86.8 费5.09" in out


def test_trade_add_json_mode(db, capsys):
    code, out, err = add_real_buy(db, capsys, json=True)
    assert code == 0
    payload = json.loads(out)
    assert payload["state"] == "inserted"
    assert payload["trade_id"] == 1


def test_trade_add_validation_error_exits_2(db, capsys):
    code, out, err = add_real_buy(db, capsys, qty=150)      # 非整手
    assert code == 2
    assert "整手" in err
    assert json.loads(err.splitlines()[0])["kind"] == "TradeValidationError"


def test_trade_add_future_date_rejected(db, capsys):
    code, out, err = run(db, "trade", "add", "--date", "2026-09-15",
                         "--code", "000333", "--side", "buy",
                         "--price", "87.0", "--qty", "100", capsys=capsys)
    assert code == 2
    assert "禁录未来成交" in err


def test_trade_add_missing_db_exits_2(tmp_path, capsys):
    code, out, err = run(tmp_path / "nope.db", "trade", "add", "--date",
                         "2026-09-14", "--code", "000333", "--side", "buy",
                         "--price", "86.8", "--qty", "100", capsys=capsys)
    assert code == 2
    assert "db not found" in err


# ---------- 幂等 / 重复 ----------

def test_duplicate_without_key_is_rejected(db, capsys):
    add_real_buy(db, capsys)
    code, out, err = add_real_buy(db, capsys)
    assert code == 2
    assert "--allow-duplicate" in err and "--idempotency-key" in err


def test_allow_duplicate_flag_lets_it_in(db, capsys):
    add_real_buy(db, capsys)
    code, out, err = add_real_buy(db, capsys, allow_duplicate=True)
    assert code == 0
    assert "已录入" in out


def test_idempotency_key_second_run_is_a_hit(db, capsys):
    code1, out1, _ = add_real_buy(db, capsys, idempotency_key="retry-1")
    code2, out2, _ = add_real_buy(db, capsys, idempotency_key="retry-1")
    assert (code1, code2) == (0, 0), "幂等命中也是成功（调度链的口径）"
    assert "幂等命中" in out2
    conn = connect(db)
    assert conn.execute("SELECT COUNT(*) FROM real_trades").fetchone()[0] == 1
    conn.close()


# ---------- trade reverse ----------

def test_trade_reverse_appends_and_keeps_the_original(db, capsys):
    add_real_buy(db, capsys)
    code, out, err = run(db, "trade", "reverse", "1", "--reason", "录错券商",
                         capsys=capsys)
    assert code == 0, err
    assert "原行未改动" in out
    conn = connect(db)
    rows = [dict(r) for r in conn.execute("SELECT * FROM real_trades ORDER BY trade_id")]
    conn.close()
    assert len(rows) == 2
    assert rows[0]["side"] == "buy" and rows[0]["qty"] == 100
    assert rows[1]["side"] == "sell" and rows[1]["qty"] == 100
    assert "冲正" in rows[1]["note"] and "录错券商" in rows[1]["note"]


def test_trade_reverse_requires_reason(db, capsys):
    add_real_buy(db, capsys)
    code = main(["trade", "reverse", "1", "--db", str(db), "--now", NOW])
    assert code == 2       # argparse：--reason 必填


def test_trade_reverse_unknown_id(db, capsys):
    code, out, err = run(db, "trade", "reverse", "99", "--reason", "x", capsys=capsys)
    assert code == 2
    assert "不存在" in err


# ---------- cash add ----------

def add_principal(db, capsys, **kw):
    argv = ["cash", "add", "--date", "2026-09-14", "--kind", "deposit",
            "--amount", "20000"]
    for k, v in kw.items():
        argv += [f"--{k.replace('_', '-')}"] + ([] if v is True else [str(v)])
    return run(db, *argv, capsys=capsys)


def test_cash_add_principal(db, capsys):
    code, out, err = add_principal(db, capsys)
    assert code == 0, err
    assert "flow_id=1" in out and "+20,000.00" in out


def test_cash_add_rejects_wrong_sign(db, capsys):
    code, out, err = add_principal(db, capsys, amount=-100)
    assert code == 2
    assert "deposit 必须 > 0" in err


def test_cash_add_idempotency(db, capsys):
    add_principal(db, capsys, idempotency_key="principal")
    code, out, err = add_principal(db, capsys, idempotency_key="principal")
    assert code == 0 and "幂等命中" in out
    conn = connect(db)
    assert conn.execute("SELECT COUNT(*) FROM cash_flows").fetchone()[0] == 1
    conn.close()


# ---------- portfolio show ----------

def add_bar(db, code, date, close):
    conn = connect(db)
    conn.execute(
        "INSERT INTO bars_daily (code, date, open, high, low, close, volume, adj_mode,"
        " source, fetched_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
        (code, date, close, close, close, close, 1000, "none", "tencent", NOW))
    conn.commit()
    conn.close()


def seed_full(db, capsys, *, with_bar=True):
    add_principal(db, capsys)
    add_real_buy(db, capsys)
    if with_bar:
        add_bar(db, "000333", "2026-09-14", 86.80)


def test_portfolio_show_table_mode(db, capsys):
    seed_full(db, capsys)
    code, out, err = run(db, "portfolio", "show", "--asof", "2026-09-14", capsys=capsys)
    assert "11,314.91" in out
    assert "000333" in out
    assert "FAIL" in out and "43.41" in out
    assert code == 1, "单票 43.41% > 40% → 报警（缺现价/超限都要人来看）"
    assert "[FAIL]" in err


def test_portfolio_show_json_mode(db, capsys):
    seed_full(db, capsys)
    code, out, err = run(db, "portfolio", "show", "--asof", "2026-09-14",
                         "--json", capsys=capsys)
    view = json.loads(out)
    assert view["asof"] == "2026-09-14"
    assert view["cash"] == 11314.91
    assert set(view) >= {"positions", "discipline", "warnings", "price_policy"}


def test_portfolio_show_asof_defaults_to_today(db, capsys):
    code, out, err = run(db, "portfolio", "show", "--json", capsys=capsys)
    assert json.loads(out)["asof"] == "2026-09-15"


def test_portfolio_show_clean_account_exits_0(db, capsys):
    """什么都不违规时退出码必须是 0 —— 否则报警就失去了信号价值。"""
    conn = connect(db)
    conn.execute("INSERT INTO instruments (code, name, market, board, added_at)"
                 " VALUES ('600690','海尔智家','sh','main',?)", (NOW,))
    conn.execute("INSERT INTO cash_flows (date, kind, amount, note, created_at)"
                 " VALUES ('2026-09-14','deposit',100000.0,NULL,?)", (NOW,))
    conn.execute("INSERT INTO real_trades (date, code, side, price, qty, fee, note,"
                 " created_at) VALUES ('2026-09-14','600690','buy',20.0,1000,5.0,NULL,?)",
                 (NOW,))
    conn.execute("INSERT INTO bars_daily (code, date, open, high, low, close, volume,"
                 " adj_mode, source, fetched_at) VALUES"
                 " ('600690','2026-09-14',20,20,20,20,1000,'none','tencent',?)", (NOW,))
    conn.execute("INSERT INTO trading_calendar (date, is_open, source, created_at)"
                 " VALUES ('2026-09-15',1,'tencent',?)", (NOW,))
    conn.commit()
    conn.close()
    code, out, err = run(db, "portfolio", "show", "--asof", "2026-09-14",
                         "--json", capsys=capsys)
    view = json.loads(out)
    assert code == 0, err
    assert view["missing_price_codes"] == []
    assert not [c for c in view["discipline"] if c["status"] == "FAIL"]
    # 20,000 市值 / 99,995 总资产 = 20.00% → 未超 40%
    # 600690 未配纪律线 → 止损/禁补仓三条 UNDETERMINED（不拿 000333 的线去量它）
    by = {c["check"]: c["status"] for c in view["discipline"]}
    assert by["stop_loss_close_85"] == "UNDETERMINED"
    # 现金 80% → 带外 WARN：WARN **不**触发报警（报警只认 FAIL 与缺现价），
    # 否则「偏离目标带」会和「破硬约束」共用一个信号，报警就失去分辨力。
    assert by["cash_band_45_60pct"] == "WARN"


def test_portfolio_show_missing_price_alarms(db, capsys):
    seed_full(db, capsys, with_bar=False)      # 一分行情都不给
    code, out, err = run(db, "portfolio", "show", "--asof", "2026-09-14",
                         "--json", capsys=capsys)
    assert code == 1
    assert json.loads(out)["missing_price_codes"] == ["000333"]
    assert "无可用现价" in err


def test_portfolio_show_usage_error_without_asof_value(db, capsys):
    code = main(["portfolio", "show", "--asof"])
    assert code == 2       # argparse 缺值
