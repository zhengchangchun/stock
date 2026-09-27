"""`ops weekly`（B3）/ `ops quarterly`（B4）的护栏 —— P89。

要钉死的主张（每条都对应一个**真出过的事故或会很贵的错**）：

1. **asof = 最近已收盘交易日**，不是运行当天：周/季度链是**读**当天数据做汇总，
   运行当天可能休市（10-05 恰好是周一、国庆休市）。取法复用
   `verify.pending.latest_closed_session`（与 `patrol.check_db` **同一份判定**）；
2. **asof 取不到 ⇒ 整轮拒绝**（exit 2、`steps=[]`）：不许猜一个日期跑（D2 的 fail-closed）；
3. **两条链都不做非交易日跳过**（与 `ops monthly` 同构）：休市周跑出来的是与上周
   同样的读数，无害、可复核；跳过反而会「连续两周没扫」；
4. **汇总报告是渲染、不是重算**：只把各步的 stdout 尾部 + `m2 report` 的 JSON
   汇总成 `<报告根>/<kind>/<asof>-<kind>.md`，每个数字都带「数据出处」；
5. **调度不撞点**：`weekly` 周一 16:30、`quarterly` 5/1+9/1+11/1 **07:00**
   （D4 原写 09:00，但它与 `patrol` 的 09:00 槽真撞点 —— 见 §7 偏离表第 2 条），
   与既有三条**无同时刻槽**（`test_ops_close.py` 的通用护栏自动覆盖 5 条）。

测试全程不起子进程（`ScriptedRunner`）、不联网、不碰真库。
"""

from __future__ import annotations

import itertools
import json
import plistlib
from pathlib import Path

from stocklab.cli.main import main
from stocklab.ops import chain, journal, schedule, window_report
from stocklab.ops.runner import EXIT_ANOMALY, EXIT_BLOCKED, EXIT_OK
from stocklab.store.db import connect
from stocklab.store.migrate import init_db
from tests.test_ops_close import _green

#: `_green` 造出来的那份库里，`latest_closed_session` 就是它的 `TODAY`。
GREEN_ASOF = "2026-09-22"
#: 运行的**当天**刻意选一个休市日（2026-10-05 周一、国庆）—— asof 必须还是 9-22。
GREEN_NOW = "2026-10-05T16:30:00+08:00"
#: quarterly 的 11 月槽（2026-11-01 周日；**必须晚于夹具里最后一个交易日** 9-22，
#: 否则 asof 会（正确地）判不出来 —— 那测的就不是「取最近已收盘交易日」了）。
QUARTERLY_NOW = "2026-11-01T07:00:00+08:00"

#: `m2 report` 的读数形态（**实测**）：长文本报告走 stdout，而那一行紧凑 JSON 是
#: `print(..., file=sys.stderr)` —— 在 **stderr 末尾**（`cmd_m2_report`）。
M2_STDOUT = "模块2 三方对标（正文，节选）……"
M2_STDERR = json.dumps(
    {"asof": GREEN_ASOF, "n_sessions": 121, "threshold": 60,
     "gate_status": "ok", "n_sides": 2,
     "forecast": {"n_scored": 21, "n_unscored": 1},
     "cases": {"n_miss_total": 4, "n_cases": 2}},
    ensure_ascii=False, sort_keys=True)


class ScriptedRunner:
    """假执行器：记下每次调用，按脚本返回退出码与 stdout/stderr 尾部（**不起子进程**）。"""

    def __init__(self, codes=None, *, stdout=None, stderr=None, on_call=None):
        self.calls: list[dict] = []
        self.codes = dict(codes or {})
        self.stdout = dict(stdout or {})
        self.stderr = dict(stderr or {})
        self.on_call = on_call

    def __call__(self, step, argv, timeout):
        self.calls.append({"step": step.name, "argv": list(argv)})
        if self.on_call:
            self.on_call(step, argv)
        code = self.codes.get(step.name, 0)
        return {"exit_code": code, "timeout": False, "duration_s": 0.01,
                "stdout_tail": self.stdout.get(step.name, f"{step.name} ok"),
                "stderr_tail": self.stderr.get(step.name, "")}


def _scripted(**kw) -> ScriptedRunner:
    kw.setdefault("stdout", {"m2_report": M2_STDOUT})
    kw.setdefault("stderr", {"m2_report": M2_STDERR})
    return ScriptedRunner(**kw)


def _empty_db(tmp_path, name="empty.db") -> Path:
    """**空库副本**：schema 齐全、一行数据都没有（G7 的 fail-closed 输入）。"""
    db = tmp_path / name
    init_db(db)
    return db


def _default_db(monkeypatch, db: Path) -> Path:
    """把 `paths.DB_PATH` 指到夹具库 —— 跨库守卫那条线要求「默认库」才放行。

    与 `test_ops_close.py` 同一条理由：`ingest financials` **不接受** `--db`
    （固定写 `paths.DB_PATH`），所以「跑在副本上」就只能靠把默认库指过去。
    """
    from stocklab.config import paths

    monkeypatch.setattr(paths, "DB_PATH", db)
    return db


def _weekly(tmp_path, monkeypatch, *, db=None, now=GREEN_NOW, runner=None,
            report_dir=None):
    db = db or _green(tmp_path)
    _default_db(monkeypatch, db)
    rd = report_dir or tmp_path / "reports"
    return chain.run_weekly(db_path=db, now=now, runner=runner or _scripted(),
                            report_dir=rd), rd


def _quarterly(tmp_path, monkeypatch, *, db=None, now=QUARTERLY_NOW, runner=None,
               report_dir=None):
    db = db or _green(tmp_path)
    _default_db(monkeypatch, db)
    rd = report_dir or tmp_path / "reports"
    return chain.run_quarterly(db_path=db, now=now, runner=runner or _scripted(),
                               report_dir=rd), rd


# ---------- 主张 1：asof = 最近已收盘交易日 ----------

def test_weekly_asof_is_the_last_closed_session_not_the_run_day(tmp_path, monkeypatch):
    """10-05（周一、休市）跑，asof 必须是 9-22 —— 不许把运行当天当 asof。"""
    payload, _ = _weekly(tmp_path, monkeypatch)
    assert payload["asof"] == GREEN_ASOF
    assert payload["asof"] != "2026-10-05"
    ev = payload["asof_evidence"]
    assert ev["bars_max_date"] == GREEN_ASOF
    assert ev["calendar_side"] == GREEN_ASOF


def test_weekly_does_not_skip_a_non_trading_day(tmp_path, monkeypatch):
    """D3：休市周**照跑**（跑出来的是与上周同样的读数），不是整轮跳过。"""
    payload, _ = _weekly(tmp_path, monkeypatch)
    assert payload["exit_code"] == EXIT_OK
    assert "skipped" not in payload
    assert [s["name"] for s in payload["steps"]] == list(chain.WEEKLY_STEP_ORDER)


def test_quarterly_asof_is_the_last_closed_session(tmp_path, monkeypatch):
    payload, _ = _quarterly(tmp_path, monkeypatch)
    assert payload["asof"] == GREEN_ASOF


# ---------- 主张 2：asof 取不到 ⇒ 整轮拒绝 ----------

def test_weekly_empty_db_is_refused_and_runs_nothing(tmp_path, monkeypatch):
    """G7：空库 ⇒ exit 2、`steps=[]`、`job_runs` 记一行（不许猜日期跑）。"""
    db = _empty_db(tmp_path)
    _default_db(monkeypatch, db)
    rd = tmp_path / "reports"
    payload = chain.run_weekly(db_path=db, now=GREEN_NOW,
                               runner=ScriptedRunner(), report_dir=rd)
    assert payload["exit_code"] == EXIT_BLOCKED
    assert payload["steps"] == []
    assert payload["asof"] is None
    assert payload["ok"] is False
    assert [a["kind"] for a in payload["anomalies"]] == ["asof_unavailable"]
    # 回执两个落点都写了（文件 + `job_runs` 一行），详情里带着 blocked 这句
    assert (rd / "ops" / "latest-weekly.json").exists()
    c = connect(db)
    try:
        row = c.execute("SELECT status, detail FROM job_runs WHERE job_name='weekly'"
                        " ORDER BY run_id DESC").fetchone()
    finally:
        c.close()
    assert row is not None
    assert row[0] == "failed"                 # 见 §7 偏离表：schema 的 CHECK 只允许
    assert "blocked" in row[1]                #   running/ok/failed，写不出 'blocked'


def test_quarterly_empty_db_is_refused_and_runs_nothing(tmp_path, monkeypatch):
    db = _empty_db(tmp_path)
    _default_db(monkeypatch, db)
    payload = chain.run_quarterly(db_path=db, now=QUARTERLY_NOW, runner=ScriptedRunner(),
                                  report_dir=tmp_path / "reports")
    assert payload["exit_code"] == EXIT_BLOCKED
    assert payload["steps"] == [] and payload["asof"] is None


def test_missing_db_is_blocked(tmp_path):
    payload = chain.run_weekly(db_path=tmp_path / "nope.db", now=GREEN_NOW,
                               runner=ScriptedRunner())
    assert payload["exit_code"] == EXIT_BLOCKED and payload["steps"] == []
    assert payload["anomalies"][0]["kind"] == "db_missing"


# ---------- 主张 3/4：链顺序 + 汇总报告 ----------

def test_weekly_steps_run_in_the_declared_order(tmp_path, monkeypatch):
    runner = _scripted()
    payload, _ = _weekly(tmp_path, monkeypatch, runner=runner)
    assert [c["step"] for c in runner.calls] == [
        n for n in chain.WEEKLY_STEP_ORDER if n != chain.WEEKLY_REPORT_STEP]
    assert [s["name"] for s in payload["steps"]] == list(chain.WEEKLY_STEP_ORDER)
    assert payload["exit_code"] == EXIT_OK


def test_quarterly_steps_run_in_the_declared_order(tmp_path, monkeypatch):
    runner = _scripted()
    payload, _ = _quarterly(tmp_path, monkeypatch, runner=runner)
    assert [c["step"] for c in runner.calls] == [
        n for n in chain.QUARTERLY_STEP_ORDER if n != chain.QUARTERLY_REPORT_STEP]
    assert [s["name"] for s in payload["steps"]] == list(chain.QUARTERLY_STEP_ORDER)
    assert payload["exit_code"] == EXIT_OK


def test_weekly_review_step_is_not_blocking(tmp_path, monkeypatch):
    """非阻断步：`candidate review` 失败**不抬升**链的退出码，但**不静默**。"""
    runner = _scripted(codes={"candidate_review": 2})
    payload, _ = _weekly(tmp_path, monkeypatch, runner=runner)
    assert payload["exit_code"] == EXIT_OK
    assert any(a["kind"] == "step_failed" and a["step"] == "candidate_review"
               for a in payload["anomalies"])


def test_quarterly_scan_step_is_not_blocking(tmp_path, monkeypatch):
    """D6 第 5 步：`m2 attribute scan` 是**归因候选**，失败不判红整条链。"""
    assert chain.QUARTERLY_STEP_BY_NAME["m2_attribute_scan"].blocking is False
    payload, _ = _quarterly(tmp_path, monkeypatch,
                            runner=_scripted(codes={"m2_attribute_scan": 2}))
    assert payload["exit_code"] == EXIT_OK


def test_weekly_blocking_step_failure_breaks_the_chain(tmp_path, monkeypatch):
    runner = _scripted(codes={"candidate_run": 2})
    payload, _ = _weekly(tmp_path, monkeypatch, runner=runner)
    assert payload["exit_code"] == EXIT_BLOCKED
    assert payload["aborted"]["kind"] == "step_fatal"
    assert [c["step"] for c in runner.calls] == ["candidate_run"]   # 断在这里


def test_weekly_report_is_written_under_the_report_root(tmp_path, monkeypatch):
    """G5：报告落在 `<报告根>/weekly/<asof>-weekly.md`，**带「数据出处」列**。"""
    payload, rd = _weekly(tmp_path, monkeypatch)
    path = rd / "weekly" / f"{GREEN_ASOF}-weekly.md"
    assert path.exists()
    text = path.read_text(encoding="utf-8")
    assert "数据出处" in text
    assert GREEN_ASOF in text
    assert payload["steps"][-1]["name"] == chain.WEEKLY_REPORT_STEP
    assert payload["report"] == str(path)


def test_weekly_report_aggregates_m2_json_instead_of_recomputing(tmp_path, monkeypatch):
    """报告的读数来自 `m2 report` 的 stdout JSON（121 个交易日），不是自己算的。"""
    _, rd = _weekly(tmp_path, monkeypatch)
    text = (rd / "weekly" / f"{GREEN_ASOF}-weekly.md").read_text(encoding="utf-8")
    assert "121" in text and "gate_status" in text


def test_weekly_report_says_no_data_when_a_step_has_none(tmp_path, monkeypatch):
    """无数据写「无数据（原因）」—— 不许写 0、不许留空（ERROR_DIARY #50 同型）。"""
    payload, rd = _weekly(tmp_path, monkeypatch, runner=ScriptedRunner())
    text = (rd / "weekly" / f"{GREEN_ASOF}-weekly.md").read_text(encoding="utf-8")
    assert "无数据" in text
    assert "无数据（`m2_report`" in text          # 点名是哪一步、为什么
    assert payload["exit_code"] == EXIT_OK


def test_quarterly_report_has_the_plugin_sandbox_pending_section(tmp_path, monkeypatch):
    """G6：报告必须出现「待人工决定是否跑 `plugin sandbox`」段落（D7 不许静默跳过）。"""
    payload, rd = _quarterly(tmp_path, monkeypatch)
    path = rd / "quarterly" / f"{GREEN_ASOF}-quarterly.md"
    assert path.exists()
    text = path.read_text(encoding="utf-8")
    assert "待人工决定是否跑" in text
    assert "plugin sandbox" in text
    assert "数据出处" in text
    assert "plugin_list" in [s["name"] for s in payload["steps"]]


def test_quarterly_has_a_read_only_portfolio_step(tmp_path, monkeypatch):
    """D7.3：风控全量复核只出 `portfolio show` **原始读数**（不发明集中度算法）。"""
    assert "portfolio_show" in chain.QUARTERLY_STEP_ORDER
    step = chain.QUARTERLY_STEP_BY_NAME["portfolio_show"]
    assert step.blocking is False               # 它 exit 1 = 「要人来看」，不是链断
    assert step.supports_db is True
    _, rd = _quarterly(tmp_path, monkeypatch)
    text = (rd / "quarterly" / f"{GREEN_ASOF}-quarterly.md").read_text(encoding="utf-8")
    assert "只出" in text and "portfolio_show" in text
    assert "集中度" in text                     # 明说本档不做打分（D7）


def test_report_step_never_raises_when_the_dir_is_unwritable(tmp_path, monkeypatch):
    """渲染失败只记异常（exit 1），不把「链跑完了」改写成「链挂了」。"""
    blocked = tmp_path / "blocked"
    blocked.mkdir()
    blocked.chmod(0o500)                        # 只读目录 ⇒ 写不进报告
    try:
        payload, _ = _weekly(tmp_path, monkeypatch, report_dir=blocked)
        assert payload["exit_code"] == EXIT_ANOMALY
        assert payload["report"] is None
        assert any(a["kind"] == "report_failed" for a in payload["anomalies"])
    finally:
        blocked.chmod(0o700)


# ---------- 主张 4：汇总器本身（纯函数） ----------

def test_window_report_path_follows_the_kind(tmp_path):
    assert window_report.report_path("weekly", GREEN_ASOF,
                                     tmp_path).name == f"{GREEN_ASOF}-weekly.md"
    assert window_report.report_path("quarterly", GREEN_ASOF,
                                     tmp_path).parent.name == "quarterly"


def test_window_report_extracts_the_last_json_line():
    payload = window_report.extract_json_tail("正文\n" + json.dumps({"asof": "x"}))
    assert payload == {"asof": "x"}
    assert window_report.extract_json_tail("正文，没有 JSON") is None
    assert window_report.extract_json_tail("") is None


def test_window_report_reads_the_m2_json_from_stderr_first(tmp_path):
    """**实测修正**：`m2 report` 把 JSON 打在 **stderr** 末尾（`cmd_m2_report`）。

    两个流都给时以 stderr 为准；只给 stdout 时仍能取到（实现若挪去 stdout 不该判失败）。
    """
    assert window_report.m2_json(
        {"stderr_tail": "…\n" + M2_STDERR, "stdout_tail": "正文"})["n_sessions"] == 121
    assert window_report.m2_json(
        {"stderr_tail": "", "stdout_tail": "正文\n" + M2_STDERR})["n_sessions"] == 121
    assert window_report.m2_json(None) is None


# ---------- 主张 5：CLI ----------

def test_cli_weekly_prints_json_and_returns_the_exit_code(tmp_path, monkeypatch, capsys):
    db = _green(tmp_path)
    _default_db(monkeypatch, db)
    monkeypatch.setattr(chain, "default_runner", _scripted())
    rd = tmp_path / "reports"
    code = main(["ops", "weekly", "--now", GREEN_NOW, "--db", str(db),
                 "--report-dir", str(rd)])
    out = capsys.readouterr()
    payload = json.loads(out.out)
    assert code == EXIT_OK
    assert payload["job"] == "weekly" and payload["asof"] == GREEN_ASOF
    assert "weekly" in out.err                                   # 摘要行
    assert (rd / "weekly" / f"{GREEN_ASOF}-weekly.md").exists()


def test_cli_quarterly_returns_two_on_an_empty_db(tmp_path, monkeypatch, capsys):
    db = _empty_db(tmp_path)
    _default_db(monkeypatch, db)
    monkeypatch.setattr(chain, "default_runner", ScriptedRunner())
    code = main(["ops", "quarterly", "--now", QUARTERLY_NOW, "--db", str(db),
                 "--report-dir", str(tmp_path / "reports")])
    out = capsys.readouterr()
    assert code == EXIT_BLOCKED
    assert json.loads(out.out)["steps"] == []


def test_summary_line_knows_the_total_of_the_new_chains(tmp_path, monkeypatch):
    payload, _ = _weekly(tmp_path, monkeypatch)
    line = chain.summary_line(payload)
    assert f"steps={len(chain.WEEKLY_STEP_ORDER)}/{len(chain.WEEKLY_STEP_ORDER)}" in line


# ---------- 主张 5：调度（plist） ----------

def _calendars(tmp_path) -> dict[str, tuple[dict[str, int], ...]]:
    out = {}
    for spec in schedule.JOBS:
        path = schedule.write_plist(spec, plist_dir=tmp_path, logs=tmp_path / "logs")
        out[spec.name] = tuple(plistlib.loads(path.read_bytes())["StartCalendarInterval"])
    return out


def test_five_jobs_are_registered(tmp_path):
    assert schedule.JOB_NAMES == ("close", "patrol", "monthly", "weekly", "quarterly")


def test_weekly_fires_monday_1630(tmp_path):
    cal = _calendars(tmp_path)["weekly"]
    assert len(cal) == 5
    assert {(e["Hour"], e["Minute"], e["Weekday"]) for e in cal} == {(16, 30, 1)}


def test_quarterly_fires_on_the_three_disclosure_dates(tmp_path):
    """日期是法定披露截止日的次日（4/30、8/31、10/31 之后）；**时刻见 §7 偏离表**。"""
    cal = _calendars(tmp_path)["quarterly"]
    assert len(cal) == 3
    assert {(e["Month"], e["Day"], e["Hour"], e["Minute"]) for e in cal} == {
        (5, 1, 7, 0), (9, 1, 7, 0), (11, 1, 7, 0)}
    assert {e["Month"] for e in cal} == {5, 9, 11}
    assert all(e["Day"] == 1 for e in cal)


def test_quarterly_does_not_collide_with_patrol(tmp_path):
    """P89 §D4 写的 09:00 与 `patrol` 的 09:00 槽**真的撞点**（§7 偏离表第 2 条）。

    这条用例把「挪到 07:00」的理由钉成可复算的：09:00 会与巡检的 09:00 撞
    （巡检覆盖工作日 09:00–14:30，而季度 entry 不含 `Weekday` ⇒ 每天都命中），
    两条链同分钟起跑会同时写同一个 SQLite 库。
    """
    cal = {name: _trigger_keys(c) for name, c in _calendars(tmp_path).items()}
    assert not cal["quarterly"] & cal["patrol"]
    assert not cal["quarterly"] & cal["monthly"]
    assert not cal["weekly"] & cal["close"]
    # 反证：若季度真的放在 09:00，撞点会立刻出现（说明护栏不是空转）
    collide = _trigger_keys(({"Month": 5, "Day": 1, "Hour": 9, "Minute": 0},))
    assert collide & cal["patrol"]


def _trigger_keys(cal) -> set[tuple[int, int, int]]:
    """与 `test_ops_close.py` 同源：缺 `Weekday` = 通配（展开成 7 天）。"""
    out: set[tuple[int, int, int]] = set()
    for e in cal:
        weekdays = (e["Weekday"],) if "Weekday" in e else range(7)
        out |= {(d, e["Hour"], e["Minute"]) for d in weekdays}
    return out


def test_no_two_jobs_fire_at_the_same_moment_including_the_new_ones(tmp_path):
    """G8：任两条任务无同时刻槽（含既有三条）。5 条一起过通用护栏。"""
    by_job = {name: _trigger_keys(cal) for name, cal in _calendars(tmp_path).items()}
    assert len(by_job) == 5
    for a, b in itertools.combinations(sorted(by_job), 2):
        shared = by_job[a] & by_job[b]
        assert not shared, f"{a} 与 {b} 撞点：{sorted(shared)}"


def test_new_plists_do_not_run_at_load(tmp_path):
    for spec in schedule.JOBS:
        path = schedule.write_plist(spec, plist_dir=tmp_path, logs=tmp_path / "logs")
        assert plistlib.loads(path.read_bytes())["RunAtLoad"] is False


def test_cli_schedule_generate_prints_five_jobs(tmp_path, capsys):
    code = main(["ops", "schedule", "generate", "--plist-dir", str(tmp_path),
                 "--log-dir", str(tmp_path / "logs")])
    out = capsys.readouterr()
    payload = json.loads(out.out)
    assert code == EXIT_OK
    assert [p["job"] for p in payload] == list(schedule.JOB_NAMES)
    assert next(p for p in payload if p["job"] == "weekly")["triggers"] == 5
    assert next(p for p in payload if p["job"] == "quarterly")["triggers"] == 3


# ---------- 主张 5：页面（D10）与回执（D8） ----------

def test_ops_page_lists_all_five_jobs(tmp_path, monkeypatch):
    from stocklab.labweb.ops_data import OpsLab

    db = _green(tmp_path)
    view = OpsLab(db, report_dir=tmp_path / "reports").view(now=GREEN_NOW)
    assert [j["name"] for j in view["jobs"]] == list(schedule.JOB_NAMES)
    for name in ("weekly", "quarterly"):
        job = next(j for j in view["jobs"] if j["name"] == name)
        assert job["window"] and job["why"] and job["argv"]
        assert job["next_trigger"]                       # 推算得出下一槽
    from stocklab.labweb import ops_render

    html = ops_render.ops_page(view, base="", built_at=GREEN_NOW)
    for name in schedule.JOB_NAMES:
        assert f">{name}</" in html or name in html


def test_receipt_lands_for_both_new_jobs(tmp_path, monkeypatch):
    """回执落在 `<报告根>/ops/latest-<job>.json`（D8）——两条链各一个文件。"""
    db = _green(tmp_path)
    rd = tmp_path / "reports"
    _default_db(monkeypatch, db)
    chain.run_weekly(db_path=db, now=GREEN_NOW, runner=_scripted(), report_dir=rd)
    chain.run_quarterly(db_path=db, now=QUARTERLY_NOW, runner=_scripted(), report_dir=rd)
    assert journal.read_latest("weekly", rd)["job"] == "weekly"
    assert journal.read_latest("quarterly", rd)["job"] == "quarterly"
