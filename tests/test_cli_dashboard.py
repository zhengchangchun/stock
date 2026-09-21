"""P14：CLI —— `dashboard build`（`serve` 已于 2026-09-21 合并下线）。

看板现在只剩**离线单文件产物**；网页服务统一走 `lab serve`（本机唯一的服务）。
本文件保留 `build` 的全部测试，并用一条用例钉住「第二个服务不会回来」。
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


# ---------- serve 已下线（2026-09-21 服务合并） ----------

def test_dashboard_serve_is_gone(capsys):
    """第二个网页服务已合并进 `lab serve` —— 子命令**不存在**。

    为什么钉的是「不存在」而不是「报错信息好看」：缺陷不是「serve 参数不好」,
    而是**两个默认端口都是 8791 的服务** —— 本机此刻跑着哪一个只能靠记忆回答，
    而这两个一个只读、一个带写路径。只要 `serve` 还能跑，这个缺陷就还在。
    """
    code, out, err = run("dashboard", "serve", "--port", "0", capsys=capsys)
    assert code == 2
    assert "invalid choice: 'serve'" in err
    assert "(choose from build)" in err      # 只剩 build，没有别的服务入口
    assert out == ""                    # 什么都没起，也没打印「已启动」


def test_lab_serve_is_the_only_web_entry(db, capsys):
    """`lab serve` 是唯一的网页入口，且它仍然存在（守回环、报端口）。"""
    seen = {}

    def _fake_serve(httpd):
        seen["host"] = httpd.server_address[0]
        seen["port"] = httpd.server_address[1]
        httpd.server_close()

    import stocklab.labweb.app as labweb
    original = labweb.serve_forever
    labweb.serve_forever = _fake_serve
    try:
        code, out, err = run("lab", "serve", "--port", "0", "--db", str(db),
                             capsys=capsys)
    finally:
        labweb.serve_forever = original
    assert code == 0, err
    assert seen["host"] == "127.0.0.1" and seen["port"] > 0
    assert f"127.0.0.1:{seen['port']}/lab/" in out
