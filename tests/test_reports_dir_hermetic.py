"""报告目录的测试期隔离（ERROR_DIARY #51）。

测试**不允许**把报告写进仓库的 `reports/`：那份目录里放的是真跑出来的报告
（预测 / 准确率 / 复盘 / 模拟盘 / 红线归因要用的 sha），而测试用的是 fixture 数据，
写出来的读数是退化值（`方向 100%`、`Brier 0`、`always_flat 100%`）——
两者混在一个目录里，比「没有报告」更糟：它长得像真的。

护栏是 `conftest.report_dir_is_tmp`（autouse）。本文件钉住它**真的生效**，
而不是「写了注释但没人执行」——那正是 #50 的教训。
"""

from __future__ import annotations

import json
from pathlib import Path

from stocklab.cli.main import main
from stocklab.config import paths
from tests.test_experiments_runner import CODE, FIRST_TARGET, _days, _env

PROJECT_ROOT = Path(__file__).resolve().parents[1]
REPO_REPORTS = PROJECT_ROOT / "reports"


def test_report_dir_does_not_point_at_the_repo():
    """autouse 夹具必须已经把 `paths.REPORT_DIR` 挪走。"""
    assert paths.REPORT_DIR != REPO_REPORTS
    assert REPO_REPORTS not in paths.REPORT_DIR.parents


def test_cli_without_report_dir_writes_under_the_redirected_dir(tmp_path, capsys):
    """**不给** `--report-dir` 的命令行（历史上就是这两处漏了）：
    报告落在被重定向的目录里，仓库目录一个字节都不碰。"""
    conn = _env(tmp_path)
    conn.close()
    days = _days()
    rc = main(["experiment", "run", "--variant", "rw-mu0",
               "--from", days[FIRST_TARGET], "--to", days[-1],
               "--db", str(tmp_path / "a.db"), "--code", CODE])
    assert rc == 0
    written = Path(json.loads(capsys.readouterr().out)["report"])
    assert written.exists()
    assert written.parent == paths.REPORT_DIR
    assert REPO_REPORTS not in written.parents


def test_predict_without_report_dir_also_lands_in_tmp(tmp_path, capsys):
    """`predict run` 同一条漏法（`<今天>-predict-2024-07-14.json` 的出处）。"""
    from stocklab.data.models import Bar
    from stocklab.config.universe import Instrument
    from stocklab.store.db import connect
    from stocklab.store.migrate import init_db
    from stocklab.store import repo

    now = "2026-09-15T10:00:00+08:00"
    dates = _days()
    db = tmp_path / "p.db"
    init_db(db)
    conn = connect(db)
    repo.upsert_instruments(conn, [Instrument(CODE, "美的集团", "sz", "main")], now=now)
    repo.insert_bars(conn, [
        Bar(code=CODE, date=d, open=10 + i * 0.01, high=10 + i * 0.01 + 0.1,
            low=10 + i * 0.01 - 0.1, close=10 + i * 0.01, volume=1000,
            amount=1000.0, turnover=1.0, source="test", adj_mode="none")
        for i, d in enumerate(dates)], now=now)
    conn.executemany(
        "INSERT OR IGNORE INTO trading_calendar (date, is_open, source, created_at)"
        " VALUES (?,1,'test',?)", [(d, now) for d in dates])
    conn.close()

    asof = dates[-1]
    assert main(["predict", "run", "--asof", asof, "--db", str(db)]) == 0
    # stdout 是「JSON 载荷 + 人类可读的尾巴」—— raw_decode 只吃前面的对象
    rep, _ = json.JSONDecoder().raw_decode(capsys.readouterr().out)
    written = Path(rep["report"])
    assert written.exists() and written.parent == paths.REPORT_DIR
    assert REPO_REPORTS not in written.parents
