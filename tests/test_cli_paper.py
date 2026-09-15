"""P19：CLI —— `paper init` / `paper step` / `paper show`。"""

import json

import pytest

from stocklab.cli.main import main
from stocklab.store.db import connect
from stocklab.store.migrate import init_db

NOW = "2026-09-15T16:00:00+08:00"
CAL = ("2026-09-11", "2026-09-14", "2026-09-15", "2026-09-16")
BARS = {
    "000333": {"2026-09-14": 86.80, "2026-09-15": 87.23, "2026-09-16": 87.60},
    "510300": {"2026-09-15": 4.523, "2026-09-16": 4.500},
    "510880": {"2026-09-15": 3.382, "2026-09-16": 3.390},
    "sh000300": {"2026-09-15": 4450.04, "2026-09-16": 4460.00},
}


@pytest.fixture
def db(tmp_db):
    init_db(tmp_db)
    c = connect(tmp_db)
    c.executemany("INSERT INTO instruments (code, name, market, board, type, added_at)"
                  " VALUES (?,?,'sz','main',?,?)",
                  [(code, code, "stock" if code == "000333" else "etf", NOW)
                   for code in ("000333", "510300", "510880")])
    c.executemany("INSERT INTO trading_calendar (date, is_open, source, created_at)"
                  " VALUES (?,1,'tencent',?)", [(d, NOW) for d in CAL])
    c.executemany(
        "INSERT INTO bars_daily (code, date, open, high, low, close, volume, adj_mode,"
        " source, fetched_at) VALUES (?,?,?,?,?,?,100,'none','x',?)",
        [(code, d, v, v, v, v, NOW) for code, s in BARS.items() for d, v in s.items()])
    c.execute("INSERT INTO cash_flows (date, kind, amount, note, created_at)"
              " VALUES ('2026-09-14','deposit',20000.0,'本金',?)", (NOW,))
    c.execute("INSERT INTO real_trades (date, code, side, price, qty, fee, note,"
              " created_at) VALUES ('2026-09-14','000333','buy',86.80,100,5.09,'首笔',?)",
              (NOW,))
    c.commit()
    c.close()
    return tmp_db


def run(db, *argv, capsys):
    code = main([*argv, "--db", str(db), "--now", NOW])
    out, err = capsys.readouterr()
    return code, out, err


def test_paper_init_then_step_then_show(db, tmp_path, capsys):
    code, out, _ = run(db, "paper", "init", capsys=capsys)
    assert code == 0, out
    rep = json.loads(out)
    assert rep["created"] is True and len(rep["accounts"]) == 5

    out_path = tmp_path / "2026-09-15-paper.md"
    code, out, err = run(db, "paper", "step", "--asof", "2026-09-15",
                         "--out", str(out_path), capsys=capsys)
    assert code == 0, err
    payload = json.loads(out)
    assert payload["asof"] == "2026-09-15"
    assert len(payload["accounts"]) == 5
    assert payload["index_300"]["level"] == 4450.04

    md = out_path.read_text(encoding="utf-8")
    assert "模拟盘 ≠ 实盘" in md and "LIVE 仍为 0" in md
    assert "样本 <120 交易日不算结论" in md
    for tag in ("arm-hold", "arm-now", "arm-discipline-05", "arm-discipline-10",
                "arm-discipline-15"):
        assert tag in md
    assert "最大回撤" in md and "累计成本" in md and "index_300" in md

    code, out, err = run(db, "paper", "show", "--asof", "2026-09-15", capsys=capsys)
    assert code == 0, err
    assert "arm-discipline-15" in out


def test_step_rerun_is_byte_identical_on_stdout_and_report(db, tmp_path, capsys):
    """验收 ①：同日重跑 —— stdout 逐字节一致、报告逐字节一致、不重复下单。"""
    run(db, "paper", "init", capsys=capsys)
    out_path = tmp_path / "r.md"
    _, first, _ = run(db, "paper", "step", "--asof", "2026-09-15",
                      "--out", str(out_path), capsys=capsys)
    md_first = out_path.read_text(encoding="utf-8")
    c = connect(db)
    n = c.execute("SELECT COUNT(*) n FROM paper_trades").fetchone()["n"]
    c.close()

    _, second, _ = run(db, "paper", "step", "--asof", "2026-09-15",
                       "--out", str(out_path), capsys=capsys)
    assert second == first
    assert out_path.read_text(encoding="utf-8") == md_first
    c = connect(db)
    assert c.execute("SELECT COUNT(*) n FROM paper_trades").fetchone()["n"] == n
    c.close()


def test_step_without_init_exits_2_with_reason(db, capsys):
    code, _, err = run(db, "paper", "step", "--asof", "2026-09-15", capsys=capsys)
    assert code == 2
    assert "paper init" in err


def test_step_before_start_date_exits_2(db, capsys):
    run(db, "paper", "init", capsys=capsys)
    code, _, err = run(db, "paper", "step", "--asof", "2026-09-14", capsys=capsys)
    assert code == 2
    assert "起跑日" in err


def test_step_rejects_unpriceable_holding(db, tmp_path, capsys):
    """持仓**根本无价可依**（库里有史以来没有一根 bar）→ 退出码 2，不落净值行。

    注意与「停牌用上一交易日收盘」区分：那是**合法**的 PIT 回退（`price_asof`
    会如实指回更早那天）；这里说的是**一根 bar 都没有**，回退无处可退。
    """
    # 先按声明口径 init（账本此时仍是 100 股 @86.80），再让实盘账本多出一笔
    # 无行情标的的成交 —— arm-now 镜像到它，于是这一步没法估值。
    run(db, "paper", "init", capsys=capsys)
    c = connect(db)
    c.execute("INSERT INTO instruments (code, name, market, board, type, added_at)"
              " VALUES ('999999','无行情标的','sz','main','stock',?)", (NOW,))
    c.execute("INSERT INTO real_trades (date, code, side, price, qty, fee, note,"
              " created_at) VALUES ('2026-09-15','999999','buy',10.0,100,5.0,'实盘',?)",
              (NOW,))
    c.commit()
    c.close()
    code, _, err = run(db, "paper", "step", "--asof", "2026-09-15",
                       "--out", str(tmp_path / "x.md"), capsys=capsys)
    assert code == 2
    assert "收盘价" in err
    c = connect(db)
    assert c.execute("SELECT COUNT(*) n FROM paper_nav_daily").fetchone()["n"] == 0, \
        "取不到价就不许落净值行"
    c.close()


def test_step_records_stale_close_honestly_when_suspended(db, tmp_path, capsys):
    """停牌（当日无 bar）→ 用上一交易日收盘，但 `price_asof` 必须如实指回那天。"""
    c = connect(db)
    c.execute("DELETE FROM bars_daily WHERE code='000333' AND date='2026-09-16'")
    c.commit()
    c.close()
    run(db, "paper", "init", capsys=capsys)
    code, out, err = run(db, "paper", "step", "--asof", "2026-09-16",
                         "--out", str(tmp_path / "x.md"), capsys=capsys)
    assert code == 0, err
    hold = next(a for a in json.loads(out)["accounts"]
                if a["account_id"] == "arm-hold")
    assert hold["marks"]["000333"]["price_asof"] == "2026-09-15"   # 不是 09-16
    assert hold["marks"]["000333"]["price"] == 87.23


def test_json_output_is_stable_sorted(db, tmp_path, capsys):
    """stdout 必须是稳定 JSON（同输入同字节）——P13 之类的下游要靠它。"""
    run(db, "paper", "init", capsys=capsys)
    _, out, _ = run(db, "paper", "step", "--asof", "2026-09-15",
                    "--out", str(tmp_path / "s.md"), capsys=capsys)
    assert out.strip() == json.dumps(json.loads(out), ensure_ascii=False,
                                     sort_keys=True, indent=2).strip()
