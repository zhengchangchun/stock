"""CLI（Task 16）：解析器 + doctor 报告。

`ingest bars` 是唯一联网的命令，其抓取逻辑在 `stocklab/data/fetch.py`
（已有离线回放测试）；这里只测**参数解析**与**离线可跑的 doctor**。
"""

import json

from stocklab.cli.main import build_parser, cmd_doctor
from stocklab.config import paths
from stocklab.store import repo
from stocklab.store.db import connect
from stocklab.store.migrate import init_db

NOW = "2026-09-14T19:00:00+08:00"


def test_parser_has_subcommands():
    p = build_parser()
    for cmd in ("db", "ingest", "doctor", "fixture"):
        argv = [cmd, "init"] if cmd == "db" else [cmd]
        assert p.parse_args(argv).command == cmd


def test_db_init_args():
    args = build_parser().parse_args(["db", "init"])
    assert args.command == "db"
    assert args.db_command == "init"


def test_ingest_bars_defaults():
    args = build_parser().parse_args(["ingest", "bars"])
    assert args.days == 1200
    assert args.code is None


def test_ingest_bars_accepts_multiple_codes():
    args = build_parser().parse_args(
        ["ingest", "bars", "--days", "300", "--code", "000333", "--code", "600690"])
    assert args.days == 300
    assert args.code == ["000333", "600690"]


def test_fixture_record_requires_name():
    args = build_parser().parse_args(
        ["fixture", "record", "--name", "x", "--key", "param_sz000333"])
    assert args.name == "x"
    assert args.source == "tencent"


def test_doctor_reports_missing_db_without_crashing(tmp_path, monkeypatch, capsys):
    """库不存在时要给人话，而不是抛异常（运维友好）。"""
    monkeypatch.setattr(paths, "DB_PATH", tmp_path / "nope.db")
    assert cmd_doctor() == 2
    out = json.loads(capsys.readouterr().out)
    assert "db init" in out["error"]


def test_doctor_reports_table_counts(tmp_db, monkeypatch, capsys):
    init_db(tmp_db)
    conn = connect(tmp_db)
    repo.upsert_instruments(conn, [], now=NOW)
    repo.insert_bars(conn, [], now=NOW)
    conn.close()
    monkeypatch.setattr(paths, "DB_PATH", tmp_db)
    assert cmd_doctor() == 0
    out = json.loads(capsys.readouterr().out)
    assert out["instruments"] == 0
    assert out["bars_daily"] == 0
    assert out["latest_bar_date"] is None
    assert "open_issues" in out
    assert "last_job" in out


def test_doctor_exposes_open_issues_and_last_job(tmp_db, monkeypatch, capsys):
    init_db(tmp_db)
    conn = connect(tmp_db)
    repo.record_job(conn, "ingest_daily_bars", status="ok", started_at=NOW,
                    detail="2/2 ok")
    conn.close()
    monkeypatch.setattr(paths, "DB_PATH", tmp_db)
    cmd_doctor()
    out = json.loads(capsys.readouterr().out)
    assert out["last_job"]["job_name"] == "ingest_daily_bars"
    assert out["last_job"]["status"] == "ok"


def test_doctor_reports_schema_markers_ok(tmp_db, monkeypatch, capsys):
    init_db(tmp_db)
    monkeypatch.setattr(paths, "DB_PATH", tmp_db)
    assert cmd_doctor() == 0
    out = json.loads(capsys.readouterr().out)
    assert out["schema"]["ok"] is True
    assert out["schema"]["markers"]["p32_predictions_origin"]["present"] is True


def test_doctor_flags_missing_origin_without_migrating(tmp_db, monkeypatch, capsys):
    """doctor 对缺列的旧库**显式告警**，但只读、不擅自迁移（P33）。"""
    init_db(tmp_db)
    conn = connect(tmp_db)
    conn.execute("ALTER TABLE predictions DROP COLUMN origin")
    conn.commit()
    conn.close()
    monkeypatch.setattr(paths, "DB_PATH", tmp_db)
    assert cmd_doctor() == 0
    out = json.loads(capsys.readouterr().out)
    assert out["schema"]["ok"] is False
    assert out["schema"]["markers"]["p32_predictions_origin"]["present"] is False
    # 只报告、不迁移：origin 列仍然缺失
    conn = connect(tmp_db)
    cols = {r[1] for r in conn.execute("PRAGMA table_info(predictions)")}
    conn.close()
    assert "origin" not in cols
