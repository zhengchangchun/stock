"""`features build` 子命令（Task 20）：离线只读 bars_daily，可重复运行。

覆盖：参数解析 / 落库 / 幂等 / 历史不足留痕 / code 过滤 / 行情被修订后的冲突上报。
"""

import json

import pytest

from stocklab.cli.main import _cmd_features_build, build_parser, cmd_features_build
from stocklab.config import paths
from stocklab.config.universe import DEFAULT_UNIVERSE
from stocklab.data.models import Bar
from stocklab.store import repo
from stocklab.store.db import connect
from stocklab.store.migrate import init_db

NOW = "2026-09-14T20:00:00+08:00"
ASOF = "2026-03-20"


def _bars(code="000333", n=80, base=10.0):
    out = []
    for i in range(n):
        c = base + i * 0.1
        out.append(Bar(code=code, date=f"2026-{(i // 28) + 1:02d}-{(i % 28) + 1:02d}",
                       open=c - 0.05, high=c + 0.15, low=c - 0.15, close=c,
                       volume=1000 + i, amount=(1000 + i) * c, turnover=1.0,
                       source="test"))
    return out


def _seed(tmp_db, codes=("000333",), n=80):
    """只登记有行情的标的 —— active 但无 K 线会额外触发 skipped，干扰断言。"""
    init_db(tmp_db)
    conn = connect(tmp_db)
    repo.upsert_instruments(
        conn, tuple(i for i in DEFAULT_UNIVERSE if i.code in codes), now=NOW)
    for code in codes:
        repo.insert_bars(conn, _bars(code, n), now=NOW)
    conn.close()


# ---------- 参数解析 ----------

def test_features_build_subcommand():
    args = build_parser().parse_args(["features", "build", "--date", "2026-09-14"])
    assert args.command == "features"
    assert args.date == "2026-09-14"
    assert args.code is None
    assert args.func is _cmd_features_build


def test_features_build_requires_date():
    with pytest.raises(SystemExit):
        build_parser().parse_args(["features", "build"])


def test_features_build_accepts_multiple_codes():
    args = build_parser().parse_args(
        ["features", "build", "--date", "2026-09-14", "--code", "000333",
         "--code", "600690"])
    assert args.code == ["000333", "600690"]


def test_features_build_rejects_unknown_action():
    with pytest.raises(SystemExit):
        build_parser().parse_args(["features", "backfill"])


# ---------- 落库与幂等 ----------

def test_features_build_writes_snapshot(tmp_db, monkeypatch, capsys):
    _seed(tmp_db)
    monkeypatch.setattr(paths, "DB_PATH", tmp_db)
    assert cmd_features_build(ASOF, None) == 0
    out = json.loads(capsys.readouterr().out)
    assert out == {"date": ASOF, "written": 1, "identical": 0, "conflicts": [],
                   "skipped": []}

    conn = connect(tmp_db)
    row = conn.execute("SELECT code, date, feature_version, payload_hash,"
                       " data_version FROM features_daily").fetchone()
    conn.close()
    assert row["code"] == "000333"
    assert row["date"] == ASOF
    assert row["feature_version"] == "v1"
    assert row["data_version"] == f"bars:{ASOF}"
    assert len(row["payload_hash"]) == 64


def test_features_build_is_idempotent(tmp_db, monkeypatch, capsys):
    """同一份数据重复运行：不得重复写入，且必须报告 identical（可安全重试）。"""
    _seed(tmp_db)
    monkeypatch.setattr(paths, "DB_PATH", tmp_db)
    assert cmd_features_build(ASOF, None) == 0
    first = json.loads(capsys.readouterr().out)
    assert cmd_features_build(ASOF, None) == 0
    second = json.loads(capsys.readouterr().out)
    assert first["written"] == 1 and second["written"] == 0
    assert second["identical"] == 1

    conn = connect(tmp_db)
    assert conn.execute("SELECT COUNT(*) FROM features_daily").fetchone()[0] == 1
    conn.close()


def _revise_last_bar_on(tmp_db, date, delta=5.0):
    """把 `date` 当日的收盘价改掉（模拟源站修订历史行情）。"""
    bars = _bars("000333", 80)
    revised = [b for b in bars if b.date == date]
    assert revised, f"{date} 不在造数范围内"
    old = revised[0]
    conn = connect(tmp_db)
    repo.insert_bars(conn, [Bar(code=old.code, date=old.date, open=old.open,
                                high=old.high + delta, low=old.low,
                                close=old.close + delta, volume=old.volume,
                                amount=old.amount, turnover=old.turnover,
                                source=old.source)], now=NOW)
    conn.close()


def test_features_build_reports_conflict_when_asof_bars_revised(tmp_db, monkeypatch,
                                                                capsys):
    """asof 当日行情被修订 → 同键重算结果不同 → 必须显式上报，而不是静默覆盖。"""
    _seed(tmp_db)
    monkeypatch.setattr(paths, "DB_PATH", tmp_db)
    cmd_features_build(ASOF, None)
    capsys.readouterr()

    _revise_last_bar_on(tmp_db, ASOF)

    assert cmd_features_build(ASOF, None) == 1
    out = json.loads(capsys.readouterr().out)
    assert out["conflicts"] == ["000333"]
    assert out["written"] == 0

    conn = connect(tmp_db)
    assert conn.execute("SELECT COUNT(*) FROM features_daily").fetchone()[0] == 1
    events = [r["message"] for r in conn.execute(
        "SELECT message FROM system_events WHERE level='warn'")]
    conn.close()
    assert any("不一致" in m for m in events)


def test_features_build_ignores_revision_of_future_bars(tmp_db, monkeypatch,
                                                        capsys):
    """端到端 PIT：修订 asof **之后**的行情，当日快照必须一模一样（identical）。"""
    _seed(tmp_db)
    monkeypatch.setattr(paths, "DB_PATH", tmp_db)
    cmd_features_build(ASOF, None)
    capsys.readouterr()

    _revise_last_bar_on(tmp_db, "2026-03-24")   # asof 之后

    assert cmd_features_build(ASOF, None) == 0
    out = json.loads(capsys.readouterr().out)
    assert out == {"date": ASOF, "written": 0, "identical": 1, "conflicts": [],
                   "skipped": []}


# ---------- 缺失与过滤 ----------

def test_features_build_skips_insufficient_history(tmp_db, monkeypatch, capsys):
    """历史不足 → 记 skipped + warn 事件，不写假快照。"""
    _seed(tmp_db, n=10)
    monkeypatch.setattr(paths, "DB_PATH", tmp_db)
    assert cmd_features_build("2026-01-05", None) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["skipped"] == ["000333"]
    assert out["written"] == 0

    conn = connect(tmp_db)
    assert conn.execute("SELECT COUNT(*) FROM features_daily").fetchone()[0] == 0
    events = list(conn.execute("SELECT message, context_json FROM system_events"
                               " WHERE module='features' AND level='warn'"))
    conn.close()
    assert len(events) == 1
    assert json.loads(events[0]["context_json"])["last_bar"] == "2026-01-05"


def test_features_build_skips_non_trading_asof_date(tmp_db, monkeypatch, capsys):
    """asof 当日无 K 线（周末/停牌）→ skipped，禁止拿前一日的价格贴当天标签。"""
    _seed(tmp_db)
    monkeypatch.setattr(paths, "DB_PATH", tmp_db)
    sum_ = _bars("000333", 80)
    assert all(b.date != "2026-03-25" for b in sum_)
    cmd_features_build("2026-03-25", None)
    out = json.loads(capsys.readouterr().out)
    assert out["skipped"] == ["000333"]


def test_features_build_respects_code_filter(tmp_db, monkeypatch, capsys):
    _seed(tmp_db, codes=("000333", "600690"))
    monkeypatch.setattr(paths, "DB_PATH", tmp_db)
    assert cmd_features_build(ASOF, ["600690"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["written"] == 1

    conn = connect(tmp_db)
    codes = [r["code"] for r in conn.execute(
        "SELECT code FROM features_daily ORDER BY code")]
    conn.close()
    assert codes == ["600690"]


def test_features_build_missing_db_returns_2(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(paths, "DB_PATH", tmp_path / "nope.db")
    assert cmd_features_build(ASOF, None) == 2
    assert "db init" in json.loads(capsys.readouterr().out)["error"]
