"""`ops close` / `ops monthly`（P38）的护栏。

要钉死的主张（每条都对应一个**真出过的事故或会很贵的错**）：

1. **asof = 运行当天**，不是 `max(bars_daily.date)` —— 旧 cron 正文就是在这里错的：
   15:30 时库里最新的一行还是**上一交易日**，于是 backfill/predict/review/paper 全部
   在补昨天，而且幂等所以不报错；
2. **收盘前拒绝执行**（exit 2，一步都不跑）：半截 bar 与当日 LIVE 预测都是 append-only；
3. **非交易日整轮跳过**（exit 0，不是异常）：与 07 §非交易日全部跳过一致；
4. **备份失败就不跑链**：这条链要往库里写一天的数据，没有当天备份时不该继续；
5. **退出码诚实**：跑完再体检一次，链本身与事后体检取较大者；某步 exit 1 不停链、
   某步 ≥2 停链（与巡检共用同一套停止线）；
6. **回执落地**：`reports/ops/latest-close.json` + `job_runs` 一行，退出码与载荷里的
   `ok` 一致。

测试全程不起子进程（`FakeRunner`）、不联网。
"""

from __future__ import annotations

import itertools
import json
import plistlib
from pathlib import Path

from stocklab.cli.main import main
from stocklab.ops import chain, journal, schedule
from stocklab.ops.chain import CLOSE_STEP_ORDER, MONTHLY_STEP_ORDER
from stocklab.ops.patrol import ro_connect
from stocklab.ops.runner import default_runner  # noqa: F401  （形态对照，未直接用）
from stocklab.predict.version import MODEL_VERSION
from stocklab.store.db import connect
from stocklab.store.migrate import init_db
from tests.test_ops_patrol import NOW, FakeRunner, _db, _pred, _sha, _snap

# 2026-09-22 是周二：盘中 10:30 → 收盘后 15:35。
CLOSE_NOW = "2026-09-22T15:35:00+08:00"
TODAY = "2026-09-22"
TOMORROW = "2026-09-23"
CAL = ("2026-09-17", "2026-09-18", "2026-09-21", "2026-09-22")
CODES = ("000333", "510300")


def _green(tmp_path, *, trading_day: bool = True) -> Path:
    """一份**跑完之后应该全绿**的库：把 asof 当天该有的东西全部备齐。

    与把「今天弄坏」相比，这个方向更好测：假执行器不会改库，所以「全绿」正好是
    「链什么都没干也仍然健康」的判据；每个用例只拆自己要测的那一处。
    """
    db = tmp_path / "close.db"
    init_db(db)
    c = connect(db)
    c.executemany(
        "INSERT INTO instruments (code, name, market, board, type, added_at)"
        " VALUES (?,?,'sz','main',?,?)",
        [(code, code, "etf" if code.startswith("5") else "stock", CLOSE_NOW)
         for code in CODES])
    # 「今天休市」的**正规**证据是休市表（`market_holidays`，ADR-013）：
    # `trading_calendar` 只存「已采集到的交易日」（`Calendar.load` 只读 `is_open=1`），
    # 往它塞一行 `is_open=0` 既不是它的语义、也没人会读 —— 那样并没有测出「休市」。
    c.executemany(
        "INSERT INTO trading_calendar (date, is_open, source, created_at)"
        " VALUES (?,1,'tencent',?)", [(d, CLOSE_NOW) for d in CAL])
    if not trading_day:
        c.execute(
            "INSERT INTO market_holidays (date, is_open, source, doc_kind,"
            " covered_year, source_url, published_at, created_at)"
            " VALUES (?,0,'sse','annual',2026,'u',?,?)",
            (TODAY, CLOSE_NOW, CLOSE_NOW))
    # ① 当天的日线：amount 由 backfill 补（这里留 NULL，回填过就算 ok）
    c.executemany(
        "INSERT INTO bars_daily (code, date, open, high, low, close, volume,"
        " amount, adj_mode, source, fetched_at)"
        " VALUES (?,?,10.0,10.0,10.0,10.0,100,NULL,'none','x',?)",
        [(code, TODAY, CLOSE_NOW) for code in CODES])
    # ② 收盘后快照（② 项在收盘后要的就是它）
    for code in CODES:
        _snap(c, TODAY, "20260922150030", code=code)
    # ③ 当日预测（target = 下一个交易日）
    for code in CODES:
        _pred(c, code=code, asof=TODAY, target=TOMORROW)
    # ⑤ review 报告（`report_dir` 由 autouse 夹具指到 tmp_path/reports）
    rd = tmp_path / "reports"
    rd.mkdir(parents=True, exist_ok=True)
    (rd / f"{TODAY}-review.md").write_text("# review\n", encoding="utf-8")
    # ⑥ 模拟盘 + 当日净值
    c.execute(
        "INSERT INTO paper_accounts (account_id, arm, start_date, initial_cash,"
        " initial_positions_json, initial_nav, params_json, created_at)"
        " VALUES ('arm-hold','hold',?,10000.0,'[]',10000.0,'{}',?)",
        (TODAY, CLOSE_NOW))
    c.execute(
        "INSERT INTO paper_nav_daily (account_id, date, cash, positions_json,"
        " market_value, nav, drawdown, cum_cost, cum_return, net_deposits,"
        " created_at) VALUES ('arm-hold',?,10000.0,'[]',10000.0,10000.0,0.0,"
        "0.0,0.0,10000.0,?)", (TODAY, CLOSE_NOW))
    # ⑦ 三张 PIT 表
    c.execute(
        "INSERT INTO valuation_daily (code, date, source, fetched_at, created_at,"
        " resp_sha256) VALUES ('000333',?,'eastmoney',?,'h',?)",
        (TODAY, CLOSE_NOW, CLOSE_NOW))
    c.execute(
        "INSERT INTO money_flow_daily (code, date, source, fetched_at, created_at,"
        " resp_sha256) VALUES ('000333',?,'sina',?,'h',?)",
        (TODAY, CLOSE_NOW, CLOSE_NOW))
    c.execute("INSERT INTO adj_factors (code, date, factor, source, fetched_at)"
              " VALUES ('000333',?,1.0,'tencent',?)", (TODAY, CLOSE_NOW))
    c.commit()
    c.close()
    return db


def _default_db(monkeypatch, db: Path) -> Path:
    """把 `paths.DB_PATH` 指到夹具库 —— 跨库守卫那条线要求「默认库」才放行。"""
    from stocklab.config import paths

    monkeypatch.setattr(paths, "DB_PATH", db)
    return db


# ---------- 主张 1：asof = 运行当天 ----------

def test_asof_is_the_run_day_not_the_latest_bar_in_the_db(tmp_path, monkeypatch):
    """旧 cron 的错：15:30 时 `max(bars_daily.date)` 还是**上一交易日**。

    夹具里给一天**只有昨天有 bar** 的库：asof 仍必须是今天，且 `session/backfill/
    predict/review/paper` 五步全部带 `--date/--asof 今天`。
    """
    db = _green(tmp_path)
    # 把今天的 bar 删掉 → 库里最新一行变成 2026-09-21（正是旧正文会拿去用的那天）
    _default_db(monkeypatch, db)
    c = connect(db)
    c.execute("DELETE FROM bars_daily WHERE date=?", (TODAY,))
    c.commit()
    c.close()

    runner = FakeRunner()
    payload = chain.run_close(db_path=db, now=CLOSE_NOW, runner=runner,
                              report_dir=tmp_path / "reports")

    assert payload["asof"] == TODAY
    by_name = {c["step"]: c["argv"] for c in runner.calls}
    assert by_name["session_backfill_close"][3:] == [
        "session", "backfill-close", "--date", TODAY, "--db", str(db)]
    assert by_name["predict_run"][3:] == ["predict", "run", "--asof", TODAY,
                                          "--db", str(db)]
    assert by_name["review_daily"][3:] == ["review", "daily", "--date", TODAY,
                                           "--db", str(db)]
    assert by_name["paper_step"][3:] == ["paper", "step", "--asof", TODAY,
                                         "--db", str(db)]
    # 今天没有 bar 行 → 如实记一条异常（假执行器不会真的采回来）
    assert "bars_missing_after_ingest" in [a["kind"] for a in payload["anomalies"]]
    assert payload["exit_code"] >= 1


# ---------- 主张 2 / 3：两道前置闸门 ----------

def test_before_close_is_refused_and_runs_nothing(tmp_path, monkeypatch):
    db = _green(tmp_path)
    _default_db(monkeypatch, db)
    runner = FakeRunner()
    payload = chain.run_close(db_path=db, now=NOW, runner=runner,   # 10:30，盘中
                              report_dir=tmp_path / "reports")

    assert payload["exit_code"] == 2 and payload["ok"] is False
    assert runner.calls == [] and payload["steps"] == []
    assert "还没收盘" in payload["refused"]
    assert [a["kind"] for a in payload["anomalies"]] == ["before_close"]
    assert "backup" not in payload            # 连备份都没做（不覆盖当天备份）


def test_non_trading_day_skips_the_whole_round_with_exit_zero(tmp_path, monkeypatch):
    """休市（休市表说今年今天不开市）→ 整轮跳过，**不算异常**（07 §非交易日全部跳过）。"""
    db = _green(tmp_path, trading_day=False)
    _default_db(monkeypatch, db)
    payload = chain.run_close(db_path=db, now=CLOSE_NOW, runner=FakeRunner(),
                              report_dir=tmp_path / "reports")

    assert payload["exit_code"] == 0 and payload["ok"] is True
    assert payload["steps"] == [] and payload["anomalies"] == []
    assert payload["skipped"] and "market_holidays" in payload["skipped"]
    assert payload["session_day"] == {"is_trading_day": False,
                                      "why": "market_holidays"}


def test_weekend_is_skipped_even_before_the_close_check(tmp_path, monkeypatch):
    """周末：判定顺序必须是「先看是不是交易日」——否则会报「还没收盘」。"""
    db = _green(tmp_path)
    _default_db(monkeypatch, db)
    payload = chain.run_close(db_path=db, now="2026-09-26T10:30:00+08:00",  # 周六
                              runner=FakeRunner(), report_dir=tmp_path / "reports")
    assert payload["exit_code"] == 0
    assert payload["session_day"]["why"] == "weekend"
    assert payload["steps"] == []


# ---------- 主张 4：备份 ----------

def test_backup_failure_blocks_the_chain(tmp_path, monkeypatch):
    db = _green(tmp_path)
    _default_db(monkeypatch, db)
    # 备份目录位置摆一个**同名文件** → `mkdir(parents=True)` 必失败
    bad = tmp_path / "backups"
    bad.write_text("not a dir", encoding="utf-8")

    runner = FakeRunner()
    payload = chain.run_close(db_path=db, now=CLOSE_NOW, runner=runner,
                              backup_dir=bad, report_dir=tmp_path / "reports")
    assert payload["exit_code"] == 2
    assert runner.calls == []
    assert payload["anomalies"][0]["kind"] == "backup_failed"


def test_backup_is_taken_before_the_first_step(tmp_path, monkeypatch):
    """备份是第 0 步：它得是**跑之前**那份库的副本（后面每写一行都会改库）。"""
    db = _green(tmp_path)
    _default_db(monkeypatch, db)
    before = _sha(db)                       # 夹具关连接之后的字节
    payload = chain.run_close(db_path=db, now=CLOSE_NOW, runner=FakeRunner(),
                              backup_dir=tmp_path / "backups",
                              report_dir=tmp_path / "reports")
    backup = Path(payload["backup"])
    assert backup.exists() and backup.name.startswith("close.preclose.")
    # 逐字节等于跑之前的那一份。这一条同时钉两件事：是副本（不是空文件），
    # 且在第一步之前 —— 回执要写 `job_runs` 一行，备份若在它之后就再也对不上了。
    assert _sha(backup) == before
    # 副本得是**能读的库**，不是 0 字节 / 半截文件
    c = ro_connect(backup)
    try:
        rows = c.execute("SELECT COUNT(*) FROM bars_daily WHERE date=?",
                         (TODAY,)).fetchone()[0]
    finally:
        c.close()
    assert rows == len(CODES)


# ---------- 主张 5：顺序、停止线与退出码 ----------

def test_steps_run_in_the_declared_order_every_time(tmp_path, monkeypatch):
    db = _green(tmp_path)
    _default_db(monkeypatch, db)
    runner = FakeRunner()
    payload = chain.run_close(db_path=db, now=CLOSE_NOW, runner=runner,
                              report_dir=tmp_path / "reports")
    assert [c["step"] for c in runner.calls] == list(CLOSE_STEP_ORDER)
    assert payload["exit_code"] == 0 and payload["ok"] is True
    assert payload["bars_rows"] == 2
    assert payload["after"]["exit_code"] == 0


def test_ingest_steps_never_get_a_db_flag(tmp_path, monkeypatch):
    """采集类命令不接受 `--db`（固定写默认库）—— 这条在 argv 上就是硬判据。"""
    db = _green(tmp_path)
    _default_db(monkeypatch, db)
    runner = FakeRunner()
    chain.run_close(db_path=db, now=CLOSE_NOW, runner=runner,
                    report_dir=tmp_path / "reports")
    for call in runner.calls:
        takes_db = call["step"] not in (
            "ingest_index", "ingest_bars", "ingest_actions", "ingest_valuation",
            "ingest_moneyflow", "doctor")
        assert ("--db" in call["argv"]) is takes_db, call


def test_step_exit_one_is_soft_and_the_chain_continues(tmp_path, monkeypatch):
    db = _green(tmp_path)
    _default_db(monkeypatch, db)
    runner = FakeRunner({"session_tick": 1})       # 已知噪声：验证冲突
    payload = chain.run_close(db_path=db, now=CLOSE_NOW, runner=runner,
                              report_dir=tmp_path / "reports")
    assert [c["step"] for c in runner.calls] == list(CLOSE_STEP_ORDER)
    assert "aborted" not in payload
    assert payload["exit_code"] == 1
    assert [a["kind"] for a in payload["anomalies"]] == ["step_anomaly"]


def test_step_exit_two_stops_and_blocks(tmp_path, monkeypatch):
    db = _green(tmp_path)
    _default_db(monkeypatch, db)
    runner = FakeRunner({"predict_run": 2})
    payload = chain.run_close(db_path=db, now=CLOSE_NOW, runner=runner,
                              report_dir=tmp_path / "reports")
    assert runner.calls[-1]["step"] == "predict_run"
    assert payload["aborted"]["kind"] == "step_fatal"
    assert payload["exit_code"] == 2


def test_after_check_can_turn_a_clean_chain_red(tmp_path, monkeypatch):
    """链每一步都 0，但数据不健康（今天没有 review 报告）→ 退出码 1。

    这一条是「跑完再体检一次」的存在理由：launchd 只看退出码。
    """
    db = _green(tmp_path)
    _default_db(monkeypatch, db)
    (tmp_path / "reports" / f"{TODAY}-review.md").unlink()
    payload = chain.run_close(db_path=db, now=CLOSE_NOW, runner=FakeRunner(),
                              report_dir=tmp_path / "reports")
    assert all(s["exit_code"] == 0 for s in payload["steps"])
    assert payload["after"]["exit_code"] == 1
    assert payload["exit_code"] == 1
    assert payload["after"]["checks"]["review"]["status"] == "missing"


def test_cross_db_run_is_refused_before_anything_happens(tmp_path, monkeypatch):
    """非默认库 + 计划里有 `ingest *` → 拒绝，**一次子进程都不起**。"""
    db = _green(tmp_path)
    runner = FakeRunner()
    payload = chain.run_close(db_path=db, now=CLOSE_NOW, runner=runner,
                              backup_dir=tmp_path / "backups",
                              report_dir=tmp_path / "reports")
    assert payload["exit_code"] == 2
    assert runner.calls == [] and payload["steps"] == []
    assert "拒绝在非默认库" in payload["refused"]
    assert not (tmp_path / "backups").exists()      # 连备份都没做
    assert [a["kind"] for a in payload["anomalies"]] == ["cross_db_refused"]


def test_budget_exhausted_stops_at_the_first_step(tmp_path, monkeypatch):
    """整轮预算用尽 → 一步都不跑，且**不许报绿**（否则「今天跑过了」就是句假话）。"""
    db = _green(tmp_path)
    _default_db(monkeypatch, db)
    runner = FakeRunner()
    payload = chain.run_close(db_path=db, now=CLOSE_NOW, runner=runner,
                              timeout_s=0, report_dir=tmp_path / "reports")
    assert runner.calls == []
    assert payload["aborted"]["kind"] == "budget_exhausted"
    # 没跑完 = 断链（2）；1 的含义是「跑完了但如实报了异常」
    assert payload["exit_code"] == 2


def test_missing_db_is_blocked_and_writes_no_journal(tmp_path):
    """库不存在时不写回执：那条路连库都没建，回执会先把库文件造出来。"""
    payload = chain.run_close(db_path=tmp_path / "nope.db", now=CLOSE_NOW,
                              report_dir=tmp_path / "reports")
    assert payload["exit_code"] == 2
    assert payload["anomalies"][0]["kind"] == "db_missing"
    assert "journal" not in payload
    assert not (tmp_path / "nope.db").exists()
    assert not (tmp_path / "reports" / "ops" / "latest-close.json").exists()


# ---------- 主张 6：回执 ----------

def test_journal_lands_in_both_places_and_matches_the_exit_code(tmp_path, monkeypatch):
    db = _green(tmp_path)
    _default_db(monkeypatch, db)
    payload = chain.run_close(db_path=db, now=CLOSE_NOW, runner=FakeRunner(),
                              backup_dir=tmp_path / "backups",
                              report_dir=tmp_path / "reports")

    assert payload["journal"]["status"] == "ok"
    report = tmp_path / "reports" / "ops" / "latest-close.json"
    written = json.loads(report.read_text(encoding="utf-8"))
    assert written["asof"] == TODAY and written["exit_code"] == 0
    assert written["steps"] and written["steps"][0]["name"] == "ingest_index"

    c = connect(db)
    try:
        rows = c.execute("SELECT job_name, status, started_at, finished_at, detail"
                         " FROM job_runs WHERE job_name='close'").fetchall()
    finally:
        c.close()
    assert len(rows) == 1
    assert rows[0]["status"] == "ok" and rows[0]["finished_at"] == CLOSE_NOW
    assert rows[0]["detail"].startswith("close: exit=0")
    assert journal.read_latest("close", tmp_path / "reports")["exit_code"] == 0


def test_journal_status_follows_a_failed_run(tmp_path, monkeypatch):
    db = _green(tmp_path)
    _default_db(monkeypatch, db)
    payload = chain.run_close(db_path=db, now=CLOSE_NOW,
                              runner=FakeRunner({"predict_run": 2}),
                              report_dir=tmp_path / "reports")
    assert payload["exit_code"] == 2
    assert payload["journal"]["status"] == "failed"
    c = connect(db)
    try:
        status = c.execute("SELECT status FROM job_runs WHERE job_name='close'"
                           ).fetchone()[0]
    finally:
        c.close()
    assert status == "failed"


def test_summary_line_is_one_line(tmp_path, monkeypatch):
    db = _green(tmp_path)
    _default_db(monkeypatch, db)
    payload = chain.run_close(db_path=db, now=CLOSE_NOW, runner=FakeRunner(),
                              report_dir=tmp_path / "reports")
    line = chain.summary_line(payload)
    assert "\n" not in line
    assert line.startswith("close: exit=0") and "steps=12/12" in line


# ---------- CLI ----------

def test_cli_close_prints_json_and_returns_the_exit_code(tmp_path, monkeypatch, capsys):
    db = _green(tmp_path)
    _default_db(monkeypatch, db)
    # CLI 没有 `--runner` 注入点，但 `chain.run_close` 是**运行时**读 `default_runner`
    # 这个模块全局 —— 不换掉它，这条用例会真起子进程跑 `ingest *`，而采集类命令
    # 不接受 `--db`（固定写 `paths.DB_PATH` = **真库**）：2026-09-22 实测就是这么把
    # 当天 12:06 的半截 bar 写进真库的（修法见 ERROR_DIARY #55）。
    monkeypatch.setattr(chain, "default_runner", FakeRunner())
    code = main(["ops", "close", "--now", CLOSE_NOW, "--db", str(db),
                 "--report-dir", str(tmp_path / "reports"),
                 "--backup-dir", str(tmp_path / "backups")])
    out = capsys.readouterr()
    payload = json.loads(out.out)
    assert code == payload["exit_code"] == 0
    assert "close: exit=0" in out.err


def test_cli_close_refuses_before_close(tmp_path, monkeypatch, capsys):
    db = _green(tmp_path)
    _default_db(monkeypatch, db)
    code = main(["ops", "close", "--now", NOW, "--db", str(db),
                 "--report-dir", str(tmp_path / "reports")])
    out = capsys.readouterr()
    assert code == 2
    assert "还没收盘" in out.err


# ---------- 月度刷新 ----------

def test_monthly_runs_its_four_steps_on_any_day(tmp_path, monkeypatch):
    """月度刷新没有交易日闸门（它是维护活，全部幂等）——**开盘前**跑也必须照跑。

    `now` 取**次日 08:00**（与 plist 上真实的「每月 1 日 08:00」同形态）：那时最新已
    收盘交易日仍是库里最全的那一天，事后体检才可能全绿。若取当天盘中 10:30，比的是
    **上一个**交易日，而本夹具只备了今天的数据 —— 那是夹具的形态，不是链的错。
    """
    db = _green(tmp_path)
    _default_db(monkeypatch, db)
    runner = FakeRunner()
    payload = chain.run_monthly(db_path=db, now="2026-09-23T08:00:00+08:00",
                                runner=runner, report_dir=tmp_path / "reports")
    assert payload["job"] == "monthly"
    assert [c["step"] for c in runner.calls] == list(MONTHLY_STEP_ORDER)
    assert payload["exit_code"] == 0
    assert payload["journal"]["status"] == "ok"


def test_monthly_long_window_is_the_only_12000_day_run(tmp_path, monkeypatch):
    db = _green(tmp_path)
    _default_db(monkeypatch, db)
    runner = FakeRunner()
    chain.run_monthly(db_path=db, now=NOW, runner=runner,
                      report_dir=tmp_path / "reports")
    argv = {c["step"]: c["argv"] for c in runner.calls}
    assert argv["ingest_bars_long"][3:] == ["ingest", "bars", "--days", "12000"]
    assert "--db" not in argv["ingest_bars_long"]

    chain.run_close(db_path=db, now=CLOSE_NOW,
                    runner=(long_runner := FakeRunner()),
                    report_dir=tmp_path / "reports")
    by_name = {c["step"]: c["argv"] for c in long_runner.calls}
    assert by_name["ingest_bars"][3:] == ["ingest", "bars", "--days", "30"]


def test_monthly_refuses_a_non_default_db_too(tmp_path):
    db = _green(tmp_path)
    payload = chain.run_monthly(db_path=db, now=NOW, runner=FakeRunner(),
                                report_dir=tmp_path / "reports")
    assert payload["exit_code"] == 2
    assert "拒绝在非默认库" in payload["refused"]


# ---------- 调度（plist） ----------

def test_plist_is_generated_for_the_three_jobs(tmp_path):
    for name in schedule.JOB_NAMES:
        path = schedule.write_plist(schedule.JOB_BY_NAME[name],
                                    plist_dir=tmp_path,
                                    logs=tmp_path / "logs")
        assert path.exists()
        text = path.read_text(encoding="utf-8")
        assert "<key>StartCalendarInterval</key>" in text
        assert "<key>RunAtLoad</key>" in text
        # S4（P43）：注释说「显式写 False」就必须真的写下来 —— `render_plist` 的注释
        # 曾经说「不设 RunAtLoad」，而下一行就是 `"RunAtLoad": False`：语义（不自动跑）
        # 是对的，注释在说谎。谁为了「加上 RunAtLoad」来改这里，会以为自己动了行为。
        assert plistlib.loads(path.read_bytes())["RunAtLoad"] is False


def _generated_calendars(tmp_path: Path) -> dict[str, tuple[dict[str, int], ...]]:
    """三条任务各生成一份 plist 到**注入的 tmp 目录**，回读它的 `StartCalendarInterval`。

    断言的对象是**生成器写出来的文件**，不是内存里的 `Job.calendar`；而目录一律注入
    （`--plist-dir` 的等价物），绝不碰 `~/Library/LaunchAgents` —— 那是运行态，
    照 ERROR_DIARY #56：直读真实目录会让用例随本机状态突变。
    """
    out: dict[str, tuple[dict[str, int], ...]] = {}
    for spec in schedule.JOBS:
        path = schedule.write_plist(spec, plist_dir=tmp_path, logs=tmp_path / "logs")
        doc = plistlib.loads(path.read_bytes())
        out[spec.name] = tuple(doc["StartCalendarInterval"])
    return out


def _trigger_keys(cal: tuple[dict[str, int], ...]) -> set[tuple[int, int, int]]:
    """一组 plist 触发点 → 它实际会命中的 `(Weekday, Hour, Minute)` 全集。

    launchd 里**缺的键 = 通配**：没有 `Weekday`（`monthly` 就是这种形状）就是每天
    都命中，所以展开成 7 天。缺 `Day` 同理忽略 —— 这是**保守**的展开（宁可多算），
    于是「交集为空」是个硬结论；反之若报撞点，就值得人来看一眼。
    """
    out: set[tuple[int, int, int]] = set()
    for e in cal:
        hour, minute = e.get("Hour", 0), e.get("Minute", 0)
        weekdays = (e["Weekday"],) if "Weekday" in e else range(7)
        out |= {(d, hour, minute) for d in weekdays}
    return out


def test_patrol_plist_covers_0900_to_1430_and_no_slot_reaches_the_close(tmp_path):
    """T1：`patrol` 的 **60 条**触发点 = 工作日 09:00–14:30 每 30 分钟。

    两条边界各自钉一件事：

    - **不含 15:30**：那一槽与 `close` 撞在同一分钟（P42 §0），`patrol --fix` 会真起
      子进程补步，与收盘链并发写同一个库 → `database is locked` → 收盘链断链；
    - **不含 15:00**（P46 §T1）：15:00 那一刻 `is_trade_date_closed(今天, now)` 已经为真
      ⇒ `latest_closed_session` 变成**今天** ⇒ patrol 会补 `predict run --asof 今天`，
      而那时当天的 K 线还是**盘中值**（`ingest bars` 是 15:30 收盘链的事）。实测
      2026-09-22 15:00 槽就这么写出了 17 条基于半截 bar 的 LIVE 预测，15:30 收盘链
      重算后全部撞 append-only → 收盘链 exit 1。所以「任一槽严格早于 15:00」不是
      排版偏好，而是这条缺陷的结构性断言。
    """
    cal = _generated_calendars(tmp_path)["patrol"]
    assert len(cal) == 60
    assert len(schedule.patrol_calendar()) == 60          # 生成器与 plist 同源
    times = {(e["Hour"], e["Minute"]) for e in cal}
    assert times == {(h, m) for h in range(9, 15) for m in (0, 30)}
    assert (15, 30) not in times
    # T1 的断言本体：patrol **永远不碰收盘时刻** —— 任一槽 (H, M) 严格早于 (15, 0)。
    assert all(t < (15, 0) for t in times), sorted(t for t in times if t >= (15, 0))
    assert all(e["Hour"] < 15 for e in cal)
    assert {e["Weekday"] for e in cal} == {1, 2, 3, 4, 5}


def test_close_plist_still_fires_weekdays_at_1530(tmp_path):
    """T2：`close` 仍是工作日 15:30 —— 错峰靠挪 patrol，不动收盘链时点。

    S5（P43）：窗口**文案**必须是 plist **实际**说的那件事。`StartCalendarInterval`
    只能表达「星期几 15:30」，表达不了「只在交易日」—— 节假日（如 10-01）照样会被拉起，
    然后由链自己判 `is_trading_day=False` **整轮跳过**（exit 0，不是异常）。
    原来文案写「每交易日 15:30」，会让读页面的人以为节假日不会触发；
    跳过行为本身由 `test_non_trading_day_skips_the_whole_round_with_exit_zero` 钉住。
    """
    cal = _generated_calendars(tmp_path)["close"]
    assert {(e["Hour"], e["Minute"]) for e in cal} == {(15, 30)}
    assert {e["Weekday"] for e in cal} == {1, 2, 3, 4, 5}
    assert schedule.JOB_BY_NAME["close"].window == "工作日 15:30（非交易日整轮跳过）"


def test_no_two_jobs_fire_at_the_same_moment(tmp_path):
    """通用护栏：任意两个 job 的触发时刻集合无交集。

    将来再加任务时撞点会**当场红**，而不是等收盘那天两个写者把链撞断（P42 §0）。
    """
    by_job = {name: _trigger_keys(cal)
              for name, cal in _generated_calendars(tmp_path).items()}
    for a, b in itertools.combinations(sorted(by_job), 2):
        shared = by_job[a] & by_job[b]
        assert not shared, f"{a} 与 {b} 撞点：{sorted(shared)}"


def test_undated_jobs_have_a_single_trigger():
    close = schedule.JOB_BY_NAME["close"].calendar
    monthly = schedule.JOB_BY_NAME["monthly"].calendar
    assert len(close) == 5 and all(e["Hour"] == 15 and e["Minute"] == 30
                                   for e in close)
    assert monthly == ({"Day": 1, "Hour": 8, "Minute": 0},)


def test_cli_schedule_generate_is_read_only(tmp_path, capsys):
    code = main(["ops", "schedule", "generate", "--job", "close",
                 "--plist-dir", str(tmp_path), "--log-dir", str(tmp_path / "logs")])
    out = capsys.readouterr()
    payload = json.loads(out.out)
    assert code == 0
    assert payload[0]["triggers"] == 5
    assert "<key>WorkingDirectory</key>" in payload[0]["plist"]
    # `generate` 一个文件都不写：既没有 plist、也没有日志目录。
    # （判据是「没有文件」而不是「目录为空」：`main()` 会给所有不带 `--db` 的命令
    #   建一次运行时目录，`reports/` 就是这么来的 —— 那是 CLI 的公共前置，
    #   与 `generate` 无关。）
    assert [p for p in tmp_path.rglob("*") if p.is_file()] == []
    assert not (tmp_path / "logs").exists()


def test_cli_schedule_rejects_an_unknown_job(capsys):
    code = main(["ops", "schedule", "status", "--job", "nope"])
    out = capsys.readouterr()
    assert code == 2 and "未知任务" in out.err
