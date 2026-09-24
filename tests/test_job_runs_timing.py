"""`job_runs.finished_at` 必须是**收尾时刻**，不是启动时刻（P67 T3 / A6「文件在说谎」）。

实测（真库 `data/stocklab.db` 只读，2026-09-24）：

```
job_name=ingest_moneyflow  started_at=2026-09-24T15:31:03  finished_at=2026-09-24T15:31:03
```

两个字段**逐字相同**，因为整条命令共用一个启动时刻 `now` 传给 `finish_job`。
后果不是「差了几毫秒」，而是「这步有多慢」这个可观测量**恒为 0**：那一步当天实耗
`duration_s = 269.647`（`reports/ops/latest-close.json`），而 0.2 秒的采集会长得
一模一样 —— 一个常年说谎的读数比没有读数更坏（与 ERROR_DIARY #53 同族）。

修法：收尾时取一个新的时刻（`datetime.now(TZ)`）写 `finished_at`，`started_at` 不动。
这里用**慢的假采集**（人为 sleep）验证差值反映真实耗时。全程离线、不碰真库。

时间戳分辨率是**秒**（`isoformat(timespec="seconds")`），所以 sleep 必须 > 1s
才能让两次读数落到不同的秒上 —— 这是本文件唯一慢的原因。
"""

from __future__ import annotations

import time
from datetime import datetime

import pytest
from zoneinfo import ZoneInfo

from stocklab.cli.main import (build_parser, cmd_ingest_actions, cmd_ingest_bars,
                              cmd_ingest_moneyflow, cmd_ingest_valuation)
from stocklab.config import paths
from stocklab.config.universe import Instrument
from stocklab.data.ingest import ingest_daily_bars
from stocklab.data.models import Bar, MoneyFlowDaily, ValuationDaily
from stocklab.store import repo
from stocklab.store.db import connect
from stocklab.store.migrate import init_db

TZ = ZoneInfo("Asia/Shanghai")
NOW = "2026-09-21T16:00:00+08:00"

#: > 1s：时间戳只到秒，必须跨过一个秒边界才算「两个不同的时刻」。
SLEEP_S = 1.1

#: 秒级**截断**（不是四舍五入）最多吃掉接近 1s ⇒ 观测差值的保证下界是
#: `floor(SLEEP_S - 1) + 1 = 1.0`。差值 ≥ 1s 就证明「两个时刻真的隔了至少一秒」。
MIN_DELTA_S = 1.0

UNIVERSE = (Instrument("000333", "美的集团", "sz", "main"),)


def _bar(code: str) -> Bar:
    return Bar(code=code, date="2026-09-18", open=10.0, high=10.8, low=9.9,
               close=10.5, volume=1000, amount=10_500.0, turnover=1.0, source="test")


@pytest.fixture
def db(tmp_db, tmp_path, monkeypatch):
    """库与 raw_cache 都指到临时目录（不许碰真库/真缓存）。"""
    init_db(tmp_db)
    for attr, value in (("DB_PATH", tmp_db), ("RAW_CACHE_DIR", tmp_path / "raw_cache"),
                        ("DATA_DIR", tmp_path), ("BACKUP_DIR", tmp_path / "backups"),
                        ("REPORT_DIR", tmp_path / "reports")):
        monkeypatch.setattr(paths, attr, value)
    conn = connect(tmp_db)
    repo.upsert_instruments(conn, UNIVERSE, now=NOW)
    yield conn
    conn.close()


def _last_job(conn, job_name: str):
    return conn.execute(
        "SELECT started_at, finished_at FROM job_runs WHERE job_name=?"
        " ORDER BY run_id DESC", (job_name,)).fetchone()


def _assert_real_duration(row, job_name: str) -> None:
    """`finished_at` 晚于 `started_at`，且差值反映真实耗时 —— 不是恒为 0。"""
    assert row is not None, f"{job_name} 没有写 job_runs 行"
    started, finished = row["started_at"], row["finished_at"]
    assert finished > started, (
        f"{job_name}: finished_at({finished}) 不晚于 started_at({started})"
        " —— 时长恒为 0，正是本站要修的读数")
    delta = (datetime.fromisoformat(finished)
             - datetime.fromisoformat(started)).total_seconds()
    assert delta >= MIN_DELTA_S, f"{job_name}: 差值 {delta}s 没反映真实耗时"


# ---------- bars：`stocklab/data/ingest.py` ----------

def test_ingest_bars_cli_finished_at_reflects_the_slow_fetch(db, monkeypatch, capsys):
    """真库实测的现场（`ingest bars` 每天 15:30 跑的那一步）。"""
    import stocklab.data.fetch as fetch_mod

    def slow_bars(client, *, code, start, end, **kw):
        time.sleep(SLEEP_S)
        return [_bar(code[2:])]

    monkeypatch.setattr(fetch_mod, "fetch_daily_bars", slow_bars)
    assert cmd_ingest_bars(build_parser().parse_args(
        ["ingest", "bars", "--days", "30"])) == 0
    capsys.readouterr()
    _assert_real_duration(_last_job(db, "ingest_daily_bars"), "ingest_daily_bars")


def test_ingest_daily_bars_finished_at_is_a_fresh_clock_reading(tmp_db):
    """单元层：`finished_at` **不再是**注入的那个 `now`，而是真跑出来的墙钟。"""
    init_db(tmp_db)
    conn = connect(tmp_db)
    repo.upsert_instruments(conn, UNIVERSE, now=NOW)
    before = datetime.now(TZ).isoformat(timespec="seconds")
    ingest_daily_bars(conn, None, None, UNIVERSE, start="2026-09-14",
                      end="2026-09-18", now=NOW, fetch=lambda code: [_bar(code)])
    after = datetime.now(TZ).isoformat(timespec="seconds")
    row = _last_job(conn, "ingest_daily_bars")
    conn.close()
    assert row["started_at"] == NOW                  # 起点不动（注入的假时刻）
    assert before <= row["finished_at"] <= after    # 终点是真墙钟
    assert row["finished_at"] != NOW


# ---------- actions：`stocklab/cli/main.py` ----------

def test_ingest_actions_cli_finished_at_reflects_the_slow_fetch(db, monkeypatch, capsys):
    import stocklab.data.fetch as fetch_mod

    def slow_actions(client, *, code, start, end, **kw):
        time.sleep(SLEEP_S)
        return []

    monkeypatch.setattr(fetch_mod, "fetch_corp_actions", slow_actions)
    assert cmd_ingest_actions(build_parser().parse_args(
        ["ingest", "actions", "--start", "2025-08-18"])) == 0
    capsys.readouterr()
    _assert_real_duration(_last_job(db, "ingest_actions"), "ingest_actions")


# ---------- valuation / moneyflow：`stocklab/cli/main.py` 的 `_cmd_ingest_series` ----------

def test_ingest_valuation_cli_finished_at_reflects_the_slow_fetch(db, monkeypatch,
                                                                  capsys):
    import stocklab.data.fetch as fetch_mod

    def slow_val(client, *, code, start, end, **kw):
        time.sleep(SLEEP_S)
        return [(ValuationDaily(code=code, date="2026-09-18", source="eastmoney"),
                 "sha", "key")]

    monkeypatch.setattr(fetch_mod, "fetch_valuation_daily", slow_val)
    assert cmd_ingest_valuation(build_parser().parse_args(
        ["ingest", "valuation", "--days", "30"])) == 0
    capsys.readouterr()
    _assert_real_duration(_last_job(db, "ingest_valuation"), "ingest_valuation")


def test_ingest_moneyflow_cli_finished_at_reflects_the_slow_fetch(db, monkeypatch,
                                                                  capsys):
    """真库实测里 `started_at == finished_at` 的就是这一步（269.647s 那次）。"""
    import stocklab.data.fetch as fetch_mod

    def slow_mf(client, *, code, start, end, **kw):
        time.sleep(SLEEP_S)
        return [(MoneyFlowDaily(code=code[2:], date="2026-09-18", source="sina"),
                 "sha", "key")]

    monkeypatch.setattr(fetch_mod, "fetch_money_flow_daily", slow_mf)
    assert cmd_ingest_moneyflow(build_parser().parse_args(
        ["ingest", "moneyflow", "--days", "30"])) == 0
    capsys.readouterr()
    _assert_real_duration(_last_job(db, "ingest_moneyflow"), "ingest_moneyflow")
