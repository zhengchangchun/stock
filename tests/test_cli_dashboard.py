"""P14：CLI —— `dashboard build` / `dashboard serve`。

`serve` 的**唯一**不可协商行为：非回环 host 报错退出（退出码 2），且**不 bind**。
本文件里那条测试就是这条红线的机器版本。
"""

from __future__ import annotations

import json
import re
from datetime import date, timedelta

import pytest

from stocklab.cli.main import main
from stocklab.data.models import Bar
from tests.test_predict_service import seed

CODE = "000333"
START = "2026-05-02"
NOW = "2026-09-15T16:00:00+08:00"
LEDGER_NOW = "2026-09-15T14:23:31+08:00"


def _bars(n=124):
    d0 = date.fromisoformat(START)
    return [Bar(code=CODE, date=(d0 + timedelta(days=i)).isoformat(),
                open=10.0 * (1 + 0.002 * i), high=10.2 * (1 + 0.002 * i),
                low=9.8 * (1 + 0.002 * i), close=10.0 * (1 + 0.002 * i),
                volume=1000, amount=None, turnover=None, source="test")
            for i in range(n)]


@pytest.fixture
def db(tmp_path):
    path = tmp_path / "dash.db"
    conn = seed(path, {CODE: _bars()})
    conn.execute("INSERT INTO cash_flows (date, kind, amount, note, created_at)"
                 " VALUES ('2026-09-14','deposit',20000.0,'本金',?)", (LEDGER_NOW,))
    conn.execute("INSERT INTO real_trades (date, code, side, price, qty, fee, note,"
                 " created_at) VALUES ('2026-09-14',?,'buy',86.80,100,5.09,'首笔',?)",
                 (CODE, LEDGER_NOW))
    conn.close()
    return path


def run(*argv, capsys):
    code = main(list(argv))
    out, err = capsys.readouterr()
    return code, out, err


# ---------- build ----------

def test_build_writes_a_single_file_html(db, tmp_path, capsys):
    out_path = tmp_path / "site" / "dashboard.html"
    code, out, err = run("dashboard", "build", "--out", str(out_path),
                         "--asof", "2026-09-14", "--db", str(db), "--now", NOW,
                         capsys=capsys)
    assert code == 0, err
    assert out_path.exists()
    doc = out_path.read_text(encoding="utf-8")
    assert "http://" not in doc and "https://" not in doc
    assert not re.search(r"<script|<link|\bsrc\s*=", doc, re.I)
    assert bytes_len(doc) == out_path.stat().st_size
    assert f"{bytes_len(doc):,}" in out
    assert "字节数" in out


def bytes_len(doc: str) -> int:
    return len(doc.encode("utf-8"))


def test_build_json_reports_out_bytes_sha256(db, tmp_path, capsys):
    out_path = tmp_path / "d.html"
    code, out, err = run("dashboard", "build", "--out", str(out_path),
                         "--asof", "2026-09-14", "--db", str(db), "--now", NOW,
                         "--json", capsys=capsys)
    assert code == 0, err
    payload = json.loads(out)
    assert payload["out"] == str(out_path)
    assert payload["bytes"] == out_path.stat().st_size
    assert len(payload["sha256"]) == 64
    assert payload["asof"] == "2026-09-14"


def test_build_fails_cleanly_without_a_db(tmp_path, capsys):
    code, out, err = run("dashboard", "build", "--out", str(tmp_path / "x.html"),
                         "--db", str(tmp_path / "nope.db"), capsys=capsys)
    assert code == 2
    assert "db not found" in err


# ---------- serve：只能绑回环 ----------

@pytest.mark.parametrize("host", ["0.0.0.0", "192.168.1.7", "::"])
def test_serve_refuses_non_loopback_host(db, host, capsys):
    """`--host 0.0.0.0` 必须**报错退出 2**，不是警告、不是静默改成回环。"""
    code, out, err = run("dashboard", "serve", "--host", host, "--port", "0",
                         "--db", str(db), capsys=capsys)
    assert code == 2
    assert "NonLoopbackHost" in err
    assert "回环" in err
    assert out == ""            # 什么都没起：连「已启动」都不该打印


def test_serve_default_host_is_loopback_and_prints_url(db, monkeypatch, capsys):
    """默认参数下必须绑到 127.0.0.1，并把**实际端口**报出来。"""
    from stocklab.dashboard import server as dash

    seen = {}

    def _fake_serve(httpd):
        seen["host"] = httpd.server_address[0]
        seen["port"] = httpd.server_address[1]
        httpd.server_close()

    monkeypatch.setattr(dash, "serve_forever", _fake_serve)
    code, out, err = run("dashboard", "serve", "--port", "0", "--db", str(db),
                         "--asof", "2026-09-14", capsys=capsys)
    assert code == 0, err
    assert seen["host"] == "127.0.0.1"
    assert seen["port"] > 0
    assert f"127.0.0.1:{seen['port']}" in out
    assert f"/lab/" in out and "/health" in out and "/api/summary" in out
    assert "只读" in out


def test_serve_validates_the_db_before_binding(db, capsys):
    """库不存在 → 退出 2；不占端口、不进入 serve_forever。"""
    code, out, err = run("dashboard", "serve", "--port", "0",
                         "--db", str(db.parent / "missing.db"), capsys=capsys)
    assert code == 2
    assert "db not found" in err
    assert "已启动" not in out
